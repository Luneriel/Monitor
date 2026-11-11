"""日志页：按时间排序 / 按程序分类，并支持选中删除运行记录。

删除流程与首页的"删除"完全一致：
    进入删除模式（横栏切换为 全选 / 取消全选 / 取消 / 确认删除）
    -> 每条记录左侧出现圆形复选框，点击记录任意位置即可勾选
    -> 确认删除时弹窗列出所有被选中的记录并提示无法撤回
    -> 删除后写入 SQLite 并刷新首页时间信息。
"""

from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFontMetrics, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QSizePolicy, QVBoxLayout, QWidget)

from .. import config, icon_extract, util
from ..i18n import tr
from . import anim, theme, vector_icons
from .tooltip_guard import suppress_tooltips
from .controls import (CircleCheck, Divider, EmptyState, IconButton, IconTile, Panel,
                       SlimScrollArea, TextButton)
from .dialogs import ConfirmDeleteLogsDialog
from .popups import MenuPopup, ProgramDropdown, SelectBox

MODE_TIME = "time"
MODE_PROGRAM = "program"
MODE_DATE = "date"
MODE_KEYS = {MODE_TIME: "logs.mode.time", MODE_PROGRAM: "logs.mode.program",
             MODE_DATE: "logs.mode.date"}

# 控制类日志的 kind -> 展示文案键
CONTROL_KIND_LABELS = {
    "daily_limit": "control.log.daily_limit",
    "unlock": "control.log.unlocked",
    "pause": "control.log.pause",
}

# 日志分页：每页基础 200 条；某一天自己就超过 200 条时该页只放这一天且全部放下
# （最多 500 条），超过 500 条再强制按 500 条切开。装不下的那一天整天挪到下一页，
# 所以页尾永远停在日期边界上，日期分隔条不会被拆到两页。
PAGE_SIZE = 200
PAGE_DAY_MAX = 500

WEEKDAYS_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
WEEKDAYS_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def mode_label(mode: str) -> str:
    return tr(MODE_KEYS.get(mode, "logs.mode.time"))


def day_separator_text(ts: float) -> str:
    """日期分隔条文案：2026-09-21 星期六 / 2026-09-21 Sat。"""
    local = time.localtime(ts)
    date = time.strftime("%Y-%m-%d", local)
    weekday = WEEKDAYS_ZH[local.tm_wday] if tr("logs.mode.date") == "按时间分类" \
        else WEEKDAYS_EN[local.tm_wday]
    return f"{date}  {weekday}"


def build_pages(day_counts: list, page_size: int = PAGE_SIZE,
                single_max: int = PAGE_DAY_MAX) -> list[list[tuple[str, int, int]]]:
    """把"每天多少条"切成页；每页是 ``[(日期, 本页该天条数, 该天内的起始偏移), ...]``。

    * 基础每页 ``page_size`` 条：一天整天装进当前页，装不下就整天挪到下一页；
    * 某天自己就超过 ``page_size`` 条 -> 该页只放这一天且全部放下（最多 ``single_max`` 条）；
    * 某天超过 ``single_max`` 条 -> 强制按 ``single_max`` 条分页。

    纯函数：不碰数据库、不建控件，因此自检可以直接拿造出来的"每天多少条"验证分页规则。
    """
    pages: list[list[tuple[str, int, int]]] = []
    current: list[tuple[str, int, int]] = []
    used = 0

    def flush() -> None:
        nonlocal current, used
        if current:
            pages.append(current)
        current = []
        used = 0

    for entry in day_counts or ():
        try:
            day = str(entry[0])
            count = int(entry[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not day or count <= 0:
            continue
        if count > single_max:
            flush()
            offset = 0
            while offset < count:
                take = min(single_max, count - offset)
                pages.append([(day, take, offset)])
                offset += take
            continue
        if count > page_size:
            flush()
            pages.append([(day, count, 0)])
            continue
        if used + count > page_size:
            flush()                     # 这一天整天挪到下一页，页尾停在日期边界上
        current.append((day, count, 0))
        used += count
    flush()
    return pages


class ClickableFrame(QFrame):
    """整条可点击的横栏。"""

    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def _log_row_at(global_pos: QPoint) -> Optional["LogRow"]:
    """全局坐标下那一行的 ``LogRow``（没有就返回 None）。

    拖动选中时鼠标可能已经离开按下那一行，所以每帧都要重新问一次"光标现在在哪一行"。
    """
    widget = QApplication.widgetAt(global_pos)
    while widget is not None and not isinstance(widget, LogRow):
        widget = widget.parentWidget()
    return widget if isinstance(widget, LogRow) else None


class LogRow(QWidget):
    """一条运行日志（放在白色面板中，行间以分隔线相连）。"""

    selection_toggled = Signal(int, bool)
    drag_started = Signal(int, bool)      # log_id, 本次拖动要套用的状态
    drag_moved = Signal(QPoint)           # 全局坐标：给页面做边缘自动滚动
    drag_finished = Signal()

    def __init__(self, log_id: int, name: str, icon_path: Optional[str], exe_path: str,
                 start_ts: float, end_ts: Optional[float], show_name: bool = True,
                 delete_mode: bool = False, selected: bool = False,
                 parent: Optional[QWidget] = None, message: str = "",
                 kind: str = "run", site: bool = False) -> None:
        super().__init__(parent)
        self.log_id = int(log_id)
        self._start_ts = float(start_ts)
        self.kind = kind or "run"
        self.message = message or ""
        self.setObjectName("LogRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._delete_mode = bool(delete_mode)
        self._selected = bool(selected)
        self._dragging = False               # 按住左键滑动选中中
        self._eliding = False                # 正在补布局（挡住 _elide_name 递归）

        layout = QHBoxLayout(self)
        layout.setContentsMargins(config.sx(12), config.sx(9), config.sx(12), config.sx(9))
        layout.setSpacing(config.sx(9))
        self._layout = layout

        self.check = CircleCheck(self._selected, self)
        self.check.setVisible(self._delete_mode)
        layout.addWidget(self.check, 0, Qt.AlignVCenter)

        if show_name:
            tile = IconTile(32, self)
            if site:
                tile.setPixmap(vector_icons.pixmap("globe", config.sx(20), config.C_ACCENT))
            else:
                tile.setPixmap(icon_extract.pixmap_for_program(icon_path, exe_path, config.sx(20)))
            layout.addWidget(tile, 0, Qt.AlignVCenter)

            name_label = QLabel()
            name_label.setObjectName("LogRowName")
            name_label.setFont(theme.font(config.FONT_BODY, bold=True))
            name_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            name_label.setToolTip(name)
            self._name_text = name
            layout.addWidget(name_label, 0, Qt.AlignVCenter)
        else:
            name_label = None
            self._name_text = name
        self._name_label = name_label

        times = QWidget(self)
        times_layout = QHBoxLayout(times)
        times_layout.setContentsMargins(0, 0, 0, 0)
        times_layout.setSpacing(config.sx(8))
        self._time_widgets: list = []

        # 起止时间：省略"开始/结束"字样（箭头已表达先后），完整时间放在 tooltip 中，
        # 这样在窄窗口下名称列与时长胶囊都不会被挤出可视区。
        start_label = QLabel(util.format_short(start_ts))
        start_label.setObjectName("LogRowTime")
        start_label.setFont(theme.font(config.FONT_SMALL))
        start_label.setToolTip(tr("dialog.time.start", time=util.format_ts(start_ts)))
        times_layout.addWidget(start_label)
        self._time_widgets.append(start_label)

        arrow = QLabel("→")
        arrow.setObjectName("LogRowArrow")
        arrow.setFont(theme.font(config.FONT_SMALL))
        times_layout.addWidget(arrow)
        self._time_widgets.append(arrow)

        # "运行中"胶囊与结束时间两个控件都建好，只切换可见性 ——
        # 这样日志结束时可以就地更新这一行（见 update_record），不必重建整页。
        chip = QLabel(tr("common.running"))
        chip.setObjectName("LogRowChip")
        chip.setFont(theme.font(config.FONT_TINY, bold=True))
        if end_ts:
            end_label = QLabel(util.format_short(end_ts))
            end_label.setObjectName("LogRowTime")
            end_label.setFont(theme.font(config.FONT_SMALL))
            end_label.setToolTip(tr("dialog.time.end", time=util.format_ts(end_ts)))
            times_layout.addWidget(end_label)
            chip.hide()
        else:
            end_label = None
            times_layout.addWidget(chip)
        self._time_widgets.append(chip)
        self._end_label = end_label

        times_layout.addStretch(1)
        layout.addWidget(times, 1)
        self._times = times

        duration = None
        if end_ts:
            duration = QLabel(util.format_duration(float(end_ts) - float(start_ts)))
            duration.setObjectName("LogRowDuration")
            duration.setFont(theme.font(config.FONT_TINY, bold=True))
            layout.addWidget(duration, 0, Qt.AlignVCenter)
        self._duration = duration

        # 控制类日志：不显示起止时间与时长，只显示时间点 + 提示文字
        if self.message:
            for widget in self._time_widgets[1:]:
                widget.hide()
            start_label.setText(f"{util.format_short(start_ts)}   {self.message}")
            start_label.setToolTip(self.message)
            if duration is not None:
                duration.hide()

        self._apply_state()

    # ------------------------------------------------------------------
    def set_delete_mode(self, enabled: bool) -> None:
        self._delete_mode = bool(enabled)
        self.check.setVisible(self._delete_mode)
        self.setCursor(QCursor(Qt.PointingHandCursor if self._delete_mode else Qt.ArrowCursor))
        self._elide_name()
        self._apply_state()

    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self.check.setChecked(self._selected)
        self._apply_state()

    def is_selected(self) -> bool:
        return self._selected

    def update_record(self, end_ts: Optional[float], message: str = "") -> None:
        """就地刷新这一行的时间信息（不重建控件）。

        正在运行的那条日志每条 tick 都在变（时长涨、结束时从"运行中"变成结束时间），
        以前只能整页拆掉重建；现在只改这几个标签的文字。
        """
        start_label = self._time_widgets[0] if self._time_widgets else None
        if start_label is None:
            return
        if self.kind != "run" or self.message or message:
            text = str(message or self.message or "")
            if text:
                start_label.setText(f"{util.format_short(self._start_ts)}   {text}")
            return
        if end_ts:
            if self._end_label is None:
                label = QLabel(util.format_short(end_ts))
                label.setObjectName("LogRowTime")
                label.setFont(theme.font(config.FONT_SMALL))
                self._times.layout().insertWidget(1, label)
                self._end_label = label
                self._time_widgets.insert(2, label)
            label = self._end_label
            label.setText(util.format_short(end_ts))
            label.setToolTip(tr("dialog.time.end", time=util.format_ts(end_ts)))
            label.show()
            if len(self._time_widgets) > 2:
                self._time_widgets[2].hide()      # "运行中"胶囊
            if self._duration is None:
                duration = QLabel(util.format_duration(float(end_ts) - float(self._start_ts)))
                duration.setObjectName("LogRowDuration")
                duration.setFont(theme.font(config.FONT_TINY, bold=True))
                self.layout().addWidget(duration, 0, Qt.AlignVCenter)
                self._duration = duration
            else:
                self._duration.setText(
                    util.format_duration(float(end_ts) - float(self._start_ts)))
        else:
            if self._end_label is not None:
                self._end_label.hide()
            if len(self._time_widgets) > 2:
                self._time_widgets[2].show()      # 回到"运行中"
            if self._duration is not None:
                self._duration.hide()

    def _apply_state(self) -> None:
        self.setProperty("selected", self._selected)
        theme.repolish(self)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._delete_mode and event.button() == Qt.LeftButton:
            # 按下即选中/取消，并进入"滑动选中"：按住左键划过别的行会一起改，
            # 具体交给 LogsPage（它还要负责拖到上下边缘时自动滚动）。
            self._dragging = True
            value = not self._selected
            self.set_selected(value)
            self.selection_toggled.emit(self.log_id, value)
            self.drag_started.emit(self.log_id, value)
            self.drag_moved.emit(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            # 按下后 Qt 会把鼠标事件都送到这一行（隐式抓取），所以这里只需要把
            # 光标位置转告页面；页面自己用 _log_row_at() 问"现在压着哪一行"。
            self.drag_moved.emit(event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._dragging and event.button() == Qt.LeftButton:
            self._dragging = False
            self.drag_finished.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide_name()

    def _elide_name(self) -> None:
        """名称列宽度 = 行宽扣除复选框 / 图标 / 起止时间 / 时长后剩下的部分。"""
        if self._name_label is None:
            return
        spacing = self._layout.spacing()
        margins = self._layout.contentsMargins()
        reserved = (margins.left() + margins.right() + spacing * 4
                    + (self.check.sizeHint().width() if self._delete_mode else 0)
                    + config.sx(32)
                    + self._times.sizeHint().width()
                    + (self._duration.sizeHint().width() if self._duration is not None else 0))
        width = max(config.sx(48), min(config.sx(200), self.width() - reserved))
        fm = QFontMetrics(self._name_label.font())
        elided = fm.elidedText(self._name_text, Qt.ElideMiddle, width)
        target = max(config.sx(44), min(width, fm.horizontalAdvance(elided) + config.sx(4)))
        self._name_label.setText(elided)
        changed = self._name_label.width() != target
        self._name_label.setFixedWidth(target)
        if changed and not self._eliding:
            # setFixedWidth() 只改本标签的宽度，同一轮布局已经给后面的时间列算好位置了；
            # 不主动再跑一轮，时间列就会停在"名称只有几像素宽"时的位置上，被名称压住一截
            # （实测首屏会出现 "10-10 16:" 被盖掉、只剩 "30:00"）。这里补一次同步布局，
            # 用 _eliding 挡住递归。
            self._eliding = True
            try:
                self._layout.activate()
            finally:
                self._eliding = False


class PageBar(QWidget):
    """日志页底部悬浮的选页框（磨砂玻璃）。

    钉在页面底部不动；自绘圆角 + 柔和阴影，并把**背后那条像素**抓来做一次廉价
    模糊（缩小 10 倍再放大）垫在底下，于是列表从下面滑过时能看到透底的磨砂效果。
    不用 QGraphicsDropShadowEffect（它在内容会变的控件上有缓存不失效的老问题）。
    """

    goto_requested = Signal(int)

    def __init__(self, source: Optional[QWidget], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._source = source                 # 背后要被模糊的那块（滚动视口）
        self.page = 0
        self.page_count = 1
        self._backdrop: Optional[QPixmap] = None
        self._pad = max(2, config.sx(5))
        self._radius = config.sx(12)
        self.setFixedHeight(config.sx(44))
        self.setAttribute(Qt.WA_StyledBackground, False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(config.sx(10), 0, config.sx(10), 0)
        layout.setSpacing(config.sx(4))

        self.prev_btn = IconButton("chevron_left", tr("logs.page.prev"), size=28, icon_size=15,
                                   parent=self, flat=True)
        self.prev_btn.clicked.connect(lambda: self._step(-1))
        layout.addWidget(self.prev_btn, 0, Qt.AlignVCenter)

        self.label = QLabel("")
        self.label.setFont(theme.font(config.FONT_SMALL, bold=True))
        self.label.setStyleSheet(f"color: {config.C_TEXT_BODY}; background: transparent;")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setMinimumWidth(config.sx(104))
        layout.addWidget(self.label, 0, Qt.AlignVCenter)

        # 页码直选：点一下页码就地变数字输入框。输入框是浮在标签上的独立子控件、
        # 不进布局，所以编辑时选页框的宽度与位置纹丝不动。
        self._editing = False
        self.label.setCursor(Qt.PointingHandCursor)
        self.label.setToolTip(tr("logs.page.goto"))
        self.label.installEventFilter(self)
        self.editor = QLineEdit(self)
        self.editor.setObjectName("LogsPageEditor")
        self.editor.setFont(theme.font(config.FONT_SMALL, bold=True))
        self.editor.setAlignment(Qt.AlignCenter)
        self.editor.setFixedWidth(config.sx(64))
        self.editor.setVisible(False)
        self.editor.installEventFilter(self)
        self.editor.editingFinished.connect(self.commit_edit)

        self.next_btn = IconButton("chevron_right", tr("logs.page.next"), size=28, icon_size=15,
                                   parent=self, flat=True)
        self.next_btn.clicked.connect(lambda: self._step(1))
        layout.addWidget(self.next_btn, 0, Qt.AlignVCenter)

    # ------------------------------------------------------------------
    def set_page(self, page: int, page_count: int) -> None:
        self.page = max(0, int(page))
        self.page_count = max(1, int(page_count))
        self.label.setText(tr("logs.page.label", current=self.page + 1, total=self.page_count))
        self.prev_btn.setEnabled(self.page > 0)
        self.next_btn.setEnabled(self.page < self.page_count - 1)
        self.prev_btn.set_icon("chevron_left",
                               config.C_TEXT_BODY if self.page > 0 else config.C_TEXT_FAINT)
        self.next_btn.set_icon("chevron_right",
                               config.C_TEXT_BODY if self.page < self.page_count - 1
                               else config.C_TEXT_FAINT)
        self._backdrop = None
        self.update()

    def _step(self, delta: int) -> None:
        target = self.page + int(delta)
        if 0 <= target < self.page_count:
            self.goto_requested.emit(target)

    # ------------------------------------------------------------------
    # 页码直选（点页码 -> 就地输入）
    # ------------------------------------------------------------------
    def begin_edit(self) -> None:
        if self._editing or self.page_count <= 1:
            return
        self._editing = True
        self.editor.setGeometry(self.label.geometry())
        self.editor.setText(str(self.page + 1))
        self.editor.setVisible(True)
        self.editor.raise_()
        self.editor.setFocus(Qt.MouseFocusReason)
        self.editor.selectAll()

    def commit_edit(self) -> None:
        """回车 / 失焦：越界夹到范围内，填了非数字就当作没改。"""
        if not self._editing:
            return
        self._editing = False
        text = self.editor.text().strip()
        self.editor.setVisible(False)
        try:
            value = int(text)
        except ValueError:
            value = self.page + 1
        value = max(1, min(value, self.page_count))
        if value - 1 != self.page:
            self.goto_requested.emit(value - 1)

    def cancel_edit(self) -> None:
        self._editing = False
        self.editor.setVisible(False)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.label and event.type() == QEvent.MouseButtonRelease:
            self.begin_edit()
        elif obj is self.editor and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                self.cancel_edit()
                return True
        return super().eventFilter(obj, event)

    def refresh_backdrop(self) -> None:
        """背后的列表动了（滚动 / 换页 / 换主题）：丢掉缓存的模糊底，重画一次。"""
        self._backdrop = None
        if self.isVisible():
            self.update()

    def moveEvent(self, event) -> None:  # noqa: N802
        # 换位置后缓存里的像素已经是"别人家"的了：必须重抓。否则旧图会被按
        # 左上角对齐贴到新位置上，看起来就是"磨砂只生效了一小半区域"。
        super().moveEvent(event)
        self._backdrop = None

    # ------------------------------------------------------------------
    def _panel_rect(self) -> QRectF:
        pad = self._pad
        return QRectF(self.rect()).adjusted(pad, pad, -pad, -pad)

    def _blurred_backdrop(self) -> Optional[QPixmap]:
        if self._backdrop is not None:
            return self._backdrop
        source = self._source
        if source is None:
            return None
        rect = self._panel_rect()
        size = QSize(max(1, int(rect.width())), max(1, int(rect.height())))
        # 视口不是本控件的祖先（选页框挂在页面本体上），所以用全局坐标换算，
        # 不能直接 mapTo(视口)：Qt 会打 "parent must be in parent hierarchy"。
        origin = self.mapToGlobal(rect.topLeft().toPoint()) - source.mapToGlobal(QPoint(0, 0))
        src = QRect(origin, size).intersected(source.rect())
        if src.width() < 3 or src.height() < 3:
            return None
        shot = source.grab(src)
        if shot.isNull():
            return None
        small = shot.scaled(max(1, src.width() // 10), max(1, src.height() // 10),
                            Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        # 缩小再放大 = 廉价模糊；QPixmap.scaled() 返回的还是 QPixmap
        self._backdrop = small.scaled(src.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        return self._backdrop

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self._panel_rect()
        theme.paint_soft_shadow(painter, rect, self._radius, spread=max(2, self._pad),
                                dy=2.0, alpha=54)
        path = QPainterPath()
        path.addRoundedRect(rect, self._radius, self._radius)
        painter.save()
        painter.setClipPath(path)
        try:
            backdrop = self._blurred_backdrop()
        except Exception:
            backdrop = None          # 抓不到背后的像素就退化成纯半透明底
        if backdrop is not None:
            # 拉伸铺满整块面板：抓到的那块像素可能比面板小（被视口裁过），
            # 按原尺寸左上角对齐贴就会只糊住一角。
            painter.drawPixmap(rect.toRect(), backdrop)
        glass = QColor(config.C_SURFACE)
        glass.setAlphaF(0.74)
        painter.fillPath(path, glass)
        painter.restore()
        border = QColor(config.C_BORDER)
        border.setAlpha(150)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(border)
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), self._radius, self._radius)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._backdrop = None


class LogsPage(QWidget):
    """日志页面。"""

    data_changed = Signal()

    DRAG_EDGE_PX = 46        # 光标进入视口上下边缘这个范围内就开始自动滚动
    DRAG_MAX_STEP = 26       # 每一帧最大滚动像素（越靠边越快）
    DRAG_TICK_MS = 24        # 自动滚动的帧间隔

    def __init__(self, db, tracker, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.db = db
        self.tracker = tracker
        self.mode = MODE_TIME
        self.selected_program_id: Optional[int] = None
        self.delete_mode = False
        self.selected: set[int] = set()
        self.records: list[dict] = []
        # 分页：只在需要时把"这一页"的日志读回来。以前一次读 3000 条、建近 3000 个控件，
        # 而且每 2 秒重来一遍，所以进日志页会卡很久、内存一路涨到 500MB。
        self.page = 0
        self.page_count = 1
        self.total_records = 0
        self._pages: list[list[tuple[str, int, int]]] = []
        self._signature: Optional[tuple] = None      # 本次算出来的数据指纹
        self._rendered: Optional[tuple] = None       # 当前屏幕上是哪一份指纹
        self._days_rendered: Optional[tuple] = None  # 屏幕上这份数据对应的"有日志的日期"集合
        self._days: tuple = ()                       # 全库里"有日志的日期"（供日期筛选条用）
        self._ids: Optional[tuple] = None            # 屏幕上这一页的 log_id 顺序
        self._ids_rendered: Optional[tuple] = None   # 真正渲染出来的那一页 log_id（就地更新的判据）
        self._mode_rendered: Optional[str] = None    # 渲染那份数据时的模式
        self._row_fingerprint: tuple = ()            # 屏幕上这一页的 (log_id, end_time)
        self._pending_rows: list = []                # 已从布局摘下来、等待销毁/复用的控件
        self.rows: dict[int, LogRow] = {}
        self._animating = False
        # 滑动选中：按住左键划过若干行会连续改状态；拖到视口上下边缘时自动滚动，
        # 这样一屏装不下的日志也能一次拖到底。
        self._drag_value = True
        self._drag_anchor = 0            # 按下的那一行（拖过的整段都要套用它）
        self._drag_pos: Optional[QPoint] = None
        self._drag_step = 0
        self._dragging = False           # 正在滑动选中（期间不要重建列表）
        # 按下那一刻每一行的状态：滑过头再滑回来要按原样恢复
        self._drag_before: dict[int, bool] = {}
        self._drag_timer = QTimer(self)
        self._drag_timer.setInterval(self.DRAG_TICK_MS)
        self._drag_timer.timeout.connect(self._drag_scroll_step)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_toolbar())
        self.header = self._build_header()
        root.addWidget(self.header)
        self.header.hide()
        self.date_header = self._build_date_header()
        root.addWidget(self.date_header)
        self.date_header.hide()

        self.scroll = SlimScrollArea(self)
        self.holder = QWidget()
        self.list_layout = QVBoxLayout(self.holder)
        self.list_layout.setContentsMargins(config.sx(22), config.sx(6), config.sx(22),
                                            config.sx(18))
        self.list_layout.setSpacing(0)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.holder)
        root.addWidget(self.scroll, 1)

        self.empty = EmptyState(tr("logs.empty.title"), "list", tr("logs.empty.hint"))
        self.empty.setParent(self.scroll.viewport())
        self.empty.hide()

        # 选页框钉在页面底部不动，浮在列表之上；磨砂底从它背后的视口像素抓。
        self.pager = PageBar(self.scroll.viewport(), self)
        self.pager.goto_requested.connect(self.goto_page)
        self.pager.hide()
        self._pager_visible = False   # 逻辑可见：页面还没显示时 pager.isVisible() 是 False
        self._pager_dirty = False     # 页面尚未被布局撑开，没能摆正，等排好再摆
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scrolled)

        self.menu_btn = IconButton("menu", tr("logs.view_tooltip"), size=34, icon_size=17)
        self.menu_btn.clicked.connect(self.open_mode_menu)

        self.tracker.programs_changed.connect(self.refresh)
        self.tracker.tick.connect(self._on_tick)
        self._tick_count = 0

    # ------------------------------------------------------------------
    # 工具栏（与首页一致：删除 -> 全选/取消全选 + 取消/确认删除）
    # ------------------------------------------------------------------
    def _build_toolbar(self) -> QWidget:
        bar = QWidget(self)
        bar.setFixedHeight(config.sx(54))
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(config.sx(22), config.sx(12), config.sx(22), config.sx(8))
        layout.setSpacing(config.sx(8))

        self.delete_btn = TextButton(tr("home.delete"), "danger", icon_name="trash", icon_size=14, height=34)
        self.delete_btn.clicked.connect(self.enter_delete_mode)

        self.select_all_btn = TextButton(tr("common.select_all"), "ghost", height=34)
        self.select_all_btn.clicked.connect(self.select_all)
        self.unselect_all_btn = TextButton(tr("common.unselect_all"), "ghost", height=34)
        self.unselect_all_btn.clicked.connect(self.clear_selection)
        self.unselect_all_btn.hide()

        self.cancel_delete_btn = TextButton(tr("common.cancel"), "ghost", height=34)
        self.cancel_delete_btn.clicked.connect(self.exit_delete_mode)
        self.confirm_delete_btn = TextButton(tr("common.confirm_delete"), "danger", height=34)
        self.confirm_delete_btn.clicked.connect(self.on_confirm_delete)

        self.count_label = QLabel("")
        self.count_label.setFont(theme.font(config.FONT_SMALL))
        self.count_label.setStyleSheet(f"color: {config.C_ON_CANVAS_MUTED}; background: transparent;")

        layout.addWidget(self.delete_btn)
        layout.addWidget(self.select_all_btn)
        layout.addWidget(self.unselect_all_btn)
        layout.addStretch(1)
        layout.addWidget(self.count_label)
        layout.addWidget(self.cancel_delete_btn)
        layout.addWidget(self.confirm_delete_btn)

        self._toolbar = bar
        self._apply_mode_visibility()
        return bar

    def _apply_mode_visibility(self) -> None:
        self.delete_btn.setVisible(not self.delete_mode)
        self.count_label.setVisible(bool(self.records))
        for widget in (self.select_all_btn, self.cancel_delete_btn, self.confirm_delete_btn):
            widget.setVisible(self.delete_mode)
        self.unselect_all_btn.setVisible(self.delete_mode and self._all_selected())

    def _all_selected(self) -> bool:
        ids = [record["log_id"] for record in self.records]
        return bool(ids) and all(log_id in self.selected for log_id in ids)

    # ------------------------------------------------------------------
    def topbar_widget(self) -> QWidget:
        return self.menu_btn

    def _sync_topbar_state(self) -> None:
        self.menu_btn.setToolTip(tr("logs.view_current", mode=mode_label(self.mode)))

    def _build_header(self) -> QWidget:
        bar = ClickableFrame(self)
        bar.setObjectName("Card")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        bar.setFixedHeight(config.sx(60))
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(config.sx(14), config.sx(9), config.sx(12), config.sx(9))
        layout.setSpacing(config.sx(12))

        self.header_tile = IconTile(38, bar)
        layout.addWidget(self.header_tile, 0, Qt.AlignVCenter)

        column = QWidget(bar)
        col = QVBoxLayout(column)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        self.header_name = QLabel(tr("logs.no_program"))
        self.header_name.setFont(theme.font(config.FONT_CARD_NAME, bold=True))
        self.header_name.setStyleSheet(f"color: {config.C_TEXT}; background: transparent;")
        col.addWidget(self.header_name)
        self.header_path = QLabel("")
        self.header_path.setFont(theme.font(config.FONT_SMALL))
        self.header_path.setStyleSheet(f"color: {config.C_TEXT_MUTED}; background: transparent;")
        col.addWidget(self.header_path)
        layout.addWidget(column, 1, Qt.AlignVCenter)

        self.header_arrow = IconButton("chevron_down", tr("logs.choose_program"), size=32, icon_size=16,
                                       flat=True)
        self.header_arrow.clicked.connect(self.open_program_dropdown)
        layout.addWidget(self.header_arrow, 0, Qt.AlignVCenter)

        bar.clicked.connect(self.open_program_dropdown)
        wrapper = QWidget(self)
        wrap_layout = QVBoxLayout(wrapper)
        wrap_layout.setContentsMargins(config.sx(22), config.sx(8), config.sx(22), 0)
        wrap_layout.setSpacing(0)
        wrap_layout.addWidget(bar)
        return wrapper

    # ------------------------------------------------------------------
    def _build_date_header(self) -> QWidget:
        """"按时间分类"的筛选条：年 / 月 / 日（月与日可选"全部"）。"""
        wrapper = QWidget(self)
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(config.sx(22), config.sx(8), config.sx(22), 0)
        layout.setSpacing(config.sx(8))

        self.year_select = SelectBox([], current="", parent=wrapper, min_width=110)
        self.month_select = SelectBox([], current="", parent=wrapper, min_width=90)
        self.day_select = SelectBox([], current="", parent=wrapper, min_width=90)
        for box in (self.year_select, self.month_select, self.day_select):
            box.current_changed.connect(self._on_date_changed)
            layout.addWidget(box)
        layout.addStretch(1)
        return wrapper

    def _refresh_date_options(self) -> None:
        """按"有日志的日期"重建 年/月/日 下拉项。

        ``self._days`` 由 ``_load_data`` 从 ``db.log_day_counts()``（**不带**日期筛选，
        否则选中某一年后其它年份就消失了）算出来，不再单独跑一次
        ``SELECT DISTINCT start_time`` 的全表查询。
        """
        parts = list(self._days)
        years = sorted({y for y, _m, _d in parts}, reverse=True)
        current_year = self.year_select.current_key()
        year_items = [(str(y), tr("logs.date.year", year=y)) for y in years]
        if year_items:
            self.year_select.set_options(year_items, keep=current_year or str(years[0]))
        months = sorted({m for y, m, _d in parts if str(y) == self.year_select.current_key()})
        month_items = [("", tr("logs.date.all"))] + [
            (str(m), tr("logs.date.month", month=m)) for m in months]
        self.month_select.set_options(month_items, keep=self.month_select.current_key())
        days = sorted({d for y, m, d in parts
                       if str(y) == self.year_select.current_key()
                       and str(m) == self.month_select.current_key()})
        day_items = [("", tr("logs.date.all"))] + [
            (str(d), tr("logs.date.day", day=d)) for d in days]
        self.day_select.set_options(day_items, keep=self.day_select.current_key())

    def _on_date_changed(self, _key: str = "") -> None:
        self._refresh_date_options()
        self.refresh(force=True)

    def _date_range(self) -> tuple[Optional[float], Optional[float]]:
        year = self.year_select.current_key()
        if not year:
            return None, None
        month = self.month_select.current_key()
        day = self.day_select.current_key()
        try:
            if month and day:
                start = time.mktime((int(year), int(month), int(day), 0, 0, 0, 0, 0, -1))
                return start, start + 86400
            if month:
                start = time.mktime((int(year), int(month), 1, 0, 0, 0, 0, 0, -1))
                if int(month) == 12:
                    end = time.mktime((int(year) + 1, 1, 1, 0, 0, 0, 0, 0, -1))
                else:
                    end = time.mktime((int(year), int(month) + 1, 1, 0, 0, 0, 0, 0, -1))
                return start, end
            start = time.mktime((int(year), 1, 1, 0, 0, 0, 0, 0, -1))
            return start, time.mktime((int(year) + 1, 1, 1, 0, 0, 0, 0, 0, -1))
        except (TypeError, ValueError, OverflowError):
            return None, None

    # ------------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        if self.delete_mode:
            self.exit_delete_mode()
        previous = self.mode
        self.mode = mode
        self._sync_topbar_state()
        if mode == MODE_DATE and previous != MODE_DATE:
            # 切到"按时间分类"时无条件把年/月/日刷新一遍：日期集合没变时 refresh() 里的
            # _days_changed() 判定会跳过它，而首次进入时下拉框必须马上有选项。
            self._refresh_date_options()
        self.refresh(force=True)

    def open_mode_menu(self) -> None:
        popup = MenuPopup(
            [(MODE_TIME, tr("logs.mode.time")), (MODE_PROGRAM, tr("logs.mode.program")),
             (MODE_DATE, tr("logs.mode.date"))],
            current=self.mode, parent=self, min_width=158,
        )
        popup.item_selected.connect(self.set_mode)
        popup.popup_at(self.menu_btn, align="right")

    def open_program_dropdown(self) -> None:
        programs = list(self.db.programs(include_deleted=True))
        if not programs:
            return
        window = self.window()
        max_height = int(window.height() * 0.7) if window else config.sx(400)
        popup = ProgramDropdown(programs, self, max_height=max_height)
        popup.program_selected.connect(self.select_program)
        popup.popup_at(self.header_arrow, align="right")
        if self.mode != MODE_PROGRAM:
            self.set_mode(MODE_PROGRAM)

    def select_program(self, program_id: int) -> None:
        if self.delete_mode:
            self.exit_delete_mode()
        self.selected_program_id = int(program_id)
        self.mode = MODE_PROGRAM
        self._sync_topbar_state()
        self.refresh(force=True)

    # ------------------------------------------------------------------
    # 删除模式（流程同首页）
    # ------------------------------------------------------------------
    def enter_delete_mode(self) -> None:
        if not self.records:
            return
        self.delete_mode = True
        self.selected.clear()
        self.refresh(force=True)

    def exit_delete_mode(self) -> None:
        self.delete_mode = False
        self._on_row_drag_finished()
        self.selected.clear()
        self.refresh(force=True)

    def on_selection_toggled(self, log_id: int, selected: bool) -> None:
        if selected:
            self.selected.add(log_id)
        else:
            self.selected.discard(log_id)
        self._apply_mode_visibility()
        self._update_count_label()

    # ------------------------------------------------------------------
    # 滑动选中（按住左键划过若干行连续选中 + 拖到上下边缘自动滚动 +
    # 往回滑撤销刚才滑过头多选的行）
    # ------------------------------------------------------------------
    def _on_row_drag_started(self, log_id: int, value: bool) -> None:
        self._drag_value = bool(value)
        self._drag_anchor = int(log_id)
        self._drag_pos = None
        self._drag_step = 0
        self._dragging = True
        # 记下按下那一刻每行的状态：范围外的行要恢复成这个状态
        self._drag_before = {row.log_id: row.is_selected() for row in self.rows.values()}
        self._drag_timer.stop()

    def _on_row_drag_moved(self, global_pos: QPoint) -> None:
        self._drag_pos = QPoint(global_pos)
        self._drag_apply_at(self._drag_pos)
        self._drag_step = self._drag_step_for(self._drag_pos)
        if self._drag_step:
            if not self._drag_timer.isActive():
                self._drag_timer.start()
        else:
            self._drag_timer.stop()

    def _on_row_drag_finished(self) -> None:
        self._drag_timer.stop()
        self._drag_pos = None
        self._drag_step = 0
        self._dragging = False
        self._drag_before = {}

    def _drag_apply_at(self, global_pos: QPoint) -> None:
        """把本次拖动的目标状态套用到"从按下那一行到光标这一行"的整段。

        只处理光标下那一行是不够的：鼠标移动事件可能稀疏（快速拖动、或拖到边缘后
        自动滚动把整屏换掉），中间那些行会被跳过。

        整段是**实时重算**的：范围外的行一律恢复成按下鼠标之前的状态，所以滑过头
        再往回滑会把刚才多选的行取消掉，而不是只增不减。
        """
        row = _log_row_at(global_pos)
        if row is None:
            # 退一步按几何找：拖动期间 Qt 会把鼠标事件隐式交给按下的那一行，光标
            # 常常已经在窗口之外；窗口没真正显示到屏幕上时 widgetAt 更是一律返回空。
            row = self._row_at_point(global_pos)
        if row is None:
            return
        order = list(self.rows)                    # 显示顺序 == 创建顺序
        try:
            anchor = order.index(self._drag_anchor)
        except ValueError:
            anchor = order.index(row.log_id)
        try:
            current = order.index(row.log_id)
        except ValueError:
            return
        low, high = (anchor, current) if anchor <= current else (current, anchor)
        for index, log_id in enumerate(order):
            target = self.rows.get(log_id)
            if target is None:
                continue
            if low <= index <= high:
                want = self._drag_value
            else:
                # 滑回后回到按下之前的状态（按下时列表已经锁定不重建，快照必然有效）
                want = self._drag_before.get(log_id, target.is_selected())
            if target.is_selected() == want:
                continue
            target.set_selected(want)
            self.on_selection_toggled(log_id, want)

    def _row_at_point(self, global_pos: QPoint) -> Optional[LogRow]:
        """按几何找出光标下这一行（``_log_row_at`` 拿不到时的兜底）。

        只认落在滚动视口里、且行矩形包含光标的行：光标跑到窗口外（拖到别的窗口
        上面）时不该继续选中，视口之外的行也不该算进来。
        """
        viewport = self.scroll.viewport()
        if not viewport.rect().contains(viewport.mapFromGlobal(global_pos)):
            return None
        for row in self.rows.values():
            if not row.isVisible() or row.width() <= 0 or row.height() <= 0:
                continue
            if row.rect().contains(row.mapFromGlobal(global_pos)):
                return row
        return None

    def _drag_step_for(self, global_pos: QPoint) -> int:
        """光标离视口上下边缘越近滚得越快；返回 0 表示这一帧不滚。"""
        viewport = self.scroll.viewport()
        local = viewport.mapFromGlobal(global_pos)
        edge = max(12, config.sx(self.DRAG_EDGE_PX))
        top, bottom = edge, viewport.height() - edge
        y = local.y()
        if y < top:
            # 拖到上边缘：内容往上走（滚动条数值减小）
            ratio = -min(1.0, (top - y) / float(edge))
        elif y > bottom:
            # 拖到下边缘：内容往下走（滚动条数值增大）
            ratio = min(1.0, (y - bottom) / float(edge))
        else:
            return 0
        return int(round(ratio * max(4, config.sx(self.DRAG_MAX_STEP))))

    def _drag_scroll_step(self) -> None:
        """拖动期间的一帧：滚动一屏再给光标下面的新行套用状态。"""
        if self._drag_pos is None or not self._drag_step:
            self._drag_timer.stop()
            return
        bar = self.scroll.verticalScrollBar()
        before = bar.value()
        bar.setValue(before + self._drag_step)
        if bar.value() == before:
            return                     # 已经到顶/到底：保持定时器，往回拖还能继续
        self._drag_apply_at(self._drag_pos)

    def select_all(self) -> None:
        for log_id, row in self.rows.items():
            self.selected.add(log_id)
            row.set_selected(True)
        self._apply_mode_visibility()
        self._update_count_label()

    def clear_selection(self) -> None:
        self.selected.clear()
        for row in self.rows.values():
            row.set_selected(False)
        self._apply_mode_visibility()
        self._update_count_label()

    def on_confirm_delete(self) -> None:
        # 选中可以跨页（翻页不会清掉已选的行），所以按 id 把待删记录从数据库取回来
        ids = sorted(self.selected)
        chosen = [self._record_from_row(row) for row in self.db.logs_by_ids(ids)]
        if not chosen:
            chosen = [record for record in self.records if record["log_id"] in self.selected]
        if not chosen:
            return
        dialog = ConfirmDeleteLogsDialog(self, chosen)
        accepted = False
        try:
            accepted = dialog.run() == QDialog.Accepted
        finally:
            dialog.deleteLater()
        if not accepted:
            return
        removed = self.db.delete_logs([record["log_id"] for record in chosen])
        print(f"[logs] 已删除 {removed} 条运行日志")
        self.delete_mode = False
        self.selected.clear()
        self._apply_mode_visibility()
        # 动画期间不会刷新列表，这里先把计数改成删除后的剩余条数
        self.count_label.setText(tr("logs.count", count=max(0, self.total_records - removed)))

        victims = [self.rows[r["log_id"]] for r in chosen if r["log_id"] in self.rows]
        panel = victims[0].parentWidget() if victims else None
        self.rows.clear()
        self._animate_delete(panel, victims)

    def _animate_delete(self, panel, victims: list) -> None:
        """日志删除动画：右移淡出 + 其余日志自上而下上移，结束后按数据库重建。

        动画期间禁止刷新（``_animating``）：定时刷新会重建列表并销毁正在动画的行，
        导致动画中断、列表空白。
        """
        if panel is None or not victims:
            self.refresh(force=True)
            self.data_changed.emit()
            return
        self._animating = True

        def _done() -> None:
            self._animating = False
            self.refresh(force=True)
            self.data_changed.emit()

        # 日志行在不会裁剪的面板里，平移 60% 宽度即可（配合同步淡出），不会滑出面板
        anim.animate_list_removal(panel, victims, 0, on_finished=_done, slide_ratio=0.6)

    # ------------------------------------------------------------------
    def _on_tick(self) -> None:
        self._tick_count += 1
        if self._animating:
            return
        if self._tick_count % 2 == 0 and self.isVisible() and not self.delete_mode:
            self.refresh()

    def refresh(self, force: bool = False) -> None:
        """重建当前页。

        数据没变就直接返回 —— 这是不再"每 2 秒整表重建"的关键：以前一次读回 3000 条
        日志、建近 3000 个控件，光构建就要 1~2 秒，还把旧控件整套丢进 DeferredDelete
        队列（每次涨 53MB）。现在只读当前页的 200 条，且**这一页的内容**没变时一个控件都不动
        （指纹含每行的 end_time，"运行中"那行的时长/时长变化能立刻反映出来）。
        """
        if self._animating or self._dragging:
            # 删除动画 / 滑动选中进行中：重建列表会把正在操作的行一起销毁，
            # 松手之后的下一次 tick（可见时每 2 秒）会刷新。
            return
        if not self._load_data(force):
            return
        # 筛选条只在"有日志的日期"真的变了、或者屏幕上这份数据换了时才重建 ——
        # 以前它在指纹判定之前无条件跑，日期模式下等于每 2 秒扫一次全表。
        self._sync_mode_headers(self._days_changed())
        ids = self._log_ids(self._signature)
        # 只有"同一批日志、同一页、只是内容（结束时间/时长/条数标签）变了"才允许就地更新：
        # 屏幕上这行的 log_id 与顺序必须完全一致，否则就是换页/换程序/新增或删除了一条，
        # 必须重建（增删会改变分页与位置，不能原地改）。
        light = (self._ids_rendered is not None and self._ids_rendered == ids
                 and self._mode_rendered == self.mode and self._rendered != self._signature)
        if not light and self._cursor_inside():
            suppress_tooltips()      # 光标不在日志页就不必挂全 app 的事件过滤器
        self._detach_rows()
        self._release_widgets(keep=light)
        if light:
            # 同一批日志、只是结束时间/时长变了（最常见：有程序正在运行）：
            # 就地改文字，Panel 与所有 LogRow 都留着不重建。
            by_id = {record["log_id"]: record for record in self.records}
            rendered = dict(self._row_fingerprint or ())    # 屏幕上这份：(log_id, end_time)
            for log_id in (self._ids_rendered or ()):
                record = by_id.get(log_id)
                if record is None or rendered.get(log_id) == record.get("end_ts"):
                    continue                     # 这一行不在本页 / 结束时间没变
                row = self.rows.get(log_id)
                if row is not None:
                    row.update_record(record.get("end_ts"), record.get("message", ""))
        else:
            self._render_records()
        self._apply_mode_visibility()
        self._update_count_label()
        self._update_pager()

        self.empty.setGeometry(0, 0, self.scroll.viewport().width(),
                               max(140, self.scroll.viewport().height()))
        self._rendered = self._signature
        self._row_fingerprint = self._signature[-1] if isinstance(self._signature, tuple) else ()
        self._ids_rendered = ids
        self._mode_rendered = self.mode
        self._days_rendered = self._days if self.mode != MODE_PROGRAM else None
        self._ids = ids
        self._flush_deleted()

    def _days_changed(self) -> bool:
        """"有日志的日期"集合是否与屏幕上这份不同（程序分类模式不看日期筛选条）。"""
        if self.mode == MODE_PROGRAM:
            return False
        return self._days != self._days_rendered

    def _sync_mode_headers(self, refresh_dates: bool = False) -> None:
        if self.mode == MODE_PROGRAM:
            self.header.show()
            self.date_header.hide()
        elif self.mode == MODE_DATE:
            self.header.hide()
            self.date_header.show()
            if refresh_dates:
                self._refresh_date_options()
        else:
            self.header.hide()
            self.date_header.hide()

    def _cursor_inside(self) -> bool:
        try:
            return bool(self.rect().contains(self.mapFromGlobal(QCursor.pos())))
        except Exception:
            return True

    def _detach_rows(self) -> None:
        """把上一页的控件从布局里摘下来，稍后统一销毁。

        只摘不销毁：真正可见的大头（Panel 与 LogRow）在"就地更新"路径里要留着复用
        （见 refresh）。分隔条 / 分隔线很小，一律重做。
        """
        self.rows.clear()
        pending: list[QWidget] = []
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is None:
                continue
            widget.hide()          # 先隐藏：可见控件脱离父级会变成顶层窗口
            pending.append(widget)
        self._pending_rows = pending
        self.list_layout.addStretch(1)

    def _release_widgets(self, keep: bool = False) -> None:
        """销毁上一步摘下来的控件；``keep=True`` 时留着 Panel 与 LogRow 复用。"""
        pending = self._pending_rows
        self._pending_rows = []
        for widget in pending:
            if keep and isinstance(widget, Panel):
                self.list_layout.insertWidget(self.list_layout.count() - 1, widget)
                widget.show()
                for row in widget.findChildren(LogRow):
                    self.rows[row.log_id] = row
                continue
            widget.deleteLater()

    @staticmethod
    def _flush_deleted() -> None:
        """立刻回收 ``deleteLater`` 的控件。

        实测：``deleteLater()`` 只是排队，而 Qt 只在事件循环"回到顶层"时才处理
        DeferredDelete —— 只要输入事件一直不断，旧控件（一次刷新约 1500 个）就会一直堆着，
        内存按"每次刷新 +40MB"涨（历史记录里的 +53MB 就是这个）。这里显式排空。
        """
        app = QApplication.instance()
        if app is not None:
            app.sendPostedEvents(None, QEvent.DeferredDelete)

    # ------------------------------------------------------------------
    def _load_data(self, force: bool = False) -> bool:
        """算好分页并取回"这一页"的记录；返回 False 表示数据没变、不用重建。"""
        if self.mode == MODE_PROGRAM:
            program = self._pick_program()
            self._update_program_header(program)
            if program is None:
                self.records = []
                self.total_records = 0
                self._pages = []
                self.page = 0
                self.page_count = 1
                self._signature = ("program", None)
                self._toggle_empty(True, tr("logs.empty.no_program"),
                                   tr("logs.empty.no_program_hint"))
                return True
            program_id = int(program["id"])
            rows = self.db.logs_for_program(program_id, limit=PAGE_SIZE,
                                            offset=self.page * PAGE_SIZE)
            fingerprint = self._fingerprint(rows)
            signature = ("program", program_id, fingerprint)
            if not self._stale(signature, force):
                return False
            self._signature = signature
            self._row_fingerprint = fingerprint
            self._ids = self._log_ids(signature)
            self.total_records = int(self.db.logs_snapshot(program_id=program_id)[0])
            self.page_count = max(1, (self.total_records + PAGE_SIZE - 1) // PAGE_SIZE)
            self.page = max(0, min(self.page, self.page_count - 1))
            self._pages = []
            self.records = [
                self._record_from_row(
                    row, show_name=False, site=str(program["kind"] or "program") == "site",
                    name=str(program["name"]), icon_path=program["icon_path"],
                    exe_path=str(program["path"]))
                for row in rows
            ]
            self._toggle_empty(self.total_records == 0,
                               tr("logs.empty.program", name=program["name"]),
                               tr("logs.empty.program_hint"))
            return True

        since, until = self._date_range() if self.mode == MODE_DATE else (None, None)
        counts = self.db.log_day_counts(since=since, until=until)
        # 日期筛选条的选项来自**不带筛选**的全量日期，否则选中某一年后别的年份就消失了。
        # 只在日期集合真的变了的时候才算（见 _days_changed / refresh）。
        days = self._day_parts(self.db.log_day_counts())
        self._days = days
        rows: list = []
        if counts:
            self._pages = build_pages(counts)
        self.page_count = max(1, len(self._pages))
        self.page = max(0, min(self.page, self.page_count - 1))
        if self._pages:
            rows = self._page_rows(since, until)
        signature = ("time", self.mode, since, until, self._fingerprint(rows))
        if not self._stale(signature, force):
            return False
        self._signature = signature
        self._row_fingerprint = signature[4]
        self._ids = self._log_ids(signature)
        self.total_records = sum(int(amount) for _day, amount, _end in counts)
        self.records = [self._record_from_row(row) for row in rows]
        empty_title = tr("logs.date.empty") if self.mode == MODE_DATE else tr("logs.empty.title")
        self._toggle_empty(self.total_records == 0, empty_title,
                           "" if self.mode == MODE_DATE else tr("logs.empty.hint"))
        return True

    @staticmethod
    def _fingerprint(rows) -> tuple:
        """这一页每一行的 (id, 结束时间) 摘要 —— 内容不变就一个控件都不用动。

        以前指纹只有"每天的条数"，于是：①正在运行的那条日志时长一直在变，屏幕上却停在
        打开页面那一刻；②删掉一条再加一条（条数没变）屏幕也不更新。
        用 (id, end_time) 元组而不是 hash()：元组本身可复现，hash() 每个进程都不一样。
        """
        return tuple((int(row["id"]), row["end_time"]) for row in rows)

    @staticmethod
    def _day_parts(counts) -> tuple:
        """``log_day_counts()`` 的日期列 -> 日期筛选条要的 (年, 月, 日) 列表（倒序）。"""
        parts = []
        for day, _amount, _end in counts:
            try:
                parts.append(tuple(int(part) for part in str(day).split("-")))
            except ValueError:
                continue
        return tuple(sorted(parts, reverse=True))

    @staticmethod
    def _log_ids(signature: tuple) -> tuple:
        """指纹里的行顺序 -> (log_id, ...)，用来判断"还是不是同一批日志"。"""
        rows = signature[-1] if isinstance(signature, tuple) and signature else ()
        if not rows or not isinstance(rows[0], tuple):
            return ()
        return tuple(int(row[0]) for row in rows)

    def _stale(self, signature: tuple, force: bool) -> bool:
        """指纹变了、或者屏幕上根本不是这份数据，就要重建。"""
        if force or signature != self._signature or signature != self._rendered:
            return True
        return False

    def _page_rows(self, since: Optional[float], until: Optional[float]) -> list:
        """按当前页的分页表把这一页的记录行读回来（一页可能横跨多天）。"""
        rows: list = []
        page = self._pages[self.page] if 0 <= self.page < len(self._pages) else []
        for day, limit, offset in page:
            rows.extend(self.db.logs_in_day(day, limit=limit, offset=offset))
        return rows

    @staticmethod
    def _record_from_row(row, show_name: bool = True, site: bool = False,
                         name: str = "", icon_path=None, exe_path: str = "") -> dict:
        keys = row.keys()
        message = row["message"] if "message" in keys else None
        kind = (row["kind"] if "kind" in keys else "run") or "run"
        if not site and "program_kind" in keys:
            site = str(row["program_kind"] or "") == "site"
        return dict(
            log_id=int(row["id"]),
            name=name or str(row["program_name"] or tr("logs.deleted_program")),
            icon_path=icon_path or (row["icon_path"] if "icon_path" in keys else None),
            exe_path=exe_path or str(row["program_path"] or ""),
            start_ts=float(row["start_time"]),
            end_ts=float(row["end_time"]) if row["end_time"] else None,
            show_name=show_name,
            kind=kind,
            message=str(message or ""),
            site=site,
        )

    def _pick_program(self):
        programs = list(self.db.programs(include_deleted=True))
        program = None
        if self.selected_program_id is not None:
            for candidate in programs:
                if int(candidate["id"]) == self.selected_program_id:
                    program = candidate
                    break
        if program is None and programs:
            program = programs[0]
            self.selected_program_id = int(program["id"])
        return program

    def _update_program_header(self, program) -> None:
        if program is None:
            self.header_tile.clear()
            self.header_name.setText(tr("logs.no_program"))
            self.header_path.setText("")
            self.header_arrow.setEnabled(False)
            return
        self.header_arrow.setEnabled(True)
        if str(program["kind"] or "program") == "site":
            self.header_tile.setPixmap(vector_icons.pixmap("globe", config.sx(24), config.C_ACCENT))
        else:
            self.header_tile.setPixmap(icon_extract.pixmap_for_program(
                program["icon_path"], str(program["path"]), config.sx(24)))
        self.header_name.setText(str(program["name"]))
        path = str(program["path"])
        self.header_path.setText(util.shorten_path(path, 70))
        self.header_path.setToolTip(path)

    # ------------------------------------------------------------------
    def _update_pager(self) -> None:
        visible = self.page_count > 1 and bool(self.records)
        self._pager_visible = visible
        self.pager.set_page(self.page, self.page_count)
        self.pager.setVisible(visible)
        if visible:
            self._place_pager()
            self.pager.raise_()
            if self._pager_dirty:
                # 现在还没有真实几何（首次进入前 QStackedWidget 只排布"当前页"），
                # 等这一轮事件循环把布局算完再摆一次。
                QTimer.singleShot(0, self._place_pager_soon)

    def _place_pager_soon(self) -> None:
        if self._pager_visible:
            self._place_pager()
            self.pager.raise_()

    def _place_pager(self) -> None:
        pager = self.pager
        # 页面还没被布局撑开时读到的宽高是 Qt 默认的 100x30，算出来的坐标一定错
        # （会被 max(0, ...) 夹到左上角）。先记账，等 showEvent / resizeEvent 里
        # 有真实几何了再摆。
        if self.width() < config.sx(200) or self.height() < config.sx(200):
            self._pager_dirty = True
            return
        self._pager_dirty = False
        width = max(config.sx(160), pager.sizeHint().width())
        pager.resize(width, pager.height())
        pager.move(max(0, (self.width() - width) // 2),
                   max(0, self.height() - pager.height() - config.sx(16)))

    def goto_page(self, page: int) -> None:
        if self.page_count <= 1:
            return
        page = max(0, min(int(page), self.page_count - 1))
        if page == self.page:
            return
        self.page = page
        self.refresh(force=True)
        self.scroll.verticalScrollBar().setValue(0)      # 翻页后回到顶部

    def _on_scrolled(self, _value: int = 0) -> None:
        if self._pager_visible:
            self.pager.refresh_backdrop()

    def _render_records(self) -> None:
        if not self.records:
            return
        panel = Panel(self.holder, elevated=False)
        inner = QVBoxLayout(panel)
        inner.setContentsMargins(config.sx(6), config.sx(6), config.sx(6), config.sx(6))
        inner.setSpacing(0)
        last_day = ""
        for index, record in enumerate(self.records):
            # 按日期分隔：同一天只出现一次日期分隔条（"按时间分类"与"按时间排序"都显示）
            if self.mode != MODE_PROGRAM:
                day = time.strftime("%Y-%m-%d", time.localtime(record["start_ts"]))
                if day != last_day:
                    last_day = day
                    inner.addWidget(self._build_separator(panel, record["start_ts"]))
                    index_sep = True
                else:
                    index_sep = False
            else:
                index_sep = False
            if index and not index_sep:
                inner.addWidget(Divider(panel))
            row = LogRow(
                record["log_id"], record["name"], record["icon_path"], record["exe_path"],
                record["start_ts"], record["end_ts"], show_name=record["show_name"],
                delete_mode=self.delete_mode, selected=record["log_id"] in self.selected,
                parent=panel, message=record.get("message", ""),
                kind=record.get("kind", "run"), site=bool(record.get("site", False)))
            row.selection_toggled.connect(self.on_selection_toggled)
            row.drag_started.connect(self._on_row_drag_started)
            row.drag_moved.connect(self._on_row_drag_moved)
            row.drag_finished.connect(self._on_row_drag_finished)
            inner.addWidget(row)
            self.rows[record["log_id"]] = row
        self.list_layout.insertWidget(self.list_layout.count() - 1, panel)
        # 刷新可能发生在页面尚不可见时，这里显式显示并立即触发布局，
        # 避免面板停留在"已创建但未布局"的状态（宽窗口下会整块空白）。
        panel.show()
        self.list_layout.activate()

    def _build_separator(self, parent: QWidget, ts: float) -> QWidget:
        """日期分隔条。"""
        row = QWidget(parent)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(config.sx(6), config.sx(6), config.sx(6), config.sx(4))
        layout.setSpacing(config.sx(8))
        label = QLabel(day_separator_text(ts), row)
        label.setObjectName("CardTitle")
        label.setFont(theme.font(config.FONT_SMALL, bold=True))
        layout.addWidget(label)
        line = QFrame(row)
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {config.C_DIVIDER}; border: none;")
        layout.addWidget(line, 1)
        return row

    def _update_count_label(self) -> None:
        total = self.total_records or len(self.records)
        if self.delete_mode:
            self.count_label.setText(
                tr("logs.selected", selected=len(self.selected), total=total))
        else:
            self.count_label.setText(tr("logs.count", count=total))

    def _toggle_empty(self, empty: bool, title: str, hint: str = "") -> None:
        if empty:
            self.empty.set_text(title, hint)
            self.empty.show()
            self.empty.raise_()
        else:
            self.empty.hide()

    def showEvent(self, event) -> None:  # noqa: N802
        # 首次进入日志页时 refresh() 是在 setCurrentIndex() 之前跑的，那会儿本页还没
        # 被 QStackedWidget 撑开（只有"当前页"会拿到几何），_place_pager() 只能记账。
        # 真正可见之后再摆正；再补一次 singleShot(0) 兜住"show 之后布局才定稿"。
        super().showEvent(event)
        if self._pager_visible:
            self._place_pager()
            self.pager.raise_()
            QTimer.singleShot(0, self._place_pager_soon)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.empty.setGeometry(0, 0, self.scroll.viewport().width(),
                               max(140, self.scroll.viewport().height()))
        # 用逻辑标记而不是 self.pager.isVisible()：本页刚被切成当前页时，子控件的
        # isVisible() 还是 False，用它判断就会漏掉这次纠正，选页框就留在左上角。
        if self._pager_visible:
            self._place_pager()
            self.pager.raise_()
