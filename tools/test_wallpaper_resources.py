"""Resource ownership checks with temporary data, fake rendering, no sampling."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid
from monitor import config
from monitor.database import Database
from monitor.ui import glass, glass_effects
from monitor.ui.wallpaper_gallery import thumbnail_pool
from tools.test_live_stacking import FakeLive

APP = QApplication.instance() or QApplication([])


def flush():
    APP.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    APP.processEvents()


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.db = Database(str(Path(self.tmp.name) / 'resources.db'))
        self.db.set_setting(config.S_GLASS_WALLPAPER, 'synthetic/project.json')
        self.db.set_setting(config.S_GLASS_MEDIA_TYPE, 'live_web')
        config.set_theme(config.THEME_GLASS)
        self.patch = patch('monitor.ui.live_wallpaper.LiveWallpaperSurface', FakeLive)
        self.patch.start()
        self.root = glass.GlassBackground(self.db)
        self.root.resize(800, 600)
        self.root.show()
        flush()
        self.root._live_surface.ready.emit()

    def tearDown(self):
        self.root.deleteLater()
        flush()
        self.patch.stop()
        self.db.close()
        self.tmp.cleanup()
        glass_effects.clear_cache()
        config.set_theme(config.THEME_DARK)

    def test_long_hide_releases_and_restores_without_changing_selection(self):
        old = self.root._live_surface
        self.root.hide()
        flush()
        self.assertTrue(self.root._idle_release.isActive())
        self.assertEqual(self.root._idle_release.interval(), 60000)
        self.root._idle_release.timeout.emit()  # Advance lifecycle, never wait a minute.
        self.assertIsNone(self.root._live_surface)
        self.assertIsNone(self.root._live_overlay)
        self.assertTrue(self.root._resources_released)
        self.assertEqual(self.db.get_setting(config.S_GLASS_WALLPAPER), 'synthetic/project.json')
        self.root.show()
        flush()
        self.assertIsNotNone(self.root._live_surface)
        self.assertIsNot(self.root._live_surface, old)
        self.assertFalse(self.root._idle_release.isActive())
        self.root._live_surface.ready.emit()
        self.assertTrue(self.root._live_surface.isVisible())

    def test_short_hide_and_visible_timeout_keep_current_renderer(self):
        old = self.root._live_surface
        self.root.hide()
        flush()
        self.root.show()
        flush()
        self.root._release_idle_resources()
        self.assertIs(self.root._live_surface, old)
        self.assertFalse(self.root._idle_release.isActive())

    def test_other_theme_releases_then_glass_refresh_restores(self):
        config.set_theme(config.THEME_DARK)
        self.root.refresh()
        self.assertFalse(self.root._idle_release.isActive())
        self.assertIsNone(self.root._live_surface)
        config.set_theme(config.THEME_GLASS)
        self.root.refresh()
        flush()
        self.assertIsNotNone(self.root._live_surface)

    def test_repeated_theme_rebuild_has_one_root_library_and_renderer(self):
        from monitor.tracker import ProcessTracker
        from monitor.ui.main_window import MainWindow
        from monitor.wallpaper_library import WallpaperLibrary
        from monitor.ui.wallpaper_rotation import RotationService
        self.root.deleteLater()
        flush()
        control = Mock()
        for name, value in {'enabled': False, 'has_password': True, 'session_grace': 60,
                            'lock_remaining': 0, 'all_rules': {}, 'self_program_ids': set()}.items():
            getattr(control, name).return_value = value
        with patch('monitor.autostart.is_enabled', return_value=False), patch('monitor.autostart.is_admin_enabled', return_value=False):
            window = MainWindow(self.db, ProcessTracker(self.db), control=control)
        self.root = window.centralWidget()
        try:
            window.show()
            window.switch_page(4, animate=False)
            flush()
            library, rotation = self.root.library, self.root.rotation
            renderer = self.root._live_surface
            window.rebuild_ui()
            flush()
            self.assertIs(self.root._live_surface, renderer)
            retired = []
            for mode in (config.THEME_DARK, config.THEME_GLASS, config.THEME_LIGHT, config.THEME_GLASS) * 4:
                retired.append(window.settings_page)
                renderer = self.root._live_surface
                self.db.set_setting(config.S_THEME, mode)
                config.set_theme(mode)
                window.rebuild_ui()
                flush()
                self.assertIs(window.centralWidget(), self.root)
                self.assertIs(self.root.library, library)
                self.assertIs(self.root.rotation, rotation)
                self.assertEqual(self.root.layout().count(), 1)
                self.assertEqual(window.stack.currentIndex(), 4)
                self.assertTrue(all(not isValid(page) for page in retired))
                self.assertEqual(len(window.findChildren(glass.GlassBackground)), 1)
                self.assertEqual(len(window.findChildren(WallpaperLibrary)), 1)
                self.assertEqual(len(window.findChildren(RotationService)), 1)
                if mode == config.THEME_GLASS:
                    self.assertEqual(len(self.root.findChildren(FakeLive)), 1)
                    self.root._live_surface.ready.emit()
                else:
                    self.assertIsNone(self.root._live_surface)
                    self.assertFalse(isValid(renderer))
                    self.assertTrue(self.root._source.isNull())
        finally:
            window.takeCentralWidget()  # ResourceTests owns root cleanup.
            window.deleteLater()
            flush()

    def test_live_first_frame_discards_loading_poster_and_grading_cache(self):
        image = QImage(100, 100, QImage.Format_RGB32)
        image.fill(QColor('red'))
        self.root._source = self.root._image = image
        glass_effects._values['brightness'] = 120
        glass_effects.graded_image(image)
        self.assertFalse(glass_effects._cache_image.isNull())
        self.root._live_surface.ready.emit()
        self.assertTrue(self.root._source.isNull())
        self.assertTrue(self.root._image.isNull())
        self.assertTrue(glass_effects._cache_image.isNull())

    def test_default_grading_drops_previous_adjustment(self):
        image = QImage(100, 100, QImage.Format_RGB32)
        image.fill(QColor('red'))
        glass_effects._values['brightness'] = 120
        glass_effects.graded_image(image)
        glass_effects._values.update(brightness=100, contrast=100, saturation=100)
        self.assertEqual(glass_effects.graded_image(image).cacheKey(), image.cacheKey())
        self.assertTrue(glass_effects._cache_image.isNull())

    def test_thumbnail_pool_is_shared_and_bounded(self):
        self.assertIs(thumbnail_pool(), thumbnail_pool())
        self.assertEqual(thumbnail_pool().maxThreadCount(), 2)

    def test_minimized_idle_releases_and_restore_reloads(self):
        with patch.object(self.root, 'isMinimized', return_value=True):
            self.root._sync_resource_lifetime()
            self.assertTrue(self.root._idle_release.isActive())
            self.root._idle_release.timeout.emit()
            self.assertIsNone(self.root._live_surface)
        self.root._sync_resource_lifetime()
        self.assertIsNotNone(self.root._live_surface)

    def test_stopping_video_releases_decoder_audio_and_sink(self):
        player, audio, sink = Mock(), Mock(), Mock()
        self.root._player, self.root._audio, self.root._video_sink = player, audio, sink
        self.root._video_path = 'synthetic.mp4'
        self.root._stop_video()
        player.stop.assert_called_once()
        for resource in (player, audio, sink):
            resource.deleteLater.assert_called_once()
        self.assertIsNone(self.root._player)
        self.assertIsNone(self.root._video_sink)


if __name__ == '__main__':
    unittest.main()
