"""程序卡片与程序行（首页卡片、添加弹窗、删除弹窗、日志下拉菜单共用）。"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor, QFontMetrics
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from .. import config, icon_extract, util
from ..i18n import tr
from . import theme
from . import vector_icons
from .controls import Card, CircleCheck, IconButton, IconTile, StatusDot, ToggleSwitch
from .name_editor import EditableName


def program_field(program: Any, key: str, default=None):
    try:
        value = program[key]
    except Exception:
        return default
    return default if value is None else value


def is_site(program: Any) -> bool:
    """这一项是"被追踪的网站"还是"程序"（programs.kind）。"""
    return str(program_field(program, "kind", "program") or "program") == "site"


def program_icon(program: Any, size: int):
    # 网站没有可执行文件图标：用矢量地球图，别退化成默认程序图标
    if is_site(program):
        return vector_icons.pixmap("globe", size, config.C_ACCENT)
    return icon_extract.pixmap_for_program(
        program_field(program, "icon_path"), str(program_field(program, "path", "") or ""), size)


class ProgramRow(QWidget):
    """通用"图标 + 名称 + 路径"一行，可附带右侧控件。"""

    renamed = Signal(str)
    clicked = Signal()

    def __init__(self, name: str, path: str, icon_path: Optional[str] = None,
                 exe_path: str = "", editable: bool = True,
                 right_widget: Optional[QWidget] = None,
                 icon_size: int = 30, tile_size: int = 40,
                 name_size: int = config.FONT_CARD_NAME,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._name = name
        self._path = path

        layout = QHBoxLayout(self)
        layout.setContentsMargins(config.sx(2), config.sx(5), config.sx(6), config.sx(5))
        layout.setSpacing(config.sx(11))

        tile = IconTile(tile_size, self)
        tile.setPixmap(icon_extract.pixmap_for_program(icon_path, exe_path, config.sx(icon_size)))
        layout.addWidget(tile, 0, Qt.AlignVCenter)

        column = QWidget(self)
        col = QVBoxLayout(column)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        self._column = column

        self.name = EditableName(name, name_size, bold=True, editable=editable, parent=column)
        # 名称可被压缩并省略，避免把右侧控件（如"保留日志"滑块）挤出可视区域
        self.name.label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.name.label.setMinimumWidth(config.sx(56))
        self.name.label.setToolTip(name if not editable else tr("common.click_to_rename"))
        self.name.renamed.connect(self._on_renamed)
        col.addWidget(self.name)

        self.path_label = QLabel()
        self.path_label.setObjectName("Muted")
        self.path_label.setFont(theme.font(config.FONT_SMALL))
        self.path_label.setStyleSheet(f"color: {config.C_TEXT_MUTED}; background: transparent;")
        self.path_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.path_label.setMinimumWidth(config.sx(56))
        self.path_label.setToolTip(path)
        col.addWidget(self.path_label)

        layout.addWidget(column, 1)

        self.right_widget = right_widget
        if right_widget is not None:
            right_widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            layout.addWidget(right_widget, 0, Qt.AlignVCenter)
        self._tile_size = config.sx(tile_size)
        self._right_width = right_widget.sizeHint().width() if right_widget is not None else 0
        self._elide()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide()

    def _text_width(self) -> int:
        """按行宽扣除图标 / 右侧控件后，留给文字的宽度。"""
        reserved = (config.sx(2) + self._tile_size + 2 * config.sx(11)
                    + self._right_width + config.sx(6) + config.sx(4))
        return max(config.sx(56), self.width() - reserved)

    def _elide(self) -> None:
        width = self._text_width()
        fm_name = QFontMetrics(self.name.label.font())
        elided_name = fm_name.elidedText(self._name, Qt.ElideMiddle, width)
        self.name.set_display(elided_name, rich=False)
        # 标签按省略后的文本自适应宽度，避免被压成 0 宽而被硬裁切
        self.name.label.setFixedWidth(
            min(width, fm_name.horizontalAdvance(elided_name) + config.sx(6)))
        fm_path = QFontMetrics(self.path_label.font())
        self.path_label.setText(fm_path.elidedText(self._path, Qt.ElideMiddle, width))

    def _on_renamed(self, text: str) -> None:
        self._name = text
        self.name.label.setToolTip(text if not self.name._editable else tr("common.click_to_rename"))
        self._elide()
        self.renamed.emit(text)


class ProgramCard(Card):
    """首页中的单个被追踪程序卡片。"""

    tracking_toggled = Signal(int, bool)
    renamed = Signal(int, str)
    selection_toggled = Signal(int, bool)

    def __init__(self, program: Any, delete_mode: bool = False, selected: bool = False,
                 manual: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.program_id = int(program_field(program, "id", 0))
        self.program_path = str(program_field(program, "path", "") or "")
        self._tracking = bool(program_field(program, "tracking", 1))
        self._delete_mode = delete_mode
        self._selected = selected
        self._manual = bool(manual)
        self._running = False
        self._query = ""

        root = QHBoxLayout(self)
        root.setContentsMargins(config.sx(16), config.sx(13), config.sx(16), config.sx(13))
        root.setSpacing(config.sx(13))
        self._root = root

        # 拖动手柄：只在"手动排序"模式下出现，且只有它响应拖动（卡片其它位置照常点击）
        self.drag_handle = IconButton("grip", tr("common.drag_handle"), size=28, icon_size=16,
                                      color=config.C_TEXT_MUTED, parent=self, flat=True)
        root.addWidget(self.drag_handle, 0, Qt.AlignVCenter)
        # 先入布局再设置可见性：无父控件时 setVisible(True) 会变成独立顶层窗口弹出
        self.drag_handle.setVisible(self._manual and not delete_mode)

        self.check = CircleCheck(selected)
        root.addWidget(self.check, 0, Qt.AlignVCenter)
        # 先入布局再设置可见性：无父控件时 setVisible(True) 会变成独立顶层窗口弹出
        self.check.setVisible(delete_mode)

        self.tile = IconTile(46, self)
        self.tile.setPixmap(program_icon(program, config.sx(30)))
        root.addWidget(self.tile, 0, Qt.AlignVCenter)

        column = QWidget(self)
        col = QVBoxLayout(column)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(config.sx(3))
        self._column = column

        self.name = EditableName(str(program_field(program, "name", "")), config.FONT_CARD_NAME,
                                 bold=True, editable=not delete_mode, parent=column)
        self.name.renamed.connect(lambda text: self.renamed.emit(self.program_id, text))
        self.name.edit_started.connect(self._on_edit_started)
        self.name.edit_finished.connect(self._on_edit_finished)
        col.addWidget(self.name)

        self.path_label = QLabel()
        self.path_label.setFont(theme.font(config.FONT_SMALL))
        self.path_label.setStyleSheet(f"color: {config.C_TEXT_MUTED}; background: transparent;")
        self.path_label.setToolTip(self.program_path)
        col.addWidget(self.path_label)

        self.time_row = QWidget(column)
        time_layout = QHBoxLayout(self.time_row)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.setSpacing(config.sx(6))
        self.dot = StatusDot(config.C_TEXT_FAINT, self.time_row, 8)
        time_layout.addWidget(self.dot, 0, Qt.AlignVCenter)
        self.time_label = QLabel(tr("card.not_run_yet"))
        self.time_label.setFont(theme.font(config.FONT_SMALL))
        self.time_label.setStyleSheet(f"color: {config.C_TEXT_MUTED}; background: transparent;")
        time_layout.addWidget(self.time_label, 0, Qt.AlignVCenter)
        time_layout.addStretch(1)
        col.addWidget(self.time_row)

        root.addWidget(column, 1)

        self.switch = ToggleSwitch(self._tracking, self)
        self.switch.toggled.connect(
            lambda state: self.tracking_toggled.emit(self.program_id, state))
        self.switch.set_look_enabled(not delete_mode)
        root.addWidget(self.switch, 0, Qt.AlignVCenter)

        if delete_mode:
            self.setCursor(QCursor(Qt.PointingHandCursor))
        self.set_selected(selected)
        self._apply_visibility()

    # ------------------------------------------------------------------
    def _apply_visibility(self) -> None:
        """路径与状态行始终显示：关闭追踪时状态行显示灰色"已关闭追踪"。"""
        self.path_label.setVisible(True)
        self.time_row.setVisible(True)

    def _on_edit_started(self) -> None:
        self.switch.hide()

    def _on_edit_finished(self) -> None:
        if not self._delete_mode:
            self.switch.show()

    # ------------------------------------------------------------------
    def set_delete_mode(self, enabled: bool, selected: bool = False) -> None:
        self._delete_mode = enabled
        self._selected = selected
        self.check.setChecked(selected)
        self.check.setVisible(enabled)
        self.drag_handle.setVisible(self._manual and not enabled)
        self.name.cancel_edit()
        self.name.set_editable(not enabled)
        self.switch.set_look_enabled(not enabled)
        self.setCursor(QCursor(Qt.PointingHandCursor if enabled else Qt.ArrowCursor))
        self.set_selected(selected)

    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self.check.setChecked(self._selected)
        super().set_selected(self._selected)

    def is_selected(self) -> bool:
        return self._selected

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._delete_mode and event.button() == Qt.LeftButton:
            self.set_selected(not self._selected)
            self.selection_toggled.emit(self.program_id, self._selected)
            event.accept()
            return
        super().mousePressEvent(event)

    # ------------------------------------------------------------------
    def update_time(self, running_start: Optional[float], last_end: Optional[float]) -> None:
        """刷新时间信息：运行中显示启动时间 + 已用时长，关闭追踪显示灰色提示。"""
        self._running = bool(running_start)
        if not self._tracking:
            self.dot.set_color(config.C_TEXT_FAINT)
            self.time_label.setStyleSheet(
                f"color: {config.C_TEXT_MUTED}; background: transparent;")
            self.time_label.setText(tr("card.tracking_off"))
            return
        if running_start:
            elapsed = max(0.0, util.now() - float(running_start))
            self.dot.set_color(config.C_SUCCESS)
            self.time_label.setStyleSheet(
                f"color: {config.C_SUCCESS}; background: transparent;")
            self.time_label.setText(tr(
                "card.running", start=util.format_ts(running_start),
                elapsed=util.format_duration(elapsed)))
        else:
            self.dot.set_color(config.C_TEXT_FAINT)
            self.time_label.setStyleSheet(
                f"color: {config.C_TEXT_MUTED}; background: transparent;")
            if last_end:
                self.time_label.setText(tr("card.last_end", time=util.format_ts(last_end)))
            else:
                self.time_label.setText(tr("card.no_logs"))

    def set_tracking_look(self, enabled: bool) -> None:
        self._tracking = bool(enabled)
        if not self._tracking:
            self.dot.set_color(config.C_TEXT_FAINT)
            self.time_label.setStyleSheet(
                f"color: {config.C_TEXT_MUTED}; background: transparent;")
            self.time_label.setText(tr("card.tracking_off"))

    # ------------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide_name()
        self._elide_path()

    def apply_search(self, query: str) -> None:
        self._query = query or ""
        self._elide_name()
        self._elide_path()

    def _available_width(self) -> int:
        return max(70, self._column.width() - config.sx(6))

    def _elide_name(self) -> None:
        query = getattr(self, "_query", "")
        name = self.name.text()
        fm = QFontMetrics(self.name.label.font())
        width = self._available_width()
        if query and query.lower() in name.lower():
            markup, _ = theme.highlight_html(name, query)
            self.name.set_display(markup, rich=True)
            self.name.label.setFixedWidth(min(width, fm.horizontalAdvance(name) + config.sx(8)))
            return
        elided = fm.elidedText(name, Qt.ElideMiddle, width)
        self.name.set_display(elided, rich=False)
        self.name.label.setFixedWidth(min(width, fm.horizontalAdvance(elided) + config.sx(6)))

    def _elide_path(self) -> None:
        query = getattr(self, "_query", "")
        if query and query.lower() in self.program_path.lower():
            markup, _ = theme.highlight_html(self.program_path, query)
            self.path_label.setTextFormat(Qt.RichText)
            self.path_label.setText(markup)
            return
        self.path_label.setTextFormat(Qt.PlainText)
        fm = QFontMetrics(self.path_label.font())
        self.path_label.setText(
            fm.elidedText(self.program_path, Qt.ElideMiddle, self._available_width()))
