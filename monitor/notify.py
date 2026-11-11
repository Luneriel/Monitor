"""通知派发：Windows（托盘气泡）与企业微信（webhook）。

设置分两层：全局（``notify.global``）与按程序独立（``notify.program.<id>``），
后者缺省时回落到全局。设置结构::

    {
      "events": {"open": true, "close": false, "daily_limit": true,
                 "unlocked": false, "pause": true, "password_changed": false},
      "durations": [60, 75],            # 追踪时长达到 N 分钟时通知（可多条）
      "windows": true,                  # Windows 通知通道开关
      "wecom": false,                   # 企业微信通道开关
      "wecom_url": "https://qyapi.weixin.qq.com/...",
      "custom": {                       # 各通道、各事件的自定义通知内容（空 = 用默认文案）
        "windows": {"open": "“{program}”已启动，今日已用 {duration}"},
        "wecom": {}
      }
    }

自定义文案里可用的占位符：
    ``{program}``  当前程序名（``{name}`` 同义）
    ``{duration}`` **本次追踪**的时长（HH:MM:SS，未知时为 --）
    ``{minutes}``  本次追踪时长的分钟数
    ``{time}``     当前时间（YYYY-MM-DD HH:MM:SS）

文案里的字面 ``\n`` 会转成真正的换行，因此可以写多行通知。
默认文案同样支持这些占位符（自带 ``{time}\n\n`` 前缀）。
"""

from __future__ import annotations

import datetime
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from .i18n import tr

# 通知事件键
EVENT_OPEN = "open"
EVENT_CLOSE = "close"
EVENT_DAILY_LIMIT = "daily_limit"
EVENT_UNLOCKED = "unlocked"
EVENT_PAUSE = "pause"
EVENT_PASSWORD = "password_changed"
EVENT_DURATION = "duration"

EVENT_KEYS = (EVENT_OPEN, EVENT_CLOSE, EVENT_DAILY_LIMIT, EVENT_UNLOCKED, EVENT_PAUSE)

# 通知通道（各自可以有独立的自定义内容）
CHANNEL_WINDOWS = "windows"
CHANNEL_WECOM = "wecom"
CHANNELS = (CHANNEL_WINDOWS, CHANNEL_WECOM)

# 可以自定义内容的全部事件
CUSTOM_EVENTS = (EVENT_OPEN, EVENT_CLOSE, EVENT_DAILY_LIMIT, EVENT_UNLOCKED, EVENT_PAUSE,
                 EVENT_DURATION, EVENT_PASSWORD)

PLACEHOLDER_HINT = "{program} / {duration} / {time} / {unlockTime}"

# 企业微信异步发送的待发上限：超了就丢掉这条通知，避免网络不通时越积越多
WECOM_QUEUE_MAX = 8


class _SendDone(QObject):
    """只用来把结果送回调用方所在线程（收件人持有它的引用，不会被提前回收）。"""

    done = Signal(bool)


class _WecomSend(QRunnable):
    """在线程池里发企业微信 webhook。

    ``urlopen`` 的超时是 6 秒。这段等待如果留在调用线程里，界面会直接卡住；
    更麻烦的是追踪线程也会触发通知（打开/关闭/达到时长阈值），一旦网络不通，
    1 秒一次的进程扫描就会跟着一起被拖慢。所以挪到线程池，调用方立刻返回。

    ``on_done`` 在工作线程里被调用，所以它**不能直接碰 Qt 控件**。需要更新界面的
    调用方应该传一个活在自己线程里的 QObject 信号（``signal.emit``），Qt 会自动
    把它排队到收件人线程；传普通函数则就地执行。
    """

    def __init__(self, url: str, payload: bytes,
                 on_done: Optional[Callable[[bool], None]] = None) -> None:
        super().__init__()
        self._url = url
        self._payload = payload
        self._on_done = on_done

    @staticmethod
    def _post(url: str, payload: bytes) -> bool:
        request = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=6) as response:
                return 200 <= getattr(response, "status", 200) < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False

    @Slot()
    def run(self) -> None:  # noqa: D102
        ok = self._post(self._url, self._payload)
        callback = self._on_done
        if callback is None:
            return
        # 用局部变量做转发：run() 返回后 QRunnable 可能被回收，但这一次 emit 已经发完。
        emitter = _SendDone()
        emitter.done.connect(callback)
        try:
            emitter.done.emit(ok)
        except Exception:
            pass


def default_settings() -> dict:
    return {
        "events": {EVENT_OPEN: False, EVENT_CLOSE: False, EVENT_DAILY_LIMIT: True,
                   EVENT_UNLOCKED: True, EVENT_PAUSE: True, EVENT_PASSWORD: False},
        "durations": [],
        "windows": False,
        "wecom": False,
        "wecom_url": "",
        "custom": {channel: {} for channel in CHANNELS},
    }


def format_duration(seconds: Optional[float]) -> str:
    """秒 -> HH:MM:SS；None 表示"未知"。"""
    if seconds is None:
        return "--"
    total = max(0, int(seconds or 0))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def render_template(template: str, program: str = "",
                    duration_seconds: Optional[float] = None,
                    when: Optional[float] = None,
                    unlock_minutes: Optional[float] = None) -> str:
    """把自定义文案里的占位符替换成实际内容（未识别的内容保持原样）。

    ``duration_seconds`` 是**本次追踪**的时长，不是今日累计；字面 ``\\n`` 会转成换行。
    ``unlock_minutes`` 只在"程序被临时解锁"事件里给出，用于 ``{unlockTime}``
    （临时解锁的时长）；其它事件没有这个值，占位符就保持原样。
    """
    moment = datetime.datetime.fromtimestamp(time.time() if when is None else float(when))
    values = {
        "program": program or "",
        "name": program or "",
        "duration": format_duration(duration_seconds),
        "minutes": str(int((duration_seconds or 0) // 60)),
        "time": moment.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if unlock_minutes is not None:
        values["unlockTime"] = tr("notify.unlock.minutes",
                                  minutes=f"{float(unlock_minutes):g}")
    text = str(template or "")
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text.replace("\\n", "\n").replace("\\t", "\t")


def _custom_block(raw) -> dict:
    """从原始设置里取出（并清洗）自定义内容：{channel: {event: text}}。"""
    cleaned = {channel: {} for channel in CHANNELS}
    if not isinstance(raw, dict):
        return cleaned
    for channel in CHANNELS:
        stored = raw.get(channel)
        if not isinstance(stored, dict):
            continue
        for event in CUSTOM_EVENTS:
            text = str(stored.get(event) or "")
            if text.strip():
                cleaned[channel][event] = text
    return cleaned


def normalize(raw: Optional[dict]) -> dict:
    settings = default_settings()
    if isinstance(raw, dict):
        events = raw.get("events")
        if isinstance(events, dict):
            for key in list(settings["events"]):
                if key in events:
                    settings["events"][key] = bool(events[key])
        durations = raw.get("durations")
        if isinstance(durations, list):
            cleaned = []
            for item in durations:
                try:
                    minutes = int(item)
                except (TypeError, ValueError):
                    continue
                if minutes > 0:
                    cleaned.append(minutes)
            settings["durations"] = sorted(set(cleaned))
        for flag in ("windows", "wecom"):
            if flag in raw:
                settings[flag] = bool(raw[flag])
        if "wecom_url" in raw:
            settings["wecom_url"] = str(raw["wecom_url"] or "")
        settings["custom"] = _custom_block(raw.get("custom"))
    return settings


class Notifier:
    """按设置把通知发送到启用的通道。

    * **事件与时长阈值**按作用域取：有独立设置的程序用它自己的，否则用全局设置；
    * **通道开关（Windows / 企业微信）与 webhook 地址始终取全局设置**，
      否则全局打开通道后，配了独立设置的程序反而收不到通知。
    """

    def __init__(self, db, tray=None) -> None:
        self.db = db
        self.tray = tray
        self._sent_durations: dict[int, set] = {}     # program_id -> 已通知过的阈值(分钟)
        self._wecom_pool: Optional[QThreadPool] = None
        self._wecom_lock = threading.Lock()
        self._wecom_pending: list = []                # 保住 QRunnable 与取件对象的引用

    def _pool(self) -> QThreadPool:
        if self._wecom_pool is None:
            self._wecom_pool = QThreadPool.globalInstance()
        return self._wecom_pool

    # ------------------------------------------------------------------
    def _settings_for(self, program_id: Optional[int]) -> dict:
        """事件 / 阈值部分（作用域：程序独立设置优先，否则全局）。"""
        global_settings = normalize(self.db.get_notify_settings(None))
        if program_id is None:
            return global_settings
        raw = self.db.get_notify_settings(int(program_id))
        if not raw:
            return global_settings
        own = normalize(raw)
        # 通道配置与自定义内容始终沿用全局
        for flag in ("windows", "wecom", "wecom_url", "custom"):
            own[flag] = global_settings.get(flag)
        return own

    def has_independent(self, program_id: int) -> bool:
        return bool(self.db.get_notify_settings(int(program_id)))

    # ------------------------------------------------------------------
    def used_seconds_today(self, program_id: Optional[int]) -> Optional[float]:
        """该程序"今日已用时长"（日志按天裁剪后累加）；无法计算时返回 None。"""
        if program_id is None:
            return None
        now = time.time()
        moment = datetime.datetime.fromtimestamp(now)
        day_start = datetime.datetime(moment.year, moment.month, moment.day).timestamp()
        day_end = day_start + 86400
        try:
            rows = self.db.logs_for_program(int(program_id), limit=2000,
                                            since=day_start, until=day_end)
        except Exception:
            return None
        total = 0.0
        for row in rows:
            try:
                start = max(float(row["start_time"] or 0.0), day_start)
                raw_end = row["end_time"]
                end = now if raw_end is None else float(raw_end)
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            end = min(end, now, day_end)
            if end > start:
                total += end - start
        return total

    def session_seconds(self, program_id: Optional[int]) -> Optional[float]:
        """**本次追踪**的时长：最近一条运行记录的时长（运行中则算到此刻）。

        与 :meth:`used_seconds_today` 不同——后者是今日累计，只用于时长阈值判断。
        """
        if program_id is None:
            return None
        try:
            rows = self.db.logs_for_program(int(program_id), limit=1)
        except Exception:
            return None
        if not rows:
            return None
        row = rows[0]
        if str(row["kind"] or "run") != "run":
            return None
        try:
            start = float(row["start_time"] or 0.0)
            raw_end = row["end_time"]
            end = time.time() if raw_end is None else float(raw_end)
        except (TypeError, ValueError, IndexError, KeyError):
            return None
        return max(0.0, end - start)

    def _channel_body(self, channel: str, event: str, default_body: str, *,
                      program_id: Optional[int], program_name: str,
                      duration_seconds: Optional[float],
                      unlock_minutes: Optional[float] = None) -> str:
        """该通道该事件若有自定义内容则用它，否则用默认文案（两者都做占位符替换）。"""
        settings = normalize(self.db.get_notify_settings(None))
        custom = (settings.get("custom") or {}).get(channel) or {}
        template = custom.get(event) or default_body
        if duration_seconds is None:
            duration_seconds = self.session_seconds(program_id)
        return render_template(template, program=program_name,
                               duration_seconds=duration_seconds,
                               unlock_minutes=unlock_minutes)

    def notify(self, event: str, title: str, body: str,
               program_id: Optional[int] = None, force: bool = False,
               channels: Optional[tuple] = None, program_name: str = "",
               duration_seconds: Optional[float] = None,
               unlock_minutes: Optional[float] = None) -> bool:
        settings = self._settings_for(program_id)
        # 时长阈值事件由 durations 列表本身决定开关，不受 events 勾选影响
        if not force and event != EVENT_DURATION and not settings["events"].get(event, False):
            return False
        wanted = tuple(channels) if channels else CHANNELS
        delivered = False
        if CHANNEL_WINDOWS in wanted and settings.get("windows") and self.tray is not None:
            text = self._channel_body(CHANNEL_WINDOWS, event, body, program_id=program_id,
                                      program_name=program_name,
                                      duration_seconds=duration_seconds,
                                      unlock_minutes=unlock_minutes)
            try:
                self.tray.show()
                self.tray.showMessage(title, text, self.tray.icon(), 6000)
                delivered = True
            except Exception:
                try:
                    self.tray.showMessage(title, text)
                    delivered = True
                except Exception:
                    pass
        if CHANNEL_WECOM in wanted and settings.get("wecom") and settings.get("wecom_url"):
            text = self._channel_body(CHANNEL_WECOM, event, body, program_id=program_id,
                                      program_name=program_name,
                                      duration_seconds=duration_seconds,
                                      unlock_minutes=unlock_minutes)
            # 异步发送：webhook 超时可以有 6 秒，绝不能在界面线程或追踪线程里等
            delivered = self._send_wecom(settings["wecom_url"], title, text,
                                         on_done=lambda _ok: None) or delivered
        return delivered

    def _send_wecom(self, url: str, title: str, body: str,
                    on_done: Optional[Callable[[bool], None]] = None) -> bool:
        """发企业微信 webhook。

        默认（``on_done is None``）在当前线程同步发送并把结果作为返回值，便于直接调用；
        传了 ``on_done`` 就丢进线程池，立刻返回 True（已受理），结果用回调报回来 ——
        调用方可能是主线程（界面），也可能是 1 秒一轮的追踪线程，都不能为了等
        webhook 的最多 6 秒超时被卡住。
        """
        # 企业微信只发正文：标题是托盘气泡用的，发到群里会多出一行"Monitor 通知"
        _ = title
        payload = json.dumps({
            "msgtype": "text",
            "text": {"content": body},
        }, ensure_ascii=False).encode("utf-8")
        if on_done is None:
            return _WecomSend._post(url, payload)

        def _finished(ok: bool) -> None:
            with self._wecom_lock:
                if task in self._wecom_pending:
                    self._wecom_pending.remove(task)
            try:
                on_done(ok)
            except Exception:
                pass

        task = _WecomSend(url, payload, _finished)
        with self._wecom_lock:
            # 队列有界：网络不通时每个任务都要耗满 6 秒超时，无上限排队只会积压过期通知
            if len(self._wecom_pending) >= WECOM_QUEUE_MAX:
                return False
            self._wecom_pending.append(task)
        self._pool().start(task)
        return True

    # ------------------------------------------------------------------
    def on_program_event(self, event: str, program_name: str,
                         program_id: Optional[int] = None,
                         duration_seconds: Optional[float] = None,
                         unlock_minutes: Optional[float] = None) -> None:
        """程序打开 / 关闭 / 达到上限 / 被解锁 / 强制暂停。"""
        title = tr("notify.title")
        body = tr(f"notify.body.{event}", name=program_name)
        self.notify(event, title, body, program_id=program_id, program_name=program_name,
                    duration_seconds=duration_seconds, unlock_minutes=unlock_minutes)

    def on_duration(self, program_id: int, program_name: str, used_seconds: float,
                    duration_seconds: Optional[float] = None) -> None:
        """追踪时长达到配置的阈值时通知（支持多个阈值）。

        ``used_seconds`` 是今日累计（用于阈值判断），``duration_seconds`` 是本次追踪时长
        （用于文案里的 ``{duration}``）。
        """
        settings = self._settings_for(program_id)
        thresholds = settings.get("durations") or []
        if not thresholds:
            return
        used_minutes = int(used_seconds // 60)
        notified = self._sent_durations.setdefault(int(program_id), set())
        for minutes in thresholds:
            if used_minutes >= minutes and minutes not in notified:
                notified.add(minutes)
                self.notify(EVENT_DURATION, tr("notify.title"),
                            tr("notify.body.duration", name=program_name, minutes=minutes),
                            program_id=program_id, program_name=program_name,
                            duration_seconds=duration_seconds)

    def reset_duration_marks(self, program_id: Optional[int] = None) -> None:
        if program_id is None:
            self._sent_durations.clear()
        else:
            self._sent_durations.pop(int(program_id), None)

    def on_password_changed(self) -> None:
        self.notify(EVENT_PASSWORD, tr("notify.title"), tr("notify.body.password_changed"))
