"""输入拦截：遮罩生效时挡住目标程序的键盘与鼠标输入。

用 SetWindowsHookEx 的**低级钩子**（WH_KEYBOARD_LL / WH_MOUSE_LL），它只观察
系统输入事件、运行在 Monitor 自己的进程与线程里，不需要注入目标进程、也不修改目标程序。

规则：
    * 键盘：前台窗口属于被限制的程序 → 吞掉按键（目标窗口拿不到任何按键）；
    * 鼠标：光标下的窗口属于被限制的程序、且不在 Monitor 的遮罩上 → 吞掉点击/滚轮；
      点在遮罩自身（含"临时解锁""关闭程序"按钮）上的事件照常放行。

钩子回调必须极快，这里只做窗口归属判断。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from typing import Callable, Optional

if sys.platform == "win32":
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
else:                                            # pragma: no cover - 仅 Windows
    _user32 = None
    _kernel32 = None

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0

WM_MOUSEMOVE = 0x0200
GA_ROOT = 2


def _prepare_callnext() -> None:
    """CallNextHookEx 的第 4 个参数是 LPARAM（指针宽度），不设签名会溢出。"""
    if _user32 is None:
        return
    try:
        _user32.CallNextHookEx.restype = ctypes.c_ssize_t
        _user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                           ctypes.c_size_t, ctypes.c_ssize_t]
    except Exception:
        pass


_prepare_callnext()


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


def _pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _root(hwnd: int) -> int:
    root = _user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT)
    return int(root or hwnd)


class InputBlocker:
    """安装/卸载低级钩子。``state_provider`` 返回

        (blocked_pids: set[int], overlay_hwnds: set[int])

    —— 被限制程序的进程 PID，以及 Monitor 自己遮罩窗口的 HWND（这些窗口上的
    鼠标事件必须放行，否则"关闭程序"按钮点不动）。
    """

    def __init__(self, state_provider: Callable[[], tuple]) -> None:
        self._state_provider = state_provider
        self._keyboard_hook = None
        self._mouse_hook = None
        self._keyboard_proc = None
        self._mouse_proc = None
        self._cached_state: tuple = (set(), set())
        self._cached_at = 0.0
        self.blocked_events = 0

    # ------------------------------------------------------------------
    # 状态缓存
    #
    # 低级钩子在**系统输入路径**上：钩子回调不返回，整台机器的鼠标键盘就一直
    # 被挂住。所以这里绝不能在每次事件里都去查进程快照（会拿锁、会建集合），
    # 而是短时间复用一份缓存。
    # ------------------------------------------------------------------
    STATE_TTL = 0.35

    def invalidate(self) -> None:
        self._cached_at = 0.0

    def _state(self) -> tuple:
        now = time.monotonic()
        if self._cached_at and (now - self._cached_at) < self.STATE_TTL:
            return self._cached_state
        try:
            pids, hwnds = self._state_provider()
            self._cached_state = (set(int(p) for p in pids), set(int(h) for h in hwnds))
        except Exception:
            self._cached_state = (set(), set())
        self._cached_at = now
        return self._cached_state

    def _is_ours(self, hwnd: int, overlay_hwnds: set[int]) -> bool:
        """该窗口是不是 Monitor 的遮罩（含遮罩的子控件）。"""
        current = hwnd
        for _ in range(6):
            if current in overlay_hwnds:
                return True
            parent = _user32.GetParent(wintypes.HWND(current))
            if not parent:
                break
            current = int(parent)
        return False

    @staticmethod
    def _in_client_area(hwnd: int, x: int, y: int) -> bool:
        """点是否落在窗口客户区内（客户区外的标题栏/边框要放行，便于移动窗口）。"""
        if _user32 is None or not hwnd:
            return False
        rect = wintypes.RECT()
        if not _user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return False
        origin = wintypes.POINT(0, 0)
        _user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(origin))
        left = origin.x
        top = origin.y
        return left <= x <= left + rect.right and top <= y <= top + rect.bottom

    def _on_keyboard(self, code, wparam, lparam):
        if code == HC_ACTION:
            pids, _hwnds = self._state()
            if pids:
                foreground = int(_user32.GetForegroundWindow() or 0)
                if foreground and _pid_of(foreground) in pids:
                    self.blocked_events += 1
                    return 1                     # 吞掉按键
        return _user32.CallNextHookEx(None, code, wparam, lparam)

    def _on_mouse(self, code, wparam, lparam):
        if code == HC_ACTION and wparam != WM_MOUSEMOVE:
            pids, overlay_hwnds = self._state()
            if pids:
                info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                point = wintypes.POINT(info.pt.x, info.pt.y)
                target = int(_user32.WindowFromPoint(point) or 0)
                if target and not self._is_ours(target, overlay_hwnds):
                    root = _root(target)
                    if _pid_of(target) in pids or _pid_of(root) in pids:
                        # 标题栏等非客户区放行，否则窗口连拖都拖不动
                        if self._in_client_area(root, info.pt.x, info.pt.y):
                            self.blocked_events += 1
                            return 1             # 吞掉客户区里的点击/滚轮
        return _user32.CallNextHookEx(None, code, wparam, lparam)

    # ------------------------------------------------------------------
    def install(self) -> None:
        # 两个钩子分别判断：以前只检查键盘钩子，一旦鼠标钩子安装失败（安全软件、
        # 权限、句柄耗尽都可能导致），后续每次 install() 都直接返回，本轮运行里
        # 点击就再也不会被拦——保护静默降级。
        if _user32 is None or (self._keyboard_hook and self._mouse_hook):
            return
        if not self._keyboard_hook:
            try:
                self._keyboard_proc = ctypes.WINFUNCTYPE(
                    ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)(
                        self._on_keyboard)
                self._keyboard_hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL,
                                                                self._keyboard_proc, None, 0)
            except Exception:
                self._keyboard_hook = None
        if not self._mouse_hook:
            try:
                self._mouse_proc = ctypes.WINFUNCTYPE(
                    ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)(
                        self._on_mouse)
                self._mouse_hook = _user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc,
                                                             None, 0)
            except Exception:
                self._mouse_hook = None

    def uninstall(self) -> None:
        for attr in ("_keyboard_hook", "_mouse_hook"):
            hook = getattr(self, attr)
            if not hook:
                continue
            try:
                # 返回 0 = 句柄已无效（回调不会再来）；两种情况都可以丢掉蹦床
                _user32.UnhookWindowsHookEx(hook)
            except Exception:
                # 调用本身失败：句柄状态未知，保守起见留着蹦床（系统可能还会回调）
                continue
            setattr(self, attr, None)
        if self._keyboard_hook is None:
            self._keyboard_proc = None
        if self._mouse_hook is None:
            self._mouse_proc = None

    def is_installed(self) -> bool:
        return bool(self._keyboard_hook or self._mouse_hook)
