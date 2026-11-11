"""弹出层：下拉菜单、程序选择下拉、下拉选择框。"""

from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import config, icon_extract, util
from . import anim, theme, vector_icons
from .controls import IconTile, SlimScrollArea

SHADOW_MARGIN = 14


class HoverRow(QWidget):
    """可点击并带悬停高亮的一行。"""

    clicked = Signal(object)

    def __init__(self, payload=None, parent: Optional[QWidget] = None,
                 margins: tuple[int, int, int, int] = (10, 6, 10, 6)) -> None:
        super().__init__(parent)
        self.payload = payload
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setObjectName("HoverRow")
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(*[config.sx(m) for m in margins])
        self._layout.setSpacing(config.sx(9))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self.payload)
        super().mouseReleaseEvent(event)


class PopupPanel(QWidget):
    """无边框、带阴影与缩放动画的弹出面板。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)
        outer.setSpacing(0)

        self.container = QFrame(self)
        self.container.setAttribute(Qt.WA_StyledBackground, True)
        self.container.setObjectName("PopupContainer")
        outer.addWidget(self.container)

        self.body = QVBoxLayout(self.container)
        self.body.setContentsMargins(config.sx(6), config.sx(6), config.sx(6), config.sx(6))
        self.body.setSpacing(config.sx(2))

    def paintEvent(self, event) -> None:  # noqa: N802
        """容器四周自绘柔和阴影（不用 QGraphicsDropShadowEffect，避免悬浮时文字闪烁）。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        theme.paint_soft_shadow(painter, QRectF(self.container.geometry()), config.sx(12),
                                spread=config.sx(11), dy=config.sx(6), alpha=58)
        painter.end()

    # ------------------------------------------------------------------
    def popup_at(self, anchor: QWidget, align: str = "left", offset: int = 7,
                 max_height: Optional[int] = None, animate: bool = True) -> None:
        self.adjustSize()
        if max_height:
            self.setMaximumHeight(int(max_height) + 2 * SHADOW_MARGIN)
            self.adjustSize()
        hint = self.sizeHint()
        width = hint.width()
        height = hint.height()

        global_pos = anchor.mapToGlobal(QPoint(0, anchor.height() + offset))
        if align == "right":
            global_pos.setX(anchor.mapToGlobal(QPoint(anchor.width(), 0)).x() - width + SHADOW_MARGIN)
        elif align == "center":
            global_pos.setX(anchor.mapToGlobal(QPoint(anchor.width() // 2, 0)).x() - width // 2)

        screen = QGuiApplication.screenAt(global_pos) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = max(area.left(), min(global_pos.x() - SHADOW_MARGIN, area.right() - width))
        y = global_pos.y() - SHADOW_MARGIN
        if y + height > area.bottom():
            y = max(area.top(), anchor.mapToGlobal(QPoint(0, 0)).y() - height - offset)
        self.setGeometry(x, y, width, height)
        self.show()
        self.raise_()
        if animate:
            anim.pop_widget(self.container, scale=0.95, duration=config.ANIM_FAST, window=self)


class MenuItem(HoverRow):
    """下拉菜单中的一行。"""

    def __init__(self, key: str, text: str, parent: Optional[QWidget] = None,
                 icon_name: str = "", checked: bool = False) -> None:
        super().__init__(payload=key, parent=parent, margins=(10, 5, 8, 5))
        self.key = key
        self.setMinimumHeight(config.sx(32))

        if icon_name:
            icon = QLabel()
            icon.setPixmap(vector_icons.pixmap(icon_name, config.sx(15), config.C_TEXT_MUTED))
            self._layout.addWidget(icon)

        self.text_label = QLabel(text)
        self.text_label.setFont(theme.font(config.FONT_BODY))
        self.text_label.setStyleSheet(f"color: {config.C_TEXT_BODY}; background: transparent;")
        self._layout.addWidget(self.text_label, 1)

        self.check_label = QLabel()
        self.check_label.setFixedWidth(config.sx(16))
        self._layout.addWidget(self.check_label)
        self.set_checked(checked)

    def set_checked(self, checked: bool) -> None:
        self._checked = checked
        if checked:
            self.check_label.setPixmap(
                vector_icons.pixmap("check", config.sx(14), config.C_PRIMARY, 2.5))
        else:
            self.check_label.clear()


class MenuPopup(PopupPanel):
    """通用下拉菜单。"""

    item_selected = Signal(str)

    def __init__(self, items: Iterable[tuple[str, str]], current: str = "",
                 parent: Optional[QWidget] = None, min_width: int = 150,
                 max_height: Optional[int] = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(config.sx(min_width))
        self._items: dict[str, MenuItem] = {}
        host: QWidget = self.container
        column: Optional[QVBoxLayout] = None
        if max_height:
            # 选项很多时（例如时段选择器的 00–59）限高 + 滚动，否则菜单会顶出屏幕
            self.scroll = SlimScrollArea(self.container)
            self.scroll.setWidgetResizable(True)
            self.scroll.setMaximumHeight(int(max_height))
            holder = QWidget()
            column = QVBoxLayout(holder)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(config.sx(2))
            self.scroll.setWidget(holder)
            self.body.addWidget(self.scroll)
            host = holder
        for key, text in items:
            item = MenuItem(key, text, host, checked=(key == current))
            item.clicked.connect(self._on_item)
            if column is None:
                self.body.addWidget(item)
            else:
                column.addWidget(item)
            self._items[key] = item

    def _on_item(self, key: str) -> None:
        for k, item in self._items.items():
            item.set_checked(k == key)
        self.item_selected.emit(key)
        self.close()

    def set_current(self, key: str) -> None:
        for k, item in self._items.items():
            item.set_checked(k == key)


class ProgramDropdown(PopupPanel):
    """日志页"按程序分类"的程序下拉菜单。"""

    program_selected = Signal(int)

    def __init__(self, programs: list, parent: Optional[QWidget] = None,
                 max_height: int = 420, width: int = 300) -> None:
        super().__init__(parent)
        self.programs = programs

        self.scroll = SlimScrollArea(self.container)
        self.scroll.setWidgetResizable(True)
        self.scroll.setMaximumHeight(int(max_height))
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(config.sx(2))
        for program in programs:
            lay.addWidget(self._build_row(program))
        lay.addStretch(1)
        self.scroll.setWidget(holder)
        self.body.addWidget(self.scroll)
        self.setMinimumWidth(config.sx(width))

    def _build_row(self, program) -> QWidget:
        row = HoverRow(payload=int(program["id"]), parent=self.container, margins=(9, 7, 9, 7))
        row.clicked.connect(self._on_row)

        tile = IconTile(34, row)
        if "kind" in program.keys() and str(program["kind"]) == "site":
            tile.setPixmap(vector_icons.pixmap("globe", config.sx(21), config.C_ACCENT))
        else:
            tile.setPixmap(icon_extract.pixmap_for_program(
                program["icon_path"], program["path"], config.sx(21)))
        row._layout.addWidget(tile, 0, Qt.AlignVCenter)

        column = QWidget(row)
        col = QVBoxLayout(column)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        name = QLabel(str(program["name"]))
        name.setFont(theme.font(config.FONT_BODY, bold=True))
        name.setStyleSheet(f"color: {config.C_TEXT}; background: transparent;")
        col.addWidget(name)
        path = QLabel(util.shorten_path(str(program["path"]), 46))
        path.setFont(theme.font(config.FONT_TINY))
        path.setStyleSheet(f"color: {config.C_TEXT_MUTED}; background: transparent;")
        path.setToolTip(str(program["path"]))
        col.addWidget(path)
        row._layout.addWidget(column, 1)
        return row

    def _on_row(self, program_id) -> None:
        self.program_selected.emit(int(program_id))
        self.close()


class SelectBox(QWidget):
    """下拉选择框（设置页"点击关闭程序时"使用）。"""

    current_changed = Signal(str)

    def __init__(self, options: Iterable[tuple[str, str]], current: str = "",
                 parent: Optional[QWidget] = None, min_width: int = 150) -> None:
        super().__init__(parent)
        self._options = list(options)
        self._current = current or (self._options[0][0] if self._options else "")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setObjectName("SelectBox")
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setFixedHeight(config.sx(34))
        self.setMinimumWidth(config.sx(min_width))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(config.sx(12), 0, config.sx(10), 0)
        layout.setSpacing(config.sx(8))
        self.label = QLabel(self._text_for(self._current))
        self.label.setFont(theme.font(config.FONT_BODY, bold=True))
        self.label.setStyleSheet(f"color: {config.C_TEXT}; background: transparent;")
        layout.addWidget(self.label, 1)
        self.arrow = QLabel()
        self.arrow.setPixmap(vector_icons.pixmap("chevron_down", config.sx(14), config.C_TEXT_MUTED))
        self.arrow.setStyleSheet("background: transparent;")
        layout.addWidget(self.arrow, 0)

    def _text_for(self, key: str) -> str:
        for k, text in self._options:
            if k == key:
                return text
        return key

    def current_key(self) -> str:
        return self._current

    def set_current(self, key: str, emit: bool = False) -> None:
        if not any(k == key for k, _ in self._options):
            return
        self._current = key
        self.label.setText(self._text_for(key))
        if emit:
            self.current_changed.emit(key)

    def set_options(self, options, keep: str = "") -> None:
        """替换候选项；``keep`` 仍存在时保持选中，否则退回第一项。"""
        self._options = list(options)
        if not self._options:
            self._current = ""
            self.label.setText("")
            return
        valid = {key for key, _ in self._options}
        self._current = keep if keep in valid else self._options[0][0]
        self.label.setText(self._text_for(self._current))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._open()
        super().mouseReleaseEvent(event)

    def _open(self) -> None:
        # 选项多（时段选择器的小时 / 分钟）时给弹层限高并滚动
        max_height = config.sx(300) if len(self._options) > 14 else None
        popup = MenuPopup(self._options, self._current, self, min_width=max(150, self.width()),
                          max_height=max_height)
        popup.item_selected.connect(self._on_selected)
        popup.popup_at(self)

    def _on_selected(self, key: str) -> None:
        self.set_current(key, emit=True)
