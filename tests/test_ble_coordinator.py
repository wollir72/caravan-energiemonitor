from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from caravan_energiemonitor.ble_coordinator import BleCoordinator, BleMode


class FakeVictron(QObject):
    scan_mode_changed = Signal(str)
    scan_paused = Signal()
    scan_resumed = Signal()
    exclusive_pause_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.pause_requests = 0
        self.resume_requests = 0
        self.exclusive_pause_requests = 0
        self.exclusive_resume_requests = 0

    def request_scan_pause(self):
        self.pause_requests += 1

    def request_scan_resume(self):
        self.resume_requests += 1

    def request_exclusive_pause(self):
        self.exclusive_pause_requests += 1

    def request_exclusive_resume(self):
        self.exclusive_resume_requests += 1


class FakeBerger(QObject):
    measurement_window_requested = Signal()
    measurement_window_finished = Signal()
    history_pause_changed = Signal(bool)
    normal_operation_ready = Signal()

    def __init__(self):
        super().__init__()
        self.permissions = 0
        self.releases = 0
        self.history_pause_requests = 0
        self.history_resume_requests = 0
        self.normal_operation_grants = 0

    def grant_measurement_permission(self):
        self.permissions += 1

    def confirm_measurement_window_finished(self):
        self.releases += 1

    def request_history_pause(self):
        self.history_pause_requests += 1

    def request_history_resume(self):
        self.history_resume_requests += 1

    def grant_normal_operation(self):
        self.normal_operation_grants += 1


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


def test_history_waits_for_scanner_and_berger_disconnect_then_restarts_berger():
    victron = FakeVictron()
    berger = FakeBerger()
    coordinator = BleCoordinator(victron, berger)
    grants = []
    coordinator.history_access_granted.connect(lambda: grants.append(True))

    assert coordinator.begin_history() is True
    assert coordinator.mode is BleMode.HISTORY_LOADING
    assert victron.exclusive_pause_requests == 1
    assert berger.history_pause_requests == 0
    assert grants == []

    victron.exclusive_pause_changed.emit(True)
    assert berger.history_pause_requests == 1
    assert grants == []
    berger.history_pause_changed.emit(True)
    assert grants == [True]

    coordinator.finish_history(True)
    assert coordinator.transition_in_progress is True
    berger.normal_operation_ready.emit()
    assert coordinator.mode is BleMode.HISTORY_READY
    assert berger.history_resume_requests == 1
    assert victron.exclusive_resume_requests == 0


def test_history_error_always_restarts_berger():
    victron = FakeVictron()
    berger = FakeBerger()
    coordinator = BleCoordinator(victron, berger)
    coordinator.begin_history()
    victron.exclusive_pause_changed.emit(True)
    berger.history_pause_changed.emit(True)
    coordinator.finish_history(False)
    berger.normal_operation_ready.emit()
    assert coordinator.mode is BleMode.HISTORY_ERROR
    assert berger.history_resume_requests == 1


def test_status_during_history_resumes_scan_only_after_gatt_finished():
    victron = FakeVictron()
    berger = FakeBerger()
    coordinator = BleCoordinator(victron, berger)
    coordinator.begin_history()
    victron.exclusive_pause_changed.emit(True)
    berger.history_pause_changed.emit(True)

    coordinator.request_status()
    assert victron.exclusive_resume_requests == 0
    coordinator.finish_history(True)
    berger.normal_operation_ready.emit()
    assert victron.exclusive_resume_requests == 1
    assert coordinator.mode is BleMode.HISTORY_LOADING
    victron.exclusive_pause_changed.emit(False)
    assert coordinator.mode is BleMode.STATUS
    assert berger.history_resume_requests == 1


def _enter_history(coordinator, victron, berger, grants):
    coordinator.history_access_granted.connect(lambda: grants.append("history"))
    coordinator.begin_history()
    victron.exclusive_pause_changed.emit(True)
    berger.history_pause_changed.emit(True)


def test_last_requested_target_replaces_older_target_during_entry():
    victron = FakeVictron()
    berger = FakeBerger()
    coordinator = BleCoordinator(victron, berger)
    grants = []
    coordinator.history_access_granted.connect(lambda: grants.append(True))

    coordinator.begin_history()
    coordinator.request_status()
    coordinator.begin_history()
    coordinator.request_status()
    assert victron.exclusive_pause_requests == 1
    assert coordinator.pending_target == "STATUS"

    victron.exclusive_pause_changed.emit(True)
    assert berger.history_pause_requests == 0
    assert victron.exclusive_resume_requests == 1
    victron.exclusive_pause_changed.emit(False)
    assert coordinator.mode is BleMode.STATUS
    assert coordinator.transition_in_progress is False
    assert grants == []


def test_latest_history_request_runs_after_cancelled_history_restore():
    victron = FakeVictron()
    berger = FakeBerger()
    coordinator = BleCoordinator(victron, berger)
    grants = []
    _enter_history(coordinator, victron, berger, grants)
    assert grants == ["history"]

    coordinator.request_status()
    coordinator.begin_history()
    coordinator.finish_history(False)
    assert grants == ["history"]
    assert coordinator.transition_in_progress is True

    berger.normal_operation_ready.emit()
    assert berger.history_pause_requests == 2
    berger.history_pause_changed.emit(True)
    assert grants == ["history", "history"]
    assert victron.exclusive_resume_requests == 0


def test_pending_history_is_replaced_by_newer_status_during_restore():
    victron = FakeVictron()
    berger = FakeBerger()
    coordinator = BleCoordinator(victron, berger)
    grants = []
    _enter_history(coordinator, victron, berger, grants)
    coordinator.request_status()
    coordinator.begin_history()
    coordinator.request_status()
    coordinator.finish_history(False)

    berger.normal_operation_ready.emit()
    assert victron.exclusive_resume_requests == 1
    assert berger.history_pause_requests == 1
    victron.exclusive_pause_changed.emit(False)
    assert coordinator.mode is BleMode.STATUS
    assert grants == ["history"]


def test_shutdown_discards_pending_transition_and_ignores_late_confirmations():
    victron = FakeVictron()
    berger = FakeBerger()
    coordinator = BleCoordinator(victron, berger)
    grants = []
    coordinator.history_access_granted.connect(lambda: grants.append(True))
    coordinator.begin_history()
    coordinator.request_status()
    coordinator.shutdown()

    victron.exclusive_pause_changed.emit(True)
    berger.history_pause_changed.emit(True)
    berger.normal_operation_ready.emit()
    assert coordinator.mode is BleMode.SHUTDOWN
    assert coordinator.transition_in_progress is False
    assert grants == []


def test_history_error_can_transition_to_status_only_after_restore_and_scan():
    victron = FakeVictron()
    berger = FakeBerger()
    coordinator = BleCoordinator(victron, berger)
    grants = []
    _enter_history(coordinator, victron, berger, grants)
    coordinator.finish_history(False)
    berger.normal_operation_ready.emit()
    assert coordinator.mode is BleMode.HISTORY_ERROR

    coordinator.request_status()
    assert coordinator.transition_in_progress is True
    victron.exclusive_pause_changed.emit(False)
    assert coordinator.mode is BleMode.STATUS
