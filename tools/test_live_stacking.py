"""Offscreen live-wallpaper layering checks; no browser or media is started."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QLabel, QWidget
from monitor import config
from monitor.database import Database
from monitor.tracker import ProcessTracker
from monitor.ui import glass, theme
from monitor.ui.main_window import MainWindow

APP = QApplication.instance() or QApplication([])


class FakeLive(QWidget):
    ready = Signal()
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.active = False

    def load(self, path, kind):
        return True

    def set_style(self, **kwargs):
        pass

    def set_active(self, active):
        self.active = active

    def stop(self):
        self.active = False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#ff0000"))


def flush():
    APP.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


class LiveStackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.db = Database(str(Path(self.tmp.name) / "stacking.db"))
        self.tracker = ProcessTracker(self.db)  # Deliberately never started.
        config.set_theme(config.THEME_GLASS)
        APP.setPalette(theme.app_palette())
        APP.setStyleSheet(theme.build_stylesheet())
        self.control = Mock()
        for name, value in {"enabled": False, "has_password": True,
                            "session_grace": 60, "lock_remaining": 0,
                            "all_rules": {}, "self_program_ids": set()}.items():
            getattr(self.control, name).return_value = value
        self.patches = [patch("monitor.ui.live_wallpaper.LiveWallpaperSurface", FakeLive),
                        patch("monitor.autostart.is_enabled", return_value=False),
                        patch("monitor.autostart.is_admin_enabled", return_value=False)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for window in APP.topLevelWidgets():
            window.deleteLater()
        flush()
        for item in reversed(self.patches):
            item.stop()
        self.db.close()
        self.tmp.cleanup()
        config.set_theme(config.THEME_DARK)

    def select_live(self, kind):
        self.db.set_setting(config.S_GLASS_WALLPAPER, "synthetic/project.json")
        self.db.set_setting(config.S_GLASS_MEDIA_TYPE, kind)

    def assert_foreground(self, window):
        root = window.centralWidget()
        body = root.findChild(QWidget, "BodyArea", Qt.FindDirectChildrenOnly)
        self.assertIsNotNone(body)
        self.assertEqual(root.layout().count(), 1)
        self.assertIs(root.layout().itemAt(0).widget(), body)
        self.assertEqual(body.geometry(), root.rect())
        self.assertTrue(body.isVisible())
        self.assertGreater(window.nav.width(), 100)
        self.assertTrue(window.nav.isVisible())
        self.assertTrue(window.stack.isVisible())
        marker = body.findChild(QLabel, "StackingMarker")
        point = marker.mapTo(root, QPoint(5, 5))
        self.assertEqual(root.grab().toImage().pixelColor(point).name(), "#00ff00")
        for target in (window.nav, window.title_bar, window.topbar, window.stack):
            hit = root.childAt(target.mapTo(root, target.rect().center()))
            self.assertIsNotNone(hit)
            while hit is not None and hit is not body:
                hit = hit.parentWidget()
            self.assertIs(hit, body)
        siblings = root.findChildren(QWidget, options=Qt.FindDirectChildrenOnly)
        self.assertLess(siblings.index(root._live_surface), siblings.index(root._live_overlay))
        self.assertLess(siblings.index(root._live_overlay), siblings.index(body))

    def test_live_startup_and_runtime_selection_keep_foreground(self):
        for startup in (True, False):
            for kind in ("live_scene", "live_web"):
                with self.subTest(startup=startup, kind=kind):
                    self.db.set_setting(config.S_GLASS_WALLPAPER, "")
                    self.db.set_setting(config.S_GLASS_MEDIA_TYPE, "image")
                    if startup:
                        self.select_live(kind)
                    window = MainWindow(self.db, self.tracker, control=self.control)
                    body = window.centralWidget().findChild(QWidget, "BodyArea", Qt.FindDirectChildrenOnly)
                    marker = QLabel("FOREGROUND", body)
                    marker.setObjectName("StackingMarker")
                    marker.setGeometry(300, 220, 300, 60)
                    marker.setStyleSheet("background:#00ff00;color:#000000")
                    marker.show()
                    window.resize(1100, 760)
                    window.show()
                    flush()
                    if not startup:
                        self.select_live(kind)
                        window.centralWidget().refresh()
                    root = window.centralWidget()
                    root._live_surface.ready.emit()
                    flush()
                    self.assert_foreground(window)
                    for size in ((1300, 850), (950, 650)):
                        window.resize(*size)
                        flush()
                        self.assert_foreground(window)
                    window.hide()
                    window.show()
                    flush()
                    self.assert_foreground(window)
                    self.db.set_setting(config.S_GLASS_WALLPAPER, "other/project.json")
                    root.refresh()
                    root._live_surface.ready.emit()
                    flush()
                    self.assert_foreground(window)
                    window.deleteLater()
                    flush()


if __name__ == "__main__":
    unittest.main(verbosity=2)
