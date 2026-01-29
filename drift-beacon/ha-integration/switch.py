"""Switch platform for Drift Beacon."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_ACTIVITY_ID,
    ATTR_CATEGORY_ID,
    ATTR_CATEGORY_NAME,
    ATTR_CATEGORY_ICON,
    ATTR_CATEGORY_COLOR,
    ATTR_COLOR,
    ATTR_DESCRIPTION,
    ATTR_ICON,
    ATTR_SESSION_DURATION,
    ATTR_SESSION_START_TIME,
    ATTR_SORT_ORDER,
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
    coordinator = entry.runtime_data
    # Track entities by activity ID
    entities: dict[str, DriftBeaconActivitySwitch] = {}

    @callback
    def _async_add_remove_entities() -> None:
        """Add new entities and remove deleted ones."""
        activities = coordinator.data["activities"]

        # Get current activity IDs (server filters archived activities)
        current_activity_ids = {activity["id"] for activity in activities}

        existing_ids = set(entities.keys())
        new_ids = current_activity_ids - existing_ids
        deleted_ids = existing_ids - current_activity_ids

        # Create entities for new activities
        new_entities = []
        for activity in activities:
            if activity["id"] in new_ids:
                entity = DriftBeaconActivitySwitch(
                    coordinator, activity, entry.entry_id
                )
                entities[activity["id"]] = entity
                new_entities.append(entity)

        if new_entities:
            async_add_entities(new_entities)

        # Remove entities for deleted activities
        for activity_id in deleted_ids:
            entity = entities.pop(activity_id)
            hass.async_create_task(entity.async_remove())

    # Add initial entities
    _async_add_remove_entities()

    # Listen for coordinator updates
    entry.async_on_unload(coordinator.async_add_listener(_async_add_remove_entities))


class DriftBeaconActivitySwitch(
    CoordinatorEntity[DriftBeaconDataUpdateCoordinator], SwitchEntity
):
    """Representation of a Activity as a switch."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DriftBeaconDataUpdateCoordinator,
        activity: Activity,
        config_entry_id: str,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)

        self._activity_id = activity["id"]
        self._config_entry_id = config_entry_id

        # Set unique ID for entity registry
        self._attr_unique_id = f"{config_entry_id}_{activity['id']}"

        # Set entity name
        self._attr_name = activity["name"]

        # Link to device
        self._attr_device_info = {
            "identifiers": {(DOMAIN, config_entry_id)},
        }

    @property
    def is_on(self) -> bool:
        """Return true if the activity has an active session."""
        live_sessions = self.coordinator.data.get("live_sessions", [])

        # Check if any session matches this activity
        for session in live_sessions:
            if session.get("activity_id") == self._activity_id:
                return True

        return False

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        # Entity is available if coordinator has data and activity still exists
        if not self.coordinator.last_update_success:
            return False

        activity = self._get_activity()

        # Entity unavailable if activity is gone
        return activity is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        activity = self._get_activity()
        if activity is None:
            return {}

        attributes = {
            ATTR_ACTIVITY_ID: activity["id"],
            ATTR_DESCRIPTION: activity["description"],
            ATTR_CATEGORY_ID: activity["category_id"],
            ATTR_CATEGORY_NAME: activity["category_name"],
            ATTR_CATEGORY_ICON: activity["category_icon"],
            ATTR_CATEGORY_COLOR: activity["category_color"],
            ATTR_COLOR: activity["color"],
            ATTR_ICON: activity["icon"],
            ATTR_SORT_ORDER: activity["sort_order"],
            ATTR_WORKSPACE_ID: activity["workspace_id"],
            ATTR_WORKSPACE_NAME: activity["workspace_name"],
        }

        # Add session information if this activity is active
        live_sessions = self.coordinator.data.get("live_sessions", [])
        for session in live_sessions:
            if session.get("activity_id") == self._activity_id:
                attributes[ATTR_SESSION_START_TIME] = session["start_time"]

                # Calculate duration if we have a start time
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
                break

        return attributes

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on - start a session for this activity."""
        _LOGGER.debug("Turning on switch for activity %s", self._activity_id)

        # Get workspace ID from activity data
        activity = self._get_activity()
        if activity is None:
            _LOGGER.error("Cannot start session - activity %s not found", self._activity_id)
            return

        workspace_id = activity["workspace_id"]
        success = await self.coordinator.start_session(self._activity_id, workspace_id)

        if not success:
            _LOGGER.error("Failed to start session for activity %s", self._activity_id)
            # The coordinator already requested a refresh, so state will update

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off - stop the session for this activity."""
        _LOGGER.debug("Turning off switch for activity %s", self._activity_id)

        # Get workspace ID from activity data
        activity = self._get_activity()
        if activity is None:
            _LOGGER.error("Cannot stop session - activity %s not found", self._activity_id)
            return

        # Only stop if this activity actually has an active session
        live_sessions = self.coordinator.data.get("live_sessions", [])
        has_active_session = any(
            session.get("activity_id") == self._activity_id
            for session in live_sessions
        )

        if has_active_session:
            workspace_id = activity["workspace_id"]
            success = await self.coordinator.stop_session(self._activity_id, workspace_id)

            if not success:
                _LOGGER.error(
                    "Failed to stop session for activity %s", self._activity_id
                )
        else:
            _LOGGER.debug(
                "Activity %s does not have active session, nothing to stop",
                self._activity_id,
            )
            # Still refresh to ensure state is correct
            await self.coordinator.async_request_refresh()

    def _get_activity(self) -> Activity | None:
        """Get the activity data for this entity."""
        activities = self.coordinator.data.get("activities", [])
        for activity in activities:
            if activity["id"] == self._activity_id:
                return activity
        return None
