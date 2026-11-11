"""控制服务：把规则引擎、数据库、监控线程、遮罩与通知串起来。

职责：
    * 控制页密码的保存 / 校验 / 修改（含连续 3 次错误锁定 1 分钟、进入页面 1 分钟内免验证）；
    * "启用控制"总开关（关闭需要密码，关闭后规则暂停、遮罩立即消失）；
    * 每秒推进引擎：累计时长、维护状态机、写控制日志、发通知；
    * 把引擎判定交给遮罩管理器；
    * 网站被遮罩时暂停播放（优先用系统媒体会话 SMTC，见 ``_sync_site_playback``）；
    * 规则的增删改查（控制页 UI 通过这里改）。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

import psutil

from . import config
from . import site_watch
from . import smtc
from . import util
from .audio import ProcessMuter, playing_pids
from .control import ControlEngine, Decision, Rule, local_day_key
from .i18n import format_amount, tr
from .overlay import OverlayManager
from .notify import (EVENT_DAILY_LIMIT, EVENT_PAUSE, EVENT_UNLOCKED, Notifier)

S_PASSWORD = "control.password"
S_ENABLED = "control.enabled"
S_FAIL_COUNT = "control.fail_count"
S_LOCK_UNTIL = "control.lock_until"

MAX_FAILS = 3
LOCK_SECONDS = 60
SESSION_GRACE = config.SESSION_GRACE   # 两次进入控制页间隔不超过 1 分钟免验证


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 120000)
    return f"{salt}${digest.hex()}"


def verify_password(stored: str, password: str) -> bool:
    if not stored or "$" not in stored:
        return False
    salt, _ = stored.split("$", 1)
    try:
        return hash_password(password, salt) == stored
    except ValueError:
        return False


class ControlService(QObject):
    """控制功能的运行时服务。"""

    state_changed = Signal()
    ticked = Signal()                      # 每秒一次：UI 用它刷新实时状态
    locked_changed = Signal(bool)          # 是否有程序被遮罩
    unlock_requested = Signal(int)         # 遮罩上点击"临时解锁"（由 UI 弹密码框）
    close_program_requested = Signal(int)  # 遮罩上点击"关闭程序"（由 UI 确认后结束进程）

    TICK_MS = 1000
    SAVE_MS = 5000

    #: 网站被遮罩时"暂停播放"的策略（见 ``_sync_site_playback``）：
    #: 主通道是系统媒体会话（SMTC，幂等暂停，后台线程每秒一次）；
    #: 只有 SMTC 用不了时才退回媒体键，媒体键每段播放最多补按这么多次
    #: （它是 toggle，按多了会把暂停的视频放起来）；两次按键至少隔 ``PAUSE_RETRY_S``；
    #: 进程树安静这么久之后重新武装（视频下次被放起来还能压回去）。
    PAUSE_RETRY_S = 1.5
    PAUSE_FALLBACK_MAX = 3
    QUIET_REARM_S = 2.0

    def __init__(self, db, tracker, tray=None, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.db = db
        self.tracker = tracker
        self.notifier = Notifier(db, tray)
        self.engine = ControlEngine(enabled=db.get_bool(S_ENABLED, False))
        self._verified_at = 0.0
        self._last_tick = time.time()
        self._last_save = time.time()

        self._muter = ProcessMuter(self)      # 带持久记忆：崩了/被强杀也能在上次启动时还原
        #: 网站暂停键的状态：program_id -> (键, 已按次数, 上次按的时刻, 上一拍是否在播)。
        #: 键 = (窗口, 网址)，所以同一个网站换了视频/换了窗口会重新武装。
        self._pause_state: dict[int, tuple] = {}
        #: 可注入的"谁在出声"探测（自检里换成假数据，真机上是 WASAPI 会话状态）
        self._pause_probe: Callable[[], set] = playing_pids
        #: 可注入的"按一次播放/暂停键"（自检里换成计数器，真机上打 VK_MEDIA_PLAY_PAUSE）
        self._pause_send: Callable[[], bool] = site_watch.pause_media
        #: 系统媒体会话（SMTC）后台监听：它是网站暂停的主通道，媒体键只是回退
        self._smtc = smtc.Watcher()
        self._smtc_pids: set = set()          # 已经武装过的浏览器进程集合
        self.overlays = OverlayManager(self._blocked_decisions, self._running_pids,
                                       lock_provider=self.lock_remaining,
                                       muter=self._muter,
                                       site_provider=self._site_targets)
        self.overlays.unlock_requested.connect(self.unlock_requested.emit)
        self.overlays.close_requested.connect(self.close_program_requested.emit)
        # 启动兜底：上一次没来得及还原就被关掉时，这里把音量放回去
        try:
            self._muter.recover()
        except Exception:
            pass

        self.reload()
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self.tick)
        self._timer.start()

    # ------------------------------------------------------------------
    # 被静音程序的"原本音量"记忆（ProcessMuter 的持久存储）
    # ------------------------------------------------------------------
    def load_audio_originals(self) -> dict:
        raw = self.db.get_setting(config.S_AUDIO_ORIGINALS, "")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_audio_originals(self, originals: dict) -> None:
        try:
            self.db.set_setting(config.S_AUDIO_ORIGINALS,
                                json.dumps(originals, ensure_ascii=False))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 密码
    # ------------------------------------------------------------------
    def has_password(self) -> bool:
        return bool(self.db.get_setting(S_PASSWORD, ""))

    def set_password(self, password: str) -> None:
        self.db.set_setting(S_PASSWORD, hash_password(password))
        self.db.set_setting(S_FAIL_COUNT, 0)
        self.db.set_setting(S_LOCK_UNTIL, 0)
        self._verified_at = time.time()
        self.notifier.on_password_changed()

    def verify(self, password: str) -> tuple[bool, str]:
        """校验密码；返回 (是否通过, 失败原因)。"""
        now = time.time()
        lock_until = self.db.get_float(S_LOCK_UNTIL, 0.0)
        if lock_until > now:
            return False, "locked"
        if verify_password(self.db.get_setting(S_PASSWORD, ""), password):
            self.db.set_setting(S_FAIL_COUNT, 0)
            self.db.set_setting(S_LOCK_UNTIL, 0)
            self._verified_at = now
            return True, ""
        fails = int(self.db.get_float(S_FAIL_COUNT, 0.0)) + 1
        self.db.set_setting(S_FAIL_COUNT, fails)
        if fails >= MAX_FAILS:
            self.db.set_setting(S_LOCK_UNTIL, now + LOCK_SECONDS)
            self.db.set_setting(S_FAIL_COUNT, 0)
            return False, "just_locked"
        return False, "wrong"

    def lock_remaining(self) -> float:
        return max(0.0, self.db.get_float(S_LOCK_UNTIL, 0.0) - time.time())

    def session_grace(self) -> float:
        """离开控制页后免验证的秒数（设置页按分钟配置，0 = 每次进入都要密码）。"""
        default_minutes = max(0.0, SESSION_GRACE / 60.0)
        try:
            minutes = float(self.db.get_float(config.S_SESSION_GRACE, default_minutes))
        except (TypeError, ValueError):
            minutes = default_minutes
        return max(0.0, minutes) * 60.0

    def set_session_grace(self, minutes: float) -> None:
        """写入免验证间隔（分钟，0 = 每次进入控制页都要重新输密码）。"""
        try:
            value = max(0.0, float(minutes))
        except (TypeError, ValueError):
            return
        self.db.set_setting(config.S_SESSION_GRACE, value)

    def session_valid(self) -> bool:
        """距上次验证不超过用户设定的免验证窗口：无需重复输入。"""
        return time.time() - self._verified_at <= self.session_grace()

    def touch_session(self) -> None:
        self._verified_at = time.time()

    # ------------------------------------------------------------------
    # 总开关
    # ------------------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.engine.enabled)

    def set_enabled(self, enabled: bool) -> None:
        self.engine.enabled = bool(enabled)
        self.db.set_setting(S_ENABLED, 1 if enabled else 0)
        if not enabled:
            self.engine.clear_all_locks()
            self.overlays.clear()
        self.save(force=True)
        self.state_changed.emit()

    # ------------------------------------------------------------------
    # 规则
    # ------------------------------------------------------------------
    def reload(self) -> None:
        rules = self.db.all_control_rules()
        states = self.db.all_control_state()
        mine = self.self_program_ids()
        if mine:
            # 历史上可能已经把自己加进过限制列表：这里顺手清掉，避免留下看不见的规则
            for pid in list(rules):
                if int(pid) in mine:
                    self.db.delete_control_rules(int(pid))
                    rules.pop(pid, None)
                    states.pop(pid, None)
        self.engine.load(rules, states)
        self._saved_states = {pid: state.to_fields() for pid, state in self.engine.states.items()}

    def self_program_ids(self) -> set[int]:
        """Monitor 自己对应的程序 id（禁止加入"程序使用限制"）。"""
        ids: set[int] = set()
        try:
            rows = self.db.programs(visible_only=False, include_deleted=True)
        except Exception:
            return ids
        for row in rows:
            try:
                if util.is_self_path(row["path"]):
                    ids.add(int(row["id"]))
            except (KeyError, TypeError, ValueError):
                continue
        return ids

    def is_self_program(self, program_id: int) -> bool:
        """该程序是不是 Monitor 自己（开发运行时主体是 pythonw.exe）。"""
        pid = int(program_id)
        if pid in self.self_program_ids():
            return True
        try:
            row = self.db.get_program(pid)
        except Exception:
            return False
        if row is None:
            return False
        try:
            if util.is_self_path(row["path"]):
                return True
        except (KeyError, TypeError):
            pass
        name = ""
        try:
            name = str(row["name"] or "")
        except (KeyError, TypeError):
            name = ""
        return name.strip().lower() in ("monitor", "python", "pythonw")

    def rules_for(self, program_id: int) -> Optional[Rule]:
        return self.engine.rules.get(int(program_id))

    def all_rules(self) -> dict[int, Rule]:
        return dict(self.engine.rules)

    def save_rule(self, rule: Rule) -> None:
        if self.is_self_program(rule.program_id):
            # Monitor 自己不允许被限制（否则会把自己遮罩 / 静音，无法再操作）
            self.db.delete_control_rules(int(rule.program_id))
            self.engine.drop_program(int(rule.program_id))
            self._saved_states.pop(int(rule.program_id), None)
            self.state_changed.emit()
            return
        self.db.set_control_rules(
            rule.program_id,
            daily_enabled=rule.daily_enabled, daily_value=rule.daily_value,
            daily_unit=rule.daily_unit,
            streak_enabled=rule.streak_enabled, streak_value=rule.streak_value,
            streak_unit=rule.streak_unit,
            pause_value=rule.pause_value, pause_unit=rule.pause_unit,
            window_enabled=rule.window_enabled, windows=rule.windows,
        )
        # set_rule 会把"已关闭的规则"对应的计时与遮罩状态清零（见 Rule/reset 说明）
        self.engine.set_rule(rule)
        fields = self.engine.state(rule.program_id).to_fields()
        self.db.save_control_state(rule.program_id, **fields)
        self._saved_states[rule.program_id] = fields
        self.overlays.sync()          # 关掉规则后遮罩要立刻消失，不等下一次 tick
        self.state_changed.emit()

    def delete_rule(self, program_id: int) -> None:
        self.db.delete_control_rules(program_id)
        self.engine.drop_program(program_id)
        self._saved_states.pop(program_id, None)
        self.overlays.sync()          # 删除规则后立刻撤掉遮罩
        self.state_changed.emit()

    def state_summary(self, program_id: int) -> dict:
        return self.engine.snapshot_state(program_id)

    # ------------------------------------------------------------------
    # 遮罩 / 临时解锁
    # ------------------------------------------------------------------
    def _blocked_decisions(self) -> dict[int, Decision]:
        return self.engine.blocked_programs()

    def _running_pids(self, program_id: int) -> list[int]:
        """该程序当前运行中的系统 PID 列表（遮罩按 PID 找窗口）。"""
        try:
            return [int(pid) for pid, _start in self.tracker.snapshot().get(int(program_id), [])]
        except Exception:
            return []

    def _site_ids(self) -> set:
        """当前配置里属于"网站"的 program_id 集合。"""
        try:
            return {int(pid) for pid in self.tracker.site.patterns()}
        except Exception:
            return set()

    def _site_targets(self) -> dict:
        """给遮罩管理器的网站目标：{program_id: {pages, rect, windows, mute_pids, block_pids}}。

        * ``pages``：要盖遮罩的窗口列表。第一个是**页面内容区**（不含标签栏 / 工具栏）——
          遮罩只盖这一块，标签栏留着可点，用户才能照常切换标签页（以前遮住整个浏览器
          窗口是 bug）；后面跟着同一个浏览器的**附属小窗（画中画）**，它们带
          ``compact = True``，盖"紧凑遮罩"（只有标题 + 倒计时 + 操作按钮）。
        * ``rect`` / ``windows``：页面内容区矩形与页面 hwnd（兼容字段，= ``pages[0]``）。
        * ``mute_pids``：该浏览器的**渲染进程 + 根进程**。Chromium 的音频其实是共享的
          ``audio.mojom.AudioService`` 子进程在播（实测：msedge 的音频会话挂在子进程
          17860 上，不是根进程 9380），只压渲染进程等于没压；遮罩管理器会把它展开成
          整棵进程树再静音。
        * ``block_pids``：**空**。网站目标不用低级钩子拦输入：钩子是按前台窗口 PID
          判定 的，一拦就把整个浏览器（含标签栏、Ctrl+Tab）一起吞掉，用户就没法切
          标签页了。页面区的点击由盖在上面那块遮罩自己吃掉，够用。
        """
        try:
            pages = self.tracker.site.snapshot()
        except Exception:
            return {}
        if not pages:
            return {}
        try:
            blocked = set(int(pid) for pid in self.engine.blocked_programs())
        except Exception:
            return {}
        result: dict[int, dict] = {}
        for program_id, page in pages.items():
            pid = int(program_id)
            if pid not in blocked:
                continue
            window = int(page.get("hwnd") or 0)
            pip_only = bool(page.get("pip_only"))
            if not window and not pip_only:
                continue
            mute: set = set()
            renderer = int(page.get("renderer_pid") or 0)
            root = int(page.get("root_pid") or 0)
            if renderer > 0:
                mute.add(renderer)
            if root > 0 and not pip_only:
                # pip_only 的 renderer 是**画中画**自己的渲染进程；root 是整棵浏览器
                # 进程树，压它会连同别的标签页一起静音，所以只压渲染进程。
                mute.add(root)
            rect = page.get("rect")
            try:
                rect = tuple(int(v) for v in rect) if rect else None
            except Exception:
                rect = None
            targets: list[dict] = []
            if not pip_only:
                targets.append({"hwnd": window, "rect": rect, "compact": False,
                                "frame": page.get("frame")})
            for aux in page.get("aux") or ():
                try:
                    aux_hwnd = int(aux.get("hwnd") or 0)
                    aux_rect = aux.get("rect")
                    aux_rect = tuple(int(v) for v in aux_rect) if aux_rect else None
                except Exception:
                    continue
                if not aux_hwnd or aux_hwnd == window:
                    continue
                targets.append({"hwnd": aux_hwnd, "rect": aux_rect, "compact": True})
            result[pid] = {"pages": targets,
                           "windows": [item["hwnd"] for item in targets],
                           "rect": rect,
                           "renderer_hwnd": int(page.get("renderer_hwnd") or 0),
                           "mute_pids": mute, "block_pids": set()}
        return result


    def unlock(self, program_id: int, minutes: float) -> bool:
        ok = self.engine.unlock(int(program_id), minutes)
        if ok:
            program = self.db.get_program(int(program_id))
            name = str(program["name"]) if program else str(program_id)
            self.db.add_control_log(int(program_id),
                                    tr("control.log.unlocked", name=name,
                                       amount=format_amount(int(minutes), "m")),
                                    kind="unlock")
            self.notifier.on_program_event(EVENT_UNLOCKED, name, program_id=int(program_id),
                                           unlock_minutes=minutes)
            self.db.save_control_state(int(program_id),
                                       **self.engine.state(int(program_id)).to_fields())
        self.overlays.sync()
        self.state_changed.emit()
        return ok

    def locked_programs(self) -> dict[int, Decision]:
        return self.engine.blocked_programs()

    # ------------------------------------------------------------------
    # 每秒推进
    # ------------------------------------------------------------------
    def tick(self) -> None:
        now = time.time()
        delta = max(0.0, min(5.0, now - self._last_tick))
        self._last_tick = now
        if delta <= 0:
            return

        try:
            snapshot = self.tracker.snapshot()
        except Exception:
            snapshot = {}
        running = set(snapshot.keys())

        if self.engine.enabled:
            events = self.engine.tick(running, now=now, delta=delta)
            for event in events:
                program = self.db.get_program(event.program_id)
                name = str(program["name"]) if program else str(event.program_id)
                if event.kind == "daily_limit":
                    self.db.add_control_log(
                        event.program_id,
                        tr("control.log.daily_limit", name=name,
                           amount=self._daily_amount(event.program_id)),
                        kind="daily_limit", ts=event.at)
                    self.notifier.on_program_event(EVENT_DAILY_LIMIT, name,
                                                   program_id=event.program_id)
                elif event.kind == "pause":
                    seconds = int(event.extra.get("seconds", 0))
                    self.db.add_control_log(
                        event.program_id,
                        tr("control.log.pause", name=name,
                           amount=format_amount(max(1, seconds), "s")),
                        kind="pause", ts=event.at)
                    self.notifier.on_program_event(EVENT_PAUSE, name, program_id=event.program_id)
                elif event.kind == "window":
                    # 指定时间段：只写一条使用日志，方便事后对照"为什么这段时间没有记录"
                    seconds = int(event.extra.get("until", 0))
                    self.db.add_control_log(
                        event.program_id,
                        tr("control.log.window", name=name,
                           amount=format_amount(max(1, seconds), "s")),
                        kind="window", ts=event.at)
            # 追踪时长阈值通知
            for program_id in running:
                program = self.db.get_program(int(program_id))
                if program is None:
                    continue
                state = self.engine.state(int(program_id))
                self.notifier.on_duration(int(program_id), str(program["name"]),
                                          state.used_seconds)
            if now - self._last_save >= self.SAVE_MS / 1000.0:
                self.save()
                self._last_save = now

        self.overlays.sync()
        self._sync_site_playback()
        self.locked_changed.emit(bool(self.overlays.overlay_count()))
        self.ticked.emit()

    def _playing_pids(self) -> set:
        """当前正在出声的进程 PID 集合（读不到就当作"没在播"）。"""
        try:
            return {int(pid) for pid in self._pause_probe()}
        except Exception:
            return set()

    def _tree_playing(self, root_pid: int, playing: set) -> bool:
        """这棵进程树里有没有正在出声的音频会话。"""
        if not playing or not root_pid:
            return False
        if int(root_pid) in playing:
            return True
        try:
            children = psutil.Process(int(root_pid)).children(recursive=True)
        except Exception:
            return False
        return any(int(child.pid) in playing for child in children)

    def _sync_site_playback(self) -> None:
        """网站被限制时暂停播放（SMTC 权威通道 + 媒体键回退）。

        老实现是"网站一进限制集合就盲目按一次播放/暂停键"，有两个毛病：①去重键只是
        网站 id，同一个网站换了视频/页面就再也不会按；②不管浏览器在不在播，按下去可能
        把**暂停着**的视频放起来，浏览器没在播时这一下还会打到别的应用（微信）身上。

        再后来发现"WASAPI 会话 Active"并不等于"真的在播"：网页里常见一条**静音**的
        WebAudio 流一直保持 Active，于是补按把已经暂停的视频又放起来——这才是用户
        "遮罩期间视频还在播"的真正原因。所以现在分成两条通道：

        * **主通道 = 系统媒体会话（SMTC，``monitor/smtc.py``）**：后台线程每秒读一次
          权威播放状态，并对命中被遮罩浏览器的会话发幂等的 ``TryPauseAsync``
          （暂停状态的会话再按一次还是暂停，不会反跳）。只要 SMTC 可用就**绝不动
          媒体键**，连"按错一下"的机会都不给。
        * **回退通道 = 媒体键**（只在 SMTC 整条链用不了时，例如系统/环境不支持）：
          仍然靠 WASAPI 判断浏览器进程树有没有在出声，按 ``(窗口, 网址)`` 记状态、
          每段播放最多补按 ``PAUSE_FALLBACK_MAX`` 次（它是 toggle，按多了会反跳）、
          两次至少隔 ``PAUSE_RETRY_S``；进程树安静 ``QUIET_REARM_S`` 之后重新武装，
          于是视频下次被放起来还能压回去。

        解禁（临时解锁 / 换天）后**不自动恢复播放**——用户明确要的。
        """
        try:
            sites = self._site_ids()
        except Exception:
            return
        if not sites:
            self._pause_state = {}
            self._arm_smtc(set())
            return
        try:
            blocked = {int(pid) for pid in self.engine.blocked_programs()}
        except Exception:
            return
        try:
            pages = self.tracker.site.snapshot()
        except Exception:
            pages = {}
        current = blocked & sites
        targets: dict[int, tuple] = {}
        for program_id in current:
            page = pages.get(int(program_id))
            if not page:
                continue
            key = (int(page.get("hwnd") or 0), str(page.get("url") or ""))
            targets[int(program_id)] = (key, int(page.get("root_pid") or 0))
        for program_id in list(self._pause_state):
            if program_id not in targets:
                # 已经不在限制里（解锁 / 换天 / 关了页面）：忘掉状态，下次重新武装
                self._pause_state.pop(program_id, None)
        # SMTC 只关心"被遮罩的浏览器进程"；没有目标时不武装（后台线程转入空转）
        self._arm_smtc({root for _key, root in targets.values() if root})
        if not targets:
            return
        smtc_ok = self._smtc.usable()
        playing = self._playing_pids()
        now = time.monotonic()
        for program_id, (key, root) in targets.items():
            state = self._pause_state.get(program_id)
            if state is None or state[0] != key:
                state = (key, 0, 0.0, False)   # 换窗口 / 换视频：重新武装
            _seen, sent, last_at, was_playing = state
            live = self._tree_playing(root, playing)
            if not live:
                # 真的安静了（用户在别处点过播放键也不会一直按）：隔一小会儿重新武装，
                # 于是视频下次被放起来时还能压回去
                if was_playing or last_at:
                    if (now - last_at) >= self.QUIET_REARM_S:
                        sent = 0
                self._pause_state[program_id] = (key, sent, last_at, False)
                continue
            if not was_playing:
                sent = 0                        # 刚进入播放：重新武装
            if smtc_ok:
                # SMTC 通道：后台线程每秒幂等地把这条会话按回暂停（暂停状态的会话
                # 再按还是暂停，不会反跳）。这里只记账，绝不碰媒体键。
                self._pause_state[program_id] = (key, sent, now, True)
                continue
            if sent >= self.PAUSE_FALLBACK_MAX or (last_at and (now - last_at) < self.PAUSE_RETRY_S):
                self._pause_state[program_id] = (key, sent, last_at, True)
                continue
            if self._pause_send():
                self._pause_state[program_id] = (key, sent + 1, now, True)
            else:
                self._pause_state[program_id] = (key, sent, last_at, True)

    def _arm_smtc(self, pids: set) -> None:
        """把"被遮罩的浏览器进程"告诉后台 SMTC 监听器（集合没变就不重复调用）。"""
        pids = {int(pid) for pid in (pids or ()) if pid}
        if pids == self._smtc_pids:
            return
        self._smtc_pids = pids
        try:
            self._smtc.arm(pids)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def set_overlays_suspended(self, suspended: bool) -> None:
        """记录弹密码框状态；遮罩保持可见（见 OverlayManager.set_suspended）。"""
        self.overlays.set_suspended(bool(suspended))

    def _daily_amount(self, program_id: int) -> str:
        """每日上限的展示文案（含单位，跟随控制页选择的单位）。"""
        rule = self.engine.rules.get(int(program_id))
        if rule is None:
            return format_amount(0, "m")
        return format_amount(rule.daily_value, rule.daily_unit)

    def save(self, force: bool = False) -> None:
        current = {pid: state.to_fields() for pid, state in self.engine.states.items()}
        pending = {pid: fields for pid, fields in current.items()
                   if force or fields != self._saved_states.get(pid)}
        if pending:
            try:
                self.db.save_control_states(pending)
            except Exception:
                return  # 保留旧快照，下次重试整批
        self._saved_states = current

    def shutdown(self) -> None:
        try:
            self._timer.stop()
            self._smtc.stop()                 # 停掉 SMTC 后台轮询线程
            self.overlays.shutdown()          # 还原音量、卸载输入钩子、销毁遮罩
            self.save(force=True)
        except Exception:
            pass
