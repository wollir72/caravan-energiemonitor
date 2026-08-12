"""Strictly read-only VictronConnect GATT history protocol subset."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ...models import VictronHistoryDay, VictronHistorySummary

SERVICE_UUID = "306b0001-b081-4037-83dc-e59fcc3cdfd0"
FLOW_CONTROL_UUID = "306b0002-b081-4037-83dc-e59fcc3cdfd0"
COMMAND_UUID = "306b0003-b081-4037-83dc-e59fcc3cdfd0"
LONG_COMMAND_UUID = "306b0004-b081-4037-83dc-e59fcc3cdfd0"
REQUIRED_CHARACTERISTIC_UUIDS = (
    FLOW_CONTROL_UUID,
    COMMAND_UUID,
    LONG_COMMAND_UUID,
)

TOTAL_HISTORY_REGISTER = 0x104F
FIRST_DAY_REGISTER = 0x1050
LAST_DAY_REGISTER = 0x106E
MAX_HISTORY_DAYS = 30
EXPLICITLY_BLOCKED_REGISTERS = frozenset({0x1030, 0x0004})

READ_REQUEST = 0x05
READ_OPERATION = 0x81
REGISTER_CLASS = 0x03
TWO_BYTE_REGISTER_ID = 0x19
TYPED_RESPONSE = 0x08
RECORD_TYPE = 0x58

FLOW_ENABLE = bytes.fromhex("FA 80 FF")
FLOW_START = bytes.fromhex("F9 80")
FLOW_ACK = bytes.fromhex("F9 01")
FLOW_KEEPALIVE = bytes.fromhex("F9 41")
COMMAND_START = bytes.fromhex("01")
MPPT_INITIALIZE = bytes.fromhex("03 03")

TOTAL_PAYLOAD_LENGTHS = frozenset({19, 34})
DAY_PAYLOAD_LENGTH = 34
WRAPPER_LENGTH = 7
MAX_FRAME_LENGTH = 64
MAX_NOTIFICATION_LENGTH = 128


class VictronGattError(Exception):
    """Expected transport-independent Victron GATT failure."""


class SafetyViolation(VictronGattError, ValueError):
    """An operation outside the immutable read-only allowlist."""


class ProtocolError(VictronGattError, ValueError):
    """A malformed or unsupported response."""


def is_history_register(register: int) -> bool:
    return register == TOTAL_HISTORY_REGISTER or FIRST_DAY_REGISTER <= register <= LAST_DAY_REGISTER


def history_day_registers(days_available: int) -> tuple[int, ...]:
    """Return only the available documented day registers, capped at 30."""
    if days_available < 0:
        raise ValueError("Die Anzahl History-Tage darf nicht negativ sein.")
    count = min(days_available, MAX_HISTORY_DAYS)
    return tuple(FIRST_DAY_REGISTER + index for index in range(count))


def build_history_read_request(
    register: int,
    *,
    request: int = READ_REQUEST,
    operation: int = READ_OPERATION,
) -> bytes:
    """Build the only supported semantic operation: an allowlisted register read."""
    if register in EXPLICITLY_BLOCKED_REGISTERS:
        raise SafetyViolation(f"Mutierendes Register 0x{register:04X} ist explizit gesperrt.")
    if not is_history_register(register):
        raise SafetyViolation(f"Register 0x{register:04X} ist nicht für History erlaubt.")
    if request != READ_REQUEST:
        raise SafetyViolation("Nur READ-Request 0x05 ist erlaubt.")
    if operation != READ_OPERATION:
        raise SafetyViolation("Nur READ-Operation 0x81 ist erlaubt.")
    return bytes((READ_REQUEST, REGISTER_CLASS, READ_OPERATION, TWO_BYTE_REGISTER_ID, register >> 8, register & 0xFF))


_ALLOWED_SESSION_FRAMES = frozenset(
    {
        (FLOW_CONTROL_UUID, FLOW_ENABLE),
        (FLOW_CONTROL_UUID, FLOW_START),
        (FLOW_CONTROL_UUID, FLOW_KEEPALIVE),
        (COMMAND_UUID, COMMAND_START),
        (COMMAND_UUID, MPPT_INITIALIZE),
    }
)


def validate_outgoing_frame(characteristic_uuid: str, payload: bytes) -> None:
    """Apply the safety allowlist immediately before every GATT write."""
    target = characteristic_uuid.lower()
    immutable = bytes(payload)
    if (target, immutable) in _ALLOWED_SESSION_FRAMES:
        return
    if target == COMMAND_UUID and len(immutable) == 6:
        register = int.from_bytes(immutable[4:6], "big")
        if immutable == build_history_read_request(register):
            return
    raise SafetyViolation(
        f"Nicht erlaubter Victron-GATT-Frame: {target}, {immutable.hex(' ').upper()}"
    )


def _allowed_lengths(register: int) -> frozenset[int]:
    if register == TOTAL_HISTORY_REGISTER:
        return TOTAL_PAYLOAD_LENGTHS
    if FIRST_DAY_REGISTER <= register <= LAST_DAY_REGISTER:
        return frozenset({DAY_PAYLOAD_LENGTH})
    raise SafetyViolation(f"Register 0x{register:04X} ist nicht erlaubt.")


def parse_history_frame(frame: bytes, expected_register: int) -> bytes:
    if not is_history_register(expected_register):
        raise SafetyViolation(f"Antwortprüfung für 0x{expected_register:04X} ist nicht erlaubt.")
    raw = bytes(frame)
    if len(raw) < WRAPPER_LENGTH or len(raw) > MAX_FRAME_LENGTH:
        raise ProtocolError("Ungültige Länge des Victron-History-Frames.")
    if raw[0] != TYPED_RESPONSE:
        raise ProtocolError(f"Unbekannter Antworttyp 0x{raw[0]:02X}.")
    if raw[1:3] != bytes((REGISTER_CLASS, TWO_BYTE_REGISTER_ID)):
        raise ProtocolError("Ungültige Registerkennung im History-Wrapper.")
    register = int.from_bytes(raw[3:5], "big")
    if register != expected_register:
        raise ProtocolError(
            f"Falsche Register-ID 0x{register:04X}; erwartet 0x{expected_register:04X}."
        )
    if raw[5] != RECORD_TYPE:
        raise ProtocolError(f"Unbekannter Record-Typ 0x{raw[5]:02X}.")
    declared = raw[6]
    if declared not in _allowed_lengths(expected_register):
        raise ProtocolError(f"Unzulässige Payloadlänge {declared} für 0x{expected_register:04X}.")
    if len(raw) != WRAPPER_LENGTH + declared:
        raise ProtocolError("Deklarierte und tatsächliche History-Länge widersprechen sich.")
    return raw[WRAPPER_LENGTH:]


def decode_history_summary(payload: bytes) -> VictronHistorySummary:
    raw = bytes(payload)
    if len(raw) not in TOTAL_PAYLOAD_LENGTHS:
        raise ProtocolError("0x104F hat weder das 19- noch das 34-Byte-Layout.")
    expected_flag = 0 if len(raw) == 19 else 1
    if raw[0] != expected_flag or raw[1] != 0:
        raise ProtocolError("0x104F enthält ein unbekanntes Layout.")
    if len(raw) == 34 and raw[21:] != b"\xFF" * 13:
        raise ProtocolError("0x104F enthält unbekannte reservierte Daten.")
    days = raw[18]
    if days > MAX_HISTORY_DAYS:
        raise ProtocolError(f"0x104F meldet mehr als {MAX_HISTORY_DAYS} Tage.")
    return VictronHistorySummary(
        days_available=days,
        yield_since_reset_kwh=int.from_bytes(raw[6:10], "little") * 0.01,
        lifetime_yield_kwh=int.from_bytes(raw[10:14], "little") * 0.01,
        max_pv_voltage_v=int.from_bytes(raw[14:16], "little") * 0.01,
        max_battery_voltage_v=int.from_bytes(raw[16:18], "little") * 0.01,
        min_battery_voltage_v=(int.from_bytes(raw[19:21], "little") * 0.01 if len(raw) == 34 else None),
        error_history=(raw[2], raw[3], raw[4], raw[5]),
    )


def decode_history_day(payload: bytes, day_index: int) -> VictronHistoryDay:
    raw = bytes(payload)
    if len(raw) != DAY_PAYLOAD_LENGTH:
        raise ProtocolError(f"Tagesrecord hat {len(raw)} statt 34 Byte.")
    if raw[0] == 0x04:
        raise ProtocolError("Tagesregister enthält einen leeren Record.")
    if raw[0] != 0 or raw[13] != 0:
        raise ProtocolError("Tagesregister enthält ein unbekanntes Layout.")
    consumed_raw = int.from_bytes(raw[5:9], "little")
    return VictronHistoryDay(
        day_index=day_index,
        sequence_number=int.from_bytes(raw[32:34], "little"),
        yield_kwh=int.from_bytes(raw[1:5], "little") * 0.01,
        load_consumption_kwh=None if consumed_raw == 0xFFFFFFFF else consumed_raw * 0.01,
        battery_voltage_max_v=int.from_bytes(raw[9:11], "little") * 0.01,
        battery_voltage_min_v=int.from_bytes(raw[11:13], "little") * 0.01,
        errors=(raw[14], raw[15], raw[16], raw[17]),
        bulk_minutes=int.from_bytes(raw[18:20], "little"),
        absorption_minutes=int.from_bytes(raw[20:22], "little"),
        float_minutes=int.from_bytes(raw[22:24], "little"),
        max_pv_power_w=int.from_bytes(raw[24:28], "little"),
        max_battery_current_a=int.from_bytes(raw[28:30], "little") * 0.1,
        max_pv_voltage_v=int.from_bytes(raw[30:32], "little") * 0.01,
    )


class HistoryResponseAssembler:
    """Reassemble exactly one ordered response from command notifications."""

    def __init__(self, register: int) -> None:
        if not is_history_register(register):
            raise SafetyViolation(f"Register 0x{register:04X} ist nicht erlaubt.")
        self.register = register
        self._buffer = bytearray()
        self._source: str | None = None
        self._expected_length: int | None = None

    def feed(self, characteristic_uuid: str, fragment: bytes) -> bytes | None:
        source = characteristic_uuid.lower()
        if source not in (COMMAND_UUID, LONG_COMMAND_UUID):
            raise ProtocolError("History-Fragment kam auf unbekannter Characteristic.")
        incoming = bytes(fragment)
        if len(incoming) > MAX_NOTIFICATION_LENGTH:
            raise ProtocolError("History-Notification ist zu lang.")
        if not self._buffer:
            prefix = bytes((TYPED_RESPONSE, REGISTER_CLASS, TWO_BYTE_REGISTER_ID, self.register >> 8, self.register & 0xFF))
            if len(incoming) >= 5 and incoming[1:5] == prefix[1:] and incoming[0] != TYPED_RESPONSE:
                raise ProtocolError("Unbekannter Antworttyp für das erwartete Register.")
            if not incoming.startswith(prefix):
                return None
            self._source = source
        elif source != self._source:
            raise ProtocolError("Characteristic wechselte innerhalb eines Records.")
        self._buffer.extend(incoming)
        if len(self._buffer) > MAX_FRAME_LENGTH:
            raise ProtocolError("History-Antwort ist zu lang.")
        if self._expected_length is None and len(self._buffer) >= WRAPPER_LENGTH:
            if self._buffer[5] != RECORD_TYPE or self._buffer[6] not in _allowed_lengths(self.register):
                raise ProtocolError("Unbekannter Record-Typ oder Payloadlänge.")
            self._expected_length = WRAPPER_LENGTH + self._buffer[6]
        if self._expected_length is None or len(self._buffer) < self._expected_length:
            return None
        if len(self._buffer) != self._expected_length:
            raise ProtocolError("History-Antwort enthält zusätzliche Daten.")
        frame = bytes(self._buffer)
        parse_history_frame(frame, self.register)
        return frame


class NotificationState:
    """Route flow acknowledgements and the currently armed history record."""

    def __init__(self) -> None:
        self.acknowledgements: asyncio.Queue[bytes] = asyncio.Queue()
        self._assembler: HistoryResponseAssembler | None = None
        self._response: asyncio.Future[bytes] | None = None

    def arm(self, register: int) -> None:
        if self._response is not None and not self._response.done():
            raise ProtocolError("Vorherige History-Abfrage ist noch aktiv.")
        self._assembler = HistoryResponseAssembler(register)
        self._response = asyncio.get_running_loop().create_future()

    def callback_for(self, characteristic_uuid: str) -> Callable[[Any, bytearray], None]:
        source = characteristic_uuid.lower()

        def callback(_sender: Any, data: bytearray) -> None:
            payload = bytes(data)
            if source == FLOW_CONTROL_UUID:
                if payload == FLOW_ACK:
                    self.acknowledgements.put_nowait(payload)
                return
            if self._assembler is None or self._response is None or self._response.done():
                return
            try:
                frame = self._assembler.feed(source, payload)
            except ProtocolError as exc:
                self._response.set_exception(exc)
            else:
                if frame is not None:
                    self._response.set_result(frame)

        return callback

    async def wait_for_ack(self, timeout: float, label: str) -> None:
        try:
            ack = await asyncio.wait_for(self.acknowledgements.get(), timeout)
        except TimeoutError as exc:
            raise ProtocolError(f"Keine F9-01-Bestätigung für {label}.") from exc
        if ack != FLOW_ACK:
            raise ProtocolError(f"Unerwartete Flow-Control-Antwort für {label}.")

    async def wait_for_response(self, timeout: float, register: int) -> bytes:
        if self._assembler is None or self._response is None or self._assembler.register != register:
            raise ProtocolError("History-Collector ist nicht passend aktiviert.")
        try:
            return await asyncio.wait_for(self._response, timeout)
        except TimeoutError as exc:
            raise ProtocolError(f"Keine History-Antwort für 0x{register:04X}.") from exc
