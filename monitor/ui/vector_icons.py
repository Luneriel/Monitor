"""矢量图标：用 QPainter 直接绘制，无需外部资源文件，任意尺寸都清晰。"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

from .. import config

_GRID = 24.0


def _pen(painter: QPainter, color: str, stroke: float) -> None:
    pen = QPen(QColor(color), stroke, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)


def _plus(p: QPainter) -> None:
    p.drawLine(QPointF(12, 5), QPointF(12, 19))
    p.drawLine(QPointF(5, 12), QPointF(19, 12))


def _close(p: QPainter) -> None:
    p.drawLine(QPointF(6, 6), QPointF(18, 18))
    p.drawLine(QPointF(18, 6), QPointF(6, 18))


def _check(p: QPainter) -> None:
    p.drawPolyline([QPointF(5, 12.5), QPointF(10, 17.5), QPointF(19, 7)])


def _search(p: QPainter) -> None:
    p.drawEllipse(QRectF(4.5, 4.5, 12, 12))
    p.drawLine(QPointF(15.5, 15.5), QPointF(20, 20))


def _trash(p: QPainter) -> None:
    p.drawLine(QPointF(4, 7), QPointF(20, 7))
    p.drawPolyline([QPointF(9.5, 7), QPointF(9.5, 4.5), QPointF(14.5, 4.5), QPointF(14.5, 7)])
    p.drawPolyline([QPointF(6.5, 7), QPointF(7.5, 20), QPointF(16.5, 20), QPointF(17.5, 7)])
    p.drawLine(QPointF(10.5, 10.5), QPointF(10.8, 16.5))
    p.drawLine(QPointF(13.5, 10.5), QPointF(13.2, 16.5))


def _sort(p: QPainter) -> None:
    p.drawLine(QPointF(8, 19), QPointF(8, 5.5))
    p.drawPolyline([QPointF(4.8, 8.7), QPointF(8, 5.5), QPointF(11.2, 8.7)])
    p.drawLine(QPointF(16, 5), QPointF(16, 18.5))
    p.drawPolyline([QPointF(12.8, 15.3), QPointF(16, 18.5), QPointF(19.2, 15.3)])


def _menu(p: QPainter) -> None:
    for y in (7, 12, 17):
        p.drawLine(QPointF(4, y), QPointF(20, y))


def _chevron_down(p: QPainter) -> None:
    p.drawPolyline([QPointF(6, 9.5), QPointF(12, 15.5), QPointF(18, 9.5)])


def _chevron_up(p: QPainter) -> None:
    p.drawPolyline([QPointF(6, 14.5), QPointF(12, 8.5), QPointF(18, 14.5)])


def _chevron_left(p: QPainter) -> None:
    p.drawPolyline([QPointF(15, 6), QPointF(9, 12), QPointF(15, 18)])


def _chevron_right(p: QPainter) -> None:
    p.drawPolyline([QPointF(9, 6), QPointF(15, 12), QPointF(9, 18)])


def _image(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3.5, 5, 17, 14), 2, 2)
    p.drawEllipse(QRectF(7.5, 8.5, 3, 3))
    p.drawPolyline([QPointF(5.5, 17.5), QPointF(10.5, 12.5), QPointF(13.5, 15.5),
                    QPointF(16, 13), QPointF(18.5, 15.5)])


def _clock(p: QPainter) -> None:
    p.drawEllipse(QRectF(3.5, 3.5, 17, 17))
    p.drawPolyline([QPointF(12, 7), QPointF(12, 12.5), QPointF(16.5, 14.5)])


def _list(p: QPainter) -> None:
    for y in (7, 12, 17):
        p.drawLine(QPointF(9, y), QPointF(20, y))
    for y in (7, 12, 17):
        p.drawPoint(QPointF(5, y))


def _monitor(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(2.5, 4.5, 19, 12.5), 2, 2)
    p.drawLine(QPointF(12, 17), QPointF(12, 20))
    p.drawLine(QPointF(8, 20), QPointF(16, 20))


def _warning(p: QPainter) -> None:
    p.drawPolyline([QPointF(12, 3.5), QPointF(21, 20), QPointF(3, 20), QPointF(12, 3.5)])
    p.drawLine(QPointF(12, 9.5), QPointF(12, 14.5))
    p.drawPoint(QPointF(12, 17.3))


def _home(p: QPainter) -> None:
    p.drawPolyline([QPointF(3.6, 11.2), QPointF(12, 4.2), QPointF(20.4, 11.2)])
    p.drawPolyline([QPointF(6, 10.2), QPointF(6, 19.8), QPointF(18, 19.8), QPointF(18, 10.2)])


def _bell(p: QPainter) -> None:
    p.drawPolyline([QPointF(6, 17), QPointF(6, 10.5),
                    QPointF(12, 4.5), QPointF(18, 10.5), QPointF(18, 17)])
    p.drawLine(QPointF(4.5, 17), QPointF(19.5, 17))
    p.drawPolyline([QPointF(10, 19.5), QPointF(12, 20.6), QPointF(14, 19.5)])


def _gear(p: QPainter) -> None:
    """设置：滑杆样式（小尺寸下比齿轮更清晰）。"""
    color = p.pen().color()
    knobs = ((7.5, 15.0), (12.0, 9.2), (16.5, 14.2))
    for y, _x in knobs:
        p.drawLine(QPointF(4, y), QPointF(20, y))
    p.setBrush(color)
    for y, x in knobs:
        p.drawEllipse(QRectF(x - 2.1, y - 2.1, 4.2, 4.2))
    p.setBrush(Qt.NoBrush)


def _play(p: QPainter) -> None:
    p.drawPolyline([QPointF(8, 5.5), QPointF(18, 12), QPointF(8, 18.5), QPointF(8, 5.5)])


def _info(p: QPainter) -> None:
    p.drawEllipse(QRectF(3.6, 3.6, 16.8, 16.8))
    p.drawLine(QPointF(12, 11), QPointF(12, 16.5))
    p.drawPoint(QPointF(12, 8))


def _refresh(p: QPainter) -> None:
    p.drawArc(QRectF(4, 4, 16, 16), 40 * 16, 280 * 16)
    p.drawPolyline([QPointF(17.6, 3.2), QPointF(18.9, 7.6), QPointF(14.6, 8.4)])


def _folder(p: QPainter) -> None:
    p.drawPolyline([QPointF(3.5, 18.5), QPointF(3.5, 6), QPointF(9.5, 6),
                    QPointF(11.5, 8.5), QPointF(20.5, 8.5), QPointF(20.5, 18.5),
                    QPointF(3.5, 18.5)])


def _power(p: QPainter) -> None:
    p.drawArc(QRectF(4.2, 5.5, 15.6, 15.6), -60 * 16, 300 * 16)
    p.drawLine(QPointF(12, 3.2), QPointF(12, 11.5))


def _minimize(p: QPainter) -> None:
    p.drawLine(QPointF(5, 12), QPointF(19, 12))


def _maximize(p: QPainter) -> None:
    p.drawRect(QRectF(5.5, 5.5, 13, 13))


def _restore(p: QPainter) -> None:
    p.drawRect(QRectF(4.5, 8, 11, 11))
    p.drawPolyline([QPointF(8, 8), QPointF(8, 4.5), QPointF(19.5, 4.5), QPointF(19.5, 15.5),
                    QPointF(16, 15.5)])


def _globe(p: QPainter) -> None:
    p.drawEllipse(QRectF(3.5, 3.5, 17, 17))
    p.drawLine(QPointF(3.5, 12), QPointF(20.5, 12))
    p.drawEllipse(QRectF(8.2, 3.5, 7.6, 17))


def _shield(p: QPainter) -> None:
    """盾牌：控制页图标。"""
    p.drawPolyline([QPointF(12, 3.0), QPointF(19.5, 6.0), QPointF(19.5, 12.0),
                    QPointF(12, 21.0), QPointF(4.5, 12.0), QPointF(4.5, 6.0),
                    QPointF(12, 3.0)])
    p.drawPolyline([QPointF(8.6, 11.8), QPointF(11.2, 14.4), QPointF(15.6, 9.4)])


def _minus(p: QPainter) -> None:
    p.drawLine(QPointF(5.0, 12.0), QPointF(19.0, 12.0))


def _grip(p: QPainter) -> None:
    """拖动手柄：两列圆点（RoundCap 的粗点）。"""
    for x in (9.5, 14.5):
        for y in (7.0, 12.0, 17.0):
            p.drawPoint(QPointF(x, y))


def _lock(p: QPainter) -> None:
    # 锁梁（上半圆 + 两侧短竖线）+ 锁体 + 锁孔
    p.drawArc(QRectF(8.5, 3.5, 7, 7), 180 * 16, -180 * 16)
    p.drawPolyline([QPointF(8.5, 7.5), QPointF(8.5, 10.5)])
    p.drawPolyline([QPointF(15.5, 7.5), QPointF(15.5, 10.5)])
    p.drawRoundedRect(QRectF(5, 10.5, 14, 9.5), 2, 2)
    p.drawPoint(QPointF(12, 15.2))


_DRAWERS = {
    "plus": _plus,
    "minus": _minus,
    "grip": _grip,
    "close": _close,
    "check": _check,
    "search": _search,
    "trash": _trash,
    "sort": _sort,
    "menu": _menu,
    "chevron_down": _chevron_down,
    "chevron_up": _chevron_up,
    "chevron_left": _chevron_left,
    "chevron_right": _chevron_right,
    "image": _image,
    "clock": _clock,
    "list": _list,
    "monitor": _monitor,
    "warning": _warning,
    "home": _home,
    "bell": _bell,
    "gear": _gear,
    "play": _play,
    "info": _info,
    "refresh": _refresh,
    "folder": _folder,
    "power": _power,
    "minimize": _minimize,
    "maximize": _maximize,
    "restore": _restore,
    "globe": _globe,
    "shield": _shield,
    "lock": _lock,
}


def paint_icon(painter: QPainter, name: str, x: float, y: float, size: int,
               color: str = "", stroke: float = 1.9) -> None:
    color = color or config.C_TEXT
    drawer = _DRAWERS.get(name)
    if drawer is None:
        return
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.translate(x, y)
    scale = size / _GRID
    painter.scale(scale, scale)
    # 换算回 24x24 坐标系，让最终线宽稳定在 stroke 像素左右
    width = max(1.4, min(3.2, stroke * _GRID / max(1.0, size)))
    _pen(painter, color, width)
    drawer(painter)
    painter.restore()


def _device_pixel_ratio() -> float:
    """当前屏幕的设备像素比：按物理像素绘制图标，高分屏下才清晰。"""
    try:
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        screen = app.primaryScreen() if app is not None else None
        if screen is not None:
            return max(1.0, float(screen.devicePixelRatio()))
    except Exception:
        pass
    return 1.0


@lru_cache(maxsize=512)
def _pixmap_impl(name: str, size: int, color: str, stroke: float, dpr: float) -> QPixmap:
    physical = max(1, int(round(size * dpr)))
    pm = QPixmap(physical, physical)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    paint_icon(painter, name, 0, 0, size, color, stroke)
    painter.end()
    return pm


def pixmap(name: str, size: int = 18, color: str = "", stroke: float = 1.9) -> QPixmap:
    color = color or config.C_TEXT
    return _pixmap_impl(name, int(size), color, float(stroke), _device_pixel_ratio())


@lru_cache(maxsize=512)
def _icon_impl(name: str, size: int, color: str, stroke: float, dpr: float) -> QIcon:
    return QIcon(_pixmap_impl(name, size, color, stroke, dpr))


def icon(name: str, size: int = 18, color: str = "", stroke: float = 1.9) -> QIcon:
    color = color or config.C_TEXT
    return _icon_impl(name, int(size), color, float(stroke), _device_pixel_ratio())


def circle_pixmap(size: int, color: str, filled: bool = True, inner: str = "#FFFFFF") -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color) if filled else Qt.NoBrush)
    painter.drawEllipse(QRectF(0.5, 0.5, size - 1, size - 1))
    painter.end()
    return pm
