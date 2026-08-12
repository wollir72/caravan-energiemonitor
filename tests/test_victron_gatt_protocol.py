from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from caravan_energiemonitor.devices.victron.gatt_protocol import (
    COMMAND_UUID,
    FIRST_DAY_REGISTER,
    FLOW_CONTROL_UUID,
    FLOW_KEEPALIVE,
    RECORD_TYPE,
    TOTAL_HISTORY_REGISTER,
    ProtocolError,
    SafetyViolation,
    build_history_read_request,
    decode_history_day,
    decode_history_summary,
    history_day_registers,
    parse_history_frame,
    validate_outgoing_frame,
)


def total_payload(days: int = 3) -> bytes:
    payload = bytearray(34)
    payload[0] = 1
    payload[2:6] = bytes((2, 17, 0, 119))
    payload[6:10] = (12345).to_bytes(4, "little")
    payload[10:14] = (67890).to_bytes(4, "little")
    payload[14:16] = (4127).to_bytes(2, "little")
    payload[16:18] = (1421).to_bytes(2, "little")
    payload[18] = days
    payload[19:21] = (1297).to_bytes(2, "little")
    payload[21:] = b"\xFF" * 13
    return bytes(payload)


def day_payload(consumed: int = 0xFFFFFFFF) -> bytes:
    payload = bytearray(34)
    payload[1:5] = (84).to_bytes(4, "little")
    payload[5:9] = consumed.to_bytes(4, "little")
    payload[9:11] = (1421).to_bytes(2, "little")
    payload[11:13] = (1297).to_bytes(2, "little")
    payload[14:18] = bytes((2, 17, 0, 119))
    payload[18:20] = (213).to_bytes(2, "little")
    payload[20:22] = (60).to_bytes(2, "little")
    payload[22:24] = (314).to_bytes(2, "little")
    payload[24:28] = (178).to_bytes(4, "little")
    payload[28:30] = (124).to_bytes(2, "little")
    payload[30:32] = (4127).to_bytes(2, "little")
    payload[32:34] = (364).to_bytes(2, "little")
    return bytes(payload)


def frame(register: int, payload: bytes, response_type: int = 0x08) -> bytes:
    return bytes((response_type, 0x03, 0x19, register >> 8, register & 0xFF, RECORD_TYPE, len(payload))) + payload


def test_summary_and_day_are_decoded_into_immutable_models():
    summary = decode_history_summary(
        parse_history_frame(frame(TOTAL_HISTORY_REGISTER, total_payload()), TOTAL_HISTORY_REGISTER)
    )
    day = decode_history_day(
        parse_history_frame(frame(FIRST_DAY_REGISTER, day_payload()), FIRST_DAY_REGISTER), 0
    )
    assert summary.days_available == 3
    assert summary.yield_since_reset_kwh == pytest.approx(123.45)
    assert summary.lifetime_yield_kwh == pytest.approx(678.90)
    assert summary.max_pv_voltage_v == pytest.approx(41.27)
    assert day.yield_kwh == pytest.approx(0.84)
    assert day.max_pv_power_w == 178
    assert day.sequence_number == 364
    assert day.load_consumption_kwh is None
    with pytest.raises(FrozenInstanceError):
        day.day_index = 2


def test_non_sentinel_consumption_is_scaled():
    assert decode_history_day(day_payload(123), 1).load_consumption_kwh == pytest.approx(1.23)


@pytest.mark.parametrize(
    "bad_frame",
    [
        frame(FIRST_DAY_REGISTER, day_payload()[:-1]),
        frame(FIRST_DAY_REGISTER + 1, day_payload()),
        frame(FIRST_DAY_REGISTER, day_payload(), response_type=0x09),
    ],
)
def test_malformed_length_register_and_unknown_type_are_rejected(bad_frame):
    with pytest.raises(ProtocolError):
        parse_history_frame(bad_frame, FIRST_DAY_REGISTER)


@pytest.mark.parametrize("register", [0x1030, 0x0004, 0xED8D, 0x106F])
def test_non_history_registers_are_blocked(register):
    with pytest.raises(SafetyViolation):
        build_history_read_request(register)


def test_write_request_and_operation_are_blocked():
    with pytest.raises(SafetyViolation):
        build_history_read_request(TOTAL_HISTORY_REGISTER, request=0x06)
    with pytest.raises(SafetyViolation):
        build_history_read_request(TOTAL_HISTORY_REGISTER, operation=0x82)
    with pytest.raises(SafetyViolation):
        validate_outgoing_frame(COMMAND_UUID, bytes.fromhex("06 03 82 19 10 4F"))


def test_read_encoding_and_fire_and_forget_keepalive_are_exactly_allowlisted():
    assert build_history_read_request(TOTAL_HISTORY_REGISTER) == bytes.fromhex(
        "05 03 81 19 10 4F"
    )
    assert build_history_read_request(FIRST_DAY_REGISTER) == bytes.fromhex(
        "05 03 81 19 10 50"
    )
    assert FLOW_KEEPALIVE == bytes.fromhex("F9 41")
    validate_outgoing_frame(FLOW_CONTROL_UUID, FLOW_KEEPALIVE)


@pytest.mark.parametrize(
    ("available", "count", "last"),
    [(0, 0, None), (1, 1, 0x1050), (30, 30, 0x106D), (99, 30, 0x106D)],
)
def test_day_register_range_is_capped_and_exact(available, count, last):
    registers = history_day_registers(available)
    assert len(registers) == count
    assert (registers[-1] if registers else None) == last


def test_summary_rejects_more_than_30_reported_days():
    with pytest.raises(ProtocolError, match="mehr als 30"):
        decode_history_summary(total_payload(31))
