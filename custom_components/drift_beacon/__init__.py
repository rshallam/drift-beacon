"""The Drift Beacon integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_HOST,
    CONF_PORT,
    CONF_PROTOCOL,
    CONF_WORKSPACE_ID,
    CONF_WORKSPACE_NAME,
    DOMAIN,
)
from .coordinator import DriftBeaconWebSocketManager

if TYPE_CHECKING:
    from .coordinator import DriftBeaconConfigEntry

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SWITCH, Platform.SENSOR, Platform.BUTTON]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reject legacy entries; workspace identity requires a clean setup."""
    if entry.version < 3:
        _LOGGER.error(
            "Drift Beacon config entry %s predates workspace-scoped identity; "
            "remove it and add each workspace again",
            entry.entry_id,
        )
        return False
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DriftBeaconConfigEntry) -> bool:
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    workspace_id = entry.data[CONF_WORKSPACE_ID]
    workspace_name = entry.data[CONF_WORKSPACE_NAME]

    _LOGGER.debug("Setting up Drift Beacon integration for %s:%s", host, port)

    # Create and connect the WebSocket manager
    manager = DriftBeaconWebSocketManager(hass, entry)
    await manager.async_connect()

    # Store manager in runtime data
    entry.runtime_data = manager

    # Create one virtual device for this workspace
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, workspace_id)},
        name=workspace_name,
        manufacturer="Drift Beacon",
        configuration_url=f"{entry.data[CONF_PROTOCOL]}://{host}:{port}",
    )

    # Setup platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    await entry.runtime_data.async_disconnect()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
