"""Offscreen transition geometry/input/lifetime regression checks; no live sampling."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['MONITOR_REDUCED_MOTION'] = '0'
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QAbstractAnimation, QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
from shiboken6 import isValid
from monitor.ui import anim, motion, theme
from monitor import config
from monitor.ui.controls import SlimScrollArea
from monitor.ui.notify_page import _Section, DurationSelector
from monitor.ui.control_page import _RuleCard
from monitor.ui.popups import MenuPopup, SelectBox, _PopupExit
from monitor.control import Rule
from monitor.database import Database
from monitor.ui.settings_page import SettingsPage

APP = QApplication.instance() or QApplication([])
motion.track_input()


def flush():
    APP.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    APP.processEvents()


class TransitionTests(unittest.TestCase):
    def setUp(self):
        APP.setProperty('reduceMotion', False)
        APP._monitor_input_modality.keyboard = False
        self.scroll = SlimScrollArea()
        self.scroll.resize(900, 500)
        self.canvas = QWidget()
        self.layout = QVBoxLayout(self.canvas)
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.canvas)
        self.scroll.show()
        APP.processEvents()

    def tearDown(self):
        for widget in QApplication.topLevelWidgets():
            widget.deleteLater()
        flush()
        APP.setProperty('reduceMotion', None)

    def mount(self, card):
        self.layout.addWidget(card)
        after = QLabel('Following content')
        self.layout.addWidget(after)
        self.layout.addStretch(1)
        APP.processEvents()
        return after

    def finish(self, content):
        holder = anim.clip_holder(content)
        if holder._height_anim is not None:
            holder._height_anim.setCurrentTime(holder._height_anim.duration())
        APP.processEvents()
        return holder

    def check_collapse(self, card, content, after):
        card.set_expanded(True, animate=False)
        APP.processEvents()
        initial = (card.height(), after.y())
        card.set_expanded(False)
        holder = anim.clip_holder(content)
        animation = holder._height_anim
        rows = []
        child_size = content.size()
        for time in (0, 20, 50, 100, 150, 190):
            animation.setCurrentTime(time)
            APP.processEvents()
            rows.append((holder.height(), card.height(), after.y()))
            self.assertEqual(content.size(), child_size, rows)
        self.assertLess(rows[1][1], initial[0], rows)
        self.assertLess(rows[1][2], initial[1], rows)
        for first, second in zip(rows, rows[1:]):
            self.assertTrue(all(b <= a for a, b in zip(first, second)), rows)
        before = rows[-1]
        animation.setCurrentTime(animation.duration())
        APP.processEvents()
        self.assertLessEqual(before[1] - card.height(), 3)
        self.assertLessEqual(before[2] - after.y(), 3)
        self.assertTrue(holder.isHidden())

    def test_notify_section_collapses_parent_and_following_sibling_each_frame(self):
        card = _Section('bell', 'Notification', 'Description')
        for _ in range(8):
            card.content_layout.addWidget(QLabel('Notification event'))
        after = self.mount(card)
        self.check_collapse(card, card.content, after)

    def test_expansion_does_not_reserve_target_gap_or_move_header(self):
        card = _Section('bell', 'Notification', 'Longer description')
        for _ in range(8):
            card.content_layout.addWidget(QLabel('Content'))
        after = self.mount(card)
        before = after.y()
        header_y = card.arrow.y()
        card.set_expanded(True)
        holder = anim.clip_holder(card.content)
        animation = holder._height_anim
        self.assertLessEqual(after.y() - before, 1)
        last = holder.height()
        for time in (20, 60, 120, 190):
            animation.setCurrentTime(time)
            APP.processEvents()
            self.assertGreaterEqual(holder.height(), last)
            self.assertEqual(card.arrow.y(), header_y)
            last = holder.height()
        old = card.height()
        animation.setCurrentTime(animation.duration())
        APP.processEvents()
        self.assertLessEqual(abs(card.height() - old), 3)

    def test_rule_card_continuous_geometry(self):
        card = _RuleCard(dict(id=1, name='Test program', path='', icon_path='', kind='program'),
                         Rule(program_id=1), None)
        after = self.mount(card)
        self.check_collapse(card, card.content, after)

    def test_nested_duration_changes_outer_card_each_frame_and_reverses(self):
        card = _Section('bell', 'Notification', expanded=True)
        selector = DurationSelector([30, 60, 90], card.content)
        card.content_layout.addWidget(selector)
        after = self.mount(card)
        anim.clip_height(card.content, None, visible=True)
        selector.set_expanded(True, animate=False)
        APP.processEvents()
        selector.set_expanded(False)
        holder = anim.clip_holder(selector.panel)
        animation = holder._height_anim
        animation.setCurrentTime(50)
        APP.processEvents()
        before = (holder.height(), card.height(), after.y())
        selector.set_expanded(True)
        self.assertEqual(holder.height(), before[0])
        holder._height_anim.setCurrentTime(100)
        APP.processEvents()
        self.assertGreater(holder.height(), before[0])
        selector.set_expanded(False)
        self.finish(selector.panel)
        self.assertLess(card.height(), before[1])
        self.assertLess(after.y(), before[2])

    def test_popup_fixed_text_geometry_and_exit_is_input_free_and_reclaimed(self):
        anchor = SelectBox([('a', 'Alpha'), ('b', 'Beta')], parent=self.canvas)
        self.mount(anchor)
        popup = MenuPopup(anchor._options, parent=anchor)
        popup.popup_at(anchor)
        geometry = popup.container.geometry()
        for time in (0, 30, 90, 179):
            popup._opening.setCurrentTime(time)
            APP.processEvents()
            self.assertEqual(popup.container.geometry(), geometry)
        popup._opening.setCurrentTime(180)
        popup.close()
        self.assertIsNone(popup._exit_ghost)
        self.assertTrue(popup.isVisible())
        popup._closing.setCurrentTime(popup._closing.duration())
        flush()
        self.assertFalse(isValid(popup))

    def test_select_keyboard_selection_and_reduced_motion_are_immediate(self):
        select = SelectBox([('a', 'Alpha'), ('b', 'Beta')], parent=self.canvas)
        self.mount(select)
        select.setFocus()
        QTest.keyClick(select, Qt.Key_Down)
        popup = select._popup
        self.assertIsNotNone(popup)
        self.assertIsNone(popup._opening)
        QTest.keyClick(popup, Qt.Key_Down)
        QTest.keyClick(popup, Qt.Key_Return)
        self.assertEqual(select.current_key(), 'b')
        flush()
        APP.setProperty('reduceMotion', True)
        APP._monitor_input_modality.keyboard = False
        card = _Section('bell', 'Reduced motion')
        card.content_layout.addWidget(QLabel('Content'))
        self.layout.insertWidget(0, card)
        card.set_expanded(True)
        self.assertIsNone(anim.clip_holder(card.content)._height_anim)
        card.set_expanded(False)
        self.assertTrue(anim.clip_holder(card.content).isHidden())

    def test_glass_settings_section_uses_same_continuous_layout(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            db = Database(str(Path(tmp) / 'settings.db'))
            db.set_setting(config.S_THEME, config.THEME_GLASS)
            try:
                with patch('monitor.ui.settings_page.autostart.is_enabled', return_value=False):
                    page = SettingsPage(db)
                page.resize(1000, 700)
                page.show()
                APP.processEvents()
                section = page.glass_section
                section.set_expanded(True, animate=False)
                APP.processEvents()
                original = section.height()
                section.set_expanded(False)
                holder = anim.clip_holder(section.content)
                holder._height_anim.setCurrentTime(60)
                APP.processEvents()
                self.assertLess(section.height(), original)
                self.finish(section.content)
                page.deleteLater()
                flush()
            finally:
                db.close()

    def test_repeated_popups_do_not_accumulate_hidden_children(self):
        select = SelectBox([('a', 'Alpha')], parent=self.canvas)
        self.mount(select)
        for i in range(20):
            select._open()
            popup = select._popup
            if popup._opening is not None:
                popup._opening.setCurrentTime(60 if i % 2 else 180)
            popup.close()
            if popup._closing is not None:
                popup._closing.setCurrentTime(popup._closing.duration())
            ghost = popup._exit_ghost
            if ghost is not None:
                ghost._animation.setCurrentTime(ghost._animation.duration())
            flush()
            self.assertIsNone(select._popup)
            self.assertEqual(len(select.findChildren(MenuPopup)), 0)
            self.assertEqual(len(self.scroll.findChildren(_PopupExit)), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
