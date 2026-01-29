"""Data update coordinator for Drift Beacon."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any, TypedDict

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_ACTIVITIES,
    API_LIVE_SESSION,
    API_START_SESSION,
    API_STOP_SESSION,
    API_TIMEOUT,
    CONF_HOST,
    CONF_PORT,
    CONF_PROTOCOL,
    CONF_SESSION_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_SESSION_CHANGED,
    EVENT_SESSION_STARTED,
    EVENT_SESSION_STOPPED,
)

_LOGGER = logging.getLogger(__name__)


class Activity(TypedDict):
    """Activity data structure."""

    id: str
    name: str
    description: str | None
    category_id: str | None
    category_name: str | None
    category_icon: str | None
    category_color: list[int] | None
    sort_order: int
    color: list[int]
    icon: str
    workspace_id: str
    workspace_name: str


class Session(TypedDict):
    """Session data structure."""

    id: str
    activity_id: str
    start_time: str
    end_time: str | None
    workspace_id: str
    workspace_name: str


class DriftBeaconData(TypedDict):
    """Drift Beacon coordinator data structure."""

    activities: list[Activity]
    live_sessions: list[Session]


type DriftBeaconConfigEntry = ConfigEntry[DriftBeaconDataUpdateCoordinator]


class DriftBeaconDataUpdateCoordinator(DataUpdateCoordinator[DriftBeaconData]):
    """Class to manage fetching Drift Beacon data with authentication."""

    config_entry: DriftBeaconConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self.session_token = entry.data[CONF_SESSION_TOKEN]
        self.host = entry.data[CONF_HOST]
        self.port = entry.data[CONF_PORT]
        self.protocol = entry.data.get(CONF_PROTOCOL, "https")  # Default to https for backward compat
        self.base_url = f"{self.protocol}://{self.host}:{self.port}"
        self.session = async_get_clientsession(hass)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

    async def _make_authenticated_request(
        self, method: str, endpoint: str, **kwargs: Any
    ) -> Any:
        """Make authenticated API request with Bearer token."""
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.session_token}"

        url = f"{self.base_url}{endpoint}"

        try:
            async with self.session.request(
                method,
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                ssl=False,  # Disable SSL verification for local add-on with self-signed certs
                **kwargs,
            ) as response:
                if response.status == 401:
                    raise ConfigEntryAuthFailed(
                        "Authentication failed. Session token expired or invalid."
                    )
                response.raise_for_status()
                return await response.json()
        except ConfigEntryAuthFailed:
            # Re-raise auth failures to trigger reauth flow
            raise
        except aiohttp.ClientResponseError as err:
            _LOGGER.error(
                "API request failed: %s %s - HTTP %s", method, endpoint, err.status
            )
            raise
        except Exception as err:
            _LOGGER.error("Request error for %s %s: %s", method, endpoint, err)
            raise

    async def _async_update_data(self) -> DriftBeaconData:
        """Fetch data from Drift Beacon API with authentication."""
        try:
            # Store old sessions before fetching new data (for event firing)
            old_sessions = self.data.get("live_sessions", []) if self.data else []

            # Fetch activities and live sessions (now returns array)
            activities = await self._make_authenticated_request("GET", API_ACTIVITIES)
            live_sessions = await self._make_authenticated_request(
                "GET", API_LIVE_SESSION
            )

            new_data = {
                "activities": activities,
                "live_sessions": live_sessions,
            }

            # Fire events based on session changes
            self._fire_session_events(old_sessions, live_sessions, activities)

            return new_data

        except ConfigEntryAuthFailed:
            # Re-raise to trigger reauth flow
            raise
        except aiohttp.ClientError as err:
            raise UpdateFailed(
                f"Error communicating with Drift Beacon API: {err}"
            ) from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def start_session(self, activity_id: str, workspace_id: str) -> bool:
        """Start a session for an activity."""
        _LOGGER.debug("Starting session for activity %s in workspace %s", activity_id, workspace_id)

        try:
            await self._make_authenticated_request(
                "POST",
                API_START_SESSION,
                json={"activityId": activity_id, "workspaceId": workspace_id},
            )

            _LOGGER.info("Successfully started session for activity %s", activity_id)

            # Immediately refresh data to update entity states
            await self.async_request_refresh()
            return True

        except ConfigEntryAuthFailed:
            _LOGGER.error("Authentication failed while starting session")
            # Re-raise to trigger reauth flow
            raise
        except aiohttp.ClientResponseError as err:
            _LOGGER.error(
                "Failed to start session for activity %s: HTTP %s",
                activity_id,
                err.status,
            )
            return False
        except Exception as err:
            _LOGGER.error(
                "Failed to start session for activity %s: %s", activity_id, err
            )
            return False

    async def stop_session(self, activity_id: str, workspace_id: str) -> bool:
        """Stop the current session."""
        _LOGGER.debug("Stopping session for activity %s in workspace %s", activity_id, workspace_id)

        try:
            await self._make_authenticated_request(
                "POST",
                API_STOP_SESSION,
                json={"activityId": activity_id, "workspaceId": workspace_id},
            )

            _LOGGER.info("Successfully stopped session for activity %s", activity_id)

            # Immediately refresh data to update entity states
            await self.async_request_refresh()
            return True

        except ConfigEntryAuthFailed:
            _LOGGER.error("Authentication failed while stopping session")
            # Re-raise to trigger reauth flow
            raise
        except aiohttp.ClientResponseError as err:
            _LOGGER.error(
                "Failed to stop session for activity %s: HTTP %s",
                activity_id,
                err.status,
            )
            return False
        except Exception as err:
            _LOGGER.error(
                "Failed to stop session for activity %s: %s", activity_id, err
            )
            return False

    def _fire_session_events(
        self,
        old_sessions: list[Session],
        new_sessions: list[Session],
        activities: list[Activity],
    ) -> None:
        """Fire events when session state changes across all workspaces."""

        def get_activity(activity_id: str | None) -> Activity | None:
            """Helper to find activity by ID."""
            if activity_id is None:
                return None
            for activity in activities:
                if activity["id"] == activity_id:
                    return activity
            return None

        # Create lookup by session ID for easier comparison
        old_sessions_by_id = {s["id"]: s for s in old_sessions}
        new_sessions_by_id = {s["id"]: s for s in new_sessions}

        # Detect new sessions (started)
        for new_session in new_sessions:
            if new_session["id"] not in old_sessions_by_id:
                activity = get_activity(new_session.get("activity_id"))
                if activity:
                    _LOGGER.debug(
                        "Firing session_started event for activity %s in workspace %s",
                        activity["name"],
                        new_session["workspace_name"]
                    )
                    self.hass.bus.async_fire(
                        EVENT_SESSION_STARTED,
                        {
                            "activity_id": activity["id"],
                            "activity_name": activity["name"],
                            "color": activity["color"],
                            "icon": activity["icon"],
                            "category_id": activity.get("category_id"),
                            "category_name": activity.get("category_name"),
                            "category_icon": activity.get("category_icon"),
                            "category_color": activity.get("category_color"),
                            "workspace_id": new_session["workspace_id"],
                            "workspace_name": new_session["workspace_name"],
                            "session_start_time": new_session["start_time"],
                        },
                    )

        # Detect stopped sessions
        for old_session in old_sessions:
            if old_session["id"] not in new_sessions_by_id:
                activity = get_activity(old_session.get("activity_id"))
                if activity:
                    _LOGGER.debug(
                        "Firing session_stopped event for activity %s in workspace %s",
                        activity["name"],
                        old_session["workspace_name"]
                    )
                    self.hass.bus.async_fire(
                        EVENT_SESSION_STOPPED,
                        {
                            "activity_id": old_session["activity_id"],
                            "activity_name": activity["name"],
                            "workspace_id": old_session["workspace_id"],
                            "workspace_name": old_session["workspace_name"],
                        },
                    )

        # Detect changed sessions (same session ID, different activity)
        for session_id in old_sessions_by_id:
            if session_id in new_sessions_by_id:
                old_session = old_sessions_by_id[session_id]
                new_session = new_sessions_by_id[session_id]

                if old_session.get("activity_id") != new_session.get("activity_id"):
                    old_activity = get_activity(old_session.get("activity_id"))
                    new_activity = get_activity(new_session.get("activity_id"))

                    if new_activity:
                        _LOGGER.debug(
                            "Firing session_changed event in workspace %s: %s -> %s",
                            new_session["workspace_name"],
                            old_activity["name"] if old_activity else "unknown",
                            new_activity["name"],
                        )
                        self.hass.bus.async_fire(
                            EVENT_SESSION_CHANGED,
                            {
                                "activity_id": new_activity["id"],
                                "activity_name": new_activity["name"],
                                "color": new_activity["color"],
                                "icon": new_activity["icon"],
                                "category_id": new_activity.get("category_id"),
                                "category_name": new_activity.get("category_name"),
                                "category_icon": new_activity.get("category_icon"),
                                "category_color": new_activity.get("category_color"),
                                "workspace_id": new_session["workspace_id"],
                                "workspace_name": new_session["workspace_name"],
                                "session_start_time": new_session["start_time"],
                                "previous_activity_id": old_session["activity_id"],
                                "previous_activity_name": (
                                    old_activity["name"] if old_activity else None
                                ),
                            },
                        )
