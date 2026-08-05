from __future__ import annotations

import textwrap

import pytest

from caravan_energiemonitor.config import ConfigError, load_config


VALID = """
[victron]
name = "SmartSolar HQ2444NCAFK"
address = "F9:9F:71:BC:CC:D0"
advertisement_key = "0123456789abcdef0123456789abcdef"

[berger]
enabled = true
name = "Berger LiFePO4 200 Ah Pro"
bluetooth_name = "11608062501619"
address = "A5:C2:37:63:DC:37"
capacity_ah = 200

[solar]
panel_count = 2
panel_power_watts = 100
installed_power_watts = 200

[display]
stale_after_seconds = 10
maximum_solar_power = 250
"""


def write_config(tmp_path, content: str):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def test_valid_config(tmp_path):
    config = load_config(write_config(tmp_path, VALID))
    assert config.victron.address == "F9:9F:71:BC:CC:D0"
    assert config.berger.enabled is True
    assert config.berger.bluetooth_name == "11608062501619"
    assert config.berger.address == "A5:C2:37:63:DC:37"
    assert config.berger.capacity_ah == 200
    assert config.solar.installed_power_watts == 200
    assert config.display.maximum_solar_power == 250


def test_missing_key(tmp_path):
    with pytest.raises(ConfigError, match="advertisement_key fehlt"):
        load_config(write_config(tmp_path, VALID.replace('advertisement_key = "0123456789abcdef0123456789abcdef"', "")))


def test_invalid_hex_characters(tmp_path):
    invalid = VALID.replace("0123456789abcdef0123456789abcdef", "gf879ceb88f8cf1c116540ef6c0922e3")
    with pytest.raises(ConfigError, match="ungültige Hex-Zeichen"):
        load_config(write_config(tmp_path, invalid))


def test_wrong_key_length(tmp_path):
    invalid = VALID.replace("0123456789abcdef0123456789abcdef", "0123")
    with pytest.raises(ConfigError, match="genau 32"):
        load_config(write_config(tmp_path, invalid))


@pytest.mark.parametrize(
    ("field", "old", "new"),
    [
        ("panel_count", "panel_count = 2", "panel_count = 0"),
        ("panel_power_watts", "panel_power_watts = 100", "panel_power_watts = -1"),
        ("installed_power_watts", "installed_power_watts = 200", "installed_power_watts = 0"),
        ("maximum_solar_power", "maximum_solar_power = 250", "maximum_solar_power = -250"),
    ],
)
def test_invalid_pv_values(tmp_path, field, old, new):
    with pytest.raises(ConfigError, match=field):
        load_config(write_config(tmp_path, VALID.replace(old, new)))


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="nicht gefunden"):
        load_config(tmp_path / "missing.toml")


def test_berger_enabled_must_be_boolean(tmp_path):
    with pytest.raises(ConfigError, match="berger.enabled"):
        load_config(write_config(tmp_path, VALID.replace("enabled = true", 'enabled = "yes"')))


def test_invalid_berger_address(tmp_path):
    with pytest.raises(ConfigError, match="berger.address"):
        load_config(write_config(tmp_path, VALID.replace("A5:C2:37:63:DC:37", "invalid")))
