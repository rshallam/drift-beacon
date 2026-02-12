"""Sensor platform for Drift Beacon."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Callable

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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
    DriftBeaconWebSocketManager,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DriftBeaconConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Drift Beacon sensor platform."""
    manager = entry.runtime_data

    # Create one live session sensor per workspace
    sensors = [
        DriftBeaconLiveSessionSensor(
            manager, entry.entry_id, workspace["id"], workspace["name"]
        )
        for workspace in manager.workspaces
    ]

    if sensors:
        async_add_entities(sensors)
    else:
        _LOGGER.warning("No workspaces found, no sensors created")


class DriftBeaconLiveSessionSensor(SensorEntity):
    """Sensor representing the live session state for a specific workspace."""

    _attr_has_entity_name = True

    def __init__(
        self,
        manager: DriftBeaconWebSocketManager,
        config_entry_id: str,
        workspace_id: str,
        workspace_name: str,
    ) -> None:
        """Initialize the sensor."""
        self._manager = manager
        self._config_entry_id = config_entry_id
        self._workspace_id = workspace_id
        self._workspace_name = workspace_name
        self._remove_listener: Callable | None = None

        # Set unique ID for entity registry (include workspace)
        self._attr_unique_id = f"{config_entry_id}_live_session_{workspace_id}"

        # Set entity name (include workspace name)
        self._attr_name = f"{workspace_name} Session"

        # Link to device
        self._attr_device_info = {
            "identifiers": {(DOMAIN, config_entry_id)},
        }

    async def async_added_to_hass(self) -> None:
        """Register listener when added to hass."""
        self._remove_listener = self._manager.async_add_listener(
            self.async_write_ha_state
        )

    async def async_will_remove_from_hass(self) -> None:
        """Remove listener when removed from hass."""
        if self._remove_listener:
            self._remove_listener()

    @property
    def native_value(self) -> str | None:
        """Return the activity name, or None if no active session in this workspace."""
        session = self._manager.get_live_session(self._workspace_id)
        if session is None:
            return None

        activity = self._manager.get_activity(session["activity_id"])
        if activity is None:
            return None

        return activity["name"]

    @property
    def icon(self) -> str:
        """Return the icon for the current activity."""
        session = self._manager.get_live_session(self._workspace_id)
        if session is None:
            return "mdi:circle"

        activity = self._manager.get_activity(session["activity_id"])
        if activity is None or not activity.get("icon"):
            return "mdi:circle"

        return activity["icon"]

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._manager.available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        session = self._manager.get_live_session(self._workspace_id)

        # If no session in this workspace, return workspace info only
        if session is None:
            return {
                ATTR_WORKSPACE_ID: self._workspace_id,
                ATTR_WORKSPACE_NAME: self._workspace_name,
            }

        # Find the activity for this session
        activity = self._manager.get_activity(session["activity_id"])
        if activity is None:
            _LOGGER.warning(
                "Activity %s not found for live session", session["activity_id"]
            )
            return {
                ATTR_WORKSPACE_ID: self._workspace_id,
                ATTR_WORKSPACE_NAME: self._workspace_name,
            }

        # Look up category
        category = self._manager.get_category(activity.get("category_id"))

        attributes = {
            ATTR_ACTIVITY_ID: activity["id"],
            ATTR_ACTIVITY_NAME: activity["name"],
            ATTR_COLOR: activity["color"],
            ATTR_ICON: activity["icon"],
            ATTR_CATEGORY_ID: activity.get("category_id"),
            ATTR_CATEGORY_NAME: category["name"] if category else None,
            ATTR_CATEGORY_ICON: category["icon"] if category else None,
            ATTR_CATEGORY_COLOR: category["color"] if category else None,
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
