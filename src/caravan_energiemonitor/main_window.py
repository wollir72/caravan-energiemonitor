from __future__ import annotations

import logging
import time
from datetime import datetime

from PySide6.QtCore import QThread, QTimer, Qt
from PySide6.QtGui import QCloseEvent, QColor, QPalette, QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .config import AppConfig
from . import __version__
from .ble_coordinator import BleCoordinator, BleMode
from caravan_energiemonitor.devices.berger.worker import BergerWorker
from caravan_energiemonitor.devices.victron.worker import VictronWorker
from caravan_energiemonitor.devices.victron.history_worker import VictronHistoryWorker
from .models import (
    BatterySnapshot,
    SolarSnapshot,
    StatusTracker,
    VictronHistoryDay,
    VictronHistorySummary,
    format_capacity,
    format_cell_delta,
    format_cell_voltage,
    format_current,
    format_enum,
    format_installed_power,
    format_percent,
    format_power,
    format_signed_current,
    format_temperature,
    format_timestamp,
    format_voltage,
    format_yield,
)
from .widgets.history_view import VictronHistoryView
from .widgets.power_gauge import PowerGauge
from .widgets.value_card import ValueCard

LOG = logging.getLogger(__name__)
INITIAL_BERGER_SNAPSHOT_TIMEOUT_MS = 30_000


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self._victron_tracker = StatusTracker(config.display.stale_after_seconds)
        self._berger_tracker = StatusTracker(config.display.stale_after_seconds)
        self._last_victron_heartbeat: float | None = None
        self._last_berger_heartbeat: float | None = None
        self._berger_connected = False
        self._closing = False
        self._victron_started = False
        self._victron_start_deferred = False
        self._ble_coordinator: BleCoordinator | None = None
        self._history_worker: VictronHistoryWorker | None = None
        self._history_result_received = False
        self.history_cache: tuple[
            VictronHistorySummary, tuple[VictronHistoryDay, ...], datetime
        ] | None = None
        self._cards: dict[str, ValueCard] = {}
        self._build_ui()

        self._victron_start_timer = QTimer(self)
        self._victron_start_timer.setSingleShot(True)
        self._victron_start_timer.setInterval(INITIAL_BERGER_SNAPSHOT_TIMEOUT_MS)
        self._victron_start_timer.timeout.connect(
            self._start_victron_after_initial_timeout
        )

        self.worker = VictronWorker(config.victron, self)
        self.worker.snapshot_received.connect(self._on_victron_snapshot)
        self.worker.target_activity.connect(self._on_victron_activity)
        self.worker.scanner_heartbeat.connect(self._on_victron_heartbeat)
        self.worker.bluetooth_error.connect(self._on_victron_error)

        self.berger_worker: BergerWorker | None = None
        if config.berger.enabled:
            self.berger_worker = BergerWorker(config.berger, self)
            self.berger_worker.snapshot_received.connect(self._on_berger_snapshot)
            self.berger_worker.target_activity.connect(self._on_berger_activity)
            self.berger_worker.worker_heartbeat.connect(self._on_berger_heartbeat)
            self.berger_worker.bluetooth_error.connect(self._on_berger_error)
            self.berger_worker.connection_state_changed.connect(
                self._on_berger_connection_state
            )
            self.berger_worker.measurement_window_finished.connect(
                self._on_initial_measurement_window_finished
            )

        self._ble_coordinator = BleCoordinator(self.worker, self.berger_worker, self)
        self._ble_coordinator.history_access_granted.connect(
            self._on_history_access_granted
        )
        self._ble_coordinator.mode_changed.connect(self._on_ble_mode_changed)
        if self.berger_worker is not None:
            self._last_berger_heartbeat = time.monotonic()
            LOG.info("Starte Berger vor dem Victron-Scanner und warte auf Snapshot")
            self._victron_start_timer.start()
            self.berger_worker.start()
        else:
            self._start_victron_worker()

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(500)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start()
        self._refresh_status()

    def _start_victron_worker(self) -> None:
        """Open the startup gate after Berger's first complete snapshot."""
        if self._closing or self._victron_started:
            return
        if self.victron_tabs.currentIndex() != 0:
            self._victron_start_deferred = True
            return
        self._victron_start_timer.stop()
        self._victron_start_deferred = False
        self._victron_started = True
        self._last_victron_heartbeat = time.monotonic()
        LOG.info("Berger-Startversuch beendet; starte Victron-Scanner")
        self.worker.start()

    def _start_victron_after_initial_timeout(self) -> None:
        if self._closing or self._victron_started:
            return
        if (
            self._ble_coordinator is not None
            and self._ble_coordinator.measurement_window_active
        ):
            self._victron_start_deferred = True
            LOG.info("Victron-Start bis zum Ende des Berger-Messfensters verschoben")
            return
        LOG.warning(
            "Kein erster Berger-Snapshot innerhalb von %.1f s; starte Victron trotzdem",
            INITIAL_BERGER_SNAPSHOT_TIMEOUT_MS / 1000,
        )
        self._start_victron_worker()

    def _on_initial_measurement_window_finished(self) -> None:
        if not self._victron_start_deferred:
            return
        self._victron_start_deferred = False
        self._start_victron_after_initial_timeout()

    def _build_ui(self) -> None:
        self.setWindowTitle("Caravan-Energiemonitor")
        self.resize(1600, 950)
        self.setMinimumSize(1280, 800)

        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 14, 18, 16)
        outer.setSpacing(10)

        title = QLabel("Caravan-Energiemonitor")
        title_font = title.font()
        title_font.setPointSize(max(19, title_font.pointSize() + 8))
        title_font.setBold(True)
        title.setFont(title_font)
        outer.addWidget(title)

        self.victron_status_label = QLabel()
        self.berger_status_label = QLabel()
        for label in (self.victron_status_label, self.berger_status_label):
            label.setFrameShape(QFrame.Shape.StyledPanel)
            label.setMargin(5)
        status_font = self.victron_status_label.font()
        status_font.setBold(True)
        self.victron_status_label.setFont(status_font)
        self.berger_status_label.setFont(status_font)

        self.gauge = PowerGauge(
            self.config.display.maximum_solar_power,
            self.config.solar.installed_power_watts,
        )
        columns = QHBoxLayout()
        columns.setSpacing(14)
        columns.addWidget(self._victron_column(), 9)
        columns.addWidget(self._berger_column(), 11)
        outer.addLayout(columns, 1)
        self.version_label = QLabel(f"v{__version__}", root)
        version_font = self.version_label.font()
        version_font.setPointSize(max(7, version_font.pointSize() - 1))
        self.version_label.setFont(version_font)
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.version_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self.version_label.adjustSize()
        self._position_version_label(root.size().width(), root.size().height())
        self.version_label.raise_()
        self.setCentralWidget(root)

    def _position_version_label(self, width: int, height: int) -> None:
        """Place the version inside the existing lower/right content margins."""
        self.version_label.move(
            max(0, width - 18 - self.version_label.width()),
            max(0, height - 2 - self.version_label.height()),
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "version_label"):
            size = event.size()
            self._position_version_label(size.width(), size.height())

    def _device_header(self, name: str, status: QLabel) -> QWidget:
        header = QWidget()
        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        device_name = QLabel(name)
        device_name.setWordWrap(True)
        device_name.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        name_font = device_name.font()
        name_font.setBold(True)
        device_name.setFont(name_font)

        layout.addWidget(device_name, 1)
        layout.addWidget(status)
        return header

    def _victron_column(self) -> QGroupBox:
        column = QGroupBox("Solarladeregler")
        layout = QVBoxLayout(column)
        layout.setContentsMargins(10, 14, 10, 10)
        layout.setSpacing(8)
        self.victron_tabs = QTabWidget()
        self.victron_tabs.setObjectName("victronTabs")
        self.victron_tabs.setStyleSheet(
            "QTabWidget#victronTabs::pane { background-color: palette(window); }"
        )
        status_page = QWidget()
        status_page.setObjectName("victronStatusPage")
        status_layout = QVBoxLayout(status_page)
        status_layout.setContentsMargins(2, 2, 2, 2)
        status_layout.setSpacing(8)
        status_layout.addWidget(
            self._device_header(self.config.victron.name, self.victron_status_label)
        )
        status_layout.addWidget(self.gauge, 1)
        status_layout.addWidget(self._solar_group())
        status_layout.addWidget(self._victron_battery_group())
        status_layout.addWidget(self._victron_device_group())
        self.history_view = VictronHistoryView()
        self.history_view.setObjectName("victronHistoryPage")
        self.history_view.refresh_requested.connect(self._refresh_history)
        self.victron_tabs.addTab(status_page, "Status")
        self.victron_tabs.addTab(self.history_view, "Verlauf")
        background = column.palette().color(QPalette.ColorRole.Window)
        self._set_victron_background(self.victron_tabs, background)
        self._set_victron_background(status_page, background)
        self.history_view.set_content_background(background)
        tab_stack = self.victron_tabs.findChild(QStackedWidget)
        if tab_stack is not None:
            self._set_victron_background(tab_stack, background)
        self.victron_tabs.setCurrentIndex(0)
        self.victron_tabs.currentChanged.connect(self._on_victron_tab_changed)
        layout.addWidget(self.victron_tabs, 1)
        return column

    @staticmethod
    def _set_victron_background(widget: QWidget, color: QColor) -> None:
        """Use the app's window color only inside the Victron tab container."""
        palette = widget.palette()
        palette.setColor(QPalette.ColorRole.Window, color)
        widget.setPalette(palette)
        widget.setAutoFillBackground(True)

    def _on_victron_tab_changed(self, index: int) -> None:
        if self._closing or self._ble_coordinator is None:
            return
        if index == 0:
            if self._history_worker is not None and self._history_worker.isRunning():
                self._history_worker.request_cancel()
            self._ble_coordinator.request_status()
            return
        if self.history_cache is not None:
            summary, days, updated_at = self.history_cache
            self.history_view.show_history(summary, days, updated_at)
            self._ble_coordinator.show_cached_history()
            return
        self._begin_history_load()

    def _refresh_history(self) -> None:
        if self._history_worker is not None and self._history_worker.isRunning():
            return
        self._begin_history_load()

    def _begin_history_load(self) -> None:
        if self._ble_coordinator is None:
            self.history_view.show_error("Berger/BLE-Koordination ist nicht verfügbar.")
            return
        self.history_view.set_loading()
        if not self._ble_coordinator.begin_history():
            return

    def _on_history_access_granted(self) -> None:
        if self._closing:
            return
        if self.victron_tabs.currentIndex() == 0:
            assert self._ble_coordinator is not None
            self._ble_coordinator.finish_history(False)
            return
        if self._history_worker is not None and self._history_worker.isRunning():
            return
        worker = VictronHistoryWorker(self.config.victron, self)
        self._history_worker = worker
        self._history_result_received = False
        worker.progress_changed.connect(self.history_view.set_progress)
        worker.history_received.connect(self._on_history_received)
        worker.error_occurred.connect(self._on_history_error)
        worker.cancelled.connect(self._on_history_cancelled)
        worker.finished.connect(self._on_history_worker_finished)
        worker.start()

    def _on_history_received(
        self,
        summary: VictronHistorySummary,
        days: tuple[VictronHistoryDay, ...],
    ) -> None:
        updated_at = datetime.now()
        self.history_cache = (summary, days, updated_at)
        self._history_result_received = True
        if self.victron_tabs.currentIndex() == 1:
            self.history_view.show_history(summary, days, updated_at)
            self.history_view.refresh_button.setEnabled(False)

    def _on_history_error(self, message: str) -> None:
        self._history_result_received = False
        if self.victron_tabs.currentIndex() == 1:
            self.history_view.show_error(message)
            self.history_view.refresh_button.setEnabled(False)

    def _on_history_cancelled(self) -> None:
        self._history_result_received = False

    def _on_history_worker_finished(self) -> None:
        worker = self._history_worker
        self._history_worker = None
        if self._ble_coordinator is not None:
            self._ble_coordinator.finish_history(self._history_result_received)
        if self.victron_tabs.currentIndex() == 1:
            self.history_view.refresh_button.setEnabled(True)
        if worker is not None:
            worker.deleteLater()

    def _on_ble_mode_changed(self, mode: BleMode) -> None:
        if mode is BleMode.STATUS and self._victron_start_deferred:
            self._start_victron_worker()

    def _berger_column(self) -> QGroupBox:
        column = QGroupBox("Batterie")
        layout = QVBoxLayout(column)
        layout.setContentsMargins(10, 14, 10, 10)
        layout.setSpacing(8)
        layout.addWidget(
            self._device_header(
                self.config.berger.name, self.berger_status_label
            )
        )

        layout.addWidget(self._berger_battery_group())
        layout.addWidget(self._berger_bms_group())
        layout.addWidget(self._cells_group())
        layout.addWidget(self._berger_device_group())
        layout.addStretch()
        return column

    def _group(
        self,
        title: str,
        entries: list[tuple[str, str, str]],
        columns: int = 2,
        *,
        compact: bool = False,
    ) -> QGroupBox:
        box = QGroupBox(title)
        layout = QGridLayout(box)
        layout.setContentsMargins(8, 12, 8, 8)
        layout.setHorizontalSpacing(7)
        layout.setVerticalSpacing(7)
        for index, (key, label, initial) in enumerate(entries):
            card = self._make_card(key, label, initial, compact=compact)
            layout.addWidget(card, index // columns, index % columns)
        for column in range(columns):
            layout.setColumnStretch(column, 1)
        return box

    def _make_card(
        self,
        key: str,
        label: str,
        initial: str,
        *,
        compact: bool = False,
    ) -> ValueCard:
        if key in self._cards:
            raise ValueError(f"Doppelter Karten-Schlüssel: {key}")
        card = ValueCard(label, initial, compact=compact)
        self._cards[key] = card
        return card

    def _solar_group(self) -> QGroupBox:
        solar = self.config.solar
        box = QGroupBox("Solar")
        layout = QGridLayout(box)
        layout.setContentsMargins(8, 12, 8, 8)
        layout.setHorizontalSpacing(7)
        layout.setVerticalSpacing(7)
        layout.addWidget(
            self._make_card("solar_power", "Solarleistung", "—"), 0, 0, 1, 3
        )
        layout.addWidget(
            self._make_card("yield_today", "Tagesertrag", "—"), 0, 3, 1, 3
        )
        layout.addWidget(
            self._make_card(
                "installed",
                "Installierte PV-Leistung",
                format_installed_power(solar.installed_power_watts),
                compact=True,
            ),
            1,
            0,
            1,
            2,
        )
        layout.addWidget(
            self._make_card("panels", "Module", f"{solar.panel_count}", compact=True),
            1,
            2,
            1,
            2,
        )
        layout.addWidget(
            self._make_card(
                "panel_power",
                "Leistung je Modul",
                f"{solar.panel_power_watts} W",
                compact=True,
            ),
            1,
            4,
            1,
            2,
        )
        for column in range(6):
            layout.setColumnStretch(column, 1)
        return box

    def _victron_battery_group(self) -> QGroupBox:
        return self._group(
            "Batterie laut Regler",
            [
                ("victron_voltage", "Batteriespannung", "—"),
                ("victron_current", "Victron-Ladestrom", "—"),
                ("victron_load", "Externe Last", "—"),
                ("victron_phase", "Ladephase", "—"),
            ],
        )

    def _berger_battery_group(self) -> QGroupBox:
        return self._group(
            "Batteriestatus",
            [
                ("soc", "Ladezustand", "—"),
                ("berger_voltage", "Batteriespannung", "—"),
                ("berger_current", "Gesamtstrom (+ Laden / − Entladen)", "—"),
                ("berger_power", "Batterieleistung", "—"),
                ("remaining", "Restkapazität", "—"),
                ("nominal", "Nennkapazität", format_capacity(self.config.berger.capacity_ah)),
            ],
            columns=3,
        )

    def _berger_bms_group(self) -> QGroupBox:
        box = QGroupBox("BMS")
        layout = QGridLayout(box)
        layout.setContentsMargins(8, 12, 8, 8)
        layout.setHorizontalSpacing(7)
        layout.setVerticalSpacing(7)
        first_row = [
            ("temperature", "BMS-Temperatur", "—"),
            ("cycles", "Ladezyklen", "—"),
            ("charge_mosfet", "Lade-MOSFET", "—"),
        ]
        for column, (key, label, initial) in enumerate(first_row):
            layout.addWidget(
                self._make_card(key, label, initial, compact=True), 0, column
            )
            layout.setColumnStretch(column, 1)
        layout.addWidget(
            self._make_card(
                "discharge_mosfet", "Entlade-MOSFET", "—", compact=True
            ),
            1,
            0,
        )
        layout.addWidget(
            self._make_card("protection", "Schutzstatus", "—", compact=True),
            1,
            1,
            1,
            2,
        )
        return box

    def _cells_group(self) -> QGroupBox:
        box = QGroupBox("Zellspannungen")
        layout = QGridLayout(box)
        layout.setContentsMargins(8, 12, 8, 8)
        layout.setHorizontalSpacing(7)
        layout.setVerticalSpacing(7)
        for index in range(4):
            layout.addWidget(
                self._make_card(
                    f"cell_{index + 1}", f"Zelle {index + 1}", "—", compact=True
                ),
                0,
                index * 3,
                1,
                3,
            )
        for index, (key, label) in enumerate(
            (
                ("cell_min", "Minimum"),
                ("cell_max", "Maximum"),
                ("cell_delta", "Differenz"),
            )
        ):
            layout.addWidget(
                self._make_card(key, label, "—", compact=True),
                1,
                index * 4,
                1,
                4,
            )
        for column in range(12):
            layout.setColumnStretch(column, 1)
        return box

    def _victron_device_group(self) -> QGroupBox:
        return self._group(
            "Gerät",
            [
                ("model", "Victron-Modell", "—"),
                ("victron_rssi", "Victron-RSSI", "—"),
                ("victron_error", "Victron-Fehlerstatus", "—"),
                ("victron_timestamp", "Letzter Victron-Datensatz", "—"),
            ],
        )

    def _berger_device_group(self) -> QGroupBox:
        return self._group(
            "Gerät",
            [
                ("berger_rssi", "Berger-RSSI", "—"),
                ("berger_timestamp", "Letzter Berger-Datensatz", "—"),
            ],
        )

    def _on_victron_snapshot(self, snapshot: SolarSnapshot) -> None:
        self._victron_tracker.record_snapshot()
        self.gauge.set_value(snapshot.solar_power)
        self._cards["solar_power"].set_value(format_power(snapshot.solar_power))
        self._cards["yield_today"].set_value(format_yield(snapshot.yield_today))
        self._cards["victron_voltage"].set_value(format_voltage(snapshot.battery_voltage))
        self._cards["victron_current"].set_value(format_current(snapshot.battery_charging_current))
        self._cards["victron_load"].set_value(format_current(snapshot.external_device_load))
        self._cards["victron_phase"].set_value(format_enum(snapshot.charge_state))
        self._cards["model"].set_value(snapshot.model_name or "—")
        self._cards["victron_rssi"].set_value("—" if snapshot.rssi is None else f"{snapshot.rssi} dBm")
        self._cards["victron_error"].set_value(format_enum(snapshot.charger_error))
        self._cards["victron_timestamp"].set_value(format_timestamp(snapshot.timestamp))
        self._refresh_status()

    def _on_berger_snapshot(self, snapshot: BatterySnapshot) -> None:
        self._berger_tracker.record_snapshot()
        self._cards["soc"].set_value(format_percent(snapshot.state_of_charge_percent))
        self._cards["berger_voltage"].set_value(format_voltage(snapshot.voltage))
        self._cards["berger_current"].set_value(format_signed_current(snapshot.current))
        power = (
            snapshot.voltage * snapshot.current
            if snapshot.voltage is not None and snapshot.current is not None
            else None
        )
        self._cards["berger_power"].set_value(format_power(power))
        self._cards["remaining"].set_value(format_capacity(snapshot.remaining_capacity_ah))
        self._cards["nominal"].set_value(format_capacity(snapshot.nominal_capacity_ah))
        temperature = snapshot.temperatures_celsius[0] if snapshot.temperatures_celsius else None
        self._cards["temperature"].set_value(format_temperature(temperature))
        self._cards["cycles"].set_value("—" if snapshot.cycle_count is None else str(snapshot.cycle_count))
        self._cards["charge_mosfet"].set_value(self._format_switch(snapshot.charge_mosfet_enabled))
        self._cards["discharge_mosfet"].set_value(self._format_switch(snapshot.discharge_mosfet_enabled))
        protection = snapshot.protection_status
        self._cards["protection"].set_value(
            "—" if protection is None else ("Kein Fehler" if protection == 0 else f"0x{protection:04X}")
        )
        for index in range(4):
            value = snapshot.cell_voltages[index] if index < len(snapshot.cell_voltages) else None
            self._cards[f"cell_{index + 1}"].set_value(format_cell_voltage(value))
        self._cards["cell_min"].set_value(format_cell_voltage(snapshot.minimum_cell_voltage))
        self._cards["cell_max"].set_value(format_cell_voltage(snapshot.maximum_cell_voltage))
        self._cards["cell_delta"].set_value(format_cell_delta(snapshot.cell_delta))
        self._cards["berger_rssi"].set_value("—" if snapshot.rssi is None else f"{snapshot.rssi} dBm")
        self._cards["berger_timestamp"].set_value(format_timestamp(snapshot.timestamp))
        self._start_victron_worker()
        self._refresh_status()

    @staticmethod
    def _format_switch(value: bool | None) -> str:
        return "—" if value is None else ("An" if value else "Aus")

    def _on_victron_activity(self) -> None:
        self._victron_tracker.record_activity()
        self._refresh_status()

    def _on_berger_activity(self) -> None:
        self._berger_tracker.record_activity()
        self._refresh_status()

    def _on_victron_heartbeat(self) -> None:
        self._last_victron_heartbeat = time.monotonic()

    def _on_berger_heartbeat(self) -> None:
        self._last_berger_heartbeat = time.monotonic()

    def _on_victron_error(self, message: str) -> None:
        self._victron_tracker.record_error(message)
        self._refresh_status()

    def _on_berger_error(self, message: str) -> None:
        self._berger_tracker.record_error(message)
        self._refresh_status()

    def _on_berger_connection_state(self, connected: bool) -> None:
        self._berger_connected = connected
        self._refresh_status()

    def _refresh_status(self) -> None:
        # BLE scan/connect operations have their own bounded timeouts. A heartbeat
        # threshold below those bounds would incorrectly turn "wartend" into error.
        heartbeat_limit = max(30.0, self.config.display.stale_after_seconds)
        now = time.monotonic()
        if (
            self._last_victron_heartbeat is not None
            and now - self._last_victron_heartbeat > heartbeat_limit
        ):
            self._victron_tracker.record_error("Der Victron-Scanner antwortet nicht mehr.")
        victron_status = self._victron_tracker.status()
        self.victron_status_label.setText(f"Victron  ●  {victron_status.value}")
        self.victron_status_label.setToolTip(self._victron_tracker.error_message or victron_status.value)

        if not self.config.berger.enabled:
            self.berger_status_label.setText("Berger  ○  Deaktiviert")
            self.berger_status_label.setToolTip("berger.enabled = false")
            return
        if (
            self._last_berger_heartbeat is not None
            and now - self._last_berger_heartbeat > heartbeat_limit
        ):
            self._berger_tracker.record_error("Der Berger-Worker antwortet nicht mehr.")
        berger_status = self._berger_tracker.status()
        connection = "verbunden" if self._berger_connected else "nicht verbunden"
        self.berger_status_label.setText(f"Berger  ●  {berger_status.value}")
        self.berger_status_label.setToolTip(
            self._berger_tracker.error_message or f"{berger_status.value}; {connection}"
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closing:
            event.accept()
            return
        self._closing = True
        self._status_timer.stop()
        self._victron_start_timer.stop()
        if self._ble_coordinator is not None:
            self._ble_coordinator.shutdown()
        workers: list[QThread] = [self.worker]
        if self.berger_worker is not None:
            workers.append(self.berger_worker)
        if self._history_worker is not None:
            workers.append(self._history_worker)

        # Stop both first; never let waiting for one delay the other's request.
        for worker in workers:
            worker.request_stop()
        for worker in workers:
            if worker.wait(3000):
                continue
            LOG.error("Bluetooth-Worker %s reagiert nicht auf Stop", type(worker).__name__)
            if worker.wait(1000):
                continue
            # Last-resort only: without this safeguard Qt may abort while destroying
            # a still-running QThread. Normal BLE operations and cleanup are bounded,
            # so this path indicates a native/BlueZ call that ignored cancellation.
            LOG.critical(
                "Erzwinge als letzten Notfall das Ende von %s", type(worker).__name__
            )
            worker.terminate()
            worker.wait(1000)
        event.accept()
