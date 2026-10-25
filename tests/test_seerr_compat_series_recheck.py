"""A changed Shoko series must durably recheck all exact file IDs."""

import tempfile
import unittest
import json
from pathlib import Path

from nagisa.seerr_compat_series_recheck import (
    ScopedSeriesRecheck, OwnedSeriesAudit, parse_metadata_series_frames)
from nagisa.seerr_compat_events import CompatibilityLinkEvents
from nagisa.seerr_journal import JsonSessionJournal


class Shoko:
    def __init__(self, response):
        self.response = response
        self.paths = []

    def request_json(self, method, path):
        self.paths.append((method, path))
        return self.response


class Events:
    def __init__(self):
        self.calls = []
        self.fail = False

    def enqueue_exact_file_ids(self, ids):
        if self.fail:
            raise ValueError('queue full')
        self.calls.append(ids)


class ScopedSeriesRecheckTests(unittest.TestCase):
    def test_metadata_frames_name_exact_changed_series(self):
        def frame(target, payload):
            return json.dumps({'type': 1, 'target': target,
                               'arguments': [payload]}) + '\x1e'
        self.assertEqual(parse_metadata_series_frames(
            frame('metadata:links.changed', {'ShokoSeriesIDs': [410]})
            + frame('metadata:episode.updated', {'ShokoSeriesIDs': [410, 411]})
            + frame('file:hashed', {'FileID': 42})), (410, 411))
        with self.assertRaises(ValueError):
            parse_metadata_series_frames(frame(
                'metadata:links.changed', {'ShokoSeriesIDs': ['410']}))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = JsonSessionJournal(Path(self.temp.name) / 'series.json')
        self.shoko = Shoko({'Total': 2, 'List': [
            {'IDs': {'ParentSeries': 410}, 'Files': [{'ID': 41}, {'ID': 42}]},
            {'IDs': {'ParentSeries': 410}, 'Files': [{'ID': 42}, {'ID': 43}]},
        ]})
        self.events = Events()

    def build(self):
        return ScopedSeriesRecheck(self.shoko, self.events, self.journal,
                                   batch_size=2)

    def test_unowned_ids_are_queued_with_restartable_batches(self):
        worker = self.build()
        worker.enqueue_series_ids((410,))
        self.assertEqual(worker.run_once(), (41, 42))
        self.assertEqual(self.build().run_once(), (43,))
        self.assertEqual(self.events.calls, [(41, 42), (43,)])
        self.assertEqual(self.build().run_once(), ())
        self.assertEqual(self.shoko.paths, [
            ('GET', '/api/v3/Series/410/Episode?page=1&pageSize=0&includeFiles=true')])

    def test_queue_failure_preserves_exact_pending_ids(self):
        worker = self.build()
        worker.enqueue_series_ids((410,))
        self.events.fail = True
        with self.assertRaisesRegex(ValueError, 'queue full'):
            worker.run_once()
        self.events.fail = False
        self.assertEqual(self.build().run_once(), (41, 42))

    def test_unavailable_series_is_retained_without_starving_next_series(self):
        class IntermittentShoko:
            def __init__(self, good):
                self.good = good
                self.fail = True

            def request_json(self, method, path):
                if '/410/' in path and self.fail:
                    raise ValueError('series temporarily unavailable')
                return self.good

        shoko = IntermittentShoko({
            'Total': 1, 'List': [
                {'IDs': {'ParentSeries': 420}, 'Files': [{'ID': 50}]}]})
        worker = ScopedSeriesRecheck(shoko, self.events, self.journal,
                                     batch_size=2)
        worker.enqueue_series_ids((410, 420))
        self.assertEqual(worker.run_once(), ())
        self.assertEqual(worker.run_once(), (50,))
        self.assertEqual(self.journal.load()['series_ids'], [410])

    def test_foreign_series_or_incomplete_page_keeps_series_queued(self):
        worker = self.build()
        worker.enqueue_series_ids((410,))
        self.shoko.response['List'][1]['IDs']['ParentSeries'] = 411
        self.assertEqual(worker.run_once(), ())
        self.assertEqual(self.journal.load()['series_ids'], [410])
        self.shoko.response['List'][1]['IDs']['ParentSeries'] = 410
        self.assertEqual(self.build().run_once(), (41, 42))

    def test_metadata_notice_enters_existing_exact_file_worker(self):
        class Reader:
            def read(self, file_id):
                raise ValueError('hold until exact proof')

        class Store:
            def upsert(self, proof):
                raise AssertionError('report mode must not write')

        pending = JsonSessionJournal(Path(self.temp.name) / 'pending.json')
        processor = CompatibilityLinkEvents(Reader(), Store(), pending)
        recheck = ScopedSeriesRecheck(
            self.shoko, processor, self.journal, batch_size=2)
        processor.attach_series_recheck(recheck)
        notice = json.dumps({
            'type': 1, 'target': 'metadata:links.changed',
            'arguments': [{'ShokoSeriesIDs': [410]}]}) + '\x1e'
        processor.ingest(notice)
        self.assertEqual(recheck.run_once(), (41, 42))
        self.assertEqual(processor.pending_ids(), (41, 42))

    def test_owned_series_audit_recovers_changes_without_a_file_event(self):
        class Store:
            def owned_series_ids_after(self, last, limit):
                return tuple(i for i in (410, 420, 430) if i > last)[:limit]

        class Recheck:
            def __init__(self):
                self.calls = []

            def enqueue_series_ids(self, ids):
                self.calls.append(ids)

        recheck = Recheck()
        cursor = JsonSessionJournal(Path(self.temp.name) / 'audit.json')
        clock = [1000]

        def audit():
            return OwnedSeriesAudit(Store(), recheck, cursor,
                                    batch_size=2, interval_seconds=60,
                                    clock=lambda: clock[0])

        self.assertEqual(audit().run_once(), (410, 420))
        self.assertEqual(audit().run_once(), (430,))
        self.assertEqual(audit().run_once(), (410, 420))
        self.assertEqual(recheck.calls, [(410, 420), (430,), (410, 420)])


if __name__ == '__main__':
    unittest.main()
