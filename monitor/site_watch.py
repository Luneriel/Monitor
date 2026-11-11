"""网站追踪：读出"当前前台窗口正在显示哪个网址"，据此判定被追踪的网站是否在使用。

方案（与用户确认过）：Windows UI Automation。

* 只看**当前前台窗口**：用户口径是"只有当前激活标签页是该网站、并且浏览器在前台
  时才算在用"。UIA 的 Document 控件（ControlType = 50030）只给出**激活标签页**的
  文档，它的 ``Value.Value``（属性 30045）就是当前网址——窗口标题可能写着
  "和另外 4 个页面"，但 UIA 只暴露激活的那个，正好是需要的口径。
* 任何窗口都算：Chromium/Edge 用 ``Chrome_WidgetWin_1``，Electron / WebView2 / Tauri
  这类应用内置浏览器同样会暴露 Document 控件。判定规则是"网址以 http/https 开头"，
  于是 ``tauri.localhost`` / ``dsh-app://`` 这类应用自身界面会被自然排除。
* 读不到就不追踪：没有 UIA（comtypes 缺失）、窗口没有文档、或网址不是 http(s) 时，
  该网站这一秒就被认为"没在使用"，绝不猜。
* 不读"不可能有网页"的窗口：资源管理器 / 桌面 / 任务栏这类窗口读一次 UIA 要 0.8 秒
  （实测），加了网站追踪后前台一切到它们，追踪线程就会每秒白烧 0.8 秒 CPU，这也是
  "加了网站限制以后变卡"的主因之一。见 ``_NO_PAGE_CLASSES`` / ``can_host_page()``。
* 遮罩用**页面内容区**：Document 元素的 ``BoundingRectangle``（30001）就是"去掉标签栏
  与工具栏以后那块真正显示网页的地方"（实测与 Chromium 的
  ``Chrome_RenderWidgetHostHWND`` 子窗口矩形完全一致）。网站遮罩只盖这一块，
  标签栏留着不动，用户才能照常切换标签页——遮住整个浏览器窗口是 bug。
* 静音用**渲染进程**：Document 元素的 ``NativeWindowHandle``（30020）指向承载该网页
  的渲染窗口，取它的 PID 就能只压住这一个标签页的音频；取不到时退化成
  ``Chrome_RenderWidgetHostHWND`` 子窗口，再取不到才用整个窗口的 PID。
* **保活**：前台切到别的程序（点遮罩按钮、切到微信、看着画中画小窗）时，只要那个
  浏览器窗口还在、没被最小化、**当前标签页仍然是这个网站**，就继续算"在用"：遮罩、
  静音与计时都不会因为失去焦点而消失（用户实测的 bug：遮罩一失焦就没）。窗口被关掉 /
  最小化 / 换成别的标签页之后立刻不再保活。
* **画中画**：画中画小窗没有地址栏，UIA 读不到网址（``read_page`` 必然返回 ``None``），
  所以只能按 Win32 特征识别：同一个浏览器根进程 + 浏览器窗口类 + 有
  ``Chrome_RenderWidgetHostHWND`` 渲染子窗口 + 可见、没最小化、没有属主、明显比页面
  窗口小、标题与页面标题对得上（画中画标题就是视频标题，页面标题 = 视频标题 + 浏览器名）。
  识别出来的附属窗口会随页面一起交给遮罩模块，盖"紧凑遮罩"。
"""

from __future__ import annotations

import ctypes
import os
import re
import threading
import time
from ctypes import wintypes
from typing import Any, Callable, Iterable, Optional

from . import trace

# --------------------------------------------------------------------------
# UI Automation 常量（实测：ControlType=50030 是 Document，属性 30045 是 Value.Value）
# --------------------------------------------------------------------------
UIA_CONTROL_TYPE_PROPERTY = 30003
UIA_NAME_PROPERTY = 30005
UIA_NATIVE_WINDOW_HANDLE_PROPERTY = 30020
UIA_VALUE_VALUE_PROPERTY = 30045
UIA_BOUNDING_RECTANGLE_PROPERTY = 30001
UIA_CONTROL_TYPE_DOCUMENT = 50030
UIA_TREE_SCOPE_DESCENDANTS = 4

CHROME_RENDERER_CLASS = "Chrome_RenderWidgetHostHWND"

#: 这些窗口类**不可能**承载网页文档，读它们的 UIA 纯属白烧 CPU。
#:
#: 实测（本机）：`read_page()` 在浏览器窗口上约 35~75ms，但在**资源管理器**
#: （``CabinetWClass``，UIA 树有上千个元素）上要 **760~840ms**。加了网站追踪后
#: 前台只要切到资源管理器/桌面/任务栏，追踪线程就会每秒卡 0.8 秒——这正是用户
#: 反馈的"加了网站限制以后有点卡顿"的主因之一，所以这些窗口直接不读。
#: 用黑名单而不是白名单：用户要求"任何窗口都算（含 App 内置浏览器）"，
#: 白名单会把不认识的内置浏览器（CEF/Electron 之外的宿主）漏掉。
_NO_PAGE_CLASSES = frozenset((
    "CabinetWClass",            # 资源管理器（文件夹窗口）
    "ExploreWClass",            # 老式资源管理器
    "Progman", "WorkerW",       # 桌面
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd",   # 任务栏
    "TaskManagerWindow",        # 任务管理器
    "SysTreeView32", "SysListView32", "DirectUIHWND",   # shell 内部控件窗口
))

VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_KEYUP = 0x0002

#: 前台窗口被我们自己的窗口（主窗口 / 对话框）占住时，继续沿用上一次判定的时间
STICKY_S = 2.0

GW_OWNER = 4

#: 浏览器窗口类：主窗口与画中画小窗是同一个类
_BROWSER_WINDOW_CLASSES = frozenset(("Chrome_WidgetWin_1", "MozillaWindowClass"))

#: Chromium 系（Edge / Chrome / Brave / Electron / WebView2）：这类窗口下一定有
#: ``Chrome_RenderWidgetHostHWND`` 渲染子窗口，用它把对话框 / 辅助窗口筛掉。
_CHROMIUM_WINDOW_CLASSES = frozenset(("Chrome_WidgetWin_1",))

#: 附属小窗（画中画）面积最多是页面内容区面积的多少倍才算"小窗"
AUX_MAX_AREA_RATIO = 0.75

#: 两个标题的公共前缀至少要有多长才算"同一个页面"（两边都可能被截断）
AUX_TITLE_PREFIX_MIN = 12

_user32 = None
try:                                            # pragma: no cover - 仅 Windows
    _user32 = ctypes.windll.user32
except Exception:                               # pragma: no cover
    _user32 = None


# --------------------------------------------------------------------------
# 匹配规则
# --------------------------------------------------------------------------
def normalize_pattern(text: str) -> str:
    """把用户填的一行"域名 / 网址"归一化成 ``host[/path]``。

    去掉协议、``www.``、查询串、锚点与结尾斜杠，统一小写。这是**唯一**把
    ``programs.path`` 当域名（而不是文件路径）解释的归一化函数。
    """
    raw = (text or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.lstrip("/")
    raw = raw.split("#", 1)[0].split("?", 1)[0]
    if raw.startswith("www."):
        raw = raw[4:]
    raw = raw.rstrip("/")
    # 域名部分去掉认证信息与端口：split_url() 给出的 host 也不含它们，
    # 两边形式必须一致，pattern 才匹配得上。
    head, _sep, tail = raw.partition("/")
    if "@" in head:
        head = head.rsplit("@", 1)[1]
    head = head.split(":", 1)[0]
    if _sep:
        raw = f"{head}/{tail}"
    else:
        raw = head
    return raw


def display_name(pattern: str) -> str:
    """给网站取一个默认显示名（``https://www.bilibili.com/*`` → ``bilibili.com``）。"""
    norm = normalize_pattern(pattern)
    host = norm.split("/", 1)[0]
    if host.startswith("*."):
        host = host[2:]
    return host or norm


def looks_like_host(pattern: str) -> bool:
    """输入看起来是不是一个域名 / 网址。

    添加网站的弹窗用它挡掉"随手填的一行字"（例如 ``这是啥``）：允许 ``*`` 通配与路径，
    但主机部分必须含点（或就是 localhost）。
    """
    # 直接粘贴带协议的完整网址也要认（normalize 会去掉协议 / www / 查询串 / 端口）
    text = normalize_pattern(pattern)
    if not text:
        return False
    host = text.split("/", 1)[0]
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    host = host.split(":", 1)[0]
    host = host.lstrip("*.")
    if not host:
        return False
    if host == "localhost":
        return True
    if "." not in host or " " in host:
        return False
    return all(ch.isalnum() or ch in ".-_" for ch in host)


def split_url(url: str) -> tuple[str, str]:
    """网址 → (host, path)（小写、去协议 / 认证信息 / 端口 / 查询串）。"""
    text = (url or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("#", 1)[0].split("?", 1)[0]
    host, _sep, path = text.partition("/")
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    host = host.split(":", 1)[0]
    return host, path


def is_web_url(url: str) -> bool:
    """是否是"真正的网页"（http/https + 带点的域名或 localhost）。

    用它把 Electron / Tauri 应用自己的界面（``dsh-app://``、``tauri.localhost``）
    和浏览器新标签页（``edge://``）排除掉。
    """
    text = (url or "").strip().lower()
    if not text.startswith("http://") and not text.startswith("https://"):
        return False
    host, _path = split_url(text)
    return bool(host) and ("." in host or host == "localhost")


def _wildcard_match(pattern: str, text: str) -> bool:
    """``*`` 通配匹配（大小写已统一为小写）。"""
    if "*" not in pattern:
        return pattern == text
    regex = re.escape(pattern).replace(r"\*", ".*")
    try:
        return re.fullmatch(regex, text) is not None
    except re.error:
        return False


def pattern_matches(pattern: str, url: str) -> bool:
    """网址是否属于该匹配串。

    规则（README 有同样一份说明）：

    * 忽略协议与 ``www.``：``https://www.bilibili.com/x`` 与 ``bilibili.com/x`` 等价。
    * ``bilibili.com``            → 该主机（含 www）的任意路径，**不含**子域。
    * ``bilibili.com/*``          → 整站，**含**所有子域（``*.`` 也可以显式写）。
    * ``*.bilibili.com``          → 所有子域（含 www），不含裸域。
    * ``bilibili.com/video``      → 该主机下以 ``/video`` 开头的路径。
    * 任何位置都可以用 ``*`` 通配。
    """
    norm = normalize_pattern(pattern)
    if not norm:
        return False
    host, _sep, path = norm.partition("/")
    url_host, url_path = split_url(url)
    if not url_host:
        return False

    whole_site = path == "*"                       # host/* → 整站（含子域）
    candidates = {url_host}
    if url_host.startswith("www."):
        candidates.add(url_host[4:])

    host_ok = False
    for candidate in candidates:
        if "*" in host:
            if _wildcard_match(host, candidate):
                host_ok = True
        elif host == candidate:
            host_ok = True
        elif whole_site and candidate.endswith("." + host):
            host_ok = True
    if not host_ok:
        return False

    if not path or path == "*":
        return True
    if _wildcard_match(path, url_path):
        return True
    return url_path.startswith(path)


# --------------------------------------------------------------------------
# Win32 小工具（本模块自带，避免和 overlay 模块互相依赖）
# --------------------------------------------------------------------------
def foreground_window() -> int:
    """当前前台窗口 hwnd（0 = 未知）。"""
    if _user32 is None:
        return 0
    try:
        return int(_user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def window_pid(hwnd: int) -> int:
    """窗口所属进程 PID（0 = 未知）。"""
    if _user32 is None or not hwnd:
        return 0
    try:
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def window_text(hwnd: int) -> str:
    """窗口标题。"""
    if _user32 is None or not hwnd:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(512)
        _user32.GetWindowTextW(wintypes.HWND(int(hwnd)), buffer, 512)
        return buffer.value
    except Exception:
        return ""


def window_class(hwnd: int) -> str:
    """窗口类名（读不到返回空串）。"""
    if _user32 is None or not hwnd:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(wintypes.HWND(int(hwnd)), buffer, 256)
        return buffer.value
    except Exception:
        return ""


def can_host_page(hwnd: int) -> bool:
    """这个窗口值不值得去读 UIA（见 ``_NO_PAGE_CLASSES`` 的实测说明）。"""
    cls = window_class(hwnd)
    if not cls:
        return False
    return cls not in _NO_PAGE_CLASSES


def _window_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    """窗口的屏幕矩形 ``(left, top, right, bottom)``（物理像素）。"""
    if _user32 is None or not hwnd:
        return None
    try:
        rect = wintypes.RECT()
        if not _user32.GetWindowRect(wintypes.HWND(int(hwnd)), ctypes.byref(rect)):
            return None
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception:
        return None


def _child_renderer_window(hwnd: int) -> tuple[int, int]:
    """窗口下 ``Chrome_RenderWidgetHostHWND`` 子窗口的 ``(hwnd, pid)``（找不到返回 ``(0, 0)``）。"""
    if _user32 is None or not hwnd:
        return (0, 0)
    found = {"hwnd": 0, "pid": 0}
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(child, _lparam):
        try:
            buffer = ctypes.create_unicode_buffer(256)
            _user32.GetClassNameW(child, buffer, 256)
            if buffer.value == CHROME_RENDERER_CLASS:
                found["hwnd"] = int(child)
                found["pid"] = window_pid(int(child))
                return False
        except Exception:
            return True
        return True

    try:
        _user32.EnumChildWindows(wintypes.HWND(int(hwnd)), EnumProc(_callback), 0)
    except Exception:
        return (0, 0)
    return (int(found["hwnd"]), int(found["pid"]))


def _child_renderer_pid(hwnd: int) -> int:
    """窗口下 ``Chrome_RenderWidgetHostHWND`` 子窗口的 PID（找不到返回 0）。"""
    return _child_renderer_window(hwnd)[1]


def is_window_visible(hwnd: int) -> bool:
    """窗口是否可见（最小化的窗口也算"可见"，另见 ``is_iconic``）。"""
    if _user32 is None or not hwnd:
        return False
    try:
        return bool(_user32.IsWindowVisible(wintypes.HWND(int(hwnd))))
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


def window_owner(hwnd: int) -> int:
    """窗口的属主窗口 hwnd（``GW_OWNER``；0 = 没有属主）。"""
    if _user32 is None or not hwnd:
        return 0
    try:
        return int(_user32.GetWindow(wintypes.HWND(int(hwnd)), GW_OWNER) or 0)
    except Exception:
        return 0


def top_level_windows(pid: int) -> list[int]:
    """某个进程的所有顶层窗口 hwnd（含不可见的，由调用方自己过滤）。"""
    if _user32 is None or not pid:
        return []
    found: list[int] = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd, _lparam):
        try:
            if window_pid(int(hwnd)) == int(pid):
                found.append(int(hwnd))
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(EnumProc(_callback), 0)
    except Exception:
        return []
    return found


def _rect_area(rect) -> int:
    """矩形面积（``None`` 或退化矩形返回 0）。"""
    if not rect:
        return 0
    try:
        width = int(rect[2]) - int(rect[0])
        height = int(rect[3]) - int(rect[1])
    except Exception:
        return 0
    return max(0, width) * max(0, height)


def same_page_title(page_title: str, aux_title: str) -> bool:
    """两个窗口标题是不是在说"同一个页面"。

    画中画窗口的标题就是**视频标题**，而浏览器主窗口的标题是
    ``视频标题 - 个人 - Microsoft Edge``（Edge 会把页面标题当前缀），所以
    "前缀相等"是最稳的判据；两边都可能是被截断的标题，于是再补一条
    "公共前缀够长"的兜底。空标题一律不算匹配。
    """
    page = (page_title or "").strip()
    aux = (aux_title or "").strip()
    if not page or not aux:
        return False
    if page == aux or page.startswith(aux) or aux.startswith(page):
        return True
    limit = min(len(page), len(aux))
    common = 0
    while common < limit and page[common] == aux[common]:
        common += 1
    return common >= AUX_TITLE_PREFIX_MIN


def pause_media() -> bool:
    """发一次"播放 / 暂停"媒体键（VK_MEDIA_PLAY_PAUSE）。

    静音只让声音消失、画面还会继续走，所以网站被限制时再补一下暂停键。
    按用户要求，临时解锁后**不**自动恢复播放。
    """
    if _user32 is None:
        return False
    try:
        _user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
        _user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# UI Automation 读取
# --------------------------------------------------------------------------
class PageInfo:
    """前台窗口当前显示的网页。

    ``rect`` 是**页面内容区**的屏幕矩形 ``(left, top, right, bottom)``（物理像素），
    也就是"去掉标签栏与工具栏以后那块真正显示网页的地方"——网站遮罩只盖这一块，
    标签栏留着不动，用户才能照常切换标签页。``renderer_hwnd`` 是承载网页的渲染
    子窗口（Chromium 的 ``Chrome_RenderWidgetHostHWND``）。
    """

    __slots__ = ("hwnd", "url", "title", "root_pid", "renderer_pid", "rect", "renderer_hwnd")

    def __init__(self, hwnd: int, url: str, title: str, root_pid: int, renderer_pid: int,
                 rect: Optional[tuple] = None, renderer_hwnd: int = 0) -> None:
        self.hwnd = int(hwnd)
        self.url = url
        self.title = title
        self.root_pid = int(root_pid)
        self.renderer_pid = int(renderer_pid)
        self.rect = rect
        self.renderer_hwnd = int(renderer_hwnd)

    def __repr__(self) -> str:                  # pragma: no cover - 调试用
        return (f"PageInfo(hwnd={self.hwnd}, url={self.url!r}, root_pid={self.root_pid},"
                f" renderer_pid={self.renderer_pid}, rect={self.rect})")


_local = threading.local()


def _automation():
    """按**线程**缓存的 IUIAutomation（COM 单元线程模型，不能跨线程用）。"""
    cached = getattr(_local, "automation", None)
    if cached is not None:
        return cached
    if getattr(_local, "failed", False):
        return None
    try:
        import comtypes.client

        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)       # APARTMENTTHREADED
        except Exception:
            pass
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        _local.automation = comtypes.client.CreateObject(
            UIA.CUIAutomation, interface=UIA.IUIAutomation)
    except Exception:
        _local.automation = None
        _local.failed = True
    return getattr(_local, "automation", None)


def uia_available() -> bool:
    """UI Automation 是否可用（comtypes 是否装好、COM 是否能起来）。"""
    return _automation() is not None


def _property(element, prop_id: int) -> Any:
    try:
        return element.GetCurrentPropertyValue(prop_id)
    except Exception:
        return None


def _document_rect(document) -> Optional[tuple[int, int, int, int]]:
    """Document 元素的屏幕矩形（页面内容区，不含标签栏 / 工具栏）。

    实测：``UIA_BoundingRectanglePropertyId``（30001）返回 ``(left, top, width, height)``
    的**物理屏幕**坐标，而且与 Chromium 的 ``Chrome_RenderWidgetHostHWND`` 子窗口矩形
    完全一致（Edge：窗口 frame=(-7,-7,1714,920)，Document=(4,114,1699,794) →
    right=1703/bottom=908 == 子窗口矩形）。
    """
    value = _property(document, UIA_BOUNDING_RECTANGLE_PROPERTY)
    try:
        left, top, width, height = (float(item) for item in value)
    except Exception:
        return None
    if width <= 1 or height <= 1:
        return None
    return (int(round(left)), int(round(top)),
            int(round(left + width)), int(round(top + height)))


def read_page(hwnd: int) -> Optional[PageInfo]:
    """读窗口当前显示的网页；不是网页（或读不到）返回 ``None``。"""
    if not hwnd:
        return None
    if not can_host_page(hwnd):
        # 资源管理器 / 桌面 / 任务栏这类窗口没有网页文档，读一次要 0.8 秒（实测），直接跳过
        return None
    automation = _automation()
    if automation is None:
        return None
    try:
        element = automation.ElementFromHandle(wintypes.HWND(int(hwnd)))
    except Exception:
        return None
    if element is None:
        return None

    url = ""
    renderer_pid = 0
    rect: Optional[tuple] = None
    try:
        condition = automation.CreatePropertyCondition(
            UIA_CONTROL_TYPE_PROPERTY, UIA_CONTROL_TYPE_DOCUMENT)
        documents = element.FindAll(UIA_TREE_SCOPE_DESCENDANTS, condition)
    except Exception:
        documents = None
    if documents is not None:
        try:
            count = int(documents.Length)
        except Exception:
            count = 0
        for index in range(count):
            try:
                document = documents.GetElement(index)
            except Exception:
                continue
            value = _property(document, UIA_VALUE_VALUE_PROPERTY)
            if not isinstance(value, str) or not is_web_url(value):
                continue
            url = value.strip()
            native = _property(document, UIA_NATIVE_WINDOW_HANDLE_PROPERTY)
            try:
                native_hwnd = int(native or 0)
            except Exception:
                native_hwnd = 0
            renderer_pid = window_pid(native_hwnd) if native_hwnd else 0
            rect = _document_rect(document)
            break

    if not url:
        return None
    root_pid = window_pid(hwnd)
    renderer_hwnd = 0
    if not renderer_pid or renderer_pid == root_pid:
        # 内置浏览器（Electron / Tauri / WebView2 单进程）本来就同进程；
        # 多进程浏览器再退一步找渲染子窗口。
        renderer_hwnd, child_pid = _child_renderer_window(hwnd)
        renderer_pid = child_pid or root_pid
    if rect is None:
        # 退一步：Chromium 的渲染宿主子窗口矩形 == 页面内容区
        if not renderer_hwnd:
            renderer_hwnd = _child_renderer_window(hwnd)[0]
        if renderer_hwnd:
            rect = _window_rect(renderer_hwnd)
    return PageInfo(hwnd, url, window_text(hwnd), root_pid, renderer_pid, rect, renderer_hwnd)


# --------------------------------------------------------------------------
# 监视器
# --------------------------------------------------------------------------
class SiteWatcher:
    """按"前台窗口当前显示的网址"判定哪些被追踪的网站正在使用。

    只在追踪线程里调用（``scan`` 每秒一次）；所有对外取值都加了锁，UI 线程可以
    安全地 ``snapshot()`` / ``page_for()``。

    **保活**：前台切到别的程序（去点遮罩上的按钮、切到微信、盯着画中画小窗）时，
    只要那个浏览器窗口还在、没被最小化、当前标签页仍然是这个网站，就把上一次的
    判定继续算作"在用"（``held = True``）——否则遮罩、静音、计时都会在用户一转移
    焦点时集体消失。保活期间**照常计时**（用户确认过的口径）。窗口关掉 / 最小化 /
    换成别的标签页后立刻释放。
    """

    #: 离开前台后，隔多久重新读一次 UIA 来确认"那一页还开着"（秒）。读一次 UIA
    #: 约 35~80ms，所以不能每秒都读；2.5 秒足够让"切标签页 / 关窗口"及时被发现。
    HOLD_VERIFY_S = 2.5
    #: 复查时 UIA 允许连续读失败几次才放弃保活（页面正在重排 / 浏览器刚被激活时
    #: 单次 ``_read`` 返回 None 是会发生的；一次就丢会让遮罩"消失一下又回来"）
    HOLD_FAIL_MAX = 3
    #: 前台窗口读出来的网址"不是这个网站"时的确认期（秒）：切标签、页面跳转、
    #: UIA 这一拍读到中间态都会导致瞬时失配，立刻撤遮罩就是用户看到的"闪没"。
    #: 只对"同一个窗口上刚才还在用"的目标生效，超过这个时间就真的释放。
    URL_MISS_GRACE_S = 1.5

    #: 附属窗口（画中画）重新枚举的最小间隔（秒）。EnumWindows 很便宜，但没必要
    #: 每次扫描都跑。
    AUX_SCAN_S = 1.0

    def __init__(self, reader: Optional[Callable[[int], Optional[PageInfo]]] = None) -> None:
        self._reader = reader
        self._lock = threading.RLock()
        self._sites: dict[int, str] = {}          # program_id -> 归一化匹配串
        self._active: dict[int, dict] = {}        # program_id -> 页面信息（含 start/held/aux）
        self._held: dict[int, dict] = {}          # 保活中的页面（上一次命中且确认还开着）
        self._verified: dict[int, float] = {}     # program_id -> 上次确认"还开着"的时刻
        self._verify_fail: dict[int, int] = {}    # program_id -> 连续几次 UIA 读失败
        self._url_miss: dict[int, float] = {}     # program_id -> 网址开始不匹配的时刻
        self._aux: dict[int, tuple] = {}          # 页面 hwnd -> (时刻, [附属窗口, ...])
        self._pip: dict[int, dict] = {}           # 画中画 hwnd -> 归属信息（见 _track_pip）
        self._last_hwnd = 0
        self._sticky_until = 0.0
        self._last_url = ""

    # -- 配置 ----------------------------------------------------------
    def configure(self, rows: Iterable[Any]) -> None:
        """刷新被追踪的网站列表（``programs.kind = 'site'`` 的行）。"""
        sites: dict[int, str] = {}
        for row in rows:
            try:
                keys = row.keys()
                if "kind" in keys and str(row["kind"] or "program") != "site":
                    continue
                if "tracking" in keys and not int(row["tracking"] or 0):
                    continue
                sites[int(row["id"])] = str(row["path"] or "")
            except Exception:
                continue
        with self._lock:
            self._sites = sites
            for program_id in list(self._active):
                if program_id not in sites:
                    self._active.pop(program_id, None)
            for program_id in list(self._held):
                if program_id not in sites:
                    self._held.pop(program_id, None)
                    self._verified.pop(program_id, None)
                    self._verify_fail.pop(program_id, None)
            for hwnd in list(self._pip):
                if int(self._pip[hwnd].get("program_id") or 0) not in sites:
                    self._pip.pop(hwnd, None)


    def patterns(self) -> dict[int, str]:
        with self._lock:
            return dict(self._sites)

    def has_sites(self) -> bool:
        with self._lock:
            return bool(self._sites)

    # -- 扫描 ----------------------------------------------------------
    def _read(self, hwnd: int) -> Optional[PageInfo]:
        reader = self._reader or read_page
        try:
            return reader(int(hwnd))
        except Exception:
            return None

    def _page_entry(self, program_id: int, page: PageInfo, moment: float) -> dict:
        """把一次 UIA 读取的结果包成判定条目。"""
        entries, page_rect = self._aux_windows(
            int(page.hwnd), int(page.root_pid), page.title, page.rect, moment)
        self._track_pip(program_id, page, entries, moment)
        return {
            "program_id": int(program_id),
            "hwnd": int(page.hwnd),
            "url": page.url,
            "title": page.title,
            "root_pid": int(page.root_pid),
            "renderer_pid": int(page.renderer_pid),
            "renderer_hwnd": int(page.renderer_hwnd),
            # 页面内容区（不含标签栏）：网站遮罩只盖这一块，标签栏留着可点
            "rect": tuple(page_rect) if page_rect else None,
            # 扫描这一刻整个窗口的框架矩形：界面层拿它跟"现在的"框架矩形做差，
            # 拖动 / 缩放窗口时遮罩就能贴住窗口，而不用等下一次 UIA 复查。
            "frame": _window_rect(int(page.hwnd)),
            "start": moment,
            #: 这一条是"保活"来的：前台并不在它上面（用户去点别的窗口了）
            "held": False,
            #: 同一个浏览器的附属小窗（画中画）：``[{"hwnd":…, "rect":…}, …]``
            "aux": entries,
        }

    def _aux_windows(self, page_hwnd: int, root_pid: int, page_title: str,
                     page_rect, moment: float) -> tuple[list, Any]:
        """找同一个浏览器进程里"另开的小窗"（画中画）。

        画中画窗口没有地址栏，UIA 读不到网址，所以只能按 Win32 特征识别：
        同一个浏览器根进程 + 浏览器窗口类 + 有 ``Chrome_RenderWidgetHostHWND``
        渲染子窗口（Chromium 系）+ 可见、没最小化、没有属主 + 明显比页面窗口小 +
        标题与页面标题对得上（画中画标题 == 视频标题，主窗口标题 == 视频标题 +
        " - 个人 - Microsoft Edge"）。

        返回 ``(附属窗口列表, 页面内容区矩形)``；矩形可能是从渲染子窗口退一步读来的。
        """
        key = int(page_hwnd)
        rect = page_rect
        cached = self._aux.get(key)
        if cached is not None and (moment - cached[0]) < self.AUX_SCAN_S:
            return ([dict(item) for item in cached[1]], rect)

        found: list = []
        page_area = _rect_area(rect)
        for hwnd in top_level_windows(int(root_pid)):
            if hwnd == key:
                continue
            cls = window_class(hwnd)
            if cls not in _BROWSER_WINDOW_CLASSES:
                continue
            if window_owner(hwnd) or not is_window_visible(hwnd) or is_iconic(hwnd):
                continue
            aux_rect = _window_rect(hwnd)
            if aux_rect is None:
                continue
            if page_area and _rect_area(aux_rect) > page_area * AUX_MAX_AREA_RATIO:
                continue        # 主窗口 / 另一个全尺寸窗口，不是画中画
            if cls in _CHROMIUM_WINDOW_CLASSES and not _child_renderer_window(hwnd)[0]:
                continue        # 没有渲染子窗口 = 对话框 / 辅助窗口
            if not same_page_title(page_title, window_text(hwnd)):
                continue
            if rect is None and page_area == 0:
                # 页面内容区没读到（有些内置浏览器没有文档控件）时，顺手用主窗口的
                # 渲染子窗口矩形兜底，免得附属窗口的面积比较全失效。
                child = _child_renderer_window(key)[0]
                if child:
                    rect = _window_rect(child)
                    page_area = _rect_area(rect)
            found.append({"hwnd": int(hwnd), "rect": tuple(int(v) for v in aux_rect)})
        found.sort(key=lambda item: item["hwnd"])
        self._aux[key] = (moment, [dict(item) for item in found])
        return (found, rect)

    def _track_pip(self, program_id: int, page: PageInfo, entries: list,
                   moment: float) -> None:
        """登记本次命中页面上的附属小窗（画中画）。

        记下来之后，用户切到别的标签页（当前网址不再是限制域名）时，只要这扇小窗还
        开着、标题还对得上，就继续把它当成该目标**唯一**的遮挡对象——画中画里放的还
        是被限制网站的视频，而它没有地址栏，UIA 读不出网址，只能靠这份登记续上。
        """
        if not entries:
            return
        with self._lock:
            for item in entries:
                hwnd = int(item.get("hwnd") or 0)
                if not hwnd:
                    continue
                previous = self._pip.get(hwnd) or {}
                self._pip[hwnd] = {
                    "program_id": int(program_id),
                    "page_hwnd": int(page.hwnd),
                    "url": page.url,
                    "title": page.title,
                    "root_pid": int(page.root_pid),
                    # 声音是画中画自己的渲染进程在放：静音只压它一个
                    "renderer_pid": _child_renderer_pid(hwnd),
                    "renderer_hwnd": _child_renderer_window(hwnd)[0],
                    "rect": tuple(int(v) for v in item.get("rect") or ()),
                    "start": float(previous.get("start") or moment),
                    "moment": float(moment),
                }

    def _pip_sweep(self, moment: float, sites: dict[int, str],
                   matches: dict[int, dict]) -> None:
        """画中画存活复查：还开着就继续遮挡，当前标签页是不是限制域名都不影响。

        登记由 ``_track_pip`` 在命中限制网站时做；这里每扫一拍复查一次：小窗关掉 /
        最小化 / 换了视频（标题对不上）才释放。释放后不再遮挡。
        只有"别的路径都没给出条目"时才补一条**只遮画中画**的条目：这时当前标签页
        很可能是一个正常网页，页面区不该被盖上（用户要的是画中画继续被挡）。
        """
        with self._lock:
            registry = {int(key): dict(value) for key, value in self._pip.items()}
        if not registry:
            return
        alive: dict[int, list] = {}
        info_for: dict[int, dict] = {}
        for hwnd, info in registry.items():
            program_id = int(info.get("program_id") or 0)
            if program_id not in sites:
                with self._lock:
                    self._pip.pop(hwnd, None)
                continue
            if not is_window_visible(hwnd) or is_iconic(hwnd):
                with self._lock:
                    self._pip.pop(hwnd, None)
                trace.log("site", f"目标 {program_id} 画中画 {hwnd} 已关闭 / 最小化，释放")
                continue
            rect = _window_rect(hwnd)
            if rect is None:
                with self._lock:
                    self._pip.pop(hwnd, None)
                continue
            title = window_text(hwnd)
            if title and not same_page_title(str(info.get("title") or ""), title):
                with self._lock:
                    self._pip.pop(hwnd, None)
                trace.log("site", f"目标 {program_id} 画中画 {hwnd} 已换内容（{title}），释放")
                continue
            rect = tuple(int(v) for v in rect)
            alive.setdefault(program_id, []).append({"hwnd": int(hwnd), "rect": rect})
            info_for[program_id] = info
            with self._lock:
                current = self._pip.get(hwnd)
                if current is not None:
                    current["rect"] = rect
                    current["moment"] = moment
            entry = matches.get(program_id)
            if entry is not None:
                # 保活 / 网址确认期里的条目可能带着旧的附属小窗列表，这里补上这一扇
                aux = [dict(item) for item in entry.get("aux") or ()]
                if not any(int(item.get("hwnd") or 0) == int(hwnd) for item in aux):
                    aux.append({"hwnd": int(hwnd), "rect": rect})
                    entry["aux"] = aux
        for program_id, pips in alive.items():
            if program_id in matches:
                continue
            info = info_for.get(program_id) or {}
            page_hwnd = int(info.get("page_hwnd") or 0)
            if not page_hwnd or not is_window_visible(page_hwnd):
                continue
            matches[program_id] = {
                "program_id": int(program_id),
                "hwnd": page_hwnd,
                "url": str(info.get("url") or ""),
                "title": str(info.get("title") or ""),
                "root_pid": int(info.get("root_pid") or 0),
                "renderer_pid": int(info.get("renderer_pid") or 0),
                "renderer_hwnd": int(info.get("renderer_hwnd") or 0),
                # 当前标签页不是被限制的网站：页面区保持可用，只遮画中画
                "rect": None,
                "frame": None,
                "start": float(info.get("start") or moment),
                "held": True,
                #: 只遮附属小窗的条目：界面层据此不生成"页面区"那块遮罩
                "pip_only": True,
                "aux": pips,
            }
            trace.log("site", f"目标 {program_id} 画中画 {pips[0]['hwnd']} 仍在，"
                              f"当前标签页不是限制域名：只遮画中画")

    def _held_matches(self, moment: float, sites: dict[int, str]) -> dict[int, dict]:
        """前台不是网页时，确认上一条命中是否还"开着"并继续算在用。"""
        out: dict[int, dict] = {}
        stale: list[int] = []
        with self._lock:
            held = {int(key): dict(value) for key, value in self._held.items()}
        for program_id, previous in held.items():
            pattern = sites.get(int(program_id))
            if not pattern:
                stale.append(int(program_id))
                continue
            hwnd = int(previous.get("hwnd") or 0)
            if not hwnd or not is_window_visible(hwnd) or is_iconic(hwnd):
                # 窗口被关掉 / 最小化了：立刻不再保活（最小化时遮罩本来就该消失）
                trace.log("site", f"目标 {program_id} 保活释放：窗口已关闭 / 最小化 / 不可见")
                stale.append(int(program_id))
                continue
            if (moment - self._verified.get(int(program_id), 0.0)) >= self.HOLD_VERIFY_S:
                # 隔几秒真的读一次：确认那个窗口的当前标签页还是这个网站
                self._verified[int(program_id)] = moment
                page = self._read(hwnd)
                if page is None:
                    # UIA 偶尔读不出页面（页面在重排 / 浏览器刚被切到前台）。
                    # 连续几次都读不到才认为"真的不在这个网站了"。
                    fails = self._verify_fail.get(int(program_id), 0) + 1
                    self._verify_fail[int(program_id)] = fails
                    trace.log("site", f"目标 {program_id} 保活复查：UIA 读不到页面"
                                      f"（第 {fails}/{self.HOLD_FAIL_MAX} 次）")
                    if fails >= self.HOLD_FAIL_MAX:
                        stale.append(int(program_id))
                        continue
                else:
                    self._verify_fail.pop(int(program_id), None)
                    if not pattern_matches(pattern, page.url):
                        trace.log("site", f"目标 {program_id} 保活释放："
                                          f"当前网址已是 {page.url}")
                        stale.append(int(program_id))
                        continue
                    entry = self._page_entry(int(program_id), page, moment)
                    entry["held"] = True
                    entry["start"] = previous.get("start", moment)
                    out[int(program_id)] = entry
                    continue
            # 还没到复查时间：沿用上一条，只刷新一下附属窗口缓存
            entry = dict(previous)
            entry["held"] = True
            entry["start"] = previous.get("start", moment)
            entry["frame"] = _window_rect(hwnd)
            entries, _rect = self._aux_windows(
                hwnd, int(previous.get("root_pid") or 0), previous.get("title") or "",
                previous.get("rect"), moment)
            entry["aux"] = entries
            out[int(program_id)] = entry
        if stale:
            with self._lock:
                for program_id in stale:
                    self._held.pop(int(program_id), None)
                    self._verified.pop(int(program_id), None)
                    self._verify_fail.pop(int(program_id), None)
        return out

    def scan(self, now: Optional[float] = None) -> dict[int, dict]:
        """扫一次前台窗口，返回 ``{program_id: 页面信息}``（含保活中的页面）。"""
        moment = time.time() if now is None else float(now)
        with self._lock:
            sites = dict(self._sites)
        if not sites:
            with self._lock:
                self._active = {}
                self._held = {}
                self._verified = {}
                self._verify_fail = {}
                self._url_miss = {}
                self._pip = {}
            return {}

        hwnd = foreground_window()
        if hwnd and window_pid(hwnd) == os.getpid():
            # 前台是我们自己的窗口（主界面 / 密码框）：片刻沿用上一次的判定，
            # 免得用户去点遮罩上的按钮时把网站判定抖掉。
            with self._lock:
                sticky = moment < self._sticky_until
                last = self._last_hwnd
            hwnd = last if (sticky and last) else 0

        page = self._read(hwnd) if hwnd else None
        if page is not None:
            if hwnd != self._last_hwnd or page.url != self._last_url:
                trace.log("site", f"前台页面 hwnd={int(page.hwnd)} url={page.url}")
            matches: dict[int, dict] = {}
            for program_id, pattern in sites.items():
                key = int(program_id)
                if pattern_matches(pattern, page.url):
                    self._url_miss.pop(key, None)
                    matches[key] = self._page_entry(key, page, moment)
                    continue
                # 读出来的网址"不是这个网站"：先别急着撤遮罩。切标签、页面跳转、UIA
                # 拿到中间态都会让某一拍读出不匹配的网址，一撤就是"闪没"。同一个窗口上
                # 刚才还在用的目标给 URL_MISS_GRACE_S 的确认期，超过才真的释放。
                since = self._url_miss.get(key)
                if since is None:
                    self._url_miss[key] = since = moment
                previous = self._active.get(key) or self._held.get(key)
                same_window = bool(previous) and \
                    int(previous.get("hwnd") or 0) == int(page.hwnd)
                if same_window and (moment - since) < self.URL_MISS_GRACE_S:
                    entry = dict(previous)
                    entry["start"] = previous.get("start", moment)
                    entry["frame"] = _window_rect(int(page.hwnd))
                    matches[key] = entry
                    trace.log("site", f"目标 {key} 这一拍网址不匹配（{page.url}），"
                                      f"确认期内继续遮挡")
                elif same_window:
                    trace.log("site", f"目标 {key} 网址不匹配已超过 "
                                      f"{self.URL_MISS_GRACE_S}s（{page.url}），释放")
        else:
            # 前台不是网页（切到别的程序 / 点到画中画小窗）。上一次命中的页面如果
            # 还开着就继续算在用——这是"遮罩一失焦就消失"的修复。
            matches = self._held_matches(moment, sites)

        # 画中画存活复查（②）：当前标签页即使已经切到别的网站，只要画中画还开着，
        # 就把它继续挂在原目标名下遮挡。已经有条目的目标只补小窗，不重复生成。
        self._pip_sweep(moment, sites, matches)

        with self._lock:
            held: dict[int, dict] = {}
            for program_id, entry in matches.items():
                previous = self._active.get(program_id)
                if previous and previous.get("hwnd") == entry["hwnd"] \
                        and previous.get("url") == entry["url"]:
                    # 同一个标签页连续使用：起始时间要接着上一次，别每秒重开一条日志
                    entry["start"] = previous.get("start", entry["start"])
                held[int(program_id)] = dict(entry)
            self._active = matches
            # 记住本次命中的页面（刚保活回来的也算）：下一次前台不再是网页时，靠它们
            # 去复查"那个窗口是不是还开着、当前标签页还是不是这个网站"，再决定继续保活
            # 还是立刻释放。（只记"已经保活"的条目会让保活永远起不来。）
            self._held = held
            # 附属窗口缓存只留还活着的页面，免得跨天越攒越多
            keep = {int(entry.get("hwnd") or 0) for entry in matches.values()}
            for key in list(self._aux):
                if key not in keep:
                    self._aux.pop(key, None)
            # 已经不再被限制的目标不留状态残留
            for key in list(self._url_miss):
                if key not in sites:
                    self._url_miss.pop(key, None)
            if page is not None:
                self._last_hwnd = int(page.hwnd)
                self._sticky_until = moment + STICKY_S
                self._last_url = page.url
        return matches


    # -- 取值 ----------------------------------------------------------
    def snapshot(self) -> dict[int, dict]:
        with self._lock:
            return {program_id: dict(entry) for program_id, entry in self._active.items()}

    def active_ids(self) -> set[int]:
        with self._lock:
            return set(self._active)

    def page_for(self, program_id: int) -> Optional[dict]:
        with self._lock:
            entry = self._active.get(int(program_id))
            return dict(entry) if entry else None

    def current_url(self) -> str:
        with self._lock:
            return self._last_url

    def clear(self) -> None:
        with self._lock:
            self._active = {}
            self._held = {}
            self._verified = {}
            self._verify_fail = {}
            self._url_miss = {}
            self._aux = {}
            self._pip = {}
            self._last_hwnd = 0
            self._sticky_until = 0.0
            self._last_url = ""
