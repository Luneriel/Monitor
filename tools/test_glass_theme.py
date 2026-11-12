"""Glass regression tests and optional review images. Uses a temporary DB only.

python tools/test_glass_theme.py
python tools/test_glass_theme.py --shots outputs/glass-review
"""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import argparse
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication, QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QFontDatabase, QImage, QLinearGradient, QPainter
from PySide6.QtWidgets import QApplication, QMainWindow
from monitor import config
from monitor.database import Database
from monitor.i18n import set_language
from monitor.ui import glass, theme
from monitor.ui.settings_page import SettingsPage

APP = QApplication.instance() or QApplication([])
for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
    path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
    if path.exists():
        QFontDatabase.addApplicationFont(str(path))


def flush():
    APP.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def sample_wallpaper(path):
    image = QImage(1500, 1000, QImage.Format_RGB32)
    painter = QPainter(image)
    gradient = QLinearGradient(0, 0, 1400, 1000)
    gradient.setColorAt(0, QColor("#657BD3"))
    gradient.setColorAt(.40, QColor("#CC9EAF"))
    gradient.setColorAt(.72, QColor("#457D98"))
    gradient.setColorAt(1, QColor("#125B60"))
    painter.fillRect(image.rect(), gradient)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#4999BB"))
    painter.drawEllipse(QRectF(-150, 540, 1400, 900))
    painter.setBrush(QColor("#9FAEE4"))
    painter.drawEllipse(QRectF(900, -400, 1000, 1200))
    painter.end()
    image.save(str(path))


class GlassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.db = Database(str(Path(self.tmp.name) / "glass-test.db"))
        set_language("zh")

    def tearDown(self):
        for window in APP.topLevelWidgets():
            window.deleteLater()
        flush()
        self.db.close()
        self.tmp.cleanup()
        config.set_theme(config.THEME_DARK)

    def test_persistence_and_corrupt_value_clamping(self):
        self.db.set_setting(config.S_GLASS_BLUR, "nan")
        self.db.set_setting(config.S_GLASS_OPACITY, 999)
        self.db.set_setting(config.S_GLASS_MASK, -1)
        options = glass.configure(self.db)
        self.assertEqual((options.blur, options.opacity, options.mask), (28, 90, 0))
        config.set_theme(config.THEME_GLASS)
        self.assertEqual(config.current_theme(), "glass")
        self.assertLess(QColor(config.C_SURFACE).alpha(), 255)
        self.db.set_setting(config.S_GLASS_WALLPAPER, "sample.png")
        reopened = Database(str(Path(self.tmp.name) / "glass-test.db"))
        self.assertEqual(reopened.get_setting(config.S_GLASS_WALLPAPER), "sample.png")
        reopened.close()

    def test_image_is_bounded_and_blur_is_cached(self):
        config.set_theme(config.THEME_GLASS)
        image_path = Path(self.tmp.name) / "wallpaper.png"
        sample_wallpaper(image_path)
        self.db.set_setting(config.S_GLASS_WALLPAPER, image_path)
        with patch.object(glass, "_blur_image", wraps=glass._blur_image) as blur:
            root = glass.GlassBackground(self.db)
            self.assertFalse(root._image.isNull())
            root.resize(1100, 760)
            root.refresh()
            root.resize(900, 600)
            root.grab()
            self.assertEqual(blur.call_count, 1)
            self.db.set_setting(config.S_GLASS_MASK, 40)
            root.refresh()
            self.assertEqual(blur.call_count, 1)
            self.db.set_setting(config.S_GLASS_BLUR, 48)
            root.refresh()
            self.assertEqual(blur.call_count, 2)
            self.assertLessEqual(root._source.width(), 1600)
            self.assertLessEqual(root._image.width(), 800)

    def test_native_dwm_and_disable_contract_without_hwnd_calls(self):
        calls = []
        frames = []
        dwm = Mock()
        dwm.DwmSetWindowAttribute.side_effect = lambda hwnd, key, data, size: calls.append((key, data._obj.value)) or 0
        dwm.DwmExtendFrameIntoClientArea.side_effect = lambda hwnd, data: frames.append(
            tuple(getattr(data._obj, field) for field in ("left", "right", "top", "bottom"))) or 0
        user = Mock()
        with patch.object(glass.ctypes, "WinDLL", side_effect=[dwm, user, dwm, user]):
            state = glass._native_backdrop(123, True, 44)
            self.assertTrue(state.available)
            self.assertEqual(state.mode, "dwm-acrylic")
            self.assertIn((38, 3), calls)
            disabled = glass._native_backdrop(123, False, 44)
            self.assertEqual(disabled.mode, "off")
            self.assertIn((38, 1), calls)
            self.assertEqual(frames, [(-1, -1, -1, -1), (1, 1, 1, 1)])

    def test_native_accent_failure_is_explicit_fallback(self):
        dwm = Mock()
        dwm.DwmSetWindowAttribute.return_value = -1
        dwm.DwmExtendFrameIntoClientArea.return_value = 0
        user = Mock()
        user.SetWindowCompositionAttribute.return_value = 0
        with patch.object(glass.ctypes, "WinDLL", side_effect=[dwm, user]):
            state = glass._native_backdrop(123, True, 44)
        self.assertFalse(state.available)
        self.assertEqual(state.mode, "fallback")
        self.assertEqual(user.SetWindowCompositionAttribute.call_count, 2)

    def test_non_glass_never_decodes_wallpaper(self):
        config.set_theme(config.THEME_DARK)
        self.db.set_setting(config.S_GLASS_WALLPAPER, "unused.png")
        with patch.object(glass, "read_wallpaper", side_effect=AssertionError("dark wallpaper decode")):
            root = glass.GlassBackground(self.db)
            root.refresh()

    def test_missing_wallpaper_and_offscreen_never_calls_windows(self):
        config.set_theme(config.THEME_GLASS)
        self.db.set_setting(config.S_GLASS_WALLPAPER, "missing-image.png")
        window = QMainWindow()
        window.setCentralWidget(glass.GlassBackground(self.db, window))
        with patch.object(glass, "_native_backdrop", side_effect=AssertionError("native call in test")):
            state = glass.refresh_window(window, self.db)
        self.assertFalse(state.available)
        self.assertEqual(state.mode, "fallback")
        self.assertFalse(window.centralWidget().native_available)

    @patch("monitor.autostart.is_enabled", return_value=False)
    @patch("monitor.autostart.is_admin_enabled", return_value=False)
    def test_settings_live_preview_and_reset(self, *_mocks):
        self.db.set_setting(config.S_THEME, config.THEME_GLASS)
        glass.configure(self.db)
        config.set_theme(config.THEME_GLASS)
        window = QMainWindow()
        root = glass.GlassBackground(self.db, window)
        window.setCentralWidget(root)
        page = SettingsPage(self.db, root)
        window.settings_page = page
        self.assertFalse(page.glass_sliders[config.S_GLASS_BLUR].isEnabled())
        page.glass_sliders[config.S_GLASS_OPACITY].setValue(72)
        page._glass_preview_timer.stop()
        page._preview_glass()
        self.assertEqual(glass.GlassOptions.read(self.db).opacity, 72)
        self.assertEqual(root.options.opacity, 72)
        self.assertGreater(QColor(config.C_SURFACE).alpha(), 170)
        page.glass_section.arrow.click()
        self.assertFalse(self.db.get_bool(config.S_GLASS_EXPANDED, True))
        page.glass_section.arrow.click()
        self.assertTrue(self.db.get_bool(config.S_GLASS_EXPANDED, False))


def export_shots(directory):
    from monitor.tracker import ProcessTracker
    from monitor.ui.main_window import MainWindow
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
        db = Database(str(Path(temporary) / "review.db"))
        wallpaper = Path(temporary) / "review-wallpaper.png"
        sample_wallpaper(wallpaper)
        tracker = ProcessTracker(db)  # Not started; no process or performance sampling.
        service = Mock()
        service.enabled.return_value = False
        service.has_password.return_value = True
        service.session_grace.return_value = 60
        service.lock_remaining.return_value = 0
        service.all_rules.return_value = {}
        service.self_program_ids.return_value = set()
        with patch("monitor.autostart.is_enabled", return_value=False), patch("monitor.autostart.is_admin_enabled", return_value=False):
            for name, language in (("light", "zh"), ("dark", "zh"), ("glass", "zh"), ("glass", "en")):
                set_language(language)
                db.set_setting(config.S_THEME, name)
                db.set_setting(config.S_GLASS_WALLPAPER, wallpaper)
                glass.configure(db)
                config.set_theme(name)
                APP.setPalette(theme.app_palette())
                APP.setStyleSheet(theme.build_stylesheet())
                window = MainWindow(db, tracker, control=service)
                window.resize(1120, 900)
                window.switch_page(4, animate=False)
                window.show()
                flush()
                window.grab().save(str(directory / f"{name}-{language}.png"))
                window.deleteLater()
                flush()
        db.close()
    print("Review images:", directory)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots")
    args, remaining = parser.parse_known_args()
    result = unittest.main(argv=[sys.argv[0]] + remaining, verbosity=2, exit=False)
    if not result.result.wasSuccessful():
        raise SystemExit(1)
    if args.shots:
        export_shots(args.shots)
