"""Config flow for Drift Beacon integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components.network import async_get_source_ip
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.exceptions import ConfigEntryAuthFailed

from .const import (
    API_AUTH_CREATE_SERVER_SESSION,
    API_AUTH_SIGN_IN,
    API_SYSTEM_STATUS,
    API_TIMEOUT,
    CONF_EMAIL,
    CONF_HUB_ID,
    CONF_HUB_NAME,
    CONF_PASSWORD,
    CONF_PROTOCOL,
    CONF_SESSION_EXPIRES,
    CONF_SESSION_TOKEN,
    CONF_USER_ID,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DETECTION_CANDIDATES,
    DETECTION_TIMEOUT,
    DOMAIN,
    PROTOCOL_DETECTION_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


class InvalidAuthError(Exception):
    """Error to indicate authentication failure."""


class SessionCreationError(Exception):
    """Error to indicate server session creation failure."""


class DriftBeaconConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Drift Beacon."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._detected_hub: dict[str, Any] | None = None
        self._reauth_entry_data: dict[str, Any] = {}
        self._webui_url: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        # Get webui URL if not already done
        if self._webui_url is None:
            self._webui_url = await self._get_webui_url()

        # Detect local hub if not already done
        if self._detected_hub is None:
            self._detected_hub = await self._detect_local_addon()

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]

            try:
                # Authenticate and create server session (protocol auto-detected)
                auth_data = await self._authenticate_and_create_session(
                    email, password, host, port
                )

                # Set unique ID to prevent duplicate entries
                unique_id = f"{auth_data['hub_id']}_{auth_data['user_id']}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                # Create config entry with auth data including detected protocol
                return self.async_create_entry(
                    title=f"{auth_data['user_email']} @ {auth_data['hub_name']}",
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_PROTOCOL: auth_data["protocol"],
                        CONF_EMAIL: auth_data["user_email"],
                        CONF_USER_ID: auth_data["user_id"],
                        CONF_SESSION_TOKEN: auth_data["session_token"],
                        CONF_SESSION_EXPIRES: auth_data["expires_at"],
                        CONF_HUB_ID: auth_data["hub_id"],
                        CONF_HUB_NAME: auth_data["hub_name"],
                    },
                )

            except aiohttp.ClientConnectionError:
                errors["base"] = "cannot_connect"
            except aiohttp.ClientResponseError as err:
                if err.status == 404:
                    errors["base"] = "invalid_server"
                else:
                    errors["base"] = "server_error"
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except SessionCreationError:
                errors["base"] = "session_creation_failed"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        # Prepare form data
        detected_info = ""
        default_host = DEFAULT_HOST
        default_port = DEFAULT_PORT

        if self._detected_hub:
            detected_info = f"✓ Local add-on detected at {self._detected_hub['url']}"
            # Parse detected URL (protocol already included)
            url_parts = (
                self._detected_hub["url"]
                .replace("https://", "")
                .replace("http://", "")
                .split(":")
            )
            default_host = url_parts[0]
            default_port = int(url_parts[1]) if len(url_parts) > 1 else DEFAULT_PORT

        # Show form
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=default_host): str,
                    vol.Required(CONF_PORT, default=default_port): int,
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={
                "detected_info": detected_info,
                "webui_url": self._webui_url or "N/A",
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

        stored_email = self._reauth_entry_data[CONF_EMAIL]
        stored_user_id = self._reauth_entry_data[CONF_USER_ID]
        host = self._reauth_entry_data[CONF_HOST]
        port = self._reauth_entry_data[CONF_PORT]
        # Use stored protocol for reauth
        protocol = self._reauth_entry_data.get(CONF_PROTOCOL, "https")

        if user_input is not None:
            try:
                # Re-authenticate with stored email + new password + stored protocol
                auth_data = await self._authenticate_and_create_session(
                    email=stored_email,
                    password=user_input[CONF_PASSWORD],
                    host=host,
                    port=port,
                    protocol=protocol,
                )

                # Security: verify same user
                if auth_data["user_id"] != stored_user_id:
                    errors["base"] = "wrong_account"
                else:
                    # Update entry with new token
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(),
                        data_updates={
                            CONF_SESSION_TOKEN: auth_data["session_token"],
                            CONF_SESSION_EXPIRES: auth_data["expires_at"],
                        },
                    )

            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except SessionCreationError:
                errors["base"] = "session_creation_failed"
            except aiohttp.ClientConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception during reauth")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"email": stored_email},
            errors=errors,
        )

    # ============================================================================
    # Protocol Detection Abstraction Layer
    # ============================================================================

    async def _try_protocol(
        self, protocol: str, host: str, port: int, endpoint: str
    ) -> dict[str, Any] | None:
        """Try a specific protocol for an endpoint.

        Args:
            protocol: "https" or "http"
            host: Hostname or IP
            port: Port number
            endpoint: API endpoint to test

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
                ssl=False,  # Don't verify SSL during detection
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

        This eliminates waiting time - whichever protocol responds first wins!

        Args:
            host: Hostname or IP
            port: Port number
            endpoint: API endpoint to test

        Returns:
            Tuple of (protocol, response_data) if successful, None otherwise
        """
        # Create tasks for both protocols
        https_task = asyncio.create_task(
            self._try_protocol("https", host, port, endpoint)
        )
        http_task = asyncio.create_task(
            self._try_protocol("http", host, port, endpoint)
        )

        tasks = [https_task, http_task]

        # Wait for first successful response
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        # Check first completed task
        for task in done:
            result = task.result()
            if result is not None:
                # Success! Cancel the other task
                for pending_task in pending:
                    pending_task.cancel()

                # Determine which protocol succeeded
                protocol = "https" if task == https_task else "http"
                _LOGGER.debug("Protocol detection: %s succeeded first", protocol)
                return (protocol, result)

        # First task failed, wait for second
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
    # Hub Detection & Authentication
    # ============================================================================

    async def _get_webui_url(self) -> str | None:
        """Get the web UI URL for account signup."""
        try:
            source_ip = await async_get_source_ip(self.hass)
            if source_ip:
                # Transform 10.0.0.193 to 10-0-0-193
                ip_with_dashes = source_ip.replace(".", "-")
                webui_url = f"http://{ip_with_dashes}.local.driftbeacon.net:9000"
                _LOGGER.debug("Generated webui URL: %s", webui_url)
                return webui_url
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to get webui URL: %s", err)
        return None

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

    async def _authenticate_and_create_session(
        self,
        email: str,
        password: str,
        host: str,
        port: int,
        protocol: str | None = None,
    ) -> dict[str, Any]:
        """Authenticate and create server session with auto protocol detection.

        Args:
            email: User email
            password: User password
            host: Server hostname/IP
            port: Server port
            protocol: Optional protocol ("https" or "http"). If None, auto-detects.

        Returns:
            Dict containing auth data including detected/used protocol
        """
        # If protocol not specified, try HTTPS first with quick timeout
        if protocol is None:
            _LOGGER.debug("Auto-detecting protocol for %s:%s", host, port)

            # Try HTTPS first (more secure) with short timeout
            try:
                result = await asyncio.wait_for(
                    self._do_auth("https", host, port, email, password),
                    timeout=PROTOCOL_DETECTION_TIMEOUT,
                )
                result["protocol"] = "https"
                return result
            except (
                asyncio.TimeoutError,
                aiohttp.ClientSSLError,
                aiohttp.ClientConnectorError,
            ) as err:
                _LOGGER.debug("HTTPS failed, trying HTTP: %s", err)

            # Fallback to HTTP
            protocol = "http"

        # Use specified or detected protocol
        result = await self._do_auth(protocol, host, port, email, password)
        result["protocol"] = protocol
        return result

    async def _do_auth(
        self, protocol: str, host: str, port: int, email: str, password: str
    ) -> dict[str, Any]:
        """Perform authentication flow with specified protocol.

        This is the core authentication logic separated for testability.
        """
        base_url = f"{protocol}://{host}:{port}"

        # Create temporary session with cookie jar ONLY for this auth flow
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar()
        ) as temp_session:

            # Step 1: Get hub identity
            _LOGGER.debug("Getting hub identity from %s", base_url)
            async with temp_session.get(
                f"{base_url}{API_SYSTEM_STATUS}",
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                ssl=False,  # Don't verify SSL
            ) as response:
                response.raise_for_status()
                hub_info = await response.json()

            # Step 2: Sign in - cookie automatically stored in jar
            _LOGGER.debug("Signing in as %s", email)
            async with temp_session.post(
                f"{base_url}{API_AUTH_SIGN_IN}",
                json={"email": email, "password": password},
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                ssl=False,
            ) as response:
                if response.status == 401:
                    raise InvalidAuthError("Invalid credentials")
                response.raise_for_status()
                user_data = await response.json()

            # Step 3: Create server session - cookie automatically sent
            server_id = (
                f"homeassistant_{hub_info["device"]["id"]}_{user_data['user']['id']}"
            )
            _LOGGER.debug("Creating server session with ID: %s", server_id)

            async with temp_session.post(
                f"{base_url}{API_AUTH_CREATE_SERVER_SESSION}",
                json={
                    "serverId": server_id,
                    "serverName": "Home Assistant",
                    "expiresInDays": 365,
                },
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                ssl=False,
            ) as response:
                if response.status == 401:
                    raise SessionCreationError("Failed to create server session")
                response.raise_for_status()
                session_data = await response.json()

            _LOGGER.info(
                "Successfully authenticated %s and created server session", email
            )

            return {
                "user_id": user_data["user"]["id"],
                "user_email": user_data["user"]["email"],
                "session_token": session_data["serverSessionToken"],
                "expires_at": session_data["expiresAt"],
                "hub_id": hub_info["device"]["id"],
                "hub_name": hub_info["device"]["name"],
            }
