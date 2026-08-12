from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from .config import ConfigError, load_config
from .logging_config import configure_logging, log_startup


def main() -> int:
    log_path = configure_logging()
    log_startup(log_path)
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
