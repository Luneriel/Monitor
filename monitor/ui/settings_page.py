"""设置页：开机自启动、静默启动、关闭行为、托盘图标。"""

from __future__ import annotations

import os
import shutil
import threading
from typing import Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot, QTimer
from PySide6.QtGui import QPixmap, QImageReader
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
                               QDialog, QListWidget, QListWidgetItem, QSpinBox, QSlider,
                               QVBoxLayout, QWidget)

from .. import autostart, config, icon_extract, util
from ..i18n import tr
from . import theme, glass
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


class _ImportSignals(QObject):
    progress = Signal(object, str)
    completed = Signal(object, object)
    failed = Signal(object, str)


class _ImportJob(QRunnable):
    """Only invoked by explicit requests; no widgets or DB access in this worker."""

    def __init__(self, operation):
        super().__init__()
        self.signals = _ImportSignals()
        self.cancel = threading.Event()
        self.token = object()
        self.operation = operation

    def run(self):
        try:
            result = self.operation(self.cancel.is_set, lambda message:
                                    self.signals.progress.emit(self.token, message))
            if not self.cancel.is_set():
                self.signals.completed.emit(self.token, result)
        except Exception as error:
            if not self.cancel.is_set():
                self.signals.failed.emit(self.token, str(error))


class WallpaperImportDialog(QDialog):
    imported = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle(tr("settings.we.title"))
        self.resize(config.sx(650), config.sx(570))
        self._job = None
        self._closed = False
        self._projects = []
        layout = QVBoxLayout(self)
        hint = QLabel(tr("settings.we.description"), self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QHBoxLayout()
        self.discover_button = TextButton(tr("settings.we.discover"), "ghost", height=34)
        self.browse_button = TextButton(tr("settings.we.browse"), "ghost", height=34)
        self.cancel_button = TextButton(tr("settings.we.cancel"), "subtle", height=34)
        self.discover_button.clicked.connect(lambda: self.start_scan())
        self.browse_button.clicked.connect(self._browse)
        self.cancel_button.clicked.connect(self.cancel_work)
        buttons.addWidget(self.discover_button)
        buttons.addWidget(self.browse_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self.project_list = QListWidget(self)
        self.project_list.currentRowChanged.connect(self._selection_changed)
        layout.addWidget(self.project_list, 1)
        self.preview = QLabel(self)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setFixedHeight(config.sx(110))
        layout.addWidget(self.preview)
        self.detail = QLabel(self)
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        self.status = QLabel(tr("settings.we.ready"), self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        footer.addStretch(1)
        close = TextButton(tr("settings.we.close"), "subtle", height=34)
        close.clicked.connect(self.reject)
        self.import_button = TextButton(tr("settings.we.apply"), "primary", height=34)
        self.import_button.clicked.connect(self._import_selected)
        footer.addWidget(close)
        footer.addWidget(self.import_button)
        layout.addLayout(footer)
        self._set_busy(False)

    def _set_busy(self, busy):
        self.discover_button.setEnabled(not busy)
        self.browse_button.setEnabled(not busy)
        self.project_list.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.import_button.setEnabled(not busy and self._selected_supported())

    def _selected_supported(self):
        row = self.project_list.currentRow()
        return (0 <= row < len(self._projects)
                and self._projects[row].import_mode != "unsupported")

    def _run(self, operation, complete):
        if self._job is not None:
            return
        self._set_busy(True)
        job = _ImportJob(operation)
        self._job = job
        # Explicit queued receivers are discarded by Qt if this dialog is destroyed.
        job.signals.progress.connect(self._progress, Qt.QueuedConnection)
        job.signals.completed.connect(complete, Qt.QueuedConnection)
        job.signals.failed.connect(self._failed, Qt.QueuedConnection)
        self.destroyed.connect(lambda _obj=None, cancel=job.cancel: cancel.set())
        QThreadPool.globalInstance().start(job)

    @Slot(object, str)
    def _progress(self, token, message):
        if not self._closed and self._job is not None and token is self._job.token:
            self.status.setText(message)

    @Slot(object, str)
    def _failed(self, token, message):
        if self._closed or self._job is None or token is not self._job.token:
            return
        self._job = None
        self._set_busy(False)
        self.status.setText(tr("settings.we.failed", error=message))

    def start_scan(self, path=None):
        from .. import wallpaper_engine as we
        self.status.setText(tr("settings.we.scanning"))
        def scan(cancel, progress):
            roots = we.discover_roots(path, discover=path is None)
            return we.scan_projects(roots, cancel=cancel, progress=progress)
        self._run(scan, self._scan_complete)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, tr("settings.we.browse"))
        if path:
            self.start_scan(path)

    @Slot(object, object)
    def _scan_complete(self, token, result):
        if self._closed or self._job is None or token is not self._job.token:
            return
        self._job = None
        self._projects = list(result.projects)
        self.project_list.clear()
        for project in self._projects:
            mode = tr("settings.we.mode." + project.import_mode)
            item = QListWidgetItem(f"{project.title}  ·  {project.type}  ·  {mode}")
            item.setToolTip(project.project_dir)
            self.project_list.addItem(item)
        self._set_busy(False)
        self.status.setText(tr("settings.we.found", count=len(self._projects))
                            + ("\n" + "\n".join(result.warnings) if result.warnings else ""))
        if self._projects:
            self.project_list.setCurrentRow(0)

    def _selection_changed(self, row):
        self.preview.clear()
        self.detail.clear()
        if 0 <= row < len(self._projects):
            project = self._projects[row]
            self.detail.setText(tr("settings.we.mode." + project.import_mode)
                                + (" · " + project.reason if project.reason else ""))
            if project.preview:
                reader = QImageReader(project.preview)
                reader.setAutoTransform(True)
                size = reader.size()
                if size.isValid() and size.width() * size.height() <= 120_000_000:
                    reader.setScaledSize(size.scaled(300, 110, Qt.KeepAspectRatio))
                    self.preview.setPixmap(QPixmap.fromImage(reader.read()))
        self.import_button.setEnabled(self._job is None and self._selected_supported())

    def _import_selected(self):
        if not self._selected_supported() or self._job is not None:
            return
        from .. import wallpaper_engine as we
        project = self._projects[self.project_list.currentRow()]
        if project.import_mode == "video" and not glass.multimedia_available():
            self.status.setText(tr("settings.we.multimedia_missing"))
            return
        self.status.setText(tr("settings.we.importing"))
        self._run(lambda cancel, progress: we.import_project(
            project, os.path.join(config.DATA_DIR, "wallpapers", "managed"), cancel=cancel),
            self._import_complete)

    @Slot(object, object)
    def _import_complete(self, token, result):
        if self._closed or self._job is None or token is not self._job.token:
            return
        self._job = None
        self._set_busy(False)
        if result.kind == "video" and not glass.multimedia_available():
            self.status.setText(tr("settings.we.multimedia_missing"))
            return
        if result.kind == "image" and glass.read_wallpaper(result.path).isNull():
            self.status.setText(tr("settings.glass.invalid"))
            return
        self.imported.emit(result)
        self.accept()

    def cancel_work(self):
        if self._job is not None:
            self._job.cancel.set()
            self._job = None
        self._set_busy(False)
        self.status.setText(tr("settings.we.cancelled"))

    def done(self, result):
        self._closed = True
        self.cancel_work()
        super().done(result)

    def hideEvent(self, event):
        if self._job is not None:
            self.cancel_work()
        super().hideEvent(event)


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
        self._pending_video_restore = None
        self._glass_preview_timer = QTimer(self)
        self._glass_preview_timer.setSingleShot(True)
        self._glass_preview_timer.setInterval(85)
        self._glass_preview_timer.timeout.connect(self._preview_glass)
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
             ("glass", tr("settings.theme.glass")),
             ("system", tr("settings.theme.system"))],
            current=self.db.get_setting(config.S_THEME, config.THEME_SYSTEM),
            parent=general, min_width=150)
        self.theme_select.current_changed.connect(self._on_theme_changed)
        general_layout.addWidget(SettingRow(
            "image", tr("settings.theme"),
            tr("settings.theme.desc"),
            self.theme_select, general))
        layout.addWidget(general)
        self._build_glass_settings(holder, layout)

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
        self.glass_section.setVisible(key == config.THEME_GLASS)
        self.theme_changed.emit(key)

    def _build_glass_settings(self, holder, layout) -> None:
        # Same card header / trailing chevron / clipping animator as notifications.
        from .notify_page import _Section
        options = glass.GlassOptions.read(self.db)
        self.glass_section = _Section(
            "image", tr("settings.glass.title"), tr("settings.glass.desc"), holder,
            expanded=self.db.get_bool(config.S_GLASS_EXPANDED, True))
        self.glass_section.arrow.clicked.connect(lambda: self.db.set_setting(
            config.S_GLASS_EXPANDED, self.glass_section._expanded))
        self.glass_section.setObjectName("Panel")
        content = self.glass_section.content
        column = self.glass_section.content_layout
        column.setContentsMargins(0, config.sx(2), 0, 0)
        column.setSpacing(config.sx(9))

        wallpaper_control = QWidget(content)
        controls = QHBoxLayout(wallpaper_control)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(config.sx(5))
        pick = TextButton(tr("settings.glass.choose"), "ghost", height=32)
        pick.clicked.connect(self._choose_wallpaper)
        self.wallpaper_clear = TextButton(tr("settings.glass.clear"), "subtle", height=32)
        self.wallpaper_clear.clicked.connect(self._clear_wallpaper)
        controls.addWidget(pick)
        controls.addWidget(self.wallpaper_clear)
        column.addWidget(SettingRow("folder", tr("settings.glass.wallpaper"),
                                    tr("settings.glass.wallpaper.desc"), wallpaper_control, content))
        self.wallpaper_name = theme.label("", config.FONT_SMALL, color=config.C_TEXT_MUTED)
        self.wallpaper_name.setWordWrap(True)
        self.wallpaper_name.setContentsMargins(config.sx(16), 0, config.sx(16), config.sx(5))
        column.addWidget(self.wallpaper_name)
        import_button = TextButton(tr("settings.we.open"), "ghost", height=32)
        from ..wallpaper_library import text as wallpaper_text
        import_button.setText(wallpaper_text("打开壁纸库与轮播", "Library and rotation"))
        import_button.clicked.connect(self._open_wallpaper_import)
        column.addWidget(SettingRow("folder", "Wallpaper Engine",
                                    tr("settings.we.short"), import_button, content))
        column.addWidget(Divider(content))

        self.glass_sliders = {}
        self.glass_numbers = {}
        for key, title, description, value, minimum, maximum, suffix in (
            (config.S_GLASS_BLUR, "blur", "blur.desc", options.blur, 0, 64, " px"),
            (config.S_GLASS_OPACITY, "opacity", "opacity.desc", options.opacity, 15, 90, "%"),
            (config.S_GLASS_MASK, "mask", "mask.desc", options.mask, 0, 75, "%"),
        ):
            control = QWidget(content)
            row = QHBoxLayout(control)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(config.sx(9))
            slider = QSlider(Qt.Horizontal, control)
            slider.setRange(minimum, maximum)
            slider.setValue(value)
            slider.setFixedWidth(config.sx(112))
            slider.setAccessibleName(tr("settings.glass." + title))
            number = theme.label(str(value) + suffix, config.FONT_SMALL, color=config.C_TEXT_BODY)
            number.setFixedWidth(config.sx(40))
            number.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(slider)
            row.addWidget(number)
            slider.valueChanged.connect(lambda value, k=key, label=number, s=suffix:
                                        self._glass_value_changed(k, value, label, s))
            self.glass_sliders[key] = slider
            self.glass_numbers[key] = (number, suffix)
            column.addWidget(SettingRow("image", tr("settings.glass." + title),
                                        tr("settings.glass." + description), control, content))
        self.glass_tint = ToggleSwitch(options.wallpaper_tint, content)
        self.glass_tint.toggled.connect(lambda enabled:
                                      self._glass_value_changed(config.S_GLASS_TINT, enabled))
        column.addWidget(SettingRow("image", tr("settings.glass.tint"),
                                    tr("settings.glass.tint.desc"), self.glass_tint, content))
        self.glass_status = theme.label("", config.FONT_SMALL, color=config.C_TEXT_MUTED)
        self.glass_status.setWordWrap(True)
        self.glass_status.setContentsMargins(config.sx(16), config.sx(5), config.sx(16), config.sx(5))
        column.addWidget(self.glass_status)
        footer = QHBoxLayout()
        footer.setContentsMargins(config.sx(16), 0, config.sx(16), config.sx(8))
        footer.addStretch(1)
        self.glass_reset = TextButton(tr("settings.glass.reset"), "subtle", height=32)
        self.glass_reset.clicked.connect(self._reset_glass)
        footer.addWidget(self.glass_reset)
        column.addLayout(footer)
        from . import glass_effects
        glass_effects.add_settings(self, column, SettingRow)
        layout.addWidget(self.glass_section)
        self.glass_section.setVisible(self.db.get_setting(config.S_THEME, config.THEME_SYSTEM) == config.THEME_GLASS)
        self._refresh_wallpaper_controls()
        self.refresh_glass_status()
        root = getattr(self.window(), "centralWidget", lambda: None)()
        if isinstance(root, glass.GlassBackground):
            root.video_status_changed.connect(self.refresh_glass_status)
            root.video_failed.connect(self._video_import_failed)
            root.video_ready.connect(self._video_import_ready)
            root.library.selection_changed.connect(self._library_selection_changed)
            root.library.status_changed.connect(self.glass_status.setText)

    def _glass_value_changed(self, key, value, number=None, suffix="") -> None:
        self.db.set_setting(key, value)
        if number is not None:
            number.setText(str(value) + suffix)
        self._glass_preview_timer.start()

    def _preview_glass(self) -> None:
        glass.configure(self.db)
        if config.current_theme() != config.THEME_GLASS:
            self._refresh_wallpaper_controls()
            glass.refresh_window(self.window(), self.db)
            return
        config.set_theme(config.THEME_GLASS)
        application = QApplication.instance()
        application.setPalette(theme.app_palette())
        application.setStyleSheet(theme.build_stylesheet())
        glass.refresh_window(self.window(), self.db)
        self._refresh_wallpaper_controls()

    def _choose_wallpaper(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, tr("settings.glass.choose"), "", tr("settings.glass.filter"))
        if not path:
            return
        if glass.read_wallpaper(path).isNull():
            QMessageBox.warning(self, tr("settings.glass.wallpaper"), tr("settings.glass.invalid"))
            return
        # Keep the user-selected original; never copy or silently delete source files.
        root = getattr(self.window(), "centralWidget", lambda: None)()
        if isinstance(root, glass.GlassBackground):
            root.library.release_selection()
        self.db.set_setting(config.S_GLASS_WALLPAPER, os.path.abspath(path))
        self.db.set_setting(config.S_GLASS_MEDIA_TYPE, "image")
        self._clear_import_info()
        self._pending_video_restore = None
        self._preview_glass()

    def _clear_wallpaper(self) -> None:
        root = getattr(self.window(), "centralWidget", lambda: None)()
        if isinstance(root, glass.GlassBackground):
            root.library.clear()
        self.db.set_setting(config.S_GLASS_WALLPAPER, "")
        self.db.set_setting(config.S_GLASS_MEDIA_TYPE, "image")
        self._clear_import_info()
        self._pending_video_restore = None
        self._preview_glass()

    def _clear_import_info(self):
        for key in (config.S_GLASS_IMPORT_MODE, config.S_GLASS_IMPORT_TITLE,
                    config.S_GLASS_IMPORT_SOURCE_TYPE):
            self.db.set_setting(key, "")

    def _reset_glass(self):
        self._glass_preview_timer.stop()
        for key, value in ((config.S_GLASS_BLUR, 28), (config.S_GLASS_OPACITY, 44),
                           (config.S_GLASS_MASK, 26), (config.S_GLASS_TINT, True)):
            self.db.set_setting(key, value)
        for key, value in ((config.S_GLASS_BLUR, 28), (config.S_GLASS_OPACITY, 44),
                           (config.S_GLASS_MASK, 26)):
            slider = self.glass_sliders[key]
            slider.blockSignals(True)
            slider.setValue(value)
            slider.blockSignals(False)
            number, suffix = self.glass_numbers[key]
            number.setText(str(value) + suffix)
        self.glass_tint.setChecked(True, animate=False)
        from . import glass_effects
        glass_effects.reset(self)
        self._preview_glass()

    def _open_wallpaper_import(self):
        root = getattr(self.window(), "centralWidget", lambda: None)()
        if not isinstance(root, glass.GlassBackground):
            return
        from .wallpaper_gallery import WallpaperGalleryDialog
        dialog = WallpaperGalleryDialog(root.library, root.rotation, self)
        dialog.open()

    @Slot(str)
    def _library_selection_changed(self, _identity):
        self._refresh_wallpaper_controls()
        self.refresh_glass_status()

    @Slot(object)
    def _apply_wallpaper_import(self, result):
        if result.kind == "video" and not glass.multimedia_available():
            self.glass_status.setText(tr("settings.we.multimedia_missing"))
            return
        root = getattr(self.window(), "centralWidget", lambda: None)()
        if isinstance(root, glass.GlassBackground):
            root.library.release_selection()
        keys = (config.S_GLASS_WALLPAPER, config.S_GLASS_MEDIA_TYPE,
                config.S_GLASS_IMPORT_MODE, config.S_GLASS_IMPORT_TITLE,
                config.S_GLASS_IMPORT_SOURCE_TYPE)
        self._pending_video_restore = ({key: self.db.get_setting(key, "image" if
            key == config.S_GLASS_MEDIA_TYPE else "") for key in keys}
            if result.kind in {"video", "live_scene", "live_web"} else None)
        for key, value in ((config.S_GLASS_WALLPAPER, result.path),
                           (config.S_GLASS_MEDIA_TYPE, result.kind),
                           (config.S_GLASS_IMPORT_MODE, result.mode),
                           (config.S_GLASS_IMPORT_TITLE, result.title),
                           (config.S_GLASS_IMPORT_SOURCE_TYPE, result.source_type)):
            self.db.set_setting(key, value)
        self._preview_glass()

    @Slot()
    def _video_import_ready(self):
        self._pending_video_restore = None

    @Slot(str)
    def _video_import_failed(self, message):
        previous = self._pending_video_restore
        if previous is None:
            self.refresh_glass_status()
            return
        self._pending_video_restore = None
        for key, value in previous.items():
            self.db.set_setting(key, value)
        self._preview_glass()
        self.glass_status.setText(tr("settings.we.failed", error=message))

    def _refresh_wallpaper_controls(self) -> None:
        path = self.db.get_setting(config.S_GLASS_WALLPAPER, "")
        dynamic = self.db.get_setting(config.S_GLASS_MEDIA_TYPE, "image") in {"video", "live_scene", "live_web"}
        valid = bool(path and (os.path.isfile(path) if dynamic else QImageReader(path).canRead()))
        self.wallpaper_name.setText(os.path.basename(path) if valid else
                                    tr("settings.glass.missing") if path else tr("settings.glass.native"))
        title = self.db.get_setting(config.S_GLASS_IMPORT_TITLE, "")
        if title and valid:
            self.wallpaper_name.setText(title + " · " + os.path.basename(path))
        self.wallpaper_name.setToolTip(path)
        self.wallpaper_clear.setEnabled(bool(path))
        self.glass_sliders[config.S_GLASS_BLUR].setEnabled(valid)
        self.glass_tint.setEnabled(valid)

    def refresh_glass_status(self, state=None) -> None:
        root = getattr(self.window(), "centralWidget", lambda: None)()
        if isinstance(root, glass.GlassBackground) and root.video_status:
            if root.options.media_type in {"live_scene", "live_web"}:
                from ..wallpaper_library import text as wallpaper_text
                labels = {"loading": ("实时壁纸 · 正在加载首帧", "Live wallpaper · loading first frame"),
                          "playing": ("实时壁纸正在运行", "Live wallpaper running"),
                          "paused": ("实时壁纸已暂停", "Live wallpaper paused"),
                          "error": ("实时壁纸加载失败", "Live wallpaper failed")}
                text = wallpaper_text(*labels.get(root.video_status, labels["loading"]))
            else:
                text = tr("settings.we.video." + root.video_status)
            if root.video_error:
                text += " · " + root.video_error
            self.glass_status.setText(text)
            return
        valid = bool(isinstance(root, glass.GlassBackground) and (not root._source.isNull() or root._live_ready))
        if valid:
            key = "wallpaper.status"
        elif state is not None and state.available:
            key = "native.status"
        else:
            key = "fallback.status"
        if hasattr(self, "glass_status"):
            text = tr("settings.glass." + key)
            mode = self.db.get_setting(config.S_GLASS_IMPORT_MODE, "")
            if mode:
                text += "\n" + tr("settings.we.mode." + mode)
            self.glass_status.setText(text)

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
