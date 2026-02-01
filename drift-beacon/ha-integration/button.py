"""Button platform for Drift Beacon point activities."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
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
    entities: dict[str, DriftBeaconActivityButton] = {}

    @callback
    def _async_add_remove_entities() -> None:
        """Add new entities and remove deleted ones."""
        activities = coordinator.data["activities"]

        # Only create buttons for point activities
        point_activities = [a for a in activities if a.get("tracking_type") == "point"]

        current_activity_ids = {activity["id"] for activity in point_activities}
        existing_ids = set(entities.keys())
        new_ids = current_activity_ids - existing_ids
        deleted_ids = existing_ids - current_activity_ids

        new_entities = []
        for activity in point_activities:
            if activity["id"] in new_ids:
                entity = DriftBeaconActivityButton(
                    coordinator, activity, entry.entry_id
                )
                entities[activity["id"]] = entity
                new_entities.append(entity)

        if new_entities:
            async_add_entities(new_entities)

        for activity_id in deleted_ids:
            entity = entities.pop(activity_id)
            hass.async_create_task(entity.async_remove())

    _async_add_remove_entities()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_remove_entities))


class DriftBeaconActivityButton(
    CoordinatorEntity[DriftBeaconDataUpdateCoordinator], ButtonEntity
):
    """Representation of a point activity as a button."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DriftBeaconDataUpdateCoordinator,
        activity: Activity,
        config_entry_id: str,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)

        self._activity_id = activity["id"]
        self._config_entry_id = config_entry_id

        self._attr_unique_id = f"{config_entry_id}_{activity['id']}"
        self._attr_name = activity["name"]

        self._attr_device_info = {
            "identifiers": {(DOMAIN, config_entry_id)},
        }

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not self.coordinator.last_update_success:
            return False
        return self._get_activity() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        activity = self._get_activity()
        if activity is None:
            return {}

        return {
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

    async def async_press(self) -> None:
        """Handle the button press - mark the point activity."""
        _LOGGER.debug("Pressing button for activity %s", self._activity_id)

        activity = self._get_activity()
        if activity is None:
            _LOGGER.error("Cannot mark activity %s - not found", self._activity_id)
            return

        workspace_id = activity["workspace_id"]
        success = await self.coordinator.mark_activity(self._activity_id, workspace_id)

        if not success:
            _LOGGER.error("Failed to mark activity %s", self._activity_id)

    def _get_activity(self) -> Activity | None:
        """Get the activity data for this entity."""
        activities = self.coordinator.data.get("activities", [])
        for activity in activities:
            if activity["id"] == self._activity_id:
                return activity
        return None
