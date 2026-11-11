"""Monitor 全局配置：路径、尺寸基准、设计令牌（颜色 / 字号 / 圆角 / 阴影）、缩放工具。

视觉风格的设计基准来自一份本地参考原型 ``example/project/monitor.py``（未包含在本仓库中）：
深色导航栏 + 浅灰蓝内容区 + 靛蓝强调色 + 白色卡片（细边框 + 柔和阴影），
整体偏现代 SaaS 观感。当前实际配色以 DARK_THEME / LIGHT_THEME 为准。
"""

from __future__ import annotations

import os
import sys

APP_NAME = "Monitor"
APP_VERSION = "0.1.0"

# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
# 程序根目录：源码运行时是项目目录（monitor 的上一级）；打包成 exe 之后是 exe
# 所在目录——数据库、图标缓存、托盘图都落在 exe 旁边。不能让它落进 PyInstaller
# 的临时解包目录：单文件模式那个目录每次启动都是新的，退出即删，数据会丢。
if getattr(sys, "frozen", False):
    PROJECT_ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)

DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "monitor.db")
ICON_DIR = os.path.join(DATA_DIR, "icons")
TRAY_DIR = os.path.join(DATA_DIR, "tray")
LOG_PATH = os.path.join(DATA_DIR, "monitor.log")

# --------------------------------------------------------------------------
# 窗口尺寸基准
# --------------------------------------------------------------------------
BASE_WINDOW_W = 1800
BASE_WINDOW_H = 1400
BASE_SCREEN_W = 3840
BASE_SCREEN_H = 2160

MIN_WINDOW_W = 680
MIN_WINDOW_H = 520
FLOOR_WINDOW_W = 460
FLOOR_WINDOW_H = 380

# --------------------------------------------------------------------------
# 会话 / 安全
# --------------------------------------------------------------------------
# 离开「控制」页超过该秒数后再回到该页，需要重新输入密码
SESSION_GRACE = 60

# 导航栏 / 主内容区宽度比例
NAV_RATIO = 2 / 9
CONTENT_RATIO = 7 / 9
# 主内容区顶栏高度比例
TOPBAR_RATIO = 3 / 35

# --------------------------------------------------------------------------
# 设计令牌 —— 颜色（两套主题：dark / light，运行时由 set_theme 切换）
# --------------------------------------------------------------------------
DARK_THEME = {
    "C_APP_BG": "#0B1120",          # 内容区画布（最深的一层）
    "C_SURFACE": "#161E2E",         # 卡片 / 面板
    "C_SURFACE_SOFT": "#1E2739",    # 图标底、次级填充
    "C_SURFACE_HOVER": "#1D2740",   # 悬停底色
    "C_APP_BORDER": "#26314A",      # 窗口外框描边
    "C_SIDEBAR": "#151D31",
    "C_SIDEBAR_HOVER": "#232E48",
    "C_SIDEBAR_ACTIVE": "#2C3856",
    "C_SIDEBAR_TEXT": "#93A0B7",
    "C_NAV_TEXT": "#93A0B7",        # 未选中导航项文字 / 图标
    "C_NAV_TEXT_ACTIVE": "#FFFFFF",  # 选中 / 悬停时的导航项文字 / 图标
    # 左侧栏目竖向渐变（顶部偏靛蓝，向下沉到近黑）
    "C_NAV_GRADIENT_TOP": "#1D2542",
    "C_NAV_GRADIENT_MID": "#131B2C",
    "C_NAV_GRADIENT_BOTTOM": "#0A0F1C",
    "C_TITLEBAR_GRADIENT_TOP": "#1D2542",
    "C_TITLEBAR_GRADIENT_BOTTOM": "#131B2C",
    "C_WINDOW_BTN_ICON": "#D0D5DD",
    "C_PRIMARY": "#4F46E5",
    "C_PRIMARY_HOVER": "#4338CA",
    "C_PRIMARY_SOFT": "#252C4E",
    "C_PRIMARY_SOFT_HOVER": "#2E3663",
    "C_PRIMARY_BORDER": "#4F46E5",
    "C_ACCENT_SOFT_TEXT": "#B7BDF7",
    "C_SELECTED_BORDER": "#6D7BE0",
    "C_DANGER": "#E5484D",
    "C_DANGER_HOVER": "#C9363B",
    "C_DANGER_SOFT": "#3A1D22",
    "C_SUCCESS": "#2ED47A",
    "C_WARNING": "#F79009",
    "C_TEXT": "#E9EDF5",
    "C_TEXT_BODY": "#C8D2E0",
    "C_TEXT_MUTED": "#8B97AC",
    "C_TEXT_FAINT": "#66738A",
    "C_BORDER": "#28324A",
    "C_BORDER_STRONG": "#39465A",
    "C_DIVIDER": "#232D3D",
    "C_SWITCH_OFF": "#3A4657",
    "C_SCROLL": "#2E3A4E",
    "C_SCROLL_HOVER": "#42526B",
    "C_TOPBAR": "#131B2C",
    "C_TOPBAR_BORDER": "#232D3D",
    "C_TOPBAR_TITLE": "#E9EDF5",
    "C_TOPBAR_HINT": "#8B97AC",
    "C_ON_CANVAS": "#E9EDF5",
    "C_ON_CANVAS_MUTED": "#8B97AC",
    "C_ON_CANVAS_FAINT": "#5A6779",
    "C_CHECK_EMPTY_FILL": "#1B2434",
    "C_EMPTY_TILE_BG": "#1A2233",
    "C_EMPTY_TILE_BORDER": "#2A3444",
    "C_EMPTY_TILE_ICON": "#7C8AA0",
    "C_BRAND_MARK_FROM": "#6D5CE7",
    "C_BRAND_MARK_TO": "#4F46E5",
    "C_TOOLTIP_BG": "#1E2739",
    "C_TOOLTIP_TEXT": "#E9EDF5",
    "C_MENU_SELECTED_TEXT": "#C7D2FE",
    "C_DISABLED_PRIMARY_BG": "#2A2F52",
}

# 浅色主题（参考 DSH 桌面端：左侧渐变、右侧纯色）
LIGHT_THEME = {
    "C_APP_BG": "#EDF1F7",
    "C_SURFACE": "#FFFFFF",
    "C_SURFACE_SOFT": "#F2F4F8",
    "C_SURFACE_HOVER": "#F6F8FC",
    "C_APP_BORDER": "#D7DEEA",
    "C_SIDEBAR": "#F5F7FB",
    "C_SIDEBAR_HOVER": "#E9EFF8",
    "C_SIDEBAR_ACTIVE": "#DCE6F7",
    "C_SIDEBAR_TEXT": "#5F6C82",
    "C_NAV_TEXT": "#5F6C82",
    "C_NAV_TEXT_ACTIVE": "#1B2A4A",
    "C_NAV_GRADIENT_TOP": "#FBFCFE",
    "C_NAV_GRADIENT_MID": "#F3F6FB",
    "C_NAV_GRADIENT_BOTTOM": "#E7EDF7",
    "C_TITLEBAR_GRADIENT_TOP": "#F8FAFD",
    "C_TITLEBAR_GRADIENT_BOTTOM": "#F8FAFD",
    "C_WINDOW_BTN_ICON": "#5F6C82",
    "C_PRIMARY": "#4F46E5",
    "C_PRIMARY_HOVER": "#4338CA",
    "C_PRIMARY_SOFT": "#EEF2FF",
    "C_PRIMARY_SOFT_HOVER": "#E0E7FF",
    "C_PRIMARY_BORDER": "#C7D2FE",
    "C_ACCENT_SOFT_TEXT": "#4F46E5",
    "C_SELECTED_BORDER": "#A5B4FC",
    "C_DANGER": "#E5484D",
    "C_DANGER_HOVER": "#C9363B",
    "C_DANGER_SOFT": "#FEECEC",
    "C_SUCCESS": "#12A150",
    "C_WARNING": "#F79009",
    "C_TEXT": "#16203A",
    "C_TEXT_BODY": "#41506B",
    "C_TEXT_MUTED": "#7A8699",
    "C_TEXT_FAINT": "#98A2B3",
    "C_BORDER": "#E3E8F0",
    "C_BORDER_STRONG": "#CBD4E1",
    "C_DIVIDER": "#EDF1F6",
    "C_SWITCH_OFF": "#CBD5E1",
    "C_SCROLL": "#C6CFDD",
    "C_SCROLL_HOVER": "#A9B4C6",
    "C_TOPBAR": "#F8FAFD",
    "C_TOPBAR_BORDER": "#E3E8F0",
    "C_TOPBAR_TITLE": "#16203A",
    "C_TOPBAR_HINT": "#7A8699",
    "C_ON_CANVAS": "#16203A",
    "C_ON_CANVAS_MUTED": "#7A8699",
    "C_ON_CANVAS_FAINT": "#98A2B3",
    "C_CHECK_EMPTY_FILL": "#FFFFFF",
    "C_EMPTY_TILE_BG": "#FFFFFF",
    "C_EMPTY_TILE_BORDER": "#E3E8F0",
    "C_EMPTY_TILE_ICON": "#A9B4C6",
    "C_BRAND_MARK_FROM": "#6D5CE7",
    "C_BRAND_MARK_TO": "#4F46E5",
    "C_TOOLTIP_BG": "#1E2739",
    "C_TOOLTIP_TEXT": "#E9EDF5",
    "C_MENU_SELECTED_TEXT": "#4F46E5",
    "C_DISABLED_PRIMARY_BG": "#C7D2FE",
}

# 搜索高亮（两套主题共用）
C_HIGHLIGHT = "#FFEB3B"

THEME_DARK = "dark"
THEME_LIGHT = "light"
THEME_SYSTEM = "system"

_current_theme = THEME_DARK


def resolve_theme(name: str) -> str:
    """把 system（跟随系统）解析为 dark / light。"""
    if name == THEME_LIGHT:
        return THEME_LIGHT
    if name == THEME_DARK:
        return THEME_DARK
    try:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtCore import Qt as _Qt

        hints = QGuiApplication.styleHints()
        scheme = hints.colorScheme() if hints is not None else None
        if scheme == _Qt.ColorScheme.Light:
            return THEME_LIGHT
        if scheme == _Qt.ColorScheme.Dark:
            return THEME_DARK
    except Exception:
        pass
    return THEME_DARK


def set_theme(name: str) -> str:
    """应用主题（写入本模块的颜色令牌），返回实际生效的主题名。"""
    resolved = resolve_theme(name)
    palette = DARK_THEME if resolved == THEME_DARK else LIGHT_THEME
    globals().update(palette)
    # 兼容旧命名
    globals()["C_BG"] = palette["C_APP_BG"]
    globals()["C_CARD"] = palette["C_SURFACE"]
    globals()["C_CARD_HOVER"] = palette["C_SURFACE_HOVER"]
    globals()["C_CARD_SELECTED"] = palette["C_PRIMARY_SOFT"]
    globals()["C_ACCENT"] = palette["C_PRIMARY"]
    globals()["C_ACCENT_DARK"] = palette["C_PRIMARY_HOVER"]
    globals()["C_ACCENT_SOFT"] = palette["C_PRIMARY_SOFT"]
    globals()["C_TEXT_SUB"] = palette["C_TEXT_MUTED"]
    global _current_theme
    _current_theme = resolved
    return resolved


def current_theme() -> str:
    return _current_theme


set_theme(THEME_DARK)

# --------------------------------------------------------------------------
# 设计令牌 —— 字体（固定像素，不随窗口缩放）
# --------------------------------------------------------------------------
FONT_FAMILY = "Microsoft YaHei UI"
FONT_FALLBACK = "Segoe UI"

FONT_BRAND = 20          # 导航栏品牌名
FONT_NAV = 14            # 导航项
FONT_PAGE_TITLE = 17     # 顶栏页面标题
FONT_CARD_NAME = 15      # 卡片程序名
FONT_SECTION = 14        # 小节标题
FONT_BODY = 13           # 正文 / 按钮
FONT_SMALL = 12          # 次要信息
FONT_TINY = 11           # 极小字

FONT_APP_TITLE = FONT_BRAND
FONT_BUTTON = FONT_BODY

# --------------------------------------------------------------------------
# 设计令牌 —— 圆角 / 阴影 / 动画
# --------------------------------------------------------------------------
R_CARD = 14
R_PANEL = 14
R_BUTTON = 9
R_INPUT = 9
R_SMALL = 8

SHADOW_IDLE_BLUR = 10
SHADOW_IDLE_ALPHA = 16
SHADOW_HOVER_BLUR = 30
SHADOW_HOVER_ALPHA = 42
SHADOW_COLOR = "#0F172A"

ANIM_FAST = 130
ANIM_NORMAL = 200
ANIM_SLOW = 260

# 列表删除动画的整段时长上限：单页日志最多 500 条，逐行错开 40ms 会拖到十几秒
ANIM_DELETE_MAX = 2500

# --------------------------------------------------------------------------
# 缩放工具
# --------------------------------------------------------------------------
_scale = 1.0


def set_ui_scale(value: float) -> None:
    """界面度量缩放系数（窗口宽 / 1800）。

    需求规定"字体、图标无需等比缩放（保持固定像素大小）"，因此只对间距 /
    图标等度量做温和缩放：小屏不至于挤成一团，大屏也不至于过分空旷。
    """
    global _scale
    _scale = max(0.86, min(1.45, float(value)))


def ui_scale() -> float:
    return _scale


def sx(value: float) -> int:
    """按缩放系数换算间距 / 图标尺寸等度量值（字体除外）。"""
    return max(1, int(round(value * _scale)))


def sxf(value: float) -> float:
    return float(value) * _scale


def initial_window_size(screen_w: int, screen_h: int) -> tuple[int, int]:
    """严格线性换算初始窗口尺寸。"""
    w = int(round(BASE_WINDOW_W * (float(screen_w) / BASE_SCREEN_W)))
    h = int(round(BASE_WINDOW_H * (float(screen_h) / BASE_SCREEN_H)))
    return max(FLOOR_WINDOW_W, w), max(FLOOR_WINDOW_H, h)


def ensure_dirs() -> None:
    for path in (DATA_DIR, ICON_DIR, TRAY_DIR):
        os.makedirs(path, exist_ok=True)


# --------------------------------------------------------------------------
# 设置项 key
# --------------------------------------------------------------------------
S_START_WITH_WINDOWS = "start_with_windows"
S_SILENT_START = "silent_start"
S_ADMIN_START = "admin_start"
S_CLOSE_ACTION = "close_action"
S_TRAY_ICON = "tray_icon"
S_LANGUAGE = "language"
S_THEME = "theme"
# 被静音程序"原本的音量"记忆（JSON：程序路径 -> [音量, 是否静音]）：
# Monitor 崩溃/被强杀时靠它在下次启动把音量放回去。
S_AUDIO_ORIGINALS = "audio_originals"
# 离开「控制」页后免验证的时间间隔（分钟，0 = 每次进入都要密码）
S_SESSION_GRACE = "control.session_grace"

LANGUAGE_ZH = "zh"
LANGUAGE_EN = "en"

CLOSE_ACTION_TRAY = "tray"
CLOSE_ACTION_EXIT = "exit"

# 排序方式
SORT_ADDED = "added_time"
SORT_NAME = "name"
SORT_LAST_RUN = "last_run_time"
SORT_MANUAL = "manual"        # 手动拖动排序（顺序存在 settings 表里）
