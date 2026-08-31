"""Sensor platform for Drift Beacon."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

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
    ATTR_PINNED_AT,
    ATTR_PROGRESS,
    ATTR_SESSION_DURATION,
    ATTR_SESSION_DURATION_FORMATTED,
    ATTR_SESSION_START_TIME,
    ATTR_TARGET,
    ATTR_UNIT,
    ATTR_USER_ID,
    ATTR_WORKSPACE_ID,
    ATTR_WORKSPACE_NAME,
)
from .coordinator import (
    DriftBeaconConfigEntry,
    DriftBeaconWebSocketManager,
    hex_to_rgb,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DriftBeaconConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Drift Beacon sensor platform."""
    manager = entry.runtime_data
    async_add_entities([DriftBeaconConnectedUserSensor(manager)])
    live_entities: dict[str, DriftBeaconLiveSessionSensor] = {}
    pinned_entities: dict[str, DriftBeaconPinnedActivitySensor] = {}

    @callback
    def _async_add_remove_entities() -> None:
        """Keep workspace sensors synchronized with subscription snapshots."""
        workspaces = {workspace["id"]: workspace for workspace in manager.workspaces}
        existing_ids = set(live_entities)

        new_entities = []
        for workspace_id in workspaces.keys() - existing_ids:
            entity = DriftBeaconLiveSessionSensor(manager, workspace_id)
            live_entities[workspace_id] = entity
            new_entities.append(entity)

        if new_entities:
            async_add_entities(new_entities)

        for workspace_id in existing_ids - workspaces.keys():
            entity = live_entities.pop(workspace_id)
            hass.async_create_task(entity.async_remove())

        pinned_workspace_ids = set(workspaces)
        existing_pinned_ids = set(pinned_entities)
        new_pinned_entities = []
        for workspace_id in pinned_workspace_ids - existing_pinned_ids:
            entity = DriftBeaconPinnedActivitySensor(manager, workspace_id)
            pinned_entities[workspace_id] = entity
            new_pinned_entities.append(entity)

        if new_pinned_entities:
            async_add_entities(new_pinned_entities)

        for workspace_id in existing_pinned_ids - pinned_workspace_ids:
            entity = pinned_entities.pop(workspace_id)
            hass.async_create_task(entity.async_remove())

    _async_add_remove_entities()
    entry.async_on_unload(manager.async_add_listener(_async_add_remove_entities))


class DriftBeaconConnectedUserSensor(SensorEntity):
    """The Drift Beacon user represented by this workspace connection."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:account-circle-outline"
    _attr_name = "Connected user"

    def __init__(self, manager: DriftBeaconWebSocketManager) -> None:
        self._manager = manager
        self._remove_listener: Callable | None = None
        self._attr_unique_id = f"{manager.workspace_id}:connected_user"
        self._attr_device_info = manager.device_info

    @property
    def native_value(self) -> str:
        """Return the connected user's friendly name."""
        return self._manager.user_name

    @property
    def available(self) -> bool:
        """Return whether the authenticated workspace is connected."""
        return self._manager.available

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Expose the stable Drift Beacon user identifier."""
        return {ATTR_USER_ID: self._manager.user_id}

    async def async_added_to_hass(self) -> None:
        """Register for identity and connection updates."""
        self._remove_listener = self._manager.async_add_listener(
            self.async_write_ha_state
        )

    async def async_will_remove_from_hass(self) -> None:
        """Remove the coordinator listener."""
        if self._remove_listener:
            self._remove_listener()


class DriftBeaconLiveSessionSensor(SensorEntity):
    """Sensor representing the live session state for a specific workspace."""

    _attr_has_entity_name = True

    def __init__(
        self,
        manager: DriftBeaconWebSocketManager,
        workspace_id: str,
    ) -> None:
        """Initialize the sensor."""
        self._manager = manager
        self._workspace_id = workspace_id
        self._remove_listener: Callable | None = None

        # Set unique ID for entity registry (include workspace)
        self._attr_unique_id = f"{manager.workspace_id}:session"

        # Set entity name (include workspace name)
        self._attr_name = "Session"

        # Link to device
        self._attr_device_info = manager.device_info

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
                ATTR_WORKSPACE_NAME: self._manager.workspace_name,
                **self._manager.user_attributes,
            }

        # Find the activity for this session
        activity = self._manager.get_activity(session["activity_id"])
        if activity is None:
            _LOGGER.warning(
                "Activity %s not found for live session", session["activity_id"]
            )
            return {
                ATTR_WORKSPACE_ID: self._workspace_id,
                ATTR_WORKSPACE_NAME: self._manager.workspace_name,
                **self._manager.user_attributes,
            }

        # Look up category
        category = self._manager.get_category(activity.get("category_id"))

        attributes = {
            ATTR_ACTIVITY_ID: activity["id"],
            ATTR_ACTIVITY_NAME: activity["name"],
            ATTR_COLOR: hex_to_rgb(activity["color"]),
            ATTR_ICON: activity["icon"],
            ATTR_CATEGORY_ID: activity.get("category_id"),
            ATTR_CATEGORY_NAME: category["name"] if category else None,
            ATTR_CATEGORY_ICON: category["icon"] if category else None,
            ATTR_CATEGORY_COLOR: hex_to_rgb(category["color"]) if category else None,
            ATTR_UNIT: activity.get("unit"),
            ATTR_PROGRESS: activity["progress"]["current"],
            ATTR_TARGET: activity["progress"]["target"],
            ATTR_WORKSPACE_ID: self._workspace_id,
            ATTR_WORKSPACE_NAME: self._manager.workspace_name,
            ATTR_SESSION_START_TIME: session["start_time"],
            **self._manager.user_attributes,
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


class DriftBeaconPinnedActivitySensor(SensorEntity):
    """Sensor representing the authenticated user's pinned activity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        manager: DriftBeaconWebSocketManager,
        workspace_id: str,
    ) -> None:
        """Initialize the pinned activity sensor."""
        self._manager = manager
        self._workspace_id = workspace_id
        self._remove_listener: Callable | None = None
        self._attr_unique_id = f"{manager.workspace_id}:pinned_activity"
        self._attr_name = "Pinned activity"
        self._attr_device_info = manager.device_info

    async def async_added_to_hass(self) -> None:
        """Register listener when added to Home Assistant."""
        self._remove_listener = self._manager.async_add_listener(
            self.async_write_ha_state
        )

    async def async_will_remove_from_hass(self) -> None:
        """Remove listener when removed from Home Assistant."""
        if self._remove_listener:
            self._remove_listener()

    @property
    def native_value(self) -> str | None:
        """Return the pinned activity name."""
        pinned_activity = self._manager.get_pinned_activity(self._workspace_id)
        if pinned_activity is None:
            return None
        activity = self._manager.get_activity(pinned_activity["activity_id"])
        return activity["name"] if activity else None

    @property
    def icon(self) -> str:
        """Return the pinned activity icon."""
        pinned_activity = self._manager.get_pinned_activity(self._workspace_id)
        if pinned_activity is None:
            return "mdi:target"
        activity = self._manager.get_activity(pinned_activity["activity_id"])
        if activity is None or not activity.get("icon"):
            return "mdi:target"
        return activity["icon"]

    @property
    def available(self) -> bool:
        """Return whether pinned activity state is connected."""
        return self._manager.available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return workspace and pinned activity metadata."""
        attributes: dict[str, Any] = {
            ATTR_WORKSPACE_ID: self._workspace_id,
            ATTR_WORKSPACE_NAME: self._manager.workspace_name,
            **self._manager.user_attributes,
        }
        pinned_activity = self._manager.get_pinned_activity(self._workspace_id)
        if pinned_activity is None:
            return attributes

        activity = self._manager.get_activity(pinned_activity["activity_id"])
        if activity is None:
            return attributes
        category = self._manager.get_category(activity.get("category_id"))
        attributes.update(
            {
                ATTR_ACTIVITY_ID: activity["id"],
                ATTR_ACTIVITY_NAME: activity["name"],
                ATTR_PINNED_AT: pinned_activity["pinned_at"],
                ATTR_COLOR: hex_to_rgb(activity["color"]),
                ATTR_ICON: activity["icon"],
                ATTR_CATEGORY_ID: activity.get("category_id"),
                ATTR_CATEGORY_NAME: category["name"] if category else None,
                ATTR_CATEGORY_ICON: category["icon"] if category else None,
                ATTR_CATEGORY_COLOR: (
                    hex_to_rgb(category["color"]) if category else None
                ),
                ATTR_UNIT: activity.get("unit"),
                ATTR_PROGRESS: activity["progress"]["current"],
                ATTR_TARGET: activity["progress"]["target"],
            }
        )
        return attributes
