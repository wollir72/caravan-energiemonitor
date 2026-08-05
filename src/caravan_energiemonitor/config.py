"""Strict TOML configuration loading for the supported devices."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib
from typing import Any


class ConfigError(ValueError):
    """A configuration file is missing or invalid."""


@dataclass(frozen=True, slots=True)
class VictronConfig:
    name: str
    address: str
    advertisement_key: str


@dataclass(frozen=True, slots=True)
class BergerConfig:
    enabled: bool
    name: str
    bluetooth_name: str
    address: str
    capacity_ah: float


@dataclass(frozen=True, slots=True)
class SolarConfig:
    panel_count: int
    panel_power_watts: int
    installed_power_watts: int


@dataclass(frozen=True, slots=True)
class DisplayConfig:
    stale_after_seconds: float
    maximum_solar_power: float


@dataclass(frozen=True, slots=True)
class AppConfig:
    victron: VictronConfig
    berger: BergerConfig
    solar: SolarConfig
    display: DisplayConfig


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"Abschnitt [{name}] fehlt oder ist ungültig.")
    return value


def _text(table: dict[str, Any], key: str, label: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} fehlt oder ist leer.")
    return value.strip()


def _positive_number(
    table: dict[str, Any], key: str, label: str, number_type: type = int
):
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{label} muss eine Zahl größer als 0 sein.")
    if number_type is int and not isinstance(value, int):
        raise ConfigError(f"{label} muss eine ganze Zahl sein.")
    return number_type(value)


def _validate_address(address: str, label: str) -> str:
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", address) is None:
        raise ConfigError(f"{label} muss eine gültige Bluetooth-MAC-Adresse sein.")
    return address.upper()


def load_config(path: str | Path = "config.toml") -> AppConfig:
    config_path = Path(path)
    try:
        with config_path.open("rb") as stream:
            raw = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Konfigurationsdatei nicht gefunden: {config_path}\n"
            "Bitte config.example.toml als config.toml kopieren."
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Konfiguration kann nicht gelesen werden: {exc}") from exc

    # [device] remains readable as a migration aid; the public model is no longer
    # generic and all newly written configuration uses [victron].
    victron = raw.get("victron", raw.get("device"))
    if not isinstance(victron, dict):
        raise ConfigError("Abschnitt [victron] fehlt oder ist ungültig.")
    berger = _table(raw, "berger")
    solar = _table(raw, "solar")
    display = _table(raw, "display")

    key = _text(victron, "advertisement_key", "victron.advertisement_key")
    if len(key) != 32:
        raise ConfigError("victron.advertisement_key muss genau 32 Hex-Zeichen enthalten.")
    if re.fullmatch(r"[0-9a-fA-F]{32}", key) is None:
        raise ConfigError("victron.advertisement_key enthält ungültige Hex-Zeichen.")

    enabled = berger.get("enabled")
    if not isinstance(enabled, bool):
        raise ConfigError("berger.enabled muss true oder false sein.")

    return AppConfig(
        victron=VictronConfig(
            name=_text(victron, "name", "victron.name"),
            address=_validate_address(
                _text(victron, "address", "victron.address"), "victron.address"
            ),
            advertisement_key=key.lower(),
        ),
        berger=BergerConfig(
            enabled=enabled,
            name=_text(berger, "name", "berger.name"),
            bluetooth_name=_text(
                berger, "bluetooth_name", "berger.bluetooth_name"
            ),
            address=_validate_address(
                _text(berger, "address", "berger.address"), "berger.address"
            ),
            capacity_ah=_positive_number(
                berger, "capacity_ah", "berger.capacity_ah", float
            ),
        ),
        solar=SolarConfig(
            panel_count=_positive_number(solar, "panel_count", "solar.panel_count"),
            panel_power_watts=_positive_number(
                solar, "panel_power_watts", "solar.panel_power_watts"
            ),
            installed_power_watts=_positive_number(
                solar, "installed_power_watts", "solar.installed_power_watts"
            ),
        ),
        display=DisplayConfig(
            stale_after_seconds=_positive_number(
                display, "stale_after_seconds", "display.stale_after_seconds", float
            ),
            maximum_solar_power=_positive_number(
                display, "maximum_solar_power", "display.maximum_solar_power", float
            ),
        ),
    )
