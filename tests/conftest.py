"""Shared fixtures: a lamp config and a fake lamp that speaks the real protocol."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import pytest

from dyson_ble_bridge import protocol
from dyson_ble_bridge.config import LampConfig
from dyson_ble_bridge.ha import LampState

LTK = bytes.fromhex("00112233445566778899aabbccddeeff")
ACCOUNT_UUID = "c032cb89-8541-433d-99bf-31fe876a6371"
SERIAL = "E5R-US-PKA0710A"
MAC = "C7:4D:76:76:15:EB"
PRODUCT_INFO = bytes.fromhex("05110200504630303035353200000000")


@pytest.fixture
def lamp_config() -> LampConfig:
    return LampConfig(serial=SERIAL, mac=MAC, name="Downstairs", ltk=LTK)


class FakeLamp:
    """A lamp that answers the handshake and reflects control writes.

    Behaves like the hardware for the parts that matter: fragments on the auth
    channel, HKDF-derived envelope, nonce echo, and read-back of written values.
    """

    def __init__(
        self,
        mac: str,
        *,
        on_disconnected: Callable[[], None] | None = None,
        fail_connects: int = 0,
        ltk: bytes = LTK,
    ) -> None:
        self.mac = mac
        self.key = protocol.derive_key(ltk)
        self.on_disconnected = on_disconnected
        self.fail_connects = fail_connects
        self.is_connected = False
        self.writes: list[tuple[str, bytes]] = []
        self.reads: list[str] = []
        self.subscribed: dict[str, Callable[[str, bytes], None]] = {}
        self.cccd: set[str] = set()
        self.power = b"\x01"
        self.lumens = 400
        self.kelvin = 2700
        self.read_failures: set[str] = set()
        self.challenge = bytes(range(0x40, 0x50))
        self.authenticated = False
        self._assembler = protocol.Assembler()

    # ── Transport interface ─────────────────────────────────────────────────

    async def connect(self) -> None:
        if self.fail_connects > 0:
            self.fail_connects -= 1
            raise TimeoutError(f"{self.mac} is not advertising")
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def start_notify(self, char: str, callback: Callable[[str, bytes], None]) -> None:
        self.subscribed[char] = callback

    async def enable_notifications(self, char: str) -> None:
        self.cccd.add(char)

    async def write(self, char: str, data: bytes, *, response: bool = False) -> None:
        payload = bytes(data)
        self.writes.append((char, payload))
        if char == protocol.CHAR_AUTH:
            message = self._assembler.feed(payload)
            if message is not None:
                self._answer(message)
            return
        if char == protocol.CHAR_POWER:
            self.power = payload
        elif char == protocol.CHAR_BRIGHTNESS_LUMENS:
            self.lumens = int.from_bytes(payload, "little")
        elif char == protocol.CHAR_COLOR_TEMP:
            self.kelvin = int.from_bytes(payload, "little")

    async def read(self, char: str) -> bytes:
        self.reads.append(char)
        if char in self.read_failures:
            raise TimeoutError(f"read {char} timed out")
        if char == protocol.CHAR_POWER:
            return self.power
        if char == protocol.CHAR_BRIGHTNESS_LUMENS:
            return self.lumens.to_bytes(2, "little")
        if char == protocol.CHAR_COLOR_TEMP:
            return self.kelvin.to_bytes(2, "little")
        raise AssertionError(f"unexpected read of {char}")

    # ── test helpers ────────────────────────────────────────────────────────

    def emit(self, char: str, data: bytes) -> None:
        """Deliver a notification the way bleak would."""
        self.subscribed[char](char, data)

    def drop_link(self) -> None:
        self.is_connected = False
        if self.on_disconnected is not None:
            self.on_disconnected()

    def _answer(self, message: protocol.Message) -> None:
        if message.type == protocol.MessageType.REQUEST_PRODUCT_INFO:
            self._send(protocol.MessageType.PRODUCT_INFO, PRODUCT_INFO)
        elif message.type == protocol.MessageType.REAUTH_A:
            nonce = protocol.unseal(self.key, message.payload[18:82])
            body = b"\x00\x00" + protocol.seal(self.key, nonce + self.challenge, iv=bytes(16))
            self._send(protocol.MessageType.REAUTH_B, body)
        elif message.type == protocol.MessageType.REAUTH_C:
            assert protocol.unseal(self.key, message.payload[2:]) == self.challenge
            self.authenticated = True
            self._send(
                protocol.MessageType.CONNECTION_ESTABLISHED, bytes.fromhex("00ffffffff0511020000")
            )

    def _send(self, type_id: int, payload: bytes) -> None:
        for frame in protocol.fragment(type_id, payload):
            self.emit(protocol.CHAR_AUTH, frame)


class StubMqtt:
    """Records publishes/subscriptions; the supervisor only needs those two."""

    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []
        self.subscriptions: list[str] = []

    def publish(self, topic: str, payload: object, qos: int = 0, retain: bool = False) -> None:
        self.published.append((topic, payload))

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscriptions.append(topic)

    def message_callback_add(self, topic: str, callback: object) -> None:
        return None

    def topics(self) -> list[str]:
        return [topic for topic, _ in self.published]

    def last_payload(self, topic: str) -> object:
        for published_topic, payload in reversed(self.published):
            if published_topic == topic:
                return payload
        return None

    def last_json(self, topic: str) -> dict:
        """Decode the last payload for ``topic`` (payloads leave as JSON strings)."""
        payload = self.last_payload(topic)
        return json.loads(payload) if isinstance(payload, (str, bytes)) else dict(payload)


@pytest.fixture
def fake_lamp() -> FakeLamp:
    return FakeLamp(MAC)


@pytest.fixture
def expected_initial_state() -> LampState:
    return LampState(on=True, lumens=400, kelvin=2700)


def transport_factory(lamp: FakeLamp) -> Callable[..., FakeLamp]:
    """Return a transport factory that always hands out ``lamp``."""

    def factory(mac: str, *, on_disconnected: Callable[[], None] | None = None) -> FakeLamp:
        lamp.on_disconnected = on_disconnected
        return lamp

    return factory


async def settle(seconds: float = 0.05) -> None:
    """Let queued callbacks and tasks run (no arbitrary sleeps in assertions)."""
    await asyncio.sleep(seconds)
