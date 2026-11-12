"""全局样式表、字体工厂与通用绘制工具（视觉系统的唯一出口）。

颜色一律通过 ``config`` 的主题令牌读取，因此浅色 / 深色主题可以在运行时切换。
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QLabel, QWidget

from .. import config


def font(size: int = config.FONT_BODY, bold: bool = False, family: str = "") -> QFont:
    f = QFont(family or config.FONT_FAMILY, -1)
    f.setPixelSize(size)
    f.setBold(bold)
    f.setStyleStrategy(QFont.PreferAntialias)
    return f


def set_font(widget: QWidget, size: int = config.FONT_BODY, bold: bool = False,
             color: str = "") -> QWidget:
    widget.setFont(font(size, bold))
    if color:
        widget.setStyleSheet(f"color: {color};")
    return widget


def label(text: str, size: int = config.FONT_BODY, bold: bool = False,
          color: str = "", parent: QWidget | None = None) -> QLabel:
    """颜色默认值在调用时解析（默认参数会在导入时固化，无法跟随主题切换）。"""
    lb = QLabel(text, parent)
    lb.setFont(font(size, bold))
    lb.setStyleSheet(f"color: {color or config.C_TEXT}; background: transparent;")
    return lb


def elide(text: str, widget_font: QFont, width: int) -> str:
    fm = QFontMetrics(widget_font)
    return fm.elidedText(text, Qt.ElideMiddle, max(20, width))


def shadow(widget: QWidget, blur: int = 18, dy: int = 4, alpha: int = 38,
           color: str = "") -> QGraphicsDropShadowEffect:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)
    c = QColor(color or config.SHADOW_COLOR)
    c.setAlpha(alpha)
    effect.setColor(c)
    widget.setGraphicsEffect(effect)
    return effect


def paint_soft_shadow(painter: QPainter, rect, radius: float, spread: int = 9,
                      dy: float = 4.0, alpha: int = 60, color: str = "") -> None:
    """用多层圆角矩形叠加出柔和阴影。

    不使用 QGraphicsDropShadowEffect：它在"内容会变化的控件"上存在缓存不失效的问题，
    表现为鼠标悬浮时文字闪烁/跳动。
    """
    if spread <= 0 or alpha <= 0:
        return
    base = QColor(color or config.SHADOW_COLOR)
    painter.save()
    painter.setPen(Qt.NoPen)
    steps = max(3, int(spread))
    for i in range(steps, 0, -1):
        t = i / steps
        layer_alpha = int(alpha * (1.0 - t) ** 2.0 / steps * 2.4)
        if layer_alpha <= 0:
            continue
        base.setAlpha(min(255, layer_alpha))
        painter.setBrush(base)
        painter.drawRoundedRect(rect.adjusted(-i, -i + dy, i, i + dy), radius + i, radius + i)
    painter.restore()


def repolish(widget: QWidget) -> None:
    """动态属性变化后重新应用样式表。"""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def QSize(width: int, height: int):
    """小工具：避免各处重复 import QSize。"""
    from PySide6.QtCore import QSize as _QSize

    return _QSize(width, height)


def highlight_html(text: str, query: str) -> tuple[str, bool]:
    """把匹配到的关键字包上黄色底纹，返回 (HTML, 是否命中)。"""
    if not query:
        return html.escape(text), False
    lowered = text.lower()
    needle = query.lower()
    parts: list[str] = []
    start = 0
    hit = False
    while True:
        index = lowered.find(needle, start)
        if index < 0:
            parts.append(html.escape(text[start:]))
            break
        hit = True
        parts.append(html.escape(text[start:index]))
        parts.append(
            f'<span style="background-color:{config.C_HIGHLIGHT}; color:#101828;">'
            f"{html.escape(text[index:index + len(needle)])}</span>"
        )
        start = index + len(needle)
    return "".join(parts), hit


def app_palette():
    """应用调色板：跟随当前主题，保证未被 QSS 覆盖的控件（菜单、提示）配色一致。"""
    from PySide6.QtGui import QPalette

    c = config
    p = QPalette()
    p.setColor(QPalette.Window, QColor(c.C_APP_BG))
    p.setColor(QPalette.WindowText, QColor(c.C_TEXT))
    p.setColor(QPalette.Base, QColor(c.C_SURFACE))
    p.setColor(QPalette.AlternateBase, QColor(c.C_SURFACE_SOFT))
    p.setColor(QPalette.Text, QColor(c.C_TEXT))
    p.setColor(QPalette.Button, QColor(c.C_SURFACE))
    p.setColor(QPalette.ButtonText, QColor(c.C_TEXT_BODY))
    p.setColor(QPalette.Highlight, QColor(c.C_PRIMARY))
    p.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    p.setColor(QPalette.ToolTipBase, QColor(c.C_TOOLTIP_BG))
    p.setColor(QPalette.ToolTipText, QColor(c.C_TOOLTIP_TEXT))
    p.setColor(QPalette.PlaceholderText, QColor(c.C_TEXT_FAINT))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(c.C_TEXT_FAINT))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(c.C_TEXT_FAINT))
    p.setColor(QPalette.Link, QColor(c.C_PRIMARY))
    return p


# 兼容旧调用
light_palette = app_palette


def build_stylesheet() -> str:
    """全局 QSS：左侧渐变栏目 + 内容区画布 + 卡片/面板 + 靛蓝强调。

    只负责颜色、背景、边框、圆角与内边距；字体一律由代码设置（两者不冲突）。
    """
    c = config
    return f"""
    * {{
        font-family: "{c.FONT_FAMILY}", "{c.FONT_FALLBACK}", sans-serif;
        color: {c.C_TEXT_BODY};
    }}

    QToolTip {{
        background: {c.C_TOOLTIP_BG}; color: {c.C_TOOLTIP_TEXT};
        border: 1px solid {c.C_BORDER};
        padding: 5px 9px; border-radius: 6px;
    }}

    /* 顶层窗口透明，只有圆角内的内容会被绘制 —— 避免四角残留浅色像素 */
    QMainWindow {{ background: transparent; }}
    #AppRoot {{ background: {c.C_APP_BG}; }}
    /* 画布颜色只由 #AppRoot / #ContentArea 绘制，中间层透明以免糊住圆角 */
    #BodyArea, #PageSurface, #PageHost {{ background: transparent; }}
    #ContentArea {{ background: {c.C_APP_BG}; }}

    /* ---------------- 无边框窗口的外框与自定义标题栏 ---------------- */
    QMainWindow[maximized="false"] #AppRoot {{
        border: 1px solid {c.C_APP_BORDER};
        border-radius: {c.sx(13)}px;
    }}
    #TitleBar {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 {c.C_TITLEBAR_GRADIENT_TOP}, stop:1 {c.C_TITLEBAR_GRADIENT_BOTTOM});
        border: none;
    }}
    QMainWindow[maximized="false"] #TitleBar {{
        border-top-right-radius: {c.sx(13)}px;
    }}
    /* 左侧功能栏目：竖向渐变（顶部偏靛蓝，向下沉） */
    #NavBar {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 {c.C_NAV_GRADIENT_TOP}, stop:0.55 {c.C_NAV_GRADIENT_MID},
            stop:1 {c.C_NAV_GRADIENT_BOTTOM});
        border: none;
    }}
    QMainWindow[maximized="false"] #NavBar {{
        border-top-left-radius: {c.sx(13)}px;
        border-bottom-left-radius: {c.sx(13)}px;
    }}
    QMainWindow[maximized="false"] #ContentArea {{
        /* 右上角也要跟着窗口圆角裁掉，否则内容区会在右上角画出方形底色（黑边） */
        border-top-right-radius: {c.sx(13)}px;
        border-bottom-right-radius: {c.sx(13)}px;
    }}

    QPushButton#WindowButton, QPushButton#WindowButtonClose {{
        background: transparent;
        border: none;
        border-radius: 0px;
    }}
    QPushButton#WindowButton:hover {{ background: {c.C_SIDEBAR_HOVER}; }}
    QPushButton#WindowButton:pressed {{ background: {c.C_SIDEBAR_ACTIVE}; }}
    QPushButton#WindowButtonClose:hover {{ background: {c.C_DANGER}; }}
    QPushButton#WindowButtonClose:pressed {{ background: {c.C_DANGER_HOVER}; }}
    /* 最右侧的关闭按钮要贴合窗口右上圆角，否则悬浮底色会在圆角外露出直角/黑边 */
    QMainWindow[maximized="false"] QPushButton#WindowButtonClose {{
        border-top-right-radius: {c.sx(13)}px;
    }}

    /* ---------------- 导航栏 ---------------- */
    #NavBar QLabel {{ background: transparent; color: {c.C_NAV_TEXT}; }}
    #BrandText {{ color: {c.C_NAV_TEXT_ACTIVE}; }}
    #BrandMark {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
            stop:0 {c.C_BRAND_MARK_FROM}, stop:1 {c.C_BRAND_MARK_TO});
        border-radius: 9px;
    }}
    #NavFooter {{ color: {c.C_SIDEBAR_TEXT}; }}

    QPushButton#NavButton {{
        color: {c.C_NAV_TEXT};
        text-align: left;
        padding: 0px {c.R_SMALL}px;
        border: none;
        border-radius: 10px;
        background: transparent;
    }}
    QPushButton#NavButton:hover {{ color: {c.C_NAV_TEXT_ACTIVE}; background: {c.C_SIDEBAR_HOVER}; }}
    QPushButton#NavButton:checked {{ color: {c.C_NAV_TEXT_ACTIVE}; background: {c.C_SIDEBAR_ACTIVE}; }}

    /* ---------------- 顶栏 ---------------- */
    #TopBar {{
        background: {c.C_TOPBAR};
        border: none;
        border-bottom: 1px solid {c.C_TOPBAR_BORDER};
    }}
    #PageTitle {{ color: {c.C_TOPBAR_TITLE}; }}
    #PageHint {{ color: {c.C_TOPBAR_HINT}; }}

    /* ---------------- 面板 ---------------- */
    /* #Card 的背景 / 边框 / 阴影全部由 controls.Card.paintEvent 自绘，
       这里不再给 #Card 任何 QSS 规则，避免二次绘制盖住阴影 */
    #Panel {{
        background: {c.C_SURFACE};
        border: 1px solid {c.C_BORDER};
        border-radius: {c.R_PANEL}px;
    }}
    #IconTile {{ background: {c.C_SURFACE_SOFT}; border-radius: 10px; }}
    #Divider {{ background: {c.C_DIVIDER}; border: none; }}

    /* ---------------- 日志行 ---------------- */
    /* 日志行每个控件都用 QSS 规则而不是 setStyleSheet：一页 200 行、每行 5 个内联
       样式表时，光解析+polish 就要几百毫秒（进日志页的卡顿主力之一）。 */
    #LogRowName {{ color: {c.C_TEXT}; background: transparent; }}
    #LogRowTime {{ color: {c.C_TEXT_MUTED}; background: transparent; }}
    #LogRowArrow {{ color: {c.C_TEXT_FAINT}; background: transparent; }}
    #LogRowChip {{
        background: {c.C_PRIMARY_SOFT};
        color: {c.C_ACCENT_SOFT_TEXT};
        border-radius: {c.sx(7)}px;
        padding: 1px {c.sx(7)}px;
    }}
    #LogRowDuration {{
        background: {c.C_SURFACE_SOFT};
        color: {c.C_TEXT_BODY};
        border-radius: {c.sx(7)}px;
        padding: 2px {c.sx(8)}px;
    }}

    /* ---------------- 文本角色 ---------------- */
    #CardTitle {{ color: {c.C_TEXT}; }}
    #Muted {{ color: {c.C_TEXT_MUTED}; }}
    #Faint {{ color: {c.C_TEXT_FAINT}; }}
    #DangerText {{ color: {c.C_DANGER}; }}
    #RunningText {{ color: {c.C_SUCCESS}; }}
    #EmptyTitle {{ color: {c.C_ON_CANVAS}; }}
    #EmptyHint {{ color: {c.C_ON_CANVAS_MUTED}; }}

    /* ---------------- 按钮 ---------------- */
    QPushButton {{
        background: {c.C_SURFACE};
        border: 1px solid transparent;
        border-radius: {c.R_BUTTON}px;
        padding: 0px 14px;
        color: {c.C_TEXT_BODY};
    }}
    QPushButton:hover {{
        background: {c.C_PRIMARY_SOFT};
        border-color: {c.C_PRIMARY_BORDER};
        color: {c.C_PRIMARY};
    }}
    QPushButton:pressed {{ background: {c.C_PRIMARY_SOFT_HOVER}; }}
    QPushButton:disabled {{
        color: {c.C_TEXT_FAINT}; background: {c.C_SURFACE_SOFT}; border-color: {c.C_BORDER};
    }}

    QPushButton[variant="primary"] {{
        background: {c.C_PRIMARY}; border-color: {c.C_PRIMARY}; color: #FFFFFF;
    }}
    QPushButton[variant="primary"]:hover {{
        background: {c.C_PRIMARY_HOVER}; border-color: {c.C_PRIMARY_HOVER}; color: #FFFFFF;
    }}
    QPushButton[variant="primary"]:disabled {{
        background: {c.C_DISABLED_PRIMARY_BG}; border-color: {c.C_DISABLED_PRIMARY_BG};
        color: {c.C_TEXT_FAINT};
    }}
    QPushButton[variant="danger"] {{
        background: {c.C_DANGER}; border-color: {c.C_DANGER}; color: #FFFFFF;
    }}
    QPushButton[variant="danger"]:hover {{
        background: {c.C_DANGER_HOVER}; border-color: {c.C_DANGER_HOVER}; color: #FFFFFF;
    }}
    QPushButton[variant="ghost"] {{
        background: {c.C_SURFACE}; border-color: {c.C_BORDER_STRONG};
    }}
    QPushButton[variant="ghost"]:hover {{
        background: {c.C_PRIMARY_SOFT}; border-color: {c.C_PRIMARY_BORDER}; color: {c.C_PRIMARY};
    }}
    QPushButton[variant="subtle"] {{
        background: transparent; border-color: transparent; color: {c.C_TEXT_MUTED};
    }}
    QPushButton[variant="subtle"]:hover {{
        background: {c.C_PRIMARY_SOFT}; color: {c.C_PRIMARY};
    }}
    QPushButton[variant="danger-ghost"] {{
        background: transparent; border-color: transparent; color: {c.C_DANGER};
    }}
    QPushButton[variant="danger-ghost"]:hover {{ background: {c.C_DANGER_SOFT}; }}

    QPushButton#IconButton {{
        background: {c.C_SURFACE}; border: 1px solid {c.C_BORDER_STRONG};
        border-radius: {c.R_BUTTON}px; padding: 0px;
    }}
    QPushButton#IconButton:hover {{
        background: {c.C_PRIMARY_SOFT}; border-color: {c.C_PRIMARY_BORDER};
    }}
    QPushButton#IconButtonFlat {{
        background: transparent; border: 1px solid transparent;
        border-radius: {c.R_BUTTON}px; padding: 0px;
    }}
    QPushButton#IconButtonFlat:hover {{ background: {c.C_SURFACE_SOFT}; }}

    /* ---------------- 输入 ---------------- */
    QLineEdit {{
        background: {c.C_SURFACE};
        border: 1px solid {c.C_BORDER_STRONG};
        border-radius: {c.R_INPUT}px;
        padding: 0px 10px;
        selection-background-color: {c.C_PRIMARY_BORDER};
        selection-color: {c.C_TEXT};
    }}
    QLineEdit:focus {{ border: 1px solid {c.C_PRIMARY}; }}
    /* 日志页选页框里的就地页码输入（宽 64px，所以 padding 要比通用输入框小） */
    #LogsPageEditor {{
        background: {c.C_SURFACE_SOFT};
        border: 1px solid {c.C_PRIMARY};
        border-radius: {c.sx(7)}px;
        padding: 0px {c.sx(4)}px;
    }}

    /* 控制页锁屏：整页内联密码框。外框由 #ControlGateField 负责，框内右侧可以直接
       写错误提示（红字），最右侧是内嵌的箭头按钮（自带分隔竖线 + 悬停淡色底）。 */
    #ControlGateField {{
        background: {c.C_SURFACE};
        border: 1px solid {c.C_BORDER_STRONG};
        border-radius: {c.sx(9)}px;
    }}
    #ControlGateField[focus="true"] {{ border: 1px solid {c.C_PRIMARY}; }}
    #ControlGateField[error="true"] {{ border: 1px solid {c.C_DANGER}; }}
    #ControlGateInput {{
        background: transparent;
        border: none;
        padding: 0px;
        color: {c.C_TEXT};
        selection-background-color: {c.C_PRIMARY_BORDER};
        selection-color: {c.C_TEXT};
    }}
    #ControlGateInput:focus {{ border: none; }}
    #ControlGateError {{ color: {c.C_DANGER}; background: transparent; }}

    QSpinBox {{
        background: {c.C_SURFACE};
        border: 1px solid {c.C_BORDER_STRONG};
        border-radius: {c.R_INPUT}px;
        padding: 0px 6px;
        color: {c.C_TEXT};
    }}
    QSpinBox:focus {{ border: 1px solid {c.C_PRIMARY}; }}
    QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; border: none; background: transparent; }}

    QCheckBox {{ color: {c.C_TEXT_BODY}; spacing: 8px; }}
    QCheckBox::indicator {{
        width: 15px; height: 15px;
        border: 1px solid {c.C_BORDER_STRONG};
        border-radius: 4px;
        background: {c.C_SURFACE};
    }}
    QCheckBox::indicator:checked {{ background: {c.C_PRIMARY}; border-color: {c.C_PRIMARY}; }}
    QCheckBox::indicator:disabled {{ background: {c.C_SURFACE_SOFT}; border-color: {c.C_BORDER}; }}

    /* ---------------- 控制遮罩 ---------------- */
    #OverlayPanel {{
        background: rgba(9, 13, 26, 236);
        border: 1px solid {c.C_PRIMARY};
        border-radius: {c.sx(18)}px;
    }}
    #OverlayTitle {{ color: #FFFFFF; }}
    #OverlayCountdown {{ color: #A5B4FC; }}
    #OverlayNotice {{ color: {c.C_DANGER}; }}
    #OverlayHint {{ color: rgba(255, 255, 255, 150); font-size: {c.sx(11)}px; }}
    #OverlayUnlock {{
        background: {c.C_PRIMARY}; color: #FFFFFF;
        border: none; border-radius: {c.sx(10)}px; padding: 0px {c.sx(18)}px;
    }}
    #OverlayUnlock:hover {{ background: {c.C_PRIMARY_HOVER}; }}
    #OverlayClose {{
        background: {c.C_DANGER}; color: #FFFFFF;
        border: none; border-radius: {c.sx(10)}px; padding: 0px {c.sx(18)}px;
    }}
    #OverlayClose:hover {{ background: {c.C_DANGER_HOVER}; }}

    /* 紧凑遮罩：盖在画中画那种小窗上（窗口本身才两三百像素），
       圆角、字号、内边距都要跟着缩小，否则文字和按钮会挤成一团 */
    #OverlayPanel[compact="true"] {{ border-radius: {c.sx(9)}px; }}
    #OverlayPanel[compact="true"] #OverlayTitle {{ font-size: {c.sx(13)}px; }}
    #OverlayPanel[compact="true"] #OverlayCountdown {{ font-size: {c.sx(12)}px; }}
    #OverlayPanel[compact="true"] #OverlayUnlock,
    #OverlayPanel[compact="true"] #OverlayClose {{
        border-radius: {c.sx(6)}px; font-size: {c.sx(11)}px; padding: 0px {c.sx(10)}px;
    }}

    /* ---------------- 带边框的"+"按钮 / 时长下拉 ---------------- */
    QPushButton#PlusButton {{
        background: {c.C_SURFACE};
        border: 1px solid {c.C_BORDER_STRONG};
        border-radius: {c.sx(9)}px;
        color: {c.C_TEXT_BODY};
        font-size: {c.sx(15)}px;
    }}
    QPushButton#PlusButton:hover {{ border-color: {c.C_PRIMARY}; color: {c.C_PRIMARY}; }}
    QPushButton#PlusButton:pressed {{ background: {c.C_SURFACE_SOFT}; }}

    QPushButton#DurationToggle {{
        background: transparent;
        border: none;
        border-radius: {c.sx(8)}px;
    }}
    QPushButton#DurationToggle:hover {{ background: {c.C_SIDEBAR_HOVER}; }}
    QPushButton#DurationToggle:checked {{ background: {c.C_SIDEBAR_ACTIVE}; }}

    #DurationRow {{
        background: {c.C_SURFACE_SOFT};
        border: 1px solid {c.C_BORDER};
        border-radius: {c.sx(10)}px;
    }}
    QLabel#DurationValue {{ color: {c.C_TEXT}; }}
    QPushButton#DurationRemove {{
        background: transparent; border: none; border-radius: {c.sx(7)}px;
        color: {c.C_TEXT_MUTED}; font-size: {c.sx(15)}px;
    }}
    QPushButton#DurationRemove:hover {{ background: {c.C_DANGER_SOFT}; color: {c.C_DANGER}; }}
    QLabel#DurationEmpty {{ color: {c.C_TEXT_MUTED}; }}

    #SelectBox {{
        background: {c.C_SURFACE};
        border: 1px solid {c.C_BORDER_STRONG};
        border-radius: {c.R_INPUT}px;
    }}
    #SelectBox:hover {{
        background: {c.C_PRIMARY_SOFT};
        border-color: {c.C_PRIMARY_BORDER};
    }}

    /* ---------------- 滚动条 ---------------- */
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 9px; margin: 3px 2px; }}
    QScrollBar::handle:vertical {{
        background: {c.C_SCROLL}; border-radius: 4px; min-height: 32px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {c.C_SCROLL_HOVER}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

    QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 2px 3px; }}
    QScrollBar::handle:horizontal {{
        background: {c.C_SCROLL}; border-radius: 4px; min-width: 32px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: {c.C_SCROLL_HOVER}; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

    /* ---------------- 弹窗容器 ---------------- */
    #DialogContainer {{
        background: {c.C_SURFACE};
        border: 1px solid {c.C_BORDER};
        border-radius: 16px;
    }}
    #PopupContainer {{
        background: {c.C_SURFACE};
        border: 1px solid {c.C_BORDER};
        border-radius: 12px;
    }}
    #HoverRow {{ background: transparent; border-radius: 8px; }}
    #HoverRow:hover {{ background: {c.C_SURFACE_HOVER}; }}
    #HoverRow QLabel {{ background: transparent; }}

    #LogRow {{ background: transparent; border-radius: {c.R_SMALL}px; }}
    #LogRow[selected="true"] {{ background: {c.C_PRIMARY_SOFT}; }}

    /* ---------------- 菜单 / 其它默认控件 ---------------- */
    QMenu {{
        background: {c.C_SURFACE};
        border: 1px solid {c.C_BORDER};
        border-radius: 10px;
        padding: 6px;
    }}
    QMenu::item {{
        padding: 7px 18px 7px 14px; border-radius: 7px; color: {c.C_TEXT_BODY};
    }}
    QMenu::item:selected {{ background: {c.C_PRIMARY_SOFT}; color: {c.C_MENU_SELECTED_TEXT}; }}
    QMenu::separator {{ height: 1px; background: {c.C_DIVIDER}; margin: 5px 8px; }}

    QCheckBox {{ color: {c.C_TEXT_BODY}; }}
    QComboBox {{
        background: {c.C_SURFACE}; border: 1px solid {c.C_BORDER_STRONG};
        border-radius: {c.R_INPUT}px; padding: 5px 10px; color: {c.C_TEXT};
    }}
    QComboBox QAbstractItemView {{
        background: {c.C_SURFACE}; border: 1px solid {c.C_BORDER};
        selection-background-color: {c.C_PRIMARY_SOFT};
        selection-color: {c.C_MENU_SELECTED_TEXT};
        outline: 0;
    }}
    QDialog {{ background: {c.C_SURFACE}; }}
    """ + (_glass_stylesheet() if c.current_theme() == c.THEME_GLASS else "") + _effect_stylesheet()


def _effect_stylesheet():
    from . import glass_effects
    return glass_effects.stylesheet()


def _glass_stylesheet() -> str:
    # Independent native windows need an opaque backing for readable menus/dialogs.
    return f"""
    #AppRoot, #BodyArea, #ContentArea, #PageSurface, #PageHost {{ background: transparent; }}
    #AppRoot {{ border: none; }}
    #NavBar {{ background: #33101A30; border-right: 1px solid #287F97BA; }}
    #TitleBar {{ background: #1818233D; }}
    #TopBar {{ background: #2418233D; }}
    QMainWindow[maximized="false"] #AppRoot {{ border: none; }}
    QMenu, QDialog, QComboBox QAbstractItemView, #PopupContainer, #DialogContainer {{ background: #202C43; }}
    QSlider::groove:horizontal {{ background: #547389B0; height: 4px; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: #A7BAFF; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: #EDF3FF; width: 13px; margin: -5px 0; border-radius: 6px; }}
    """
