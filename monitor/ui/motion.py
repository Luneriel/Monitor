"""Shared motion policy for native Qt surfaces.

Short, interruptible transitions; no child staggering or text scaling.
"""
from __future__ import annotations

import os
from functools import lru_cache

from PySide6.QtCore import QEvent, QObject, QEasingCurve, QPointF
from PySide6.QtWidgets import QApplication

POPOVER_MS = 180
COLLAPSE_MS = 200
FEEDBACK_MS = 130


class _InputModality(QObject):
    keyboard = False

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() == QEvent.ApplicationActivate:
            _system_reduced_motion.cache_clear()
        if event.type() == QEvent.KeyPress:
            self.keyboard = True
        elif event.type() in (QEvent.MouseButtonPress, QEvent.TouchBegin):
            self.keyboard = False
        return False


def track_input() -> None:
    app = QApplication.instance()
    if app is not None and not hasattr(app, '_monitor_input_modality'):
        observer = _InputModality(app)
        app._monitor_input_modality = observer
        app.installEventFilter(observer)


@lru_cache(maxsize=1)
def _system_reduced_motion() -> bool:
    if os.name != 'nt':
        return False
    try:
        import ctypes
        enabled = ctypes.c_int(1)
        ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0)
        return not bool(enabled.value)
    except (AttributeError, OSError):
        return False


def reduced_motion() -> bool:
    app = QApplication.instance()
    override = app.property('reduceMotion') if app is not None else None
    if override is not None:
        return bool(override)
    env = os.environ.get('MONITOR_REDUCED_MOTION')
    if env is not None:
        return env.strip().lower() in ('1', 'true', 'yes', 'on')
    return _system_reduced_motion()


def should_animate(widget=None) -> bool:
    track_input()
    app = QApplication.instance()
    observer = getattr(app, '_monitor_input_modality', None)
    return not reduced_motion() and not (observer is not None and observer.keyboard)


def easing() -> QEasingCurve:
    """Emil's strong ease-out: cubic-bezier(.23, 1, .32, 1)."""
    curve = QEasingCurve(QEasingCurve.BezierSpline)
    curve.addCubicBezierSegment(QPointF(.23, 1), QPointF(.32, 1), QPointF(1, 1))
    return curve
