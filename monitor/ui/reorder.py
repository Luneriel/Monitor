"""卡片拖动手柄排序：同组卡片互相换位 + 拖到视口边缘自动滚动 + 拖动动效。

用法::

    self.reorder = ReorderController(self.scroll, self._commit_order, self)
    ...
    self.reorder.clear()                       # 重建卡片前先清空（旧控件会被销毁）
    self.reorder.register("program", cards)    # cards 按当前显示顺序传入

约定：卡片控件上带 ``program_id`` 与 ``drag_handle`` 两个属性。控制器只给手柄装事件
过滤器，卡片其余位置（标题 / 输入框 / 按钮）照常响应点击；拖动只在同一 group 内部生效，
跨分组（程序 ↔ 网站）不会发生任何交换。

动效：
    * 按下：卡片被"拿起"——脱离布局浮到列表上层（``Card.set_lift`` 加深阴影，尺寸轻微
      放大），原位留一个虚线占位框，抬起过程 130ms；
    * 移动：浮层跟随光标（保持按下时的抓取偏移）；越过别的卡片中线时占位框换位，
      让位的卡片用 140ms OutCubic 滑到新槽位；
    * 松手：浮层 160ms 落回占位框，再按最终顺序提交（``on_commit``）。
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, QRect, QSize,
                            QTimer, Qt, QVariantAnimation)
from PySide6.QtGui import QColor, QCursor
from PySide6.QtWidgets import QAbstractScrollArea, QFrame, QWidget

from .. import config

EDGE_PX = 46         # 光标进入距视口上下边缘这个范围内开始自动滚动
MAX_STEP = 26        # 每次自动滚动最多移动多少像素
TICK_MS = 24         # 自动滚动的帧间隔
LIFT_MS = 130        # 拿起 / 放下时的阴影与放大动画时长
SLIDE_MS = 140       # 让位的卡片滑到新槽位
DROP_MS = 160        # 松手后浮层落回占位框
LIFT_SCALE = 1.016   # 拿起时的放大比例（"轻微抬高"）


class ReorderController(QObject):
    """把一组卡片的手柄变成拖动排序手柄。"""

    def __init__(self, scroll: QAbstractScrollArea,
                 on_commit: Callable[[str, list], None],
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent or scroll)
        self._scroll = scroll
        self._on_commit = on_commit
        self._holds: dict[QWidget, QWidget] = {}      # 手柄 -> 卡片
        self._groups: dict[str, list[QWidget]] = {}   # 分组 -> 卡片（当前顺序）
        self._group_of: dict[QWidget, str] = {}       # 卡片 -> 分组
        self._card: Optional[QWidget] = None
        self._group = ""
        self._last = QPoint()
        self._origin: list[QWidget] = []              # 按下那一刻的顺序（取消时还原）
        self._holder: Optional[QWidget] = None        # 卡片所在的列表容器
        self._placeholder: Optional[QWidget] = None   # 原位虚线占位框
        self._slot_order: list[QWidget] = []          # 布局里当前的槽位顺序（含占位框）
        self._slots: dict[QWidget, QRect] = {}        # 各卡片的目标槽位（holder 坐标）
        self._press_offset = QPoint()
        self._grab_size = QSize()
        self._lift_progress = 0.0
        self._slides: dict[QWidget, QPropertyAnimation] = {}
        self._lift_anim: Optional[QVariantAnimation] = None
        self._drop_anim: Optional[QPropertyAnimation] = None
        self._pending: Optional[tuple] = None         # 落回动画结束后要做的还原
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._on_tick)

    # ------------------------------------------------------------- 登记 / 清理
    def clear(self) -> None:
        """重建卡片前调用：结束进行中的拖动、摘掉事件过滤器、丢弃旧引用。"""
        self._abort()
        for handle in list(self._holds):
            try:
                handle.removeEventFilter(self)
            except RuntimeError:          # 控件已经被销毁
                pass
        self._holds.clear()
        self._groups.clear()
        self._group_of.clear()

    def register(self, group: str, cards: list[QWidget]) -> None:
        """登记一组卡片；只有同组卡片之间可以拖动换位。"""
        items = [card for card in cards if card is not None]
        self._groups[group] = list(items)
        for card in items:
            self._group_of[card] = group
            handle = getattr(card, "drag_handle", None)
            if handle is None:
                continue
            handle.installEventFilter(self)
            self._holds[handle] = card
            handle.setCursor(QCursor(Qt.OpenHandCursor))

    def order(self, group: str) -> list:
        """该分组当前的显示顺序（卡片上的 program_id 列表）。"""
        return [getattr(card, "program_id", None) for card in self._groups.get(group, ())]

    def dragging(self) -> bool:
        return self._card is not None

    # --------------------------------------------------------------- 事件过滤
    def eventFilter(self, obj, event) -> bool:      # noqa: N802（Qt 命名）
        card = self._holds.get(obj)
        if card is None:
            return False
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress:
            if event.button() != Qt.LeftButton:
                return False
            self._begin(card, event.globalPosition().toPoint())
            return True
        if kind == QEvent.Type.MouseMove and self._card is card:
            self._last = event.globalPosition().toPoint()
            self._move_float()
            self._reorder()
            return True
        if kind == QEvent.Type.MouseButtonRelease and self._card is card:
            self._finish(commit=True)
            return True
        return False

    # ------------------------------------------------------------------ 拖动
    def _begin(self, card: QWidget, global_pos: QPoint) -> None:
        group = self._group_of.get(card, "")
        if not group or self._card is not None:
            return
        try:
            holder = card.parentWidget()
        except RuntimeError:
            return
        if holder is None:
            return
        cards = list(self._groups.get(group) or [])
        self._card = card
        self._group = group
        self._holder = holder
        self._last = global_pos
        self._origin = list(cards)
        self._slot_order = list(cards)
        self._grab_size = card.size()
        self._press_offset = global_pos - card.mapToGlobal(QPoint(0, 0))
        self._slots = {item: item.geometry() for item in cards}
        self._lift_progress = 0.0
        self._sync_handle(card, active=True)
        self._build_placeholder(card)
        self._float_card(card)
        self._lift(1.0)
        self._timer.start()

    def _build_placeholder(self, card: QWidget) -> None:
        """在卡片原来的槽位放一个等高虚线框，保证布局不塌、别的卡片不跳。"""
        holder = self._holder
        layout = holder.layout() if holder is not None else None
        frame = QFrame(holder)
        frame.setObjectName("CardSlot")
        frame.setFixedHeight(max(config.sx(24), card.height()))
        edge = QColor(config.C_PRIMARY)
        fill = QColor(config.C_PRIMARY)
        fill.setAlpha(26)
        frame.setStyleSheet(
            "#CardSlot { background: rgba(%d, %d, %d, %d);"
            " border: 1px dashed rgba(%d, %d, %d, 150); border-radius: %dpx; }"
            % (fill.red(), fill.green(), fill.blue(), fill.alpha(),
               edge.red(), edge.green(), edge.blue(), config.sx(13)))
        if layout is not None:
            index = layout.indexOf(card)
            layout.insertWidget(index if index >= 0 else 0, frame)
        frame.show()
        self._placeholder = frame
        self._slots[frame] = frame.geometry()

    def _float_card(self, card: QWidget) -> None:
        """把卡片挪到视口上层跟着光标走（布局里由占位框顶着它的位置）。"""
        try:
            viewport = self._scroll.viewport()
            layout = self._holder.layout() if self._holder is not None else None
            if layout is not None:
                layout.removeWidget(card)
            card.setParent(viewport)
            card.show()
            card.raise_()
        except RuntimeError:
            return
        self._move_float()

    def _move_float(self) -> None:
        """按"光标位置 - 抓取偏移"摆放浮层，并按抬起进度轻微放大。"""
        card = self._card
        if card is None:
            return
        try:
            origin = self._scroll.viewport().mapToGlobal(QPoint(0, 0))
        except RuntimeError:
            return
        width, height = self._grab_size.width(), self._grab_size.height()
        grow = LIFT_SCALE - 1.0
        extra_x = int(round(width * grow * self._lift_progress))
        extra_y = int(round(height * grow * self._lift_progress))
        x = self._last.x() - origin.x() - self._press_offset.x() - extra_x // 2
        y = self._last.y() - origin.y() - self._press_offset.y() - extra_y // 2
        try:
            card.setGeometry(x, y, width + extra_x, height + extra_y)
        except RuntimeError:
            pass

    def _lift(self, target: float) -> None:
        """抬起 / 放下：阴影加深与轻微放大同步渐变。"""
        card = self._card
        if card is None:
            return
        if self._lift_anim is not None:
            try:
                self._lift_anim.stop()
            except RuntimeError:
                pass
            self._lift_anim = None
        start = float(self._lift_progress)
        if abs(start - float(target)) < 0.01:
            return
        animation = QVariantAnimation(self)
        animation.setDuration(LIFT_MS)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.setStartValue(start)
        animation.setEndValue(float(target))

        def _on_value(value) -> None:
            self._lift_progress = float(value)
            current = self._card
            if current is None:
                return
            setter = getattr(current, "set_lift", None)
            if callable(setter):
                try:
                    setter(self._lift_progress)
                except RuntimeError:
                    return
            self._move_float()

        animation.valueChanged.connect(_on_value)
        animation.start(QAbstractAnimation.DeleteWhenStopped)
        self._lift_anim = animation

    def _finish(self, commit: bool) -> None:
        card = self._card
        if card is None:
            return
        self._timer.stop()
        self._card = None
        group = self._group
        self._group = ""
        self._sync_handle(card, active=False)
        ids = [i for i in self.order(group) if isinstance(i, int)] if group else []
        self._settle(card, commit)
        if group and not commit:
            self._groups[group] = list(self._origin)
        if commit and group and ids:
            self._on_commit(group, ids)

    def _settle(self, card: QWidget, commit: bool) -> None:
        """松手：浮层落回占位框（提交）或立刻还原（取消）。"""
        holder = self._holder
        placeholder = self._placeholder
        self._placeholder = None
        self._holder = None
        self._stop_slides()
        if self._lift_anim is not None:
            try:
                self._lift_anim.stop()
            except RuntimeError:
                pass
            self._lift_anim = None
        self._lift_progress = 0.0
        setter = getattr(card, "set_lift", None)
        if callable(setter):
            try:
                setter(0.0)
            except RuntimeError:
                pass
        self._pending = (card, placeholder, holder, not commit)
        if commit and placeholder is not None and holder is not None:
            try:
                animation = QPropertyAnimation(card, b"geometry", card)
                animation.setDuration(DROP_MS)
                animation.setEasingCurve(QEasingCurve.OutCubic)
                animation.setStartValue(card.geometry())
                animation.setEndValue(self._placeholder_rect(placeholder))
                animation.finished.connect(self._finish_drop)
                self._drop_anim = animation
                animation.start(QAbstractAnimation.DeleteWhenStopped)
                return
            except RuntimeError:
                pass
        self._finish_drop()

    def _placeholder_rect(self, placeholder: QWidget) -> QRect:
        """占位框在视口坐标系里的矩形（浮层与占位框分属两个父控件）。"""
        holder = placeholder.parentWidget()
        viewport = self._scroll.viewport()
        top_left = placeholder.geometry().topLeft()
        if holder is not None:
            top_left = holder.mapTo(viewport, top_left)
        return QRect(top_left, placeholder.size())

    def _finish_drop(self) -> None:
        """落回动画结束（或立即）后：占位框换回真卡片并收尾。"""
        pending = self._pending
        self._pending = None
        self._drop_anim = None
        if pending is None:
            return
        card, placeholder, holder, revert = pending
        try:
            self._restore(card, placeholder, holder, bool(revert))
        except RuntimeError:                 # 卡片已经被销毁（重建页面）
            pass

    def _restore(self, card: QWidget, placeholder: Optional[QWidget],
                 holder: Optional[QWidget], revert: bool) -> None:
        layout = holder.layout() if holder is not None else None
        if layout is not None:
            if placeholder is not None:
                index = layout.indexOf(placeholder)
                layout.removeWidget(placeholder)
                layout.insertWidget(index if index >= 0 else 0, card)
            else:
                layout.addWidget(card)
        card.setParent(holder)
        setter = getattr(card, "set_lift", None)
        if callable(setter):
            setter(0.0)
        card.show()
        if placeholder is not None:
            placeholder.hide()
            placeholder.setParent(None)
            placeholder.deleteLater()
        if revert and layout is not None and self._origin:
            self._arrange([item for item in self._origin], holder)
        if layout is not None:
            layout.activate()
        self._slots = {}
        self._slot_order = []
        self._lift_progress = 0.0

    def _abort(self) -> None:
        """立刻结束进行中的拖动（不播放落回动画），用于重建卡片 / 收尾。"""
        self._timer.stop()
        if self._lift_anim is not None:
            try:
                self._lift_anim.stop()
            except RuntimeError:
                pass
            self._lift_anim = None
        if self._drop_anim is not None:
            try:
                self._drop_anim.stop()
            except RuntimeError:
                pass
            self._drop_anim = None
        self._stop_slides()
        card = self._card
        self._card = None
        self._group = ""
        if self._pending is not None:
            self._finish_drop()              # 已经把 _pending 交给还原逻辑
        elif card is not None:
            placeholder = self._placeholder
            holder = self._holder
            self._placeholder = None
            self._holder = None
            self._sync_handle(card, active=False)
            try:
                self._restore(card, placeholder, holder, True)
            except RuntimeError:
                pass

    # ------------------------------------------------------------------ 排序
    def _reorder(self) -> None:
        card = self._card
        group = self._group
        cards = self._groups.get(group)
        placeholder = self._placeholder
        holder = self._holder
        if card is None or placeholder is None or holder is None or not cards or len(cards) < 2:
            return
        rest = [item for item in cards if item is not card]
        y = self._last.y()
        index = 0
        for other in rest:
            slot = self._slots.get(other)
            if slot is None:
                continue
            try:
                top = holder.mapToGlobal(slot.topLeft()).y()
            except RuntimeError:
                return
            if y > top + slot.height() / 2.0:
                index += 1
        order = rest[:index] + [placeholder] + rest[index:]
        if order == self._slot_order:
            return
        if self._apply(order):
            self._slot_order = list(order)
            self._groups[group] = [card if item is placeholder else item for item in order]

    def _apply(self, order: list[QWidget]) -> bool:
        """让位：布局换位后把卡片从动画起点滑到新槽位。"""
        holder = self._holder
        if holder is None:
            return False
        before = {item: item.geometry() for item in order if item is not self._card}
        if not self._arrange(order, holder):
            return False
        targets = {}
        for item in order:
            if item is self._card:
                continue
            try:
                targets[item] = item.geometry()
            except RuntimeError:
                return False
        for item, rect in before.items():
            try:
                item.setGeometry(rect)       # 先放回动画起点，否则会看到瞬移
            except RuntimeError:
                return False
        self._slots.update(targets)
        self._start_slides(before, targets)
        return True

    def _arrange(self, order: list[QWidget], holder: QWidget) -> bool:
        """把这组控件按 ``order`` 塞回布局中原本属于它们的槽位（分区标题、弹簧不动）。"""
        layout = holder.layout() if holder is not None else None
        if layout is None or not order:
            return False
        members = set(order)
        positions = [i for i in range(layout.count()) if layout.itemAt(i).widget() in members]
        if len(positions) != len(order):
            return False
        for item in order:
            layout.removeWidget(item)
        for index, item in zip(positions, order):
            layout.insertWidget(index, item)
        layout.activate()
        return True

    def _start_slides(self, before: dict, targets: dict) -> None:
        for item, rect in targets.items():
            start = before.get(item)
            if start is None or start == rect:
                continue
            self._stop_slide(item)
            try:
                animation = QPropertyAnimation(item, b"geometry", item)
                animation.setDuration(SLIDE_MS)
                animation.setEasingCurve(QEasingCurve.OutCubic)
                animation.setStartValue(start)
                animation.setEndValue(rect)
                animation.finished.connect(lambda card=item: self._slides.pop(card, None))
                animation.start(QAbstractAnimation.DeleteWhenStopped)
            except RuntimeError:
                continue
            self._slides[item] = animation

    def _stop_slide(self, item: QWidget) -> None:
        animation = self._slides.pop(item, None)
        if animation is None:
            return
        try:
            animation.stop()
        except RuntimeError:
            pass

    def _stop_slides(self) -> None:
        for animation in list(self._slides.values()):
            try:
                animation.stop()
            except RuntimeError:
                pass
        self._slides.clear()

    @staticmethod
    def _sync_handle(card: QWidget, active: bool) -> None:
        handle = getattr(card, "drag_handle", None)
        if handle is None:
            return
        try:
            handle.setCursor(QCursor(Qt.ClosedHandCursor if active else Qt.OpenHandCursor))
            handle.set_icon("grip", config.C_PRIMARY if active else config.C_TEXT_MUTED)
        except RuntimeError:
            pass

    # -------------------------------------------------------------- 边缘滚动
    def _on_tick(self) -> None:
        if self._card is None:
            return
        step = self._edge_step()
        if not step:
            return
        bar = self._scroll.verticalScrollBar()
        value = max(bar.minimum(), min(bar.maximum(), bar.value() + step))
        if value == bar.value():
            return
        bar.setValue(value)
        self._move_float()               # 列表滚动了，浮层要重新跟住光标
        self._reorder()

    def _edge_step(self) -> int:
        """光标越靠近上下边缘，滚动越快；返回本帧要滚动的像素（上负下正）。"""
        try:
            viewport = self._scroll.viewport()
            top = viewport.mapToGlobal(QPoint(0, 0)).y()
            height = viewport.height()
        except RuntimeError:
            return 0
        edge = max(1, config.sx(EDGE_PX))
        limit = float(max(1, config.sx(MAX_STEP)))
        y = self._last.y()
        if y < top + edge:
            ratio = (top + edge - y) / float(edge)
            return -int(max(1.0, min(limit, ratio * limit)))
        bottom = top + height - edge
        if y > bottom:
            ratio = (y - bottom) / float(edge)
            return int(max(1.0, min(limit, ratio * limit)))
        return 0
