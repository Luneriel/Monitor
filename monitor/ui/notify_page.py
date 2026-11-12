"""通知页：全局 / 按程序独立的通知设置 + 通知通道（Windows、企业微信）。

界面层次：
    * 全局通知设置（可展开卡片）：事件勾选、"程序追踪时长达到"下拉栏、密码修改事件；
      卡片下方是"独立设置"容器（+ 号添加程序，每个程序一张带程序图标的卡片）；
    * 通知通道：可开关的"启用 Windows 通知"与"启用企业微信通知"（webhook 地址 + 发送测试）。

"程序追踪时长达到"不再用勾选框 + 横向排列的时间胶囊（时间一多就会挤出窗口），
改为：左边一个 + 号按钮（点击展开下拉栏并在最下方新增一行自定义分钟数），
右边一个下拉箭头，下拉栏里每行一个时间值，时间值右侧的 - 号（两个独立容器）可移除该值。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPropertyAnimation, QSize, Qt, Signal, Slot
from PySide6.QtWidgets import (QCheckBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout, QWidget)

from .. import config, icon_extract
from ..i18n import tr
from ..notify import (CHANNEL_WECOM, CHANNEL_WINDOWS, CUSTOM_EVENTS, EVENT_CLOSE,
                      EVENT_DAILY_LIMIT, EVENT_DURATION, EVENT_OPEN, EVENT_PAUSE, EVENT_PASSWORD,
                      EVENT_UNLOCKED, PLACEHOLDER_HINT, normalize)
from . import anim, theme, vector_icons
from .anim import animate_height, clip_height
from .cards import is_site
from .controls import (CheckBox, Divider, IconButton, IconTile, Panel, SlimScrollArea, TextButton,
                       ToggleSwitch)
from .reorder import ReorderController
from .tooltip_guard import suppress_tooltips

# 前三行按需求固定排布
EVENT_LAYOUT = [
    (0, 0, EVENT_OPEN, "notify.event.open"),
    (0, 1, EVENT_CLOSE, "notify.event.close"),
    (1, 0, EVENT_DAILY_LIMIT, "notify.event.daily_limit"),
    (1, 1, EVENT_UNLOCKED, "notify.event.unlocked"),
    (2, 0, EVENT_PAUSE, "notify.event.pause"),
]

DEFAULT_DURATIONS = (30, 60, 90)


# 可展开卡片的高度动画来自 anim（控制页也用同一套，避免两份实现逐渐跑偏）
# --------------------------------------------------------------------------
# 可展开卡片（带高度动画）
# --------------------------------------------------------------------------
class _Section(Panel):
    """带图标、标题、说明与展开箭头的分组卡片；展开/收起带高度动画。"""

    def __init__(self, icon: str, title: str, hint: str = "", parent=None,
                 expanded: bool = False, trailing: Optional[QWidget] = None,
                 icon_path: str = "", icon_exe: str = "",
                 handle: bool = False) -> None:
        super().__init__(parent, elevated=True, radius=16)
        self._expanded = expanded
        self._anim: Optional[QPropertyAnimation] = None
        self.drag_handle: Optional[QWidget] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(config.sx(20), config.sx(16), config.sx(20), config.sx(16))
        root.setSpacing(config.sx(12))

        head = QHBoxLayout()
        head.setSpacing(config.sx(14))
        if handle:
            # 拖动排序手柄（放在标题行最左侧，只有"独立设置"卡片用）
            self.drag_handle = IconButton("grip", tr("common.drag_handle"), size=28,
                                          icon_size=16, color=config.C_TEXT_MUTED,
                                          parent=self, flat=True)
            head.addWidget(self.drag_handle, 0, Qt.AlignVCenter)
        tile = IconTile(42, self)
        if icon_path or icon_exe:
            tile.setPixmap(icon_extract.pixmap_for_program(icon_path or None,
                                                           icon_exe or "", config.sx(26)))
        else:
            tile.setPixmap(vector_icons.pixmap(icon, config.sx(21), config.C_TEXT_BODY, 1.8))
        head.addWidget(tile, 0, Qt.AlignVCenter)

        texts = QVBoxLayout()
        texts.setSpacing(config.sx(3))
        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("CardTitle")
        self.title_label.setFont(theme.font(config.FONT_CARD_NAME, bold=True))
        texts.addWidget(self.title_label)
        self.hint_label = QLabel(hint, self)
        self.hint_label.setObjectName("Muted")
        self.hint_label.setFont(theme.font(config.FONT_SMALL))
        self.hint_label.setWordWrap(True)
        self.hint_label.setVisible(bool(hint))
        texts.addWidget(self.hint_label)
        head.addLayout(texts, 1)

        if trailing is not None:
            head.addWidget(trailing, 0, Qt.AlignVCenter)

        self.arrow = IconButton("chevron_right", parent=self, size=30, icon_size=16, flat=True)
        self.arrow.setObjectName("IconButtonFlat")
        self.arrow.setCursor(Qt.PointingHandCursor)
        self.arrow.setFixedSize(config.sx(30), config.sx(30))
        head.addWidget(self.arrow, 0, Qt.AlignVCenter)
        root.addLayout(head)

        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(config.sx(56), config.sx(2), 0, 0)
        self.content_layout.setSpacing(config.sx(10))
        root.addWidget(self.content)
        self.content.setMaximumHeight(16777215 if self._expanded else 0)
        self.content.setVisible(self._expanded)

        self.arrow.clicked.connect(self.toggle)
        self._sync_arrow()

    def _sync_arrow(self, animate=True) -> None:
        self.arrow.set_expanded(self._expanded, animate=animate and self.isVisible(), right=True)

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool, animate: bool = True) -> None:
        expanded = bool(expanded)
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        self._expanded = expanded
        self._sync_arrow(animate)
        if not animate:
            clip_height(self.content, None if expanded else 0, visible=expanded)
            return
        animate_height(self.content, expanded)


# --------------------------------------------------------------------------
# "程序追踪时长达到"下拉栏
# --------------------------------------------------------------------------
class _DurationRow(QFrame):
    """下拉栏里的一行：左侧时间值容器、右侧 − 号容器（两个独立容器）。"""

    remove_requested = Signal(int)

    def __init__(self, minutes: int, parent=None) -> None:
        super().__init__(parent)
        self.minutes = int(minutes)
        self.setObjectName("DurationRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(config.sx(12), config.sx(6), config.sx(6), config.sx(6))
        layout.setSpacing(config.sx(8))

        self.value_box = QWidget(self)                 # 时间值容器
        value_layout = QHBoxLayout(self.value_box)
        value_layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(tr("notify.duration.chip", minutes=self.minutes), self.value_box)
        label.setObjectName("DurationValue")
        label.setFont(theme.font(config.FONT_BODY))
        value_layout.addWidget(label)
        layout.addWidget(self.value_box, 1)

        self.remove_box = QWidget(self)                # − 号容器
        remove_layout = QHBoxLayout(self.remove_box)
        remove_layout.setContentsMargins(0, 0, 0, 0)
        button = QPushButton(self.remove_box)
        button.setObjectName("DurationRemove")
        button.setFixedSize(config.sx(26), config.sx(26))
        button.setCursor(Qt.PointingHandCursor)
        button.setIcon(vector_icons.icon("minus", config.sx(14), config.C_TEXT_MUTED, 2.0))
        button.setIconSize(QSize(config.sx(14), config.sx(14)))
        button.setToolTip(tr("notify.duration.remove"))
        button.clicked.connect(lambda: self.remove_requested.emit(self.minutes))
        remove_layout.addWidget(button)
        layout.addWidget(self.remove_box, 0)


class _PendingRow(QFrame):
    """待选行：输入框 + "添加"，位于下拉栏最下方（点 + 号后出现）。"""

    confirmed = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("DurationRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(config.sx(10), config.sx(6), config.sx(6), config.sx(6))
        layout.setSpacing(config.sx(8))
        self.spin = QSpinBox(self)
        self.spin.setRange(1, 24 * 60)
        self.spin.setValue(60)
        self.spin.setSuffix(tr("control.minutes_suffix"))
        self.spin.setFixedHeight(config.sx(30))
        layout.addWidget(self.spin, 1)
        self.add_btn = TextButton(tr("notify.duration.confirm"), "primary", height=30,
                                  parent=self)
        self.add_btn.setFixedWidth(config.sx(66))
        self.add_btn.clicked.connect(self._on_confirm)
        layout.addWidget(self.add_btn, 0)
        self.spin.lineEdit().returnPressed.connect(self._on_confirm)

    def _on_confirm(self) -> None:
        self.confirmed.emit(int(self.spin.value()))

    def suggest(self, value: int) -> None:
        self.spin.setValue(int(value))

    def grab(self) -> None:
        self.spin.setFocus()
        self.spin.lineEdit().selectAll()


class _DurationPanel(Panel):
    """"程序追踪时长达到"的内联下拉栏。

    它不是弹窗：就位于标题行下方、属于原容器的一部分，展开时**真实改变容器高度**
    （高度动画见 animate_height）。每行一个已有时间值（右侧 − 可移除）。
    """

    changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent, elevated=True, radius=14)
        self.values: list[int] = []

        box = QVBoxLayout(self)
        box.setContentsMargins(config.sx(14), config.sx(12), config.sx(14), config.sx(12))
        box.setSpacing(config.sx(8))

        title = QLabel(tr("notify.duration.title"), self)
        title.setObjectName("CardTitle")
        title.setFont(theme.font(config.FONT_BODY, bold=True))
        box.addWidget(title)

        self.rows_host = QWidget(self)
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(config.sx(6))
        box.addWidget(self.rows_host)

        self.empty_label = QLabel(tr("notify.duration.empty"), self)
        self.empty_label.setObjectName("DurationEmpty")
        self.empty_label.setFont(theme.font(config.FONT_SMALL))
        box.addWidget(self.empty_label)

        # 待选行：只有点了 + 号才出现，始终只有一行（不会堆积）
        self.pending = _PendingRow(self)
        self.pending.confirmed.connect(self._on_confirm)
        self.pending.setVisible(False)
        box.addWidget(self.pending)

    # ------------------------------------------------------------------
    def set_values(self, values: list) -> None:
        self.values = sorted({int(v) for v in values})
        self._rebuild()

    def show_pending(self) -> None:
        self.pending.setVisible(True)
        self.pending.suggest((self.values[-1] + 15) if self.values else 60)
        self.pending.grab()
        self._sync_empty()

    def _on_confirm(self, minutes: int) -> None:
        minutes = int(minutes)
        if minutes <= 0:
            return
        self.values = sorted(set(self.values + [minutes]))
        self._rebuild()
        self.pending.suggest(minutes + 15)
        self.pending.grab()
        self.changed.emit()

    def _remove(self, minutes: int) -> None:
        self.values = [v for v in self.values if int(v) != int(minutes)]
        self._rebuild()
        self.changed.emit()

    def _sync_empty(self) -> None:
        self.empty_label.setVisible(not self.values and self.pending.isHidden())

    def _rebuild(self) -> None:
        suppress_tooltips()
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        for minutes in self.values:
            row = _DurationRow(minutes, self.rows_host)
            row.remove_requested.connect(self._remove)
            self.rows_layout.addWidget(row)
        self._sync_empty()

class DurationSelector(QWidget):
    """标题与右对齐的 [ + ] [ v ]，下方是**内联**展开的 _DurationPanel。

    * 点 [ v ]：展开 / 收起，展开是真的把所在容器撑高（不是浮在上面的弹窗）；
    * 点 [ + ]：展开下拉栏，并在最下方出现一行待选项（输入分钟数 + 添加）。
    """

    changed = Signal(list)

    def __init__(self, values: list, parent=None) -> None:
        super().__init__(parent)
        self._values = sorted({int(v) for v in values})
        self._expanded = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(config.sx(8))

        head = QHBoxLayout()
        head.setSpacing(config.sx(12))
        label = QLabel(tr("notify.event.duration"), self)
        label.setFont(theme.font(config.FONT_BODY))
        label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        head.addWidget(label, 0)
        head.addStretch(1)

        self.add_btn = QPushButton(self)
        self.add_btn.setObjectName("PlusButton")
        self.add_btn.setFixedSize(config.sx(28), config.sx(28))
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.setIcon(vector_icons.icon("plus", config.sx(15), config.C_TEXT_BODY, 2.0))
        self.add_btn.setIconSize(QSize(config.sx(15), config.sx(15)))
        self.add_btn.setToolTip(tr("notify.duration.add"))
        self.add_btn.clicked.connect(self._on_add)
        head.addWidget(self.add_btn, 0)

        self.toggle_btn = IconButton("chevron_down", parent=self, size=28, icon_size=15, flat=True)
        self.toggle_btn.setObjectName("PlusButton")
        self.toggle_btn.setFixedSize(config.sx(28), config.sx(28))
        self.toggle_btn.setCursor(Qt.PointingHandCursor)
        self.toggle_btn.setIconSize(QSize(config.sx(15), config.sx(15)))
        self.toggle_btn.setToolTip(tr("notify.duration.title"))
        self.toggle_btn.clicked.connect(self._toggle)
        head.addWidget(self.toggle_btn, 0)
        root.addLayout(head)

        self.panel = _DurationPanel(self)
        self.panel.set_values(self._values)
        self.panel.changed.connect(self._on_panel_changed)
        self.panel.setMinimumHeight(0)
        self.panel.setMaximumHeight(0)
        self.panel.setVisible(False)
        root.addWidget(self.panel)
        self._sync_toggle_icon()

    # ------------------------------------------------------------------
    def values(self) -> list:
        return list(self._values)

    def set_values(self, values: list) -> None:
        self._values = sorted({int(v) for v in values})
        self.panel.set_values(self._values)

    def is_expanded(self) -> bool:
        return self._expanded

    def _sync_toggle_icon(self, animate=True) -> None:
        self.toggle_btn.set_expanded(self._expanded, animate=animate and self.isVisible())

    def _on_panel_changed(self) -> None:
        self._values = list(self.panel.values)
        self.changed.emit(self.values())

    def _toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def _on_add(self) -> None:
        """点 + 号：展开下拉栏（真实改变容器高度）并在最下方出现一行待选项。"""
        self.panel.show_pending()
        self.set_expanded(True)
        self.panel.pending.grab()

    def set_expanded(self, expanded: bool, animate: bool = True) -> None:
        expanded = bool(expanded)
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self._sync_toggle_icon(animate)
        if not animate:
            clip_height(self.panel, None if expanded else 0, visible=expanded)
            return
        animate_height(self.panel, expanded)


# --------------------------------------------------------------------------
# 事件勾选组
# --------------------------------------------------------------------------
class _EventSection(QWidget):
    """一组事件勾选项（全局与独立设置共用）。"""

    changed = Signal()

    def __init__(self, settings: dict, scope: str, parent=None,
                 allow_controls: bool = True, allow_password: bool = False) -> None:
        super().__init__(parent)
        self.settings = normalize(settings)
        self.scope = scope
        self.allow_controls = allow_controls

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(config.sx(10))

        grid = QGridLayout()
        grid.setHorizontalSpacing(config.sx(28))
        grid.setVerticalSpacing(config.sx(10))
        self.boxes: dict[str, QCheckBox] = {}
        for row, column, key, label_key in EVENT_LAYOUT:
            box = CheckBox(tr(label_key), self)
            box.setFont(theme.font(config.FONT_BODY))
            box.setChecked(bool(self.settings["events"].get(key)))
            if not allow_controls and key in (EVENT_DAILY_LIMIT, EVENT_UNLOCKED, EVENT_PAUSE):
                box.setEnabled(False)        # 未加入控制页：第三至五项置灰不可勾选
            box.toggled.connect(self._on_changed)
            grid.addWidget(box, row, column)
            self.boxes[key] = box
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        # 第四行：程序追踪时长达到（标题 + 右侧 +/下拉，下拉栏内联展开在下方）
        self.duration_selector = DurationSelector(list(self.settings["durations"]), self)
        self.duration_selector.changed.connect(self._on_durations_changed)
        root.addWidget(self.duration_selector)

        if allow_password:
            box = CheckBox(tr("notify.event.password_changed"), self)
            box.setFont(theme.font(config.FONT_BODY))
            box.setChecked(bool(self.settings["events"].get(EVENT_PASSWORD)))
            box.toggled.connect(self._on_changed)
            root.addWidget(box)
            self.boxes[EVENT_PASSWORD] = box

    # ------------------------------------------------------------------
    def _on_changed(self, *_args) -> None:
        for key, box in self.boxes.items():
            self.settings["events"][key] = bool(box.isChecked())
        self.changed.emit()

    def _on_durations_changed(self, values: list) -> None:
        self.settings["durations"] = list(values)
        self.changed.emit()

    def value(self) -> dict:
        self.settings["durations"] = self.duration_selector.values()
        return normalize(self.settings)


# --------------------------------------------------------------------------
# 各通道的自定义通知内容
# --------------------------------------------------------------------------
class _CustomEditor(QWidget):
    """某个通知通道的"自定义通知内容"：每个通知事件一行输入框。

    入口就在"启用 Windows 通知"/"启用企业微信通知"各自展开的容器里；
    留空表示沿用默认文案，可用占位符见 notify.PLACEHOLDER_HINT。
    """

    changed = Signal()

    def __init__(self, channel: str, custom: dict, parent=None) -> None:
        super().__init__(parent)
        self.channel = channel
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(config.sx(8))

        title = QLabel(tr("notify.custom.title"), self)
        title.setObjectName("CardTitle")
        title.setFont(theme.font(config.FONT_SECTION, bold=True))
        root.addWidget(title)
        hint = QLabel(tr("notify.custom.hint", tokens=PLACEHOLDER_HINT), self)
        hint.setObjectName("Muted")
        hint.setFont(theme.font(config.FONT_TINY))
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.edits: dict[str, QLineEdit] = {}
        stored = dict(custom or {})
        for event in CUSTOM_EVENTS:
            row = QHBoxLayout()
            row.setSpacing(config.sx(12))
            label = QLabel(tr(f"notify.event.{event}"), self)
            label.setFont(theme.font(config.FONT_SMALL))
            label.setFixedWidth(config.sx(200))
            row.addWidget(label, 0)
            edit = QLineEdit(self)
            edit.setText(str(stored.get(event) or ""))
            edit.setPlaceholderText(tr(f"notify.body.{event}"))
            edit.setFixedHeight(config.sx(32))
            edit.editingFinished.connect(self._on_changed)
            row.addWidget(edit, 1)
            root.addLayout(row)
            self.edits[event] = edit

    def _on_changed(self) -> None:
        self.changed.emit()

    def value(self) -> dict:
        return {event: edit.text().strip() for event, edit in self.edits.items()
                if edit.text().strip()}


# --------------------------------------------------------------------------
# 程序选择弹窗
# --------------------------------------------------------------------------
class _ProgramPicker(QDialog):
    """"+"号弹窗：勾选要单独配置通知的程序。"""

    def __init__(self, programs: list, chosen: set, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.selected: list[int] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = Panel(self, elevated=True, radius=16)
        outer.addWidget(panel)
        box = QVBoxLayout(panel)
        box.setContentsMargins(config.sx(22), config.sx(20), config.sx(22), config.sx(18))
        box.setSpacing(config.sx(10))
        title = QLabel(tr("notify.independent.add"), panel)
        title.setObjectName("CardTitle")
        title.setFont(theme.font(config.FONT_CARD_NAME, bold=True))
        box.addWidget(title)
        hint = QLabel(tr("notify.independent.add_hint"), panel)
        hint.setObjectName("Muted")
        hint.setFont(theme.font(config.FONT_SMALL))
        hint.setWordWrap(True)
        box.addWidget(hint)

        self.boxes: list[tuple[int, QCheckBox]] = []
        for program in programs:
            pid = int(program["id"])
            row = QWidget(panel)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(config.sx(10))
            tile = IconTile(30, row)
            tile.setPixmap(icon_extract.pixmap_for_program(
                program.get("icon_path"), str(program.get("path") or ""), config.sx(18)))
            row_layout.addWidget(tile, 0, Qt.AlignVCenter)
            box_row = CheckBox(str(program.get("name") or pid), row)
            box_row.setFont(theme.font(config.FONT_BODY))
            box_row.setChecked(pid in chosen)
            box_row.setEnabled(pid not in chosen)
            row_layout.addWidget(box_row, 1)
            box.addWidget(row)
            self.boxes.append((pid, box_row))

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = TextButton(tr("common.cancel"), "ghost", height=32, parent=panel)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        ok = TextButton(tr("common.confirm"), "primary", height=32, parent=panel)
        ok.clicked.connect(self._on_accept)
        buttons.addWidget(ok)
        box.addLayout(buttons)
        self.setFixedWidth(config.sx(340))

    def _on_accept(self) -> None:
        self.selected = [pid for pid, box in self.boxes if box.isChecked() and box.isEnabled()]
        self.accept()


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
class NotifyPage(QWidget):
    """通知页：右栏配置通知设置。"""

    settings_changed = Signal()
    _test_send_done = Signal(bool)      # 线程池里的发送结果 -> 界面线程改提示文字

    def __init__(self, db, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.db = db
        self._test_send_done.connect(self._on_test_send_done)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.scroll = SlimScrollArea(self)
        self.holder = QWidget()
        self.layout_ = QVBoxLayout(self.holder)
        self.layout_.setContentsMargins(config.sx(24), config.sx(16), config.sx(24), config.sx(20))
        self.layout_.setSpacing(config.sx(14))
        self.scroll.setWidget(self.holder)
        root.addWidget(self.scroll, 1)

        # 拖动排序（独立设置卡片；程序与网站各自一组，不能跨类拖）
        self.reorder = ReorderController(self.scroll, self._commit_order, self)

        # ---------------- 全局通知设置 ----------------
        self.global_card = _Section("bell", tr("notify.global.title"),
                                    tr("notify.global.hint"), self.holder, expanded=True)
        self.global_section = _EventSection(self.db.get_notify_settings(None), "global",
                                            self.global_card, allow_controls=True,
                                            allow_password=True)
        self.global_section.changed.connect(self._save_global)
        self.global_card.content_layout.addWidget(self.global_section)
        self.global_card.content_layout.addWidget(Divider(self.global_card))

        independent_head = QHBoxLayout()
        independent_head.setSpacing(config.sx(10))
        label = QLabel(tr("notify.independent.title"), self.global_card)
        label.setObjectName("CardTitle")
        label.setFont(theme.font(config.FONT_SECTION, bold=True))
        independent_head.addWidget(label)
        independent_head.addStretch(1)
        self.add_program_btn = QPushButton(self.global_card)
        self.add_program_btn.setObjectName("PlusButton")
        self.add_program_btn.setFixedSize(config.sx(28), config.sx(28))
        self.add_program_btn.setCursor(Qt.PointingHandCursor)
        # 用图标而不是文字 "+"（文字在部分缩放下不渲染）
        self.add_program_btn.setIcon(vector_icons.icon("plus", config.sx(15),
                                                      config.C_TEXT_BODY, 2.0))
        self.add_program_btn.setIconSize(QSize(config.sx(15), config.sx(15)))
        self.add_program_btn.setToolTip(tr("notify.independent.add"))
        self.add_program_btn.clicked.connect(self._on_add_program)
        independent_head.addWidget(self.add_program_btn)
        self.global_card.content_layout.addLayout(independent_head)

        self.independent_host = QWidget(self.global_card)
        self.independent_layout = QVBoxLayout(self.independent_host)
        self.independent_layout.setContentsMargins(0, 0, 0, 0)
        self.independent_layout.setSpacing(config.sx(10))
        self.global_card.content_layout.addWidget(self.independent_host)
        self.layout_.addWidget(self.global_card)

        # ---------------- 通知通道 ----------------
        settings = normalize(self.db.get_notify_settings(None))
        self.windows_switch = ToggleSwitch(bool(settings.get("windows")), self.holder)
        self.windows_switch.toggled.connect(self._on_windows_toggled)
        self.windows_card = _Section("monitor", tr("notify.windows.title"),
                                     tr("notify.windows.hint"), self.holder,
                                     trailing=self.windows_switch)
        test_row = QHBoxLayout()
        test_row.setSpacing(config.sx(10))
        self.test_hint = QLabel(tr("notify.test.hint"), self.windows_card)
        self.test_hint.setObjectName("Muted")
        self.test_hint.setFont(theme.font(config.FONT_SMALL))
        test_row.addWidget(self.test_hint, 1)
        self.test_btn = TextButton(tr("notify.test.send"), "ghost", height=32,
                                   parent=self.windows_card)
        self.test_btn.clicked.connect(lambda: self._send_test(CHANNEL_WINDOWS))
        test_row.addWidget(self.test_btn)
        self.windows_card.content_layout.addLayout(test_row)
        self.windows_custom = _CustomEditor(CHANNEL_WINDOWS,
                                            settings["custom"].get(CHANNEL_WINDOWS),
                                            self.windows_card)
        self.windows_custom.changed.connect(self._save_custom)
        self.windows_card.content_layout.addWidget(Divider(self.windows_card))
        self.windows_card.content_layout.addWidget(self.windows_custom)
        self.layout_.addWidget(self.windows_card)

        self.wecom_switch = ToggleSwitch(bool(settings.get("wecom")), self.holder)
        self.wecom_switch.toggled.connect(self._on_wecom_toggled)
        self.wecom_card = _Section("bell", tr("notify.wecom.title"),
                                   tr("notify.wecom.hint"), self.holder,
                                   trailing=self.wecom_switch)
        url_row = QHBoxLayout()
        url_row.setSpacing(config.sx(10))
        self.wecom_url = QLineEdit(str(settings.get("wecom_url") or ""), self.wecom_card)
        self.wecom_url.setPlaceholderText(tr("notify.wecom.placeholder"))
        self.wecom_url.setFixedHeight(config.sx(34))
        self.wecom_url.editingFinished.connect(self._on_wecom_url)
        url_row.addWidget(self.wecom_url, 1)
        self.wecom_test_btn = TextButton(tr("notify.test.send"), "ghost", height=32,
                                         parent=self.wecom_card)
        self.wecom_test_btn.clicked.connect(lambda: self._send_test(CHANNEL_WECOM))
        url_row.addWidget(self.wecom_test_btn, 0)
        self.wecom_card.content_layout.addLayout(url_row)
        self.wecom_custom = _CustomEditor(CHANNEL_WECOM, settings["custom"].get(CHANNEL_WECOM),
                                          self.wecom_card)
        self.wecom_custom.changed.connect(self._save_custom)
        self.wecom_card.content_layout.addWidget(Divider(self.wecom_card))
        self.wecom_card.content_layout.addWidget(self.wecom_custom)
        self.layout_.addWidget(self.wecom_card)
        self.layout_.addStretch(1)

        self._independent_cards: dict[int, _Section] = {}
        self._rebuild_independent()

    # ------------------------------------------------------------------
    def topbar_widget(self) -> Optional[QWidget]:
        return None

    # ------------------------------------------------------------------
    def _save_global(self) -> None:
        """只写回事件勾选与时长阈值。

        不能用 ``self.global_section.value()`` 整块覆盖：它是页面构建时的快照，
        会把之后改过的通道开关（Windows / 企业微信）与自定义内容一起写回旧值，
        导致"关掉 Windows 通知却仍收到通知"。
        """
        settings = normalize(self.db.get_notify_settings(None))
        section = self.global_section.value()
        settings["events"] = section["events"]
        settings["durations"] = section["durations"]
        self.db.set_notify_settings(settings, None)
        self.global_section.settings = normalize(settings)
        self.settings_changed.emit()

    def _on_windows_toggled(self, checked: bool) -> None:
        settings = normalize(self.db.get_notify_settings(None))
        settings["windows"] = bool(checked)
        self.db.set_notify_settings(settings, None)
        self.settings_changed.emit()

    def _on_wecom_toggled(self, checked: bool) -> None:
        settings = normalize(self.db.get_notify_settings(None))
        settings["wecom"] = bool(checked)
        if checked:
            settings["wecom_url"] = self.wecom_url.text().strip()
        self.db.set_notify_settings(settings, None)
        self.settings_changed.emit()

    def _on_wecom_url(self) -> None:
        settings = normalize(self.db.get_notify_settings(None))
        settings["wecom_url"] = self.wecom_url.text().strip()
        self.db.set_notify_settings(settings, None)
        self.settings_changed.emit()

    def _save_custom(self) -> None:
        settings = normalize(self.db.get_notify_settings(None))
        settings["custom"] = {
            CHANNEL_WINDOWS: self.windows_custom.value(),
            CHANNEL_WECOM: self.wecom_custom.value(),
        }
        self.db.set_notify_settings(settings, None)
        self.settings_changed.emit()

    def _send_test(self, channel: str) -> None:
        """测试按钮只走它自己所属的通道（以前两个按钮会把两个通道都发一遍）。"""
        from ..notify import Notifier

        settings = normalize(self.db.get_notify_settings(None))
        if channel == CHANNEL_WINDOWS:
            if not settings.get("windows"):
                self.test_hint.setText(tr("notify.test.need_windows"))
                return
            notifier = Notifier(self.db, getattr(self.window(), "tray_icon", None))
            ok = notifier.notify("test", tr("notify.title"), tr("notify.test.body"),
                                 force=True, channels=(CHANNEL_WINDOWS,))
            self.test_hint.setText(tr("notify.test.sent") if ok else tr("notify.test.failed"))
            return
        if not (settings.get("wecom") and settings.get("wecom_url")):
            self.test_hint.setText(tr("notify.test.need_wecom"))
            return
        self._send_test_wecom(settings["wecom_url"])

    def _send_test_wecom(self, url: str) -> None:
        """企业微信测试发送：异步，结果回来再改提示文字。

        以前这里是同步的：webhook 不通时要等满 6 秒超时，界面整段时间没有响应。
        发送在线程池里，结果通过页面自己的信号回到界面线程（跨线程改动控件不安全）。
        """
        from ..notify import Notifier

        self.test_hint.setText(tr("notify.test.sending"))
        notifier = Notifier(self.db, getattr(self.window(), "tray_icon", None))
        notifier._send_wecom(url, tr("notify.title"), tr("notify.test.body"),
                             on_done=self._test_send_done.emit)

    @Slot(bool)
    def _on_test_send_done(self, ok: bool) -> None:  # noqa: D102
        self.test_hint.setText(tr("notify.test.sent") if ok else tr("notify.test.failed"))

    # ------------------------------------------------------------------
    def _on_add_program(self) -> None:
        programs = [dict(p) for p in self.db.programs(visible_only=True)]
        chosen = set(self._independent_cards)
        picker = _ProgramPicker(programs, chosen, self.window())
        if picker.exec() != QDialog.Accepted:
            return
        for pid in picker.selected:
            self.db.set_notify_settings(normalize(None), pid)
        self._rebuild_independent()
        self.settings_changed.emit()

    def _remove_independent(self, program_id: int) -> None:
        # 必须整行删除：空值记录仍会让它出现在列表里
        self.db.delete_setting(self.db.notify_key(program_id))
        self._rebuild_independent()
        self.settings_changed.emit()

    # ------------------------------------------------------------------
    @staticmethod
    def _order_key(kind: str) -> str:
        return f"order.notify.{kind}"

    def _order_items(self, entries: list, kind: str) -> list:
        """按拖动保存的顺序排（没记录过的程序按数据库顺序追加在后面）。"""
        order = self.db.get_order(self._order_key(kind))
        if not order:
            return entries
        rank = {int(pid): index for index, pid in enumerate(order)}
        return sorted(entries, key=lambda item: rank.get(int(item[0]), len(rank)))

    def _commit_order(self, kind: str, ids: list) -> None:
        self.db.set_order(self._order_key(kind), ids)

    def _rebuild_independent(self) -> None:
        suppress_tooltips()
        self.reorder.clear()           # 旧卡片马上销毁，先摘掉拖动控制器里的引用
        while self.independent_layout.count():
            item = self.independent_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()          # 先隐藏：可见控件脱离父级会变成顶层窗口
                widget.setParent(None)
                widget.deleteLater()
        self._independent_cards.clear()

        programs = {int(p["id"]): dict(p) for p in self.db.programs(visible_only=True)}
        rules = {int(pid) for pid in self.db.all_control_rules()}
        groups: dict[str, list] = {"program": [], "site": []}
        for program_id in self.db.all_notify_program_ids():
            program = programs.get(int(program_id))
            if program is None:
                continue                       # 程序已从首页删除 -> 不再显示独立设置
            groups["site" if is_site(program) else "program"].append((int(program_id), program))

        cards: dict[str, list] = {"program": [], "site": []}
        for kind in ("program", "site"):
            for program_id, program in self._order_items(groups[kind], kind):
                card = _Section("bell", tr("notify.independent.item",
                                           name=str(program.get("name") or program_id)),
                                tr("notify.independent.item_hint"), self.independent_host,
                                expanded=False, icon_path=str(program.get("icon_path") or ""),
                                icon_exe=str(program.get("path") or ""), handle=True)
                card.program_id = program_id          # 拖动排序要按它找回顺序
                in_control = program_id in rules
                section = _EventSection(self.db.get_notify_settings(program_id), "program",
                                        card, allow_controls=in_control, allow_password=False)
                section.changed.connect(
                    lambda pid=program_id, sec=section: self._save_program(pid, sec))
                card.content_layout.addWidget(section)
                if not in_control:
                    note = QLabel(tr("notify.independent.not_in_control"), card)
                    note.setObjectName("Muted")
                    note.setFont(theme.font(config.FONT_TINY))
                    note.setWordWrap(True)
                    card.content_layout.addWidget(note)
                remove = TextButton(tr("notify.independent.remove"), "danger-ghost", height=32,
                                    parent=card)
                remove.clicked.connect(
                    lambda _=False, pid=program_id: self._remove_independent(pid))
                row = QHBoxLayout()
                row.addStretch(1)
                row.addWidget(remove)
                card.content_layout.addLayout(row)
                self.independent_layout.addWidget(card)
                anim.fade_in(card, config.ANIM_FAST)
                self._independent_cards[program_id] = card
                cards[kind].append(card)

        for kind, group_cards in cards.items():
            self.reorder.register(kind, group_cards)

    def _save_program(self, program_id: int, section: _EventSection) -> None:
        """只写回事件勾选与时长阈值，通道配置与自定义内容仍取全局。"""
        stored = normalize(self.db.get_notify_settings(int(program_id)))
        section_value = section.value()
        stored["events"] = section_value["events"]
        stored["durations"] = section_value["durations"]
        self.db.set_notify_settings(stored, int(program_id))
        section.settings = normalize(stored)
        self.settings_changed.emit()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """外部（例如程序被删除）触发的刷新。"""
        self._rebuild_independent()
