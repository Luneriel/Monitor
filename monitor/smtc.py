"""系统媒体会话（SMTC）：手写 WinRT，用来“真的让网页停下来”。

为什么需要它
------------
老的暂停手段是模拟一次全局“播放/暂停”媒体键（``VK_MEDIA_PLAY_PAUSE``）。那是**切换**
语义，而且投递还依赖前台状态：网页里常常留着一条静音的 Active 音频会话，我们会以为
“还在播”而反复补按，于是把已经暂停的视频又放起来——用户看到的就是“遮罩期间视频还在
播”。SMTC 给的是**权威状态**（``PlaybackStatus``）与**幂等命令**（``TryPauseAsync``）：
按一百次也不会把视频按回来，所以可以放心地持续纠正，直到状态真的变成 Paused。

实现要点
--------
* 全程 ctypes 手写 vtable（复用 ``audio.py`` 的 ``_vt`` / ``_release`` / ``_guid``），
  不引入任何第三方依赖；接口 IID 与虚表槽位取自 WinRT 元数据
  （windows-rs 生成的 ``Windows.Media.Control`` 投影，见下面的 ``_VT_*`` 注释）。
* 所有 WinRT 调用都在自己的后台线程里做（``Watcher``）：既不和 Qt 主线程的 COM 单元
  打架，也不让 UI 线程去等异步操作。
* 任何一步失败都返回空结果 / False，由上层退回老的媒体键方案
  （见 ``control_service._sync_site_playback``）。
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Optional

from .audio import GUID, _guid, _release, _vt   # 复用同一套手写 COM 工具

S_OK = 0
S_FALSE = 1
_RPC_E_CHANGED_MODE = -2147417850       # 0x80010106：线程已经在别的单元里
_RO_INIT_MULTITHREADED = 1
_ERROR_INSUFFICIENT_BUFFER = 122
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: ``GlobalSystemMediaTransportControlsSessionPlaybackStatus``
STATUS_CLOSED = 0
STATUS_OPENED = 1
STATUS_CHANGING = 2
STATUS_STOPPED = 3
STATUS_PLAYING = 4
STATUS_PAUSED = 5
#: 处在这两个状态就继续发暂停（Changing = 网页正在缓冲/起播）
PLAYING_STATUSES = (STATUS_PLAYING, STATUS_CHANGING)
STATUS_NAMES = {
    STATUS_CLOSED: "closed", STATUS_OPENED: "opened", STATUS_CHANGING: "changing",
    STATUS_STOPPED: "stopped", STATUS_PLAYING: "playing", STATUS_PAUSED: "paused",
}

# ---------------------------------------------------------------------------
# 接口 IID（取自 WinRT 元数据）
_IID_MANAGER_STATICS = _guid("2050C4EE-11A0-57DE-AED7-C97C70338245")
#: 运行时类名（RoGetActivationFactory 用它换到静态接口）
MANAGER_CLASS = "Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager"

# ---------------------------------------------------------------------------
# 虚表槽位。WinRT 接口不是从 IUnknown 而是从 **IInspectable** 派生的：槽位 0..2 是
# IUnknown（QueryInterface/AddRef/Release），槽位 3..5 是 IInspectable
# （GetIids/GetRuntimeClassName/GetTrustLevel），**接口自己的方法从槽位 6 开始**。
# （第一版按 IUnknown 直接派生来算，于是“RequestAsync”打到了 GetIids 上：HRESULT 是
# S_OK、输出参数却被写成 IID 数量，紧接着解引用就 0xC0000005 了。）
#
#   IGlobalSystemMediaTransportControlsSessionManagerStatics:
#       6 RequestAsync
#   IGlobalSystemMediaTransportControlsSessionManager:
#       6 get_CurrentSession, 7 GetSessions, 8 add_CurrentSessionChanged,
#       9 remove_…, 10 add_SessionsChanged, 11 remove_…
#   IGlobalSystemMediaTransportControlsSession:
#       6 get_SourceAppUserModelId, 7 TryGetMediaPropertiesAsync,
#       8 GetTimelineProperties, 9 GetPlaybackInfo, 10 TryPlayAsync,
#       11 TryPauseAsync … 20 TryTogglePlayPauseAsync
#   IGlobalSystemMediaTransportControlsSessionPlaybackInfo:
#       6 get_Controls, 7 get_PlaybackStatus
#   IVectorView<T>: 6 GetAt（index 按值传，不是指针）, 7 get_Size
#   IAsyncOperation<T>: 6 put_Completed, 7 get_Completed, 8 GetResults
#       —— **实测**：这个接口自己的方法排在前面，IAsyncInfo 的五个方法并不在这张表里
#       （元数据里 IAsyncOperation 只 requires IAsyncInfo）。所以第一版按
#       “IAsyncInfo 先占 6..10”去数槽位全错了：把 get_Completed 当成 get_Status
#       读回来永远是 0，GetResults 打空，表现为“异步操作永远不完成”。
#       GetResults 在未完成时返回 0x8000000E(E_ILLEGAL_METHOD_CALL)，完成后 S_OK 并写出
#       结果 —— 直接轮询它即可，不需要 Completed 回调，也不需要消息泵（工作线程是 MTA）。
_VT_STATICS_REQUEST = 6
_VT_MANAGER_CURRENT = 6
_VT_MANAGER_SESSIONS = 7
_VT_SESSION_AUMID = 6
_VT_SESSION_PLAYBACK_INFO = 9
_VT_SESSION_TRY_PAUSE = 11
_VT_INFO_STATUS = 7
_VT_VECTOR_GET_AT = 6
_VT_VECTOR_SIZE = 7
_VT_ASYNC_PUT_COMPLETED = 6     # 没用上，留着说明布局
_VT_ASYNC_GET_COMPLETED = 7
_VT_ASYNC_RESULTS = 8
_E_ASYNC_NOT_DONE = -2147483634  # 0x8000000E：GetResults 在操作未完成时返回它

#: 异步操作等待上限：暂停命令的等待在后台线程里做，慢一点也不能卡住界面
ASYNC_WAIT_S = 0.4

_combase = None
_kernel32 = None
if hasattr(ctypes, "WinDLL"):                    # pragma: no cover - 仅 Windows
    try:
        _combase = ctypes.WinDLL("combase.dll")
        _kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    except OSError:                              # pragma: no cover
        _combase = None
        _kernel32 = None

if _combase is not None:                         # pragma: no cover - 仅 Windows
    _combase.RoInitialize.argtypes = [ctypes.c_int]
    _combase.RoInitialize.restype = ctypes.c_long
    _combase.RoGetActivationFactory.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    _combase.RoGetActivationFactory.restype = ctypes.c_long
    _combase.WindowsCreateString.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                            ctypes.POINTER(ctypes.c_void_p)]
    _combase.WindowsCreateString.restype = ctypes.c_long
    _combase.WindowsGetStringRawBuffer.argtypes = [ctypes.c_void_p,
                                                   ctypes.POINTER(ctypes.c_uint32)]
    _combase.WindowsGetStringRawBuffer.restype = ctypes.c_wchar_p
    _combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
    _combase.WindowsDeleteString.restype = ctypes.c_long

if _kernel32 is not None:                        # pragma: no cover - 仅 Windows
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetApplicationUserModelId.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p]
    _kernel32.GetApplicationUserModelId.restype = ctypes.c_long


# ---------------------------------------------------------------------------
# 基础工具
def _hresult_ok(hr: int) -> bool:
    return hr >= 0


def _hresult_text(hr: int) -> str:
    return f"0x{hr & 0xFFFFFFFF:08X}"


def _hstring(text: str):
    """建一个 WinRT HSTRING；调用方负责 ``_delete_hstring``。"""
    out = ctypes.c_void_p()
    hr = _combase.WindowsCreateString(text, len(text), ctypes.byref(out))
    if not _hresult_ok(hr):
        return None
    return out


def _hstring_value(hs) -> str:
    if not hs:
        return ""
    try:
        return _combase.WindowsGetStringRawBuffer(hs, None) or ""
    except Exception:
        return ""


def _delete_hstring(hs) -> None:
    if hs:
        try:
            _combase.WindowsDeleteString(hs)
        except Exception:
            pass


def _await_async(op, timeout: float = ASYNC_WAIT_S) -> tuple:
    """轮询 ``GetResults`` 直到异步操作结束，返回 ``(ok, 结果)``。

    未完成时 GetResults 返回 ``E_ILLEGAL_METHOD_CALL``，所以直接轮询它是安全的；
    ``ok`` 为 False 表示超时、取消或出错（结果无意义）。
    """
    out = ctypes.c_void_p(None)
    fn = _vt(op, _VT_ASYNC_RESULTS, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        out = ctypes.c_void_p(None)
        hr = fn(op, ctypes.byref(out))
        if _hresult_ok(hr):
            return True, out
        if (hr & 0xFFFFFFFF) != 0x8000000E:      # 不是“还没完成”就是真失败
            return False, out
        if time.monotonic() >= deadline:
            return False, out
        time.sleep(0.005)


def aumid_for_pid(pid: int) -> str:
    """进程的 AppUserModelID（拿不到就返回空串）。

    Chromium 系浏览器都会 ``SetCurrentProcessExplicitAppUserModelID``（任务栏分组
    用），所以这个值正好就是 SMTC 会话里的 ``SourceAppUserModelId``——用它就能把
    “被遮罩的浏览器进程”和“系统媒体会话”精确对上，不需要猜应用名。
    """
    if _kernel32 is None or not pid:
        return ""
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        length = ctypes.c_uint32(512)
        buf = ctypes.create_unicode_buffer(512)
        rc = _kernel32.GetApplicationUserModelId(handle, ctypes.byref(length), buf)
        if rc == _ERROR_INSUFFICIENT_BUFFER and 0 < length.value <= 4096:
            buf = ctypes.create_unicode_buffer(int(length.value))
            rc = _kernel32.GetApplicationUserModelId(handle, ctypes.byref(length), buf)
        if rc != 0:
            return ""
        return buf.value or ""
    except Exception:
        return ""
    finally:
        try:
            _kernel32.CloseHandle(handle)
        except Exception:
            pass


def wanted_playing(snapshot: dict, aumids, tokens=frozenset()) -> set:
    """从状态表里挑出“该暂停”的会话。

    ``snapshot`` 是 ``{AUMID: 状态}``；``aumids`` 是要精确命中的 AUMID 集合，
    ``tokens`` 是退化的子串匹配（拿不到 AUMID 时用进程名兜底）。只有**确实在播**
    的会话才会被选中——这就是“不会把暂停的视频按起来”的关键。
    """
    wanted = {str(a).lower() for a in (aumids or ()) if a}
    words = [str(t).lower() for t in (tokens or ()) if t]
    out = set()
    for aumid, status in (snapshot or {}).items():
        if status not in PLAYING_STATUSES:
            continue
        low = str(aumid).lower()
        if low in wanted or any(word in low for word in words):
            out.add(aumid)
    return out


# ---------------------------------------------------------------------------
class _Api:
    """绑定到**创建它的那个线程**的一组 SMTC 对象。"""

    def __init__(self) -> None:
        self.manager = None
        self.error = ""
        self._retry_at = 0.0

    # -- 建立 ----------------------------------------------------------
    def open(self) -> bool:
        if self.manager is not None:
            return True
        if _combase is None:
            self.error = "no combase"
            return False
        now = time.monotonic()
        if self.error and now < self._retry_at:
            return False
        try:
            hr = _combase.RoInitialize(_RO_INIT_MULTITHREADED)
            if not _hresult_ok(hr) and hr != _RPC_E_CHANGED_MODE:
                raise OSError(f"RoInitialize {_hresult_text(hr)}")
            name = _hstring(MANAGER_CLASS)
            if name is None:
                raise OSError("WindowsCreateString 失败")
            factory = ctypes.c_void_p()
            try:
                hr = _combase.RoGetActivationFactory(
                    name, ctypes.byref(_IID_MANAGER_STATICS), ctypes.byref(factory))
            finally:
                _delete_hstring(name)
            if not _hresult_ok(hr) or not factory:
                raise OSError(f"RoGetActivationFactory {_hresult_text(hr)}")
            try:
                op = ctypes.c_void_p()
                hr = _vt(factory, _VT_STATICS_REQUEST, ctypes.c_long,
                         ctypes.POINTER(ctypes.c_void_p))(factory, ctypes.byref(op))
                if not _hresult_ok(hr) or not op:
                    raise OSError(f"RequestAsync {_hresult_text(hr)}")
                try:
                    ok, manager = _await_async(op, 2.0)
                    if not ok or not manager:
                        raise OSError("RequestAsync 未完成（GetResults 超时或失败）")
                    self.manager = manager
                finally:
                    _release(op)
            finally:
                _release(factory)
        except Exception as exc:                 # 拿不到就等一会儿再试
            self.error = str(exc)
            self._retry_at = time.monotonic() + 30.0
            self.manager = None
            return False
        self.error = ""
        return True

    def close(self) -> None:
        _release(self.manager)
        self.manager = None

    # -- 读取 ----------------------------------------------------------
    def _session_aumid(self, session) -> str:
        hs = ctypes.c_void_p()
        hr = _vt(session, _VT_SESSION_AUMID, ctypes.c_long,
                 ctypes.POINTER(ctypes.c_void_p))(session, ctypes.byref(hs))
        if not _hresult_ok(hr) or not hs:
            return ""
        try:
            return _hstring_value(hs)
        finally:
            _delete_hstring(hs)

    def _session_status(self, session) -> int:
        info = ctypes.c_void_p()
        hr = _vt(session, _VT_SESSION_PLAYBACK_INFO, ctypes.c_long,
                 ctypes.POINTER(ctypes.c_void_p))(session, ctypes.byref(info))
        if not _hresult_ok(hr) or not info:
            return -1
        try:
            status = ctypes.c_int(-1)
            hr = _vt(info, _VT_INFO_STATUS, ctypes.c_long,
                     ctypes.POINTER(ctypes.c_int))(info, ctypes.byref(status))
            if not _hresult_ok(hr):
                return -1
            return int(status.value)
        finally:
            _release(info)

    def _iter_sessions(self):
        """遍历当前所有媒体会话（每次调用都要释放，避免轮询时泄漏）。"""
        vector = ctypes.c_void_p()
        hr = _vt(self.manager, _VT_MANAGER_SESSIONS, ctypes.c_long,
                 ctypes.POINTER(ctypes.c_void_p))(self.manager, ctypes.byref(vector))
        if not _hresult_ok(hr) or not vector:
            return
        try:
            size = ctypes.c_uint32(0)
            hr = _vt(vector, _VT_VECTOR_SIZE, ctypes.c_long,
                     ctypes.POINTER(ctypes.c_uint32))(vector, ctypes.byref(size))
            if not _hresult_ok(hr) or size.value > 64:   # 防御：槽位对不上时别乱跑
                return
            for index in range(int(size.value)):
                session = ctypes.c_void_p()
                hr = _vt(vector, _VT_VECTOR_GET_AT, ctypes.c_long, ctypes.c_uint32,
                         ctypes.POINTER(ctypes.c_void_p))(vector, index,
                                                          ctypes.byref(session))
                if not _hresult_ok(hr) or not session:
                    continue
                try:
                    yield session
                finally:
                    _release(session)
        finally:
            _release(vector)

    def sessions(self) -> list:
        """``[(AUMID, 状态)]``；读不到就是空列表。"""
        out = []
        try:
            for session in self._iter_sessions():
                out.append((self._session_aumid(session),
                            self._session_status(session)))
        except Exception as exc:
            self.error = str(exc)
            return out
        return out

    def current(self) -> tuple:
        session = ctypes.c_void_p()
        try:
            hr = _vt(self.manager, _VT_MANAGER_CURRENT, ctypes.c_long,
                     ctypes.POINTER(ctypes.c_void_p))(self.manager,
                                                      ctypes.byref(session))
            if not _hresult_ok(hr) or not session:
                return ("", -1)
            return (self._session_aumid(session), self._session_status(session))
        except Exception as exc:
            self.error = str(exc)
            return ("", -1)
        finally:
            _release(session)

    def try_pause(self, session) -> bool:
        """幂等暂停：已经是暂停态再按也不会有副作用。"""
        op = ctypes.c_void_p()
        hr = _vt(session, _VT_SESSION_TRY_PAUSE, ctypes.c_long,
                 ctypes.POINTER(ctypes.c_void_p))(session, ctypes.byref(op))
        if not _hresult_ok(hr) or not op:
            return False
        try:
            ok, value = _await_async(op)
            # 结果是 WinRT 布尔：GetResults 只写一个字节，高位保持初始的 0
            return bool(ok and value.value)
        finally:
            _release(op)

    # -- 轮询 ----------------------------------------------------------
    def poll(self, aumids, tokens=frozenset()) -> tuple:
        """读一遍所有会话，并把它里边“正在播且命中”的暂停掉。

        返回 ``(状态表, 发出的暂停数)``；状态表是 ``{AUMID: 状态}``。
        """
        snapshot: dict = {}
        paused = 0
        for session in self._iter_sessions():
            aumid = self._session_aumid(session)
            status = self._session_status(session)
            if aumid:
                snapshot[aumid] = status
            if status in PLAYING_STATUSES and wanted_playing({aumid: status}, aumids, tokens):
                if self.try_pause(session):
                    paused += 1
        return snapshot, paused


# ---------------------------------------------------------------------------
# 便捷函数：在**当前线程**上用（自检 / 探针用；常驻工作交给 Watcher）
_LOCAL = threading.local()


def _api() -> _Api:
    api = getattr(_LOCAL, "api", None)
    if api is None:
        api = _Api()
        _LOCAL.api = api
    return api


def reset() -> None:
    """丢掉本线程的绑定（下次调用重建）。"""
    api = getattr(_LOCAL, "api", None)
    if api is not None:
        api.close()
    _LOCAL.api = None


def available() -> bool:
    return bool(_api().open())


def last_error() -> str:
    return _api().error


def sessions() -> list:
    api = _api()
    if not api.open():
        return []
    return api.sessions()


def current() -> tuple:
    api = _api()
    if not api.open():
        return ("", -1)
    return api.current()


def pause_aumids(aumids, tokens=frozenset()) -> int:
    """对命中的会话发一次幂等暂停，返回实际按下几个。"""
    api = _api()
    if not api.open():
        return 0
    _snapshot, paused = api.poll(aumids, tokens)
    return paused


# ---------------------------------------------------------------------------
IDLE_S = 2.0        # 没被武装时的空转间隔（基本不耗 CPU）
POLL_S = 1.0        # 武装后的轮询间隔


class Watcher:
    """后台线程：按秒轮询 SMTC，把命中的浏览器会话**持续**按回暂停。

    上层只要 ``arm({浏览器进程 PID})``；进程 → AUMID 的换算（含进程名兜底）都在
    工作线程里做并缓存。任何异常都只是让这一轮失效，不会把线程搞死。
    """

    def __init__(self, interval: float = POLL_S) -> None:
        self._interval = max(0.2, float(interval))
        self._lock = threading.Lock()
        self._armed: set = set()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._polls = 0
        self._pauses = 0
        self._errors = 0
        self._last_pause = 0.0
        self._last_error = ""
        self._snapshot: dict = {}
        self._current: tuple = ("", -1)
        self._cover = False              # 最近一次轮询是否真的认出了被武装的浏览器
        self._aumids: dict = {}          # pid -> AUMID（只在工作线程里读写）

    # -- 与上层交互 ----------------------------------------------------
    def arm(self, pids) -> bool:
        """告诉它现在关心哪些浏览器进程（空集合 = 不用管了）。

        返回 True 表示走 SMTC 这条路；False 表示 SMTC 用不了（或已经确定不可用），
        上层应当退回老的媒体键方案。
        """
        wanted = {int(pid) for pid in (pids or ()) if pid}
        with self._lock:
            self._armed = wanted
        if not wanted:
            return True
        if self.usable() and self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="smtc-watch",
                                            daemon=True)
            self._thread.start()
        return self.usable()

    def usable(self) -> bool:
        """SMTC 是否可用（工作线程报告过失败就认为不可用）。"""
        if _combase is None:
            return False
        with self._lock:
            if self._last_error.startswith("open:"):
                return False
        return True

    def cover(self) -> bool:
        """最近一次轮询是否真的认出了被武装的浏览器（认出了就该由 SMTC 当权威）。"""
        with self._lock:
            return bool(self._cover)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def stats(self) -> dict:
        with self._lock:
            return {
                "armed": sorted(self._armed),
                "polls": self._polls,
                "pauses": self._pauses,
                "errors": self._errors,
                "last_error": self._last_error,
                "last_pause": self._last_pause,
                "snapshot": dict(self._snapshot),
                "current": tuple(self._current),
                "cover": bool(self._cover),
                "running": bool(self._thread and self._thread.is_alive()),
            }

    # -- 工作线程 ------------------------------------------------------
    def _resolve(self, pids) -> tuple:
        """PID → (精确 AUMID 集合, 进程名兜底 token 集合)。"""
        aumids: set = set()
        tokens: set = set()
        for pid in pids:
            if pid not in self._aumids:
                self._aumids[pid] = aumid_for_pid(pid)
            aumid = self._aumids.get(pid, "")
            if aumid:
                aumids.add(aumid)
                continue
            try:
                import psutil
                name = psutil.Process(int(pid)).name()
            except Exception:
                name = ""
            if name:
                tokens.add(name.rsplit(".", 1)[0].lower())
        return aumids, tokens

    def _run(self) -> None:
        api = _Api()
        while not self._stop.is_set():
            with self._lock:
                pids = set(self._armed)
            if not pids:
                with self._lock:
                    self._cover = False
                self._stop.wait(IDLE_S)
                continue
            if not api.open():
                with self._lock:
                    self._errors += 1
                    self._last_error = "open: " + api.error
                    self._cover = False
                self._stop.wait(IDLE_S)
                continue
            aumids, tokens = self._resolve(pids)
            if not aumids and not tokens:
                with self._lock:
                    self._cover = False
                self._stop.wait(self._interval)
                continue
            try:
                snapshot, paused = api.poll(aumids, tokens)
                cur = api.current()
            except Exception as exc:             # 单轮失败：重建绑定再来
                api.close()
                with self._lock:
                    self._errors += 1
                    self._last_error = str(exc)
                    self._cover = False
                self._stop.wait(self._interval)
                continue
            with self._lock:
                self._polls += 1
                self._pauses += int(paused)
                self._cover = True
                if paused:
                    self._last_pause = time.time()
                self._snapshot = dict(snapshot)
                self._current = tuple(cur)
            self._stop.wait(self._interval)
        api.close()
