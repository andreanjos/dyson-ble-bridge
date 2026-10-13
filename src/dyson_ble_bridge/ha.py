"""Home Assistant MQTT discovery and payload shaping — pure functions, no I/O."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .config import LampConfig
from .protocol import KELVIN_MAX, KELVIN_MIN, LUMENS_MAX, clamp_kelvin, clamp_lumens

DISCOVERY_PREFIX = "homeassistant"
BRIDGE_AVAILABILITY_TOPIC = "dyson_ble/bridge/availability"
MANUFACTURER = "Dyson"
MODEL = "Solarcycle Morph (E5R)"
SW_VERSION = "PF000552 / fw 5.17"


@dataclass(frozen=True, slots=True)
class LampState:
    """What Home Assistant is told about one lamp."""

    on: bool = False
    lumens: int | None = None
    kelvin: int | None = None

    def payload(self) -> dict[str, object]:
        """MQTT state payload for ``schema: json``.

        ``color_mode`` is mandatory for HA to accept colour temperature, and
        brightness is expressed on the lumen scale declared by ``brightness_scale``.
        """
        payload: dict[str, object] = {"state": "ON" if self.on else "OFF"}
        if not self.on:
            return payload
        payload["color_mode"] = "color_temp" if self.kelvin else "brightness"
        if self.lumens is not None:
            payload["brightness"] = self.lumens
        if self.kelvin is not None:
            payload["color_temp_kelvin"] = self.kelvin
        return payload


@dataclass(frozen=True, slots=True)
class Command:
    """A parsed light command; ``None`` means "leave this attribute alone"."""

    on: bool | None = None
    lumens: int | None = None
    kelvin: int | None = None

    @property
    def empty(self) -> bool:
        return self.on is None and self.lumens is None and self.kelvin is None


class CommandError(ValueError):
    """Command payload could not be interpreted."""


def parse_command(raw: str | bytes) -> Command:
    """Parse an HA MQTT ``schema: json`` light command.

    ``brightness`` is taken as the lumen value directly: discovery declares
    ``brightness_scale`` equal to :data:`~.protocol.LUMENS_MAX`, so HA's 0-255 user
    range arrives already scaled to 0-1000.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise CommandError(f"not JSON: {err}") from err
    if not isinstance(data, dict):
        raise CommandError("command must be a JSON object")

    on: bool | None = None
    if "state" in data:
        value = str(data["state"]).strip().upper()
        if value not in {"ON", "OFF"}:
            raise CommandError(f"state must be ON or OFF, got {data['state']!r}")
        on = value == "ON"

    lumens: int | None = None
    if data.get("brightness") is not None:
        try:
            lumens = clamp_lumens(int(data["brightness"]))
        except (TypeError, ValueError) as err:
            raise CommandError(f"brightness must be a number, got {data['brightness']!r}") from err

    kelvin: int | None = None
    if data.get("color_temp_kelvin") is not None:
        try:
            kelvin = clamp_kelvin(int(data["color_temp_kelvin"]))
        except (TypeError, ValueError) as err:
            raise CommandError(
                f"color_temp_kelvin must be a number, got {data['color_temp_kelvin']!r}"
            ) from err

    return Command(on=on, lumens=lumens, kelvin=kelvin)


def device_block(cfg: LampConfig) -> dict[str, object]:
    return {
        "identifiers": [f"dyson_ble_{cfg.serial}"],
        "name": cfg.name,
        "manufacturer": MANUFACTURER,
        "model": MODEL,
        "sw_version": SW_VERSION,
    }


def _availability(cfg: LampConfig) -> list[dict[str, str]]:
    """Per-lamp topic *and* the bridge-wide topic: a crashed bridge must show up."""
    return [{"topic": f"{cfg.base_topic}/availability"}, {"topic": BRIDGE_AVAILABILITY_TOPIC}]


def light_discovery(cfg: LampConfig) -> tuple[str, dict[str, object]]:
    """Discovery topic + payload for the light entity."""
    payload: dict[str, object] = {
        "name": None,
        "has_entity_name": True,
        "unique_id": f"dyson_ble_{cfg.serial}",
        "schema": "json",
        "state_topic": f"{cfg.base_topic}/state",
        "command_topic": f"{cfg.base_topic}/set",
        "brightness": True,
        "brightness_scale": LUMENS_MAX,
        "color_temp_kelvin": True,
        "supported_color_modes": ["color_temp"],
        "min_kelvin": KELVIN_MIN,
        "max_kelvin": KELVIN_MAX,
        "availability": _availability(cfg),
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device_block(cfg),
    }
    return f"{DISCOVERY_PREFIX}/light/dyson_ble_{cfg.serial}/config", payload


def motion_discovery(cfg: LampConfig) -> tuple[str, dict[str, object]]:
    payload: dict[str, object] = {
        "name": "Motion",
        "has_entity_name": True,
        "unique_id": f"dyson_ble_{cfg.serial}_motion",
        "state_topic": f"{cfg.base_topic}/motion",
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "motion",
        "availability": _availability(cfg),
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device_block(cfg),
    }
    return f"{DISCOVERY_PREFIX}/binary_sensor/dyson_ble_{cfg.serial}_motion/config", payload
