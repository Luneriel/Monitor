"""Isolated functional checks. No live monitoring, hooks, audio or performance sampling."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from monitor.database import Database
from monitor.tracker import ProcessTracker
from monitor.control import State, Decision
from monitor.control_service import ControlService
from monitor.overlay import OverlayManager
from monitor.ui.home_page import HomePage

APP = QApplication.instance() or QApplication([])


class IdleWorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.db = Database(str(Path(self.tmp.name) / 'test.db'))
        self.pid = self.db.add_program(r'C:\test\sample.exe', 'sample')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_config_resync_does_not_wake_unchanged_tracker(self):
        tracker = ProcessTracker(self.db)
        changed = Mock()
        tracker.targets_changed.connect(changed)
        tracker._resync()
        tracker._dirty.clear()
        tracker._resync()
        self.assertFalse(tracker._dirty.is_set())
        self.assertEqual(changed.call_count, 1)
        self.db.set_tracking(self.pid, False)
        tracker.set_programs(self.db.programs(visible_only=True), notify=False)
        self.assertTrue(tracker._dirty.is_set())
        self.assertEqual(changed.call_count, 2)
        self.assertNotIn(self.pid, tracker._programs)
        self.db.set_tracking(self.pid, True)
        tracker._resync()
        self.assertIn(self.pid, tracker._fresh)

    def test_wait_can_be_woken_by_stop_without_polling(self):
        tracker = ProcessTracker(self.db)
        tracker._scan = Mock()
        tracker.close_all = Mock()
        tracker._dirty = Mock()
        tracker._dirty.wait.side_effect = lambda timeout: tracker.stop()
        tracker.run()  # synchronous; no real thread or process scan
        self.assertEqual(tracker._scan.call_count, 1)
        self.assertEqual(tracker._dirty.wait.call_count, 1)
        tracker.close_all.assert_called_once()

    def test_batch_is_one_transaction_and_rolls_back(self):
        other = self.db.add_program(r'C:\test\other.exe', 'other')
        statements = []
        self.db._conn.set_trace_callback(statements.append)
        self.db.save_control_states({self.pid: {'used_seconds': 10}, other: {'used_seconds': 20}})
        self.assertEqual(sum(s == 'COMMIT' for s in statements), 1)
        self.assertEqual(self.db.control_state(other)['used_seconds'], 20)
        with self.assertRaises(Exception):
            self.db.save_control_states({self.pid: {'used_seconds': 50}, other: {'invalid_column': 1}})
        self.assertEqual(self.db.control_state(self.pid)['used_seconds'], 10)

    def test_dirty_save_retry_and_force(self):
        state = State(self.pid, used_seconds=10)
        service = SimpleNamespace(db=Mock(), engine=SimpleNamespace(states={self.pid: state}),
                                  _saved_states={})
        ControlService.save(service)
        ControlService.save(service)
        self.assertEqual(service.db.save_control_states.call_count, 1)
        state.used_seconds = 11
        service.db.save_control_states.side_effect = RuntimeError('simulated write failure')
        ControlService.save(service)
        self.assertEqual(service._saved_states[self.pid]['used_seconds'], 10)
        service.db.save_control_states.side_effect = None
        ControlService.save(service)
        self.assertEqual(service._saved_states[self.pid]['used_seconds'], 11)
        ControlService.save(service, force=True)
        self.assertEqual(service.db.save_control_states.call_count, 4)

    def test_overlay_idle_active_idle_transitions(self):
        blocked = {}
        with patch('monitor.overlay.ForegroundHook'), patch('monitor.overlay.InputBlocker'):
            muter = Mock()
            manager = OverlayManager(lambda: blocked, lambda pid: [], muter=muter)
            manager._maintain_effects = Mock()
            self.assertFalse(manager._timer.isActive())
            self.assertFalse(manager._follow_timer.isActive())
            manager.sync()
            manager.sync()
            muter.restore.assert_called_once()
            blocked[1] = Decision(1, blocked=True)
            manager.sync()
            self.assertTrue(manager._timer.isActive())
            self.assertTrue(manager._follow_timer.isActive())
            self.assertEqual(manager._timer.interval(), 120)
            self.assertEqual(manager._follow_timer.interval(), 30)
            blocked.clear()
            manager.sync()
            manager.sync()
            self.assertFalse(manager._timer.isActive())
            self.assertFalse(manager._follow_timer.isActive())
            self.assertEqual(muter.restore.call_count, 2)
            manager.shutdown()

    def test_hidden_home_skips_ticks_and_show_refreshes(self):
        tracker = ProcessTracker(self.db)
        page = HomePage(self.db, tracker)
        page.update_times = Mock()
        tracker.tick.emit()
        page.update_times.assert_not_called()
        page.show()
        APP.processEvents()
        self.assertGreater(page.update_times.call_count, 0)
        page.update_times.reset_mock()
        page.hide()
        tracker.tick.emit()
        page.update_times.assert_not_called()
        page.deleteLater()
        APP.processEvents()


if __name__ == '__main__':
    unittest.main(verbosity=2)
