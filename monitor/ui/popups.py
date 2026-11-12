"""弹出层：下拉菜单、程序选择下拉、下拉选择框。"""

from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtCore import QAbstractAnimation, QEvent, QObject, QPoint, QRect, QRectF, Qt, Signal, QTimer, QVariantAnimation
from PySide6.QtGui import QCursor, QGuiApplication, QPainter, QRegion
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import config, icon_extract, util
from . import motion, theme, vector_icons
from .controls import IconTile, SlimScrollArea

SHADOW_MARGIN = 14


class _AnchorGuard(QObject):
    """Consume only the physical click that toggles an already-open anchor."""
    def __init__(self, anchor, popup):
        super().__init__(anchor)
        self.anchor = anchor
        self.popup = popup
        self.release_pending = False
        QApplication.instance().installEventFilter(self)

    def detach(self):
        QApplication.instance().removeEventFilter(self)
        self.deleteLater()

    def eventFilter(self, obj, event):
        if event.type() not in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            return False
        if event.button() != Qt.LeftButton:
            return False
        inside = self.anchor.rect().contains(self.anchor.mapFromGlobal(event.globalPosition().toPoint()))
        if event.type() == QEvent.MouseButtonPress and inside and self.popup is not None:
            self.release_pending = True
            self.popup.close()
            return True
        if event.type() == QEvent.MouseButtonPress and self.popup is not None:
            if not self.popup.rect().contains(self.popup.mapFromGlobal(event.globalPosition().toPoint())):
                self.popup._animate = False
        if event.type() == QEvent.MouseButtonRelease and self.release_pending:
            self.release_pending = False
            self.detach()
            return True
        return False

    def dismissed(self):
        self.popup = None
        if not self.release_pending:
            # Native Qt popup dismissal can precede delivery of the anchor press.
            if QApplication.mouseButtons() & Qt.LeftButton and self.anchor.rect().contains(
                    self.anchor.mapFromGlobal(QCursor.pos())):
                self.release_pending = True
            else:
                self.detach()


class HoverRow(QWidget):
    """可点击并带悬停高亮的一行。"""

    clicked = Signal(object)

    def __init__(self, payload=None, parent: Optional[QWidget] = None,
                 margins: tuple[int, int, int, int] = (10, 6, 10, 6)) -> None:
        super().__init__(parent)
        self.payload = payload
        self.setFocusPolicy(Qt.StrongFocus)
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


class _PopupExit(QWidget):
    """A paint-only exit that cannot retain a popup grab or consume outside clicks."""
    finished = Signal()

    def __init__(self, popup):
        owner = popup.parentWidget()
        super().__init__(owner.window() if owner is not None else None,
                         Qt.ToolTip | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
                         | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setGeometry(popup.geometry())
        self._image = popup.grab()
        self._progress = popup._reveal
        self._above = popup._above
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(motion.POPOVER_MS)
        self._animation.setStartValue(self._progress)
        self._animation.setEndValue(0.)
        self._animation.setEasingCurve(motion.easing())
        self._animation.valueChanged.connect(self._tick)
        self._animation.finished.connect(self._finish)
        self.show()
        self._animation.start()

    def _tick(self, value):
        self._progress = float(value)
        self.update()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        height = round(self.height() * self._progress)
        y = self.height() - height if self._above else 0
        painter.setClipRect(0, y, self.width(), height)
        painter.setOpacity(.65 + .35 * self._progress)
        painter.drawPixmap(0, 0, self._image)
        painter.end()

    def _finish(self):
        self.finished.emit()
        self.hide()
        self.deleteLater()


class PopupPanel(QWidget):
    """A fixed native popup with a trigger-anchored reveal and input-free exit."""
    dismissed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        motion.track_input()
        self._reveal = 1.
        self._above = False
        self._opening = None
        self._exit_ghost = None
        self._was_shown = False
        self._animate = True
        self._closing = None
        self._close_complete = False
        self._anchor_guard = None
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
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        theme.paint_soft_shadow(painter, QRectF(self.container.geometry()), config.sx(12),
                                spread=config.sx(11), dy=config.sx(6), alpha=58)
        painter.end()

    def _set_reveal(self, value):
        self._reveal = max(0., min(1., float(value)))
        if self._reveal >= 1.:
            self.clearMask()
        else:
            height = max(1, round(self.height() * self._reveal))
            top = self.height() - height if self._above else 0
            # The native hit region follows exactly the visible panel.
            self.setMask(QRegion(QRect(0, top, self.width(), height)))

    def popup_at(self, anchor: QWidget, align: str = "left", offset: int = 7,
                 max_height: Optional[int] = None, animate: bool = True) -> None:
        self._animate = bool(animate) and motion.should_animate(self)
        self._anchor_guard = _AnchorGuard(anchor, self)
        self.dismissed.connect(self._anchor_guard.dismissed)
        self.setAttribute(Qt.WA_DeleteOnClose, False)  # one shared deferred lifecycle
        self.adjustSize()
        screen = QGuiApplication.screenAt(anchor.mapToGlobal(anchor.rect().center())) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        if max_height:
            self.setMaximumHeight(min(int(max_height) + 2 * SHADOW_MARGIN, area.height()))
        else:
            self.setMaximumHeight(area.height())
        self.adjustSize()
        width = min(self.width(), area.width())
        height = min(self.height(), self.maximumHeight(), area.height())
        anchor_top = anchor.mapToGlobal(QPoint(0, 0))
        anchor_bottom = anchor.mapToGlobal(QPoint(0, anchor.height()))
        x = anchor_top.x() - SHADOW_MARGIN
        if align == "right":
            x = anchor_top.x() + anchor.width() - width + SHADOW_MARGIN
        elif align == "center":
            x = anchor_top.x() + (anchor.width() - width) // 2
        x = max(area.left(), min(x, area.right() - width + 1))
        below = anchor_bottom.y() + offset - SHADOW_MARGIN
        self._above = below + height > area.bottom() + 1
        y = anchor_top.y() - height - offset + SHADOW_MARGIN if self._above else below
        y = max(area.top(), min(y, area.bottom() - height + 1))
        self.setGeometry(x, y, width, height)
        if self.layout() is not None:
            self.layout().activate()
        self._set_reveal(0. if self._animate else 1.)
        self._was_shown = True
        self.show()
        self.raise_()
        rows = self.findChildren(HoverRow)
        if rows:
            current = next((row for row in rows if getattr(row, '_checked', False)), rows[0])
            current.setFocus(Qt.PopupFocusReason)
        if self._animate:
            animation = QVariantAnimation(self)
            animation.setDuration(motion.POPOVER_MS)
            animation.setStartValue(self._reveal)
            animation.setEndValue(1.)
            animation.setEasingCurve(motion.easing())
            animation.valueChanged.connect(self._set_reveal)
            animation.finished.connect(lambda: setattr(self, '_opening', None))
            self._opening = animation
            animation.start(QAbstractAnimation.DeleteWhenStopped)

    def closeEvent(self, event):
        if self._close_complete or not self.isVisible() or not self._animate or not motion.should_animate(self):
            return super().closeEvent(event)
        event.ignore()
        if self._closing is not None:
            return
        if self._opening is not None:
            self._opening.stop()
            self._opening.deleteLater()
            self._opening = None
        self._closing = QVariantAnimation(self)
        self._closing.setDuration(130)
        self._closing.setStartValue(self._reveal)
        self._closing.setEndValue(0.)
        self._closing.setEasingCurve(motion.easing())
        self._closing.valueChanged.connect(self._set_reveal)
        self._closing.finished.connect(self._finish_close)
        self._closing.start()

    def _finish_close(self):
        self._close_complete = True
        self.close()

    def hideEvent(self, event):  # noqa: N802
        # Let Qt.Popup release its native mouse/focus grab immediately. Deferring
        # hide/close until an exit animation ends swallows the outside click.
        if self._was_shown:
            self._was_shown = False
            if self._opening is not None:
                self._opening.stop()
                self._opening.deleteLater()
                self._opening = None
            if self._closing is not None:
                self._closing.stop()
            # No replacement native window: avoids the hide/show flash. Outside
            # dismissal stays immediate so Qt delivers that click normally.
            QTimer.singleShot(0, self, self.deleteLater)
            self.dismissed.emit()
        super().hideEvent(event)

    def keyPressEvent(self, event):  # noqa: N802
        rows = self.findChildren(HoverRow)
        focus = self.focusWidget()
        index = rows.index(focus) if focus in rows else -1
        if rows and event.key() in (Qt.Key_Down, Qt.Key_Up, Qt.Key_Home, Qt.Key_End):
            if event.key() == Qt.Key_Home:
                index = 0
            elif event.key() == Qt.Key_End:
                index = len(rows) - 1
            else:
                index = (index + (1 if event.key() == Qt.Key_Down else -1)) % len(rows)
            rows[index].setFocus(Qt.TabFocusReason)
            scroll = getattr(self, 'scroll', None)
            if isinstance(scroll, SlimScrollArea):
                scroll.ensureWidgetVisible(rows[index])
            event.accept()
            return
        if rows and event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space) and index >= 0:
            self._animate = False
            rows[index].clicked.emit(rows[index].payload)
            event.accept()
            return
        if event.key() == Qt.Key_Escape:
            self._animate = False
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)


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

    def focus_current(self):
        items = list(self._items.values())
        current = next((item for item in items if item._checked), items[0] if items else None)
        if current is not None:
            current.setFocus(Qt.PopupFocusReason)

    def keyPressEvent(self, event):  # noqa: N802
        items = list(self._items.values())
        focus = self.focusWidget()
        index = items.index(focus) if focus in items else -1
        if items and event.key() in (Qt.Key_Down, Qt.Key_Up, Qt.Key_Home, Qt.Key_End):
            if event.key() == Qt.Key_Home:
                index = 0
            elif event.key() == Qt.Key_End:
                index = len(items) - 1
            else:
                index = (index + (1 if event.key() == Qt.Key_Down else -1)) % len(items)
            items[index].setFocus(Qt.TabFocusReason)
            if isinstance(getattr(self, 'scroll', None), SlimScrollArea):
                self.scroll.ensureWidgetVisible(items[index])
            event.accept()
            return
        if items and event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space) and index >= 0:
            self._animate = False
            self._on_item(items[index].key)
            event.accept()
            return
        super().keyPressEvent(event)

    def _on_item(self, key: str) -> None:
        for k, item in self._items.items():
            item.set_checked(k == key)
        self.close()
        self.item_selected.emit(key)

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
        self.close()
        self.program_selected.emit(int(program_id))


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
        self.setFocusPolicy(Qt.StrongFocus)
        self._popup = None
        motion.track_input()
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
        from .controls import Chevron
        self.arrow = Chevron(config.sx(14), config.C_TEXT_MUTED, self)
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

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Down):
            self._open(animate=False)
            event.accept()
            return
        super().keyPressEvent(event)

    def _open(self, animate=True) -> None:
        if self._popup is not None:
            self._popup.close()
            return
        # 选项多（时段选择器的小时 / 分钟）时给弹层限高并滚动
        max_height = config.sx(300) if len(self._options) > 14 else None
        popup = MenuPopup(self._options, self._current, self, min_width=max(150, self.width()),
                          max_height=max_height)
        self._popup = popup
        popup.item_selected.connect(self._on_selected)
        popup.dismissed.connect(self._popup_closed)
        self.arrow.set_expanded(True, animate=animate)
        popup.popup_at(self, animate=animate)
        popup.focus_current()

    def _popup_closed(self):
        self._popup = None
        self.arrow.set_expanded(False)

    def _on_selected(self, key: str) -> None:
        self.set_current(key, emit=True)
