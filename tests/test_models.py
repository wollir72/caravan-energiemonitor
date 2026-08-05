from __future__ import annotations

from dataclasses import FrozenInstanceError
from enum import Enum

import pytest

from caravan_energiemonitor.models import (
    BatterySnapshot,
    SolarSnapshot,
    format_capacity,
    format_cell_delta,
    format_cell_voltage,
    format_current,
    format_enum,
    format_installed_power,
    format_percent,
    format_power,
    format_signed_current,
    format_temperature,
    format_voltage,
    format_yield,
)
from caravan_energiemonitor.widgets.power_gauge import clamp_gauge_value, reference_mark_ratio


class ExampleEnum(Enum):
    OFF = 0
    BULK = 3
    ABSORPTION = 4
    FLOAT = 5
    STORAGE = 6
    NO_ERROR = 7
    SOMETHING_NEW = 99


def test_snapshot_is_immutable():
    snapshot = SolarSnapshot(solar_power=42)
    with pytest.raises(FrozenInstanceError):
        snapshot.solar_power = 43

    battery = BatterySnapshot(voltage=12.8)
    with pytest.raises(FrozenInstanceError):
        battery.voltage = 13.0


def test_numeric_formatting():
    assert format_voltage(13.456) == "13.46 V"
    assert format_current(1.234) == "1.23 A"
    assert format_power(123.6) == "124 W"
    assert format_percent(100) == "100 %"
    assert format_capacity(199.01) == "199.01 Ah"
    assert format_temperature(29.66) == "29.7 °C"
    assert format_cell_voltage(3.508) == "3.508 V"
    assert format_cell_delta(0.004) == "4 mV"
    assert format_signed_current(1.25) == "+1.25 A"
    assert format_signed_current(-2.5) == "-2.50 A"


def test_yield_formatting():
    assert format_yield(850) == "850 Wh"
    assert format_yield(1250) == "1.25 kWh"


def test_installed_power_is_wp():
    assert format_installed_power(200) == "200 Wp"


@pytest.mark.parametrize(
    ("member", "expected"),
    [
        (ExampleEnum.OFF, "Aus"),
        (ExampleEnum.BULK, "Bulk"),
        (ExampleEnum.ABSORPTION, "Absorption"),
        (ExampleEnum.FLOAT, "Float"),
        (ExampleEnum.STORAGE, "Lagerung"),
        (ExampleEnum.NO_ERROR, "Kein Fehler"),
        (ExampleEnum.SOMETHING_NEW, "Something New"),
    ],
)
def test_enum_translation(member, expected):
    assert format_enum(member) == expected


@pytest.mark.parametrize(
    "formatter",
    [
        format_voltage,
        format_current,
        format_signed_current,
        format_power,
        format_percent,
        format_capacity,
        format_temperature,
        format_cell_voltage,
        format_cell_delta,
        format_yield,
        format_installed_power,
        format_enum,
    ],
)
def test_none_values(formatter):
    assert formatter(None) == "—"


def test_gauge_value_is_clamped_without_changing_measurement():
    assert clamp_gauge_value(300, 250) == 250
    assert clamp_gauge_value(-10, 250) == 0
    assert clamp_gauge_value(200, 250) == 200
    assert clamp_gauge_value(None, 250) is None


def test_reference_mark_for_200_w_on_250_w_scale():
    assert reference_mark_ratio(200, 250) == pytest.approx(0.8)
