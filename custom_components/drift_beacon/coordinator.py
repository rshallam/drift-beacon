"""WebSocket manager for Drift Beacon integration."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any, TypedDict

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    API_TIMEOUT,
    CONF_API_TOKEN,
    CONF_HOST,
    CONF_PORT,
    CONF_PROTOCOL,
    DOMAIN,
    EVENT_ACTIVITY_ARMED,
    EVENT_ACTIVITY_DISARMED,
    EVENT_SESSION_CHANGED,
    EVENT_SESSION_STARTED,
    EVENT_SESSION_STOPPED,
    WS_PATH,
    WS_RECONNECT_MAX_DELAY,
    WS_RECONNECT_MIN_DELAY,
)

_LOGGER = logging.getLogger(__name__)


class _SubscriptionError(Exception):
    """Raised when the server rejects or does not initialize a subscription."""


# ============================================================================
# Data Models
# ============================================================================


class ActivityProgress(TypedDict):
    """Activity progress data."""

    current: float
    target: float | None


class Activity(TypedDict):
    """Activity data from WebSocket API."""

    id: str
    name: str
    description: str | None
    category_id: str | None
    sort_order: int
    color: str  # hex color e.g. "#4A90D9"
    icon: str
    tracking_type: str  # "span" | "point"
    archived: bool
    unit: str | None
    progress: ActivityProgress


class Category(TypedDict):
    """Category data from WebSocket API."""

    id: str
    name: str
    color: str  # hex color
    icon: str
    sort_order: int


class Workspace(TypedDict):
    """Workspace data from WebSocket API."""

    id: str
    name: str


class LiveSession(TypedDict):
    """Live session data from WebSocket API."""

    id: str
    activity_id: str
    start_time: str  # ISO 8601


class ArmedActivity(TypedDict):
    """Armed activity data from WebSocket API."""

    activity_id: str
    armed_at: str  # ISO 8601


type DriftBeaconConfigEntry = ConfigEntry["DriftBeaconWebSocketManager"]


# ============================================================================
# Color Conversion
# ============================================================================


def hex_to_rgb(hex_color: str) -> list[int]:
    """Convert hex color string to RGB list. e.g. '#4A90D9' -> [74, 144, 217]."""
    h = hex_color.lstrip("#")
    return [int(h[i : i + 2], 16) for i in (0, 2, 4)]


# ============================================================================
# Parsing Helpers (camelCase → snake_case)
# ============================================================================


def _parse_activity(data: dict[str, Any]) -> Activity:
    """Parse a raw activity dict from the WebSocket API."""
    progress_raw = data.get("progress", {})
    return Activity(
        id=data["id"],
        name=data["name"],
        description=data.get("description"),
        category_id=data.get("categoryId"),
        sort_order=data.get("sortOrder", 0),
        color=data.get("color", "#808080"),
        icon=data.get("icon", "mdi:circle"),
        tracking_type=data.get("trackingType", "span"),
        archived=data.get("archived", False),
        unit=data.get("unit"),
        progress=ActivityProgress(
            current=progress_raw.get("current", 0),
            target=progress_raw.get("target"),
        ),
    )


def _parse_category(data: dict[str, Any]) -> Category:
    """Parse a raw category dict from the WebSocket API."""
    return Category(
        id=data["id"],
        name=data["name"],
        color=data.get("color", "#808080"),
        icon=data.get("icon", "mdi:circle"),
        sort_order=data.get("sortOrder", 0),
    )


def _parse_workspace(data: dict[str, Any]) -> Workspace:
    """Parse a raw workspace dict from the WebSocket API."""
    return Workspace(id=data["id"], name=data["name"])


def _parse_live_session(data: dict[str, Any]) -> LiveSession:
    """Parse a raw live session dict from the WebSocket API."""
    return LiveSession(
        id=data["id"],
        activity_id=data.get("activityId", ""),
        start_time=data.get("startTime", ""),
    )


def _parse_armed_activity(data: dict[str, Any]) -> ArmedActivity:
    """Parse a raw armed activity dict from the WebSocket API."""
    return ArmedActivity(
        activity_id=data.get("activityId", ""),
        armed_at=data.get("armedAt", ""),
    )


# ============================================================================
# WebSocket Manager
# ============================================================================


class DriftBeaconWebSocketManager:
    """Manages WebSocket connection and state for the Drift Beacon integration."""

    config_entry: DriftBeaconConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the WebSocket manager."""
        self.hass = hass
        self.config_entry = entry
        self.session_token: str = entry.data[CONF_API_TOKEN]
        self.host: str = entry.data[CONF_HOST]
        self.port: int = entry.data[CONF_PORT]
        self.protocol: str = entry.data.get(CONF_PROTOCOL, "https")

        # State per workspace (kept for future multi-workspace support)
        self._workspaces: list[Workspace] = []
        self._workspace_activities: dict[str, dict[str, Activity]] = {}
        self._workspace_categories: dict[str, dict[str, Category]] = {}
        self._workspace_live_sessions: dict[str, LiveSession | None] = {}
        self._workspace_armed_activities: dict[str, ArmedActivity | None] = {}

        # Connection
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._rpc_id: int = 0
        self._connection_task: asyncio.Task | None = None
        self._initial_ready: asyncio.Future[None] | None = None
        self._reconnect_attempt: int = 0
        self._intentional_disconnect: bool = False
        self._pending_responses: dict[int, asyncio.Future] = {}
        self._subscription_rpc_id: int | None = None

        # HA integration
        self._listeners: list[Callable] = []
        self._available: bool = False

    # ========================================================================
    # Public State Properties
    # ========================================================================

    @property
    def available(self) -> bool:
        """Return True when connected and data has been received."""
        return self._available

    @property
    def workspaces(self) -> list[Workspace]:
        """Return all workspaces."""
        return list(self._workspaces)

    @property
    def activities(self) -> list[Activity]:
        """Return a flat list of all activities across workspaces."""
        result: list[Activity] = []
        for acts in self._workspace_activities.values():
            result.extend(acts.values())
        return result

    def get_activity(self, activity_id: str) -> Activity | None:
        """Look up an activity by ID across all workspaces."""
        for acts in self._workspace_activities.values():
            if activity_id in acts:
                return acts[activity_id]
        return None

    def get_category(self, category_id: str | None) -> Category | None:
        """Look up a category by ID across all workspaces."""
        if category_id is None:
            return None
        for cats in self._workspace_categories.values():
            if category_id in cats:
                return cats[category_id]
        return None

    def get_workspace_for_activity(self, activity_id: str) -> Workspace | None:
        """Get the workspace that contains a given activity."""
        for ws_id, acts in self._workspace_activities.items():
            if activity_id in acts:
                return self._get_workspace_by_id(ws_id)
        return None

    def get_live_session(self, workspace_id: str) -> LiveSession | None:
        """Get the live session for a workspace."""
        return self._workspace_live_sessions.get(workspace_id)

    def get_armed_activity(self, workspace_id: str) -> ArmedActivity | None:
        """Get the armed activity for a workspace."""
        return self._workspace_armed_activities.get(workspace_id)

    def _get_workspace_by_id(self, workspace_id: str) -> Workspace | None:
        """Get a workspace by its ID."""
        for ws in self._workspaces:
            if ws["id"] == workspace_id:
                return ws
        return None

    # ========================================================================
    # Listener Management
    # ========================================================================

    @callback
    def async_add_listener(self, update_callback: Callable) -> Callable:
        """Add a listener and return a removal callable."""
        self._listeners.append(update_callback)

        @callback
        def remove_listener() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return remove_listener

    @callback
    def _notify_listeners(self) -> None:
        """Notify all registered listeners of a state change."""
        for listener in self._listeners:
            listener()

    # ========================================================================
    # Connection Lifecycle
    # ========================================================================

    async def async_connect(self) -> None:
        """Start the connection supervisor and wait for the first snapshot."""
        if self._connection_task and not self._connection_task.done():
            return

        self._intentional_disconnect = False
        self._initial_ready = self.hass.loop.create_future()
        self._connection_task = self.hass.async_create_task(
            self._connection_supervisor(), f"{DOMAIN}_ws_connection"
        )

        try:
            await self._initial_ready
        except asyncio.CancelledError:
            await self.async_disconnect()
            raise
        except (ConfigEntryAuthFailed, ConfigEntryNotReady):
            await self.async_disconnect()
            raise
        finally:
            self._initial_ready = None

    def _websocket_url(self) -> str:
        """Return the configured WebSocket URL."""
        ws_scheme = "wss" if self.protocol == "https" else "ws"
        return f"{ws_scheme}://{self.host}:{self.port}{WS_PATH}"

    async def _open_connection(self) -> None:
        """Open and authenticate a WebSocket connection."""
        ws_url = self._websocket_url()
        _LOGGER.debug("Connecting to WebSocket at %s", ws_url)
        http_session = async_get_clientsession(self.hass)

        try:
            self._ws = await http_session.ws_connect(
                ws_url,
                headers={"Authorization": f"Bearer {self.session_token}"},
                ssl=False,
                heartbeat=30,
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            )
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403):
                raise ConfigEntryAuthFailed(
                    "Drift Beacon API token is invalid, expired, "
                    "or no longer grants workspace access"
                ) from err
            raise

        _LOGGER.info("WebSocket connected to %s", ws_url)

    async def _setup_subscription(self) -> None:
        """Subscribe to the workspace scoped by the API key."""
        if self._ws is None or self._ws.closed:
            raise ConnectionError("WebSocket is not connected")

        rpc_id = self._next_rpc_id()
        self._subscription_rpc_id = rpc_id

        request = {
            "jsonrpc": "2.0",
            "method": "Subscribe",
            "params": {},
            "id": rpc_id,
        }

        _LOGGER.debug("Subscribing (rpc_id=%d)", rpc_id)
        await self._ws.send_json(request)

    async def _connection_supervisor(self) -> None:
        """Maintain the connection until the integration is unloaded."""
        while not self._intentional_disconnect:
            try:
                await self._open_connection()
                await self._setup_subscription()
                await asyncio.wait_for(
                    self._receive_until_snapshot(), timeout=API_TIMEOUT
                )

                if self._initial_ready and not self._initial_ready.done():
                    self._initial_ready.set_result(None)

                await self._listen_loop()
            except asyncio.CancelledError:
                raise
            except ConfigEntryAuthFailed as err:
                self._intentional_disconnect = True
                await self._cleanup_connection(err)
                if self._initial_ready and not self._initial_ready.done():
                    self._initial_ready.set_exception(err)
                else:
                    _LOGGER.warning(
                        "Drift Beacon API token is invalid, expired, or no longer "
                        "grants workspace access; reauthentication is required"
                    )
                    self.config_entry.async_start_reauth(self.hass)
                return
            except Exception as err:  # noqa: BLE001
                await self._cleanup_connection(
                    ConnectionError(f"WebSocket connection lost: {err}")
                )

                if self._initial_ready and not self._initial_ready.done():
                    self._initial_ready.set_exception(
                        ConfigEntryNotReady(
                            f"Unable to connect to Drift Beacon: {err}"
                        )
                    )
                    return

                delay = self._next_reconnect_delay()
                _LOGGER.warning(
                    "WebSocket connection lost; reconnect attempt %d in %.1fs: %s",
                    self._reconnect_attempt,
                    delay,
                    err,
                )
                await asyncio.sleep(delay)
            finally:
                if self._intentional_disconnect:
                    await self._cleanup_connection(
                        ConnectionError("WebSocket disconnected")
                    )

    def _next_reconnect_delay(self) -> float:
        """Return the next capped exponential delay."""
        delay = min(
            WS_RECONNECT_MIN_DELAY * (2**self._reconnect_attempt),
            WS_RECONNECT_MAX_DELAY,
        )
        self._reconnect_attempt += 1
        return float(delay)

    async def async_disconnect(self) -> None:
        """Gracefully disconnect from the WebSocket."""
        self._intentional_disconnect = True

        if self._ws and not self._ws.closed:
            # Best-effort unsubscribe
            try:
                rpc_id = self._next_rpc_id()
                await asyncio.wait_for(
                    self._ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "Unsubscribe",
                            "params": {},
                            "id": rpc_id,
                        }
                    ),
                    timeout=2,
                )
            except Exception:  # noqa: BLE001
                pass

        connection_task = self._connection_task
        if connection_task and connection_task is not asyncio.current_task():
            connection_task.cancel()
            with suppress(asyncio.CancelledError):
                await connection_task
        self._connection_task = None

        await self._cleanup_connection(ConnectionError("WebSocket disconnected"))

    async def _cleanup_connection(self, error: Exception) -> None:
        """Close the socket and fail requests owned by this connection."""
        was_available = self._available
        self._available = False

        ws = self._ws
        self._ws = None
        if ws and not ws.closed:
            try:
                await ws.close()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Error while closing WebSocket: %s", err)

        for future in self._pending_responses.values():
            if not future.done():
                future.set_exception(error)
        self._pending_responses.clear()
        self._subscription_rpc_id = None

        if was_available:
            self._notify_listeners()

    # ========================================================================
    # JSON-RPC Client
    # ========================================================================

    def _next_rpc_id(self) -> int:
        """Get the next RPC request ID."""
        self._rpc_id += 1
        return self._rpc_id

    async def _send_rpc(self, method: str, params: dict | None = None) -> Any:
        """Send a JSON-RPC request and await the non-stream response."""
        if self._ws is None or self._ws.closed:
            raise ConnectionError("WebSocket is not connected")

        rpc_id = self._next_rpc_id()
        future: asyncio.Future = self.hass.loop.create_future()
        self._pending_responses[rpc_id] = future

        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": rpc_id,
        }

        _LOGGER.debug("Sending RPC: %s (id=%d)", method, rpc_id)
        try:
            await self._ws.send_json(request)
            return await asyncio.wait_for(future, timeout=API_TIMEOUT)
        finally:
            self._pending_responses.pop(rpc_id, None)
            if not future.done():
                future.cancel()

    # ========================================================================
    # Message Listening
    # ========================================================================

    async def _listen_loop(self) -> None:
        """Main WebSocket message receive loop."""
        if self._ws is None:
            raise ConnectionError("WebSocket is not connected")

        async for msg in self._ws:
            self._process_websocket_message(msg)

        raise ConnectionError("WebSocket closed")

    async def _receive_until_snapshot(self) -> None:
        """Receive subscription messages until a valid snapshot arrives."""
        if self._ws is None:
            raise ConnectionError("WebSocket is not connected")

        while not self._available:
            msg = await self._ws.receive()
            self._process_websocket_message(msg)

    def _process_websocket_message(self, msg: aiohttp.WSMessage) -> None:
        """Process one WebSocket frame or raise when the connection ends."""
        if msg.type == aiohttp.WSMsgType.TEXT:
            self._handle_message(msg.data)
            return
        if msg.type == aiohttp.WSMsgType.ERROR:
            error = self._ws.exception() if self._ws else None
            raise ConnectionError(f"WebSocket error: {error}")
        if msg.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED,
        ):
            raise ConnectionError("WebSocket closed")

    @callback
    def _handle_message(self, raw: str) -> None:
        """Parse a JSON-RPC response and route it."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            _LOGGER.warning("Received non-JSON WebSocket message")
            return

        # Check for errors
        if "error" in data:
            rpc_id = data.get("id")
            error_msg = data["error"].get("message", "Unknown error")
            _LOGGER.error("RPC error (id=%s): %s", rpc_id, error_msg)

            if rpc_id and rpc_id in self._pending_responses:
                self._pending_responses.pop(rpc_id).set_exception(
                    Exception(f"RPC error: {error_msg}")
                )
            elif rpc_id == self._subscription_rpc_id:
                raise _SubscriptionError(error_msg)
            return

        is_chunked = data.get("chunk", False)
        rpc_id = data.get("id")

        if is_chunked:
            # Stream messages from Subscribe
            if rpc_id != self._subscription_rpc_id:
                _LOGGER.debug(
                    "Received chunked message for unknown rpc_id=%s", rpc_id
                )
                return

            results = data.get("result", [])
            for msg in results:
                self._apply_stream_message(msg)
            self._notify_listeners()
        else:
            # Non-stream RPC response (StartSession, StopSession, etc.)
            if rpc_id and rpc_id in self._pending_responses:
                self._pending_responses.pop(rpc_id).set_result(
                    data.get("result")
                )

    # ========================================================================
    # Stream Message Handlers
    # ========================================================================

    def _apply_stream_message(self, msg: dict[str, Any]) -> None:
        """Apply a single stream message to local state."""
        tag = msg.get("_tag")
        if not tag:
            return

        handler = {
            "Snapshot": self._apply_snapshot,
            "ActivityCreated": self._apply_activity_created,
            "ActivityUpdated": self._apply_activity_updated,
            "ActivityDeleted": self._apply_activity_deleted,
            "CategoryCreated": self._apply_category_created,
            "CategoryUpdated": self._apply_category_updated,
            "CategoryDeleted": self._apply_category_deleted,
            "SessionStarted": self._apply_session_started,
            "SessionEnded": self._apply_session_ended,
            "ArmedActivityChanged": self._apply_armed_activity_changed,
        }.get(tag)

        if handler:
            handler(msg)
        else:
            _LOGGER.debug("Unknown stream message tag: %s", tag)

    def _apply_snapshot(self, msg: dict[str, Any]) -> None:
        """Apply a Snapshot message — full state replacement for the workspace."""
        workspace_id = msg.get("workspaceId", "")
        workspace_name = msg.get("workspaceName", workspace_id)

        # Populate workspace list from Snapshot
        self._workspaces = [Workspace(id=workspace_id, name=workspace_name)]

        activities_raw = msg.get("activities", [])
        categories_raw = msg.get("categories", [])
        # Snapshot carries a `liveSessions` array; the API token scopes Subscribe
        # to one workspace/user, so at most one entry is relevant.
        live_sessions_raw = msg.get("liveSessions", [])
        live_session_raw = live_sessions_raw[0] if live_sessions_raw else None
        armed_activity_raw = msg.get("armedActivity")

        # The API token scopes Subscribe to one workspace, so each Snapshot
        # replaces all state retained from the previous connection.
        self._workspace_activities.clear()
        self._workspace_categories.clear()
        self._workspace_live_sessions.clear()
        self._workspace_armed_activities.clear()

        self._workspace_activities[workspace_id] = {
            a["id"]: _parse_activity(a) for a in activities_raw
        }
        self._workspace_categories[workspace_id] = {
            c["id"]: _parse_category(c) for c in categories_raw
        }
        self._workspace_live_sessions[workspace_id] = (
            _parse_live_session(live_session_raw) if live_session_raw else None
        )
        self._workspace_armed_activities[workspace_id] = (
            _parse_armed_activity(armed_activity_raw) if armed_activity_raw else None
        )

        self._available = True
        self._reconnect_attempt = 0

        _LOGGER.debug(
            "Snapshot received for workspace %s (%s): %d activities, %d categories, live_session=%s, armed_activity=%s",
            workspace_name,
            workspace_id,
            len(activities_raw),
            len(categories_raw),
            "yes" if live_session_raw else "no",
            armed_activity_raw.get("activityId") if armed_activity_raw else "none",
        )

    def _apply_activity_created(self, msg: dict[str, Any]) -> None:
        activity = _parse_activity(msg["activity"])
        ws_id = self._workspaces[0]["id"] if self._workspaces else ""
        self._workspace_activities.setdefault(ws_id, {})[
            activity["id"]
        ] = activity
        _LOGGER.debug("Activity created: %s (%s)", activity["name"], activity["id"])

    def _apply_activity_updated(self, msg: dict[str, Any]) -> None:
        activity = _parse_activity(msg["activity"])
        ws_id = self._workspaces[0]["id"] if self._workspaces else ""
        self._workspace_activities.setdefault(ws_id, {})[
            activity["id"]
        ] = activity
        _LOGGER.debug("Activity updated: %s (%s)", activity["name"], activity["id"])

    def _apply_activity_deleted(self, msg: dict[str, Any]) -> None:
        activity_id = msg["activityId"]
        ws_id = self._workspaces[0]["id"] if self._workspaces else ""
        acts = self._workspace_activities.get(ws_id, {})
        acts.pop(activity_id, None)
        _LOGGER.debug("Activity deleted: %s", activity_id)

    def _apply_category_created(self, msg: dict[str, Any]) -> None:
        category = _parse_category(msg["category"])
        ws_id = self._workspaces[0]["id"] if self._workspaces else ""
        self._workspace_categories.setdefault(ws_id, {})[
            category["id"]
        ] = category

    def _apply_category_updated(self, msg: dict[str, Any]) -> None:
        category = _parse_category(msg["category"])
        ws_id = self._workspaces[0]["id"] if self._workspaces else ""
        self._workspace_categories.setdefault(ws_id, {})[
            category["id"]
        ] = category

    def _apply_category_deleted(self, msg: dict[str, Any]) -> None:
        cat_id = msg["categoryId"]
        ws_id = self._workspaces[0]["id"] if self._workspaces else ""
        cats = self._workspace_categories.get(ws_id, {})
        cats.pop(cat_id, None)

    def _apply_session_started(self, msg: dict[str, Any]) -> None:
        workspace = self._workspaces[0] if self._workspaces else None
        ws_id = workspace["id"] if workspace else ""
        prev_session = self._workspace_live_sessions.get(ws_id)
        new_session = _parse_live_session(msg["session"])
        self._workspace_live_sessions[ws_id] = new_session

        activity = self.get_activity(new_session["activity_id"])

        if activity and workspace:
            category = self.get_category(activity.get("category_id"))

            event_data: dict[str, Any] = {
                "activity_id": activity["id"],
                "activity_name": activity["name"],
                "color": hex_to_rgb(activity["color"]),
                "icon": activity["icon"],
                "category_id": activity.get("category_id"),
                "category_name": category["name"] if category else None,
                "category_icon": category["icon"] if category else None,
                "category_color": hex_to_rgb(category["color"]) if category else None,
                "workspace_id": workspace["id"],
                "workspace_name": workspace["name"],
                "session_start_time": new_session["start_time"],
            }

            if prev_session and prev_session["activity_id"] != new_session["activity_id"]:
                # Session changed (different activity)
                prev_activity = self.get_activity(prev_session["activity_id"])
                event_data["previous_activity_id"] = prev_session["activity_id"]
                event_data["previous_activity_name"] = (
                    prev_activity["name"] if prev_activity else None
                )
                self.hass.bus.async_fire(EVENT_SESSION_CHANGED, event_data)
                _LOGGER.debug(
                    "Session changed: %s -> %s",
                    prev_activity["name"] if prev_activity else "unknown",
                    activity["name"],
                )
            else:
                self.hass.bus.async_fire(EVENT_SESSION_STARTED, event_data)
                _LOGGER.debug("Session started: %s", activity["name"])

    def _apply_session_ended(self, msg: dict[str, Any]) -> None:
        session_id = msg["sessionId"]
        activity_id = msg["activityId"]
        workspace = self._workspaces[0] if self._workspaces else None
        ws_id = workspace["id"] if workspace else ""
        self._workspace_live_sessions[ws_id] = None

        activity = self.get_activity(activity_id)

        if activity and workspace:
            # Include the currently armed activity (if any) so automations can
            # fall back to armed lighting instead of turning off outright —
            # session lighting always took priority while the session was live.
            armed = self.get_armed_activity(workspace["id"])
            armed_activity = self.get_activity(armed["activity_id"]) if armed else None

            self.hass.bus.async_fire(
                EVENT_SESSION_STOPPED,
                {
                    "activity_id": activity_id,
                    "activity_name": activity["name"],
                    "workspace_id": workspace["id"],
                    "workspace_name": workspace["name"],
                    "armed_activity_id": armed["activity_id"] if armed else None,
                    "armed_activity_name": (
                        armed_activity["name"] if armed_activity else None
                    ),
                    "armed_color": (
                        hex_to_rgb(armed_activity["color"]) if armed_activity else None
                    ),
                },
            )
            _LOGGER.debug("Session ended: %s (session %s)", activity["name"], session_id)

    def _apply_armed_activity_changed(self, msg: dict[str, Any]) -> None:
        """Apply the current authenticated user's armed activity.

        Fires `drift_beacon_activity_armed`/`_disarmed` for automations, tagged
        with whether a live session is active right now. This lets lighting
        automations suppress the armed light while a session is showing —
        `Activity.startSession` implicitly disarms, so starting a session on
        an already-armed activity fires both a session-started and a
        disarmed message; `has_live_session` here is read fresh, after
        `_apply_session_started` would already have updated
        `_workspace_live_sessions`, so the disarm is correctly seen as a
        no-op for lighting purposes. Armed state itself is still recorded
        here regardless of `has_live_session`, so if the session later ends,
        `_apply_session_ended` finds this activity still armed and hands
        lighting back to it instead of turning off.
        """
        workspace = self._workspaces[0] if self._workspaces else None
        if workspace is None:
            return

        workspace_id = workspace["id"]
        previous = self._workspace_armed_activities.get(workspace_id)
        armed_activity_raw = msg.get("armedActivity")
        new = _parse_armed_activity(armed_activity_raw) if armed_activity_raw else None
        self._workspace_armed_activities[workspace_id] = new

        _LOGGER.debug(
            "Armed activity changed: %s",
            armed_activity_raw.get("activityId") if armed_activity_raw else "none",
        )

        previous_activity_id = previous["activity_id"] if previous else None
        new_activity_id = new["activity_id"] if new else None
        if previous_activity_id == new_activity_id:
            return

        has_live_session = self.get_live_session(workspace_id) is not None

        if new is not None:
            activity = self.get_activity(new["activity_id"])
            if activity is None:
                return
            category = self.get_category(activity.get("category_id"))
            self.hass.bus.async_fire(
                EVENT_ACTIVITY_ARMED,
                {
                    "activity_id": activity["id"],
                    "activity_name": activity["name"],
                    "color": hex_to_rgb(activity["color"]),
                    "icon": activity["icon"],
                    "category_id": activity.get("category_id"),
                    "category_name": category["name"] if category else None,
                    "category_icon": category["icon"] if category else None,
                    "category_color": (
                        hex_to_rgb(category["color"]) if category else None
                    ),
                    "workspace_id": workspace["id"],
                    "workspace_name": workspace["name"],
                    "armed_at": new["armed_at"],
                    "has_live_session": has_live_session,
                },
            )
        elif previous is not None:
            previous_activity = self.get_activity(previous["activity_id"])
            self.hass.bus.async_fire(
                EVENT_ACTIVITY_DISARMED,
                {
                    "activity_id": previous["activity_id"],
                    "activity_name": (
                        previous_activity["name"] if previous_activity else None
                    ),
                    "workspace_id": workspace["id"],
                    "workspace_name": workspace["name"],
                    "has_live_session": has_live_session,
                },
            )

    # ========================================================================
    # Actions
    # ========================================================================

    async def start_session(self, activity_id: str) -> bool:
        """Start a span session for an activity via RPC."""
        _LOGGER.debug("Starting session for activity %s", activity_id)
        try:
            await self._send_rpc(
                "StartSession",
                {"activityId": activity_id},
            )
            return True
        except Exception as err:
            _LOGGER.error("Failed to start session: %s", err)
            return False

    async def stop_session(self, activity_id: str | None = None) -> bool:
        """Stop a live session via RPC.

        When ``activity_id`` is given, stop that activity's live session. When
        omitted, stop whatever session is live for this user (idempotent — the
        server returns success even if nothing is running).
        """
        _LOGGER.debug("Stopping session for activity %s", activity_id or "<any>")
        try:
            params = {"activityId": activity_id} if activity_id is not None else {}
            await self._send_rpc("StopSession", params)
            return True
        except Exception as err:
            _LOGGER.error("Failed to stop session: %s", err)
            return False

    async def mark_activity(self, activity_id: str) -> bool:
        """Mark a point activity via RPC."""
        _LOGGER.debug("Marking activity %s", activity_id)
        try:
            await self._send_rpc(
                "Mark",
                {"activityId": activity_id},
            )
            return True
        except Exception as err:
            _LOGGER.error("Failed to mark activity: %s", err)
            return False

    async def arm_activity(self, activity_id: str) -> bool:
        """Arm an activity via RPC."""
        _LOGGER.debug("Arming activity %s", activity_id)
        try:
            await self._send_rpc("ArmActivity", {"activityId": activity_id})
            return True
        except Exception as err:
            _LOGGER.error("Failed to arm activity: %s", err)
            return False

    async def disarm_activity(self, activity_id: str | None = None) -> bool:
        """Disarm an activity, or whichever activity is armed when omitted."""
        _LOGGER.debug("Disarming activity %s", activity_id or "<any>")
        try:
            params = {"activityId": activity_id} if activity_id is not None else {}
            await self._send_rpc("DisarmActivity", params)
            return True
        except Exception as err:
            _LOGGER.error("Failed to disarm activity: %s", err)
            return False
