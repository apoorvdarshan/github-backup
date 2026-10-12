import datetime as dt
import unittest
from unittest.mock import patch
import monitor


class MonitorTests(unittest.TestCase):
    def test_age_and_timezone(self):
        now = dt.datetime(2026, 10, 12, 12, tzinfo=dt.timezone.utc)
        self.assertEqual(monitor.age_hours('2026-10-11T12:00:00Z', now), 24)
        with self.assertRaises(ValueError):
            monitor.age_hours('2026-10-13T12:00:00Z', now)

    def test_missing_success_fails(self):
        with patch.object(monitor, 'get', return_value=[]):
            self.assertEqual(monitor.main(), 1)

    def test_old_backup_fails(self):
        with patch.object(monitor, 'get', side_effect=[[{'id': 1}], {'status': 'success', 'finished_at': '2020-01-01T00:00:00Z'}]):
            self.assertEqual(monitor.main(), 1)

    def test_recent_backup_passes(self):
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
        with patch.object(monitor, 'get', side_effect=[[{'id': 1}], {'status': 'success', 'finished_at': timestamp}]):
            self.assertEqual(monitor.main(), 0)


if __name__ == '__main__':
    unittest.main()
