"""Saved rotation lists and one visibility-aware, demand-driven timer."""
from __future__ import annotations

import copy
import json
import random
import uuid

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QSpinBox,
    QVBoxLayout, QWidget)

from .. import config
from ..wallpaper_library import ROTATION_KEY, _json_setting, text
from .controls import TextButton


def read_rotation(db):
    raw = _json_setting(db, ROTATION_KEY, {})
    if not isinstance(raw, dict):
        raw = {}
    lists = []
    raw_lists = raw.get("lists", [])
    for item in raw_lists[:100] if isinstance(raw_lists, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        try:
            seconds = max(10, min(86400, int(item.get("seconds", 300))))
        except (TypeError, ValueError, OverflowError):
            seconds = 300
        items = item.get("items", [])
        lists.append({"id": item["id"][:128], "name": str(item.get("name", ""))[:80],
            "seconds": seconds, "mode": "random" if item.get("mode") == "random" else "ordered",
            "items": list(dict.fromkeys(p for p in items[:we_limit()] if isinstance(p, str)))
                     if isinstance(items, list) else []})
    active = raw.get("active", "")
    return {"lists": lists, "active": active if any(p["id"] == active for p in lists) else ""}


def we_limit():
    return 20_000


class RotationService(QObject):
    changed = Signal()

    def __init__(self, db, library, background, parent=None):
        super().__init__(parent)
        self.db, self.library, self.background = db, library, background
        self.state = read_rotation(db)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.advance)
        self._remaining = None
        self._failed_ids = set()
        self._waiting_id = ""
        self._visible = True
        library.applied.connect(self._applied)
        library.failed.connect(self._failed)
        library.busy_changed.connect(self._busy_changed)
        library.refreshed.connect(self._library_changed)
        background.window().installEventFilter(self)
        self.sync()

    @property
    def active_list(self):
        return next((p for p in self.state["lists"] if p["id"] == self.state["active"]), None)

    def _save(self):
        self.db.set_setting(ROTATION_KEY, json.dumps(self.state, ensure_ascii=False))
        self.changed.emit()

    def save_list(self, value):
        # Copy the draft. The editor never owns a reference to live state.
        draft = copy.deepcopy(value)
        existing = next((i for i, p in enumerate(self.state["lists"]) if p["id"] == draft["id"]), None)
        if existing is None:
            self.state["lists"].append(draft)
        else:
            self.state["lists"][existing] = draft
        if draft["id"] == self.state["active"]:
            self.timer.stop()
            self._remaining = None
            self._failed_ids.clear()
        self._save()
        self.sync()

    def delete_list(self, identity):
        self.state["lists"] = [p for p in self.state["lists"] if p["id"] != identity]
        if self.state["active"] == identity:
            self.state["active"] = ""
        self._remaining = None
        self._save()
        self.sync()

    def activate(self, identity):
        self.timer.stop()
        self.state["active"] = identity if any(p["id"] == identity for p in self.state["lists"]) else ""
        self._remaining = None
        self._failed_ids.clear()
        self._save()
        candidates = self.candidates()
        if candidates and self.library.selected_id not in candidates and self._can_run():
            self._waiting_id = candidates[0]
            self.library.select(candidates[0])
        self.sync()

    def candidates(self):
        active = self.active_list
        return [p for p in active["items"] if p not in self._failed_ids and self.library.store.eligible(p)] if active else []

    def _can_run(self):
        window = self.background.window()
        return bool(self._visible and config.current_theme() == config.THEME_GLASS
                    and self.background.isVisible() and window.isVisible() and not window.isMinimized())

    def sync(self):
        active = self.active_list
        should_run = bool(active and len(self.candidates()) > 1 and self._can_run() and not self.library.busy)
        if not should_run:
            if self.timer.isActive():
                self._remaining = max(1, self.timer.remainingTime())
                self.timer.stop()
            return
        if not self.timer.isActive():
            delay = self._remaining if self._remaining is not None else active["seconds"] * 1000
            self._remaining = None
            self.timer.start(max(1, delay))

    def _busy_changed(self, busy):
        self.sync()

    def _library_changed(self):
        self._failed_ids.clear()
        self.sync()

    def _applied(self, identity):
        self._waiting_id = ""
        self.timer.stop()
        self._remaining = None
        self.sync()

    def _failed(self, message):
        if self._waiting_id:
            self._failed_ids.add(self._waiting_id)
            self._waiting_id = ""
        self._remaining = None
        self.sync()

    def advance(self):
        if not self._can_run() or self.library.busy:
            self.sync()
            return
        candidates = self.candidates()
        if len(candidates) < 2:
            self.sync()
            return
        current = self.library.selected_id
        if self.active_list["mode"] == "random":
            identity = random.choice([p for p in candidates if p != current] or candidates)
        else:
            index = candidates.index(current) if current in candidates else -1
            identity = candidates[(index + 1) % len(candidates)]
        self._waiting_id = identity
        self.library.select(identity)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Hide, QEvent.Close):
            self._visible = False
            self.sync()
        elif event.type() in (QEvent.Show, QEvent.WindowStateChange):
            self._visible = True
            self.sync()
        return super().eventFilter(watched, event)


class RotationEditor(QWidget):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.library = service.library
        self.draft = None
        self._loading = False
        outer = QVBoxLayout(self)
        active_row = QHBoxLayout()
        active_row.addWidget(QLabel(text("正在轮播", "Playing list")))
        self.active = QComboBox(self)
        self.active.currentIndexChanged.connect(self._activate)
        active_row.addWidget(self.active, 1)
        outer.addLayout(active_row)
        body = QHBoxLayout()
        left = QVBoxLayout()
        self.profiles = QListWidget(self)
        self.profiles.setMaximumWidth(config.sx(180))
        self.profiles.currentRowChanged.connect(self._edit_row)
        left.addWidget(self.profiles)
        actions = QHBoxLayout()
        for label, callback in ((text("新建", "New"), self.new_list),
                                (text("删除", "Delete"), self.delete_list)):
            button = TextButton(label, "ghost", height=32)
            button.clicked.connect(callback)
            actions.addWidget(button)
        left.addLayout(actions)
        body.addLayout(left)
        right = QVBoxLayout()
        form = QFormLayout()
        self.name = QLineEdit(self)
        self.name.setMaxLength(80)
        self.seconds = QSpinBox(self)
        self.seconds.setRange(10, 86400)
        self.seconds.setSuffix(text(" 秒", " s"))
        self.mode = QComboBox(self)
        self.mode.addItem(text("顺序", "In order"), "ordered")
        self.mode.addItem(text("随机", "Random"), "random")
        form.addRow(text("列表名称", "List name"), self.name)
        form.addRow(text("切换间隔", "Interval"), self.seconds)
        form.addRow(text("播放顺序", "Order"), self.mode)
        right.addLayout(form)
        self.hint = QLabel(text("点击缩略图加入或移出；下方列表可拖动调整顺序。保存后生效。",
                               "Click thumbnails to add or remove. Drag selected items to reorder. Save to apply."))
        self.hint.setWordWrap(True)
        right.addWidget(self.hint)
        from .wallpaper_gallery import GalleryGrid
        self.grid = GalleryGrid(self.library, selection_mode=True, parent=self)
        self.grid.activated.connect(self.toggle_item)
        right.addWidget(self.grid, 3)
        self.selected = QListWidget(self)
        self.selected.setDragDropMode(QAbstractItemView.InternalMove)
        self.selected.setMaximumHeight(config.sx(110))
        right.addWidget(self.selected, 1)
        footer = QHBoxLayout()
        self.cancel_button = TextButton(text("取消", "Cancel"), "subtle", height=34)
        self.cancel_button.clicked.connect(self.cancel_edit)
        self.save_button = TextButton(text("保存列表", "Save list"), "primary", height=34)
        self.save_button.clicked.connect(self.save)
        footer.addStretch(1)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.save_button)
        right.addLayout(footer)
        body.addLayout(right, 1)
        outer.addLayout(body, 1)
        self.service.changed.connect(self.refresh)
        self.library.changed.connect(self._refresh_grid)
        self.refresh()
        if self.service.state["lists"]:
            self.profiles.setCurrentRow(0)
        else:
            self.new_list()

    def refresh(self):
        self._loading = True
        editing_id = self.draft["id"] if self.draft else ""
        self.active.clear()
        self.active.addItem(text("关闭轮播", "Rotation off"), "")
        self.profiles.clear()
        row = -1
        for i, profile in enumerate(self.service.state["lists"]):
            self.active.addItem(profile["name"], profile["id"])
            self.profiles.addItem(profile["name"])
            if profile["id"] == editing_id:
                row = i
        self.active.setCurrentIndex(max(0, self.active.findData(self.service.state["active"])))
        if row >= 0:
            self.profiles.setCurrentRow(row)
        self._loading = False

    def _activate(self, index):
        if not self._loading:
            self.service.activate(self.active.itemData(index) or "")

    def _edit_row(self, row):
        if self._loading or not 0 <= row < len(self.service.state["lists"]):
            return
        self._load(copy.deepcopy(self.service.state["lists"][row]))

    def _load(self, draft):
        self.draft = draft
        self.selected.clear()
        self.name.setText(draft["name"])
        self.seconds.setValue(draft["seconds"])
        self.mode.setCurrentIndex(self.mode.findData(draft["mode"]))
        self._refresh_grid()

    def new_list(self):
        self.profiles.setCurrentRow(-1)
        self._load({"id": uuid.uuid4().hex, "name": text("新轮播列表", "New rotation"),
                    "seconds": 300, "mode": "ordered", "items": []})

    def _selected_ids(self):
        return [self.selected.item(i).data(Qt.UserRole) for i in range(self.selected.count())]

    def _refresh_grid(self):
        if not self.draft:
            return
        items = self._selected_ids() if self.selected.count() else self.draft["items"]
        self.selected.clear()
        for identity in items:
            project = self.library.store.project(identity)
            item = QListWidgetItem(project.title if project else text("缺失项目", "Missing item") + " · " + identity)
            item.setData(Qt.UserRole, identity)
            self.selected.addItem(item)
        self.grid.set_checked(items)

    def toggle_item(self, identity):
        if not self.draft:
            return
        items = self._selected_ids()
        if identity in items:
            items.remove(identity)
        elif self.library.store.eligible(identity):
            items.append(identity)
        self.draft["items"] = items
        self.selected.clear()
        self._refresh_grid()

    def save(self):
        if not self.draft or not self.name.text().strip():
            self.name.setFocus()
            return
        self.draft.update(name=self.name.text().strip(), seconds=self.seconds.value(),
                          mode=self.mode.currentData(), items=self._selected_ids())
        self.service.save_list(self.draft)

    def cancel_edit(self):
        if not self.draft:
            return
        saved = next((p for p in self.service.state["lists"] if p["id"] == self.draft["id"]), None)
        if saved:
            self._load(copy.deepcopy(saved))
        else:
            self.new_list()

    def delete_list(self):
        if not self.draft:
            return
        self.service.delete_list(self.draft["id"])
        self.new_list()
