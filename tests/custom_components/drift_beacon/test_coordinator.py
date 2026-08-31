"""Tests for the Drift Beacon WebSocket connection manager."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import aiohttp
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from custom_components.drift_beacon.const import (
    CONF_API_TOKEN,
    CONF_HOST,
    CONF_HUB_ID,
    CONF_PORT,
    CONF_PROTOCOL,
    CONF_USER_ID,
    CONF_USER_NAME,
    CONF_WORKSPACE_ID,
    CONF_WORKSPACE_NAME,
    EVENT_ACTIVITY_PINNED,
    EVENT_ACTIVITY_UNPINNED,
    EVENT_SESSION_STOPPED,
    WS_RECONNECT_MAX_DELAY,
)
from custom_components.drift_beacon.coordinator import (
    DriftBeaconWebSocketManager,
    _SubscriptionError,
    hex_to_rgb,
)


@pytest.fixture(autouse=True)
def mock_device_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep coordinator tests independent from Home Assistant storage setup."""
    registry = SimpleNamespace(
        async_get_device=Mock(return_value=None), async_update_device=Mock()
    )
    monkeypatch.setattr(
        "custom_components.drift_beacon.coordinator.dr.async_get",
        lambda _hass: registry,
    )


class FakeBus:
    """Collects fired Home Assistant events for assertions."""

    def __init__(self) -> None:
        self.fired: list[tuple[str, dict]] = []

    def async_fire(self, event_type: str, event_data: dict | None = None) -> None:
        """Record a fired event."""
        self.fired.append((event_type, event_data or {}))


class FakeHomeAssistant:
    """Minimal Home Assistant task interface used by the manager."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.bus = FakeBus()
        self.data = {}
        self.config_entries = SimpleNamespace(async_update_entry=Mock())

    def async_create_task(self, coro, name: str | None = None):
        """Create a named asyncio task."""
        return asyncio.create_task(coro, name=name)


class FakeConfigEntry:
    """Minimal config entry used by the manager."""

    def __init__(
        self,
        workspace_id: str = "workspace-1",
        workspace_name: str = "Personal",
        user_id: str = "user-1",
        user_name: str = "Rich",
    ) -> None:
        self.data = {
            CONF_API_TOKEN: "token",
            CONF_HOST: "example.test",
            CONF_PORT: 9000,
            CONF_PROTOCOL: "https",
            CONF_HUB_ID: "hub-1",
            CONF_WORKSPACE_ID: workspace_id,
            CONF_WORKSPACE_NAME: workspace_name,
            CONF_USER_ID: user_id,
            CONF_USER_NAME: user_name,
        }
        self.entry_id = f"entry-{workspace_id}"
        self.title = workspace_name
        self.reauth_calls = 0

    def async_start_reauth(self, hass) -> None:
        """Record a reauthentication request."""
        self.reauth_calls += 1


def create_manager(
    workspace_id: str = "workspace-1",
    workspace_name: str = "Personal",
    user_id: str = "user-1",
    user_name: str = "Rich",
) -> tuple[DriftBeaconWebSocketManager, FakeConfigEntry]:
    """Create a manager with minimal Home Assistant collaborators."""
    entry = FakeConfigEntry(workspace_id, workspace_name, user_id, user_name)
    return DriftBeaconWebSocketManager(FakeHomeAssistant(), entry), entry


def access_error(status: int) -> aiohttp.ClientResponseError:
    """Build an HTTP error raised when WebSocket upgrade is rejected."""
    return aiohttp.ClientResponseError(
        request_info=SimpleNamespace(real_url="https://example.test/api/ws"),
        history=(),
        status=status,
        message="Invalid API key or workspace access denied",
    )


def snapshot_message(
    workspace_id: str = "workspace-1",
    workspace_name: str = "Personal",
    user_id: str = "user-1",
    user_name: str = "Rich",
    activity_name: str = "Focus",
) -> dict:
    """Build a subscription snapshot response."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "chunk": True,
        "result": [
            {
                "_tag": "Snapshot",
                "workspaceId": workspace_id,
                "workspaceName": workspace_name,
                "userId": user_id,
                "userName": user_name,
                "activities": [
                    {
                        "id": "activity-1",
                        "name": activity_name,
                        "trackingType": "span",
                    }
                ],
                "categories": [],
                "liveSessions": [],
                "pinnedActivity": None,
            }
        ],
    }


def two_activity_snapshot_message() -> dict:
    """Build a subscription snapshot with two span activities for pinning tests."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "chunk": True,
        "result": [
            {
                "_tag": "Snapshot",
                "workspaceId": "workspace-1",
                "workspaceName": "Personal",
                "userId": "user-1",
                "userName": "Rich",
                "activities": [
                    {
                        "id": "activity-1",
                        "name": "Focus",
                        "trackingType": "span",
                        "color": "#4A90D9",
                    },
                    {
                        "id": "activity-2",
                        "name": "Read",
                        "trackingType": "span",
                        "color": "#7ED321",
                    },
                ],
                "categories": [],
                "liveSessions": [],
                "pinnedActivity": None,
            }
        ],
    }


def pinned_changed_message(
    activity_id: str | None, pinned_at: str = "2026-08-14T10:00:00.000Z"
) -> str:
    """Build a raw PinnedActivityChanged stream message."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "chunk": True,
            "result": [
                {
                    "_tag": "PinnedActivityChanged",
                    "pinnedActivity": (
                        {"activityId": activity_id, "pinnedAt": pinned_at}
                        if activity_id
                        else None
                    ),
                }
            ],
        }
    )


def session_ended_message(session_id: str, activity_id: str) -> str:
    """Build a raw SessionEnded stream message."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "chunk": True,
            "result": [
                {
                    "_tag": "SessionEnded",
                    "sessionId": session_id,
                    "activityId": activity_id,
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_initial_transport_failure_uses_home_assistant_retry() -> None:
    """A transient first failure must abort setup with ConfigEntryNotReady."""
    manager, _ = create_manager()
    manager._open_connection = AsyncMock(side_effect=OSError("offline"))

    with pytest.raises(ConfigEntryNotReady):
        await manager.async_connect()

    assert manager._connection_task is None


@pytest.mark.asyncio
async def test_supervisor_keeps_retrying_after_failed_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure inside a reconnect attempt must not suppress later attempts."""
    manager, _ = create_manager()
    attempts = 0
    third_attempt = asyncio.Event()
    original_sleep = asyncio.sleep

    async def open_connection() -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("still offline")
        third_attempt.set()
        await asyncio.Future()

    async def no_delay(_delay: float) -> None:
        await original_sleep(0)

    manager._open_connection = open_connection
    manager._initial_ready = None
    monkeypatch.setattr(
        "custom_components.drift_beacon.coordinator.asyncio.sleep", no_delay
    )

    task = asyncio.create_task(manager._connection_supervisor())
    await asyncio.wait_for(third_attempt.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert attempts == 3


@pytest.mark.asyncio
async def test_reconnect_backoff_starts_at_minimum_and_caps() -> None:
    """Backoff should start at one second and never exceed its cap."""
    manager, _ = create_manager()

    delays = [manager._next_reconnect_delay() for _ in range(5)]

    assert delays == [1, 2, 4, WS_RECONNECT_MAX_DELAY, WS_RECONNECT_MAX_DELAY]


@pytest.mark.asyncio
async def test_snapshot_restores_availability_and_replaces_state() -> None:
    """A recovered subscription snapshot should replace stale cached data."""
    manager, _ = create_manager()
    manager._subscription_rpc_id = 1
    manager._reconnect_attempt = 4
    manager._workspace_activities["old"] = {
        "old": {
            "id": "old",
            "name": "Old",
        }
    }
    manager._handle_message(json.dumps(snapshot_message(activity_name="Recovered")))

    assert manager.available
    assert manager._reconnect_attempt == 0
    assert manager.workspaces == [{"id": "workspace-1", "name": "Personal"}]
    assert manager.get_activity("activity-1")["name"] == "Recovered"
    assert manager.get_activity("old") is None
    assert manager.get_pinned_activity("workspace-1") is None


@pytest.mark.asyncio
async def test_snapshot_rejects_a_different_workspace() -> None:
    """A runtime token must never silently move an entry to another workspace."""
    manager, _ = create_manager()
    manager._subscription_rpc_id = 1

    with pytest.raises(ConfigEntryAuthFailed, match="different Drift Beacon workspace"):
        manager._handle_message(
            json.dumps(snapshot_message(workspace_id="workspace-2"))
        )


@pytest.mark.asyncio
async def test_snapshot_refreshes_workspace_and_user_names() -> None:
    """Friendly-name changes update runtime and config-entry metadata."""
    manager, entry = create_manager()
    manager._subscription_rpc_id = 1
    message = snapshot_message()
    snapshot = message["result"][0]
    snapshot["workspaceName"] = "Renamed Workspace"
    snapshot["userName"] = "Richard"

    manager._handle_message(json.dumps(message))

    assert manager.workspace_name == "Renamed Workspace"
    assert manager.user_id == "user-1"
    assert manager.user_name == "Richard"
    manager.hass.config_entries.async_update_entry.assert_called_once_with(
        entry,
        data={
            **entry.data,
            CONF_WORKSPACE_NAME: "Renamed Workspace",
            CONF_USER_NAME: "Richard",
        },
        title="Renamed Workspace",
    )


@pytest.mark.asyncio
async def test_two_workspace_managers_keep_identity_and_state_isolated() -> None:
    """Entries from one hub must never share personal or workspace state."""
    personal, _ = create_manager()
    family, _ = create_manager("workspace-2", "Family", "user-2", "Priya")
    personal._subscription_rpc_id = 1
    family._subscription_rpc_id = 1

    personal._handle_message(json.dumps(snapshot_message(activity_name="Focus")))
    family._handle_message(
        json.dumps(
            snapshot_message(
                workspace_id="workspace-2",
                workspace_name="Family",
                user_id="user-2",
                user_name="Priya",
                activity_name="Chores",
            )
        )
    )

    assert personal.workspace_id == "workspace-1"
    assert family.workspace_id == "workspace-2"
    assert personal.user_attributes == {"user_id": "user-1", "user_name": "Rich"}
    assert family.user_attributes == {"user_id": "user-2", "user_name": "Priya"}
    assert [activity["name"] for activity in personal.activities] == ["Focus"]
    assert [activity["name"] for activity in family.activities] == ["Chores"]


@pytest.mark.asyncio
async def test_pinned_activity_snapshot_and_change_update_state() -> None:
    """Pinned activity state should hydrate and follow stream changes."""
    manager, _ = create_manager()
    manager._subscription_rpc_id = 1
    message = snapshot_message()
    message["result"][0]["pinnedActivity"] = {
        "activityId": "activity-1",
        "pinnedAt": "2026-08-14T10:00:00.000Z",
    }

    manager._handle_message(json.dumps(message))

    assert manager.get_pinned_activity("workspace-1") == {
        "activity_id": "activity-1",
        "pinned_at": "2026-08-14T10:00:00.000Z",
    }

    manager._handle_message(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "chunk": True,
                "result": [
                    {
                        "_tag": "PinnedActivityChanged",
                        "pinnedActivity": None,
                    }
                ],
            }
        )
    )

    assert manager.get_pinned_activity("workspace-1") is None


@pytest.mark.asyncio
async def test_activity_pinned_and_unpinned_events_track_live_session_state() -> None:
    """Pinned/unpinned events fire only on real transitions and carry has_live_session."""
    manager, _ = create_manager()
    manager._subscription_rpc_id = 1
    manager._handle_message(json.dumps(two_activity_snapshot_message()))

    # Pin with no live session -> event fires with has_live_session False and the
    # full activity/category/workspace payload.
    manager._handle_message(pinned_changed_message("activity-1"))
    assert manager.hass.bus.fired == [
        (
            EVENT_ACTIVITY_PINNED,
            {
                "activity_id": "activity-1",
                "activity_name": "Focus",
                "color": hex_to_rgb("#4A90D9"),
                "icon": "mdi:circle",
                "category_id": None,
                "category_name": None,
                "category_icon": None,
                "category_color": None,
                "workspace_id": "workspace-1",
                "workspace_name": "Personal",
                "pinned_at": "2026-08-14T10:00:00.000Z",
                "has_live_session": False,
            },
        )
    ]
    manager.hass.bus.fired.clear()

    # Re-pinning the same activity is not a transition -> nothing fires.
    manager._handle_message(pinned_changed_message("activity-1"))
    assert manager.hass.bus.fired == []

    # A live session starts on a different activity. Poked directly (as other
    # tests in this file do) rather than replaying the full SessionStarted flow,
    # since only its effect on `get_live_session` matters here.
    manager._workspace_live_sessions["workspace-1"] = {
        "id": "session-1",
        "activity_id": "activity-2",
        "start_time": "2026-08-14T10:05:00.000Z",
    }

    # Displacing the pinned activity while a session is live still updates state...
    manager._handle_message(
        pinned_changed_message("activity-2", pinned_at="2026-08-14T10:06:00.000Z")
    )
    assert manager.get_pinned_activity("workspace-1") == {
        "activity_id": "activity-2",
        "pinned_at": "2026-08-14T10:06:00.000Z",
    }
    # ...but the fired event reports a live session, so lighting automations no-op.
    assert len(manager.hass.bus.fired) == 1
    assert manager.hass.bus.fired[0][0] == EVENT_ACTIVITY_PINNED
    assert manager.hass.bus.fired[0][1]["has_live_session"] is True
    manager.hass.bus.fired.clear()

    # Unpinning while the session is still live also reports has_live_session True.
    manager._handle_message(pinned_changed_message(None))
    assert manager.hass.bus.fired == [
        (
            EVENT_ACTIVITY_UNPINNED,
            {
                "activity_id": "activity-2",
                "activity_name": "Read",
                "workspace_id": "workspace-1",
                "workspace_name": "Personal",
                "has_live_session": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_session_stopped_falls_back_to_activity_pinned_during_the_session() -> (
    None
):
    """Ending a session hands lighting to whatever got pinned while it was live."""
    manager, _ = create_manager()
    manager._subscription_rpc_id = 1
    manager._handle_message(json.dumps(two_activity_snapshot_message()))

    # Session live on activity-2, nothing pinned -> stopping falls back to off.
    manager._workspace_live_sessions["workspace-1"] = {
        "id": "session-1",
        "activity_id": "activity-2",
        "start_time": "2026-08-14T10:00:00.000Z",
    }
    manager._handle_message(session_ended_message("session-1", "activity-2"))
    assert manager.hass.bus.fired[-1] == (
        EVENT_SESSION_STOPPED,
        {
            "activity_id": "activity-2",
            "activity_name": "Read",
            "workspace_id": "workspace-1",
            "workspace_name": "Personal",
            "pinned_activity_id": None,
            "pinned_activity_name": None,
            "pinned_color": None,
        },
    )
    manager.hass.bus.fired.clear()

    # A session goes live, a *different* activity gets pinned while it's live, then
    # the session ends -> the pinned activity's lighting should take over instead of
    # turning off, regardless of when the pinning happened relative to the session.
    manager._workspace_live_sessions["workspace-1"] = {
        "id": "session-2",
        "activity_id": "activity-2",
        "start_time": "2026-08-14T10:10:00.000Z",
    }
    manager._handle_message(
        pinned_changed_message("activity-1", pinned_at="2026-08-14T10:11:00.000Z")
    )
    manager.hass.bus.fired.clear()  # discard the has_live_session=True pinned event

    manager._handle_message(session_ended_message("session-2", "activity-2"))
    assert manager.hass.bus.fired == [
        (
            EVENT_SESSION_STOPPED,
            {
                "activity_id": "activity-2",
                "activity_name": "Read",
                "workspace_id": "workspace-1",
                "workspace_name": "Personal",
                "pinned_activity_id": "activity-1",
                "pinned_activity_name": "Focus",
                "pinned_color": hex_to_rgb("#4A90D9"),
            },
        )
    ]


@pytest.mark.asyncio
async def test_pin_and_unpin_rpc_payloads() -> None:
    """Pinned activity actions should use the integration RPC contract."""
    manager, _ = create_manager()
    manager._send_rpc = AsyncMock(return_value={})

    assert await manager.pin_activity("activity-1")
    assert await manager.unpin_activity("activity-1")
    assert await manager.unpin_activity()

    assert manager._send_rpc.await_args_list == [
        call("PinActivity", {"activityId": "activity-1"}),
        call("UnpinActivity", {"activityId": "activity-1"}),
        call("UnpinActivity", {}),
    ]


@pytest.mark.asyncio
async def test_subscription_error_forces_connection_retry() -> None:
    """A rejected Subscribe RPC should fail the connection attempt."""
    manager, _ = create_manager()
    manager._subscription_rpc_id = 7

    with pytest.raises(_SubscriptionError, match="subscription denied"):
        manager._handle_message(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "error": {"message": "subscription denied"},
                }
            )
        )


@pytest.mark.asyncio
async def test_missing_snapshot_times_out() -> None:
    """An open socket without a snapshot must not count as connected."""
    manager, _ = create_manager()
    manager._ws = SimpleNamespace(receive=AsyncMock(side_effect=asyncio.TimeoutError))

    with pytest.raises(asyncio.TimeoutError):
        await manager._receive_until_snapshot()

    assert not manager.available


@pytest.mark.asyncio
async def test_cleanup_marks_unavailable_and_fails_pending_rpc() -> None:
    """Disconnect cleanup should immediately update entities and RPC callers."""
    manager, _ = create_manager()
    listener = SimpleNamespace(calls=0)

    def on_update() -> None:
        listener.calls += 1

    manager._available = True
    manager.async_add_listener(on_update)
    future = asyncio.get_running_loop().create_future()
    manager._pending_responses[3] = future

    await manager._cleanup_connection(ConnectionError("lost"))

    assert not manager.available
    assert listener.calls == 1
    with pytest.raises(ConnectionError, match="lost"):
        await future


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_initial_auth_rejection_requires_reauthentication(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """An initial auth rejection must escape setup as ConfigEntryAuthFailed."""
    manager, entry = create_manager()
    ws_connect = AsyncMock(side_effect=access_error(status))
    monkeypatch.setattr(
        "custom_components.drift_beacon.coordinator.async_get_clientsession",
        lambda _hass: SimpleNamespace(ws_connect=ws_connect),
    )

    with pytest.raises(ConfigEntryAuthFailed, match="workspace access"):
        await manager.async_connect()

    assert ws_connect.await_count == 1
    assert entry.reauth_calls == 0
    assert manager._connection_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_runtime_auth_rejection_reauthenticates_once_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """An auth rejection should stop reconnecting and request reauthentication."""
    manager, entry = create_manager()
    manager._initial_ready = None
    ws_connect = AsyncMock(side_effect=access_error(status))
    reconnect_delay = Mock()
    monkeypatch.setattr(
        "custom_components.drift_beacon.coordinator.async_get_clientsession",
        lambda _hass: SimpleNamespace(ws_connect=ws_connect),
    )
    manager._next_reconnect_delay = reconnect_delay

    await manager._connection_supervisor()

    assert ws_connect.await_count == 1
    assert entry.reauth_calls == 1
    assert manager._intentional_disconnect
    reconnect_delay.assert_not_called()


@pytest.mark.asyncio
async def test_disconnect_cancels_connection_supervisor() -> None:
    """Integration unload should leave no connection task running."""
    manager, _ = create_manager()
    started = asyncio.Event()

    async def run_forever() -> None:
        started.set()
        await asyncio.Future()

    task = asyncio.create_task(run_forever())
    manager._connection_task = task
    await started.wait()

    await manager.async_disconnect()

    assert task.cancelled()
    assert manager._connection_task is None
