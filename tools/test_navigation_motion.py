"""Offscreen navigation trajectory checks (no live monitor or user database)."""
import os
import sys
import unittest
from pathlib import Path
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QAbstractAnimation, QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid
from monitor.ui.main_window import NavBar

app = QApplication.instance() or QApplication([])


class NavigationTests(unittest.TestCase):
    def setUp(self):
        app.setProperty('reduceMotion', False)
        self.nav = NavBar()
        self.nav.resize(220, 700)
        self.nav.show()
        app.processEvents()
        app._monitor_input_modality.keyboard = False

    def tearDown(self):
        self.nav.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.setProperty('reduceMotion', None)

    def test_continuous_retarget_preserves_hit_geometry(self):
        before = [b.geometry() for b in self.nav.buttons]
        self.nav.set_current(2)
        animation = self.nav._highlight_motion
        animation.setCurrentTime(70)
        middle = self.nav._highlight.geometry()
        self.assertGreater(middle.y(), before[0].y())
        self.assertLess(middle.y(), before[2].y())
        self.nav.set_current(1)
        self.assertEqual(animation.startValue(), middle)
        animation.setCurrentTime(animation.duration())
        self.assertEqual(self.nav._highlight.geometry(), before[1])
        self.assertEqual(before, [b.geometry() for b in self.nav.buttons])
        self.assertEqual(len(self.nav.findChildren(QAbstractAnimation)), 1)

    def test_reduced_keyboard_and_resize(self):
        app.setProperty('reduceMotion', True)
        self.nav.set_current(3)
        self.assertEqual(self.nav._highlight.geometry(), self.nav.buttons[3].geometry())
        app.setProperty('reduceMotion', False)
        app._monitor_input_modality.keyboard = True
        self.nav.set_current(4)
        self.assertEqual(self.nav._highlight_motion.state(), QAbstractAnimation.Stopped)
        self.nav.resize(180, 550)
        app.processEvents()
        self.assertEqual(self.nav._highlight.geometry(), self.nav.buttons[4].geometry())


if __name__ == '__main__':
    unittest.main(verbosity=2)
