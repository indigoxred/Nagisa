"""Missed Shoko deletion recovery uses only owned IDs and existing guards."""

import unittest

from nagisa.seerr_compat_owned_audit import OwnedDeletionAudit


class OwnedDeletionAuditTests(unittest.TestCase):
    def test_only_absent_owned_files_are_queued_and_scan_is_rate_limited(self):
        class Census:
            calls = 0

            def read_ids(self):
                self.calls += 1
                return (1, 2, 4, 5)

        class TVStore:
            def manifest(self):
                return {'links': {
                    '10/1/1': {'file_id': 1},
                    '10/1/2': {'file_id': 3},
                }}

        class MovieStore:
            def manifest(self):
                return {1418665: {
                    'desired': {'links': [{'file_id': 4}]},
                    'previous': {'links': [{'file_id': 6}]},
                }}

        class TVEvents:
            queued = None

            def enqueue_deleted_file_ids(self, ids):
                self.queued = tuple(ids)

        class MovieEvents:
            queued = []

            def enqueue_file_deletion(self, file_id):
                self.queued.append(file_id)

        now = [1000.0]
        census, tv, movie = Census(), TVEvents(), MovieEvents()
        audit = OwnedDeletionAudit(census, TVStore(), tv,
                                   MovieStore(), movie,
                                   clock=lambda: now[0])
        self.assertEqual(audit.run_once(), (1, 1))
        self.assertEqual(tv.queued, (3,))
        self.assertEqual(movie.queued, [6])
        self.assertEqual(audit.run_once(), None)
        self.assertEqual(census.calls, 1)
        now[0] += 21600
        self.assertEqual(audit.run_once(), (1, 1))
        self.assertEqual(census.calls, 2)

    def test_failed_queue_does_not_advance_sweep_time(self):
        class Census:
            calls = 0

            def read_ids(self):
                self.calls += 1
                return ()

        class TVStore:
            def manifest(self):
                return {'links': {'10/1/1': {'file_id': 3}}}

        class TVEvents:
            calls = 0

            def enqueue_deleted_file_ids(self, ids):
                self.calls += 1
                if self.calls == 1:
                    raise OSError('journal unavailable')

        now = [1000.0]
        census, tv = Census(), TVEvents()
        audit = OwnedDeletionAudit(census, TVStore(), tv,
                                   clock=lambda: now[0])
        with self.assertRaises(OSError):
            audit.run_once()
        self.assertIsNone(audit.run_once())
        now[0] += 300
        self.assertEqual(audit.run_once(), (1, 0))
        self.assertEqual(census.calls, 2)


if __name__ == '__main__':
    unittest.main()
