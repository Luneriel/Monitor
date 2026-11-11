"""弹窗：添加程序、删除确认。

* 添加弹窗宽为程序窗口的 50%、高为 60%，超出时右侧出现上下滑动栏；
* 删除确认弹窗列出被选中的程序，并可为每个程序保留 / 不保留日志。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QGuiApplication, QPainter
from PySide6.QtWidgets import (QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
                               QVBoxLayout, QWidget)

from .. import config, icon_extract, site_watch
from ..i18n import tr
from ..database import normalize_path
from . import anim, theme, vector_icons
from .cards import ProgramRow
from .controls import Divider, IconButton, IconTile, SlimScrollArea, TextButton, ToggleSwitch

DIALOG_SHADOW = 20


def pick_executables(parent: Optional[QWidget] = None) -> list[str]:
    """调用 Windows 11 文件选择器（Qt 原生对话框），可多选。"""
    files, _ = QFileDialog.getOpenFileNames(
        parent, tr("dialog.pick.title"), "", tr("dialog.pick.filter")
    )
    return [f for f in files if f]


class DialogHeader(QWidget):
    """弹窗标题行：标题 + 副标题 + 右上角关闭按钮。"""

    def __init__(self, title: str, subtitle: str = "", parent: Optional[QWidget] = None,
                 closable: bool = True) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(config.sx(10))

        texts = QVBoxLayout()
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(2)
        self.title = QLabel(title)
        self.title.setFont(theme.font(config.FONT_PAGE_TITLE, bold=True))
        self.title.setStyleSheet(f"color: {config.C_TEXT}; background: transparent;")
        texts.addWidget(self.title)
        self.subtitle = QLabel(subtitle)
        self.subtitle.setFont(theme.font(config.FONT_SMALL))
        self.subtitle.setStyleSheet(f"color: {config.C_TEXT_MUTED}; background: transparent;")
        # 先入布局再设置可见性（无父控件时 setVisible(True) 会弹成独立窗口）
        texts.addWidget(self.subtitle)
        self.subtitle.setVisible(bool(subtitle))
        layout.addLayout(texts)
        layout.addStretch(1)

        self.close_btn: Optional[IconButton] = None
        if closable:
            self.close_btn = IconButton("close", tr("common.close"), size=30, icon_size=15,
                                        color=config.C_TEXT_MUTED, flat=True)
            layout.addWidget(self.close_btn, 0, Qt.AlignTop)

    def set_subtitle(self, text: str) -> None:
        self.subtitle.setText(text)
        self.subtitle.setVisible(bool(text))


class BaseDialog(QDialog):
    """无边框、圆角、带阴影与缩放动画的对话框。"""

    def __init__(self, parent: Optional[QWidget], width: int, height: int) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(DIALOG_SHADOW, DIALOG_SHADOW, DIALOG_SHADOW, DIALOG_SHADOW)
        outer.setSpacing(0)

        self.container = QFrame(self)
        self.container.setObjectName("DialogContainer")
        self.container.setAttribute(Qt.WA_StyledBackground, True)
        outer.addWidget(self.container)

        self.body = QVBoxLayout(self.container)
        self.body.setContentsMargins(config.sx(20), config.sx(18), config.sx(20), config.sx(18))
        self.body.setSpacing(config.sx(14))

        self._content_w = max(380, int(width))
        self._content_h = max(280, int(height))
        self.setFixedSize(self._content_w + 2 * DIALOG_SHADOW,
                          self._content_h + 2 * DIALOG_SHADOW)

    def paintEvent(self, event) -> None:  # noqa: N802
        """容器四周自绘柔和阴影（避免 QGraphicsDropShadowEffect 的缓存闪烁）。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        theme.paint_soft_shadow(painter, QRectF(self.container.geometry()), config.sx(16),
                                spread=config.sx(13), dy=config.sx(7), alpha=64)
        painter.end()

    # ------------------------------------------------------------------
    def add_footer(self, buttons: list[QWidget]) -> None:
        self.body.addWidget(Divider(self.container))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(config.sx(12))
        for button in buttons:
            row.addWidget(button, 1)
        self.body.addLayout(row)

    def center_on_parent(self) -> None:
        parent = self.parentWidget()
        screen = None
        if parent is not None:
            geo = parent.window().frameGeometry()
            screen = QGuiApplication.screenAt(geo.center())
        screen = screen or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        if parent is not None:
            geo = parent.window().frameGeometry()
            x = geo.center().x() - self.width() // 2
            y = geo.center().y() - self.height() // 2
        else:
            x = area.center().x() - self.width() // 2
            y = area.center().y() - self.height() // 2
        x = max(area.left(), min(x, area.right() - self.width()))
        y = max(area.top(), min(y, area.bottom() - self.height()))
        self.move(x, y)

    def run(self) -> int:
        self.center_on_parent()
        self.setWindowOpacity(0.0)
        self.show()
        self.layout().activate()
        anim.pop_widget(self.container, scale=0.94, duration=config.ANIM_SLOW, window=self)
        return self.exec()


class AddProgramsDialog(BaseDialog):
    """添加程序弹窗。"""

    def __init__(self, parent: Optional[QWidget], paths: list[str],
                 existing_paths: Optional[set[str]] = None) -> None:
        window = parent.window() if parent else None
        win_w = window.width() if window else config.BASE_WINDOW_W
        win_h = window.height() if window else config.BASE_WINDOW_H
        super().__init__(parent, int(win_w * 0.5), int(win_h * 0.6))

        self.entries: list[dict] = []
        self._existing = {normalize_path(p) for p in (existing_paths or set())}
        self._already_tracked = 0
        self.result_programs: list[dict] = []

        self.header = DialogHeader(tr("dialog.add.title"), tr("dialog.add.subtitle"), self.container)
        self.header.close_btn.clicked.connect(self.reject)
        self.body.addWidget(self.header)
        self.body.addWidget(Divider(self.container))

        self.scroll = SlimScrollArea(self.container)
        self.list_holder = QWidget()
        self.list_layout = QVBoxLayout(self.list_holder)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(config.sx(2))
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_holder)
        self.body.addWidget(self.scroll, 1)

        self.add_more_btn = TextButton(tr("dialog.add.more"), variant="subtle", icon_name="plus",
                                       icon_size=14, height=32)
        self.add_more_btn.clicked.connect(self._on_add_more)
        self.body.addWidget(self.add_more_btn, 0, Qt.AlignLeft)

        self.cancel_btn = TextButton(tr("common.cancel"), variant="ghost", height=36)
        self.confirm_btn = TextButton(tr("common.confirm"), variant="primary", height=36)
        self.cancel_btn.clicked.connect(self.reject)
        self.confirm_btn.clicked.connect(self._on_confirm)
        self.add_footer([self.cancel_btn, self.confirm_btn])

        self.add_entries(paths)

    # ------------------------------------------------------------------
    def add_entries(self, paths: list[str]) -> None:
        seen = {normalize_path(e["path"]) for e in self.entries}
        for path in paths:
            norm = normalize_path(path)
            if not norm or norm in seen:
                continue
            if norm in self._existing:
                self._already_tracked += 1
                continue
            seen.add(norm)
            entry = {"path": norm, "name": icon_extract.file_display_name(norm)}
            self.entries.append(entry)
            row = ProgramRow(entry["name"], entry["path"], icon_path=None, exe_path=entry["path"],
                             editable=True, parent=self.list_holder)
            row.renamed.connect(lambda text, e=entry: e.update({"name": text}))
            self.list_layout.insertWidget(self.list_layout.count() - 1, row)
        self._update_header()

    def _update_header(self) -> None:
        total = len(self.entries)
        if total:
            self.header.set_subtitle(tr("dialog.add.selected", count=total))
        elif self._already_tracked:
            self.header.set_subtitle(tr("dialog.add.all_existing", count=self._already_tracked))
        else:
            self.header.set_subtitle(tr("dialog.add.none"))
        self.confirm_btn.setEnabled(total > 0)

    def _on_add_more(self) -> None:
        paths = pick_executables(self)
        if paths:
            self.add_entries(paths)

    def _on_confirm(self) -> None:
        self.result_programs = [dict(e) for e in self.entries]
        self.accept()


class AddSitesDialog(BaseDialog):
    """添加被追踪的网站：一个多行文本框，**一行一个域名**（支持通配与路径）。"""

    def __init__(self, parent: Optional[QWidget] = None,
                 existing: Optional[set] = None) -> None:
        window = parent.window() if parent else None
        win_w = window.width() if window else config.BASE_WINDOW_W
        win_h = window.height() if window else config.BASE_WINDOW_H
        super().__init__(parent, int(win_w * 0.48), int(win_h * 0.44))

        self.result_patterns: list[str] = []
        self._existing = {site_watch.normalize_pattern(p) for p in (existing or set()) if p}

        self.header = DialogHeader(tr("dialog.sites.title"), tr("dialog.sites.subtitle"),
                                   self.container)
        self.header.close_btn.clicked.connect(self.reject)
        self.body.addWidget(self.header)
        self.body.addWidget(Divider(self.container))

        self.hint = QLabel(tr("dialog.sites.hint"), self.container)
        self.hint.setWordWrap(True)
        self.hint.setFont(theme.font(config.FONT_SMALL))
        self.hint.setStyleSheet(f"color: {config.C_TEXT_MUTED}; background: transparent;")
        self.body.addWidget(self.hint)

        self.editor = QPlainTextEdit(self.container)
        self.editor.setObjectName("SiteEditor")
        self.editor.setPlaceholderText(tr("dialog.sites.placeholder"))
        self.editor.setFont(theme.font(config.FONT_BODY))
        self.editor.setMinimumHeight(config.sx(150))
        self.editor.setTabChangesFocus(True)
        self.body.addWidget(self.editor, 1)

        # 先入布局、再控制可见性（无父控件的 setVisible(True) 会弹成独立窗口）
        self.notice = QLabel("", self.container)
        self.notice.setWordWrap(True)
        self.notice.setFont(theme.font(config.FONT_SMALL))
        self.notice.setStyleSheet(f"color: {config.C_DANGER}; background: transparent;")
        self.body.addWidget(self.notice)

        self.cancel_btn = TextButton(tr("common.cancel"), variant="ghost", height=36)
        self.confirm_btn = TextButton(tr("common.confirm"), variant="primary", height=36)
        self.cancel_btn.clicked.connect(self.reject)
        self.confirm_btn.clicked.connect(self._on_confirm)
        self.add_footer([self.cancel_btn, self.confirm_btn])

        self.editor.textChanged.connect(self._validate)
        self._validate()

    # ------------------------------------------------------------------
    def patterns(self) -> list[str]:
        """解析多行输入：去空行、归一化、去重，并过滤掉明显不是域名的行。"""
        seen: set = set()
        result: list[str] = []
        for line in self.editor.toPlainText().splitlines():
            text = line.strip()
            if not text:
                continue
            pattern = site_watch.normalize_pattern(text)
            if not pattern or not site_watch.looks_like_host(pattern):
                continue
            if pattern in seen:
                continue
            seen.add(pattern)
            result.append(pattern)
        return result

    def _validate(self) -> None:
        patterns = self.patterns()
        lines = [line for line in self.editor.toPlainText().splitlines() if line.strip()]
        # 无效行 = 既不是合法域名、又是重复行的那些（提示里说"无效或重复"）
        valid = [site_watch.normalize_pattern(line) for line in lines]
        valid = [p for p in valid if p and site_watch.looks_like_host(p)]
        dropped = len(lines) - len(set(valid))
        fresh = [p for p in patterns if p not in self._existing]
        already = len(patterns) - len(fresh)
        self.confirm_btn.setEnabled(bool(fresh))
        parts: list[str] = []
        if fresh:
            parts.append(tr("dialog.sites.ready", count=len(fresh)))
        if already:
            parts.append(tr("dialog.sites.existing", count=already))
        if lines and not patterns:
            parts.append(tr("dialog.sites.invalid"))
        elif dropped > 0:
            parts.append(tr("dialog.sites.dropped", count=dropped))
        self.notice.setText("；".join(parts))
        self.notice.setVisible(bool(self.notice.text()))
        if fresh:
            self.notice.setStyleSheet(f"color: {config.C_TEXT_MUTED}; background: transparent;")

    def _on_confirm(self) -> None:
        self.result_patterns = [p for p in self.patterns() if p not in self._existing]
        self.accept()


class ConfirmDeleteDialog(BaseDialog):
    """删除确认弹窗：列出被选中的程序，并可逐项决定是否保留日志。"""

    def __init__(self, parent: Optional[QWidget], programs: list) -> None:
        window = parent.window() if parent else None
        win_w = window.width() if window else config.BASE_WINDOW_W
        win_h = window.height() if window else config.BASE_WINDOW_H
        super().__init__(parent, int(win_w * 0.5), int(win_h * 0.6))

        self.programs = list(programs)
        self.result: dict[int, bool] = {}

        header = QWidget(self.container)
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(config.sx(10))

        badge = QLabel()
        badge.setFixedSize(config.sx(34), config.sx(34))
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(
            f"background: {config.C_DANGER_SOFT}; border-radius: {config.sx(17)}px;"
        )
        badge.setPixmap(vector_icons.pixmap("warning", config.sx(17), config.C_DANGER, 1.9))
        header_row.addWidget(badge, 0, Qt.AlignVCenter)

        texts = QVBoxLayout()
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(2)
        self.title_label = QLabel(tr("dialog.delete.title", count=len(self.programs)))
        self.title_label.setFont(theme.font(config.FONT_PAGE_TITLE, bold=True))
        self.title_label.setStyleSheet(f"color: {config.C_TEXT}; background: transparent;")
        texts.addWidget(self.title_label)
        hint = QLabel(tr("dialog.delete.hint"))
        hint.setFont(theme.font(config.FONT_SMALL))
        hint.setStyleSheet(f"color: {config.C_TEXT_MUTED}; background: transparent;")
        texts.addWidget(hint)
        header_row.addLayout(texts)
        header_row.addStretch(1)

        close_btn = IconButton("close", "关闭", size=30, icon_size=15,
                               color=config.C_TEXT_MUTED, flat=True)
        close_btn.clicked.connect(self.reject)
        header_row.addWidget(close_btn, 0, Qt.AlignTop)
        self.body.addWidget(header)
        self.body.addWidget(Divider(self.container))

        self.scroll = SlimScrollArea(self.container)
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(config.sx(2))
        self.switches: dict[int, ToggleSwitch] = {}
        for program in self.programs:
            program_id = int(program["id"])
            switch = ToggleSwitch(True)
            switch.setToolTip(tr("dialog.delete.keep_logs_tip"))
            self.switches[program_id] = switch
            right = QWidget()
            right_layout = QHBoxLayout(right)
            right_layout.setContentsMargins(0, 0, 0, 0)
            right_layout.setSpacing(config.sx(7))
            caption = QLabel(tr("dialog.delete.keep_logs"))
            caption.setFont(theme.font(config.FONT_TINY))
            caption.setStyleSheet(f"color: {config.C_TEXT_MUTED}; background: transparent;")
            right_layout.addWidget(caption)
            right_layout.addWidget(switch)
            row = ProgramRow(str(program["name"]), str(program["path"]),
                             icon_path=program["icon_path"], exe_path=str(program["path"]),
                             editable=False, right_widget=right, parent=holder,
                             icon_size=26, tile_size=36)
            layout.addWidget(row)
        layout.addStretch(1)
        self.scroll.setWidget(holder)
        self.body.addWidget(self.scroll, 1)

        self.cancel_btn = TextButton("取消", variant="ghost", height=36)
        self.confirm_btn = TextButton(tr("common.confirm_delete"), variant="danger", height=36)
        self.cancel_btn.clicked.connect(self.reject)
        self.confirm_btn.clicked.connect(self._on_confirm)
        self.add_footer([self.cancel_btn, self.confirm_btn])

    def _on_confirm(self) -> None:
        self.result = {pid: sw.isChecked() for pid, sw in self.switches.items()}
        self.accept()


class ConfirmDeleteLogsDialog(BaseDialog):
    """日志删除确认弹窗：列出被选中的运行记录（流程与首页"删除"一致）。"""

    def __init__(self, parent: Optional[QWidget], records: list) -> None:
        window = parent.window() if parent else None
        win_w = window.width() if window else config.BASE_WINDOW_W
        win_h = window.height() if window else config.BASE_WINDOW_H
        super().__init__(parent, int(win_w * 0.5), int(win_h * 0.6))

        self.records = list(records)

        header = QWidget(self.container)
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(config.sx(10))

        badge = QLabel()
        badge.setFixedSize(config.sx(34), config.sx(34))
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(
            f"background: {config.C_DANGER_SOFT}; border-radius: {config.sx(17)}px;"
        )
        badge.setPixmap(vector_icons.pixmap("warning", config.sx(17), config.C_DANGER, 1.9))
        header_row.addWidget(badge, 0, Qt.AlignVCenter)

        texts = QVBoxLayout()
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(2)
        title = QLabel(tr("dialog.delete_logs.title", count=len(self.records)))
        title.setFont(theme.font(config.FONT_PAGE_TITLE, bold=True))
        title.setStyleSheet(f"color: {config.C_TEXT}; background: transparent;")
        texts.addWidget(title)
        hint = QLabel(tr("dialog.delete_logs.hint"))
        hint.setFont(theme.font(config.FONT_SMALL))
        hint.setStyleSheet(f"color: {config.C_TEXT_MUTED}; background: transparent;")
        texts.addWidget(hint)
        header_row.addLayout(texts)
        header_row.addStretch(1)

        close_btn = IconButton("close", "关闭", size=30, icon_size=15,
                               color=config.C_TEXT_MUTED, flat=True)
        close_btn.clicked.connect(self.reject)
        header_row.addWidget(close_btn, 0, Qt.AlignTop)
        self.body.addWidget(header)
        self.body.addWidget(Divider(self.container))

        self.scroll = SlimScrollArea(self.container)
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(config.sx(4))
        for record in self.records:
            layout.addWidget(LogSummaryRow(record, holder))
        layout.addStretch(1)
        self.scroll.setWidget(holder)
        self.body.addWidget(self.scroll, 1)

        self.cancel_btn = TextButton(tr("common.cancel"), variant="ghost", height=36)
        self.confirm_btn = TextButton(tr("common.confirm_delete"), variant="danger", height=36)
        self.cancel_btn.clicked.connect(self.reject)
        self.confirm_btn.clicked.connect(self.accept)
        self.add_footer([self.cancel_btn, self.confirm_btn])


class LogSummaryRow(QWidget):
    """确认弹窗中的一条日志摘要：图标 + 名称 / 路径 / 起止时间 + 时长胶囊。

    时间信息放在同一列内换行显示，窄弹窗下也不会被挤出可视区。
    """

    def __init__(self, record: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from PySide6.QtCore import Qt as _Qt

        from .. import util
        from . import theme as _theme

        self._name_text = str(record.get("name", ""))
        self._path_text = str(record.get("exe_path", ""))
        self._tile_size = config.sx(36)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(config.sx(2), config.sx(6), config.sx(6), config.sx(6))
        layout.setSpacing(config.sx(11))
        self._layout = layout

        tile = IconTile(self._tile_size, self)
        tile.setPixmap(icon_extract.pixmap_for_program(
            record.get("icon_path"), self._path_text, config.sx(24)))
        layout.addWidget(tile, 0, _Qt.AlignTop)

        column = QWidget(self)
        col = QVBoxLayout(column)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        self._column = column

        self.name_label = QLabel()
        self.name_label.setFont(_theme.font(config.FONT_CARD_NAME, bold=True))
        self.name_label.setStyleSheet(f"color: {config.C_TEXT}; background: transparent;")
        self.name_label.setToolTip(self._name_text)
        col.addWidget(self.name_label)

        self.path_label = QLabel()
        self.path_label.setFont(_theme.font(config.FONT_SMALL))
        self.path_label.setStyleSheet(f"color: {config.C_TEXT_MUTED}; background: transparent;")
        self.path_label.setToolTip(self._path_text)
        col.addWidget(self.path_label)

        time_row = QWidget(column)
        time_layout = QHBoxLayout(time_row)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.setSpacing(config.sx(8))
        start = float(record.get("start_ts") or 0)
        end = record.get("end_ts")
        times = QLabel(f"{util.format_short(start)}  →  "
                       f"{util.format_short(float(end)) if end else tr('common.running')}")
        times.setFont(_theme.font(config.FONT_SMALL))
        times.setStyleSheet(f"color: {config.C_TEXT_MUTED}; background: transparent;")
        times.setToolTip(f"{tr('dialog.time.start', time=util.format_ts(start))} · "
                         f"{tr('dialog.time.end', time=util.format_ts(float(end))) if end else tr('common.running')}")
        time_layout.addWidget(times)
        if end:
            chip = QLabel(util.format_duration(float(end) - start))
            chip.setFont(_theme.font(config.FONT_TINY, bold=True))
            chip.setStyleSheet(
                f"background: {config.C_SURFACE_SOFT}; color: {config.C_TEXT_BODY};"
                f" border-radius: {config.sx(7)}px; padding: 1px {config.sx(8)}px;"
            )
            time_layout.addWidget(chip)
        time_layout.addStretch(1)
        col.addWidget(time_row)

        layout.addWidget(column, 1)
        self._elide()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QFontMetrics as _Metrics

        width = max(config.sx(90), self.width() - self._tile_size - config.sx(31))
        fm_name = _Metrics(self.name_label.font())
        self.name_label.setText(fm_name.elidedText(self._name_text, _Qt.ElideMiddle, width))
        fm_path = _Metrics(self.path_label.font())
        self.path_label.setText(fm_path.elidedText(self._path_text, _Qt.ElideMiddle, width))
