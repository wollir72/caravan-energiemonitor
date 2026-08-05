from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
import pytest

from caravan_energiemonitor.config import (
    AppConfig,
    BergerConfig,
    DisplayConfig,
    SolarConfig,
    VictronConfig,
)
from caravan_energiemonitor.models import BatterySnapshot, MonitorStatus
import caravan_energiemonitor.main_window as main_window_module


class FakeVictronWorker(QObject):
    snapshot_received = Signal(object)
    target_activity = Signal()
    scanner_heartbeat = Signal()
    bluetooth_error = Signal(str)
    scan_paused = Signal()
    scan_resumed = Signal()
    scan_mode_changed = Signal(str)

    def __init__(self, _config, _parent=None):
        super().__init__()
        self.started = False
        self.stop_requested = False
        self.terminated = False
        self.pause_requests = 0
        self.resume_requests = 0

    def start(self):
        self.started = True

    def request_stop(self):
        self.stop_requested = True

    def request_scan_pause(self):
        self.pause_requests += 1

    def request_scan_resume(self):
        if not self.stop_requested:
            self.resume_requests += 1

    def wait(self, _milliseconds):
        return True

    def terminate(self):
        self.terminated = True


class FakeBergerWorker(QObject):
    snapshot_received = Signal(object)
    target_activity = Signal()
    worker_heartbeat = Signal()
    bluetooth_error = Signal(str)
    connection_state_changed = Signal(bool)
    initial_connection_attempt_finished = Signal()
    measurement_window_requested = Signal()
    measurement_window_finished = Signal()

    def __init__(self, _config, _parent=None):
        super().__init__()
        self.started = False
        self.stop_requested = False
        self.terminated = False
        self.permissions = 0
        self.releases = 0

    def start(self):
        self.started = True

    def request_stop(self):
        self.stop_requested = True

    def grant_measurement_permission(self):
        self.permissions += 1

    def confirm_measurement_window_finished(self):
        self.releases += 1

    def wait(self, _milliseconds):
        return True

    def terminate(self):
        self.terminated = True


class FakeCloseEvent:
    def __init__(self):
        self.accepted = False
        self.ignored = False

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def config():
    return AppConfig(
        victron=VictronConfig(
            name="Victron",
            address="F9:9F:71:BC:CC:D0",
            advertisement_key="0123456789abcdef0123456789abcdef",
        ),
        berger=BergerConfig(
            enabled=True,
            name="Berger",
            bluetooth_name="TEST",
            address="A5:C2:37:63:DC:37",
            capacity_ah=200,
        ),
        solar=SolarConfig(2, 100, 200),
        display=DisplayConfig(10.0, 250.0),
    )


@pytest.fixture
def window(monkeypatch, qapp, config):
    monkeypatch.setattr(main_window_module, "VictronWorker", FakeVictronWorker)
    monkeypatch.setattr(main_window_module, "BergerWorker", FakeBergerWorker)
    value = main_window_module.MainWindow(config)
    yield value
    value._status_timer.stop()
    value.close()


def test_first_complete_berger_snapshot_starts_victron(window):
    assert window.berger_worker is not None
    assert window.berger_worker.started is True
    assert window.worker.started is False

    window.berger_worker.initial_connection_attempt_finished.emit()
    assert window.worker.started is False

    window.berger_worker.snapshot_received.emit(BatterySnapshot())
    assert window.worker.started is True


def test_initial_timeout_starts_victron_despite_berger_error(window):
    assert window.worker.started is False
    window._on_berger_error("test timeout")
    window._start_victron_after_initial_timeout()
    assert window.worker.started is True


def test_device_status_errors_are_independent(window):
    window._on_berger_error("Berger test error")
    assert window._berger_tracker.status() is MonitorStatus.BLUETOOTH_ERROR
    assert window._victron_tracker.status() is not MonitorStatus.BLUETOOTH_ERROR

    window._on_victron_error("Victron test error")
    assert window._victron_tracker.error_message == "Victron test error"
    assert window._berger_tracker.error_message == "Berger test error"


def test_close_requests_stop_for_both_workers(window):
    event = FakeCloseEvent()
    window.closeEvent(event)

    assert window.worker.stop_requested is True
    assert window.berger_worker is not None
    assert window.berger_worker.stop_requested is True
    assert event.accepted is True
    assert event.ignored is False
