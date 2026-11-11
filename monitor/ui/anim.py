"""统一的动画工具：页面淡入淡出、弹窗缩放弹出、控件淡入。"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QParallelAnimationGroup, QPauseAnimation,
                            QPoint, QPropertyAnimation, QRect, QSequentialAnimationGroup, Qt,
                            QTimer, QVariantAnimation)
from PySide6.QtWidgets import (QAbstractScrollArea, QGraphicsOpacityEffect, QSizePolicy, QVBoxLayout,
                               QWidget)

from .. import config

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
def clip_holder(widget: QWidget) -> QWidget:
    """按需给 ``widget`` 包两层容器，返回**外层**（父布局里的那一项）。

    外层（holder）负责给区域占位，内层（view）才是逐帧变高的裁剪框。要两层的原因：
    展开时如果让**父布局里那一项**的高度逐帧变大，父链的尺寸需求就逐帧变大，而父链的
    实际几何要晚一轮布局才跟上 —— 这一两帧的高度缺口会被 ``qGeomCalc`` 从别的项目身上
    扣（实测：控制页规则卡的标题行被从 53 挤到 25、内容整体上跳 28px；通知页时长下拉栏
    上方的勾选网格被挤掉，整条下拉栏上跳 19px，这就是用户报的"展开时文字发抖"）。

    新结构下展开瞬间外层就直接占满目标高度、之后全程不动，逐帧变化的只有内层，于是
    布局需求从头到尾不变、谁也不挪位（见 ``animate_height``）。
    """
    holder = getattr(widget, "_clip_container", None)
    if holder is not None:
        return holder

    parent = widget.parentWidget()
    layout = parent.layout() if parent is not None else None
    index = layout.indexOf(widget) if layout is not None else -1
    collapsed = widget.maximumHeight() == 0

    holder = QWidget(parent)
    holder.setObjectName("ClipHolder")
    outer = QVBoxLayout(holder)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)

    view = QWidget(holder)
    view.setObjectName("ClipView")
    inner = QVBoxLayout(view)
    inner.setContentsMargins(0, 0, 0, 0)
    inner.setSpacing(0)
    inner.addWidget(widget)
    outer.addWidget(view)
    # 内层裁剪框必须钉在外层顶部：外层在展开动画里全程是"目标高度"，而内层当时只有
    # 当前动画高度，QVBoxLayout 会把偏小的内层**垂直居中**在外层里（实测 y = (275-0)/2
    # = 137px），于是内容整体从下方 137px 处往上滑 —— 标题行不跳了，改成内容跳。
    # 钉住顶部后内层从外层上沿向下展开，内容首行一动不动。
    outer.setAlignment(view, Qt.AlignTop)

    # 约束交给容器：控件本体恢复成"自然高度"，动画期间也不会被压扁
    widget.setMinimumHeight(0)
    widget.setMaximumHeight(16777215)
    widget.setVisible(True)
    widget._clip_container = holder
    widget._clip_view = view
    holder._clip_view = view

    if layout is not None:
        layout.insertWidget(max(0, min(index, layout.count())), holder)
        # 兜住多余高度：收起时释放出来的空间若被"别的"项目吃掉，它们就会长高一下。
        # 尾部弹簧独占多余空间后，其余各项的位置就完全不受影响。
        if hasattr(layout, "addStretch") and layout.count():
            last = layout.itemAt(layout.count() - 1)
            if last is not None and last.widget() is holder:
                layout.addStretch(1)
    holder.setMinimumHeight(0)
    view.setMinimumHeight(0)
    holder.setMaximumHeight(0 if collapsed else 16777215)
    view.setMaximumHeight(0 if collapsed else 16777215)
    holder.setVisible(not collapsed)
    return holder


def clip_height(widget: QWidget, height: Optional[int], visible: Optional[bool] = None) -> None:
    """不做动画地设置裁剪高度（``height=None`` = 恢复自动高度）。"""
    holder = clip_holder(widget)
    view = getattr(holder, "_clip_view", holder)
    limit = 16777215 if height is None else max(0, int(height))
    holder.setMinimumHeight(0)
    holder.setMaximumHeight(limit)
    view.setMinimumHeight(0)
    view.setMaximumHeight(limit)
    if visible is not None:
        holder.setVisible(bool(visible))
    _settle_ancestors(holder)


def _settle_ancestors(widget: QWidget) -> None:
    """把 ``widget`` 的祖先按"新的尺寸需求"同步放大到位。

    逐帧抬高裁剪容器的上界，等于逐帧抬高**父链的尺寸需求**（Qt 在算布局时会用
    ``maximumSize`` 去夹控件的 sizeHint），但父链的**实际几何**要等下一轮布局
    （``QScrollArea`` 重新给内层控件定尺寸、各层 ``QLayout::activate``）才跟上。
    这一两帧的"高度缺口"会被 ``qGeomCalc`` 从**前面的项目**身上扣：控制页规则卡
    里是标题行被从 53 挤到 25（内容整体上跳 28px），通知页时长下拉栏里是被挤掉
    上方的勾选网格（整条下拉栏上跳 19px）——用户看到的就是"展开时文字发抖"。

    所以这里不等事件循环，直接把祖先撑到各自的 sizeHint（只增不减，且跳过顶层
    窗口；碰到滚动区域就只处理它的内层控件，那才是内容真正所在的画布）。
    """
    chain = []
    parent = widget.parentWidget()
    while parent is not None and not parent.isWindow():
        chain.append(parent)
        parent = parent.parentWidget()

    for ancestor in reversed(chain):
        if isinstance(ancestor, QAbstractScrollArea):
            inner = ancestor.widget()
            if inner is not None:
                needed = inner.sizeHint().height()
                if inner.height() < needed:
                    inner.resize(inner.width(), needed)
                layout = inner.layout()
                if layout is not None:
                    layout.activate()
            break
        needed = ancestor.sizeHint().height()
        if ancestor.height() < needed:
            ancestor.resize(ancestor.width(), needed)
        layout = ancestor.layout()
        if layout is not None:
            layout.activate()


def _item_widgets(item):
    """展开一个布局项里的所有控件（布局项可能是子布局，例如卡片标题行）。"""
    if item is None:
        return
    widget = item.widget()
    if widget is not None:
        yield widget
        return
    layout = item.layout()
    if layout is not None:
        for index in range(layout.count()):
            yield from _item_widgets(layout.itemAt(index))


def _unlock_items(locked) -> None:
    for widget, minimum, maximum in locked or ():
        try:
            widget.setMinimumHeight(minimum)
            widget.setMaximumHeight(maximum)
        except RuntimeError:            # 控件已被销毁（重建界面时）
            pass


def _lock_items_before(holder: QWidget) -> list:
    """动画期间把"排在 holder 之前"的兄弟项钉成固定高度。

    父链的实际几何要晚一轮布局才跟上新的高度需求，这一两帧的高度缺口会被
    ``qGeomCalc`` 从排在 holder **之前**的项目身上扣 —— 实测：控制页规则卡里标题行
    被从 53 挤到 25（内容整体上跳 28px），通知页时长下拉栏里是上方的勾选网格被挤掉
    （整条下拉栏上跳 19px）。把这些项目钉住后，缺口只能由 holder 自己吃：它本来就是
    一个高度可变的裁剪框，肉眼看不到它变矮。
    """
    locked = []
    node = holder
    while node is not None and not node.isWindow():
        parent = node.parentWidget()
        if parent is None or isinstance(parent, QAbstractScrollArea):
            break
        layout = parent.layout()
        if layout is not None:
            index = layout.indexOf(node)
            for position in range(index if index >= 0 else layout.count()):
                for widget in _item_widgets(layout.itemAt(position)):
                    if widget is node or widget in [entry[0] for entry in locked]:
                        continue
                    height = max(0, widget.height())
                    locked.append((widget, widget.minimumHeight(), widget.maximumHeight()))
                    widget.setMinimumHeight(height)
                    widget.setMaximumHeight(height)
        node = parent
    return locked


def animate_height(widget: QWidget, expanded: bool, parent=None) -> None:
    """展开 / 收起 widget 所在区域，高度做补间。

    两个方向用的是**不同**的动画量，因为只有"需求变大"才会引发前文那种挤压：

    * 展开 —— 外层容器瞬间占满目标高度（父布局的需求一次到位、之后全程不变），
      逐帧变高的只有内层裁剪框：内容就在已经预留好的空间里"向下展开"。父链不变，
      所以标题行 / 勾选网格一格都不会挪。展开那一帧的需求跳变由 ``_settle_ancestors``
      当场撑住。
    * 收起 —— 外层容器逐帧变矮（需求只减不增，多出来的空间有尾部弹簧兜着），
      内层不限高，内容自然被外层裁掉。
    """
    holder = clip_holder(widget)
    view = getattr(holder, "_clip_view", holder)
    _unlock_items(getattr(holder, "_locked_items", None))
    holder._locked_items = None
    previous = getattr(holder, "_height_anim", None)
    if previous is not None:
        try:
            previous.stop()
            previous.deleteLater()
        except RuntimeError:
            pass
        holder._height_anim = None

    if expanded:
        holder.setVisible(True)
    # 量自然高度前先把宽度推到真实值：收起状态下的内容停在旧宽度上（hidden 的控件
    # 不参与排版，实测宽度一直是 100px），带自动换行的文本会按那个宽度换行，于是
    # ``sizeHint`` 既可能偏大（实测 403，真实宽度下只要 375 —— 展开动画结束后整套
    # 内容会缩回 28px，正是用户报的“展开时文本/图标高度收缩”）也可能偏小（动画期间
    # 孩子被压扁）。这里先撑一次父链、让内容布局在真实宽度下重算，再取自然高度。
    _settle_ancestors(holder)
    content_layout = widget.layout()
    if content_layout is not None:
        content_layout.activate()
    # 动画期间冻结内容高度：内部布局不重排，高度也就不会抖。
    # 展开时用 sizeHint（要长到完整高度）；收起时改用**当前实际高度**——布局可能
    # 已经把它压得比 sizeHint 小（实测 348 vs 362），若按 sizeHint 冻结，容器第一帧
    # 会被内容顶回 362（收起时先向上"弹"14px 再回落，正是要修的发抖）。
    natural = max(0, widget.sizeHint().height())
    if widget.hasHeightForWidth():
        # 带自动换行的内容在宽度确定后，真实自然高度由 heightForWidth 给出：
        # sizeHint 是"不受宽度约束"的那份估计（实测 403 vs 真实 375），差出来的
        # 28px 会在动画结束时被布局收回去 —— 用户看到的就是「展开时文本/图标高度收缩」。
        hint = widget.heightForWidth(widget.width())
        if hint >= 0:
            natural = max(0, int(hint))
    freeze = natural if expanded else min(natural, widget.height() or natural)
    widget.setMinimumHeight(freeze)
    widget.setMaximumHeight(freeze)

    # 内层裁剪框的竖直尺寸策略设成 Ignored：它的最小高度来自被冻结的内容
    # （min=max=freeze），而 Qt 的 qSmartMinSize 只会用显式最小尺寸往上抬、不会把
    # 布局推导出的最小值压下去——所以只写 setMinimumHeight(0) 是空操作，容器依旧是
    # "刚性"的。设成 Ignored 后它才能真的被压矮（收起方向靠它把内容裁掉）。
    # 动画结束必须恢复原策略：长期 Ignored 会让容器去抢父布局的富余空间。
    policy = getattr(view, "_layout_policy", None)
    if policy is None:
        policy = view.sizePolicy()
        view._layout_policy = policy
    view.setSizePolicy(policy.horizontalPolicy(), QSizePolicy.Ignored)

    if expanded:
        start = view.height() if view.isVisible() else 0
        end = natural
    else:
        start = holder.height() if holder.isVisible() else 0
        end = 0

    animation = QVariantAnimation(parent or holder)
    animation.setDuration(config.ANIM_NORMAL)
    animation.setStartValue(int(max(0, start)))
    animation.setEndValue(int(max(0, end)))
    animation.setEasingCurve(QEasingCurve.OutCubic)

    def _apply(value) -> None:
        height = int(value)
        if expanded:
            # 外层钉在目标高度（只钉一次），逐帧只抬高内层裁剪框
            holder.setMinimumHeight(natural)
            holder.setMaximumHeight(natural)
            view.setMinimumHeight(0)
            view.setMaximumHeight(height)
        else:
            view.setMinimumHeight(0)
            view.setMaximumHeight(16777215)
            holder.setMinimumHeight(0)
            holder.setMaximumHeight(height)
        _settle_ancestors(holder)

    def _finish() -> None:
        holder._height_anim = None
        _unlock_items(getattr(holder, "_locked_items", None))
        holder._locked_items = None
        view.setSizePolicy(policy)          # 恢复原策略（见上面的注释）
        widget.setMinimumHeight(0)          # 解除冻结，让内容按需自适应
        widget.setMaximumHeight(16777215)
        holder.setMinimumHeight(0)
        view.setMinimumHeight(0)
        if expanded:
            holder.setMaximumHeight(16777215)
            view.setMaximumHeight(16777215)
        else:
            holder.setMaximumHeight(0)
            view.setMaximumHeight(0)
            holder.setVisible(False)

    animation.valueChanged.connect(_apply)
    animation.finished.connect(_finish)
    holder._height_anim = animation       # 持有引用，避免动画对象被回收
    holder._locked_items = _lock_items_before(holder)
    _apply(start)                         # 先把第一帧摆好（外层占位 / 内层归零）
    animation.start(QAbstractAnimation.DeleteWhenStopped)
