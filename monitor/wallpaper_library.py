"""Persistent library inventory and cancellable asynchronous selection service.

Only the UI thread accesses SQLite. Inventory and playlists are JSON values in
the existing settings table; managed assets remain outside source control.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
import threading

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot

from . import config, wallpaper_engine as we
from .i18n import current_language

LIBRARY_KEY = "glass.library.v1"
ROTATION_KEY = "glass.rotation.v1"
SELECTED_KEY = "glass.selected_id"
MAX_SNAPSHOT_BYTES = 24 * 1024 * 1024


def text(zh, en):
    return en if current_language() == "en" else zh


def _json_setting(db, key, default):
    value = db.get_setting(key, "")
    if not value or len(value) > MAX_SNAPSHOT_BYTES:
        return default
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return default


def source_available(project):
    """Cheap eligibility only. Import always re-reads the source safely."""
    return bool(project.import_mode != "unsupported" and
                ((project.entry and os.path.isfile(project.entry)) or
                 os.path.isfile(os.path.join(project.project_dir, "project.json"))))


class LibraryStore:
    def __init__(self, db):
        self.db = db
        self.roots = []
        self.projects = []
        self.results = {}
        self.reload()

    def reload(self):
        snapshot = _json_setting(self.db, LIBRARY_KEY, {})
        if not isinstance(snapshot, dict):
            return
        roots = snapshot.get("roots", [])
        self.roots = [p for p in roots[:100] if isinstance(p, str)] if isinstance(roots, list) else []
        self.projects = []
        raw_projects = snapshot.get("projects", [])
        for raw in raw_projects[:we.MAX_PROJECTS] if isinstance(raw_projects, list) else []:
            try:
                p = we.WallpaperProject(**raw)
                if all(isinstance(getattr(p, k), str) for k in
                       ("id", "title", "project_dir", "type", "entry", "import_mode", "reason")) and (
                       p.preview is None or isinstance(p.preview, str)):
                    self.projects.append(p)
            except (TypeError, ValueError):
                continue
        self.results = {}
        raw_results = snapshot.get("results", {})
        for key, raw in list(raw_results.items())[:we.MAX_PROJECTS] if isinstance(raw_results, dict) else []:
            try:
                result = we.ImportResult(**raw)
                if (all(isinstance(getattr(result, k), str) for k in
                        ("id", "title", "path", "kind", "source_type", "mode", "message")) and
                        (result.preview is None or isinstance(result.preview, str)) and
                        result.kind in {"image", "video", "live_scene", "live_web"}):
                    self.results[key] = result
            except (TypeError, ValueError):
                continue

    def save(self):
        self.db.set_setting(LIBRARY_KEY, json.dumps({"version": 1, "roots": self.roots,
            "projects": [asdict(p) for p in self.projects],
            "results": {key: asdict(value) for key, value in self.results.items()}}, ensure_ascii=False))

    def update_scan(self, result, *, replace=True):
        self.roots = list(dict.fromkeys([*self.roots, *result.roots]))[:100]
        if replace:
            # The scan normally covers all saved roots. A manually chosen root
            # is merged so adding one library never silently removes another.
            self.projects = list(result.projects)
        else:
            by_id = {p.id: p for p in self.projects}
            by_id.update({p.id: p for p in result.projects})
            self.projects = sorted(by_id.values(), key=lambda p: p.title.casefold())
        self.save()

    def project(self, identity):
        return next((p for p in self.projects if p.id == identity), None)

    def eligible(self, identity):
        project = self.project(identity)
        return bool(project and source_available(project))


class JobSignals(QObject):
    progress = Signal(object, str)
    completed = Signal(object, object)
    failed = Signal(object, str)


class LibraryJob(QRunnable):
    def __init__(self, operation):
        super().__init__()
        self.signals = JobSignals()
        self.cancel = threading.Event()
        self.token = object()
        self.operation = operation

    def run(self):
        try:
            result = self.operation(self.cancel.is_set,
                lambda message: self.signals.progress.emit(self.token, message))
            if not self.cancel.is_set():
                self.signals.completed.emit(self.token, result)
        except Exception as error:
            if not self.cancel.is_set():
                self.signals.failed.emit(self.token, str(error))


class WallpaperLibrary(QObject):
    changed = Signal()
    refreshed = Signal()
    selection_changed = Signal(str)
    status_changed = Signal(str)
    applied = Signal(str)
    failed = Signal(str)
    busy_changed = Signal(bool)

    def __init__(self, db, background=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.background = background
        self.store = LibraryStore(db)
        self.selected_id = db.get_setting(SELECTED_KEY, "")
        self._job = None
        self._job_kind = ""
        self._pending = None
        self._candidate = ""
        self.status = ""
        self._scan_replace = True
        if background is not None:
            background.video_ready.connect(self.confirm_ready)
            background.video_failed.connect(self.renderer_failed)
        self.destroyed.connect(self.cancel)

    @property
    def busy(self):
        return self._job is not None or self._pending is not None

    def _status(self, message):
        self.status = message
        self.status_changed.emit(message)

    def _run(self, operation, kind):
        self.cancel()
        job = LibraryJob(operation)
        self._job, self._job_kind = job, kind
        job.signals.progress.connect(self._progress, Qt.QueuedConnection)
        job.signals.completed.connect(self._complete, Qt.QueuedConnection)
        job.signals.failed.connect(self._failed, Qt.QueuedConnection)
        self.destroyed.connect(lambda _obj=None, cancel=job.cancel: cancel.set())
        self.busy_changed.emit(True)
        QThreadPool.globalInstance().start(job)

    def cancel(self, *_):
        if self._job is not None:
            self._job.cancel.set()
            self._job = None
            self.selected_id = self.db.get_setting(SELECTED_KEY, "")
            self.selection_changed.emit(self.selected_id)
            self.busy_changed.emit(self.busy)

    def cancel_work(self):
        was_busy = self.busy
        self.cancel()
        self._restore_pending()
        if was_busy:
            self._status(text("操作已取消，保留原壁纸。", "Task cancelled; previous wallpaper retained."))
            self.busy_changed.emit(False)

    def scan(self, path=None, *, discover=False):
        saved = tuple(self.store.roots)
        self._scan_replace = path is None
        def operation(cancel, progress):
            roots = (we.discover_roots(path, discover=False) if path else
                     tuple(dict.fromkeys([*saved, *map(str, we.discover_roots())])) if discover or not saved else saved)
            return we.scan_projects(roots, cancel=cancel, progress=progress)
        self._status(text("正在刷新壁纸库…", "Refreshing library…"))
        self._run(operation, "scan")

    def select(self, identity):
        if not identity:
            self.clear()
            return
        project = self.store.project(identity)
        if project is None or not source_available(project):
            self._status(text("源文件已移动或缺失，请刷新目录。", "Source moved or missing. Refresh the library."))
            self.failed.emit(self.status)
            return
        # Cancel obsolete extraction and restore an unconfirmed renderer before
        # accepting another click. The selected outline changes immediately.
        self.cancel()
        self._restore_pending()
        self._candidate = identity
        self._status(text("正在应用：", "Applying: ") + project.title)
        self._run(lambda cancel, progress: we.import_project(project,
            os.path.join(config.DATA_DIR, "wallpapers", "managed"), cancel=cancel), "apply")
        self.selected_id = identity
        self.selection_changed.emit(identity)

    @Slot(object, str)
    def _progress(self, token, message):
        if self._job is not None and token is self._job.token:
            self._status(message)

    @Slot(object, str)
    def _failed(self, token, message):
        if self._job is None or token is not self._job.token:
            return
        self._job = None
        self.selected_id = self.db.get_setting(SELECTED_KEY, "")
        self.selection_changed.emit(self.selected_id)
        self._status(message)
        self.busy_changed.emit(self.busy)
        self.failed.emit(message)

    @Slot(object, object)
    def _complete(self, token, result):
        if self._job is None or token is not self._job.token:
            return
        kind = self._job_kind
        self._job = None
        if kind == "scan":
            self.store.update_scan(result, replace=self._scan_replace)
            self.changed.emit()
            self.refreshed.emit()
            self._status(text("壁纸库已更新，共 ", "Library updated: ") + str(len(self.store.projects)) +
                         ("\n" + "\n".join(result.warnings[:8]) if result.warnings else ""))
            self.busy_changed.emit(self.busy)
            return
        if result.kind == "image":
            from .ui.glass import read_wallpaper
            if read_wallpaper(result.path).isNull():
                self._status(text("图片无法解码，保留原壁纸。", "Cannot decode image; previous wallpaper retained."))
                self.selected_id = self.db.get_setting(SELECTED_KEY, "")
                self.selection_changed.emit(self.selected_id)
                self.busy_changed.emit(False)
                self.failed.emit(self.status)
                return
        keys = (config.S_GLASS_WALLPAPER, config.S_GLASS_MEDIA_TYPE,
                config.S_GLASS_IMPORT_MODE, config.S_GLASS_IMPORT_TITLE,
                config.S_GLASS_IMPORT_SOURCE_TYPE, SELECTED_KEY)
        self._pending = {key: self.db.get_setting(key, "image" if key == config.S_GLASS_MEDIA_TYPE else "")
                         for key in keys}
        self.store.results[self._candidate] = result
        self.store.save()
        for key, value in ((config.S_GLASS_WALLPAPER, result.path), (config.S_GLASS_MEDIA_TYPE, result.kind),
                           (config.S_GLASS_IMPORT_MODE, result.mode), (config.S_GLASS_IMPORT_TITLE, result.title),
                           (config.S_GLASS_IMPORT_SOURCE_TYPE, result.source_type), (SELECTED_KEY, self._candidate)):
            self.db.set_setting(key, value)
        if self.background is not None:
            self.background.refresh()
            from .ui.glass import apply_backdrop
            window = self.background.window()
            if hasattr(window, "centralWidget"):
                apply_backdrop(window, self.db)
        if result.kind == "image":
            self.confirm_ready()
        self.changed.emit()

    @Slot()
    def confirm_ready(self):
        if self._pending is None:
            return
        self._pending = None
        self.selected_id = self.db.get_setting(SELECTED_KEY, "")
        self.selection_changed.emit(self.selected_id)
        self._status(text("已应用壁纸", "Wallpaper applied"))
        self.busy_changed.emit(False)
        self.applied.emit(self.selected_id)

    def _restore_pending(self):
        if self._pending is None:
            return
        previous, self._pending = self._pending, None
        for key, value in previous.items():
            self.db.set_setting(key, value)
        self.selected_id = self.db.get_setting(SELECTED_KEY, "")
        self.selection_changed.emit(self.selected_id)
        if self.background is not None:
            self.background.refresh()

    @Slot(str)
    def renderer_failed(self, message):
        if self._pending is None:
            return
        self._restore_pending()
        self._status(message)
        self.busy_changed.emit(False)
        self.failed.emit(message)

    def clear(self):
        self.cancel()
        self._pending = None
        self.db.set_setting(config.S_GLASS_WALLPAPER, "")
        self.db.set_setting(config.S_GLASS_MEDIA_TYPE, "image")
        for key in (config.S_GLASS_IMPORT_MODE, config.S_GLASS_IMPORT_TITLE,
                    config.S_GLASS_IMPORT_SOURCE_TYPE, SELECTED_KEY):
            self.db.set_setting(key, "")
        self.selected_id = ""
        # Clearing a wallpaper explicitly stops rotation, preserving its lists.
        rotation = getattr(self.background, "rotation", None)
        if rotation is not None:
            rotation.activate("")
        if self.background is not None:
            self.background.refresh()
            from .ui.glass import apply_backdrop
            window = self.background.window()
            if hasattr(window, "centralWidget"):
                apply_backdrop(window, self.db)
        self.selection_changed.emit("")
        self.changed.emit()
        self.applied.emit("")

    def release_selection(self):
        """Hand selection back to an ordinary local file picker safely."""
        self.cancel()
        self._restore_pending()
        self.selected_id = ""
        self.db.set_setting(SELECTED_KEY, "")
        rotation = getattr(self.background, "rotation", None)
        if rotation is not None:
            rotation.activate("")
        self.selection_changed.emit("")
        self.busy_changed.emit(False)
