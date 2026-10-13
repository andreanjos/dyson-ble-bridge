"""One lamp's BLE session: connect, authenticate, read state, notify, write.

The transport is injectable (:class:`Transport`), so the session logic — handshake,
pacing, state reconciliation — is testable without hardware.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from bleak import BleakClient, BleakScanner

from . import protocol
from .config import LampConfig
from .ha import Command, LampState

_LOGGER = logging.getLogger(__name__)

AUTH_TIMEOUT = 30.0
PRODUCT_INFO_TIMEOUT = 8.0
READ_TIMEOUT = 10.0
FRAGMENT_PACING = 0.06  # the lamp drops back-to-back fragments
DAYLIGHT_SETTLE = 0.25
WRITE_SETTLE = 0.6

_NOTIFY_CHARS: tuple[tuple[str, str], ...] = (
    (protocol.CHAR_POWER, "_on_power"),
    (protocol.CHAR_BRIGHTNESS_LUMENS, "_on_lumens"),
    (protocol.CHAR_COLOR_TEMP, "_on_kelvin"),
    (protocol.CHAR_MOTION, "_on_motion_frame"),
)


class Transport(Protocol):
    """Minimal async BLE surface used by :class:`LampLink`."""

    @property
    def is_connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def start_notify(self, char: str, callback: Callable[[str, bytes], None]) -> None: ...

    async def enable_notifications(self, char: str) -> None: ...

    async def write(self, char: str, data: bytes, *, response: bool = False) -> None: ...

    async def read(self, char: str) -> bytes: ...


class BleakTransport:
    """``bleak``-backed transport.

    Connects to a *scanned* device object: on BlueZ, connecting to a bare address
    can time out or fail to resolve, while a discovered device connects reliably.
    """

    def __init__(
        self,
        mac: str,
        *,
        on_disconnected: Callable[[], None] | None = None,
        scan_timeout: float = 20.0,
        connect_timeout: float = 25.0,
    ) -> None:
        self._mac = mac
        self._on_disconnected = on_disconnected
        self._scan_timeout = scan_timeout
        self._connect_timeout = connect_timeout
        self._client: BleakClient | None = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self) -> None:
        device = await BleakScanner.find_device_by_address(self._mac, timeout=self._scan_timeout)
        if device is None:
            raise TimeoutError(f"{self._mac} is not advertising")
        self._client = BleakClient(
            device,
            timeout=self._connect_timeout,
            disconnected_callback=lambda _client: self._disconnected(),
        )
        await self._client.connect()

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as err:
                _LOGGER.debug("%s: disconnect failed: %s", self._mac, err)

    async def start_notify(self, char: str, callback: Callable[[str, bytes], None]) -> None:
        await self._require().start_notify(char, lambda _c, data: callback(char, bytes(data)))

    async def enable_notifications(self, char: str) -> None:
        """Force the characteristic's CCCD to 0x0001 when the stack does not.

        ESPHome Bluetooth proxies accept ``start_notify`` but leave the CCCD at
        0000, so the lamp never notifies and the handshake times out.  BlueZ (and
        therefore bleak on Linux) sets it inside ``start_notify`` and *refuses* a
        direct write, so a failure here is expected and harmless.
        """
        client = self._require()
        try:
            characteristic = client.services.get_characteristic(char)
        except Exception as err:
            _LOGGER.debug("%s: no cached characteristic %s (%s)", self._mac, char, err)
            return
        for descriptor in getattr(characteristic, "descriptors", ()):
            if str(descriptor.uuid).lower().startswith(protocol.CCCD_UUID_PREFIX):
                try:
                    await client.write_gatt_descriptor(descriptor.handle, b"\x01\x00")
                except Exception as err:
                    _LOGGER.debug(
                        "%s: CCCD for %s left to the stack (%s)", self._mac, char[:8], err
                    )
                return

    async def write(self, char: str, data: bytes, *, response: bool = False) -> None:
        await self._require().write_gatt_char(char, data, response=response)

    async def read(self, char: str) -> bytes:
        return bytes(
            await asyncio.wait_for(self._require().read_gatt_char(char), timeout=READ_TIMEOUT)
        )

    def _require(self) -> BleakClient:
        if self._client is None:
            raise RuntimeError("transport is not connected")
        return self._client

    def _disconnected(self) -> None:
        if self._on_disconnected is not None:
            self._on_disconnected()


class LampLink:
    """Serialized access to one lamp: connect + authenticate, then read/write.

    Callbacks run on the event loop: ``on_state`` when the published state changes,
    ``on_motion`` for occupancy reports, ``on_link_lost`` when the link drops.
    """

    def __init__(
        self,
        cfg: LampConfig,
        account_uuid: str,
        *,
        transport_factory: Callable[..., Transport] | None = None,
        on_state: Callable[[LampState], None] | None = None,
        on_motion: Callable[[bool], None] | None = None,
        on_link_lost: Callable[[], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.account_uuid = account_uuid
        self.key = protocol.derive_key(cfg.ltk)
        self.state = LampState()
        self._transport_factory: Callable[..., Transport] = transport_factory or BleakTransport
        self._on_state = on_state
        self._on_motion = on_motion
        self._on_link_lost = on_link_lost
        self._transport: Transport | None = None
        self._assembler = protocol.Assembler()
        self._auth_inbox: asyncio.Queue[protocol.Message] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self.link_lost = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._transport is not None and self._transport.is_connected

    async def connect(self) -> None:
        """Connect, enable notifications, authenticate, read the initial state."""
        transport = self._transport_factory(self.cfg.mac, on_disconnected=self._link_dropped)
        await transport.connect()
        self._transport = transport
        self.link_lost = False
        self._assembler.reset()
        while not self._auth_inbox.empty():  # stale frames from a previous session
            self._auth_inbox.get_nowait()

        await transport.start_notify(protocol.CHAR_AUTH, self._on_auth_frame)
        await transport.enable_notifications(protocol.CHAR_AUTH)
        await self._authenticate()

        for char, handler_name in _NOTIFY_CHARS:
            handler = getattr(self, handler_name)
            try:
                await transport.start_notify(char, handler)
                await transport.enable_notifications(char)
            except Exception as err:
                _LOGGER.warning(
                    "%s: cannot subscribe to %s (%s); state for it is read on refresh",
                    self.cfg.serial,
                    char[:8],
                    err,
                )
        await self.refresh()

    async def disconnect(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.disconnect()

    def _link_dropped(self) -> None:
        """Transport disconnect callback — must be synchronous (bleak calls it directly)."""
        self.link_lost = True
        if self._on_link_lost is not None:
            self._on_link_lost()

    # ── handshake ────────────────────────────────────────────────────────────

    async def _authenticate(self) -> None:
        await self._send(protocol.MessageType.REQUEST_PRODUCT_INFO)
        try:
            await self._await_message(protocol.MessageType.PRODUCT_INFO, PRODUCT_INFO_TIMEOUT)
        except TimeoutError:
            _LOGGER.debug("%s: no product info response (continuing)", self.cfg.serial)

        nonce = os.urandom(16)
        await self._send(
            protocol.MessageType.REAUTH_A,
            protocol.reauth_payload_a(self.account_uuid, self.key, nonce),
        )
        message = await self._await_message(protocol.MessageType.REAUTH_B, AUTH_TIMEOUT)
        echoed, challenge = protocol.parse_reauth_payload_b(self.key, message.payload)
        if echoed != nonce:
            raise protocol.ProtocolError("nonce echo mismatch — LTK does not match this lamp")

        await self._send(
            protocol.MessageType.REAUTH_C, protocol.reauth_payload_c(self.key, challenge)
        )
        await self._await_message(protocol.MessageType.CONNECTION_ESTABLISHED, AUTH_TIMEOUT)
        _LOGGER.info("%s: authenticated", self.cfg.serial)

    async def _send(self, type_id: int, payload: bytes = b"") -> None:
        transport = self._require()
        fragments = protocol.fragment(type_id, payload)
        for index, fragment in enumerate(fragments):
            await transport.write(protocol.CHAR_AUTH, fragment)
            if index + 1 < len(fragments):
                await asyncio.sleep(FRAGMENT_PACING)

    async def _await_message(self, type_id: int, timeout: float) -> protocol.Message:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"{self.cfg.serial}: no message 0x{type_id:02x} in {timeout}s")
            message = await asyncio.wait_for(self._auth_inbox.get(), timeout=remaining)
            if message.type == type_id:
                return message

    # ── state ────────────────────────────────────────────────────────────────

    async def refresh(self) -> bool:
        """Read power, brightness and colour temperature; True when state changed."""
        transport = self._require()
        raw: dict[str, bytes] = {}
        for char in (
            protocol.CHAR_POWER,
            protocol.CHAR_BRIGHTNESS_LUMENS,
            protocol.CHAR_COLOR_TEMP,
        ):
            try:
                raw[char] = await transport.read(char)
            except Exception as err:
                _LOGGER.debug("%s: read %s failed: %s", self.cfg.serial, char[:8], err)

        state = self.state
        power = protocol.decode_power(raw.get(protocol.CHAR_POWER, b""))
        if power is not None:
            state = replace(state, on=power)
        lumens = protocol.decode_lumens(raw.get(protocol.CHAR_BRIGHTNESS_LUMENS, b""))
        if lumens is not None:
            state = replace(state, lumens=lumens)
        kelvin = protocol.decode_kelvin(raw.get(protocol.CHAR_COLOR_TEMP, b""))
        if kelvin is not None:
            state = replace(state, kelvin=kelvin)
        return self._publish_state(state)

    async def apply(self, command: Command) -> bool:
        """Apply a command, then re-read the lamp; True when the state changed."""
        if command.empty:
            return False
        async with self._write_lock:
            transport = self._require()
            if command.on is not None:
                await transport.write(protocol.CHAR_POWER, protocol.power_payload(command.on))
            if command.lumens is not None or command.kelvin is not None:
                await self._leave_daylight_mode(transport)
            if command.lumens is not None:
                await transport.write(
                    protocol.CHAR_BRIGHTNESS_LUMENS, protocol.lumens_payload(command.lumens)
                )
            if command.kelvin is not None:
                await transport.write(
                    protocol.CHAR_COLOR_TEMP, protocol.kelvin_payload(command.kelvin)
                )
            await asyncio.sleep(WRITE_SETTLE)
            return await self.refresh()

    async def _leave_daylight_mode(self, transport: Transport) -> None:
        """Disable the lamp's daylight algorithm so manual writes are honoured."""
        try:
            await transport.write(protocol.CHAR_WRITE_ATTR, protocol.DAYLIGHT_OFF)
            await asyncio.sleep(DAYLIGHT_SETTLE)
        except Exception as err:
            _LOGGER.debug("%s: daylight-off write failed: %s", self.cfg.serial, err)

    def _publish_state(self, state: LampState) -> bool:
        if state == self.state:
            return False
        self.state = state
        if self._on_state is not None:
            self._on_state(state)
        return True

    # ── notification handlers ────────────────────────────────────────────────

    def _on_auth_frame(self, _char: str, data: bytes) -> None:
        message = self._assembler.feed(data)
        if message is not None:
            self._auth_inbox.put_nowait(message)

    def _on_power(self, _char: str, data: bytes) -> None:
        power = protocol.decode_power(data)
        if power is not None:
            self._publish_state(replace(self.state, on=power))

    def _on_lumens(self, _char: str, data: bytes) -> None:
        lumens = protocol.decode_lumens(data)
        if lumens is not None:
            self._publish_state(replace(self.state, lumens=lumens))

    def _on_kelvin(self, _char: str, data: bytes) -> None:
        kelvin = protocol.decode_kelvin(data)
        if kelvin is not None:
            self._publish_state(replace(self.state, kelvin=kelvin))

    def _on_motion_frame(self, _char: str, data: bytes) -> None:
        if self._on_motion is not None:
            self._on_motion(any(data))

    def _require(self) -> Transport:
        if self._transport is None:
            raise RuntimeError("lamp link is not connected")
        return self._transport
