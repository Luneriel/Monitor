"""Synthetic live import/origin tests and mocked WebEngine lifecycle; no GPU load."""
from __future__ import annotations
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor import wallpaper_engine as we
from monitor.we_runtime import WallpaperOrigin, request_allowed


class LiveImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="live-synthetic-", dir=Path(__file__).resolve().parents[1])
        self.base = Path(self.tmp.name)
        self.project = self.base / "source"
        self.project.mkdir()
        self.managed = self.base / "managed"
        (self.project / "project.json").write_text(json.dumps({"type": "web", "file": "index.html", "title": "Synthetic"}))
        (self.project / "index.html").write_text('<html><script src="main.js"></script></html>')
        (self.project / "main.js").write_text('document.body.textContent="synthetic"')
        (self.project / "run.exe").write_bytes(b"MZ")
        (self.project / "run.ps1").write_text("must never run")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_browser_resources_no_native_programs_dedup(self):
        result = we.import_project(self.project, self.managed)
        self.assertEqual(result.kind, "live_web")
        self.assertFalse(result.degraded)
        root = Path(result.path).parent
        self.assertEqual({p.name for p in root.iterdir()}, {"import.json", "project.json", "index.html", "main.js"})
        self.assertTrue(we.import_project(self.project, self.managed).duplicate)
        (root / "main.js").write_text("tampered")
        with self.assertRaisesRegex(we.WallpaperEngineError, "校验"):
            we.import_project(self.project, self.managed)

    def test_scene_prefers_real_project_and_retains_legacy_preview_mode(self):
        (self.project / "scene.json").write_text('{"objects":[]}')
        (self.project / "project.json").write_text('{"type":"scene","file":"scene.json"}')
        result = we.import_project(self.project, self.managed)
        self.assertEqual(result.kind, "live_scene")
        self.assertEqual(Path(result.path).name, "project.json")
        self.assertEqual(we.read_project(self.project, prefer_live=False).import_mode, "unsupported")

    def test_caps_cancel_and_no_partial_publish(self):
        with patch.object(we, "MAX_LIVE_FILES", 1), self.assertRaises(we.WallpaperEngineError):
            we.import_project(self.project, self.managed)
        with patch.object(we, "MAX_LIVE_BYTES", 10), self.assertRaises(we.WallpaperEngineError):
            we.import_project(self.project, self.managed)
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            return calls > 12
        with self.assertRaises(we.ImportCancelled):
            we.import_project(self.project, self.managed, cancel=cancel)
        self.assertFalse(list(self.managed.glob("*")))

    def test_finite_offline_server_range_and_boundary(self):
        result = we.import_project(self.project, self.managed)
        origin = WallpaperOrigin(result.path, result.kind)
        with origin:
            base = origin.base_url
            self.assertTrue(base.startswith("http://127.0.0.1:"))
            with urlopen(base + "runtime/index.html") as response:
                html = response.read()
                self.assertIn(b"renderer.js", html)
                self.assertNotIn(b"webwallgl.global.min.js", html)
                self.assertIn("connect-src " + base, response.headers["Content-Security-Policy"])
            with urlopen(base + "asset/index.html") as response:
                self.assertIn("sandbox allow-scripts", response.headers["Content-Security-Policy"])
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
                self.assertIsNone(response.headers.get("Access-Control-Allow-Credentials"))
            with urlopen(Request(base + "asset/main.js", method="OPTIONS", headers={"Access-Control-Request-Method":"GET", "Access-Control-Request-Headers":"Range"})) as response:
                self.assertEqual(response.status, 204)
                self.assertEqual(response.headers["Access-Control-Allow-Methods"], "GET, HEAD")
            with urlopen(Request(base + "asset/main.js", headers={"Range":"bytes=0-7"})) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), b"document")
            for relative in ("asset/import.json", "asset/run.exe", "asset/../project.json", "asset/%2e%2e/project.json", "runtime/VERSION.json", "asset/C:%5CWindows%5Ctest"):
                with self.subTest(relative=relative), self.assertRaises(HTTPError) as caught:
                    urlopen(base + relative)
                caught.exception.close()
            for url in ("file:///C:/Windows/win.ini", "https://example.com/", "http://127.0.0.1:1/", base.replace(origin.token, "wrong") + "asset/main.js"):
                self.assertFalse(request_allowed(url, base))
            self.assertTrue(request_allowed(base + "asset/main.js", base))
            self.assertTrue(request_allowed("blob:" + base + "123", base))
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(base + "asset/main.js", headers={"Host":"example.com"}))
            caught.exception.close()
            origin.close(wait=False)
            self.assertEqual(origin.base_url, "")
            origin.close(wait=True)
            self.assertFalse(origin._closing_thread.is_alive())
        self.assertEqual(origin.base_url, "")


class MockLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from monitor.ui.live_wallpaper import LiveWallpaperSurface
        self.surface = LiveWallpaperSurface()

    def tearDown(self):
        self.surface.dispose()
        self.surface.close()

    def test_parent_destruction_closes_source_without_qt_callback_errors(self):
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QWidget
        from monitor.ui.live_wallpaper import LiveWallpaperSurface
        parent = QWidget()
        surface = LiveWallpaperSurface(parent)
        origin = MagicMock()
        surface._cleanup_bag["origin"] = origin
        errors = []
        with patch("sys.excepthook", side_effect=lambda *args: errors.append(args)):
            parent.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            surface._load_timeout(surface._generation)
        origin.close.assert_called_once()
        self.assertEqual(errors, [])

    def test_styles_validate_and_forward_without_starting_browser(self):
        self.surface._page = MagicMock()
        self.surface._loaded = True
        self.surface.set_style(fit="contain", x=20, y=80, scale=120, brightness=1.2, contrast=.8, saturation=1.5, blur=3)
        script = self.surface._page.runJavaScript.call_args.args[0]
        self.assertIn('"fit": "contain"', script)
        self.assertIn('"contrast": 0.8', script)
        for patch_style in ({"fit":"bad"}, {"x":-1}, {"scale":151}, {"brightness":float("nan")}):
            with self.assertRaises(ValueError):
                self.surface.set_style(**patch_style)

    def test_native_load_failure_closes_source_and_emits_no_success(self):
        ready, failed = MagicMock(), MagicMock()
        self.surface.ready.connect(ready)
        self.surface.failed.connect(failed)
        with patch("monitor.ui.live_wallpaper.WallpaperOrigin") as origin, patch.object(self.surface, "_make_browser", side_effect=RuntimeError("mock unavailable")):
            self.assertFalse(self.surface.load("synthetic/project.json", "live_scene"))
            origin.return_value.close.assert_called_once()
        ready.assert_not_called()
        failed.assert_called_once()
        self.assertIn("mock unavailable", self.surface.error_message)

    def test_hidden_pause_freezes_page_and_releases(self):
        self.surface._page, self.surface._view = MagicMock(), MagicMock()
        self.surface._loaded = True
        page = self.surface._page
        self.surface.set_active(False)
        page.setVisible.assert_called_with(False)
        page.setLifecycleState.assert_called_with(page.LifecycleState.Frozen)
        self.assertFalse(self.surface._poll.isActive())
        self.surface._origin = MagicMock()
        origin = self.surface._origin
        # A real QWidget must be used for layout removal, so detach this view mock.
        self.surface._view = None
        self.surface.dispose()
        origin.close.assert_called_once()
        self.assertIsNone(self.surface._page)

    def test_loading_hidden_freezes_and_suspends_timeout_until_visible(self):
        self.surface._page, self.surface._view = MagicMock(), MagicMock()
        self.surface._loaded = False
        self.surface._disposed = False
        page = self.surface._page
        self.surface.set_active(False)
        page.setLifecycleState.assert_called_with(page.LifecycleState.Frozen)
        self.assertFalse(self.surface._load_watchdog.isActive())
        self.assertFalse(self.surface._poll.isActive())
        self.surface.show()
        self.surface.set_active(True)
        page.setLifecycleState.assert_called_with(page.LifecycleState.Active)
        self.assertTrue(self.surface._load_watchdog.isActive())
        self.assertTrue(self.surface._poll.isActive())
        self.surface._view.show.assert_not_called()
        self.surface.hide()
        self.assertFalse(self.surface._load_watchdog.isActive())
        self.assertFalse(self.surface._poll.isActive())
        self.surface._view = None
        # Gallery keeps a child surface hidden until ready. A visible host
        # must still allow its hidden loading child to reach the first frame.
        from PySide6.QtWidgets import QWidget
        from monitor.ui.live_wallpaper import LiveWallpaperSurface
        host = QWidget()
        child = LiveWallpaperSurface(host)
        child.hide()
        host.show()
        child._page, child._view = MagicMock(), MagicMock()
        child.set_active(True)
        self.assertFalse(child.isVisible())
        self.assertTrue(child._load_watchdog.isActive())
        child._page.setVisible.assert_called_with(True)
        child._view.show.assert_not_called()
        host.hide()
        child.set_active(False)
        self.assertFalse(child._load_watchdog.isActive())
        child._view = None
        child.dispose()
        host.close()

    def test_failed_status_never_marks_poster_as_live(self):
        self.surface._page = MagicMock()
        self.surface._disposed = False
        self.surface._page.runJavaScript.side_effect = lambda script, callback=None: callback('{"status":"failed","error":"fixture failure"}') if callback else None
        ready, failed = MagicMock(), MagicMock()
        self.surface.ready.connect(ready)
        self.surface.failed.connect(failed)
        self.surface._check_state()
        ready.assert_not_called()
        failed.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
