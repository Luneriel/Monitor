"""前台窗口事件钩子（``SetWinEventHook``）：遮罩的即时 z 序纠正。

遮罩（尤其是网站遮罩）是**顶层窗口**：用户点浏览器标题栏 / 切回浏览器时，
shell 会把浏览器抬到同层最上面，遮罩就被压在下面，看起来"消失"了。只靠
``SYNC_MS = 120`` 的轮询补位，用户能看见那一瞬间的露底（用户 ② 的反馈）。

这里在独立线程里挂 ``EVENT_SYSTEM_FOREGROUND``，一有前台变化就发 Qt 信号给
主线程，主线程立刻补一次 z 序（见 ``OverlayManager._on_foreground_changed``）。

实现要点（都是踩过的坑）：

* 钩子必须用 ``WINEVENT_OUTOFCONTEXT``：回调发生在**挂了钩子并且正在抽消息**的
  线程上，所以自己起线程 + ``GetMessage`` 循环；Qt 主线程的消息循环被 Qt 占着，
  不适合再挂一个要自己抽消息的钩子。
* ``SetWinEventHook`` 返回的是指针大小的句柄，必须声明 ``restype``，否则 64 位下
  被截断成 32 位，``UnhookWinEvent`` 拿到废句柄 → 钩子泄漏。
* 回调对象要一直持有引用，否则会被 GC 掉，系统回调进野指针直接崩进程。
* ``WINEVENT_SKIPOWNPROCESS``：跳过本进程自己的窗口事件，否则我们自己摆放遮罩
  （``SetWindowPos``）也会触发前台事件，来回自激。

取不到钩子（非 Windows / 系统拒绝）时 ``start()`` 返回 False，调用方退回纯轮询。
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from typing import Optional

from PySide6.QtCore import QObject, Signal

EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
WM_QUIT = 0x0012

if sys.platform == "win32":
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _WinEventProc = ctypes.WINFUNCTYPE(
        None,
        ctypes.c_void_p,        # HWINEVENTHOOK
        ctypes.c_uint32,        # event
        ctypes.c_void_p,        # HWND
        ctypes.c_long,          # idObject
        ctypes.c_long,          # idChild
        ctypes.c_uint32,        # idEventThread
        ctypes.c_uint32,        # dwmsEventTime
    )
    _user32.SetWinEventHook.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, _WinEventProc,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
    _user32.SetWinEventHook.restype = ctypes.c_void_p
    _user32.UnhookWinEvent.argtypes = [ctypes.c_void_p]
    _user32.UnhookWinEvent.restype = ctypes.c_int
    _user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_uint32, ctypes.c_uint32]
    _user32.GetMessageW.restype = ctypes.c_int
else:                                            # pragma: no cover - 仅 Windows
    _user32 = None
    _kernel32 = None
    _WinEventProc = None


class ForegroundHook(QObject):
    """前台窗口变化 → ``changed`` 信号（每变化一次发一次，不做合并）。"""

    changed = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._callback = None
        self._hook = None
        self._thread_id = 0
        self._last_error = ""

    # -- 生命周期 ------------------------------------------------------
    def start(self) -> bool:
        """挂上钩子；返回 True 表示事件通道可用（False → 调用方退回轮询）。"""
        if _user32 is None:
            self._last_error = "非 Windows 平台"
            return False
        if self._thread is not None:
            return self._hook is not None
        self._stopping.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="monitor-foreground-hook",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(2.0)
        return self._hook is not None

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stopping.set()
        if self._thread_id:
            try:
                _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        self._thread.join(timeout=1.5)
        self._thread = None

    def running(self) -> bool:
        return bool(self._hook) and self._thread is not None

    def last_error(self) -> str:
        return self._last_error

    # -- 内部 ----------------------------------------------------------
    def _run(self) -> None:                        # pragma: no cover - 独立线程
        try:
            self._thread_id = int(_kernel32.GetCurrentThreadId())
            # 回调必须持有引用，否则 GC 之后系统回调进来就是野指针
            self._callback = _WinEventProc(self._on_event)
            self._hook = _user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None,
                self._callback, 0, 0,
                WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
            if not self._hook:
                self._last_error = f"SetWinEventHook 失败（{ctypes.get_last_error()}）"
                self._ready.set()
                return
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while not self._stopping.is_set():
                got = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):              # WM_QUIT / 出错
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            pass
        finally:
            try:
                if self._hook:
                    _user32.UnhookWinEvent(self._hook)
            except Exception:
                pass
            self._hook = None

    def _on_event(self, _hook, _event, _hwnd, _id_object, _id_child,
                  _thread, _time) -> None:        # pragma: no cover - 系统回调
        try:
            self.changed.emit()                 # 跨线程 → Qt 自动排队到主线程
        except Exception:
            pass
