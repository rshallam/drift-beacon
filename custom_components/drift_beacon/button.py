"""Button platform for Drift Beacon point activities."""

from __future__ import annotations

import logging
from typing import Any, Callable

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_ACTIVITY_ID,
    ATTR_CATEGORY_COLOR,
    ATTR_CATEGORY_ICON,
    ATTR_CATEGORY_ID,
    ATTR_CATEGORY_NAME,
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
    DriftBeaconWebSocketManager,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DriftBeaconConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager = entry.runtime_data
    entities: dict[str, DriftBeaconActivityButton] = {}

    @callback
    def _async_add_remove_entities() -> None:
        """Add new entities and remove deleted ones."""
        # Only create buttons for point activities (not archived)
        point_activities = [
            a for a in manager.activities
            if a.get("tracking_type") == "point" and not a.get("archived", False)
        ]

        current_activity_ids = {activity["id"] for activity in point_activities}
        existing_ids = set(entities.keys())
        new_ids = current_activity_ids - existing_ids
        deleted_ids = existing_ids - current_activity_ids

        new_entities = []
        for activity in point_activities:
            if activity["id"] in new_ids:
                entity = DriftBeaconActivityButton(
                    manager, activity, entry.entry_id
                )
                entities[activity["id"]] = entity
                new_entities.append(entity)

        if new_entities:
            async_add_entities(new_entities)

        for activity_id in deleted_ids:
            entity = entities.pop(activity_id)
            hass.async_create_task(entity.async_remove())

    _async_add_remove_entities()
    entry.async_on_unload(manager.async_add_listener(_async_add_remove_entities))


class DriftBeaconActivityButton(ButtonEntity):
    """Representation of a point activity as a button."""

    _attr_has_entity_name = True

    def __init__(
        self,
        manager: DriftBeaconWebSocketManager,
        activity: Activity,
        config_entry_id: str,
    ) -> None:
        """Initialize the button."""
        self._manager = manager
        self._activity_id = activity["id"]
        self._config_entry_id = config_entry_id
        self._remove_listener: Callable | None = None

        self._attr_unique_id = f"{config_entry_id}_{activity['id']}"
        self._attr_name = activity["name"]

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

        return {
            ATTR_ACTIVITY_ID: activity["id"],
            ATTR_DESCRIPTION: activity["description"],
            ATTR_CATEGORY_ID: activity.get("category_id"),
            ATTR_CATEGORY_NAME: category["name"] if category else None,
            ATTR_CATEGORY_ICON: category["icon"] if category else None,
            ATTR_CATEGORY_COLOR: category["color"] if category else None,
            ATTR_COLOR: activity["color"],
            ATTR_ICON: activity["icon"],
            ATTR_SORT_ORDER: activity["sort_order"],
            ATTR_WORKSPACE_ID: workspace["id"] if workspace else None,
            ATTR_WORKSPACE_NAME: workspace["name"] if workspace else None,
        }

    async def async_press(self) -> None:
        """Handle the button press - mark the point activity."""
        _LOGGER.debug("Pressing button for activity %s", self._activity_id)

        workspace = self._manager.get_workspace_for_activity(self._activity_id)
        if workspace is None:
            _LOGGER.error("Cannot mark activity %s - workspace not found", self._activity_id)
            return

        success = await self._manager.mark_activity(self._activity_id, workspace["id"])

        if not success:
            _LOGGER.error("Failed to mark activity %s", self._activity_id)

    def _get_activity(self) -> Activity | None:
        """Get the activity data for this entity."""
        return self._manager.get_activity(self._activity_id)
