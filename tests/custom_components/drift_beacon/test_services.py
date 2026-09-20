"""Tests for Drift Beacon service actions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.drift_beacon.const import ATTR_PAUSE, DOMAIN
from custom_components.drift_beacon.services import (
    ACTIVITY_SCHEMA,
    ACTIVITY_SERVICES,
    STOP_SESSION_SCHEMA,
    async_handle_activity,
    async_handle_stop_session,
    async_setup_services,
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

    await async_handle_stop_session(
        fake_hass(entry), service_call(**{ATTR_PAUSE: True})
    )

    entry.runtime_data.pause_session.assert_awaited_once_with()
    entry.runtime_data.stop_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_loaded_drift_beacon_entries_are_targeted(targeted) -> None:
    """Other integrations and unloaded workspaces are ignored."""
    loaded = fake_entry("entry-1")
    other = fake_entry("entry-2", domain="mqtt")
    unloaded = fake_entry("entry-3", state=ConfigEntryState.SETUP_RETRY)
    targeted("entry-1", "entry-2", "entry-3")

    await async_handle_stop_session(fake_hass(loaded, other, unloaded), service_call())

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


@pytest.fixture
def activity_context(monkeypatch: pytest.MonkeyPatch):
    """Use renamed entities and registry identities, not display names or states."""
    entry = fake_entry("entry-1")
    manager = entry.runtime_data
    manager.workspace_id = "workspace-1"
    manager.available = True
    activities = {
        "span-1": {
            "id": "span-1",
            "name": "Reading",
            "tracking_type": "span",
            "archived": False,
        },
        "point-1": {
            "id": "point-1",
            "name": "Water",
            "tracking_type": "point",
            "archived": False,
        },
    }
    manager.get_activity = Mock(side_effect=activities.get)
    manager.get_live_session = Mock(return_value=None)
    manager.get_pinned_activity = Mock(return_value=None)
    for method in (
        "start_session",
        "mark_activity",
        "pin_activity",
        "unpin_activity",
        "queue_activity",
    ):
        setattr(manager, method, AsyncMock(return_value=True))
    entities = {}
    for entity_id, identity in (
        ("switch.renamed", "session:span-1"),
        ("switch.pin", "pin:span-1"),
        ("button.water", "mark:point-1"),
        ("switch.water_pin", "pin:point-1"),
        ("button.workspace_stop", "stop_session"),
    ):
        entities[entity_id] = SimpleNamespace(
            platform=DOMAIN,
            config_entry_id=entry.entry_id,
            unique_id=f"workspace-1:{identity}",
        )
    registry = SimpleNamespace(async_get=Mock(side_effect=entities.get))
    monkeypatch.setattr(
        "custom_components.drift_beacon.services.er.async_get", lambda hass: registry
    )
    return fake_hass(entry), entry, entities, activities


def activity_call(service: str, *entities: str) -> SimpleNamespace:
    """Apply the activity service schema to its explicit entity targets."""
    return SimpleNamespace(
        service=service, data=ACTIVITY_SCHEMA({"entity_id": list(entities)})
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service", "entity_id", "live", "expected", "activity_id"),
    [
        ("track_activity", "switch.renamed", None, "start_session", "span-1"),
        ("track_activity", "switch.renamed", "span-1", "stop_session", "span-1"),
        ("track_activity", "switch.renamed", "other", "start_session", "span-1"),
        ("track_activity", "button.water", "span-1", "mark_activity", "point-1"),
        ("track_activity", "switch.water_pin", "span-1", "mark_activity", "point-1"),
        ("track_activity", "switch.pin", "span-1", "stop_session", "span-1"),
        ("pause_activity", "switch.renamed", "span-1", "pause_session", "span-1"),
        ("pause_activity", "switch.renamed", "other", None, "span-1"),
        ("pause_activity", "switch.renamed", None, None, "span-1"),
        ("pause_activity", "button.water", "span-1", None, "point-1"),
        ("pin_activity", "switch.renamed", None, "pin_activity", "span-1"),
        ("queue_activity", "switch.renamed", None, "queue_activity", "span-1"),
        ("queue_activity", "button.water", "span-1", "queue_activity", "point-1"),
    ],
)
async def test_activity_actions_preserve_tracking_and_scope(
    activity_context, service, entity_id, live, expected, activity_id
) -> None:
    """Track toggles spans/marks points; Pause cannot end an unrelated live session."""
    hass, entry, _, _ = activity_context
    manager = entry.runtime_data
    manager.get_live_session.return_value = {"activity_id": live} if live else None
    await async_handle_activity(hass, activity_call(service, entity_id))
    for method in (
        "start_session",
        "stop_session",
        "pause_session",
        "mark_activity",
        "pin_activity",
        "unpin_activity",
        "queue_activity",
    ):
        if method == expected:
            getattr(manager, method).assert_awaited_once_with(activity_id)
        else:
            getattr(manager, method).assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [None, "other", "span-1"])
async def test_unpin_only_affects_the_selected_activity(
    activity_context, pinned
) -> None:
    """Unpin never clears another activity's pinned slot."""
    hass, entry, _, _ = activity_context
    manager = entry.runtime_data
    manager.get_pinned_activity.return_value = (
        {"activity_id": pinned} if pinned else None
    )
    await async_handle_activity(hass, activity_call("unpin_activity", "switch.renamed"))
    if pinned == "span-1":
        manager.unpin_activity.assert_awaited_once_with("span-1")
    else:
        manager.unpin_activity.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_entities_only_apply_the_action_once(activity_context) -> None:
    """Session and pin entities for one activity must not double-toggle or double-queue."""
    hass, entry, _, _ = activity_context
    await async_handle_activity(
        hass, activity_call("queue_activity", "switch.renamed", "switch.pin")
    )
    entry.runtime_data.queue_activity.assert_awaited_once_with("span-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["button.workspace_stop", "switch.missing"])
async def test_invalid_target_prevents_all_activity_actions(
    activity_context, invalid
) -> None:
    """A workspace control is not an activity, even if it exposes activity metadata."""
    hass, entry, _, _ = activity_context
    with pytest.raises(ServiceValidationError):
        await async_handle_activity(
            hass, activity_call("track_activity", "switch.renamed", invalid)
        )
    entry.runtime_data.start_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    ["foreign", "unloaded", "archived", "offline", "wrong_workspace", "deleted"],
)
async def test_unusable_activity_targets_are_rejected(activity_context, reason) -> None:
    """Validate registry ownership, connection state, and activity availability."""
    hass, entry, entities, activities = activity_context
    if reason == "foreign":
        entities["switch.renamed"].platform = "other"
    elif reason == "unloaded":
        entry.state = ConfigEntryState.SETUP_RETRY
    elif reason == "archived":
        activities["span-1"]["archived"] = True
    elif reason == "offline":
        entry.runtime_data.available = False
    elif reason == "wrong_workspace":
        entities["switch.renamed"].unique_id = "different:session:span-1"
    elif reason == "deleted":
        del activities["span-1"]
    with pytest.raises(HomeAssistantError):
        await async_handle_activity(
            hass, activity_call("track_activity", "switch.renamed")
        )
    entry.runtime_data.start_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_activity_rpc_failure_is_visible_in_automation_trace(
    activity_context,
) -> None:
    """Do not report success when the server rejects the action."""
    hass, entry, _, _ = activity_context
    entry.runtime_data.queue_activity.return_value = False
    with pytest.raises(HomeAssistantError, match="queue activity"):
        await async_handle_activity(
            hass, activity_call("queue_activity", "switch.renamed")
        )


def test_activity_service_requires_explicit_entity_targets() -> None:
    """A workspace or area must not accidentally act on every activity."""
    with pytest.raises(vol.Invalid):
        ACTIVITY_SCHEMA({"device_id": "workspace-device"})


def test_all_activity_services_are_registered() -> None:
    """The blueprint's service names are installed through integration setup."""
    hass = SimpleNamespace(services=SimpleNamespace(async_register=Mock()))
    async_setup_services(hass)
    registered = {c.args[1] for c in hass.services.async_register.call_args_list}
    assert registered == {"stop_session", *ACTIVITY_SERVICES}
