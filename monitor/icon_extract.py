"""图标提取与缓存。

需求：使用 pywin32（win32api + win32gui）从 .exe 提取图标，失败时使用默认系统图标。

优先级：
    1. user32.PrivateExtractIconsW（可指定 256 / 64 像素，画质最好）
    2. win32gui.ExtractIconEx（pywin32 经典接口，32 / 16 像素）
    3. Qt 的 QFileIconProvider（系统 shell 图标）
    4. 程序内置的默认图标（default_app.png）

提取结果缓存为 PNG（data/icons/*.png），数据库 programs.icon_path 保存该路径。
"""

from __future__ import annotations

import ctypes
import hashlib
import os
from ctypes import wintypes
from typing import Optional

from PySide6.QtCore import QFileInfo, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QGuiApplication, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import QFileIconProvider

from . import config

# --------------------------------------------------------------------------
# Win32 声明
# --------------------------------------------------------------------------
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

_DI_NORMAL = 0x0003


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _hicon_to_image(hicon: int, size: int) -> Optional[QImage]:
    """把 HICON 画进 32 位 DIB 再读回来（保留 Alpha 通道）。"""
    if not hicon:
        return None
    hdc = _user32.GetDC(0)
    memdc = _gdi32.CreateCompatibleDC(hdc)
    hbmp = None
    old = None
    try:
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = size
        bmi.bmiHeader.biHeight = -size          # 负值 => 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0         # BI_RGB
        bits = ctypes.c_void_p()
        hbmp = _gdi32.CreateDIBSection(memdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not hbmp:
            return None
        old = _gdi32.SelectObject(memdc, hbmp)
        if not _user32.DrawIconEx(memdc, 0, 0, hicon, size, size, 0, None, _DI_NORMAL):
            return None
        raw = ctypes.string_at(bits, size * size * 4)
        img = QImage(raw, size, size, QImage.Format_ARGB32).copy()
        return img if not img.isNull() else None
    except Exception:
        return None
    finally:
        try:
            if old:
                _gdi32.SelectObject(memdc, old)
            if hbmp:
                _gdi32.DeleteObject(hbmp)
            _gdi32.DeleteDC(memdc)
            _user32.ReleaseDC(0, hdc)
        except Exception:
            pass


def _private_extract(exe_path: str, size: int) -> Optional[QImage]:
    """PrivateExtractIconsW：可以指定目标尺寸（优先 256，再退 64/48/32）。"""
    try:
        fn = _user32.PrivateExtractIconsW
    except AttributeError:
        return None
    fn.restype = wintypes.UINT
    fn.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                   ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.UINT),
                   wintypes.UINT, wintypes.UINT]
    for want in (256, 128, 96, 64, 48, 32):
        hicon = wintypes.HICON()
        icon_id = wintypes.UINT()
        try:
            count = fn(exe_path, 0, want, want, ctypes.byref(hicon), ctypes.byref(icon_id), 1, 0)
        except Exception:
            return None
        if count and hicon.value:
            try:
                img = _hicon_to_image(int(hicon.value), want)
            finally:
                _user32.DestroyIcon(hicon)
            if img is not None and not img.isNull():
                return img
        if count == 0:
            break
    return None


def _extract_icon_ex(exe_path: str) -> Optional[QImage]:
    """pywin32 的 win32gui.ExtractIconEx（大图标优先）。"""
    try:
        import win32gui
    except Exception:
        return None
    handles: list[int] = []
    try:
        large, small = win32gui.ExtractIconEx(exe_path, 0)
        handles = list(large or []) + list(small or [])
        for hicon in handles:
            img = _hicon_to_image(int(hicon), 32)
            if img is not None and not img.isNull():
                return img
    except Exception:
        return None
    finally:
        for hicon in handles:
            try:
                win32gui.DestroyIcon(hicon)
            except Exception:
                pass
    return None


def _shell_icon(exe_path: str, size: int = 64) -> Optional[QImage]:
    """系统 shell 图标（Qt 的 QFileIconProvider，本身就来自 Win32）。"""
    try:
        provider = QFileIconProvider()
        icon = provider.icon(QFileInfo(exe_path))
        if icon.isNull():
            return None
        pm = icon.pixmap(size, size)
        if pm.isNull():
            return None
        return pm.toImage()
    except Exception:
        return None


def _save_image(img: QImage, key: str, size: int = 64) -> Optional[str]:
    try:
        os.makedirs(config.ICON_DIR, exist_ok=True)
        if img.width() != size or img.height() != size:
            img = img.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        pm = QPixmap.fromImage(img)
        digest = hashlib.sha1(key.encode("utf-8", "surrogatepass")).hexdigest()[:20]
        out = os.path.join(config.ICON_DIR, f"{digest}.png")
        return out if pm.save(out, "PNG") else None
    except Exception:
        return None


_icon_cache: dict[str, Optional[str]] = {}


def extract_icon(exe_path: str, size: int = 64, use_cache: bool = True) -> Optional[str]:
    """从可执行文件提取图标并落盘为 PNG，返回 PNG 路径；失败返回 None。"""
    if not exe_path:
        return None
    exe_path = os.path.abspath(exe_path)
    if use_cache and exe_path in _icon_cache:
        cached = _icon_cache[exe_path]
        if cached and os.path.exists(cached):
            return cached

    key = f"{exe_path.lower()}|{size}"
    digest = hashlib.sha1(key.encode("utf-8", "surrogatepass")).hexdigest()[:20]
    target = os.path.join(config.ICON_DIR, f"{digest}.png")
    if use_cache and os.path.exists(target) and os.path.getsize(target) > 0:
        _icon_cache[exe_path] = target
        return target

    img: Optional[QImage] = None
    if os.path.exists(exe_path):
        img = _private_extract(exe_path, size)
        if img is None:
            img = _extract_icon_ex(exe_path)

    result = _save_image(img, key, size) if img is not None else None
    if result is None:
        shell = _shell_icon(exe_path, size)
        result = _save_image(shell, key + "|shell", size) if shell is not None else None

    _icon_cache[exe_path] = result
    return result


def file_display_name(exe_path: str) -> str:
    """用 win32api 读取文件版本信息里的描述作为默认程序名，取不到则用文件名。"""
    stem = os.path.splitext(os.path.basename(exe_path))[0]
    try:
        import win32api

        info = win32api.GetFileVersionInfo(exe_path, "\\VarFileInfo\\Translation")
        if info:
            lang, codepage = info[0]
            for key in ("FileDescription", "ProductName", "InternalName"):
                try:
                    value = win32api.GetFileVersionInfo(
                        exe_path, f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{key}"
                    )
                except Exception:
                    value = None
                if value and str(value).strip():
                    return str(value).strip()
    except Exception:
        pass
    return stem


# --------------------------------------------------------------------------
# 内置默认图标
# --------------------------------------------------------------------------
def _draw_default_icon(size: int = 128, rounded: bool = True) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.TextAntialiasing, True)

    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor("#6D5CE7"))
    grad.setColorAt(1.0, QColor(config.C_PRIMARY))
    path = QPainterPath()
    if rounded:
        path.addRoundedRect(QRectF(2, 2, size - 4, size - 4), size * 0.26, size * 0.26)
    else:
        path.addEllipse(QRectF(2, 2, size - 4, size - 4))
    painter.fillPath(path, QBrush(grad))

    # 屏幕轮廓
    painter.setPen(QPen(QColor(255, 255, 255, 235), max(2.0, size * 0.055)))
    screen = QRectF(size * 0.26, size * 0.30, size * 0.48, size * 0.32)
    painter.drawRoundedRect(screen, size * 0.05, size * 0.05)
    painter.drawLine(int(size * 0.50), int(size * 0.62), int(size * 0.50), int(size * 0.72))
    painter.drawLine(int(size * 0.36), int(size * 0.73), int(size * 0.64), int(size * 0.73))
    painter.end()
    return pm


def default_app_pixmap(size: int = 64) -> QPixmap:
    return _draw_default_icon(max(size * 2, 128)).scaled(
        size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation
    )


def default_app_icon_path() -> Optional[str]:
    """生成/返回默认程序图标 PNG 路径（用于"提取失败时使用默认系统图标"）。"""
    target = os.path.join(config.ICON_DIR, "default_app.png")
    if not os.path.exists(target):
        try:
            os.makedirs(config.ICON_DIR, exist_ok=True)
            if not _draw_default_icon(128).save(target, "PNG"):
                return None
        except Exception:
            return None
    return target


def _dpr() -> float:
    try:
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            return max(1.0, float(screen.devicePixelRatio()))
    except Exception:
        pass
    return 1.0


def _scaled_pixmap(source: QPixmap, size: int) -> QPixmap:
    """按逻辑尺寸 size 输出（内部按设备像素比放大，保证高分屏清晰）。"""
    dpr = _dpr()
    target = max(1, int(round(size * dpr)))
    pm = source.scaled(target, target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    pm.setDevicePixelRatio(dpr)
    return pm


def pixmap_for_program(icon_path: Optional[str], exe_path: str = "",
                       size: int = 48, fallback_text: str = "") -> QPixmap:
    """统一的图标取用入口：先读缓存 PNG，再尝试实时提取，最后用默认图标。"""
    if icon_path and os.path.exists(icon_path):
        pm = QPixmap(icon_path)
        if not pm.isNull():
            return _scaled_pixmap(pm, size)
    if exe_path:
        path = extract_icon(exe_path, size=max(size * 2, 64))
        if path and os.path.exists(path):
            pm = QPixmap(path)
            if not pm.isNull():
                return _scaled_pixmap(pm, size)
    dpr = _dpr()
    return _scaled_pixmap(default_app_pixmap(max(128, int(size * dpr * 2))), size)


def default_tray_pixmap(size: int = 64) -> QPixmap:
    """默认托盘图标：不透明底色的圆形监控标志（托盘不接受全透明小图）。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setBrush(QBrush(QColor(config.C_PRIMARY)))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(QRectF(0, 0, size, size))
    painter.setPen(QPen(QColor("#FFFFFF"), max(2.0, size * 0.09), Qt.SolidLine, Qt.RoundCap))
    rect = QRectF(size * 0.24, size * 0.30, size * 0.52, size * 0.34)
    painter.drawRoundedRect(rect, size * 0.06, size * 0.06)
    painter.drawLine(int(size * 0.50), int(size * 0.64), int(size * 0.50), int(size * 0.74))
    painter.drawLine(int(size * 0.34), int(size * 0.75), int(size * 0.66), int(size * 0.75))
    painter.end()
    return pm


def save_default_tray_icon() -> Optional[str]:
    target = os.path.join(config.TRAY_DIR, "default_tray.png")
    if not os.path.exists(target):
        try:
            os.makedirs(config.TRAY_DIR, exist_ok=True)
            if not default_tray_pixmap(128).save(target, "PNG"):
                return None
        except Exception:
            return None
    return target
