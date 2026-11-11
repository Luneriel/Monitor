"""Object-lifetime regression checks; offscreen, no live performance sampling."""
import os
import sys
import unittest
import tempfile
from unittest.mock import Mock
from pathlib import Path
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QAbstractAnimation, QCoreApplication, QEvent, QRect, QTimer
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel
from shiboken6 import isValid
from monitor.ui import anim
from monitor.ui.controls import Card, CheckBox, ToggleSwitch, SlimScrollArea
from monitor.ui.main_window import TopBar, MainWindow
from monitor.database import Database
from monitor.tracker import ProcessTracker
from monitor.ui.reorder import ReorderController

APP = QApplication.instance() or QApplication([])


def flush():
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    APP.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


class LifetimeTests(unittest.TestCase):
    def tearDown(self):
        for widget in QApplication.topLevelWidgets():
            widget.deleteLater()
        flush()

    def test_card_reuses_animation(self):
        card = Card()
        for i in range(100):
            card._animate_hover(float(i % 2))
            card._hover_anim.setCurrentTime(card._hover_anim.duration())
        self.assertEqual(len(card.findChildren(QAbstractAnimation)), 1)

    def test_checkbox_releases_completed_and_interrupted(self):
        box = CheckBox()
        for i in range(60):
            box.nextCheckState()
            if box._check_anim is not None:
                duration = box._check_anim.duration()
                box._check_anim.setCurrentTime(duration if i % 2 else duration // 2)
            flush()
            self.assertLessEqual(len(box.findChildren(QAbstractAnimation)), 1)
        box.setChecked(False)
        flush()
        self.assertEqual(box._progress, 0)
        self.assertEqual(len(box.findChildren(QAbstractAnimation)), 0)

    def test_switch_releases_and_preserves_final_state(self):
        switch = ToggleSwitch()
        for i in range(60):
            switch.setChecked(not switch.isChecked())
            if i % 2:
                switch._anim.setCurrentTime(switch._anim.duration())
            flush()
            self.assertLessEqual(len(switch.findChildren(QAbstractAnimation)), 1)
        switch.setChecked(True, animate=False)
        flush()
        self.assertTrue(switch.isChecked())
        self.assertEqual(switch._offset, 1)
        self.assertEqual(len(switch.findChildren(QAbstractAnimation)), 0)

    def test_global_registry_releases_destroyed_owner(self):
        before = set(anim._running)
        owner = QWidget()
        anim.slide_in(owner, duration=10000)
        self.assertGreater(len(anim._running), len(before))
        owner.deleteLater()
        flush()
        self.assertEqual(anim._running, before)

    def test_completed_slide_deleted(self):
        owner = QWidget()
        for _ in range(30):
            anim.slide_in(owner)
            animation = owner.findChild(QAbstractAnimation)
            animation.setCurrentTime(animation.duration())
            flush()
            self.assertEqual(len(owner.findChildren(QAbstractAnimation)), 0)

    def test_height_animation_releases(self):
        owner = QWidget()
        layout = QVBoxLayout(owner)
        content = QWidget(owner)
        QVBoxLayout(content).addWidget(QLabel('Content'))
        layout.addWidget(content)
        for i in range(40):
            anim.animate_height(content, not bool(i % 2))
            holder = anim.clip_holder(content)
            animation = holder._height_anim
            if i % 3:
                animation.setCurrentTime(animation.duration())
            flush()
            self.assertLessEqual(len(owner.findChildren(QAbstractAnimation)), 1)
        anim.animate_height(content, False)
        holder._height_anim.setCurrentTime(holder._height_anim.duration())
        flush()
        self.assertTrue(holder.isHidden())
        self.assertEqual(len(owner.findChildren(QAbstractAnimation)), 0)

    def test_drag_slide_releases(self):
        scroll = SlimScrollArea()
        holder = QWidget()
        scroll.setWidget(holder)
        card = QWidget(holder)
        controller = ReorderController(scroll, lambda *args: None)
        for i in range(40):
            controller._start_slides({card: QRect(0, 0, 100, 40)},
                                     {card: QRect(0, 50, 100, 40)})
            animation = controller._slides[card]
            if i % 2:
                animation.setCurrentTime(animation.duration())
            else:
                controller._stop_slides()
            flush()
            self.assertEqual(len(card.findChildren(QAbstractAnimation)), 0)
        controller.deleteLater()

    def test_detached_topbar_widget_dies_with_topbar(self):
        bar = TopBar()
        button = QWidget()
        bar.set_right_widget(button)
        bar.set_right_widget(None)
        self.assertIs(button.parentWidget(), bar.right_slot)
        self.assertTrue(button.isHidden())
        bar.set_right_widget(button)
        self.assertFalse(button.isHidden())
        bar.deleteLater()
        flush()
        self.assertFalse(isValid(button))

    def test_rebuild_destroys_old_pages_and_detached_menu(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            db = Database(str(Path(tmp) / 'lifetime.db'))
            tracker = ProcessTracker(db)
            window = None
            try:
                service = Mock()
                service.enabled.return_value = False
                service.has_password.return_value = True
                service.session_grace.return_value = 60
                service.lock_remaining.return_value = 0
                service.all_rules.return_value = {}
                service.self_program_ids.return_value = set()
                window = MainWindow(db, tracker, control=service)
                for _ in range(5):
                    window.switch_page(1, animate=False)
                    menu = window.logs_page.menu_btn
                    window.switch_page(0, animate=False)
                    pages = [window.stack.widget(i) for i in range(window.stack.count())]
                    window.rebuild_ui()
                    flush()
                    self.assertTrue(all(not isValid(page) for page in pages))
                    self.assertFalse(isValid(menu))
                    tracker.tick.emit()
                    tracker.targets_changed.emit()
            finally:
                if window is not None:
                    window.deleteLater()
                flush()
                db.close()

    def test_list_removal_releases_guard(self):
        for forced in (False, True):
            owner = QWidget()
            layout = QVBoxLayout(owner)
            victim = QLabel('victim', owner)
            layout.addWidget(victim)
            owner.show()
            APP.processEvents()
            completed = []
            group = anim.animate_list_removal(owner, [victim], 0,
                                              on_finished=lambda: completed.append(True))
            if forced:
                owner.findChild(QTimer).timeout.emit()
            else:
                group.setCurrentTime(group.duration())
            flush()
            self.assertEqual(completed, [True])
            self.assertEqual(len(owner.findChildren(QTimer)), 0)
            self.assertEqual(len(owner.findChildren(QAbstractAnimation)), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
