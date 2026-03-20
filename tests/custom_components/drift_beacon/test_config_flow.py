"""Tests for Drift Beacon authentication flows."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from homeassistant.const import CONF_HOST, CONF_PORT

from custom_components.drift_beacon.config_flow import DriftBeaconConfigFlow
from custom_components.drift_beacon.const import (
    CONF_API_TOKEN,
    CONF_PROTOCOL,
)


def access_error(status: int) -> aiohttp.ClientResponseError:
    """Build an HTTP error raised when WebSocket upgrade is rejected."""
    return aiohttp.ClientResponseError(
        request_info=SimpleNamespace(real_url="https://example.test/api/ws"),
        history=(),
        status=status,
        message="Invalid API key or workspace access denied",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_user_flow_reports_invalid_credentials(status: int) -> None:
    """An auth rejection during configuration should show invalid_auth."""
    flow = DriftBeaconConfigFlow()
    flow._detected_hub = {}
    flow._detect_protocol_parallel = AsyncMock(
        return_value=("https", {"device": {"id": "hub-1", "name": "Drift Beacon"}})
    )
    flow._validate_token = AsyncMock(side_effect=access_error(status))

    result = await flow.async_step_user(
        {
            CONF_HOST: "example.test",
            CONF_PORT: 9000,
            CONF_API_TOKEN: "invalid",
        }
    )

    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_reauth_flow_reports_invalid_credentials(status: int) -> None:
    """A rejected replacement token should keep reauthentication open."""
    flow = DriftBeaconConfigFlow()
    flow._reauth_entry_data = {
        CONF_HOST: "example.test",
        CONF_PORT: 9000,
        CONF_PROTOCOL: "https",
    }
    flow._validate_token = AsyncMock(side_effect=access_error(status))

    result = await flow.async_step_reauth_confirm(
        {CONF_API_TOKEN: "still-invalid"}
    )

    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.asyncio
async def test_valid_reauth_token_updates_and_reloads_entry() -> None:
    """A valid replacement token should update and reload the config entry."""
    flow = DriftBeaconConfigFlow()
    flow._reauth_entry_data = {
        CONF_HOST: "example.test",
        CONF_PORT: 9000,
        CONF_PROTOCOL: "https",
    }
    flow._validate_token = AsyncMock(return_value=None)
    entry = object()
    flow._get_reauth_entry = Mock(return_value=entry)
    flow.async_update_reload_and_abort = Mock(
        return_value={"type": "abort", "reason": "reauth_successful"}
    )

    result = await flow.async_step_reauth_confirm(
        {CONF_API_TOKEN: "replacement-token"}
    )

    assert result["reason"] == "reauth_successful"
    flow.async_update_reload_and_abort.assert_called_once_with(
        entry,
        data_updates={CONF_API_TOKEN: "replacement-token"},
    )
