"""Service actions for Drift Beacon."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_extract_config_entry_ids

from .const import ATTR_PAUSE, DOMAIN, SERVICE_STOP_SESSION

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


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register Drift Beacon service actions."""

    async def _async_stop_session(call: ServiceCall) -> None:
        await async_handle_stop_session(hass, call)

    hass.services.async_register(
        DOMAIN, SERVICE_STOP_SESSION, _async_stop_session, schema=STOP_SESSION_SCHEMA
    )
