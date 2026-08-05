from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from caravan_energiemonitor.ble_coordinator import BleCoordinator


class FakeVictron(QObject):
    scan_mode_changed = Signal(str)
    scan_paused = Signal()
    scan_resumed = Signal()

    def __init__(self):
        super().__init__()
        self.pause_requests = 0
        self.resume_requests = 0

    def request_scan_pause(self):
        self.pause_requests += 1

    def request_scan_resume(self):
        self.resume_requests += 1


class FakeBerger(QObject):
    measurement_window_requested = Signal()
    measurement_window_finished = Signal()

    def __init__(self):
        super().__init__()
        self.permissions = 0
        self.releases = 0

    def grant_measurement_permission(self):
        self.permissions += 1

    def confirm_measurement_window_finished(self):
        self.releases += 1


def test_active_scan_is_paused_before_measurement_permission():
    victron = FakeVictron()
    berger = FakeBerger()
    coordinator = BleCoordinator(victron, berger)
    victron.scan_mode_changed.emit("active")

    berger.measurement_window_requested.emit()
    assert victron.pause_requests == 1
    assert berger.permissions == 0

    victron.scan_paused.emit()
    assert berger.permissions == 1

    berger.measurement_window_finished.emit()
    assert victron.resume_requests == 1
    assert berger.releases == 0
    victron.scan_resumed.emit()
    assert berger.releases == 1


def test_passive_scan_does_not_need_pause():
    victron = FakeVictron()
    berger = FakeBerger()
    coordinator = BleCoordinator(victron, berger)
    victron.scan_mode_changed.emit("passive")

    berger.measurement_window_requested.emit()
    berger.measurement_window_finished.emit()

    assert victron.pause_requests == 0
    assert victron.resume_requests == 0
    assert berger.permissions == 1
    assert berger.releases == 1


def test_shutdown_prevents_resume():
    victron = FakeVictron()
    berger = FakeBerger()
    coordinator = BleCoordinator(victron, berger)
    victron.scan_mode_changed.emit("active")
    berger.measurement_window_requested.emit()
    victron.scan_paused.emit()

    coordinator.shutdown()
    berger.measurement_window_finished.emit()

    assert victron.resume_requests == 0
