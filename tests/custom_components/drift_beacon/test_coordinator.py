"""Stream handling: events, reconnects, availability and device maintenance."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.drift_beacon.const import (
    EVENT_ACTIVITY_MARKED,
    EVENT_ACTIVITY_PINNED,
    EVENT_ACTIVITY_UNPINNED,
    EVENT_FOCUS_CHANGED,
    EVENT_SESSION_STARTED,
    EVENT_SESSION_STOPPED,
    UNAVAILABLE_GRACE_PERIOD,
)

from .conftest import (
    WORKSPACE_ID,
    FakeDriftBeacon,
    activity,
    device_for,
    entity_id_for,
    live_session,
    wait_for,
)


def _device(hass: HomeAssistant, activity_id: str) -> dr.DeviceEntry | None:
    return device_for(hass, f"{WORKSPACE_ID}:activity:{activity_id}")


def _capture(hass: HomeAssistant) -> dict[str, list[Event]]:
    return {
        name: async_capture_events(hass, name)
        for name in (
            EVENT_SESSION_STARTED,
            EVENT_SESSION_STOPPED,
            EVENT_ACTIVITY_PINNED,
            EVENT_ACTIVITY_UNPINNED,
            EVENT_ACTIVITY_MARKED,
            EVENT_FOCUS_CHANGED,
        )
    }


def _data(events: list[Event]) -> list[dict[str, Any]]:
    return [event.data for event in events]


@pytest.fixture
def events(hass: HomeAssistant) -> dict[str, list[Event]]:
    """Captured Drift Beacon bus events, registered before setup."""
    return _capture(hass)


async def test_initial_focus_is_announced_once_after_start(
    hass: HomeAssistant,
    events: dict[str, list[Event]],
    setup_integration: MockConfigEntry,
) -> None:
    """Lights resync on startup: one focus event, flagged initial, and no transitions."""
    assert _data(events[EVENT_FOCUS_CHANGED]) == [
        {
            "workspace_id": WORKSPACE_ID,
            "workspace_device_id": device_for(hass, WORKSPACE_ID).id,
            "activity_id": None,
            "activity_device_id": None,
            "activity_name": None,
            "color": None,
            "state": "idle",
            "previous_state": None,
            "previous_activity_id": None,
            "initial": True,
        }
    ]
    assert not events[EVENT_SESSION_STARTED]


async def test_switching_activity_is_one_focus_change(
    hass: HomeAssistant,
    events: dict[str, list[Event]],
    setup_integration: MockConfigEntry,
    server: FakeDriftBeacon,
) -> None:
    """Ended+Started in one chunk: stop and start events, but a single live→live focus."""
    await server.push(
        {"_tag": "SessionStarted", "session": live_session("s1", "reading")}
    )
    await wait_for(lambda: len(events[EVENT_FOCUS_CHANGED]) == 2)
    await server.push(
        {
            "_tag": "ActivityCreated",
            "activity": activity("run", "Run", color="#ff0000"),
        },
    )
    await wait_for(lambda: _device(hass, "run") is not None)
    await server.push(
        {"_tag": "SessionEnded", "sessionId": "s1", "activityId": "reading"},
        {"_tag": "SessionStarted", "session": live_session("s2", "run")},
    )
    await wait_for(lambda: len(events[EVENT_FOCUS_CHANGED]) == 3)

    focus = events[EVENT_FOCUS_CHANGED][-1].data
    assert focus["state"] == "live"
    assert focus["previous_state"] == "live"
    assert focus["previous_activity_id"] == "reading"
    assert focus["activity_id"] == "run"
    assert focus["activity_device_id"] == _device(hass, "run").id
    assert focus["color"] == [255, 0, 0]
    assert focus["initial"] is False
    assert [e["session_id"] for e in _data(events[EVENT_SESSION_STOPPED])] == ["s1"]
    assert [e["session_id"] for e in _data(events[EVENT_SESSION_STARTED])] == [
        "s1",
        "s2",
    ]
    assert hass.states.get("switch.reading_session").state == "off"


async def test_pin_and_mark_events(
    hass: HomeAssistant,
    events: dict[str, list[Event]],
    setup_integration: MockConfigEntry,
    server: FakeDriftBeacon,
) -> None:
    """Pins report both sides of a change; marks come from PointMarked."""
    pinned = {"activityId": "reading", "pinnedAt": "2026-09-24T09:00:00Z"}
    await server.push({"_tag": "PinnedActivityChanged", "pinnedActivity": pinned})
    await server.push(
        {
            "_tag": "PinnedActivityChanged",
            "pinnedActivity": {
                "activityId": "water",
                "pinnedAt": "2026-09-24T09:01:00Z",
            },
        },
        {
            "_tag": "PointMarked",
            "session": {
                "id": "m1",
                "activityId": "water",
                "memberIds": ["user-1"],
                "markedAt": "2026-09-24T09:02:00Z",
            },
        },
    )
    await wait_for(lambda: len(events[EVENT_ACTIVITY_MARKED]) == 1)

    assert [e["activity_id"] for e in _data(events[EVENT_ACTIVITY_PINNED])] == [
        "reading",
        "water",
    ]
    assert [e["activity_id"] for e in _data(events[EVENT_ACTIVITY_UNPINNED])] == [
        "reading"
    ]
    mark = events[EVENT_ACTIVITY_MARKED][0].data
    assert mark["activity_name"] == "Water"
    assert mark["activity_device_id"] == _device(hass, "water").id
    assert [e["state"] for e in _data(events[EVENT_FOCUS_CHANGED])] == [
        "idle",
        "pinned",
        "pinned",
    ]
    assert hass.states.get("switch.water_pin").state == "on"
    assert hass.states.get("switch.reading_pin").state == "off"


async def test_deleting_a_live_activity_still_names_its_device(
    hass: HomeAssistant,
    events: dict[str, list[Event]],
    setup_integration: MockConfigEntry,
    server: FakeDriftBeacon,
) -> None:
    """Events fire before the device is removed, so device triggers still match."""
    await server.push(
        {"_tag": "SessionStarted", "session": live_session("s1", "reading")}
    )
    await wait_for(lambda: len(events[EVENT_SESSION_STARTED]) == 1)
    device_id = _device(hass, "reading").id
    await server.push(
        {"_tag": "SessionEnded", "sessionId": "s1", "activityId": "reading"},
        {"_tag": "ActivityDeleted", "activityId": "reading"},
    )
    await wait_for(lambda: len(events[EVENT_SESSION_STOPPED]) == 1)
    stopped = events[EVENT_SESSION_STOPPED][0].data
    assert stopped["activity_device_id"] == device_id
    assert stopped["activity_name"] == "Reading"
    assert _device(hass, "reading") is None


async def test_recolouring_the_focused_activity_updates_focus(
    hass: HomeAssistant,
    events: dict[str, list[Event]],
    setup_integration: MockConfigEntry,
    server: FakeDriftBeacon,
) -> None:
    """Lights follow a colour change of whatever is live."""
    await server.push(
        {"_tag": "SessionStarted", "session": live_session("s1", "reading")}
    )
    await server.push(
        {
            "_tag": "ActivityUpdated",
            "activity": activity("reading", "Reading", color="#00ff00"),
        }
    )
    await wait_for(lambda: len(events[EVENT_FOCUS_CHANGED]) == 3)
    assert events[EVENT_FOCUS_CHANGED][-1].data["color"] == [0, 255, 0]


async def test_reconnect_delivers_what_changed_while_disconnected(
    hass: HomeAssistant,
    events: dict[str, list[Event]],
    setup_integration: MockConfigEntry,
    server: FakeDriftBeacon,
) -> None:
    """The new snapshot is diffed against the last state, so missed transitions still fire."""
    server.snapshot["liveSessions"] = [live_session("s9", "reading")]
    await server.disconnect()
    await wait_for(lambda: len(events[EVENT_SESSION_STARTED]) == 1)
    assert server.connections == 2
    assert events[EVENT_FOCUS_CHANGED][-1].data["state"] == "live"
    assert hass.states.get("switch.reading_session").state == "on"


async def test_short_disconnects_do_not_flap_entities(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    server: FakeDriftBeacon,
) -> None:
    """Entities stay available through the grace period and go unavailable after it."""
    server.handshake_status = 503
    await server.disconnect()
    # A refused reconnect proves the client noticed the drop and started its grace period.
    await wait_for(lambda: server.handshakes >= 2)
    await hass.async_block_till_done()
    assert hass.states.get("switch.reading_pin").state == "off"

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=UNAVAILABLE_GRACE_PERIOD + 1)
    )
    await hass.async_block_till_done()
    assert hass.states.get("switch.reading_pin").state == "unavailable"

    server.handshake_status = None
    await wait_for(lambda: hass.states.get("switch.reading_pin").state == "off", 5)


async def test_malformed_message_is_skipped_without_reconnecting(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    server: FakeDriftBeacon,
) -> None:
    """One bad message must not cost the connection or the rest of its chunk."""
    await server.push(
        {"_tag": "SessionStarted", "session": {"id": "broken"}},
        {"_tag": "SessionStarted", "session": live_session("s1", "reading")},
    )
    await wait_for(lambda: hass.states.get("switch.reading_session").state == "on")
    assert server.connections == 1


async def test_garbage_frames_are_ignored(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    server: FakeDriftBeacon,
) -> None:
    """Non-JSON frames and unsolicited errors are logged and skipped."""
    await server.send_raw("not json")
    await server.send_raw('{"jsonrpc":"2.0","id":-32603,"error":{"_tag":"Defect"}}')
    await server.push(
        {"_tag": "SessionStarted", "session": live_session("s1", "reading")}
    )
    await wait_for(lambda: hass.states.get("switch.reading_session").state == "on")
    assert server.connections == 1


async def test_server_ending_the_stream_triggers_a_reconnect(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    server: FakeDriftBeacon,
) -> None:
    """A subscription Exit is treated as a lost connection, not silently ignored."""
    await server.end_stream()
    await wait_for(lambda: server.connections == 2)


async def test_rename_and_type_change_update_the_device(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    server: FakeDriftBeacon,
) -> None:
    """Renames follow the activity unless the user named the device; types swap entities."""
    registry = dr.async_get(hass)
    water = _device(hass, "water")
    registry.async_update_device(water.id, name_by_user="Hydration")

    await server.push(
        {"_tag": "ActivityUpdated", "activity": activity("reading", "Books")},
        {"_tag": "ActivityUpdated", "activity": activity("water", "Drink")},
    )
    await wait_for(lambda: _device(hass, "reading").name == "Books")
    assert _device(hass, "water").name == "Drink"
    assert _device(hass, "water").name_by_user == "Hydration"
    assert _device(hass, "water").model_id == "span"

    session_uid = f"{WORKSPACE_ID}:water:session"
    await wait_for(lambda: entity_id_for(hass, "switch", session_uid) is not None)
    assert entity_id_for(hass, "button", f"{WORKSPACE_ID}:water:mark") is None
    # New entities take the user's device name.
    session = er.async_get(hass).async_get(entity_id_for(hass, "switch", session_uid))
    assert session.entity_id == "switch.hydration_session"
    assert session.hidden_by is not None


async def test_deleted_activity_loses_its_device_and_gets_it_back(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    server: FakeDriftBeacon,
) -> None:
    """Archive removes the device; unarchive restores the same id, keeping automations valid."""
    reading_id = _device(hass, "reading").id
    await server.push({"_tag": "ActivityDeleted", "activityId": "reading"})
    await wait_for(lambda: _device(hass, "reading") is None)
    assert er.async_get(hass).async_get("switch.reading_session") is None

    await server.push(
        {"_tag": "ActivityCreated", "activity": activity("reading", "Reading")}
    )
    await wait_for(lambda: _device(hass, "reading") is not None)
    assert _device(hass, "reading").id == reading_id
    await wait_for(lambda: hass.states.get("switch.reading_session") is not None)


async def test_snapshot_removes_devices_of_activities_gone_while_offline(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    server: FakeDriftBeacon,
) -> None:
    """Reconciling against each snapshot catches deletions missed while disconnected."""
    server.snapshot["activities"] = [activity("reading", "Reading")]
    await server.disconnect()
    await wait_for(lambda: _device(hass, "water") is None)
    assert _device(hass, "reading") is not None
