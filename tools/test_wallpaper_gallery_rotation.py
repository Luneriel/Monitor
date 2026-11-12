"""Synthetic inventory and mocked renderers only; no user media or WebGL."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool, Signal, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget
from monitor import config, wallpaper_engine as we
from monitor.database import Database
from monitor.wallpaper_library import LibraryStore, WallpaperLibrary, SELECTED_KEY, ROTATION_KEY
from monitor.ui.glass import GlassBackground
from monitor.ui.wallpaper_gallery import GalleryGrid, WallpaperGalleryDialog, ThumbnailCache
from monitor.ui.wallpaper_rotation import RotationService, RotationEditor

APP = QApplication.instance() or QApplication([])


def flush():
    APP.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def wait_until(condition):
    deadline = time.monotonic() + 4
    while not condition() and time.monotonic() < deadline:
        flush()
        time.sleep(.005)
    if not condition():
        raise AssertionError("Asynchronous operation timed out")


class FakeLive(QWidget):
    ready = Signal()
    failed = Signal(str)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.load = Mock(return_value=True)
        self.set_active = Mock()
        self.set_style = Mock()
        self.stop = Mock()


class GalleryRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.directory = Path(self.tmp.name)
        self.db = Database(str(self.directory / "test.db"))
        config.set_theme(config.THEME_GLASS)
        self.image = self.directory / "blue.png"
        image = QImage(96, 64, QImage.Format_RGB32)
        image.fill(QColor("#4588aa"))
        image.save(str(self.image))
        self.projects = [we.WallpaperProject(str(i), "Synthetic " + str(i), str(self.directory),
            "image", str(self.image), str(self.image), "image") for i in range(4)]
        self.window = QMainWindow()
        self.root = GlassBackground(self.db, self.window)
        self.window.setCentralWidget(self.root)
        self.library = self.root.library
        self.library.store.update_scan(we.ScanResult(tuple(self.projects), (), (str(self.directory),)))
        self.window.show()
        flush()

    def tearDown(self):
        for window in APP.topLevelWidgets():
            window.deleteLater()
        flush()
        self.assertTrue(QThreadPool.globalInstance().waitForDone(4000))
        flush()
        self.db.close()
        self.tmp.cleanup()
        config.set_theme(config.THEME_DARK)

    def result(self, kind="image"):
        return we.ImportResult("imported", "Synthetic", str(self.image), kind, "image", kind, "Synthetic", False)

    def profile(self, items=None):
        return {"id": "list1", "name": "First list", "seconds": 120,
                "mode": "ordered", "items": items or [p.id for p in self.projects]}

    def test_inventory_reopens_from_sqlite_without_discovery_or_scan(self):
        with patch.object(we, "discover_roots", side_effect=AssertionError("must not rediscover")), \
             patch.object(we, "scan_projects", side_effect=AssertionError("must not rescan")):
            store = LibraryStore(self.db)
            self.assertEqual(store.roots, [str(self.directory)])
            self.assertEqual([p.id for p in store.projects], ["0", "1", "2", "3"])
            dialog = WallpaperGalleryDialog(self.library, self.root.rotation)
            self.assertEqual(dialog.gallery.items.count(), 5)
            dialog.close()

    def test_refresh_uses_saved_roots_and_revalidates_missing_sources(self):
        calls = []
        def scan(roots, cancel=None, progress=None):
            calls.append(tuple(roots))
            return we.ScanResult((), (), tuple(roots))
        with patch.object(we, "discover_roots", side_effect=AssertionError("saved roots")), \
             patch.object(we, "scan_projects", side_effect=scan):
            self.library.scan()
            wait_until(lambda: not self.library.busy)
        self.assertEqual(calls, [(str(self.directory),)])
        self.assertEqual(self.library.store.projects, [])

    def test_click_highlights_immediately_then_applies_without_confirmation(self):
        started, release = threading.Event(), threading.Event()
        def operation(*args, **kwargs):
            started.set()
            release.wait(2)
            return self.result()
        grid = GalleryGrid(self.library)
        grid.activated.connect(self.library.select)
        with patch.object(we, "import_project", side_effect=operation):
            grid._clicked(grid.items.item(2))
            wait_until(started.is_set)
            self.assertEqual(self.library.selected_id, "1")
            self.assertTrue(grid.items.item(2).isSelected())
            release.set()
            wait_until(lambda: not self.library.busy)
        self.assertEqual(self.db.get_setting(SELECTED_KEY), "1")
        self.assertEqual(self.db.get_setting(config.S_GLASS_WALLPAPER), str(self.image))

    def test_cancel_discards_obsolete_scan_result(self):
        started, release = threading.Event(), threading.Event()
        def scan(*args, **kwargs):
            started.set()
            release.wait(2)
            return we.ScanResult(())
        with patch.object(we, "scan_projects", side_effect=scan):
            self.library.scan()
            wait_until(started.is_set)
            job = self.library._job
            self.library.cancel()
            self.assertTrue(job.cancel.is_set())
            release.set()
            QThreadPool.globalInstance().waitForDone(3000)
            flush()
        self.assertEqual(len(self.library.store.projects), 4)

    def test_first_live_frame_failure_restores_previous_wallpaper_and_selection(self):
        self.db.set_setting(config.S_GLASS_WALLPAPER, str(self.image))
        self.db.set_setting(SELECTED_KEY, "0")
        self.library.selected_id = "0"
        self.root.refresh()
        with patch("monitor.ui.live_wallpaper.LiveWallpaperSurface", FakeLive), \
             patch.object(we, "import_project", return_value=self.result("live_scene")):
            self.library.select("1")
            wait_until(lambda: self.root._live_surface is not None)
            self.assertTrue(self.library.busy)
            self.root._live_surface.failed.emit("synthetic first-frame failure")
            self.assertEqual(self.db.get_setting(config.S_GLASS_WALLPAPER), str(self.image))
            self.assertEqual(self.db.get_setting(config.S_GLASS_MEDIA_TYPE), "image")
            self.assertEqual(self.library.selected_id, "0")
            self.assertFalse(self.root._source.isNull())
            self.assertIn("synthetic", self.library.status)

    def test_live_ready_stacking_lifecycle_and_effects_do_not_reload(self):
        with patch("monitor.ui.live_wallpaper.LiveWallpaperSurface", FakeLive), \
             patch.object(we, "import_project", return_value=self.result("live_web")):
            self.library.select("1")
            wait_until(lambda: self.root._live_surface is not None)
            surface = self.root._live_surface
            surface.ready.emit()
            self.assertFalse(self.library.busy)
            self.assertTrue(surface.isVisible())
            self.db.set_setting("glass.effects.brightness", 85)
            self.root.refresh()
            self.assertEqual(surface.load.call_count, 1)
            self.assertEqual(surface.set_style.call_args.kwargs["brightness"], .85)
            self.window.hide()
            self.assertFalse(surface.set_active.call_args.args[0])
            self.window.show()
            flush()
            self.assertTrue(surface.set_active.call_args.args[0])
            config.set_theme(config.THEME_DARK)
            self.root.refresh()
            self.assertFalse(surface.isVisible())

    def test_cancel_unconfirmed_renderer_and_handoff_to_local_picker(self):
        self.db.set_setting(config.S_GLASS_WALLPAPER, str(self.image))
        self.db.set_setting(config.S_GLASS_MEDIA_TYPE, "image")
        self.root.refresh()
        with patch("monitor.ui.live_wallpaper.LiveWallpaperSurface", FakeLive), \
             patch.object(we, "import_project", return_value=self.result("live_scene")):
            self.library.select("1")
            wait_until(lambda: self.root._live_surface is not None)
            self.library.cancel_work()
            self.assertFalse(self.library.busy)
            self.assertIsNone(self.root._live_surface)
            self.assertEqual(self.db.get_setting(config.S_GLASS_MEDIA_TYPE), "image")
            self.library.select("2")
            wait_until(lambda: self.root._live_surface is not None)
            self.library.release_selection()
            self.assertFalse(self.library.busy)
            self.assertEqual(self.library.selected_id, "")
            self.assertEqual(self.db.get_setting(config.S_GLASS_WALLPAPER), str(self.image))

    def test_rotation_pause_preserves_remaining_and_has_no_hidden_or_empty_timer(self):
        rotation = self.root.rotation
        rotation.save_list(self.profile())
        self.library.selected_id = "0"
        rotation.activate("list1")
        self.assertTrue(rotation.timer.isActive())
        rotation.timer.start(95000)
        self.window.hide()
        remaining = rotation._remaining
        self.assertFalse(rotation.timer.isActive())
        self.assertGreater(remaining, 90000)
        self.window.show()
        flush()
        self.assertTrue(rotation.timer.isActive())
        self.assertLessEqual(rotation.timer.remainingTime(), remaining + 1000)
        config.set_theme(config.THEME_DARK)
        self.root.refresh()
        self.assertFalse(rotation.timer.isActive())
        config.set_theme(config.THEME_GLASS)
        self.root.refresh()
        self.assertTrue(rotation.timer.isActive())
        self.image.unlink()
        rotation.sync()
        self.assertFalse(rotation.timer.isActive())

    def test_rotation_order_random_and_unavailable_entries(self):
        rotation = self.root.rotation
        rotation.save_list(self.profile())
        self.library.selected_id = "1"
        rotation.activate("list1")
        with patch.object(self.library, "select") as select:
            rotation.advance()
            select.assert_called_once_with("2")
        rotation.active_list["mode"] = "random"
        with patch.object(self.library, "select") as select:
            rotation.advance()
            self.assertNotEqual(select.call_args.args[0], "1")
        self.image.unlink()
        with patch.object(self.library, "select") as select:
            rotation.advance()
            select.assert_not_called()

    def test_wallpaper_off_stops_rotation_and_preserves_saved_lists(self):
        rotation = self.root.rotation
        rotation.save_list(self.profile())
        self.library.selected_id = "0"
        rotation.activate("list1")
        self.assertTrue(rotation.timer.isActive())
        self.library.clear()
        self.assertFalse(rotation.timer.isActive())
        self.assertEqual(rotation.state["active"], "")
        self.assertEqual(len(rotation.state["lists"]), 1)
        self.assertEqual(self.db.get_setting(config.S_GLASS_WALLPAPER), "")

    def test_editor_draft_cancel_save_list_switch_and_delete(self):
        rotation = self.root.rotation
        rotation.save_list(self.profile(["0", "1"]))
        second = self.profile(["2", "3"])
        second.update(id="list2", name="Second")
        rotation.save_list(second)
        editor = RotationEditor(rotation)
        editor.profiles.setCurrentRow(0)
        editor.name.setText("Unsaved")
        editor.toggle_item("2")
        self.assertEqual(rotation.state["lists"][0]["name"], "First list")
        self.assertEqual(rotation.state["lists"][0]["items"], ["0", "1"])
        editor.cancel_edit()
        self.assertEqual(editor.name.text(), "First list")
        self.assertEqual(editor._selected_ids(), ["0", "1"])
        editor.profiles.setCurrentRow(1)
        self.assertEqual(editor._selected_ids(), ["2", "3"])
        editor.name.setText("Saved")
        editor.toggle_item("1")
        editor.save()
        self.assertEqual(rotation.state["lists"][1]["name"], "Saved")
        self.assertEqual(rotation.state["lists"][1]["items"], ["2", "3", "1"])
        editor.delete_list()
        self.assertEqual(len(rotation.state["lists"]), 1)
        persisted = json.loads(self.db.get_setting(ROTATION_KEY))
        self.assertEqual(len(persisted["lists"]), 1)

    def test_grid_filter_pagination_and_bounded_thumbnail_cache(self):
        self.library.store.projects = [replace(self.projects[0], id=str(i), title=f"Synthetic {i}") for i in range(130)]
        grid = GalleryGrid(self.library)
        self.assertEqual(grid.items.count(), 49)
        grid._change_page(1)
        self.assertEqual(grid.items.count(), 49)
        grid.search.setText("Synthetic 129")
        self.assertEqual(grid.items.count(), 2)
        self.assertEqual(grid.page, 0)
        cache = ThumbnailCache()
        for i in range(150):
            job = object()
            key = (i,)
            cache.pending[key] = job
            cache._ready(job, key, QImage(1, 1, QImage.Format_RGB32))
        self.assertEqual(len(cache.images), cache.LIMIT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
