"""Central, failure-tolerant logging configuration for the application."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys


LOG_DIRECTORY_NAME = "caravan-energiemonitor"
LOG_FILE_NAME = "caravan-energiemonitor.log"
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

_HANDLER_MARKER = "_caravan_energiemonitor_handler"
_FILE_HANDLER = "file"
_TERMINAL_HANDLER = "terminal"
_failed_log_paths: set[Path] = set()


def get_log_path() -> Path:
    """Return the Linux state-file location without touching the filesystem."""
    state_home = os.environ.get("XDG_STATE_HOME")
    base_directory = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base_directory / LOG_DIRECTORY_NAME / LOG_FILE_NAME


def _application_handler(root: logging.Logger, kind: str) -> logging.Handler | None:
    return next(
        (
            handler
            for handler in root.handlers
            if getattr(handler, _HANDLER_MARKER, None) == kind
        ),
        None,
    )


def configure_logging() -> Path:
    """Configure INFO terminal/file logging once and tolerate file I/O failures."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    if _application_handler(root, _TERMINAL_HANDLER) is None:
        terminal_handler = logging.StreamHandler(sys.stderr)
        terminal_handler.setLevel(logging.INFO)
        terminal_handler.setFormatter(formatter)
        setattr(terminal_handler, _HANDLER_MARKER, _TERMINAL_HANDLER)
        root.addHandler(terminal_handler)

    log_path = get_log_path()
    if _application_handler(root, _FILE_HANDLER) is not None:
        return log_path
    if log_path in _failed_log_paths:
        return log_path

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except (OSError, ValueError) as exc:
        _failed_log_paths.add(log_path)
        logging.getLogger(__name__).warning(
            "Datei-Logging konnte nicht aktiviert werden (%s): %s",
            log_path,
            exc,
        )
        return log_path

    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    setattr(file_handler, _HANDLER_MARKER, _FILE_HANDLER)
    root.addHandler(file_handler)
    return log_path


def log_startup(log_path: Path) -> None:
    """Write the two non-sensitive startup records."""
    logger = logging.getLogger("caravan_energiemonitor.app")
    logger.info("Caravan-Energiemonitor gestartet")
    logger.info("Logdatei: %s", log_path)
