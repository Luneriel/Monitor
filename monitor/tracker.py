"""进程追踪：psutil 每 1 秒轮询一次，按 PID 感知进程启停并写入日志。

* 需求要求"使用 Windows 事件钩子感知程序启停"，同时接受轮询方案
  （强杀进程也能被捕获）——这里采用 1 秒轮询实现。
* 同一程序多实例分别记录（logs.pid），UI 合并成一张卡片并显示
  "当前运行时间最长的实例"时长。
* 被追踪的**网站**（``programs.kind = 'site'``）走同一套日志/事件流程：每秒问一次
  ``SiteWatcher``"前台窗口现在显示的是哪个被追踪的网站"，把命中的网站当成一个
  正在运行的实例合并进 ``found``——渲染进程 PID 作为实例 PID 记录，这样引擎的
  ``running``、日志、通知、时长统计全部自动复用。

"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterable, Optional

import psutil
from PySide6.QtCore import QThread, Signal

from .database import Database, normalize_path
from .site_watch import SiteWatcher

#: 取不到渲染进程 PID 时给网站实例用的合成 PID（只用于日志/引擎，不参与静音）
SITE_PID_BASE = 0x40000000


class ProcessTracker(QThread):
    """后台监控线程。"""

    instance_started = Signal(int, int, float)     # program_id, pid, start_ts
    instance_stopped = Signal(int, int, float)     # program_id, pid, end_ts
    programs_changed = Signal()                    # 卡片需要重建
    targets_changed = Signal()                    # 配置变化（含 notify=False）
    tick = Signal()                                # 每秒一次，用于刷新时长

    def __init__(self, db: Database, interval: float = 1.0, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.interval = max(0.2, float(interval))
        self._lock = threading.RLock()
        self._stop_flag = False
        self._programs: dict[int, dict[str, Any]] = {}     # program_id -> {path, added_time}
        self._path_index: dict[str, int] = {}              # normalize(path) -> program_id
        self._sites: dict[int, dict[str, Any]] = {}        # program_id -> {pattern, added_time}
        self.site = SiteWatcher()                          # 前台窗口网址（UI Automation）
        self._active: dict[int, dict[int, float]] = {}     # program_id -> {pid: start_ts}
        # 每个程序同一时间只保留一条日志（多实例合并为一次使用记录），
        # 避免像微信这类多进程程序在日志页刷出多条重复记录。
        self._log_ids: dict[int, int] = {}                 # program_id -> log id
        self._snapshot: dict[int, list[tuple[int, float]]] = {}
        # 本次运行才被（重新）开启追踪的程序：它已经运行的进程此前没有被计时，
        # 所以新的一次计时必须从"现在"算起（而不是进程的创建时间）。
        self._fresh: set[int] = set()
        self._initialized = False
        self._dirty = threading.Event()
        self._target_signature = None

    # ------------------------------------------------------------------
    # 外部接口（UI 线程调用）
    # ------------------------------------------------------------------
    def set_programs(self, rows: Iterable[Any], notify: bool = True) -> None:
        """刷新被追踪的程序/网站表（只包含开启追踪且未被删除的项）。

        ``notify=False`` 用于"调用方自己会就地更新界面"的场景（例如手动关闭追踪），
        避免再发 ``programs_changed`` 触发整列表重建、打断滑块过渡动画。
        """
        items = list(rows)
        signature = tuple(sorted((int(r["id"]), str(r["name"]), str(r["path"]),
                                  int(r["tracking"]), float(r["added_time"]),
                                  str(r["kind"]) if "kind" in r.keys() else "program")
                                 for r in items))
        with self._lock:
            if signature == self._target_signature:
                return
            self._target_signature = signature
        # 网站列表交给 SiteWatcher（它自己按 kind / tracking 过滤）
        try:
            self.site.configure(items)
        except Exception:
            pass
        programs: dict[int, dict[str, Any]] = {}
        sites: dict[int, dict[str, Any]] = {}
        index: dict[str, int] = {}
        for row in items:
            # 只追踪开关处于开启状态的项（关闭追踪后必须真正停止监控）
            if "tracking" in row.keys() and not int(row["tracking"]):
                continue
            pid = int(row["id"])
            if "kind" in row.keys() and str(row["kind"] or "program") == "site":
                sites[pid] = {"pattern": str(row["path"] or ""),
                              "added_time": float(row["added_time"] or 0.0)}
                continue
            path = normalize_path(row["path"])
            programs[pid] = {"path": path, "added_time": float(row["added_time"] or 0.0)}
            index[path] = pid
        with self._lock:
            removed = [pid for pid in list(self._programs) + list(self._sites)
                       if pid not in programs and pid not in sites]
            added = [pid for pid in programs if pid not in self._programs]
            self._programs = programs
            self._sites = sites
            self._path_index = index
            # 首次填充（程序启动时）保持"按进程真实创建时间计时"的行为；
            # 之后的每一次新增（例如重新打开追踪开关）都从当下开始计时。
            if self._initialized:
                self._fresh.update(added)
            self._initialized = True
        if removed:
            # 关闭已被取消追踪的程序当前实例
            now = time.time()
            with self._lock:
                for pid in removed:
                    log_id = self._log_ids.pop(pid, None)
                    if log_id is not None:
                        self.db.close_log(log_id, now)
                    self._active.pop(pid, None)
                    self._snapshot.pop(pid, None)
                    self.instance_stopped.emit(pid, -1, now)
            if notify:
                self.programs_changed.emit()
        self._dirty.set()
        self.targets_changed.emit()

    def stop(self) -> None:
        self._stop_flag = True
        self._dirty.set()

    def snapshot(self) -> dict[int, list[tuple[int, float]]]:
        """返回当前运行中的实例：program_id -> [(pid, start_ts), ...]。"""
        with self._lock:
            return {pid: list(items.items()) for pid, items in self._active.items()}

    def longest_instance(self, program_id: int) -> Optional[tuple[int, float]]:
        """当前运行时间最长的实例（开始时间最早的那个）。"""
        with self._lock:
            items = self._active.get(program_id)
            if not items:
                return None
            pid, start = min(items.items(), key=lambda kv: kv[1])
            return pid, start

    def close_program(self, program_id: int, end_ts: Optional[float] = None) -> None:
        """立即结束某程序的当前使用记录（用于删除程序 / 关闭追踪）。"""
        now = end_ts or time.time()
        with self._lock:
            log_id = self._log_ids.pop(program_id, None)
            if log_id is not None:
                self.db.close_log(log_id, now)
            self._active.pop(program_id, None)
            self._snapshot.pop(program_id, None)

    # ------------------------------------------------------------------
    # 线程主体
    # ------------------------------------------------------------------
    def run(self) -> None:  # noqa: D102
        self._resync()
        while not self._stop_flag:
            self._dirty.clear()
            started = time.monotonic()
            try:
                self._scan()
            except Exception:
                pass
            self.tick.emit()
            # 配置变更/退出会立即唤醒；不再每 100ms 轮询事件。
            remaining = max(0.0, self.interval - (time.monotonic() - started))
            if not self._stop_flag:
                self._dirty.wait(remaining)
        self.close_all()

    def close_all(self) -> None:
        now = time.time()
        with self._lock:
            for program_id in list(self._log_ids.keys()):
                log_id = self._log_ids.pop(program_id, None)
                if log_id is not None:
                    self.db.close_log(log_id, now)
            self._active.clear()
            self._snapshot.clear()

    def _resync(self) -> None:
        """每 5 秒从数据库重新读取一次追踪列表，防止外部改动。"""
        try:
            self.set_programs(self.db.programs(visible_only=True))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 单次扫描
    # ------------------------------------------------------------------
    def _scan(self) -> None:
        now = time.time()
        with self._lock:
            index = dict(self._path_index)
            programs = dict(self._programs)
            fresh = set(self._fresh)
            has_sites = bool(self._sites)

        if not index and not has_sites:
            with self._lock:
                if self._active:
                    self.close_all_locked(now)
            self._maybe_resync(now)
            return

        found: dict[int, dict[int, float]] = {}
        for proc in psutil.process_iter(["pid", "exe"]):
            try:
                exe = proc.info.get("exe")
                if not exe:
                    continue
                key = normalize_path(exe)
                program_id = index.get(key)
                if program_id is None:
                    continue
                proc_pid = int(proc.info["pid"])
                entry = found.setdefault(program_id, {})
                if proc_pid in entry:
                    continue
                added = programs.get(program_id, {}).get("added_time", 0.0)
                start = now
                if program_id in fresh:
                    # 本次才重新开启追踪：已运行的进程此前未被计时，这次从零开始
                    start = now
                else:
                    try:
                        created = float(proc.create_time())
                        # 若进程早于"加入追踪"的时间点启动，则以检测时间为准
                        start = created if created >= added else now
                    except Exception:
                        start = now
                entry[proc_pid] = start
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue

        started_events: list[tuple[int, int, float]] = []   # (program_id, 代表 pid, 开始时间)
        stopped_events: list[tuple[int, float]] = []        # (program_id, 结束时间)

        # 网站目标：前台窗口当前显示的网址命中某个被追踪的网站时，当作"正在运行"。
        # 实例 PID 用承载该网页的渲染进程（静音就是按它生效的）。
        if has_sites:
            try:
                pages = self.site.scan(now)
            except Exception:
                pages = {}
            for program_id, page in (pages or {}).items():
                proc_pid = int(page.get("renderer_pid") or 0)
                if proc_pid <= 0:
                    proc_pid = SITE_PID_BASE + int(program_id)
                found.setdefault(int(program_id), {})[proc_pid] = float(page.get("start") or now)

        with self._lock:
            # 扫描期间追踪列表可能被改动（例如用户关掉了追踪开关）：
            # 必须以"此刻"的列表为准，否则会给已经关闭追踪的程序重新开一条日志
            found = {pid: items for pid, items in found.items()
                     if pid in self._programs or pid in self._sites}
            for program_id, items in found.items():
                was_active = bool(self._active.get(program_id))
                active = self._active.setdefault(program_id, {})
                for proc_pid, start in items.items():
                    active.setdefault(proc_pid, start)
                # 同一程序的多个进程只合并成"一次使用"：仅在从无到有时写一条日志，
                # 代表实例取运行时间最长（开始时间最早）的那个 PID。
                if not was_active and items:
                    proc_pid, start = min(items.items(), key=lambda kv: kv[1])
                    started_events.append((program_id, proc_pid, start))
            # 结束消失的实例；程序的所有实例都消失才算一次使用结束
            for program_id in list(self._active.keys()):
                current = found.get(program_id, {})
                for proc_pid in list(self._active[program_id].keys()):
                    if proc_pid not in current:
                        del self._active[program_id][proc_pid]
                if not self._active[program_id]:
                    del self._active[program_id]
                    stopped_events.append((program_id, now))
            self._snapshot = {pid: dict(items) for pid, items in self._active.items()}

        for program_id, proc_pid, start in started_events:
            # 写库与"是否仍在追踪"的判定放在同一把锁里：关闭追踪后绝不再新开日志
            with self._lock:
                # 程序与网站都可能在这里新开日志：两类都要认（网站只登记在 _sites 里）
                if ((program_id not in self._programs and program_id not in self._sites)
                        or program_id in self._log_ids):
                    continue
                try:
                    log_id = self.db.open_log(program_id, proc_pid, start)
                except Exception:
                    continue
                self._log_ids[program_id] = log_id
                self._fresh.discard(program_id)
            try:
                self.db.touch_last_run(program_id, start)
            except Exception:
                pass
            self.instance_started.emit(program_id, proc_pid, start)

        for program_id, end_ts in stopped_events:
            with self._lock:
                log_id = self._log_ids.pop(program_id, None)
            try:
                if log_id is not None:
                    self.db.close_log(log_id, end_ts)
                self.db.touch_last_run(program_id, end_ts)
            except Exception:
                pass
            self.instance_stopped.emit(program_id, -1, end_ts)

        if started_events or stopped_events:
            self.programs_changed.emit()

        self._maybe_resync(now)

    def close_all_locked(self, now: float) -> None:
        for program_id in list(self._log_ids.keys()):
            log_id = self._log_ids.pop(program_id, None)
            if log_id is not None:
                self.db.close_log(log_id, now)
        self._active.clear()
        self._snapshot.clear()

    _last_resync = 0.0

    def _maybe_resync(self, now: float) -> None:
        if now - self._last_resync >= 5.0:
            self._last_resync = now
            self._resync()
