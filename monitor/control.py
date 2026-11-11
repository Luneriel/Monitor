"""控制规则引擎：指定时间段 + 每日使用上限 + 连续使用强制暂停 + 临时解锁。

设计要点（对应需求）：
    * 有效使用 = 程序正在运行、**不处于指定时间段**、**不处于强制暂停**、
      且**不处于临时解锁**期间——一句话：只要遮罩生效就不计时；
    * 指定时间段按本地时间与星期判断，支持跨午夜（22:00–07:00 归**开始那天**，
      所以"周三 22:00–07:00"会一路挡到周四早上 7 点）；
    * 时段限制结束时把连续累计与休息清零：时段里本来就不该累计，但"时段挡住、
      期间程序一直开着"如果不清零，时段一结束就会连上时段前的旧累计立刻触发
      强制暂停；
    * 每日累计按本地 0 点重置；
    * 连续累计达到 X 分钟触发强制暂停，暂停时长 = Y − 已累计休息时间
      （休息时间在程序完全退出期间累积）；
    * 程序在累计未达 X 时退出并重新打开：休息 ≥ Y 则连续累计与休息都清零，
      否则两者都保留；
    * 临时解锁 = 整段停表：解锁期间不累计任何时长，也不产生新的"达到上限 /
      触发暂停"事件；解锁开始时连续累计清零（正处在强制暂停时连暂停一起清除），
      解锁结束后每日累计从原值继续、连续累计从 0 重新累计。之所以必须停表：
      解锁期间遮罩本来就不出现（优先级最高），如果照旧累计，连续规则会每隔
      X 秒又触发一次强制暂停——用户看到的是计时反复停住、还每 X 秒弹一次
      "强制停止"通知，却没有任何遮罩，看起来像临时解锁根本没生效；
    * 规则优先级：临时解锁 > 每日使用上限 > 连续使用强制暂停。

本模块只做纯逻辑（不碰 UI / 窗口），便于自检直接驱动时间轴验证状态机。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# 触发原因
REASON_NONE = ""
REASON_WINDOW = "window"
REASON_DAILY = "daily"
REASON_STREAK = "streak"

# 规则单位：秒 / 分钟 / 小时（控制页可切换，数据库里存单位与数值）
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}
UNIT_ORDER = ("h", "m", "s")           # 控制页下拉的顺序
UNIT_MAX = {"s": 86400, "m": 1440, "h": 24}   # 各单位的最大可填数值（统一为最多 24 小时）

# 星期掩码：bit0 = 周一 … bit6 = 周日
ALL_DAYS = 0b1111111
WORKDAYS = 0b0011111
WEEKEND = 0b1100000
MINUTES_PER_DAY = 24 * 60
DAY_KEYS = ("control.day.mon", "control.day.tue", "control.day.wed", "control.day.thu",
            "control.day.fri", "control.day.sat", "control.day.sun")


def unit_seconds(unit: str) -> int:
    """单位 -> 秒；未知单位按分钟处理（老库没有单位列时用 'm'）。"""
    return UNIT_SECONDS.get(str(unit or "m"), 60)


def local_day_key(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def seconds_to_midnight(ts: float) -> float:
    """距离次日 0 点的秒数。"""
    local = time.localtime(ts)
    tomorrow = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)) + 86400
    return max(0.0, tomorrow - ts)


def weekday_index(ts: float) -> int:
    """本地时间星期几：0 = 周一 … 6 = 周日（与星期掩码的位序一致）。"""
    return int(time.localtime(ts).tm_wday)


def day_start(ts: float) -> float:
    """本地时间当天 0 点的时间戳。"""
    local = time.localtime(ts)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))


def format_minute(minute: int) -> str:
    """分钟数 -> "HH:MM"。"""
    minute = max(0, min(MINUTES_PER_DAY - 1, int(minute)))
    return "%02d:%02d" % (minute // 60, minute % 60)


def days_label_key(days: int) -> str:
    """星期掩码 -> 现成的文案键（自定义组合由界面自己拼）。"""
    days = int(days) & ALL_DAYS
    if days == ALL_DAYS:
        return "control.days.all"
    if days == WORKDAYS:
        return "control.days.workdays"
    if days == WEEKEND:
        return "control.days.weekend"
    if days == 0:
        return "control.days.none"
    return "control.days.custom"


@dataclass
class Window:
    """一条"指定时间段无法使用"。

    ``start_minute`` / ``end_minute`` 是当天 0 点起的分钟数（0~1439），``days``
    是星期掩码（bit0 = 周一 … bit6 = 周日）。三种形态：

    * ``end > start``：当天内的普通时段（09:00–12:00）；
    * ``end <= start``：跨午夜（22:00–07:00），归**开始那天**——勾了周三就会从
      周三 22:00 一路挡到周四 07:00；
    * ``start == end``：全天（勾选的那几天整天不可用）。
    """

    start_minute: int = 0
    end_minute: int = 0
    days: int = ALL_DAYS

    @property
    def all_day(self) -> bool:
        return self.start_minute == self.end_minute

    @property
    def crosses_midnight(self) -> bool:
        return self.end_minute < self.start_minute

    def contains(self, now: float) -> bool:
        """``now`` 此刻是否落在本时段内。"""
        days = int(self.days) & ALL_DAYS
        local = time.localtime(now)
        minute = local.tm_hour * 60 + local.tm_min
        today = int(local.tm_wday)
        if self.all_day:
            return bool(days >> today & 1)
        if not self.crosses_midnight:
            return bool(days >> today & 1) and self.start_minute <= minute < self.end_minute
        # 跨午夜：要么今天到点开始，要么昨天开始、今天还没结束
        if minute >= self.start_minute:
            return bool(days >> today & 1)
        return bool(days >> ((today - 1) % 7) & 1) and minute < self.end_minute

    def seconds_until_end(self, now: float) -> float:
        """距离本时段结束的秒数（不判断此刻是否在时段内）。"""
        minute = time.localtime(now).tm_hour * 60 + time.localtime(now).tm_min
        offset = self.end_minute * 60
        if minute >= self.end_minute:
            offset += 86400
        return max(0.0, day_start(now) + offset - now)

    def to_row(self) -> list[int]:
        return [int(self.start_minute), int(self.end_minute), int(self.days)]

    @staticmethod
    def from_row(row: Any) -> Optional["Window"]:
        """从 ``[start, end, days]`` 还原；坏数据返回 None（直接丢弃）。"""
        try:
            start, end, days = int(row[0]), int(row[1]), int(row[2])
        except (TypeError, ValueError, IndexError, KeyError):
            return None
        if not (0 <= start < MINUTES_PER_DAY and 0 <= end < MINUTES_PER_DAY):
            return None
        if not 0 <= days <= ALL_DAYS:
            days = ALL_DAYS
        return Window(start_minute=start, end_minute=end, days=days)


def windows_from_rows(rows: Any) -> list[Window]:
    """数据库里的 JSON -> ``Window`` 列表。"""
    result: list[Window] = []
    if not isinstance(rows, (list, tuple)):
        return result
    for row in rows:
        window = Window.from_row(row)
        if window is not None:
            result.append(window)
    return result


def windows_to_rows(windows: Iterable[Window]) -> list[list[int]]:
    """``Window`` 列表 -> 可直接 json.dumps 的紧凑结构。"""
    return [w.to_row() for w in windows]


@dataclass
class Rule:
    program_id: int
    daily_enabled: bool = False
    daily_value: int = 0
    daily_unit: str = "m"
    streak_enabled: bool = False
    streak_value: int = 0
    streak_unit: str = "m"
    pause_value: int = 0
    pause_unit: str = "m"
    window_enabled: bool = False
    windows: list[Window] = field(default_factory=list)

    @property
    def daily_seconds(self) -> float:
        return max(0, int(self.daily_value)) * unit_seconds(self.daily_unit)

    @property
    def streak_seconds(self) -> float:
        return max(0, int(self.streak_value)) * unit_seconds(self.streak_unit)

    @property
    def pause_seconds(self) -> float:
        return max(0, int(self.pause_value)) * unit_seconds(self.pause_unit)

    @property
    def daily_active(self) -> bool:
        return bool(self.daily_enabled) and int(self.daily_value) > 0

    @property
    def streak_active(self) -> bool:
        return bool(self.streak_enabled) and int(self.streak_value) > 0

    @property
    def window_active(self) -> bool:
        return bool(self.window_enabled) and bool(self.windows)

    def window_block(self, now: float) -> Optional[Window]:
        """此刻生效的时段（没有则 None）。"""
        if not self.window_active:
            return None
        for window in self.windows:
            if window.contains(now):
                return window
        return None

    @property
    def has_rules(self) -> bool:
        return self.daily_active or self.streak_active or self.window_active


@dataclass
class State:
    program_id: int
    day_key: str = ""
    used_seconds: float = 0.0
    streak_seconds: float = 0.0
    rest_seconds: float = 0.0
    pause_until: float = 0.0
    unlock_until: float = 0.0
    limit_logged: str = ""
    pause_logged: bool = False
    running: bool = False          # 上一次 tick 程序是否在运行（用于休息累计）

    def to_fields(self) -> dict:
        return {
            "day_key": self.day_key,
            "used_seconds": self.used_seconds,
            "streak_seconds": self.streak_seconds,
            "rest_seconds": self.rest_seconds,
            "pause_until": self.pause_until,
            "unlock_until": self.unlock_until,
            "limit_logged": self.limit_logged,
            "pause_logged": 1 if self.pause_logged else 0,
        }


@dataclass
class Decision:
    """某一时刻对某个程序的判定结果（供遮罩与日志使用）。"""

    program_id: int
    blocked: bool = False
    reason: str = REASON_NONE
    remaining: float = 0.0        # 距离恢复可用的秒数
    text: str = ""                # 遮罩主文案（不含倒计时）
    unlocked: bool = False        # 处于临时解锁中

    @property
    def unlocked_remaining(self) -> float:
        return self.remaining if self.unlocked else 0.0


@dataclass
class Event:
    """一次 tick 产生的事件（写入日志 / 发通知）。"""

    kind: str                     # daily_limit | unlocked | pause
    program_id: int
    at: float = field(default_factory=time.time)
    extra: dict = field(default_factory=dict)


class ControlEngine:
    """维护每个程序的控制状态；由 UI 定时器每秒调用 ``tick``。"""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = bool(enabled)
        self.rules: dict[int, Rule] = {}
        self.states: dict[int, State] = {}
        # 上一次 tick 时哪些程序正被"指定时间段"挡住（只用于识别进入 / 离开）
        self._window_blocked: set[int] = set()

    # ------------------------------------------------------------------
    # 配置与状态装载（来自数据库）
    # ------------------------------------------------------------------
    @staticmethod
    def parse_rule(program_id: int, row: dict) -> Rule:
        """数据库行 -> ``Rule``（``windows`` 可能是 JSON 字符串 / 列表 / None）。"""
        rows = row.get("windows")
        if isinstance(rows, (str, bytes)):
            try:
                rows = json.loads(rows or "[]")
            except (TypeError, ValueError):
                rows = []
        return Rule(
            program_id=int(program_id),
            daily_enabled=bool(row.get("daily_enabled")),
            daily_value=int(row.get("daily_value") or 0),
            daily_unit=str(row.get("daily_unit") or "m"),
            streak_enabled=bool(row.get("streak_enabled")),
            streak_value=int(row.get("streak_value") or 0),
            streak_unit=str(row.get("streak_unit") or "m"),
            pause_value=int(row.get("pause_value") or 0),
            pause_unit=str(row.get("pause_unit") or "m"),
            window_enabled=bool(row.get("window_enabled")),
            windows=windows_from_rows(rows),
        )

    def load(self, rules: dict[int, dict], states: dict[int, dict]) -> None:
        self.rules = {
            int(pid): self.parse_rule(int(pid), row)
            for pid, row in rules.items()
        }
        self._window_blocked.clear()
        self.states = {}
        for pid, row in states.items():
            self.states[int(pid)] = State(
                program_id=int(pid),
                day_key=str(row.get("day_key") or ""),
                used_seconds=float(row.get("used_seconds") or 0.0),
                streak_seconds=float(row.get("streak_seconds") or 0.0),
                rest_seconds=float(row.get("rest_seconds") or 0.0),
                pause_until=float(row.get("pause_until") or 0.0),
                unlock_until=float(row.get("unlock_until") or 0.0),
                limit_logged=str(row.get("limit_logged") or ""),
                pause_logged=bool(row.get("pause_logged")),
            )

    def set_rule(self, rule: Rule, reset: bool = True) -> None:
        """写入/更新一条规则。

        ``reset=True``（默认）时，把**已关闭**的那部分规则的计时与遮罩状态清零：
        用户关掉"每日使用上限"后"今日已用"要从 0 重新开始，关掉"连续使用"后
        连续累计 / 休息累计 / 强制暂停都要清掉。否则会出现
        "关掉规则再打开，旧计时立刻又触发遮罩"的问题。
        """
        self.rules[rule.program_id] = rule
        state = self.state(rule.program_id)          # 保证状态存在
        if not reset:
            return
        if not rule.daily_active:
            state.used_seconds = 0.0
            state.limit_logged = ""
        if not rule.streak_active:
            state.streak_seconds = 0.0
            state.rest_seconds = 0.0
            state.pause_until = 0.0
            state.pause_logged = False
        if not rule.window_active:
            self._window_blocked.discard(rule.program_id)
        if not rule.has_rules:
            state.unlock_until = 0.0

    def drop_program(self, program_id: int) -> None:
        self.rules.pop(program_id, None)
        self.states.pop(program_id, None)
        self._window_blocked.discard(program_id)

    def state(self, program_id: int) -> State:
        state = self.states.get(program_id)
        if state is None:
            state = State(program_id=program_id)
            self.states[program_id] = state
        return state

    # ------------------------------------------------------------------
    # 临时解锁（只能从遮罩触发）
    # ------------------------------------------------------------------
    def unlock(self, program_id: int, minutes: float, now: Optional[float] = None) -> bool:
        """临时解锁：本次解锁结束前不允许再次延长。"""
        now = now or time.time()
        state = self.state(program_id)
        if state.unlock_until > now:
            return False                     # 不允许在本次结束前再次延长
        state.unlock_until = now + max(1, int(minutes)) * 60.0
        if state.pause_until > now:
            # 强制暂停期间解锁：直接清除暂停，连续累计清零，之后从 0 重新累计
            state.pause_until = 0.0
            state.pause_logged = False
        state.streak_seconds = 0.0
        state.rest_seconds = 0.0
        return True

    def clear_all_locks(self) -> None:
        """关闭总开关：立即解除所有遮罩状态。"""
        for state in self.states.values():
            state.pause_until = 0.0
            state.unlock_until = 0.0
            state.pause_logged = False

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    def decide(self, program_id: int, now: Optional[float] = None) -> Decision:
        now = now or time.time()
        decision = Decision(program_id=program_id)
        if not self.enabled:
            return decision
        rule = self.rules.get(program_id)
        if rule is None or not rule.has_rules:
            return decision
        state = self.state(program_id)

        # 优先级 1：临时解锁
        if state.unlock_until > now:
            decision.unlocked = True
            decision.remaining = state.unlock_until - now
            return decision

        # 优先级 2：指定时间段（临时解锁可解除，所以排在它后面）
        window = rule.window_block(now)
        if window is not None:
            decision.blocked = True
            decision.reason = REASON_WINDOW
            decision.remaining = window.seconds_until_end(now)
            return decision

        # 优先级 3：每日使用上限
        if rule.daily_active and state.used_seconds >= rule.daily_seconds:
            decision.blocked = True
            decision.reason = REASON_DAILY
            decision.remaining = seconds_to_midnight(now)
            return decision

        # 优先级 4：连续使用强制暂停
        if state.pause_until > now:
            decision.blocked = True
            decision.reason = REASON_STREAK
            decision.remaining = state.pause_until - now
            return decision

        return decision

    def blocked_programs(self, now: Optional[float] = None) -> dict[int, Decision]:
        now = now or time.time()
        result: dict[int, Decision] = {}
        if not self.enabled:
            return result
        for program_id in self.rules:
            decision = self.decide(program_id, now)
            if decision.blocked:
                result[program_id] = decision
        return result

    # ------------------------------------------------------------------
    # 时间推进
    # ------------------------------------------------------------------
    def tick(self, running: Iterable[int], now: Optional[float] = None,
             delta: float = 1.0) -> list[Event]:
        """推进 ``delta`` 秒；``running`` 为当前正在运行的程序 id。"""
        now = now or time.time()
        events: list[Event] = []
        running_set = {int(pid) for pid in running}
        if not self.enabled:
            for state in self.states.values():
                state.running = state.program_id in running_set
            return events

        for program_id, rule in list(self.rules.items()):
            state = self.state(program_id)
            state = self._advance_day(state, now)

            is_running = program_id in running_set
            was_running = state.running

            # 程序重新打开：按休息时长决定是否重置连续累计
            if is_running and not was_running:
                if rule.pause_seconds > 0 and state.rest_seconds >= rule.pause_seconds:
                    state.streak_seconds = 0.0
                    state.rest_seconds = 0.0
                    state.pause_until = 0.0
                    state.pause_logged = False
                elif state.pause_until > now and state.rest_seconds >= rule.pause_seconds:
                    state.pause_until = 0.0

            state.running = is_running

            # 临时解锁 = 整段停表（详见模块 docstring）：解锁期间不累计每日时长、
            # 不累计连续使用，也不触发"达到上限 / 强制暂停"事件。
            # 少了这一步，连续规则会在解锁期间每满 X 秒又记一次"强制停止"日志并
            # 再发一次通知（遮罩却因解锁优先级不出现），看起来像解锁没生效。
            if state.unlock_until and state.unlock_until <= now:
                state.unlock_until = 0.0         # 解锁刚结束：这一 tick 起恢复计时
            if state.unlock_until > now:
                continue

            blocking = (rule.daily_active
                        and state.used_seconds >= rule.daily_seconds)
            # 暂停刚好结束：必须在这里先把连续累计清零。
            # 否则同一 tick 后面的"达到阈值"判断仍成立，会立刻又开一次暂停，
            # 表现为倒计时走完 → 新一轮暂停 → 永远解不开。
            if state.pause_until and state.pause_until <= now:
                state.pause_until = 0.0
                state.pause_logged = False
                state.streak_seconds = 0.0
                state.rest_seconds = 0.0
            in_pause = state.pause_until > now

            # 指定时间段：进入 / 离开各处理一次。
            # 离开时段时把连续累计与休息清零——时段里本来就不该累计，但"时段挡住、
            # 期间程序一直开着"如果不清零，时段一结束就会连上时段前的旧累计立刻
            # 触发强制暂停（用户看到的是刚能用了马上又被暂停）。
            window = rule.window_block(now)
            if window is not None:
                if program_id not in self._window_blocked:
                    self._window_blocked.add(program_id)
                    events.append(Event(kind="window", program_id=program_id, at=now,
                                        extra={"until": window.seconds_until_end(now)}))
            elif program_id in self._window_blocked:
                self._window_blocked.discard(program_id)
                state.streak_seconds = 0.0
                state.rest_seconds = 0.0
                state.pause_until = 0.0
                state.pause_logged = False

            if is_running:
                if not in_pause and window is None:
                    # 有效使用：计入每日上限与连续累计。时段内不计时（遮罩生效期
                    # 一律不算使用），只统计"该条规则已启用"的时长：关掉规则的计时
                    # 必须保持为 0，否则关掉再打开时旧的累计会立刻再次触发遮罩。
                    if rule.daily_active:
                        state.used_seconds += delta
                    if rule.streak_enabled:
                        state.streak_seconds += delta
                # 达到每日上限时记一次日志
                if (blocking and rule.daily_active
                        and state.limit_logged != state.day_key):
                    state.limit_logged = state.day_key
                    events.append(Event(kind="daily_limit", program_id=program_id, at=now))
            else:
                if rule.streak_enabled and rule.streak_seconds > 0 \
                        and state.streak_seconds < rule.streak_seconds:
                    # 未达阈值就退出：累计休息时间
                    state.rest_seconds += delta

            # 连续累计达到阈值 → 触发强制暂停
            if (rule.streak_enabled and rule.streak_seconds > 0
                    and state.streak_seconds >= rule.streak_seconds
                    and state.pause_until <= now):
                remaining = max(0.0, rule.pause_seconds - state.rest_seconds)
                state.pause_until = now + remaining
                state.rest_seconds = 0.0
                if remaining > 0 and not state.pause_logged:
                    state.pause_logged = True
                    events.append(Event(kind="pause", program_id=program_id, at=now,
                                        extra={"seconds": remaining}))

        return events

    def _advance_day(self, state: State, now: float) -> State:
        key = local_day_key(now)
        if state.day_key != key:
            state.day_key = key
            state.used_seconds = 0.0
            state.limit_logged = ""
        return state

    # ------------------------------------------------------------------
    def snapshot_state(self, program_id: int, now: Optional[float] = None) -> dict:
        """给 UI 展示用的状态摘要。"""
        now = now or time.time()
        rule = self.rules.get(program_id)
        state = self.state(program_id)
        decision = self.decide(program_id, now)
        window = rule.window_block(now) if rule is not None else None
        return {
            "used_seconds": state.used_seconds,
            # 规则没启用时不要把额度算进来，否则界面会显示"今日已用"却并未开启限制
            "daily_limit": rule.daily_seconds if rule and rule.daily_active else 0.0,
            "streak_seconds": state.streak_seconds,
            "streak_limit": rule.streak_seconds if rule and rule.streak_active else 0.0,
            "rest_seconds": state.rest_seconds,
            "pause_remaining": max(0.0, state.pause_until - now),
            "unlock_remaining": max(0.0, state.unlock_until - now),
            "window_active": bool(rule and rule.window_active),
            "window_count": len(rule.windows) if rule else 0,
            "window_remaining": window.seconds_until_end(now) if window is not None else 0.0,
            "blocked": decision.blocked,
            "reason": decision.reason,
        }
