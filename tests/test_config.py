"""Unit tests for configuration loading and validation."""

from __future__ import annotations

import json
import pathlib

import pytest

from dyson_ble_bridge.config import Config, ConfigError


def base_config() -> dict:
    return {
        "account_uuid": "c032cb89-8541-433d-99bf-31fe876a6371",
        "mqtt": {
            "host": "10.0.0.10",
            "port": 1883,
            "username": "homeassistant",
            "password": "secret",
        },
        "lamps": [
            {
                "serial": "E5R-US-PKA0710A",
                "mac": "C7:4D:76:76:15:EB",
                "name": "Downstairs",
                "ltk": "00112233445566778899aabbccddeeff",
            }
        ],
    }


def test_from_dict_parses_and_normalises() -> None:
    cfg = Config.from_dict(base_config())
    lamp = cfg.lamps[0]
    assert cfg.status_port == 8765
    assert cfg.mqtt.host == "10.0.0.10"
    assert lamp.serial == "E5R-US-PKA0710A"
    assert lamp.mac == "C7:4D:76:76:15:EB"
    assert lamp.ltk == bytes.fromhex("00112233445566778899aabbccddeeff")
    assert lamp.base_topic == "dyson_ble/E5R-US-PKA0710A"


def test_lowercase_mac_is_uppercased() -> None:
    raw = base_config()
    raw["lamps"][0]["mac"] = "c7:4d:76:76:15:eb"
    assert Config.from_dict(raw).lamps[0].mac == "C7:4D:76:76:15:EB"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r: r.pop("account_uuid"), id="missing-account"),
        pytest.param(lambda r: r.update(account_uuid="not-a-uuid"), id="bad-account"),
        pytest.param(lambda r: r.update(mqtt=None), id="missing-mqtt"),
        pytest.param(lambda r: r["mqtt"].pop("host"), id="missing-mqtt-host"),
        pytest.param(lambda r: r.update(lamps=[]), id="no-lamps"),
        pytest.param(lambda r: r["lamps"][0].update(mac="zz:zz"), id="bad-mac"),
        pytest.param(lambda r: r["lamps"][0].update(ltk="abc"), id="odd-ltk"),
        pytest.param(lambda r: r["lamps"][0].update(ltk="nothex!!"), id="non-hex-ltk"),
        pytest.param(lambda r: r["lamps"][0].pop("serial"), id="missing-serial"),
        pytest.param(lambda r: r["lamps"].append(dict(r["lamps"][0])), id="duplicate-serial"),
    ],
)
def test_invalid_configs_are_rejected(mutate) -> None:
    raw = base_config()
    mutate(raw)
    with pytest.raises(ConfigError):
        Config.from_dict(raw)


def test_load_reports_unreadable_and_malformed_files(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        Config.load(tmp_path / "nope.json")

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        Config.load(broken)

    good = tmp_path / "good.json"
    good.write_text(json.dumps(base_config()))
    assert Config.load(good).lamps[0].name == "Downstairs"


def test_empty_password_is_allowed_for_anonymous_brokers() -> None:
    raw = base_config()
    raw["mqtt"]["password"] = ""
    assert Config.from_dict(raw).mqtt.password == ""
