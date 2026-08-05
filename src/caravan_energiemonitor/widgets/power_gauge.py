from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget


def clamp_gauge_value(value: float | None, maximum: float) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return min(max(number, 0.0), maximum)


def reference_mark_ratio(installed_power: float, maximum: float) -> float:
    return min(max(float(installed_power) / float(maximum), 0.0), 1.0)


class PowerGauge(QWidget):
    def __init__(self, maximum: float, installed_power: float, parent=None) -> None:
        super().__init__(parent)
        self.maximum = float(maximum)
        self.installed_power = float(installed_power)
        self.value: float | None = None
        self.setMinimumSize(320, 175)
        self.setMaximumHeight(290)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

    def sizeHint(self) -> QSize:
        return QSize(560, 240)

    def minimumSizeHint(self) -> QSize:
        return QSize(320, 175)

    def set_value(self, value: float | None) -> None:
        self.value = value
        self.update()

    def bounded_value(self) -> float | None:
        return clamp_gauge_value(self.value, self.maximum)

    def reference_ratio(self) -> float:
        return reference_mark_ratio(self.installed_power, self.maximum)

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()

        margin = 22.0
        top_inset = 8.0
        arc_center_y = self.height() - 34.0

        # QPainter centers the pen on the ellipse. Reserve half of the stroke
        # inside every widget edge so the antialiased arc cannot be clipped.
        available_radius = max(
            0.0,
            min(
                self.width() / 2 - margin,
                arc_center_y - top_inset,
            ),
        )
        width = max(10.0, available_radius * 2 * 0.055)
        radius = max(
            0.0,
            min(
                self.width() / 2 - margin - width / 2,
                arc_center_y - top_inset - width / 2,
            ),
        )
        diameter = radius * 2
        rect = QRectF(
            (self.width() - diameter) / 2,
            arc_center_y - radius,
            diameter,
            diameter,
        )
        painter.setPen(
            QPen(
                palette.mid().color(),
                width,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawArc(rect, 0, 180 * 16)

        bounded = self.bounded_value()
        if bounded is not None and bounded > 0:
            painter.setPen(
                QPen(
                    palette.highlight().color(),
                    width,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                )
            )
            painter.drawArc(
                rect,
                (180 - 180 * bounded / self.maximum) * 16,
                (180 * bounded / self.maximum) * 16,
            )

        ratio = self.reference_ratio()
        angle = math.radians(180 - 180 * ratio)
        center = rect.center()
        radius = rect.width() / 2
        inner = radius - width * 1.25
        outer = radius + width * 0.25
        p1 = QPointF(
            center.x() + inner * math.cos(angle),
            center.y() - inner * math.sin(angle),
        )
        p2 = QPointF(
            center.x() + outer * math.cos(angle),
            center.y() - outer * math.sin(angle),
        )
        painter.setPen(QPen(palette.text().color(), 2))
        painter.drawLine(p1, p2)

        value_text = "-- W" if self.value is None else f"{self.value:.0f} W"
        font = QFont(self.font())
        font.setBold(True)
        font.setPointSize(max(18, int(self.height() * 0.115)))
        painter.setFont(font)
        painter.setPen(palette.text().color())
        painter.drawText(
            QRectF(0, self.height() * 0.44, self.width(), 50),
            Qt.AlignmentFlag.AlignCenter,
            value_text,
        )

        small = QFont(self.font())
        small.setPointSize(max(8, small.pointSize()))
        painter.setFont(small)
        painter.drawText(
            QRectF(margin, self.height() - 28, 80, 22),
            Qt.AlignmentFlag.AlignLeft,
            "0 W",
        )
        painter.drawText(
            QRectF(self.width() - margin - 100, self.height() - 28, 100, 22),
            Qt.AlignmentFlag.AlignRight,
            f"{self.maximum:.0f} W",
        )
        painter.drawText(
            QRectF(0, self.height() - 27, self.width(), 22),
            Qt.AlignmentFlag.AlignCenter,
            f"Referenz {self.installed_power:.0f} Wp",
        )
