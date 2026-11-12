"""Cached wallpaper material and Windows compositor backdrop (no screen capture)."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import math
import os
import sys
import time

from PySide6.QtCore import QEvent, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QLinearGradient, QPainter, QPainterPath, QPixmap, QRegion
from PySide6.QtWidgets import QApplication, QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene, QWidget

from .. import config
from . import glass_effects


@dataclass(frozen=True)
class GlassOptions:
    wallpaper: str = ""
    blur: int = 28
    opacity: int = 44
    mask: int = 26
    wallpaper_tint: bool = True
    media_type: str = "image"

    @classmethod
    def read(cls, db):
        def percent(key, default, low=0, high=100):
            value = db.get_float(key, default)
            return max(low, min(high, round(value))) if math.isfinite(value) else default
        return cls(db.get_setting(config.S_GLASS_WALLPAPER, ""),
                   percent(config.S_GLASS_BLUR, 28, 0, 64),
                   percent(config.S_GLASS_OPACITY, 44, 15, 90),
                   percent(config.S_GLASS_MASK, 26, 0, 75),
                   db.get_bool(config.S_GLASS_TINT, True),
                   db.get_setting(config.S_GLASS_MEDIA_TYPE, "image"))


def multimedia_available() -> bool:
    try:
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink  # noqa: F401
        return True
    except (ImportError, OSError):
        return False


def configure(db) -> GlassOptions:
    glass_effects.configure(db)
    options = GlassOptions.read(db)
    config.set_glass_options({"blur": options.blur, "opacity": options.opacity,
                              "mask": options.mask})
    return options


@dataclass(frozen=True)
class BackdropState:
    available: bool = False
    mode: str = "fallback"
    reason: str = "unsupported"


def read_wallpaper(path: str) -> QImage:
    """Bound decoding allocation; decode EXIF orientation and preserve aspect ratio."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    # Reject pathological/broken images before asking Qt to allocate them.
    if not size.isValid() or size.width() * size.height() > 120_000_000:
        return QImage()
    if size.width() > 1600 or size.height() > 1100:
        reader.setScaledSize(size.scaled(1600, 1100, Qt.KeepAspectRatio))
    image = reader.read()
    if not image.isNull():
        image = image.scaled(1600, 1100, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return image


def _blur_image(source: QImage, radius: int) -> QImage:
    if source.isNull() or radius <= 0:
        return source
    # Work at half resolution. Qt raster blur runs once per image/radius change,
    # never in paintEvent or on a timer. Bound both source and resulting cache.
    image = (source.scaled(800, 550, Qt.KeepAspectRatio, Qt.SmoothTransformation)
             if source.width() > 800 or source.height() > 550 else source)
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(QPixmap.fromImage(image))
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius / 2)
    effect.setBlurHints(QGraphicsBlurEffect.QualityHint)
    item.setGraphicsEffect(effect)
    scene.addItem(item)
    output = QImage(image.size(), QImage.Format_ARGB32_Premultiplied)
    output.fill(QColor("#152039"))
    painter = QPainter(output)
    scene.render(painter, QRectF(output.rect()), QRectF(image.rect()))
    painter.end()
    return output


class GlassBackground(QWidget):
    """Root canvas. Child surfaces stay transparent so the material is continuous."""

    video_failed = Signal(str)
    video_ready = Signal()
    video_status_changed = Signal()

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setObjectName("AppRoot")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self._source_key = None
        self._blur_key = None
        self._source = QImage()
        self._image = QImage()
        self._tint = QColor("#6C80C9")
        self._player = None
        self._audio = None
        self._video_sink = None
        self._video_path = ""
        self.video_status = ""
        self.video_error = ""
        self._last_video_frame = 0.0
        self._video_has_frame = False
        self._live_surface = None
        self._live_overlay = None
        self._live_path = ""
        self._live_ready = False
        self.native_available = False
        self.options = GlassOptions()
        self._resources_released = False
        self._idle_release = QTimer(self)
        self._idle_release.setSingleShot(True)
        self._idle_release.setInterval(60_000)
        self._idle_release.timeout.connect(self._release_idle_resources)
        self._lifetime_change = QTimer(self)
        self._lifetime_change.setSingleShot(True)
        self._lifetime_change.setInterval(0)
        self._lifetime_change.timeout.connect(self._sync_resource_lifetime)
        self.window().installEventFilter(self)
        self.refresh()
        from ..wallpaper_library import WallpaperLibrary
        from .wallpaper_rotation import RotationService
        self.library = WallpaperLibrary(db, self, self)
        self.rotation = RotationService(db, self.library, self, self)

    def refresh(self):
        self._resources_released = False
        self._lifetime_change.start()
        glass_effects.configure(self.db)
        self.options = GlassOptions.read(self.db)
        if hasattr(self, "rotation"):
            self.rotation.sync()
        if config.current_theme() != config.THEME_GLASS:
            # Theme changes are explicit: release immediately rather than
            # keeping the previous browser alive for the hide grace period.
            self.clear_material_cache()
            self._resources_released = True
            self._idle_release.stop()
            self.update()
            return
        path = self.options.wallpaper
        if path and self.options.media_type in {"live_scene", "live_web"}:
            if self._video_path:
                self._stop_video()
            self._refresh_live(path)
            self.update()
            return
        self._stop_live()
        if path and self.options.media_type == "video":
            self._refresh_video(path)
            self.update()
            return
        self._stop_video()
        try:
            stat = os.stat(path)
            key = (path, stat.st_mtime_ns, stat.st_size)
        except OSError:
            key = (path, None)
        if key != self._source_key:
            self._source_key = key
            self._source = read_wallpaper(path) if path else QImage()
            self._blur_key = None
            if not self._source.isNull():
                sample = self._source.scaled(12, 12, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                count = sample.width() * sample.height()
                channels = [sum(sample.pixelColor(x, y).getRgb()[channel]
                                for x in range(sample.width()) for y in range(sample.height())) // count
                            for channel in range(3)]
                color = QColor(*channels)
                hue, saturation, _value, _alpha = color.getHsv()
                self._tint = QColor.fromHsv(max(0, hue), min(120, saturation), 185)
        blur_key = (self._source_key, self.options.blur)
        if blur_key != self._blur_key:
            self._image = _blur_image(self._source, self.options.blur)
            self._blur_key = blur_key
        self.setProperty("wallpaperValid", not self._source.isNull())
        self.update()

    def _set_video_status(self, status):
        if self.video_status != status:
            self.video_status = status
            self.video_status_changed.emit()

    def clear_material_cache(self):
        self._stop_video()
        self._stop_live()
        self._source_key = self._blur_key = None
        self._source = QImage()
        self._image = QImage()
        glass_effects.clear_cache()
        self._last_video_frame = 0.0
        self.setProperty("wallpaperValid", False)
        self.update()

    def _material_visible(self):
        window = self.window()
        return (config.current_theme() == config.THEME_GLASS and self.isVisible()
                and window.isVisible() and not window.isMinimized())

    def _sync_resource_lifetime(self):
        if self._material_visible():
            self._idle_release.stop()
            if self._resources_released:
                self.refresh()
        elif not self._resources_released and not self._idle_release.isActive():
            self._idle_release.start()

    def _release_idle_resources(self):
        if self._material_visible():
            return
        # Retain selection/rotation in the database, not a frozen browser or
        # decoder. A short hide keeps playback position; a long hide reloads.
        self._resources_released = True
        self.clear_material_cache()

    def _refresh_video(self, path):
        if self._video_path == path:
            self._sync_video_playback()
            if self._video_has_frame and self._blur_key != ("video", self.options.blur):
                self._image = _blur_image(self._source, self.options.blur)
                self._blur_key = ("video", self.options.blur)
            return
        self._stop_video()
        self._video_path = path
        self._source_key = None
        self._blur_key = None
        # Keep the previous material as a poster until a valid first frame arrives.
        self.video_error = ""
        self._video_has_frame = False
        self.setProperty("wallpaperValid", not self._source.isNull())
        if not os.path.isfile(path):
            self._on_video_error(None, "Video file unavailable")
            return
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
            if self._player is None:
                self._player = QMediaPlayer(self)
                self._audio = QAudioOutput(self)
                self._audio.setMuted(True)
                self._audio.setVolume(0)
                self._player.setAudioOutput(self._audio)
                self._video_sink = QVideoSink(self)
                self._player.setVideoSink(self._video_sink)
                self._player.setLoops(QMediaPlayer.Infinite)
                self._video_sink.videoFrameChanged.connect(self._on_video_frame)
                self._player.errorOccurred.connect(self._on_video_error)
            self._player.setSource(QUrl.fromLocalFile(path))
            self._set_video_status("loading")
            self._sync_video_playback()
        except (ImportError, OSError, RuntimeError) as error:
            self._on_video_error(None, str(error))

    def _stop_video(self):
        if self._player is not None and self._video_path:
            self._player.stop()
            self._player.setSource(QUrl())
        if self._player is not None:
            self._player.deleteLater()
            self._player = None
        if self._audio is not None:
            self._audio.deleteLater()
            self._audio = None
        if self._video_sink is not None:
            self._video_sink.deleteLater()
            self._video_sink = None
        self._video_path = ""
        self._video_has_frame = False
        self.video_error = ""
        self._set_video_status("")

    def _video_should_play(self):
        window = self.window()
        return bool(self._video_path and not self.video_error
                    and config.current_theme() == config.THEME_GLASS
                    and self.isVisible() and window.isVisible() and not window.isMinimized())

    def _refresh_live(self, path):
        if self._live_surface is not None:
            self._live_surface.set_style(**glass_effects.live_style(), blur=self.options.blur)
        if self._live_path == path:
            self._sync_live_playback()
            if self._live_overlay is not None:
                self._live_overlay.update()
            return
        self._stop_live()
        self._live_path = path
        self.video_error = ""
        self._set_video_status("loading")
        from .live_wallpaper import LiveWallpaperSurface
        self._live_surface = LiveWallpaperSurface(self)
        self._live_surface.setGeometry(self.rect())
        self._live_surface.hide()
        self._live_surface.ready.connect(self._on_live_ready)
        self._live_surface.failed.connect(self._on_live_failed)
        self._live_overlay = _LiveMaterialOverlay(self)
        self._live_overlay.setGeometry(self.rect())
        self._live_overlay.hide()
        self._live_surface.set_style(**glass_effects.live_style(), blur=self.options.blur)
        self._live_surface.load(path, self.options.media_type)
        self._sync_live_playback()

    def _sync_live_playback(self):
        if self._live_surface is None:
            return
        self._layout_live_surfaces()
        window = self.window()
        active = bool(config.current_theme() == config.THEME_GLASS and self.isVisible()
                      and window.isVisible() and not window.isMinimized())
        self._live_surface.set_active(active)
        self._live_surface.setVisible(active and self._live_ready)
        if self._live_overlay is not None:
            self._live_overlay.setVisible(active and self._live_ready)
            self._live_overlay.lower()
        self._live_surface.lower()
        if not self.video_error:
            self._set_video_status("playing" if active and self._live_ready else "loading" if active else "paused")

    def _stop_live(self):
        surface, self._live_surface = self._live_surface, None
        overlay, self._live_overlay = self._live_overlay, None
        was_live = bool(self._live_path)
        self._live_path = ""
        self._live_ready = False
        if surface is not None:
            surface.hide()
            surface.stop()
            surface.deleteLater()
        if overlay is not None:
            overlay.hide()
            overlay.deleteLater()
        if was_live:
            self.video_error = ""
            self._set_video_status("")

    def _on_live_ready(self):
        if self.sender() is not self._live_surface or not self._live_path:
            return
        self._live_ready = True
        # The old image is only a loading poster; the live browser owns the
        # displayed wallpaper after its first frame.
        self._source_key = self._blur_key = None
        self._source = QImage()
        self._image = QImage()
        glass_effects.clear_cache()
        self.setProperty("wallpaperValid", True)
        self._sync_live_playback()
        self.video_ready.emit()
        if hasattr(self.window(), "centralWidget"):
            apply_backdrop(self.window(), self.db)

    def _on_live_failed(self, message):
        if self.sender() is not self._live_surface or not self._live_path:
            return
        self.video_error = message
        if self._live_surface is not None:
            self._live_surface.hide()
            self._live_surface.set_active(False)
        if self._live_overlay is not None:
            self._live_overlay.hide()
        self._set_video_status("error")
        self.video_failed.emit(message)
        self.update()

    def _sync_video_playback(self):
        if self._player is None or not self._video_path or self.video_error:
            return
        if self._video_should_play():
            self._player.play()
            self._set_video_status("playing" if self._video_has_frame else "loading")
        else:
            self._player.pause()
            self._set_video_status("paused")

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Hide, QEvent.Close):
            if self._player is not None and self._video_path:
                self._player.pause()
                self._set_video_status("paused")
            if self._live_surface is not None:
                self._live_surface.set_active(False)
        elif event.type() in (QEvent.Show, QEvent.WindowStateChange):
            self._sync_video_playback()
            self._sync_live_playback()
        if event.type() in (QEvent.Hide, QEvent.Close, QEvent.Show, QEvent.WindowStateChange):
            self._lifetime_change.start()
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_resource_lifetime()
        self._sync_video_playback()
        self._sync_live_playback()
        if hasattr(self, "rotation"):
            self.rotation.sync()

    def hideEvent(self, event):
        self._sync_resource_lifetime()
        if self._player is not None and self._video_path:
            self._player.pause()
            self._set_video_status("paused")
        if self._live_surface is not None:
            self._live_surface.set_active(False)
        if hasattr(self, "rotation"):
            self.rotation.sync()
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_live_surfaces()

    def _layout_live_surfaces(self):
        radius = 0 if self.window().isMaximized() else config.sx(13)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), radius, radius)
        region = QRegion(clip.toFillPolygon().toPolygon())
        for surface in (self._live_surface, self._live_overlay):
            if surface is not None:
                surface.setGeometry(self.rect())
                if radius:
                    surface.setMask(region)
                else:
                    surface.clearMask()

    def _on_video_error(self, _code=None, message=""):
        if not self._video_path:
            return
        # The settings page can restore the previous wallpaper for a new import.
        self.video_error = message or "Video decoding unavailable"
        if self._player is not None:
            self._player.stop()
        self._set_video_status("error")
        self.video_failed.emit(self.video_error)
        self.update()

    def _on_video_frame(self, frame):
        now = time.monotonic()
        if not self._video_should_play() or now - self._last_video_frame < .20:
            return
        image = frame.toImage()
        if image.isNull():
            return
        self._last_video_frame = now
        # At most five bounded, low resolution material updates per second.
        self._source = image.scaled(480, 330, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._image = _blur_image(self._source, self.options.blur)
        self._blur_key = ("video", self.options.blur)
        self._tint = self._source.pixelColor(self._source.width() // 2, self._source.height() // 2)
        self.setProperty("wallpaperValid", True)
        first = not self._video_has_frame
        self._video_has_frame = True
        self._set_video_status("playing")
        if first:
            self.video_ready.emit()
            if hasattr(self.window(), "centralWidget"):
                apply_backdrop(self.window(), self.db)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        radius = 0 if self.window().isMaximized() else config.sx(13)
        clip = QPainterPath()
        clip.addRoundedRect(rect, radius, radius)
        painter.setClipPath(clip)
        if config.current_theme() != config.THEME_GLASS:
            painter.fillRect(rect, QColor(config.C_APP_BG))
        else:
            if not self._image.isNull():
                glass_effects.draw_wallpaper(painter, rect, self._image)
            elif not self.native_available:
                gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
                gradient.setColorAt(0, QColor("#31466A"))
                gradient.setColorAt(.46, QColor("#282E50"))
                gradient.setColorAt(1, QColor("#163E46"))
                painter.fillRect(rect, gradient)
            if self.options.wallpaper_tint and not self._source.isNull():
                tint = QColor(self._tint)
                tint.setAlpha(36)
                painter.fillRect(rect, tint)
            mask = QColor("#07111F")
            mask.setAlpha(round(self.options.mask * 2.55))
            painter.fillRect(rect, mask)
            # Thin luminous upper edge lends material depth without motion.
            sheen = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            sheen.setColorAt(0, QColor(220, 235, 255, 18))
            sheen.setColorAt(.45, QColor(220, 235, 255, 0))
            painter.fillRect(rect, sheen)
        painter.setClipping(False)
        painter.setPen(QColor(config.C_APP_BORDER))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)
        painter.end()


class _LiveMaterialOverlay(QWidget):
    """Tint and mask above the live surface, below all application controls."""
    def __init__(self, background):
        super().__init__(background)
        self.background = background
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)

    def paintEvent(self, event):
        root = self.background
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        radius = 0 if root.window().isMaximized() else config.sx(13)
        clip = QPainterPath()
        clip.addRoundedRect(rect, radius, radius)
        painter.setClipPath(clip)
        if root.options.wallpaper_tint:
            color = QColor(root._tint)
            color.setAlpha(36)
            painter.fillRect(rect, color)
        color = QColor("#07111F")
        color.setAlpha(round(root.options.mask * 2.55))
        painter.fillRect(rect, color)
        painter.setPen(QColor(config.C_APP_BORDER))
        painter.drawRoundedRect(rect, radius, radius)
        painter.end()


def _native_backdrop(hwnd: int, enabled: bool, opacity: int) -> BackdropState:
    """Ask Windows to compose actual surfaces behind this HWND, never capture them."""
    from ctypes import wintypes
    dwm = ctypes.WinDLL("dwmapi")
    user = ctypes.WinDLL("user32", use_last_error=True)
    dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    dwm.DwmSetWindowAttribute.restype = ctypes.c_long
    class Margins(ctypes.Structure):
        _fields_ = [(name, ctypes.c_int) for name in ("left", "right", "top", "bottom")]
    dwm.DwmExtendFrameIntoClientArea.argtypes = [wintypes.HWND, ctypes.POINTER(Margins)]
    dwm.DwmExtendFrameIntoClientArea.restype = ctypes.c_long
    backdrop = ctypes.c_int(3 if enabled else 1)  # DWMSBT_TRANSIENTWINDOW / NONE
    dark = ctypes.c_int(0 if config.current_theme() == config.THEME_LIGHT else 1)
    dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
    result = dwm.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop))
    margins = Margins(*([-1] * 4 if enabled else [1] * 4))
    frame_result = dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
    # Clear the legacy accent policy even after switching wallpaper/theme.
    class Accent(ctypes.Structure):
        _fields_ = [("state", ctypes.c_int), ("flags", ctypes.c_int),
                    ("color", wintypes.DWORD), ("animation", ctypes.c_int)]
    class AttributeData(ctypes.Structure):
        _fields_ = [("attribute", ctypes.c_int), ("data", ctypes.c_void_p), ("size", ctypes.c_size_t)]
    setter = getattr(user, "SetWindowCompositionAttribute", None)
    def accent(state):
        if setter is None:
            return False
        setter.argtypes = [wintypes.HWND, ctypes.POINTER(AttributeData)]
        setter.restype = wintypes.BOOL
        # ABGR tint, low enough alpha to reveal the compositor blur.
        policy = Accent(state, 2, (round(opacity * 1.4) << 24) | 0x302016, 0)
        data = AttributeData(19, ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p), ctypes.sizeof(policy))
        return bool(setter(hwnd, ctypes.byref(data)))
    accent(0)
    if not enabled:
        return BackdropState(False, "off", "disabled")
    if result == 0 and frame_result == 0:
        return BackdropState(True, "dwm-acrylic", "system")
    if accent(4):
        return BackdropState(True, "accent-acrylic", "system")
    # Ensure there is no partial glass state after both methods fail.
    dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(Margins(1, 1, 1, 1)))
    return BackdropState(False, "fallback", "unsupported")


def apply_backdrop(window, db) -> BackdropState:
    options = GlassOptions.read(db)
    root = window.centralWidget()
    wallpaper_valid = bool(isinstance(root, GlassBackground) and (not root._source.isNull() or root._live_ready))
    enabled = config.current_theme() == config.THEME_GLASS and not wallpaper_valid
    signature = (enabled, options.opacity)
    # Never call native APIs in offscreen tests or on non-Windows hosts.
    if sys.platform != "win32" or QApplication.platformName() in ("offscreen", "minimal"):
        state = BackdropState(False, "fallback" if enabled else "off", "unsupported" if enabled else "disabled")
    elif getattr(window, "_glass_native_signature", None) == signature:
        state = getattr(window, "_glass_backdrop_state", BackdropState())
    else:
        try:
            state = _native_backdrop(int(window.winId()), enabled, options.opacity)
        except (OSError, AttributeError, ValueError):
            state = BackdropState(False, "fallback", "unsupported")
        window._glass_native_signature = signature
    window._glass_backdrop_state = state
    if isinstance(root, GlassBackground):
        root.native_available = state.available
        root.update()
    return state


def refresh_window(window, db) -> BackdropState:
    root = window.centralWidget()
    if isinstance(root, GlassBackground):
        root.refresh()
    state = apply_backdrop(window, db)
    settings = getattr(window, "settings_page", None)
    if settings is not None and hasattr(settings, "refresh_glass_status"):
        settings.refresh_glass_status(state)
    return state
