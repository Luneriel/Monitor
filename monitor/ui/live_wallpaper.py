"""Optional, isolated QWebEngine widget for real Scene and local Web wallpapers.

The browser is imported lazily: normal native pages and installations without
WebEngine retain the existing media background. No native API is exposed to JS.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

from PySide6.QtCore import QEvent, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..we_runtime import WallpaperOrigin, request_allowed


def webengine_available() -> bool:
    import importlib.util
    return (importlib.util.find_spec("PySide6.QtWebEngineCore") is not None
            and importlib.util.find_spec("PySide6.QtWebEngineWidgets") is not None)


class LiveWallpaperSurface(QWidget):
    ready = Signal()
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._origin = self._view = self._page = self._profile = None
        self._interceptor = None
        self._generation = 0
        self._loaded = False
        self._active = True
        self._disposed = False
        self.error_message = ""
        self._kind = ""
        # Keep a cleanup bag independent of this QObject: parent destruction can
        # occur without closeEvent, and a loopback service must never outlive it.
        self._cleanup_bag = {}
        def cleanup(_object=None, bag=self._cleanup_bag):
            origin = bag.pop("origin", None)
            bag["destroyed"] = True
            if origin:
                origin.close(wait=False)
        self.destroyed.connect(cleanup)
        self._style = {"fit": "cover", "x": 50.0, "y": 50.0, "scale": 100.0,
                       "brightness": 1.0, "contrast": 1.0, "saturation": 1.0, "blur": 0.0}
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._poll = QTimer(self)
        self._poll.setInterval(250)
        self._poll.timeout.connect(self._check_state)
        self._load_watchdog = QTimer(self)
        self._load_watchdog.setSingleShot(True)
        self._load_watchdog.setInterval(35000)
        self._load_watchdog.timeout.connect(lambda: self._load_timeout(self._generation))
        self.window().installEventFilter(self)

    def _make_browser(self):
        from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile,
            QWebEngineSettings, QWebEngineUrlRequestInterceptor, QWebEngineScript)
        from PySide6.QtWebEngineWidgets import QWebEngineView
        surface = self

        class Fence(QWebEngineUrlRequestInterceptor):
            def interceptRequest(self, info):
                info.block(not (surface._origin and request_allowed(
                    info.requestUrl().toString(), surface._origin.base_url)))

        class Page(QWebEnginePage):
            def acceptNavigationRequest(self, url, nav_type, main_frame):
                # The trusted top document cannot be replaced by a project link.
                if main_frame:
                    return bool(surface._origin and url.toString().startswith(
                        surface._origin.base_url + "runtime/index.html"))
                return bool(surface._origin and request_allowed(url.toString(), surface._origin.base_url))

            def createWindow(self, _kind):
                return None

            def chooseFiles(self, *_args):
                return []

            def javaScriptConfirm(self, *_args):
                return False

            def javaScriptPrompt(self, *_args):
                return False, ""

            def javaScriptAlert(self, *_args):
                pass

        # Unnamed profile is off-the-record: no shared cookies, cache, persisted
        # permissions, history, extensions, downloads or account sessions.
        self._profile = QWebEngineProfile(self)
        self._profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
        self._profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        self._profile.downloadRequested.connect(lambda request: request.cancel())
        self._interceptor = Fence(self._profile)
        self._profile.setUrlRequestInterceptor(self._interceptor)
        self._page = Page(self._profile, self)
        # A transparent WebEngine page makes Qt's internal Quick widget use
        # WA_AlwaysStackOnTop, ignoring QWidget sibling order. That lets the
        # wallpaper cover the controls even after lower(). Only the outer
        # application window needs transparency; this bottom layer is opaque.
        self._page.setBackgroundColor(QColor("#111827"))
        self._page.setAudioMuted(True)
        settings = self._page.settings()
        for name, value in {"JavascriptEnabled": True, "WebGLEnabled": True,
                "JavascriptCanOpenWindows": False, "JavascriptCanAccessClipboard": False,
                "LocalContentCanAccessRemoteUrls": False, "LocalContentCanAccessFileUrls": False,
                "PluginsEnabled": False, "FullScreenSupportEnabled": False,
                "ScreenCaptureEnabled": False, "AllowGeolocationOnInsecureOrigins": False,
                "HyperlinkAuditingEnabled": False, "PdfViewerEnabled": False,
                "NavigateOnDropEnabled": False, "WebRTCPublicInterfacesOnly": True,
                "PlaybackRequiresUserGesture": False, "JavascriptCanPaste": False}.items():
            option = getattr(QWebEngineSettings.WebAttribute, name, None)
            if option is not None:
                settings.setAttribute(option, value)
        # Do not let WebRTC's non-URL-request sockets bypass the local-only fence.
        lockdown = QWebEngineScript()
        lockdown.setName("Monitor offline network boundary")
        lockdown.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        lockdown.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        lockdown.setRunsOnSubFrames(True)
        lockdown.setSourceCode("""for (const key of ['RTCPeerConnection', 'webkitRTCPeerConnection',
            'RTCDataChannel', 'WebTransport']) { try { Object.defineProperty(window, key,
            {value: undefined, writable: false, configurable: false}); } catch (_) {} }
            try { Object.defineProperty(navigator, 'serviceWorker', {value: undefined}); } catch (_) {}
            if (window !== window.parent) {
                // A silent compatibility seed only: never capture system audio.
                window.wallpaperRegisterAudioListener = listener => {
                    if (typeof listener === 'function') queueMicrotask(() => listener(new Array(128).fill(0)));
                };
                const report = reason => {
                    try { window.parent.postMessage({__monitorWallpaperError: true,
                        error: String(reason?.message || reason).slice(0, 600)}, '*'); } catch (_) {}
                };
                window.addEventListener('error', event => {
                    if (event.error || event.message) report(event.error || event.message);
                });
                window.addEventListener('unhandledrejection', event => report(event.reason || 'Unhandled script rejection'));
            }
            window.addEventListener('message', event => {
                if (window === window.parent || event.source !== window.parent || !event.data?.__monitorWallpaper) return;
                const listener = window.wallpaperPropertyListener;
                try {
                    if (event.data.op === 'pause') listener?.setPaused?.(!!event.data.paused);
                    if (event.data.op === 'properties') listener?.applyUserProperties?.(event.data.properties || {});
                } catch (_) {}
            });""")
        self._page.scripts().insert(lockdown)
        if hasattr(self._page, "permissionRequested"):
            self._page.permissionRequested.connect(lambda permission: permission.deny())
        self._page.featurePermissionRequested.connect(lambda origin, feature:
            self._page and self._page.setFeaturePermission(origin, feature,
                QWebEnginePage.PermissionPolicy.PermissionDeniedByUser))
        self._page.fullScreenRequested.connect(lambda request: request.reject())
        current_page, generation = self._page, self._generation
        self._page.renderProcessTerminated.connect(lambda *_:
            self._fail("实时壁纸渲染进程已停止。")
            if self._page is current_page and generation == self._generation else None)
        self._page.loadFinished.connect(lambda ok:
            self._on_document_loaded(ok)
            if self._page is current_page and generation == self._generation else None)
        self._view = QWebEngineView(self)
        self._view.setPage(self._page)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self._view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._view.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._layout.addWidget(self._view)
        self._view.hide()

    def load(self, asset_path: str, asset_type: str) -> bool:
        self.dispose()
        self._disposed = False
        self.error_message = ""
        self._kind = asset_type
        try:
            self._origin = WallpaperOrigin(asset_path, asset_type)
            self._origin.start()
            self._cleanup_bag["origin"] = self._origin
            self._make_browser()
            self._page.setUrl(QUrl(self._origin.base_url + "runtime/index.html?" +
                urlencode({"kind": asset_type, "entry": self._origin.entry})))
            # Keep Chromium's loading document visible to its scheduler even
            # while the view stays hidden behind the previous wallpaper. A
            # hidden page can otherwise stop rAF before WebGL's first frame.
            self._page.setVisible(True)
            self._update_active()
            return True
        except Exception as exc:
            self._fail(f"实时壁纸无法启动：{exc}")
            return False

    def _load_timeout(self, generation):
        if (not self._cleanup_bag.get("destroyed") and generation == self._generation
                and not self._disposed and not self._loaded):
            self._fail("实时壁纸未在 35 秒内显示首帧；请改用预览或其他壁纸。")

    def _on_document_loaded(self, ok):
        if not ok and not self._disposed:
            self._fail("本地实时壁纸页面加载失败。")

    def _check_state(self):
        if not self._page or self._disposed:
            return
        generation = self._generation
        def receive(raw):
            if (generation != self._generation or self._disposed
                    or self._cleanup_bag.get("destroyed")):
                return
            try:
                state = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                return
            if state.get("status") == "failed":
                self._fail("实时渲染失败：" + str(state.get("error", "未知错误")))
            elif state.get("status") == "ready" and not self._loaded:
                self._loaded = True
                self._load_watchdog.stop()
                self._apply_style()
                self._update_active()
                self.ready.emit()
        self._page.runJavaScript("JSON.stringify(window.__monitorState || {})", receive)

    def _fail(self, message):
        if self._disposed:
            return
        self.error_message = str(message)
        self.dispose()
        self.failed.emit(self.error_message)

    def set_style(self, *, fit=None, x=None, y=None, scale=None, brightness=None,
                  contrast=None, saturation=None, blur=None):
        import math
        values = {"fit": fit, "x": x, "y": y, "scale": scale, "brightness": brightness,
                  "contrast": contrast, "saturation": saturation, "blur": blur}
        for key, value in values.items():
            if key == "self" or value is None:
                continue
            if key == "fit":
                if value not in {"cover", "contain", "stretch"}:
                    raise ValueError("Unsupported wallpaper fit")
            else:
                value = float(value)
                limits = {"x": (0, 100), "y": (0, 100), "scale": (50, 150),
                          "brightness": (0, 3), "contrast": (0, 3), "saturation": (0, 3), "blur": (0, 100)}
                low, high = limits[key]
                if not math.isfinite(value) or not low <= value <= high:
                    raise ValueError(f"Wallpaper style {key} out of range")
            self._style[key] = value
        self._apply_style()

    def _apply_style(self):
        if self._page and self._loaded:
            self._page.runJavaScript("window.__monitor?.style(" + json.dumps(self._style) + ")")

    def pause(self, paused=True):
        self.set_active(not paused)

    def set_active(self, active):
        self._active = bool(active)
        self._update_active()

    def _update_active(self):
        if not self._page:
            return
        # Before first frame the surface is deliberately hidden. Its window
        # still controls loading activity; afterwards the surface's visibility
        # additionally controls actual playback.
        active = (self._active and self.window().isVisible() and not self.window().isMinimized()
                  and (not self._loaded or self.isVisible()))
        page = self._page
        if active:
            page.setLifecycleState(page.LifecycleState.Active)
            if self._loaded:
                self._view.show()
            else:
                self._view.hide()
                if not self._load_watchdog.isActive():
                    self._load_watchdog.start()
            page.setVisible(True)
            page.runJavaScript("window.__monitor?.pause(false)")
            self._poll.start()
        else:
            # Hidden browser pages are eligible for Chromium's Frozen lifecycle;
            # it suspends script timers as well as WebWallGL's own render loop.
            page.runJavaScript("window.__monitor?.pause(true)")
            self._view.hide()
            page.setVisible(False)
            page.setLifecycleState(page.LifecycleState.Frozen)
            self._poll.stop()
            self._load_watchdog.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self._update_active()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._update_active()

    def eventFilter(self, watched, event):
        if event.type() in {QEvent.Type.WindowStateChange, QEvent.Type.Show, QEvent.Type.Hide}:
            QTimer.singleShot(0, self._update_active)
        return super().eventFilter(watched, event)

    def dispose(self):
        self._generation += 1
        self._disposed = True
        self._loaded = False
        self._poll.stop()
        self._load_watchdog.stop()
        page, view, profile = self._page, self._view, self._profile
        self._page = self._view = self._profile = self._interceptor = None
        if page:
            try:
                page.runJavaScript("window.__monitor?.dispose()")
                page.triggerAction(page.WebAction.Stop)
                page.setVisible(False)
                page.deleteLater()
            except RuntimeError:
                pass
        if view:
            self._layout.removeWidget(view)
            view.hide()
            view.deleteLater()
        if profile:
            # Qt requires pages to die before their profile.
            if page:
                page.destroyed.connect(profile.deleteLater)
            else:
                profile.deleteLater()
        if self._origin:
            self._origin.close(wait=False)
            self._origin = None
        self._cleanup_bag.clear()

    stop = dispose

    def closeEvent(self, event):
        self.dispose()
        super().closeEvent(event)
