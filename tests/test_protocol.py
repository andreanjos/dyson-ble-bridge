"""Unit tests for the protocol layer — no hardware, no I/O."""

from __future__ import annotations

import pytest

from dyson_ble_bridge import protocol
from dyson_ble_bridge.protocol import (
    Assembler,
    Message,
    MessageType,
    ProtocolError,
    derive_key,
    fragment,
    parse_reauth_payload_b,
    reauth_payload_a,
    reauth_payload_c,
    seal,
    unseal,
)

LTK = bytes(32)
# Real value observed from the lamp during development (auth succeeded with it).
KEY_ZERO_LTK = bytes.fromhex("f2da722b0b00d398ce0e72638efb22b6")


def test_derive_key_matches_device_observed_vector() -> None:
    assert derive_key(LTK) == KEY_ZERO_LTK


def test_derive_key_rejects_empty_ltk() -> None:
    with pytest.raises(ValueError):
        derive_key(b"")


def test_seal_layout_is_iv_ct_mac() -> None:
    key = KEY_ZERO_LTK
    iv = bytes(range(16))
    envelope = seal(key, b"A" * 16, iv=iv)
    assert len(envelope) == 16 + 16 + 32
    assert envelope[:16] == iv
    assert unseal(key, envelope) == b"A" * 16


def test_seal_requires_block_aligned_plaintext() -> None:
    with pytest.raises(ProtocolError):
        seal(KEY_ZERO_LTK, b"not a block")


def test_seal_rejects_bad_iv_length() -> None:
    with pytest.raises(ProtocolError):
        seal(KEY_ZERO_LTK, b"A" * 16, iv=b"short")


def test_unseal_detects_wrong_key_and_tampering() -> None:
    envelope = seal(KEY_ZERO_LTK, b"A" * 16, iv=bytes(16))
    with pytest.raises(ProtocolError, match="MAC mismatch"):
        unseal(derive_key(b"\x01" * 16), envelope)
    tampered = bytearray(envelope)
    tampered[20] ^= 0xFF
    with pytest.raises(ProtocolError, match="MAC mismatch"):
        unseal(KEY_ZERO_LTK, bytes(tampered))
    with pytest.raises(ProtocolError, match="expected 16n"):
        unseal(KEY_ZERO_LTK, envelope[:-1])


@pytest.mark.parametrize(
    "payload", [b"", b"\x00", b"A" * 18, b"B" * 19, b"C" * 20, bytes(range(200))]
)
def test_fragment_and_assembler_roundtrip(payload: bytes) -> None:
    assembler = Assembler()
    messages = [assembler.feed(frame) for frame in fragment(0x07, payload)]
    assert messages[-1] == Message(0x07, payload)
    assert all(message is None for message in messages[:-1])


def test_fragment_headers_match_device_wire_format() -> None:
    """Five fragments for an 82-byte payload: 0x84 then 0x01..0x04 (observed on the lamp)."""
    frames = fragment(0x07, bytes(82))
    assert [frame[0] for frame in frames] == [0x84, 0x01, 0x02, 0x03, 0x04]
    assert [len(frame) for frame in frames] == [20, 20, 20, 20, 8]
    assert b"".join(frame[1:] for frame in frames)[0] == 0x07


def test_fragment_rejects_absurd_payload() -> None:
    with pytest.raises(ProtocolError):
        fragment(0x01, bytes(100_000))
    with pytest.raises(ValueError):
        fragment(0x01, b"x", capacity=1)


def test_assembler_ignores_stray_continuation_and_rejects_short_first() -> None:
    assembler = Assembler()
    assert assembler.feed(b"\x00\x01") is None
    with pytest.raises(ProtocolError, match="no type byte"):
        assembler.feed(b"\x80")


def test_assembler_rejects_out_of_order_fragments() -> None:
    assembler = Assembler()
    assert assembler.feed(bytes((0x81, 0x07)) + bytes(19)) is None  # first of two
    with pytest.raises(ProtocolError, match="out of order"):
        assembler.feed(bytes((0x05,)) + bytes(4))  # index 5 of 2


def test_reauth_payloads_have_device_shapes() -> None:
    guid = "c032cb89-8541-433d-99bf-31fe876a6371"
    payload_a = reauth_payload_a(guid, KEY_ZERO_LTK, bytes(16))
    assert len(payload_a) == 82
    assert payload_a[:16] == bytes.fromhex(guid.replace("-", ""))
    assert payload_a[16:18] == b"\x00\x00"
    assert reauth_payload_c(KEY_ZERO_LTK, bytes(16))[:2] == b"\x00\x00"
    assert len(reauth_payload_c(KEY_ZERO_LTK, bytes(16))) == 66

    with pytest.raises(ProtocolError):
        reauth_payload_a(guid, KEY_ZERO_LTK, bytes(15))
    with pytest.raises(ProtocolError):
        reauth_payload_c(KEY_ZERO_LTK, bytes(15))


def test_parse_reauth_payload_b_recovers_nonce_and_challenge() -> None:
    nonce, challenge = bytes(range(16)), bytes(range(16, 32))
    iv = bytes(16)
    payload_b = b"\x00\x00" + seal(KEY_ZERO_LTK, nonce + challenge, iv=iv)
    assert parse_reauth_payload_b(KEY_ZERO_LTK, payload_b) == (nonce, challenge)

    with pytest.raises(ProtocolError):
        parse_reauth_payload_b(KEY_ZERO_LTK, b"\x00\x00" + bytes(10))
    with pytest.raises(ProtocolError, match="MAC mismatch"):
        parse_reauth_payload_b(derive_key(b"\x02" * 16), payload_b)


def test_message_type_constants_match_the_lamp() -> None:
    assert (MessageType.REAUTH_A, MessageType.REAUTH_B, MessageType.REAUTH_C) == (0x06, 0x07, 0x08)
    assert MessageType.CONNECTION_ESTABLISHED == 0x26
    assert bytes((0x13, 0x20, 0x01, 0x00, 0x00)) == protocol.DAYLIGHT_OFF


def test_control_payloads_encode_and_clamp() -> None:
    assert protocol.power_payload(True) == b"\x01"
    assert protocol.power_payload(False) == b"\x00"
    assert protocol.lumens_payload(400) == bytes.fromhex("9001")
    assert protocol.lumens_payload(10) == protocol.LUMENS_MIN.to_bytes(2, "little")
    assert protocol.lumens_payload(99_999) == protocol.LUMENS_MAX.to_bytes(2, "little")
    assert protocol.kelvin_payload(4000) == bytes.fromhex("a00f")
    assert protocol.kelvin_payload(2000) == protocol.KELVIN_MIN.to_bytes(2, "little")
    assert protocol.kelvin_payload(9000) == protocol.KELVIN_MAX.to_bytes(2, "little")


def test_decoders_tolerate_short_reads() -> None:
    assert protocol.decode_power(b"") is None
    assert protocol.decode_power(b"\x01") is True
    assert protocol.decode_lumens(b"\x90") is None
    assert protocol.decode_lumens(bytes.fromhex("9001")) == 400
    assert protocol.decode_kelvin(bytes.fromhex("a00f")) == 4000
