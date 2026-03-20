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
    CONF_PORT,
    CONF_PROTOCOL,
    EVENT_ACTIVITY_ARMED,
    EVENT_ACTIVITY_DISARMED,
    EVENT_SESSION_STOPPED,
    WS_RECONNECT_MAX_DELAY,
)
from custom_components.drift_beacon.coordinator import (
    DriftBeaconWebSocketManager,
    _SubscriptionError,
    hex_to_rgb,
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

    def async_create_task(self, coro, name: str | None = None):
        """Create a named asyncio task."""
        return asyncio.create_task(coro, name=name)


class FakeConfigEntry:
    """Minimal config entry used by the manager."""

    def __init__(self) -> None:
        self.data = {
            CONF_API_TOKEN: "token",
            CONF_HOST: "example.test",
            CONF_PORT: 9000,
            CONF_PROTOCOL: "https",
        }
        self.reauth_calls = 0

    def async_start_reauth(self, hass) -> None:
        """Record a reauthentication request."""
        self.reauth_calls += 1


def create_manager() -> tuple[DriftBeaconWebSocketManager, FakeConfigEntry]:
    """Create a manager with minimal Home Assistant collaborators."""
    entry = FakeConfigEntry()
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
    workspace_id: str = "workspace-1", activity_name: str = "Focus"
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
                "workspaceName": "Personal",
                "activities": [
                    {
                        "id": "activity-1",
                        "name": activity_name,
                        "trackingType": "span",
                    }
                ],
                "categories": [],
                "liveSessions": [],
                "armedActivity": None,
            }
        ],
    }


def two_activity_snapshot_message() -> dict:
    """Build a subscription snapshot with two span activities for arming tests."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "chunk": True,
        "result": [
            {
                "_tag": "Snapshot",
                "workspaceId": "workspace-1",
                "workspaceName": "Personal",
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
                "armedActivity": None,
            }
        ],
    }


def armed_changed_message(
    activity_id: str | None, armed_at: str = "2026-08-14T10:00:00.000Z"
) -> str:
    """Build a raw ArmedActivityChanged stream message."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "chunk": True,
            "result": [
                {
                    "_tag": "ArmedActivityChanged",
                    "armedActivity": (
                        {"activityId": activity_id, "armedAt": armed_at}
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
    assert manager.get_armed_activity("workspace-1") is None


@pytest.mark.asyncio
async def test_armed_activity_snapshot_and_change_update_state() -> None:
    """Armed activity state should hydrate and follow stream changes."""
    manager, _ = create_manager()
    manager._subscription_rpc_id = 1
    message = snapshot_message()
    message["result"][0]["armedActivity"] = {
        "activityId": "activity-1",
        "armedAt": "2026-08-14T10:00:00.000Z",
    }

    manager._handle_message(json.dumps(message))

    assert manager.get_armed_activity("workspace-1") == {
        "activity_id": "activity-1",
        "armed_at": "2026-08-14T10:00:00.000Z",
    }

    manager._handle_message(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "chunk": True,
                "result": [
                    {
                        "_tag": "ArmedActivityChanged",
                        "armedActivity": None,
                    }
                ],
            }
        )
    )

    assert manager.get_armed_activity("workspace-1") is None


@pytest.mark.asyncio
async def test_activity_armed_and_disarmed_events_track_live_session_state() -> None:
    """Armed/disarmed events fire only on real transitions and carry has_live_session."""
    manager, _ = create_manager()
    manager._subscription_rpc_id = 1
    manager._handle_message(json.dumps(two_activity_snapshot_message()))

    # Arm with no live session -> event fires with has_live_session False and the
    # full activity/category/workspace payload.
    manager._handle_message(armed_changed_message("activity-1"))
    assert manager.hass.bus.fired == [
        (
            EVENT_ACTIVITY_ARMED,
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
                "armed_at": "2026-08-14T10:00:00.000Z",
                "has_live_session": False,
            },
        )
    ]
    manager.hass.bus.fired.clear()

    # Re-arming the same activity is not a transition -> nothing fires.
    manager._handle_message(armed_changed_message("activity-1"))
    assert manager.hass.bus.fired == []

    # A live session starts on a different activity. Poked directly (as other
    # tests in this file do) rather than replaying the full SessionStarted flow,
    # since only its effect on `get_live_session` matters here.
    manager._workspace_live_sessions["workspace-1"] = {
        "id": "session-1",
        "activity_id": "activity-2",
        "start_time": "2026-08-14T10:05:00.000Z",
    }

    # Displacing the armed activity while a session is live still updates state...
    manager._handle_message(
        armed_changed_message("activity-2", armed_at="2026-08-14T10:06:00.000Z")
    )
    assert manager.get_armed_activity("workspace-1") == {
        "activity_id": "activity-2",
        "armed_at": "2026-08-14T10:06:00.000Z",
    }
    # ...but the fired event reports a live session, so lighting automations no-op.
    assert len(manager.hass.bus.fired) == 1
    assert manager.hass.bus.fired[0][0] == EVENT_ACTIVITY_ARMED
    assert manager.hass.bus.fired[0][1]["has_live_session"] is True
    manager.hass.bus.fired.clear()

    # Disarming while the session is still live also reports has_live_session True.
    manager._handle_message(armed_changed_message(None))
    assert manager.hass.bus.fired == [
        (
            EVENT_ACTIVITY_DISARMED,
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
async def test_session_stopped_falls_back_to_activity_armed_during_the_session() -> None:
    """Ending a session hands lighting to whatever got armed while it was live."""
    manager, _ = create_manager()
    manager._subscription_rpc_id = 1
    manager._handle_message(json.dumps(two_activity_snapshot_message()))

    # Session live on activity-2, nothing armed -> stopping falls back to off.
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
            "armed_activity_id": None,
            "armed_activity_name": None,
            "armed_color": None,
        },
    )
    manager.hass.bus.fired.clear()

    # A session goes live, a *different* activity gets armed while it's live, then
    # the session ends -> the armed activity's lighting should take over instead of
    # turning off, regardless of when the arming happened relative to the session.
    manager._workspace_live_sessions["workspace-1"] = {
        "id": "session-2",
        "activity_id": "activity-2",
        "start_time": "2026-08-14T10:10:00.000Z",
    }
    manager._handle_message(
        armed_changed_message("activity-1", armed_at="2026-08-14T10:11:00.000Z")
    )
    manager.hass.bus.fired.clear()  # discard the has_live_session=True armed event

    manager._handle_message(session_ended_message("session-2", "activity-2"))
    assert manager.hass.bus.fired == [
        (
            EVENT_SESSION_STOPPED,
            {
                "activity_id": "activity-2",
                "activity_name": "Read",
                "workspace_id": "workspace-1",
                "workspace_name": "Personal",
                "armed_activity_id": "activity-1",
                "armed_activity_name": "Focus",
                "armed_color": hex_to_rgb("#4A90D9"),
            },
        )
    ]


@pytest.mark.asyncio
async def test_arm_and_disarm_rpc_payloads() -> None:
    """Armed activity actions should use the integration RPC contract."""
    manager, _ = create_manager()
    manager._send_rpc = AsyncMock(return_value={})

    assert await manager.arm_activity("activity-1")
    assert await manager.disarm_activity("activity-1")
    assert await manager.disarm_activity()

    assert manager._send_rpc.await_args_list == [
        call("ArmActivity", {"activityId": "activity-1"}),
        call("DisarmActivity", {"activityId": "activity-1"}),
        call("DisarmActivity", {}),
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
