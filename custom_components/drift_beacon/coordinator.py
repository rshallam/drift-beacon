"""WebSocket manager for Drift Beacon integration."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, TypedDict

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    API_TIMEOUT,
    CONF_API_TOKEN,
    CONF_HOST,
    CONF_PORT,
    CONF_PROTOCOL,
    DOMAIN,
    EVENT_SESSION_CHANGED,
    EVENT_SESSION_STARTED,
    EVENT_SESSION_STOPPED,
    WS_PATH,
    WS_RECONNECT_MAX_DELAY,
    WS_RECONNECT_MIN_DELAY,
)

_LOGGER = logging.getLogger(__name__)


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

        # Connection
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._rpc_id: int = 0
        self._listen_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
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
        """Connect to the WebSocket, list workspaces, and subscribe."""
        self._intentional_disconnect = False

        ws_scheme = "wss" if self.protocol == "https" else "ws"
        ws_url = f"{ws_scheme}://{self.host}:{self.port}{WS_PATH}"

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
        except aiohttp.WSServerHandshakeError as err:
            if err.status == 401:
                raise ConfigEntryAuthFailed(
                    "Session token expired or invalid"
                ) from err
            _LOGGER.error("WebSocket handshake failed: %s", err)
            self._schedule_reconnect()
            return
        except (aiohttp.ClientError, OSError) as err:
            _LOGGER.error("Failed to connect WebSocket: %s", err)
            self._schedule_reconnect()
            return

        _LOGGER.info("WebSocket connected to %s", ws_url)

        # Start the listen loop
        self._listen_task = self.hass.async_create_task(
            self._listen_loop(), f"{DOMAIN}_ws_listen"
        )

        # List workspaces and subscribe
        try:
            await self._setup_subscription()
        except Exception as err:
            _LOGGER.error("Failed to set up subscriptions: %s", err)
            self._schedule_reconnect()

    async def _setup_subscription(self) -> None:
        """Subscribe to the workspace scoped by the API key."""
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

    async def async_disconnect(self) -> None:
        """Gracefully disconnect from the WebSocket."""
        self._intentional_disconnect = True

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None

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
            except (asyncio.TimeoutError, Exception):
                pass

            await self._ws.close()

        self._ws = None

        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            self._listen_task = None

        # Clear pending responses
        for future in self._pending_responses.values():
            if not future.done():
                future.cancel()
        self._pending_responses.clear()
        self._subscription_rpc_id = None

        self._available = False
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
        await self._ws.send_json(request)

        try:
            return await asyncio.wait_for(future, timeout=API_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending_responses.pop(rpc_id, None)
            raise

    # ========================================================================
    # Message Listening
    # ========================================================================

    async def _listen_loop(self) -> None:
        """Main WebSocket message receive loop."""
        if self._ws is None:
            return

        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.error(
                        "WebSocket error: %s", self._ws.exception()
                    )
                    break
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
        except asyncio.CancelledError:
            return
        except Exception as err:
            _LOGGER.error("WebSocket listen error: %s", err)

        # Connection lost
        if not self._intentional_disconnect:
            _LOGGER.warning("WebSocket connection lost")
            self._available = False
            self._notify_listeners()
            self._schedule_reconnect()

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
        live_session_raw = msg.get("liveSession")

        self._workspace_activities[workspace_id] = {
            a["id"]: _parse_activity(a) for a in activities_raw
        }
        self._workspace_categories[workspace_id] = {
            c["id"]: _parse_category(c) for c in categories_raw
        }
        self._workspace_live_sessions[workspace_id] = (
            _parse_live_session(live_session_raw) if live_session_raw else None
        )

        self._available = True
        self._reconnect_attempt = 0

        _LOGGER.debug(
            "Snapshot received for workspace %s (%s): %d activities, %d categories, live_session=%s",
            workspace_name,
            workspace_id,
            len(activities_raw),
            len(categories_raw),
            "yes" if live_session_raw else "no",
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
            self.hass.bus.async_fire(
                EVENT_SESSION_STOPPED,
                {
                    "activity_id": activity_id,
                    "activity_name": activity["name"],
                    "workspace_id": workspace["id"],
                    "workspace_name": workspace["name"],
                },
            )
            _LOGGER.debug("Session ended: %s (session %s)", activity["name"], session_id)

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

    async def stop_session(self, activity_id: str) -> bool:
        """Stop the live session for an activity via RPC."""
        _LOGGER.debug("Stopping session for activity %s", activity_id)
        try:
            await self._send_rpc(
                "StopSession",
                {"activityId": activity_id},
            )
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

    # ========================================================================
    # Reconnection
    # ========================================================================

    def _schedule_reconnect(self) -> None:
        """Schedule a reconnection with exponential backoff."""
        if self._intentional_disconnect:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return

        self._reconnect_attempt += 1
        delay = min(
            WS_RECONNECT_MIN_DELAY * (2 ** self._reconnect_attempt),
            WS_RECONNECT_MAX_DELAY,
        )

        _LOGGER.info(
            "Scheduling reconnect attempt %d in %.1fs",
            self._reconnect_attempt,
            delay,
        )
        self._reconnect_task = self.hass.async_create_task(
            self._reconnect(delay), f"{DOMAIN}_ws_reconnect"
        )

    async def _reconnect(self, delay: float) -> None:
        """Wait and then reconnect."""
        await asyncio.sleep(delay)

        if self._intentional_disconnect:
            return

        # Clean up old connection state
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            self._listen_task = None

        for future in self._pending_responses.values():
            if not future.done():
                future.cancel()
        self._pending_responses.clear()
        self._subscription_rpc_id = None

        await self.async_connect()
