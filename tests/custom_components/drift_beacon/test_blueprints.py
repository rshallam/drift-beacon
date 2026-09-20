"""Tests for the bundled Drift Beacon blueprints."""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.components.automation.config import (
    AUTOMATION_BLUEPRINT_SCHEMA,
    PLATFORM_SCHEMA,
)
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.core import HomeAssistant
from homeassistant.helpers.script import Script
from homeassistant.util.yaml import load_yaml

BLUEPRINTS = (
    Path(__file__).parents[3] / "custom_components" / "drift_beacon" / "blueprints"
)
ACTIVITY_BLUEPRINT = "drift_beacon_activity_button.yaml"
ACTION_INPUTS = [
    ("track_trigger", "track_activity"),
    ("pause_trigger", "pause_activity"),
    ("pin_trigger", "pin_activity"),
    ("queue_trigger", "queue_activity"),
    ("unpin_trigger", "unpin_activity"),
    ("pause_current_trigger", "pause_current"),
    ("stop_current_trigger", "stop_current"),
]


def load_blueprint(name: str) -> Blueprint:
    """Load and schema-validate a bundled automation blueprint."""
    return Blueprint(
        load_yaml(BLUEPRINTS / name),
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )


def substitute(**mappings) -> dict:
    """Fill the blueprint and validate the resulting automation with HA."""
    inputs = BlueprintInputs(
        load_blueprint(ACTIVITY_BLUEPRINT),
        {
            "use_blueprint": {
                "path": ACTIVITY_BLUEPRINT,
                "input": {"activity": "switch.renamed_activity", **mappings},
            }
        },
    )
    inputs.validate()
    return PLATFORM_SCHEMA(inputs.async_substitute())


def event_trigger(name: str) -> dict:
    """User IDs must not determine which activity action is run."""
    return {"trigger": "event", "event_type": name, "id": "custom-id"}


@pytest.mark.parametrize("name", sorted(p.name for p in BLUEPRINTS.glob("*.yaml")))
def test_blueprint_is_valid(name: str) -> None:
    """Every bundled blueprint passes Home Assistant's blueprint schema."""
    load_blueprint(name)


@pytest.mark.asyncio
@pytest.mark.parametrize(("input_name", "action"), ACTION_INPUTS)
async def test_each_action_can_be_the_only_mapping(
    tmp_path: Path, input_name: str, action: str
) -> None:
    """Empty sections drop out and any action can run without a Track mapping."""
    hass = HomeAssistant(str(tmp_path))
    try:
        config = substitute(**{input_name: [event_trigger("button_event")]})
        variables = config["variables"].async_render(
            hass, {"trigger": {"idx": "0", "id": "custom-id"}}
        )
        assert len(config["triggers"]) == 1
        assert variables["activity_action"] == action

        calls = []

        async def record(call) -> None:
            calls.append(call)

        expected_service = "stop_session" if action.endswith("_current") else action
        hass.services.async_register("drift_beacon", expected_service, record)
        script = Script(hass, config["actions"], "Activity controls", "automation")
        await script.async_run(variables)
        assert len(calls) == 1
        assert calls[0].service == expected_service
        assert calls[0].data["entity_id"] == ["switch.renamed_activity"]
        if action.endswith("_current"):
            assert calls[0].data["pause"] is (action == "pause_current")
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_multiple_triggers_and_disabled_triggers_keep_their_action(
    tmp_path: Path,
) -> None:
    """Dispatch uses HA's flat index, including disabled trigger positions."""
    hass = HomeAssistant(str(tmp_path))
    try:
        config = substitute(
            track_trigger=[
                event_trigger("a"),
                {**event_trigger("b"), "enabled": False},
            ],
            queue_trigger=[event_trigger("c"), event_trigger("d")],
            stop_current_trigger=[event_trigger("e")],
        )
        assert len(config["triggers"]) == 5
        for idx, action in enumerate(
            [
                "track_activity",
                "track_activity",
                "queue_activity",
                "queue_activity",
                "stop_current",
            ]
        ):
            variables = config["variables"].async_render(
                hass, {"trigger": {"idx": str(idx)}}
            )
            assert variables["activity_action"] == action
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_empty_mappings_and_manual_runs_do_nothing(tmp_path: Path) -> None:
    """An unconfigured control or manual run cannot fall through to another action."""
    hass = HomeAssistant(str(tmp_path))
    try:
        config = substitute()
        assert config["triggers"] == []
        assert config["variables"].async_render(hass, {})["activity_action"] == ""
        config = substitute(track_trigger=[event_trigger("a")])
        for trigger in ({}, {"trigger": {"idx": "-1"}}, {"trigger": {"idx": "99"}}):
            variables = config["variables"].async_render(hass, trigger)
            assert variables["activity_action"] == ""
            # No services registered: an unexpected call would fail this run.
            await Script(
                hass, config["actions"], "Activity controls", "automation"
            ).async_run(variables)
    finally:
        await hass.async_stop(force=True)
