"""Serialized Qt-side ownership of Victron and Berger BLE mode transitions."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from PySide6.QtCore import QObject, Signal

LOG = logging.getLogger(__name__)


class BleMode(Enum):
    STATUS = "STATUS"
    HISTORY_LOADING = "HISTORY_LOADING"
    HISTORY_READY = "HISTORY_READY"
    HISTORY_ERROR = "HISTORY_ERROR"
    SHUTDOWN = "SHUTDOWN"


class _Target(Enum):
    STATUS = "STATUS"
    HISTORY_VIEW = "HISTORY"
    HISTORY_LOAD = "HISTORY_RELOAD"


class _Transition(Enum):
    IDLE = "IDLE"
    STOPPING_HISTORY_SCAN = "STOPPING_HISTORY_SCAN"
    PAUSING_BERGER = "PAUSING_BERGER"
    HISTORY_RUNNING = "HISTORY_RUNNING"
    RESTORING_BERGER = "RESTORING_BERGER"
    RESUMING_STATUS_SCAN = "RESUMING_STATUS_SCAN"


class BleCoordinator(QObject):
    """Single Qt-side owner of live BLE arbitration and exclusive history mode."""

    history_access_granted = Signal()
    mode_changed = Signal(object)

    def __init__(self, victron_worker: Any, berger_worker: Any | None, parent=None) -> None:
        super().__init__(parent)
        self._victron = victron_worker
        self._berger = berger_worker
        self._scan_mode = "stopped"
        self._pause_pending = False
        self._active_window = False
        self._resume_pending = False
        self._window_open = False
        self._shutting_down = False
        self._mode = BleMode.STATUS
        self._history_scan_paused = False
        self._history_berger_paused = berger_worker is None
        self._target = _Target.STATUS
        self._transition = _Transition.IDLE
        self._history_request_serial = 0
        self._active_history_serial = 0
        self._settled_history_mode = BleMode.HISTORY_READY
        self._berger_restore_waiting = False

        victron_worker.scan_mode_changed.connect(self._on_scan_mode_changed)
        victron_worker.scan_paused.connect(self._on_scan_paused)
        victron_worker.scan_resumed.connect(self._on_scan_resumed)
        victron_worker.exclusive_pause_changed.connect(
            self._on_exclusive_pause_changed
        )
        if berger_worker is not None:
            berger_worker.measurement_window_requested.connect(
                self._on_measurement_window_requested
            )
            berger_worker.measurement_window_finished.connect(
                self._on_measurement_window_finished
            )
            berger_worker.history_pause_changed.connect(self._on_history_pause_changed)
            berger_worker.normal_operation_ready.connect(
                self._on_berger_normal_operation_ready
            )

    @property
    def mode(self) -> BleMode:
        return self._mode

    @property
    def transition_in_progress(self) -> bool:
        return self._transition is not _Transition.IDLE

    @property
    def pending_target(self) -> str:
        return self._target.value

    def _set_mode(self, mode: BleMode) -> None:
        if self._mode is mode:
            return
        self._mode = mode
        LOG.info("BLE-Modus: %s", mode.value)
        self.mode_changed.emit(mode)

    def begin_history(self) -> bool:
        """Request one serialized history load; the newest target always wins."""
        if self._shutting_down:
            return False
        self._history_request_serial += 1
        self._request_target(_Target.HISTORY_LOAD)
        return True

    def show_cached_history(self) -> None:
        """Keep Berger live, but silence advertisements while history is viewed."""
        if self._shutting_down:
            return
        self._settled_history_mode = BleMode.HISTORY_READY
        self._request_target(_Target.HISTORY_VIEW)

    def request_status(self) -> None:
        """Resume live scan only after an active history GATT worker has finished."""
        if self._shutting_down:
            return
        self._request_target(_Target.STATUS)

    def _request_target(self, target: _Target) -> None:
        previous = self._target
        self._target = target
        if self._transition is not _Transition.IDLE:
            LOG.info(
                "BLE-Transition aktiv; Ziel %s vorgemerkt%s",
                target.value,
                " (ersetzt " + previous.value + ")" if previous is not target else "",
            )
            return
        self._start_target_transition()

    def _start_target_transition(self) -> None:
        if self._shutting_down or self._transition is not _Transition.IDLE:
            return
        if self._target is _Target.STATUS:
            if self._history_scan_paused:
                self._start_scan_resume()
            else:
                self._set_mode(BleMode.STATUS)
            return
        if self._target is _Target.HISTORY_VIEW and self._history_scan_paused:
            self._set_mode(self._settled_history_mode)
            return

        LOG.info("BLE-Transition gestartet: %s -> HISTORY", self._mode.value)
        self._transition = _Transition.STOPPING_HISTORY_SCAN
        if self._target is _Target.HISTORY_LOAD:
            self._set_mode(BleMode.HISTORY_LOADING)
        self._victron.request_exclusive_pause()

    def finish_history(self, success: bool) -> None:
        """Called only after the history QThread has closed Victron GATT."""
        if self._shutting_down:
            return
        if self._transition is not _Transition.HISTORY_RUNNING:
            LOG.warning("History-Ende ohne aktive History-Transition ignoriert")
            return
        self._settled_history_mode = (
            BleMode.HISTORY_READY if success else BleMode.HISTORY_ERROR
        )
        if (
            self._target is _Target.HISTORY_LOAD
            and self._history_request_serial == self._active_history_serial
        ):
            self._target = _Target.HISTORY_VIEW
        self._start_berger_restore()

    def _start_berger_restore(self) -> None:
        self._transition = _Transition.RESTORING_BERGER
        self._berger_restore_waiting = self._berger is not None
        LOG.info("Victron History GATT beendet; Berger-Restore wird gestartet")
        if self._berger is None:
            self._after_berger_restore_ready()
        else:
            self._berger.request_history_resume()

    def _on_berger_normal_operation_ready(self) -> None:
        if self._shutting_down or self._transition is not _Transition.RESTORING_BERGER:
            return
        LOG.info("Berger-Restore hat einen sicheren Bestätigungspunkt erreicht")
        self._after_berger_restore_ready()

    def _after_berger_restore_ready(self) -> None:
        if self._target is _Target.STATUS:
            self._start_scan_resume()
            return
        if self._target is _Target.HISTORY_LOAD:
            LOG.info("Pending BLE-Ziel wird ausgeführt: HISTORY")
            self._transition = _Transition.PAUSING_BERGER
            if self._berger is None:
                self._history_berger_paused = True
                self._grant_history_access()
            else:
                self._berger.request_history_pause()
                self._berger.grant_normal_operation()
            return

        if self._berger is not None:
            self._berger.grant_normal_operation()
        self._berger_restore_waiting = False
        self._transition = _Transition.IDLE
        self._set_mode(self._settled_history_mode)
        LOG.info("BLE-Transition beendet: %s", self._mode.value)

    def _start_scan_resume(self) -> None:
        self._transition = _Transition.RESUMING_STATUS_SCAN
        self._victron.request_exclusive_resume()

    def _on_exclusive_pause_changed(self, paused: bool) -> None:
        self._history_scan_paused = paused
        if self._shutting_down:
            return
        if paused and self._transition is _Transition.STOPPING_HISTORY_SCAN:
            if self._target is _Target.STATUS:
                self._start_scan_resume()
            elif self._target is _Target.HISTORY_VIEW:
                if self._berger_restore_waiting and self._berger is not None:
                    self._berger.grant_normal_operation()
                self._berger_restore_waiting = False
                self._transition = _Transition.IDLE
                self._set_mode(self._settled_history_mode)
                LOG.info("BLE-Transition beendet: %s", self._mode.value)
            else:
                self._transition = _Transition.PAUSING_BERGER
                if self._berger is None:
                    self._history_berger_paused = True
                    self._grant_history_access()
                else:
                    self._berger.request_history_pause()
        elif not paused and self._transition is _Transition.RESUMING_STATUS_SCAN:
            if self._target is not _Target.STATUS:
                LOG.info(
                    "Scanner-Resume abgeschlossen; neueres Ziel %s bleibt vorgemerkt",
                    self._target.value,
                )
                self._transition = _Transition.STOPPING_HISTORY_SCAN
                self._victron.request_exclusive_pause()
                return
            if self._berger_restore_waiting and self._berger is not None:
                self._berger.grant_normal_operation()
            self._berger_restore_waiting = False
            self._transition = _Transition.IDLE
            self._set_mode(BleMode.STATUS)
            LOG.info("BLE-Transition beendet: STATUS")
            if self._target is not _Target.STATUS:
                LOG.info("Pending BLE-Ziel wird ausgeführt: %s", self._target.value)
                self._start_target_transition()

    def _on_history_pause_changed(self, paused: bool) -> None:
        self._history_berger_paused = paused
        if (
            not self._shutting_down
            and paused
            and self._transition is _Transition.PAUSING_BERGER
        ):
            if self._target is _Target.HISTORY_LOAD:
                self._grant_history_access()
            else:
                self._start_berger_restore()

    def _grant_history_access(self) -> None:
        if self._shutting_down or self._transition is not _Transition.PAUSING_BERGER:
            return
        if not self._history_scan_paused or not self._history_berger_paused:
            return
        if self._target is not _Target.HISTORY_LOAD:
            self._start_berger_restore()
            return
        self._transition = _Transition.HISTORY_RUNNING
        self._active_history_serial = self._history_request_serial
        LOG.info("Exklusiver Victron-History-GATT-Zugriff freigegeben")
        self.history_access_granted.emit()

    def shutdown(self) -> None:
        """Prevent any resume once application shutdown has begun."""
        self._shutting_down = True
        self._target = _Target.STATUS
        self._transition = _Transition.IDLE
        self._berger_restore_waiting = False
        self._set_mode(BleMode.SHUTDOWN)
        self._pause_pending = False
        self._resume_pending = False

    @property
    def measurement_window_active(self) -> bool:
        return self._window_open

    def _on_scan_mode_changed(self, mode: str) -> None:
        self._scan_mode = mode
        if self._shutting_down:
            return
        if (
            mode == "active"
            and self._window_open
            and not self._pause_pending
            and not self._active_window
        ):
            self._pause_pending = True
            self._victron.request_scan_pause()
            return
        if self._pause_pending and mode != "active":
            self._pause_pending = False
            self._active_window = False
            if self._berger is not None:
                self._berger.grant_measurement_permission()
        if self._resume_pending and mode == "stopped":
            self._resume_pending = False
            self._active_window = False
            self._window_open = False
            if self._berger is not None:
                self._berger.confirm_measurement_window_finished()

    def _on_measurement_window_requested(self) -> None:
        if self._shutting_down or self._berger is None:
            return
        self._window_open = True
        if self._scan_mode != "active":
            self._active_window = False
            self._berger.grant_measurement_permission()
            return
        if self._pause_pending or self._active_window:
            return
        self._pause_pending = True
        LOG.info("Berger-Messfenster angefordert; aktiver Scan muss pausieren")
        self._victron.request_scan_pause()

    def _on_scan_paused(self) -> None:
        if self._shutting_down or self._berger is None or not self._pause_pending:
            return
        self._pause_pending = False
        self._active_window = True
        self._berger.grant_measurement_permission()

    def _on_measurement_window_finished(self) -> None:
        if self._shutting_down or self._berger is None:
            return
        if self._mode is BleMode.HISTORY_LOADING:
            self._pause_pending = False
            self._resume_pending = False
            self._active_window = False
            self._window_open = False
            self._berger.confirm_measurement_window_finished()
            return
        if self._pause_pending:
            # Berger timed out while Victron was still stopping. Cancel the
            # pending pause and release Berger without leaving scanning paused.
            self._pause_pending = False
            self._window_open = False
            self._victron.request_scan_resume()
            self._berger.confirm_measurement_window_finished()
            return
        if not self._active_window:
            self._window_open = False
            self._berger.confirm_measurement_window_finished()
            return
        if self._resume_pending:
            return
        self._resume_pending = True
        self._victron.request_scan_resume()

    def _on_scan_resumed(self) -> None:
        if self._shutting_down or self._berger is None or not self._resume_pending:
            return
        self._resume_pending = False
        self._active_window = False
        self._window_open = False
        self._berger.confirm_measurement_window_finished()
