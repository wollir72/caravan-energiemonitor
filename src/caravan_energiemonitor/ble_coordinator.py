"""Qt-side arbitration between Berger GATT traffic and Victron active scanning."""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QObject

LOG = logging.getLogger(__name__)


class BleCoordinator(QObject):
    """Grant Berger measurement windows only after active scanning has stopped."""

    def __init__(self, victron_worker: Any, berger_worker: Any, parent=None) -> None:
        super().__init__(parent)
        self._victron = victron_worker
        self._berger = berger_worker
        self._scan_mode = "stopped"
        self._pause_pending = False
        self._active_window = False
        self._resume_pending = False
        self._window_open = False
        self._shutting_down = False

        victron_worker.scan_mode_changed.connect(self._on_scan_mode_changed)
        victron_worker.scan_paused.connect(self._on_scan_paused)
        victron_worker.scan_resumed.connect(self._on_scan_resumed)
        berger_worker.measurement_window_requested.connect(
            self._on_measurement_window_requested
        )
        berger_worker.measurement_window_finished.connect(
            self._on_measurement_window_finished
        )

    def shutdown(self) -> None:
        """Prevent any resume once application shutdown has begun."""
        self._shutting_down = True
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
            self._berger.grant_measurement_permission()
        if self._resume_pending and mode == "stopped":
            self._resume_pending = False
            self._active_window = False
            self._window_open = False
            self._berger.confirm_measurement_window_finished()

    def _on_measurement_window_requested(self) -> None:
        if self._shutting_down:
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
        if self._shutting_down or not self._pause_pending:
            return
        self._pause_pending = False
        self._active_window = True
        self._berger.grant_measurement_permission()

    def _on_measurement_window_finished(self) -> None:
        if self._shutting_down:
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
        if self._shutting_down or not self._resume_pending:
            return
        self._resume_pending = False
        self._active_window = False
        self._window_open = False
        self._berger.confirm_measurement_window_finished()
