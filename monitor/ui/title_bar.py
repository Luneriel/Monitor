"""自定义标题栏：仅包含最小化 / 最大化 / 关闭（位于右上角）。

配合无边框窗口（``Qt.FramelessWindowHint``）使用，布局为
"左[项目名称 + 功能栏目]、右上[窗口按钮]、右下[功能页面]"：
    * 本控件只占右上角一条横栏，左侧留白可拖动窗口（走系统移动循环，保留 Win 的贴靠）；
    * 双击切换最大化；项目名称与功能栏目在左侧导航栏中。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from .. import config
from ..i18n import tr
from . import theme, vector_icons


class WindowButton(QPushButton):
    """标题栏右侧的窗口按钮。"""

    def __init__(self, icon_name: str, tooltip: str, danger: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("WindowButtonClose" if danger else "WindowButton")
        self.setCursor(QCursor(Qt.ArrowCursor))
        self.setFlat(True)
        self.setFocusPolicy(Qt.NoFocus)
        self._danger = danger
        self._hover = False
        self._icon_name = icon_name
        self.set_icon(icon_name)
        self.setToolTip(tooltip)

    def set_icon(self, icon_name: str) -> None:
        self._icon_name = icon_name
        size = max(13, config.sx(17))
        self.setIconSize(theme.QSize(size, size))
        color = "#FFFFFF" if (self._danger and self._hover) else config.C_WINDOW_BTN_ICON
        self.setIcon(vector_icons.icon(icon_name, size, color, 1.9))

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.set_icon(self._icon_name)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.set_icon(self._icon_name)
        super().leaveEvent(event)


class TitleBar(QWidget):
    """右上角标题栏（仅窗口按钮 + 可拖动区域）。"""

    minimize_requested = Signal()
    maximize_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("TitleBar")
        self.setFixedHeight(config.sx(46))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch(1)

        self.minimize_btn = WindowButton("minimize", tr("window.minimize"), parent=self)
        self.maximize_btn = WindowButton("maximize", tr("window.maximize"), parent=self)
        self.close_btn = WindowButton("close", tr("common.close"), danger=True, parent=self)
        self.minimize_btn.clicked.connect(self.minimize_requested.emit)
        self.maximize_btn.clicked.connect(self.maximize_requested.emit)
        self.close_btn.clicked.connect(self.close_requested.emit)
        for button in (self.minimize_btn, self.maximize_btn, self.close_btn):
            button.setFixedHeight(self.height())
            button.setFixedWidth(config.sx(54))
            layout.addWidget(button)

    # ------------------------------------------------------------------
    def set_maximized(self, maximized: bool) -> None:
        self.maximize_btn.set_icon("restore" if maximized else "maximize")
        self.maximize_btn.setToolTip(tr("window.restore") if maximized else tr("window.maximize"))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.maximize_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)
