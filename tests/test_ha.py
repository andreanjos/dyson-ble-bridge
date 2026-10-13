"""Unit tests for Home Assistant discovery payloads and command parsing."""

from __future__ import annotations

import json

import pytest

from dyson_ble_bridge import ha
from dyson_ble_bridge.config import LampConfig
from dyson_ble_bridge.ha import Command, CommandError, LampState, parse_command
from dyson_ble_bridge.protocol import KELVIN_MAX, KELVIN_MIN, LUMENS_MAX, LUMENS_MIN


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"state": "ON"}', Command(on=True)),
        ('{"state": "off"}', Command(on=False)),
        ('{"state": "ON", "brightness": 750}', Command(on=True, lumens=750)),
        ('{"brightness": 10}', Command(lumens=LUMENS_MIN)),
        ('{"brightness": 99999}', Command(lumens=LUMENS_MAX)),
        # HA sends the kelvin value under the "color_temp" key (see the round-trip test below)
        ('{"color_temp": 4000}', Command(kelvin=4000)),
        (b'{"color_temp": 100}', Command(kelvin=KELVIN_MIN)),
        (b'{"color_temp": 9000}', Command(kelvin=KELVIN_MAX)),
        ('{"color_temp_kelvin": 5000}', Command(kelvin=5000)),  # accepted alias
        ("{}", Command()),
    ],
)
def test_parse_command(raw: str | bytes, expected: Command) -> None:
    assert parse_command(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        '{"state": "blue"}',
        '{"brightness": "bright"}',
        '{"color_temp": null}',
    ],
)
def test_parse_command_rejects_garbage(raw: str) -> None:
    if raw == '{"color_temp": null}':
        assert parse_command(raw).kelvin is None  # explicit null means "leave alone"
        return
    with pytest.raises(CommandError):
        parse_command(raw)


def test_state_payload_reports_color_mode_for_home_assistant() -> None:
    assert LampState().payload() == {"state": "OFF"}
    assert LampState(on=True).payload() == {"state": "ON", "color_mode": "brightness"}
    payload = LampState(on=True, lumens=400, kelvin=2700).payload()
    assert payload == {
        "state": "ON",
        "color_mode": "color_temp",
        "brightness": 400,
        "color_temp": 2700,
    }


def test_state_payload_keys_round_trip_through_command_parsing() -> None:
    """HA echoes the keys it is given in state back as commands.

    Home Assistant keeps the ``color_temp`` key name and switches only its unit,
    so a state payload that HA could apply must parse back to the same colour
    temperature. Renaming the key on one side only would silently drop every
    colour command, which is exactly what this pins down.
    """
    payload = LampState(on=True, lumens=400, kelvin=2700).payload()
    command = parse_command(json.dumps(payload))
    assert (command.on, command.lumens, command.kelvin) == (True, 400, 2700)


def test_state_payload_is_json_serialisable() -> None:
    json.loads(json.dumps(LampState(on=True, lumens=400, kelvin=2700).payload()))


def test_light_discovery_topic_and_keys(lamp_config: LampConfig) -> None:
    topic, payload = ha.light_discovery(lamp_config)
    assert topic == f"homeassistant/light/dyson_ble_{lamp_config.serial}/config"
    assert payload["state_topic"] == f"{lamp_config.base_topic}/state"
    assert payload["command_topic"] == f"{lamp_config.base_topic}/set"
    assert payload["brightness_scale"] == LUMENS_MAX
    # With this flag HA reads and writes colour temperature in kelvin.
    assert payload["color_temp_kelvin"] is True
    assert payload["supported_color_modes"] == ["color_temp"]
    assert (payload["min_kelvin"], payload["max_kelvin"]) == (KELVIN_MIN, KELVIN_MAX)
    assert payload["device"]["identifiers"] == [f"dyson_ble_{lamp_config.serial}"]
    # A crashed bridge must mark the entity unavailable too.
    assert {"topic": ha.BRIDGE_AVAILABILITY_TOPIC} in payload["availability"]
    assert {"topic": f"{lamp_config.base_topic}/availability"} in payload["availability"]


def test_motion_discovery_topic_and_keys(lamp_config: LampConfig) -> None:
    topic, payload = ha.motion_discovery(lamp_config)
    assert topic == f"homeassistant/binary_sensor/dyson_ble_{lamp_config.serial}_motion/config"
    assert payload["state_topic"] == f"{lamp_config.base_topic}/motion"
    assert payload["device_class"] == "motion"
    assert payload["payload_on"] == "ON"


def test_discovery_payloads_are_json_serialisable(lamp_config: LampConfig) -> None:
    for _, payload in (ha.light_discovery(lamp_config), ha.motion_discovery(lamp_config)):
        json.loads(json.dumps(payload))
