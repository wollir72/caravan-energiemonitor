from pathlib import Path

import caravan_energiemonitor
import caravan_energiemonitor.main_window as main_window_module


def test_package_version_is_0_2_1():
    assert caravan_energiemonitor.__version__ == "0.2.1"


def test_gui_uses_central_package_version():
    source = Path(main_window_module.__file__).read_text(encoding="utf-8")
    assert "from . import __version__" in source
    assert '"0.2.1"' not in source
