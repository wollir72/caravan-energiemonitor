from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QStackedWidget
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
from caravan_energiemonitor.models import VictronHistoryDay, VictronHistorySummary
import caravan_energiemonitor.main_window as main_window_module


class FakeVictronWorker(QObject):
    snapshot_received = Signal(object)
    target_activity = Signal()
    scanner_heartbeat = Signal()
    bluetooth_error = Signal(str)
    scan_paused = Signal()
    scan_resumed = Signal()
    scan_mode_changed = Signal(str)
    exclusive_pause_changed = Signal(bool)

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

    def request_exclusive_pause(self):
        self.exclusive_pause_changed.emit(True)

    def request_exclusive_resume(self):
        self.exclusive_pause_changed.emit(False)

    def isRunning(self):
        return self.started and not self.stop_requested

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
    history_pause_changed = Signal(bool)
    normal_operation_ready = Signal()

    def __init__(self, _config, _parent=None):
        super().__init__()
        self.started = False
        self.stop_requested = False
        self.terminated = False
        self.permissions = 0
        self.releases = 0
        self.normal_operation_grants = 0

    def start(self):
        self.started = True

    def request_stop(self):
        self.stop_requested = True

    def grant_measurement_permission(self):
        self.permissions += 1

    def confirm_measurement_window_finished(self):
        self.releases += 1

    def request_history_pause(self):
        self.history_pause_changed.emit(True)

    def request_history_resume(self):
        self.history_pause_changed.emit(False)
        self.normal_operation_ready.emit()

    def grant_normal_operation(self):
        self.normal_operation_grants += 1

    def isRunning(self):
        return self.started and not self.stop_requested

    def wait(self, _milliseconds):
        return True

    def terminate(self):
        self.terminated = True


class FakeHistoryWorker(QObject):
    loading_started = Signal()
    progress_changed = Signal(int, int)
    history_received = Signal(object, object)
    error_occurred = Signal(str)
    cancelled = Signal()
    finished = Signal()
    instances = []

    def __init__(self, _config, _parent=None):
        super().__init__()
        self.running = False
        self.stop_requested = False
        type(self).instances.append(self)

    def start(self):
        self.running = True
        self.loading_started.emit()

    def isRunning(self):
        return self.running

    def request_cancel(self):
        self.stop_requested = True

    def request_stop(self):
        self.request_cancel()

    def wait(self, _milliseconds):
        return True

    def terminate(self):
        self.running = False

    def complete(self, summary, days):
        self.history_received.emit(summary, days)
        self.running = False
        self.finished.emit()

    def finish_cancelled(self):
        self.cancelled.emit()
        self.running = False
        self.finished.emit()


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
    FakeHistoryWorker.instances.clear()
    monkeypatch.setattr(main_window_module, "VictronWorker", FakeVictronWorker)
    monkeypatch.setattr(main_window_module, "BergerWorker", FakeBergerWorker)
    monkeypatch.setattr(main_window_module, "VictronHistoryWorker", FakeHistoryWorker)
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


def _history_data():
    summary = VictronHistorySummary(1, 12.3, 45.6, 41.2, 14.4, 12.8, (0, 0, 0, 0))
    day = VictronHistoryDay(0, 1, 0.84, None, 14.2, 12.9, (0, 0, 0, 0), 10, 20, 30, 180, 12.4, 41.2)
    return summary, (day,)


def test_history_first_switch_loads_second_switch_uses_cache(window):
    assert window.victron_tabs.currentIndex() == 0
    window.victron_tabs.setCurrentIndex(1)
    assert len(FakeHistoryWorker.instances) == 1
    first = FakeHistoryWorker.instances[0]
    summary, days = _history_data()
    first.complete(summary, days)
    assert window.history_cache is not None

    window.victron_tabs.setCurrentIndex(0)
    window.victron_tabs.setCurrentIndex(1)
    assert len(FakeHistoryWorker.instances) == 1


def test_refresh_forces_reload_despite_cache(window):
    summary, days = _history_data()
    window.history_cache = (summary, days, main_window_module.datetime.now())
    window.victron_tabs.setCurrentIndex(1)
    assert len(FakeHistoryWorker.instances) == 0
    window.history_view.refresh_requested.emit()
    assert len(FakeHistoryWorker.instances) == 1


def test_fast_tab_changes_never_create_parallel_history_workers(window):
    window.victron_tabs.setCurrentIndex(1)
    first = FakeHistoryWorker.instances[0]
    window.victron_tabs.setCurrentIndex(0)
    window.victron_tabs.setCurrentIndex(1)
    window.victron_tabs.setCurrentIndex(0)

    assert first.stop_requested is True
    assert len(FakeHistoryWorker.instances) == 1
    first.finish_cancelled()
    assert window._ble_coordinator.mode.name == "STATUS"
    assert len(FakeHistoryWorker.instances) == 1


def test_latest_history_tab_request_starts_only_after_old_worker_finished(window):
    window.victron_tabs.setCurrentIndex(1)
    first = FakeHistoryWorker.instances[0]
    window.victron_tabs.setCurrentIndex(0)
    window.victron_tabs.setCurrentIndex(1)
    assert len(FakeHistoryWorker.instances) == 1

    first.finish_cancelled()
    assert len(FakeHistoryWorker.instances) == 2
    assert FakeHistoryWorker.instances[0].running is False
    assert FakeHistoryWorker.instances[1].running is True


def test_refresh_while_history_worker_is_active_is_ignored(window):
    window.victron_tabs.setCurrentIndex(1)
    assert len(FakeHistoryWorker.instances) == 1
    window.history_view.refresh_requested.emit()
    window.history_view.refresh_requested.emit()
    assert len(FakeHistoryWorker.instances) == 1


def test_shutdown_during_history_discards_pending_reload(window):
    window.victron_tabs.setCurrentIndex(1)
    worker = FakeHistoryWorker.instances[0]
    window._ble_coordinator.begin_history()
    event = FakeCloseEvent()
    window.closeEvent(event)
    assert worker.stop_requested is True
    assert window._ble_coordinator.mode.name == "SHUTDOWN"
    assert event.accepted is True


def test_victron_tabs_and_history_viewport_use_window_background(window):
    expected = window.victron_tabs.palette().color(QPalette.ColorRole.Window)
    status_page = window.victron_tabs.widget(0)
    tab_stack = window.victron_tabs.findChild(QStackedWidget)

    assert window.victron_tabs.autoFillBackground() is True
    assert status_page.autoFillBackground() is True
    assert window.history_view.autoFillBackground() is True
    assert tab_stack is not None
    assert tab_stack.autoFillBackground() is True
    assert window.history_view.table.palette().color(
        QPalette.ColorRole.Base
    ) == expected
    assert window.history_view.table.viewport().palette().color(
        QPalette.ColorRole.Base
    ) == expected
