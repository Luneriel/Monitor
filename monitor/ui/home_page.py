"""首页：程序列表 + 顶部固定横栏（添加 / 删除 / 排序 / 搜索）。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import config, icon_extract
from ..i18n import tr
from . import anim
from . import theme
from .tooltip_guard import suppress_tooltips
from .cards import ProgramCard, is_site
from .controls import EmptyState, IconButton, SearchBar, SlimScrollArea, TextButton
from .dialogs import AddProgramsDialog, AddSitesDialog, ConfirmDeleteDialog, pick_executables
from .popups import MenuItem, MenuPopup, PopupPanel
from .reorder import ReorderController


class SortPopup(PopupPanel):
    """排序下拉菜单：排序方式 + 正倒序。"""

    sort_changed = Signal(str, bool)

    def __init__(self, sort_key: str, ascending: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(config.sx(178))
        self.sort_key = sort_key
        self.ascending = ascending
        manual = sort_key == config.SORT_MANUAL

        keys = [
            (config.SORT_ADDED, tr("home.sort.added")),
            (config.SORT_NAME, tr("home.sort.name")),
            (config.SORT_LAST_RUN, tr("home.sort.last_run")),
            (config.SORT_MANUAL, tr("home.sort.manual")),
        ]
        for key, text in keys:
            item = MenuItem(f"key:{key}", text, self.container, checked=(key == sort_key))
            item.clicked.connect(self._on_key)
            self.body.addWidget(item)

        # 手动排序没有"正序/倒序"的概念，这两项就不显示了
        if manual:
            return

        from .controls import Divider

        self.body.addWidget(Divider(self.container))

        for key, text in (("asc", tr("home.sort.asc")), ("desc", tr("home.sort.desc"))):
            checked = (key == "asc") == bool(ascending)
            item = MenuItem(f"dir:{key}", text, self.container, checked=checked)
            item.clicked.connect(self._on_dir)
            self.body.addWidget(item)

    def _on_key(self, payload: str) -> None:
        self.sort_key = payload.split(":", 1)[1]
        self.sort_changed.emit(self.sort_key, self.ascending)
        self.close()

    def _on_dir(self, payload: str) -> None:
        self.ascending = payload.split(":", 1)[1] == "asc"
        self.sort_changed.emit(self.sort_key, self.ascending)
        self.close()


class HomePage(QWidget):
    """首页。"""

    def __init__(self, db, tracker, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.db = db
        self.tracker = tracker
        self.cards: dict[int, ProgramCard] = {}
        self.section_labels: list = []
        self.programs: list = []
        self.last_ends: dict[int, float] = {}
        self._animating = False
        self._editing = False
        self._refresh_pending = False
        self._rendered_signature: Optional[tuple] = None
        self.delete_mode = False
        self.selected: set[int] = set()
        self.sort_key = config.SORT_ADDED
        self.sort_ascending = True
        self.search_text = ""

        # 搜索框每敲一个字符都会重建整页卡片；180 ms 内的连续输入合并成一次刷新。
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(self.refresh)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_toolbar())

        self.scroll = SlimScrollArea(self)
        self.holder = QWidget()
        self.list_layout = QVBoxLayout(self.holder)
        self.list_layout.setContentsMargins(config.sx(22), config.sx(6), config.sx(22),
                                            config.sx(18))
        self.list_layout.setSpacing(config.sx(11))
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.holder)
        root.addWidget(self.scroll, 1)

        self.empty = EmptyState(tr("home.empty.title"), "monitor", tr("home.empty.hint"))
        self.empty.setParent(self.scroll.viewport())
        self.empty.hide()

        self.reorder = ReorderController(self.scroll, self._commit_order, self)

        self.tracker.tick.connect(self._update_visible_times)
        self.tracker.programs_changed.connect(self.on_programs_changed)

    # ------------------------------------------------------------------
    # 顶栏
    # ------------------------------------------------------------------
    def _build_toolbar(self) -> QWidget:
        bar = QWidget(self)
        bar.setFixedHeight(config.sx(54))
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(config.sx(22), config.sx(12), config.sx(22), config.sx(8))
        layout.setSpacing(config.sx(8))

        self.add_btn = TextButton(tr("home.add"), "primary", icon_name="plus", icon_size=14, height=34)
        self.add_btn.clicked.connect(self.on_add_menu)
        self.delete_btn = TextButton(tr("home.delete"), "danger", icon_name="trash", icon_size=14, height=34)
        self.delete_btn.clicked.connect(self.enter_delete_mode)

        self.select_all_btn = TextButton(tr("common.select_all"), "ghost", height=34)
        self.select_all_btn.clicked.connect(self.select_all)
        self.unselect_all_btn = TextButton(tr("common.unselect_all"), "ghost", height=34)
        self.unselect_all_btn.clicked.connect(self.clear_selection)
        self.unselect_all_btn.hide()

        self.search = SearchBar(tr("home.search_placeholder"))
        self.search.textChanged.connect(self.on_search_changed)

        self.sort_btn = IconButton("sort", tr("home.sort"), size=34, icon_size=17)
        self.sort_btn.clicked.connect(self.toggle_sort)

        self.cancel_delete_btn = TextButton(tr("common.cancel"), "ghost", height=34)
        self.cancel_delete_btn.clicked.connect(self.exit_delete_mode)
        self.confirm_delete_btn = TextButton(tr("common.confirm_delete"), "danger", height=34)
        self.confirm_delete_btn.clicked.connect(self.on_confirm_delete)

        layout.addWidget(self.add_btn)
        layout.addWidget(self.delete_btn)
        layout.addWidget(self.select_all_btn)
        layout.addWidget(self.unselect_all_btn)
        layout.addStretch(1)
        layout.addWidget(self.search)
        layout.addStretch(1)
        layout.addWidget(self.sort_btn)
        layout.addWidget(self.cancel_delete_btn)
        layout.addWidget(self.confirm_delete_btn)

        self._toolbar = bar
        self._toolbar_layout = layout
        self._apply_mode_visibility()
        return bar

    def _apply_mode_visibility(self) -> None:
        for widget in (self.add_btn, self.delete_btn, self.search, self.sort_btn):
            widget.setVisible(not self.delete_mode)
        for widget in (self.select_all_btn, self.cancel_delete_btn, self.confirm_delete_btn):
            widget.setVisible(self.delete_mode)
        self.unselect_all_btn.setVisible(self.delete_mode and self._all_selected())
        self.select_all_btn.setVisible(self.delete_mode)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        width = max(176, min(380, int(self.width() * 0.34)))
        self.search.setFixedWidth(width)
        self.empty.setGeometry(0, 0, self.scroll.viewport().width(),
                               max(140, self.scroll.viewport().height()))

    # ------------------------------------------------------------------
    # 数据刷新
    # ------------------------------------------------------------------
    def on_programs_changed(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        if self._animating:
            # 删除动画进行中：重建列表会销毁正在动画的卡片，动画结束自会刷新
            return
        if self._editing:
            # 正在就地改名：重建会销毁承载输入框的那张卡片，连带把它装的应用级
            # 事件过滤器一起漏掉。先记下待刷新，编辑结束（确认/取消/点别处）再刷。
            self._refresh_pending = True
            return

        self.programs = [p for p in self.db.programs(visible_only=True)]
        self.last_ends = self.db.last_end_times()

        query = self.search_text.strip()
        visible: list = []
        lowered = query.lower()
        for program in self.programs:
            name = str(program["name"])
            path = str(program["path"])
            if query and lowered not in name.lower() and lowered not in path.lower():
                continue
            visible.append(program)

        manual = self.sort_key == config.SORT_MANUAL
        visible = self._sorted(visible)

        # 程序 / 网站分区显示（只有两种都存在时才显示分区标题）
        groups: dict[str, list] = {"program": [], "site": []}
        for program in visible:
            groups["site" if is_site(program) else "program"].append(program)
        if manual:
            # 手动排序：程序与网站各自一组，组内按用户拖动后保存的顺序排
            groups = {kind: self._order_items(groups[kind], kind) for kind in ("program", "site")}
        both = bool(groups["program"]) and bool(groups["site"])

        # 变更指纹：控件结构只由「列表内容 + 搜索 + 排序」决定，四者都没变就不重建。
        # 这样 1 Hz 的 programs_changed / 每次 sort_selection 都不会再拆掉整页卡片。
        signature = (query, self.sort_key, bool(self.sort_ascending), manual, both,
                     tuple((kind, tuple(int(p["id"]) for p in groups[kind]))
                           for kind in ("program", "site")))
        if signature == self._rendered_signature and set(self.cards) == {
                int(p["id"]) for p in visible}:
            self._refresh_pending = False
            self.update_times()
            self._apply_mode_visibility()
            return
        self._rendered_signature = signature
        self._refresh_pending = False

        suppress_tooltips()
        self._clear_cards()

        group_cards: dict[str, list] = {"program": [], "site": []}
        for kind in ("program", "site"):
            if both:
                self.section_labels.append(self._section_label(tr(f"home.section.{kind}s")))
                self.list_layout.insertWidget(self.list_layout.count() - 1,
                                              self.section_labels[-1])
            for program in groups[kind]:
                card = ProgramCard(program, delete_mode=self.delete_mode,
                                   selected=int(program["id"]) in self.selected,
                                   manual=manual, parent=self.holder)
                card.tracking_toggled.connect(self.on_tracking_toggled)
                card.renamed.connect(self.on_renamed)
                card.selection_toggled.connect(self.on_selection_toggled)
                # 改名开始/结束要通知首页：编辑期间禁止重建卡片（见 refresh 开头）
                card.name.edit_started.connect(self._on_edit_started)
                card.name.edit_finished.connect(self._on_edit_finished)
                card.apply_search(query)
                self.list_layout.insertWidget(self.list_layout.count() - 1, card)
                self.cards[int(program["id"])] = card
                group_cards[kind].append(card)

        if manual:
            # 只有"手动排序"模式下手柄才可拖；两组互不串（程序 ↔ 网站不会交换）
            for kind, kind_cards in group_cards.items():
                self.reorder.register(kind, kind_cards)

        self._update_empty_state(len(visible), query)
        self.update_times()
        self._apply_mode_visibility()

    def _on_edit_started(self) -> None:
        self._editing = True

    def _on_edit_finished(self) -> None:
        self._editing = False
        if self._refresh_pending:
            self.refresh()

    def _section_label(self, text: str) -> QLabel:
        """分区标题（程序 / 网站）。"""
        label = theme.label(text, config.FONT_SMALL, True, config.C_TEXT_MUTED, self.holder)
        label.setObjectName("HomeSectionLabel")
        label.setContentsMargins(config.sx(6), config.sx(3), 0, 0)
        return label

    def _clear_cards(self) -> None:
        """清掉首页上所有卡片控件。

        除了 ``self.cards`` 里记录的，还要扫掉"游离"的卡片：删除动画会把参与动画的卡片
        移出布局并手工定位，如果只按 ``self.cards`` 清理，这些卡片会残留在 holder 上
        （表现为首页出现重复卡片、已删除的程序还在），所以这里按控件树兜底清理。
        """
        self.reorder.clear()     # 旧卡片马上销毁，先摘掉拖动控制器里的引用
        for card in self.holder.findChildren(ProgramCard, options=Qt.FindDirectChildrenOnly):
            # 正在就地改名的卡片：它自己往应用上装了事件过滤器，只能由它自己摘掉。
            # 直接 deleteLater 会漏掉那个过滤器（每次重建多一个，永不回收）。
            editor = getattr(card, "name", None)
            if editor is not None and editor.is_editing():
                editor.cancel_edit()
            self.list_layout.removeWidget(card)
            card.hide()
            card.hide()          # 先隐藏：可见控件脱离父级会变成顶层窗口
            card.setParent(None)
            card.deleteLater()
        self.cards.clear()
        for label in self.section_labels:
            self.list_layout.removeWidget(label)
            label.hide()
            label.setParent(None)
            label.deleteLater()
        self.section_labels.clear()

    def _update_empty_state(self, count: int, query: str) -> None:
        if count == 0:
            if query:
                self.empty.set_text(tr("home.empty.no_match", query=query),
                                tr("home.empty.no_match_hint"))
            elif self.delete_mode:
                self.empty.set_text(tr("home.empty.nothing_to_delete"))
            else:
                self.empty.set_text(tr("home.empty.title"), tr("home.empty.hint"))
            self.empty.setGeometry(0, 0, self.scroll.viewport().width(),
                                   max(140, self.scroll.viewport().height()))
            self.empty.show()
            self.empty.raise_()
        else:
            self.empty.hide()

    def _sorted(self, programs: list) -> list:
        if self.sort_key == config.SORT_MANUAL:
            # 手动排序：先保持数据库顺序，分组之后再按用户保存的顺序排
            return list(programs)
        key = self.sort_key
        reverse = not self.sort_ascending

        def sort_key(program):
            if key == config.SORT_NAME:
                return str(program["name"]).lower()
            if key == config.SORT_LAST_RUN:
                return float(program["last_run_time"] or 0.0)
            return float(program["added_time"] or 0.0)

        return sorted(programs, key=sort_key, reverse=reverse)

    def _update_visible_times(self) -> None:
        if self.isVisible() and not self.window().isMinimized():
            self.update_times()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.update_times()

    def update_times(self) -> None:
        snapshot = self.tracker.snapshot()
        for program_id, card in self.cards.items():
            instances = snapshot.get(program_id) or []
            if instances:
                start = min(start for _, start in instances)
                card.update_time(start, None)
            else:
                card.update_time(None, self.last_ends.get(program_id))

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def on_search_changed(self, text: str) -> None:
        self.search_text = text
        # 连续输入合并成一次刷新，别每敲一个字符就重建一遍卡片
        self._search_timer.start()

    def on_tracking_toggled(self, program_id: int, enabled: bool) -> None:
        self.db.set_tracking(program_id, enabled)
        if not enabled:
            # 关闭追踪：立即结束本次使用记录，之后不再监控该程序
            self.tracker.close_program(program_id)
        # 就地更新卡片（不重建列表）：否则滑块动画会被立刻销毁，表现为"闪一下"
        card = self.cards.get(program_id)
        if card is not None:
            card.set_tracking_look(enabled)
            card.update_time(None, self.last_ends.get(program_id))
        # sqlite3.Row 不可变，这里用字典副本同步开关状态
        self.programs = [
            ({**dict(p), "tracking": 1 if enabled else 0} if int(p["id"]) == program_id else p)
            for p in self.programs
        ]
        self.tracker.set_programs(self.db.programs(visible_only=True), notify=False)

    def on_renamed(self, program_id: int, name: str) -> None:
        self.db.rename_program(program_id, name)
        self.programs = [
            ({**dict(p), "name": name} if int(p["id"]) == program_id else p)
            for p in self.programs
        ]

    def on_selection_toggled(self, program_id: int, selected: bool) -> None:
        if selected:
            self.selected.add(program_id)
        else:
            self.selected.discard(program_id)
        self._apply_mode_visibility()

    def _all_selected(self) -> bool:
        visible_ids = [int(p["id"]) for p in self.programs if int(p["id"]) in self.cards]
        return bool(visible_ids) and all(pid in self.selected for pid in visible_ids)

    def select_all(self) -> None:
        for program_id, card in self.cards.items():
            self.selected.add(program_id)
            card.set_selected(True)
        self._apply_mode_visibility()

    def clear_selection(self) -> None:
        self.selected.clear()
        for card in self.cards.values():
            card.set_selected(False)
        self._apply_mode_visibility()

    def enter_delete_mode(self) -> None:
        if not self.cards:
            return
        self._search_timer.stop()
        self.delete_mode = True
        self._rendered_signature = None   # 模式变化后必须走一次完整重建
        self.selected.clear()
        self.search_text = ""
        self.search.clear()
        # 就地切换为删除模式：不重建卡片，避免鼠标下的卡片被替换而闪出 tooltip
        for card in self.cards.values():
            card.set_delete_mode(True, selected=False)
        self._apply_mode_visibility()

    def exit_delete_mode(self) -> None:
        self.delete_mode = False
        self._rendered_signature = None
        self.selected.clear()
        for card in self.cards.values():
            card.set_delete_mode(False, selected=False)
        self._apply_mode_visibility()

    # ------------------------------------------------------------------
    def on_add_menu(self) -> None:
        """「添加」按钮：外形不变，点开是"添加程序 / 添加网站"二选一。"""
        menu = MenuPopup([("program", tr("home.add.program")), ("site", tr("home.add.site"))],
                         parent=self.window())
        menu.setAttribute(Qt.WA_DeleteOnClose, True)
        menu.item_selected.connect(self._on_add_choice)
        menu.popup_at(self.add_btn)

    def _on_add_choice(self, key: str) -> None:
        if key == "site":
            self.on_add_sites()
        else:
            self.on_add()

    def on_add_sites(self) -> None:
        """添加被追踪的网站（一行一个域名，支持通配）。"""
        dialog = AddSitesDialog(self, {str(p["path"]) for p in self.db.sites(visible_only=True)})
        accepted = False
        patterns: list[str] = []
        try:
            accepted = dialog.run() == QDialog.Accepted
            if accepted:
                patterns = list(dialog.result_patterns)
        finally:
            dialog.deleteLater()
        if not accepted or not patterns:
            return
        added = 0
        for pattern in patterns:
            if self.db.add_site(pattern):
                added += 1
        if added:
            # 清空搜索框，保证新增的卡片一定能被看到
            if self.search_text:
                self.search.clear()
                self.search_text = ""
            self.tracker.set_programs(self.db.programs(visible_only=True))
            self.refresh()

    def on_add(self) -> None:
        paths = pick_executables(self)
        if not paths:
            return
        # 只把"仍在列表中"的程序视为已存在；已删除（保留日志）的可以重新添加回来
        existing = {str(p["path"]) for p in self.db.programs(visible_only=True)}
        dialog = AddProgramsDialog(self, paths, existing_paths=existing)
        accepted = False
        result_programs: list[dict] = []
        try:
            accepted = dialog.run() == QDialog.Accepted
            # 必须在 deleteLater 之前取出结果：延迟删除一旦执行，读取对话框属性会失效
            if accepted:
                result_programs = [dict(entry) for entry in dialog.result_programs]
        finally:
            dialog.deleteLater()

        if not accepted or not result_programs:
            return
        added = 0
        for entry in result_programs:
            icon_path = icon_extract.extract_icon(entry["path"], 64)
            if self.db.add_program(entry["path"], entry["name"], icon_path, tracking=True):
                added += 1
        if added:
            # 清空搜索框，保证新增的卡片一定能被看到
            if self.search_text:
                self.search.clear()
                self.search_text = ""
            self.tracker.set_programs(self.db.programs(visible_only=True))
            self.refresh()

    def on_confirm_delete(self) -> None:
        selected_programs = [p for p in self.programs if int(p["id"]) in self.selected]
        if not selected_programs:
            return
        dialog = ConfirmDeleteDialog(self, selected_programs)
        accepted = False
        result: dict[int, bool] = {}
        try:
            accepted = dialog.run() == QDialog.Accepted
            if accepted:
                result = dict(dialog.result)
        finally:
            dialog.deleteLater()
        if not accepted:
            return
        keep_ids = [pid for pid, keep in result.items() if keep]
        purge_ids = [pid for pid, keep in result.items() if not keep]
        victims = [self.cards[pid] for pid in list(keep_ids) + list(purge_ids) if pid in self.cards]
        for program_id in list(keep_ids) + list(purge_ids):
            self.tracker.close_program(program_id)
        if keep_ids:
            self.db.remove_programs(keep_ids, keep_logs=True)
        if purge_ids:
            self.db.remove_programs(purge_ids, keep_logs=False)
        self.delete_mode = False
        self.selected.clear()
        self._apply_mode_visibility()
        self.tracker.set_programs(self.db.programs(visible_only=True), notify=False)

        # 先播放删除动画（被删卡片右移淡出 + 其余卡片自上而下上移），结束后再按数据库重建。
        # 注意：这里**不能**清空 self.cards —— 动画会把卡片移出布局并手工定位，
        # 结束后 refresh() 需要靠 self.cards / _clear_cards() 把新旧卡片一并清掉，
        # 否则旧卡片会残留成"幽灵卡片"（首页出现重复项、删掉的程序还在）。
        self._animating = True

        def _done() -> None:
            self._animating = False
            self.refresh()

        anim.animate_list_removal(self.holder, victims, self.list_layout.spacing(),
                                  on_finished=_done)

    def toggle_sort(self) -> None:
        popup = SortPopup(self.sort_key, self.sort_ascending, self)
        popup.sort_changed.connect(self.on_sort_changed)
        popup.popup_at(self.sort_btn, align="right")

    def on_sort_changed(self, key: str, ascending: bool) -> None:
        self.sort_key = key
        self.sort_ascending = ascending
        self.refresh()

    # ------------------------------------------------------------------
    # 手动排序（顺序存在 settings 表里，程序与网站各一份，互不影响）
    # ------------------------------------------------------------------
    @staticmethod
    def _order_key(kind: str) -> str:
        return f"order.home.{kind}"

    def _order_items(self, items: list, kind: str) -> list:
        """按用户拖动保存的顺序排（没记录过的目标保持原来的相对顺序，追加在后面）。"""
        order = self.db.get_order(self._order_key(kind))
        if not order:
            return list(items)
        rank = {int(pid): index for index, pid in enumerate(order)}
        return sorted(items, key=lambda item: rank.get(int(item["id"]), len(rank)))

    def _commit_order(self, kind: str, ids: list) -> None:
        """拖动手柄松手：把该分组的新顺序写进数据库。"""
        self.db.set_order(self._order_key(kind), ids)
