import os
import sys
import unittest
from pathlib import Path
os.environ['QT_QPA_PLATFORM']='offscreen'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton
from shiboken6 import isValid
from monitor.ui.popups import SelectBox, PopupPanel, _PopupExit
APP=QApplication.instance() or QApplication([])


def flush():
    APP.processEvents()
    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)


class PopupTests(unittest.TestCase):
    def setUp(self):
        APP.setProperty('reduceMotion',False)
        self.host=QWidget()
        layout=QVBoxLayout(self.host)
        self.anchor=SelectBox([('a','Alpha'),('b','Beta')],parent=self.host)
        self.other=QPushButton('Other',self.host)
        layout.addWidget(self.anchor)
        layout.addWidget(self.other)
        self.host.show()
        flush()

    def tearDown(self):
        self.host.deleteLater()
        flush()
        APP.setProperty('reduceMotion',None)

    def test_second_physical_click_closes_without_reopening(self):
        QTest.mouseClick(self.anchor,Qt.LeftButton)
        popup=self.anchor._popup
        self.assertIsNotNone(popup)
        popup._opening.setCurrentTime(180)
        QTest.mouseClick(self.anchor,Qt.LeftButton)
        self.assertIs(self.anchor._popup,popup)
        self.assertIsNotNone(popup._closing)
        popup._closing.setCurrentTime(130)
        flush()
        self.assertIsNone(self.anchor._popup)
        QTest.mouseClick(self.anchor,Qt.LeftButton)
        self.assertIsNotNone(self.anchor._popup)
        self.assertIsNot(self.anchor._popup,popup)

    def test_close_uses_same_surface_no_ghost(self):
        QTest.mouseClick(self.anchor,Qt.LeftButton)
        popup=self.anchor._popup
        popup._opening.setCurrentTime(180)
        rect=popup.geometry()
        popup.close()
        popup._closing.setCurrentTime(60)
        self.assertEqual(popup.geometry(),rect)
        self.assertTrue(popup.isVisible())
        self.assertFalse(self.host.findChildren(_PopupExit))
        popup._closing.setCurrentTime(130)
        flush()
        self.assertFalse(isValid(popup))

    def test_outside_click_still_delivers_action(self):
        clicks=[]
        self.other.clicked.connect(lambda:clicks.append(True))
        QTest.mouseClick(self.anchor,Qt.LeftButton)
        popup=self.anchor._popup
        popup._opening.setCurrentTime(180)
        QTest.mouseClick(self.other,Qt.LeftButton)
        # Offscreen doesn't emulate every platform's popup stack; verify the
        # actual outside action is never consumed and native hide has no ghost.
        popup.hide()
        flush()
        self.assertEqual(clicks,[True])
        self.assertFalse(self.host.findChildren(_PopupExit))


if __name__=='__main__':unittest.main(verbosity=2)
