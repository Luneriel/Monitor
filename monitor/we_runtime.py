"""Ephemeral, read-only, token-scoped offline origins for wallpaper WebEngine.

This module has no Qt dependency and never starts a server until a validated
managed project is loaded. It serves only a finite import manifest and three
trusted runtime files. No filesystem browsing, arbitrary URL proxy or writes.
"""
from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import threading
from urllib.parse import unquote, urlsplit

from . import wallpaper_engine as we

RUNTIME_DIR = Path(__file__).resolve().parent / "assets" / "we-runtime"
RUNTIME_FILES = frozenset({"index.html", "renderer.js", "webwallgl.global.min.js"})


def request_allowed(url: str, base_url: str) -> bool:
    """Shared URL fence used by both the browser and native tests."""
    try:
        candidate, base = urlsplit(url), urlsplit(base_url)
        if candidate.scheme in {"blob", "data"}:
            return True
        return (candidate.scheme == "http" and candidate.netloc == base.netloc
                and candidate.username is None and candidate.password is None
                and candidate.path.startswith(base.path)
                and candidate.path[len(base.path):].split("/", 1)[0] in {"runtime", "asset"})
    except (ValueError, TypeError):
        return False


class WallpaperOrigin:
    def __init__(self, asset_path: str | Path, kind: str):
        if kind not in {"live_scene", "live_web"}:
            raise we.WallpaperEngineError("未知实时壁纸类型。")
        project_path = Path(asset_path).resolve()
        if project_path.name != "project.json":
            raise we.WallpaperEngineError("实时壁纸必须来自已导入的项目。")
        self.root = project_path.parent
        metadata = we._json(we.safe_project_path(self.root, "import.json"))
        if metadata.get("version") != 2 or metadata.get("kind") != kind:
            raise we.WallpaperEngineError("实时壁纸的导入记录不匹配。")
        records = metadata.get("files")
        if not isinstance(records, list) or not 1 <= len(records) <= we.MAX_LIVE_FILES:
            raise we.WallpaperEngineError("实时壁纸资源清单无效。")
        self.assets = {}
        total = 0
        for record in records:
            name = we._relative_name(record["path"])
            if name in self.assets or Path(name).suffix.lower() not in we.LIVE_EXTENSIONS:
                raise we.WallpaperEngineError("实时壁纸包含未允许的资源。")
            target = we.safe_project_path(self.root, name)
            size = record.get("size")
            if not isinstance(size, int) or size < 0 or target.stat().st_size != size:
                raise we.WallpaperEngineError("实时壁纸资源大小校验失败。")
            total += size
            if total > we.MAX_LIVE_BYTES:
                raise we.WallpaperEngineError("实时壁纸资源超过上限。")
            self.assets[name] = size
        self.entry = we._relative_name(metadata["entry"])
        if "project.json" not in self.assets or self.entry not in self.assets:
            raise we.WallpaperEngineError("实时壁纸入口丢失。")
        self.kind = kind
        self.token = secrets.token_urlsafe(32)
        self._thread = None
        self._server = None
        self._closing_thread = None

    @property
    def base_url(self):
        if self._server is None:
            return ""
        return f"http://127.0.0.1:{self._server.server_port}/{self.token}/"

    def start(self):
        if self._server:
            return self.base_url
        origin = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                self._serve(False)

            def do_HEAD(self):
                self._serve(True)

            def do_OPTIONS(self):
                if origin._server is None:
                    self.send_error(410)
                    return
                expected_host = f"127.0.0.1:{self.server.server_port}"
                if (self.headers.get("Host") != expected_host or not request_allowed(
                        f"http://{expected_host}{self.path}", origin.base_url)
                        or self.headers.get("Access-Control-Request-Method", "GET") not in {"GET", "HEAD"}
                        or self.headers.get("Access-Control-Request-Headers", "").lower() not in {"", "range"}):
                    self.send_error(403)
                    return
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, HEAD")
                self.send_header("Access-Control-Allow-Headers", "Range")
                self.end_headers()

            def _serve(self, head):
                if origin._server is None:
                    self.send_error(410)
                    return
                expected_host = f"127.0.0.1:{self.server.server_port}"
                if self.headers.get("Host") != expected_host:
                    self.send_error(403)
                    return
                try:
                    raw = urlsplit(self.path).path
                    prefix = f"/{origin.token}/"
                    if not raw.startswith(prefix):
                        self.send_error(404)
                        return
                    route, name = unquote(raw[len(prefix):], errors="strict").split("/", 1)
                    name = we._relative_name(name)
                    if route == "runtime" and name in RUNTIME_FILES:
                        target = we.safe_project_path(RUNTIME_DIR, name)
                    elif route == "asset" and name in origin.assets:
                        target = we.safe_project_path(origin.root, name)
                        if target.stat().st_size != origin.assets[name]:
                            raise ValueError("asset changed")
                    else:
                        self.send_error(404)
                        return
                    size = target.stat().st_size
                    start, end, partial = 0, size - 1, False
                    range_header = self.headers.get("Range")
                    if range_header:
                        import re
                        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                        if not match or size == 0:
                            self.send_error(416)
                            return
                        a, b = match.groups()
                        if a:
                            start, end = int(a), min(int(b), size - 1) if b else size - 1
                        elif b:
                            start, end = max(0, size - int(b)), size - 1
                        else:
                            self.send_error(416)
                            return
                        if start > end or start >= size:
                            self.send_error(416)
                            return
                        partial = True
                    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                    if Path(name).suffix.lower() in {".js", ".mjs"}:
                        mime = "text/javascript"
                    self.send_response(206 if partial else 200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(max(0, end - start + 1)))
                    self.send_header("Accept-Ranges", "bytes")
                    if partial:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), display-capture=(), usb=(), serial=(), clipboard-read=(), clipboard-write=()")
                    source = origin.base_url
                    csp = (f"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' {source} blob:; "
                           f"style-src 'unsafe-inline' {source}; img-src {source} data: blob:; "
                           f"media-src {source} data: blob:; font-src {source} data:; "
                           f"connect-src {source}; worker-src blob:; frame-src {source} blob:; "
                           "object-src 'none'; base-uri 'none'; form-action 'none';")
                    if route == "asset" and mime == "text/html":
                        csp += " sandbox allow-scripts;"
                    self.send_header("Content-Security-Policy", csp)
                    self.end_headers()
                    if not head:
                        with target.open("rb") as stream:
                            stream.seek(start)
                            remaining = end - start + 1
                            while remaining > 0:
                                chunk = stream.read(min(256 * 1024, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                except (OSError, ValueError, KeyError, we.WallpaperEngineError):
                    if not self.wfile.closed:
                        self.send_error(403)

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False

        self._server = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.5}, daemon=True,
                                        name="MonitorOfflineWallpaper")
        self._thread.start()
        return self.base_url

    def close(self, *, wait=True):
        server, thread = self._server, self._thread
        # Revoke the origin immediately, then let native UI callers reap the
        # listener off-thread. The server's ordinary 0.5s idle poll avoids a
        # 20Hz wakeup merely because a hidden wallpaper still owns a source.
        self._server, self._thread = None, None
        def reap():
            if server:
                server.shutdown()
                server.server_close()
            if thread:
                thread.join(timeout=1)
        if wait:
            reap()
            if self._closing_thread:
                self._closing_thread.join(timeout=2)
        elif server:
            self._closing_thread = threading.Thread(target=reap, daemon=True,
                name="MonitorWallpaperShutdown")
            self._closing_thread.start()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.close()
