from __future__ import annotations

import logging
from pathlib import Path
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from .config import ConfigError, load_config


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Caravan-Energiemonitor")
    app.setOrganizationName("Caravan-Energiemonitor")

    try:
        config = load_config(Path.cwd() / "config.toml")
    except ConfigError as exc:
        QMessageBox.critical(None, "Konfigurationsfehler", str(exc))
        return 2

    from .main_window import MainWindow

    window = MainWindow(config)
    window.show()
    return app.exec()
