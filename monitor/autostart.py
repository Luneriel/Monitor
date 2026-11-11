"""Windows 开机自启动。

两种方式：
* 普通自启动 —— 注册表 ``HKCU\\...\\Run``（当前权限启动）；
* 管理员自启动 —— 登录触发的计划任务 + ``/RL HIGHEST``（以最高权限启动，
  登录时不会弹 UAC：这正是"以管理员身份启动"需要的效果）。

计划任务需要管理员权限才能写入，因此 ``MODE_ADMIN`` 只在 Monitor 已经以
管理员身份运行时才会真正生效（调用方负责提示与提权重启）。
"""

from __future__ import annotations

import os
import subprocess
import sys

from . import config

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Monitor"
TASK_NAME = "Monitor"
CREATE_NO_WINDOW = 0x08000000

MODE_NONE = "none"
MODE_RUN = "run"
MODE_ADMIN = "admin"

LAST_ERROR = ""


def _launch_command(silent: bool = False) -> str:
    if getattr(sys, "frozen", False):
        # 打包成 exe 后 exe 自己就是入口（而且本身没有控制台窗口），没有 main.py 可指
        command = f'"{os.path.abspath(sys.executable)}"'
    else:
        exe = sys.executable or "python"
        # 自启动时优先使用无控制台窗口的 pythonw.exe
        candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(candidate):
            exe = candidate
        entry = os.path.join(config.PROJECT_ROOT, "main.py")
        command = f'"{exe}" "{entry}"'
    if silent:
        command += " --silent"
    return command


def _task_command(silent: bool = False) -> str:
    """计划任务要执行的命令行。"""
    return _launch_command(silent)


def _run_schtasks(*args: str) -> tuple[int, str]:
    """调用 schtasks.exe；返回 (退出码, 输出)。"""
    try:
        result = subprocess.run(["schtasks", *args], capture_output=True, text=True,
                                creationflags=CREATE_NO_WINDOW)
    except OSError as error:                      # schtasks 不存在（非 Windows）
        return -1, str(error)
    output = f"{result.stdout or ''}{result.stderr or ''}".strip()
    return int(result.returncode), output


def read_run_value() -> str:
    """读取注册表里的自启动命令行；不存在时返回空串。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value).strip()
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def write_run_value(silent: bool = False) -> bool:
    global LAST_ERROR
    try:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _launch_command(silent))
        LAST_ERROR = ""
        return True
    except OSError as error:
        LAST_ERROR = str(error)
        return False


def remove_run_value() -> bool:
    global LAST_ERROR
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        LAST_ERROR = ""
        return True
    except FileNotFoundError:
        return True
    except OSError as error:
        LAST_ERROR = str(error)
        return False


def has_admin_task() -> bool:
    """是否已存在"以管理员身份自启动"的计划任务。"""
    code, _ = _run_schtasks("/Query", "/TN", TASK_NAME)
    return code == 0


def create_admin_task(silent: bool = False) -> bool:
    """创建登录触发的最高权限计划任务（需要管理员权限）。"""
    global LAST_ERROR
    code, output = _run_schtasks("/Create", "/F", "/SC", "ONLOGON", "/RL", "HIGHEST",
                                 "/TN", TASK_NAME, "/TR", _task_command(silent))
    if code != 0:
        LAST_ERROR = output or "schtasks 创建计划任务失败"
        return False
    LAST_ERROR = ""
    return True


def delete_admin_task() -> bool:
    global LAST_ERROR
    if not has_admin_task():
        return True
    code, output = _run_schtasks("/Delete", "/F", "/TN", TASK_NAME)
    if code != 0:
        LAST_ERROR = output or "schtasks 删除计划任务失败"
        return False
    LAST_ERROR = ""
    return True


def mode() -> str:
    """当前生效的自启动方式。"""
    if has_admin_task():
        return MODE_ADMIN
    if read_run_value():
        return MODE_RUN
    return MODE_NONE


def is_admin_enabled() -> bool:
    return mode() == MODE_ADMIN


def is_enabled() -> bool:
    return mode() != MODE_NONE


def enable(silent: bool = False) -> bool:
    return write_run_value(silent)


def disable() -> bool:
    return remove_run_value()


def apply(enabled: bool, silent: bool = False, admin: bool = False) -> bool:
    """按需要同步注册表与计划任务；返回是否成功。

    * ``admin=True`` 时用计划任务（并移除注册表项，避免重复启动）；
    * 关闭时两种方式一起清掉。
    """
    if not enabled:
        return remove_run_value() and delete_admin_task()
    if admin:
        if not create_admin_task(silent):
            return False
        remove_run_value()          # 计划任务优先，避免登录时启动两次
        return True
    delete_admin_task()
    return write_run_value(silent)
