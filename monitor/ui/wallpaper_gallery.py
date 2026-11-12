"""Paged visual library with bounded asynchronous thumbnail decoding."""
from __future__ import annotations

from collections import OrderedDict
import os

from PySide6.QtCore import QCoreApplication, QObject, QRunnable, QSize, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QImage, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog,
    QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem, QTabWidget,
    QVBoxLayout, QWidget)

from .. import config
from ..wallpaper_library import source_available, text
from .controls import SearchBar, TextButton

_thumbnail_pool = None


def thumbnail_pool():
    global _thumbnail_pool
    if _thumbnail_pool is None:
        _thumbnail_pool = QThreadPool(QCoreApplication.instance())
        _thumbnail_pool.setMaxThreadCount(2)
        _thumbnail_pool.setExpiryTimeout(1000)
    return _thumbnail_pool


class _ThumbnailSignals(QObject):
    ready = Signal(object, object, object)


class _ThumbnailJob(QRunnable):
    def __init__(self, key, path):
        super().__init__()
        self.key, self.path = key, path
        self.signals = _ThumbnailSignals()
        self.cancelled = False

    def run(self):
        image = QImage()
        if not self.cancelled:
            reader = QImageReader(self.path)
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid() and size.width() * size.height() <= 64_000_000:
                reader.setScaledSize(size.scaled(320, 180, Qt.KeepAspectRatio))
                image = reader.read()
                if not image.isNull():
                    image = image.scaled(320, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if not self.cancelled:
            self.signals.ready.emit(self, self.key, image)


class ThumbnailCache(QObject):
    ready = Signal(object, object)
    LIMIT = 128
    MAX_PENDING = 48

    def __init__(self, parent=None):
        super().__init__(parent)
        self.images = OrderedDict()
        self.pending = {}
        self.destroyed.connect(self.cancel)

    def request(self, path):
        if not path:
            return None, QImage()
        try:
            stat = os.stat(path)
            key = (path, stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None, QImage()
        if key in self.images:
            image = self.images.pop(key)
            self.images[key] = image
            return key, image
        if key not in self.pending and len(self.pending) < self.MAX_PENDING:
            job = _ThumbnailJob(key, path)
            self.pending[key] = job
            job.signals.ready.connect(self._ready, Qt.QueuedConnection)
            thumbnail_pool().start(job)
        return key, QImage()

    @Slot(object, object, object)
    def _ready(self, job, key, image):
        if self.pending.get(key) is not job:
            return
        self.pending.pop(key, None)
        self.images[key] = image
        while len(self.images) > self.LIMIT:
            self.images.popitem(last=False)
        self.ready.emit(key, image)

    def cancel(self, *_):
        for job in self.pending.values():
            job.cancelled = True
        self.pending.clear()


class GalleryGrid(QWidget):
    activated = Signal(str)
    PAGE_SIZE = 48

    def __init__(self, library, *, selection_mode=False, parent=None):
        super().__init__(parent)
        self.library, self.selection_mode = library, selection_mode
        self.page = 0
        self.checked = set()
        self._thumb_items = {}
        self.cache = ThumbnailCache(self)
        self.cache.ready.connect(self._thumbnail_ready)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        filters = QHBoxLayout()
        self.search = SearchBar(text("搜索壁纸名称…", "Search wallpapers…"), self)
        self.search.textChanged.connect(self._filter_changed)
        filters.addWidget(self.search, 1)
        self.types = QComboBox(self)
        for name, value in ((text("全部类型", "All types"), ""), (text("图片", "Images"), "image"),
                            (text("视频", "Videos"), "video"), (text("场景", "Scenes"), "scene"),
                            (text("网页", "Web"), "web")):
            self.types.addItem(name, value)
        self.types.currentIndexChanged.connect(self._filter_changed)
        filters.addWidget(self.types)
        layout.addLayout(filters)
        self.items = QListWidget(self)
        self.items.setViewMode(QListView.IconMode)
        self.items.setResizeMode(QListView.Adjust)
        self.items.setMovement(QListView.Static)
        self.items.setWrapping(True)
        self.items.setWordWrap(True)
        self.items.setSpacing(config.sx(8))
        self.items.setIconSize(QSize(config.sx(170), config.sx(100)))
        self.items.setGridSize(QSize(config.sx(190), config.sx(162)))
        self.items.setSelectionMode(QAbstractItemView.MultiSelection)
        self.items.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.items.itemClicked.connect(self._clicked)
        self.items.setStyleSheet(f"QListWidget {{ background: transparent; border: none; }} "
            f"QListWidget::item {{ border: 2px solid transparent; border-radius: {config.sx(10)}px; "
            f"padding: {config.sx(5)}px; color: {config.C_TEXT}; }} "
            f"QListWidget::item:selected {{ border-color: {config.C_ACCENT}; background: {config.C_SURFACE}; }}")
        layout.addWidget(self.items, 1)
        self.empty = QLabel(text("添加本地目录，或发现 Wallpaper Engine 壁纸库。", "Add a local folder or discover your Wallpaper Engine library."))
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        pages = QHBoxLayout()
        self.previous = TextButton(text("上一页", "Previous"), "subtle", height=30)
        self.next = TextButton(text("下一页", "Next"), "subtle", height=30)
        self.previous.clicked.connect(lambda: self._change_page(-1))
        self.next.clicked.connect(lambda: self._change_page(1))
        self.page_label = QLabel(self)
        pages.addWidget(self.previous)
        pages.addStretch(1)
        pages.addWidget(self.page_label)
        pages.addStretch(1)
        pages.addWidget(self.next)
        layout.addLayout(pages)
        self.library.changed.connect(self.refresh)
        self.library.selection_changed.connect(self._selected_changed)
        self.refresh()

    def _filter_changed(self, *_):
        self.page = 0
        self.refresh()

    def _change_page(self, delta):
        self.page = max(0, self.page + delta)
        self.refresh()

    def set_checked(self, identities):
        self.checked = set(identities)
        self._selected_changed(self.library.selected_id)

    def _selected_changed(self, identity):
        for row in range(self.items.count()):
            item = self.items.item(row)
            value = item.data(Qt.UserRole)
            item.setSelected(value in self.checked if self.selection_mode else value == identity)

    def _placeholder(self, close=False):
        pixmap = QPixmap(config.sx(170), config.sx(100))
        pixmap.fill(QColor(config.C_SURFACE))
        painter = QPainter(pixmap)
        painter.setPen(QColor(config.C_TEXT_MUTED))
        painter.drawText(pixmap.rect(), Qt.AlignCenter, "×" if close else "▧")
        painter.end()
        return QIcon(pixmap)

    def refresh(self):
        query, kind = self.search.text().strip().casefold(), self.types.currentData()
        projects = [p for p in self.library.store.projects if (not query or query in p.title.casefold())
                    and (not kind or p.type == kind)]
        if self.selection_mode:
            projects = [p for p in projects if p.import_mode != "unsupported"]
        pages = max(1, (len(projects) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = min(self.page, pages - 1)
        self.cache.cancel()
        self.items.clear()
        self._thumb_items.clear()
        if not self.selection_mode:
            close = QListWidgetItem(self._placeholder(True), text("关闭壁纸\n使用系统玻璃", "Wallpaper off\nSystem glass"))
            close.setData(Qt.UserRole, "")
            self.items.addItem(close)
        for project in projects[self.page * self.PAGE_SIZE:(self.page + 1) * self.PAGE_SIZE]:
            live = project.import_mode in {"live_scene", "live_web"}
            mode = text("实时", "Live") if live else text("动态", "Motion") if project.import_mode == "video" else text("静态", "Static")
            if project.import_mode == "unsupported":
                mode = text("不支持", "Unsupported")
            available = source_available(project)
            status = "" if available else " · " + text("源缺失", "Missing source")
            type_label = {"image": text("图片", "Image"), "video": text("视频", "Video"),
                          "scene": text("场景", "Scene"), "web": text("网页", "Web"),
                          "application": text("应用程序", "Application")}.get(project.type, project.type)
            item = QListWidgetItem(self._placeholder(), project.title[:52] + "\n" + type_label + " · " + mode + status)
            item.setData(Qt.UserRole, project.id)
            item.setToolTip(project.title + "\n" + project.project_dir + ("\n" + project.reason if project.reason else ""))
            self.items.addItem(item)
            result = self.library.store.results.get(project.id)
            preview = project.preview or (result.preview if result else None)
            key, image = self.cache.request(preview)
            if key is not None:
                self._thumb_items.setdefault(key, []).append(item)
            if not image.isNull():
                item.setIcon(QIcon(QPixmap.fromImage(image)))
            if not available:
                item.setForeground(QColor(config.C_TEXT_MUTED))
        self.empty.setVisible(not projects)
        if not projects and self.library.store.projects:
            self.empty.setText(text("没有匹配的壁纸。", "No matching wallpapers."))
        self.page_label.setText(f"{self.page + 1} / {pages} · {len(projects)}")
        self.previous.setEnabled(self.page > 0)
        self.next.setEnabled(self.page + 1 < pages)
        self._selected_changed(self.library.selected_id)

    @Slot(object, object)
    def _thumbnail_ready(self, key, image):
        if not image.isNull():
            for item in self._thumb_items.get(key, []):
                item.setIcon(QIcon(QPixmap.fromImage(image)))

    def _clicked(self, item):
        identity = item.data(Qt.UserRole)
        self.activated.emit(identity)
        self._selected_changed(self.library.selected_id)

    def hideEvent(self, event):
        self.cache.cancel()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()


class WallpaperGalleryDialog(QDialog):
    def __init__(self, library, rotation, parent=None):
        super().__init__(parent)
        self.library = library
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle(text("壁纸库与轮播", "Wallpaper library and rotation"))
        self.resize(config.sx(920), config.sx(720))
        self.setMinimumSize(config.sx(700), config.sx(520))
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        for label, callback in ((text("发现 WE 壁纸库", "Discover WE library"), lambda: library.scan(discover=True)),
                                (text("添加目录", "Add folder"), self._browse),
                                (text("添加图片 / 视频", "Add image / video"), self._file),
                                (text("刷新", "Refresh"), lambda: library.scan()),
                                (text("取消操作", "Cancel task"), library.cancel_work)):
            button = TextButton(label, "ghost", height=32)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        self.sources = QLabel(self)
        self.sources.setWordWrap(True)
        layout.addWidget(self.sources)
        self.tabs = QTabWidget(self)
        self.gallery = GalleryGrid(library, parent=self)
        self.gallery.activated.connect(library.select)
        self.tabs.addTab(self.gallery, text("壁纸库", "Library"))
        from .wallpaper_rotation import RotationEditor
        self.rotation_editor = RotationEditor(rotation, self)
        self.tabs.addTab(self.rotation_editor, text("轮播列表", "Rotation lists"))
        layout.addWidget(self.tabs, 1)
        self.status = QLabel(library.status or text("点击卡片即可应用。源文件变动后可刷新；目录与列表会自动保存。",
            "Click a card to apply. Refresh after source changes. Folders and lists are saved automatically."), self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        library.status_changed.connect(self.status.setText)
        library.changed.connect(self._sources)
        self._sources()

    def _sources(self):
        roots = self.library.store.roots
        self.sources.setText(text("已保存目录：", "Saved folders: ") +
            (" · ".join(os.path.basename(p.rstrip("/\\")) or p for p in roots) if roots else text("尚未添加", "None")))
        self.sources.setToolTip("\n".join(roots))

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, text("添加壁纸目录", "Add wallpaper folder"))
        if path:
            self.library.scan(path)

    def _file(self):
        path, _ = QFileDialog.getOpenFileName(self, text("添加图片或视频", "Add image or video"), "",
            "Media (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.avif *.mp4 *.webm *.mkv *.mov *.m4v)")
        if path:
            self.library.scan(path)

    def done(self, result):
        # Closing a picker cancels extraction, never a confirmed wallpaper.
        self.library.cancel_work()
        self.gallery.cache.cancel()
        self.rotation_editor.grid.cache.cancel()
        super().done(result)
