"""Config flow for Drift Beacon integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    API_SYSTEM_STATUS,
    API_TIMEOUT,
    CONF_API_TOKEN,
    CONF_HUB_ID,
    CONF_HUB_NAME,
    CONF_PROTOCOL,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DETECTION_CANDIDATES,
    DETECTION_TIMEOUT,
    DOMAIN,
    WS_PATH,
)

_LOGGER = logging.getLogger(__name__)


class DriftBeaconConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Drift Beacon."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._detected_hub: dict[str, Any] | None = None
        self._reauth_entry_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        # Detect local hub if not already done
        if self._detected_hub is None:
            self._detected_hub = await self._detect_local_addon()

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            api_token = user_input[CONF_API_TOKEN]

            try:
                # Detect protocol and get hub info
                detection = await self._detect_protocol_parallel(host, port)
                if detection is None:
                    errors["base"] = "cannot_connect"
                else:
                    protocol, hub_data = detection
                    hub_id = hub_data["device"]["id"]
                    hub_name = hub_data["device"]["name"]

                    # Validate token via WebSocket handshake
                    await self._validate_token(protocol, host, port, api_token)

                    # Set unique ID to prevent duplicate entries
                    await self.async_set_unique_id(hub_id)
                    self._abort_if_unique_id_configured()

                    return self.async_create_entry(
                        title=f"Drift Beacon @ {hub_name}",
                        data={
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_PROTOCOL: protocol,
                            CONF_API_TOKEN: api_token,
                            CONF_HUB_ID: hub_id,
                            CONF_HUB_NAME: hub_name,
                        },
                    )

            except aiohttp.ClientConnectionError:
                errors["base"] = "cannot_connect"
            except aiohttp.ClientResponseError as err:
                if err.status == 404:
                    errors["base"] = "invalid_server"
                else:
                    errors["base"] = "server_error"
            except aiohttp.WSServerHandshakeError as err:
                if err.status == 401:
                    errors["base"] = "invalid_auth"
                else:
                    errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        # Prepare form data
        detected_info = ""
        default_host = DEFAULT_HOST
        default_port = DEFAULT_PORT

        if self._detected_hub:
            detected_info = f"✓ Local add-on detected at {self._detected_hub['url']}"
            url_parts = (
                self._detected_hub["url"]
                .replace("https://", "")
                .replace("http://", "")
                .split(":")
            )
            default_host = url_parts[0]
            default_port = int(url_parts[1]) if len(url_parts) > 1 else DEFAULT_PORT

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=default_host): str,
                    vol.Required(CONF_PORT, default=default_port): int,
                    vol.Required(CONF_API_TOKEN): str,
                }
            ),
            description_placeholders={
                "detected_info": detected_info,
            },
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauthentication."""
        self._reauth_entry_data = entry_data
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauthentication."""
        errors: dict[str, str] = {}

        host = self._reauth_entry_data[CONF_HOST]
        port = self._reauth_entry_data[CONF_PORT]
        protocol = self._reauth_entry_data.get(CONF_PROTOCOL, "https")

        if user_input is not None:
            api_token = user_input[CONF_API_TOKEN]

            try:
                await self._validate_token(protocol, host, port, api_token)

                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_API_TOKEN: api_token,
                    },
                )

            except aiohttp.WSServerHandshakeError as err:
                if err.status == 401:
                    errors["base"] = "invalid_auth"
                else:
                    errors["base"] = "cannot_connect"
            except aiohttp.ClientConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception during reauth")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_TOKEN): str}),
            errors=errors,
        )

    # ============================================================================
    # Token Validation
    # ============================================================================

    async def _validate_token(
        self, protocol: str, host: str, port: int, api_token: str
    ) -> None:
        """Validate an API token by attempting a WebSocket handshake.

        Raises WSServerHandshakeError (401) if the token is invalid.
        Raises ClientConnectionError if the server is unreachable.
        """
        ws_scheme = "wss" if protocol == "https" else "ws"
        ws_url = f"{ws_scheme}://{host}:{port}{WS_PATH}"

        session = async_get_clientsession(self.hass)

        ws = await session.ws_connect(
            ws_url,
            headers={"Authorization": f"Bearer {api_token}"},
            ssl=False,
            timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
        )
        await ws.close()

    # ============================================================================
    # Protocol Detection
    # ============================================================================

    async def _try_protocol(
        self, protocol: str, host: str, port: int, endpoint: str
    ) -> dict[str, Any] | None:
        """Try a specific protocol for an endpoint.

        Returns:
            Response JSON if successful, None otherwise
        """
        try:
            session = async_get_clientsession(self.hass)
            url = f"{protocol}://{host}:{port}{endpoint}"

            _LOGGER.debug("Trying %s", url)

            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=DETECTION_TIMEOUT),
                ssl=False,
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to connect via %s: %s", protocol, err)

        return None

    async def _detect_protocol_parallel(
        self, host: str, port: int, endpoint: str = API_SYSTEM_STATUS
    ) -> tuple[str, dict[str, Any]] | None:
        """Detect protocol by racing HTTPS and HTTP in parallel.

        Returns:
            Tuple of (protocol, response_data) if successful, None otherwise
        """
        https_task = asyncio.create_task(
            self._try_protocol("https", host, port, endpoint)
        )
        http_task = asyncio.create_task(
            self._try_protocol("http", host, port, endpoint)
        )

        tasks = [https_task, http_task]

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for task in done:
            result = task.result()
            if result is not None:
                for pending_task in pending:
                    pending_task.cancel()

                protocol = "https" if task == https_task else "http"
                _LOGGER.debug("Protocol detection: %s succeeded first", protocol)
                return (protocol, result)

        if pending:
            for task in pending:
                try:
                    result = await task
                    if result is not None:
                        protocol = "https" if task == https_task else "http"
                        _LOGGER.debug(
                            "Protocol detection: %s succeeded (fallback)", protocol
                        )
                        return (protocol, result)
                except Exception:  # noqa: BLE001
                    pass

        _LOGGER.debug("Protocol detection failed for %s:%s", host, port)
        return None

    # ============================================================================
    # Hub Detection
    # ============================================================================

    async def _detect_local_addon(self) -> dict[str, Any] | None:
        """Detect if local Drift Beacon add-on is available.

        Uses parallel protocol detection for fast, zero-penalty discovery.
        """
        for host, port in DETECTION_CANDIDATES:
            _LOGGER.debug("Checking for Drift Beacon at %s:%s", host, port)

            result = await self._detect_protocol_parallel(host, port)

            if result:
                protocol, data = result
                url = f"{protocol}://{host}:{port}"
                _LOGGER.info(
                    "Detected Drift Beacon at %s: %s", url, data["device"]["name"]
                )
                return {
                    "protocol": protocol,
                    "url": url,
                    "id": data["device"]["id"],
                    "name": data["device"]["name"],
                }

        _LOGGER.debug("No local Drift Beacon add-on detected")
        return None
