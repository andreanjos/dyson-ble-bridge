"""Dyson BLE auth/control protocol primitives.

Verified against Dyson E5R lamps (Solarcycle / Lightcycle Morph), firmware 5.17,
product code PF000552.  Everything here is pure: bytes in, bytes out, no I/O.

Protocol summary
----------------
Framing   every logical message is ``[type][payload]``, split into <=20-byte ATT
          writes; the first fragment header is ``0x80 | (total - 1)``, later
          headers are ``index & 0x7F``.
Key       ``key = HKDF-SHA256(salt=b"", ikm=LTK, info="USER_AUTH_AES\\0\\0\\0", len=16)``
Envelope  ``IV(16) || AES-128-CBC(plaintext) || HMAC-SHA256(key, ciphertext)``
          (encrypt-then-MAC; *not* AES-GCM as one earlier write-up claimed).
Re-auth   0x06 PayloadA = account GUID (16) || 00 00 || envelope(nonce)
          0x07 PayloadB = 00 00 || IV || CT(32) || MAC(32); CT = nonce_echo || challenge
          0x08 PayloadC = 00 00 || envelope(challenge)
          0x26 = connection established.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from enum import IntEnum
from uuid import UUID

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ── GATT ─────────────────────────────────────────────────────────────────────

_UUID_BASE = "-1c37-452d-8979-d1b4a787d0a4"
CHAR_AUTH = "2dd10011" + _UUID_BASE  # framed request/notify channel
CHAR_POWER = "2dd11005" + _UUID_BASE  # 1 byte: 0 off / 1 on
CHAR_BRIGHTNESS_PCT = "2dd11000" + _UUID_BASE  # 1 byte percent (non-daylight lamps)
CHAR_BRIGHTNESS_LUMENS = "2dd11009" + _UUID_BASE  # uint16 LE lumens (daylight lamps)
CHAR_COLOR_TEMP = "2dd11001" + _UUID_BASE  # uint16 LE kelvin
CHAR_MOTION = "2dd11008" + _UUID_BASE  # non-zero byte = motion
CHAR_WRITE_ATTR = "2dd10021" + _UUID_BASE  # Dyson write-attribute channel
CCCD_UUID_PREFIX = "00002902"

# ── control values ───────────────────────────────────────────────────────────

LUMENS_MIN, LUMENS_MAX = 100, 1000
KELVIN_MIN, KELVIN_MAX = 2700, 6500

#: attribute 0x2013 = daylight mode; disabling it makes the lamp accept manual
#: brightness and colour-temperature writes (it ignores them while auto-tracking).
DAYLIGHT_OFF = bytes((0x13, 0x20, 0x01, 0x00, 0x00))
DAYLIGHT_ON = bytes((0x13, 0x20, 0x01, 0x00, 0x01))

# ── framing ──────────────────────────────────────────────────────────────────

FRAGMENT_CAPACITY = 20
_FIRST_FRAGMENT_BIT = 0x80
_MAX_FRAGMENTS = 128
_RESERVED = b"\x00\x00"


class MessageType(IntEnum):
    """Message type byte carried by the first fragment."""

    REAUTH_A = 0x06
    REAUTH_B = 0x07
    REAUTH_C = 0x08
    REQUEST_PRODUCT_INFO = 0x0A
    PRODUCT_INFO = 0x0B
    CONNECTION_ESTABLISHED = 0x26


@dataclass(frozen=True, slots=True)
class Message:
    """A reassembled logical message. ``type`` stays an int: lamps emit more types than we name."""

    type: int
    payload: bytes


class ProtocolError(Exception):
    """Malformed frame, envelope or handshake message."""


def fragment(type_id: int, payload: bytes = b"", capacity: int = FRAGMENT_CAPACITY) -> list[bytes]:
    """Split ``[type][payload]`` into ATT-sized fragments."""
    if capacity < 2:
        raise ValueError("fragment capacity must be >= 2")
    logical = bytes((type_id,)) + payload
    per_fragment = capacity - 1
    total = max(1, -(-len(logical) // per_fragment))
    if total > _MAX_FRAGMENTS:
        raise ProtocolError(f"message needs {total} fragments (max {_MAX_FRAGMENTS})")
    fragments = []
    for index in range(total):
        header = _FIRST_FRAGMENT_BIT | (total - 1) if index == 0 else index & 0x7F
        fragments.append(
            bytes((header,)) + logical[index * per_fragment : (index + 1) * per_fragment]
        )
    return fragments


class Assembler:
    """Reassemble inbound fragments. One instance per BLE link, not thread-safe."""

    __slots__ = ("_buffer", "_expected", "_received", "_type")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._buffer = bytearray()
        self._type = 0
        self._expected = 0
        self._received = 0

    def feed(self, frame: bytes) -> Message | None:
        """Consume one notification fragment; return a message when complete."""
        if not frame:
            return None
        header = frame[0]
        if header & _FIRST_FRAGMENT_BIT:
            if len(frame) < 2:
                raise ProtocolError("first fragment carries no type byte")
            self._buffer = bytearray(frame[2:])
            self._expected = (header & 0x7F) + 1
            self._received = 1
            self._type = frame[1]
        else:
            if not self._expected:
                return None  # stray continuation: nothing to attach it to
            self._received = (header & 0x7F) + 1
            self._buffer.extend(frame[1:])
        if self._expected and self._received == self._expected:
            message = Message(self._type, bytes(self._buffer))
            self.reset()
            return message
        if self._received > self._expected:
            self.reset()
            raise ProtocolError("fragment out of order")
        return None


# ── crypto ───────────────────────────────────────────────────────────────────

_HKDF_INFO = b"USER_AUTH_AES\x00\x00\x00"
_KEY_LEN = 16


def derive_key(ltk: bytes) -> bytes:
    """HKDF-SHA256 (empty salt, one expand block, truncated) as used by ``g20/b.d``."""
    if not ltk:
        raise ValueError("LTK must not be empty")
    prk = hmac.new(b"", ltk, hashlib.sha256).digest()
    return hmac.new(prk, _HKDF_INFO + b"\x01", hashlib.sha256).digest()[:_KEY_LEN]


def _cbc(key: bytes, iv: bytes, data: bytes, *, encrypt: bool) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    operation = cipher.encryptor() if encrypt else cipher.decryptor()
    return operation.update(data) + operation.finalize()


def seal(key: bytes, plaintext: bytes, iv: bytes | None = None) -> bytes:
    """Encrypt-then-MAC envelope: ``IV || AES-128-CBC(plaintext) || HMAC-SHA256(ct)``."""
    if len(plaintext) % 16:
        raise ProtocolError("plaintext must be a whole number of AES blocks")
    iv = iv if iv is not None else os.urandom(16)
    if len(iv) != 16:
        raise ProtocolError("IV must be 16 bytes")
    ciphertext = _cbc(key, iv, plaintext, encrypt=True)
    return iv + ciphertext + hmac.new(key, ciphertext, hashlib.sha256).digest()


def unseal(key: bytes, envelope: bytes) -> bytes:
    """Verify and decrypt an envelope; raise :class:`ProtocolError` when the MAC fails."""
    if len(envelope) < 32 or (len(envelope) - 32) % 16:
        raise ProtocolError(f"envelope is {len(envelope)} bytes, expected 16n + 32")
    iv, ciphertext, mac = envelope[:16], envelope[16:-32], envelope[-32:]
    expected = hmac.new(key, ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise ProtocolError("envelope MAC mismatch (wrong LTK or corrupted frame)")
    return _cbc(key, iv, ciphertext, encrypt=False)


# ── handshake ────────────────────────────────────────────────────────────────


def reauth_payload_a(account: str | UUID, key: bytes, nonce: bytes) -> bytes:
    """0x06 PayloadA: account GUID || 00 00 || envelope(nonce)."""
    if len(nonce) != 16:
        raise ProtocolError("nonce must be 16 bytes")
    guid = account if isinstance(account, UUID) else UUID(account)
    return guid.bytes + _RESERVED + seal(key, nonce)


def parse_reauth_payload_b(key: bytes, payload: bytes) -> tuple[bytes, bytes]:
    """Split 0x07 PayloadB into ``(nonce_echo, device_challenge)``."""
    if len(payload) < 2 + 16 + 32 + 32:
        raise ProtocolError(f"PayloadB is {len(payload)} bytes, expected >= 82")
    plaintext = unseal(key, payload[2:82])
    return plaintext[:16], plaintext[16:32]


def reauth_payload_c(key: bytes, challenge: bytes) -> bytes:
    """0x08 PayloadC: 00 00 || envelope(challenge)."""
    if len(challenge) != 16:
        raise ProtocolError("challenge must be 16 bytes")
    return _RESERVED + seal(key, challenge)


# ── control payloads ─────────────────────────────────────────────────────────


def clamp_lumens(lumens: int) -> int:
    return max(LUMENS_MIN, min(LUMENS_MAX, int(lumens)))


def clamp_kelvin(kelvin: int) -> int:
    return max(KELVIN_MIN, min(KELVIN_MAX, int(kelvin)))


def power_payload(on: bool) -> bytes:
    return b"\x01" if on else b"\x00"


def lumens_payload(lumens: int) -> bytes:
    return clamp_lumens(lumens).to_bytes(2, "little")


def kelvin_payload(kelvin: int) -> bytes:
    return clamp_kelvin(kelvin).to_bytes(2, "little")


def decode_power(raw: bytes) -> bool | None:
    return bool(raw[0]) if raw else None


def decode_lumens(raw: bytes) -> int | None:
    return int.from_bytes(raw[:2], "little") if len(raw) >= 2 else None


def decode_kelvin(raw: bytes) -> int | None:
    return int.from_bytes(raw[:2], "little") if len(raw) >= 2 else None
