"""Immutable data and presentation-independent status logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import math
import time
from typing import Any


@dataclass(frozen=True, slots=True)
class SolarSnapshot:
    timestamp: datetime | None = None
    device_name: str | None = None
    address: str | None = None
    rssi: int | None = None
    model_name: str | None = None
    battery_voltage: float | None = None
    battery_charging_current: float | None = None
    solar_power: float | None = None
    yield_today: float | None = None
    external_device_load: float | None = None
    charge_state: Any | None = None
    charger_error: Any | None = None


@dataclass(frozen=True, slots=True)
class BatterySnapshot:
    """One immutable, presentation-independent Berger BMS reading."""

    timestamp: datetime | None = None
    device_name: str | None = None
    address: str | None = None
    rssi: int | None = None
    voltage: float | None = None
    current: float | None = None
    remaining_capacity_ah: float | None = None
    nominal_capacity_ah: float | None = None
    state_of_charge_percent: int | None = None
    cycle_count: int | None = None
    production_date: date | None = None
    software_version: str | None = None
    protection_status: int | None = None
    charge_mosfet_enabled: bool | None = None
    discharge_mosfet_enabled: bool | None = None
    cell_count: int | None = None
    temperatures_celsius: tuple[float, ...] = ()
    cell_voltages: tuple[float, ...] = ()
    minimum_cell_voltage: float | None = None
    maximum_cell_voltage: float | None = None
    cell_delta: float | None = None
    raw_basic_response: bytes | None = None
    raw_cell_response: bytes | None = None


@dataclass(frozen=True, slots=True)
class VictronHistorySummary:
    """Immutable, transport-independent SmartSolar history metadata."""

    days_available: int
    yield_since_reset_kwh: float
    lifetime_yield_kwh: float
    max_pv_voltage_v: float
    max_battery_voltage_v: float
    min_battery_voltage_v: float | None
    error_history: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class VictronHistoryDay:
    """One immutable daily SmartSolar history record."""

    day_index: int
    sequence_number: int
    yield_kwh: float
    load_consumption_kwh: float | None
    battery_voltage_max_v: float
    battery_voltage_min_v: float
    errors: tuple[int, int, int, int]
    bulk_minutes: int
    absorption_minutes: int
    float_minutes: int
    max_pv_power_w: int
    max_battery_current_a: float
    max_pv_voltage_v: float


class MonitorStatus(Enum):
    WAITING = "Warte auf Bluetooth-Daten"
    ONLINE = "Online"
    STALE = "Keine aktuellen Daten"
    BLUETOOTH_ERROR = "Bluetooth-Fehler"
    CONFIG_ERROR = "Konfigurationsfehler"


class StatusTracker:
    """Track target activity separately from de-duplicated measurements."""

    def __init__(self, stale_after_seconds: float, clock=time.monotonic):
        self.stale_after_seconds = stale_after_seconds
        self._clock = clock
        self._started_at = clock()
        self._last_activity: float | None = None
        self._has_snapshot = False
        self._error: str | None = None

    def record_activity(self, when: float | None = None) -> None:
        self._last_activity = self._clock() if when is None else when

    def record_snapshot(self, when: float | None = None) -> None:
        self._has_snapshot = True
        self._error = None
        self.record_activity(when)

    def record_error(self, message: str) -> None:
        self._error = message

    @property
    def error_message(self) -> str | None:
        return self._error

    def status(self, now: float | None = None) -> MonitorStatus:
        if self._error is not None:
            return MonitorStatus.BLUETOOTH_ERROR
        current = self._clock() if now is None else now
        reference = self._last_activity
        if reference is None:
            if current - self._started_at > self.stale_after_seconds:
                return MonitorStatus.STALE
            return MonitorStatus.WAITING
        if current - reference > self.stale_after_seconds:
            return MonitorStatus.STALE
        return MonitorStatus.ONLINE if self._has_snapshot else MonitorStatus.WAITING


def _finite(value: float | int | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def format_voltage(value: float | None) -> str:
    number = _finite(value)
    return "—" if number is None else f"{number:.2f} V"


def format_current(value: float | None) -> str:
    number = _finite(value)
    return "—" if number is None else f"{number:.2f} A"


def format_signed_current(value: float | None) -> str:
    """Format BMS current, where positive means charging."""
    number = _finite(value)
    return "—" if number is None else f"{number:+.2f} A"


def format_percent(value: float | int | None) -> str:
    number = _finite(value)
    return "—" if number is None else f"{number:.0f} %"


def format_capacity(value: float | None) -> str:
    number = _finite(value)
    return "—" if number is None else f"{number:.2f} Ah"


def format_temperature(value: float | None) -> str:
    number = _finite(value)
    return "—" if number is None else f"{number:.1f} °C"


def format_cell_voltage(value: float | None) -> str:
    number = _finite(value)
    return "—" if number is None else f"{number:.3f} V"


def format_cell_delta(value: float | None) -> str:
    """Format a voltage delta supplied in volts as millivolts."""
    number = _finite(value)
    return "—" if number is None else f"{number * 1000:.0f} mV"


def format_power(value: float | None) -> str:
    number = _finite(value)
    return "—" if number is None else f"{number:.0f} W"


def format_installed_power(value: float | int | None) -> str:
    number = _finite(value)
    return "—" if number is None else f"{number:.0f} Wp"


def format_yield(value: float | None) -> str:
    number = _finite(value)
    if number is None:
        return "—"
    if abs(number) >= 1000:
        return f"{number / 1000:.2f} kWh"
    return f"{number:.0f} Wh"


ENUM_TRANSLATIONS = {
    "OFF": "Aus",
    "BULK": "Bulk",
    "ABSORPTION": "Absorption",
    "FLOAT": "Float",
    "STORAGE": "Lagerung",
    "NO_ERROR": "Kein Fehler",
}


def format_enum(value: Any | None) -> str:
    if value is None:
        return "—"
    name = getattr(value, "name", None)
    if not isinstance(name, str):
        name = str(value)
    return ENUM_TRANSLATIONS.get(name.upper(), name.replace("_", " ").title())


def format_timestamp(value: datetime | None) -> str:
    return "—" if value is None else value.astimezone().strftime("%d.%m.%Y %H:%M:%S")
