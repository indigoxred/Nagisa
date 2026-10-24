"""Bounded private upkeep for native TMDB link claims."""

import tempfile
import unittest
from pathlib import Path

from nagisa.seerr_compat_mapping_audit import OwnedMappingAudit
from nagisa.seerr_journal import JsonSessionJournal


class Store:
    def __init__(self):
        self.ids = [11, 12, 13]

    def owned_file_ids_after(self, last, limit):
        return tuple(item for item in self.ids if item > last)[:limit]


class Events:
    def __init__(self):
        self.calls = []
        self.fail = False

    def enqueue_exact_file_ids(self, ids):
        if self.fail:
            raise ValueError('queue full')
        self.calls.append(ids)


class OwnedMappingAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.journal = JsonSessionJournal(Path(self.temp.name) / 'mapping.json')
        self.store = Store()
        self.events = Events()
        self.now = 1000.0

    def audit(self):
        return OwnedMappingAudit(self.store, self.events, self.journal,
                                 batch_size=2, interval_seconds=60,
                                 clock=lambda: self.now)

    def test_bounded_cursor_survives_restart_and_wraps(self):
        audit = self.audit()
        self.assertEqual(audit.run_once(), (11, 12))
        self.assertEqual(self.audit().run_once(), (13,))
        self.assertEqual(self.audit().run_once(), (11, 12))
        self.assertEqual(self.events.calls, [(11, 12), (13,), (11, 12)])

    def test_queue_failure_does_not_advance_mapping_cursor(self):
        self.events.fail = True
        audit = self.audit()
        with self.assertRaisesRegex(ValueError, 'queue full'):
            audit.run_once()
        self.events.fail = False
        self.assertIsNone(audit.run_once())
        self.assertEqual(self.audit().run_once(), (11, 12))
        self.now += 60
        self.assertEqual(audit.run_once(), (13,))

    def test_waits_between_batches_in_one_running_worker(self):
        audit = self.audit()
        self.assertEqual(audit.run_once(), (11, 12))
        self.assertIsNone(audit.run_once())
        self.now += 60
        self.assertEqual(audit.run_once(), (13,))
