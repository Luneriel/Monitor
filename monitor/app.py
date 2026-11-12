"""应用装配：QApplication、托盘图标、启动流程与自检模式。"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import autostart, config, icon_extract, util
from .control_service import ControlService
from .database import Database
from .i18n import set_language, tr
from .tracker import ProcessTracker
from .ui import theme, glass
from .ui.main_window import MainWindow


def quit_needs_password(service) -> bool:
    """退出 Monitor 前是否必须验密码（用户 ⑤ 的口径）。

    只有"启用控制"真的开着时才需要：控制开着意味着正在按规则遮挡网站，这时候
    随手退出就等于绕过控制。控制没开（含还没设过密码）一律不问。
    """
    if service is None:
        return False
    try:
        return bool(service.has_password() and service.engine.enabled)
    except Exception:
        return False


class MonitorApp:
    """把数据库、监控线程、主窗口与托盘串起来。"""

    def __init__(self, app: QApplication, db: Database, silent: bool = False,
                 size: Optional[tuple[int, int]] = None) -> None:
        self.app = app
        self.db = db
        self.silent = silent
        self._quitting = False
        self._size = size
        self._theme_setting = db.get_setting(config.S_THEME, config.THEME_SYSTEM)

        self.tracker = ProcessTracker(db)
        self.tray = self._build_tray()
        # 控制功能（规则引擎 + 遮罩 + 通知）在窗口之前建好，页面构造时即可使用
        self.control = ControlService(db, self.tracker, tray=self.tray, parent=self.app)
        self.window = MainWindow(db, self.tracker, control=self.control)
        self._wire_window(self.window)
        if size:
            self.window.resize(size[0], size[1])

        self.control.unlock_requested.connect(self._on_unlock_requested)
        self.control.close_program_requested.connect(self._on_close_program)
        # 程序打开 / 关闭的通知
        self.tracker.instance_started.connect(self._on_instance_started)
        self.tracker.instance_stopped.connect(self._on_instance_stopped)
        self.tracker.programs_changed.connect(self._refresh_control_rules)

        self.tracker.set_programs(db.programs(visible_only=True))
        self.tracker.start()

        if not silent:
            self.show_window()

    # ------------------------------------------------------------------
    def _wire_window(self, window) -> None:
        window.quit_requested.connect(self.quit)
        window.tray_icon_change_requested.connect(lambda _p: self.refresh_tray_icon())
        window.theme_change_requested.connect(self._on_theme_changed)
        window.language_change_requested.connect(self._on_language_changed)
        window.tray_icon = self.tray                       # 通知页"发送测试"要用它
        # 关机 / 注销时 Windows 会让 Qt 走会话管理：这两种场合不问密码（用户 ⑤）。
        for name in ("saveStateRequest", "commitDataRequest"):
            signal = getattr(self.app, name, None)
            if signal is not None:
                try:
                    signal.connect(self._on_session_end)
                except Exception:
                    pass

    def _on_session_end(self, *_args) -> None:
        """系统关机 / 注销：跳过密码闸门，按正常流程退出（音量等会被还原）。"""
        self.quit(force=True)

    # ------------------------------------------------------------------
    # 遮罩上的"关闭程序"：确认后正常结束该程序的所有进程；
    # 程序未响应时再询问是否强制结束。
    # ------------------------------------------------------------------
    def _program_pids(self, program_id: int) -> list[int]:
        try:
            return [int(pid) for pid, _start in
                    self.tracker.snapshot().get(int(program_id), [])]
        except Exception:
            return []

    def _terminate_pids(self, pids: list, force: bool = False) -> list:
        """结束进程，返回仍然存活的 PID 列表。"""
        import psutil

        for pid in pids:
            try:
                process = psutil.Process(int(pid))
                if force:
                    process.kill()
                else:
                    process.terminate()
            except Exception:
                continue
        deadline = time.time() + (0.6 if force else 3.0)
        while time.time() < deadline:
            alive = [pid for pid in pids if psutil.pid_exists(int(pid))]
            if not alive:
                return []
            QApplication.processEvents()
            time.sleep(0.05)
        return [pid for pid in pids if psutil.pid_exists(int(pid))]

    def _on_close_program(self, program_id: int) -> None:
        from PySide6.QtWidgets import QMessageBox

        program = self.db.get_program(int(program_id))
        name = str(program["name"]) if program else str(program_id)
        pids = self._program_pids(int(program_id))
        if not pids:
            return
        # 遮罩不收起来（收起来等于白送一次解锁），只把确认框置顶显示。
        box = QMessageBox(self.window)
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        box.setWindowTitle(tr("control.close.title"))
        box.setIcon(QMessageBox.Question)
        box.setText(tr("control.close.confirm", name=name))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        remaining = self._terminate_pids(pids)
        if remaining:
            force = QMessageBox(self.window)
            force.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            force.setWindowTitle(tr("control.close.title"))
            force.setIcon(QMessageBox.Warning)
            force.setText(tr("control.close.timeout", name=name))
            force.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            force.setDefaultButton(QMessageBox.No)
            if force.exec() == QMessageBox.Yes:
                self._terminate_pids(remaining, force=True)
        self.control.notifier.on_program_event("close", name, program_id=int(program_id))

    # ------------------------------------------------------------------
    # 程序打开 / 关闭的通知与控制规则同步
    # ------------------------------------------------------------------
    def _on_instance_started(self, program_id: int, _pid: int, _ts: float) -> None:
        program = self.db.get_program(int(program_id))
        if program is None:
            return
        self.control.notifier.on_program_event("open", str(program["name"]),
                                               program_id=int(program_id))
        self.control.notifier.reset_duration_marks(int(program_id))

    def _on_instance_stopped(self, program_id: int, _pid: int, _ts: float) -> None:
        program = self.db.get_program(int(program_id))
        if program is None:
            return
        self.control.notifier.on_program_event("close", str(program["name"]),
                                               program_id=int(program_id))

    def _refresh_control_rules(self) -> None:
        """程序列表变化时重载控制规则（程序可能被删除）。"""
        self.control.reload()

    # ------------------------------------------------------------------
    # 遮罩上的"临时解锁"：验证密码后按分钟数解锁
    # ------------------------------------------------------------------
    def _on_unlock_requested(self, program_id: int) -> None:
        from .ui.control_page import _PasswordDialog
        from PySide6.QtWidgets import QDialog

        service = self.control
        try:
            window = self.window
        except Exception:
            window = None
        dialog = _PasswordDialog(window, tr("control.unlock.title"),
                                 tr("control.unlock.hint"), minutes=True)
        # 遮罩不再收起来：弹窗本身是模态窗口，就在最上面。这样即使点开弹窗又取消，
        # 目标程序依然被遮罩挡着、输入依然被拦截。
        dialog.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        dialog.raise_()
        dialog.activateWindow()
        while dialog.exec() == QDialog.Accepted:
            remaining = service.lock_remaining()
            if remaining > 0:
                dialog.set_error(tr("control.gate.locked",
                                    seconds=max(1, int(remaining + 0.999))))
                continue
            ok, reason = service.verify(dialog.password())
            if ok:
                service.unlock(int(program_id), dialog.value_minutes)
                return
            if reason == "locked":
                dialog.set_error(tr("control.gate.locked",
                                    seconds=max(1, int(service.lock_remaining() + 0.999))))
                continue
            dialog.set_error(tr("control.error.wrong"))
            dialog.edit.clear()

    # ------------------------------------------------------------------
    # 语言（中文 / English）
    # ------------------------------------------------------------------
    def _on_language_changed(self, code: str) -> None:
        set_language(code)
        QTimer.singleShot(0, self._rebuild_window)

    # ------------------------------------------------------------------
    # 主题（浅色 / 深色 / 跟随系统）
    # ------------------------------------------------------------------
    def _on_theme_changed(self, name: str) -> None:
        self._theme_setting = name
        self.apply_theme(name, rebuild=True)

    def apply_theme(self, name: str, rebuild: bool = False) -> None:
        glass.configure(self.db)
        config.set_theme(name)
        self.app.setPalette(theme.app_palette())
        self.app.setStyleSheet(theme.build_stylesheet())
        if rebuild:
            # 颜色令牌在构件时被写入各处样式，最稳妥的做法是重建主窗口
            QTimer.singleShot(0, self._rebuild_window)

    def _rebuild_window(self) -> None:
        """原地重建界面：复用同一个窗口句柄，避免整窗闪烁与提示气泡。"""
        try:
            self.window.rebuild_ui()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 托盘
    # ------------------------------------------------------------------
    def tray_icon(self) -> QIcon:
        custom = self.db.get_setting(config.S_TRAY_ICON, "")
        if custom and os.path.exists(custom):
            icon = QIcon(custom)
            if not icon.isNull():
                return icon
        path = icon_extract.save_default_tray_icon()
        if path and os.path.exists(path):
            return QIcon(path)
        return QIcon(icon_extract.default_tray_pixmap(64))

    def _build_tray(self) -> QSystemTrayIcon:
        tray = QSystemTrayIcon(self.tray_icon(), self.app)
        tray.setToolTip(tr("tray.tooltip"))
        menu = QMenu()
        show_action = QAction(tr("tray.show"), menu)
        show_action.triggered.connect(self.show_window)
        quit_action = QAction(tr("tray.quit"), menu)
        quit_action.triggered.connect(lambda: self.quit())    # 会走退出闸门（可能要密码）
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        return tray

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_window()

    def refresh_tray_icon(self) -> None:
        self.tray.setIcon(self.tray_icon())

    # ------------------------------------------------------------------
    def show_window(self) -> None:
        self.window.show()
        self.window.setWindowState(
            (self.window.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        self.window.raise_()
        self.window.activateWindow()

    def toggle_window(self) -> None:
        if self.window.isVisible() and not self.window.isMinimized():
            self.window.hide()
        else:
            self.show_window()

    def _pass_quit_gate(self) -> bool:
        """用户主动退出时的密码闸门：只有"启用控制"开着才要密码（用户 ⑤）。"""
        service = getattr(self, "control", None)
        if not quit_needs_password(service):
            return True
        from .ui.control_page import ask_exit_password

        return ask_exit_password(self.window, service)

    def quit(self, force: bool = False) -> None:
        """按正常流程退出：先停监控线程并闭合日志，再收托盘与窗口，最后关数据库。

        ``force=True`` 用于关机 / 注销 / ``--quit-after`` 这些"不是用户主动点退出"
        的场合（用户 ⑤ 的口径：关机与注销不拦）。用户主动退出时，若"启用控制"
        开着，先验一次密码（每次都问）。
        """
        if self._quitting:
            return
        if not force and not self._pass_quit_gate():
            return
        self._quitting = True
        try:
            self.control.shutdown()
        except Exception:
            pass
        try:
            self.tracker.stop()
            self.tracker.wait(3000)
            self.tracker.close_all()
        except Exception:
            pass
        try:
            self.tray.hide()
        except Exception:
            pass
        try:
            self.window.prepare_quit()
        except Exception:
            pass
        self.app.quit()

    def shutdown(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def run(self) -> int:
        code = self.app.exec()
        self.shutdown()
        return code


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="Monitor", description="Monitor 追踪计时程序")
    parser.add_argument("--silent", action="store_true", help="静默启动（仅托盘图标）")
    parser.add_argument("--windowed", action="store_true", help="强制显示主窗口")
    parser.add_argument("--db", default="", help="指定数据库文件（默认 data/monitor.db）")
    parser.add_argument("--selftest", action="store_true", help="启动自检并自动退出")
    parser.add_argument("--shots", default="", help="自检截图输出目录")
    parser.add_argument("--quit-after", type=float, default=0.0,
                        help="N 秒后按正常流程退出（冒烟测试用）")
    return parser


def create_qapplication(argv: list[str]) -> QApplication:
    # QtWebEngine is loaded lazily when a live wallpaper is selected.
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationName(config.APP_NAME)
    app.setApplicationDisplayName(config.APP_NAME)
    app.setOrganizationName(config.APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setPalette(theme.app_palette())
    app.setFont(theme.font(config.FONT_BODY))
    app.setStyleSheet(theme.build_stylesheet())
    return app


def initial_window_geometry() -> tuple[int, int]:
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return config.BASE_WINDOW_W, config.BASE_WINDOW_H
    geometry = screen.geometry()
    return config.initial_window_size(geometry.width(), geometry.height())


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    parser = build_argument_parser()
    args = parser.parse_args(argv[1:])

    config.ensure_dirs()
    app = create_qapplication(argv)

    width, height = initial_window_geometry()
    config.set_ui_scale(width / config.BASE_WINDOW_W)

    db_path = args.db or config.DB_PATH
    if args.selftest:
        from .selftest import _soften_console, real_db_conflict, selftest_refuse_text

        # 自检的第一步就是删除并重建目标库：一旦落在真实库上就会清空用户的程序、
        # 规则与密码（曾经真的发生过），所以宁可直接拒绝启动。
        if real_db_conflict(args.db):
            _soften_console()
            print(selftest_refuse_text(args.db))
            return 2
        # 自检必须从干净的数据库开始：否则上一轮"添加程序"写入的记录会被弹窗当作
        # 已存在而过滤掉，导致条目为空、确认框弹出警告后卡在模态循环里。
        for suffix in ("", "-wal", "-shm"):
            candidate = db_path + suffix
            if os.path.exists(candidate):
                try:
                    os.remove(candidate)
                except OSError:
                    pass

    db = Database(db_path)

    if args.selftest:
        from .selftest import run_selftest

        return run_selftest(app, db, args.shots)

    # 先按设置解析语言与主题，再创建窗口（文案与颜色在构件时生效）
    language = db.get_setting(config.S_LANGUAGE, config.LANGUAGE_ZH)
    set_language(language)
    theme_setting = db.get_setting(config.S_THEME, config.THEME_SYSTEM)
    glass.configure(db)
    config.set_theme(theme_setting)
    app.setPalette(theme.app_palette())
    app.setStyleSheet(theme.build_stylesheet())

    silent_setting = db.get_bool(config.S_SILENT_START, False)
    silent = bool(args.silent or (silent_setting and not args.windowed))

    # "以管理员身份启动"是两段式：用户在设置页勾选后需要提权重启，重启后在这里
    # 才真正写入计划任务（同时也修复计划任务被外部删掉的情况）。
    if db.get_bool(config.S_ADMIN_START, False) and util.is_admin() \
            and not autostart.is_admin_enabled():
        autostart.apply(True, silent_setting, admin=True)

    monitor = MonitorApp(app, db, silent=silent, size=(width, height))
    if args.quit_after > 0:
        QTimer.singleShot(int(args.quit_after * 1000), lambda: monitor.quit(force=True))
    return monitor.run()
