"""Unit tests for the supervisor loop: reconnect, publishing, command handling."""

from __future__ import annotations

import asyncio
import json

import pytest

from dyson_ble_bridge import bridge as bridge_module
from dyson_ble_bridge import ha, protocol
from dyson_ble_bridge.config import LampConfig
from dyson_ble_bridge.ha import Command, parse_command
from tests.conftest import ACCOUNT_UUID, SERIAL, FakeLamp, StubMqtt, transport_factory


@pytest.fixture
def fast_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse staggering and backoff so the loop can be observed in milliseconds."""
    monkeypatch.setattr(bridge_module, "CONNECT_STAGGER", 0.0)
    monkeypatch.setattr(bridge_module, "MONITOR_INTERVAL", 0.005)
    monkeypatch.setattr(bridge_module, "FULL_REFRESH_INTERVAL", 0.02)
    monkeypatch.setattr(bridge_module, "BACKOFF_BASE", 0.005)
    monkeypatch.setattr(bridge_module, "BACKOFF_FACTOR", 1.0)


async def wait_until(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


async def wait_ready(supervisor: bridge_module.LampSupervisor) -> None:
    """Connected *and* past the initial read (is_connected alone is set before the handshake)."""
    await wait_until(
        lambda: supervisor.link.is_connected and supervisor.link.state.lumens is not None
    )


def control_writes(lamp: FakeLamp) -> list[tuple[str, bytes]]:
    return [write for write in lamp.writes if write[0] != protocol.CHAR_AUTH]


def make_supervisor(
    cfg: LampConfig, lamp: FakeLamp, mqtt: StubMqtt
) -> bridge_module.LampSupervisor:
    return bridge_module.LampSupervisor(
        cfg,
        ACCOUNT_UUID,
        mqtt,  # type: ignore[arg-type] - StubMqtt implements the surface used
        index=0,
        loop=asyncio.get_running_loop(),
        transport_factory=transport_factory(lamp),
    )


async def test_supervisor_recovers_from_a_failed_connect(
    fast_loops: None, lamp_config: LampConfig
) -> None:
    lamp = FakeLamp(lamp_config.mac, fail_connects=1)
    mqtt = StubMqtt()
    supervisor = make_supervisor(lamp_config, lamp, mqtt)

    task = asyncio.create_task(supervisor.run())
    try:
        await wait_until(lambda: supervisor.stats.connects >= 1)
        assert supervisor.stats.failures >= 1
        assert supervisor.stats.last_error is None  # cleared by the successful connect
        assert mqtt.last_payload(f"{lamp_config.base_topic}/availability") == "online"
        assert f"homeassistant/light/dyson_ble_{SERIAL}/config" in mqtt.topics()
        assert f"{lamp_config.base_topic}/state" in mqtt.topics()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_supervisor_publishes_state_changes(
    fast_loops: None, lamp_config: LampConfig
) -> None:
    lamp = FakeLamp(lamp_config.mac)
    mqtt = StubMqtt()
    supervisor = make_supervisor(lamp_config, lamp, mqtt)

    task = asyncio.create_task(supervisor.run())
    try:
        await wait_ready(supervisor)
        lamp.lumens = 900
        lamp.emit(protocol.CHAR_BRIGHTNESS_LUMENS, (900).to_bytes(2, "little"))
        await wait_until(
            lambda: "brightness" in str(mqtt.last_payload(f"{lamp_config.base_topic}/state"))
        )
        assert mqtt.last_json(f"{lamp_config.base_topic}/state") == {
            "state": "ON",
            "color_mode": "color_temp",
            "brightness": 900,
            "color_temp": 2700,
        }
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_supervisor_applies_and_reports_commands(
    fast_loops: None, lamp_config: LampConfig
) -> None:
    lamp = FakeLamp(lamp_config.mac)
    mqtt = StubMqtt()
    supervisor = make_supervisor(lamp_config, lamp, mqtt)

    task = asyncio.create_task(supervisor.run())
    try:
        await wait_ready(supervisor)
        await supervisor._apply(parse_command('{"state": "ON", "brightness": 600}'))
        assert (protocol.CHAR_BRIGHTNESS_LUMENS, (600).to_bytes(2, "little")) in lamp.writes
        assert mqtt.last_json(f"{lamp_config.base_topic}/state")["brightness"] == 600
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_supervisor_drops_commands_while_offline(
    fast_loops: None, lamp_config: LampConfig
) -> None:
    lamp = FakeLamp(lamp_config.mac, fail_connects=99)
    mqtt = StubMqtt()
    supervisor = make_supervisor(lamp_config, lamp, mqtt)

    task = asyncio.create_task(supervisor.run())
    try:
        await wait_until(lambda: supervisor.stats.failures >= 1)
        supervisor.on_message(b'{"state": "ON"}')  # must not raise, must not write
        await asyncio.sleep(0.02)
        assert control_writes(lamp) == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_supervisor_ignores_malformed_commands(
    fast_loops: None, lamp_config: LampConfig
) -> None:
    lamp = FakeLamp(lamp_config.mac)
    mqtt = StubMqtt()
    supervisor = make_supervisor(lamp_config, lamp, mqtt)

    task = asyncio.create_task(supervisor.run())
    try:
        await wait_ready(supervisor)
        supervisor.on_message(b"not json at all")
        await asyncio.sleep(0.02)
        assert control_writes(lamp) == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_status_projection_is_json_serialisable(
    fast_loops: None, lamp_config: LampConfig
) -> None:
    lamp = FakeLamp(lamp_config.mac)
    mqtt = StubMqtt()
    supervisor = make_supervisor(lamp_config, lamp, mqtt)
    task = asyncio.create_task(supervisor.run())
    try:
        await wait_ready(supervisor)
        status = supervisor.status()
        assert status["connected"] is True
        assert status["state"] == {"on": True, "lumens": 400, "kelvin": 2700}
        json.loads(json.dumps(status))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_announce_publishes_no_state_before_the_lamp_is_read(
    lamp_config: LampConfig,
) -> None:
    """A lamp that has not answered yet must not be reported as "OFF"."""
    mqtt = StubMqtt()
    supervisor = make_supervisor(lamp_config, FakeLamp(lamp_config.mac), mqtt)

    supervisor.announce()

    assert mqtt.last_payload(f"{lamp_config.base_topic}/state") is None
    assert mqtt.last_payload(f"{lamp_config.base_topic}/availability") == "offline"
    assert mqtt.last_payload(f"homeassistant/light/dyson_ble_{lamp_config.serial}/config")


async def test_single_instance_lock_is_exclusive(tmp_path) -> None:
    path = tmp_path / "bridge.lock"
    first = bridge_module.acquire_single_instance_lock(path)
    try:
        with pytest.raises(RuntimeError, match="another bridge instance"):
            bridge_module.acquire_single_instance_lock(path)
    finally:
        first.close()  # type: ignore[attr-defined]
    bridge_module.acquire_single_instance_lock(path).close()  # type: ignore[attr-defined]


def test_ha_module_imports_are_wired() -> None:
    assert ha.BRIDGE_AVAILABILITY_TOPIC == "dyson_ble/bridge/availability"
    assert Command(on=True) != Command()
