"""Tests for Drift Beacon activity switch discovery and pinned state."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from custom_components.drift_beacon.switch import async_setup_entry


def activity(activity_id: str, name: str, tracking_type: str) -> dict:
    """Build activity data used by switch entities."""
    return {
        "id": activity_id,
        "name": name,
        "description": None,
        "category_id": None,
        "sort_order": 0,
        "color": "#808080",
        "icon": "mdi:circle",
        "tracking_type": tracking_type,
        "archived": False,
        "unit": None,
        "progress": {"current": 0, "target": None},
    }


class FakeManager:
    """Manager state and actions used by activity switches."""

    def __init__(self) -> None:
        self.activities = [
            activity("span-1", "Focus", "span"),
            activity("point-1", "Water", "point"),
        ]
        self.available = True
        self.workspace_id = "workspace-1"
        self.device_info = {"identifiers": {("drift_beacon", self.workspace_id)}}
        self.user_attributes = {"user_id": "user-1", "user_name": "Rich"}
        self.pinned_activity = None
        self.listeners = []
        self.pin_activity = AsyncMock(return_value=True)
        self.unpin_activity = AsyncMock(return_value=True)

    def async_add_listener(self, listener):
        """Register a state listener."""
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def get_workspace_for_activity(self, _activity_id: str) -> dict:
        """Return the single test workspace."""
        return {"id": "workspace-1", "name": "Personal"}

    def get_pinned_activity(self, _workspace_id: str):
        """Return current pinned state."""
        return self.pinned_activity

    def get_live_session(self, _workspace_id: str):
        """Return no live session."""
        return

    def get_activity(self, activity_id: str):
        """Look up an activity."""
        return next(
            (item for item in self.activities if item["id"] == activity_id),
            None,
        )

    def get_category(self, _category_id: str | None):
        """Return no category."""
        return


class FakeEntry:
    """Config entry interface used by platform setup."""

    def __init__(self, manager: FakeManager) -> None:
        self.entry_id = "entry-1"
        self.runtime_data = manager
        self.unload_callbacks = []

    def async_on_unload(self, callback) -> None:
        """Retain an unload callback."""
        self.unload_callbacks.append(callback)


class FakeHomeAssistant:
    """Task interface used when removing obsolete switches."""

    def async_create_task(self, coro):
        """Create an asyncio task."""
        return asyncio.create_task(coro)


@pytest.mark.asyncio
async def test_discovers_session_and_pinned_activity_switches() -> None:
    """Span activities get session switches and all activities get pin switches."""
    manager = FakeManager()
    entry = FakeEntry(manager)
    added_entities = []

    def add_entities(entities) -> None:
        added_entities.extend(entities)

    await async_setup_entry(
        FakeHomeAssistant(),
        entry,
        add_entities,
    )

    assert {entity.unique_id for entity in added_entities} == {
        "workspace-1:session:span-1",
        "workspace-1:pin:span-1",
        "workspace-1:pin:point-1",
    }
    assert all(entity.device_info == manager.device_info for entity in added_entities)
    assert all(
        entity.extra_state_attributes["user_name"] == "Rich"
        for entity in added_entities
    )


@pytest.mark.asyncio
async def test_pinned_switches_follow_single_slot_and_send_rpcs() -> None:
    """Displacement updates switch state and only the active switch unpins."""
    manager = FakeManager()
    entry = FakeEntry(manager)
    added_entities = []

    def add_entities(entities) -> None:
        added_entities.extend(entities)

    await async_setup_entry(
        FakeHomeAssistant(),
        entry,
        add_entities,
    )
    pinned_switches = {
        entity.unique_id: entity
        for entity in added_entities
        if ":pin:" in entity.unique_id
    }
    span_switch = pinned_switches["workspace-1:pin:span-1"]
    point_switch = pinned_switches["workspace-1:pin:point-1"]

    manager.pinned_activity = {
        "activity_id": "span-1",
        "pinned_at": "2026-08-14T10:00:00.000Z",
    }
    assert span_switch.is_on
    assert not point_switch.is_on

    manager.pinned_activity = {
        "activity_id": "point-1",
        "pinned_at": "2026-08-14T10:01:00.000Z",
    }
    assert not span_switch.is_on
    assert point_switch.is_on

    await span_switch.async_turn_on()
    manager.pin_activity.assert_awaited_once_with("span-1")

    await span_switch.async_turn_off()
    manager.unpin_activity.assert_not_awaited()
    await point_switch.async_turn_off()
    manager.unpin_activity.assert_awaited_once_with("point-1")
