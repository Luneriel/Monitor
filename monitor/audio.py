"""按进程静音：用 WASAPI 的 IAudioSessionManager2 找到目标进程的音频会话，
记录它的音量与静音状态，把音量降到 0%（并静音），遮罩结束后原样还原。

只用 ctypes 调 COM（不引入新依赖），任何一步失败都安静地放弃（返回 False），
不会影响遮罩本身的可用性；最后一次失败原因记录在 LAST_ERROR 里便于诊断。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from typing import Optional

if sys.platform == "win32":
    _ole32 = ctypes.windll.ole32
else:                                            # pragma: no cover - 仅 Windows
    _ole32 = None

# 最近一次失败原因（例如 CoCreateInstance 的 HRESULT），便于排查"为什么没静音"
LAST_ERROR: str = ""

# 线程本地的"这个线程已经 CoInitializeEx 过了"标记（见 _ensure_com）
_COM_LOCAL = threading.local()


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> GUID:
    """把 "8-4-4-4-12" 形式的 GUID 文本解析成 GUID 结构。

    注意：最后两段（4 位 + 12 位）合起来才是 Data4 的 8 个字节。以前只取了
    最后一段的 6 个字节、把第 4 段整个丢掉了，于是所有 CLSID / IID 都是错的
    ——CoCreateInstance 直接返回 REGDB_E_CLASSNOTREG (0x80040154)，音频会话
    永远枚举不到，"遮罩时静音"自然从来没生效过。
    """
    value = text.strip().strip("{}")
    parts = value.split("-")
    if len(parts) != 5:
        raise ValueError(f"GUID 格式不对：{text!r}")
    data4 = bytes.fromhex(parts[3] + parts[4])
    if len(data4) != 8:
        raise ValueError(f"GUID 格式不对：{text!r}")
    return GUID(int(parts[0], 16), int(parts[1], 16), int(parts[2], 16),
                (ctypes.c_ubyte * 8)(*data4))


CLSID_MMDeviceEnumerator = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioSessionManager2 = _guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
IID_ISimpleAudioVolume = _guid("87CE5498-68D6-44E5-9215-6DA47EF883D8")

CLSCTX_ALL = 0x17
E_RENDER = 0
E_CONSOLE = 0
VT_EMPTY = 0
VT_BSTR = 8

#: ``IAudioSessionControl2::GetState`` 的取值：0 = 没在出声，1 = 正在播放
SESSION_STATE_INACTIVE = 0
SESSION_STATE_ACTIVE = 1
SESSION_STATE_EXPIRED = 2


def _vt(ptr, index: int, restype, *argtypes):
    """取出 COM 接口第 index 个虚函数并设置签名。"""
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    func = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])
    return func


def _release(ptr) -> None:
    if not ptr:
        return
    try:
        _vt(ptr, 2, ctypes.c_ulong)(ptr)
    except Exception:
        pass


class _Session:
    """某个进程的音频会话（记录原始音量/静音状态，用于还原）。"""

    __slots__ = ("volume", "original_mute", "original_volume", "pid", "path")

    def __init__(self, volume, original_mute: bool, pid: int,
                 original_volume: Optional[float] = None, path: str = "") -> None:
        self.volume = volume
        self.original_mute = original_mute
        self.original_volume = original_volume
        self.pid = pid
        self.path = path        # 程序路径：同一个程序重建会话后靠它沿用原状态

    def get_volume(self) -> Optional[float]:
        try:
            # ISimpleAudioVolume: 4=GetMasterVolume(float*)
            level = ctypes.c_float(0.0)
            hr = _vt(self.volume, 4, ctypes.c_long, ctypes.POINTER(ctypes.c_float))(
                self.volume, ctypes.byref(level))
            if hr == 0:
                return float(level.value)
        except Exception:
            pass
        return None

    def set_volume(self, level: float) -> bool:
        try:
            # ISimpleAudioVolume: 3=SetMasterVolume(float, LPCGUID)
            hr = _vt(self.volume, 3, ctypes.c_long, ctypes.c_float, ctypes.c_void_p)(
                self.volume, ctypes.c_float(max(0.0, min(1.0, float(level)))), None)
            return hr == 0
        except Exception:
            return False

    def set_mute(self, mute: bool) -> bool:
        try:
            # ISimpleAudioVolume: 5=SetMute(BOOL, LPCGUID) 6=GetMute(BOOL*)
            result = _vt(self.volume, 5, ctypes.c_long, ctypes.c_int, ctypes.c_void_p)(
                self.volume, ctypes.c_int(1 if mute else 0), None)
            return result == 0
        except Exception:
            return False

    def silence(self) -> bool:
        """把该程序的音量降到 0%（同时静音），返回是否成功。"""
        ok = self.set_volume(0.0)
        return self.set_mute(True) or ok

    def restore_state(self) -> bool:
        """还原为被限制之前的音量与静音状态；返回是否**全部**还原成功。

        以前这里只返回 ``set_mute`` 的结果：音量那一步失败时也报"成功"，
        调用方于是把会话丢掉、再也不重试，用户就看到"解除限制后没有声音"。
        """
        ok = True
        if self.original_volume is not None:
            ok = self.set_volume(self.original_volume) and ok
        return self.set_mute(self.original_mute) and ok

    def release(self) -> None:
        _release(self.volume)


DEVICE_STATE_ACTIVE = 0x1
DEVICE_STATE_ALL = 0xF


def _same_interface(left, right) -> bool:
    """两个 ISimpleAudioVolume 指针是否指向同一个会话对象（用于识别"还是那个会话"）。"""
    left_value = getattr(left, "value", None)
    right_value = getattr(right, "value", None)
    if left_value is None or right_value is None:
        return False
    try:
        return int(left_value or 0) == int(right_value or 0)
    except (TypeError, ValueError):
        return False


def _activate_session_manager(device):
    """IMMDevice::Activate(IID_IAudioSessionManager2) -> IAudioSessionManager2 指针。

    参数顺序必须是 (REFIID, DWORD dwClsCtx, PROPVARIANT*, void**)。以前这里把
    REFIID 写成了 None、把 byref(IID) 当成了 dwClsCtx，Activate 直接返回
    E_POINTER，于是会话枚举永远为空、静音从来没生效过。
    """
    manager = ctypes.c_void_p()
    hr = _vt(device, 3, ctypes.c_long, ctypes.POINTER(GUID), ctypes.c_ulong,
             ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
        device, ctypes.byref(IID_IAudioSessionManager2), CLSCTX_ALL, None,
        ctypes.byref(manager))
    if hr != 0 or not manager:
        return None, f"Activate hr=0x{hr & 0xFFFFFFFF:08X}"
    return manager, ""


def _sessions_on_device(device) -> list:
    """枚举一个渲染端点上的所有音频会话。

    返回 ``[(pid, ISimpleAudioVolume_ptr, 是否静音, 音量, 会话状态), ...]``；
    状态 1 = ``SESSION_STATE_ACTIVE`` 表示这个会话当前正在出声。
    """
    global LAST_ERROR
    sessions = []
    manager, error = _activate_session_manager(device)
    if manager is None:
        LAST_ERROR = error
        return sessions
    collection = ctypes.c_void_p()
    try:
        # IAudioSessionManager2: 5=GetSessionEnumerator
        hr = _vt(manager, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
            manager, ctypes.byref(collection))
        if hr != 0 or not collection:
            LAST_ERROR = f"GetSessionEnumerator hr=0x{hr & 0xFFFFFFFF:08X}"
            return sessions
        count = ctypes.c_int()
        # IAudioSessionEnumerator: 3=GetCount
        hr = _vt(collection, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
            collection, ctypes.byref(count))
        if hr != 0:
            LAST_ERROR = f"GetCount hr=0x{hr & 0xFFFFFFFF:08X}"
            return sessions
        for index in range(count.value):
            control = ctypes.c_void_p()
            # IAudioSessionEnumerator: 4=GetSession
            hr = _vt(collection, 4, ctypes.c_long, ctypes.c_int,
                     ctypes.POINTER(ctypes.c_void_p))(collection, index,
                                                      ctypes.byref(control))
            if hr != 0 or not control:
                continue
            try:
                pid = ctypes.c_ulong()
                # IAudioSessionControl2: 14=GetProcessId
                hr = _vt(control, 14, ctypes.c_long, ctypes.POINTER(ctypes.c_ulong))(
                    control, ctypes.byref(pid))
                if hr != 0:
                    continue
                state = ctypes.c_int(0)
                # IAudioSessionControl2 继承 IAudioSessionControl：3=GetState
                # （0 = Inactive 没在出声，1 = Active 正在播放，2 = Expired）
                _vt(control, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                    control, ctypes.byref(state))
                volume = ctypes.c_void_p()
                # IUnknown::QueryInterface(IID_ISimpleAudioVolume)
                hr = _vt(control, 0, ctypes.c_long, ctypes.POINTER(GUID),
                         ctypes.POINTER(ctypes.c_void_p))(
                    control, ctypes.byref(IID_ISimpleAudioVolume), ctypes.byref(volume))
                if hr != 0 or not volume:
                    continue
                muted = ctypes.c_int(0)
                # ISimpleAudioVolume: 6=GetMute
                _vt(volume, 6, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                    volume, ctypes.byref(muted))
                level = ctypes.c_float(0.0)
                # ISimpleAudioVolume: 4=GetMasterVolume
                has_level = _vt(volume, 4, ctypes.c_long,
                                ctypes.POINTER(ctypes.c_float))(
                    volume, ctypes.byref(level)) == 0
                sessions.append((int(pid.value), volume, bool(muted.value),
                                 float(level.value) if has_level else None,
                                 int(state.value)))
            finally:
                _release(control)
    finally:
        _release(collection)
        _release(manager)
    return sessions


def _render_devices(enumerator) -> list:
    """所有活动的音频输出端点；一个都没有时退回默认端点。"""
    devices = []
    collection = ctypes.c_void_p()
    try:
        # IMMDeviceEnumerator: 3=EnumAudioEndpoints
        hr = _vt(enumerator, 3, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
                 ctypes.POINTER(ctypes.c_void_p))(
            enumerator, E_RENDER, DEVICE_STATE_ACTIVE, ctypes.byref(collection))
        if hr == 0 and collection:
            count = ctypes.c_int()
            # IMMDeviceCollection: 3=GetCount
            hr = _vt(collection, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                collection, ctypes.byref(count))
            if hr == 0:
                for index in range(count.value):
                    item = ctypes.c_void_p()
                    # IMMDeviceCollection: 4=Item
                    hr = _vt(collection, 4, ctypes.c_long, ctypes.c_int,
                             ctypes.POINTER(ctypes.c_void_p))(collection, index,
                                                              ctypes.byref(item))
                    if hr == 0 and item:
                        devices.append(item)
    finally:
        _release(collection)
    if devices:
        return devices
    # 退路：默认输出端点（EnumAudioEndpoints 失败或没有任何活动设备时）
    device = ctypes.c_void_p()
    # IMMDeviceEnumerator: 4=GetDefaultAudioEndpoint
    hr = _vt(enumerator, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int,
             ctypes.POINTER(ctypes.c_void_p))(
        enumerator, E_RENDER, E_CONSOLE, ctypes.byref(device))
    if hr == 0 and device:
        return [device]
    return []


def _ensure_com() -> None:
    """每个线程只 ``CoInitializeEx`` 一次。

    COM 的初始化计数是**按线程累加**的：每次枚举会话都调一遍，计数就一层层往上堆，
    而本程序没有"线程退出前反初始化"的时机（``CoUninitialize`` 会把同线程上还在用
    的 Qt/COM 组件一起拆掉）。所以这里只记一个线程本地开关。
    """
    if _ole32 is None or getattr(_COM_LOCAL, "ready", False):
        return
    try:
        _ole32.CoInitializeEx(None, 0x2)          # COINIT_APARTMENTTHREADED
    except Exception:
        return
    _COM_LOCAL.ready = True


def _enumerate_sessions() -> list:
    """返回 ``[(pid, ISimpleAudioVolume_ptr, 原始静音, 原始音量, 会话状态), ...]``。

    会话状态来自 ``IAudioSessionControl2::GetState``：0 = Inactive（没在出声）、
    1 = Active（正在播放）、2 = Expired。**注意 ``GetMasterVolume`` 只是音量旋钮，
    不是音量峰值**，所以"这个进程现在有没有在出声"只能看 GetState。

    **枚举所有活动的输出端点**，而不只是默认端点：程序把音频输出到别的设备
    （HDMI / 虚拟声卡 / 蓝牙耳机）时，只看默认端点会完全找不到它的会话。
    """
    global LAST_ERROR
    if _ole32 is None:
        LAST_ERROR = "ole32 不可用"
        return []
    sessions = []
    enumerator = ctypes.c_void_p()
    _ensure_com()
    try:
        hr = _ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                     ctypes.byref(IID_IMMDeviceEnumerator),
                                     ctypes.byref(enumerator))
        if hr != 0 or not enumerator:
            LAST_ERROR = f"CoCreateInstance hr=0x{hr & 0xFFFFFFFF:08X}"
            return []
        devices = _render_devices(enumerator)
        if not devices:
            LAST_ERROR = "没有活动的音频输出设备"
            return []
        for device in devices:
            try:
                sessions.extend(_sessions_on_device(device))
            finally:
                _release(device)
        if sessions:
            LAST_ERROR = ""
    except Exception as error:                        # pragma: no cover - 防御性
        LAST_ERROR = f"枚举会话异常：{error!r}"
        return sessions
    finally:
        _release(enumerator)
    return sessions


def playing_pids() -> set[int]:
    """当前**正在出声**（音频会话状态 = Active）的进程 PID 集合。

    只读：不碰任何会话的音量 / 静音状态（读到的 ``ISimpleAudioVolume`` 指针立刻释放）。
    网站限制用它来判断"现在值不值得去按一下暂停键"——盲目按播放/暂停键会把已经暂停的
    视频放起来，也会把按键打到别的应用（比如微信）身上。
    """
    result: set[int] = set()
    try:
        for pid, volume, _muted, _level, state in _enumerate_sessions():
            _release(volume)
            if int(state) == SESSION_STATE_ACTIVE:
                result.add(int(pid))
    except Exception:                                 # pragma: no cover - 防御性
        return result
    return result


class ProcessMuter:
    """把指定进程集合静音；解除时恢复它们原本的静音状态。"""

    # 目标集合没变时，重新枚举音频会话的最小间隔：COM 枚举不便宜，
    # 每秒枚举几十次会把主线程（也就是系统输入钩子所在的线程）拖住。
    ENUM_INTERVAL = 3.0

    # 音量低于这个值、而且会话带着 mute 标志 = "已经被压成静音"。
    # 这种状态**不能**当作"原始音量"：它多半就是 Monitor 自己上一次压下去的
    # （程序重建会话后会新建一个音量 0% 的会话，Windows 也会按应用记住 0%），
    # 把它当成原样还原，就会让程序解除限制之后依旧没有声音。
    SILENT_LEVEL = 0.01
    FALLBACK_VOLUME = 1.0        # 拿不准原始音量时用的兜底值（听得见、不静音）

    def __init__(self, store=None) -> None:
        # pid -> 该进程当前被静音的会话（一个进程可能有多个会话）
        self._sessions: dict[int, list] = {}
        self.available = _ole32 is not None
        self._last_wanted: Optional[set] = None
        self._last_enum = 0.0
        # 程序路径 -> [原始音量, 原始静音]。程序切换场景/重新初始化音频时会重建
        # 会话，pid 不变但会话对象换了一个；靠这份记忆才不会把新会话的 0% 当成
        # "原始音量"。``store`` 可选，提供后这份记忆还能跨 Monitor 重启存活。
        self._originals: dict[str, list] = {}
        self._store = store
        self._load_originals()

    # ------------------------------------------------------------------
    # "原始音量"记忆
    # ------------------------------------------------------------------
    def _load_originals(self) -> None:
        if self._store is None:
            return
        try:
            remembered = self._store.load_audio_originals() or {}
        except Exception:
            return
        for path, value in remembered.items():
            try:
                self._originals[str(path)] = [float(value[0]), bool(value[1])]
            except (TypeError, ValueError, IndexError):
                continue

    def _save_originals(self) -> None:
        if self._store is None:
            return
        try:
            self._store.save_audio_originals(
                {path: [float(level), bool(muted)]
                 for path, (level, muted) in self._originals.items()})
        except Exception:
            pass

    def originals(self) -> dict[str, tuple]:
        """当前记住的"被限制之前的音量"（路径 -> (音量, 是否静音)）。"""
        return {path: (float(value[0]), bool(value[1]))
                for path, value in self._originals.items()}

    def _remember(self, path: str, level: float, muted: bool) -> None:
        if not path:
            return
        self._originals[path] = [float(level), bool(muted)]
        self._save_originals()

    def _forget(self, path: str) -> None:
        if path and self._originals.pop(path, None) is not None:
            self._save_originals()

    def _path_active(self, path: str) -> bool:
        """同一个程序路径是否还有别的会话在登记中（同一个 exe 多开时别急着忘掉记忆）。"""
        if not path:
            return False
        return any(other.path == path
                   for items in self._sessions.values() for other in items)

    @staticmethod
    def _path_for_pid(pid: int) -> str:
        """进程可执行文件路径（小写）；拿不到就返回空串。"""
        try:
            import psutil
        except Exception:                                # pragma: no cover - 防御性
            return ""
        try:
            return str(psutil.Process(int(pid)).exe() or "").lower()
        except Exception:
            return ""

    @classmethod
    def looks_silenced(cls, level: Optional[float], muted: bool) -> bool:
        """这个会话状态看起来是不是"已经被压成静音"？"""
        if not muted:
            return False
        return level is None or float(level) <= cls.SILENT_LEVEL

    @classmethod
    def original_state(cls, level: Optional[float], muted: bool) -> tuple:
        """把"当前状态"翻译成可以写回去的"原始状态"。

        正常情况照抄；看起来已经被压成静音时改用 :data:`FALLBACK_VOLUME`
        （0% + 静音）——否则还原就是把 0% 又写回去，程序依旧没声音。
        """
        if cls.looks_silenced(level, muted):
            return cls.FALLBACK_VOLUME, False
        return (float(level) if level is not None else cls.FALLBACK_VOLUME, bool(muted))

    def _silenced(self, pid: int, volume) -> Optional[_Session]:
        for session in self._sessions.get(int(pid), ()):
            if _same_interface(session.volume, volume):
                return session
        return None

    def mute(self, pids: set[int], force: bool = False) -> int:
        """静音这些 PID 的音频会话，返回新增静音的会话数。

        ``force=True`` 时忽略节流，立即重新枚举（用于刚出现遮罩等关键时机）。
        每次枚举都会把已知会话**再压一次**：被限制的程序经常会把音量调回来，
        或者在切换场景时重建音频会话（新会话必须重新按到 0%）。
        新登记的会话会按 :meth:`original_state` 记录"原始音量"：看起来已经
        被压成静音的状态改用 100%/不静音兜底，绝不把 0% 当成原始音量。
        """
        global LAST_ERROR
        if not self.available:
            return 0
        wanted = set(int(p) for p in pids)
        # 已经不在限制中的进程：先还原
        for pid in list(self._sessions):
            if pid not in wanted:
                self.restore({pid})
        if not wanted:
            # 目标集合为空 = 什么都不该被静音（上面那轮已经把它们全还原了）。
            # 以前这里是 `if not pids: return 0` 直接早退，于是"遮罩撤销了、
            # 但查不到运行中的进程"时，已经压到 0% 的会话永远不会被还原。
            self._last_wanted = None
            return 0
        now = time.monotonic()
        if not force and wanted == self._last_wanted \
                and (now - self._last_enum) < self.ENUM_INTERVAL:
            return 0
        self._last_wanted = set(wanted)
        self._last_enum = now
        added = 0
        failed = 0
        for pid, volume, muted, level, _state in _enumerate_sessions():
            if pid not in wanted:
                _release(volume)
                continue
            already = self._silenced(pid, volume)
            if already is not None:
                already.silence()          # 程序可能自己把音量调回来了
                _release(volume)
                continue
            path = self._path_for_pid(pid)
            remembered = self._originals.get(path) if path else None
            if remembered is not None:
                original_volume = float(remembered[0])
                original_mute = bool(remembered[1])
            else:
                original_volume, original_mute = self.original_state(level, muted)
                self._remember(path, original_volume, original_mute)
            session = _Session(volume, original_mute, pid, original_volume, path)
            if session.silence():          # 音量降到 0% + 静音
                self._sessions.setdefault(int(pid), []).append(session)
                added += 1
            else:
                failed += 1
                session.release()
        if failed:
            LAST_ERROR = f"{failed} 个音频会话无法降到 0%（可能被程序独占）"
        return added

    def restore(self, pids: Optional[set[int]] = None) -> None:
        """还原静音状态（pids 为空则还原全部）。

        还原失败的会话**不丢弃**：留在 ``_sessions`` 里等下一次重试，
        否则一次失败的调用就会让程序永远停在 0%（用户报的另一个症状）。
        """
        global LAST_ERROR
        targets = set(self._sessions) if not pids else set(int(p) for p in pids)
        retry = 0
        for pid in list(targets):
            pending = []
            for session in self._sessions.pop(pid, []):
                try:
                    ok = bool(session.restore_state())   # 还原被限制之前的音量与静音状态
                except Exception:
                    ok = False
                if ok:
                    if not self._path_active(session.path):
                        self._forget(session.path)
                    session.release()
                else:
                    pending.append(session)
            if pending:
                retry += len(pending)
                self._sessions[int(pid)] = pending
        if retry:
            LAST_ERROR = f"{retry} 个音频会话还原失败，稍后重试"
        # 还原之后"当前生效集合"不再是已知状态，下一次 mute 必须重新枚举
        self._last_wanted = None

    def recover(self) -> int:
        """启动兜底：把记忆里"被我们压成静音、但现在已经不该静音"的程序放回去。

        Monitor 上一次没来得及还原就被关掉（崩溃 / 任务管理器结束）时，Windows
        会把这个应用的音量一直记成 0%：启动时读回记忆，发现它现在确实还是
        0% + 静音就还原成记录的原始音量，然后忘掉这条记忆。
        """
        global LAST_ERROR
        if not self._originals:
            return 0
        remembered = {path: tuple(value) for path, value in self._originals.items() if path}
        if not remembered:
            return 0
        restored = 0
        seen: set = set()
        for pid, volume, muted, level, _state in _enumerate_sessions():
            try:
                path = self._path_for_pid(pid)
                if path not in remembered:
                    continue
                seen.add(path)
                if not self.looks_silenced(level, muted):
                    continue          # 现在听起来是正常的，别去动它
                session = _Session(volume, False, pid, None, path)
                session.set_volume(remembered[path][0])
                session.set_mute(remembered[path][1])
                restored += 1
            finally:
                _release(volume)
        for path in seen:
            self._forget(path)
        if restored:
            LAST_ERROR = ""
        return restored

    def muted_pids(self) -> set[int]:
        return set(self._sessions)

    def muted_session_count(self) -> int:
        return sum(len(items) for items in self._sessions.values())
