"""Tests for Drift Beacon authentication flows."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from homeassistant.const import CONF_HOST, CONF_PORT

from custom_components.drift_beacon.config_flow import DriftBeaconConfigFlow
from custom_components.drift_beacon.const import (
    CONF_API_TOKEN,
    CONF_HUB_ID,
    CONF_PROTOCOL,
    CONF_USER_ID,
    CONF_USER_NAME,
    CONF_WORKSPACE_ID,
    CONF_WORKSPACE_NAME,
)

CONNECTION_INFO = {
    "workspaceId": "workspace-1",
    "workspaceName": "Personal",
    "userId": "user-1",
    "userName": "Rich",
}


@pytest.mark.asyncio
async def test_connection_info_rpc_returns_workspace_and_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup identity is read from the authenticated WebSocket RPC."""
    websocket = SimpleNamespace(
        send_json=AsyncMock(),
        receive=AsyncMock(
            return_value=SimpleNamespace(
                type=aiohttp.WSMsgType.TEXT,
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "result": CONNECTION_INFO}),
            )
        ),
        close=AsyncMock(),
    )
    session = SimpleNamespace(ws_connect=AsyncMock(return_value=websocket))
    monkeypatch.setattr(
        "custom_components.drift_beacon.config_flow.async_get_clientsession",
        lambda _hass: session,
    )
    flow = DriftBeaconConfigFlow()
    flow.hass = object()

    result = await flow._get_connection_info(
        "https", "example.test", 9000, "workspace-token"
    )

    assert result == CONNECTION_INFO
    websocket.send_json.assert_awaited_once_with(
        {
            "jsonrpc": "2.0",
            "method": "GetConnectionInfo",
            "params": {},
            "id": 1,
        }
    )
    websocket.close.assert_awaited_once()


def access_error(status: int) -> aiohttp.ClientResponseError:
    """Build an HTTP error raised when WebSocket upgrade is rejected."""
    return aiohttp.ClientResponseError(
        request_info=SimpleNamespace(real_url="https://example.test/api/ws"),
        history=(),
        status=status,
        message="Invalid API key or workspace access denied",
    )


def configured_user_flow(
    connection_info: dict[str, str], hub_id: str = "hub-1"
) -> DriftBeaconConfigFlow:
    """Build a config flow with server and identity discovery stubbed."""
    flow = DriftBeaconConfigFlow()
    flow.context = {"source": "user"}
    flow._detected_hub = {}
    flow._detect_protocol_parallel = AsyncMock(
        return_value=("https", {"device": {"id": hub_id, "name": "Beacon"}})
    )
    flow._get_connection_info = AsyncMock(return_value=connection_info)

    async def set_unique_id(value: str) -> None:
        flow.context["unique_id"] = value

    flow.async_set_unique_id = AsyncMock(side_effect=set_unique_id)
    return flow


@pytest.mark.asyncio
async def test_same_hub_supports_distinct_workspace_entries() -> None:
    """Globally unique workspace IDs create independent config entries."""
    first = configured_user_flow(CONNECTION_INFO)
    first._async_current_entries = Mock(return_value=[])
    second = configured_user_flow(
        {
            **CONNECTION_INFO,
            "workspaceId": "workspace-2",
            "workspaceName": "Family",
        }
    )
    second._async_current_entries = Mock(return_value=[])
    user_input = {
        CONF_HOST: "example.test",
        CONF_PORT: 9000,
        CONF_API_TOKEN: "token",
    }

    first_result = await first.async_step_user(user_input)
    second_result = await second.async_step_user(user_input)

    assert first.unique_id == "workspace-1"
    assert second.unique_id == "workspace-2"
    assert first_result["title"] == "Personal"
    assert second_result["title"] == "Family"
    assert first_result["data"][CONF_HUB_ID] == "hub-1"
    assert first_result["data"][CONF_USER_NAME] == "Rich"


@pytest.mark.asyncio
async def test_duplicate_workspace_identifies_the_connected_user() -> None:
    """A workspace stays unique even when discovered through another hub."""
    flow = configured_user_flow(
        {**CONNECTION_INFO, "userId": "user-2", "userName": "Priya"},
        hub_id="replacement-hub",
    )
    flow._async_current_entries = Mock(
        return_value=[
            SimpleNamespace(unique_id="workspace-1", data={CONF_USER_NAME: "Rich"})
        ]
    )

    result = await flow.async_step_user(
        {
            CONF_HOST: "example.test",
            CONF_PORT: 9000,
            CONF_API_TOKEN: "second-user-token",
        }
    )

    assert result["reason"] == "already_configured"
    assert result["description_placeholders"] == {"user_name": "Rich"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_user_flow_reports_invalid_credentials(status: int) -> None:
    """An auth rejection during configuration should show invalid_auth."""
    flow = DriftBeaconConfigFlow()
    flow._detected_hub = {}
    flow._detect_protocol_parallel = AsyncMock(
        return_value=("https", {"device": {"id": "hub-1", "name": "Drift Beacon"}})
    )
    flow._get_connection_info = AsyncMock(side_effect=access_error(status))

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
        CONF_WORKSPACE_ID: "workspace-1",
        CONF_WORKSPACE_NAME: "Personal",
        CONF_USER_ID: "user-1",
        CONF_USER_NAME: "Rich",
    }
    flow._get_connection_info = AsyncMock(side_effect=access_error(status))

    result = await flow.async_step_reauth_confirm({CONF_API_TOKEN: "still-invalid"})

    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.asyncio
async def test_valid_reauth_token_updates_and_reloads_entry() -> None:
    """A valid replacement token should update and reload the config entry."""
    flow = DriftBeaconConfigFlow()
    flow._reauth_entry_data = {
        CONF_HOST: "example.test",
        CONF_PORT: 9000,
        CONF_PROTOCOL: "https",
        CONF_WORKSPACE_ID: "workspace-1",
        CONF_WORKSPACE_NAME: "Personal",
        CONF_USER_ID: "user-1",
        CONF_USER_NAME: "Rich",
    }
    flow._get_connection_info = AsyncMock(
        return_value={**CONNECTION_INFO, "userId": "user-2", "userName": "Priya"}
    )
    entry = object()
    flow._get_reauth_entry = Mock(return_value=entry)
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=Mock())
    )
    flow.async_update_reload_and_abort = Mock(
        return_value={"type": "abort", "reason": "reauth_successful"}
    )

    result = await flow.async_step_reauth_confirm({CONF_API_TOKEN: "replacement-token"})

    assert result["reason"] == "reauth_successful"
    flow.async_update_reload_and_abort.assert_called_once_with(
        entry,
        data_updates={
            CONF_API_TOKEN: "replacement-token",
            CONF_WORKSPACE_NAME: "Personal",
            CONF_USER_ID: "user-2",
            CONF_USER_NAME: "Priya",
        },
    )
    flow.hass.config_entries.async_update_entry.assert_called_once_with(
        entry, title="Personal"
    )


@pytest.mark.asyncio
async def test_reauth_rejects_a_token_for_another_workspace() -> None:
    """Reauthentication may replace the user but never the workspace."""
    flow = DriftBeaconConfigFlow()
    flow._reauth_entry_data = {
        CONF_HOST: "example.test",
        CONF_PORT: 9000,
        CONF_PROTOCOL: "https",
        CONF_WORKSPACE_ID: "workspace-1",
    }
    flow._get_connection_info = AsyncMock(
        return_value={**CONNECTION_INFO, "workspaceId": "workspace-2"}
    )

    result = await flow.async_step_reauth_confirm({CONF_API_TOKEN: "wrong-workspace"})

    assert result["errors"] == {"base": "wrong_workspace"}
