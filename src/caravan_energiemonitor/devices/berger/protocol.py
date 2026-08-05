"""Read-only subset of the JBD protocol used by the Berger battery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import struct

BASIC_REGISTER = 0x03
CELL_REGISTER = 0x04
BASIC_INFO_COMMAND = bytes.fromhex("DD A5 03 00 FF FD 77")
CELL_VOLTAGES_COMMAND = bytes.fromhex("DD A5 04 00 FF FC 77")


class BergerProtocolError(ValueError):
    """Base class for malformed or rejected JBD frames."""


class FrameBoundaryError(BergerProtocolError):
    """A frame has an invalid start or end marker."""


class RegisterMismatchError(BergerProtocolError):
    """A response belongs to a register other than the requested one."""


class DeviceStatusError(BergerProtocolError):
    """The BMS returned a non-zero status byte."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"BMS-Fehlerstatus 0x{status:02X}")


class PayloadLengthError(BergerProtocolError):
    """The announced and actual payload sizes differ."""


class ChecksumError(BergerProtocolError):
    """A response checksum is invalid."""


@dataclass(frozen=True, slots=True)
class BasicData:
    voltage: float
    current: float
    remaining_capacity_ah: float
    nominal_capacity_ah: float
    cycle_count: int
    production_date: date
    software_version: str
    state_of_charge_percent: int
    protection_status: int
    charge_mosfet_enabled: bool
    discharge_mosfet_enabled: bool
    cell_count: int
    temperatures_celsius: tuple[float, ...]
    extension_bytes: bytes


def jbd_checksum(register: int, status: int, payload: bytes) -> int:
    """Return the 16-bit two's complement response checksum."""
    return (-(register + status + len(payload) + sum(payload))) & 0xFFFF


def validate_response(frame: bytes, expected_register: int) -> bytes:
    """Validate a complete JBD response and return its payload."""
    if len(frame) < 7 or frame[0] != 0xDD:
        raise FrameBoundaryError("Ungültiges JBD-Startbyte (erwartet 0xDD).")
    if frame[-1] != 0x77:
        raise FrameBoundaryError("Ungültiges JBD-Endbyte (erwartet 0x77).")
    register, status, payload_length = frame[1], frame[2], frame[3]
    if register != expected_register:
        raise RegisterMismatchError(
            f"Antwortregister 0x{register:02X}, erwartet 0x{expected_register:02X}."
        )
    if status != 0:
        raise DeviceStatusError(status)
    expected_length = payload_length + 7
    if len(frame) != expected_length:
        raise PayloadLengthError(
            f"Frame hat {len(frame)} Bytes, angekündigt sind {expected_length}."
        )
    payload = frame[4 : 4 + payload_length]
    received_checksum = int.from_bytes(frame[-3:-1], "big")
    expected_checksum = jbd_checksum(register, status, payload)
    # The captured Berger frames use the widespread JBD response variant that
    # omits the register from the sum (F9E7/ FCE9). Accept both that real device
    # form and the documented register-inclusive form; both remain fully checked.
    berger_checksum = (-(status + payload_length + sum(payload))) & 0xFFFF
    if received_checksum not in (expected_checksum, berger_checksum):
        raise ChecksumError(
            f"Prüfsumme 0x{received_checksum:04X}, erwartet "
            f"0x{expected_checksum:04X} oder Berger-Variante 0x{berger_checksum:04X}."
        )
    return payload


class FrameAssembler:
    """Join arbitrary BLE notification fragments into complete JBD frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def clear(self) -> None:
        self._buffer.clear()

    def feed(self, fragment: bytes | bytearray) -> list[bytes]:
        self._buffer.extend(fragment)
        frames: list[bytes] = []
        while self._buffer:
            try:
                start = self._buffer.index(0xDD)
            except ValueError:
                self._buffer.clear()
                break
            if start:
                del self._buffer[:start]
            if len(self._buffer) < 4:
                break
            frame_length = self._buffer[3] + 7
            if len(self._buffer) < frame_length:
                break
            frames.append(bytes(self._buffer[:frame_length]))
            del self._buffer[:frame_length]
        return frames


def _production_date(raw: int) -> date:
    year = 2000 + ((raw >> 9) & 0x7F)
    month = (raw >> 5) & 0x0F
    day = raw & 0x1F
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise BergerProtocolError(f"Ungültiges JBD-Produktionsdatum: 0x{raw:04X}") from exc


def _software_version(raw: int) -> str:
    """Decode JBD nibbles, including the verified Berger 0x29 quirk."""
    if raw == 0x29:
        return "4.1"
    return f"{raw >> 4}.{raw & 0x0F}"


def parse_basic_response(frame: bytes) -> BasicData:
    payload = validate_response(frame, BASIC_REGISTER)
    if len(payload) < 23:
        raise PayloadLengthError("Basisdaten-Payload ist kürzer als 23 Bytes.")
    temperature_count = payload[22]
    known_length = 23 + temperature_count * 2
    if len(payload) < known_length:
        raise PayloadLengthError("Basisdaten enthalten nicht alle angekündigten Temperaturen.")

    voltage, current_raw, remaining, nominal, cycles, production = struct.unpack(
        ">HhHHHH", payload[:12]
    )
    protection = int.from_bytes(payload[16:18], "big")
    version_raw = payload[18]
    fet_status = payload[20]
    temperatures = tuple(
        int.from_bytes(payload[offset : offset + 2], "big") / 10 - 273.15
        for offset in range(23, known_length, 2)
    )
    return BasicData(
        voltage=voltage / 100,
        current=current_raw / 100,
        remaining_capacity_ah=remaining / 100,
        nominal_capacity_ah=nominal / 100,
        cycle_count=cycles,
        production_date=_production_date(production),
        software_version=_software_version(version_raw),
        state_of_charge_percent=payload[19],
        protection_status=protection,
        charge_mosfet_enabled=bool(fet_status & 0x01),
        discharge_mosfet_enabled=bool(fet_status & 0x02),
        cell_count=payload[21],
        temperatures_celsius=temperatures,
        extension_bytes=bytes(payload[known_length:]),
    )


def parse_cell_response(frame: bytes) -> tuple[float, ...]:
    payload = validate_response(frame, CELL_REGISTER)
    if len(payload) % 2:
        raise PayloadLengthError("Zellspannungs-Payload hat eine ungerade Bytezahl.")
    return tuple(
        int.from_bytes(payload[offset : offset + 2], "big") / 1000
        for offset in range(0, len(payload), 2)
    )
