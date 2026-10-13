"""Bridge configuration: dataclasses plus fail-fast validation.

The file is JSON (mode 0600 — it holds per-lamp Long Term Keys and the MQTT
password)::

    {
      "account_uuid": "00000000-0000-0000-0000-000000000000",
      "mqtt": {"host": "10.0.0.10", "port": 1883, "username": "u", "password": "p"},
      "status_port": 8765,
      "lamps": [
        {"serial": "E5R-US-PKA0710A", "mac": "C7:4D:76:76:15:EB", "name": "Downstairs",
         "ltk": "00112233445566778899aabbccddeeff"}
      ]
    }
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
DEFAULT_STATUS_PORT = 8765
DEFAULT_MQTT_PORT = 1883


class ConfigError(Exception):
    """Configuration is missing or malformed."""


@dataclass(frozen=True, slots=True)
class MqttConfig:
    host: str
    username: str
    password: str
    port: int = DEFAULT_MQTT_PORT


@dataclass(frozen=True, slots=True)
class LampConfig:
    serial: str
    mac: str
    name: str
    ltk: bytes

    @property
    def base_topic(self) -> str:
        return f"dyson_ble/{self.serial}"


@dataclass(frozen=True, slots=True)
class Config:
    account_uuid: str
    mqtt: MqttConfig
    lamps: tuple[LampConfig, ...]
    status_port: int = DEFAULT_STATUS_PORT

    @classmethod
    def load(cls, path: Path) -> Config:
        try:
            raw = json.loads(path.read_text())
        except OSError as err:
            raise ConfigError(f"cannot read {path}: {err}") from err
        except json.JSONDecodeError as err:
            raise ConfigError(f"{path} is not valid JSON: {err}") from err
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: object) -> Config:
        if not isinstance(raw, dict):
            raise ConfigError("configuration must be a JSON object")
        account = _require_str(raw, "account_uuid")
        try:
            UUID(account)
        except ValueError as err:
            raise ConfigError(f"account_uuid is not a UUID: {account!r}") from err

        mqtt_raw = raw.get("mqtt")
        if not isinstance(mqtt_raw, dict):
            raise ConfigError("missing 'mqtt' object")
        mqtt = MqttConfig(
            host=_require_str(mqtt_raw, "host"),
            username=_require_str(mqtt_raw, "username"),
            password=_require_str(mqtt_raw, "password", allow_empty=True),
            port=int(mqtt_raw.get("port", DEFAULT_MQTT_PORT)),
        )

        lamps_raw = raw.get("lamps")
        if not isinstance(lamps_raw, list) or not lamps_raw:
            raise ConfigError("'lamps' must be a non-empty list")
        lamps: list[LampConfig] = []
        seen: set[str] = set()
        for index, entry in enumerate(lamps_raw):
            if not isinstance(entry, dict):
                raise ConfigError(f"lamps[{index}] must be an object")
            serial = _require_str(entry, "serial", prefix=f"lamps[{index}].").upper()
            if serial in seen:
                raise ConfigError(f"duplicate lamp serial {serial}")
            seen.add(serial)
            mac = _require_str(entry, "mac", prefix=f"lamps[{index}].").upper()
            if not MAC_RE.match(mac):
                raise ConfigError(f"lamps[{index}].mac is not a MAC address: {mac!r}")
            ltk_hex = _require_str(entry, "ltk", prefix=f"lamps[{index}].")
            if len(ltk_hex) % 2 or not HEX_RE.match(ltk_hex):
                raise ConfigError(f"lamps[{index}].ltk must be an even-length hex string")
            lamps.append(
                LampConfig(
                    serial=serial,
                    mac=mac,
                    name=str(entry.get("name") or serial),
                    ltk=bytes.fromhex(ltk_hex),
                )
            )

        return cls(
            account_uuid=account,
            mqtt=mqtt,
            lamps=tuple(lamps),
            status_port=int(raw.get("status_port", DEFAULT_STATUS_PORT)),
        )


def _require_str(
    source: dict[str, object], key: str, *, prefix: str = "", allow_empty: bool = False
) -> str:
    value = source.get(key)
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ConfigError(f"{prefix}{key} must be a non-empty string")
    return value
