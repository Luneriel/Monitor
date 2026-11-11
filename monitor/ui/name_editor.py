"""程序名称的就地编辑控件。

需求要点：
    * 点击名称 -> 名称位置变成输入框，内容为原名；
    * 输入框初始宽度 = 窗口宽度的 15%，随文本向右扩展；
    * 但确认按钮碰到窗口右边框后不再扩展，改为输入框下方出现可左右滑动的滑块条；
    * 右侧有"取消（叉形、灰底）"与"确认（勾形、蓝底）"图标按钮；
    * 取消 / 点击界面其他位置 -> 还原；确认 / 回车 -> 改名。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLineEdit, QPushButton,
                               QScrollArea, QScrollBar, QSizePolicy, QVBoxLayout, QWidget)

from .. import config
from ..i18n import tr
from . import theme, vector_icons
from .controls import ClickableLabel

EDITOR_HEIGHT = 26
BUTTON_SIZE = 24
WINDOW_RIGHT_PAD = 22


class RoundIconButton(QPushButton):
    """圆形图标按钮（取消=灰底叉形，确认=蓝底勾形）。"""

    def __init__(self, icon_name: str, background: str, hover: str,
                 icon_color: str = "#FFFFFF", size: int = BUTTON_SIZE,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(config.sx(size), config.sx(size))
        self.setIcon(vector_icons.icon(icon_name, max(11, config.sx(13)), icon_color, 2.3))
        radius = config.sx(size) // 2
        self.setStyleSheet(
            f"QPushButton {{ background: {background}; border: none; border-radius: {radius}px; }}"
            f"QPushButton:hover {{ background: {hover}; }}"
        )


class NameEditor(QWidget):
    """输入框 + 取消 / 确认按钮。"""

    confirmed = Signal(str)
    cancelled = Signal()

    def __init__(self, text: str, initial_width: int, max_view_width: int,
                 font_size: int = config.FONT_CARD_NAME, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._initial_width = max(config.sx(70), int(initial_width))
        self._max_view = max(config.sx(70), int(max_view_width))
        self._finished = False
        self._press_inside = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(config.sx(5))

        # 输入框与其下方的水平滑块条放在同一列：滑块条在输入框外侧，
        # 不会占用输入框高度、也不会遮挡正在编辑的名称。
        column = QWidget(self)
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(config.sx(3))

        self.scroll = QScrollArea(column)
        self.scroll.setWidgetResizable(False)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(
            f"QScrollArea {{ background: {config.C_SURFACE};"
            f" border: 1px solid {config.C_PRIMARY};"
            f" border-radius: {config.sx(7)}px; }}"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )

        self.edit = QLineEdit(text)
        self.edit.setFrame(False)
        self.edit.setFont(theme.font(font_size))
        self.edit.setStyleSheet("QLineEdit { border: none; background: transparent; padding: 0px; }")
        self._edit_h = config.sx(EDITOR_HEIGHT)
        self.edit.setFixedHeight(self._edit_h - 2)
        self.scroll.setWidget(self.edit)
        self.scroll.setFixedHeight(self._edit_h)
        column_layout.addWidget(self.scroll)

        # 输入框下方的水平滑块条（文本超出可视宽度时出现）
        self.bar = QScrollBar(Qt.Horizontal, column)
        self.bar.setObjectName("NameScrollBar")
        self.bar.setFixedHeight(config.sx(7))
        self.bar.setStyleSheet(
            "QScrollBar#NameScrollBar:horizontal { background: transparent; height: 7px;"
            " margin: 0px; border: none; }"
            "QScrollBar#NameScrollBar::handle:horizontal { background: #C6CEDC;"
            " border-radius: 3px; min-width: 28px; }"
            "QScrollBar#NameScrollBar::handle:horizontal:hover { background: #A6B2C6; }"
            "QScrollBar#NameScrollBar::add-line:horizontal,"
            "QScrollBar#NameScrollBar::sub-line:horizontal { width: 0px; }"
            "QScrollBar#NameScrollBar::add-page:horizontal,"
            "QScrollBar#NameScrollBar::sub-page:horizontal { background: transparent; }"
        )
        self.bar.setVisible(False)
        column_layout.addWidget(self.bar)

        self.cancel_btn = RoundIconButton("close", "#9E9E9E", "#8A8A8A")
        self.confirm_btn = RoundIconButton("check", config.C_ACCENT, config.C_ACCENT_DARK)
        self.cancel_btn.setToolTip(tr("common.cancel"))
        self.confirm_btn.setToolTip(tr("common.confirm"))
        # 用 released 而不是 clicked：clicked 要求"按下与抬起都落在按钮内"，
        # 真实鼠标点击时若焦点在按下瞬间发生变化（触发"点击外部即取消"），
        # clicked 会被吞掉，表现为"点 √ 重命名无效"；released 不会。
        self.cancel_btn.released.connect(self._on_cancel)
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.confirm_btn.released.connect(self._on_confirm)
        self.confirm_btn.clicked.connect(self._on_confirm)

        layout.addWidget(column)
        layout.addWidget(self.cancel_btn, 0, Qt.AlignTop)
        layout.addWidget(self.confirm_btn, 0, Qt.AlignTop)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self.bar.valueChanged.connect(self._on_bar_moved)
        self.edit.textChanged.connect(self._relayout)
        self.edit.returnPressed.connect(self._on_confirm)
        self.edit.installEventFilter(self)
        # 应用级过滤器：点击"输入框以外的界面部分"要取消编辑
        self._app = QApplication.instance()
        if self._app is not None:
            self._app.installEventFilter(self)
        self._relayout()
        QTimer.singleShot(0, self._focus)

    # ------------------------------------------------------------------
    def _focus(self) -> None:
        self.edit.setFocus(Qt.OtherFocusReason)
        self.edit.selectAll()

    def _text_width(self) -> int:
        fm = QFontMetrics(self.edit.font())
        return fm.horizontalAdvance(self.edit.text()) + config.sx(16)

    def _on_bar_moved(self, value: int) -> None:
        self.scroll.horizontalScrollBar().setValue(value)

    def _relayout(self) -> None:
        text_w = self._text_width()
        view = min(max(self._initial_width, text_w), self._max_view)
        self.scroll.setFixedWidth(view)
        self.edit.setFixedWidth(max(view, text_w))
        self.scroll.setFixedHeight(self._edit_h)

        # 文本超出可视宽度：显示输入框下方的水平滑块条（输入框本身高度不变）
        overflow = max(0, text_w - view)
        need_scroll = overflow > 1
        self.bar.setRange(0, overflow)
        self.bar.setPageStep(max(1, view))
        self.bar.setSingleStep(config.sx(16))
        self.bar.setVisible(need_scroll)
        if not need_scroll:
            self.bar.setValue(0)
        inner = self.scroll.horizontalScrollBar()
        inner.setRange(0, overflow)
        inner.setPageStep(max(1, view))
        inner.setValue(self.bar.value())

        column_h = self._edit_h + (self.bar.height() + config.sx(3) if need_scroll else 0)
        total = view + self.cancel_btn.width() + self.confirm_btn.width() + 2 * config.sx(5)
        self.setFixedWidth(total)
        self.setFixedHeight(column_h)

    # ------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.edit:
            if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                self._on_cancel()
                return True
            if event.type() == QEvent.FocusOut:
                QTimer.singleShot(0, self._maybe_cancel_on_focus_out)
        elif event.type() == QEvent.MouseButtonPress and not self._finished:
            # 点击界面中除输入框（含取消/确认按钮）以外的任何位置 => 取消
            try:
                global_pos = event.globalPosition().toPoint()
            except AttributeError:
                global_pos = event.globalPos()
            self._press_inside = self.rect().contains(self.mapFromGlobal(global_pos))
            if not self._press_inside:
                QTimer.singleShot(0, self._maybe_cancel_on_focus_out)
        elif event.type() == QEvent.MouseButtonRelease:
            self._press_inside = False
        return super().eventFilter(obj, event)

    def _release_filter(self) -> None:
        if self._app is not None:
            try:
                self._app.removeEventFilter(self)
            except (RuntimeError, TypeError):
                pass
            self._app = None

    def _finish(self) -> bool:
        if self._finished:
            return False
        self._finished = True
        self._release_filter()
        return True

    def _on_confirm(self) -> None:
        if not self._finish():
            return
        text = self.edit.text().strip()
        self.confirmed.emit(text)
        self.hide()

    def _on_cancel(self) -> None:
        if not self._finish():
            return
        self.cancelled.emit()
        self.hide()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        # 点击界面其他位置 => 取消（按钮不接收焦点，因此不会误触发）
        if not self._finished:
            QTimer.singleShot(0, self._maybe_cancel_on_focus_out)
        super().focusOutEvent(event)

    def _maybe_cancel_on_focus_out(self) -> None:
        """焦点离开时判断是否"点了别处"，是才取消编辑。

        两种情况必须放过，否则会把点 √ / × 的那次点击吞掉（表现为重命名无效）：
            1. 鼠标仍按着（这一次点击还没走完，等它结束再判）；
            2. 这次按下就发生在编辑区内部（含两个按钮）。
        """
        if self._finished:
            return
        if QApplication.mouseButtons() != Qt.NoButton or self._press_inside:
            QTimer.singleShot(60, self._maybe_cancel_on_focus_out)
            return
        app = QApplication.instance()
        focus = app.focusWidget() if app else None
        if focus is not None and (focus is self or self.isAncestorOf(focus)
                                  or focus in (self.cancel_btn, self.confirm_btn)):
            return
        self._on_cancel()


class EditableName(QWidget):
    """可点击改名的一行文字（卡片与弹窗列表共用）。"""

    renamed = Signal(str)
    edit_started = Signal()
    edit_finished = Signal()

    def __init__(self, text: str, font_size: int = config.FONT_CARD_NAME, bold: bool = False,
                 color: str = "", editable: bool = True,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        color = color or config.C_TEXT
        self._text = text
        self._font_size = font_size
        self._bold = bold
        self._color = color
        self._editable = editable
        self._editor: Optional[NameEditor] = None

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(config.sx(6))
        self.label = ClickableLabel(text)
        self.label.setFont(theme.font(font_size, bold))
        self.label.setStyleSheet(f"color: {color}; background: transparent;")
        self.label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        if editable:
            self.label.setToolTip(tr("common.click_to_rename"))
            self.label.clicked.connect(self.begin_edit)
        else:
            self.label.setCursor(Qt.ArrowCursor)
        self._layout.addWidget(self.label)
        self._layout.addStretch(1)

    # ------------------------------------------------------------------
    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text
        self.label.setTextFormat(Qt.PlainText)
        self.label.setText(text)

    def set_display(self, shown: str, rich: bool = False) -> None:
        """仅改变显示内容（用于搜索高亮），真实名称保持在 _text 中。"""
        self.label.setTextFormat(Qt.RichText if rich else Qt.PlainText)
        self.label.setText(shown)

    def set_editable(self, editable: bool) -> None:
        self._editable = editable
        if editable:
            self.label.setCursor(Qt.IBeamCursor)
            self.label.setToolTip("点击可重命名")
        else:
            self.label.setCursor(Qt.ArrowCursor)
            self.label.setToolTip("")

    def is_editing(self) -> bool:
        return self._editor is not None

    def cancel_edit(self) -> None:
        if self._editor is not None:
            self._editor._finish()
            self._close_editor()

    # ------------------------------------------------------------------
    def begin_edit(self) -> None:
        if not self._editable or self._editor is not None:
            return
        window = self.window()
        win_width = window.width() if window else config.BASE_WINDOW_W
        initial = int(win_width * 0.15)

        origin = self.mapTo(window, QPoint(0, 0)) if window else QPoint(0, 0)
        right_limit = win_width - config.sx(WINDOW_RIGHT_PAD)
        buttons = 2 * config.sx(BUTTON_SIZE) + 3 * config.sx(5)
        max_view = max(config.sx(80), right_limit - origin.x() - buttons)

        editor = NameEditor(self._text, initial, max_view, self._font_size, self)
        editor.confirmed.connect(self._on_confirmed)
        editor.cancelled.connect(self._on_cancelled)
        self._layout.replaceWidget(self.label, editor)
        self.label.hide()
        self._editor = editor
        self.edit_started.emit()

    def _close_editor(self) -> None:
        editor = self._editor
        self._editor = None
        if editor is None:
            return
        self._layout.replaceWidget(editor, self.label)
        editor.hide()          # 先隐藏：可见控件脱离父级会变成顶层窗口
        editor.setParent(None)
        editor.deleteLater()
        self.label.show()
        self.edit_finished.emit()

    def _on_confirmed(self, text: str) -> None:
        if text:
            self._text = text
            self.label.setText(text)
            self.renamed.emit(text)
        self._close_editor()

    def _on_cancelled(self) -> None:
        self._close_editor()
