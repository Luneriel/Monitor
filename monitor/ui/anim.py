"""统一的动画工具：页面淡入淡出、弹窗缩放弹出、控件淡入。"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QParallelAnimationGroup, QPauseAnimation,
                            QEvent, QPoint, QPropertyAnimation, QRect, QSize, QSequentialAnimationGroup, Qt,
                            QTimer, QVariantAnimation)
from PySide6.QtWidgets import (QAbstractScrollArea, QGraphicsOpacityEffect, QSizePolicy,
                               QWidget)

from .. import config
from . import motion

# 保持动画对象引用，避免被 GC 回收
_running: set = set()


def _keep(anim) -> None:
    _running.add(anim)
    anim.finished.connect(lambda: _running.discard(anim))
    anim.destroyed.connect(lambda: _running.discard(anim))


def set_opacity(widget: QWidget, value: float) -> QGraphicsOpacityEffect:
    effect = widget.graphicsEffect()
    if not isinstance(effect, QGraphicsOpacityEffect):
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
    effect.setOpacity(value)
    return effect


def clear_opacity(widget: QWidget) -> None:
    widget.setGraphicsEffect(None)


def fade_in(widget: QWidget, duration: int = config.ANIM_NORMAL, on_done: Optional[Callable[[], None]] = None) -> None:
    effect = set_opacity(widget, 0.0)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)

    def _finish() -> None:
        clear_opacity(widget)
        if on_done:
            on_done()

    anim.finished.connect(_finish)
    _keep(anim)
    anim.start(QAbstractAnimation.DeleteWhenStopped)


def fade_out(widget: QWidget, duration: int = config.ANIM_FAST, on_done: Optional[Callable[[], None]] = None) -> None:
    effect = set_opacity(widget, 1.0)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(1.0)
    anim.setEndValue(0.0)
    anim.setEasingCurve(QEasingCurve.InCubic)

    def _finish() -> None:
        clear_opacity(widget)
        if on_done:
            on_done()

    anim.finished.connect(_finish)
    _keep(anim)
    anim.start(QAbstractAnimation.DeleteWhenStopped)


def pop_in(window: QWidget, duration: int = config.ANIM_SLOW, scale: float = 0.92) -> None:
    """弹窗缩放弹出：透明度 + 尺寸同时动画。"""
    end_rect: QRect = window.geometry()
    center = end_rect.center()
    w = max(80, int(end_rect.width() * scale))
    h = max(80, int(end_rect.height() * scale))
    start_rect = QRect(center.x() - w // 2, center.y() - h // 2, w, h)

    group = QParallelAnimationGroup(window)
    geo = QPropertyAnimation(window, b"geometry", window)
    geo.setDuration(duration)
    geo.setStartValue(start_rect)
    geo.setEndValue(end_rect)
    geo.setEasingCurve(QEasingCurve.OutCubic)
    group.addAnimation(geo)

    window.setWindowOpacity(0.0)

    def _opacity_tick(value: float) -> None:
        window.setWindowOpacity(value)

    opacity = QPropertyAnimation(window, b"windowOpacity", window)
    opacity.setDuration(duration)
    opacity.setStartValue(0.0)
    opacity.setEndValue(1.0)
    opacity.setEasingCurve(QEasingCurve.OutCubic)
    group.addAnimation(opacity)

    _keep(group)
    group.start(QAbstractAnimation.DeleteWhenStopped)


def pop_widget(container: QWidget, scale: float = 0.94, duration: int = config.ANIM_NORMAL,
               window: Optional[QWidget] = None) -> None:
    """弹窗内容缩放弹出：对内部容器做 geometry 动画（外层窗口保持固定尺寸）。"""
    target: QRect = container.geometry()
    w = max(60, int(target.width() * scale))
    h = max(60, int(target.height() * scale))
    start = QRect(target.center().x() - w // 2, target.center().y() - h // 2, w, h)

    group = QParallelAnimationGroup(container)

    geo = QPropertyAnimation(container, b"geometry", container)
    geo.setDuration(duration)
    geo.setStartValue(start)
    geo.setEndValue(target)
    geo.setEasingCurve(QEasingCurve.OutCubic)
    group.addAnimation(geo)

    if window is not None:
        window.setWindowOpacity(0.35)
        opacity = QPropertyAnimation(window, b"windowOpacity", window)
        opacity.setDuration(duration)
        opacity.setStartValue(0.35)
        opacity.setEndValue(1.0)
        opacity.setEasingCurve(QEasingCurve.OutCubic)
        group.addAnimation(opacity)

    _keep(group)
    group.start(QAbstractAnimation.DeleteWhenStopped)


def animate_towards(getter, setter, target: float, duration: int = config.ANIM_NORMAL,
                    owner: Optional[QWidget] = None) -> None:
    """对任意数值做平滑过渡（滑块、阴影等）。"""
    from PySide6.QtCore import QVariantAnimation

    start = float(getter())
    va = QVariantAnimation(owner)
    va.setDuration(duration)
    va.setStartValue(start)
    va.setEndValue(float(target))
    va.setEasingCurve(QEasingCurve.OutCubic)
    va.valueChanged.connect(lambda v: setter(float(v)))
    _keep(va)
    va.start(QAbstractAnimation.DeleteWhenStopped)


def animate_list_removal(parent: QWidget, victims: list, spacing: int,
                         on_finished: Optional[Callable[[], None]] = None,
                         slide_ms: int = 260, lift_ms: int = 220,
                         stagger_ms: int = 40, slide_ratio: float = 1.0,
                         max_ms: int = config.ANIM_DELETE_MAX
                         ) -> Optional[QSequentialAnimationGroup]:
    """列表删除动画。

    阶段一：被删除的条目**向右平移并淡出**（速度线性提升，InQuad 缓动）。
    阶段二：其余条目按**从上到下的顺序**依次上移填补空位。
    动画结束后回调 ``on_finished``（通常用于按数据库结果重建列表）。

    ``slide_ratio`` 控制平移距离占宽度的比例：外层有滚动视口裁剪时用 1.0（完全滑出），
    条目位于不会裁剪的面板内时用较小值，避免滑出面板边界。

    ``max_ms`` 是整段动画（阶段一 + 阶段二）的时长上限：单页日志最多 500 条，
    逐行错开 40ms 会一路排到二十秒开外，而那时动画早就滚出可见范围，白等又白烧性能。
    所以错峰间隔按预算压缩（行越多间隔越小、可以到 0），错峰本来就装得下时不受影响；
    万一动画真跑过头，还有一次强制收尾兜底（没走完的行直接落到终点）。

    返回动画组（调用方通常忽略，自检用它断言总时长）。
    """
    layout = parent.layout() if parent is not None else None
    if layout is None:
        if on_finished:
            on_finished()
        return None

    widgets = []
    for index in range(layout.count()):
        item = layout.itemAt(index)
        widget = item.widget() if item is not None else None
        if widget is not None and widget.isVisible():
            widgets.append(widget)

    victim_ids = {id(w) for w in victims}
    victim_widgets = [w for w in widgets if id(w) in victim_ids]
    if not widgets or not victim_widgets:
        if on_finished:
            on_finished()
        return None

    # 冻结容器尺寸：把子项移出布局后布局会塌陷，子控件会被父控件裁剪而"整块消失"
    frozen = parent.size()
    parent.setFixedSize(frozen)

    # 脱离布局，改为手工定位，避免动画期间被布局重置
    for widget in widgets:
        layout.removeWidget(widget)

    survivor_ids = {id(w) for w in victim_widgets}
    survivors = [w for w in widgets if id(w) not in survivor_ids]
    targets: dict = {}
    cursor = widgets[0].y()
    for widget in survivors:
        targets[widget] = cursor
        cursor += widget.height() + spacing

    master = QSequentialAnimationGroup(parent)

    # 阶段一：向右平移 + 淡出（速度线性提升）
    slide_group = QParallelAnimationGroup()
    for widget in victim_widgets:
        distance = max(24.0, widget.width() * max(0.1, slide_ratio))
        move = QPropertyAnimation(widget, b"pos", widget)
        move.setDuration(slide_ms)
        move.setStartValue(widget.pos())
        move.setEndValue(QPoint(int(widget.x() + distance), widget.y()))
        move.setEasingCurve(QEasingCurve.InQuad)
        slide_group.addAnimation(move)

        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(1.0)
        widget.setGraphicsEffect(effect)
        fade = QPropertyAnimation(effect, b"opacity", widget)
        fade.setDuration(slide_ms)
        fade.setStartValue(1.0)
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.InQuad)
        slide_group.addAnimation(fade)
    master.addAnimation(slide_group)

    # 阶段二：其余条目从上到下一个接一个上移
    # 只给"真的要挪位置"的行挂动画：被删行上方那些行的目标位置就是当前位置，挂上去
    # 既不产生任何动画效果，又照样每帧参与 Qt 的动画树遍历。
    moving = [w for w in survivors if targets[w] != w.y()]
    if moving:
        # 错峰间隔按预算压缩：留出阶段一 + 相间停顿 + 单项上移之外的余量，
        # 行数越多间隔越小（可为 0，即整段几乎同时上移）。
        budget = max(0, max_ms - slide_ms - 20 - lift_ms)
        if len(moving) > 1:
            stagger_ms = max(0, min(stagger_ms, budget // (len(moving) - 1)))
        else:
            stagger_ms = 0
        master.addAnimation(QPauseAnimation(20))
        lift_group = QParallelAnimationGroup()
        for index, widget in enumerate(moving):
            sequence = QSequentialAnimationGroup()
            if index:
                sequence.addAnimation(QPauseAnimation(index * stagger_ms))
            lift = QPropertyAnimation(widget, b"pos", widget)
            lift.setDuration(lift_ms)
            lift.setStartValue(widget.pos())
            lift.setEndValue(QPoint(widget.x(), targets[widget]))
            lift.setEasingCurve(QEasingCurve.OutCubic)
            sequence.addAnimation(lift)
            lift_group.addAnimation(sequence)
        master.addAnimation(lift_group)

    guard = QTimer(parent)
    guard.setSingleShot(True)
    guard.setInterval(max(1, int(max_ms)))
    done = {"flag": False}

    def _cleanup() -> None:
        if done["flag"]:
            return
        done["flag"] = True
        guard.stop()
        guard.deleteLater()
        _running.discard(master)
        parent.setMinimumSize(0, 0)
        parent.setMaximumSize(16777215, 16777215)
        for widget in victim_widgets:
            widget.hide()
        if on_finished:
            # 日志页重建会 sendPostedEvents(DeferredDelete) 回收旧面板。
            # 必须先退出 finished/timeout 的 Qt 回调栈，否则面板销毁时
            # 会连带删除尚在执行的动画组/定时器，产生原生层崩溃。
            # 绑定 parent 上下文：页面提前关闭时不再调用重建回调。
            QTimer.singleShot(0, parent, on_finished)

    def _force_finish() -> None:
        # 兜底：动画正常时必定在 max_ms 内结束；真要跑过头就把没走完的行直接落到
        # 终点、立刻走收尾流程 —— 删除逻辑不会为了动画多等。
        for widget, target in targets.items():
            widget.move(widget.x(), target)
        master.stop()
        _cleanup()

    guard.timeout.connect(_force_finish)
    guard.start()
    master.finished.connect(_cleanup)
    _keep(master)
    master.start(QAbstractAnimation.DeleteWhenStopped)
    return master


def slide_in(widget: QWidget, dy: int = 14, duration: int = config.ANIM_NORMAL) -> None:
    """内容轻微下滑淡入（只动位置，不动透明度，避免整页离屏渲染的文字抖动）。"""
    base = widget.pos()
    widget.move(base.x(), base.y() + dy)
    animation = QPropertyAnimation(widget, b"pos", widget)
    animation.setDuration(duration)
    animation.setStartValue(widget.pos())
    animation.setEndValue(base)
    animation.setEasingCurve(QEasingCurve.OutCubic)
    _keep(animation)
    animation.start(QAbstractAnimation.DeleteWhenStopped)


# --------------------------------------------------------------------------
# 可展开卡片的高度动画（通知页 / 控制页共用）
#
# 展开 / 收起只改**裁剪容器**的高度，内容控件本体在动画期间保持自然高度：否则
# 每帧压缩控件高度会让内部布局反复重排（文字重新换行 → 高度再变），表现出来就是
# 卡片在收起时"发抖"。
# --------------------------------------------------------------------------
class _ClipHolder(QWidget):
    """One layout item, one clipping boundary; the child never gets compressed.

    The old two-layer holder reserved the full expansion immediately and only grew
    ancestors. Its layout minimum therefore retained the whole gap until finish.
    A manually positioned child cannot contribute a rigid minimum to this holder.
    """
    def __init__(self, content: QWidget, parent: QWidget | None, collapsed: bool, gap=0):
        super().__init__(parent)
        self.setObjectName("ClipHolder")
        self.content = content
        self._layout_gap = gap
        self._height_anim = None
        self._expanded = not collapsed
        self._extent = 0 if collapsed else None
        self._placing = False
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        content.setParent(self)
        content.setMinimumHeight(0)
        content.setMaximumHeight(16777215)
        content.installEventFilter(self)
        content.show()
        if collapsed:
            self.setFixedHeight(0)
            self.hide()

    def natural_height(self) -> int:
        width = max(1, self.width())
        content = self.content
        hint = content.heightForWidth(width) if content.hasHeightForWidth() else -1
        return self._layout_gap + max(0, hint if hint >= 0 else content.sizeHint().height())

    def sizeHint(self):  # noqa: N802
        height = self.natural_height() if self._extent is None else self._extent
        return QSize(self.content.sizeHint().width(), height)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(0, 0)

    def _place(self):
        if self._placing:
            return
        self._placing = True
        try:
            content = self.content
            content.setGeometry(0, self._layout_gap, max(1, self.width()),
                                max(0, self.natural_height() - self._layout_gap))
            if content.layout() is not None:
                content.layout().activate()
        finally:
            self._placing = False

    def resizeEvent(self, event):  # noqa: N802
        self._place()
        super().resizeEvent(event)

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.content and event.type() == QEvent.LayoutRequest and not self._placing:
            self._place()
            animation = self._height_anim
            if animation is not None and self._expanded:
                animation.setEndValue(self.natural_height())
            elif self._expanded:
                self.updateGeometry()
                _settle_ancestors(self)
        return False


def clip_holder(widget: QWidget) -> QWidget:
    """Return the actual animated layout item, preserving the public content API."""
    holder = getattr(widget, "_clip_container", None)
    if holder is not None:
        return holder
    motion.track_input()
    parent = widget.parentWidget()
    layout = parent.layout() if parent is not None else None
    index = layout.indexOf(widget) if layout is not None else -1
    collapsed = widget.maximumHeight() == 0 or widget.isHidden()
    if layout is not None:
        layout.removeWidget(widget)
    # A header/content pair owns its inter-item gap. Fold that gap into the
    # animated extent too, otherwise QWidget.hide() removes spacing in one jump
    # even after the content has smoothly reached zero pixels.
    gap = 0
    if layout is not None and index == 1 and layout.count() == 1:
        gap = max(0, layout.spacing())
        layout.setSpacing(0)
    holder = _ClipHolder(widget, parent, collapsed, gap)
    widget._clip_container = holder
    # Compatibility for diagnostics that previously inspected a second clip layer.
    widget._clip_view = holder
    holder._clip_view = holder
    if layout is not None:
        layout.insertWidget(max(0, index), holder)
    holder.setVisible(not collapsed)
    _settle_ancestors(holder)
    return holder


def _stop_height(holder):
    previous = holder._height_anim
    if previous is not None:
        holder._height_anim = None
        previous.stop()
        previous.deleteLater()


def clip_height(widget: QWidget, height: Optional[int], visible: Optional[bool] = None) -> None:
    """Set a collapsed or natural height immediately, cancelling any transition."""
    holder = clip_holder(widget)
    _stop_height(holder)
    holder._extent = None if height is None else max(0, int(height))
    holder._expanded = height is None or bool(height)
    if height is None:
        holder.setMinimumHeight(0)
        holder.setMaximumHeight(16777215)
    else:
        holder.setFixedHeight(holder._extent)
    if visible is not None:
        holder.setVisible(bool(visible))
    holder._place()
    holder.updateGeometry()
    _settle_ancestors(holder)


def _settle_ancestors(widget: QWidget) -> None:
    """Invalidate bottom-up, then lay out top-down in the *same* animation frame.

    Both shrinking and growing change the scroll canvas, so siblings close their
    gap while the content is clipped rather than waiting for animation completion.
    No sibling minimum heights or permanent ancestor height locks are introduced.
    """
    chain = []
    parent = widget.parentWidget()
    scroll = None
    while parent is not None and not parent.isWindow():
        if isinstance(parent, QAbstractScrollArea):
            scroll = parent
            break
        chain.append(parent)
        parent = parent.parentWidget()
    for ancestor in chain:
        layout = ancestor.layout()
        if layout is not None:
            layout.invalidate()
        ancestor.updateGeometry()
    if scroll is not None:
        canvas = scroll.widget() if hasattr(scroll, "widget") else None
        if canvas is not None:
            viewport = scroll.viewport()
            layout = canvas.layout()
            hint = layout.totalHeightForWidth(viewport.width()) if layout and layout.hasHeightForWidth() else -1
            needed = max(canvas.minimumSizeHint().height(), hint if hint >= 0 else canvas.sizeHint().height())
            canvas.resize(viewport.width(), max(viewport.height(), needed))
    for ancestor in reversed(chain):
        layout = ancestor.layout()
        if layout is not None:
            layout.activate()


def animate_height(widget: QWidget, expanded: bool, parent=None) -> None:
    """Retarget from the visible height; stable content is revealed with one clip."""
    holder = clip_holder(widget)
    start = holder.height() if not holder.isHidden() else 0
    _stop_height(holder)
    holder._expanded = bool(expanded)
    holder._place()
    end = holder.natural_height() if expanded else 0
    if not motion.should_animate(widget):
        clip_height(widget, None if expanded else 0, visible=expanded)
        return
    holder._extent = start
    holder.setFixedHeight(start)
    holder.show()
    animation = QVariantAnimation(holder)
    animation.setDuration(motion.COLLAPSE_MS)
    animation.setStartValue(int(start))
    animation.setEndValue(int(end))
    animation.setEasingCurve(motion.easing())

    def _apply(value):
        holder._extent = max(0, int(value))
        holder.setFixedHeight(holder._extent)
        holder.updateGeometry()
        _settle_ancestors(holder)

    def _finish():
        if holder._height_anim is not animation:
            return
        holder._height_anim = None
        holder._extent = None if expanded else 0
        if expanded:
            holder.setMinimumHeight(0)
            holder.setMaximumHeight(16777215)
        else:
            holder.setFixedHeight(0)
            holder.hide()
        holder.updateGeometry()
        _settle_ancestors(holder)

    animation.valueChanged.connect(_apply)
    animation.finished.connect(_finish)
    holder._height_anim = animation
    _apply(start)
    animation.start(QAbstractAnimation.DeleteWhenStopped)
