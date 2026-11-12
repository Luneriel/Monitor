"""Native stacking regression: synthetic WebGL, temporary DB, no tracker sampling.

Opens one test window, checks four lifecycle stages, saves native screenshots
under outputs/stacking-probe/regression, and exits nonzero on failure.
"""
from __future__ import annotations
import json
from pathlib import Path
import sys
import tempfile
import time
import traceback
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QPushButton, QWidget
from monitor.app import create_qapplication
from monitor import config, wallpaper_engine as we
from monitor.database import Database
from monitor.tracker import ProcessTracker
from monitor.ui import glass, theme
from monitor.ui.main_window import MainWindow


class StackingProbe:
    def __init__(self, app, db, result, output):
        self.app, self.db, self.result, self.output = app, db, result, output
        self.finished, self.exit_code, self.clicks = False, 1, 0
        self.records = []
        control = Mock()
        for name, value in {"enabled": False, "has_password": True, "session_grace": 60,
                            "lock_remaining": 0, "all_rules": {}, "self_program_ids": set()}.items():
            getattr(control, name).return_value = value
        self.tracker = ProcessTracker(db)  # Deliberately never started.
        self.window = MainWindow(db, self.tracker, control=control)
        self.window.resize(1000, 720)
        self.window.move(80, 60)
        self.root = self.window.centralWidget()
        self.body = self.root.findChild(QWidget, "BodyArea", Qt.FindDirectChildrenOnly)
        self.marker = QLabel("FOREGROUND MUST REMAIN VISIBLE", self.body)
        self.marker.setGeometry(260, 180, 600, 90)
        self.marker.setStyleSheet("background:#00ff00;color:#000000;font-size:24px;font-weight:bold")
        self.marker.show()
        self.button = QPushButton("FOREGROUND BUTTON", self.body)
        self.button.setGeometry(300, 300, 400, 80)
        self.button.setStyleSheet("background:#ffff00;color:#000000;font-size:24px")
        self.button.clicked.connect(self.clicked)
        self.button.show()
        self.timeout = QTimer(self.window)
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(lambda: self.finish(AssertionError("Probe exceeded 35 seconds")))
        self.timeout.start(35000)
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.later(300, self.select_first)

    def clicked(self):
        self.clicks += 1

    def later(self, delay, operation):
        def guarded():
            if self.finished:
                return
            try:
                operation()
            except Exception as error:
                traceback.print_exc()
                self.finish(error)
        QTimer.singleShot(delay, guarded)

    def ready_then(self, stage, continuation):
        deadline = time.monotonic() + 12
        def check():
            if self.root.video_error:
                raise AssertionError(self.root.video_error)
            surface = self.root._live_surface
            if (self.window.windowHandle().isExposed() and not self.window.isMinimized()
                    and self.root._live_ready and surface and surface.isVisible()
                    and surface._view and surface._view.isVisible()):
                # GPU texture composition follows the ready signal asynchronously.
                self.later(1600 if stage == "restored" else 650,
                           lambda: (self.capture(stage), continuation()))
            elif time.monotonic() >= deadline:
                raise AssertionError(f"{stage}: no visible first frame in 12 seconds")
            else:
                self.later(50, check)
        self.later(0, check)

    def select_first(self):
        assert self.root._live_surface is None
        self.window.settings_page._apply_wallpaper_import(self.result)
        self.first_surface = self.root._live_surface
        self.ready_then("selected", self.resize)

    def resize(self):
        self.window.resize(1100, 800)
        self.ready_then("resized", self.minimize)

    def minimize(self):
        self.window.showMinimized()
        def restore():
            assert self.window.isMinimized(), "Window did not minimize"
            self.window.showNormal()
            self.window.raise_()
            self.window.activateWindow()
            self.ready_then("restored", self.reload)
        self.later(900, restore)

    def reload(self):
        self.root.library.clear()
        assert self.root._live_surface is None, "Clear retained browser surface"
        assert not self.root._live_path
        self.window.settings_page._apply_wallpaper_import(self.result)
        assert self.root._live_surface is not self.first_surface
        self.ready_then("reloaded", self.finish)

    def capture(self, stage):
        surface = self.root._live_surface
        assert self.body.geometry() == self.root.rect()
        assert self.root.layout().count() == 1
        assert surface._page.backgroundColor().alpha() == 255, "Browser background must be opaque"
        quick = [child for child in surface._view.findChildren(QWidget) if child.inherits("QQuickWidget")]
        assert quick, "No internal WebEngine QQuickWidget found"
        assert all(not child.testAttribute(Qt.WA_AlwaysStackOnTop) for child in quick), "Browser stacks above UI"
        self.root.grab().save(str(self.output / f"{stage}-qt.png"))
        # Inspect the compositor's final desktop pixels, bounded to this test
        # window. QWidget.grab misses texture stacking bugs, while grabWindow
        # with a restored translucent HWND can return an entirely black bitmap.
        position = self.window.mapToGlobal(QPoint(0, 0)) - self.window.screen().geometry().topLeft()
        native = self.window.screen().grabWindow(0, position.x(), position.y(),
                                                 self.window.width(), self.window.height())
        assert not native.isNull(), "Native screenshot unavailable"
        assert native.save(str(self.output / f"{stage}-native.png"))
        point = self.marker.mapTo(self.window, QPoint(8, 8))
        ratio = native.devicePixelRatio()
        pixel = native.toImage().pixelColor(round(point.x() * ratio), round(point.y() * ratio))
        assert pixel.name() == "#00ff00", f"{stage}: native marker is {pixel.name()}, expected green"
        target = self.button.mapTo(self.root, self.button.rect().center())
        assert self.root.childAt(target) is self.button, "Wallpaper blocks button hit testing"
        before = self.clicks
        QTest.mouseClick(self.button, Qt.LeftButton)
        assert self.clicks == before + 1, "Button did not receive QtTest click"
        record = {"stage": stage, "nativeMarker": pixel.name(), "devicePixelRatio": ratio,
                  "backgroundAlpha": surface._page.backgroundColor().alpha(), "quickWidgets": len(quick),
                  "buttonClicks": self.clicks, "windowSize": [self.window.width(), self.window.height()]}
        self.records.append(record)
        print(json.dumps(record), flush=True)

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timeout.stop()
        self.exit_code = 1 if error else 0
        report = {"passed": error is None, "error": str(error) if error else "", "stages": self.records}
        (self.output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)
        self.root._stop_live()
        self.window.hide()
        self.window.deleteLater()
        QTimer.singleShot(200, self.app.quit)


def main():
    app = create_qapplication(sys.argv)
    assert app.platformName() not in {"offscreen", "minimal"}, "Requires native Windows composition"
    output = Path(__file__).resolve().parents[1] / "outputs" / "stacking-probe" / "regression"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="native-stacking-", dir=Path(__file__).resolve().parent) as temporary:
        base = Path(temporary)
        db = Database(str(base / "probe.db"))
        try:
            db.set_setting(config.S_THEME, config.THEME_GLASS)
            config.set_theme(config.THEME_GLASS)
            glass.configure(db)
            app.setPalette(theme.app_palette())
            app.setStyleSheet(theme.build_stylesheet())
            source = base / "source"
            source.mkdir()
            (source / "project.json").write_text('{"type":"web","file":"index.html","title":"Synthetic WebGL"}')
            (source / "index.html").write_text('''<!doctype html><body style="margin:0;background:#f00000;height:100vh">
                <canvas id="c" width="64" height="64"></canvas><script>
                const gl=c.getContext("webgl");
                if (!gl) throw new Error("Synthetic WebGL context unavailable");
                gl.clearColor(1,0,0,1);gl.clear(gl.COLOR_BUFFER_BIT);
                </script>''')
            result = we.import_project(source, base / "managed")
            with patch("monitor.autostart.is_enabled", return_value=False), patch("monitor.autostart.is_admin_enabled", return_value=False):
                probe = StackingProbe(app, db, result, output)
                app.exec()
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                return probe.exit_code
        finally:
            for window in app.topLevelWidgets():
                window.hide()
                window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
