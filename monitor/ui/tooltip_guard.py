"""提示气泡（tooltip）抑制器。

场景：鼠标停在按钮/输入框上时，列表或卡片被重建（刷新、保存规则、添加程序…），
Qt 会为"光标下的新控件"重新弹出提示气泡；如果紧接着又因为布局变化收起，
用户看到的就是"弹窗一闪而过"。

做法：重建前调用 ``suppress_tooltips()``，短暂屏蔽应用级 ToolTip 事件并隐藏当前气泡。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QApplication, QToolTip

_DEFAULT_MS = 700
_active: list = []


class _TooltipGuard(QObject):
    def __init__(self, app: QApplication, duration_ms: int) -> None:
        super().__init__(app)
        self.app = app
        self._duration = max(120, int(duration_ms))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._release)
        self._timer.start(self._duration)
        app.installEventFilter(self)

    def extend(self, duration_ms: int) -> None:
        """已经在屏蔽中就顺延计时，不重复安装事件过滤器。"""
        self._duration = max(self._duration, 120, int(duration_ms))
        self._timer.start(self._duration)

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() == QEvent.ToolTip:
            return True                     # 吞掉本次气泡
        return False

    def _release(self) -> None:
        try:
            self.app.removeEventFilter(self)
        except (RuntimeError, TypeError):
            pass
        if self in _active:
            _active.remove(self)
        self.deleteLater()


def suppress_tooltips(duration_ms: int = _DEFAULT_MS) -> None:
    """短暂屏蔽提示气泡（并立刻隐藏正在显示的那个）。

    复用同一个守卫：刷新很频繁（滚动、搜索、列表重建），每次都新建一个
    ``_TooltipGuard`` 就是每次往应用上多挂一个事件过滤器，短时间内叠十几个，
    每个事件都要过一遍它们。同一时间只需要一个。
    """
    app = QApplication.instance()
    if app is None:
        return
    try:
        QToolTip.hideText()
    except Exception:
        pass
    for guard in list(_active):
        try:
            guard.extend(duration_ms)
            return
        except RuntimeError:      # C++ 侧已析构：清掉，建新的
            _active.remove(guard)
    guard = _TooltipGuard(app, duration_ms)
    _active.append(guard)
