"""Isolated log deletion regression: real Qt event loop, temporary database."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QWidget, QVBoxLayout, QLabel
from monitor.ui import anim
from monitor.database import Database
from monitor.tracker import ProcessTracker
from monitor.ui.logs_page import LogsPage

APP = QApplication.instance() or QApplication([])


class LogDeletionTests(unittest.TestCase):
    def test_rebuild_runs_after_animation_callback_returns(self):
        self._assert_deferred_rebuild(False)

    def test_timeout_rebuild_runs_after_timer_callback_returns(self):
        self._assert_deferred_rebuild(True)

    def _assert_deferred_rebuild(self, forced):
        panel = QWidget()
        row = QLabel('log', panel)
        QVBoxLayout(panel).addWidget(row)
        panel.show()
        APP.processEvents()
        inside_animation = False
        observed = []

        def rebuild():
            observed.append(inside_animation)
            panel.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

        group = anim.animate_list_removal(panel, [row], 0, on_finished=rebuild)
        inside_animation = True
        if forced:
            panel.findChild(QTimer).timeout.emit()
        else:
            group.setCurrentTime(group.duration())
        inside_animation = False
        APP.processEvents()
        self.assertEqual(observed, [False], 'rebuild must not delete an active Qt animation stack')

    def test_closing_page_cancels_pending_rebuild(self):
        panel = QWidget()
        row = QLabel('log', panel)
        QVBoxLayout(panel).addWidget(row)
        panel.show()
        APP.processEvents()
        completed = []
        group = anim.animate_list_removal(panel, [row], 0,
                                          on_finished=lambda: completed.append(True))
        group.setCurrentTime(group.duration())
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        APP.processEvents()
        self.assertEqual(completed, [])

    def test_delete_then_refresh(self):
        errors = []
        previous = sys.excepthook
        sys.excepthook = lambda *args: errors.append(args[1])
        try:
            with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
                db = Database(str(Path(tmp) / 'logs.db'))
                tracker = ProcessTracker(db)
                page = LogsPage(db, tracker)
                try:
                    pid = db.add_program(r'C:\test\sample.exe', 'sample')
                    ids = []
                    for i in range(3):
                        log = db.open_log(pid, i + 1, time.time() - 100 + i)
                        db.close_log(log, time.time() - 50 + i)
                        ids.append(log)
                    page.resize(900, 600)
                    page.show()
                    page.refresh(force=True)
                    APP.processEvents()
                    for selected in ([ids[0]], ids[1:]):
                        page.enter_delete_mode()
                        page.selected = set(selected)
                        with patch('monitor.ui.logs_page.ConfirmDeleteLogsDialog.run',
                                   return_value=QDialog.Accepted):
                            page.on_confirm_delete()
                        QTest.qWait(1200)
                        self.assertFalse(page._animating)
                        self.assertFalse(db.logs_by_ids(selected))
                        page.refresh(force=True)
                    self.assertFalse(page.rows)
                    self.assertFalse(errors, errors)
                finally:
                    page.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                    db.close()
        finally:
            sys.excepthook = previous


if __name__ == '__main__':
    unittest.main(verbosity=2)
