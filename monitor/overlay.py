"""遮罩窗口：覆盖目标程序的所有可见顶层窗口，并拦截输入、静音、提供关闭出口。

工作机制
--------
* 用 Win32 枚举目标进程的可见顶层窗口（含最小化标记），窗口边界取 DWM 的
  ``DWMWA_EXTENDED_FRAME_BOUNDS``；
* 每个窗口一个遮罩 QWidget，尽量挂成该窗口的**子窗口**（``QWindow.setParent``，
  失败则退回 Win32 ``SetParent`` + ``WS_CHILD``）：子窗口由系统随父窗口一起移动、
  缩放、最小化、还原与销毁，因此没有跟随延迟；
* **无边框全屏**目标窗口同样优先挂载：挂成子窗口后遮罩只存在于该窗口的层级里，
  所以既能盖住全屏程序，又不会盖住叠在它上面的其它窗口；挂载成功后显隐一律走
  Win32（Qt 的 show()/hide() 会把子窗口重新变成顶层弹出窗口，表现为周期性闪烁）；
* 目标窗口短暂消失（切全屏、DWM 重排）时保留遮罩 GRACE_S 秒，避免"销毁→重建"
  造成的闪烁；
* 只有独占全屏（真正无法叠加窗口）的退化路径才强制最小化目标窗口，
  并同样静音 + 拦截输入；
* 遮罩生效期间用低级钩子拦截目标程序的键盘/鼠标输入、把目标进程的音频会话静音，
  解除时恢复原来的音量状态；
* 遮罩上有"临时解锁""关闭程序"两个按钮、规则提示与倒计时。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from typing import Callable, Optional

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QWindow
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                               QWidget)

from . import config
from . import trace
from .audio import ProcessMuter
from .i18n import tr
from .input_hook import InputBlocker
from .winhook import ForegroundHook

if sys.platform == "win32":
    _user32 = ctypes.windll.user32
    try:
        _dwmapi = ctypes.windll.dwmapi
    except OSError:                              # pragma: no cover - 仅 Windows
        _dwmapi = None
    # HWND 是 64 位句柄：不声明 restype/argtypes 时 ctypes 按 32 位 int 处理，
    # 高位句柄会被截断。
    _HWND = ctypes.c_void_p
    _user32.GetWindow.argtypes = [_HWND, ctypes.c_uint]
    _user32.GetWindow.restype = _HWND
    _user32.GetParent.argtypes = [_HWND]
    _user32.GetParent.restype = _HWND
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = _HWND
    _user32.GetWindowThreadProcessId.argtypes = [_HWND, ctypes.POINTER(ctypes.c_ulong)]
    _user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    _user32.SetParent.argtypes = [_HWND, _HWND]
    _user32.SetParent.restype = _HWND
    _user32.SetWindowPos.argtypes = [_HWND, _HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    _user32.SetWindowPos.restype = ctypes.c_bool
    _user32.ShowWindow.argtypes = [_HWND, ctypes.c_int]
    _user32.ShowWindow.restype = ctypes.c_bool
    _user32.IsWindow.argtypes = [_HWND]
    _user32.IsWindow.restype = ctypes.c_bool
    _user32.GetWindowLongW.argtypes = [_HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = ctypes.c_long
    _user32.SetWindowLongW.argtypes = [_HWND, ctypes.c_int, ctypes.c_long]
    _user32.SetWindowLongW.restype = ctypes.c_long
    _user32.GetWindowRect.argtypes = [_HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.restype = ctypes.c_bool
    _user32.GetClientRect.argtypes = [_HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetClientRect.restype = ctypes.c_bool
    _user32.GetClassNameW.argtypes = [_HWND, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.IsWindowVisible.argtypes = [_HWND]
    _user32.IsWindowVisible.restype = ctypes.c_bool
    _user32.IsIconic.argtypes = [_HWND]
    _user32.IsIconic.restype = ctypes.c_bool
    # 遮罩上的鼠标操作：聚焦目标窗口 + 把按住拖动转交给目标窗口做原生移动
    _user32.SetForegroundWindow.argtypes = [_HWND]
    _user32.SetForegroundWindow.restype = ctypes.c_bool
    _user32.BringWindowToTop.argtypes = [_HWND]
    _user32.BringWindowToTop.restype = ctypes.c_bool
    _user32.ReleaseCapture.argtypes = []
    _user32.ReleaseCapture.restype = ctypes.c_bool
    _user32.PostMessageW.argtypes = [_HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    _user32.PostMessageW.restype = ctypes.c_bool
    _user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ulong,
                                    ctypes.POINTER(ctypes.c_ulong)]
    _user32.keybd_event.restype = None
    # 自己实现的窗口拖动：读鼠标物理位置 / 物理按键状态，移动窗口、还原最大化
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _user32.GetCursorPos.restype = ctypes.c_bool
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _user32.IsZoomed.argtypes = [_HWND]
    _user32.IsZoomed.restype = ctypes.c_bool
else:                                            # pragma: no cover - 仅 Windows
    _user32 = None
    _dwmapi = None

GWL_EXSTYLE = -20
GWL_STYLE = -16
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
# DirectComposition 合成的窗口（Chromium / Electron 的程序窗口）没有可重定向的表面
WS_EX_NOREDIRECTIONBITMAP = 0x00200000
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_CLIPSIBLINGS = 0x04000000
GW_OWNER = 4
GW_HWNDPREV = 3

VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020
SW_MINIMIZE = 6
HWND_TOP = 0                                     # SetWindowPos 的 hWndInsertAfter：置顶
HWND_TOPMOST = -1                                # 置顶且进入 TOPMOST 层
HWND_NOTOPMOST = -2                              # 普通层最上面（所有置顶窗之下）
SW_HIDE = 0
SW_SHOWNA = 8                                     # 显示但不激活
SW_RESTORE = 9                                    # 还原（拖动最大化窗口前用）

VK_LBUTTON = 0x01                                 # GetAsyncKeyState：物理左键

DWMWA_EXTENDED_FRAME_BOUNDS = 9

# 把"按住遮罩空白处"变成目标窗口的原生移动：WM_NCLBUTTONDOWN + HTCAPTION
WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION = 2

# 需要跳过的系统外壳窗口类名
_SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Windows.UI.Core.CoreWindow"}


# --------------------------------------------------------------------------
# 坐标与窗口工具
# --------------------------------------------------------------------------
def physical_to_logical(rect: QRect) -> QRect:
    """Win32 物理像素坐标 → Qt 逻辑坐标（按窗口所在屏幕的 DPR 与逻辑原点换算）。"""
    screens = QGuiApplication.screens()
    if not screens:
        return rect
    center = rect.center()
    for screen in screens:
        dpr = float(screen.devicePixelRatio() or 1.0)
        geo = screen.geometry()
        phys = QRect(round(geo.x() * dpr), round(geo.y() * dpr),
                     round(geo.width() * dpr), round(geo.height() * dpr))
        if phys.contains(center):
            return QRect(geo.x() + round((rect.x() - phys.x()) / dpr),
                         geo.y() + round((rect.y() - phys.y()) / dpr),
                         max(1, round(rect.width() / dpr)),
                         max(1, round(rect.height() / dpr)))
    dpr = float(screens[0].devicePixelRatio() or 1.0)
    return QRect(round(rect.x() / dpr), round(rect.y() / dpr),
                 max(1, round(rect.width() / dpr)), max(1, round(rect.height() / dpr)))


def logical_size(rect: QRect) -> QRect:
    """只换算尺寸（子窗口内部坐标从 (0,0) 开始）。"""
    converted = physical_to_logical(QRect(0, 0, rect.width(), rect.height()))
    return QRect(0, 0, converted.width(), converted.height())


def _site_rect(raw) -> Optional[QRect]:
    """网站监视器给的 ``(left, top, right, bottom)`` → ``QRect``（x, y, w, h）。

    跟 ``window_frame_rect()`` / ``sync_geometry()`` 的约定保持一致：内部一律用
    QRect 传几何。坏数据返回 ``None``。
    """
    if not raw:
        return None
    try:
        left, top, right, bottom = (int(v) for v in raw)
    except Exception:
        return None
    return QRect(left, top, max(1, right - left), max(1, bottom - top))



def _follow_window(rect: QRect, frame_then: Optional[QRect],
                   frame_now: Optional[QRect]) -> QRect:
    """把"扫描那一刻算出的页面内容区"挪到窗口**现在**的位置 / 尺寸上。

    页面内容区是 UIA 给的，网站监视器一秒才复查一次；而用户按住遮罩拖动浏览器窗口时，
    遮罩得跟着实时动（120ms 一拍）。所以拿窗口矩形的前后差做补偿：位置按左上角差值平移，
    大小按宽高差值缩放。下一次 UIA 复查会给出准确值，不怕累计误差。
    """
    if frame_then is None or frame_now is None:
        return rect
    return QRect(rect.x() + frame_now.x() - frame_then.x(),
                 rect.y() + frame_now.y() - frame_then.y(),
                 max(1, rect.width() + frame_now.width() - frame_then.width()),
                 max(1, rect.height() + frame_now.height() - frame_then.height()))


def window_frame_rect(hwnd: int) -> Optional[QRect]:
    """窗口的可见边界（物理像素）。"""
    if _user32 is None:
        return None
    if _dwmapi is not None:
        try:
            bounds = wintypes.RECT()
            result = _dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd), wintypes.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
                ctypes.byref(bounds), ctypes.sizeof(bounds))
            if result == 0 and bounds.right > bounds.left and bounds.bottom > bounds.top:
                return QRect(bounds.left, bounds.top,
                             bounds.right - bounds.left, bounds.bottom - bounds.top)
        except Exception:
            pass
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    return QRect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


def _window_rect(hwnd: int) -> Optional[QRect]:
    """``GetWindowRect`` 的窗口矩形（物理像素）。

    跟 ``window_frame_rect()`` 只差一条不可见边框：DWM 的 *extended frame bounds* 是
    看得见的那一圈，``GetWindowRect`` 还多算 Windows 用来做缩放手柄的隐形边（约 7px）。
    网站监视器记录的"扫描时的窗口矩形"用的是 ``GetWindowRect``，算拖动补偿时两端必须
    同源，所以这里单独留一个 GetWindowRect 版本。
    """
    if _user32 is None or not hwnd:
        return None
    try:
        rect = wintypes.RECT()
        if not _user32.GetWindowRect(wintypes.HWND(int(hwnd)), ctypes.byref(rect)):
            return None
        return QRect(rect.left, rect.top, max(1, rect.right - rect.left),
                     max(1, rect.bottom - rect.top))
    except Exception:
        return None


def client_rect(hwnd: int) -> QRect:
    """客户区尺寸（物理像素）。"""
    if _user32 is None:
        return QRect()
    rect = wintypes.RECT()
    if not _user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return QRect()
    return QRect(0, 0, max(0, rect.right), max(0, rect.bottom))


def parent_of(hwnd: int) -> int:
    if _user32 is None or not hwnd:
        return 0
    return int(_user32.GetParent(wintypes.HWND(hwnd)) or 0)


def minimize_window(hwnd: int) -> None:
    if _user32 is None:
        return
    try:
        _user32.ShowWindow(wintypes.HWND(hwnd), SW_MINIMIZE)
    except Exception:
        pass


def focus_window(hwnd: int) -> bool:
    """把目标窗口拉到前台（``SetForegroundWindow`` 失败时退回 ``BringWindowToTop``）。

    遮罩窗口是 ``WindowDoesNotAcceptFocus`` + ``WA_ShowWithoutActivating`` 的"不吃焦点"
    窗口：点它既不会激活遮罩、也不会激活下面的目标窗口（用户实测：被遮罩的程序点不动、
    没法聚焦到它上面去做临时解锁 / 关闭程序）。所以鼠标按在遮罩空白处时，由我们自己
    把目标窗口激活。
    """
    if _user32 is None or not hwnd:
        return False
    handle = wintypes.HWND(int(hwnd))
    try:
        if _user32.SetForegroundWindow(handle):
            return True
    except Exception:
        pass
    # SetForegroundWindow 受"前台锁"限制（调用进程不是前台进程时会被拒）。用户刚点在
    # 我们的遮罩上，一般情况下这一条已经满足；万一被拒，就补一次经典做法：先敲一下
    # ALT（系统会把前台切换权交给我们，然后立刻松开，不留痕迹），再试一次。
    try:
        _user32.keybd_event(VK_MENU, 0, 0, None)
        _user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)
        if _user32.SetForegroundWindow(handle):
            return True
    except Exception:
        pass
    try:
        return bool(_user32.BringWindowToTop(handle))
    except Exception:
        return False


def start_window_drag(hwnd: int) -> bool:
    """把"这一次鼠标按住"转交给目标窗口的标题栏，让 Windows 做**原生**窗口移动。

    做法就是经典的那一招：``ReleaseCapture()`` 之后给目标窗口发一条
    ``WM_NCLBUTTONDOWN / HTCAPTION``。用 ``PostMessage`` 而不是 ``SendMessage``：
    SendMessage 会一直阻塞到对方的拖动循环结束（本进程界面线程会被冻住，遮罩也就
    跟不动了），PostMessage 只把消息排进对方队列，自己的线程立刻返回，拖动期间遮罩
    继续按 120ms 的节拍贴住目标窗口。最大化窗口这样拖也会自动还原成可拖动的大小。
    """
    if _user32 is None or not hwnd:
        return False
    try:
        _user32.ReleaseCapture()
    except Exception:
        pass
    try:
        return bool(_user32.PostMessageW(wintypes.HWND(int(hwnd)), WM_NCLBUTTONDOWN,
                                         HTCAPTION, 0))
    except Exception:
        return False


def move_window(hwnd: int, left: int, top: int) -> bool:
    """把窗口左上角挪到 ``(left, top)``（物理像素）：尺寸 / 层级 / 激活状态都不动。"""
    if _user32 is None or not hwnd:
        return False
    try:
        return bool(_user32.SetWindowPos(wintypes.HWND(int(hwnd)), None,
                                         int(left), int(top), 0, 0,
                                         SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE))
    except Exception:
        return False


def is_maximized(hwnd: int) -> bool:
    if _user32 is None or not hwnd:
        return False
    try:
        return bool(_user32.IsZoomed(wintypes.HWND(int(hwnd))))
    except Exception:
        return False


def restore_window(hwnd: int) -> bool:
    """还原最大化的窗口（原生拖动最大化窗口时系统也是先还原）。"""
    if _user32 is None or not hwnd:
        return False
    try:
        return bool(_user32.ShowWindow(wintypes.HWND(int(hwnd)), SW_RESTORE))
    except Exception:
        return False


def cursor_pos() -> tuple:
    """鼠标当前位置，**物理像素**（跟 ``SetWindowPos`` 同一坐标系，不受 DPR 影响）。"""
    if _user32 is None:
        return (0, 0)
    point = wintypes.POINT()
    try:
        if not _user32.GetCursorPos(ctypes.byref(point)):
            return (0, 0)
    except Exception:
        return (0, 0)
    return (int(point.x), int(point.y))


def left_button_down() -> bool:
    """物理左键是否仍按着：自己实现的拖动靠它判断"松手"。

    （不依赖 Qt 的鼠标事件——拖动时窗口会从光标下面跑掉，事件随时可能断。）
    """
    if _user32 is None:
        return False
    try:
        return bool(int(_user32.GetAsyncKeyState(VK_LBUTTON)) & 0x8000)
    except Exception:
        return False


def is_topmost(hwnd: int) -> bool:
    """窗口是否处于 TOPMOST 层（部分全屏游戏/播放器会自己置顶）。"""
    if _user32 is None or not hwnd:
        return False
    try:
        ex_style = int(_user32.GetWindowLongW(wintypes.HWND(int(hwnd)), GWL_EXSTYLE))
        return bool(ex_style & WS_EX_TOPMOST)
    except Exception:
        return False


def place_topmost(hwnd_overlay: int) -> bool:
    """把遮罩设成 TOPMOST（只能盖住提权目标时的退路，会盖住所有普通窗口）。"""
    return _set_after(hwnd_overlay, HWND_TOPMOST)


def _set_after(hwnd_overlay: int, insert_after: int) -> bool:
    if _user32 is None or not hwnd_overlay:
        return False
    try:
        return bool(_user32.SetWindowPos(wintypes.HWND(int(hwnd_overlay)),
                                         wintypes.HWND(int(insert_after)), 0, 0, 0, 0,
                                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))
    except Exception:
        return False


def place_above(hwnd_overlay: int, hwnd_target: int) -> bool:
    """把顶层遮罩压在目标窗口正上方（不设 TOPMOST，避免盖住其它程序）。

    返回是否成功。**目标窗口属于更高完整性级别的进程（提权运行的游戏）时 UIPI 会
    直接拒绝这次调用**（实测 `SetWindowPos` 返回 FALSE），此时遮罩只能退化为
    TOPMOST 置顶（见 `place_topmost`），调用方需要据此切换显示策略。

    注意 ``SetWindowPos`` 的 ``hWndInsertAfter`` 是"排在被调整窗口**前面**"的窗口，
    也就是被调整窗口会落到它**后面**：直接传目标窗口会把遮罩塞到目标窗口下面
    （实测两个重叠窗口时，传目标窗口后遮罩被目标窗口完全盖住）。
    正确做法是插到"目标窗口正上方那个窗口"的后面，遮罩就正好落在目标之上。

    插入点必须和目标的"置顶性"一致，否则遮罩会被带进 TOPMOST 层，盖住所有普通窗口
    （实测：遮罩把屏幕上除当前焦点窗口以外的窗口全部盖住）。按目标窗口的上一个窗口
    分四种情况：
    * 目标非置顶、上一个窗口也非置顶 → 插到它后面（同为普通层，正好压在目标上）。
    * 目标非置顶、上一个窗口是置顶窗（说明目标已是普通层最上面）→ 用 ``HWND_NOTOPMOST``，
      它会落在"所有普通窗口之上、所有置顶窗之下"，正好就是目标上方。
    * 目标置顶、上一个窗口也置顶 → 插到它后面（同为置顶层）。
    * 目标置顶且上面没有窗口 → ``HWND_TOPMOST``。
    """
    if _user32 is None or not hwnd_overlay or not hwnd_target:
        return False
    try:
        target = wintypes.HWND(int(hwnd_target))
        previous = int(_user32.GetWindow(target, GW_HWNDPREV) or 0)
        if previous == int(hwnd_overlay):
            return True                           # 已经紧贴在目标上方
        target_top = is_topmost(hwnd_target)
        if previous and is_topmost(previous) == target_top:
            insert_after = previous
        else:
            insert_after = HWND_TOPMOST if target_top else HWND_NOTOPMOST
        return _set_after(hwnd_overlay, insert_after)
    except Exception:
        return False


def is_directly_above(hwnd_overlay: int, hwnd_target: int) -> bool:
    """遮罩是否**紧贴**在目标窗口上方（目标窗口的上一个窗口就是遮罩）。

    只判断"在目标之上"是不够的：顶层遮罩被 show() 时 Qt 会把它抬到同层最上面，
    于是它虽然"在目标之上"，却同时盖住了目标与遮罩之间的其它窗口
    （实测：遮罩把屏幕上除当前焦点窗口以外的所有窗口都盖住了）。
    """
    if _user32 is None or not hwnd_overlay or not hwnd_target:
        return False
    try:
        previous = int(_user32.GetWindow(wintypes.HWND(int(hwnd_target)), GW_HWNDPREV) or 0)
        return previous == int(hwnd_overlay)
    except Exception:
        return False


def is_above(hwnd_overlay: int, hwnd_target: int) -> bool:
    """遮罩是否在目标窗口之上（沿 GW_HWNDPREV 链向上查找）。

    每帧都重设 z 序会让全屏目标程序与遮罩互相抢层级（表现为周期性闪烁），
    所以只在层级真的不对时才动它。
    """
    if _user32 is None or not hwnd_overlay or not hwnd_target:
        return False
    try:
        cur = int(_user32.GetWindow(wintypes.HWND(int(hwnd_target)), GW_HWNDPREV) or 0)
        for _ in range(64):
            if not cur:
                return False
            if cur == int(hwnd_overlay):
                return True
            cur = int(_user32.GetWindow(wintypes.HWND(cur), GW_HWNDPREV) or 0)
    except Exception:
        return False
    return False


def foreground_window() -> int:
    """当前前台窗口的 hwnd（0 表示未知）。"""
    if _user32 is None:
        return 0
    try:
        return int(_user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def window_pid(hwnd: int) -> int:
    """窗口所属进程 PID（0 表示未知）。"""
    if _user32 is None or not hwnd:
        return 0
    try:
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def is_window(hwnd: int) -> bool:
    """HWND 是否仍然有效。"""
    if _user32 is None or not hwnd:
        return False
    try:
        return bool(_user32.IsWindow(wintypes.HWND(int(hwnd))))
    except Exception:
        return False


def is_iconic(hwnd: int) -> bool:
    """窗口是否已最小化。"""
    if _user32 is None or not hwnd:
        return False
    try:
        return bool(_user32.IsIconic(wintypes.HWND(int(hwnd))))
    except Exception:
        return False


def show_window(hwnd: int, visible: bool) -> None:
    """用 Win32 显隐窗口（不激活）。"""
    if _user32 is None or not hwnd:
        return
    try:
        _user32.ShowWindow(wintypes.HWND(int(hwnd)), SW_SHOWNA if visible else SW_HIDE)
    except Exception:
        pass


def visible_windows(pids: set[int]) -> dict[int, list[tuple[int, QRect, bool]]]:
    """返回 {pid: [(hwnd, 窗口矩形(物理像素), 是否最小化), ...]}。"""
    result: dict[int, list[tuple[int, QRect, bool]]] = {}
    if _user32 is None or not pids:
        return result

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    leftover = set(int(p) for p in pids)

    def _callback(hwnd, _lparam):
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in leftover:
            return True
        if not _user32.IsWindowVisible(hwnd):
            return True
        iconified = bool(_user32.IsIconic(hwnd))
        ex_style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        owned = _user32.GetWindow(hwnd, GW_OWNER)
        if ex_style & WS_EX_TOOLWINDOW and not ex_style & WS_EX_APPWINDOW:
            return True
        if owned and not ex_style & WS_EX_APPWINDOW:
            return True
        buffer = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, buffer, 256)
        if buffer.value in _SKIP_CLASSES:
            return True
        rect = window_frame_rect(int(hwnd))
        if rect is None or rect.width() < 40 or rect.height() < 40:
            return True
        result.setdefault(pid.value, []).append((int(hwnd), rect, iconified))
        return True

    try:
        _user32.EnumWindows(EnumWindowsProc(_callback), 0)
    except Exception:
        return result
    return result


def _format_seconds(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _repolish(widget: QWidget) -> None:
    """动态属性变了之后让 QSS 重新生效（属性选择器写在遮罩面板上、管到子控件）。"""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


# --------------------------------------------------------------------------
# 遮罩窗口
# --------------------------------------------------------------------------
class OverlayWindow(QWidget):
    """单个目标窗口上的遮罩：尽量以子窗口形式挂载，随父窗口移动。"""

    unlock_requested = Signal(int)          # program_id
    close_requested = Signal(int)           # program_id

    #: 按住多久才算"拖动窗口"（短于它的按下 = 只聚焦，不改动窗口）
    LONG_PRESS_MS = 200
    #: 拖动时移动窗口的节拍（≈60fps）
    DRAG_MS = 15

    def __init__(self, program_id: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.program_id = int(program_id)
        self.is_site = False                     # 网站遮罩（位置来自网站监视器给的页面区）
        self.target_hwnd = 0
        self.mounted = False
        self.last_mount_failed = False
        self.last_mount_try = 0.0
        self.unmountable = False                 # 目标窗口拒绝被挂载（如全屏游戏进程
                                                 # 拥有者不同/SetParent 被拒），不再重试
        self.placement_denied = False            # 目标窗口不允许本进程调整 z 序（UIPI：
                                                 # 提权游戏），只能退化成 TOPMOST 置顶
        self.last_place_try = 0.0
        self._layer_visible = False
        self._foreign_parent: Optional[QWindow] = None
        self._style: Optional[int] = None
        self._ex_style: Optional[int] = None
        #: 自己实现的拖动（长按起）：目标窗口矩形 / 光标在按下时的物理位置
        self._dragging = False
        self._drag_target = 0
        self._drag_win: Optional[QRect] = None
        self._drag_cursor: Optional[tuple] = None
        #: 最近一次同步时遮罩自身的物理矩形 + 当时目标窗口的框架矩形，
        #: 供"快速跟随"（30ms 只做几何补偿）算窗口位移
        self._phys_rect: Optional[QRect] = None
        self._frame_rect: Optional[QRect] = None
        self._drag_arm = QTimer(self)
        self._drag_arm.setSingleShot(True)
        self._drag_arm.setInterval(self.LONG_PRESS_MS)
        self._drag_arm.timeout.connect(self._begin_drag)
        self._drag_timer = QTimer(self)
        self._drag_timer.setInterval(self.DRAG_MS)
        self._drag_timer.timeout.connect(self._drag_step)
        #: True = 紧凑遮罩（画中画这种小窗），None = 还没定过
        self._compact: Optional[bool] = None
        #: 最近一次判定：紧凑 / 普通形态切换时要照它重渲染文案
        self._decision = None
        #: 目标窗口不允许调整 z 序、遮罩只能强制置顶（要在遮罩上提示解决办法）
        self._topmost_fallback = False
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus
                            | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.panel = QWidget(self)
        self.panel.setObjectName("OverlayPanel")
        self.panel.setAttribute(Qt.WA_StyledBackground, True)
        outer.addWidget(self.panel, 1)

        inner = self._inner = QVBoxLayout(self.panel)
        inner.setContentsMargins(config.sx(28), config.sx(24), config.sx(28), config.sx(24))
        inner.setSpacing(config.sx(12))
        inner.addStretch(1)

        self.title = QLabel(tr("overlay.limit_title"), self.panel)
        self.title.setObjectName("OverlayTitle")
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setWordWrap(True)
        inner.addWidget(self.title)

        self.countdown = QLabel("", self.panel)
        self.countdown.setObjectName("OverlayCountdown")
        self.countdown.setAlignment(Qt.AlignCenter)
        inner.addWidget(self.countdown)

        # 密码锁定提示（点"临时解锁"输错太多次时用，平时隐藏）
        self.notice = QLabel("", self.panel)
        self.notice.setObjectName("OverlayNotice")
        self.notice.setAlignment(Qt.AlignCenter)
        self.notice.setWordWrap(True)
        inner.addWidget(self.notice)
        self.notice.setVisible(False)

        # 目标程序是提权进程时无法"只盖住它"：给出可操作提示（以管理员身份重启）
        self.hint = QLabel(tr("overlay.admin_hint"), self.panel)
        self.hint.setObjectName("OverlayHint")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setWordWrap(True)
        inner.addWidget(self.hint)
        self.hint.setVisible(False)

        row = self._row = QHBoxLayout()
        row.setSpacing(config.sx(12))
        row.addStretch(1)
        self.unlock_btn = QPushButton(tr("overlay.unlock"), self.panel)
        self.unlock_btn.setObjectName("OverlayUnlock")
        self.unlock_btn.setCursor(Qt.PointingHandCursor)
        self.unlock_btn.setFixedHeight(config.sx(38))
        self.unlock_btn.clicked.connect(lambda: self.unlock_requested.emit(self.program_id))
        row.addWidget(self.unlock_btn)
        self.close_btn = QPushButton(tr("overlay.close_program"), self.panel)
        self.close_btn.setObjectName("OverlayClose")
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setFixedHeight(config.sx(38))
        self.close_btn.clicked.connect(lambda: self.close_requested.emit(self.program_id))
        row.addWidget(self.close_btn)
        row.addStretch(1)
        inner.addLayout(row)
        inner.addStretch(1)

    # ------------------------------------------------------------------
    def apply_decision(self, decision) -> None:
        """记下这次判定并渲染文案（紧凑 / 普通两种形态共用这一份数据）。"""
        self._decision = decision
        self._render_decision()

    def _render_decision(self) -> None:
        decision = self._decision
        if decision is None:
            return
        compact = bool(self._compact)
        if decision.reason == "daily":
            self.title.setText(tr("overlay.compact.daily_title" if compact
                                  else "overlay.daily_title"))
            self.countdown.setText(tr("overlay.daily_countdown",
                                      time=_format_seconds(decision.remaining)))
        elif decision.reason == "window":
            # 指定时间段：到点自动解除，所以提示"距离本时段结束"
            self.title.setText(tr("overlay.compact.window_title" if compact
                                  else "overlay.window_title"))
            self.countdown.setText(tr("overlay.window_countdown",
                                      time=_format_seconds(decision.remaining)))
        else:
            if compact:
                # 紧凑遮罩放不下"连续使用 00:45:00"这种长句，只留结论
                self.title.setText(tr("overlay.compact.streak_title"))
            else:
                self.title.setText(tr("overlay.streak_title",
                                      time=_format_seconds(decision.remaining)))
            self.countdown.setText(tr("overlay.streak_countdown",
                                      time=_format_seconds(decision.remaining)))

    # ------------------------------------------------------------------
    def set_compact(self, compact: bool) -> None:
        """切成"紧凑遮罩"：画中画这种两三百像素的小窗专用。

        普通遮罩铺满整个程序窗口，边距 / 按钮都可以按桌面尺寸来；画中画小窗照那个
        尺寸排下来，标题和两个按钮会挤成一团，所以另给一套小边距、矮按钮和短文案。
        """
        compact = bool(compact)
        if self._compact == compact:
            return
        self._compact = compact
        margin = config.sx(10) if compact else config.sx(28)
        vertical = config.sx(8) if compact else config.sx(24)
        self._inner.setContentsMargins(margin, vertical, margin, vertical)
        self._inner.setSpacing(config.sx(6) if compact else config.sx(12))
        self._row.setSpacing(config.sx(6) if compact else config.sx(12))
        height = config.sx(26) if compact else config.sx(38)
        for button in (self.unlock_btn, self.close_btn):
            button.setFixedHeight(height)
        self.unlock_btn.setText(tr("overlay.compact.unlock" if compact
                                   else "overlay.unlock"))
        self.close_btn.setText(tr("overlay.compact.close" if compact
                                  else "overlay.close_program"))
        if compact:
            self.hint.setVisible(False)
        self._sync_hint()
        self.panel.setProperty("compact", "true" if compact else "false")
        for widget in [self.panel] + self.panel.findChildren(QWidget):
            _repolish(widget)
        self._render_decision()

    # ------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        """短按 = 聚焦目标窗口；程序遮罩按住 ``LONG_PRESS_MS`` 之后拖动 = 把窗口挪走。

        遮罩是"不吃焦点"的（``WindowDoesNotAcceptFocus`` + ``WA_ShowWithoutActivating``），
        点它既不会激活遮罩、也不会激活下面的目标窗口，于是用户碰到两个不便：聚焦不到
        被遮罩的程序（没法去做临时解锁 / 关闭程序）；也抓不住它的标题栏把窗口挪开。

        早期版本把"这一次按住"整个转交给系统的原生移动循环（``PostMessage`` +
        ``WM_NCLBUTTONDOWN/HTCAPTION``）：PostMessage 是**异步**的，目标窗口处理到它时
        物理按键往往已经松开，系统于是进入了一个"按键早已抬起"的移动循环——表现为
        松手后窗口还跟着鼠标跑，必须再点一下左键才停下。现在自己实现移动：
        按住够 200ms 才算拖动，松手（物理左键抬起）立刻结束。

        **网站遮罩不参与拖动**（``self.is_site``，见 ``_arm_drag``）：它盖住的是浏览器
        窗口，浏览器自己的标题栏还露在外面，用户拖标题栏就能移动；而且在遮罩上按住拖动
        会和页面里的拖拽（视频进度条、滑块）抢手势。
        """
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        target = int(self.target_hwnd or 0)
        if not target or not is_window(target):
            super().mousePressEvent(event)
            return
        focus_window(target)
        self._arm_drag(target)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._end_drag()
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------------
    def _arm_drag(self, target: int) -> None:
        """按下之后开始计时：够长就进入拖动，提前松手就只当"聚焦"。

        网站遮罩直接拒绝武装：短按聚焦照旧（上面已经 ``focus_window``），长按不再拖动。
        """
        if self.is_site:
            return
        self._end_drag()
        self._drag_target = int(target)
        self._drag_cursor = cursor_pos()
        self._drag_win = window_frame_rect(int(target))
        if self._drag_win is not None:
            self._drag_arm.start()

    def _begin_drag(self) -> None:
        target = int(self._drag_target or 0)
        if not target or not is_window(target) or not left_button_down():
            self._end_drag()
            return
        if is_maximized(target):
            # 原生拖动最大化窗口时会先还原成普通大小；这里也还原，并让光标落在
            # 窗口里的同一个相对位置（不然窗口会"跳"一下）
            frame = window_frame_rect(target)
            cursor = cursor_pos()
            restore_window(target)
            restored = window_frame_rect(target)
            if frame is not None and restored is not None \
                    and frame.width() > 0 and frame.height() > 0:
                rel_x = (cursor[0] - frame.x()) / float(frame.width())
                rel_y = (cursor[1] - frame.y()) / float(frame.height())
                move_window(target,
                            int(cursor[0] - rel_x * restored.width()),
                            int(cursor[1] - rel_y * restored.height()))
            self._drag_cursor = cursor_pos()
            self._drag_win = window_frame_rect(target)
        if self._drag_win is None or self._drag_cursor is None:
            self._end_drag()
            return
        self._dragging = True
        self._drag_timer.start()

    def _drag_step(self) -> None:
        target = int(self._drag_target or 0)
        if not self._dragging or not target or not is_window(target) \
                or not left_button_down():
            self._end_drag()
            return
        cursor = cursor_pos()
        left = self._drag_win.x() + cursor[0] - self._drag_cursor[0]
        top = self._drag_win.y() + cursor[1] - self._drag_cursor[1]
        if not move_window(target, left, top):
            self._end_drag()
            return
        self.follow_now()                        # 遮罩立刻跟上，不等 120ms 那一拍

    def _end_drag(self) -> None:
        self._drag_arm.stop()
        self._drag_timer.stop()
        self._dragging = False
        self._drag_target = 0
        self._drag_win = None
        self._drag_cursor = None

    # ------------------------------------------------------------------
    def apply_lock(self, remaining: float) -> None:
        """密码错误过多被锁定时在遮罩上给出提示，并暂时禁用"临时解锁"。"""
        locked = remaining > 0
        if locked:
            self.notice.setText(tr("overlay.locked",
                                   seconds=max(1, int(remaining + 0.999))))
            if not self.notice.isVisible():
                self.notice.setVisible(True)
        elif self.notice.isVisible():
            self.notice.setVisible(False)
        if self.unlock_btn.isEnabled() == locked:
            self.unlock_btn.setEnabled(not locked)

    # ------------------------------------------------------------------
    def set_close_visible(self, visible: bool) -> None:
        """是否提供"关闭程序"按钮：网站目标只遮罩，不能关掉整个浏览器。"""
        self.close_btn.setVisible(bool(visible))

    def set_topmost_fallback(self, active: bool) -> None:
        """标记"遮罩被迫置顶"（目标提权/不允许调整 z 序）并在遮罩上提示解决办法。"""
        self._topmost_fallback = bool(active)
        self._sync_hint()

    def _sync_hint(self) -> None:
        """提示文字的显隐：被迫置顶时显示；紧凑遮罩（画中画小窗）里一律不显示。"""
        visible = self._topmost_fallback and not self._compact
        if visible == self.hint.isVisible():
            return
        self.hint.setVisible(visible)

    # ------------------------------------------------------------------
    def set_layer_visible(self, visible: bool) -> None:
        """遮罩显隐。

        挂成子窗口之后必须走 Win32：Qt 的 show()/hide() 会重新套用顶层窗口样式，
        把子窗口变回弹出窗口，于是遮罩每 1.5 秒"闪"一下（先消失再出现）。
        """
        if self.mounted:
            show_window(int(self.winId()), bool(visible))
            self._layer_visible = bool(visible)
            return
        if visible:
            if not self.isVisible():
                self.show()
        else:
            self._end_drag()                     # 遮罩藏起来时别再移动窗口
            self.hide()
        self._layer_visible = bool(visible)

    def mount_into(self, hwnd_target: int) -> bool:
        """把遮罩挂成目标窗口的子窗口；成功返回 True。

        全屏（无边框）目标窗口同样支持挂载 —— 这是遮罩能在全屏程序上显示、
        又不会盖住其它窗口的关键：子窗口只存在于父窗口的层级里。
        """
        if _user32 is None or not hwnd_target:
            return False
        try:
            if not _user32.IsWindow(wintypes.HWND(int(hwnd_target))):
                return False
        except Exception:
            return False
        self.target_hwnd = int(hwnd_target)
        # Chromium / Electron 的程序窗口（Chrome_WidgetWin_1 之类）用 DirectComposition
        # 合成，ex_style 带 WS_EX_NOREDIRECTIONBITMAP：没有可重定向的表面，SetParent 会
        # "成功"但子窗口一像素都不绘制（实测同一块洋红窗口：顶层摆放 4240 像素 → 挂成
        # 子窗 0 像素），而挂载"成功"后代码就不会再退回顶层通路，限制看起来像失效。
        # 这类目标统一判定为不可挂载 —— 不是针对某个程序，任何 DirectComposition 宿主
        # （VS Code / Discord / Slack / Teams 等）都走顶层遮罩，跟全屏游戏失败时同一条路。
        try:
            target_ex_style = int(
                _user32.GetWindowLongW(wintypes.HWND(int(hwnd_target)), GWL_EXSTYLE))
        except Exception:
            target_ex_style = 0
        if target_ex_style & WS_EX_NOREDIRECTIONBITMAP:
            self.mounted = False
            self.last_mount_failed = True
            self.unmountable = True
            self._restore_top_level()
            return False
        # 先让 Qt 认为窗口已显示（否则它不会绘制内容）；挂载后用 Win32 控制显隐
        if not self.isVisible():
            self.show()
        hwnd = int(self.winId())
        if hwnd and (self._style is None or self._ex_style is None):
            # 记住原始样式：挂载失败时要能恢复成普通顶层窗口
            try:
                self._style = int(_user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE))
                self._ex_style = int(_user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE))
            except Exception:
                pass
        # 1) 首选 Qt 原生 API：把窗口嵌入外部窗口
        try:
            handle = self.windowHandle()
            foreign = QWindow.fromWinId(int(hwnd_target))
            if handle is not None and foreign is not None:
                handle.setParent(foreign)
                self._foreign_parent = foreign
                if parent_of(int(self.winId())) == int(hwnd_target):
                    self.mounted = True
                    self.last_mount_failed = False
                    self.unmountable = False
                    self._layer_visible = True
                    show_window(int(self.winId()), True)
                    self._resize_to_parent()
                    return True
        except Exception:
            pass
        # 2) 退回 Win32：SetParent + WS_CHILD
        try:
            ctypes.set_last_error(0)
            _user32.SetParent(wintypes.HWND(hwnd), wintypes.HWND(int(hwnd_target)))
            style = _user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE)
            style = (style & ~WS_POPUP) | WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS
            _user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE, style)
            ex_style = _user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
            _user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE,
                                   ex_style & ~(WS_EX_TOOLWINDOW | WS_EX_APPWINDOW))
            _user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                                 | SWP_NOACTIVATE | SWP_FRAMECHANGED)
            if parent_of(hwnd) == int(hwnd_target):
                self.mounted = True
                self.last_mount_failed = False
                self.unmountable = False
                self._layer_visible = True
                show_window(hwnd, True)
                self._resize_to_parent()
                return True
        except Exception:
            pass
        # 两条路径都失败：必须回滚，否则窗口会挂在 Qt 的临时父窗口上（既不可见
        # 也收不到点击）。全屏游戏进程常常直接拒绝 SetParent（ERROR_ACCESS_DENIED，
        # 因为反作弊/权限不同），此时标记为不可挂载，之后一直用顶层遮罩。
        self.mounted = False
        self.last_mount_failed = True
        self.unmountable = True
        self._restore_top_level()
        return False

    def _restore_top_level(self) -> None:
        """把窗口恢复成普通顶层窗口（挂载失败的收尾）。"""
        hwnd = int(self.winId())
        if _user32 is None or not hwnd:
            return
        try:
            if self._style is not None:
                _user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE, self._style)
            if self._ex_style is not None:
                _user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE, self._ex_style)
            _user32.SetParent(wintypes.HWND(hwnd), None)
            _user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                                 | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        except Exception:
            pass
        self._foreign_parent = None

    def _resize_to_parent(self) -> None:
        """把遮罩铺满父窗口客户区（坐标从 (0,0) 起，随父窗口自动跟随）。"""
        if not self.mounted or not self.target_hwnd:
            return
        size = logical_size(client_rect(self.target_hwnd))
        if size.width() <= 0 or size.height() <= 0:
            return
        if self.geometry() != size:
            self.setGeometry(size)

    def sync_geometry(self, rect: QRect) -> None:
        """顶层遮罩模式：按目标窗口的屏幕矩形摆放（``rect`` 是**物理**像素）。"""
        if self.mounted:
            self._resize_to_parent()
            return
        self._phys_rect = QRect(rect)
        self._frame_rect = window_frame_rect(int(self.target_hwnd or 0))
        target = physical_to_logical(rect)
        if self.geometry() != target:
            self.setGeometry(target)

    def follow_now(self) -> None:
        """快速跟随：目标窗口动了就立刻贴上去（只做一两次 ``GetWindowRect``）。

        ``SYNC_MS``（120ms）那一拍对"用户正拖着窗口跑"太慢，遮罩会明显落在后面
        （看起来就是"拖动时很卡"）。这里用一个 30ms 的小定时器只补几何：窗口框架
        跟上次同步时不一样才动，代价只有一次 DWM 框架矩形查询（`window_frame_rect()`，
        失败时退回 `GetWindowRect`），不碰枚举 / 静音 / 输入钩子，也不读 UIA。
        """
        if self.mounted or not self._layer_visible or not self.target_hwnd:
            return
        if self._phys_rect is None or self._frame_rect is None:
            return
        frame = window_frame_rect(int(self.target_hwnd))
        if frame is None or frame == self._frame_rect:
            return
        rect = _follow_window(self._phys_rect, self._frame_rect, frame)
        self._frame_rect = frame
        self._phys_rect = rect
        target = physical_to_logical(rect)
        if self.geometry() != target:
            self.setGeometry(target)

    def unmount(self) -> None:
        """销毁前解挂，避免目标窗口销毁我们的子窗口。"""
        if not self.mounted:
            return
        try:
            if self._foreign_parent is not None:
                handle = self.windowHandle()
                if handle is not None:
                    handle.setParent(None)
                self._foreign_parent = None
        except Exception:
            pass
        try:
            if _user32 is not None:
                _user32.SetParent(wintypes.HWND(int(self.winId())), None)
        except Exception:
            pass
        self.mounted = False
        self._layer_visible = False

    def is_mounted_to(self, hwnd_target: int) -> bool:
        if not self.mounted or not self.target_hwnd:
            return False
        return parent_of(int(self.winId())) == int(hwnd_target)


# --------------------------------------------------------------------------
# 管理器
# --------------------------------------------------------------------------
class OverlayManager(QWidget):
    """维护"被限制程序"的遮罩集合。

    ``blocked_provider`` → {program_id: Decision}
    ``pids_provider``    → 该程序当前运行的系统 PID 列表
    """

    unlock_requested = Signal(int)
    close_requested = Signal(int)

    SYNC_MS = 120                                # 顶层遮罩模式的跟随间隔
    FOLLOW_MS = 30                               # 只做几何的"快速跟随"间隔（窗口被拖动时用）
    REMOUNT_MS = 1500                            # 子窗口挂载状态检查间隔
    SCAN_MS = 200                                # 枚举目标窗口的最小间隔
    EFFECT_TTL = 2.0                             # 子进程展开（psutil）缓存时间
    EXPAND_MAP_TTL = 1.0                         # 进程表 {pid: ppid} 快照的复用时间
    FORCE_MIN_S = 0.6                            # 两次"强制音频枚举"之间的最小间隔
    GRACE_S = 3.0                                # 窗口短暂消失时的宽限期（避免闪烁）
    PLACE_RETRY_MS = 5000                        # z 序被拒（提权目标）后的重试间隔

    def __init__(self, blocked_provider: Callable[[], dict],
                 pids_provider: Optional[Callable[[int], list]] = None,
                 parent: Optional[QWidget] = None,
                 lock_provider: Optional[Callable[[], float]] = None,
                 muter=None,
                 site_provider: Optional[Callable[[], dict]] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_DontShowOnScreen, True)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self._blocked_provider = blocked_provider
        self._pids_provider = pids_provider
        # 网站目标（被追踪的网址）只能按"具体哪个浏览器窗口"遮挡：
        # 由它返回 {program_id: {"windows": [hwnd], "mute_pids": {...}, "block_pids": {...}}}
        self._site_provider = site_provider
        self.lock_provider = lock_provider
        self._overlays: dict[int, dict[int, OverlayWindow]] = {}
        self._suspended = False
        self._muter = muter if muter is not None else ProcessMuter()
        self._blocker = InputBlocker(self._input_state)
        self._last_remount = 0.0
        self._scan_pids: Optional[set] = None
        self._scan_at = 0.0
        self._scan_result: dict = {}
        self._effects_base: Optional[set] = None
        self._effects_site_base: set = set()  # 网站目标的实例 PID（只静音、不拦输入）
        self._effects_expanded: set = set()
        self._effects_applied: set = set()
        self._effects_mute: set = set()      # 实际下发给静音器的 PID 集合
        self._effects_block: set = set()     # 实际下发给输入钩子的 PID 集合
        self._effects_extras: tuple = ()     # (mute_extra, block_extra) 缓存键
        self._effects_at = 0.0
        self._forced_at = 0.0                # 上次强制音频枚举的时间
        self._ppid_cache: Optional[dict] = None     # 进程表快照缓存（见 _ppid_snapshot）
        self._ppid_cache_at = 0.0
        self._blocker_key: Optional[tuple] = None   # 上次下发给钩子的（静音/拦截/遮罩）状态
        self._missing: dict[tuple, float] = {}      # (program_id, hwnd) -> 首次发现消失的时间
        self._timer = QTimer(self)
        self._timer.setInterval(self.SYNC_MS)
        self._timer.timeout.connect(self.sync)
        self._effects_idle = False
        # 只做几何的快速跟随：拖动窗口时让遮罩 1:1 贴着走（详见 follow_now）
        self._follow_timer = QTimer(self)
        self._follow_timer.setInterval(self.FOLLOW_MS)
        self._follow_timer.timeout.connect(self._follow_all)
        # 高频定时器仅在存在限制目标时启动。
        # 前台窗口事件钩子：用户点回浏览器时 shell 会把浏览器抬到遮罩上面，
        # 只靠 SYNC_MS(120ms) 的轮询补位会露出那一瞬间的底部画面（用户 ②）。
        # 钩子取不到就退回纯轮询（不影响功能，只是偶尔闪一下）。
        self._fg_hook = ForegroundHook(self)
        self._fg_hook.changed.connect(self._on_foreground_changed)
        if not self._fg_hook.start():
            trace.log("hook", f"前台事件钩子不可用：{self._fg_hook.last_error()}（退回轮询）")

    def _on_foreground_changed(self) -> None:
        """前台换了：立刻把顶层遮罩重新压回目标窗口上方（不等下一拍）。

        只做 z 序这一件事：不做枚举、不建窗口，所以可以在每次前台变化时跑。
        已经贴着（``is_directly_above``）的遮罩直接跳过，一次 GetWindow 就结束。
        """
        if self._suspended or not self._overlays:
            return
        for program_id in list(self._overlays):
            windows = self._overlays.get(int(program_id)) or {}
            for overlay in list(windows.values()):
                try:
                    if overlay.mounted or not overlay._layer_visible:
                        continue
                    target = int(getattr(overlay, "target_hwnd", 0) or 0)
                    if not target:
                        continue
                    win_id = int(overlay.winId())
                    if is_directly_above(win_id, target):
                        continue
                    if place_above(win_id, target):
                        overlay.placement_denied = False
                        overlay.last_place_try = __import__("time").time()
                        overlay.set_topmost_fallback(False)
                        trace.log("z", f"前台变化：遮罩 {program_id} 重新压到 {target} 上方")
                    else:
                        trace.log("z", f"前台变化：遮罩 {program_id} 压不回 {target} 上方")
                except Exception as exc:
                    trace.log("z", f"前台变化补位失败：{type(exc).__name__}: {exc}")
                    continue

    def _follow_all(self) -> None:
        """慢一拍也没关系的小活：窗口在动就立刻补几何。没有遮罩时什么都不做。"""
        if self._suspended or not self._overlays:
            return
        for windows in self._overlays.values():
            for overlay in windows.values():
                try:
                    overlay.follow_now()
                except Exception:
                    continue

    # ------------------------------------------------------------------
    def _site_targets(self) -> dict:
        """当前被限制的网站目标（只保留仍在下发限制的 program_id）。"""
        if self._site_provider is None:
            return {}
        try:
            data = self._site_provider() or {}
        except Exception:
            return {}
        result: dict[int, dict] = {}
        for program_id, info in data.items():
            try:
                result[int(program_id)] = info or {}
            except Exception:
                continue
        return result

    def _site_windows(self, info: dict) -> list:
        """把网站目标给的窗口变成 ``(hwnd, rect, iconified, True, compact)`` 条目。

        矩形优先用网站监视器给的**页面内容区**（``info["rect"]``，不含标签栏 /
        工具栏）：网站遮罩只盖网页那一块，标签栏留着不动，用户才能切标签页。
        取不到页面矩形时才退回整个窗口的框架矩形（老行为，聊胜于无）。

        网站监视器给的 ``info["pages"]`` 可以有多项：第一项是浏览器主窗口，后面几项
        是**附属小窗（画中画）**——画中画没有标签栏也没有工具栏，盖"紧凑遮罩"即可
        （``compact = True``，只留标题 + 倒计时 + 操作按钮）。

        主窗口那一项还带 ``frame``（扫描时用 GetWindowRect 记下的窗口矩形）：窗口被
        拖动 / 缩放时用它算出偏移，遮罩就能贴住窗口实时移动。
        """
        entries: list = []
        default_rect = _site_rect(info.get("rect"))
        pages = info.get("pages")
        if not pages:
            # 兼容只有 windows/rect 的老格式
            pages = [{"hwnd": hwnd, "rect": None, "compact": False}
                     for hwnd in (info.get("windows") or ())]
        for item in pages:
            try:
                handle = int(item.get("hwnd") or 0)
            except Exception:
                continue
            if not handle or not is_window(handle):
                continue
            rect = _site_rect(item.get("rect")) or default_rect or window_frame_rect(handle)
            if rect is None:
                continue
            if not item.get("compact"):
                # 页面内容区是"扫描时"读到的，窗口被拖动后要按框架位移补偿一下；
                # 画中画整窗就是矩形本身，不用补偿。
                rect = _follow_window(rect, _site_rect(item.get("frame")),
                                      _window_rect(handle))
            entries.append((handle, rect, is_iconic(handle), True, bool(item.get("compact"))))
        return entries


    def _running_pids(self, program_id: int) -> list:
        if self._pids_provider is None:
            return []
        try:
            return [int(p) for p in (self._pids_provider(int(program_id)) or [])]
        except Exception:
            return []

    def _overlay_hwnds(self) -> frozenset:
        """当前所有遮罩窗口的 HWND（只是 winId()，很便宜）。"""
        hwnds = set()
        for items in self._overlays.values():
            for overlay in items.values():
                try:
                    hwnds.add(int(overlay.winId()))
                except Exception:
                    continue
        return frozenset(hwnds)

    def _input_state(self) -> tuple:
        """给低级钩子用：(被限制的 PID 集合, 自己的遮罩 HWND 集合)。"""
        # 程序目标由 _maintain_effects 算好放在 _effects_block 里；
        # **网站目标不进这个集合**——低级钩子按前台窗口 PID 判定，一拦就把整个浏览器
        # （含标签栏、Ctrl+Tab）一起吞掉，用户就没法切标签页了。网站目标改由盖在页面
        # 内容区上的那块遮罩吃掉点击。
        pids = set(self._effects_block)
        sites = self._site_targets()
        for program_id in self._overlays:
            if int(program_id) in sites:
                continue
            pids.update(self._running_pids(program_id))
        return pids, set(self._overlay_hwnds())

    def overlay_hwnds(self) -> set:
        return self._input_state()[1]

    def blocked_pids(self) -> set:
        return self._input_state()[0]

    # ------------------------------------------------------------------
    def _scan_windows(self, pids: set, now: float) -> dict:
        """枚举目标进程的窗口（带节流：Win32 枚举不便宜，别每次都做）。"""
        if self._scan_pids == pids and (now - self._scan_at) < self.SCAN_MS / 1000.0:
            return self._scan_result
        result = visible_windows(pids)
        self._scan_pids = set(pids)
        self._scan_at = now
        self._scan_result = result
        return result

    def invalidate_scan(self) -> None:
        """丢弃窗口扫描缓存（测试替换 visible_windows 后需要立即生效）。"""
        self._scan_pids = None
        self._scan_at = 0.0
        # 进程表快照同理：测试刚起的子进程要能立刻被 _expand 看到
        self._ppid_cache = None
        self._ppid_cache_at = 0.0

    def lock_remaining(self) -> float:
        if self.lock_provider is None:
            return 0.0
        try:
            return max(0.0, float(self.lock_provider() or 0.0))
        except Exception:
            return 0.0

    def sync(self) -> None:
        now = __import__("time").time()
        blocked = self._blocked_provider() or {}
        wanted = set(int(pid) for pid in blocked)
        for program_id in list(self._overlays):
            if program_id not in wanted:
                self._destroy(program_id)

        if not wanted or QApplication.instance() is None:
            self._timer.stop()
            self._follow_timer.stop()
            if not self._effects_idle:
                self._release_effects()
                self._effects_idle = True
            return

        self._effects_idle = False
        if not self._timer.isActive():
            self._timer.start()
        if not self._follow_timer.isActive():
            self._follow_timer.start()
        pid_map: dict[int, int] = {}
        site_pids: set = set()
        # 网站目标（被追踪的网址）只能按"具体哪个浏览器窗口的页面内容区"遮挡：
        # 由它返回 {program_id: {"windows": [hwnd], "rect": (...), "mute_pids": {...}}}
        sites = self._site_targets()
        for program_id in wanted:
            pids = [int(p) for p in self._running_pids(program_id)]
            if int(program_id) in sites:
                # 网站目标**不能**拿它的实例 PID 去扫窗口、更不能进拦截集合：
                # Chromium 的"渲染进程"就是浏览器根进程（实测 Chrome_RenderWidgetHostHWND
                # 属于根进程），一扫就拿到**整个浏览器窗口**——遮罩会变成整窗、还会被挂成
                # Chromium 合成窗口的子窗口（看不见），根进程进拦截集合后连标签栏都点不动。
                # 但它仍要进**静音**集合（网站被限制时不能出声）。
                site_pids.update(pids)
                continue
            for os_pid in pids:
                pid_map[int(os_pid)] = program_id

        per_program: dict[int, list] = {}
        if pid_map:
            windows = self._scan_windows(set(pid_map), now)
            for os_pid, entries in windows.items():
                program_id = pid_map.get(os_pid)
                if program_id is None:
                    continue
                per_program.setdefault(int(program_id), []).extend(
                    (hwnd, rect, icon, False, False) for hwnd, rect, icon in entries)
        for program_id in wanted:
            info = sites.get(int(program_id))
            if not info:
                continue
            per_program.setdefault(int(program_id), []).extend(self._site_windows(info))

        remount = (now - self._last_remount) >= self.REMOUNT_MS / 1000.0
        if remount:
            self._last_remount = now
        locked = self.lock_remaining()

        for program_id in wanted:
            decision = blocked.get(program_id)
            if decision is None:
                continue
            entries = per_program.get(int(program_id)) or []
            current = self._overlays.setdefault(int(program_id), {})
            alive = {hwnd for hwnd, _rect, _icon, _site, _compact in entries}
            # 目标窗口短暂消失（切全屏、DWM 重排、枚举抖动）时先留宽限期：
            # 立刻销毁再重建会让遮罩每 1.5 秒闪一下。
            for hwnd in list(current):
                key = (int(program_id), hwnd)
                if hwnd in alive:
                    self._missing.pop(key, None)
                    continue
                since = self._missing.setdefault(key, now)
                if (now - since) < self.GRACE_S:
                    current[hwnd].set_layer_visible(False)
                    continue
                self._missing.pop(key, None)
                overlay = current.pop(hwnd)
                overlay.set_layer_visible(False)
                overlay.unmount()
                overlay.deleteLater()
            for hwnd, rect, iconified, is_site, compact in entries:
                overlay = current.get(hwnd)
                if overlay is None:
                    overlay = OverlayWindow(int(program_id))
                    overlay.set_close_visible(not is_site)
                    overlay.unlock_requested.connect(self.unlock_requested.emit)
                    overlay.close_requested.connect(self.close_requested.emit)
                    current[hwnd] = overlay
                overlay.set_compact(bool(compact))
                overlay.is_site = bool(is_site)
                overlay.apply_lock(locked)
                if iconified:
                    overlay.set_layer_visible(False)
                    continue
                overlay.apply_decision(decision)
                # 子窗口挂载：随父窗口移动/缩放/最小化，没有跟随延迟。
                # 全屏（无边框）窗口同样优先挂载——挂不上再退回顶层遮罩。
                # **网站目标永远不挂载**：Chromium 把页面画在合成表面里，挂上去的子窗口
                # 根本看不见（用户反馈的"遮罩没显示"），只能当顶层窗口摆在页面矩形上。
                overlay.target_hwnd = int(hwnd)
                may_try = (not is_site) and (not overlay.mounted) and (not overlay.unmountable) and (
                    (not overlay.last_mount_failed)
                    or (now - overlay.last_mount_try) >= self.REMOUNT_MS / 1000.0)
                if may_try:
                    overlay.last_mount_try = now
                    if overlay.mount_into(hwnd):
                        continue
                if not overlay.mounted:
                    # 顶层遮罩：始终显示（目标窗口最小化的情况已在上面的 iconified 分支返回）。
                    # 显示后必须保证"紧贴"目标窗口上方——Qt show() 会把遮罩抬到同层最上面，
                    # 只判断"在目标之上"不够，那样它会同时盖住用户切过去的其它窗口。
                    overlay.sync_geometry(rect)
                    was_visible = overlay._layer_visible
                    overlay.set_layer_visible(self._should_show_top_level(hwnd, overlay))
                    # z 序：先做一次"还贴着吗"的判定，没被挤下去就什么都不做。
                    # 早期版本把这一步额外限制成"最多每 REMOUNT_MS(1.5s) 检查一次"，
                    # 于是目标窗口被重新激活后会短暂压在遮罩上面——用户看到的就是
                    # "遮罩消失、短时间后又出现"（那一小段时间网站还能被操作）。
                    # 现在每一拍都查（一次 GetWindow，已贴好时 place_above 直接返回）。
                    retry = (overlay.placement_denied
                             and (now - overlay.last_place_try) >= self.PLACE_RETRY_MS / 1000.0)
                    broken = overlay._layer_visible and not is_directly_above(
                        int(overlay.winId()), hwnd)
                    if overlay._layer_visible and (not was_visible or retry or broken):
                        if broken and overlay.mounted is False:
                            trace.log("z", f"轮询发现遮罩不在 {hwnd} 之上，重新压回"
                                           f"（{int(program_id)}）")
                        overlay.last_place_try = now
                        if place_above(int(overlay.winId()), hwnd):
                            overlay.placement_denied = False
                            overlay.set_topmost_fallback(False)
                        elif not is_topmost(hwnd):
                            # UIPI：目标窗口属于更高完整性级别的进程（提权运行的游戏），
                            # 本进程无法把遮罩插到它上方。唯一能盖住它的方式是置顶，
                            # 代价是会同时盖住所有普通窗口——因此换成"只有目标在前台
                            # 时才显示"的严格策略，并在遮罩上提示以管理员身份重启。
                            overlay.placement_denied = True
                            place_topmost(int(overlay.winId()))
                            overlay.set_topmost_fallback(True)
                    continue
                if overlay.mounted:
                    overlay.set_layer_visible(True)
                    overlay._resize_to_parent()

        # 静音 + 拦截输入（程序目标拦输入，网站目标只静音 + 盖页面）
        self._maintain_effects(set(pid_map), sites, site_pids)

    def _should_show_top_level(self, hwnd_target: int, overlay: OverlayWindow) -> bool:
        """顶层遮罩是否应该显示。

        正常情况（能把遮罩塞到"紧贴目标窗口上方"这一格）：始终显示——遮罩只会盖住
        目标窗口本身，用户切到别的窗口后它依然贴在目标窗口上（这正是"切到别的窗口 /
        点画中画时遮罩反而消失"那个 bug 的修法）。目标自己是置顶窗（画中画小窗就是）
        时也走这一条：``place_above`` 会把它一起放进置顶那一档、且只比目标高一位，
        所以它盖住的范围不会超过目标窗口自己盖住的范围。

        唯一的例外是 ``placement_denied``：``SetWindowPos`` 被拒（目标属于更高完整性
        级别的进程，提权运行的游戏），这时只能退化成"霸屏置顶"（``place_topmost``）。
        置顶遮罩会盖住所有普通窗口，所以那种情况下只在目标窗口自己位于前台时才显示，
        避免挡住用户正在用的别的程序（实测：否则焦点在 Monitor 上时遮罩会把整屏除
        焦点窗口外全部盖住）。
        """
        if not overlay.placement_denied:
            return True
        fg = foreground_window()
        if not fg:
            return True
        return fg == int(hwnd_target) or fg == int(overlay.winId())

    def _maintain_effects(self, pids: set, sites: Optional[dict] = None,
                          site_pids: Optional[set] = None) -> None:
        """静音 + 输入拦截（含目标程序的子进程，例如微信的渲染/小程序进程）。

        子进程展开要递归扫进程表，音频会话枚举要走 COM，两者都不能每次 sync
        都做——它们跑在 Qt 主线程，也正是低级输入钩子所在的线程。

        网站目标的 PID 单独处理：**静音要含承载网页的进程**（网站被限制时不能出声），
        **输入拦截绝不含它**（拦了就没法切标签页，页面区的点击交给遮罩自己吃）。
        """
        now = time.time()
        base = set(int(p) for p in pids)
        site_base = set(int(p) for p in (site_pids or ())) - base
        mute_extra: set = set()
        block_extra: set = set()
        for info in (sites or {}).values():
            try:
                mute_extra.update(int(p) for p in (info.get("mute_pids") or ()))
                block_extra.update(int(p) for p in (info.get("block_pids") or ()))
            except Exception:
                continue
        extras = (frozenset(mute_extra), frozenset(block_extra))
        if base and base == self._effects_base and site_base == self._effects_site_base \
                and extras == self._effects_extras \
                and (now - self._effects_at) < self.EFFECT_TTL:
            expanded = set(self._effects_expanded)
            mute = set(self._effects_mute)
            block = set(self._effects_block)
        else:
            expanded = self._expand(base | site_base)
            program_expanded = self._expand(base) if base else set()
            # 网站目标的静音 PID 也要**展开成整棵进程树**：Chromium 的音频通常是共享的
            # ``audio.mojom.AudioService`` 子进程在播（实测：msedge 的音频会话挂在子进程
            # 上，不是根进程），只压渲染/根进程等于没压——用户实测的"网站遮罩出现了但没静音"。
            mute = expanded | self._expand(mute_extra)
            block = program_expanded | block_extra
            self._effects_base = set(base)
            self._effects_site_base = set(site_base)
            self._effects_extras = extras
            self._effects_expanded = set(expanded)
            self._effects_mute = set(mute)
            self._effects_block = set(block)
            self._effects_at = now
        if not mute and not block:
            # 没有任何进程该被限制（例如被限制的程序已经退出、或暂时查不到
            # 它的进程）：彻底放开音量与输入拦截，别把 0% 留在系统里。
            self._release_effects()
            return
        changed = mute != self._effects_applied
        # force=True 会当场走一遍全部音频端点（COM）。集合因为"前台窗口/网站判定抖动"
        # 一秒变几次时，绝不能每次都强制枚举——节流到每 FORCE_MIN_S 一次；期间不写
        # _effects_applied，下一个 sync（120ms 后）会接着试，延迟上限就是 FORCE_MIN_S。
        force = changed and (now - self._forced_at) >= self.FORCE_MIN_S
        if force or not changed:
            self._muter.mute(mute, force=changed)
            self._effects_applied = set(mute)
            if force:
                self._forced_at = now
        # 只在"该拦哪些 PID / 有哪些遮罩窗口"真的变了时才动钩子。
        # 这里以前每趟 sync（120ms）都无条件 install()+invalidate()，
        # 低级钩子的 0.35 秒状态缓存等于没有：每个按键/点击都要重算状态，
        # 而重算里包含 tracker 快照和每个遮罩一次 winId()。
        key = (frozenset(mute), frozenset(block), self._overlay_hwnds())
        if key != self._blocker_key:
            self._blocker_key = key
            self._blocker.install()
            # 遮罩窗口集合或拦截目标刚变过：让钩子立刻重新取一次状态
            self._blocker.invalidate()

    def _ppid_snapshot(self) -> dict:
        """整张进程表的 ``{pid: ppid}`` 快照，``EXPAND_MAP_TTL`` 秒内复用。

        ``psutil.Process(pid).children(recursive=True)`` 每调用一次都会重新快照一遍
        全表（实测 298 个进程 ≈ 11 ms），而 ``psutil._ppid_map()`` 就是它内部用的那张
        快照（Windows 上是 C 实现）。所以这里只取一次、自己遍历：
        同一批目标从"每个 PID 一次全表扫描"变成"一次全表扫描"。

        缓存只作用于**快照本身**（1 秒），每次调用仍按最新快照走一遍树，所以
        "新启动的子进程要被一起静音/拦截"这个语义只延迟最多 1 秒——比现有
        ``EFFECT_TTL``（2 秒）更紧。取不到 ``_ppid_map`` 时回落到 psutil 公开 API。
        """
        now = time.time()
        cached = self._ppid_cache
        if cached is not None and (now - self._ppid_cache_at) < self.EXPAND_MAP_TTL:
            return cached
        mapping: dict = {}
        try:
            import psutil

            helper = getattr(psutil, "_ppid_map", None)
            if helper is not None:
                mapping = dict(helper())
            else:                       # 兜底：psutil 没有私有快照时用公开 API
                for pid in psutil.pids():
                    try:
                        mapping[int(pid)] = int(psutil.Process(int(pid)).ppid())
                    except Exception:
                        continue
        except Exception:
            mapping = {}
        self._ppid_cache = mapping
        self._ppid_cache_at = now
        return mapping

    def _expand(self, pids: set) -> set:
        """把一批 PID 展开成"它们自己 + 所有子进程"（共用一个进程表快照）。"""
        expanded = set(int(p) for p in pids)
        if not expanded:
            return expanded
        mapping = self._ppid_snapshot()
        if not mapping:
            return expanded
        reverse: dict = {}
        for pid, ppid in mapping.items():
            reverse.setdefault(ppid, []).append(pid)
        stack = list(expanded)
        while stack:
            pid = stack.pop()
            for child in reverse.get(pid, ()):
                if child not in expanded:
                    expanded.add(child)
                    stack.append(child)
        return expanded

    @staticmethod
    def _covers_screen(rect: QRect) -> bool:
        """窗口是否铺满整块屏幕（全屏独占的启发式判断）。"""
        for screen in QGuiApplication.screens():
            dpr = float(screen.devicePixelRatio() or 1.0)
            geo = screen.geometry()
            if rect.width() >= geo.width() * dpr - 8 and rect.height() >= geo.height() * dpr - 8:
                return True
        return False

    # ------------------------------------------------------------------
    def _release_effects(self) -> None:
        self._muter.restore()
        self._blocker.uninstall()
        self._effects_base = None
        self._effects_site_base = set()
        self._effects_expanded = set()
        self._effects_applied = set()
        self._effects_mute = set()
        self._effects_block = set()
        self._effects_extras = ()
        self._effects_at = 0.0
        self._forced_at = 0.0
        self._blocker_key = None
        self._blocker.invalidate()

    def _destroy(self, program_id: int) -> None:
        for hwnd, overlay in self._overlays.pop(program_id, {}).items():
            self._missing.pop((int(program_id), int(hwnd)), None)
            overlay.set_layer_visible(False)
            overlay.unmount()
            overlay.deleteLater()

    def set_suspended(self, suspended: bool) -> None:
        """记录"正在弹对话框"这一状态（**不再隐藏遮罩**）。

        以前这里会把所有遮罩藏起来，于是点一下"临时解锁"或"关闭程序"、再取消，
        目标程序就又能正常使用了。现在遮罩始终留在目标窗口上，只有真正的解锁
        （引擎进入临时解锁状态）才会让它消失。
        """
        self._suspended = bool(suspended)

    def is_suspended(self) -> bool:
        return self._suspended

    def clear(self) -> None:
        self._timer.stop()
        self._follow_timer.stop()
        self._release_effects()
        self._effects_idle = True
        for program_id in list(self._overlays):
            self._destroy(program_id)

    def overlay_count(self) -> int:
        return sum(len(items) for items in self._overlays.values())

    def mounted_count(self) -> int:
        return sum(1 for items in self._overlays.values()
                   for overlay in items.values() if overlay.mounted)

    def muted_pids(self) -> set:
        return self._muter.muted_pids()

    def blocked_events(self) -> int:
        return self._blocker.blocked_events

    def input_blocking(self) -> bool:
        return self._blocker.is_installed()

    def shutdown(self) -> None:
        self._timer.stop()
        self._follow_timer.stop()
        try:
            self._fg_hook.stop()
        except Exception:
            pass
        self._release_effects()
        self.clear()
