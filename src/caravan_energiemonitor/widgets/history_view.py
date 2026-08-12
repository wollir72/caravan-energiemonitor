"""Compact presentation widget for cached Victron daily history."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models import VictronHistoryDay, VictronHistorySummary


def _day_label(index: int) -> str:
    if index == 0:
        return "Heute"
    if index == 1:
        return "Gestern"
    return f"Vor {index} Tagen"


def _duration(minutes: int) -> str:
    hours, remainder = divmod(minutes, 60)
    return f"{hours} h {remainder:02d} min" if hours else f"{remainder} min"


class VictronHistoryView(QWidget):
    refresh_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Verlauf")
        title_font = title.font()
        title_font.setBold(True)
        title_font.setPointSize(title_font.pointSize() + 2)
        title.setFont(title_font)
        self.updated_label = QLabel("Noch nicht geladen")
        self.updated_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.refresh_button = QPushButton("Aktualisieren")
        self.refresh_button.clicked.connect(self.refresh_requested)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.updated_label)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        self.status_label = QLabel("Beim ersten Öffnen wird der Verlauf geladen.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        summary_box = QGroupBox("Zusammenfassung")
        summary_layout = QGridLayout(summary_box)
        self.summary_values: dict[str, QLabel] = {}
        for column, (key, caption) in enumerate(
            (
                ("days", "Verfügbare Tage"),
                ("reset", "Ertrag seit Reset"),
                ("lifetime", "Gesamtertrag"),
                ("pv", "Max. PV-Spannung"),
            )
        ):
            caption_label = QLabel(caption)
            value_label = QLabel("—")
            value_font = value_label.font()
            value_font.setBold(True)
            value_label.setFont(value_font)
            summary_layout.addWidget(caption_label, 0, column)
            summary_layout.addWidget(value_label, 1, column)
            summary_layout.setColumnStretch(column, 1)
            self.summary_values[key] = value_label
        layout.addWidget(summary_box)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ("Tag", "Ertrag", "Max. Leistung", "Max. PV", "Batt. min", "Batt. max")
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_details)
        layout.addWidget(self.table, 1)

        self.details = QLabel("Tag auswählen, um weitere Werte anzuzeigen.")
        self.details.setWordWrap(True)
        self.details.setFrameStyle(QFrame.Shape.StyledPanel)
        self.details.setMargin(6)
        layout.addWidget(self.details)
        self._days: tuple[VictronHistoryDay, ...] = ()

    def set_content_background(self, color: QColor) -> None:
        """Apply the surrounding window color to this view and its scroll viewport."""
        window_palette = self.palette()
        window_palette.setColor(QPalette.ColorRole.Window, color)
        self.setPalette(window_palette)
        self.setAutoFillBackground(True)

        table_palette = self.table.palette()
        table_palette.setColor(QPalette.ColorRole.Window, color)
        table_palette.setColor(QPalette.ColorRole.Base, color)
        table_palette.setColor(QPalette.ColorRole.AlternateBase, color)
        self.table.setPalette(table_palette)
        self.table.setAutoFillBackground(True)

        viewport = self.table.viewport()
        viewport.setPalette(table_palette)
        viewport.setAutoFillBackground(True)

        detail_palette = self.details.palette()
        detail_palette.setColor(QPalette.ColorRole.Window, color)
        self.details.setPalette(detail_palette)
        self.details.setAutoFillBackground(True)

    def set_loading(self, current: int = 0, total: int | None = None) -> None:
        self.refresh_button.setEnabled(False)
        suffix = "" if total is None else f" {current} / {total}"
        self.status_label.setText(f"Verlauf wird geladen …{suffix}")

    def set_progress(self, current: int, total: int) -> None:
        self.set_loading(current, total)

    def show_error(self, cause: str) -> None:
        self.refresh_button.setEnabled(True)
        self.status_label.setText("Verlauf konnte nicht geladen werden.")
        self.status_label.setToolTip(cause)

    def show_history(
        self,
        summary: VictronHistorySummary,
        days: tuple[VictronHistoryDay, ...],
        updated_at: datetime,
    ) -> None:
        self.refresh_button.setEnabled(True)
        self.updated_label.setText(f"Zuletzt aktualisiert: {updated_at:%H:%M:%S}")
        self.status_label.setText(f"{len(days)} Tagesdatensätze geladen.")
        self.summary_values["days"].setText(str(summary.days_available))
        self.summary_values["reset"].setText(f"{summary.yield_since_reset_kwh:.2f} kWh")
        self.summary_values["lifetime"].setText(f"{summary.lifetime_yield_kwh:.2f} kWh")
        self.summary_values["pv"].setText(f"{summary.max_pv_voltage_v:.2f} V")
        self._days = days
        self.table.setRowCount(len(days))
        for row, day in enumerate(days):
            values = (
                _day_label(day.day_index),
                f"{day.yield_kwh:.2f} kWh",
                f"{day.max_pv_power_w} W",
                f"{day.max_pv_voltage_v:.2f} V",
                f"{day.battery_voltage_min_v:.2f} V",
                f"{day.battery_voltage_max_v:.2f} V",
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        if days:
            self.table.selectRow(0)
        else:
            self.details.setText("Keine Tagesdatensätze verfügbar.")

    def _update_details(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._days):
            return
        day = self._days[row]
        consumption = "—" if day.load_consumption_kwh is None else f"{day.load_consumption_kwh:.2f} kWh"
        errors = ", ".join(f"0x{code:02X}" for code in day.errors if code) or "Keine"
        self.details.setText(
            f"{_day_label(day.day_index)} · Verbrauch: {consumption} · Max. Ladestrom: "
            f"{day.max_battery_current_a:.1f} A · Bulk: {_duration(day.bulk_minutes)} · "
            f"Absorption: {_duration(day.absorption_minutes)} · Float: {_duration(day.float_minutes)} · "
            f"Fehler: {errors}"
        )
