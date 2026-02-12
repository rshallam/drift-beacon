"""The Drift Beacon integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_HOST, CONF_PORT, DOMAIN
from .coordinator import DriftBeaconWebSocketManager

if TYPE_CHECKING:
    from .coordinator import DriftBeaconConfigEntry

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SWITCH, Platform.SENSOR, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: DriftBeaconConfigEntry) -> bool:
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]

    _LOGGER.debug("Setting up Drift Beacon integration for %s:%s", host, port)

    # Create and connect the WebSocket manager
    manager = DriftBeaconWebSocketManager(hass, entry)
    await manager.async_connect()

    # Store manager in runtime data
    entry.runtime_data = manager

    # Create device registry entry for the Drift Beacon server
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Drift Beacon",
        manufacturer="Drift Beacon",
        configuration_url=f"http://{host}:{port}",
    )

    # Setup platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    await entry.runtime_data.async_disconnect()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
