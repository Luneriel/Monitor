"""控制页：密码验证 + 按程序配置使用限制规则。

流程：
    * 未设置密码 → 引导设置密码；已设置 → 先验证（两次进入间隔 ≤ 1 分钟免验证）；
    * 通过后显示"启用控制"总开关（关闭需密码）与"修改密码"；
    * 程序使用限制：规则卡片**只显示手动添加过的程序**（右上角 + 添加程序）。
      卡片默认收起，标题行常显"名称 + 规则摘要 + 实时状态"，点标题行或箭头展开设置；
      设置里有"每日使用上限 / 连续使用强制暂停 / 指定时间段无法使用"三种规则。
    * 卡片左侧手柄可拖动排序（程序与网站各自一组，不能跨类拖），拖到上下边缘自动滚动，
      顺序保存在数据库里（settings 表的 order.control.program / order.control.site）。
"""

from __future__ import annotations

import math
import time

from typing import Optional

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QSizePolicy, QSpinBox,
                               QVBoxLayout, QWidget)

from .. import config, util
from ..control import (ALL_DAYS, DAY_KEYS, UNIT_MAX, Rule, Window, days_label_key,
                       format_minute, unit_seconds)
from ..i18n import format_amount, tr
from . import anim, theme, vector_icons
from .tooltip_guard import suppress_tooltips
from .cards import is_site, program_icon
from .popups import SelectBox
from .reorder import ReorderController
from .controls import (CheckBox, Divider, IconButton, IconTile, Panel, SlimScrollArea, TextButton,
                       ToggleSwitch)

PASSWORD_MIN = 4
UNLOCK_PRESETS = (10, 30, 60)


# --------------------------------------------------------------------------
# 密码 / 解锁时长弹窗
# --------------------------------------------------------------------------
class _PasswordDialog(QDialog):
    """统一的密码输入弹窗（可带第二输入框用于设置/确认，或时长选择用于临时解锁）。"""

    def __init__(self, parent: Optional[QWidget], title: str, hint: str,
                 confirm: bool = False, minutes: bool = False,
                 preset_minutes: Optional[list] = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self._minutes = minutes
        self.value_minutes = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.panel = Panel(self, elevated=True, radius=16)
        outer.addWidget(self.panel)

        box = QVBoxLayout(self.panel)
        box.setContentsMargins(config.sx(24), config.sx(22), config.sx(24), config.sx(20))
        box.setSpacing(config.sx(12))

        label = QLabel(title, self.panel)
        label.setObjectName("CardTitle")
        label.setFont(theme.font(config.FONT_CARD_NAME, bold=True))
        box.addWidget(label)

        sub = QLabel(hint, self.panel)
        sub.setObjectName("Muted")
        sub.setFont(theme.font(config.FONT_SMALL))
        sub.setWordWrap(True)
        box.addWidget(sub)

        self.edit = QLineEdit(self.panel)
        self.edit.setEchoMode(QLineEdit.Password)
        self.edit.setFixedHeight(config.sx(36))
        box.addWidget(self.edit)

        self.edit2: Optional[QLineEdit] = None
        if confirm:
            self.edit2 = QLineEdit(self.panel)
            self.edit2.setEchoMode(QLineEdit.Password)
            self.edit2.setPlaceholderText(tr("control.setup.confirm_placeholder"))
            self.edit2.setFixedHeight(config.sx(36))
            box.addWidget(self.edit2)

        self.custom: Optional[QSpinBox] = None
        if minutes:
            preset_row = QHBoxLayout()
            preset_row.setSpacing(config.sx(8))
            for value in (preset_minutes or list(UNLOCK_PRESETS)):
                button = TextButton(tr("control.unlock.preset", minutes=value), "ghost",
                                    height=32, parent=self.panel)
                button.setAutoDefault(False)
                button.clicked.connect(lambda _=False, v=value: self._accept_minutes(v))
                preset_row.addWidget(button)
            preset_row.addStretch(1)
            box.addLayout(preset_row)
            self.custom = QSpinBox(self.panel)
            self.custom.setRange(1, 24 * 60)
            self.custom.setValue(30)
            self.custom.setSuffix(tr("control.minutes_suffix"))
            self.custom.setFixedHeight(config.sx(34))
            box.addWidget(self.custom)

        # 预留一行高度：错误提示出现时不会把下面的按钮顶下去
        self.error = QLabel(" ", self.panel)
        self.error.setObjectName("DangerText")
        self.error.setFont(theme.font(config.FONT_SMALL))
        self.error.setFixedHeight(config.sx(18))
        box.addWidget(self.error)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = TextButton(tr("common.cancel"), "ghost", height=34, parent=self.panel)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        ok = TextButton(tr("common.confirm"), "primary", height=34, parent=self.panel)
        ok.clicked.connect(self._on_accept)
        ok.setDefault(True)
        ok.setAutoDefault(True)
        buttons.addWidget(ok)
        # 回车直接确认
        self.edit.returnPressed.connect(self._on_accept)
        if self.edit2 is not None:
            self.edit2.returnPressed.connect(self._on_accept)
        if self.custom is not None:
            self.custom.lineEdit().returnPressed.connect(self._on_accept)
        box.addLayout(buttons)

        self.setFixedWidth(config.sx(400))

    def _accept_minutes(self, value: int) -> None:
        """预设时长只填入数值（不提交）：提交必须经过密码验证，否则会被当成密码错误。"""
        self.value_minutes = int(value)
        if self.custom is not None:
            self.custom.setValue(int(value))
        self.edit.setFocus()

    def _on_accept(self) -> None:
        if self._minutes:
            # 解锁也要先填密码：否则反复点"×分钟/确认"会拿空密码去校验，
            # 三次就触发密码锁定，用户看到的是"临时解锁按钮失灵"。
            text = self.edit.text()
            if len(text) < PASSWORD_MIN:
                self.error.setText(tr("control.error.too_short"))
                self.edit.setFocus()
                return
            self.value_minutes = int(self.custom.value()) if self.custom else 30
            self.accept()
            return
        text = self.edit.text()
        if len(text) < PASSWORD_MIN:
            self.error.setText(tr("control.error.too_short"))
            return
        if self.edit2 is not None and text != self.edit2.text():
            self.error.setText(tr("control.error.mismatch"))
            return
        self.accept()

    def password(self) -> str:
        return self.edit.text()

    def set_error(self, text: str) -> None:
        self.error.setText(text)


def ask_exit_password(parent: Optional[QWidget], service,
                      key: str = "control.quit.need_password") -> bool:
    """退出程序 / 提权重启之前验一次密码：返回 True 表示放行。

    没设过密码（= 没人用过控制功能）时直接放行；密码锁定期间不弹框，只提示剩余秒数。
    用户的 ⑤ 口径是"每次都问"，所以这里不做"刚验证过就放行"的豁免。
    """
    if service is None or not service.has_password():
        return True
    remaining = service.lock_remaining()
    if remaining > 0:
        QMessageBox.information(parent, tr("control.verify.title"),
                                tr("control.gate.locked", seconds=int(remaining)))
        return False
    dialog = _PasswordDialog(parent, tr("control.verify.title"), tr(key))
    while dialog.exec() == QDialog.Accepted:
        ok, reason = service.verify(dialog.password())
        if ok:
            return True
        if reason == "locked":
            QMessageBox.information(parent, tr("control.verify.title"),
                                    tr("control.gate.locked", seconds=60))
            return False
        dialog.set_error(tr("control.error.wrong"))
        dialog.edit.clear()
    return False


class _ProgramPicker(QDialog):
    """控制页的"添加程序"弹窗。"""

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
        box.setContentsMargins(config.sx(24), config.sx(22), config.sx(24), config.sx(20))
        box.setSpacing(config.sx(10))
        title = QLabel(tr("control.add_program"), panel)
        title.setObjectName("CardTitle")
        title.setFont(theme.font(config.FONT_CARD_NAME, bold=True))
        box.addWidget(title)
        hint = QLabel(tr("control.add_program.hint"), panel)
        hint.setObjectName("Muted")
        hint.setFont(theme.font(config.FONT_SMALL))
        hint.setWordWrap(True)
        box.addWidget(hint)

        from PySide6.QtWidgets import QCheckBox

        self.boxes: list[tuple[int, QCheckBox]] = []
        if not programs:
            empty = QLabel(tr("control.rules.empty"), panel)
            empty.setObjectName("Muted")
            empty.setFont(theme.font(config.FONT_SMALL))
            box.addWidget(empty)
        groups: dict[str, list] = {"program": [], "site": []}
        for program in programs:
            groups["site" if is_site(program) else "program"].append(program)
        both = bool(groups["program"]) and bool(groups["site"])
        for kind in ("program", "site"):
            if both:
                section = theme.label(tr(f"control.section.{kind}s"), config.FONT_SMALL, True,
                                      config.C_TEXT_MUTED, panel)
                box.addWidget(section)
            for program in groups[kind]:
                pid = int(program["id"])
                already = pid in chosen
                row = QWidget(panel)
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(config.sx(10))
                tile = IconTile(30, row)
                tile.setPixmap(program_icon(program, config.sx(18)))
                row_layout.addWidget(tile, 0, Qt.AlignVCenter)
                box_row = QCheckBox(str(program.get("name") or pid), row)
                box_row.setFont(theme.font(config.FONT_BODY))
                box_row.setEnabled(not already)
                box_row.setChecked(already)
                row_layout.addWidget(box_row, 1)
                box.addWidget(row)
                self.boxes.append((pid, box_row))

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = TextButton(tr("common.cancel"), "ghost", height=34, parent=panel)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        ok = TextButton(tr("common.confirm"), "primary", height=34, parent=panel)
        ok.clicked.connect(self._on_accept)
        buttons.addWidget(ok)
        box.addLayout(buttons)
        self.setFixedWidth(config.sx(360))

    def _on_accept(self) -> None:
        self.selected = [pid for pid, box in self.boxes if box.isChecked() and box.isEnabled()]
        self.accept()


# --------------------------------------------------------------------------
# 单个程序的规则卡
# --------------------------------------------------------------------------
class _WindowRow(QWidget):
    """一条"指定时间段"编辑行：起止时间（时 : 分 下拉）+ 星期勾选。"""

    removed = Signal(object)          # 参数是自己

    def __init__(self, window: Window, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._window = window
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(config.sx(8))

        line = QHBoxLayout()
        line.setSpacing(config.sx(4))
        line.addWidget(self._text(tr("control.window.start")))
        self.start_hour = self._time_box(window.start_minute // 60, 24)
        self.start_min_box = self._time_box(window.start_minute % 60, 60)
        line.addWidget(self.start_hour)
        line.addWidget(self._colon())
        line.addWidget(self.start_min_box)
        line.addSpacing(config.sx(10))
        line.addWidget(self._text(tr("control.window.end")))
        self.end_hour = self._time_box(window.end_minute // 60, 24)
        self.end_min_box = self._time_box(window.end_minute % 60, 60)
        line.addWidget(self.end_hour)
        line.addWidget(self._colon())
        line.addWidget(self.end_min_box)
        self.tag = QLabel("", self)
        self.tag.setObjectName("Muted")
        self.tag.setFont(theme.font(config.FONT_SMALL))
        line.addSpacing(config.sx(8))
        line.addWidget(self.tag)
        line.addStretch(1)
        self.remove_btn = IconButton("close", tr("control.window.remove"), size=28, icon_size=14,
                                     color=config.C_TEXT_MUTED, parent=self, flat=True)
        self.remove_btn.clicked.connect(lambda: self.removed.emit(self))
        line.addWidget(self.remove_btn, 0, Qt.AlignVCenter)
        box.addLayout(line)

        repeat = QHBoxLayout()
        repeat.setSpacing(config.sx(8))
        repeat.addWidget(self._text(tr("control.window.repeat")))
        self.day_boxes: list = []
        for index in range(7):
            day_box = CheckBox(tr(DAY_KEYS[index]), self)
            day_box.setFont(theme.font(config.FONT_SMALL))
            day_box.setChecked(bool(window.days >> index & 1))
            day_box.toggled.connect(self._sync_tag)
            self.day_boxes.append(day_box)
            repeat.addWidget(day_box)
        repeat.addStretch(1)
        box.addLayout(repeat)

        for combo in (self.start_hour, self.start_min_box, self.end_hour, self.end_min_box):
            combo.current_changed.connect(self._sync_tag)
        self._sync_tag()

    # ------------------------------------------------------------------
    @staticmethod
    def _text(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Muted")
        label.setFont(theme.font(config.FONT_SMALL))
        return label

    def _colon(self) -> QLabel:
        label = QLabel(":", self)
        label.setFont(theme.font(config.FONT_SMALL))
        return label

    @staticmethod
    def _time_box(value: int, count: int) -> SelectBox:
        """00–23 / 00–59 的下拉（选项多，SelectBox 会自动给弹层限高滚动）。"""
        options = [(f"{index:02d}", f"{index:02d}") for index in range(count)]
        current = f"{max(0, min(int(value or 0), count - 1)):02d}"
        combo = SelectBox(options, current=current, min_width=64)
        combo.setFixedWidth(config.sx(64))
        return combo

    @staticmethod
    def _minutes(hour: SelectBox, minute: SelectBox) -> int:
        try:
            return int(hour.current_key()) * 60 + int(minute.current_key())
        except (TypeError, ValueError):
            return 0

    def value(self) -> Window:
        """把界面上的选择收成一个 Window。"""
        days = 0
        for index, day_box in enumerate(self.day_boxes):
            if day_box.isChecked():
                days |= 1 << index
        return Window(start_minute=self._minutes(self.start_hour, self.start_min_box),
                      end_minute=self._minutes(self.end_hour, self.end_min_box),
                      days=days)

    def _sync_tag(self, *_args) -> None:
        """跨午夜的时段在行尾标一下，避免用户以为填错了。"""
        self.tag.setText(tr("control.window.overnight") if self.value().crosses_midnight else "")


class _ElidedLabel(QLabel):
    """标题行里的单行标签：宽度不够时用省略号收尾，完整内容放进悬停提示。

    规则配得多时摘要/状态会很长，普通 QLabel 会按文本算出很大的最小宽度，
    于是把卡片撑出可视区域（右侧被硬裁断）。这里把宽度策略设成 Ignored，
    让布局可以自由压缩它，再在 `resizeEvent` 里按当前宽度重新省略。
    """

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(config.sx(48))
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = str(text or "")
        super().setToolTip(self._full)
        self._elide()

    def full_text(self) -> str:
        """未省略的完整文本（自检与 tooltip 用）。"""
        return self._full

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        super().setText(theme.elide(self._full, self.font(), self.width()))


class _RuleCard(Panel):
    """一个程序 / 网站的规则卡。

    标题行常显：名称 + 规则摘要 + 实时状态 + 拖动手柄 + 展开箭头 + 删除；
    设置（每日上限 / 连续暂停 / 指定时间段）收在可折叠的内容区里，默认收起。
    """

    changed = Signal()
    removed = Signal(int)
    expand_changed = Signal(int, bool)

    def __init__(self, program: dict, rule: Rule, service, parent=None,
                 expanded: bool = False) -> None:
        super().__init__(parent, elevated=True, radius=16)
        self.program = dict(program)
        self.program_id = int(program["id"])
        self.service = service
        self.rule = rule
        self._expanded = bool(expanded)
        self._window_rows: list = []

        root = QVBoxLayout(self)
        root.setContentsMargins(config.sx(20), config.sx(14), config.sx(20), config.sx(14))
        root.setSpacing(config.sx(12))

        # ---------------------------------------------------------- 标题行（常显）
        self.head = QWidget(self)
        head = QHBoxLayout(self.head)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(config.sx(12))

        # 拖动手柄：只有它响应拖动，卡片其它位置照常点击
        self.drag_handle = IconButton("grip", tr("common.drag_handle"), size=28, icon_size=16,
                                      color=config.C_TEXT_MUTED, parent=self.head, flat=True)
        head.addWidget(self.drag_handle, 0, Qt.AlignVCenter)

        tile = IconTile(42, self.head)
        tile.setPixmap(program_icon(program, config.sx(26)))
        head.addWidget(tile, 0, Qt.AlignVCenter)

        texts = QVBoxLayout()
        texts.setSpacing(config.sx(2))
        self.name_label = QLabel(str(program.get("name") or ""), self.head)
        self.name_label.setObjectName("CardTitle")
        self.name_label.setFont(theme.font(config.FONT_CARD_NAME, bold=True))
        self.name_label.setToolTip(str(program.get("path") or ""))
        texts.addWidget(self.name_label)
        self.summary = _ElidedLabel("", self.head)
        self.summary.setObjectName("Muted")
        self.summary.setFont(theme.font(config.FONT_SMALL))
        texts.addWidget(self.summary)
        self.status = _ElidedLabel("", self.head)
        self.status.setObjectName("Muted")
        self.status.setFont(theme.font(config.FONT_SMALL))
        texts.addWidget(self.status)
        head.addLayout(texts, 1)

        self.arrow = IconButton("chevron_down" if self._expanded else "chevron_right",
                                tr("control.rule.collapse") if self._expanded
                                else tr("control.rule.expand"),
                                size=30, icon_size=16,
                                color=config.C_TEXT_MUTED, parent=self.head, flat=True)
        self.arrow.set_expanded(self._expanded, animate=False, right=True)
        self.arrow.clicked.connect(lambda: self.set_expanded(not self._expanded))
        head.addWidget(self.arrow, 0, Qt.AlignVCenter)

        self.remove_btn = TextButton(tr("control.rule.remove"), "danger-ghost", height=32,
                                     parent=self.head)
        self.remove_btn.clicked.connect(lambda: self.removed.emit(self.program_id))
        head.addWidget(self.remove_btn, 0, Qt.AlignVCenter)
        root.addWidget(self.head)

        # ---------------------------------------------------------- 内容区（默认收起）
        self.content = QWidget(self)
        content_box = QVBoxLayout(self.content)
        content_box.setContentsMargins(0, 0, 0, 0)
        content_box.setSpacing(config.sx(12))

        path = QLabel(util.shorten_path(str(program.get("path") or ""), 84), self.content)
        path.setObjectName("Muted")
        path.setFont(theme.font(config.FONT_SMALL))
        path.setToolTip(str(program.get("path") or ""))
        content_box.addWidget(path)
        content_box.addWidget(Divider(self.content))

        grid = QGridLayout()
        grid.setHorizontalSpacing(config.sx(14))
        grid.setVerticalSpacing(config.sx(12))
        grid.setColumnMinimumWidth(0, config.sx(150))

        self.daily_switch = ToggleSwitch(rule.daily_enabled, self.content)
        self.daily_spin = self._spin(rule.daily_value, self.content)
        self.daily_unit = self._unit_box(rule.daily_unit, self.daily_spin, self.content)
        grid.addWidget(self._label(tr("control.rule.daily"), self.content), 0, 0)
        grid.addWidget(self.daily_switch, 0, 1)
        grid.addWidget(self.daily_spin, 0, 2)
        grid.addWidget(self.daily_unit, 0, 3)

        self.streak_switch = ToggleSwitch(rule.streak_enabled, self.content)
        self.streak_spin = self._spin(rule.streak_value, self.content)
        self.streak_unit = self._unit_box(rule.streak_unit, self.streak_spin, self.content)
        grid.addWidget(self._label(tr("control.rule.streak_x"), self.content), 1, 0)
        grid.addWidget(self.streak_switch, 1, 1)
        grid.addWidget(self.streak_spin, 1, 2)
        grid.addWidget(self.streak_unit, 1, 3)

        self.pause_spin = self._spin(rule.pause_value, self.content)
        self.pause_unit = self._unit_box(rule.pause_unit, self.pause_spin, self.content)
        grid.addWidget(self._label(tr("control.rule.streak_y"), self.content), 2, 0)
        spacer = QWidget(self.content)
        spacer.setFixedWidth(config.sx(46))
        grid.addWidget(spacer, 2, 1)
        grid.addWidget(self.pause_spin, 2, 2)
        grid.addWidget(self.pause_unit, 2, 3)
        grid.setColumnStretch(4, 1)

        self.save_btn = TextButton(tr("control.rule.save"), "primary", height=34,
                                   parent=self.content)
        self.save_btn.clicked.connect(self._save)
        grid.addWidget(self.save_btn, 0, 4, 1, 1, Qt.AlignRight | Qt.AlignTop)
        content_box.addLayout(grid)

        # ---------------------------------------------------------- 指定时间段
        content_box.addWidget(Divider(self.content))
        window_head = QHBoxLayout()
        window_head.setSpacing(config.sx(12))
        window_title = QLabel(tr("control.rule.window"), self.content)
        window_title.setFont(theme.font(config.FONT_BODY))
        window_head.addWidget(window_title)
        self.window_switch = ToggleSwitch(rule.window_enabled, self.content)
        window_head.addWidget(self.window_switch)
        window_head.addStretch(1)
        content_box.addLayout(window_head)

        window_hint = QLabel(tr("control.rule.window.hint"), self.content)
        window_hint.setObjectName("Muted")
        window_hint.setFont(theme.font(config.FONT_SMALL))
        window_hint.setWordWrap(True)
        content_box.addWidget(window_hint)

        self.window_host = QWidget(self.content)
        self.window_layout = QVBoxLayout(self.window_host)
        self.window_layout.setContentsMargins(0, 0, 0, 0)
        self.window_layout.setSpacing(config.sx(10))
        content_box.addWidget(self.window_host)

        self.window_empty = QLabel(tr("control.window.empty"), self.content)
        self.window_empty.setObjectName("Muted")
        self.window_empty.setFont(theme.font(config.FONT_SMALL))
        content_box.addWidget(self.window_empty)

        window_add_row = QHBoxLayout()
        self.window_add = TextButton(tr("control.window.add"), "ghost", height=32,
                                     icon_name="plus", icon_size=14, parent=self.content)
        self.window_add.clicked.connect(lambda: self._add_window())
        window_add_row.addWidget(self.window_add)
        window_add_row.addStretch(1)
        content_box.addLayout(window_add_row)

        root.addWidget(self.content)

        for window in rule.windows:
            self._add_window(window, sync=False)
        for switch in (self.daily_switch, self.streak_switch, self.window_switch):
            switch.toggled.connect(self._sync_enabled)
        self._sync_enabled()
        self._sync_windows()
        if self._expanded:
            anim.clip_height(self.content, None, visible=True)     # 直接以展开态出现
        else:
            self.content.setMaximumHeight(0)
            self.content.setVisible(False)      # 收起：clip_holder 会按这个状态建容器
        self.refresh_status()

    # ------------------------------------------------------------------
    @staticmethod
    def _label(text: str, parent: QWidget) -> QLabel:
        label = QLabel(text, parent)
        label.setFont(theme.font(config.FONT_BODY))
        label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        return label

    @staticmethod
    def _spin(value: int, parent: QWidget) -> QSpinBox:
        spin = QSpinBox(parent)
        spin.setRange(1, UNIT_MAX["m"])
        spin.setValue(max(1, int(value or 1)))
        spin.setFixedHeight(config.sx(34))
        spin.setFixedWidth(config.sx(110))
        return spin

    def _unit_box(self, unit: str, spin: QSpinBox, parent: QWidget) -> SelectBox:
        """单位下拉（小时 / 分钟 / 秒）。切换时只在"能整除"的情况下换算数值。"""
        options = [(code, tr(key)) for code, key in (("h", "unit.h"), ("m", "unit.m"), ("s", "unit.s"))]
        box = SelectBox(options, current=str(unit or "m"), parent=parent, min_width=92)
        box.setFixedWidth(config.sx(92))
        box.current_changed.connect(lambda code, s=spin, b=box: self._on_unit_changed(s, b, code))
        spin._unit = str(unit or "m")
        spin.setRange(1, UNIT_MAX.get(str(unit or "m"), UNIT_MAX["m"]))
        return box

    @staticmethod
    def _on_unit_changed(spin: QSpinBox, box: SelectBox, unit: str) -> None:
        """单位切换：老的数值按新单位的最大刻度收进范围；能整除时换算成等值数值。

        例：60（分钟）-> 1（小时）；90（分钟）-> 保留 90（小时）由用户自己确认与保存。
        """
        old_unit = getattr(spin, "_unit", "m")
        value = int(spin.value())
        factor_old = unit_seconds(old_unit)
        factor_new = unit_seconds(unit)
        if factor_new and factor_old >= factor_new and factor_old % factor_new == 0:
            value = max(1, value * factor_old // factor_new)          # 大单位 -> 小单位
        elif factor_old and factor_new > factor_old and value * factor_old % factor_new == 0:
            value = max(1, value * factor_old // factor_new)          # 小单位 -> 大单位（整除才换算）
        spin._unit = unit
        spin.setRange(1, UNIT_MAX.get(unit, UNIT_MAX["m"]))
        spin.setValue(max(1, min(int(value), UNIT_MAX.get(unit, UNIT_MAX["m"]))))

    def _sync_enabled(self, *_args) -> None:
        daily = self.daily_switch.isChecked()
        streak = self.streak_switch.isChecked()
        # 关闭开关后与其无关的输入框不可编辑
        self.daily_spin.setEnabled(daily)
        self.daily_unit.setEnabled(daily)
        self.streak_spin.setEnabled(streak)
        self.streak_unit.setEnabled(streak)
        self.pause_spin.setEnabled(streak)
        self.pause_unit.setEnabled(streak)
        self._sync_windows()

    def _sync_windows(self) -> None:
        enabled = self.window_switch.isChecked()
        self.window_add.setEnabled(enabled)
        self.window_empty.setVisible(enabled and not self._window_rows)
        for row in self._window_rows:
            row.setEnabled(enabled)

    def _add_window(self, window: Optional[Window] = None, sync: bool = True) -> None:
        row = _WindowRow(window or Window(8 * 60, 12 * 60, ALL_DAYS), self.window_host)
        row.removed.connect(self._remove_window)
        self.window_layout.addWidget(row)
        self._window_rows.append(row)
        if sync:
            self._sync_windows()

    def _remove_window(self, row) -> None:
        if row in self._window_rows:
            self._window_rows.remove(row)
            self.window_layout.removeWidget(row)
            row.hide()               # 先隐藏：可见控件脱离父级会变成顶层窗口
            row.setParent(None)
            row.deleteLater()
        self._sync_windows()

    # ------------------------------------------------------------------
    def set_expanded(self, expanded: bool, animate: bool = True) -> None:
        expanded = bool(expanded)
        changed = expanded != self._expanded
        self._expanded = expanded
        self.arrow.set_expanded(expanded, animate=animate, right=True)
        self.arrow.setToolTip(tr("control.rule.collapse") if expanded
                              else tr("control.rule.expand"))
        if animate:
            anim.animate_height(self.content, expanded)
        else:
            anim.clip_height(self.content, None if expanded else 0, visible=expanded)
        if changed:
            self.expand_changed.emit(self.program_id, expanded)

    def mouseReleaseEvent(self, event) -> None:              # noqa: N802 - Qt 命名
        """点标题行（手柄 / 箭头 / 删除按钮以外的位置）= 展开 / 收起。"""
        if event.button() == Qt.LeftButton and self.head.geometry().contains(
                event.position().toPoint()):
            self.set_expanded(not self._expanded)
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------------
    def _save(self) -> None:
        rule = Rule(
            program_id=self.program_id,
            daily_enabled=bool(self.daily_switch.isChecked()),
            daily_value=int(self.daily_spin.value()),
            daily_unit=str(self.daily_unit.current_key() or "m"),
            streak_enabled=bool(self.streak_switch.isChecked()),
            streak_value=int(self.streak_spin.value()),
            streak_unit=str(self.streak_unit.current_key() or "m"),
            pause_value=int(self.pause_spin.value()),
            pause_unit=str(self.pause_unit.current_key() or "m"),
            window_enabled=bool(self.window_switch.isChecked()),
            windows=[row.value() for row in self._window_rows],
        )
        self.rule = rule
        self.service.save_rule(rule)
        self.refresh_status()
        self.changed.emit()

    # ------------------------------------------------------------------
    @staticmethod
    def days_text(days: int) -> str:
        """星期掩码 -> "每天 / 周一至周五 / 周末 / 一、三" 这类短文案。"""
        days = int(days or 0)
        key = days_label_key(days)
        if key != "control.days.custom":
            return tr(key)
        names = [tr(DAY_KEYS[index]) for index in range(7) if days >> index & 1]
        return tr(key, days=tr("common.list_sep").join(names))

    @classmethod
    def window_text(cls, window: Window) -> str:
        return tr("control.summary.window_range", start=format_minute(window.start_minute),
                  end=format_minute(window.end_minute), days=cls.days_text(window.days))

    def _rule_text(self) -> str:
        """规则摘要（配置成什么样），常显在标题行。"""
        rule = self.rule
        parts = []
        if rule.window_active:
            shown = rule.windows[:3]
            ranges = tr("common.list_sep").join(self.window_text(w) for w in shown)
            if len(rule.windows) > len(shown):
                ranges += "…"
            parts.append(tr("control.summary.window", ranges=ranges))
        if rule.daily_active:
            parts.append(tr("control.summary.daily",
                            amount=format_amount(rule.daily_value, rule.daily_unit)))
        if rule.streak_active:
            parts.append(tr("control.summary.streak",
                            x=format_amount(rule.streak_value, rule.streak_unit),
                            y=format_amount(rule.pause_value, rule.pause_unit)))
        return " · ".join(parts) if parts else tr("control.summary.none")

    def refresh_status(self) -> None:
        """标题行：规则摘要 + 实时状态（今日已用 / 连续已用 / 时段剩余 / 临时解锁）。"""
        self.summary.setText(self._rule_text())
        parts = []
        state = self.service.state_summary(self.program_id) if self.service else {}
        if state.get("daily_limit"):
            used = util.format_duration(float(state.get("used_seconds", 0)))
            parts.append(tr("control.summary.used", used=used))
        if state.get("streak_limit"):
            streak = util.format_duration(float(state.get("streak_seconds", 0)))
            parts.append(tr("control.summary.streak_used", used=streak))
        if state.get("window_active"):
            left = util.format_duration(float(state.get("window_remaining", 0)))
            parts.append(tr("control.summary.window_used", time=left))
        if state.get("blocked"):
            parts.append(tr("control.summary.blocked"))
        elif state.get("unlock_remaining", 0) > 0:
            # 临时解锁期间把剩余时间显示出来（与"今日已用"同一格式），
            # 否则这一行只有"临时解锁中"四个字，看不出还要等多久。
            remaining = util.format_duration(float(state["unlock_remaining"]))
            parts.append(tr("control.summary.unlocked", time=remaining))
        self.status.setText(" · ".join(parts))
        self.status.setVisible(bool(parts))


# --------------------------------------------------------------------------
# 控制页
# --------------------------------------------------------------------------
class _GateField(QFrame):
    """锁屏输入框：圆角外框 + 框内无边框输入 + 框内右侧的错误提示。

    错误提示直接写在框内右侧，因此不会改变外部布局；提示会一直留在原处，直到
    用户再次敲键或重新提交（不再随时间自动消失）。
    提交用的箭头按钮回到框外右侧（见 _build_gate 里的 IconButton）。
    """

    submit = Signal()
    typed = Signal()

    def __init__(self, placeholder: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("ControlGateField")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(config.sx(42))
        self.setProperty("focus", False)
        self.setProperty("error", False)
        self._error_text = ""

        box = QHBoxLayout(self)
        box.setContentsMargins(config.sx(14), 0, config.sx(14), 0)
        box.setSpacing(config.sx(6))

        self.edit = QLineEdit(self)
        self.edit.setObjectName("ControlGateInput")
        self.edit.setEchoMode(QLineEdit.Password)
        self.edit.setFont(theme.font(config.FONT_BODY))
        self.edit.setPlaceholderText(placeholder)
        self.edit.returnPressed.connect(self.submit)
        self.edit.textEdited.connect(self._on_typed)
        self.edit.installEventFilter(self)
        box.addWidget(self.edit, 1)

        # 错误提示不进布局：它盖在输入区右侧，位置由 _place_message() 摆好。
        # 一旦进布局，提示出现时会把输入框撑宽 —— 那正是用户不想要的"布局改变"。
        self.message = QLabel("", self)
        self.message.setObjectName("ControlGateError")
        self.message.setFont(theme.font(config.FONT_SMALL))
        self.message.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.message.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.message.setVisible(False)

    # ---------------------------------------------------------------- 输入
    def text(self) -> str:
        return self.edit.text()

    def clear(self) -> None:
        self.edit.clear()

    def set_placeholder(self, text: str) -> None:
        self.edit.setPlaceholderText(text)

    def setText(self, text: str) -> None:                    # noqa: N802 - 兼容原 QLineEdit 调用
        self.edit.setText(text)

    def setFocus(self, reason=Qt.OtherFocusReason) -> None:  # noqa: N802 - 兼容原 QLineEdit 调用
        self.edit.setFocus(reason)

    def set_enabled_input(self, enabled: bool) -> None:
        self.edit.setEnabled(enabled)

    # ------------------------------------------------------- 框内错误提示
    def error_text(self) -> str:
        return self._error_text

    def set_error(self, text: str) -> None:
        text = text or ""
        self._error_text = text
        changed = bool(self.property("error")) != bool(text)
        self.setProperty("error", bool(text))
        if changed:
            theme.repolish(self)
        self.message.setVisible(bool(text))
        self._place_message()

    def _place_message(self) -> None:
        """把提示摆到框内右侧（绝对定位，绝不参与布局）。"""
        if not self._error_text:
            self.message.setText("")
            return
        margin = config.sx(14)
        right = self.width() - margin
        room = max(config.sx(40), right - margin)
        width = min(max(config.sx(40), int(self.width() * 0.52)), room)
        metrics = QFontMetrics(self.message.font())
        self.message.setText(metrics.elidedText(self._error_text, Qt.ElideRight, width))
        self.message.setGeometry(int(right - width), 0, int(width), self.height())
        self.message.raise_()

    # ---------------------------------------------------------------- 事件
    def _on_typed(self, _text: str = "") -> None:
        self.typed.emit()

    def eventFilter(self, obj, event) -> bool:               # noqa: N802 - Qt 命名
        if obj is self.edit and event.type() in (QEvent.FocusIn, QEvent.FocusOut):
            focused = event.type() == QEvent.FocusIn
            if bool(self.property("focus")) != focused:
                self.setProperty("focus", focused)
                theme.repolish(self)
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:                    # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._place_message()


class ControlPage(QWidget):
    """左侧导航的"控制"页。"""

    def __init__(self, db, service, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.db = db
        self.service = service
        self._unlocked = False
        self._left_at = 0.0              # 上次离开本页的时刻（用于 >1 分钟重新验证）
        self._cards: dict[int, _RuleCard] = {}
        self._expanded: set = set()      # 展开的卡片：重建后保持展开状态
        self._animating = False          # 删除动画播放期间不要重建卡片
        self._gate_key = None            # 锁定态签名：只有它变化时才重置输入框
        self._gate_locked = False        # 上一次刷新时是否处于"密码错误锁定中"

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._root_layout = root          # 锁屏表单挂在页面根布局上（不随滚动区滚动）
        self.scroll = SlimScrollArea(self)
        self.holder = QWidget()
        self.layout_ = QVBoxLayout(self.holder)
        self.layout_.setContentsMargins(config.sx(24), config.sx(16), config.sx(24), config.sx(20))
        self.layout_.setSpacing(config.sx(14))
        self.scroll.setWidget(self.holder)
        root.addWidget(self.scroll, 1)

        # 拖动手柄排序（程序 / 网站各自一组，互不跨类）
        self.reorder = ReorderController(self.scroll, self._commit_order, self)

        self._build_gate()
        self._build_body()

        service.state_changed.connect(self.refresh)
        service.ticked.connect(self._on_tick)      # 实时刷新"今日已用 / 连续已用"
        self.refresh()

    # ------------------------------------------------------------------
    def _on_tick(self) -> None:
        # 密码错误锁定中：倒计时每秒刷新（以前是进入锁定时算一次就不再变）。
        # 这里不能看 gate.isVisible()：切到别的页面它就变成 False，倒计时会停在被切走的
        # 那一刻，用户切回来看到的就是一个不再走的秒数（用户报的 bug）。锁屏该不该显示
        # 只由 _unlocked / has_password 决定，与当前是不是正在看这一页无关。
        if self.service.has_password() and not self._unlocked:
            remaining = self.service.lock_remaining()
            self._sync_gate_hint(remaining)
            if remaining <= 0 and self._gate_locked:
                # 只在"锁定刚结束"这一拍刷新。以前每秒都刷 —— 刷新会把用户正在输入的
                # 密码清空、把刚出现的错误提示抹掉、还会把焦点抢回第一个输入框。
                self.refresh()

        # 卡片状态行（今日已用 / 连续已用 / 临时解锁剩余）每秒跟着引擎走。
        # 以前只在 state_changed（进入/离开限制）时刷新，所以"连续使用"看着是停的。
        if self.body.isVisible() and not self._animating:
            self._refresh_statuses()          # 只更新文字，不重建控件

    # ------------------------------------------------------------------
    def topbar_widget(self) -> Optional[QWidget]:
        return None

    def _build_gate(self) -> None:
        """锁定态：不套卡片，整页垂直居中直接给输入框（回车或右侧箭头解锁）。"""
        self.gate = QWidget(self)
        outer = QVBoxLayout(self.gate)
        outer.setContentsMargins(config.sx(28), config.sx(28), config.sx(28), config.sx(28))
        outer.setSpacing(0)
        outer.addStretch(1)

        column = QWidget(self.gate)
        column.setMaximumWidth(config.sx(470))
        col = QVBoxLayout(column)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(config.sx(12))

        self.gate_icon = QLabel(column)
        self.gate_icon.setPixmap(vector_icons.pixmap("lock", config.sx(40), config.C_PRIMARY, 1.7))
        self.gate_icon.setAlignment(Qt.AlignCenter)
        col.addWidget(self.gate_icon)

        self.gate_title = QLabel(tr("control.gate.title"), column)
        self.gate_title.setObjectName("CardTitle")
        self.gate_title.setFont(theme.font(config.FONT_PAGE_TITLE, bold=True))
        self.gate_title.setAlignment(Qt.AlignCenter)
        col.addWidget(self.gate_title)

        self.gate_hint = QLabel(tr("control.gate.hint"), column)
        self.gate_hint.setObjectName("Muted")
        self.gate_hint.setFont(theme.font(config.FONT_SMALL))
        self.gate_hint.setWordWrap(True)
        self.gate_hint.setAlignment(Qt.AlignCenter)
        col.addWidget(self.gate_hint)

        col.addSpacing(config.sx(4))
        self.gate_field = _GateField(tr("control.setup.placeholder"), parent=column)
        self.gate_field.setMinimumWidth(config.sx(360))
        self.gate_field.submit.connect(self._submit_gate)
        self.gate_field.typed.connect(self._on_gate_typed)

        # 箭头按钮在输入框外面、右侧：与输入框同高的独立图标按钮
        self.gate_submit = IconButton("chevron_right", tr("control.gate.unlock"),
                                      size=42, icon_size=19, color=config.C_PRIMARY,
                                      parent=column, flat=True)
        self.gate_submit.clicked.connect(self._submit_gate)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(config.sx(10))
        row.addWidget(self.gate_field, 1)
        row.addWidget(self.gate_submit, 0)
        col.addLayout(row)

        self.gate_confirm = _GateField(tr("control.setup.confirm_placeholder"), parent=column)
        self.gate_confirm.setMinimumWidth(config.sx(360))
        self.gate_confirm.submit.connect(self._submit_gate)
        self.gate_confirm.typed.connect(self._on_gate_typed)
        col.addWidget(self.gate_confirm)

        # 兼容旧引用（自检与探针一直用这个名字）：gate_edit = 第一个框里的输入框
        self.gate_edit = self.gate_field.edit

        self.gate_bottom = QLabel(tr("control.gate.enter_hint"), column)
        self.gate_bottom.setObjectName("Faint")
        self.gate_bottom.setFont(theme.font(config.FONT_TINY))
        self.gate_bottom.setAlignment(Qt.AlignCenter)
        col.addWidget(self.gate_bottom)

        outer.addWidget(column, 0, Qt.AlignHCenter)
        outer.addStretch(1)
        self._root_layout.addWidget(self.gate, 1)
        self.gate.setVisible(False)

    def _build_body(self) -> None:
        self.body = QWidget(self.holder)
        body_box = QVBoxLayout(self.body)
        body_box.setContentsMargins(0, 0, 0, 0)
        body_box.setSpacing(config.sx(14))

        header = Panel(self.body, elevated=True, radius=16)
        head_box = QVBoxLayout(header)
        head_box.setContentsMargins(config.sx(20), config.sx(16), config.sx(20), config.sx(16))
        head_box.setSpacing(config.sx(12))
        top = QHBoxLayout()
        top.setSpacing(config.sx(14))
        self.enable_switch = ToggleSwitch(self.service.enabled(), header)
        self.enable_switch.toggled.connect(self._on_enable_toggled)
        top.addWidget(self.enable_switch, 0, Qt.AlignVCenter)
        title = QLabel(tr("control.enable"), header)
        title.setObjectName("CardTitle")
        title.setFont(theme.font(config.FONT_CARD_NAME, bold=True))
        top.addWidget(title, 0, Qt.AlignVCenter)
        top.addStretch(1)
        self.password_btn = TextButton(tr("control.change_password"), "ghost", height=34,
                                       parent=header)
        self.password_btn.clicked.connect(self._on_change_password)
        top.addWidget(self.password_btn, 0, Qt.AlignVCenter)
        head_box.addLayout(top)
        hint = QLabel(tr("control.enable.hint"), header)
        hint.setObjectName("Muted")
        hint.setFont(theme.font(config.FONT_SMALL))
        hint.setWordWrap(True)
        head_box.addWidget(hint)

        # 管理员提示：游戏等提权程序需要 Monitor 同样提权才能"只遮住它自己"
        self.admin_row = QWidget(header)
        admin_box = QHBoxLayout(self.admin_row)
        admin_box.setContentsMargins(0, 0, 0, 0)
        admin_box.setSpacing(config.sx(10))
        admin_hint = QLabel(tr("admin.hint"), self.admin_row)
        admin_hint.setObjectName("Muted")
        admin_hint.setFont(theme.font(config.FONT_SMALL))
        admin_hint.setWordWrap(True)
        admin_box.addWidget(admin_hint, 1)
        self.admin_btn = TextButton(tr("admin.restart"), "ghost", height=32,
                                    parent=self.admin_row)
        self.admin_btn.clicked.connect(self._on_admin_restart)
        admin_box.addWidget(self.admin_btn, 0, Qt.AlignTop)
        head_box.addWidget(self.admin_row)
        self.admin_row.setVisible(not util.is_admin())
        body_box.addWidget(header)

        list_head = QHBoxLayout()
        list_head.setSpacing(config.sx(12))
        list_title = QLabel(tr("control.rules.title"), self.body)
        list_title.setObjectName("CardTitle")
        list_title.setFont(theme.font(config.FONT_SECTION, bold=True))
        list_head.addWidget(list_title)
        list_head.addStretch(1)
        self.add_btn = TextButton(tr("control.add_program"), "ghost", height=34,
                                  icon_name="plus", icon_size=14, parent=self.body)
        self.add_btn.clicked.connect(self._on_add_program)
        list_head.addWidget(self.add_btn)
        body_box.addLayout(list_head)

        self.list_host = QWidget(self.body)
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(config.sx(12))
        body_box.addWidget(self.list_host)
        body_box.addStretch(1)
        self.body.hide()
        self.layout_.addWidget(self.body)
        self.layout_.addStretch(1)

    # ------------------------------------------------------------------
    def _on_admin_restart(self) -> None:
        """以管理员身份重新启动（提权后才能只遮挡提权运行的游戏）。"""
        if util.is_admin():
            return
        # 设过密码之后，这条"退出 + 提权重启"的路也要密码（用户 ⑤ 的口径）。
        if not ask_exit_password(self.window(), self.service):
            return
        if util.restart_as_admin():
            app = QApplication.instance()
            if app is not None:
                app.quit()
            return
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self.window())
        box.setWindowTitle(tr("admin.restart"))
        box.setText(tr("admin.failed"))
        box.setIcon(QMessageBox.Warning)
        box.exec()

    # ------------------------------------------------------------------
    def _sync_gate_hint(self, remaining: float) -> None:
        """校验态标题下方的说明：锁定期写"请 N 秒后再试"，否则写常规提示。

        每秒 tick、切页回来、任何一次 state_changed 都调它，所以秒数永远和真实剩余
        时间一致（以前只在"锁定签名变化"那一刻写一次，秒数会停住）。
        """
        if remaining > 0:
            self.gate_hint.setText(
                tr("control.gate.locked", seconds=max(1, int(math.ceil(remaining)))))
        else:
            self.gate_hint.setText(tr("control.gate.hint"))

    def _set_gate_error(self, text: str, target: str = "first") -> None:
        """错误提示写在框内右侧；target 决定写进哪一个输入框。"""
        if target == "second":
            self.gate_field.set_error("")
            self.gate_confirm.set_error(text)
        else:
            self.gate_confirm.set_error("")
            self.gate_field.set_error(text)

    def _on_gate_typed(self, _text: str = "") -> None:
        # 提示只在这一刻消失：用户再次敲键（改哪个框都会清），不再随时间淡出。
        if self.gate_field.error_text() or self.gate_confirm.error_text():
            self._set_gate_error("")

    def _focus_gate(self) -> None:
        if self.gate.isVisible():
            self.gate_edit.setFocus()

    def _submit_gate(self) -> None:
        """回车 / 框外右侧箭头：既用于输入密码解锁，也用于首次设置密码。"""
        if not self.gate.isVisible():
            return
        if self.service.lock_remaining() > 0:
            # 锁定期：箭头本身是禁用的（点不动），回车在这里挡掉 —— 不提交、也不清空
            # 用户已经敲好的密码。倒计时归零后直接回车即可解锁（输入框一直可用）。
            return
        if not self.service.has_password():
            password = self.gate_edit.text()
            if len(password) < PASSWORD_MIN:
                self._set_gate_error(tr("control.error.too_short"), "first")
                return
            if password != self.gate_confirm.text():
                # 两次不一致属于第二个框的问题：提示就写在第二个框内右侧
                self._set_gate_error(tr("control.error.mismatch"), "second")
                return
            self.service.set_password(password)
            self._unlocked = True
            self.refresh()
            return

        password = self.gate_edit.text()
        if not password:
            self._set_gate_error(tr("control.error.empty"), "first")
            return
        ok, reason = self.service.verify(password)
        if ok:
            self._unlocked = True
            self.refresh()
            return
        if reason in ("locked", "just_locked"):
            wait = max(1, int(math.ceil(self.service.lock_remaining() or 60)))
            self._sync_gate_hint(wait)
            self._set_gate_error(tr("control.error.locked"), "first")
            self._apply_gate_lock(True)
            # 记下"已经进入锁定态"：否则下一次 refresh（state_changed / 每秒 tick）会把
            # 刚出现的框内提示和用户敲进去的内容再清一遍。
            self._gate_locked = True
            self._gate_key = (True, True, False)
            return
        self._set_gate_error(tr("control.error.wrong"), "first")
        self.gate_edit.clear()
        self.gate_edit.setFocus()

    def _on_enable_toggled(self, enabled: bool) -> None:
        if enabled:
            if not util.is_admin():
                # 遮罩要覆盖以管理员身份运行的程序（如原神），未提权的 Monitor
                # 做不到"只遮挡该程序"——因此总开关的前置条件是管理员身份。
                self.enable_switch.setChecked(False, animate=True, emit=False)
                answer = QMessageBox.question(self.window(), tr("control.enable"),
                                              tr("control.enable.need_admin"))
                if answer != QMessageBox.StandardButton.Yes:
                    return
                # 提权重启 = 退出当前进程，同样要密码（用户 ⑤ 的口径）。
                if not ask_exit_password(self.window(), self.service):
                    return
                if util.restart_as_admin():
                    QApplication.instance().quit()
                return
            self.service.set_enabled(True)
            self.refresh()
            return
        # 关闭总开关不再需要密码：能走到这一步本身就说明已经验证过了 —— 页面主体
        # 只在 ``_unlocked and service.has_password()`` 时才可见（见 refresh()）。
        self.service.set_enabled(False)
        self.refresh()

    def _on_change_password(self) -> None:
        dialog = _PasswordDialog(self.window(), tr("control.change_password"),
                                 tr("control.change_password.hint"), confirm=True)
        if dialog.exec() != QDialog.Accepted:
            return
        self.service.set_password(dialog.password())
        self.service.touch_session()
        self.refresh()

    # ------------------------------------------------------------------
    def _addable_programs(self) -> list[dict]:
        """可加入"程序使用限制"的目标（程序 + 网站）：排除 Monitor 自己。

        限制自己 = 把自己遮罩 / 静音，之后控制页再也打不开，所以这里直接不给选。
        开发运行时 Monitor 的主体进程是 pythonw.exe（见 util.is_self_path）。
        """
        mine = self.service.self_program_ids()
        rows = [dict(p) for p in self.db.programs(visible_only=True)]
        return [p for p in rows
                if int(p["id"]) not in mine and not util.is_self_path(p.get("path"))]

    def _on_add_program(self) -> None:
        programs = self._addable_programs()
        if not programs:
            QMessageBox.information(self.window(), tr("control.add_program"),
                                    tr("control.add_program.none"))
            return

        picker = _ProgramPicker(programs, set(self._cards), self.window())
        if picker.exec() != QDialog.Accepted:
            return
        for program_id in picker.selected:
            # 新加入控制页的目标：默认两条规则都是关闭状态，等待用户配置
            self.service.save_rule(Rule(program_id=int(program_id), daily_value=60,
                                        streak_value=60, pause_value=10))
        self._rebuild_cards()

    def _on_remove_card(self, program_id: int) -> None:
        """删除规则卡片 = 从控制页移除该程序（规则与状态一并清除），带删除动画。"""
        program_id = int(program_id)
        card = self._cards.get(program_id)
        if card is not None and card.isVisible():
            # 与首页 / 日志页一致的删除动画：卡片右移淡出，其余卡片依次上移。
            # 必须先进入"动画中"状态再删规则：delete_rule 会同步发出 state_changed
            # 触发 refresh()，否则卡片会被立刻重建移除、动画根本来不及播放。
            self._animating = True

            def _done() -> None:
                self._animating = False
                self._cards.pop(program_id, None)
                self._rebuild_cards()

            anim.animate_list_removal(self.list_host, [card], self.list_layout.spacing(),
                                      on_finished=_done, slide_ratio=0.6)
            self.service.delete_rule(program_id)      # 后台立刻清规则 + 撤遮罩
            return
        self.service.delete_rule(program_id)
        self._rebuild_cards()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        remaining = self.service.lock_remaining()
        locked = remaining > 0
        has_password = self.service.has_password()
        show_body = self._unlocked and has_password
        # 只有在"锁定态真的变化"时才重置输入框与提示：ControlService 每秒一次 tick、
        # 以及任何 state_changed 都会走这里，无脑清空会把用户正在敲的密码抹掉。
        key = (has_password, locked, show_body)
        if key != self._gate_key:
            previous = self._gate_key
            self._gate_key = key
            self._gate_locked = locked
            # 锁定刚结束（True -> False）：留住用户敲好的密码，见 _reset_gate 的 keep_input
            keep_input = bool(previous) and bool(previous[1]) and not locked
            self._reset_gate(has_password, remaining, locked, keep_input=keep_input)
        self._apply_gate_lock(locked)
        if has_password and not show_body:
            self._sync_gate_hint(remaining)      # 切页回来 / state_changed 也把秒数对齐

        self.gate.setVisible(not show_body)
        # 锁屏占满整页：滚动的规则列表收成 0 高（保留可见性，免得影响"滚动条恒定占位"）
        self.scroll.setMaximumHeight(0 if not show_body else 16777215)
        self.body.setVisible(show_body)
        if not show_body:
            self._focus_gate()
            return
        self.enable_switch.setChecked(self.service.enabled(), animate=False, emit=False)
        self._rebuild_cards()

    def _reset_gate(self, has_password: bool, remaining: float, locked: bool,
                    keep_input: bool = False) -> None:
        """锁定态切换时才动输入框：换文案、清输入、清提示。

        keep_input=True（锁定刚结束）时留着用户敲好的密码 —— 锁定期输入框照样能敲，
        倒计时一归零就能直接回车解锁，不该把这段输入抹掉。
        """
        if not has_password:
            self.gate_title.setText(tr("control.setup.title"))
            self.gate_hint.setText(tr("control.setup.hint"))
            self.gate_field.set_placeholder(tr("control.setup.placeholder"))
            self.gate_confirm.set_placeholder(tr("control.setup.confirm_placeholder"))
            self.gate_confirm.setVisible(True)
        else:
            self.gate_title.setText(tr("control.gate.title"))
            self._sync_gate_hint(remaining if locked else 0.0)
            self.gate_field.set_placeholder(tr("control.gate.placeholder"))
            self.gate_confirm.clear()
            self.gate_confirm.setVisible(False)
        if not keep_input:
            self.gate_field.clear()
        self.gate_field.set_error("")
        self.gate_confirm.set_error("")

    def _apply_gate_lock(self, locked: bool) -> None:
        """锁定期只锁"提交"（箭头 + 回车），输入框照旧能敲。

        以前连输入框一起 disable：用户连"先把密码打好、等倒计时结束直接回车"都做不到。
        """
        self.gate_field.set_enabled_input(True)
        self.gate_confirm.set_enabled_input(True)
        self.gate_submit.setEnabled(not locked)
        self.gate_submit.setCursor(Qt.PointingHandCursor if not locked else Qt.ArrowCursor)

    # ------------------------------------------------------------------
    @staticmethod
    def _order_key(kind: str) -> str:
        return f"order.control.{kind}"

    def _order_items(self, items: list, kind: str) -> list:
        """按用户拖动保存的顺序排（没记录过的目标按数据库顺序追加在后面）。"""
        order = self.db.get_order(self._order_key(kind))
        if not order:
            return items
        rank = {int(pid): index for index, pid in enumerate(order)}
        return sorted(items, key=lambda item: rank.get(int(item[0]["id"]), len(rank)))

    def _commit_order(self, kind: str, ids: list) -> None:
        """拖动手柄松手：把该分类的新顺序写进数据库（只影响本页显示顺序）。"""
        self.db.set_order(self._order_key(kind), ids)

    def _on_expand_changed(self, program_id: int, expanded: bool) -> None:
        if expanded:
            self._expanded.add(int(program_id))
        else:
            self._expanded.discard(int(program_id))

    def _rebuild_cards(self) -> None:
        if self._animating:
            return                    # 删除动画进行中：等动画结束再重建
        suppress_tooltips()          # 重建卡片时不要让提示气泡闪一下
        self.reorder.clear()         # 旧卡片马上销毁，先摘掉拖动控制器里的引用
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()          # 先隐藏：可见控件脱离父级会变成顶层窗口
                widget.setParent(None)
                widget.deleteLater()
        self._cards.clear()

        programs = {int(p["id"]): dict(p) for p in self.db.programs(visible_only=True)}
        rules = self.service.all_rules()
        mine = self.service.self_program_ids()
        groups: dict[str, list] = {"program": [], "site": []}
        for program_id, rule in rules.items():
            if int(program_id) in mine:
                continue                       # Monitor 自己的规则永不显示（也不会被加载）
            program = programs.get(int(program_id))
            if program is None:
                continue                       # 程序已从首页删除 -> 规则卡不再显示
            groups["site" if is_site(program) else "program"].append((program, rule))

        has_both = bool(groups["program"]) and bool(groups["site"])
        cards: dict[str, list] = {"program": [], "site": []}
        for kind in ("program", "site"):
            items = self._order_items(groups[kind], kind)
            if not items:
                continue
            if has_both:
                section = theme.label(tr(f"control.section.{kind}s"), config.FONT_SMALL, True,
                                      config.C_TEXT_MUTED, self.list_host)
                section.setContentsMargins(config.sx(4), config.sx(2), 0, -config.sx(4))
                self.list_layout.addWidget(section)
            for program, rule in items:
                program_id = int(program["id"])
                card = _RuleCard(program, rule, self.service, self.list_host,
                                 expanded=program_id in self._expanded)
                card.removed.connect(self._on_remove_card)
                card.changed.connect(self._refresh_statuses)
                card.expand_changed.connect(self._on_expand_changed)
                self.list_layout.addWidget(card)
                anim.fade_in(card, config.ANIM_FAST)
                self._cards[program_id] = card
                cards[kind].append(card)

        if not self._cards:
            empty = QLabel(tr("control.rules.empty"), self.list_host)
            empty.setObjectName("Muted")
            empty.setFont(theme.font(config.FONT_SMALL))
            self.list_layout.addWidget(empty)
        else:
            for kind, group_cards in cards.items():
                self.reorder.register(kind, group_cards)

    def refresh_names(self) -> None:
        """首页重命名/增删程序后刷新卡片（程序名、路径、图标都取最新的）。"""
        if not self.body.isVisible():
            return
        self._rebuild_cards()

    def _refresh_statuses(self) -> None:
        for card in self._cards.values():
            card.refresh_status()

    # ------------------------------------------------------------------
    # 离开本页超过用户设定的间隔 → 回来要重新验证
    # ------------------------------------------------------------------
    def _on_page_shown(self) -> None:
        """回到控制页：离开超过设定间隔（默认 1 分钟，0 = 每次都验证）就重新锁上。"""
        if self.gate.isVisible():
            # 锁屏态：倒计时（甚至整段锁定）可能就在离开的这段时间里走完，回来先按
            # 真实剩余时间对齐一次 —— 只走 refresh() 的签名守卫，不会清掉正在敲的密码。
            self.refresh()
        if not self._left_at:
            return
        gap = time.time() - self._left_at
        self._left_at = 0.0
        if gap <= self.service.session_grace():
            if self._unlocked:
                self.service.touch_session()      # 仍在免验证窗口内：顺手续期
            return
        if self._unlocked:
            self._unlocked = False
            self.refresh()

    def showEvent(self, event) -> None:              # noqa: N802 - Qt 命名
        super().showEvent(event)
        self._on_page_shown()

    def hideEvent(self, event) -> None:              # noqa: N802 - Qt 命名
        super().hideEvent(event)
        self._left_at = time.time()
