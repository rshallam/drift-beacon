"""Tests for the bundled Drift Beacon blueprints."""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest
from homeassistant.components.automation.config import (
    AUTOMATION_BLUEPRINT_SCHEMA,
    PLATFORM_SCHEMA,
)
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import load_yaml

BLUEPRINTS = (
    Path(__file__).parents[3] / "custom_components" / "drift_beacon" / "blueprints"
)


def mqtt_trigger(subtype: str) -> dict:
    """Build a device trigger like the one Home Assistant's picker produces."""
    return {
        "trigger": "device",
        "domain": "mqtt",
        "device_id": "remote-1",
        "type": "action",
        "subtype": subtype,
    }


def load_blueprint(name: str) -> Blueprint:
    """Load and schema-validate a bundled automation blueprint."""
    return Blueprint(
        load_yaml(BLUEPRINTS / name),
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )


@pytest.mark.parametrize("name", sorted(p.name for p in BLUEPRINTS.glob("*.yaml")))
def test_blueprint_is_valid(name: str) -> None:
    """Every bundled blueprint passes Home Assistant's blueprint schema."""
    load_blueprint(name)


@pytest.mark.asyncio
async def test_activity_button_substitutes_into_a_valid_automation(
    tmp_path: Path,
) -> None:
    """Optional gestures left empty drop out of the flattened trigger list."""
    # Template validation needs a Home Assistant instance in context.
    hass = HomeAssistant(str(tmp_path))
    blueprint = load_blueprint("drift_beacon_activity_button.yaml")
    inputs = BlueprintInputs(
        blueprint,
        {
            "use_blueprint": {
                "path": "drift_beacon_activity_button.yaml",
                "input": {
                    "activity": "switch.personal_desk_session",
                    "press_trigger": [mqtt_trigger("single")],
                    "hold_trigger": [mqtt_trigger("hold")],
                },
            }
        },
    )
    inputs.validate()

    config = PLATFORM_SCHEMA(inputs.async_substitute())

    assert [t["subtype"] for t in config["triggers"]] == ["single", "hold"]
    assert config["variables"].as_dict()["hold_action"] == "stop"
    await hass.async_stop(force=True)


@pytest.mark.parametrize(
    ("presses", "doubles", "idx", "gesture"),
    [
        (1, 1, 0, "press"),
        (1, 1, 1, "double_press"),
        (1, 1, 2, "hold"),
        (1, 0, 1, "hold"),
        (2, 1, 1, "press"),
        (2, 1, 2, "double_press"),
    ],
)
def test_activity_button_gesture_from_trigger_index(
    presses: int, doubles: int, idx: int, gesture: str
) -> None:
    """The gesture is recovered from where the trigger sits in the flattened list."""
    raw = load_yaml(BLUEPRINTS / "drift_beacon_activity_button.yaml")
    template = jinja2.Environment().from_string(raw["variables"]["gesture"])

    rendered = template.render(
        trigger={"idx": str(idx)},
        press_triggers=[{}] * presses,
        double_press_triggers=[{}] * doubles,
    )

    assert rendered == gesture
