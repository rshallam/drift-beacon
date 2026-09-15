"""Tests for Drift Beacon service actions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.drift_beacon.const import ATTR_PAUSE, DOMAIN
from custom_components.drift_beacon.services import (
    STOP_SESSION_SCHEMA,
    async_handle_stop_session,
)


def fake_entry(
    entry_id: str,
    domain: str = DOMAIN,
    state: ConfigEntryState = ConfigEntryState.LOADED,
    success: bool = True,
) -> SimpleNamespace:
    """Build a config entry whose manager records stop and pause calls."""
    return SimpleNamespace(
        entry_id=entry_id,
        domain=domain,
        state=state,
        title=f"Workspace {entry_id}",
        runtime_data=SimpleNamespace(
            stop_session=AsyncMock(return_value=success),
            pause_session=AsyncMock(return_value=success),
        ),
    )


def fake_hass(*entries: SimpleNamespace) -> SimpleNamespace:
    """Build a Home Assistant stand-in that can look up config entries."""
    by_id = {entry.entry_id: entry for entry in entries}
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_get_entry=Mock(side_effect=by_id.get))
    )


@pytest.fixture
def targeted(monkeypatch: pytest.MonkeyPatch):
    """Control which config entries the service call's target resolves to."""

    def _target(*entry_ids: str) -> None:
        monkeypatch.setattr(
            "custom_components.drift_beacon.services.async_extract_config_entry_ids",
            AsyncMock(return_value=set(entry_ids)),
        )

    return _target


def service_call(**data) -> SimpleNamespace:
    """Build a service call with schema defaults applied."""
    return SimpleNamespace(data=STOP_SESSION_SCHEMA(data))


@pytest.mark.asyncio
async def test_stop_session_stops_the_targeted_workspace(targeted) -> None:
    """Without pause, the targeted workspace's live session is stopped."""
    entry = fake_entry("entry-1")
    targeted("entry-1")

    await async_handle_stop_session(fake_hass(entry), service_call())

    entry.runtime_data.stop_session.assert_awaited_once_with()
    entry.runtime_data.pause_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_flag_pauses_instead_of_stopping(targeted) -> None:
    """With pause, the live session ends and its activity is re-pinned."""
    entry = fake_entry("entry-1")
    targeted("entry-1")

    await async_handle_stop_session(fake_hass(entry), service_call(**{ATTR_PAUSE: True}))

    entry.runtime_data.pause_session.assert_awaited_once_with()
    entry.runtime_data.stop_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_loaded_drift_beacon_entries_are_targeted(targeted) -> None:
    """Other integrations and unloaded workspaces are ignored."""
    loaded = fake_entry("entry-1")
    other = fake_entry("entry-2", domain="mqtt")
    unloaded = fake_entry("entry-3", state=ConfigEntryState.SETUP_RETRY)
    targeted("entry-1", "entry-2", "entry-3")

    await async_handle_stop_session(
        fake_hass(loaded, other, unloaded), service_call()
    )

    loaded.runtime_data.stop_session.assert_awaited_once()
    other.runtime_data.stop_session.assert_not_awaited()
    unloaded.runtime_data.stop_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_target_without_a_workspace_is_rejected(targeted) -> None:
    """A target that resolves to no connected workspace is a validation error."""
    targeted("entry-2")

    with pytest.raises(ServiceValidationError):
        await async_handle_stop_session(
            fake_hass(fake_entry("entry-2", domain="mqtt")), service_call()
        )


@pytest.mark.asyncio
async def test_failed_rpc_surfaces_as_an_error(targeted) -> None:
    """A failed stop is raised so automation traces show it."""
    targeted("entry-1")

    with pytest.raises(HomeAssistantError):
        await async_handle_stop_session(
            fake_hass(fake_entry("entry-1", success=False)), service_call()
        )
