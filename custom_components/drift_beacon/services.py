"""Service actions for Drift Beacon."""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_extract_config_entry_ids

from .const import (
    ATTR_PAUSE,
    DOMAIN,
    SERVICE_PAUSE_ACTIVITY,
    SERVICE_PIN_ACTIVITY,
    SERVICE_QUEUE_ACTIVITY,
    SERVICE_STOP_SESSION,
    SERVICE_TRACK_ACTIVITY,
    SERVICE_UNPIN_ACTIVITY,
)

if TYPE_CHECKING:
    from .coordinator import Activity, DriftBeaconWebSocketManager

ACTIVITY_SERVICES = (
    SERVICE_TRACK_ACTIVITY,
    SERVICE_PAUSE_ACTIVITY,
    SERVICE_PIN_ACTIVITY,
    SERVICE_QUEUE_ACTIVITY,
    SERVICE_UNPIN_ACTIVITY,
)
# Activity actions require explicit entities, so a device/area cannot accidentally
# track every activity in a workspace. Session, pin, and mark entities are accepted.
ACTIVITY_SCHEMA = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_ids})

STOP_SESSION_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_PAUSE, default=False): cv.boolean,
        **cv.TARGET_SERVICE_FIELDS,
    }
)


async def async_handle_stop_session(hass: HomeAssistant, call: ServiceCall) -> None:
    """Stop (or pause) whatever session is live in each targeted workspace."""
    entries = [
        entry
        for entry_id in await async_extract_config_entry_ids(call)
        if (entry := hass.config_entries.async_get_entry(entry_id)) is not None
        and entry.domain == DOMAIN
        and entry.state is ConfigEntryState.LOADED
    ]
    if not entries:
        raise ServiceValidationError(
            "Target a connected Drift Beacon workspace device or entity"
        )

    pause = call.data[ATTR_PAUSE]
    for entry in entries:
        manager = entry.runtime_data
        success = (
            await manager.pause_session() if pause else await manager.stop_session()
        )
        if not success:
            raise HomeAssistantError(
                f"Failed to {'pause' if pause else 'stop'} the live session "
                f"in {entry.title}"
            )


def _resolve_activities(
    hass: HomeAssistant, entity_ids: list[str]
) -> list[tuple[DriftBeaconWebSocketManager, Activity]]:
    """Resolve stable activity identity, validating all targets before acting."""
    registry = er.async_get(hass)
    resolved = {}
    for entity_id in entity_ids:
        entity = registry.async_get(entity_id)
        entry = (
            hass.config_entries.async_get_entry(entity.config_entry_id)
            if entity and entity.platform == DOMAIN and entity.config_entry_id
            else None
        )
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
        ):
            raise ServiceValidationError(
                f"{entity_id} is not an entity of a connected Drift Beacon workspace"
            )
        manager = entry.runtime_data
        activity_id = None
        for role in ("session", "pin", "mark"):
            prefix = f"{manager.workspace_id}:{role}:"
            if entity.unique_id.startswith(prefix):
                activity_id = entity.unique_id[len(prefix) :]
                break
        activity = manager.get_activity(activity_id) if activity_id else None
        if activity is None or activity.get("archived", False):
            raise ServiceValidationError(
                f"{entity_id} does not identify an available Drift Beacon activity"
            )
        if not manager.available:
            raise HomeAssistantError(f"Drift Beacon workspace {entry.title} is offline")
        # Selecting both the session and pin entities must not toggle or queue twice.
        resolved[(entry.entry_id, activity_id)] = (manager, activity)
    if not resolved:
        raise ServiceValidationError("Select at least one Drift Beacon activity entity")
    return list(resolved.values())


async def async_handle_activity(hass: HomeAssistant, call: ServiceCall) -> None:
    """Apply an activity action for the selected connection user and activity."""
    for manager, activity in _resolve_activities(hass, call.data[ATTR_ENTITY_ID]):
        activity_id = activity["id"]
        live = manager.get_live_session(manager.workspace_id)
        is_live = live is not None and live["activity_id"] == activity_id

        if call.service == SERVICE_TRACK_ACTIVITY:
            if activity["tracking_type"] == "point":
                success = await manager.mark_activity(activity_id)
            elif is_live:
                success = await manager.stop_session(activity_id)
            else:
                success = await manager.start_session(activity_id)
        elif call.service == SERVICE_PAUSE_ACTIVITY:
            if activity["tracking_type"] != "span" or not is_live:
                continue
            success = await manager.pause_session(activity_id)
        elif call.service == SERVICE_PIN_ACTIVITY:
            success = await manager.pin_activity(activity_id)
        elif call.service == SERVICE_UNPIN_ACTIVITY:
            pinned = manager.get_pinned_activity(manager.workspace_id)
            if pinned is None or pinned["activity_id"] != activity_id:
                continue
            success = await manager.unpin_activity(activity_id)
        elif call.service == SERVICE_QUEUE_ACTIVITY:
            success = await manager.queue_activity(activity_id)
        else:
            raise ServiceValidationError(f"Unknown activity action: {call.service}")

        if not success:
            raise HomeAssistantError(
                f"Failed to {call.service.replace('_', ' ')}: {activity['name']}"
            )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register Drift Beacon service actions."""

    async def _async_stop_session(call: ServiceCall) -> None:
        await async_handle_stop_session(hass, call)

    hass.services.async_register(
        DOMAIN, SERVICE_STOP_SESSION, _async_stop_session, schema=STOP_SESSION_SCHEMA
    )

    async def _async_activity(call: ServiceCall) -> None:
        await async_handle_activity(hass, call)

    for service in ACTIVITY_SERVICES:
        hass.services.async_register(
            DOMAIN, service, _async_activity, schema=ACTIVITY_SCHEMA
        )
