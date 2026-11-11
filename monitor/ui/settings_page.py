"""设置页：开机自启动、静默启动、关闭行为、托盘图标。"""

from __future__ import annotations

import os
import shutil
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
                               QSpinBox, QVBoxLayout, QWidget)

from .. import autostart, config, icon_extract, util
from ..i18n import tr
from . import theme
from .controls import Divider, IconTile, Panel, SlimScrollArea, TextButton, ToggleSwitch
from .popups import SelectBox


class SettingRow(QWidget):
    """图标 + 标题 + 说明 + 右侧控件。"""

    def __init__(self, icon_name: str, title: str, description: str, control: QWidget,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(config.sx(16), config.sx(13), config.sx(16), config.sx(13))
        layout.setSpacing(config.sx(13))

        tile = IconTile(38, self)
        tile.setPixmap(vector_icon(icon_name, config.sx(19)))
        layout.addWidget(tile, 0, Qt.AlignVCenter)

        column = QWidget(self)
        col = QVBoxLayout(column)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        title_label = QLabel(title)
        title_label.setFont(theme.font(config.FONT_BODY, bold=True))
        title_label.setStyleSheet(f"color: {config.C_TEXT}; background: transparent;")
        col.addWidget(title_label)
        if description:
            desc_label = QLabel(description)
            desc_label.setFont(theme.font(config.FONT_SMALL))
            desc_label.setStyleSheet(f"color: {config.C_TEXT_MUTED}; background: transparent;")
            desc_label.setWordWrap(True)
            col.addWidget(desc_label)
        layout.addWidget(column, 1)
        layout.addWidget(control, 0, Qt.AlignVCenter)


def vector_icon(name: str, size: int) -> QPixmap:
    from . import vector_icons

    return vector_icons.pixmap(name, size, config.C_TEXT_MUTED, 1.8)


class SettingsPage(QWidget):
    """设置页面。"""

    close_action_changed = Signal(str)
    tray_icon_changed = Signal(str)
    silent_start_changed = Signal(bool)
    theme_changed = Signal(str)
    language_changed = Signal(str)

    def __init__(self, db, parent: Optional[QWidget] = None, control=None) -> None:
        super().__init__(parent)
        self.db = db
        self.control = control      # 退出 / 提权重启的密码闸门要用（见 control_page）

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.scroll = SlimScrollArea(self)
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(config.sx(22), config.sx(14), config.sx(22), config.sx(18))
        layout.setSpacing(config.sx(14))

        # ---- 通用（语言 / 外观）-----------------------------------------
        general = Panel(holder)
        general_layout = QVBoxLayout(general)
        general_layout.setContentsMargins(0, 0, 0, 0)
        general_layout.setSpacing(0)

        self.language_select = SelectBox(
            [(config.LANGUAGE_ZH, tr("settings.language.zh")),
             (config.LANGUAGE_EN, tr("settings.language.en"))],
            current=self.db.get_setting(config.S_LANGUAGE, config.LANGUAGE_ZH),
            parent=general, min_width=150)
        self.language_select.current_changed.connect(self._on_language_changed)
        general_layout.addWidget(SettingRow(
            "globe", tr("settings.language"),
            tr("settings.language.desc"),
            self.language_select, general))
        general_layout.addWidget(Divider(general))

        self.theme_select = SelectBox(
            [("light", tr("settings.theme.light")), ("dark", tr("settings.theme.dark")),
             ("system", tr("settings.theme.system"))],
            current=self.db.get_setting(config.S_THEME, config.THEME_SYSTEM),
            parent=general, min_width=150)
        self.theme_select.current_changed.connect(self._on_theme_changed)
        general_layout.addWidget(SettingRow(
            "image", tr("settings.theme"),
            tr("settings.theme.desc"),
            self.theme_select, general))
        layout.addWidget(general)

        # ---- 启动 ------------------------------------------------------
        startup = Panel(holder)
        startup_layout = QVBoxLayout(startup)
        startup_layout.setContentsMargins(0, 0, 0, 0)
        startup_layout.setSpacing(0)

        self.autostart_switch = ToggleSwitch(autostart.is_enabled())
        self.autostart_switch.toggled.connect(self._on_autostart_toggled)
        startup_layout.addWidget(SettingRow(
            "power", tr("settings.autostart"),
            tr("settings.autostart.desc"),
            self.autostart_switch, startup))
        startup_layout.addWidget(Divider(startup))

        self.silent_switch = ToggleSwitch(self.db.get_bool(config.S_SILENT_START, False))
        self.silent_switch.toggled.connect(self._on_silent_toggled)
        startup_layout.addWidget(SettingRow(
            "monitor", tr("settings.silent"),
            tr("settings.silent.desc"),
            self.silent_switch, startup))
        startup_layout.addWidget(Divider(startup))

        # 以管理员身份启动：走登录触发的计划任务（/RL HIGHEST），
        # 登录时不会弹 UAC，遮罩才能只遮挡提权程序本身。
        self.admin_switch = ToggleSwitch(
            autostart.is_admin_enabled() or self.db.get_bool(config.S_ADMIN_START, False))
        self.admin_switch.toggled.connect(self._on_admin_toggled)
        startup_layout.addWidget(SettingRow(
            "shield", tr("settings.admin"),
            tr("settings.admin.desc"),
            self.admin_switch, startup))
        layout.addWidget(startup)

        # ---- 安全：控制页免验证间隔 -------------------------------------
        security = Panel(holder)
        security_layout = QVBoxLayout(security)
        security_layout.setContentsMargins(0, 0, 0, 0)
        security_layout.setSpacing(0)

        default_minutes = max(0.0, config.SESSION_GRACE / 60.0)
        minutes = int(round(self.db.get_float(config.S_SESSION_GRACE, default_minutes)))
        self.grace_spin = QSpinBox(security)
        self.grace_spin.setRange(0, 24 * 60)
        self.grace_spin.setValue(max(0, minutes))
        self.grace_spin.setSuffix(tr("settings.grace.suffix"))
        self.grace_spin.setFixedWidth(config.sx(132))
        self.grace_spin.valueChanged.connect(self._on_grace_changed)
        security_layout.addWidget(SettingRow(
            "clock", tr("settings.grace"), tr("settings.grace.desc"),
            self.grace_spin, security))
        layout.addWidget(security)

        # ---- 托盘与关闭 ------------------------------------------------
        tray_panel = Panel(holder)
        tray_layout = QVBoxLayout(tray_panel)
        tray_layout.setContentsMargins(0, 0, 0, 0)
        tray_layout.setSpacing(0)

        self.close_select = SelectBox(
            [(config.CLOSE_ACTION_TRAY, tr("settings.close_action.tray")),
             (config.CLOSE_ACTION_EXIT, tr("settings.close_action.exit"))],
            current=self.db.get_setting(config.S_CLOSE_ACTION, config.CLOSE_ACTION_TRAY),
            parent=tray_panel, min_width=160)
        self.close_select.current_changed.connect(self._on_close_action)
        tray_layout.addWidget(SettingRow(
            "close", tr("settings.close_action"),
            tr("settings.close_action.desc"),
            self.close_select, tray_panel))
        tray_layout.addWidget(Divider(tray_panel))

        tray_control = QWidget(tray_panel)
        tray_control_layout = QHBoxLayout(tray_control)
        tray_control_layout.setContentsMargins(0, 0, 0, 0)
        tray_control_layout.setSpacing(config.sx(8))
        self.tray_preview = QLabel()
        self.tray_preview.setFixedSize(config.sx(30), config.sx(30))
        self.tray_preview.setStyleSheet("background: transparent;")
        tray_control_layout.addWidget(self.tray_preview)
        change_btn = TextButton(tr("settings.tray_icon.change"), "ghost", height=30)
        change_btn.clicked.connect(self._on_change_tray_icon)
        reset_btn = TextButton(tr("settings.tray_icon.reset"), "subtle", height=30)
        reset_btn.clicked.connect(self._on_reset_tray_icon)
        tray_control_layout.addWidget(change_btn)
        tray_control_layout.addWidget(reset_btn)
        tray_layout.addWidget(SettingRow(
            "image", tr("settings.tray_icon"),
            tr("settings.tray_icon.desc"),
            tray_control, tray_panel))
        layout.addWidget(tray_panel)

        tip = QLabel(tr("settings.tip"))
        tip.setFont(theme.font(config.FONT_SMALL))
        tip.setStyleSheet(f"color: {config.C_ON_CANVAS_MUTED}; background: transparent;")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        layout.addStretch(1)

        self.scroll.setWidget(holder)
        root.addWidget(self.scroll, 1)

        self._refresh_tray_preview()

    # ------------------------------------------------------------------
    def _on_language_changed(self, key: str) -> None:
        self.db.set_setting(config.S_LANGUAGE, key)
        self.language_changed.emit(key)

    def _on_theme_changed(self, key: str) -> None:
        self.db.set_setting(config.S_THEME, key)
        self.theme_changed.emit(key)

    def _on_autostart_toggled(self, enabled: bool) -> None:
        admin = bool(self.admin_switch.isChecked()) and enabled
        if not enabled:
            # 关掉自启动时，"以管理员身份启动"也随之失效
            self.admin_switch.setChecked(False, animate=True)
            self.db.set_setting(config.S_ADMIN_START, False)
            admin = False
        ok = autostart.apply(enabled, self.db.get_bool(config.S_SILENT_START, False),
                             admin=admin)
        if not ok:
            self.autostart_switch.setChecked(not enabled, animate=True)
            self.db.set_setting(config.S_START_WITH_WINDOWS, not enabled)
        else:
            self.db.set_setting(config.S_START_WITH_WINDOWS, enabled)

    def _on_silent_toggled(self, enabled: bool) -> None:
        self.db.set_setting(config.S_SILENT_START, enabled)
        self.silent_start_changed.emit(enabled)
        if autostart.is_enabled():
            autostart.apply(True, enabled, admin=self.admin_switch.isChecked())

    def _on_admin_toggled(self, enabled: bool) -> None:
        """以管理员身份启动：需要管理员权限写计划任务。"""
        if not enabled:
            self.db.set_setting(config.S_ADMIN_START, False)
            # 回到普通自启动（开机自启动开关仍开着时）
            autostart.apply(self.autostart_switch.isChecked(),
                            self.db.get_bool(config.S_SILENT_START, False), admin=False)
            return
        self.db.set_setting(config.S_ADMIN_START, True)
        if not util.is_admin():
            from .control_page import ask_exit_password

            answer = QMessageBox.question(self, tr("settings.admin"), tr("settings.admin.need"))
            if answer == QMessageBox.StandardButton.Yes:
                # 提权重启 = 退出当前进程：设过控制密码就同样要密码（用户 ⑤ 的口径）。
                if not ask_exit_password(self.window(), self.control):
                    self.admin_switch.setChecked(False, animate=True)
                    self.db.set_setting(config.S_ADMIN_START, False)
                    return
                if util.restart_as_admin():
                    QApplication.instance().quit()      # 重启后由启动流程写入计划任务
                    return
            self.admin_switch.setChecked(False, animate=True)
            self.db.set_setting(config.S_ADMIN_START, False)
            return
        silent = self.db.get_bool(config.S_SILENT_START, False)
        if autostart.apply(True, silent, admin=True):
            # 只有"开机自启动"本来没开时才联动打开；已经开着就别再播一遍开启动画
            # （用户 ①：要修的是联动本身该不该动，不是动画）。
            if not self.autostart_switch.isChecked():
                self.autostart_switch.setChecked(True, animate=True)
            self.db.set_setting(config.S_START_WITH_WINDOWS, True)
        else:
            self.admin_switch.setChecked(False, animate=True)
            self.db.set_setting(config.S_ADMIN_START, False)
            QMessageBox.warning(self, tr("settings.admin"),
                                tr("settings.admin.failed", error=autostart.LAST_ERROR))

    def _on_grace_changed(self, value: int) -> None:
        """控制页免验证间隔（分钟，0 = 每次进入都要重新输密码）。"""
        self.db.set_setting(config.S_SESSION_GRACE, float(max(0, int(value))))

    def _on_close_action(self, key: str) -> None:
        self.db.set_setting(config.S_CLOSE_ACTION, key)
        self.close_action_changed.emit(key)

    def _on_change_tray_icon(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, tr("settings.tray_icon.pick"), "", tr("settings.tray_icon.filter"))
        if not path:
            return
        try:
            os.makedirs(config.TRAY_DIR, exist_ok=True)
            ext = os.path.splitext(path)[1].lower() or ".png"
            target = os.path.join(config.TRAY_DIR, f"custom_tray{ext}")
            shutil.copyfile(path, target)
        except OSError:
            return
        self.db.set_setting(config.S_TRAY_ICON, target)
        self._refresh_tray_preview()
        self.tray_icon_changed.emit(target)

    def _on_reset_tray_icon(self) -> None:
        self.db.set_setting(config.S_TRAY_ICON, "")
        self._refresh_tray_preview()
        self.tray_icon_changed.emit("")

    def _refresh_tray_preview(self) -> None:
        custom = self.db.get_setting(config.S_TRAY_ICON, "")
        pixmap = None
        if custom and os.path.exists(custom):
            pixmap = QPixmap(custom)
            if pixmap.isNull():
                pixmap = None
        if pixmap is None:
            pixmap = icon_extract.default_tray_pixmap(config.sx(30))
        self.tray_preview.setPixmap(pixmap.scaled(config.sx(30), config.sx(30),
                                                  Qt.KeepAspectRatio, Qt.SmoothTransformation))
