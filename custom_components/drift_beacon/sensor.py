"""Sensor platform for Drift Beacon."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_ACTIVITY_ID,
    ATTR_ACTIVITY_NAME,
    ATTR_CATEGORY_COLOR,
    ATTR_CATEGORY_ICON,
    ATTR_CATEGORY_ID,
    ATTR_CATEGORY_NAME,
    ATTR_COLOR,
    ATTR_ICON,
    ATTR_SESSION_DURATION,
    ATTR_SESSION_DURATION_FORMATTED,
    ATTR_SESSION_START_TIME,
    ATTR_WORKSPACE_ID,
    ATTR_WORKSPACE_NAME,
    DOMAIN,
)
from .coordinator import (
    Activity,
    DriftBeaconConfigEntry,
    DriftBeaconDataUpdateCoordinator,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DriftBeaconConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Drift Beacon sensor platform."""
    coordinator = entry.runtime_data

    # Extract unique workspaces from activities
    activities = coordinator.data.get("activities", [])
    workspaces: dict[str, str] = {}  # workspace_id -> workspace_name

    for activity in activities:
        workspace_id = activity.get("workspace_id")
        workspace_name = activity.get("workspace_name")
        if workspace_id and workspace_name:
            workspaces[workspace_id] = workspace_name

    # Create one live session sensor per workspace
    sensors = [
        DriftBeaconLiveSessionSensor(
            coordinator, entry.entry_id, workspace_id, workspace_name
        )
        for workspace_id, workspace_name in workspaces.items()
    ]

    if sensors:
        async_add_entities(sensors)
    else:
        _LOGGER.warning("No workspaces found, no sensors created")


class DriftBeaconLiveSessionSensor(
    CoordinatorEntity[DriftBeaconDataUpdateCoordinator], SensorEntity
):
    """Sensor representing the live session state for a specific workspace."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DriftBeaconDataUpdateCoordinator,
        config_entry_id: str,
        workspace_id: str,
        workspace_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self._config_entry_id = config_entry_id
        self._workspace_id = workspace_id
        self._workspace_name = workspace_name

        # Set unique ID for entity registry (include workspace)
        self._attr_unique_id = f"{config_entry_id}_live_session_{workspace_id}"

        # Set entity name (include workspace name)
        self._attr_name = f"{workspace_name} Session"

        # Link to device
        self._attr_device_info = {
            "identifiers": {(DOMAIN, config_entry_id)},
        }

    @property
    def native_value(self) -> str | None:
        """Return the activity name, or None if no active session in this workspace."""
        # Get session for this workspace
        session = self._get_workspace_session()
        if session is None:
            return None

        # Find the activity for this session
        activity = self._get_activity(session.get("activity_id"))
        if activity is None:
            return None

        return activity["name"]

    @property
    def icon(self) -> str:
        """Return the icon for the current activity."""
        session = self._get_workspace_session()
        if session is None:
            return "mdi:circle"

        # Find the activity for this session
        activity = self._get_activity(session.get("activity_id"))
        if activity is None or not activity.get("icon"):
            return "mdi:circle"

        # Icons are already formatted for HA (e.g., "mdi:brain")
        return activity["icon"]

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        session = self._get_workspace_session()

        # If no session in this workspace, return workspace info only
        if session is None:
            return {
                ATTR_WORKSPACE_ID: self._workspace_id,
                ATTR_WORKSPACE_NAME: self._workspace_name,
            }

        # Find the activity for this session
        activity = self._get_activity(session.get("activity_id"))
        if activity is None:
            _LOGGER.warning(
                "Activity %s not found for live session", session.get("activity_id")
            )
            return {
                ATTR_WORKSPACE_ID: self._workspace_id,
                ATTR_WORKSPACE_NAME: self._workspace_name,
            }

        attributes = {
            ATTR_ACTIVITY_ID: activity["id"],
            ATTR_ACTIVITY_NAME: activity["name"],
            ATTR_COLOR: activity["color"],
            ATTR_ICON: activity["icon"],
            ATTR_CATEGORY_ID: activity.get("category_id"),
            ATTR_CATEGORY_NAME: activity.get("category_name"),
            ATTR_CATEGORY_ICON: activity.get("category_icon"),
            ATTR_CATEGORY_COLOR: activity.get("category_color"),
            ATTR_WORKSPACE_ID: self._workspace_id,
            ATTR_WORKSPACE_NAME: self._workspace_name,
            ATTR_SESSION_START_TIME: session["start_time"],
        }

        # Calculate duration if we have a start time
        if session.get("start_time"):
            try:
                start_time = datetime.fromisoformat(
                    session["start_time"].replace("Z", "+00:00")
                )
                duration = (
                    datetime.now(start_time.tzinfo) - start_time
                ).total_seconds()
                duration_seconds = int(duration)
                attributes[ATTR_SESSION_DURATION] = duration_seconds
                attributes[ATTR_SESSION_DURATION_FORMATTED] = self._format_duration(
                    duration_seconds
                )
            except (ValueError, TypeError) as err:
                _LOGGER.debug("Failed to calculate session duration: %s", err)

        return attributes

    def _format_duration(self, seconds: int) -> str:
        """Format duration in seconds to human-readable string."""
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60

        if hours > 0:
            return f"{hours}h {minutes}m {secs}s"
        elif minutes > 0:
            return f"{minutes}m {secs}s"
        else:
            return f"{secs}s"

    def _get_workspace_session(self) -> dict[str, Any] | None:
        """Get the active session for this workspace."""
        live_sessions = self.coordinator.data.get("live_sessions", [])

        for session in live_sessions:
            if session.get("workspace_id") == self._workspace_id:
                return session

        return None

    def _get_activity(self, activity_id: str | None) -> Activity | None:
        """Get activity data by ID."""
        if activity_id is None:
            return None

        activities = self.coordinator.data.get("activities", [])
        for activity in activities:
            if activity["id"] == activity_id:
                return activity
        return None
