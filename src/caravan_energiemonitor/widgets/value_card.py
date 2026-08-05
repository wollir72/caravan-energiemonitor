from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout


class ValueCard(QFrame):
    def __init__(
        self,
        title: str,
        value: str = "—",
        parent=None,
        *,
        compact: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )

        layout = QVBoxLayout(self)
        if compact:
            layout.setContentsMargins(8, 4, 8, 5)
        else:
            layout.setContentsMargins(10, 5, 10, 6)
        layout.setSpacing(0)

        title_label = QLabel(title)
        title_font = title_label.font()
        title_font.setPointSize(max(8, title_font.pointSize() - 1))
        title_label.setFont(title_font)
        title_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        title_label.setProperty("secondary", True)
        title_label.setToolTip(title)

        self.value_label = QLabel(value)
        font = self.value_label.font()
        font.setPointSize(
            max(font.pointSize() + (2 if compact else 3), 11 if compact else 12)
        )
        font.setBold(True)
        self.value_label.setFont(font)
        self.value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        layout.addWidget(title_label)
        layout.addWidget(self.value_label)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)
