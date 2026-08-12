from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from caravan_energiemonitor import logging_config


@pytest.fixture(autouse=True)
def isolated_application_handlers(monkeypatch, tmp_path):
    root = logging.getLogger()
    original_level = root.level
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    yield
    for handler in list(root.handlers):
        if getattr(handler, logging_config._HANDLER_MARKER, None) is not None:
            root.removeHandler(handler)
            handler.close()
    logging_config._failed_log_paths.clear()
    root.setLevel(original_level)


def application_handlers(kind: str) -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, logging_config._HANDLER_MARKER, None) == kind
    ]


def flush_application_handlers() -> None:
    for handler in logging.getLogger().handlers:
        if getattr(handler, logging_config._HANDLER_MARKER, None) is not None:
            handler.flush()


def test_log_path_uses_xdg_state_home(monkeypatch, tmp_path):
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    assert logging_config.get_log_path() == (
        state_home / "caravan-energiemonitor" / "caravan-energiemonitor.log"
    )


def test_log_path_falls_back_to_user_local_state(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    assert logging_config.get_log_path() == (
        tmp_path
        / "home"
        / ".local"
        / "state"
        / "caravan-energiemonitor"
        / "caravan-energiemonitor.log"
    )


def test_configuration_creates_directory_and_rotating_handler(tmp_path):
    log_path = logging_config.configure_logging()

    assert log_path.parent.is_dir()
    handlers = application_handlers(logging_config._FILE_HANDLER)
    assert len(handlers) == 1
    handler = handlers[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == 3
    assert handler.encoding.lower().replace("-", "") == "utf8"
    assert handler.level == logging.INFO


def test_configuration_is_idempotent_without_duplicate_handlers():
    first_path = logging_config.configure_logging()
    second_path = logging_config.configure_logging()

    assert second_path == first_path
    assert len(application_handlers(logging_config._FILE_HANDLER)) == 1
    assert len(application_handlers(logging_config._TERMINAL_HANDLER)) == 1


def test_info_warning_error_and_exception_are_written():
    log_path = logging_config.configure_logging()
    logger = logging.getLogger("caravan_energiemonitor.tests.logging")

    logger.info("Test-Info")
    logger.warning("Test-Warnung")
    logger.error("Test-Fehler")
    try:
        raise RuntimeError("Test-Ausnahme")
    except RuntimeError:
        logger.exception("Test-Traceback")
    flush_application_handlers()

    contents = log_path.read_text(encoding="utf-8")
    assert "INFO caravan_energiemonitor.tests.logging: Test-Info" in contents
    assert "WARNING caravan_energiemonitor.tests.logging: Test-Warnung" in contents
    assert "ERROR caravan_energiemonitor.tests.logging: Test-Fehler" in contents
    assert "Test-Traceback" in contents
    assert "Traceback (most recent call last)" in contents
    assert "RuntimeError: Test-Ausnahme" in contents


def test_startup_logs_resolved_path():
    log_path = logging_config.configure_logging()

    logging_config.log_startup(log_path)
    flush_application_handlers()

    contents = log_path.read_text(encoding="utf-8")
    assert "Caravan-Energiemonitor gestartet" in contents
    assert f"Logdatei: {log_path}" in contents


def test_file_logging_failure_keeps_terminal_configuration(monkeypatch, caplog):
    def fail_to_open(*_args, **_kwargs):
        raise PermissionError("Testzugriff verweigert")

    monkeypatch.setattr(logging_config, "RotatingFileHandler", fail_to_open)

    with caplog.at_level(logging.WARNING):
        log_path = logging_config.configure_logging()

    assert log_path == logging_config.get_log_path()
    assert len(application_handlers(logging_config._FILE_HANDLER)) == 0
    assert len(application_handlers(logging_config._TERMINAL_HANDLER)) == 1
    assert "Datei-Logging konnte nicht aktiviert werden" in caplog.text


def test_file_logging_failure_is_reported_only_once(monkeypatch, caplog):
    def fail_to_open(*_args, **_kwargs):
        raise PermissionError("Testzugriff verweigert")

    monkeypatch.setattr(logging_config, "RotatingFileHandler", fail_to_open)

    with caplog.at_level(logging.WARNING):
        logging_config.configure_logging()
        logging_config.configure_logging()

    warnings = [
        record
        for record in caplog.records
        if "Datei-Logging konnte nicht aktiviert werden" in record.message
    ]
    assert len(warnings) == 1
