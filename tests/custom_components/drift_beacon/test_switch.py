"""Tests for Drift Beacon activity switch discovery and armed state."""

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
        self.armed_activity = None
        self.listeners = []
        self.arm_activity = AsyncMock(return_value=True)
        self.disarm_activity = AsyncMock(return_value=True)

    def async_add_listener(self, listener):
        """Register a state listener."""
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def get_workspace_for_activity(self, _activity_id: str) -> dict:
        """Return the single test workspace."""
        return {"id": "workspace-1", "name": "Personal"}

    def get_armed_activity(self, _workspace_id: str):
        """Return current armed state."""
        return self.armed_activity

    def get_activity(self, activity_id: str):
        """Look up an activity."""
        return next(
            (item for item in self.activities if item["id"] == activity_id),
            None,
        )

    def get_category(self, _category_id: str | None):
        """Return no category."""
        return None


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
async def test_discovers_session_and_armed_activity_switches() -> None:
    """Span activities get session switches and all activities get arm switches."""
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
        "entry-1_span-1",
        "entry-1_armed_span-1",
        "entry-1_armed_point-1",
    }


@pytest.mark.asyncio
async def test_armed_switches_follow_single_slot_and_send_rpcs() -> None:
    """Displacement updates switch state and only the active switch disarms."""
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
    armed_switches = {
        entity.unique_id: entity
        for entity in added_entities
        if "_armed_" in entity.unique_id
    }
    span_switch = armed_switches["entry-1_armed_span-1"]
    point_switch = armed_switches["entry-1_armed_point-1"]

    manager.armed_activity = {
        "activity_id": "span-1",
        "armed_at": "2026-08-14T10:00:00.000Z",
    }
    assert span_switch.is_on
    assert not point_switch.is_on

    manager.armed_activity = {
        "activity_id": "point-1",
        "armed_at": "2026-08-14T10:01:00.000Z",
    }
    assert not span_switch.is_on
    assert point_switch.is_on

    await span_switch.async_turn_on()
    manager.arm_activity.assert_awaited_once_with("span-1")

    await span_switch.async_turn_off()
    manager.disarm_activity.assert_not_awaited()
    await point_switch.async_turn_off()
    manager.disarm_activity.assert_awaited_once_with("point-1")
