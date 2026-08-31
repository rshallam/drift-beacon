"""Tests for Drift Beacon workspace sensor discovery."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.drift_beacon.sensor import async_setup_entry


class FakeManager:
    """Manager state and listener interface used by platform setup."""

    def __init__(self) -> None:
        self.workspaces = []
        self.listeners = []
        self.available = True
        self.workspace_id = "workspace-1"
        self.device_info = {"identifiers": {("drift_beacon", self.workspace_id)}}
        self.user_id = "user-1"
        self.user_name = "Rich"
        self.user_attributes = {"user_id": self.user_id, "user_name": self.user_name}
        self.workspace_name = "Personal"
        self.pinned_activity = None
        self.activity = {
            "id": "activity-1",
            "name": "Focus",
            "category_id": None,
            "color": "#808080",
            "icon": "mdi:target",
            "unit": None,
            "progress": {"current": 0, "target": None},
        }

    def async_add_listener(self, listener):
        """Register a state listener."""
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def get_pinned_activity(self, workspace_id: str):
        """Return current pinned state."""
        return self.pinned_activity

    def get_activity(self, activity_id: str):
        """Return the test activity."""
        return self.activity if activity_id == self.activity["id"] else None

    def get_category(self, category_id: str | None):
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
    """Task interface used when removing obsolete sensors."""

    def async_create_task(self, coro):
        """Create an asyncio task."""
        return asyncio.create_task(coro)


@pytest.mark.asyncio
async def test_sensor_created_when_workspace_snapshot_arrives_late() -> None:
    """Platform setup before the first snapshot must still create its sensor."""
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
        "workspace-1:connected_user"
    }

    manager.workspaces = [{"id": "workspace-1", "name": "Personal"}]
    for listener in list(manager.listeners):
        listener()

    assert {entity.unique_id for entity in added_entities} == {
        "workspace-1:connected_user",
        "workspace-1:session",
        "workspace-1:pinned_activity",
    }
    assert {entity.name for entity in added_entities} == {
        "Connected user",
        "Session",
        "Pinned activity",
    }


@pytest.mark.asyncio
async def test_pinned_sensor_exposes_current_activity() -> None:
    """Pinned sensor should expose the current activity and timestamp."""
    manager = FakeManager()
    manager.workspaces = [{"id": "workspace-1", "name": "Personal"}]
    manager.pinned_activity = {
        "activity_id": "activity-1",
        "pinned_at": "2026-08-14T10:00:00.000Z",
    }
    entry = FakeEntry(manager)
    added_entities = []

    await async_setup_entry(
        FakeHomeAssistant(),
        entry,
        lambda entities: added_entities.extend(entities),
    )

    pinned_sensor = next(
        entity
        for entity in added_entities
        if entity.unique_id == "workspace-1:pinned_activity"
    )
    assert pinned_sensor.native_value == "Focus"
    assert pinned_sensor.icon == "mdi:target"
    assert pinned_sensor.extra_state_attributes["pinned_at"] == (
        "2026-08-14T10:00:00.000Z"
    )
    assert pinned_sensor.extra_state_attributes["user_id"] == "user-1"
    assert pinned_sensor.extra_state_attributes["user_name"] == "Rich"
    connected_user = next(
        entity
        for entity in added_entities
        if entity.unique_id == "workspace-1:connected_user"
    )
    assert connected_user.native_value == "Rich"
    assert connected_user.extra_state_attributes == {"user_id": "user-1"}
