"""自检模式：真实启动界面、走一遍主要交互，并输出各页面截图。

    python main.py --selftest --shots data/shots --db data/selftest.db

**必须显式指定独立的自检库**：自检会先删除再重建它打开的数据库，所以不指定
`--db`（或指向真实库 data/monitor.db）时会直接拒绝启动，绝不碰用户的数据。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Optional

import psutil
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPixmap, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QSpinBox, QApplication, QDialog, QFrame, QLabel, QScrollBar, QVBoxLayout,
                               QWidget, QPushButton)

from . import autostart, config, icon_extract, overlay, site_watch, util
from .app import MonitorApp, initial_window_geometry
from .control import Rule
from .database import Database, normalize_path
from .i18n import tr
from .notify import (CHANNEL_WECOM, CHANNEL_WINDOWS, Notifier, normalize, render_template)
from .ui import anim, logs_page, main_window
from .ui.controls import CheckBox
from .ui.dialogs import AddProgramsDialog, ConfirmDeleteDialog

SAMPLE_EXES = [
    r"C:\Windows\System32\notepad.exe",
    r"C:\Windows\System32\calc.exe",
    r"C:\Windows\System32\mspaint.exe",
    r"C:\Windows\System32\cmd.exe",
    r"C:\Windows\System32\charmap.exe",
    r"C:\Windows\System32\write.exe",
]

# 用于"添加弹窗"演示的候选程序
CANDIDATE_EXES = [
    r"C:\Windows\System32\osk.exe",
    r"C:\Windows\System32\magnify.exe",
    r"C:\Windows\System32\cleanmgr.exe",
    r"C:\Windows\System32\dxdiag.exe",
    r"C:\Windows\System32\notepad.exe",
    r"C:\Windows\System32\mspaint.exe",
    r"C:\Windows\System32\explorer.exe",
    r"C:\Windows\System32\regedit.exe",
]


# 自检用的独立数据库文件名（放在真实库同目录）
SELFTEST_DB_NAME = "selftest.db"


def real_db_conflict(db_path: str = "") -> bool:
    """自检目标库是否落在真实库上（含"没有显式指定 --db"）。

    自检启动时会先删掉再重建它打开的数据库（见 app.main），所以绝不能让自检打开
    用户平时用的 data/monitor.db —— 一旦误开，用户的程序、规则、密码都会被清空。
    """
    if not str(db_path or "").strip():
        return True
    target = os.path.normcase(os.path.abspath(str(db_path)))
    real = os.path.normcase(os.path.abspath(config.DB_PATH))
    return target == real


def selftest_refuse_text(db_path: str = "") -> str:
    """拒绝在真实库上自检时打印的提示（含可以直接抄的命令）。"""
    real = os.path.abspath(config.DB_PATH)
    real_dir = os.path.dirname(real)
    target = (os.path.abspath(str(db_path)) if str(db_path or "").strip()
              else "（未指定 --db，默认就是真实库）")
    shots = os.path.join(real_dir, "shots")
    suggested = os.path.join(real_dir, SELFTEST_DB_NAME)
    return "\n".join([
        "[selftest] 拒绝启动：自检会先删除再重建它打开的数据库，因此绝不能指向真实库。",
        f"[selftest] 真实库  : {real}",
        f"[selftest] 本次目标: {target}",
        "[selftest] 请显式指定独立的自检库，例如：",
        f'[selftest]   python main.py --selftest --shots "{shots}" --db "{suggested}"',
    ])


def _pump(app: QApplication, seconds: float = 0.35) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)


def _label_text(label) -> str:
    """取标签的完整文本：省略标签（_ElidedLabel）返回未省略的原文本。"""
    getter = getattr(label, "full_text", None)
    if callable(getter):
        try:
            return str(getter())
        except RuntimeError:
            return ""
    return label.text()


def _wait_until(predicate, app: QApplication, timeout: float = 4.0) -> bool:
    """推进事件循环直到条件成立（用于等待动画结束，避免用固定时长造成的偶发失败）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
        try:
            if predicate():
                return True
        except RuntimeError:
            return True
    return False


def _sample_y(app: QApplication, target, probes: dict, seconds: float = 0.8) -> dict:
    """动画期间采样各探针控件相对 target 的 y 坐标，返回 {名字: [y, ...]}。

    用来验证展开/收起动画期间"文字不上下跳"：某探针的位移波动 = max(y) - min(y)。
    探针用控件名而不是行号，失败信息才能直接指出是哪一行的文字在动。
    """
    samples: dict = {name: [] for name in probes}
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        for name, probe in probes.items():
            try:
                samples[name].append(probe.mapTo(target, QPoint(0, 0)).y())
            except RuntimeError:      # 控件已被重建
                pass
        time.sleep(0.015)
    return samples


def _trace(app: QApplication, target, probes: dict, seconds: float = 0.8) -> dict:
    """和 `_sample_y` 一样采样，但同时记录控件高度：定位"谁在动"时用。

    返回 ``{名字: [(y, 高度), ...]}``。
    """
    out: dict = {name: [] for name in probes}
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        for name, probe in probes.items():
            try:
                out[name].append((probe.mapTo(target, QPoint(0, 0)).y(), probe.height()))
            except RuntimeError:      # 控件已被重建
                pass
        time.sleep(0.015)
    return out


def _ys(samples: dict, name: str) -> list:
    """把 `_trace`（或 `_sample_y`）的结果化成纯 y 列表。"""
    values = samples.get(name) or []
    return [int(item[0]) if isinstance(item, (tuple, list)) else int(item)
            for item in values]


def _jitter(samples: dict, name: str) -> int:
    """某个探针的位移波动（max-min）；一次都没采到返回 99（视为失败）。"""
    values = _ys(samples, name)
    return (max(values) - min(values)) if values else 99


def _oscillation(samples: dict, name: str) -> tuple:
    """某个探针的 (上行次数, 折返次数)，用来看位移是不是"上下跳"。

    展开方向的正常位移是**单向向下**（内容被一层层推下去），所以上行 = 抖动；
    下行起点那一次布局落定（从折叠态的退化几何变成真实几何）不算抖动，
    因此允许最多一次方向折返。一次都没采到返回 (-1, -1)。
    """
    values = [int(value) for value in _ys(samples, name)]
    if len(values) < 2:
        return -1, -1
    ups = sum(1 for before, after in zip(values, values[1:]) if after < before - 1)
    turns = 0
    direction = 0
    for before, after in zip(values, values[1:]):
        step = 1 if after > before else (-1 if after < before else 0)
        if not step:
            continue
        if direction and step != direction:
            turns += 1
        direction = step
    return ups, turns


def _shot(widget, path: str) -> None:
    """截图：把（可能透明的）窗口合成到中性底色上，便于观察圆角是否干净。"""
    widget.repaint()
    app = QApplication.instance()
    if app is not None:
        app.processEvents()
    pixmap = widget.grab()
    canvas = QPixmap(pixmap.size())
    canvas.setDevicePixelRatio(pixmap.devicePixelRatio() or 1.0)
    canvas.fill(QColor("#AEB8C8"))
    painter = QPainter(canvas)
    painter.drawPixmap(0, 0, pixmap)
    painter.end()
    canvas.save(path, "PNG")


def _seed(db: Database) -> list[str]:
    existing = [p for p in SAMPLE_EXES if os.path.exists(p)]
    for path in existing:
        icon_path = icon_extract.extract_icon(path, 64)
        db.add_program(path, icon_extract.file_display_name(path), icon_path, tracking=True)

    added = list(db.programs(visible_only=True))
    # 故意留一个超长名称，用于验证卡片 / 弹窗的省略与右侧控件不被挤出可视区
    if len(added) >= 2:
        db.rename_program(int(added[1]["id"]), "Microsoft DirectX 诊断工具（超长名称显示测试）")
        added = list(db.programs(visible_only=True))
    # 造一些历史日志（昨天 / 前天）
    now = time.time()
    day = 86400.0
    for index, program in enumerate(added):
        for offset in (1, 2):
            start = now - offset * day - index * 900
            end = start + 1800 + index * 300
            log_id = db.open_log(int(program["id"]), 9000 + index, start)
            db.close_log(log_id, end)
        db.touch_last_run(int(program["id"]), now - day)

    # 一个"删除但保留日志"的程序（用于日志页下拉菜单）
    ghost = r"C:\Windows\System32\notepad.exe"
    ghost_id = db.add_program(ghost + ".ghost", "已删除的程序示例",
                              icon_extract.default_app_icon_path(), tracking=False)
    if ghost_id:
        log_id = db.open_log(ghost_id, 4242, now - 3 * day)
        db.close_log(log_id, now - 3 * day + 4200)
        db.remove_programs([ghost_id], keep_logs=True)
    return existing


# 自检里的遮罩检查会把 overlay.visible_windows 换成桩并且不还原；收尾清理窗口
# 之前必须换回真正的实现，否则看不到任何窗口，别名程序的窗口就漏掉了。
_REAL_VISIBLE_WINDOWS = overlay.visible_windows

# 自检启动的示例程序里，计算器与新版"记事本"其实是应用商店别名：calc.exe /
# notepad.exe 只是个激活器，启动它的进程立刻退出，真正带窗口的是
# CalculatorApp.exe / 另一个 Notepad 进程 —— 对 launched 调 terminate() 关不掉
# 这些窗口，自检跑完会留下一堆窗口，所以要按"窗口属于哪个进程"来收尾。
_SAMPLE_PROCESS_NAMES = {
    "notepad.exe", "notepad",
    "calc.exe", "calculator.exe", "calculatorapp.exe", "windowscalculator.exe",
    "mspaint.exe", "paint.exe", "paintapp.exe",
    "cmd.exe", "conhost.exe", "charmap.exe", "write.exe", "wordpad.exe",
}

# 商店应用（计算器 / 新版记事本 / 画图）的顶层窗口常常属于宿主
# ApplicationFrameHost.exe，真正的应用进程（CalculatorApp.exe 等）是它的子进程。
# 这些宿主窗口也必须算进"示例程序窗口"，否则上下的差集永远是空集——自检会假 PASS，
# 计算器窗口就一直留在桌面上（用户反馈的 bug）。这里只用来**找窗口**，
# 结束进程时对宿主另有照顾（见 _close_sample_windows）。
_SAMPLE_HOST_NAMES = {
    "applicationframehost.exe",
}

_SAMPLE_WINDOW_NAMES = _SAMPLE_PROCESS_NAMES | _SAMPLE_HOST_NAMES


def _sample_window_pids() -> set[int]:
    """名字像示例程序（含应用商店别名进程与 UWP 宿主）的 PID 集合。"""
    pids: set[int] = set()
    for process in psutil.process_iter(["name"]):
        try:
            name = str(process.info.get("name") or "").lower()
        except Exception:
            continue
        if name in _SAMPLE_WINDOW_NAMES:
            pids.add(int(process.pid))
    return pids


def _sample_windows() -> set[int]:
    """当前属于示例程序的顶层窗口句柄集合（自检前后对比用）。"""
    try:
        visible = overlay.visible_windows(_sample_window_pids())
    except Exception:
        return set()
    return {hwnd for items in visible.values() for hwnd, _rect, _iconic in items}


def _new_window_pids(before: set[int]) -> set[int]:
    """自检期间新出现的示例程序窗口属于哪些进程。"""
    pids: set[int] = set()
    try:
        visible = overlay.visible_windows(_sample_window_pids())
    except Exception:
        return pids
    for pid, items in visible.items():
        for hwnd, _rect, _iconic in items:
            if hwnd not in before:
                pids.add(int(pid))
    return pids


def _kill_pid(pid: int) -> None:
    """结束进程（连带子进程）；失败就忽略——收尾清理不该影响自检结果。"""
    try:
        process = psutil.Process(pid)
    except Exception:
        return
    try:
        children = process.children(recursive=True)
    except Exception:
        children = []
    for child in children:
        try:
            child.kill()
        except Exception:
            pass
    try:
        process.kill()
    except Exception:
        pass


def _process_name(pid: int) -> str:
    try:
        return str(psutil.Process(int(pid)).name() or "").lower()
    except Exception:
        return ""


def _close_sample_windows(before: set[int], launched: list) -> int:
    """收掉自检启动的示例程序窗口（记事本 / 计算器 / 画图 / cmd）。

    ``before`` 是启动示例程序之前的窗口句柄集合：用户自己先开着的记事本、
    计算器不在里面，永远不会被误关。

    商店应用（计算器）的窗口属于宿主 ApplicationFrameHost.exe，而 terminate()
    只能结束激活器进程，所以这里按"窗口属于哪个进程"反复收，直到连续两次
    看不到新窗口为止（UWP 的窗口要 1~2 秒才出来，不能第一次看不到就收工）。
    """
    pids: set[int] = set()
    for process in launched:
        try:
            pids.add(int(process.pid))
        except Exception:
            continue
    for pid in list(pids):
        try:
            pids.update(int(child.pid) for child in psutil.Process(pid).children(recursive=True))
        except Exception:
            pass
    empty = 0
    started = time.time()
    while True:
        fresh = _new_window_pids(before)
        targets = set(pids) | set(fresh)
        # 商店应用的窗口挂在宿主上：真正要结束的是它的示例子进程。误杀宿主会
        # 连带关掉用户自己的其它 UWP 窗口，所以能定位到示例子进程时就不动宿主。
        for pid in list(targets):
            try:
                children = psutil.Process(pid).children(recursive=True)
            except Exception:
                children = []
            hits = [c for c in children if _process_name(int(c.pid)) in _SAMPLE_PROCESS_NAMES]
            if not hits:
                continue
            targets.update(int(c.pid) for c in hits)
            if _process_name(pid) in _SAMPLE_HOST_NAMES:
                targets.discard(int(pid))
        for pid in sorted(targets):
            _kill_pid(pid)
        pids |= targets
        if not fresh:
            empty += 1
            if empty >= 2 and (time.time() - started) >= 3.0:
                break
        else:
            empty = 0
        if (time.time() - started) >= 20.0:
            break
        time.sleep(0.4)
    return len(pids)


def _launch_samples(paths: list[str]) -> list[subprocess.Popen]:
    """启动示例进程；对 cmd.exe 故意启动 3 个实例，用于验证"同一程序只记一条日志"。"""
    processes = []
    for path in paths[:3]:
        try:
            processes.append(subprocess.Popen([path], stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL))
        except Exception:
            pass
    multi = r"C:\Windows\System32\cmd.exe"
    if os.path.exists(multi):
        for _ in range(3):
            try:
                processes.append(subprocess.Popen([multi, "/k", "timeout /t 60"],
                                                  stdout=subprocess.DEVNULL,
                                                  stderr=subprocess.DEVNULL))
            except Exception:
                pass
    return processes


def _source_of(target) -> str:
    """取一个函数/模块的源码文本；取不到（打包成 exe）时返回空串。

    少数校验是"读源码断言"（例如"关闭总开关的分支里不许再出现密码框"、
    "退出闸门只在某几处被调用"）。这类断言在冻结环境里没有 .py 文件可用，
    只能跳过：调用方用 ``not source`` 判断，打印里也会标出"跳过"，
    不会假装通过。
    """
    import inspect as _inspect_mod

    try:
        return _inspect_mod.getsource(target)
    except (OSError, TypeError):
        return ""


def _soften_console() -> None:
    """把标准流的编码错误策略改成 replace。

    自检日志里有中文与少量符号（例如 "−"），当控制台是 GBK 代码页（管道 / 后台
    运行时 stdout 会退化成 ANSI 代码页）时 print 会抛 UnicodeEncodeError，把整轮
    自检打断。这里改成"打不出来的字符变 ?"，保证一行日志不会毁掉整次运行。
    """
    import sys as _sys

    for stream in (_sys.stdout, _sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def run_selftest(app: QApplication, db: Database, shots_dir: str = "") -> int:
    _soften_console()
    shots = shots_dir or os.path.join(config.DATA_DIR, "shots")
    os.makedirs(shots, exist_ok=True)
    failures: list[str] = []

    # 第二道闸：万一 app.main 里的闸门被绕过，也绝不往真实库里写自检数据。
    if real_db_conflict(getattr(db, "path", "")):
        print(selftest_refuse_text(getattr(db, "path", "")))
        return 2

    print(f"[selftest] 数据库: {db.path}")
    _check(real_db_conflict(config.DB_PATH) and real_db_conflict("")
           and not real_db_conflict(db.path),
           "自检拒绝在真实库上运行（不带 --db 或指向 data/monitor.db 时直接退出，不写任何数据）",
           failures)
    samples = _seed(db)
    print(f"[selftest] 已写入示例程序 {len(samples)} 个: {[os.path.basename(p) for p in samples]}")

    width, height = initial_window_geometry()
    config.set_ui_scale(width / config.BASE_WINDOW_W)
    print(f"[selftest] 初始窗口尺寸: {width}x{height}（屏幕线性缩放）")

    monitor = MonitorApp(app, db, silent=True, size=(width, height))
    monitor.window.show()
    _pump(app, 0.6)

    # 先记下示例程序窗口（用户自己开着的记事本 / 计算器）：收尾时只关这次
    # 新开出来的窗口，绝不碰用户原有的窗口。
    sample_windows_before = _sample_windows()
    launched = _launch_samples(samples)
    _pump(app, 2.6)  # 让轮询线程至少扫描两次

    window = monitor.window
    home = window.home_page
    logs = window.logs_page

    # 1) 首页
    window.switch_page(0, animate=False)
    _pump(app, 0.4)
    print(f"[selftest] 首页卡片数量: {len(home.cards)}")

    # 1b) 自定义标题栏（替代系统标题栏）
    title_bar = window.title_bar
    _check(title_bar.isVisible() and title_bar.minimize_btn.isVisible()
           and title_bar.maximize_btn.isVisible() and title_bar.close_btn.isVisible(),
           "自定义标题栏与最小化/最大化/关闭按钮已显示", failures)
    _check(bool(window.windowFlags() & Qt.FramelessWindowHint),
           "窗口已去掉系统标题栏（FramelessWindowHint）", failures)
    window.toggle_maximized()
    _pump(app, 0.4)
    maximized_ok = window.isMaximized() and window.property("maximized") is True
    _pump(app, 0.3)
    window.toggle_maximized()
    _pump(app, 0.4)
    restored_ok = (not window.isMaximized()) and window.property("maximized") is False
    print(f"[selftest] 最大化按钮: 最大化={maximized_ok} 还原={restored_ok}")
    _check(maximized_ok and restored_ok, "标题栏最大化按钮可最大化并还原窗口", failures)

    running = monitor.tracker.snapshot()
    print(f"[selftest] 检测到运行中的程序: "
          f"{ {pid: [p for p, _ in items] for pid, items in running.items()} }")

    # 同一程序多进程只应产生一条日志（否则微信这类多进程程序会刷出多条重复记录）
    multi_program = next((pid for pid, items in running.items() if len(items) >= 2), None)
    if multi_program is None:
        print("[selftest] 未检测到多实例程序，跳过重复日志校验")
        _check(True, "多实例日志去重（本次未出现多实例程序，跳过）", failures)
    else:
        rows = db.logs_for_program(multi_program)
        open_rows = [r for r in rows if r["end_time"] is None]
        print(f"[selftest] 程序 {multi_program} 运行实例 {len(running[multi_program])} 个，"
              f"对应日志 {len(rows)} 条（未闭合 {len(open_rows)} 条）")
        _check(len(running[multi_program]) >= 2 and len(open_rows) == 1,
               "同一程序多进程合并为一条使用记录（忽略重复进程）", failures)

    # 关闭追踪：立即结束本次使用记录 + 卡片显示灰色"已关闭追踪"；且真正停止监控
    # （没有任何运行中的实例时也要跑，保证这条端到端链路始终被覆盖）
    tracking_program = multi_program
    if tracking_program is None and home.cards:
        tracking_program = next(iter(home.cards))
    if tracking_program is not None:
        card = home.cards.get(tracking_program)
        if card is not None:
            trailing_before = card.time_label.text()
            card.switch.setChecked(False, animate=True, emit=True)
            offsets = []
            for _ in range(12):
                _pump(app, 0.03)
                offsets.append(round(card.switch.offset, 2))
            _pump(app, 0.3)
            same_card = home.cards.get(tracking_program) is card
            animated = len(set(offsets)) >= 3
            program_row = db.get_program(tracking_program)
            open_rows = [r for r in db.logs_for_program(tracking_program) if r["end_time"] is None]
            still_tracked = tracking_program in monitor.tracker.snapshot()
            label_text = card.time_label.text()
            print(f"[selftest] 关闭追踪：原状态='{trailing_before[:18]}' 现状态='{label_text}' "
                  f"未闭合日志={len(open_rows)} 仍在监控={still_tracked} "
                  f"滑块动画采样={offsets[:6]}")
            _check(program_row is not None and not bool(program_row["tracking"])
                   and not open_rows and not still_tracked and label_text == "已关闭追踪",
                   "关闭追踪后立即结束本次追踪、停止监控并显示“已关闭追踪”", failures)
            _check(same_card and animated,
                   "关闭追踪时卡片不被重建、开关滑块有平滑过渡动画", failures)
            _shot(window, os.path.join(shots, "01b_tracking_off.png"))
            # 恢复追踪
            if card is not None:
                card.switch.setChecked(True, animate=False, emit=True)
                _pump(app, 0.4)

    # 追踪状态机的确定性校验（不依赖真实进程）：
    # 用假的进程列表驱动 tracker._scan()，验证"关闭追踪后即使扫描竞态也不会重开日志"、
    # "重新开启追踪后这一次计时从零开始"。
    state_program = next((int(p["id"]) for p in db.programs(visible_only=True)), None)
    if state_program is not None and multi_program is not None:
        state_program = multi_program
    if state_program is not None:
        from monitor import tracker as _tracker_module
        from monitor.database import normalize_path as _norm

        class _FakeProc:
            def __init__(self, pid: int, exe: str, created: float) -> None:
                self.info = {"pid": pid, "exe": exe}
                self._created = created

            def create_time(self) -> float:
                return self._created

        path = str(db.get_program(state_program)["path"])
        fake_pid = 999999
        db.set_tracking(state_program, False)
        monitor.tracker.set_programs(db.programs(visible_only=True), notify=False)
        monitor.tracker.close_program(state_program)
        # 人为构造"陈旧索引"：模拟扫描开始后用户才关掉追踪的竞态
        with monitor.tracker._lock:
            monitor.tracker._path_index[_norm(path)] = state_program
        original_iter = _tracker_module.psutil.process_iter
        _tracker_module.psutil.process_iter = lambda *a, **k: [
            _FakeProc(fake_pid, path, time.time() - 120)]
        monitor.tracker.blockSignals(True)      # 这一段是确定性单元校验，不要打扰界面
        try:
            monitor.tracker._scan()
        finally:
            monitor.tracker.blockSignals(False)
            _tracker_module.psutil.process_iter = original_iter
        open_after = [r for r in db.logs_for_program(state_program) if r["end_time"] is None]
        print(f"[selftest] 竞态校验：关闭追踪后陈旧索引再扫描，未闭合日志={len(open_after)}")
        _check(not open_after,
               "关闭追踪后即使扫描竞态也不会重新开始计时（不会重开日志）", failures)

        # 重新开启追踪：进程已运行 120 秒，本次计时仍应从"现在"开始
        db.set_tracking(state_program, True)
        monitor.tracker.set_programs(db.programs(visible_only=True), notify=False)
        fresh_ok = state_program in monitor.tracker._fresh
        _tracker_module.psutil.process_iter = lambda *a, **k: [
            _FakeProc(fake_pid, path, time.time() - 120)]
        started_at = time.time()
        monitor.tracker.blockSignals(True)
        try:
            monitor.tracker._scan()
        finally:
            monitor.tracker.blockSignals(False)
            _tracker_module.psutil.process_iter = original_iter
        rows = [r for r in db.logs_for_program(state_program) if r["end_time"] is None]
        delta = (rows[-1]["start_time"] - started_at) if rows else None
        print(f"[selftest] 重新开启追踪：fresh={fresh_ok} 新日志起点偏差="
              f"{round(delta, 2) if delta is not None else None} 秒（应接近 0，而不是 -120）")
        _check(bool(rows) and delta is not None and abs(delta) < 5,
               "重新开启追踪后本次计时从零开始（不包含之前的运行时间）", failures)
        monitor.tracker.close_program(state_program)
        db.set_tracking(state_program, False)
        monitor.tracker.set_programs(db.programs(visible_only=True), notify=False)
        db.set_tracking(state_program, True)
        monitor.tracker.set_programs(db.programs(visible_only=True), notify=False)

    # 悬浮不应引起文字位移（回归 QGraphicsDropShadowEffect 缓存导致的闪烁）
    hover_card = next(iter(home.cards.values()), None)
    if hover_card is not None:
        _check(hover_card.graphicsEffect() is None,
               "卡片不使用 QGraphicsDropShadowEffect（阴影改为自绘，避免悬浮闪烁）", failures)
        try:
            before = hover_card.name.label.grab().toImage()
            hover_card._animate_hover(1.0)
            _pump(app, 0.45)
            middle = hover_card.name.label.grab().toImage()
            hover_card._animate_hover(0.0)
            _pump(app, 0.45)
            after = hover_card.name.label.grab().toImage()
            _check(before == middle == after, "卡片悬浮/离开时文字不发生跳动", failures)
        except RuntimeError:
            # 追踪事件在检查期间刷新了列表、卡片被替换，跳过这一帧比较
            _check(True, "卡片悬浮/离开时文字不发生跳动（本次列表刚被刷新，跳过）", failures)

    # 添加程序：完整走一遍"选择文件 → 弹窗确认 → 入库 → 卡片出现"
    import monitor.ui.home_page as _home_module

    add_target = next((p for p in CANDIDATE_EXES if os.path.exists(p)), "")
    if add_target:
        original_picker = _home_module.pick_executables
        _home_module.pick_executables = lambda parent=None, _p=add_target: [_p]
        programs_before = len(db.programs(visible_only=True))

        def _confirm_add() -> None:
            for widget in app.topLevelWidgets():
                if isinstance(widget, AddProgramsDialog) and widget.isVisible():
                    print(f"[selftest] 添加弹窗条目={len(widget.entries)}，点击确认")
                    widget.confirm_btn.click()

        QTimer.singleShot(500, _confirm_add)
        try:
            home.on_add()
        except Exception as exc:  # noqa: BLE001
            print("[selftest] on_add 抛出异常:", type(exc).__name__, exc)
        finally:
            _home_module.pick_executables = original_picker
        _pump(app, 0.5)
        rows_after = [dict(p) for p in db.programs(visible_only=True)]
        added_ok = (len(rows_after) == programs_before + 1
                    and any(str(r["path"]).lower() == add_target.lower() for r in rows_after))
        print(f"[selftest] 添加程序: 目标={os.path.basename(add_target)} "
              f"程序数 {programs_before} -> {len(rows_after)} 首页卡片={len(home.cards)}")
        _check(added_ok, "首页“添加”：选择文件 → 弹窗确认 → 写入数据库 → 卡片出现", failures)
        _shot(window, os.path.join(shots, "01c_after_add.png"))

    _shot(window, os.path.join(shots, "01_home.png"))

    # 2) 首页 + 搜索高亮
    home.search.setText("note")
    _pump(app, 0.3)
    _shot(window, os.path.join(shots, "02_home_search.png"))
    home.search.setText("")
    _pump(app, 0.2)

    # 2b) 搜索无结果时的空状态
    home.search.setText("zzz-不存在")
    _pump(app, 0.3)
    print(f"[selftest] 搜索无结果时空状态可见: {home.empty.isVisible()}")
    _shot(window, os.path.join(shots, "02b_home_empty.png"))
    home.search.setText("")
    _pump(app, 0.2)

    # 3) 排序菜单
    home.toggle_sort()
    _pump(app, 0.4)
    popup = None
    for widget in app.topLevelWidgets():
        if widget.isVisible() and widget.__class__.__name__ == "SortPopup":
            popup = widget
    if popup is not None:
        _shot(popup, os.path.join(shots, "03_sort_menu.png"))
        popup.close()
    _pump(app, 0.2)

    # 4) 重命名编辑器（就地输入框）
    if home.cards:
        first = next(iter(home.cards.values()))
        original_name = first.name.text()
        first.name.begin_edit()
        _pump(app, 0.4)
        _shot(window, os.path.join(shots, "04_rename_editor.png"))

        # 4b) 输入框自动向右扩展 + 超出后出现水平滑块
        editor = first.name._editor
        if editor is not None:
            base_width = editor.width()
            base_input_height = editor.scroll.height()
            editor.edit.setText(original_name + " —— 一个非常非常长的程序名称用于验证输入框自动扩展行为")
            _pump(app, 0.3)
            widened = editor.width()
            has_bar = editor.bar.isVisible()
            input_bottom = editor.scroll.mapTo(editor, QPoint(0, editor.scroll.height())).y()
            bar_top = editor.bar.mapTo(editor, QPoint(0, 0)).y()
            no_overlap = bar_top >= input_bottom
            print(f"[selftest] 输入框宽度 {base_width} -> {widened}，出现水平滑块: {has_bar}，"
                  f"滑块位于输入框下方: {no_overlap}（输入框下沿 y={input_bottom}，滑块上沿 y={bar_top}）")
            _check(has_bar, "超长名称时输入框下方出现水平滑块条", failures)
            _check(no_overlap and editor.scroll.height() == base_input_height,
                   "水平滑块条在输入框外侧、不占用输入框高度（不遮挡名称）", failures)

            # 拖动滑块应能左右滚动输入内容
            editor.bar.setValue(editor.bar.maximum())
            _pump(app, 0.2)
            scrolled = editor.scroll.horizontalScrollBar().value() > 0
            _check(scrolled, "拖动滑块可左右滚动输入框内容", failures)
            editor.bar.setValue(0)
            _pump(app, 0.15)
            _shot(window, os.path.join(shots, "04b_rename_expand.png"))

            # 4c) 回车确认改名
            QTest.keyClick(editor.edit, Qt.Key_Return)
            _pump(app, 0.3)
            renamed = db.get_program(first.program_id)
            new_name = str(renamed["name"]) if renamed else ""
            print(f"[selftest] 回车确认改名成功: {new_name.endswith('输入框自动扩展行为')} "
                  f"({new_name[:24]}…)")
            # 还原名称
            db.rename_program(first.program_id, original_name)
            home.refresh()
            _pump(app, 0.2)

        # 4d) 点击界面其他位置应取消编辑
        target = first.name
        # 4d) 点击 √ 也要能重命名
        #     回归：真实鼠标点击会在按下瞬间让输入框失焦，旧逻辑把这次点击当成"点了别处"
        #     而先取消编辑，于是点 √ 无效（只有回车能改名）。
        first.name.begin_edit()
        _pump(app, 0.5)
        click_editor = first.name._editor
        if click_editor is not None:
            click_editor.edit.setText("点击勾号改名")
            _pump(app, 0.2)
            center = click_editor.confirm_btn.rect().center()
            QTest.mousePress(click_editor.confirm_btn, Qt.LeftButton, Qt.NoModifier, center)
            click_editor.edit.clearFocus()          # 模拟真实点击导致的焦点变化
            _pump(app, 0.08)
            QTest.mouseRelease(click_editor.confirm_btn, Qt.LeftButton, Qt.NoModifier, center)
            _pump(app, 0.5)
            saved_name = str(db.get_program(first.program_id)["name"])
            print(f"[selftest] 点击 √ 重命名（按下时失焦）：数据库名称='{saved_name}' "
                  f"编辑器已关闭={first.name._editor is None}")
            _check(saved_name == "点击勾号改名" and first.name._editor is None,
                   "点击 √ 按钮也能完成重命名（不因按下时失焦被取消）", failures)
            db.rename_program(first.program_id, original_name)
            home.refresh()
            _pump(app, 0.3)

        # 4e) 点击界面其他位置应取消编辑
        target = first.name
        target.begin_edit()
        _pump(app, 0.3)
        editing_before = target.is_editing()
        second = [c for c in home.cards.values() if c is not first]
        if second:
            QTest.mouseClick(second[0], Qt.LeftButton)
            _pump(app, 0.3)
        print(f"[selftest] 点击其他区域取消编辑: 编辑中={editing_before} -> "
              f"{target.is_editing()}")
        if target.is_editing():
            target.cancel_edit()
        _pump(app, 0.2)

    # 5) 添加弹窗（用尚未加入列表的程序，模拟"文件选择器确认后"的弹窗）
    existing = {str(p["path"]) for p in db.programs(include_deleted=True)}
    candidates = [p for p in CANDIDATE_EXES if os.path.exists(p)
                  and normalize_path(p) not in existing]
    candidates += [p for p in samples if os.path.exists(p)]  # 混入已存在的程序验证去重
    add_dialog = AddProgramsDialog(window, candidates, existing_paths=existing)
    add_dialog.center_on_parent()
    add_dialog.setWindowOpacity(1.0)
    add_dialog.show()
    _pump(app, 0.5)
    print(f"[selftest] 添加弹窗候选项: {len(candidates)} 个，去重后 {len(add_dialog.entries)} 个")
    _shot(add_dialog, os.path.join(shots, "05_add_dialog.png"))
    add_dialog.close()
    _pump(app, 0.2)

    # 6) 删除模式 + 确认删除弹窗
    cards_before_delete = dict(home.cards)
    home.enter_delete_mode()
    _pump(app, 0.4)
    same_cards = all(home.cards.get(pid) is card for pid, card in cards_before_delete.items())
    print(f"[selftest] 进入删除模式：卡片是否原对象={same_cards}（不重建才不会闪出提示气泡）")
    _check(same_cards and home.delete_mode,
           "点击“删除”就地切换删除模式、不重建卡片（不会闪出提示）", failures)
    home.select_all()
    _pump(app, 0.3)
    _shot(window, os.path.join(shots, "06_delete_mode.png"))
    selected = [p for p in db.programs(visible_only=True)]
    delete_dialog = ConfirmDeleteDialog(window, selected)
    delete_dialog.center_on_parent()
    delete_dialog.setWindowOpacity(1.0)
    delete_dialog.show()
    _pump(app, 0.5)
    _shot(delete_dialog, os.path.join(shots, "07_delete_dialog.png"))

    # 回归检查：右侧"保留日志"滑块必须完整落在弹窗可视范围内
    container = delete_dialog.container
    clipped = []
    for program_id, switch in delete_dialog.switches.items():
        right_edge = switch.mapTo(container, QPoint(switch.width(), 0)).x()
        if right_edge > container.width():
            clipped.append((program_id, right_edge, container.width()))
    print(f"[selftest] 删除弹窗宽度={container.width()}，滑块右边界超界项: {clipped}")
    _check(not clipped, "删除弹窗右侧“保留日志”滑块完整显示（未被裁切）", failures)

    delete_dialog.close()
    _pump(app, 0.3)

    # 6b) 真正执行删除：验证"被删卡片右移淡出 + 其余卡片自上而下上移"动画
    all_programs = [dict(p) for p in db.programs(visible_only=True)]
    protected = {int(all_programs[0]["id"]), int(all_programs[-1]["id"])}
    # 搜索检查依赖路径含 "note" 的程序，不要删掉
    protected |= {int(p["id"]) for p in all_programs if "note" in str(p["path"]).lower()}
    safe_ids = [pid for pid in home.cards if pid not in protected]
    victim_ids = safe_ids[:2] if len(home.cards) - 2 >= 3 else safe_ids[:1]
    for program_id, card in home.cards.items():
        card.set_selected(program_id in victim_ids)
    home.selected = set(victim_ids)
    _pump(app, 0.2)
    victims = [home.cards[i] for i in victim_ids]
    count_before = len(home.cards)

    original_run = ConfirmDeleteDialog.run

    def _fake_delete_run(self) -> int:
        self.result = {pid: True for pid in self.switches}
        return QDialog.Accepted

    ConfirmDeleteDialog.run = _fake_delete_run
    try:
        home.on_confirm_delete()
    finally:
        ConfirmDeleteDialog.run = original_run

    xs: list[int] = []
    for _ in range(10):
        _pump(app, 0.04)
        xs.append(victims[0].x())
    deltas = [b - a for a, b in zip(xs, xs[1:]) if b > a]
    moving_right = xs[-1] > xs[0] + 30 and all(b >= a for a, b in zip(xs, xs[1:]))
    accelerating = bool(deltas) and max(deltas) > deltas[0] * 1.5
    print(f"[selftest] 删除动画：被删卡片 x 轨迹={xs[:8]} "
          f"分段位移={deltas}（递增={moving_right} 加速={accelerating}）")
    _check(moving_right and accelerating,
           "删除时被删卡片向右平移且平移速度线性提升", failures)
    _shot(window, os.path.join(shots, "06b_delete_anim.png"))
    _wait_until(lambda: not home._animating, app, 4.0)
    _pump(app, 0.4)
    print(f"[selftest] 动画结束后卡片 {count_before} -> {len(home.cards)}，"
          f"数据库程序数={len(db.programs(visible_only=True))}")
    _check(len(home.cards) == count_before - len(victim_ids),
           "删除动画结束后列表按数据库结果重建", failures)

    # 回归：删除动画把卡片移出布局后必须被彻底清理，否则首页会残留"幽灵卡片"
    # （表现为同一程序出现两张卡片、已删除的程序还留在首页）
    from .ui.cards import ProgramCard as _ProgramCard

    leftover = home.holder.findChildren(_ProgramCard, options=Qt.FindDirectChildrenOnly)
    visible_leftover = [w for w in leftover if not w.isHidden()]
    print(f"[selftest] 删除后卡片控件数={len(leftover)}（可见 {len(visible_leftover)}）"
          f"，self.cards={len(home.cards)}")
    _check(len(leftover) == len(home.cards) and len(visible_leftover) <= len(home.cards),
           "删除后不残留旧卡片控件（首页不会出现重复卡片/已删程序仍在）", failures)

    # 首页滚轮滚动：平滑补间（不是一格格跳）
    rolling = home.scroll
    if rolling.verticalScrollBar().maximum() > 0:
        home.holder.setMinimumHeight(home.holder.height())
        wheel_home = QWheelEvent(
            QPointF(rolling.viewport().rect().center()),
            QPointF(rolling.viewport().mapToGlobal(rolling.viewport().rect().center())),
            QPoint(0, 0), QPoint(0, -120), Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
        app.sendEvent(rolling.viewport(), wheel_home)
        home_wheel: list[int] = []
        for _ in range(6):
            _pump(app, 0.02)
            home_wheel.append(rolling.verticalScrollBar().value())
        print(f"[selftest] 首页滚轮采样={home_wheel}")
        _check(len(set(home_wheel)) >= 3 and home_wheel[-1] > home_wheel[0],
               "首页滚轮滚动为平滑补间动画（非一格格跳变）", failures)
        rolling.verticalScrollBar().setValue(0)
        _pump(app, 0.2)
    else:
        print("[selftest] 首页内容不足一屏，滚轮校验改到日志页进行")

    # 7) 日志页 - 按时间排序 / 按程序分类
    window.switch_page(1, animate=False)
    logs.set_mode("time")
    _pump(app, 0.5)
    log_count = sum(1 for i in range(logs.list_layout.count())
                    if logs.list_layout.itemAt(i).widget() is not None)
    print(f"[selftest] 日志页（按时间排序）行数: {log_count}")
    _shot(window, os.path.join(shots, "08_logs_time.png"))

    # 日志页滚轮滚动：同样是平滑补间（与首页共用 SlimScrollArea 的实现）
    logs_scroll = logs.scroll
    if logs_scroll.verticalScrollBar().maximum() > 0:
        wheel_logs = QWheelEvent(
            QPointF(logs_scroll.viewport().rect().center()),
            QPointF(logs_scroll.viewport().mapToGlobal(logs_scroll.viewport().rect().center())),
            QPoint(0, 0), QPoint(0, -120), Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
        app.sendEvent(logs_scroll.viewport(), wheel_logs)
        logs_wheel: list[int] = []
        for _ in range(6):
            _pump(app, 0.02)
            logs_wheel.append(logs_scroll.verticalScrollBar().value())
        print(f"[selftest] 日志页滚轮采样={logs_wheel}")
        _check(len(set(logs_wheel)) >= 3 and logs_wheel[-1] > logs_wheel[0],
               "日志页滚轮滚动为平滑补间动画（非一格格跳变）", failures)
        logs_scroll.verticalScrollBar().setValue(0)
        _pump(app, 0.2)
    else:
        _check(True, "日志页滚轮滚动为平滑补间动画（本页内容不足一屏，跳过）", failures)

    logs.open_program_dropdown()
    _pump(app, 0.5)
    dropdown = None
    for widget in app.topLevelWidgets():
        if widget.isVisible() and widget.__class__.__name__ == "ProgramDropdown":
            dropdown = widget
    if dropdown is not None:
        _shot(dropdown, os.path.join(shots, "09_logs_dropdown.png"))
        dropdown.close()
    _pump(app, 0.3)
    _shot(window, os.path.join(shots, "10_logs_program.png"))

    # 7b) 日志页删除流程（与首页"删除"一致：删除模式 -> 勾选 -> 确认弹窗 -> 落库删除）
    logs.set_mode("time")
    _pump(app, 0.4)
    logs.enter_delete_mode()
    _pump(app, 0.4)
    checkboxes_visible = bool(logs.rows) and all(
        row.check.isVisible() for row in logs.rows.values())
    print(f"[selftest] 日志删除模式：记录 {len(logs.records)} 条，行控件 {len(logs.rows)} 个，"
          f"复选框可见 {checkboxes_visible}，横栏计数“{logs.count_label.text()}”")
    _shot(window, os.path.join(shots, "08b_logs_delete_mode.png"))
    _check(logs.delete_mode and checkboxes_visible,
           "日志页删除模式：每条记录左侧出现圆形复选框", failures)
    _check(not logs.delete_btn.isVisible() and logs.confirm_delete_btn.isVisible(),
           "日志页删除模式：横栏切换为 全选/取消全选 + 取消/确认删除", failures)

    # 滑动选中：按住左键划过若干行会连续选中；靠近上下边缘时开始自动滚动；
    # 滑过头再往回滑，多选的行要恢复成按下之前的状态
    # （前面的滚轮检查会把列表滚下去，先回到顶部；示例程序窗口此刻可能压在
    #   Monitor 之上，QApplication.widgetAt 会命中别的进程的窗口而返回空，
    #   所以先把主窗口提到最前）
    bar = logs.scroll.verticalScrollBar()
    # 先恢复正常的屏幕状态：前面的窗口检查可能把主窗口最小化过、或把它停到屏幕外
    # （本文件后面就有 `host.move(-4000, -4000)` 这类"别出现在截图里"的做法）。
    # 这两种情况下 QApplication.widgetAt 一律返回空——压不到任何一行，
    # 所以这里先 showNormal 再拉回主屏，最后提到最前。
    from PySide6.QtGui import QGuiApplication
    screens = QGuiApplication.screens()
    on_screen = any(s.geometry().intersects(window.frameGeometry()) for s in screens)
    print(f"[selftest] 拖动前窗口: pos={window.pos().x()},{window.pos().y()} "
          f"尺寸={window.width()}x{window.height()} 屏幕数={len(screens)} 在屏内={on_screen}")
    if window.isMinimized() or not on_screen:
        window.showNormal()
        if screens:
            area = screens[0].availableGeometry()
            window.move(area.x() + max(0, (area.width() - window.width()) // 2),
                        area.y() + max(0, (area.height() - window.height()) // 2))
    window.show()
    window.raise_()
    window.activateWindow()
    bar.setValue(bar.minimum())
    _pump(app, 0.45)

    def _hit_log_id(global_pos: QPoint) -> Optional[int]:
        widget = QApplication.widgetAt(global_pos)
        while widget is not None and getattr(widget, "log_id", None) is None:
            widget = widget.parentWidget()
        if widget is None:
            row = logs._row_at_point(global_pos)
            return getattr(row, "log_id", None)
        return getattr(widget, "log_id", None)

    drag_ids = list(logs.rows)[:3]
    drag_rows = [logs.rows[i] for i in drag_ids if i in logs.rows]
    drag_ok = len(drag_rows) >= 2
    back_ok = False
    after_back: list[int] = []
    if drag_ok:
        for row in logs.rows.values():
            row.set_selected(False)
        logs.selected.clear()
        first, last = drag_rows[0], drag_rows[-1]
        logs._on_row_drag_started(first.log_id, True)
        logs._on_row_drag_moved(last.mapToGlobal(last.rect().center()))
        dragged = [row.log_id for row in drag_rows if row.is_selected()]
        viewport = logs.scroll.viewport()
        edge_pos = viewport.mapToGlobal(QPoint(viewport.width() // 2, viewport.height() - 3))
        step_down = logs._drag_step_for(edge_pos)
        logs._on_row_drag_moved(edge_pos)
        auto_scroll = logs._drag_timer.isActive()
        # 往回滑一行：光标离开的那一行必须恢复成"按下之前"的未选中
        back_row = drag_rows[-2] if len(drag_rows) >= 2 else drag_rows[0]
        logs._on_row_drag_moved(back_row.mapToGlobal(back_row.rect().center()))
        after_back = [row.log_id for row in drag_rows if row.is_selected()]
        back_ok = (len(dragged) >= 2 and len(after_back) < len(dragged)
                   and last.log_id not in after_back)
        # 反向（按住已选中的行取消选中）往回滑同样要恢复
        for row in drag_rows:
            row.set_selected(True)
            logs.selected.add(row.log_id)
        logs._on_row_drag_started(drag_rows[0].log_id, False)
        logs._on_row_drag_moved(last.mapToGlobal(last.rect().center()))
        logs._on_row_drag_moved(drag_rows[0].mapToGlobal(drag_rows[0].rect().center()))
        restored = [row.log_id for row in drag_rows[1:] if row.is_selected()]
        back_ok = back_ok and not drag_rows[0].is_selected() \
            and len(restored) == len(drag_rows) - 1
        logs._on_row_drag_finished()
        drag_ok = (len(dragged) >= 2 and step_down > 0 and auto_scroll
                   and not logs._drag_timer.isActive())
        logs.clear_selection()
        hits = [_hit_log_id(row.mapToGlobal(row.rect().center())) for row in drag_rows]
        probe = drag_rows[0].mapToGlobal(drag_rows[0].rect().center())
        top_widget = QApplication.topLevelAt(probe)
        top_window = QGuiApplication.topLevelAt(probe)
        print(f"[selftest] 日志滑动选中：拖过 {len(dragged)} 行 边缘步长={step_down} "
              f"自动滚动={auto_scroll} 往回滑剩 {len(after_back)} 行 "
              f"反向恢复 {len(restored)} 行 命中={hits} 行={[r.log_id for r in drag_rows]} "
              f"行可见={[r.isVisible() for r in drag_rows]} "
              f"窗口可见={window.isVisible()} 最小化={window.isMinimized()} "
              f"滚动条={bar.value()} 顶层控件={type(top_widget).__name__ if top_widget else None} "
              f"顶层窗口={top_window.title() if top_window else None}")
    else:
        print("[selftest] 日志滑动选中：记录不足，跳过")
    _check(drag_ok, "日志页支持按住左键滑动选中多行，拖到上下边缘会自动滚动", failures)
    _check(back_ok, "日志页滑动选中可往回滑：滑过头多选的行恢复成按下之前的状态", failures)

    logs.select_all()
    _pump(app, 0.25)
    _check(logs._all_selected() and logs.unselect_all_btn.isVisible(),
           "日志页“全选”选中当前页的全部记录并出现“取消全选”", failures)

    # 只保留前两条选中，用真实流程执行删除（把弹窗 exec 换成展示 + 截图 + 确认）
    keep_ids = [record["log_id"] for record in logs.records[:2]]
    for log_id, row in logs.rows.items():
        row.set_selected(log_id in keep_ids)
    logs.selected = set(keep_ids)
    logs._apply_mode_visibility()
    logs._update_count_label()
    _pump(app, 0.25)

    before_count = db.count_logs()
    from PySide6.QtWidgets import QDialog as _QDialog

    from monitor.ui import dialogs as _dialogs_module

    original_run = _dialogs_module.ConfirmDeleteLogsDialog.run

    def _fake_run(self):
        self.center_on_parent()
        self.setWindowOpacity(1.0)
        self.show()
        self.layout().activate()
        _pump(app, 0.45)
        print(f"[selftest] 日志删除确认弹窗列出 {len(self.records)} 条记录")
        _shot(self, os.path.join(shots, "08c_logs_delete_dialog.png"))
        self.close()
        return _QDialog.Accepted

    _dialogs_module.ConfirmDeleteLogsDialog.run = _fake_run
    victim_rows = [logs.rows[i] for i in keep_ids if i in logs.rows]
    victim_x: list[int] = []
    try:
        logs.on_confirm_delete()
        # 动画进行中故意触发定时刷新（回归：定时刷新曾销毁正在动画的日志行，导致列表空白）
        for _ in range(6):
            _pump(app, 0.035)
            logs._on_tick()
            if victim_rows:
                victim_x.append(victim_rows[0].x())
    finally:
        _dialogs_module.ConfirmDeleteLogsDialog.run = original_run
    _wait_until(lambda: not logs._animating, app, 4.0)
    _pump(app, 0.4)

    logs_moving = len(victim_x) > 3 and victim_x[-1] > victim_x[0] + 20 \
        and all(b >= a for a, b in zip(victim_x, victim_x[1:]))
    print(f"[selftest] 日志删除动画：被删行 x 轨迹={victim_x}（右移={logs_moving}）"
          f" 动画后行数={len(logs.rows)} 记录={len(logs.records)} "
          f"db={db.count_logs()} animating={logs._animating} mode={logs.mode}")
    _check(logs_moving, "日志页删除时被删日志向右平移", failures)
    _check(len(logs.rows) == db.count_logs() and len(logs.rows) > 0,
           "日志删除动画期间定时刷新不会打断动画、结束后列表完整", failures)
    _shot(window, os.path.join(shots, "08e_logs_after_anim.png"))

    after_count = db.count_logs()
    remaining_ids = {int(row["id"]) for row in db.all_logs()}
    print(f"[selftest] 日志条数 {before_count} -> {after_count}（删除 {len(keep_ids)} 条）")
    _check(after_count == before_count - len(keep_ids),
           "日志页确认删除后记录从 SQLite 中移除", failures)
    _check(not (set(keep_ids) & remaining_ids),
           "被删除的日志不再出现在列表与数据库中", failures)
    _check(not logs.delete_mode and not logs.selected,
           "删除完成后自动退出删除模式并清空勾选", failures)
    _shot(window, os.path.join(shots, "08d_logs_after_delete.png"))

    # 7b2) 删除动画时长上限：单页 500 条时逐行 40ms 错峰会一路排到十几秒
    def _probe_list(rows: int):
        panel = QWidget(window)
        panel.setGeometry(-4000, -4000, 200, 24 * rows)
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        items = []
        for _ in range(rows):
            row = QWidget()
            row.setFixedHeight(24)
            box.addWidget(row)
            items.append(row)
        panel.show()
        _pump(app, 0.15)
        return panel, items

    big_panel, big_rows = _probe_list(500)
    big_group = anim.animate_list_removal(big_panel, big_rows[:2], 0,
                                          max_ms=config.ANIM_DELETE_MAX, slide_ratio=0.6)
    big_total = big_group.totalDuration()
    big_group.stop()
    big_panel.hide()
    big_panel.deleteLater()

    small_panel, small_rows = _probe_list(10)
    small_group = anim.animate_list_removal(small_panel, small_rows[:1], 0,
                                            max_ms=config.ANIM_DELETE_MAX, slide_ratio=0.6)
    small_total = small_group.totalDuration()
    small_group.stop()
    small_panel.hide()
    small_panel.deleteLater()

    forced_panel, forced_rows = _probe_list(20)
    forced = {"count": 0}

    def _count_finished() -> None:
        forced["count"] += 1

    anim.animate_list_removal(forced_panel, forced_rows[:1], 0, on_finished=_count_finished,
                              max_ms=1, slide_ratio=0.6)
    _pump(app, 0.3)
    forced_ok = forced["count"] == 1 and forced_panel.maximumHeight() == 16777215
    forced_panel.hide()
    forced_panel.deleteLater()

    print(f"[selftest] 删除动画上限：500 行总时长={big_total}ms（上限 {config.ANIM_DELETE_MAX}ms）"
          f" 10 行总时长={small_total}ms 超时强制收尾回调={forced['count']} 次")
    _check(0 < big_total <= config.ANIM_DELETE_MAX,
           "删除动画总时长被压到上限内（单页 500 行不再排到十几秒）", failures)
    _check(800 <= small_total <= 1000,
           "小列表的 40ms 逐行错峰不受上限影响", failures)
    _check(forced_ok, "动画超过上限时强制收尾：直接落到终点并只回调一次", failures)

    # 7c) 日志页分页：日期边界规则 + 底部悬浮选页框 + 翻页 + 全选=当前页 + 数据没变不重建
    fake_days = [(day, amount, None) for day, amount in
                 (("2026-01-03", 88), ("2026-01-02", 139), ("2026-01-01", 300), ("2025-12-31", 700))]
    shape = [[tuple(item) for item in page] for page in logs_page.build_pages(fake_days)]
    expected_pages = [[("2026-01-03", 88, 0)], [("2026-01-02", 139, 0)], [("2026-01-01", 300, 0)],
                      [("2025-12-31", 500, 0)], [("2025-12-31", 200, 500)]]
    print(f"[selftest] 日志分页规则（88/139/300/700 四天）: {shape}")
    _check(shape == expected_pages,
           "日志分页：整天装进当前页、装不下整天挪下一页、单日 >200 独占一页、单日 >500 强制按 500 切",
           failures)

    real_build = logs_page.build_pages
    real_page_size = logs_page.PAGE_SIZE
    logs_page.PAGE_SIZE = 1
    logs_page.build_pages = lambda counts: real_build(counts, page_size=1, single_max=1)
    try:
        logs.set_mode("time")
        logs.page = 0
        logs.refresh(force=True)
        _pump(app, 0.3)
        first_ids = [record["log_id"] for record in logs.records]
        print(f"[selftest] 日志分页：总 {logs.total_records} 条 -> {logs.page_count} 页，"
              f"首页 {len(logs.records)} 行，选页框 visible={logs.pager.isVisible()} "
              f"标签“{logs.pager.label.text()}”")
        _check(logs.total_records > 2 and logs.page_count == logs.total_records
               and len(logs.records) == 1,
               "日志页按每页条数分页：只取当前页的记录（每页 1 条 -> 页数等于总条数）", failures)
        _check(logs.pager.isVisible(), "日志页底部出现悬浮选页框", failures)
        _check(logs.pager.label.text() == tr("logs.page.label", current=1, total=logs.page_count),
               "选页框显示“第 x / N 页”", failures)
        _check(not logs.pager.prev_btn.isEnabled() and logs.pager.next_btn.isEnabled(),
               "第一页时“上一页”不可点、“下一页”可点", failures)
        _shot(window, os.path.join(shots, "08f_logs_pager.png"))

        before_widgets = [id(row) for row in logs.rows.values()]
        logs.refresh()
        logs.refresh()
        _pump(app, 0.15)
        same_widgets = [id(row) for row in logs.rows.values()] == before_widgets
        print(f"[selftest] 日志分页：数据没变时刷两次，行控件原样={same_widgets}")
        _check(same_widgets,
               "日志页数据没变时定时刷新不重建列表（不再每 2 秒重建整张表）", failures)

        logs.goto_page(1)
        _pump(app, 0.3)
        second_ids = [record["log_id"] for record in logs.records]
        _check(logs.page == 1 and len(second_ids) == 1 and not (set(first_ids) & set(second_ids)),
               "选页框翻页后列表换成下一页的记录且不重复", failures)
        _check(logs.scroll.verticalScrollBar().value() == 0, "翻页后列表回到顶部", failures)
        _check(logs.pager.label.text() == tr("logs.page.label", current=2, total=logs.page_count),
               "选页框页码随翻页更新", failures)

        logs.enter_delete_mode()
        _pump(app, 0.2)
        logs.select_all()
        _pump(app, 0.2)
        print(f"[selftest] 日志分页：第 2 页“全选”后已选 {len(logs.selected)} 条 / 共 {logs.total_records} 条")
        _check(len(logs.selected) == len(logs.records) == 1 and logs.total_records > 1,
               "日志页“全选”只选中当前页的记录", failures)
        logs.exit_delete_mode()
        _pump(app, 0.2)

        logs.goto_page(logs.page_count - 1)
        _pump(app, 0.3)
        _check(not logs.pager.next_btn.isEnabled() and logs.pager.prev_btn.isEnabled(),
               "最后一页时“下一页”不可点、“上一页”可点", failures)

        # 7d) 选页框就位 / 底图随移动失效 / 页码直选 / 边缘让位滚动条 / 行内时间列就位
        logs.goto_page(0)
        _pump(app, 0.3)
        expect_x = (logs.width() - logs.pager.width()) // 2
        expect_y = logs.height() - logs.pager.height() - config.sx(16)
        print(f"[selftest] 选页框就位：期望=({expect_x}, {expect_y}) "
              f"实际={logs.pager.geometry().getRect()}")
        _check((logs.pager.x(), logs.pager.y()) == (expect_x, expect_y),
               "选页框钉在页面底部居中（首屏就位，不跑到左上角压住工具栏）", failures)

        row = next(iter(logs.rows.values()))
        name_x_end = row._name_label.x() + row._name_label.width() + row._layout.spacing()
        print(f"[selftest] 行内时间列：名称右缘+间距={name_x_end} 时间列 x={row._times.x()}")
        _check(row._times.x() == name_x_end,
               "名称列省略后同一轮补布局，时间列就位（不再被名称压掉一截）", failures)

        good_rect = logs.pager.geometry().getRect()
        good_size = (logs.width(), logs.height())
        logs.resize(config.sx(100), config.sx(30))
        logs._pager_dirty = False
        logs._place_pager()
        print(f"[selftest] 选页框：页面被缩到极小后 dirty={logs._pager_dirty} "
              f"位置={logs.pager.geometry().getRect()}")
        _check(logs.pager.geometry().getRect() == good_rect and logs._pager_dirty,
               "日志页还没被布局撑开时不按错误几何摆放选页框（记下待摆放）", failures)
        logs.resize(*good_size)
        logs._place_pager()
        _check(logs.pager.geometry().getRect() == good_rect,
               "日志页撑开后选页框立刻回到正确位置", failures)

        logs.pager.refresh_backdrop()
        logs.pager.grab()
        cached = logs.pager._backdrop is not None
        logs.pager.move(logs.pager.x() + config.sx(3), logs.pager.y())
        print(f"[selftest] 选页框底图：移动前有缓存={cached} "
              f"移动后已失效={logs.pager._backdrop is None}")
        _check(cached and logs.pager._backdrop is None,
               "选页框移动后丢弃旧底图（不再把旧位置那块像素贴到新位置）", failures)
        logs._place_pager()
        _check(logs.pager.geometry().getRect() == good_rect, "选页框重新摆回原位", failures)

        logs.pager.begin_edit()
        editing = logs.pager.editor.isVisible() and logs.pager.editor.text() == "1"
        logs.pager.editor.setText("2")
        logs.pager.commit_edit()
        _pump(app, 0.4)
        print(f"[selftest] 页码直选：点标签变输入框={editing} 输入 2 后 page={logs.page} "
              f"标签“{logs.pager.label.text()}”")
        _check(editing and logs.page == 1
               and logs.pager.label.text() == tr("logs.page.label", current=2,
                                                 total=logs.page_count),
               "点“第 x / N 页”就地变输入框，输入页码直接跳转", failures)
        _check(not logs.pager.editor.isVisible(), "跳转后输入框收起，选页框外观恢复", failures)
        logs.pager.begin_edit()
        logs.pager.editor.setText("999")
        logs.pager.commit_edit()
        _pump(app, 0.4)
        _check(logs.page == logs.page_count - 1, "输入超出范围的页码被夹到最后一页", failures)
        logs.pager.begin_edit()
        logs.pager.editor.setText("abc")
        logs.pager.commit_edit()
        _pump(app, 0.3)
        _check(logs.page == logs.page_count - 1 and not logs.pager.editor.isVisible(),
               "非法输入不跳转、输入框照常收起", failures)
        logs.goto_page(0)
        _pump(app, 0.3)

        probe_bar = QScrollBar(Qt.Vertical, logs)
        probe_bar.resize(config.sx(9), config.sx(120))
        probe_bar.move(logs.width() - config.sx(11), config.sx(60))
        probe_bar.show()
        _pump(app, 0.1)
        centre = probe_bar.mapToGlobal(QPoint(probe_bar.width() // 2, probe_bar.height() // 2))
        on_bar = main_window.point_hits_scrollbar(centre, window)
        off_bar = main_window.point_hits_scrollbar(
            probe_bar.mapToGlobal(QPoint(-config.sx(300), probe_bar.height() // 2)), window)
        edge = window._resize_edge(window.mapFromGlobal(centre))
        print(f"[selftest] 边缘滚动条：条上命中={on_bar} 旁边命中={off_bar} 该处缩放判定={edge!r}")
        _check(on_bar and not off_bar and edge == "right",
               "窗口右缘缩放判定让位给滚动条（6px 判定带里也能点到滚动条）", failures)
        probe_bar.hide()
        probe_bar.setParent(None)
        probe_bar.deleteLater()
        _pump(app, 0.2)
    finally:
        logs_page.PAGE_SIZE = real_page_size
        logs_page.build_pages = real_build
        logs.page = 0
        logs.refresh(force=True)
        _pump(app, 0.3)

    # 8) 通知 / 设置
    window.switch_page(2, animate=False)
    _pump(app, 0.4)
    _shot(window, os.path.join(shots, "11_notify.png"))

    window.switch_page(4, animate=False)
    _pump(app, 0.4)
    _shot(window, os.path.join(shots, "12_settings.png"))

    # 8b) 设置页顶部"语言 / 外观"卡片与主题切换
    settings = window.settings_page
    print(f"[selftest] 设置项：语言='{settings.language_select.current_key()}' "
          f"外观='{settings.theme_select.current_key()}'（默认应为 zh 与 system）")
    _check(settings.language_select.current_key() in ("zh", "en")
           and settings.theme_select.current_key() in ("light", "dark", "system"),
           "设置页顶部新增“语言 / 外观”两项设置（下拉选项）", failures)

    # 切换主题 / 语言期间不允许弹出任何额外顶层窗口（回归：无父控件时 setVisible(True)
    # 会把标签弹成独立窗口，表现为"弹窗一闪而过"）
    flashed: list[str] = []

    class _TopLevelWatcher(QObject):
        def eventFilter(self, obj, event):  # noqa: N802
            if (event.type() == QEvent.Show and isinstance(obj, QWidget)
                    and obj.isWindow() and obj is not window):
                flashed.append(f"{obj.__class__.__name__}:{obj.objectName()}")
            return False

    top_watcher = _TopLevelWatcher()
    app.installEventFilter(top_watcher)

    settings.theme_select.set_current("light", emit=True)
    hwnd_before = int(window.winId())
    window_obj = window
    _pump(app, 1.3)
    window = monitor.window
    home = window.home_page
    logs = window.logs_page
    settings = window.settings_page
    print(f"[selftest] 切换到浅色模式：current_theme={config.current_theme()} "
          f"下拉值={settings.theme_select.current_key()} "
          f"窗口复用={window is window_obj and int(window.winId()) == hwnd_before} "
          f"导航栏宽度={window.nav.width()}")
    _check(config.current_theme() == "light" and settings.theme_select.current_key() == "light",
           "“外观”可切换到浅色模式并即时重建界面", failures)
    _check(window is window_obj and int(window.winId()) == hwnd_before,
           "切换主题时原地重建界面、复用同一窗口（不闪窗）", failures)
    _check(window.nav.isVisible() and window.nav.width() > 60,
           "切换主题后左侧功能栏目仍然正常显示（宽度未被压成 0）", failures)
    window.switch_page(0, animate=False)
    _pump(app, 0.4)
    _shot(window, os.path.join(shots, "20_light_home.png"))
    window.switch_page(4, animate=False)
    _pump(app, 0.4)
    _shot(window, os.path.join(shots, "21_light_settings.png"))
    window.switch_page(1, animate=False)
    _pump(app, 0.4)
    _shot(window, os.path.join(shots, "22_light_logs.png"))

    settings.theme_select.set_current("dark", emit=True)
    _pump(app, 1.3)
    window = monitor.window
    home = window.home_page
    logs = window.logs_page
    print(f"[selftest] 切回深色模式：current_theme={config.current_theme()}")
    _check(config.current_theme() == "dark", "可切回深色模式", failures)
    window.switch_page(0, animate=False)
    _pump(app, 0.4)

    # 8c) 语言：切换到 English 并即时生效
    from monitor.i18n import current_language

    settings = window.settings_page
    settings.language_select.set_current("en", emit=True)
    _pump(app, 1.3)
    window = monitor.window
    home = window.home_page
    logs = window.logs_page
    settings = window.settings_page
    nav_first = window.nav.buttons[0].text() if window.nav.buttons else ""
    page_title = window.topbar.title.text()
    print(f"[selftest] 切换到 English：language={current_language()} "
          f"导航首项='{nav_first}' 页面标题='{page_title}'")
    _check(current_language() == "en" and nav_first == "Home" and page_title == "Home",
           "“语言”可切换到 English 并即时生效（导航与页面标题同步）", failures)
    window.switch_page(0, animate=False)
    _pump(app, 0.4)
    _shot(window, os.path.join(shots, "23_en_home.png"))
    window.switch_page(4, animate=False)
    _pump(app, 0.4)
    _shot(window, os.path.join(shots, "24_en_settings.png"))
    window.switch_page(1, animate=False)
    _pump(app, 0.4)
    _shot(window, os.path.join(shots, "25_en_logs.png"))

    settings.language_select.set_current("zh", emit=True)
    _pump(app, 1.3)
    app.removeEventFilter(top_watcher)
    window = monitor.window
    home = window.home_page
    logs = window.logs_page
    nav_first = window.nav.buttons[0].text() if window.nav.buttons else ""
    print(f"[selftest] 切回中文：language={current_language()} 导航首项='{nav_first}' "
          f"导航栏宽度={window.nav.width()} 切换期间弹出窗口={flashed}")
    _check(current_language() == "zh" and nav_first == "首页", "可切回中文", failures)
    _check(not flashed,
           "切换外观 / 语言期间不弹出额外顶层窗口（弹窗一闪而过已修复）", failures)
    window.switch_page(0, animate=False)
    _pump(app, 0.4)

    # 9) 放大窗口（1180x820）再各拍一张：便于评估正常尺寸下的观感
    window.resize(1180, 820)
    _pump(app, 0.5)
    window.switch_page(0, animate=False)
    _pump(app, 0.4)
    _shot(window, os.path.join(shots, "13_wide_home.png"))
    window.switch_page(1, animate=False)
    logs.set_mode("time")
    _pump(app, 0.4)
    _check(bool(logs.rows) and logs.rows[next(iter(logs.rows))].isVisible(),
           "宽窗口下日志列表面板正常渲染（面板未被缓存为空白）", failures)
    _shot(window, os.path.join(shots, "14_wide_logs.png"))
    window.switch_page(4, animate=False)
    _pump(app, 0.4)
    _shot(window, os.path.join(shots, "15_wide_settings.png"))
    window.switch_page(2, animate=False)
    _pump(app, 0.3)
    _shot(window, os.path.join(shots, "16_wide_notify.png"))
    window.switch_page(0, animate=False)
    _pump(app, 0.3)

    # 数据落盘检查
    open_logs = db.active_logs()
    print(f"[selftest] SQLite 中未闭合（运行中）日志: {len(open_logs)} 条")
    print(f"[selftest] 日志总数: {len(db.all_logs())}")
    _control_state_machine_checks(failures)
    _control_page_checks(app, window, db, failures, shots)
    _site_checks(app, window, db, failures, shots)
    failures = _functional_checks(db, monitor, home, failures)

    for process in launched:
        try:
            process.terminate()
        except Exception:
            pass
    # 应用商店别名的"计算器 / 记事本"其实是激活器进程，terminate() 关不掉真正的
    # 窗口进程，所以再按窗口收一遍（用户自己开的窗口不在 before 里，不会被关）。
    overlay.visible_windows = _REAL_VISIBLE_WINDOWS
    closed = _close_sample_windows(sample_windows_before, launched)
    _pump(app, 0.6)
    leftover = _sample_windows() - sample_windows_before
    if leftover:
        from . import site_watch as _site_watch

        detail = [f"{hex(hwnd)}(pid={_site_watch.window_pid(hwnd)},"
                  f"class={_site_watch.window_class(hwnd)})" for hwnd in sorted(leftover)]
        print(f"[selftest] 收尾后仍残留的示例窗口: {detail}")
    print(f"[selftest] 示例窗口收尾: 结束进程 {closed} 个，残留窗口 {len(leftover)} 个")
    _check(not leftover,
           f"自检收尾关闭示例程序窗口（记事本 / 计算器 / 画图 / cmd）残留={len(leftover)}",
           failures)

    monitor.tracker.stop()
    monitor.tracker.wait(3000)
    monitor.tray.hide()
    db.close()
    print(f"[selftest] 截图输出目录: {shots}")
    if failures:
        for line in failures:
            print(f"[selftest] 功能校验未通过: {line}")
        print("[selftest] FAILED")
        return 1
    print("[selftest] 功能校验全部通过")
    print("[selftest] OK")
    return 0


def _check(condition: bool, text: str, failures: list) -> None:
    print(f"[selftest] {'PASS' if condition else 'FAIL'} - {text}")
    if not condition:
        failures.append(text)



def _control_state_machine_checks(failures: list) -> None:
    """纯逻辑校验：控制规则状态机（每日上限 / 连续暂停 / 休息 / 临时解锁 / 优先级）。"""
    from .control import (REASON_DAILY, REASON_STREAK, ControlEngine, Rule)

    base = 1_700_000_000.0
    engine = ControlEngine(enabled=True)
    rule = Rule(program_id=1, daily_enabled=True, daily_value=10,
                streak_enabled=True, streak_value=30, pause_value=20)
    engine.set_rule(rule)

    # 每日上限：跑满 10 分钟即触发，且只记一次日志
    events = []
    for step in range(11 * 60):
        events += engine.tick({1}, now=base + step, delta=1.0)
    decision = engine.decide(1, now=base + 11 * 60)
    daily_hits = [e for e in events if e.kind == "daily_limit"]
    _check(engine.state(1).used_seconds >= 600 and decision.blocked
           and decision.reason == REASON_DAILY and len(daily_hits) == 1,
           "达到每日上限后触发限制并只记录一次控制日志", failures)

    # 优先级：临时解锁 > 每日上限
    _check(engine.unlock(1, 5, now=base + 11 * 60), "临时解锁成功", failures)
    decision = engine.decide(1, now=base + 11 * 60 + 10)
    _check(decision.unlocked and not decision.blocked,
           "临时解锁优先级高于每日上限", failures)
    _check(not engine.unlock(1, 5, now=base + 11 * 60 + 20),
           "本次临时解锁结束前不允许再次延长", failures)

    # 解锁结束后立即重新检查：仍超限 -> 立即重新遮罩
    after = base + 11 * 60 + 5 * 60 + 5
    decision = engine.decide(1, now=after)
    _check(decision.blocked and decision.reason == REASON_DAILY,
           "临时解锁结束后重新检查，仍超限则立即恢复限制", failures)

    # 临时解锁期间整段停表：不累计任何时长，也不产生新的"达到上限 / 强制暂停"事件。
    # 回归的是"临时解锁后每 X 秒又被限制一次并弹强制停止通知"的问题。
    engine5 = ControlEngine(enabled=True)
    engine5.set_rule(Rule(program_id=5, daily_enabled=True, daily_value=2,
                          streak_enabled=True, streak_value=1, pause_value=10))
    for step in range(30):                          # 先累计 30 秒（连续阈值 60 秒）
        engine5.tick({5}, now=base + step, delta=1.0)
    engine5.unlock(5, 1, now=base + 30)             # 临时解锁 1 分钟
    used_before = engine5.state(5).used_seconds
    streak_before = engine5.state(5).streak_seconds
    unlocked_events = []
    for step in range(55):                          # 解锁窗口内再跑 55 秒
        unlocked_events += engine5.tick({5}, now=base + 30 + step, delta=1.0)
    state = engine5.state(5)
    _check(state.used_seconds == used_before and state.streak_seconds == streak_before == 0.0
           and state.pause_until == 0.0 and not unlocked_events,
           "临时解锁期间不计时，也不会反复触发强制暂停/上限事件", failures)
    engine5.tick({5}, now=base + 300, delta=1.0)
    _check(engine5.state(5).used_seconds > used_before,
           "临时解锁结束后恢复计时", failures)

    # 连续使用：X=30 分钟触发，暂停时长 = Y - 已累计休息
    engine2 = ControlEngine(enabled=True)
    engine2.set_rule(Rule(program_id=2, streak_enabled=True, streak_value=30,
                          pause_value=20))
    start = base
    for step in range(30 * 60):
        engine2.tick({2}, now=start + step, delta=1.0)
    state = engine2.state(2)
    _check(state.pause_until > start + 30 * 60,
           "连续使用达到阈值 X 后触发强制暂停", failures)

    # 强制暂停期间不计入连续累计
    pause_start = start + 30 * 60
    streak_before = engine2.state(2).streak_seconds
    for step in range(60):
        engine2.tick({2}, now=pause_start + step, delta=1.0)
    _check(abs(engine2.state(2).streak_seconds - streak_before) < 1.0,
           "强制暂停期间不增加连续使用累计", failures)

    # 暂停期间临时解锁：清除暂停并清零连续累计
    engine3 = ControlEngine(enabled=True)
    engine3.set_rule(Rule(program_id=3, streak_enabled=True, streak_value=10,
                          pause_value=20))
    for step in range(10 * 60):
        engine3.tick({3}, now=base + step, delta=1.0)
    _check(engine3.state(3).pause_until > base + 600, "达到阈值后进入强制暂停", failures)
    engine3.unlock(3, 5, now=base + 610)
    _check(engine3.state(3).pause_until == 0.0 and engine3.state(3).streak_seconds == 0.0,
           "强制暂停期间临时解锁会清除暂停并把连续累计清零", failures)

    # 累计未达 X 时退出 -> 累计休息；休息 >= Y 则重置连续累计
    engine4 = ControlEngine(enabled=True)
    engine4.set_rule(Rule(program_id=4, streak_enabled=True, streak_value=10,
                          pause_value=5))
    for step in range(4 * 60):
        engine4.tick({4}, now=base + step, delta=1.0)
    for step in range(5 * 60 + 5):
        engine4.tick(set(), now=base + 240 + step, delta=1.0)
    rest = engine4.state(4).rest_seconds
    engine4.tick({4}, now=base + 240 + 5 * 60 + 10, delta=1.0)
    _check(rest >= 300 and engine4.state(4).streak_seconds <= 1.0
           and engine4.state(4).rest_seconds == 0.0,
           "退出时累计休息，休息满 Y 后重新打开会重置连续累计", failures)

    # 休息不足 Y：保留连续累计与已休息时间，暂停时长 = Y - 已休息
    engine5 = ControlEngine(enabled=True)
    engine5.set_rule(Rule(program_id=5, streak_enabled=True, streak_value=10,
                          pause_value=10))
    for step in range(10 * 60 - 60):
        engine5.tick({5}, now=base + step, delta=1.0)
    engine5.tick(set(), now=base + 600, delta=1.0)
    for step in range(4 * 60):
        engine5.tick(set(), now=base + 601 + step, delta=1.0)
    engine5.tick({5}, now=base + 601 + 4 * 60 + 1, delta=1.0)
    kept = engine5.state(5).streak_seconds
    for step in range(60):
        engine5.tick({5}, now=base + 601 + 4 * 60 + 2 + step, delta=1.0)
    pause_left = engine5.state(5).pause_until - (base + 601 + 4 * 60 + 61)
    _check(kept > 0 and 330 <= pause_left <= 400,
           "休息不足 Y 时保留连续累计，暂停时长 = Y - 已累计休息", failures)

    # 强制暂停走完：必须真正解除，不能同一 tick 又开一轮（否则永远解不开）
    engine7 = ControlEngine(enabled=True)
    engine7.set_rule(Rule(program_id=7, streak_enabled=True, streak_value=5,
                          pause_value=5))
    cycle_starts: list[float] = []
    cycle_ends: list[float] = []
    now = base
    for _step in range(3600):
        engine7.tick({7}, now=now, delta=1.0)
        pause_until = engine7.state(7).pause_until
        if pause_until > 0 and (not cycle_ends or abs(cycle_ends[-1] - pause_until) > 0.5):
            cycle_starts.append(now)
            cycle_ends.append(pause_until)
        now += 1.0
    first_pause_end = cycle_ends[0] if cycle_ends else 0.0
    pause_len = (cycle_ends[0] - cycle_starts[0]) if cycle_ends else 0.0
    # 单独走一遍"暂停刚好结束"的时刻：解除后 60 秒内不能又开一轮
    engine7b = ControlEngine(enabled=True)
    engine7b.set_rule(Rule(program_id=7, streak_enabled=True, streak_value=5,
                           pause_value=5))
    mark = base
    for _step in range(5 * 60 + 1):
        engine7b.tick({7}, now=mark, delta=1.0)
        mark += 1.0
    until = engine7b.state(7).pause_until
    engine7b.tick({7}, now=until + 1, delta=1.0)
    released = (engine7b.state(7).pause_until == 0.0
                and engine7b.state(7).streak_seconds <= 1.0)
    re_paused = False
    for _step in range(60):
        engine7b.tick({7}, now=mark, delta=1.0)
        if engine7b.state(7).pause_until > 0:
            re_paused = True
        mark += 1.0
    gaps = [round(b - a) for a, b in zip(cycle_ends, cycle_starts[1:])]
    still_free = all(gap >= 60 for gap in gaps)
    pause_lens = [round(b - a) for a, b in zip(cycle_starts, cycle_ends)]
    print(f"[selftest] 暂停循环：次数={len(cycle_starts)} 每次时长={pause_lens} 解除={released} "
          f"解除后60秒内又暂停={re_paused} 两次暂停间隔={gaps}（期望 >=60 秒）")
    _check(pause_len >= 295 and released and not re_paused and still_free
           and len(cycle_starts) >= 3
           and all(295 <= item <= 305 for item in pause_lens),
           "强制暂停倒计时结束后不会立刻再次进入强制暂停", failures)

    # 每日 0 点重置
    engine6 = ControlEngine(enabled=True)
    engine6.set_rule(Rule(program_id=6, daily_enabled=True, daily_value=5))
    day_one = base
    for step in range(6 * 60):
        engine6.tick({6}, now=day_one + step, delta=1.0)
    _check(engine6.state(6).used_seconds >= 300, "每日累计计时", failures)
    engine6.tick({6}, now=day_one + 86400 + 5, delta=1.0)
    _check(engine6.state(6).used_seconds < 5,
           "每日累计在次日 0 点重置", failures)

    # 单位换算：秒 / 分钟 / 小时（未知单位按分钟处理）
    from .control import UNIT_MAX, unit_seconds
    from .i18n import format_amount

    unit_cases = [
        (Rule(program_id=20, daily_enabled=True, daily_value=90, daily_unit="s"), 90.0, "90 秒"),
        (Rule(program_id=21, daily_enabled=True, daily_value=3, daily_unit="m"), 180.0, "3 分钟"),
        (Rule(program_id=22, daily_enabled=True, daily_value=2, daily_unit="h"), 7200.0, "2 小时"),
        (Rule(program_id=23, daily_enabled=True, daily_value=5, daily_unit="?"), 300.0, "5 分钟"),
    ]
    unit_ok = True
    for item, expected, label in unit_cases:
        got = item.daily_seconds
        text = format_amount(item.daily_value, item.daily_unit)
        unit_ok = unit_ok and abs(got - expected) < 1e-6 and text == label
    unit_ok = (unit_ok and unit_seconds("h") == 3600 and UNIT_MAX["h"] == 24
               and UNIT_MAX["s"] == 86400)
    print(f"[selftest] 规则单位：秒={unit_cases[0][0].daily_seconds} "
          f"分钟={unit_cases[1][0].daily_seconds} 小时={unit_cases[2][0].daily_seconds} "
          f"文案={[format_amount(r.daily_value, r.daily_unit) for r, _e, _l in unit_cases]} "
          f"上限={UNIT_MAX}")
    _check(unit_ok, "规则单位支持 秒/分钟/小时，并按单位换算为秒与本地化文案", failures)

    # 关闭规则必须重置该程序的计时与遮罩状态（否则关掉再打开会立刻又被遮罩）
    engine10 = ControlEngine(enabled=True)
    engine10.set_rule(Rule(program_id=10, daily_enabled=True, daily_value=10))
    for step in range(5 * 60):
        engine10.tick({10}, now=base + step, delta=1.0)
    used_before = engine10.state(10).used_seconds
    engine10.set_rule(Rule(program_id=10, daily_enabled=False, daily_value=10))
    daily_cleared = engine10.state(10).used_seconds == 0.0
    for step in range(120):
        engine10.tick({10}, now=base + 5 * 60 + step, delta=1.0)
    daily_idle = engine10.state(10).used_seconds == 0.0
    engine10.set_rule(Rule(program_id=10, daily_enabled=True, daily_value=10))
    engine10.tick({10}, now=base + 5 * 60 + 200, delta=1.0)
    daily_fresh = not engine10.decide(10, now=base + 5 * 60 + 200).blocked

    engine11 = ControlEngine(enabled=True)
    engine11.set_rule(Rule(program_id=11, streak_enabled=True, streak_value=5, pause_value=5))
    for step in range(5 * 60 + 2):
        engine11.tick({11}, now=base + step, delta=1.0)
    was_paused = engine11.state(11).pause_until > 0
    engine11.set_rule(Rule(program_id=11, streak_enabled=False, streak_value=5, pause_value=5))
    streak_cleared = (engine11.state(11).pause_until == 0.0
                      and engine11.state(11).streak_seconds == 0.0
                      and engine11.state(11).rest_seconds == 0.0)
    engine11.unlock(11, 5, now=base + 400)
    engine11.set_rule(Rule(program_id=11, streak_enabled=False, streak_value=5, pause_value=5))
    unlock_cleared = (engine11.state(11).unlock_until == 0.0
                      and not engine11.decide(11, now=base + 401).blocked)
    print(f"[selftest] 关闭规则重置：每日已用 {used_before:.0f}s -> 关闭后 "
          f"{'0' if daily_cleared else '未清零'} 关闭期间不累计={daily_idle} "
          f"重开不立即受限={daily_fresh} 暂停期间曾被暂停={was_paused} "
          f"连续/暂停清零={streak_cleared} 解锁清零={unlock_cleared}")
    _check(daily_cleared and daily_idle and daily_fresh and was_paused and streak_cleared
           and unlock_cleared,
           "关闭规则会清零该程序的计时/暂停/解锁状态，重开不会立即再次被遮罩", failures)

    # ---------------------------------------------------------------
    # 指定时间段（开始/结束 + 星期 + 跨午夜）
    # ---------------------------------------------------------------
    from .control import (ALL_DAYS, REASON_WINDOW, Window, day_start, format_minute,
                          weekday_index, windows_from_rows, windows_to_rows)

    noon = day_start(base) + 10 * 3600 + 30 * 60        # 当天 10:30（本地时间）
    today = weekday_index(noon)
    tomorrow = (today + 1) % 7
    yesterday = (today - 1) % 7

    inside = Window(9 * 60, 12 * 60, ALL_DAYS)     # 09:00-12:00 每天
    outside = Window(12 * 60, 15 * 60, ALL_DAYS)   # 12:00-15:00 每天
    day_only = Window(9 * 60, 12 * 60, 1 << today)
    other_day = Window(9 * 60, 12 * 60, 1 << tomorrow)
    night = Window(22 * 60, 7 * 60, 1 << today)    # 22:00-次日 07:00（跨午夜）
    all_day = Window(0, 0, 1 << today)             # 起=止 → 一整天

    next_night = day_start(noon) + 86400 + 3 * 3600        # 次日 03:00
    next_morning = day_start(noon) + 86400 + 8 * 3600      # 次日 08:00
    range_ok = (inside.contains(noon) and not outside.contains(noon)
                and abs(inside.seconds_until_end(noon) - 90 * 60) < 1
                and day_only.contains(noon) and not other_day.contains(noon)
                and not night.contains(noon) and night.contains(next_night)
                and abs(night.seconds_until_end(next_night) - 4 * 3600) < 1
                and not night.contains(next_morning)
                and all_day.contains(noon) and all_day.contains(noon + 3600)
                and not Window(0, 0, 1 << tomorrow).contains(noon)
                and inside.crosses_midnight is False and night.crosses_midnight is True
                and all_day.all_day and format_minute(570) == "09:30")
    print(f"[selftest] 指定时间段：命中={inside.contains(noon)} 剩余="
          f"{inside.seconds_until_end(noon):.0f}s 星期筛选={day_only.contains(noon)}/"
          f"{other_day.contains(noon)} 跨午夜(次日3点)={night.contains(next_night)} "
          f"剩余={night.seconds_until_end(next_night):.0f}s 全天={all_day.contains(noon)}")
    _check(range_ok, "指定时间段支持起止时间、按星期筛选、跨午夜与全天", failures)

    rows_ok = windows_to_rows([inside, night]) == [[540, 720, ALL_DAYS],
                                                   [1320, 420, 1 << today]]
    back = windows_from_rows(windows_to_rows([inside, night]))
    rows_ok = rows_ok and len(back) == 2 and back[1].start_minute == 1320
    rows_ok = rows_ok and windows_from_rows([[1, 2], None, "x", [0, 0, 1 << today]]) \
        == [Window(0, 0, 1 << today)]
    _check(rows_ok, "时间段的数据库序列化只接受合法行，坏数据被丢弃", failures)

    # 优先级：临时解锁 > 指定时间段 > 每日上限 > 连续暂停
    engine_w = ControlEngine(enabled=True)
    engine_w.set_rule(Rule(program_id=30, window_enabled=True, windows=[inside],
                           daily_enabled=True, daily_value=1,
                           streak_enabled=True, streak_value=5, pause_value=5))
    decision = engine_w.decide(30, now=noon)
    window_first = decision.blocked and decision.reason == REASON_WINDOW
    engine_w.tick({30}, now=noon, delta=1.0)
    idle_used = engine_w.state(30).used_seconds == 0.0
    idle_streak = engine_w.state(30).streak_seconds == 0.0
    engine_w.unlock(30, 5, now=noon)
    unlocked_first = not engine_w.decide(30, now=noon).blocked
    # 时段内不计时：即使程序一直开着，也不会因为"跑到上限"而改变优先级
    events = []
    for step in range(60):
        events += engine_w.tick({30}, now=noon + step, delta=1.0)
    still_idle = engine_w.state(30).used_seconds == 0.0
    print(f"[selftest] 时段优先级：时段>每日={window_first} 时段内不计时="
          f"{idle_used and idle_streak and still_idle} 临时解锁>时段={unlocked_first}")
    _check(window_first and idle_used and idle_streak and unlocked_first and still_idle,
           "优先级 临时解锁 > 指定时间段 > 每日上限 > 连续暂停，且时段内不计时", failures)

    # 进入 / 离开各记一次事件，离开后恢复计时且连续累计从头开始
    engine_w2 = ControlEngine(enabled=True)
    engine_w2.set_rule(Rule(program_id=31, window_enabled=True, windows=[inside],
                            daily_enabled=True, daily_value=999,
                            streak_enabled=True, streak_value=600, pause_value=5))
    start = day_start(base) + 9 * 3600              # 09:00 整，时段刚开始
    engine_w2.tick({31}, now=start - 60, delta=1.0)          # 时段前 1 分钟正常计时
    before_used = engine_w2.state(31).used_seconds
    entry_events = []
    for step in range(120):
        entry_events += engine_w2.tick({31}, now=start + step, delta=1.0)
    in_window_used = engine_w2.state(31).used_seconds
    leave_events = engine_w2.tick({31}, now=start + 3 * 3600 + 1, delta=1.0)
    after_used = engine_w2.state(31).used_seconds
    window_events = [e for e in entry_events + leave_events if e.kind == "window"]
    print(f"[selftest] 时段计时：进入前={before_used:.0f}s 时段内={in_window_used:.0f}s "
          f"离开后={after_used:.0f}s 时段事件={len(window_events)} "
          f"离开时连续累计={engine_w2.state(31).streak_seconds:.0f}s（期望 <=1）")
    _check(in_window_used == before_used and len(window_events) == 1
           and after_used > before_used
           and engine_w2.state(31).streak_seconds <= 1.0,
           "进入时段不计时并记一次日志，离开时段恢复计时且连续累计清零", failures)

    # 时段内开着的程序，时段结束时不能立刻触发强制暂停（否则用户刚能用又被暂停）
    engine_w3 = ControlEngine(enabled=True)
    engine_w3.set_rule(Rule(program_id=32, window_enabled=True, windows=[inside],
                            streak_enabled=True, streak_value=60, pause_value=600))
    engine_w3.tick({32}, now=start - 6, delta=1.0)
    for step in range(4):
        engine_w3.tick({32}, now=start - 5 + step, delta=1.0)   # 时段前累计 4 秒
    for step in range(3 * 3600):                                 # 时段内一直被遮罩
        engine_w3.tick({32}, now=start + step, delta=1.0)
    engine_w3.tick({32}, now=start + 3 * 3600 + 1, delta=1.0)    # 刚过时段
    no_instant_pause = engine_w3.state(32).pause_until == 0.0
    engine_w3.tick({32}, now=start + 3 * 3600 + 2, delta=1.0)
    _check(no_instant_pause and engine_w3.state(32).streak_seconds <= 2.0,
           "时段结束后连续累计清零，不会立刻又被强制暂停", failures)

    # 遮罩出现时的卡顿：低级钩子跑在系统输入路径上，状态查询必须带缓存
    from . import audio as audio_module
    from .input_hook import InputBlocker

    hook_calls = {"n": 0}

    def _hook_provider():
        hook_calls["n"] += 1
        return {11, 22}, {33}

    blocker = InputBlocker(_hook_provider)
    first_state = blocker._state()
    for _ in range(25):
        blocker._state()
    cached_ok = hook_calls["n"] == 1 and first_state == ({11, 22}, {33})
    blocker.invalidate()
    blocker._state()
    invalidate_ok = hook_calls["n"] == 2
    print(f"[selftest] 输入钩子缓存：provider 调用次数={hook_calls['n']} "
          f"复用={cached_ok} 失效后可刷新={invalidate_ok}")
    _check(cached_ok and invalidate_ok,
           "输入钩子复用进程快照缓存（不在每次鼠标事件里重查）", failures)

    enum_calls = {"n": 0}
    original_enum = audio_module._enumerate_sessions
    audio_module._enumerate_sessions = \
        lambda: (enum_calls.__setitem__("n", enum_calls["n"] + 1) or [])
    try:
        muter = audio_module.ProcessMuter()
        muter.available = True
        muter.mute({7})
        first_enum = enum_calls["n"]
        for _ in range(8):
            muter.mute({7})
        throttled_enum = enum_calls["n"]
        muter.mute({7}, force=True)
        forced_enum = enum_calls["n"]
    finally:
        audio_module._enumerate_sessions = original_enum
    print(f"[selftest] 音频枚举节流：首次={first_enum} 连续 8 次后={throttled_enum} "
          f"force 后={forced_enum}")
    _check(first_enum == 1 and throttled_enum == 1 and forced_enum == 2,
           "音频静音不再每次同步都枚举会话（force 时才立即重新枚举）", failures)


_AUDIO_CHILD = r'''
import math, struct, sys, time, wave, winsound
path = sys.argv[1]
rate = 44100
with wave.open(path, "wb") as handle:
    handle.setnchannels(1)
    handle.setsampwidth(2)
    handle.setframerate(rate)
    frames = bytearray()
    for index in range(rate):
        frames += struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440.0 * index / rate)))
    handle.writeframes(bytes(frames))
winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
print("playing", flush=True)
time.sleep(30)
'''


def _read_session_state(pid: int) -> list:
    """读回某进程所有音频会话的 (音量, 是否静音)。"""
    import ctypes

    from . import audio

    found = []
    for session_pid, volume, _muted, _level, _state in audio._enumerate_sessions():
        if session_pid != pid:
            audio._release(volume)
            continue
        muted = ctypes.c_int(0)
        audio._vt(volume, 6, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
            volume, ctypes.byref(muted))
        level = ctypes.c_float(-1.0)
        audio._vt(volume, 4, ctypes.c_long, ctypes.POINTER(ctypes.c_float))(
            volume, ctypes.byref(level))
        found.append((round(float(level.value), 3), bool(muted.value)))
        audio._release(volume)
    return found


def _force_session_state(pid: int, level: float, muted: bool) -> int:
    """把某进程的音频会话直接设成给定状态。

    用来模拟"程序重建会话之后音量又是 0% + 静音"（切场景、重新初始化音频，
    或者上一次 Monitor 没来得及还原就被关掉）。
    """
    import ctypes

    from . import audio

    hits = 0
    for session_pid, volume, _muted, _level, _state in audio._enumerate_sessions():
        if session_pid == pid:
            session = audio._Session(volume, False, int(session_pid), None)
            session.set_volume(level)
            session.set_mute(muted)
            hits += 1
        audio._release(volume)
    return hits


class _AudioStore:
    """ProcessMuter 的持久存储桩（真实实现是 ControlService 读写数据库）。"""

    def __init__(self, data=None):
        self.data = dict(data or {})

    def load_audio_originals(self):
        return dict(self.data)

    def save_audio_originals(self, data):
        self.data = dict(data)


def _audio_end_to_end() -> dict:
    """真实端到端：播放声音的子进程 -> 静音 -> 读回 0% -> 还原原音量。

    顺带覆盖"解除遮罩时未恢复原音量"的四条回归：
      A 接管之前会话就已经是 0% + 静音（上一次没还原 / 系统按应用记住的 0%）；
      B ``mute(set())``（遮罩撤销但查不到运行中的进程）也必须还原；
      C 同一个程序重建会话时沿用记忆里的原始音量，不被 0% 覆盖；
      D ``recover()`` 启动时把上次没来得及还原的音量放回去。

    没有音频输出设备（枚举不到会话）时视为跳过，不算失败。
    """
    import os
    import subprocess
    import sys
    import tempfile
    import time

    from . import audio

    child = None
    detail: dict = {}
    wav_path = os.path.join(tempfile.gettempdir(), "_monitor_selftest_tone.wav")
    if getattr(sys, "frozen", False):
        # 打包成 exe 之后没有解释器可以拉起"会发声的子进程"（子进程命令是
        # sys.executable -c，冻结后那就是 Monitor.exe 自己），这一项只能跳过。
        return {"ok": True,
                "detail": "打包成 exe 后无法拉起 python 子进程发声，本项跳过"}
    try:
        child = subprocess.Popen([sys.executable, "-c", _AUDIO_CHILD, wav_path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.6)
        before = _read_session_state(child.pid)
        if not before:
            return {"ok": True,
                    "detail": f"pid={child.pid} 枚举不到音频会话（本机可能没有输出设备），"
                              f"已跳过 LAST_ERROR={audio.LAST_ERROR!r}"}
        muter = audio.ProcessMuter()
        added = muter.mute({child.pid}, force=True)
        time.sleep(0.35)
        during = _read_session_state(child.pid)
        muter.restore()
        time.sleep(0.35)
        after = _read_session_state(child.pid)
        detail["基础"] = (added >= 1 and before[0][0] > 0.5 and bool(during)
                          and during[0][0] == 0.0 and during[0][1] and bool(after)
                          and after[0][0] > 0.5)

        # A：接管之前就已经是 0% + 静音 —— 0% 不能当成"原始音量"
        _force_session_state(child.pid, 0.0, True)
        time.sleep(0.2)
        muter_a = audio.ProcessMuter()
        muter_a.mute({child.pid}, force=True)
        time.sleep(0.2)
        muter_a.restore()
        time.sleep(0.3)
        state_a = _read_session_state(child.pid)
        detail["接管前已静音"] = bool(state_a) and state_a[0][0] > 0.5 and not state_a[0][1]

        # B：目标集合为空（遮罩撤销但查不到进程）也要还原
        _force_session_state(child.pid, 1.0, False)
        time.sleep(0.2)
        muter_b = audio.ProcessMuter()
        muter_b.mute({child.pid}, force=True)
        time.sleep(0.2)
        muted_b = _read_session_state(child.pid)
        muter_b.mute(set())
        time.sleep(0.3)
        state_b = _read_session_state(child.pid)
        detail["空集合还原"] = (bool(muted_b) and muted_b[0][0] == 0.0
                                and bool(state_b) and state_b[0][0] > 0.5)

        # C：记忆里的原始音量优先于"当前 0%"
        path = audio.ProcessMuter._path_for_pid(child.pid)
        _force_session_state(child.pid, 0.0, True)
        time.sleep(0.2)
        store_c = _AudioStore({path: [0.8, False]} if path else {})
        muter_c = audio.ProcessMuter(store_c)
        muter_c.mute({child.pid}, force=True)
        time.sleep(0.2)
        muter_c.restore()
        time.sleep(0.3)
        state_c = _read_session_state(child.pid)
        detail["记忆优先"] = (bool(path) and bool(state_c)
                              and abs(state_c[0][0] - 0.8) < 0.02 and not state_c[0][1])

        # D：recover() 启动兜底
        _force_session_state(child.pid, 0.0, True)
        time.sleep(0.2)
        store_d = _AudioStore({path: [0.8, False]} if path else {})
        healed = audio.ProcessMuter(store_d).recover()
        time.sleep(0.3)
        state_d = _read_session_state(child.pid)
        detail["启动兜底"] = (bool(path) and healed >= 1 and bool(state_d)
                              and abs(state_d[0][0] - 0.8) < 0.02 and not state_d[0][1]
                              and store_d.data == {})

        ok = all(detail.values())
        return {"ok": bool(ok),
                "detail": f"pid={child.pid} 前={before} 静音后={during} 还原后={after} "
                          f"新增会话={added} 细节={detail} "
                          f"结论={'正确' if ok else '异常'}"}
    except Exception as error:                       # pragma: no cover - 防御性
        return {"ok": False, "detail": f"异常：{error!r} 细节={detail}"}
    finally:
        if child is not None:
            try:
                child.kill()
            except Exception:
                pass
            try:
                child.stdout.close()
            except Exception:
                pass
        try:
            if os.path.exists(wav_path):
                os.remove(wav_path)
        except OSError:
            pass


def _control_page_checks(app, window, db, failures: list, shots: str) -> None:
    """控制页 / 通知页 / 日志分类 / 页脚的新功能校验（含 UI 交互）。"""
    from PySide6.QtWidgets import QDialog, QMessageBox

    from .control import Rule
    from .notify import Notifier, normalize
    from .overlay import OverlayManager
    from .ui import control_page as control_page_module
    from . import audio as audio_module

    page = window.control_page
    service = window.control
    print(f"[selftest] 导航项数量={len(window.nav.buttons)} "
          f"标题={[b.text() for b in window.nav.buttons]}")
    _check(len(window.nav.buttons) == 5
           and window.nav.buttons[3].text() == tr("nav.control"),
           "左侧边栏新增“控制”功能页选项", failures)

    # 页脚 [N]/[sum]
    programs = [dict(p) for p in db.programs(visible_only=True)]
    tracked = sum(1 for p in programs if int(p["tracking"] or 0))
    expected = tr("nav.footer", tracked=tracked, total=len(programs))
    print(f"[selftest] 页脚='{window.nav.footer.text()}' 期望='{expected}'")
    _check(window.nav.footer.text() == expected,
           "页脚显示 正在追踪 [开启数]/[总程序数]", failures)

    # 提权目标：控制页在非管理员运行时给出"以管理员身份重启"入口
    from . import util as util_module
    admin_row = getattr(page, "admin_row", None)
    admin_state = bool(util_module.is_admin())
    admin_hint_ok = (admin_row is not None
                     and admin_row.isHidden() == admin_state
                     and callable(getattr(util_module, "restart_as_admin", None)))
    print(f"[selftest] 管理员入口：当前管理员={admin_state} "
          f"提示行隐藏={None if admin_row is None else admin_row.isHidden()} "
          f"重启函数={callable(getattr(util_module, 'restart_as_admin', None))}")
    _check(admin_hint_ok,
           "非管理员运行时控制页提供“以管理员身份重启”（管理员时不显示）", failures)

    # 功能：只有管理员身份才允许打开控制页总开关（未提权时开关回弹并给出提示）
    real_question = QMessageBox.question
    real_admin = util_module.is_admin
    gate_asked = {"n": 0}
    was_enabled = bool(service.enabled())
    service.set_enabled(False)

    def _decline(*_args, **_kwargs):
        gate_asked["n"] += 1
        return QMessageBox.StandardButton.No

    util_module.is_admin = lambda: False
    QMessageBox.question = staticmethod(_decline)
    try:
        page.enable_switch.setChecked(False, animate=False, emit=False)
        page._on_enable_toggled(True)
        gate_blocked = (not service.enabled() and not page.enable_switch.isChecked()
                        and gate_asked["n"] == 1)
    finally:
        util_module.is_admin = real_admin
        QMessageBox.question = real_question
    was_unlocked = page._unlocked
    real_has_password = service.has_password
    util_module.is_admin = lambda: True
    try:
        # refresh() 只在"已解锁 + 已设密码"时才把总开关状态回写到开关上，
        # 这一段校验的门禁发生在密码校验之前，因此把密码检查打桩隔离（只动内存，不写库）。
        service.has_password = lambda: True
        page._unlocked = True
        page.enable_switch.setChecked(False, animate=False, emit=False)
        page._on_enable_toggled(True)
        gate_allowed = bool(service.enabled()) and page.enable_switch.isChecked()
        gate_state = (service.enabled(), page.enable_switch.isChecked())
    finally:
        service.has_password = real_has_password
        page._unlocked = was_unlocked
        util_module.is_admin = real_admin
    print(f"[selftest] 控制总开关管理员门禁：非管理员被拦={gate_blocked} "
          f"提示次数={gate_asked['n']} 管理员可开={gate_allowed} 状态={gate_state}")
    _check(gate_blocked and gate_allowed,
           "“启用控制”总开关需要管理员身份（未提权时回弹并提示以管理员身份重启）", failures)
    service.set_enabled(was_enabled)
    page.refresh()

    # 功能：设置页第三项"以管理员身份启动"（登录计划任务 + 管理员门禁）
    from . import autostart as autostart_module

    settings = window.settings_page
    applied = []

    def _record_apply(enabled, silent=False, admin=False):
        applied.append((bool(enabled), bool(silent), bool(admin)))
        return True

    real_apply = autostart_module.apply
    settings_autostart_state = settings.autostart_switch.isChecked()
    try:
        autostart_module.apply = _record_apply
        settings.admin_switch.setChecked(False, animate=False, emit=False)
        util_module.is_admin = lambda: False
        QMessageBox.question = staticmethod(_decline)
        settings._on_admin_toggled(True)       # 非管理员：回弹，不写计划任务
        admin_gate_ok = (not settings.admin_switch.isChecked()
                         and not db.get_bool(config.S_ADMIN_START, False))
        util_module.is_admin = real_admin
        settings.autostart_switch.setChecked(True, animate=False, emit=False)
        settings.admin_switch.setChecked(True, animate=False, emit=False)
        settings._on_autostart_toggled(False)  # 关闭自启动：管理员自启动同时失效
        cascade_ok = (not settings.admin_switch.isChecked() and applied
                      and applied[-1] == (False, False, False))
    finally:
        autostart_module.apply = real_apply
        util_module.is_admin = real_admin
        QMessageBox.question = real_question
        settings.autostart_switch.setChecked(settings_autostart_state, animate=False,
                                             emit=False)
    mode_ok = (autostart_module.MODE_ADMIN == "admin"
               and autostart_module.mode() in (autostart_module.MODE_NONE,
                                               autostart_module.MODE_RUN,
                                               autostart_module.MODE_ADMIN))
    print(f"[selftest] 管理员自启动：开关存在={settings.admin_switch is not None} "
          f"非管理员回弹={admin_gate_ok} 关闭自启动时级联关闭={cascade_ok} "
          f"当前方式={autostart_module.mode()!r} 调用记录={applied}")
    _check(admin_gate_ok and cascade_ok and mode_ok,
           "设置页新增“以管理员身份启动”（计划任务 /RL HIGHEST，需管理员权限，随自启动级联）",
           failures)

    # 密码：设置 -> 错误 3 次锁定 -> 正确通过 -> 会话内免验证
    db.set_setting("control.password", "")
    db.set_setting("control.fail_count", 0)
    db.set_setting("control.lock_until", 0)
    service._verified_at = 0.0
    _check(not service.has_password(), "首次进入控制页时处于未设置密码状态", failures)
    service.set_password("test1234")
    _check(service.has_password() and service.session_valid(),
           "可以设置控制页密码并在会话内免重复验证", failures)
    ok_wrong, _reason = service.verify("bad")
    service.verify("bad")
    ok_third, reason_third = service.verify("bad")
    locked = service.lock_remaining() > 0
    ok_while_locked, reason_locked = service.verify("test1234")
    print(f"[selftest] 密码：错误={ok_wrong} 第3次={ok_third}/{reason_third} "
          f"锁定={locked} 锁定期内正确密码={ok_while_locked}/{reason_locked}")
    _check(not ok_wrong and locked and reason_third == "just_locked"
           and not ok_while_locked and reason_locked == "locked",
           "连续 3 次密码错误后锁定 1 分钟（锁定期内正确密码也不放行）", failures)
    db.set_setting("control.lock_until", 0)
    ok_right, _r = service.verify("test1234")
    _check(ok_right, "锁定解除后正确密码可通过验证", failures)

    # 临时解锁弹窗：空密码反复确认不能消耗校验次数（否则 3 次就锁定，表现为"按钮失灵"）
    from .ui.control_page import _PasswordDialog
    db.set_setting("control.fail_count", 0)
    db.set_setting("control.lock_until", 0)
    unlock_dialog = _PasswordDialog(window, tr("overlay.unlock"), tr("control.verify.hint"),
                                    minutes=True)
    unlock_dialog._accept_minutes(45)
    preset_only = unlock_dialog.value_minutes == 45 and unlock_dialog.result() == 0
    for _ in range(4):
        unlock_dialog._on_accept()
    empty_guarded = (unlock_dialog.result() == 0
                     and int(db.get_float("control.fail_count", 0.0)) == 0
                     and service.lock_remaining() == 0.0)
    unlock_dialog.edit.setText("test1234")
    unlock_dialog._on_accept()
    filled_ok = (unlock_dialog.result() == QDialog.Accepted
                 and unlock_dialog.value_minutes == 45)
    print(f"[selftest] 临时解锁弹窗：预设只填值={preset_only} "
          f"空密码不消耗次数={empty_guarded} 填密码后提交={filled_ok}")
    _check(preset_only and empty_guarded and filled_ok,
           "临时解锁弹窗空密码时反复确认不消耗校验次数（不误触发锁定）", failures)
    unlock_dialog.deleteLater()

    # 规则 CRUD + 遮罩
    program_id = int(programs[0]["id"]) if programs else None
    if program_id is not None:
        service.set_enabled(True)
        service.save_rule(Rule(program_id=program_id, daily_enabled=True, daily_value=1,
                               streak_enabled=True, streak_value=2, pause_value=1))
        saved = db.control_rules(program_id)
        _check(saved is not None and int(saved["daily_value"]) == 1
               and int(saved["streak_value"]) == 2,
               "控制页可以保存程序的每日上限与连续暂停规则", failures)

        # 控制页 / 通知页 / 日志分类截图
        page._unlocked = True
        page.refresh()
        window.switch_page(3, animate=False)
        _pump(app, 0.5)
        _shot(window, os.path.join(shots, "17_control.png"))
        card = page._cards.get(program_id)
        if card is not None:
            # 规则卡默认收起（标题行常显），这里先展开再验证内部控件联动
            card.set_expanded(True, animate=False)
            _pump(app, 0.2)
            # 验证开关会联动输入框可用性
            card.daily_switch.setChecked(False, animate=False, emit=True)
            disabled = not card.daily_spin.isEnabled()
            card.daily_switch.setChecked(True, animate=False, emit=True)
            enabled = card.daily_spin.isEnabled()
            print(f"[selftest] 控制页规则卡：关闭每日上限后输入框禁用={disabled} "
                  f"重新开启后可用={enabled}")
            _check(disabled and enabled,
                   "控制页规则卡：开关联动输入框可用状态", failures)

            # 单位下拉：小时/分钟/秒；切换时"能整除就换算"，并按新单位收窄范围
            from .control import UNIT_MAX as _UNIT_MAX
            card.daily_spin.setValue(120)
            card._on_unit_changed(card.daily_spin, card.daily_unit, "h")
            card.daily_unit.set_current("h", emit=False)
            to_hour = (card.daily_spin.value() == 2
                       and card.daily_spin.maximum() == _UNIT_MAX["h"])
            card._on_unit_changed(card.daily_spin, card.daily_unit, "m")
            card.daily_unit.set_current("m", emit=False)
            to_minute = (card.daily_spin.value() == 120
                         and card.daily_spin.maximum() == _UNIT_MAX["m"])
            card._on_unit_changed(card.daily_spin, card.daily_unit, "s")
            card.daily_unit.set_current("s", emit=False)
            to_second = (card.daily_spin.value() == 7200
                         and card.daily_spin.maximum() == _UNIT_MAX["s"])
            card.daily_spin.setValue(90)
            card._on_unit_changed(card.daily_spin, card.daily_unit, "h")
            card.daily_unit.set_current("h", emit=False)
            clamped = card.daily_spin.value() == _UNIT_MAX["h"]
            unit_values = (card.daily_unit.current_key(), card.streak_unit.current_key(),
                           card.pause_unit.current_key())
            card.daily_unit.set_current("m", emit=False)
            card.daily_spin.setValue(1)
            card._on_unit_changed(card.daily_spin, card.daily_unit, "m")
            unit_labels = [box.label.text() for box in
                           (card.daily_unit, card.streak_unit, card.pause_unit)]
            print(f"[selftest] 单位下拉：120 分钟 -> {'2 小时' if to_hour else '未换算'} "
                  f"-> {card.daily_spin.value()} 分钟 -> {'7200 秒' if to_second else '未换算'} "
                  f"越界夹紧={clamped} 当前单位={unit_values} 文案={unit_labels}")
            _check(to_hour and to_minute and to_second and clamped,
                   "控制页规则单位可在 小时/分钟/秒 间切换并按单位换算数值与范围", failures)

            # 果冻开关：350ms 弹簧回弹（冲过终点再回弹）+ 滑动时横向拉长 + 底色灰->紫
            jelly = card.streak_switch
            jelly.setChecked(False, animate=False)
            jelly.setChecked(True, animate=True)
            stretch_seen = 0.0
            overshoot_seen = jelly._offset
            color_seen = [jelly._color_t]
            for _ in range(14):
                _pump(app, 0.03)
                stretch_seen = max(stretch_seen, jelly._stretch)
                overshoot_seen = max(overshoot_seen, jelly._offset)
                color_seen.append(jelly._color_t)
            jelly_duration = jelly.JELLY_MS
            _pump(app, 0.4)
            jelly_settled = (abs(jelly._offset - 1.0) < 1e-6 and abs(jelly._stretch) < 1e-6
                             and abs(jelly._color_t - 1.0) < 1e-6)
            jelly_grayscale = all(a <= b + 1e-6 for a, b in zip(color_seen, color_seen[1:]))
            print(f"[selftest] 果冻开关：时长={jelly_duration}ms 拉长峰值={stretch_seen:.3f} "
                  f"越过终点={overshoot_seen:.3f} 底色渐变单调={jelly_grayscale} "
                  f"最终归位={jelly_settled}")
            _check(jelly_duration == 350 and stretch_seen > 0.05 and overshoot_seen > 1.0
                   and jelly_grayscale and jelly_settled,
                   "程序开关为果冻动画（350ms、滑块横向拉长并冲过终点回弹、底色灰->紫)", failures)

            # 实时状态：未启用每日上限时不应显示"今日已用"
            rule = db.control_rules(program_id)
            if rule is not None:
                summary = service.state_summary(program_id)
                print(f"[selftest] 状态摘要：每日上限={summary['daily_limit']} "
                      f"已用={summary['used_seconds']} 连续上限={summary['streak_limit']}")
                _check(summary["daily_limit"] == 0 or int(rule["daily_enabled"]) == 1,
                       "未启用每日上限时状态摘要不显示每日额度", failures)
            has_tick_signal = hasattr(service, "ticked")
            service.ticked.emit()
            _pump(app, 0.2)
            text_after = _label_text(card.status)
            print(f"[selftest] 实时状态：ticked 信号={has_tick_signal} "
                  f"状态文字='{text_after[:40]}'")
            _check(has_tick_signal and bool(text_after),
                   "控制页状态文字随每秒 tick 实时刷新", failures)

            # 临时解锁：状态行必须显示解锁剩余时间（不然只有"临时解锁中"四个字，
            # 看不出还要等多久）
            engine_state = service.engine.state(program_id)
            # 余量给 3661.5 秒：format_duration 是向下取整，刷新时才恰好是 01:01:01
            engine_state.unlock_until = time.time() + 3661.5
            page._refresh_statuses()
            _pump(app, 0.2)
            unlock_text = _label_text(card.status)
            _check("临时解锁" in unlock_text and "01:01:01" in unlock_text,
                   "临时解锁时状态行显示解锁剩余时间", failures)
            _shot(window, os.path.join(shots, "17c_control_unlock.png"))
            engine_state.unlock_until = 0.0
            page._refresh_statuses()
            _pump(app, 0.2)
            print(f"[selftest] 解锁剩余时间：文字='{unlock_text}' "
                  f"解锁结束后='{_label_text(card.status)}'")
            _pump(app, 0.3)
            _shot(window, os.path.join(shots, "17b_control_edit.png"))

        # 规则卡只显示手动添加过的程序：刚 save_rule 的程序应当出现，未配置的不会出现
        configured = set(db.all_control_rules())
        visible_cards = set(page._cards)
        unconfigured = [int(p["id"]) for p in db.programs(visible_only=True)
                        if int(p["id"]) not in configured]
        print(f"[selftest] 控制页规则卡={sorted(visible_cards)} 已配置={sorted(configured)} "
              f"未配置程序数={len(unconfigured)}")
        _check(visible_cards == configured and bool(configured)
               and not (set(unconfigured) & visible_cards),
               "控制页只显示手动添加过规则的程序（不是自动列出全部程序）", failures)

        # 删除规则 = 播删除动画（与首页/日志一致）后移除卡片与数据库规则
        window.switch_page(3, animate=False)      # 卡片必须真的可见，动画才会播放
        page._unlocked = True
        page.refresh()
        _pump(app, 0.3)
        card_before = program_id in page._cards
        page._on_remove_card(program_id)
        animating = page._animating
        card_during = program_id in page._cards
        removed_card = page._cards.get(program_id)
        _pump(app, 0.6)
        card_after = program_id in page._cards
        rules_after = db.control_rules(program_id)
        try:
            card_hidden = removed_card is not None and not removed_card.isVisible()
        except RuntimeError:
            card_hidden = True          # 已被 deleteLater 回收，同样视为已移除
        leftover = page.list_layout.count()
        print(f"[selftest] 删除规则卡片：之前={card_before} 动画中={animating} "
              f"动画期间仍在列表={card_during} 之后={card_after} 已隐藏={card_hidden} "
              f"布局剩余={leftover} 数据库规则={rules_after}")
        _check(card_before and animating and card_during and not card_after and card_hidden
               and rules_after is None,
               "“删除规则”播放滑出/上移动画后移除卡片与数据库规则", failures)
        # 重新加回来继续后面的检查
        service.save_rule(Rule(program_id=program_id, daily_enabled=True, daily_value=1,
                               streak_enabled=True, streak_value=2, pause_value=1))
        page._rebuild_cards()

        # ---------------------------------------------------------- 折叠 / 时段 / 拖动排序
        # （1）规则卡默认收起：标题行常显（名称 + 规则摘要 + 实时状态），设置项收进内容区
        for _card in page._cards.values():
            _card.set_expanded(False, animate=False)
        _pump(app, 0.2)
        collapsed_all = bool(page._cards) and all(
            not c.content.isVisible() for c in page._cards.values())
        card = page._cards.get(program_id)
        head_ok = (card is not None and card.head.isVisible()
                   and bool(card.name_label.text()) and bool(_label_text(card.summary))
                   and not card.content.isVisible())
        collapsed_summary = _label_text(card.summary) if card is not None else ""
        print(f"[selftest] 规则卡折叠：卡片数={len(page._cards)} 全部收起={collapsed_all} "
              f"标题行常显={head_ok} 摘要='{collapsed_summary}'")
        _check(collapsed_all and head_ok,
               "控制页规则卡默认全部收起，标题行常显名称 + 规则摘要 + 实时状态", failures)

        # （2）展开 / 收起，且展开状态跨"重建卡片"保持（保存一张卡不会让全部塌回去）
        card.set_expanded(True, animate=False)
        _pump(app, 0.25)
        expanded_visible = card.content.isVisible()
        remembered = program_id in page._expanded
        _shot(window, os.path.join(shots, "17d_control_expanded.png"))
        page._rebuild_cards()
        _pump(app, 0.3)
        rebuilt = page._cards.get(program_id)
        kept_expanded = rebuilt is not None and rebuilt.content.isVisible()
        others_collapsed = all(not c.content.isVisible()
                               for pid, c in page._cards.items() if pid != program_id)
        print(f"[selftest] 规则卡展开：展开后可见={expanded_visible} 记住={remembered} "
              f"重建后仍展开={kept_expanded} 其它卡仍收起={others_collapsed}")
        _check(expanded_visible and remembered and kept_expanded and others_collapsed,
               "规则卡可展开 / 收起，且展开状态跨卡片重建保持", failures)

        # （2b）展开动画过程中标题行不能上下跳（m07636 bug1：展开方向的文字颤抖）
        page._rebuild_cards()
        _pump(app, 0.3)
        for _card in page._cards.values():
            _card.set_expanded(False, animate=False)
        _pump(app, 0.2)
        jitter_card = page._cards.get(program_id)
        jitter_samples: dict = {}
        if jitter_card is not None:
            jitter_probes = {"标题行": jitter_card.name_label, "头部": jitter_card.head,
                             "卡片": jitter_card, "内容": jitter_card.content}
            first = jitter_card.content.layout().itemAt(0) if jitter_card.content.layout() else None
            if first is not None and first.widget() is not None:
                jitter_probes["内容首行"] = first.widget()
            holder = getattr(jitter_card.content, "_clip_container", None)
            if holder is not None:
                jitter_probes["容器"] = holder
            jitter_card.set_expanded(True, animate=True)
            jitter_samples = _trace(app, jitter_card, jitter_probes, 0.8)
            _pump(app, 0.25)
        jitter = _jitter(jitter_samples, "标题行")
        first_row_jitter = _jitter(jitter_samples, "内容首行") if "内容首行" in jitter_samples else -1
        ups, turns = _oscillation(jitter_samples, "内容首行")
        sampled = len(jitter_samples.get("标题行") or [])
        print(f"[selftest] 展开动画：标题行位移波动={jitter}px 内容首行波动={first_row_jitter}px "
              f"上行={ups} 折返={turns} 采样={sampled}")
        _check(sampled >= 8 and 0 <= jitter <= 1 and ups in (-1, 0) and turns <= 1,
               "展开规则卡时标题行不上下跳（消除展开方向的文字颤抖）", failures)

        # （3）指定时间段：加一条跨午夜 22:00-07:00（周一 / 三 / 五）并保存到数据库
        card = page._cards.get(program_id)
        card._add_window()
        _pump(app, 0.1)
        row = card._window_rows[-1]
        row.start_hour.set_current("22")
        row.start_min_box.set_current("00")
        row.end_hour.set_current("07")
        row.end_min_box.set_current("00")
        for index, day_box in enumerate(row.day_boxes):
            day_box.setChecked(index in (0, 2, 4))
        row._sync_tag()
        card.window_switch.setChecked(True, animate=False, emit=False)
        card._sync_windows()
        row_values = [r.value().to_row() for r in card._window_rows]
        overnight_tag = bool(row.tag.text())
        card._save()                      # 保存会触发 state_changed -> 卡片整体重建
        _pump(app, 0.3)
        saved_rule = db.control_rules(program_id)
        saved_windows = json.loads(saved_rule["windows"] or "[]") if saved_rule else []
        engine_rule = service.all_rules().get(program_id)
        card = page._cards.get(program_id)      # _save 后旧卡片已重建，必须重新取
        ui_rows = len(card._window_rows) if card is not None else 0
        summary_has_slot = card is not None and "22:00" in _label_text(card.summary)
        window_ok = (row_values == [[1320, 420, 0b0010101]] and overnight_tag
                     and saved_rule is not None and int(saved_rule["window_enabled"]) == 1
                     and saved_windows == [[1320, 420, 0b0010101]]
                     and engine_rule is not None and engine_rule.window_active
                     and len(engine_rule.windows) == 1
                     and engine_rule.windows[0].start_minute == 1320
                     and engine_rule.windows[0].end_minute == 420
                     and engine_rule.windows[0].days == 0b0010101
                     and engine_rule.windows[0].crosses_midnight
                     and ui_rows == 1 and summary_has_slot)
        print(f"[selftest] 指定时间段：界面值={row_values} 跨午夜标记={overnight_tag} "
              f"落库={saved_windows} 引擎={engine_rule.windows if engine_rule else None} "
              f"重建后时段行={ui_rows} 摘要含 22:00={summary_has_slot}")
        _check(window_ok,
               "规则卡可编辑“指定时间段”（跨午夜 + 星期勾选）并保存 / 回显到数据库", failures)
        # 还原成不带时间段的规则：后面的"每日上限 -> 遮罩"用例依赖它
        service.save_rule(Rule(program_id=program_id, daily_enabled=True, daily_value=1,
                               streak_enabled=True, streak_value=2, pause_value=1))
        _pump(app, 0.2)

        # （3b）规则配置很多时摘要不越出卡片：单行省略 + 悬停 tooltip 给出全文（m07636 bug2）
        card = page._cards.get(program_id)
        if card is not None:
            for _ in range(5):
                card._add_window()
            _pump(app, 0.15)
            # 五个互不重叠的时段 + 全周勾选，保证摘要一定长过卡片可用宽度
            spans = ((1320, 420, 0b0010101), (480, 570, 0b1111111), (600, 660, 0b1111111),
                     (780, 840, 0b0111110), (1020, 1080, 0b1010101))
            for row, (start, end, days) in zip(card._window_rows, spans):
                row.start_hour.set_current("%02d" % (start // 60))
                row.start_min_box.set_current("%02d" % (start % 60))
                row.end_hour.set_current("%02d" % (end // 60))
                row.end_min_box.set_current("%02d" % (end % 60))
                for index, day_box in enumerate(row.day_boxes):
                    day_box.setChecked(bool(days & (1 << index)))
                row._sync_tag()
            card.window_switch.setChecked(True, animate=False, emit=False)
            card._sync_windows()
            card._save()
            _pump(app, 0.35)
        card = page._cards.get(program_id)
        elide_ok = False
        if card is not None:
            # 规则摘要本身在 >3 个时段时已经用 "…" 收口，宽度还够用时不会省略；
            # 这里再直接把两行标签的文本拉长到必然溢出（×12 足够超过卡片宽度），
            # 验证"省略 + tooltip 全文 + 不越框"。
            long_text = card.summary.full_text() * 12 + " 尾巴"
            card.summary.setText(long_text)
            card.status.setText(long_text)
            card.status.setVisible(True)
            _pump(app, 0.3)
            # 上面这 0.3s 里卡片自己的状态刷新会把两行文字改回真实摘要（那是产品该有的行为），
            # 所以读完之前必须再设一次：`_ElidedLabel.setText` 是同步省略，期间不跑事件循环。
            card.summary.setText(long_text)
            card.status.setText(long_text)
            full = _label_text(card.summary)
            shown = card.summary.text()
            tip = card.summary.toolTip() or ""
            right_edge = card.summary.mapTo(card, QPoint(card.summary.width(), 0)).x()
            status_full = _label_text(card.status)
            status_shown = card.status.text()
            status_tip = card.status.toolTip() or ""
            status_right = card.status.mapTo(card, QPoint(card.status.width(), 0)).x()
            elide_ok = (full == long_text and len(shown) < len(full) and "…" in shown
                        and "22:00" in full and tip == full
                        and 0 < card.summary.width() <= card.width() + 1
                        and right_edge <= card.width() + 1
                        and len(status_shown) < len(status_full) and status_tip == status_full
                        and status_right <= card.width() + 1)
            print(f"[selftest] 摘要省略：显示='{shown[:28]}…' 全文={len(full)}字 显示={len(shown)}字 "
                  f"省略号={'…' in shown} 含22:00={'22:00' in full} tooltip一致={tip == full} "
                  f"右边缘={right_edge} 卡宽={card.width()} 摘要宽={card.summary.width()} "
                  f"状态显示={len(status_shown)}字 状态全文={len(status_full)}字 "
                  f"状态一致={status_tip == status_full} 状态右边缘={status_right}")
        _check(elide_ok, "规则摘要过长时单行省略、悬停显示全文且不越出卡片范围", failures)
        # 还原成不带时间段的规则（后面还有别的用例依赖它）
        service.save_rule(Rule(program_id=program_id, daily_enabled=True, daily_value=1,
                               streak_enabled=True, streak_value=2, pause_value=1))
        if card is not None:
            try:
                card.refresh_status()
            except RuntimeError:
                pass
        else:
            page._refresh_statuses()
        _pump(app, 0.25)

        # （4）拖动排序：程序组内换位后立即落库，重建列表仍按新顺序
        group_ids = [pid for pid in page.reorder.order("program") if isinstance(pid, int)]
        if len(group_ids) < 2:
            other = next((p for p in programs if int(p["id"]) != program_id
                          and str(p.get("kind") or "program") != "site"
                          and int(p["id"]) not in group_ids), None)
            if other is not None:
                service.save_rule(Rule(program_id=int(other["id"])))
                _pump(app, 0.2)
            group_ids = [pid for pid in page.reorder.order("program") if isinstance(pid, int)]
        stored = []
        rebuilt_order = []
        drag_ok = False
        if len(group_ids) >= 2:
            first, second = group_ids[0], group_ids[1]
            card_first, card_second = page._cards[first], page._cards[second]
            page.reorder._begin(card_second, card_second.mapToGlobal(QPoint(10, 10)))
            top_left = card_first.mapToGlobal(QPoint(0, 0))
            page.reorder._last = QPoint(top_left.x() + 5, top_left.y() + 1)
            page.reorder._reorder()
            moved = page.reorder.order("program")[:2]
            page.reorder._finish(commit=True)
            stored = db.get_order("order.control.program")
            page._rebuild_cards()
            _pump(app, 0.3)
            rebuilt_order = [pid for pid in page.reorder.order("program") if isinstance(pid, int)]
            drag_ok = (moved == [second, first] and list(stored)[:2] == [second, first]
                       and rebuilt_order[:2] == [second, first])
        print(f"[selftest] 规则卡拖动排序：本组顺序={group_ids} 落库={stored} "
              f"重建后={rebuilt_order} 全部卡片={list(page._cards)}")
        _check(drag_ok, "规则卡左侧手柄拖动换位后立即落库，重建列表仍按新顺序", failures)

        # （5）拖到边缘自动滚动：上边缘步长为负、下边缘为正，并真的推动滚动条
        bar = page.scroll.verticalScrollBar()
        viewport = page.scroll.viewport()
        anchor_card = page._cards.get(program_id) or next(iter(page._cards.values()))
        page.reorder._begin(anchor_card, anchor_card.mapToGlobal(QPoint(10, 10)))
        vp_top = viewport.mapToGlobal(QPoint(0, 0))
        vp_bottom = viewport.mapToGlobal(QPoint(0, max(1, viewport.height() - 2)))
        page.reorder._last = QPoint(vp_top.x() + 20, vp_top.y() + 2)
        step_up = page.reorder._edge_step()
        page.reorder._last = QPoint(vp_bottom.x() + 20, vp_bottom.y())
        step_down = page.reorder._edge_step()
        old_range = (bar.minimum(), bar.maximum())
        bar.setRange(0, 400)
        bar.setValue(200)
        page.reorder._last = QPoint(vp_top.x() + 20, vp_top.y() + 2)
        page.reorder._on_tick()
        value_after_up = bar.value()
        page.reorder._last = QPoint(vp_bottom.x() + 20, vp_bottom.y())
        page.reorder._on_tick()
        value_after_down = bar.value()
        page.reorder._finish(commit=False)
        bar.setRange(*old_range)
        bar.setValue(0)
        page.refresh()
        _pump(app, 0.3)
        edge_ok = (step_up < 0 < step_down and value_after_up < 200
                   and value_after_down > value_after_up)
        print(f"[selftest] 边缘自动滚动：上边缘步长={step_up} 下边缘步长={step_down} "
              f"滚动条 200->{value_after_up}->{value_after_down}")
        _check(edge_ok, "拖动手柄靠近列表上下边缘时自动滚动（上边缘向上、下边缘向下）", failures)

        # （6）跨分类：程序组的拖动碰不到网站组（两个分组各自排序）
        from PySide6.QtWidgets import QHBoxLayout as _HBox
        from PySide6.QtWidgets import QVBoxLayout as _VBox
        from .ui.controls import IconButton as _IconButton
        from .ui.reorder import ReorderController as _Reorder

        host = QWidget(window)
        host.move(-4000, -4000)          # 屏幕外，免得出现在截图里
        box = _VBox(host)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(config.sx(8))
        made = []
        for fake_id in (9101, 9102, 9103, 9104):
            item = QWidget(host)
            item.program_id = fake_id
            item.setFixedHeight(config.sx(34))        # 高度固定，拖动判定才有稳定的中点
            inner = _HBox(item)
            inner.setContentsMargins(0, 0, 0, 0)
            item.drag_handle = _IconButton("grip", "", size=28, icon_size=16,
                                           parent=item, flat=True)
            inner.addWidget(item.drag_handle)
            inner.addStretch(1)
            box.addWidget(item)
            made.append(item)
        host.show()                       # 有父控件，安全
        _pump(app, 0.25)
        commits = []
        ctrl = _Reorder(page.scroll, lambda kind, order: commits.append((kind, order)), window)
        ctrl.register("program", [made[0], made[1]])       # 9101 / 9102
        ctrl.register("site", [made[2], made[3]])          # 9103 / 9104
        # 第一段：程序组内换位（9102 排到 9101 前面），网站组纹丝不动
        ctrl._begin(made[1], made[1].mapToGlobal(QPoint(10, 10)))
        # 动效：卡片浮到视口上层、原位留虚线占位框、尺寸轻微放大（"拿起"）
        _pump(app, 0.3)
        floated = made[1].parentWidget() is page.scroll.viewport()
        placeholder = ctrl._placeholder
        placeholder_ok = (placeholder is not None and placeholder.parentWidget() is host
                          and placeholder.height() == config.sx(34)
                          and placeholder.objectName() == "CardSlot")
        lift_ok = ctrl._lift_progress > 0.9 and made[1].width() > ctrl._grab_size.width()
        anchor_top = made[0].mapToGlobal(QPoint(0, 0))
        ctrl._last = QPoint(anchor_top.x() + 5, anchor_top.y() + 1)
        ctrl._reorder()
        slides_ok = bool(ctrl._slides)          # 让位的卡片正在播放 geometry 动画
        group_swap = ctrl.order("program")
        site_untouched = ctrl.order("site")
        # 第二段：把程序卡拖到"网站"卡上也不许跨类——它只能落在程序组内部
        ctrl._last = QPoint(anchor_top.x() + 5,
                            made[2].mapToGlobal(QPoint(0, made[2].height() // 2)).y())
        _dbg_rest = []
        for _other in ctrl._groups.get("program", ()):
            if _other is ctrl._card:
                continue
            _slot = ctrl._slots.get(_other)
            if _slot is not None:
                _dbg_rest.append((getattr(_other, "program_id", None),
                                  ctrl._holder.mapToGlobal(_slot.topLeft()).y(),
                                  int(_slot.height() // 2)))
        print(f"[selftest] 拖动诊断：光标y={ctrl._last.y()} 同组槽位(全局y,半高)={_dbg_rest} "
              f"槽位序={[getattr(i, 'program_id', 'slot') for i in ctrl._slot_order]}")
        ctrl._reorder()
        cross_swap = ctrl.order("program")
        site_after = ctrl.order("site")
        slot_ok = (box.indexOf(made[1]) < 2 and box.indexOf(made[2]) == 2
                   and box.indexOf(made[3]) == 3)
        ctrl._finish(commit=True)
        _pump(app, 0.35)                        # 等落回动画结束
        restored_ok = (made[1].parentWidget() is host and ctrl._placeholder is None
                       and ctrl._lift_progress == 0.0
                       and box.indexOf(made[1]) == 1 and box.indexOf(made[0]) == 0)
        ctrl.clear()
        for item in made:
            item.hide()
            item.setParent(None)
            item.deleteLater()
        host.hide()
        host.setParent(None)
        host.deleteLater()
        _pump(app, 0.15)
        cross_ok = (group_swap == [9102, 9101] and site_untouched == [9103, 9104]
                    and cross_swap == [9101, 9102] and site_after == [9103, 9104]
                    and slot_ok and commits == [("program", [9101, 9102])])
        print(f"[selftest] 跨分类拖动：程序组={group_swap} 拖到网站后程序组={cross_swap} "
              f"网站组={site_after} 槽位={slot_ok} 提交={commits}")
        _check(cross_ok, "拖动手柄排序只在同一分类内生效（程序与网站不会互串）", failures)
        print(f"[selftest] 拖动动效：浮层={floated} 占位框={placeholder_ok} 抬起={lift_ok} "
              f"让位动画={slides_ok} 松手还原={restored_ok}")
        _check(floated and placeholder_ok and lift_ok and slides_ok and restored_ok,
               "拖动动效：卡片浮起跟随光标、原位留占位框、经过的卡片让位、松手落回原位", failures)

        # 触发每日上限 -> 遮罩（用假的可见窗口列表驱动遮罩管理器）
        state = service.engine.state(program_id)
        service.engine._advance_day(state, __import__("time").time())
        state.used_seconds = 999
        blocked = service.engine.blocked_programs()
        _check(program_id in blocked, "达到每日上限后该程序进入受限状态", failures)

        import monitor.overlay as overlay_module
        from PySide6.QtCore import QRect

        fake_calls = {"count": 0}

        def fake_windows(pids):
            fake_calls["count"] += 1
            return {int(pid): [(123456, QRect(100, 100, 400, 300), False)] for pid in pids}

        original = overlay_module.visible_windows
        original_pids = service.overlays._pids_provider
        service.overlays._pids_provider = lambda program_id: [4242]
        overlay_module.visible_windows = fake_windows
        service.overlays.invalidate_scan()
        try:
            service.overlays.sync()
            _pump(app, 0.2)
            overlays = service.overlays.overlay_count()
            created = list(service.overlays._overlays.get(program_id, {}).values())
            expected_rect = overlay_module.physical_to_logical(QRect(100, 100, 400, 300))
            geometry_ok = bool(created) and created[0].geometry() == expected_rect
            service.overlays.sync()
            _pump(app, 0.1)
            stable = service.overlays.overlay_count() == overlays
            # 窗口消失 -> 先进入宽限期（避免抖动），超过 GRACE_S 后销毁
            overlay_module.visible_windows = lambda pids: {}
            service.overlays.invalidate_scan()
            service.overlays.sync()
            _pump(app, 0.2)
            during_grace = service.overlays.overlay_count() == overlays
            for key in list(service.overlays._missing):
                service.overlays._missing[key] = 0.0
            service.overlays.sync()
            _pump(app, 0.2)
            gone = service.overlays.overlay_count() == 0
        finally:
            overlay_module.visible_windows = original
            service.overlays._pids_provider = original_pids
        print(f"[selftest] 遮罩：数量={overlays} 位置同步={geometry_ok} 稳定={stable} "
              f"宽限期保留={during_grace} 超时后销毁={gone}")

        # DPI 换算：遮罩几何必须走 physical_to_logical（否则缩放的屏幕上会放大并偏移）
        service.overlays._pids_provider = lambda program_id: [4242]
        original_convert = overlay_module.physical_to_logical
        shifted = []
        overlay_module.physical_to_logical = lambda rect: (
            shifted.append(rect) or QRect(rect.x() + 7, rect.y() + 9, rect.width(), rect.height()))
        try:
            overlay_module.visible_windows = fake_windows
            service.overlays.invalidate_scan()
            service.overlays.set_suspended(False)
            service.overlays.sync()
            _pump(app, 0.2)
            shifted_overlay = list(service.overlays._overlays.get(program_id, {}).values())
            converted = bool(shifted) and bool(shifted_overlay) \
                and shifted_overlay[0].geometry().x() == 107 \
                and shifted_overlay[0].geometry().y() == 109
        finally:
            overlay_module.physical_to_logical = original_convert
            overlay_module.visible_windows = lambda pids: {}
            service.overlays.invalidate_scan()
            service.overlays.sync()
            _pump(app, 0.1)
        print(f"[selftest] 遮罩坐标换算：物理矩形经过换算={bool(shifted)} 几何已应用={converted}")
        _check(converted, "遮罩几何经过物理像素→逻辑坐标换算（DPI 缩放不错位）", failures)

        # 弹窗期间遮罩必须保持可见（否则点一下"临时解锁"就白得了解锁）
        service.overlays._pids_provider = lambda program_id: [4242]
        overlay_module.visible_windows = fake_windows
        service.overlays.invalidate_scan()
        service.overlays.set_suspended(True)
        service.overlays.sync()
        _pump(app, 0.2)
        during = service.overlays.overlay_count()
        during_lock = service.overlays.lock_remaining()
        service.overlays.set_suspended(False)
        service.overlays.sync()
        _pump(app, 0.2)
        after = service.overlays.overlay_count()
        # 遮罩上的锁定提示
        live_overlay = list(service.overlays._overlays.get(program_id, {}).values())
        notified = False
        if live_overlay:
            # 顶层遮罩只在目标（或本程序窗口）位于前台时才显示：这里把前台固定成遮罩自己，
            # 否则断言会随"当前哪个窗口在前台"而飘
            original_fg_here = overlay_module.foreground_window
            overlay_module.foreground_window = lambda: int(live_overlay[0].winId())
            try:
                service.overlays.lock_provider = lambda: 42
                service.overlays.sync()
                _pump(app, 0.2)
                notified = (live_overlay[0].notice.isVisible()
                            and "42" in live_overlay[0].notice.text()
                            and not live_overlay[0].unlock_btn.isEnabled())
                _shot(live_overlay[0], os.path.join(shots, "17b_overlay_locked.png"))
                service.overlays.lock_provider = None
                service.overlays.sync()
                _pump(app, 0.2)
                cleared = (live_overlay[0].notice.isHidden()
                           and live_overlay[0].unlock_btn.isEnabled())
            finally:
                overlay_module.foreground_window = original_fg_here
        else:
            cleared = False
        overlay_module.visible_windows = lambda pids: {}
        service.overlays.invalidate_scan()
        service.overlays.sync()
        _pump(app, 0.1)
        print(f"[selftest] 遮罩暂停：弹窗期间={during} 关闭后={after} "
              f"锁定提示={notified} 解除后清除={cleared}")
        _check(during == 1 and after == 1,
               "弹密码框期间遮罩保持可见（不会因为弹窗而失去遮罩）", failures)
        _check(notified and cleared,
               "遮罩上显示密码锁定提示并禁用临时解锁，锁定结束后恢复", failures)

        # 子窗口遮罩：把遮罩挂到本进程自己的窗口上，验证父子关系与客户区铺满
        overlay_module.visible_windows = fake_windows
        service.overlays.invalidate_scan()
        service.overlays._pids_provider = lambda program_id: [4242]
        service.overlays.set_suspended(False)
        service.overlays.sync()
        _pump(app, 0.2)
        own_hwnd = int(window.winId())
        real_target = lambda pids: {int(p): [(own_hwnd, window.frameGeometry(), False)]
                                    for p in pids}
        overlay_module.visible_windows = real_target
        service.overlays.invalidate_scan()
        service.overlays.sync()
        _pump(app, 0.4)
        # 之前的假窗口遮罩此刻还在宽限期内（不是目标窗口的），要按 hwnd 精确取目标对应的那个
        overlay = service.overlays._overlays.get(program_id, {}).get(own_hwnd)
        mounted = bool(overlay) and overlay.mounted
        _pump(app, 0.2)
        parent_ok = bool(overlay) and overlay_module.parent_of(int(overlay.winId())) == own_hwnd
        client = overlay_module.logical_size(overlay_module.client_rect(own_hwnd))
        size_ok = bool(overlay) and overlay.width() == client.width()
        print(f"[selftest] 子窗口遮罩：挂载成功={mounted} GetParent 正确={parent_ok} "
              f"铺满客户区={size_ok}（{overlay.width() if overlay else 0}x"
              f"{overlay.height() if overlay else 0} vs {client.width()}x{client.height()}）")
        _check(mounted and parent_ok and size_ok,
               "遮罩可挂成目标窗口的子窗口并铺满客户区", failures)
        mounted_count = service.overlays.mounted_count()
        print(f"[selftest] 已挂载遮罩数={mounted_count}")
        _check(mounted_count >= 1, "遮罩管理器统计已挂载的子窗口遮罩", failures)

        # 无边框全屏游戏（原神）等窗口拒绝被挂载：必须回滚成顶层窗口并标记不可挂载，
        # 否则遮罩会挂在 Qt 的临时父窗口上（盖不住游戏，还会周期性闪烁）
        bogus = overlay_module.OverlayWindow(program_id)
        bogus.show()
        original_parent_of = overlay_module.parent_of
        overlay_module.parent_of = lambda hwnd: -1          # 让两条挂载路径都"校验失败"
        try:
            bogus_mounted = bogus.mount_into(own_hwnd)
        finally:
            overlay_module.parent_of = original_parent_of
        bogus.hide()
        rollback_ok = (not bogus_mounted and bogus.unmountable and not bogus.mounted
                       and overlay_module.parent_of(int(bogus.winId())) == 0)
        bogus.deleteLater()
        print(f"[selftest] 挂载失败回滚：挂载成功={bogus_mounted} 标记不可挂载={bogus.unmountable} "
              f"已还原为顶层窗口={rollback_ok}")
        _check(rollback_ok,
               "挂载失败的窗口（无边框全屏游戏）会回滚成顶层遮罩并标记不可挂载", failures)

        # 标记不可挂载之后，管理器不能再反复尝试（反复 setParent 就是"闪烁"的来源）
        retry_calls = {"count": 0}
        original_mount = overlay_module.OverlayWindow.mount_into

        def counting_mount(self, hwnd_target):
            retry_calls["count"] += 1
            return original_mount(self, hwnd_target)

        overlay_module.visible_windows = fake_windows
        service.overlays._pids_provider = lambda program_id: [4242]
        service.overlays.invalidate_scan()
        service.overlays.sync()
        _pump(app, 0.15)
        targets = list(service.overlays._overlays.get(program_id, {}).values())
        overlay_module.OverlayWindow.mount_into = counting_mount
        try:
            for item in targets:
                item.unmountable = True
            retry_calls["count"] = 0
            service.overlays.sync()
            _pump(app, 0.1)
            service.overlays.sync()
            _pump(app, 0.1)
            no_retry = retry_calls["count"] == 0
        finally:
            overlay_module.OverlayWindow.mount_into = original_mount
        for item in targets:
            item.unmountable = False
        print(f"[selftest] 不可挂载标记：标记后再次 sync 的挂载尝试次数={retry_calls['count']}（期望 0）")
        _check(no_retry, "标记为不可挂载的窗口不再重试挂载（避免周期性闪烁）", failures)

        # 顶层兜底：遮罩被强制"紧贴目标上方"时始终显示（切到别的窗口也不会浮上去）；
        # 只有遮罩不得不置顶（目标提权 / 目标自身置顶）时，才改成"仅目标在前台时显示"，
        # 否则置顶遮罩会把屏幕上所有普通窗口都盖住。
        def _live_overlay():
            service.overlays.invalidate_scan()
            service.overlays._last_remount = 0.0
            service.overlays.sync()
            _pump(app, 0.1)
            items = service.overlays._overlays.get(program_id, {})
            return next(iter(items.values()), None)

        original_fg = overlay_module.foreground_window
        original_wpid = overlay_module.window_pid
        original_place = overlay_module.place_above
        gate = _live_overlay() or overlay
        try:
            # 情形 1：普通目标（z 序被强制紧贴目标上方）-> 别的程序在前台也始终显示
            gate.placement_denied = False
            overlay_module.foreground_window = lambda: 555001
            overlay_module.window_pid = lambda hwnd: 999 if hwnd == 555001 else 0
            shown_others = service.overlays._should_show_top_level(own_hwnd, gate) is True
            # 情形 2：遮罩被迫置顶（提权目标），其它程序在前台 -> 隐藏
            gate.placement_denied = True
            hidden_others = service.overlays._should_show_top_level(own_hwnd, gate) is False
            # 情形 3：遮罩被迫置顶，目标窗口自己在前台 -> 显示
            overlay_module.foreground_window = lambda: own_hwnd
            overlay_module.window_pid = lambda hwnd: 0
            shown_own = service.overlays._should_show_top_level(own_hwnd, gate) is True
            # 情形 4：遮罩被迫置顶，本程序弹窗在前台 -> 也必须隐藏（置顶遮罩会盖住其它程序）
            overlay_module.foreground_window = lambda: 555002
            overlay_module.window_pid = lambda hwnd: os.getpid() if hwnd == 555002 else 999
            hidden_dialog = service.overlays._should_show_top_level(own_hwnd, gate) is False
        finally:
            gate.placement_denied = False
            overlay_module.foreground_window = original_fg
            overlay_module.window_pid = original_wpid
        print(f"[selftest] 顶层遮罩前台判定：普通目标其它程序在前台时显示={shown_others} "
              f"置顶遮罩其它程序在前台时隐藏={hidden_others} 目标在前台时显示={shown_own} "
              f"本程序弹窗在前台时隐藏={hidden_dialog}")
        _check(shown_others and hidden_others and shown_own and hidden_dialog,
               "顶层遮罩普通目标紧贴目标上方始终显示；被迫置顶时只在目标位于前台才显示", failures)

        # 提权目标（UIPI 拒绝 SetWindowPos）时的退路：置顶 + 提示 + 状态可自愈
        overlay_module.foreground_window = lambda: own_hwnd
        try:
            overlay_module.place_above = lambda overlay_hwnd, target_hwnd: False
            denied_overlay = _live_overlay()
            denied = bool(denied_overlay is not None and denied_overlay.placement_denied)
            topmost_after = bool(denied_overlay is not None
                                 and overlay_module.is_topmost(int(denied_overlay.winId())))
            hint_shown = bool(denied_overlay is not None and not denied_overlay.hint.isHidden())
            print(f"[selftest] z 序被拒（提权目标）：标记被拒={denied} 遮罩置顶={topmost_after} "
                  f"提示可见={hint_shown}")
            _check(denied and topmost_after and hint_shown,
                   "目标窗口拒绝调整 z 序（提权程序）时遮罩退化为置顶并显示管理员提示", failures)
            # 恢复：z 序可用后撤掉置顶与提示（前台仍保持目标窗口，否则置顶遮罩按策略隐藏）
            target_hwnd = int(getattr(denied_overlay, "target_hwnd", 0) or own_hwnd)
            overlay_module.place_above = original_place
            overlay_module.foreground_window = lambda: target_hwnd
            recovered_overlay = _live_overlay()
            recovered = bool(recovered_overlay is not None
                             and not recovered_overlay.placement_denied
                             and recovered_overlay.hint.isHidden())
            print(f"[selftest] z 序恢复后：标记被拒="
                  f"{None if recovered_overlay is None else recovered_overlay.placement_denied} "
                  f"提示可见="
                  f"{None if recovered_overlay is None else not recovered_overlay.hint.isHidden()} "
                  f"已复位={recovered}")
            _check(recovered,
                   "z 序重新可用时自动撤掉置顶回退与提示（无需重启）", failures)
        finally:
            overlay_module.place_above = original_place
            overlay_module.foreground_window = original_fg

        # z 序判定：必须是链式查找（中间隔着别的窗口也能查出"遮罩在上方"）
        probe_win = QWidget()
        probe_win.resize(120, 80)
        probe_win.show()
        _pump(app, 0.15)
        probe_hwnd = int(probe_win.winId())
        overlay_module.place_above(probe_hwnd, own_hwnd)
        above_ok = overlay_module.is_above(probe_hwnd, own_hwnd)
        overlay_module.place_above(own_hwnd, probe_hwnd)
        below_ok = not overlay_module.is_above(probe_hwnd, own_hwnd)
        probe_win.hide()
        probe_win.deleteLater()
        _pump(app, 0.1)
        print(f"[selftest] z 序链式查找：置顶后判定在上方={above_ok} 置底后判定不在上方={below_ok}")
        _check(above_ok and below_ok,
               "z 序判定沿 GW_HWNDPREV 链式查找（被其它窗口隔开也能正确判断）", failures)

        # 输入拦截：钩子可安装，且遮罩窗口登记在放行名单里
        blocked_pids, overlay_hwnds = service.overlays._input_state()
        service.overlays._blocker.install()
        installed = service.overlays.input_blocking()
        service.overlays._blocker.uninstall()
        print(f"[selftest] 输入拦截：钩子可安装={installed} 被限制 PID={sorted(blocked_pids)[:3]} "
              f"遮罩 HWND 已登记={own_hwnd in overlay_hwnds}")
        _check(installed and bool(overlay_hwnds),
               "遮罩生效时安装低级输入钩子并登记遮罩窗口（放行自己的按钮）", failures)

        # 静音：音频不可用时要安静降级
        muted = service.overlays._muter.mute(set(blocked_pids))
        service.overlays._muter.restore()
        audio_error = getattr(audio_module, "LAST_ERROR", "")
        print(f"[selftest] 静音模块：可用={service.overlays._muter.available} 本次静音会话={muted} "
              f"环境错误='{audio_error or '无'}'")
        _check(isinstance(muted, int),
               "静音模块在无音频设备时安静降级（不抛异常）", failures)

        # 静音调用链：记录原音量 -> 音量降到 0% + 静音 -> 结束时还原（用假会话验证接线）
        mute_calls = []

        class _FakeVolume:
            """假的 ISimpleAudioVolume 指针：只要 .value 相同就代表同一个会话。"""

            def __init__(self, value: int) -> None:
                self.value = value

        class _FakeSession:
            def __init__(self, volume, original_mute, pid, original_volume=None,
                         path="") -> None:
                self.volume = volume
                self.original_mute = original_mute
                self.pid = pid
                self.original_volume = original_volume
                self.path = path

            def silence(self) -> bool:
                mute_calls.append(("silence", self.pid, self.original_volume,
                                   self.original_mute))
                return True

            def restore_state(self) -> bool:
                mute_calls.append(("restore", self.pid, self.original_volume,
                                   self.original_mute))
                return True

            def release(self) -> None:
                mute_calls.append(("release", self.pid, None, None))

        real_sessions = audio_module._enumerate_sessions
        real_session_cls = audio_module._Session
        fake_volume = _FakeVolume(0x1000)
        probe_muter = audio_module.ProcessMuter()
        probe_muter.available = True
        try:
            audio_module._enumerate_sessions = \
                lambda: [(4242, fake_volume, False, 0.42, 1)]
            audio_module._Session = _FakeSession
            added = probe_muter.mute({4242}, force=True)
            registered = (probe_muter.muted_pids() == {4242}
                          and probe_muter.muted_session_count() == 1)
            # 同一个会话被再次枚举到：要再压一次（程序自己把音量调回来的情况）
            repress_added = probe_muter.mute({4242}, force=True)
            second_silence = sum(1 for call in mute_calls if call[0] == "silence")
            probe_muter.restore()
            cleared = probe_muter.muted_pids() == set()
        finally:
            audio_module._enumerate_sessions = real_sessions
            audio_module._Session = real_session_cls
        silence_ok = (added == 1 and mute_calls and mute_calls[0][0] == "silence"
                      and mute_calls[0][2] == 0.42)
        repress_ok = repress_added == 0 and second_silence == 2
        restore_index = next((index for index, call in enumerate(mute_calls)
                              if call[0] == "restore" and call[2] == 0.42), None)
        restore_ok = (restore_index is not None and restore_index > 0 and cleared)
        print(f"[selftest] 静音调用链：新增={added} 登记={registered} 重复压制={repress_added} "
              f"silence次数={second_silence} 调用={mute_calls}")
        _check(silence_ok and repress_ok and restore_ok,
               "遮罩触发时把音量降到 0% 并静音、程序调回后再次压制，解除时还原原音量", failures)

        # GUID 解析回归：以前丢掉 GUID 第 4 段，导致 CLSID/IID 全错、会话永远枚举不到
        expected_guids = {
            "CLSID_MMDeviceEnumerator": "9503debc2fe57c468e3dc4579291692e",
            "IID_IMMDeviceEnumerator": "d26456a91496354fa746de8db63617e6",
            "IID_IAudioSessionManager2": "a099aa77d61b4f488bc72c654c9a9b6f",
            "IID_ISimpleAudioVolume": "9854ce87d668e54492156da47ef883d8",
        }
        actual_guids = {name: bytes(getattr(audio_module, name)).hex()
                        for name in expected_guids}
        print(f"[selftest] 音频 GUID 解析：{actual_guids}")
        _check(actual_guids == expected_guids,
               "WASAPI 的 CLSID/IID 按 GUID 规则完整解析（错一个字节就找不到音频会话）", failures)

        # 真实端到端：起一个循环播放的子进程 -> 枚举到它的会话 -> 压到 0% -> 还原
        # 同时回归「解除遮罩时未恢复原音量」：接管前已是 0%/静音、空集合还原、
        # 记忆优先、启动 recover 兜底
        live = _audio_end_to_end()
        print(f"[selftest] 真实静音：{live['detail']}")
        _check(live["ok"],
               "真实进程的音频会话能被枚举到、压到 0% 静音，解除后还原原音量"
               "（含接管前已是 0%、空集合、记忆优先、启动 recover 四条回归）", failures)

        # 原始音量判定：被静音的会话不能把 0% 记成"原始音量"
        rule_ok = (audio_module.ProcessMuter.looks_silenced(0.0, True)
                   and audio_module.ProcessMuter.looks_silenced(None, True)
                   and not audio_module.ProcessMuter.looks_silenced(0.6, True)
                   and not audio_module.ProcessMuter.looks_silenced(0.0, False)
                   and audio_module.ProcessMuter.original_state(0.0, True)
                   == (audio_module.ProcessMuter.FALLBACK_VOLUME, False)
                   and audio_module.ProcessMuter.original_state(0.35, True) == (0.35, True)
                   and audio_module.ProcessMuter.original_state(None, False)
                   == (audio_module.ProcessMuter.FALLBACK_VOLUME, False))
        print(f"[selftest] 原始音量判定：0%+静音视为被静音={rule_ok} "
              f"回退音量={audio_module.ProcessMuter.FALLBACK_VOLUME}")
        _check(rule_ok,
               "静音判定：0%/无音量且处于静音的会话不记为原始音量，回退到 100% 未静音", failures)

        # 原始音量跨进程记忆：写进数据库后能被新的 ProcessMuter 读回
        store_ok = False
        try:
            service.save_audio_originals({"c:\\x\\demo.exe": [0.8, False]})
            store_ok = service.load_audio_originals() == {"c:\\x\\demo.exe": [0.8, False]}
            fresh = audio_module.ProcessMuter(service)
            store_ok = store_ok and fresh.originals() == {"c:\\x\\demo.exe": (0.8, False)}
        finally:
            service.save_audio_originals({})
        print(f"[selftest] 原始音量记忆：写读一致={store_ok} "
              f"重启后={service.load_audio_originals()}")
        _check(store_ok,
               "原始音量按程序路径持久化（Monitor 重启后仍能把音量还回去）", failures)

        # 关闭出口：遮罩上有"关闭程序"按钮
        close_btn = getattr(overlay, "close_btn", None)
        print(f"[selftest] 关闭出口按钮：存在={close_btn is not None}")
        _check(close_btn is not None and close_btn.text() == tr("overlay.close_program"),
               "遮罩上提供“关闭程序”出口按钮", failures)

        # 还原环境
        service.overlays._destroy(program_id)
        overlay_module.visible_windows = lambda pids: {}
        service.overlays.invalidate_scan()
        service.overlays.sync()
        _pump(app, 0.1)
        _check(overlays == 1 and geometry_ok and stable and during_grace and gone,
               "遮罩窗口按目标窗口创建 / 同步位置 / 窗口短暂消失时保留 / 超时后销毁", failures)

        # 临时解锁：解锁后不再受限，结束后重新检查
        unlocked = service.unlock(program_id, 1)
        still_blocked = program_id in service.engine.blocked_programs()
        state = service.engine.state(program_id)
        state.unlock_until = 0.0
        blocked_again = program_id in service.engine.blocked_programs()
        logs = [r for r in db.logs_for_program(program_id) if (r["kind"] or "run") != "run"]
        print(f"[selftest] 解锁={unlocked} 解锁期间受限={still_blocked} "
              f"结束后受限={blocked_again} 控制日志={len(logs)}")
        _check(unlocked and not still_blocked and blocked_again,
               "临时解锁期间不受限，结束后重新检查并恢复限制", failures)
        _check(any((r["kind"] or "") == "unlock" for r in logs),
               "控制相关日志写入 SQLite（临时解锁）", failures)
        service.delete_rule(program_id)
        _check(db.control_rules(program_id) is None, "控制页可以删除规则", failures)

    # 时长下拉栏：点 + 号新增自定义行、下拉栏内 - 号逐行移除、无勾选框
    notify_page = window.notify_page
    selector = notify_page.global_section.duration_selector
    has_checkbox = hasattr(notify_page.global_section, "duration_box")
    # 展开企业微信通知卡片：确认 URL 行与测试通知按钮共存
    window.switch_page(2, animate=False)
    _pump(app, 0.3)
    window.notify_page.wecom_card.set_expanded(True, animate=False)
    _pump(app, 0.5)
    _shot(window, os.path.join(shots, "18c_notify_wecom.png"))
    window.notify_page.wecom_card.set_expanded(False, animate=False)
    _pump(app, 0.2)
    # 展开 Windows 通知卡片：确认自定义通知内容输入行也在这里
    window.notify_page.windows_card.set_expanded(True, animate=False)
    _pump(app, 0.5)
    _shot(window, os.path.join(shots, "18e_notify_windows.png"))
    window.notify_page.windows_card.set_expanded(False, animate=False)
    _pump(app, 0.2)
    plus_is_bordered = selector.add_btn.objectName() == "PlusButton"
    window.switch_page(2, animate=False)
    selector.set_values([60, 90])
    collapsed_height = selector.panel.height()
    selector._on_add()
    _pump(app, 0.6)
    # 内联下拉栏：展开后真实撑高所在容器（不是浮在上面的弹窗）。
    # 展开/收起动画作用在裁剪容器上（内容高度全程冻结，避免抖动），因此量容器高度。
    panel_host = getattr(selector.panel, "_clip_container", None) or selector.panel
    expanded_height = panel_host.height()
    panel_inline = (not selector.panel.isWindow()
                    and (panel_host is selector or selector.isAncestorOf(panel_host)))
    pending_visible = selector.panel.pending.isVisible()
    _pump(app, 0.2)
    _shot(window, os.path.join(shots, "18d_notify_duration.png"))
    values_before = sorted(selector.panel.values)
    add_spins = len(selector.panel.findChildren(QSpinBox))
    # 反复点 + 号不应堆积出多个待选行
    selector._on_add()
    selector._on_add()
    _pump(app, 0.2)
    add_spins_again = len(selector.panel.findChildren(QSpinBox))
    selector.panel.pending.spin.setValue(45)
    selector.panel.pending._on_confirm()
    _pump(app, 0.3)
    after_add = sorted(selector.panel.values)
    selector.panel._remove(90)
    _pump(app, 0.2)
    after_remove = sorted(selector.panel.values)
    row_count = len(selector.panel.rows_host.findChildren(QFrame))
    selector._toggle()
    _pump(app, 0.6)
    collapsed_again = panel_host.height()
    plus_icon = not selector.add_btn.icon().isNull()
    toggle_icon = not selector.toggle_btn.icon().isNull()
    print(f"[selftest] 时长下拉：勾选框已移除={not has_checkbox} +号有边框={plus_is_bordered} "
          f"图标={plus_icon}/{toggle_icon} 内联={panel_inline} "
          f"高度={collapsed_height}->{expanded_height}->{collapsed_again} 待选行={pending_visible} "
          f"输入行数={add_spins}/{add_spins_again} 值={values_before} 添加后={after_add} "
          f"移除后={after_remove} 值行数={row_count}")
    _check(not has_checkbox and plus_is_bordered and plus_icon and toggle_icon
           and panel_inline and pending_visible and expanded_height > collapsed_height
           and collapsed_again < expanded_height
           and add_spins == 1 and add_spins_again == 1
           and after_add == [45, 60, 90] and after_remove == [45, 60],
           "时长下拉栏：内联展开撑高容器、+ 号弹出唯一待选行、可添加与逐行移除", failures)

    # 收起动画不抖：动画只收缩外层裁剪容器，内容控件自身尺寸全程冻结
    from .ui.anim import clip_holder as _clip_holder_fn

    window.switch_page(2, animate=False)
    _pump(app, 0.2)
    section = window.notify_page.wecom_card
    content = section.content
    holder = _clip_holder_fn(content)
    section.set_expanded(True, animate=False)
    # 等展开高度真正稳定再开动画：卡片里的说明文字与图标会让内容高度在展开后继续
    # 变化（实测 334 -> 362），拿一个偏小的值当动画起点会被判成"回弹了一下"。
    # 占位符提示文字变长后（多了 {unlockTime}）布局会多走几轮，所以要求连续 4 次采样
    # 都不动才认为稳定，避免在中间某个"暂时的平台"上就开动画。
    stable = 0
    previous = -1.0
    for _ in range(40):
        _pump(app, 0.1)
        current = float(holder.height())
        stable = stable + 1 if abs(current - previous) < 0.5 else 0
        previous = current
        if stable >= 4:
            break
    expanded_h = holder.height()
    section.set_expanded(False, animate=True)
    frozen_samples = set()
    heights = []
    for _ in range(18):
        _pump(app, 0.02)
        if holder._height_anim is None:
            break
        frozen_samples.add((content.minimumHeight(), content.maximumHeight()))
        heights.append(holder.height())
    _pump(app, 0.5)
    monotonic = all(b <= a + 2 for a, b in zip(heights, heights[1:]))
    no_bounce = not heights or max(heights) <= expanded_h + 4
    frozen_value = next(iter(frozen_samples)) if frozen_samples else (0, 0)
    frozen_ok = len(frozen_samples) == 1 and frozen_value[0] > 0 and frozen_value[0] == frozen_value[1]
    released = content.minimumHeight() == 0 and content.maximumHeight() == 16777215
    collapsed_now = holder.maximumHeight() == 0 and not holder.isVisible()
    print(f"[selftest] 通知页收起关节：展开高={expanded_h} 冻结样本={frozen_samples} "
          f"高度序列尾部={heights[-3:]} 单调收缩={monotonic} 无回弹={no_bounce} "
          f"终态隐藏={collapsed_now} 解冻={released}")
    _check(frozen_ok and monotonic and no_bounce and collapsed_now and released,
           "通知页卡片收起时只收缩外层容器高度（内容尺寸冻结），不再抖动", failures)
    _pump(app, 0.2)
    _check(not hasattr(window.control_page, "page_entered")
           and not hasattr(window.notify_page, "page_entered"),
           "控制页与通知页与主页/日志页统一使用直接淡入", failures)

    # 另外两处折叠动画也要不抖：全局通知里的"程序追踪时长达到指定时长"下拉栏
    # （在 _EventSection 里、下面还有密码勾选框，不是容器最后一项）。
    window.switch_page(2, animate=False)
    _pump(app, 0.4)
    if selector.is_expanded():
        selector.set_expanded(False, animate=False)
    _pump(app, 0.3)
    selector.set_expanded(True, animate=True)
    selector_probes = {
        "下拉栏标题": selector.toggle_btn,
        "下拉栏": selector,
        "面板": selector.panel,
        "区块": selector.parentWidget(),
        "块内容": window.notify_page.global_card.content,
        "全局卡片标题": window.notify_page.global_card.title_label,
    }
    holder = getattr(selector.panel, "_clip_container", None)
    if holder is not None:
        selector_probes["面板容器"] = holder
    selector_samples = _trace(app, window.notify_page.holder, selector_probes, 0.9)
    _pump(app, 0.4)
    selector_title_jitter = _jitter(selector_samples, "下拉栏标题")
    selector_self_jitter = _jitter(selector_samples, "下拉栏")
    card_title_jitter = _jitter(selector_samples, "全局卡片标题")
    selector_sampled = len(selector_samples.get("下拉栏标题") or [])
    sel_ups, sel_turns = _oscillation(selector_samples, "下拉栏标题")
    print(f"[selftest] 时长下拉栏展开：标题波动={selector_title_jitter}px 下拉栏波动={selector_self_jitter}px "
          f"所在卡片标题波动={card_title_jitter}px 上行={sel_ups} 折返={sel_turns} 采样={selector_sampled}")
    _check(selector_sampled >= 8 and 0 <= selector_title_jitter <= 1
           and 0 <= card_title_jitter <= 1,
           "展开时长下拉栏时标题行与所在卡片不上下跳", failures)
    if selector.is_expanded():
        selector.set_expanded(False, animate=False)
    _pump(app, 0.3)

    # 滚动条区域恒定占位：出现/消失都不改变内容宽度
    candidates = [getattr(window.notify_page, "scroll", None),
                  getattr(window.control_page, "scroll", None),
                  getattr(window.logs_page, "scroll", None),
                  getattr(window.home_page, "scroll", None)]
    scroll_areas = [area for area in candidates if area is not None]
    widths = []
    for area in scroll_areas:
        bar = area.verticalScrollBar()
        bar.setRange(0, 0)
        area._sync_bar_margin()
        _pump(app, 0.1)
        content_hidden = area.widget().width()
        bar.setRange(0, max(1, area.viewport().height()))
        area._sync_bar_margin()
        content_shown = area.widget().width()   # 立即读取（避免重排把范围还原）
        _pump(app, 0.2)
        content_settled = area.widget().width()
        widths.append((content_hidden, content_shown, content_settled))
    # 实现改为给内容控件固定宽度（不再用视口留白），因此断言"三种状态下宽度完全一致"
    ok = all(item[0] > 0 and len(set(item)) == 1 for item in widths)
    print(f"[selftest] 滚动条占位（内容宽度恒定 = 区域宽 - 滚动条宽）：{widths}")
    _check(ok, "滚动条恒定占位：出现/消失时内容宽度不变化（消除展开动画抖动）", failures)

    # ---------------- 本轮新增：通道测试隔离 / 自定义内容 / 勾选动画 / 锁定倒计时 -------
    # 1) 两个"发送"按钮各发各的通道（以前会同时发 Windows 与企业微信）
    sent_calls = []
    import sys as _sys
    _notify_module = _sys.modules[Notifier.__module__]
    original_send_wecom = _notify_module.Notifier._send_wecom

    def _fake_send_wecom(self, url, title, body, on_done=None):
        sent_calls.append((url, body))
        if on_done is not None:      # 企业微信发送现在是异步的（回调在真实实现里由线程池触发）
            on_done(True)
        return True

    try:
        _notify_module.Notifier._send_wecom = _fake_send_wecom
        current = normalize(db.get_notify_settings(None))
        current["windows"] = True
        current["wecom"] = True
        current["wecom_url"] = "https://example.invalid/hook"
        db.set_notify_settings(current, None)
        page = window.notify_page
        page._send_test(CHANNEL_WINDOWS)
        after_windows_test = len(sent_calls)          # 只发 Windows：企业微信不应被调用
        page._send_test(CHANNEL_WECOM)
        after_wecom_test = len(sent_calls)
        print(f"[selftest] 通道隔离：windows 测试后 wecom 调用={after_windows_test}，"
              f"wecom 测试后 wecom 调用={after_wecom_test}，hint={page.test_hint.text()!r}，"
              f"补丁生效={_notify_module.Notifier._send_wecom is _fake_send_wecom}，"
              f"类一致={_notify_module.Notifier is Notifier}，"
              f"设置={normalize(db.get_notify_settings(None))}")
    finally:
        _notify_module.Notifier._send_wecom = original_send_wecom
    _check(after_windows_test == 0 and after_wecom_test == 1,
           "两个通道的“发送”按钮只发送自己所属通道", failures)

    # 2) 自定义通知内容：占位符替换 + 存库 + 真正用于通知正文
    rendered = render_template("“{program}”今日已用 {duration}", program="记事本",
                               duration_seconds=3725, when=time.time())
    page = window.notify_page
    editors = len(page.windows_custom.edits) == 7 and len(page.wecom_custom.edits) == 7
    page.windows_custom.edits["open"].setText("“{program}”启动，本次已用 {duration}")
    page._save_custom()
    stored_custom = normalize(db.get_notify_settings(None))["custom"][CHANNEL_WINDOWS]
    notifier = Notifier(db, getattr(window, "tray_icon", None))
    custom_body = notifier._channel_body(CHANNEL_WINDOWS, "open", "默认文案",
                                         program_id=None, program_name="记事本",
                                         duration_seconds=90)
    _check("记事本" in rendered and "01:02:05" in rendered and editors
           and stored_custom.get("open") == "“{program}”启动，本次已用 {duration}"
           and custom_body == "“记事本”启动，本次已用 00:01:30",
           "自定义通知内容：占位符 {program}/{duration}/{time} 生效并用于通道正文", failures)

    # 2b) {duration} 必须是"本次追踪时长"，默认文案要带 {time}\n\n 前缀，且 \n 真换行
    duration_pid = db.add_program("C:\\windows\\system32\\monitor_duration_probe.exe",
                                  "时长测试程序")
    assert duration_pid is not None
    log_id = db.open_log(duration_pid, 4242, time.time() - 754)
    db.close_log(log_id, time.time())
    session = notifier.session_seconds(duration_pid)
    default_open = tr("notify.body.open")
    default_duration = tr("notify.body.duration")
    session_body = notifier._channel_body(CHANNEL_WINDOWS, "duration", default_duration,
                                          program_id=duration_pid, program_name="时长测试程序",
                                          duration_seconds=session)
    # 默认文案要在"没有自定义内容"的前提下看，所以先把 custom 清空
    clean = normalize(db.get_notify_settings(None))
    clean["custom"] = {"windows": {}, "wecom": {}}
    db.set_notify_settings(clean, None)
    plain_body = notifier._channel_body(CHANNEL_WINDOWS, "open", default_open,
                                        program_id=None, program_name="记事本",
                                        duration_seconds=None)
    newline_ok = ("\n\n" in session_body and "\\n" not in session_body
                  and default_open.startswith("{time}\\n\\n")
                  and default_duration == "{time}\\n\\n“{program}”已使用{duration}")
    print(f"[selftest] 时长占位符：本次追踪时长={session} 正文={session_body!r} "
          f"默认文案={default_open!r} 换行已转换={newline_ok} 默认正文={plain_body!r}")
    _check(session is not None and 750 < session < 760 and newline_ok
           and "00:12:34" in session_body and "“时长测试程序”已使用00:12:34" in session_body
           and plain_body.count("\n\n") == 1 and "“记事本”已启动" in plain_body,
           "{duration} 为本次追踪时长；默认文案带 {time} 换行前缀且 \\n 转为真换行", failures)
    db.remove_programs([duration_pid], keep_logs=False)

    # 2d) {unlockTime}：临时解锁的时长占位符（只在 unlocked 事件里有值）
    unlock_default = tr("notify.body.unlocked")
    unlock_body = notifier._channel_body(CHANNEL_WINDOWS, "unlocked", unlock_default,
                                         program_id=None, program_name="原神",
                                         duration_seconds=None, unlock_minutes=10)
    unlock_render = render_template("{time}\\n\\n“{program}”被临时解锁{unlockTime}",
                                    program="原神", unlock_minutes=10, when=time.time())
    unlock_absent = render_template("{unlockTime}", program="原神")
    print(f"[selftest] {{{{unlockTime}}}}：默认文案={unlock_default!r} 正文={unlock_body!r} "
          f"渲染={unlock_render!r} 无解锁时={unlock_absent!r}")
    _check("被临时解锁10 分钟" in unlock_body and "{unlockTime}" in unlock_default
           and "被临时解锁10 分钟" in unlock_render and unlock_absent == "{unlockTime}",
           "通知新增 {unlockTime} 占位符（临时解锁时长），默认文案读作“被临时解锁10 分钟”", failures)

    # 2e) 企业微信正文不再带"Monitor 通知"标题行（标题只用于托盘气泡）
    payloads = []
    original_urlopen = _notify_module.urllib.request.urlopen

    class _FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _fake_urlopen(request, timeout=None):
        payloads.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse()

    wecom_notifier = Notifier(db, None)
    try:
        _notify_module.urllib.request.urlopen = _fake_urlopen
        wecom_ok = wecom_notifier.notify("test", tr("notify.title"), "正文一行", force=True,
                                         channels=(CHANNEL_WECOM,))
        # 企业微信发送现在跑在线程池里（不能为了 webhook 的 6 秒超时卡住界面或追踪线程），
        # 所以要等队列排空，再撤销 urlopen 补丁。
        waited = _notify_module.QThreadPool.globalInstance().waitForDone(3000)
        _ = waited
    finally:
        _notify_module.urllib.request.urlopen = original_urlopen
    wecom_content = payloads[0]["text"]["content"] if payloads else ""
    print(f"[selftest] 企业微信正文：受理={wecom_ok} payload数={len(payloads)} "
          f"content={wecom_content!r}（不应含标题行）")
    _check(wecom_ok and len(payloads) == 1 and wecom_content == "正文一行"
           and tr("notify.title") not in wecom_content,
           "企业微信通知正文不再包含“Monitor 通知”标题行", failures)

    # 2c) 关掉 Windows 通知后再勾选任何事件，通道开关不能被旧快照改回去
    globals_now = normalize(db.get_notify_settings(None))
    globals_now["windows"] = False
    db.set_notify_settings(globals_now, None)
    page._save_global()
    after_save = normalize(db.get_notify_settings(None))
    print(f"[selftest] 通道开关保护：保存事件勾选后 windows={after_save['windows']} "
          f"（期望 False）")
    _check(after_save["windows"] is False,
           "保存事件勾选不会把已关闭的 Windows/企业微信通道开关改回开启", failures)

    # 3) 勾选框勾选/取消带进度动画
    test_box = CheckBox()
    test_box.setChecked(True)
    test_box.nextCheckState()
    box_animating = test_box._check_anim is not None
    _pump(app, 0.5)
    _check(box_animating and abs(test_box.checkProgress - 0.0) < 0.01
           and test_box._check_anim is None,
           "勾选框勾选/取消勾选有动画（进度属性从 1 动画到 0）", failures)

    # 4) 密码错误锁定后倒计时每秒刷新（每次 tick 重新取剩余时间）
    control_page = window.control_page
    service = control_page.service
    original_remaining = service.lock_remaining
    original_has_password = service.has_password
    original_unlocked = control_page._unlocked
    window.switch_page(3, animate=False)
    _pump(app, 0.3)
    try:
        service.has_password = lambda: True          # type: ignore[assignment]
        service.lock_remaining = lambda: 42.0        # type: ignore[assignment]
        control_page._unlocked = False                # 锁屏态：gate 由 refresh 自己显示
        control_page.refresh()
        _pump(app, 0.2)
        control_page._on_tick()
        first_text = control_page.gate_hint.text()
        service.lock_remaining = lambda: 7.0         # type: ignore[assignment]
        control_page._on_tick()
        second_text = control_page.gate_hint.text()
        service.lock_remaining = lambda: 0.0         # type: ignore[assignment]
        control_page._on_tick()
        third_text = control_page.gate_hint.text()
        print(f"[selftest] 锁定倒计时：visible={control_page.gate.isVisible()} "
              f"first={first_text!r} second={second_text!r} third={third_text!r}")
    finally:
        service.lock_remaining = original_remaining  # type: ignore[assignment]
        service.has_password = original_has_password  # type: ignore[assignment]
        control_page._unlocked = original_unlocked
        control_page.refresh()
    _check("42" in first_text and "7" in second_text and third_text != second_text,
           "控制页锁定文案的秒数每秒实时刷新", failures)

    # 4b) 离开控制页超过 1 分钟再回来：需要重新输入密码
    original_touch = service.touch_session
    original_unlocked = control_page._unlocked
    touched = {"count": 0}
    short_gap_ok = False
    long_gap_locked = False
    try:
        service.has_password = lambda: True          # type: ignore[assignment]
        service.touch_session = lambda: touched.__setitem__("count", touched["count"] + 1)
        control_page._unlocked = True
        control_page.refresh()
        _pump(app, 0.1)
        body_visible = control_page.body.isVisible()

        control_page._left_at = time.time() - (config.SESSION_GRACE - 5)
        control_page._on_page_shown()                 # ≤1 分钟：仍然解锁、并续期
        short_gap_ok = (control_page._unlocked and touched["count"] == 1
                        and control_page.body.isVisible())

        control_page._unlocked = True
        control_page.refresh()
        control_page._left_at = time.time() - (config.SESSION_GRACE + 5)
        control_page._on_page_shown()                 # ＞1 分钟：重新锁上
        long_gap_locked = (not control_page._unlocked and control_page.gate.isVisible()
                           and not control_page.body.isVisible())
        print(f"[selftest] 控制页重新验证：初始正文可见={body_visible} "
              f"≤{config.SESSION_GRACE}s 回顾仍解锁={short_gap_ok} 续期次数={touched['count']} "
              f"＞{config.SESSION_GRACE}s 回顾已锁上={long_gap_locked}")
    finally:
        service.touch_session = original_touch       # type: ignore[assignment]
        service.has_password = original_has_password  # type: ignore[assignment]
        control_page._unlocked = original_unlocked
        control_page.refresh()
    _check(short_gap_ok and long_gap_locked,
           "离开控制页超过 1 分钟后回到该页需要重新输入密码", failures)

    # 4b2) 免验证间隔可配置（新增功能）：按分钟自由输入，0 = 每次进控制页都要密码
    original_grace = service.db.get_float(config.S_SESSION_GRACE, config.SESSION_GRACE / 60.0)
    grace_zero_ok = grace_minutes_ok = False
    try:
        service.db.set_setting(config.S_SESSION_GRACE, 0.0)
        service.touch_session()
        grace_zero_ok = service.session_grace() == 0.0 and not service.session_valid()
        control_page._unlocked = True
        control_page.refresh()
        control_page._left_at = time.time() - 2.0          # 只离开 2 秒也得重新验证
        control_page._on_page_shown()
        grace_zero_ok = grace_zero_ok and not control_page._unlocked
        service.db.set_setting(config.S_SESSION_GRACE, 7.0)
        grace_minutes_ok = service.session_grace() == 420.0 and service.session_valid()
        print(f"[selftest] 控制页免验证间隔：0 分钟=每次都要密码 {grace_zero_ok} "
              f"7 分钟={service.session_grace()}s 仍有效={grace_minutes_ok}")
    finally:
        service.db.set_setting(config.S_SESSION_GRACE, original_grace)
        control_page._unlocked = original_unlocked
        control_page.refresh()
    _check(grace_zero_ok and grace_minutes_ok,
           "设置页可调“离开控制页后重新验证”的间隔（分钟，自由输入），0 = 每次进入都要密码",
           failures)

    # 4b3) 锁定态不再是单卡片：整页内联表单，直接给输入框（回车 / 右侧箭头提交）
    page_unlocked_before = control_page._unlocked
    real_has_password2 = service.has_password
    real_verify2 = service.verify
    real_lock_remaining2 = service.lock_remaining
    real_set_password2 = service.set_password
    setup_form_ok = short_ok = mismatch_ok = verify_form_ok = wrong_ok = unlock_ok = False
    locked_form_ok = False
    live_input_ok = away_ok = keep_input_ok = False
    arrow_outer_ok = inline_ok = stable_ok = keep_ok = brief_ok = False
    saved_pw: dict = {}
    try:
        service.has_password = lambda: False                 # type: ignore[assignment]
        service.lock_remaining = lambda: 0.0                 # type: ignore[assignment]
        service.set_password = lambda pw: saved_pw.setdefault("pw", pw)  # type: ignore[assignment]
        control_page._unlocked = False
        control_page.refresh()
        _pump(app, 0.3)
        first, second = control_page.gate_field, control_page.gate_confirm
        arrow = control_page.gate_submit
        setup_form_ok = (control_page.gate.isVisible() and first.isVisible() and second.isVisible()
                         and not control_page.body.isVisible()
                         and control_page.scroll.height() == 0
                         and control_page.gate_title.text() == tr("control.setup.title"))
        # 箭头按钮在输入框外面右侧：整颗落在框右边、和第一个框同一行，两个框里都没有按钮
        arrow_outer_ok = (arrow.isVisible() and arrow.isEnabled()
                          and arrow.x() >= first.x() + first.width()
                          and abs(arrow.y() - first.y()) <= 2
                          and not first.findChildren(QPushButton)
                          and not second.findChildren(QPushButton))
        first.edit.setText("12")                             # 太短：提示写在第一个框内
        second.edit.setText("12")
        first.edit.returnPressed.emit()
        short_ok = (not saved_pw and first.error_text() == tr("control.error.too_short")
                    and not second.error_text())
        first.edit.setText("1234")                           # 两次不一致：提示写在第二个框内
        second.edit.setText("5678")
        arrow.click()                                        # 框外右侧箭头
        mismatch_ok = (not saved_pw and second.error_text() == tr("control.error.mismatch")
                       and not first.error_text())
        first.edit.setText("1234")                           # 回车设置成功
        second.edit.setText("1234")
        first.edit.returnPressed.emit()
        setup_form_ok = (setup_form_ok and short_ok and mismatch_ok and arrow_outer_ok
                         and saved_pw.get("pw") == "1234")

        service.has_password = lambda: True                  # type: ignore[assignment]
        control_page._unlocked = False
        control_page.refresh()
        _pump(app, 0.3)
        verify_form_ok = (control_page.gate.isVisible() and first.isVisible()
                          and not second.isVisible() and arrow.isVisible())
        service.verify = lambda pw: (False, "wrong")         # type: ignore[assignment]
        width_before = first.width()
        first.edit.setText("bad")
        arrow.click()
        # 输错后输入被清空；提示写在框内右侧（整段落在框里）；框宽不变（提示不再撑开布局）
        inline_ok = (first.edit.text() == ""
                     and first.message.x() >= 0
                     and first.message.x() + first.message.width() <= first.width()
                     and first.message.x() > first.width() * 0.2)
        stable_ok = width_before == first.width()
        first.edit.setText("abc")                            # 用户接着敲
        control_page._on_tick()                              # 每秒 tick 不该抹掉输入与提示
        control_page.refresh()                               # 任何 state_changed 同样不该
        keep_ok = (first.text() == "abc" and first.error_text() == tr("control.error.wrong")
                   and bool(first.property("error")))
        wrong_ok = keep_ok and not control_page._unlocked
        control_page._on_gate_typed("")                      # 再次敲键才清掉提示与红框
        wrong_ok = wrong_ok and first.error_text() == "" and not bool(first.property("error"))
        service.verify = lambda pw: (True, "")               # type: ignore[assignment]
        first.edit.setText("1234")
        first.edit.returnPressed.emit()
        _pump(app, 0.3)
        unlock_ok = (control_page._unlocked and not control_page.gate.isVisible()
                     and control_page.body.isVisible() and control_page.scroll.height() > 0)
        def _verify_just_locked(pw):                         # 密码本身之外：这一次刚好触发锁定
            service.lock_remaining = lambda: 42.0            # type: ignore[assignment]
            return (False, "just_locked")

        service.lock_remaining = lambda: 0.0                 # type: ignore[assignment]
        service.verify = _verify_just_locked                 # type: ignore[assignment]
        control_page._unlocked = False
        control_page.refresh()
        _pump(app, 0.2)
        first.edit.setText("nope")
        arrow.click()
        # 框内长文案已简化：只说"错误次数过多"，秒数只留在标题下方的提示里
        brief_ok = ("{" not in first.error_text()
                    and not any(ch.isdigit() for ch in first.error_text())
                    and len(first.error_text()) < len(tr("control.gate.locked", seconds=42)))
        # 锁定期：输入框照样能改密码，被锁住的只有"提交"（箭头 + 回车）
        locked_form_ok = (first.edit.isEnabled() and not arrow.isEnabled()
                          and "42" in control_page.gate_hint.text() and brief_ok)
        first.edit.setText("typed")
        first.edit.textEdited.emit("typed")                  # 用户真的在敲（setText 不发这个信号）
        first.edit.returnPressed.emit()                      # 锁定期回车：不提交，也不清空
        _pump(app, 0.2)
        live_input_ok = (first.text() == "typed" and not control_page._unlocked
                         and not first.error_text() and first.edit.isEnabled()
                         and not arrow.isEnabled())
        # 锁定期切到别的页面：倒计时也得继续走（老写法看 gate.isVisible()，切走就停住）
        service.lock_remaining = lambda: 30.0                # type: ignore[assignment]
        window.switch_page(0, animate=False)
        _pump(app, 0.25)
        control_page._on_tick()
        away_ok = (not control_page.gate.isVisible()
                   and "30" in control_page.gate_hint.text())
        # 切回控制页：showEvent -> _on_page_shown 按真实剩余时间重新对齐
        service.lock_remaining = lambda: 17.0                # type: ignore[assignment]
        window.switch_page(3, animate=False)
        _pump(app, 0.25)
        away_ok = away_ok and "17" in control_page.gate_hint.text()
        # 倒计时归零：锁定期敲好的密码留着，输入框与箭头恢复，能直接回车解锁
        service.lock_remaining = lambda: 0.0                 # type: ignore[assignment]
        service.verify = lambda pw: (True, "")               # type: ignore[assignment]
        first.edit.setText("1234")
        control_page._on_tick()
        _pump(app, 0.2)
        keep_input_ok = (first.text() == "1234" and first.edit.isEnabled()
                         and arrow.isEnabled() and "秒" not in control_page.gate_hint.text())
        first.edit.returnPressed.emit()
        _pump(app, 0.3)
        keep_input_ok = keep_input_ok and control_page._unlocked
        print(f"[selftest] 控制页锁屏：整页表单={setup_form_ok} 校验={short_ok}/{mismatch_ok} "
              f"箭头在框外={arrow_outer_ok} 提示在框内={inline_ok} 框宽不变={stable_ok} "
              f"输入抗 tick={keep_ok} 单框={verify_form_ok} 输错变红={wrong_ok} "
              f"解锁进正文={unlock_ok} 长文案简化={brief_ok} 锁定期只禁提交={locked_form_ok} "
              f"锁定期可改密码={live_input_ok} 切页倒计时={away_ok} 归零留输入={keep_input_ok}"
              f"（{control_page.gate_hint.text()}）")
    finally:
        service.has_password = real_has_password2            # type: ignore[assignment]
        service.verify = real_verify2                        # type: ignore[assignment]
        service.lock_remaining = real_lock_remaining2        # type: ignore[assignment]
        service.set_password = real_set_password2            # type: ignore[assignment]
        control_page._unlocked = page_unlocked_before
        control_page.refresh()
        _pump(app, 0.2)
    _check(setup_form_ok and verify_form_ok and wrong_ok and unlock_ok and locked_form_ok
           and arrow_outer_ok and inline_ok and stable_ok and keep_ok
           and live_input_ok and away_ok and keep_input_ok,
           "控制页锁定态是整页内联表单（锁图标 + 密码框，回车或点框外右侧的箭头按钮解锁，"
           "错误提示写在框内右侧、出现时输入框宽度不变、一直留到用户再次敲键；"
           "首次设置用同一套表单的两个输入框，锁定时长文案简化成“错误次数过多”），"
           "密码错误锁定期间输入框仍可改密码、只有回车与箭头不生效，"
           "切到别的页面倒计时照走、归零后输入不丢",
           failures)

    # 4c) Monitor 自己不能被加入"程序使用限制"（开发运行时主体是 pythonw.exe）
    self_path_ok = False
    rule_blocked = False
    hidden_from_picker = False
    legacy_cleaned = False
    self_pids = []
    try:
        self_pid = db.add_program(_sys.executable, "Monitor")
        assert self_pid is not None
        self_pids.append(int(self_pid))
        self_path_ok = util.is_self_path(_sys.executable)
        service.save_rule(Rule(program_id=int(self_pid), daily_value=60,
                               streak_value=60, pause_value=10))
        rule_blocked = int(self_pid) not in service.all_rules()
        addable = [int(p["id"]) for p in control_page._addable_programs()]
        hidden_from_picker = int(self_pid) not in addable and addable
        # 历史遗留的自规则：reload() 顺手清掉，不能留在库里
        db.set_control_rules(int(self_pid), daily_enabled=True, daily_value=60, daily_unit="m",
                             streak_enabled=False, streak_value=0, streak_unit="m",
                             pause_value=10, pause_unit="m")
        service.reload()
        legacy_cleaned = int(self_pid) not in db.all_control_rules()
        print(f"[selftest] Monitor 自我保护：路径识别={self_path_ok} "
              f"save_rule 被拒={rule_blocked} 添加弹窗隐藏={hidden_from_picker} "
              f"历史规则清理={legacy_cleaned} 可选程序数={len(addable)}")
    finally:
        if self_pids:
            db.remove_programs(self_pids, keep_logs=False)
        service.reload()
    _check(bool(self_path_ok and rule_blocked and hidden_from_picker and legacy_cleaned),
           "Monitor 自己不能被加入程序使用限制（保存与添加弹窗都拒绝）", failures)

    # 4d) 关闭"启用控制"不再需要密码（用户 ④：能进这一页本身就说明刚验过密码）
    enable_calls: list = []
    refreshed = {"n": 0}

    class _FakeEnableSwitch:
        def setChecked(self, *args, **kwargs) -> None:
            enable_calls.append(("setChecked", args[:1]))

    class _FakeEnableService:
        def set_enabled(self, value) -> None:
            enable_calls.append(("enabled", bool(value)))

    class _FakeControlPage:
        def __init__(self) -> None:
            self.service = _FakeEnableService()
            self.enable_switch = _FakeEnableSwitch()

        def window(self):
            return None

        def refresh(self) -> None:
            refreshed["n"] += 1

    class _NoPasswordDialog:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("关闭总开关时不应该弹密码框")

    real_password_dialog = control_page_module._PasswordDialog
    close_source = ""
    control_page_module._PasswordDialog = _NoPasswordDialog     # type: ignore[assignment]
    try:
        control_page_module.ControlPage._on_enable_toggled(_FakeControlPage(), False)
        close_source = _source_of(control_page_module.ControlPage._on_enable_toggled)
        close_without_password_ok = (enable_calls == [("enabled", False)]
                                     and refreshed["n"] == 1
                                     and (not close_source
                                          or "_PasswordDialog" not in close_source))
    finally:
        control_page_module._PasswordDialog = real_password_dialog  # type: ignore[assignment]
    print(f"[selftest] 关闭总开关免密码：set_enabled={enable_calls} "
          f"refresh={refreshed['n']} 关闭分支无密码框="
          f"{'_PasswordDialog' not in close_source if close_source else '跳过（打包后无源码）'}")
    _check(close_without_password_ok,
           "控制页关闭“启用控制”不再要求输入密码（直接关掉并刷新）", failures)

    # 4e) 退出闸门条件：只有"启用控制"开着才要密码（用户 ⑤）
    from .app import quit_needs_password

    class _GateEngine:
        def __init__(self, enabled: bool) -> None:
            self.enabled = enabled

    class _GateService:
        def __init__(self, enabled: bool, has_password: bool) -> None:
            self.engine = _GateEngine(enabled)
            self._has_password = has_password

        def has_password(self) -> bool:
            return self._has_password

    gate_ok = (quit_needs_password(None) is False
               and quit_needs_password(_GateService(True, True)) is True
               and quit_needs_password(_GateService(True, False)) is False
               and quit_needs_password(_GateService(False, True)) is False)
    print(f"[selftest] 退出闸门条件：无服务=False 控制开+有密码={quit_needs_password(_GateService(True, True))} "
          f"控制开+无密码={quit_needs_password(_GateService(True, False))} "
          f"控制关={quit_needs_password(_GateService(False, True))}")
    _check(gate_ok,
           "退出闸门只在“启用控制”开着时才要求密码（没设过密码 / 控制没开都不拦）", failures)

    # 4f) 退出密码框：每次都问、密码错可以重试、取消就放弃、锁定期间不弹框；
    #     并且确认四条用户路径都接上了这道闸门（用户 ⑤）。
    from . import app as app_module
    from .ui import settings_page as settings_page_module

    class _FakeEdit:
        def clear(self) -> None:
            return None

    class _FakePasswordDialog:
        answers: list = []
        text = "pw"
        built = 0

        def __init__(self, parent, title, hint) -> None:
            self.errors: list = []
            self.edit = _FakeEdit()
            _FakePasswordDialog.built += 1

        def exec(self) -> int:
            return _FakePasswordDialog.answers.pop(0) if _FakePasswordDialog.answers \
                else QDialog.Rejected

        def password(self) -> str:
            return _FakePasswordDialog.text

        def set_error(self, text: str) -> None:
            self.errors.append(text)

    class _AskService:
        def __init__(self, password: str = "pw", has_password: bool = True,
                     remaining: float = 0.0) -> None:
            self._password = password
            self._has_password = has_password
            self._remaining = remaining
            self.verify_calls: list = []

        def has_password(self) -> bool:
            return self._has_password

        def lock_remaining(self) -> float:
            return self._remaining

        def verify(self, password: str) -> tuple:
            self.verify_calls.append(password)
            if password == self._password:
                return True, ""
            return False, "wrong"

    class _FakeMessageBox:
        info: list = []

        @staticmethod
        def information(parent, title, text):
            _FakeMessageBox.info.append(text)

        @staticmethod
        def question(*args, **kwargs):
            return None

    ask_ok = False
    real_message_box = control_page_module.QMessageBox
    control_page_module._PasswordDialog = _FakePasswordDialog   # type: ignore[assignment]
    control_page_module.QMessageBox = _FakeMessageBox           # type: ignore[assignment]
    try:
        _FakePasswordDialog.answers = [QDialog.Accepted]
        _FakePasswordDialog.text = "pw"
        good = control_page_module.ask_exit_password(None, _AskService())
        good_calls = _FakePasswordDialog.built

        _FakePasswordDialog.answers = [QDialog.Accepted, QDialog.Rejected]
        _FakePasswordDialog.text = "wrong"
        canceled = control_page_module.ask_exit_password(None, _AskService())
        retried = _FakePasswordDialog.built

        _FakePasswordDialog.answers = []
        _FakePasswordDialog.text = "pw"
        before = _FakePasswordDialog.built
        no_password = control_page_module.ask_exit_password(
            None, _AskService(has_password=False))
        skipped_dialog = _FakePasswordDialog.built == before

        _FakePasswordDialog.answers = []
        locked = control_page_module.ask_exit_password(None, _AskService(remaining=5.0))
        locked_hint = bool(_FakeMessageBox.info) and _FakePasswordDialog.built == before

        admin_restart_src = _source_of(control_page_module.ControlPage._on_admin_restart)
        admin_toggled_src = _source_of(settings_page_module.SettingsPage._on_admin_toggled)
        quit_src = _source_of(app_module.MonitorApp.quit)
        session_end_src = _source_of(app_module.MonitorApp._on_session_end)
        app_src = _source_of(app_module)
        # 打包成 exe 后没有 .py 文件可读，这几条"读源码"的子断言只能跳过；
        # 同一个校验里的行为断言（正确密码放行、错误重试、锁定提示等）照常执行。
        has_source = bool(admin_restart_src and admin_toggled_src and quit_src
                          and session_end_src and app_src)
        wiring_ok = (not has_source) or (
            "ask_exit_password" in admin_restart_src
            and "ask_exit_password" in admin_toggled_src
            and "force: bool = False" in quit_src
            and "_pass_quit_gate" in quit_src
            and "quit(force=True)" in session_end_src
            and "lambda: monitor.quit(force=True)" in app_src
            and "quit_action.triggered.connect" in app_src
        )
        ask_ok = (good is True and good_calls == 1
                  and canceled is False and retried == 2
                  and no_password is True and skipped_dialog
                  and locked is False and locked_hint
                  and wiring_ok)
        print(f"[selftest] 退出密码框：正确密码放行={good is True} 密码错重试后取消="
              f"{canceled is False} 未设密码不弹框={skipped_dialog} "
              f"锁定期间提示不弹框={locked_hint} "
              f"四条路径接线={wiring_ok if has_source else '跳过（打包后无源码）'}")
    finally:
        control_page_module._PasswordDialog = real_password_dialog  # type: ignore[assignment]
        control_page_module.QMessageBox = real_message_box          # type: ignore[assignment]
    _check(ask_ok,
           "退出 Monitor（托盘退出 / 关闭按钮 / Alt+F4）与提权重启都要输密码，"
           "每次都问；密码错可重试、取消即放弃、锁定期间只提示不弹框", failures)

    # 5) 文案修正：未加入控制页 -> 第三至五项
    _check("第三至五项" in tr("notify.independent.not_in_control"),
           "独立设置提示文案为“第三至五项不可用”", failures)

    # 通知设置：全局 + 独立
    window.notify_page.refresh()
    window.switch_page(2, animate=False)
    _pump(app, 0.5)
    _shot(window, os.path.join(shots, "18_notify_settings.png"))

    db.set_setting(db.notify_key(None), "")
    global_settings = normalize(db.get_notify_settings(None))
    global_settings["events"]["open"] = True
    global_settings["durations"] = [60, 75]
    db.set_notify_settings(global_settings, None)
    reloaded = normalize(db.get_notify_settings(None))
    _check(reloaded["events"]["open"] and reloaded["durations"] == [60, 75],
           "通知设置可保存事件勾选与多个时长条件", failures)

    if program_id is not None:
        own = normalize(None)
        own["events"]["close"] = True
        db.set_notify_settings(own, program_id)
        notifier = Notifier(db, None)
        _check(notifier._settings_for(None)["events"]["open"]
               and notifier._settings_for(program_id)["events"]["close"],
               "按程序独立的通知设置与全局设置分别生效", failures)
        sent = {"count": 0}

        class _FakeTray:
            def showMessage(self, title, body):
                sent["count"] += 1

        notifier2 = Notifier(db, _FakeTray())
        own["durations"] = [1]
        db.set_notify_settings(own, program_id)
        global_settings = normalize(db.get_notify_settings(None))
        global_settings["windows"] = True
        db.set_notify_settings(global_settings, None)
        notifier2.on_duration(program_id, "测试", 61)
        notifier2.on_duration(program_id, "测试", 61)      # 同一阈值不应重复通知
        print(f"[selftest] 时长阈值通知次数={sent['count']}（应为 1）")
        _check(sent["count"] == 1,
               "追踪时长达到阈值时通知且同一阈值只通知一次", failures)
        # 独立设置卡片必须显示程序自己的图标（不是空白方块）
        from .ui.controls import IconTile

        window.notify_page._rebuild_independent()
        _pump(app, 0.3)
        card = window.notify_page._independent_cards.get(program_id)
        tiles = card.findChildren(IconTile) if card is not None else []
        has_icon = bool(tiles) and tiles[0].pixmap() is not None \
            and not tiles[0].pixmap().isNull()
        print(f"[selftest] 独立设置卡片图标：卡片存在={card is not None} 图标有效={has_icon}")
        _check(has_icon, "通知页独立设置卡片显示程序图标", failures)
        # 独立设置卡片（与"三个主卡片"同一个 _Section，但套在独立设置容器里）展开也不能抖
        window.switch_page(2, animate=False)
        _pump(app, 0.4)
        if card is not None:
            card.set_expanded(False, animate=False)
            _pump(app, 0.3)
            card.set_expanded(True, animate=True)
            own_probes = {"卡片标题": card.title_label, "卡片": card,
                          "卡片内容": card.content,
                          "所属容器": card.parentWidget()}
            own_holder = getattr(card.content, "_clip_container", None)
            if own_holder is not None:
                own_probes["卡片容器"] = own_holder
            own_samples = _trace(app, window.notify_page.holder, own_probes, 0.9)
            _pump(app, 0.3)
            own_title_jitter = _jitter(own_samples, "卡片标题")
            own_self_jitter = _jitter(own_samples, "卡片")
            own_ups, own_turns = _oscillation(own_samples, "卡片标题")
            own_sampled = len(own_samples.get("卡片标题") or [])
        else:
            own_title_jitter = own_self_jitter = 99
            own_ups = own_turns = -1
            own_sampled = 0
        print(f"[selftest] 独立设置卡片展开：标题波动={own_title_jitter}px "
              f"卡片波动={own_self_jitter}px 上行={own_ups} 折返={own_turns} 采样={own_sampled}")
        _check(own_sampled >= 8 and 0 <= own_title_jitter <= 1,
               "展开独立设置卡片时标题行不上下跳", failures)
        # 拖动排序：卡片左侧要有拖动手柄，并且已按 order.notify.* 注册进同一套排序控制器
        handle_cards = [item for item in window.notify_page._independent_cards.values()
                        if getattr(item, "drag_handle", None) is not None
                        and getattr(item, "program_id", None) is not None]
        notify_order = [pid for pid in window.notify_page.reorder.order("program")
                        if isinstance(pid, int)]
        print(f"[selftest] 通知页独立设置排序：带手柄卡片={len(handle_cards)} "
              f"已注册顺序={notify_order}")
        _check(bool(handle_cards) and program_id in notify_order,
               "通知页独立设置卡片带拖动手柄，并注册进按数据库顺序的排序控制器", failures)
        window.switch_page(2, animate=False)
        _pump(app, 0.4)
        _shot(window, os.path.join(shots, "18b_notify_independent.png"))

        db.set_setting(db.notify_key(program_id), "")
        window.notify_page._rebuild_independent()
        _pump(app, 0.3)
        removed_shown = program_id in window.notify_page._independent_cards
        db.delete_setting(db.notify_key(program_id))
        window.notify_page._rebuild_independent()
        _pump(app, 0.3)
        print(f"[selftest] 移除独立设置：空值仍显示={removed_shown} "
              f"整行删除后仍显示={program_id in window.notify_page._independent_cards}")
        _check(not removed_shown and program_id not in window.notify_page._independent_cards,
               "通知页“移除此独立设置”生效（空值记录不再显示）", failures)

    # 日志页：按时间分类 + 日期分隔
    logs = window.logs_page
    window.switch_page(1, animate=False)
    logs.set_mode("date")
    _pump(app, 0.5)
    part_count = len(logs.records)
    separators = [w for w in logs.holder.findChildren(QWidget)
                  if w.parent() is not None and w.layout() is not None
                  and w.findChildren(QLabel)]
    _check(logs.mode == "date" and logs.date_header.isVisible(),
           "日志页下拉新增“按时间分类”并可筛选年月日", failures)
    _check(part_count >= 0 and logs.year_select.current_key() != "",
           "按时间分类会列出可选的年份", failures)
    if part_count:
        first = logs.records[0]["start_ts"]
        import time as _time
        local = _time.localtime(first)
        logs.year_select.set_current(str(local.tm_year), emit=False)
        logs.month_select.set_options(
            [(str(local.tm_mon), str(local.tm_mon)), ("", tr("logs.date.all"))],
            keep=str(local.tm_mon))
        filtered = [r for r in logs.records]
        _check(bool(filtered) or part_count == 0, "按年份筛选可返回记录", failures)
    _shot(window, os.path.join(shots, "19_logs_date.png"))
    logs.set_mode("time")
    _pump(app, 0.4)
    day_labels = []
    for child in logs.holder.findChildren(QLabel):
        text = child.text()
        if len(text) >= 10 and text[:4].isdigit() and text[4] == "-":
            day_labels.append(text)
    print(f"[selftest] 日志页日期分隔条={day_labels[:3]}")
    _check(bool(day_labels), "日志页按日期分隔不同日期的记录", failures)

def _site_checks(app, window, db, failures: list, shots: str) -> None:
    """网站追踪与控制：匹配规则 / 数据库 / 监视器 / 追踪器 / 遮罩 / 首页·控制页·添加弹窗。"""
    from PySide6.QtCore import QRect

    from . import site_watch
    from .site_watch import PageInfo
    from .tracker import ProcessTracker
    from .ui import cards as cards_module
    from .ui.dialogs import AddSitesDialog

    page = window.control_page
    service = window.control
    home = window.home_page
    print("[selftest] —— 网站追踪与控制 ——")

    # 1) 匹配规则：忽略协议与 www、* 通配、子域、路径、大小写
    cases = (
        ("bilibili.com", "https://www.bilibili.com/", True),
        ("bilibili.com", "http://bilibili.com/video/BV1", True),
        ("bilibili.com", "https://live.bilibili.com/1", False),
        ("bilibili.com", "https://www.bilibili.com.evil.com/", False),
        ("bilibili.com/*", "https://live.bilibili.com/1", True),
        ("*.bilibili.com", "https://live.bilibili.com/1", True),
        ("*.bilibili.com", "https://www.bilibili.com/1", True),
        ("*.bilibili.com", "https://bilibili.com/1", False),
        ("https://www.bilibili.com/video/*", "https://www.bilibili.com/video/BV1?p=2", True),
        ("https://www.bilibili.com/video/*", "https://www.bilibili.com/", False),
        ("BILIBILI.COM", "https://WWW.BiliBili.com/x", True),
        ("localhost:3000", "http://localhost:3000/index", True),
        ("bilibili.com", "edge://newtab", False),
        ("bilibili.com", "dsh-app://app/", False),
    )
    wrong = [f"{p} × {u}（期望 {want}，实际 {site_watch.pattern_matches(p, u)}）"
             for p, u, want in cases if site_watch.pattern_matches(p, u) != want]
    print(f"[selftest] 网站匹配：{len(cases)} 个用例，不符合期望 {len(wrong)} 个 {wrong}")
    _check(not wrong,
           "网站匹配规则：忽略协议与 www、支持 * 通配与子域、可带路径、大小写不敏感", failures)

    # 2) 归一化 / 显示名 / 输入合法性 / 是否真正的网页
    norm_ok = (site_watch.normalize_pattern("https://www.bilibili.com/*") == "bilibili.com/*"
               and site_watch.normalize_pattern("HTTPS://WWW.Bilibili.com/video/?a=1#b")
               == "bilibili.com/video"
               and site_watch.normalize_pattern("  *.live.bilibili.com/  ") == "*.live.bilibili.com"
               and site_watch.display_name("https://www.bilibili.com/*") == "bilibili.com"
               and site_watch.display_name("*.live.bilibili.com") == "live.bilibili.com"
               and site_watch.looks_like_host("https://www.bilibili.com/video/*")
               and site_watch.looks_like_host("localhost:3000")
               and not site_watch.looks_like_host("这是啥")
               and not site_watch.looks_like_host("just-text"))
    _check(norm_ok,
           "添加网站时统一归一化（去协议 / www / 查询串 / 端口），并挡掉不是域名的输入", failures)

    web_ok = (site_watch.is_web_url("https://bilibili.com/x")
              and site_watch.is_web_url("http://127.0.0.1:8000/x")
              and not site_watch.is_web_url("dsh-app://app/")
              and not site_watch.is_web_url("tauri.localhost/")
              and not site_watch.is_web_url("edge://newtab"))
    _check(web_ok,
           "只有 http(s) 网页算“网站”（应用自身界面与浏览器内部页不计时）", failures)

    # 3) 数据库：网站与程序同表，用 kind 区分
    site_id = db.add_site("https://www.bilibili.com/*")
    duplicate = db.add_site("bilibili.com/*")
    site_row = db.get_program(int(site_id)) if site_id else None
    db_ok = (site_id is not None and duplicate is None and site_row is not None
             and str(site_row["kind"]) == "site"
             and str(site_row["path"]) == "bilibili.com/*"
             and str(site_row["name"]) == "bilibili.com"
             and int(site_id) in [int(p["id"]) for p in db.sites(visible_only=True)]
             and int(site_id) not in [int(p["id"]) for p in
                                      db.programs(visible_only=True, kind="program")])
    print(f"[selftest] 网站入库：id={site_id} 重复添加={duplicate} "
          f"名称={None if site_row is None else site_row['name']}")
    _check(db_ok, "添加网站写入同一张表（kind='site'，默认名取域名），重复域名自动去重", failures)
    if site_id is None:
        return
    site_id = int(site_id)

    # 4) 监视器：按"前台窗口当前显示的网址"判定（桩 reader，不依赖真实浏览器）
    shown = {"url": "https://www.bilibili.com/video/BV1"}
    page_rect = (10, 20, 810, 620)        # 页面内容区（left, top, right, bottom）：不含标签栏

    def _reader(hwnd: int):
        if not hwnd:
            return None
        return PageInfo(hwnd=int(hwnd), url=shown["url"], title="哔哩哔哩",
                        root_pid=9001, renderer_pid=9002, rect=page_rect,
                        renderer_hwnd=0x5A5B)

    watcher = site_watch.SiteWatcher(reader=_reader)
    watcher.configure([{"id": site_id, "kind": "site", "tracking": 1, "path": "bilibili.com"}])
    real_foreground = site_watch.foreground_window
    real_window_pid = site_watch.window_pid
    site_watch.foreground_window = lambda: 0x5A5A5A
    site_watch.window_pid = lambda hwnd: 999999        # 不是本进程：不走"前台是自己"的粘滞分支
    try:
        first = watcher.scan(now=1000.0).get(site_id) or {}
        again = watcher.scan(now=1004.0).get(site_id) or {}
        shown["url"] = "https://www.example.com/"
        # 4-1) 这一拍读出来的网址不是这个网站（切标签、页面跳转、UIA 拿到中间态）：
        #      URL_MISS_GRACE_S 之内必须继续遮挡，否则就是用户看到的"闪没"（②）。
        grace = watcher.scan(now=1008.0).get(site_id) or {}
        # 超过确认期才真的释放（窗口仍然在前台、只是网址不再是这个网站）
        gone = watcher.scan(now=1010.0)
        watch_ok = (int(first.get("hwnd") or 0) == 0x5A5A5A
                    and int(first.get("root_pid") or 0) == 9001
                    and int(first.get("renderer_pid") or 0) == 9002
                    and tuple(first.get("rect") or ()) == page_rect
                    and float(again.get("start") or 0) == float(first.get("start") or 0)
                    and bool(grace)
                    and float(grace.get("start") or 0) == float(first.get("start") or 0)
                    and gone == {} and watcher.active_ids() == set())
    finally:
        site_watch.foreground_window = real_foreground
        site_watch.window_pid = real_window_pid
    print(f"[selftest] 网站监视器：命中={bool(first)} "
          f"起始时间沿用={float(again.get('start') or 0) == float(first.get('start') or 0)} "
          f"网址瞬断仍在遮挡={bool(grace)} 超过确认期清空={gone == {}}")
    _check(watch_ok,
           "监视器按前台窗口当前显示的网址计时（同一页面连续使用不重开一条记录）；"
           f"读出的网址瞬时不是该网站时给 {site_watch.SiteWatcher.URL_MISS_GRACE_S}s "
           "确认期不撤遮罩，超时才释放", failures)

    # 4b) 保活：前台不再是那个网页时（去点遮罩上的按钮 / 切到微信 / 盯着画中画小窗），
    #     只要那个浏览器窗口还在、没最小化、当前标签页仍是这个网站，就继续算"在用"
    #     ——这是"遮罩一失焦就消失"的修复；保活期间照常计时（用户确认过的口径）。
    hold_shown = {"url": "https://www.bilibili.com/video/BV1"}
    hold_hwnd = int(window.winId())       # 真实窗口：保活要求"窗口还在且没最小化"
    other_hwnd = 0x7777

    def _hold_reader(hwnd: int):
        if int(hwnd) != hold_hwnd:
            return None                    # 前台是别的窗口：读不到网页
        return PageInfo(hwnd=hold_hwnd, url=hold_shown["url"], title="哔哩哔哩",
                        root_pid=9001, renderer_pid=9002, rect=page_rect,
                        renderer_hwnd=0x5A5B)

    hold_watcher = site_watch.SiteWatcher(reader=_hold_reader)
    hold_watcher.configure([{"id": site_id, "kind": "site", "tracking": 1, "path": "bilibili.com"}])
    real_fg = site_watch.foreground_window
    site_watch.foreground_window = lambda: hold_hwnd
    site_watch.window_pid = lambda hwnd: 999999        # 别让"前台是自己"的粘滞分支顶上
    try:
        live = hold_watcher.scan(now=2000.0).get(site_id) or {}
        site_watch.foreground_window = lambda: other_hwnd
        held = hold_watcher.scan(now=2001.0).get(site_id) or {}
        held_again = hold_watcher.scan(now=2002.0).get(site_id) or {}
        hold_ok = (bool(live) and not live.get("held")
                   and bool(held) and held.get("held") is True
                   and int(held.get("hwnd") or 0) == hold_hwnd
                   and float(held.get("start") or 0) == float(live.get("start") or 0)
                   and float(held_again.get("start") or 0) == float(live.get("start") or 0)
                   and site_id in hold_watcher.active_ids())
        # 换成别的标签页：隔几秒复查一次 UIA，发现网址不再匹配就立刻释放
        hold_shown["url"] = "https://www.example.com/"
        lost = hold_watcher.scan(now=2004.0)
        hold_release_ok = lost == {} and hold_watcher.active_ids() == set()
        # 窗口被最小化 / 关掉：立刻释放（最小化时遮罩本来就该消失）。
        # 注意得先回到浏览器前台重新命中一次——释放后保活记忆已清空，只要前台还是
        # 别的窗口就没有任何东西可以让它自己"活过来"。
        hold_shown["url"] = "https://www.bilibili.com/video/BV1"
        site_watch.foreground_window = lambda: hold_hwnd
        resumed = hold_watcher.scan(now=2005.0).get(site_id) or {}
        site_watch.foreground_window = lambda: other_hwnd
        held_again2 = hold_watcher.scan(now=2005.5).get(site_id) or {}
        real_visible_fn = site_watch.is_window_visible
        site_watch.is_window_visible = lambda hwnd: False
        try:
            minimized = hold_watcher.scan(now=2006.0)
        finally:
            site_watch.is_window_visible = real_visible_fn
        hold_release_ok = (hold_release_ok and bool(resumed)
                           and bool(held_again2) and held_again2.get("held") is True
                           and minimized == {})
        # 4b-2) 复查时 UIA 偶尔读不出页面（页面在重排 / 浏览器刚被切到前台）不能立刻丢掉
        #       保活，否则遮罩会"消失一下又回来"（用户报告的 bug）；连续 HOLD_FAIL_MAX
        #       次读失败才放弃。注意先要前台命中一次，保活记忆才有东西。
        hold_shown["url"] = "https://www.bilibili.com/video/BV1"
        site_watch.foreground_window = lambda: hold_hwnd
        relive = hold_watcher.scan(now=3000.0).get(site_id) or {}
        site_watch.foreground_window = lambda: other_hwnd
        real_read_fn = hold_watcher._read
        hold_watcher._read = lambda hwnd: None
        try:
            tolerated = [bool(hold_watcher.scan(now=3003.0 + index * 3.0).get(site_id))
                         for index in range(2)]
            given_up = hold_watcher.scan(now=3012.0).get(site_id) is None
        finally:
            hold_watcher._read = real_read_fn
        fail_tolerate_ok = bool(relive) and tolerated == [True, True] and given_up
    finally:
        site_watch.foreground_window = real_fg
        site_watch.window_pid = real_window_pid
    print(f"[selftest] 网站保活：前台命中={bool(live)} 失焦后仍算在用={bool(held)} "
          f"保活标记={None if not held else bool(held.get('held'))} 起始时间不变="
          f"{None if not held else float(held.get('start') or 0) == float(live.get('start') or 0)} "
          f"切标签释放={hold_release_ok} 读失败容错={fail_tolerate_ok}")
    _check(hold_ok and hold_release_ok and fail_tolerate_ok,
           "网站判定保活：失焦后（切到别的程序 / 点画中画）遮罩仍显示且照常计时，"
           "切标签页 / 最小化 / 关窗口后立即释放；复查时单次 UIA 读失败不会让遮罩闪没"
           f"（连续 {site_watch.SiteWatcher.HOLD_FAIL_MAX} 次才释放）", failures)

    # 5) 追踪器：网站与程序共用日志流程（只装这个网站，避免影响别的时间线）
    probe = ProcessTracker(db, interval=30.0)
    probe.set_programs([dict(site_row)])
    registered = (site_id in probe._sites
                  and probe.site.patterns().get(site_id) == "bilibili.com/*")
    synthetic = {site_id: {"program_id": site_id, "hwnd": 0x5A5A5A,
                           "url": "https://www.bilibili.com/", "title": "",
                           "root_pid": 9001, "renderer_pid": 9002, "start": time.time()}}
    real_probe_scan = probe.site.scan
    try:
        probe.site.scan = lambda now=None: dict(synthetic)
        probe._scan()
        running = site_id in [int(row["program_id"]) for row in db.active_logs()]
        snapshot_ok = site_id in probe.snapshot()
        probe.site.scan = lambda now=None: {}
        probe._scan()
        stopped = site_id not in [int(row["program_id"]) for row in db.active_logs()]
        snapshot_cleared = site_id not in probe.snapshot()
    finally:
        probe.site.scan = real_probe_scan
    print(f"[selftest] 网站追踪：注册={registered} 运行日志={running} 快照={snapshot_ok} "
          f"切走结束日志={stopped} 快照清空={snapshot_cleared}")
    _check(registered and running and snapshot_ok and stopped and snapshot_cleared,
           "网站进入追踪器：匹配时写运行日志（与程序同一套），切走后自动结束", failures)

    # 6) 限制目标：窗口 / 静音进程 / 拦截进程分开给
    own_hwnd = int(window.winId())
    real_patterns = service.tracker.site.patterns
    real_snapshot = service.tracker.site.snapshot
    real_pause = site_watch.pause_media
    real_pause_probe = service._pause_probe
    real_pause_send = service._pause_send
    real_smtc = service._smtc
    paused = {"n": 0}

    class _FakeSmtc:
        """系统媒体会话（SMTC）的替身：不碰真实 WinRT，只记下 arm 调用与"能不能用"。"""

        def __init__(self, usable: bool = False) -> None:
            self._usable = usable
            self.armed: list = []

        def usable(self) -> bool:
            return self._usable

        def arm(self, pids) -> bool:
            self.armed.append({int(p) for p in (pids or ()) if p})
            return self._usable

        def stop(self, timeout: float = 2.0) -> None:
            return None

        def stats(self) -> dict:
            return {"armed": self.armed[-1] if self.armed else set()}

    def _fake_pause() -> bool:
        paused["n"] += 1
        return True

    service.tracker.site.patterns = lambda: {site_id: "bilibili.com/*"}
    site_page = {"program_id": site_id, "hwnd": own_hwnd, "url": "https://www.bilibili.com/",
                 "title": "哔哩哔哩", "root_pid": 9001, "renderer_pid": 9002,
                 "renderer_hwnd": 0x5A5B, "rect": page_rect, "start": 0.0}
    service.tracker.site.snapshot = lambda: {site_id: dict(site_page)}
    # 暂停键现在是"状态门控"：注入假的"正在出声的进程集合"和发送器，不依赖真实浏览器
    # 也能确定性验证（旧写法直接改 site_watch.pause_media 已无效——服务启动时就把
    # 发送器绑成自己的字段了）。SMTC 监听器也换成替身：主通道与回退通道要分别验证。
    service._smtc = _FakeSmtc(False)
    service._smtc_pids = set()
    service._pause_probe = lambda: {9001, 9002}
    service._pause_send = _fake_pause
    real_visible = overlay.visible_windows
    was_unlocked = page._unlocked
    program_rule_id = 0
    try:
        idle_targets = service._site_targets()
        service._sync_site_playback()
        idle_ok = idle_targets == {} and paused["n"] == 0

        service.save_rule(Rule(program_id=site_id, daily_enabled=True, daily_value=1,
                               streak_enabled=True, streak_value=30, pause_value=1))
        state = service.engine.state(site_id)
        service.engine._advance_day(state, time.time())
        state.used_seconds = 999
        targets = service._site_targets()
        info = targets.get(site_id) or {}
        pages = info.get("pages") or []
        split_ok = (info.get("windows") == [own_hwnd]
                    and info.get("mute_pids") == {9001, 9002}
                    and info.get("block_pids") == set()
                    and tuple(info.get("rect") or ()) == page_rect
                    and int(info.get("renderer_hwnd") or 0) == 0x5A5B
                    and len(pages) == 1 and pages[0].get("compact") is False
                    and int(pages[0].get("hwnd") or 0) == own_hwnd
                    and tuple(pages[0].get("rect") or ()) == page_rect)
        service._sync_site_playback()
        paused_once = paused["n"] == 1
        service._sync_site_playback()
        paused_not_repeated = paused["n"] == 1
        print(f"[selftest] 网站限制：未受限时目标={idle_targets} 目标拆分={info} "
              f"暂停播放次数={paused['n']}")
        _check(idle_ok and split_ok and paused_once and paused_not_repeated,
               "网站达成限制：遮罩只盖页面内容区（不含标签栏）、静音整棵浏览器进程（渲染进程"
               "+根进程，Chromium 音频挂在 audio service 子进程上）、不拦浏览器输入（标签页仍可切换）",
               failures)

        # 6b) 暂停改为状态门控：进程树没出声时不许按键（老实现会把暂停着的视频放起来、
        #     甚至在浏览器没播时把这一下打到微信上）
        paused["n"] = 0
        service._pause_state.clear()
        service._pause_probe = lambda: set()
        service._sync_site_playback()
        silent_skip = paused["n"] == 0
        service._pause_probe = lambda: {9001, 9002}
        service._pause_state.clear()
        site_page["url"] = "https://www.bilibili.com/video/BV2"
        service._sync_site_playback()
        rearm_first = paused["n"] == 1
        site_page["url"] = "https://www.bilibili.com/video/BV3"
        service._sync_site_playback()
        rearm_second = paused["n"] == 2          # 换视频（新网址）要重新按一次
        # 同一段播放期间持续补按：把上次按键时刻挪到超过 PAUSE_RETRY_S 之前
        pause_entry = service._pause_state[site_id]
        service._pause_state[site_id] = (pause_entry[0], pause_entry[1], 0.0, pause_entry[3])
        service._sync_site_playback()
        retried = paused["n"] == 3                # 还在出声 → 下一拍再补一次
        service._pause_state[site_id] = (
            (own_hwnd, "https://www.bilibili.com/video/BV3"),
            service.PAUSE_FALLBACK_MAX, 0.0, True)
        service._sync_site_playback()
        capped = paused["n"] == 3                # 保险丝：到上限就不再按
        # 进程树安静下来（超过 QUIET_REARM_S）之后要重新武装：视频下次被放起来还能压回去
        service._pause_state[site_id] = (
            (own_hwnd, "https://www.bilibili.com/video/BV3"), 5,
            time.monotonic() - service.QUIET_REARM_S - 1.0, True)
        service._pause_probe = lambda: set()
        service._sync_site_playback()             # 这一拍发现确实没出声 → sent 归零
        service._pause_probe = lambda: {9001, 9002}
        service._sync_site_playback()             # 又出声（被放起来了）→ 重新按一次
        rearmed_after_quiet = paused["n"] == 4
        site_page["url"] = "https://www.bilibili.com/"
        gate_ok = (silent_skip and rearm_first and rearm_second and retried and capped
                   and rearmed_after_quiet)
        print(f"[selftest] 网站暂停门控：没出声不按={silent_skip} "
              f"换视频重按={rearm_first}/{rearm_second} 持续补按={retried} "
              f"到保险丝上限={capped} 安静后重新武装={rearmed_after_quiet} 总次数={paused['n']}")
        _check(gate_ok,
               "网站暂停改为状态门控：只在进程树确实出声时按播放/暂停键，换视频可重按，"
               "同一段播放期间持续补按（遮罩短暂失效时被放起来也能压回去），"
               "进程树安静下来后重新武装", failures)

        # 6d) 暂停主通道 = 系统媒体会话（SMTC）：只要它可用就**绝不动媒体键**，改成把
        #     "被遮罩的浏览器根进程"交给后台监听器（它按 AUMID 精确对上会话，再做幂等的
        #     TryPauseAsync）；集合没变时不重复 arm。SMTC 整条链用不了才退回媒体键。
        paused["n"] = 0
        service._pause_state.clear()
        service._smtc_pids = set()
        smtc = _FakeSmtc(True)
        service._smtc = smtc
        try:
            service._pause_probe = lambda: {9001, 9002}
            site_page["url"] = "https://www.bilibili.com/video/BV4"
            service._sync_site_playback()
            smtc_took_over = (paused["n"] == 0
                              and bool(smtc.armed) and smtc.armed[-1] == {9001}
                              and service._pause_state[site_id][3] is True)
            arm_count = len(smtc.armed)
            service._sync_site_playback()
            smtc_dedup = len(smtc.armed) == arm_count     # 集合没变不重复 arm
            service._smtc = _FakeSmtc(False)              # SMTC 用不了 → 必须退回媒体键
            service._smtc_pids = set()
            service._pause_state.clear()
            service._sync_site_playback()
            fallback_used = paused["n"] == 1
        finally:
            service._smtc = real_smtc
            service._smtc_pids = set()
        print(f"[selftest] SMTC 主通道：接管={smtc_took_over} 只 arm 一次={smtc_dedup} "
              f"不可用时退回媒体键={fallback_used} arm 记录={smtc.armed}")
        _check(smtc_took_over and smtc_dedup and fallback_used,
               "暂停主通道改为系统媒体会话（SMTC）：可用时按 AUMID 对上被遮罩的浏览器会话"
               "做幂等暂停、绝不碰媒体键；不可用时才退回媒体键（每段播放最多补按 "
               f"{service.PAUSE_FALLBACK_MAX} 次）", failures)

        # 6c) 进程树：静音 / 暂停判定都要带上子进程（Chromium 的音频在 audio service
        #     子进程上，画中画也挂在同一个根进程上）
        if getattr(sys, "frozen", False):
            # 打包后 sys.executable 就是 Monitor.exe，不能再拿它当"会睡觉的 python
            # 子进程"；进程树判定只关心父子关系，换成一个必定存在且不弹窗口的
            # Windows 子进程（cmd + ping 会安静地存活 ~29 秒）。
            cmd_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                   "System32", "cmd.exe")
            child = subprocess.Popen([cmd_exe, "/c", "ping", "-n", "30", "127.0.0.1"],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     creationflags=0x08000000)
        else:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        expanded: set = set()
        tree_ok = False
        try:
            # 进程表快照有 1 秒复用期（见 overlay._ppid_snapshot）；测试里直接清缓存，
            # 等价于"等过了 TTL"，免得刚起的子进程碰巧赶在缓存期内。
            service.overlays.invalidate_scan()
            expanded = service.overlays._expand({os.getpid()})
            tree_ok = (int(child.pid) in expanded
                       and service._tree_playing(os.getpid(), {int(child.pid)})
                       and not service._tree_playing(os.getpid(), set())
                       and not service._tree_playing(999999, {int(child.pid)}))
        finally:
            child.kill()
            child.wait(timeout=5)
        print(f"[selftest] 进程树展开：子进程={child.pid} 命中={child.pid in expanded} "
              f"树内发声判定={tree_ok}")
        _check(tree_ok,
               "静音与暂停按整棵进程树判定（子进程里的音频会话也算这个程序/网站）", failures)

        # 7) 遮罩：盖在当前显示该网站的那个窗口的**页面内容区**上（顶层窗口，
        #    不挂进 Chromium 合成窗口——挂上去根本看不见），且不提供"关闭程序"
        overlay.visible_windows = lambda pids: {}      # 只让网站目标产生遮罩
        service.overlays.invalidate_scan()
        service.overlays.set_suspended(False)
        service.overlays.sync()
        _pump(app, 0.3)
        mask = service.overlays._overlays.get(site_id, {}).get(own_hwnd)
        expect_rect = overlay.physical_to_logical(
            QRect(page_rect[0], page_rect[1], page_rect[2] - page_rect[0],
                  page_rect[3] - page_rect[1]))
        mask_ok = (bool(mask) and not mask.mounted
                   and int(mask.target_hwnd or 0) == own_hwnd
                   and mask.is_site is True
                   and mask.geometry() == expect_rect)
        no_close = bool(mask) and not mask.close_btn.isVisibleTo(mask)
        effects_ok = (9002 in service.overlays._effects_mute
                      and 9001 not in service.overlays._effects_block)
        hook_pids, _hook_hwnds = service.overlays._input_state()
        hook_ok = 9001 not in hook_pids
        if mask is not None:
            _shot(mask, os.path.join(shots, "20d_site_mask.png"))
        print(f"[selftest] 网站遮罩：顶层不挂载={bool(mask) and not mask.mounted} "
              f"几何={None if mask is None else mask.geometry()} 期望={expect_rect} "
              f"无关闭按钮={no_close} 静音集合={sorted(service.overlays._effects_mute)} "
              f"拦截集合={sorted(service.overlays._effects_block)} 钩子PID={sorted(hook_pids)}")
        _check(mask_ok and no_close and effects_ok and hook_ok,
               "网站遮罩只盖住该网站页面的内容区（不挂进浏览器、不遮标签栏），"
               "隐藏“关闭程序”，只静音不拦输入",
               failures)

        # 7b) 网站目标可以是多个窗口：主窗口（盖页面内容区 + 按窗口位移补偿）和画中画
        #     那种没有地址栏的小窗（盖整窗、用紧凑遮罩）
        follow_ok = (
            overlay._follow_window(QRect(100, 200, 800, 600), QRect(0, 0, 1000, 800),
                                   QRect(50, 30, 1010, 810)) == QRect(150, 230, 810, 610)
            and overlay._follow_window(QRect(100, 200, 800, 600), None, None)
            == QRect(100, 200, 800, 600))
        main_frame = overlay._window_rect(own_hwnd) or QRect(0, 0, 100, 100)
        pip_rect = (100, 120, 300, 260)
        # 造一个"扫描时"的旧窗口矩形：它必须比**当前**窗口小 10x5 且左上各偏 -10/-5，
        # 这样 _follow_window(页面矩形, 旧矩形, 当前矩形) 的位移量才正好是 (+10,+5,+10,+5)。
        then = QRect(main_frame.x() - 10, main_frame.y() - 5,
                     main_frame.width() - 10, main_frame.height() - 5)
        moved_frame = (then.x(), then.y(),
                       then.x() + then.width(), then.y() + then.height())
        site_page["aux"] = [{"hwnd": own_hwnd + 12345, "rect": pip_rect}]
        site_page["frame"] = moved_frame
        aux_info = service._site_targets().get(site_id) or {}
        aux_pages = aux_info.get("pages") or []
        aux_ok = (len(aux_pages) == 2
                  and aux_pages[0].get("compact") is False
                  and tuple(aux_pages[1].get("rect") or ()) == pip_rect
                  and aux_pages[1].get("compact") is True
                  and tuple(aux_pages[0].get("frame") or ()) == moved_frame)
        main_entry = next((item for item in service.overlays._site_windows(aux_info)
                           if int(item[0]) == own_hwnd), None)
        expect_moved = QRect(page_rect[0] + 10, page_rect[1] + 5,
                             page_rect[2] - page_rect[0] + 10,
                             page_rect[3] - page_rect[1] + 5)
        follow_applied = main_entry is not None and main_entry[1] == expect_moved
        # 合成两条同 hwnd 的页面项：只看 5 元组最后一位的紧凑标记有没有跟着走
        synth_entries = service.overlays._site_windows({
            "pages": [{"hwnd": own_hwnd, "rect": page_rect, "compact": False},
                      {"hwnd": own_hwnd, "rect": pip_rect, "compact": True}],
            "rect": page_rect})
        synth_ok = (len(synth_entries) == 2
                    and synth_entries[0][4] is False and synth_entries[1][4] is True
                    and synth_entries[1][1] == QRect(pip_rect[0], pip_rect[1],
                                                     pip_rect[2] - pip_rect[0],
                                                     pip_rect[3] - pip_rect[1]))
        print(f"[selftest] 网站多窗口：位移补偿={follow_ok}/{follow_applied} "
              f"目标={[(int(item.get('hwnd') or 0), item.get('compact')) for item in aux_pages]} "
              f"合成紧凑标记={[(int(item[0]), item[4]) for item in synth_entries]}")
        _check(follow_ok and aux_ok and follow_applied and synth_ok,
               "画中画按独立目标处理：主窗口遮罩按窗口位移实时贴合页面内容区，"
               "画中画小窗按整窗盖紧凑遮罩", failures)

        # 7b2) 画中画保活（用户 ②）：浏览器当前标签页切到**别的**网站后，画中画小窗还要
        #      继续被遮挡（画中画里放的仍是被限制网站的视频），但页面区不能被盖上——那是
        #      用户正常在看的一页。小窗关掉 / 最小化才释放。这里把窗口层的读函数全换成
        #      替身，驱动真实的 SiteWatcher 状态机，不依赖真的开一个画中画。
        pip_ok = pip_only_ok = pip_release_ok = False
        snapshot_fn = service.tracker.site.snapshot
        saved_page = {key: site_page.get(key)
                      for key in ("pip_only", "rect", "aux", "renderer_pid")}
        try:
            watcher = site_watch.SiteWatcher()
            watcher.configure([{"id": site_id, "path": "bilibili.com/*",
                                "kind": "site", "tracking": 1}])
            pip_hwnd = int(own_hwnd) + 777
            pip_rect = (200, 220, 420, 400)
            pip_state = {"alive": True,
                         "page": site_watch.PageInfo(int(own_hwnd),
                                                     "https://www.bilibili.com/video",
                                                     "哔哩哔哩", 9001, 9002, page_rect, 0x5A5B)}
            watcher._read = lambda hwnd: pip_state["page"]        # type: ignore[assignment]
            watcher._aux_windows = lambda *a, **k: (              # type: ignore[assignment]
                [{"hwnd": pip_hwnd, "rect": pip_rect}] if pip_state["alive"] else [], page_rect)
            real_funcs = (site_watch.foreground_window, site_watch.window_pid,
                          site_watch.is_window_visible, site_watch.is_iconic,
                          site_watch._window_rect, site_watch.window_text,
                          site_watch._child_renderer_pid, site_watch._child_renderer_window)
            site_watch.foreground_window = lambda: int(own_hwnd)
            site_watch.window_pid = lambda hwnd: 4242
            site_watch.is_window_visible = lambda hwnd: int(hwnd) != pip_hwnd or pip_state["alive"]
            site_watch.is_iconic = lambda hwnd: False
            site_watch._window_rect = lambda hwnd: (pip_rect if int(hwnd) == pip_hwnd
                                                    else page_rect)
            site_watch.window_text = lambda hwnd: "哔哩哔哩"
            site_watch._child_renderer_pid = lambda hwnd: 9100
            site_watch._child_renderer_window = lambda hwnd: (0x9999, 9100)
            try:
                first = watcher.scan()
                pip_ok = (int(site_id) in first
                          and not first[int(site_id)].get("pip_only")
                          and [int(item["hwnd"])
                               for item in first[int(site_id)].get("aux") or ()] == [pip_hwnd])
                pip_state["page"] = site_watch.PageInfo(int(own_hwnd), "https://example.com/",
                                                        "Example", 9001, 9002, page_rect, 0x5A5B)
                base = time.time()
                watcher.scan(now=base)                       # 这一拍开始网址不再匹配
                swapped = watcher.scan(now=base + 3.0)       # 超过确认期：页面条目释放
                entry = swapped.get(int(site_id)) or {}
                pip_only_ok = (bool(entry.get("pip_only")) and entry.get("rect") is None
                               and [int(item["hwnd"]) for item in entry.get("aux") or ()]
                               == [pip_hwnd])
                # 目标转换层：pip_only 只生成画中画那一块紧凑遮罩，且只静音画中画的渲染进程
                # （root 是整棵浏览器进程树，压它会把别的标签页一起静音）
                st = service.engine.state(int(site_id))
                service.engine._advance_day(st, time.time())
                st.used_seconds = 999
                service.tracker.site.snapshot = lambda: {site_id: dict(entry)}
                pip_target = service._site_targets().get(int(site_id)) or {}
                pip_pages = pip_target.get("pages") or []
                pip_only_ok = pip_only_ok and (len(pip_pages) == 1
                                               and pip_pages[0].get("compact") is True
                                               and int(pip_pages[0].get("hwnd") or 0) == pip_hwnd
                                               and tuple(pip_pages[0].get("rect") or ()) == pip_rect
                                               and pip_target.get("mute_pids") == {9100})
                pip_state["alive"] = False
                released = watcher.scan(now=base + 4.0)
                pip_release_ok = int(site_id) not in released
            finally:
                (site_watch.foreground_window, site_watch.window_pid,
                 site_watch.is_window_visible, site_watch.is_iconic, site_watch._window_rect,
                 site_watch.window_text, site_watch._child_renderer_pid,
                 site_watch._child_renderer_window) = real_funcs
        finally:
            service.tracker.site.snapshot = snapshot_fn
            for key, value in saved_page.items():
                if value is None:
                    site_page.pop(key, None)
                else:
                    site_page[key] = value
        print(f"[selftest] 画中画保活：命中时小窗进 aux={pip_ok} 切到非限制网站后只遮画中画="
              f"{pip_only_ok} 小窗关闭即释放={pip_release_ok}")
        _check(pip_ok and pip_only_ok and pip_release_ok,
               "画中画保活：浏览器当前标签页切到非限制网站后，画中画小窗继续被遮罩"
               "（页面区不遮），小窗关闭 / 最小化后释放", failures)

        # 7c) 紧凑遮罩 + 鼠标：短按 = 聚焦（不动窗口），按住 LONG_PRESS_MS 之后 = 自己
        #     实现的拖动，物理左键一抬就结束。上一版把"整次按住"异步转交给系统的原生
        #     移动循环（PostMessage WM_NCLBUTTONDOWN/HTCAPTION），目标窗口处理到它时按键
        #     往往已经松开 → 松手后窗口还跟着鼠标跑、要再点一下左键才停（用户报的 bug）。
        compact_ok = drag_ok = False
        if mask is not None:
            mask.set_compact(True)
            _pump(app, 0.25)
            _shot(mask, os.path.join(shots, "20e_site_mask_compact.png"))
            # 缩放成紧凑态只为了截图看效果；断言前必须**再设一次**：_pump 里事件循环
            # 转起来后 ControlService 的 tick 会跑 OverlayManager.sync()，而这块遮罩
            # 对应的主页面目标 compact=False，会把紧凑态刷回去。这时不再跑事件循环，
            # sync 就来不及覆盖；按钮高度也用 min/max（setFixedHeight 的效果），
            # 不依赖布局有没有跑过。
            mask.set_compact(True)
            compact_ok = (mask._compact is True
                          and str(mask.panel.property("compact")) == "true"
                          and mask.unlock_btn.minimumHeight() == config.sx(26)
                          and mask.unlock_btn.maximumHeight() == config.sx(26)
                          and mask.unlock_btn.text() == tr("overlay.compact.unlock")
                          and not mask.hint.isVisibleTo(mask))
            calls = {"focus": [], "native": [], "move": [], "follow": []}
            real_focus = overlay.focus_window
            real_native = overlay.start_window_drag
            real_frame = overlay.window_frame_rect
            real_cursor = overlay.cursor_pos
            real_down = overlay.left_button_down
            real_move = overlay.move_window
            real_zoom = overlay.is_maximized
            real_follow = mask.follow_now
            real_target = mask.target_hwnd
            real_is_site = mask.is_site
            try:
                overlay.focus_window = lambda hwnd: calls["focus"].append(int(hwnd)) or True
                overlay.start_window_drag = lambda hwnd: calls["native"].append(int(hwnd)) or True
                overlay.move_window = (lambda hwnd, left, top:
                                       calls["move"].append((int(hwnd), int(left), int(top)))
                                       or True)
                overlay.window_frame_rect = lambda hwnd: QRect(100, 200, 800, 600)
                overlay.cursor_pos = lambda: (300, 260)
                overlay.left_button_down = lambda: True
                overlay.is_maximized = lambda hwnd: False
                mask.follow_now = lambda: calls["follow"].append(1)
                mask.target_hwnd = own_hwnd
                press = QMouseEvent(QEvent.MouseButtonPress, QPointF(5, 5), QPointF(5, 5),
                                    Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
                # 网站遮罩：短按只聚焦（按被遮罩窗口拿到前台），**绝不**计时拖动——
                # 用户要求网站遮罩不参与拖动（浏览器自己的标题栏还露在外面，拖标题栏即可，
                # 而且在遮罩上按住拖会跟页面里的拖拽抢手势）。
                mask.is_site = True
                press.setAccepted(False)
                mask.mousePressEvent(press)
                site_ok = (calls["focus"] == [own_hwnd] and press.isAccepted()
                           and not mask._drag_arm.isActive() and calls["native"] == [])
                # 程序遮罩：短按同样只聚焦，但会开始 200ms 计时，到点进入自研拖动
                calls["focus"].clear()
                mask.is_site = False
                press2 = QMouseEvent(QEvent.MouseButtonPress, QPointF(5, 5), QPointF(5, 5),
                                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
                press2.setAccepted(False)
                mask.mousePressEvent(press2)
                short_ok = (site_ok and calls["focus"] == [own_hwnd] and press2.isAccepted()
                            and calls["native"] == [] and mask._drag_arm.isActive())
                mask._begin_drag()
                armed_ok = (mask._dragging and mask._drag_win == QRect(100, 200, 800, 600)
                            and mask._drag_timer.isActive() and calls["native"] == [])
                overlay.cursor_pos = lambda: (320, 250)          # 右下 +20 / 上 -10
                mask._drag_step()
                step_ok = (calls["move"] == [(own_hwnd, 120, 190)]
                           and bool(calls["follow"]))             # 遮罩同步跟上，不平移滞后
                overlay.left_button_down = lambda: False          # 松手
                mask._drag_step()
                release_ok = (not mask._dragging and not mask._drag_timer.isActive()
                              and not mask._drag_arm.isActive())
                right = QMouseEvent(QEvent.MouseButtonPress, QPointF(5, 5), QPointF(5, 5),
                                    Qt.RightButton, Qt.RightButton, Qt.NoModifier)
                right.setAccepted(True)
                mask.mousePressEvent(right)
                drag_ok = (short_ok and armed_ok and step_ok and release_ok
                           and not right.isAccepted() and calls["focus"] == [own_hwnd])
            finally:
                overlay.focus_window = real_focus
                overlay.start_window_drag = real_native
                overlay.window_frame_rect = real_frame
                overlay.cursor_pos = real_cursor
                overlay.left_button_down = real_down
                overlay.move_window = real_move
                overlay.is_maximized = real_zoom
                mask.follow_now = real_follow
                mask.target_hwnd = real_target
                mask.is_site = real_is_site
                mask._end_drag()
            mask.set_compact(False)
            _pump(app, 0.25)
        print(f"[selftest] 紧凑遮罩：{compact_ok} 短按聚焦：{site_ok if mask is not None else None} "
              f"程序遮罩长按拖动/松手即停：{drag_ok}")
        _check(compact_ok and drag_ok,
               "画中画紧凑遮罩：边距更小、按钮更矮更短、隐藏置顶提示；遮罩空白处短按把"
               "被遮罩窗口拿到前台（不改动窗口），按住 200ms 后才拖动窗口、松手立刻停"
               "（不再依赖系统原生移动循环）；**网站遮罩只保留短按聚焦，不参与拖动**"
               "（浏览器标题栏还露在外面，拖标题栏即可）", failures)

        # 7c-2) 快速跟随：窗口自己被挪动（用户拖标题栏、系统重排）时，30ms 那一拍只做
        #       一次 GetWindowRect 就把遮罩贴上去，不必等 120ms 的整轮同步——拖动时遮罩
        #       明显落在窗口后面，用户看到的就是"拖被遮罩的窗口时很卡"。
        follow_now_ok = False
        if mask is not None:
            real_frame_fn = overlay.window_frame_rect
            real_layer = mask._layer_visible
            real_target = mask.target_hwnd
            real_phys = mask._phys_rect
            real_win = mask._frame_rect
            real_geom = mask.geometry()
            try:
                mask.target_hwnd = own_hwnd
                mask._layer_visible = True
                mask._phys_rect = QRect(100, 200, 800, 600)
                mask._frame_rect = QRect(100, 200, 800, 600)
                mask.setGeometry(overlay.physical_to_logical(QRect(100, 200, 800, 600)))
                overlay.window_frame_rect = lambda hwnd: QRect(140, 225, 800, 600)
                mask.follow_now()
                moved = overlay.physical_to_logical(QRect(140, 225, 800, 600))
                moved_ok = (mask.geometry().topLeft() == moved.topLeft()
                            and mask._frame_rect == QRect(140, 225, 800, 600))
                mask.follow_now()            # 窗口没再动：什么都不用做
                steady_ok = mask.geometry().topLeft() == moved.topLeft()
                follow_now_ok = moved_ok and steady_ok
            finally:
                overlay.window_frame_rect = real_frame_fn
                mask._layer_visible = real_layer
                mask.target_hwnd = real_target
                mask._phys_rect = real_phys
                mask._frame_rect = real_win
                mask.setGeometry(real_geom)
        print(f"[selftest] 遮罩快速跟随：{follow_now_ok}")
        _check(follow_now_ok,
               "顶层遮罩快速跟随：目标窗口被挪动时，30ms 那一拍只做一次 GetWindowRect "
               "就立刻贴合（拖动时遮罩不再滞后一整拍）", failures)

        # 7c-3) z 序每一拍都检查并修复：目标窗口被重新激活 / 抬到遮罩上面时，下一次
        #       sync（120ms 内）就要贴回去，而不是等 REMOUNT_MS(1.5s)——那段时间遮罩被
        #       压在下面看不见、网站还能被操作（用户报告"每次重新聚焦浏览器遮罩都会消失、
        #       短时间后又出现"）。
        zorder_ok = False
        if mask is not None:
            real_above = overlay.is_directly_above
            real_place = overlay.place_above
            real_topmost = overlay.is_topmost
            seen = {"place": 0}
            try:
                mask.set_layer_visible(True)
                overlay.is_directly_above = lambda one, two: False
                overlay.place_above = (lambda one, two:
                                       seen.__setitem__("place", seen["place"] + 1) or True)
                overlay.is_topmost = lambda hwnd: False
                service.overlays.sync()
                first = seen["place"]
                service.overlays.sync()          # 紧接着：远早于 REMOUNT_MS(1.5s)
                second = seen["place"]
                refreshed = service.overlays._overlays.get(site_id, {}).get(own_hwnd)
                if refreshed is not None:
                    mask = refreshed
                zorder_ok = first == 1 and second == 2
            finally:
                overlay.is_directly_above = real_above
                overlay.place_above = real_place
                overlay.is_topmost = real_topmost
        print(f"[selftest] z 序每拍修复：{zorder_ok}")
        _check(zorder_ok,
               "遮罩 z 序每一拍都校验并修复：目标窗口被重新激活 / 抬到遮罩上面时，"
               "下一次同步（120ms 内）就贴回目标窗口正上方，不再有最长 1.5 秒的空窗"
               "（那段时间遮罩看不见、网站还能被操作）", failures)

        # 7c-4) 前台事件钩子：用户点回浏览器时 shell 会把浏览器抬到遮罩上面，等不了
        #       下一拍（120ms）——钩子收到前台变化就立刻补 z 序（用户 ②）。
        #       已经贴着（is_directly_above）时只多跑一次 GetWindow，不做任何多余动作。
        instant_ok = False
        hook_ok = False
        if mask is not None:
            real_above = overlay.is_directly_above
            real_place = overlay.place_above
            calls = {"place": 0}
            try:
                mask.set_layer_visible(True)
                overlay.is_directly_above = lambda one, two: False
                overlay.place_above = (lambda one, two:
                                       calls.__setitem__("place", calls["place"] + 1) or True)
                service.overlays.sync()                      # 先让 z 序处于"已贴好"状态
                calls["place"] = 0
                service.overlays._on_foreground_changed()    # 模拟"前台变化"事件
                first = calls["place"]
                overlay.is_directly_above = lambda one, two: True
                service.overlays._on_foreground_changed()    # 已经贴着：什么都不用做
                second = calls["place"]
                instant_ok = first >= 1 and second == first
                hook = service.overlays._fg_hook
                hook_ok = hook is not None and bool(hook.running() or hook.last_error())
            finally:
                overlay.is_directly_above = real_above
                overlay.place_above = real_place
        # 钩子自身的契约：起得来、停得掉（起不来也只是退回轮询，不影响功能）
        standalone = overlay.ForegroundHook()
        standalone.start()
        standalone_ok = standalone.running()
        standalone.stop()
        standalone_ok = standalone_ok and not standalone.running()
        print(f"[selftest] 前台事件钩子：可起可停={standalone_ok} 已装配={hook_ok} "
              f"即时补位={instant_ok}")
        _check(standalone_ok and hook_ok and instant_ok,
               "前台窗口一变就立刻把遮罩压回目标窗口上方（事件钩子驱动，不再等下一拍"
               "轮询；已经贴着时不重复设置）", failures)

        # 7d) 画中画识别：画中画没有地址栏，UIA 读不到网址，所以只能按 Win32 特征找
        fake_main, fake_pip, fake_owned, fake_big, fake_other = 0x11, 0x12, 0x13, 0x14, 0x15
        fake_rects = {fake_main: (0, 0, 1700, 900), fake_pip: (1280, 630, 1690, 890),
                      fake_owned: (100, 100, 400, 300), fake_big: (0, 0, 1700, 900),
                      fake_other: (500, 500, 800, 700)}
        fake_titles = {fake_main: "视频标题 - 个人 - Microsoft Edge", fake_pip: "视频标题",
                       fake_owned: "视频标题", fake_big: "视频标题", fake_other: "另一个视频"}
        aux_calls = {"n": 0}

        def _counting_top(pid) -> list:
            aux_calls["n"] += 1
            return [fake_main, fake_pip, fake_owned, fake_big, fake_other]

        aux_stubs = {
            "top_level_windows": _counting_top,
            "window_class": lambda hwnd: "Chrome_WidgetWin_1",
            "window_owner": lambda hwnd: 0x99 if int(hwnd) == fake_owned else 0,
            "is_window_visible": lambda hwnd: True,
            "is_iconic": lambda hwnd: False,
            "_child_renderer_window": lambda hwnd: (0xA0A0, 4242),
            "window_text": lambda hwnd: fake_titles.get(int(hwnd), ""),
            "_window_rect": lambda hwnd: fake_rects.get(int(hwnd)),
        }
        saved_aux = {name: getattr(site_watch, name) for name in aux_stubs}
        aux_found: list = []
        cached_ok = recount_ok = False
        try:
            for name, value in aux_stubs.items():
                setattr(site_watch, name, value)
            aux_watcher = site_watch.SiteWatcher()
            aux_found, _aux_rect = aux_watcher._aux_windows(
                fake_main, 9001, "视频标题", (0, 60, 1700, 900), 5000.0)
            first_count = aux_calls["n"]
            aux_watcher._aux_windows(fake_main, 9001, "视频标题", (0, 60, 1700, 900), 5000.5)
            cached_ok = aux_calls["n"] == first_count
            aux_watcher._aux_windows(fake_main, 9001, "视频标题", (0, 60, 1700, 900), 5002.0)
            recount_ok = aux_calls["n"] == first_count + 1
        finally:
            for name, value in saved_aux.items():
                setattr(site_watch, name, value)
        pip_ok = (len(aux_found) == 1
                  and int(aux_found[0].get("hwnd") or 0) == fake_pip
                  and tuple(aux_found[0].get("rect") or ()) == fake_rects[fake_pip])
        print(f"[selftest] 画中画识别：命中={[int(i.get('hwnd') or 0) for i in aux_found]} "
              f"（主窗口/有属主/同尺寸/别的视频都要被过滤）缓存={cached_ok}/{recount_ok}")
        _check(pip_ok and cached_ok and recount_ok,
               "画中画识别只看 Win32 特征（同根进程 + 浏览器窗口类 + 无属主 + 有渲染子窗口 + "
               "比主窗口小 + 标题对得上），不依赖地址栏；枚举结果按 1 秒缓存", failures)

        # 切走标签页 / 限制解除 -> 遮罩销毁、静音与拦截撤回
        service.tracker.site.snapshot = lambda: {}
        state.used_seconds = 0
        service.overlays.invalidate_scan()
        service.overlays.sync()
        _pump(app, 0.3)
        released = (site_id not in service.overlays._overlays
                    and 9001 not in service.overlays._effects_block
                    and 9002 not in service.overlays._effects_mute)
        print(f"[selftest] 网站遮罩撤回：{released}")
        _check(released, "离开该网站（或限制解除）后遮罩销毁、音量与输入拦截恢复", failures)

        # 8) 界面：首页 / 控制页分区 + 添加网站弹窗
        window.switch_page(0, animate=False)
        _pump(app, 0.4)
        home.refresh()
        _pump(app, 0.3)
        card = home.cards.get(site_id)
        globe = cards_module.program_icon(dict(site_row), config.sx(30))
        tile_ok = bool(card) and card.tile.pixmap().toImage() == globe.toImage()
        section_texts = [label.text() for label in home.section_labels]
        home_ok = (bool(card) and tile_ok
                   and cards_module.is_site(dict(site_row))
                   and tr("home.section.sites") in section_texts)
        _shot(window, os.path.join(shots, "20_home_sites.png"))
        print(f"[selftest] 首页网站分区：标题={section_texts} 地球图标={tile_ok}")
        _check(home_ok, "首页把网站单独分区显示，并用地球图标区别于程序", failures)

        # 8) 界面：控制页两类规则同屏时各自分区（先给一个真实程序也加上规则）
        program_row = next((dict(p) for p in db.programs(visible_only=True)
                            if not cards_module.is_site(p)), None)
        program_rule_id = int(program_row["id"]) if program_row else 0
        if program_rule_id:
            service.save_rule(Rule(program_id=program_rule_id, daily_enabled=True,
                                   daily_value=30, streak_enabled=False, streak_value=0,
                                   pause_value=5))
        window.switch_page(3, animate=False)
        _pump(app, 0.4)
        page._unlocked = True
        page.refresh()
        _pump(app, 0.4)
        rule_card = page._cards.get(site_id)
        control_sections = []
        for index in range(page.list_layout.count()):
            item = page.list_layout.itemAt(index)
            widget = item.widget() if item is not None else None
            if isinstance(widget, QLabel):
                control_sections.append(widget.text())
        _shot(window, os.path.join(shots, "20c_control_sites.png"))
        control_ok = (rule_card is not None and int(rule_card.program_id) == site_id
                      and tr("control.section.sites") in control_sections
                      and tr("control.section.programs") in control_sections)
        print(f"[selftest] 控制页网站分区：标题={control_sections} 规则卡={rule_card is not None}")
        _check(control_ok, "控制页把网站规则单独分区，网站与程序用同一套限制设置", failures)

        dialog = AddSitesDialog(home, {"bilibili.com"})
        dialog.editor.setPlainText("bilibili.com\nhttps://www.bilibili.com/*\n这是啥\n"
                                  "live.bilibili.com")
        dialog._validate()
        dialog.show()
        _pump(app, 0.3)
        _shot(dialog, os.path.join(shots, "20b_add_sites.png"))
        parsed = dialog.patterns()
        expected_parsed = ["bilibili.com", "bilibili.com/*", "live.bilibili.com"]
        confirm_enabled = dialog.confirm_btn.isEnabled()
        notice_text = dialog.notice.text()
        dialog._on_confirm()
        confirmed = list(dialog.result_patterns)
        dialog.close()
        dialog.deleteLater()
        _pump(app, 0.2)
        notice_ok = (tr("dialog.sites.dropped", count=1) in notice_text
                     and tr("dialog.sites.ready", count=2) in notice_text)
        dialog_ok = (parsed == expected_parsed and confirm_enabled and notice_ok
                     and confirmed == ["bilibili.com/*", "live.bilibili.com"])
        print(f"[selftest] 添加网站弹窗：解析={parsed} 确认可点={confirm_enabled} "
              f"提示={notice_text!r} 提交={confirmed}")
        _check(dialog_ok,
               "添加网站弹窗支持多行 / 完整网址（每行一个域名），并跳过已追踪与无效行", failures)

        # 9) 卡顿修复：资源管理器 / 桌面 / 任务栏这类窗口**不去读 UIA**
        #    （实测读一次 0.76~0.84 秒，前台一切过去就每秒白烧 0.8 秒 CPU）
        real_class = site_watch.window_class
        real_automation = site_watch._automation
        calls = {"n": 0}

        def _counting_automation():
            calls["n"] += 1
            return None

        try:
            site_watch._automation = _counting_automation
            site_watch.window_class = lambda hwnd: "CabinetWClass"
            explorer_page = site_watch.read_page(0x1234)
            site_watch.window_class = lambda hwnd: "Chrome_WidgetWin_1"
            browser_allowed = site_watch.can_host_page(0x1234)
        finally:
            site_watch.window_class = real_class
            site_watch._automation = real_automation
        gate_ok = (explorer_page is None and not calls["n"]
                   and browser_allowed and "CabinetWClass" in site_watch._NO_PAGE_CLASSES)
        print(f"[selftest] UIA 读取门控：资源管理器页面={explorer_page} "
              f"UIA 调用次数={calls['n']} 浏览器窗口允许={browser_allowed}")
        _check(gate_ok,
               "不读资源管理器 / 桌面 / 任务栏的 UIA（它们是加了网站追踪后卡顿的主因）", failures)
    finally:
        overlay.visible_windows = real_visible
        site_watch.pause_media = real_pause
        service._pause_probe = real_pause_probe
        service._pause_send = real_pause_send
        service._smtc = real_smtc
        service._smtc_pids = set()
        service._pause_state.clear()
        service.tracker.site.patterns = real_patterns
        service.tracker.site.snapshot = real_snapshot
        page._unlocked = was_unlocked
        if program_rule_id:
            service.delete_rule(program_rule_id)
        service.delete_rule(site_id)
        db.remove_programs([site_id], keep_logs=False)
        service.reload()
        window.tracker.set_programs(list(db.programs(visible_only=True)))
        home.refresh()
        page.refresh()
        window.switch_page(0, animate=False)
        service.overlays.invalidate_scan()
        service.overlays.sync()
        _pump(app, 0.3)


def _functional_checks(db: Database, monitor: MonitorApp, home,
                       failures: Optional[list] = None) -> list[str]:
    """针对需求文档做的功能校验（数据库与追踪行为）。"""
    if failures is None:
        failures = []
    programs = list(db.programs(visible_only=True))
    _check(len(programs) >= 3, "程序列表已持久化到 SQLite", failures)

    # 追踪开关
    target = programs[0]
    db.set_tracking(int(target["id"]), False)
    monitor.tracker.set_programs(db.programs(visible_only=True))
    _check(not bool(db.get_program(int(target["id"]))["tracking"]), "关闭追踪开关会写入数据库", failures)
    monitor.tracker.close_program(int(target["id"]))
    db.set_tracking(int(target["id"]), True)
    monitor.tracker.set_programs(db.programs(visible_only=True))

    # 改名
    db.rename_program(int(target["id"]), "改名测试")
    _check(str(db.get_program(int(target["id"]))["name"]) == "改名测试", "程序改名已持久化", failures)
    db.rename_program(int(target["id"]), str(target["name"]))

    # 删除但保留日志
    keep_id = int(programs[-1]["id"])
    logs_before = len(db.logs_for_program(keep_id))
    db.remove_programs([keep_id], keep_logs=True)
    still_there = db.get_program(keep_id) is not None
    visible = [int(p["id"]) for p in db.programs(visible_only=True)]
    in_dropdown = [int(p["id"]) for p in db.programs(include_deleted=True)]
    _check(still_there and keep_id not in visible and keep_id in in_dropdown
           and len(db.logs_for_program(keep_id)) == logs_before,
           "删除并保留日志：首页消失、日志页下拉菜单保留、日志未删除", failures)

    # 删除后能重新添加回来（回归：曾被判为"已在追踪列表中"而无法添加）
    readd_path = str(db.get_program(keep_id)["path"])
    readd = db.add_program(readd_path, "重新添加测试", None, tracking=True)
    visible_again = [int(p["id"]) for p in db.programs(visible_only=True)]
    print(f"[selftest] 删除后重新添加：返回 id={readd} 是否回到可见列表="
          f"{keep_id in visible_again} 日志仍为 {len(db.logs_for_program(keep_id))} 条")
    _check(readd is not None and keep_id in visible_again
           and str(db.get_program(keep_id)["name"]) == "重新添加测试"
           and len(db.logs_for_program(keep_id)) == logs_before,
           "被删除（保留日志）的程序可以重新添加回来，历史日志保留", failures)
    db.remove_programs([keep_id], keep_logs=True)

    # 删除且不保留日志
    db.remove_programs([keep_id], keep_logs=False)
    _check(db.get_program(keep_id) is None and len(db.logs_for_program(keep_id)) == 0,
           "删除且不保留日志：程序与日志被彻底清除", failures)

    # 排序
    home.sort_key = config.SORT_NAME
    home.sort_ascending = True
    names = [str(p["name"]).lower() for p in home._sorted(db.programs(visible_only=True))]
    _check(names == sorted(names), "按名称正序排序正确", failures)
    home.sort_ascending = False
    names_desc = [str(p["name"]).lower() for p in home._sorted(db.programs(visible_only=True))]
    _check(names_desc == sorted(names_desc, reverse=True), "按名称倒序排序正确", failures)
    home.sort_key = config.SORT_ADDED
    home.sort_ascending = True

    # 手动排序（m07636）：排序菜单新增"手动"项；只有该模式下手柄才出现并可拖；程序/网站各排各的
    from .ui.home_page import SortPopup as _SortPopup
    from .ui.popups import MenuItem as _MenuItem

    auto_popup = _SortPopup(config.SORT_ADDED, True, monitor.window)
    manual_popup = _SortPopup(config.SORT_MANUAL, True, monitor.window)
    qt_app = QApplication.instance()
    auto_rows = len(auto_popup.container.findChildren(_MenuItem))
    manual_rows = len(manual_popup.container.findChildren(_MenuItem))
    auto_popup.deleteLater()
    manual_popup.deleteLater()
    home.refresh()
    _pump(qt_app, 0.25)
    auto_handles = bool(home.cards) and all(
        not card.drag_handle.isVisible() for card in home.cards.values())

    home.on_sort_changed(config.SORT_MANUAL, True)      # 走真实入口：重建卡片 + 登记拖动
    _pump(qt_app, 0.3)
    manual_handles = bool(home.cards) and all(
        card.drag_handle.isVisible() for card in home.cards.values())
    prog_ids = [pid for pid in home.reorder.order("program") if isinstance(pid, int)]
    site_ids_before = [pid for pid in home.reorder.order("site") if isinstance(pid, int)]
    moved: list = []
    stored: list = []
    rebuilt: list = []
    home_drag_ok = False
    if len(prog_ids) >= 2:
        first, second = prog_ids[0], prog_ids[1]
        card_first, card_second = home.cards[first], home.cards[second]
        home.reorder._begin(card_second, card_second.mapToGlobal(QPoint(10, 10)))
        top_left = card_first.mapToGlobal(QPoint(0, 0))
        home.reorder._last = QPoint(top_left.x() + 5, top_left.y() + 1)
        home.reorder._reorder()
        moved = home.reorder.order("program")[:2]
        home.reorder._finish(commit=True)
        _pump(qt_app, 0.35)
        stored = list(db.get_order("order.home.program"))
        home.refresh()
        _pump(qt_app, 0.35)
        rebuilt = [pid for pid in home.reorder.order("program") if isinstance(pid, int)]
        home_drag_ok = (moved == [second, first] and stored[:2] == [second, first]
                        and rebuilt[:2] == [second, first]
                        and [pid for pid in home.reorder.order("site")
                             if isinstance(pid, int)] == site_ids_before)
    print(f"[selftest] 首页手动排序：菜单项={auto_rows}->{manual_rows} 自动模式手柄={auto_handles} "
          f"手动模式手柄={manual_handles} 本组顺序={prog_ids} 落库={stored} 重建后={rebuilt}")
    _check(auto_rows == 6 and manual_rows == 4 and auto_handles and manual_handles
           and home_drag_ok,
           "首页“手动排序”模式：卡片左侧才出现拖动手柄，可拖动换位并落库（程序/网站各一组）",
           failures)
    # 还原：顺序与排序方式都回到原样
    db.set_order("order.home.program", prog_ids)
    db.set_order("order.home.site", site_ids_before)
    home.sort_key = config.SORT_ADDED
    home.sort_ascending = True
    home.refresh()
    _pump(qt_app, 0.25)

    # 搜索
    home.search_text = "note"
    matched = [p for p in db.programs(visible_only=True)
               if "note" in str(p["name"]).lower() or "note" in str(p["path"]).lower()]
    _check(len(matched) >= 1, "搜索支持按名称/路径匹配", failures)
    home.search_text = ""

    # 数据库文件
    _check(os.path.exists(db.path), f"日志数据库文件存在: {db.path}", failures)
    _check(not os.path.exists(db.path + "-wal"), "数据库保持单文件（无 -wal 附文件）", failures)

    # 注册表（只读检查，不改动用户设置）
    _check(isinstance(autostart.is_enabled(), bool), "开机自启动注册表读取正常", failures)

    # 多实例：卡片显示运行时间最长的实例
    import time as _time

    now = _time.time()
    with monitor.tracker._lock:
        monitor.tracker._active = {999: {100: now - 120, 200: now - 30}}
    longest = monitor.tracker.longest_instance(999)
    _check(longest is not None and longest[0] == 100,
           "多实例合并为一张卡片并取运行时间最长的实例", failures)
    with monitor.tracker._lock:
        monitor.tracker._active.pop(999, None)

    # 托盘图标
    _check(monitor.tray.isVisible(), "托盘图标已创建并显示", failures)

    # 关闭行为：隐藏到托盘
    monitor.window.db.set_setting(config.S_CLOSE_ACTION, config.CLOSE_ACTION_TRAY)
    was_visible = monitor.window.isVisible()
    monitor.window.close()
    _check(was_visible and not monitor.window.isVisible(),
           "关闭按钮按设置隐藏到托盘（程序不退出）", failures)
    monitor.window.show()

    # 设置项
    monitor.window.db.set_setting(config.S_CLOSE_ACTION, config.CLOSE_ACTION_EXIT)
    _check(monitor.window.close_action() == config.CLOSE_ACTION_EXIT,
           "关闭行为下拉设置可持久化", failures)
    monitor.window.db.set_setting(config.S_CLOSE_ACTION, config.CLOSE_ACTION_TRAY)
    return failures
