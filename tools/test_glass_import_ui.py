"""Isolated offscreen import/reset/video contracts; never plays user media."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThreadPool, QUrl, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QMainWindow
from monitor import config, wallpaper_engine as we
from monitor.database import Database
from monitor.i18n import set_language
from monitor.ui import glass
from monitor.ui.settings_page import SettingsPage, WallpaperImportDialog, _ImportJob

APP = QApplication.instance() or QApplication([])


def flush():
    APP.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def wait_until(condition):
    deadline = time.monotonic() + 3
    while not condition() and time.monotonic() < deadline:
        flush()
        time.sleep(.005)
    if not condition():
        raise AssertionError("worker did not complete")


class FakePlayer(QObject):
    errorOccurred = Signal(object, str)
    Infinite = -1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.play = Mock()
        self.pause = Mock()
        self.stop = Mock()
        self.setSource = Mock()
        self.setAudioOutput = Mock()
        self.setVideoSink = Mock()
        self.setLoops = Mock()


class FakeAudio(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMuted = Mock()
        self.setVolume = Mock()


class FakeSink(QObject):
    videoFrameChanged = Signal(object)


FAKE_MULTIMEDIA = SimpleNamespace(QMediaPlayer=FakePlayer, QAudioOutput=FakeAudio,
                                   QVideoSink=FakeSink)


class ImportUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.directory = Path(self.tmp.name)
        self.db = Database(str(self.directory / "test.db"))
        set_language("zh")
        self.db.set_setting(config.S_THEME, config.THEME_GLASS)
        config.set_theme(config.THEME_GLASS)
        self.image = self.directory / "sample.png"
        image = QImage(1280, 720, QImage.Format_RGB32)
        image.fill(QColor("#5378ab"))
        image.save(str(self.image))
        self.video = self.directory / "synthetic.mp4"
        self.video.write_bytes(b"synthetic test only; player always mocked")
        self.patches = [patch("monitor.autostart.is_enabled", return_value=False),
                        patch("monitor.autostart.is_admin_enabled", return_value=False)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for window in APP.topLevelWidgets():
            window.deleteLater()
        flush()
        self.assertTrue(QThreadPool.globalInstance().waitForDone(3000))
        for item in reversed(self.patches):
            item.stop()
        self.db.close()
        self.tmp.cleanup()
        config.set_theme(config.THEME_DARK)

    def window(self, path=None, media_type="image"):
        self.db.set_setting(config.S_GLASS_WALLPAPER, path or "")
        self.db.set_setting(config.S_GLASS_MEDIA_TYPE, media_type)
        window = QMainWindow()
        root = glass.GlassBackground(self.db, window)
        window.setCentralWidget(root)
        page = SettingsPage(self.db, root)
        window.settings_page = page
        return window, root, page

    def result(self, kind="image", mode="image"):
        return we.ImportResult("sample", "Sample project", str(self.video if kind == "video"
            else self.image), kind, "scene" if mode == "static_preview" else kind,
            mode, "sample", False)

    def test_reset_preserves_wallpaper_metadata_expansion_and_business_settings(self):
        self.db.set_setting(config.S_GLASS_EXPANDED, False)
        self.db.set_setting(config.S_LANGUAGE, "en")
        self.db.set_setting(config.S_GLASS_IMPORT_MODE, "static_preview")
        self.db.set_setting(config.S_GLASS_IMPORT_TITLE, "Scene sample")
        self.db.set_setting(config.S_GLASS_IMPORT_SOURCE_TYPE, "scene")
        self.db.set_setting(config.S_SESSION_GRACE, 17)
        self.db.set_setting(config.S_SILENT_START, True)
        window, root, page = self.window(self.image)
        source_key = root._source_key
        for key, value in ((config.S_GLASS_BLUR, 60), (config.S_GLASS_OPACITY, 80),
                           (config.S_GLASS_MASK, 70)):
            page.glass_sliders[key].setValue(value)
        page.glass_tint.setChecked(False)
        page._glass_value_changed(config.S_GLASS_TINT, False)
        page._reset_glass()
        self.assertEqual(glass.GlassOptions.read(self.db), glass.GlassOptions(str(self.image)))
        self.assertEqual(root._source_key, source_key)
        self.assertFalse(root._source.isNull())
        self.assertFalse(page.glass_section._expanded)
        for key, value in ((config.S_GLASS_BLUR, 28), (config.S_GLASS_OPACITY, 44),
                           (config.S_GLASS_MASK, 26)):
            self.assertEqual(page.glass_sliders[key].value(), value)
            label, suffix = page.glass_numbers[key]
            self.assertEqual(label.text(), str(value) + suffix)
        self.assertTrue(page.glass_tint.isChecked())
        self.assertFalse(page._glass_preview_timer.isActive())
        self.assertEqual(self.db.get_setting(config.S_GLASS_IMPORT_MODE), "static_preview")
        self.assertEqual(self.db.get_setting(config.S_GLASS_IMPORT_TITLE), "Scene sample")
        self.assertEqual(self.db.get_setting(config.S_GLASS_IMPORT_SOURCE_TYPE), "scene")
        self.assertEqual(self.db.get_setting(config.S_THEME), "glass")
        self.assertEqual(self.db.get_setting(config.S_LANGUAGE), "en")
        self.assertEqual(self.db.get_float(config.S_SESSION_GRACE), 17)
        self.assertTrue(self.db.get_bool(config.S_SILENT_START))
        self.assertTrue(self.image.exists())

    def test_image_import_uses_existing_wallpaper_and_explicit_static_mode(self):
        window, root, page = self.window()
        page._apply_wallpaper_import(self.result(mode="static_preview"))
        self.assertEqual(self.db.get_setting(config.S_GLASS_MEDIA_TYPE), "image")
        self.assertFalse(root._image.isNull())
        self.assertIn("仅静态预览", page.glass_status.text())
        page._clear_wallpaper()
        self.assertEqual(self.db.get_setting(config.S_GLASS_IMPORT_MODE), "")
        self.assertTrue(self.image.exists())

    def test_missing_multimedia_preserves_wallpaper(self):
        window, root, page = self.window(self.image)
        with patch.object(glass, "multimedia_available", return_value=False):
            page._apply_wallpaper_import(self.result("video", "video"))
        self.assertEqual(self.db.get_setting(config.S_GLASS_WALLPAPER), str(self.image))
        self.assertIn("QtMultimedia", page.glass_status.text())

    def test_decode_failure_restores_previous_image_and_metadata(self):
        window, root, page = self.window(self.image)
        with patch.dict(sys.modules, {"PySide6.QtMultimedia": FAKE_MULTIMEDIA}):
            page._apply_wallpaper_import(self.result("video", "video"))
            self.assertFalse(root._source.isNull())  # old material remains as poster
            root._on_video_error(None, "synthetic decoder failure")
        self.assertEqual(self.db.get_setting(config.S_GLASS_WALLPAPER), str(self.image))
        self.assertEqual(self.db.get_setting(config.S_GLASS_MEDIA_TYPE), "image")
        self.assertFalse(root._image.isNull())
        self.assertIn("synthetic decoder failure", page.glass_status.text())

    def test_video_frame_throttle_size_lifecycle_and_reset_does_not_reload(self):
        with patch.dict(sys.modules, {"PySide6.QtMultimedia": FAKE_MULTIMEDIA}):
            window, root, page = self.window(self.video, "video")
            self.db.set_setting(config.S_GLASS_IMPORT_MODE, "video")
            self.db.set_setting(config.S_GLASS_IMPORT_TITLE, "Video sample")
            player = root._player
            self.assertEqual(player.setLoops.call_args.args, (-1,))
            self.assertEqual(root._audio.setMuted.call_args.args, (True,))
            self.assertEqual(root._audio.setVolume.call_args.args, (0,))
            window.show()
            flush()
            self.assertTrue(player.play.called)
            frame = Mock()
            frame.toImage.return_value = QImage(str(self.image))
            with patch.object(glass.time, "monotonic", side_effect=[10., 10.05, 10.10, 10.25]), \
                    patch.object(glass, "_blur_image", wraps=glass._blur_image) as blur:
                for _ in range(4):
                    root._on_video_frame(frame)
                self.assertEqual(frame.toImage.call_count, 2)
                self.assertEqual(blur.call_count, 2)
                self.assertLessEqual(root._source.width(), 480)
                self.assertLessEqual(root._image.height(), 330)
            source_calls = player.setSource.call_count
            stop_calls = player.stop.call_count
            self.db.set_setting(config.S_GLASS_BLUR, 60)
            root.refresh()
            page._reset_glass()
            self.assertIs(root._player, player)
            self.assertEqual(player.setSource.call_count, source_calls)
            self.assertEqual(player.stop.call_count, stop_calls)
            self.assertEqual(self.db.get_setting(config.S_GLASS_WALLPAPER), str(self.video))
            self.assertEqual(self.db.get_setting(config.S_GLASS_MEDIA_TYPE), "video")
            self.assertEqual(self.db.get_setting(config.S_GLASS_IMPORT_TITLE), "Video sample")
            window.showMinimized()
            flush()
            self.assertFalse(root._video_should_play())
            self.assertEqual(root.video_status, "paused")
            window.showNormal()
            flush()
            self.assertTrue(root._video_should_play())
            window.hide()
            self.assertEqual(root.video_status, "paused")
            window.show()
            flush()
            config.set_theme(config.THEME_DARK)
            root.refresh()
            self.assertIsNone(root._player)
            self.assertEqual(root.video_status, "")
            flush()
            from shiboken6 import isValid
            self.assertFalse(isValid(player))
            config.set_theme(config.THEME_GLASS)
            root.refresh()
            self.assertTrue(root._video_should_play())
            page._clear_wallpaper()
            self.assertEqual(root._video_path, "")
            self.assertTrue(player.stop.called)
            self.assertEqual(player.setSource.call_args.args, (QUrl(),))

    def test_scan_runs_off_main_thread_and_reports_modes(self):
        dialog = WallpaperImportDialog()
        project = we.WallpaperProject("x", "Scene title", str(self.directory), "scene", "",
                                       str(self.image), "static_preview", "Preview only")
        threads = []
        def scan(roots, cancel=None, progress=None):
            threads.append(threading.get_ident())
            progress("synthetic scan")
            return we.ScanResult((project,))
        with patch.object(we, "discover_roots", return_value=(self.directory,)), \
                patch.object(we, "scan_projects", side_effect=scan):
            dialog.start_scan(str(self.directory))
            wait_until(lambda: dialog._job is None)
        self.assertNotEqual(threads[0], threading.get_ident())
        self.assertIn("仅静态预览", dialog.project_list.item(0).text())
        self.assertIn("Preview only", dialog.detail.text())
        self.assertTrue(dialog.import_button.isEnabled())

    def test_cancel_discards_late_result_and_close_cancels_worker(self):
        dialog = WallpaperImportDialog()
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def operation(cancel, progress):
            started.set()
            release.wait(2)
            finished.set()
            return we.ScanResult(())
        dialog._run(operation, dialog._scan_complete)
        wait_until(started.is_set)
        old = dialog._job
        dialog.cancel_work()
        self.assertTrue(old.cancel.is_set())
        new = _ImportJob(lambda cancel, progress: None)
        dialog._job = new
        dialog._scan_complete(old.token, we.ScanResult(()))
        self.assertIs(dialog._job, new)
        dialog._job = old
        dialog.reject()
        flush()
        release.set()
        wait_until(finished.is_set)
        self.assertTrue(old.cancel.is_set())

    def test_failed_worker_does_not_emit_import(self):
        dialog = WallpaperImportDialog()
        imported = Mock()
        dialog.imported.connect(imported)
        def operation(cancel, progress):
            raise ValueError("synthetic extraction failure")
        dialog._run(operation, dialog._import_complete)
        wait_until(lambda: dialog._job is None)
        self.assertIn("synthetic extraction failure", dialog.status.text())
        imported.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
