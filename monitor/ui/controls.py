"""通用控件：开关滑块、圆形复选框、按钮、搜索框、卡片、空状态等。

样式统一来自 ``theme.build_stylesheet()`` 的全局 QSS；这里只负责结构、
objectName / 动态属性与自定义绘制（开关、圆形复选框、状态点）。
"""

from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import (Property, QAbstractAnimation, QEasingCurve, QPropertyAnimation, QRectF,
                            QSize, Qt, QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QCursor, QFontMetrics, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QStyle, QStyleOptionButton,
                               QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .. import config
from ..i18n import tr
from . import motion, theme, vector_icons

# --------------------------------------------------------------------------
# 基础容器
# --------------------------------------------------------------------------


class Card(QFrame):
    """白色圆角卡片：细边框 + 自绘柔和阴影，悬停时阴影加深、边框转为靛蓝。

    阴影不用 QGraphicsDropShadowEffect —— 该特效在内容会变化的控件上有缓存不失效的
    问题，会在鼠标悬浮时表现为文字闪烁/跳动；这里改为在 paintEvent 中自绘。
    """

    def __init__(self, parent: Optional[QWidget] = None, radius: int = config.R_CARD,
                 hover: bool = True, elevated: bool = True) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._radius = config.sx(radius)
        self._hover = hover
        self._elevated = elevated
        self._hover_progress = 0.0
        self._lift = 0.0
        self._selected = False
        self.shadow_pad = config.sx(9) if elevated else 0
        self._hover_anim: Optional[QVariantAnimation] = None
        if hover:
            self.setAttribute(Qt.WA_Hover, True)

    # ------------------------------------------------------------------
    # 自绘：阴影 + 圆角背景 + 边框
    # ------------------------------------------------------------------
    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self.update()

    def is_selected(self) -> bool:
        return self._selected

    def set_lift(self, value: float) -> None:
        """拖动排序时"被拿起"的程度（0 = 贴在列表里，1 = 完全拿起）。

        只影响自绘的阴影强度与底色/边框，用来表现"卡片被轻微抬离列表"。
        """
        value = max(0.0, min(1.0, float(value)))
        if abs(value - self._lift) < 0.01:
            return
        self._lift = value
        self.update()

    def _strength(self) -> float:
        """阴影/边框的强调程度：悬停或"被拿起"取较大者。"""
        return max(self._hover_progress, self._lift)

    def _border_color(self) -> str:
        if self._selected:
            return config.C_SELECTED_BORDER
        if self._strength() > 0.02:
            return config.C_PRIMARY_BORDER
        return config.C_BORDER

    def _background_color(self) -> QColor:
        if self._selected:
            return QColor(config.C_PRIMARY_SOFT)
        t = self._strength()
        if t <= 0.02:
            return QColor(config.C_SURFACE)
        idle = QColor(config.C_SURFACE)
        hovered = QColor(config.C_SURFACE_HOVER)
        return QColor(
            int(idle.red() + (hovered.red() - idle.red()) * t),
            int(idle.green() + (hovered.green() - idle.green()) * t),
            int(idle.blue() + (hovered.blue() - idle.blue()) * t),
            int(idle.alpha() + (hovered.alpha() - idle.alpha()) * t),
        )

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pad = self.shadow_pad
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5 - pad)
        if self._elevated:
            strength = self._strength()
            theme.paint_soft_shadow(
                painter, rect, self._radius,
                spread=int(pad * (0.5 + 0.55 * strength)) or 1,
                dy=config.sx(2) + config.sx(4) * strength,
                alpha=int(config.SHADOW_IDLE_ALPHA + 26 * strength),
            )
        surface = self._background_color()
        border = QColor(self._border_color())
        if config.current_theme() == config.THEME_GLASS:
            from . import glass_effects
            surface.setAlphaF(glass_effects.opacity('card'))
            if not self._selected:
                border = QColor(glass_effects.border_color())
        painter.setBrush(surface)
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(rect, self._radius, self._radius)
        painter.end()

    # ------------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        if self._hover:
            self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover:
            self._animate_hover(0.0)
        super().leaveEvent(event)

    def _animate_hover(self, target: float) -> None:
        if self._hover_anim is None:
            self._hover_anim = QVariantAnimation(self)
            self._hover_anim.setDuration(config.ANIM_NORMAL)
            self._hover_anim.setEasingCurve(QEasingCurve.OutCubic)
            self._hover_anim.valueChanged.connect(self._on_hover_step)
        anim = self._hover_anim
        anim.stop()
        anim.setStartValue(self._hover_progress)
        anim.setEndValue(float(target))
        anim.start()

    def _on_hover_step(self, value) -> None:
        self._hover_progress = float(value)
        self.update()


class Panel(QFrame):
    """白色分组面板（设置项、日志容器等）：只描边，不加投影。"""

    def __init__(self, parent: Optional[QWidget] = None, elevated: bool = True,
                 radius: Optional[int] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("Panel")
        self._radius = radius
        if radius is not None:
            self.setStyleSheet(f"#Panel {{ border-radius: {config.sx(int(radius))}px; }}")


class CheckBox(QCheckBox):
    """勾选后自己在方框里画一个 √（QSS 无法绘制对勾）；勾选/取消带缩放淡入动画。"""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self._progress = 1.0 if self.isChecked() else 0.0
        self._check_anim: Optional[QPropertyAnimation] = None

    # -- 动画属性（0 = 完全收起，1 = 完全显示） --------------------------
    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, value: float) -> None:
        self._progress = max(0.0, min(1.0, float(value)))
        self.update()

    checkProgress = Property(float, _get_progress, _set_progress)

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        if self._check_anim is not None:
            self._check_anim.stop()
            self._check_anim.deleteLater()
            self._check_anim = None
        super().setChecked(checked)
        # 代码设置（初始化 / 重建）直接到位，不播动画
        self._set_progress(1.0 if checked else 0.0)

    def nextCheckState(self) -> None:  # noqa: N802
        # 用户点击 / 空格：从当前进度动画到目标进度
        previous = self._progress
        super().nextCheckState()
        self._animate_check(previous, 1.0 if self.isChecked() else 0.0)

    def _animate_check(self, start: float, end: float) -> None:
        if self._check_anim is not None:
            self._check_anim.stop()
            self._check_anim.deleteLater()
            self._check_anim = None
        if abs(start - end) < 0.01:
            self._set_progress(end)
            return
        self._set_progress(start)
        animation = QPropertyAnimation(self, b"checkProgress", self)
        animation.setDuration(config.ANIM_FAST)
        animation.setStartValue(float(start))
        animation.setEndValue(float(end))
        animation.setEasingCurve(QEasingCurve.OutCubic)

        def _finish() -> None:
            self._progress = float(end)
            self._check_anim = None
            self.update()

        animation.finished.connect(_finish)
        self._check_anim = animation
        animation.start(QAbstractAnimation.DeleteWhenStopped)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        progress = self._progress
        if progress <= 0.01:
            return
        from PySide6.QtCore import QPointF, QRect
        from PySide6.QtGui import QColor, QPainter, QPen

        option = QStyleOptionButton()
        self.initStyleOption(option)
        rect = self.style().subElementRect(QStyle.SE_CheckBoxIndicator, option, self)
        if rect.width() <= 0:
            rect = QRect(config.sx(4), (self.height() - config.sx(15)) // 2,
                         config.sx(15), config.sx(15))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setOpacity(min(1.0, progress))
        pen = QPen(QColor("#FFFFFF"))
        # 动画期间按进度稍微缩小，形成一个"弹一下"的效果
        scale = 0.7 + 0.3 * min(1.0, progress)
        pen.setWidthF(max(1.6, rect.width() * 0.14) * max(0.75, scale))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        size = rect.width() * scale
        left = rect.left() + (rect.width() - size) / 2
        top = rect.top() + (rect.height() - size) / 2
        painter.drawPolyline([QPointF(left + size * 0.22, top + size * 0.52),
                              QPointF(left + size * 0.42, top + size * 0.72),
                              QPointF(left + size * 0.78, top + size * 0.30)])
        painter.end()


class SlimScrollArea(QScrollArea):
    """无边框、透明背景、细滚动条的滚动区域。

    滚轮滚动带补间动画：一次滚轮事件的目标位移在 120ms 内按时间插值完成，
    不再一格格跳；动画永远从**当前实际位置**起算，连续滚动不会重置或排队。
    """

    WHEEL_MS = 120

    def __init__(self, parent: Optional[QWidget] = None, horizontal: bool = False) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.viewport().setAutoFillBackground(False)
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if horizontal else Qt.ScrollBarAlwaysOff
        )
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._wheel_anim: Optional[QVariantAnimation] = None
        # 滚动条出现/消失时保持内容宽度不变：内容宽度永远按"滚动条可见"计算，
        # 滚动条消失后右侧留出等宽空白顶替。
        self.verticalScrollBar().rangeChanged.connect(lambda *_: self._sync_bar_margin())
        self._sync_bar_margin()


    # ------------------------------------------------------------------
    def _bar_width(self) -> int:
        bar = self.verticalScrollBar()
        width = bar.sizeHint().width()
        return max(width, config.sx(8))

    def _sync_bar_margin(self) -> None:
        """内容宽度**始终按滚动条可见**计算，出现 / 消失都不改变内容宽度。

        以前靠 ``setViewportMargins`` 留白，但滚动条隐藏时 Qt 并不会把这块留白
        算进视口宽度（实测视口会长回满宽），于是收起卡片的动画一跨过"是否需要
        滚动条"的阈值，内容宽度就变 9px -> 文字重新换行 -> 再抖一次。
        现在直接把滚动区域控件固定成"减掉滚动条宽度"的宽度，滚动条隐藏时右边
        空出 9px，内容宽度全程不变。
        """
        try:
            if self.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff:
                return
            widget = self.widget()
            if widget is None:
                return
            width = max(0, self.width() - self._bar_width())
            if widget.minimumWidth() != width or widget.maximumWidth() != width:
                widget.setFixedWidth(width)
        except RuntimeError:
            return

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_bar_margin()

    # ------------------------------------------------------------------
    def _animate_scroll_to(self, target: int) -> None:
        bar = self.verticalScrollBar()
        if self._wheel_anim is not None:
            try:
                self._wheel_anim.stop()
            except RuntimeError:
                pass
            self._wheel_anim = None
        animation = QVariantAnimation(self)
        animation.setDuration(self.WHEEL_MS)
        animation.setStartValue(int(bar.value()))       # 从当前实际位置起算
        animation.setEndValue(int(target))
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.valueChanged.connect(lambda value: bar.setValue(int(value)))
        animation.finished.connect(lambda: setattr(self, "_wheel_anim", None))
        self._wheel_anim = animation
        animation.start(QAbstractAnimation.DeleteWhenStopped)

    def wheelEvent(self, event) -> None:  # noqa: N802
        bar = self.verticalScrollBar()
        delta = event.angleDelta().y()
        if delta == 0 or bar.maximum() <= 0:
            super().wheelEvent(event)
            return
        step = max(config.sx(40), bar.singleStep() * 3)
        current = bar.value()
        target = int(max(0, min(bar.maximum(), current - delta / 120.0 * step)))
        self._animate_scroll_to(target)
        event.accept()


class Divider(QFrame):
    """1px 分隔线。"""

    def __init__(self, parent: Optional[QWidget] = None, horizontal: bool = True) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("Divider")
        if horizontal:
            self.setFixedHeight(1)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        else:
            self.setFixedWidth(1)
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)


# 兼容旧名字
Separator = Divider


class IconTile(QLabel):
    """圆角浅灰底图标容器。"""

    def __init__(self, size: int = 46, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("IconTile")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(config.sx(size), config.sx(size))
        self.setAlignment(Qt.AlignCenter)


# --------------------------------------------------------------------------
# 开关 / 复选框
# --------------------------------------------------------------------------


class ToggleSwitch(QWidget):
    """平滑过渡的开关滑块（果冻手感：滑动时横向拉长，到终点回弹，底色灰 -> 紫）。"""

    toggled = Signal(bool)

    JELLY_MS = 350              # 果冻动画时长
    STRETCH = 0.38              # 滑块最大横向拉伸比例

    def __init__(self, checked: bool = False, parent: Optional[QWidget] = None,
                 width: int = 42, height: int = 23) -> None:
        super().__init__(parent)
        self._w = config.sx(width)
        self._h = config.sx(height)
        self.setFixedSize(self._w, self._h)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self._checked = bool(checked)
        self._offset = 1.0 if checked else 0.0
        self._color_t = 1.0 if checked else 0.0     # 底色进度：0 = 灰，1 = 紫
        self._stretch = 0.0                         # 滑块横向拉伸量
        self._enabled_look = True
        self._anim: Optional[QVariantAnimation] = None

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = max(0.0, min(1.0, float(value)))
        self.update()

    offset = Property(float, _get_offset, _set_offset)

    def isChecked(self) -> bool:  # noqa: N802
        return self._checked

    def setChecked(self, checked: bool, animate: bool = True, emit: bool = False) -> None:  # noqa: N802
        checked = bool(checked)
        changed = checked != self._checked
        self._checked = checked
        target = 1.0 if checked else 0.0
        if animate and not changed:
            # 状态没变就不重播动画：跨开关联动时把"本来就已经打开"的开关再抖一遍，
            # 看起来像被重新点了一次（用户 ①）。正在跑的动画让它自己跑完。
            return
        if self._anim is not None:
            self._anim.stop()
            self._anim.deleteLater()
            self._anim = None
        if animate:
            start_offset = self._offset
            start_color = self._color_t
            anim = QVariantAnimation(self)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setDuration(self.JELLY_MS)
            anim.setEasingCurve(QEasingCurve.Linear)     # 进度本身线性，弹簧在下面自己算

            def _apply(value, start_offset=start_offset, start_color=start_color,
                       target=target) -> None:
                t = max(0.0, min(1.0, float(value)))
                self._offset = start_offset + (target - start_offset) * self._spring(t)
                self._color_t = start_color + (target - start_color) * t
                self._stretch = self._jelly(t)
                self.update()

            anim.valueChanged.connect(_apply)
            anim.finished.connect(self._settle)
            self._anim = anim
            anim.start(QAbstractAnimation.DeleteWhenStopped)
        else:
            self._set_offset(target)
            self._color_t = target
            self._stretch = 0.0
            self.update()
        if emit and changed:
            self.toggled.emit(checked)

    @staticmethod
    def _spring(t: float) -> float:
        """OutBack 弹簧曲线：冲过终点一点点再回弹（比 OutCubic 更有"果冻"感）。"""
        c1 = 1.70158
        c3 = c1 + 1.0
        p = t - 1.0
        return 1.0 + c3 * p * p * p + c1 * p * p

    @classmethod
    def _jelly(cls, t: float) -> float:
        """滑动过程中的横向拉伸：中途拉长、终点回到 0，尾部带一点余震。"""
        main = math.sin(math.pi * t)
        wobble = math.sin(3.0 * math.pi * t) * (1.0 - t) * 0.25
        return max(0.0, main + wobble) * cls.STRETCH

    def _settle(self) -> None:
        self._anim = None
        self._stretch = 0.0
        self.update()

    def set_look_enabled(self, enabled: bool) -> None:
        self._enabled_look = bool(enabled)
        self.setCursor(QCursor(Qt.PointingHandCursor if enabled else Qt.ArrowCursor))
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._enabled_look and self.isEnabled():
            self.setChecked(not self._checked, animate=True, emit=True)
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        track_on = QColor(config.C_PRIMARY)
        track_off = QColor(config.C_SWITCH_OFF)
        if not self._enabled_look:
            track_on.setAlpha(90)
            track_off.setAlpha(120)
        # 底色跟随动画由灰渐变到紫（关闭时反向）
        track = QColor(
            round(track_off.red() + (track_on.red() - track_off.red()) * self._color_t),
            round(track_off.green() + (track_on.green() - track_off.green()) * self._color_t),
            round(track_off.blue() + (track_on.blue() - track_off.blue()) * self._color_t),
            round(track_off.alpha() + (track_on.alpha() - track_off.alpha()) * self._color_t),
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(track)
        radius = self._h / 2.0
        painter.drawRoundedRect(QRectF(0, 0, self._w, self._h), radius, radius)

        diameter = self._h - config.sx(5)
        pad = config.sx(2.5)
        travel = self._w - diameter - 2 * pad
        # 果冻：滑动中滑块横向拉长（以滑块中心为基准向两边撑开），并始终留在轨道内
        width = diameter * (1.0 + self._stretch)
        center = pad + travel * self._offset + diameter / 2.0
        x = center - width / 2.0
        x = max(0.0, min(x, self._w - width))
        painter.setBrush(QColor(255, 255, 255, 255 if self._enabled_look else 200))
        painter.drawRoundedRect(QRectF(x, pad + 0.5, width, diameter), diameter / 2.0, diameter / 2.0)
        painter.end()


class CircleCheck(QWidget):
    """圆形复选框（删除模式使用）。"""

    def __init__(self, checked: bool = False, parent: Optional[QWidget] = None,
                 diameter: int = 20) -> None:
        super().__init__(parent)
        self._d = config.sx(diameter)
        self.setFixedSize(self._d, self._d)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._checked = checked

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        if self._checked != bool(checked):
            self._checked = bool(checked)
            self.update()

    def isChecked(self) -> bool:  # noqa: N802
        return self._checked

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(1, 1, self._d - 2, self._d - 2)
        if self._checked:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(config.C_PRIMARY))
            painter.drawEllipse(rect)
            vector_icons.paint_icon(painter, "check", 1, 1, self._d, "#FFFFFF", 2.4)
        else:
            painter.setPen(QPen(QColor(config.C_BORDER_STRONG), 1.6))
            painter.setBrush(QColor(config.C_CHECK_EMPTY_FILL))
            painter.drawEllipse(rect)
        painter.end()


class StatusDot(QWidget):
    """运行状态小圆点。"""

    def __init__(self, color: str = config.C_SUCCESS, parent: Optional[QWidget] = None,
                 diameter: int = 8) -> None:
        super().__init__(parent)
        self._d = config.sx(diameter)
        self.setFixedSize(self._d, self._d)
        self._color = color

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        halo = QColor(self._color)
        halo.setAlpha(48)
        painter.setPen(Qt.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(QRectF(0, 0, self._d, self._d))
        painter.setBrush(QColor(self._color))
        inset = self._d * 0.24
        painter.drawEllipse(QRectF(inset, inset, self._d - 2 * inset, self._d - 2 * inset))
        painter.end()


# --------------------------------------------------------------------------
# 按钮
# --------------------------------------------------------------------------


def _animate_chevron(owner, target, apply, animate=True):
    previous = getattr(owner, '_chevron_anim', None)
    if previous is not None:
        previous.stop()
        previous.deleteLater()
        owner._chevron_anim = None
    start = getattr(owner, '_chevron_angle', target)
    if not animate or not motion.should_animate(owner) or abs(target - start) < .1:
        owner._chevron_angle = target
        apply(target)
        return
    animation = QVariantAnimation(owner)
    animation.setDuration(motion.COLLAPSE_MS)
    animation.setStartValue(start)
    animation.setEndValue(float(target))
    animation.setEasingCurve(motion.easing())
    def tick(value):
        owner._chevron_angle = float(value)
        apply(float(value))
    animation.valueChanged.connect(tick)
    animation.finished.connect(lambda: setattr(owner, '_chevron_anim', None))
    owner._chevron_anim = animation
    animation.start(QAbstractAnimation.DeleteWhenStopped)


class Chevron(QWidget):
    """A fixed-size painted disclosure icon; rotating never changes text layout."""
    def __init__(self, size=14, color='', parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.color = color or config.C_TEXT_MUTED
        self._chevron_angle = 0.
        motion.track_input()

    def set_expanded(self, expanded, animate=True):
        _animate_chevron(self, 180. if expanded else 0., lambda _: self.update(), animate)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.translate(self.width() / 2., self.height() / 2.)
        painter.rotate(self._chevron_angle)
        size = self.width()
        vector_icons.paint_icon(painter, 'chevron_down', -size / 2., -size / 2., size,
                                self.color, 1.9)
        painter.end()


class IconButton(QPushButton):
    """图标按钮：`IconButton`（描边）或 `IconButtonFlat`（无底）。"""

    def __init__(self, icon_name: str, tooltip: str = "", size: int = 34,
                 icon_size: int = 17, color: str = "",
                 parent: Optional[QWidget] = None, flat: bool = False) -> None:
        super().__init__(parent)
        motion.track_input()
        color = color or config.C_TEXT_BODY
        self.setObjectName("IconButtonFlat" if flat else "IconButton")
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setFlat(True)
        self.setFixedSize(config.sx(size), config.sx(size))
        self.setIconSize(QSize(icon_size, icon_size))
        self._icon_name = icon_name
        self._icon_size = icon_size
        self._color = color
        self.set_icon(icon_name, color)
        if tooltip:
            self.setToolTip(tooltip)

    def set_icon(self, name: str, color: str = "") -> None:
        self._icon_name = name
        if color:
            self._color = color
        self.setIcon(vector_icons.icon(name, self._icon_size, self._color))

    def set_expanded(self, expanded: bool, animate=True, right=False) -> None:
        def apply(angle):
            size = config.sx(self._icon_size)
            image = QPixmap(size, size)
            image.fill(Qt.transparent)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.translate(size / 2., size / 2.)
            painter.rotate(angle)
            vector_icons.paint_icon(painter, 'chevron_down', -size / 2., -size / 2.,
                                    size, self._color, 1.9)
            painter.end()
            self.setIcon(QIcon(image))
        if not hasattr(self, '_chevron_angle'):
            self._chevron_angle = (-90. if right else 0.)
        target = (0. if expanded else -90.) if right else (180. if expanded else 0.)
        _animate_chevron(self, target, apply, animate)

    def set_icon_size_px(self, size: int) -> None:
        self._icon_size = size
        self.setIconSize(QSize(size, size))
        self.set_icon(self._icon_name)


class TextButton(QPushButton):
    """文字按钮：primary / danger / ghost / subtle / danger-ghost。"""

    def __init__(self, text: str, variant: str = "ghost", parent: Optional[QWidget] = None,
                 height: int = 34, icon_name: str = "", icon_size: int = 15) -> None:
        super().__init__(text, parent)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setFont(theme.font(config.FONT_BUTTON, bold=True))
        self.setFixedHeight(config.sx(height))
        self._variant = variant
        self.setProperty("variant", variant)
        self._icon_name = icon_name
        self._icon_size = icon_size
        if icon_name:
            self.setIcon(vector_icons.icon(icon_name, icon_size, self._icon_color()))
            self.setIconSize(QSize(icon_size, icon_size))
        metrics = QFontMetrics(self.font())
        extra = (icon_size + config.sx(7)) if icon_name else 0
        self.setMinimumWidth(metrics.horizontalAdvance(text) + extra + config.sx(30))

    def _icon_color(self) -> str:
        if self._variant in ("primary", "danger"):
            return "#FFFFFF"
        if self._variant == "danger-ghost":
            return config.C_DANGER
        if self._variant == "subtle":
            return config.C_TEXT_MUTED
        return config.C_TEXT_BODY

    def set_variant(self, variant: str, icon_name: str = "") -> None:
        self._variant = variant
        self.setProperty("variant", variant)
        if icon_name:
            self._icon_name = icon_name
        if self._icon_name:
            self.setIcon(vector_icons.icon(self._icon_name, self._icon_size, self._icon_color()))
        theme.repolish(self)


class ClickableLabel(QLabel):
    clicked = Signal()

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setCursor(QCursor(Qt.IBeamCursor))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class SearchBar(QLineEdit):
    """带放大镜图标的搜索栏，输入即刷新。"""

    def __init__(self, placeholder: str = "", parent: Optional[QWidget] = None) -> None:
        placeholder = placeholder or tr("common.search")
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self.setFont(theme.font(config.FONT_BODY))
        self.setFixedHeight(config.sx(34))
        self.addAction(vector_icons.icon("search", 15, config.C_TEXT_FAINT),
                       QLineEdit.LeadingPosition)


class BrandMark(QLabel):
    """品牌标识：自绘靛蓝渐变圆角方块 + 白色监控图标（不依赖 QSS，两种主题都清晰）。"""

    def __init__(self, size: int = 26, icon_ratio: float = 0.58,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._d = config.sx(size)
        self._ratio = icon_ratio
        self.setFixedSize(self._d, self._d)

    def paintEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtCore import QRectF as _QRectF
        from PySide6.QtGui import QLinearGradient as _Gradient

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        d = min(self.width(), self.height())
        gradient = _Gradient(0, 0, d, d)
        gradient.setColorAt(0.0, QColor(config.C_BRAND_MARK_FROM))
        gradient.setColorAt(1.0, QColor(config.C_BRAND_MARK_TO))
        painter.setPen(Qt.NoPen)
        painter.setBrush(gradient)
        radius = d * 0.32
        painter.drawRoundedRect(_QRectF(0, 0, d, d), radius, radius)
        icon_size = max(10, int(d * self._ratio))
        offset = (d - icon_size) / 2.0
        vector_icons.paint_icon(painter, "monitor", offset, offset, icon_size, "#FFFFFF", 2.1)
        painter.end()


class CircleTile(QLabel):
    """圆形图标底座（自绘，适配深色画布）。"""

    def __init__(self, icon_name: str = "monitor", size: int = 64, icon_ratio: float = 0.44,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._d = config.sx(size)
        self._ratio = icon_ratio
        self.setFixedSize(self._d, self._d)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        d = min(self.width(), self.height())
        painter.setPen(QPen(QColor(config.C_EMPTY_TILE_BORDER), 1))
        painter.setBrush(QColor(config.C_EMPTY_TILE_BG))
        painter.drawEllipse(QRectF(0.5, 0.5, d - 1, d - 1))
        icon_size = max(10, int(d * self._ratio))
        offset = (d - icon_size) / 2.0
        vector_icons.paint_icon(painter, self._icon_name, offset, offset, icon_size,
                                config.C_EMPTY_TILE_ICON, 1.7)
        painter.end()


class EmptyState(QWidget):
    """空列表占位：圆形图标 + 标题 + 说明。"""

    def __init__(self, text: str, icon_name: str = "monitor", hint: str = "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(config.sx(12))
        lay.addStretch(1)

        tile = CircleTile(icon_name, 64, 0.44, self)
        holder = QHBoxLayout()
        holder.addStretch(1)
        holder.addWidget(tile)
        holder.addStretch(1)
        lay.addLayout(holder)

        self.text_label = theme.label(text, config.FONT_SECTION, True, config.C_ON_CANVAS)
        self.text_label.setObjectName("EmptyTitle")
        self.text_label.setAlignment(Qt.AlignCenter)
        self.text_label.setWordWrap(True)
        lay.addWidget(self.text_label)

        self.hint_label = theme.label(hint, config.FONT_SMALL, False, config.C_ON_CANVAS_MUTED)
        self.hint_label.setObjectName("EmptyHint")
        self.hint_label.setAlignment(Qt.AlignCenter)
        self.hint_label.setWordWrap(True)
        # 必须先加进布局（获得父控件）再 setVisible：
        # 对没有父控件的控件调用 setVisible(True) 会让它变成独立顶层窗口并弹出来
        lay.addWidget(self.hint_label)
        self.hint_label.setVisible(bool(hint))
        lay.addStretch(1)

    def set_text(self, text: str, hint: str = "") -> None:
        self.text_label.setText(text)
        self.hint_label.setText(hint)
        self.hint_label.setVisible(bool(hint))
