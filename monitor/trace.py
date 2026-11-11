"""临时诊断日志开关（用户 ② 要求"内置可开关"）。

排查"遮罩为什么闪没"这类问题时，设环境变量 ``MONITOR_TRACE=1`` 启动 Monitor
即可在 stderr 看到逐拍判定；不设时每次调用只是一次布尔判断，没有开销。

输出统一带 ``[monitor-trace]`` 前缀与毫秒时间戳，方便和 Windows 事件日志对时间。
"""

from __future__ import annotations

import os
import sys
import time

PREFIX = "[monitor-trace]"

#: 开关：``MONITOR_TRACE=1``（或历史名 ``MONITOR_TRACE_SITE=1``）
ENABLED = (os.environ.get("MONITOR_TRACE") or os.environ.get("MONITOR_TRACE_SITE")) == "1"


def enabled() -> bool:
    return ENABLED


def log(area: str, message: str) -> None:
    """按需打印一行诊断日志（关掉时什么都不做，也不会抛异常）。"""
    if not ENABLED:
        return
    try:
        now = time.time()
        stamp = time.strftime("%H:%M:%S", time.localtime(now)) + ".%03d" % int(now % 1 * 1000)
        print(f"{PREFIX} {stamp} {area}: {message}", file=sys.stderr, flush=True)
    except Exception:
        pass
