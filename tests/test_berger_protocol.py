from __future__ import annotations

from datetime import date

import pytest

from caravan_energiemonitor.devices.berger.protocol import (
    BASIC_INFO_COMMAND,
    BASIC_REGISTER,
    CELL_REGISTER,
    CELL_VOLTAGES_COMMAND,
    ChecksumError,
    DeviceStatusError,
    FrameAssembler,
    FrameBoundaryError,
    PayloadLengthError,
    RegisterMismatchError,
    jbd_checksum,
    parse_basic_response,
    parse_cell_response,
)

BASIC_RESPONSE = bytes.fromhex(
    "DD 03 00 22 05 80 00 00 4D BD 4E 20 00 01 32 DB 00 00 00 00 "
    "00 00 29 64 03 04 01 0B D4 00 00 00 4E 20 4D BD 00 00 F9 E7 77"
)
CELL_RESPONSE = bytes.fromhex(
    "DD 04 00 08 0D B4 0D B8 0D B8 0D B7 FC E9 77"
)


def build_response(register: int, payload: bytes, status: int = 0) -> bytes:
    checksum = jbd_checksum(register, status, payload)
    return bytes((0xDD, register, status, len(payload))) + payload + checksum.to_bytes(2, "big") + b"\x77"


def test_known_read_commands_are_exact():
    assert BASIC_INFO_COMMAND == bytes.fromhex("DD A5 03 00 FF FD 77")
    assert CELL_VOLTAGES_COMMAND == bytes.fromhex("DD A5 04 00 FF FC 77")


def test_parse_real_basic_response():
    data = parse_basic_response(BASIC_RESPONSE)
    assert data.voltage == 14.08
    assert data.current == 0.0
    assert data.remaining_capacity_ah == 199.01
    assert data.nominal_capacity_ah == 200.0
    assert data.cycle_count == 1
    assert data.production_date == date(2025, 6, 27)
    assert data.software_version == "4.1"
    assert data.state_of_charge_percent == 100
    assert data.charge_mosfet_enabled is True
    assert data.discharge_mosfet_enabled is True
    assert data.cell_count == 4
    assert data.temperatures_celsius == pytest.approx((29.65,))
    assert data.protection_status == 0
    assert data.extension_bytes == bytes.fromhex("00 00 00 4E 20 4D BD 00 00")


def test_standard_nibble_software_version():
    payload = bytearray(BASIC_RESPONSE[4:-3])
    payload[18] = 0x52
    assert parse_basic_response(build_response(BASIC_REGISTER, payload)).software_version == "5.2"


def test_parse_real_cell_response():
    cells = parse_cell_response(CELL_RESPONSE)
    assert cells == pytest.approx((3.508, 3.512, 3.512, 3.511))
    assert min(cells) == pytest.approx(3.508)
    assert max(cells) == pytest.approx(3.512)
    assert (max(cells) - min(cells)) * 1000 == pytest.approx(4)


@pytest.mark.parametrize(("raw", "expected"), [(123, 1.23), (-250, -2.5), (-32768, -327.68)])
def test_current_is_signed_16_bit(raw, expected):
    payload = bytearray(BASIC_RESPONSE[4:-3])
    payload[2:4] = raw.to_bytes(2, "big", signed=True)
    assert parse_basic_response(build_response(BASIC_REGISTER, payload)).current == expected


def test_frame_assembler_joins_fragments_and_multiple_frames():
    assembler = FrameAssembler()
    assert assembler.feed(BASIC_RESPONSE[:5]) == []
    assert assembler.feed(BASIC_RESPONSE[5:19]) == []
    assert assembler.feed(BASIC_RESPONSE[19:] + CELL_RESPONSE[:3]) == [BASIC_RESPONSE]
    assert assembler.feed(CELL_RESPONSE[3:]) == [CELL_RESPONSE]


def test_checksum_rejects_changed_payload():
    corrupted = bytearray(BASIC_RESPONSE)
    corrupted[5] ^= 1
    with pytest.raises(ChecksumError):
        parse_basic_response(bytes(corrupted))


def test_documented_register_inclusive_checksum():
    payload = BASIC_RESPONSE[4:-3]
    assert jbd_checksum(BASIC_REGISTER, 0, payload) == 0xF9E4
    # A frame using the documented variant is accepted as well as the captured
    # Berger response variant (0xF9E7).
    assert parse_basic_response(build_response(BASIC_REGISTER, payload)).voltage == 14.08


def test_wrong_register():
    with pytest.raises(RegisterMismatchError):
        parse_basic_response(CELL_RESPONSE)


def test_error_status():
    frame = build_response(BASIC_REGISTER, b"", status=0x80)
    with pytest.raises(DeviceStatusError, match="0x80"):
        parse_basic_response(frame)


def test_false_announced_payload_length():
    frame = bytearray(BASIC_RESPONSE)
    frame[3] -= 1
    with pytest.raises(PayloadLengthError):
        parse_basic_response(bytes(frame))


@pytest.mark.parametrize("index", [0, -1])
def test_invalid_frame_boundary(index):
    frame = bytearray(BASIC_RESPONSE)
    frame[index] = 0
    with pytest.raises(FrameBoundaryError):
        parse_basic_response(bytes(frame))


def test_odd_cell_payload_is_rejected():
    with pytest.raises(PayloadLengthError):
        parse_cell_response(build_response(CELL_REGISTER, b"\x0d\xb4\x00"))
