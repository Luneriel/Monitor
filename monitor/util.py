"""通用小工具：时间格式化、路径处理等。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Optional


def now() -> float:
    return time.time()


def format_duration(seconds: Optional[float]) -> str:
    """时长格式：小时:分钟:秒数（小时不截断到 24）。"""
    if seconds is None or seconds < 0:
        seconds = 0
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_ts(ts: Optional[float], with_seconds: bool = True) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromtimestamp(float(ts))
    except (OverflowError, OSError, ValueError):
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M")


def format_short(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "—"


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024.0:
            return f"{num:.0f}{unit}"
        num /= 1024.0
    return f"{num:.1f}TB"


def shorten_path(path: str, max_len: int = 90) -> str:
    if len(path) <= max_len:
        return path
    keep = max_len // 2 - 2
    return f"{path[:keep]}…{path[-keep:]}"


def base_name(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


# --------------------------------------------------------------------------
# 权限（Windows）
# --------------------------------------------------------------------------
def is_admin() -> bool:
    """当前进程是否以管理员（高完整性级别）身份运行。

    只有管理员进程才能调整"以管理员身份运行的程序"（例如原神这类带反作弊、
    要求提权的游戏）的 z 序，非管理员时遮罩只能退化成置顶。
    """
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def admin_command() -> tuple[str, str]:
    """返回以管理员身份重启本程序所需的 (可执行文件, 参数)。"""
    argv = [os.path.abspath(sys.argv[0])]
    argv.extend(sys.argv[1:])
    if getattr(sys, "frozen", False):            # 打包成 exe 时 sys.argv[0] 就是 exe
        return os.path.abspath(sys.argv[0]), subprocess.list2cmdline(sys.argv[1:])
    return sys.executable, subprocess.list2cmdline(argv)


def restart_as_admin() -> bool:
    """请求以管理员身份重新启动本程序（弹出 UAC；用户拒绝时返回 False）。"""
    try:
        import ctypes
        exe, params = admin_command()
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, os.getcwd(), 1)
        return int(result) > 32                  # >32 表示成功，<=32 是错误码
    except Exception:
        return False


def self_executable() -> str:
    """Monitor 自己的可执行文件（打包后是 Monitor.exe，开发时是 pythonw.exe / python.exe）。"""
    return os.path.abspath(sys.executable or "")


def is_self_path(path: Optional[str]) -> bool:
    """该程序路径是不是 Monitor 自己。

    开发运行时 Monitor 的主体进程是 ``pythonw.exe``（任务管理器里看到的就是它），
    所以除了与 :func:`self_executable` 完全相同的路径，同一解释器目录下的
    ``python.exe`` / ``pythonw.exe``、以及 ``Monitor.exe`` 也算自己——这些都不该
    出现在"程序使用限制"里（把自己遮起来等于把 Monitor 锁死）。
    """
    if not path:
        return False
    try:
        target = os.path.normcase(os.path.abspath(str(path)))
        mine = os.path.normcase(self_executable())
    except (OSError, ValueError):
        return False
    if not mine:
        return False
    if target == mine:
        return True
    stem = os.path.splitext(os.path.basename(target))[0].lower()
    if stem not in ("python", "pythonw", "monitor"):
        return False
    return os.path.dirname(target) == os.path.dirname(mine)
