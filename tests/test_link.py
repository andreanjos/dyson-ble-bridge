"""Unit tests for the per-lamp BLE session (handshake, writes, state)."""

from __future__ import annotations

import pytest

from dyson_ble_bridge import protocol
from dyson_ble_bridge.config import LampConfig
from dyson_ble_bridge.ha import Command, LampState
from dyson_ble_bridge.link import LampLink
from tests.conftest import ACCOUNT_UUID, LTK, FakeLamp, transport_factory


async def connected_link(lamp: FakeLamp, cfg: LampConfig, **callbacks) -> LampLink:
    link = LampLink(cfg, ACCOUNT_UUID, transport_factory=transport_factory(lamp), **callbacks)
    await link.connect()
    return link


async def test_connect_authenticates_and_reads_initial_state(
    lamp_config: LampConfig, fake_lamp: FakeLamp
) -> None:
    link = await connected_link(fake_lamp, lamp_config)
    assert fake_lamp.authenticated, "handshake must complete (challenge verified by the fake lamp)"
    assert link.is_connected
    assert link.state == LampState(on=True, lumens=400, kelvin=2700)
    assert protocol.CHAR_AUTH in fake_lamp.cccd
    assert set(fake_lamp.subscribed) >= {
        protocol.CHAR_AUTH,
        protocol.CHAR_POWER,
        protocol.CHAR_BRIGHTNESS_LUMENS,
        protocol.CHAR_COLOR_TEMP,
        protocol.CHAR_MOTION,
    }


async def test_connect_rejects_wrong_ltk(lamp_config: LampConfig) -> None:
    """A lamp with a different LTK must not be accepted on the echo check."""
    wrong = LampConfig(
        serial=lamp_config.serial, mac=lamp_config.mac, name=lamp_config.name, ltk=b"\x09" * 16
    )
    lamp = FakeLamp(wrong.mac, ltk=LTK)  # lamp keeps the real key
    link = LampLink(wrong, ACCOUNT_UUID, transport_factory=transport_factory(lamp))
    with pytest.raises(protocol.ProtocolError, match="MAC mismatch"):
        await link.connect()


async def test_apply_leaves_daylight_mode_before_manual_writes(
    lamp_config: LampConfig, fake_lamp: FakeLamp
) -> None:
    link = await connected_link(fake_lamp, lamp_config)
    fake_lamp.writes.clear()

    changed = await link.apply(Command(on=True, lumens=750, kelvin=4000))

    written = [char for char, _ in fake_lamp.writes]
    daylight = written.index(protocol.CHAR_WRITE_ATTR)
    assert (protocol.CHAR_WRITE_ATTR, protocol.DAYLIGHT_OFF) in fake_lamp.writes
    # the lamp ignores manual values while its daylight algorithm is active
    assert daylight < written.index(protocol.CHAR_BRIGHTNESS_LUMENS)
    assert daylight < written.index(protocol.CHAR_COLOR_TEMP)
    assert (protocol.CHAR_POWER, protocol.power_payload(True)) in fake_lamp.writes
    assert (protocol.CHAR_COLOR_TEMP, protocol.kelvin_payload(4000)) in fake_lamp.writes
    assert changed and link.state == LampState(on=True, lumens=750, kelvin=4000)


async def test_apply_power_only_skips_daylight_packet(
    lamp_config: LampConfig, fake_lamp: FakeLamp
) -> None:
    link = await connected_link(fake_lamp, lamp_config)
    fake_lamp.writes.clear()
    await link.apply(Command(on=False))
    assert [char for char, _ in fake_lamp.writes] == [protocol.CHAR_POWER]


async def test_apply_with_no_fields_is_a_noop(lamp_config: LampConfig, fake_lamp: FakeLamp) -> None:
    link = await connected_link(fake_lamp, lamp_config)
    fake_lamp.writes.clear()
    assert await link.apply(Command()) is False
    assert fake_lamp.writes == []


async def test_notifications_update_state_and_notify_callback(
    lamp_config: LampConfig, fake_lamp: FakeLamp
) -> None:
    seen: list[LampState] = []
    link = await connected_link(fake_lamp, lamp_config, on_state=seen.append)
    fake_lamp.lumens = 900
    fake_lamp.emit(protocol.CHAR_BRIGHTNESS_LUMENS, (900).to_bytes(2, "little"))
    assert link.state.lumens == 900
    assert seen[-1].lumens == 900

    fake_lamp.emit(protocol.CHAR_POWER, b"\x00")
    assert link.state.on is False
    fake_lamp.emit(protocol.CHAR_COLOR_TEMP, (4000).to_bytes(2, "little"))
    assert link.state.kelvin == 4000


async def test_unchanged_state_does_not_emit(lamp_config: LampConfig, fake_lamp: FakeLamp) -> None:
    seen: list[LampState] = []
    await connected_link(fake_lamp, lamp_config, on_state=seen.append)
    seen.clear()
    fake_lamp.emit(protocol.CHAR_POWER, b"\x01")  # same value as initial state
    assert seen == []


async def test_motion_and_link_loss_callbacks(lamp_config: LampConfig, fake_lamp: FakeLamp) -> None:
    motions: list[bool] = []
    losses: list[bool] = []
    link = await connected_link(
        fake_lamp, lamp_config, on_motion=motions.append, on_link_lost=lambda: losses.append(True)
    )
    fake_lamp.emit(protocol.CHAR_MOTION, b"\x01")
    fake_lamp.emit(protocol.CHAR_MOTION, b"\x00")
    assert motions == [True, False]

    fake_lamp.drop_link()
    assert losses == [True]
    assert link.link_lost and not link.is_connected


async def test_refresh_tolerates_a_failing_characteristic(
    lamp_config: LampConfig, fake_lamp: FakeLamp
) -> None:
    link = await connected_link(fake_lamp, lamp_config)
    fake_lamp.read_failures.add(protocol.CHAR_COLOR_TEMP)
    fake_lamp.lumens = 500
    assert await link.refresh() is True
    assert link.state.lumens == 500
    assert link.state.kelvin == 2700  # stale value kept, not zeroed


async def test_operations_require_a_connection(
    lamp_config: LampConfig, fake_lamp: FakeLamp
) -> None:
    link = LampLink(lamp_config, ACCOUNT_UUID, transport_factory=transport_factory(fake_lamp))
    with pytest.raises(RuntimeError, match="not connected"):
        await link.refresh()
    with pytest.raises(RuntimeError, match="not connected"):
        await link.apply(Command(on=True))
