"""Switch platform for Drift Beacon."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Callable

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_ACTIVITY_ID,
    ATTR_ARMED_AT,
    ATTR_CATEGORY_COLOR,
    ATTR_CATEGORY_ICON,
    ATTR_CATEGORY_ID,
    ATTR_CATEGORY_NAME,
    ATTR_COLOR,
    ATTR_DESCRIPTION,
    ATTR_ICON,
    ATTR_SESSION_DURATION,
    ATTR_SESSION_START_TIME,
    ATTR_SORT_ORDER,
    ATTR_TARGET,
    ATTR_UNIT,
    ATTR_PROGRESS,
    ATTR_WORKSPACE_ID,
    ATTR_WORKSPACE_NAME,
    DOMAIN,
)
from .coordinator import (
    Activity,
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
    manager = entry.runtime_data
    session_entities: dict[str, DriftBeaconActivitySwitch] = {}
    armed_entities: dict[str, DriftBeaconArmedActivitySwitch] = {}

    @callback
    def _async_add_remove_entities() -> None:
        """Add new entities and remove deleted ones."""
        # Only create switches for span activities (not archived)
        span_activities = [
            a for a in manager.activities
            if a.get("tracking_type") == "span" and not a.get("archived", False)
        ]

        current_activity_ids = {activity["id"] for activity in span_activities}
        existing_ids = set(session_entities.keys())
        new_ids = current_activity_ids - existing_ids
        deleted_ids = existing_ids - current_activity_ids

        # Create entities for new activities
        new_entities = []
        for activity in span_activities:
            if activity["id"] in new_ids:
                entity = DriftBeaconActivitySwitch(
                    manager, activity, entry.entry_id
                )
                session_entities[activity["id"]] = entity
                new_entities.append(entity)

        if new_entities:
            async_add_entities(new_entities)

        # Remove entities for deleted activities
        for activity_id in deleted_ids:
            entity = session_entities.pop(activity_id)
            hass.async_create_task(entity.async_remove())

        armable_activities = [
            activity
            for activity in manager.activities
            if not activity.get("archived", False)
        ]

        current_armable_ids = {activity["id"] for activity in armable_activities}
        existing_armed_ids = set(armed_entities)
        new_armed_entities = []
        for activity in armable_activities:
            if activity["id"] not in existing_armed_ids:
                entity = DriftBeaconArmedActivitySwitch(
                    manager, activity, entry.entry_id
                )
                armed_entities[activity["id"]] = entity
                new_armed_entities.append(entity)

        if new_armed_entities:
            async_add_entities(new_armed_entities)

        for activity_id in existing_armed_ids - current_armable_ids:
            entity = armed_entities.pop(activity_id)
            hass.async_create_task(entity.async_remove())

    # Add initial entities
    _async_add_remove_entities()

    # Listen for manager updates
    entry.async_on_unload(manager.async_add_listener(_async_add_remove_entities))


class DriftBeaconActivitySwitch(SwitchEntity):
    """Representation of an Activity as a switch."""

    _attr_has_entity_name = True
    _attr_entity_registry_visible_default = False

    def __init__(
        self,
        manager: DriftBeaconWebSocketManager,
        activity: Activity,
        config_entry_id: str,
    ) -> None:
        """Initialize the switch."""
        self._manager = manager
        self._activity_id = activity["id"]
        self._config_entry_id = config_entry_id
        self._remove_listener: Callable | None = None

        # Set unique ID for entity registry
        self._attr_unique_id = f"{config_entry_id}_{activity['id']}"

        # Set entity name
        self._attr_name = f"{activity['name']} Session"

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
    def is_on(self) -> bool:
        """Return true if the activity has an active session."""
        workspace = self._manager.get_workspace_for_activity(self._activity_id)
        if workspace is None:
            return False

        session = self._manager.get_live_session(workspace["id"])
        if session is None:
            return False

        return session["activity_id"] == self._activity_id

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not self._manager.available:
            return False
        return self._get_activity() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        activity = self._get_activity()
        if activity is None:
            return {}

        workspace = self._manager.get_workspace_for_activity(self._activity_id)
        category = self._manager.get_category(activity.get("category_id"))

        attributes = {
            ATTR_ACTIVITY_ID: activity["id"],
            ATTR_DESCRIPTION: activity["description"],
            ATTR_CATEGORY_ID: activity.get("category_id"),
            ATTR_CATEGORY_NAME: category["name"] if category else None,
            ATTR_CATEGORY_ICON: category["icon"] if category else None,
            ATTR_CATEGORY_COLOR: hex_to_rgb(category["color"]) if category else None,
            ATTR_COLOR: hex_to_rgb(activity["color"]),
            ATTR_ICON: activity["icon"],
            ATTR_SORT_ORDER: activity["sort_order"],
            ATTR_UNIT: activity.get("unit"),
            ATTR_PROGRESS: activity["progress"]["current"],
            ATTR_TARGET: activity["progress"]["target"],
            ATTR_WORKSPACE_ID: workspace["id"] if workspace else None,
            ATTR_WORKSPACE_NAME: workspace["name"] if workspace else None,
        }

        # Add session information if this activity is active
        if workspace:
            session = self._manager.get_live_session(workspace["id"])
            if session and session["activity_id"] == self._activity_id:
                attributes[ATTR_SESSION_START_TIME] = session["start_time"]

                if session.get("start_time"):
                    try:
                        start_time = datetime.fromisoformat(
                            session["start_time"].replace("Z", "+00:00")
                        )
                        duration = (
                            datetime.now(start_time.tzinfo) - start_time
                        ).total_seconds()
                        attributes[ATTR_SESSION_DURATION] = int(duration)
                    except (ValueError, TypeError) as err:
                        _LOGGER.debug("Failed to calculate session duration: %s", err)

        return attributes

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on - start a session for this activity."""
        _LOGGER.debug("Turning on switch for activity %s", self._activity_id)

        workspace = self._manager.get_workspace_for_activity(self._activity_id)
        if workspace is None:
            _LOGGER.error("Cannot start session - workspace not found for activity %s", self._activity_id)
            return

        success = await self._manager.start_session(self._activity_id)

        if not success:
            _LOGGER.error("Failed to start session for activity %s", self._activity_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off - stop the session for this activity."""
        _LOGGER.debug("Turning off switch for activity %s", self._activity_id)

        workspace = self._manager.get_workspace_for_activity(self._activity_id)
        if workspace is None:
            _LOGGER.error("Cannot stop session - workspace not found for activity %s", self._activity_id)
            return

        session = self._manager.get_live_session(workspace["id"])
        if session and session["activity_id"] == self._activity_id:
            success = await self._manager.stop_session(self._activity_id)
            if not success:
                _LOGGER.error(
                    "Failed to stop session for activity %s", self._activity_id
                )
        else:
            _LOGGER.debug(
                "Activity %s does not have active session, nothing to stop",
                self._activity_id,
            )

    def _get_activity(self) -> Activity | None:
        """Get the activity data for this entity."""
        return self._manager.get_activity(self._activity_id)


class DriftBeaconArmedActivitySwitch(SwitchEntity):
    """Switch that arms or disarms one activity."""

    _attr_has_entity_name = True
    _attr_entity_registry_visible_default = False

    def __init__(
        self,
        manager: DriftBeaconWebSocketManager,
        activity: Activity,
        config_entry_id: str,
    ) -> None:
        """Initialize the armed activity switch."""
        self._manager = manager
        self._activity_id = activity["id"]
        self._remove_listener: Callable | None = None
        self._attr_unique_id = f"{config_entry_id}_armed_{activity['id']}"
        self._attr_name = f"{activity['name']} Arm"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, config_entry_id)},
        }

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
    def is_on(self) -> bool:
        """Return whether this is the activity in the user's armed slot."""
        workspace = self._manager.get_workspace_for_activity(self._activity_id)
        if workspace is None:
            return False
        armed_activity = self._manager.get_armed_activity(workspace["id"])
        return (
            armed_activity is not None
            and armed_activity["activity_id"] == self._activity_id
        )

    @property
    def available(self) -> bool:
        """Return whether armed activity controls are available."""
        return self._manager.available and self._get_activity() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return activity metadata and the armed timestamp."""
        activity = self._get_activity()
        if activity is None:
            return {}

        workspace = self._manager.get_workspace_for_activity(self._activity_id)
        category = self._manager.get_category(activity.get("category_id"))
        attributes = {
            ATTR_ACTIVITY_ID: activity["id"],
            ATTR_DESCRIPTION: activity["description"],
            ATTR_CATEGORY_ID: activity.get("category_id"),
            ATTR_CATEGORY_NAME: category["name"] if category else None,
            ATTR_CATEGORY_ICON: category["icon"] if category else None,
            ATTR_CATEGORY_COLOR: (
                hex_to_rgb(category["color"]) if category else None
            ),
            ATTR_COLOR: hex_to_rgb(activity["color"]),
            ATTR_ICON: activity["icon"],
            ATTR_SORT_ORDER: activity["sort_order"],
            ATTR_UNIT: activity.get("unit"),
            ATTR_PROGRESS: activity["progress"]["current"],
            ATTR_TARGET: activity["progress"]["target"],
            ATTR_WORKSPACE_ID: workspace["id"] if workspace else None,
            ATTR_WORKSPACE_NAME: workspace["name"] if workspace else None,
        }

        if workspace:
            armed_activity = self._manager.get_armed_activity(workspace["id"])
            if (
                armed_activity is not None
                and armed_activity["activity_id"] == self._activity_id
            ):
                attributes[ATTR_ARMED_AT] = armed_activity["armed_at"]

        return attributes

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Arm this activity."""
        success = await self._manager.arm_activity(self._activity_id)
        if not success:
            _LOGGER.error("Failed to arm activity %s", self._activity_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disarm this activity if it currently owns the armed slot."""
        if not self.is_on:
            return
        success = await self._manager.disarm_activity(self._activity_id)
        if not success:
            _LOGGER.error("Failed to disarm activity %s", self._activity_id)

    def _get_activity(self) -> Activity | None:
        """Get the activity data for this entity."""
        return self._manager.get_activity(self._activity_id)
