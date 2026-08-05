from caravan_energiemonitor.models import MonitorStatus, StatusTracker


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_status_lifecycle_and_stale_detection():
    clock = Clock()
    tracker = StatusTracker(10, clock=clock)
    assert tracker.status() is MonitorStatus.WAITING

    clock.now = 1
    tracker.record_snapshot()
    assert tracker.status() is MonitorStatus.ONLINE

    clock.now = 12
    assert tracker.status() is MonitorStatus.STALE


def test_unchanged_target_advertisement_keeps_status_online():
    clock = Clock()
    tracker = StatusTracker(10, clock=clock)
    tracker.record_snapshot()
    clock.now = 8
    tracker.record_activity()
    clock.now = 15
    assert tracker.status() is MonitorStatus.ONLINE
    clock.now = 19
    assert tracker.status() is MonitorStatus.STALE


def test_bluetooth_error_takes_precedence():
    clock = Clock()
    tracker = StatusTracker(10, clock=clock)
    tracker.record_error("BlueZ nicht verfügbar")
    assert tracker.status() is MonitorStatus.BLUETOOTH_ERROR

    clock.now = 2
    tracker.record_activity()
    assert tracker.status() is MonitorStatus.BLUETOOTH_ERROR

    tracker.record_snapshot()
    assert tracker.status() is MonitorStatus.ONLINE
