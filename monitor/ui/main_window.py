"""主窗口：深色导航栏（2/9）+ 主内容区（7/9，含 3/35 高的顶栏）。"""

from __future__ import annotations

import sys
from typing import Optional

from PySide6.QtCore import QEasingCurve, QEvent, QPointF, QPropertyAnimation, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QCursor, QIcon, QPainterPath, QRegion
from PySide6.QtWidgets import (QAbstractSlider, QFrame, QGraphicsOpacityEffect,
                               QHBoxLayout, QLabel, QMainWindow, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

from .. import config, icon_extract
from ..i18n import tr
from . import anim, theme, vector_icons
from .controls import BrandMark
from .control_page import ControlPage
from .home_page import HomePage
from .logs_page import LogsPage
from .notify_page import NotifyPage
from .settings_page import SettingsPage
from .title_bar import TitleBar

# 页面顺序固定：首页 / 日志 / 通知 / 控制 / 设置（文案随语言切换）
PAGE_KEYS = [
    ("nav.home", "home"),
    ("nav.logs", "list"),
    ("nav.notify", "bell"),
    ("nav.control", "shield"),
    ("nav.settings", "gear"),
]

TOP_PAGES = [0, 1, 2, 3]
BOTTOM_PAGE = 4


def page_titles() -> list[str]:
    return [tr(key) for key, _ in PAGE_KEYS]


def page_title(index: int) -> str:
    return tr(PAGE_KEYS[index][0])


def page_hint(index: int) -> str:
    return tr(("hint.home", "hint.logs", "hint.notify", "hint.control",
               "hint.settings")[index])


class NavButton(QPushButton):
    """导航栏中的一个选项卡：图标 + 文字，左对齐。"""

    def __init__(self, text: str, icon_name: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("NavButton")
        self.setCheckable(True)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setFont(theme.font(config.FONT_NAV, bold=True))
        self.setFixedHeight(config.sx(44))
        self.setIconSize(QSize(config.sx(17), config.sx(17)))
        self._icon_name = icon_name
        self.set_active(False)

    def set_active(self, active: bool) -> None:
        self.setChecked(active)
        color = config.C_NAV_TEXT_ACTIVE if active else config.C_NAV_TEXT
        self.setIcon(vector_icons.icon(self._icon_name, config.sx(17), color))

    def enterEvent(self, event) -> None:  # noqa: N802
        if not self.isChecked():
            self.setIcon(vector_icons.icon(self._icon_name, config.sx(17), config.C_NAV_TEXT_ACTIVE))
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if not self.isChecked():
            self.setIcon(vector_icons.icon(self._icon_name, config.sx(17), config.C_NAV_TEXT))
        super().leaveEvent(event)


class NavBar(QWidget):
    """左侧深色导航栏（占满整列高度）：品牌 + 首页/日志/通知，分隔线下为置底的设置。"""

    page_requested = Signal(int)
    maximize_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("NavBar")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(config.sx(12), config.sx(10), config.sx(12), config.sx(14))
        layout.setSpacing(0)

        # 品牌区（项目名称）：与标题栏同色，可拖动窗口
        brand = QWidget(self)
        brand_row = QHBoxLayout(brand)
        brand_row.setContentsMargins(config.sx(4), 0, 0, 0)
        brand_row.setSpacing(config.sx(9))
        mark = BrandMark(26, parent=self)
        brand_row.addWidget(mark, 0, Qt.AlignVCenter)
        title = QLabel("Monitor")
        title.setObjectName("BrandText")
        title.setFont(theme.font(config.FONT_BRAND, bold=True))
        brand_row.addWidget(title, 1, Qt.AlignVCenter)
        layout.addWidget(brand)
        layout.addSpacing(config.sx(18))

        self.buttons: list[NavButton] = []

        def add_button(index: int) -> NavButton:
            text, icon_name = tr(PAGE_KEYS[index][0]), PAGE_KEYS[index][1]
            button = NavButton(text, icon_name, self)
            button.clicked.connect(lambda _=False, i=index: self.page_requested.emit(i))
            layout.addWidget(button)
            self.buttons.append(button)
            return button

        layout.addSpacing(config.sx(4))
        add_button(TOP_PAGES[0])
        layout.addSpacing(config.sx(14))
        add_button(TOP_PAGES[1])
        layout.addSpacing(config.sx(14))
        add_button(TOP_PAGES[2])
        layout.addSpacing(config.sx(14))
        add_button(TOP_PAGES[3])
        layout.addStretch(1)

        # 底部分组：分隔线 + 设置 + 追踪状态
        divider = QFrame(self)
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {config.C_SIDEBAR_ACTIVE}; border: none;")
        divider_holder = QWidget(self)
        divider_layout = QHBoxLayout(divider_holder)
        divider_layout.setContentsMargins(config.sx(2), 0, config.sx(2), 0)
        divider_layout.addWidget(divider)
        layout.addWidget(divider_holder)
        layout.addSpacing(config.sx(12))

        add_button(BOTTOM_PAGE)
        layout.addSpacing(config.sx(14))

        self.footer = QLabel(tr("nav.footer", tracked=0, total=0))
        self.footer.setObjectName("NavFooter")
        self.footer.setFont(theme.font(config.FONT_TINY))
        self.footer.setWordWrap(True)
        self.footer.setStyleSheet(f"color: {config.C_SIDEBAR_TEXT}; background: transparent;")
        layout.addWidget(self.footer)
        self._footer_text: Optional[str] = None

    # ------------------------------------------------------------------
    # 空白处可拖动窗口 / 双击最大化（与标题栏一致）
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.maximize_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def set_current(self, index: int) -> None:
        for button, page_index in zip(self.buttons, TOP_PAGES + [BOTTOM_PAGE]):
            button.set_active(page_index == index)

    def set_footer(self, text: str) -> None:
        # 1 Hz 的 tick 会反复喂同样的文本，QLabel 每次都走一遍布局/重绘，
        # 内容没变就直接返回（重建界面时是新标签，_footer_text 初值 None 不会误判）。
        if text == self._footer_text:
            return
        self._footer_text = text
        self.footer.setText(text)


class TopBar(QWidget):
    """主内容区顶栏：左侧页面标题，右侧页面自定义控件。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("TopBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(config.sx(22), 0, config.sx(18), 0)
        layout.setSpacing(config.sx(10))

        texts = QVBoxLayout()
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(0)
        self.title = QLabel(page_title(0))
        self.title.setObjectName("PageTitle")
        self.title.setFont(theme.font(config.FONT_PAGE_TITLE, bold=True))
        texts.addWidget(self.title)
        self.hint = QLabel(page_hint(0))
        self.hint.setObjectName("PageHint")
        self.hint.setFont(theme.font(config.FONT_TINY))
        texts.addWidget(self.hint)
        texts.addSpacing(config.sx(2))
        layout.addLayout(texts)
        layout.addStretch(1)

        self.right_slot = QWidget(self)
        self.right_layout = QHBoxLayout(self.right_slot)
        self.right_layout.setContentsMargins(0, 0, 0, 0)
        self.right_layout.setSpacing(config.sx(6))
        layout.addWidget(self.right_slot, 0, Qt.AlignVCenter)

    def set_title(self, text: str, hint: str = "") -> None:
        self.title.setText(text)
        self.hint.setText(hint)

    def set_right_widget(self, widget: Optional[QWidget]) -> None:
        while self.right_layout.count():
            item = self.right_layout.takeAt(0)
            child = item.widget() if item else None
            if child is not None:
                child.hide()          # 先隐藏：可见控件脱离父级会变成顶层窗口
                # 保留在顶栏父对象下，界面重建时随父对象销毁。
        if widget is not None:
            widget.setParent(self.right_slot)
            self.right_layout.addWidget(widget)
            widget.show()


def point_hits_scrollbar(global_pos, root: QWidget) -> bool:
    """光标下是滚动条时，窗口边缘的缩放判定要让路。

    自绘无边框窗口把边缘 6px 划给系统做缩放，而细滚动条（QSS 里 9px 宽）正好贴在
    内容区右缘：实测窗口右缘 1~6px 全判成 'right'，9px 里 6px 被吃掉、只剩 3px 能点。
    这里用 ``childAt`` 沿命中链上溯做一次纯几何命中测试（``QApplication.widgetAt``
    依赖系统层命中，窗口未激活时会返回 None），把落在滚动条上的点击还给 Qt。
    """
    try:
        widget = root.childAt(root.mapFromGlobal(global_pos))
    except Exception:
        return False
    while widget is not None and widget is not root:
        if isinstance(widget, QAbstractSlider) and widget.isVisible():
            return True
        widget = widget.parentWidget()
    return False


class MainWindow(QMainWindow):
    """Monitor 主窗口。"""

    quit_requested = Signal()
    theme_change_requested = Signal(str)
    language_change_requested = Signal(str)
    tray_icon_change_requested = Signal(str)

    def __init__(self, db, tracker, parent: Optional[QWidget] = None,
                 control=None) -> None:
        super().__init__(parent)
        self.db = db
        self.tracker = tracker
        self.control = control
        self._current_index = 0
        self._switching = False
        self._force_quit = False
        self._pending_refresh = True

        self.setWindowTitle("Monitor")
        self.setWindowIcon(QIcon(icon_extract.default_app_pixmap(64)))
        self.setMinimumSize(config.MIN_WINDOW_W, config.MIN_WINDOW_H)
        # 去掉系统标题栏，改用自绘标题栏（保留最小化 / 最大化 / 关闭与边缘拖拽缩放）
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        # 顶层窗口透明：四角圆角之外不绘制任何像素，避免出现浅色残留
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setProperty("maximized", False)
        self._build_ui()
        # 追踪器信号只在这里连一次：_build_ui() 会被 rebuild_ui() 复用（切主题/语言），
        # 连接写在里面每重建一次就多一条，1 Hz 的 tick 会成倍触发页脚刷新。
        self.tracker.targets_changed.connect(self._update_nav_footer)
        self.switch_page(0, animate=False)

    # ------------------------------------------------------------------
    # 界面构建 / 主题与语言切换时的原地重建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        central.setObjectName("AppRoot")
        central.setAttribute(Qt.WA_StyledBackground, True)
        self._central = central
        self.setCentralWidget(central)

        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QWidget(central)
        body.setObjectName("BodyArea")
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        outer.addWidget(body, 1)

        # 左：整列高度的功能栏目（含项目名称）
        self.nav = NavBar(body)
        self.nav.page_requested.connect(self.switch_page)
        self.nav.maximize_requested.connect(self.toggle_maximized)
        layout.addWidget(self.nav, 0)

        # 右上：仅含窗口按钮的标题栏；右下：页面标题栏 + 功能页面
        content = QWidget(body)
        content.setObjectName("ContentArea")
        content.setAttribute(Qt.WA_StyledBackground, True)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.title_bar = TitleBar(content)
        self.title_bar.minimize_requested.connect(self.showMinimized)
        self.title_bar.maximize_requested.connect(self.toggle_maximized)
        self.title_bar.close_requested.connect(self.close)
        content_layout.addWidget(self.title_bar, 0)

        self.topbar = TopBar(content)
        content_layout.addWidget(self.topbar, 0)

        self.stack = QStackedWidget(content)
        self.stack.setObjectName("PageHost")
        self.home_page = HomePage(self.db, self.tracker, self.stack)
        self.logs_page = LogsPage(self.db, self.tracker, self.stack)
        self.notify_page = NotifyPage(self.db, self.stack)
        self.control_page = ControlPage(self.db, self.control, self.stack)
        self.settings_page = SettingsPage(self.db, self.stack, control=self.control)
        for page in (self.home_page, self.logs_page, self.notify_page, self.control_page,
                     self.settings_page):
            self.stack.addWidget(page)
        content_layout.addWidget(self.stack, 1)
        layout.addWidget(content, 1)

        self._content = content
        # 页面级信号统一上升为窗口信号，重建后无需重新连线
        self.settings_page.tray_icon_changed.connect(self.tray_icon_change_requested.emit)
        self.settings_page.theme_changed.connect(self.theme_change_requested.emit)
        self.settings_page.language_changed.connect(self.language_change_requested.emit)
        self.settings_page.close_action_changed.connect(self._on_close_action_changed)
        self.logs_page.data_changed.connect(self._on_logs_changed)

    def rebuild_ui(self) -> None:
        """原地重建界面（用于切换主题 / 语言）。

        不重建窗口本身：复用同一个 HWND，避免窗口重新显示造成的整屏闪烁与
        提示气泡一闪而过。
        """
        index = self._current_index
        old = self.centralWidget()
        old.hide()
        self.setCentralWidget(None)
        self._build_ui()
        old.deleteLater()
        # 新中央部件此刻还没被布局撑开，先按窗口尺寸摆好再应用度量
        self._central.setGeometry(0, 0, self.width(), self.height())
        self._apply_layout_metrics()
        self._current_index = -1
        self.switch_page(index, animate=False)
        self._update_nav_footer()
        self._central.update()

    # ------------------------------------------------------------------
    # 无边框窗口：最大化 / 还原、边缘拖拽缩放、圆角
    # ------------------------------------------------------------------
    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            maximized = self.isMaximized() or self.isFullScreen()
            self.setProperty("maximized", bool(maximized))
            self.title_bar.set_maximized(bool(maximized))
            theme.repolish(self)
            self._update_window_mask()
            if not self.isMinimized():
                self.home_page._update_visible_times()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._apply_window_chrome()

    def _apply_window_chrome(self) -> None:
        """Win11：圆角 + 系统阴影（DWM），失败时静默忽略。"""
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes

            hwnd = int(self.winId())
            dwm = ctypes.windll.dwmapi
            preference = ctypes.c_int(2)  # DWMWCP_ROUND
            dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(preference), ctypes.sizeof(preference))

            class _Margins(ctypes.Structure):
                _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int),
                            ("top", ctypes.c_int), ("bottom", ctypes.c_int)]

            margins = _Margins(1, 1, 1, 1)
            dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
        except Exception:
            pass

    _RESIZE_MARGIN = 6
    _HT_CODES = {
        "left": 10, "right": 11, "top": 12, "topleft": 13,
        "topright": 14, "bottom": 15, "bottomleft": 16, "bottomright": 17,
    }

    def _resize_edge(self, pos) -> str:
        if self.isMaximized() or self.isFullScreen():
            return ""
        margin = self._RESIZE_MARGIN
        width, height = self.width(), self.height()
        horizontal = ""
        vertical = ""
        if pos.x() <= margin:
            horizontal = "left"
        elif pos.x() >= width - margin:
            horizontal = "right"
        if pos.y() <= margin:
            vertical = "top"
        elif pos.y() >= height - margin:
            vertical = "bottom"
        return f"{vertical}{horizontal}"

    def nativeEvent(self, event_type, message):  # noqa: N802
        """无边框窗口的边缘缩放：命中测试交给系统，保留原生缩放手感与贴靠。"""
        try:
            is_windows_msg = (event_type == b"windows_generic_MSG"
                              or (hasattr(event_type, "data")
                                  and event_type.data() == b"windows_generic_MSG"))
            if is_windows_msg:
                import ctypes
                from ctypes import wintypes

                class _MSG(ctypes.Structure):
                    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                                ("time", wintypes.DWORD),
                                ("pt_x", wintypes.LONG), ("pt_y", wintypes.LONG)]

                msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
                if msg.message == 0x0084:  # WM_NCHITTEST
                    cursor = QCursor.pos()
                    # 滚动条优先：否则窗口边缘的缩放判定会把贴边的滚动条整段吃掉
                    if not point_hits_scrollbar(cursor, self):
                        edge = self._resize_edge(self.mapFromGlobal(cursor))
                        if edge:
                            return True, self._HT_CODES[edge]
        except Exception:
            pass
        return super().nativeEvent(event_type, message)

    def _on_logs_changed(self) -> None:
        """日志被删除后同步刷新首页的时间信息。"""
        self.home_page.refresh()
        self._update_nav_footer()

    # ------------------------------------------------------------------
    # 尺寸：导航栏 2/9、主内容区 7/9、顶栏占主内容区 3/35
    # ------------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_layout_metrics()
        self._update_window_mask()

    def _update_window_mask(self) -> None:
        """圆角由透明窗口 + 各区域自绘的圆角背景实现。

        不使用 ``setMask``：QRegion 是 1 位掩码，边缘必然出现锯齿；
        交给 Qt 的透明窗口合成反而能得到抗锯齿的圆角。
        """
        if not self.mask().isEmpty():
            self.clearMask()

    def _apply_layout_metrics(self) -> None:
        # 用窗口自身尺寸：重建界面后 centralWidget() 还没被布局撑开，
        # 若此时读它的宽高会得到极小值，导航栏会被压成 0 宽而"消失"
        width = max(1, self.width())
        height = max(1, self.height())
        self.nav.setFixedWidth(int(round(width * config.NAV_RATIO)))
        # 顶栏：以主内容区高度的 3/35 为基准，并给舒适下限，避免标题贴着上沿
        content_h = max(1, height - self.title_bar.height())
        self.topbar.setFixedHeight(
            max(config.sx(72), int(round(content_h * config.TOPBAR_RATIO))))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._apply_layout_metrics()
        if self._pending_refresh:
            self._pending_refresh = False
            self.home_page.refresh()
            self.logs_page.refresh()
            self._update_nav_footer()

    def _update_nav_footer(self) -> None:
        try:
            programs = [dict(p) for p in self.db.programs(visible_only=True)]
        except Exception:
            return
        total = len(programs)
        tracked = sum(1 for p in programs if int(p.get("tracking") or 0))
        self.nav.set_footer(tr("nav.footer", tracked=tracked, total=total))

    # ------------------------------------------------------------------
    # 页面切换（淡入淡出）
    # ------------------------------------------------------------------
    def switch_page(self, index: int, animate: bool = True) -> None:
        if index < 0 or index >= self.stack.count():
            return
        if index == self._current_index and self.stack.currentIndex() == index:
            self.nav.set_current(index)
            return
        self._current_index = index
        self.nav.set_current(index)
        self.topbar.set_title(page_title(index), page_hint(index))

        target = self.stack.widget(index)
        page_topbar = getattr(target, "topbar_widget", None)
        self.topbar.set_right_widget(page_topbar() if callable(page_topbar) else None)

        if index == 0:
            self.home_page.refresh()
        elif index == 1:
            self.logs_page.refresh()
            self.logs_page._sync_topbar_state()
        elif index == 2:
            self.notify_page.refresh()
        elif index == 3:
            self.control_page.refresh_names()

        self.stack.setCurrentIndex(index)

        enter = getattr(target, "page_entered", None)
        if callable(enter) and animate and self.isVisible():
            enter()

        if not animate or not self.isVisible():
            return
        self._fade_curtain()

    def _fade_curtain(self) -> None:
        """页面切换的"淡入"效果。

        不用 QGraphicsOpacityEffect 直接作用在页面上（会把整页渲染到离屏位图，
        文字与边框会出现亚像素级位移，看起来像在晃动），
        改为在页面上盖一层与画布同色的幕布并把透明度动画到 0。
        """
        view = self.stack
        curtain = QWidget(view)
        curtain.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        curtain.setAttribute(Qt.WA_StyledBackground, True)
        curtain.setStyleSheet(f"background: {config.C_APP_BG};")
        curtain.setGeometry(view.rect())
        curtain.show()
        curtain.raise_()

        effect = QGraphicsOpacityEffect(curtain)
        effect.setOpacity(0.9)
        curtain.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", curtain)
        animation.setDuration(config.ANIM_NORMAL)
        animation.setStartValue(0.9)
        animation.setEndValue(0.0)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.finished.connect(curtain.deleteLater)
        animation.start(QPropertyAnimation.DeleteWhenStopped)

    # ------------------------------------------------------------------
    # 关闭 / 托盘
    # ------------------------------------------------------------------
    def _on_tray_icon_changed(self, path: str) -> None:
        pass

    def _on_close_action_changed(self, key: str) -> None:
        self.db.set_setting(config.S_CLOSE_ACTION, key)

    def close_action(self) -> str:
        return self.db.get_setting(config.S_CLOSE_ACTION, config.CLOSE_ACTION_TRAY)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._force_quit or self.close_action() == config.CLOSE_ACTION_EXIT:
            self.quit_requested.emit()
            event.accept()
            return
        event.ignore()
        self.hide()

    def request_quit(self) -> None:
        self._force_quit = True
        self.close()

    def prepare_quit(self) -> None:
        self._force_quit = True
        self.hide()
