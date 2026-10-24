"""Private bounded Shoko import-list reader; no listener or status writes."""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from nagisa.seerr_journal import JsonSessionJournal
from nagisa.shoko_file_signal import ShokoFileSignalProcessor
from nagisa.shoko_recent_catchup import (
    ShokoRecentCatchup, ShokoRecentCatchupWorker)
from tests.test_shoko_file_signal import Catalogue, Shoko as FileShoko


def entry(file_id, hour):
    return {'ID': file_id, 'Imported': f'2026-10-13T{hour:02d}:00:00Z'}


class Shoko:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def request_json(self, method, path):
        self.calls.append((method, path))
        page = int(path.split('page=')[1].split('&')[0])
        return self.pages[page]


class RecentCatchupTests(unittest.TestCase):
    def test_periodic_head_check_skips_full_scan_until_new_import(self):
        class CountingShoko:
            def __init__(self):
                self.rows = [entry(10, 10)]
                self.paths = []

            def request_json(self, method, path):
                self.paths.append(path)
                size = int(path.split('pageSize=')[1].split('&')[0])
                page = int(path.split('page=')[1].split('&')[0])
                start = (page - 1) * size
                return {'Total': len(self.rows),
                        'List': self.rows[start:start + size]}

        shoko = CountingShoko()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor = JsonSessionJournal(root / 'cursor.json')
            backlog = JsonSessionJournal(root / 'backlog.json')
            processor = ShokoFileSignalProcessor(
                FileShoko([]), Catalogue(),
                journal=JsonSessionJournal(root / 'pending.json'))
            worker = ShokoRecentCatchupWorker(
                ShokoRecentCatchup(shoko), processor, cursor,
                backlog_journal=backlog)
            worker.bootstrap_before_start()
            shoko.paths.clear()
            idle = worker.run_if_head_changed()
            self.assertTrue(idle.complete)
            self.assertEqual(idle.file_ids, ())
            self.assertEqual(len(shoko.paths), 2)
            self.assertTrue(all('pageSize=1' in path for path in shoko.paths))
            shoko.rows.insert(0, entry(11, 11))
            changed = worker.run_if_head_changed()
            self.assertEqual(changed.file_ids, (11,))
            self.assertEqual(backlog.load()['pending_file_ids'], [11])
            self.assertEqual(cursor.load()['file_id'], 11)
            shoko.rows = [entry(9, 9)]
            with self.assertRaisesRegex(ValueError, 'older than saved'):
                worker.run_if_head_changed()

    def test_bootstrap_uses_stable_exact_head_then_catches_next_import(self):
        class ChangingShoko:
            def __init__(self):
                self.rows = [entry(10, 10)]

            def request_json(self, method, path):
                page = int(path.split('page=')[1].split('&')[0])
                return {'Total': len(self.rows),
                        'List': self.rows[page - 1:page]}

        shoko = ChangingShoko()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor = JsonSessionJournal(root / 'cursor.json')
            pending = JsonSessionJournal(root / 'pending.json')
            backlog = JsonSessionJournal(root / 'backlog.json')
            processor = ShokoFileSignalProcessor(
                FileShoko([]), Catalogue(), journal=pending)
            reader = ShokoRecentCatchup(shoko, page_size=1)
            worker = ShokoRecentCatchupWorker(
                reader, processor, cursor, backlog_journal=backlog)

            self.assertEqual(worker.bootstrap_before_start(),
                             (datetime(2026, 10, 13, 10,
                                       tzinfo=timezone.utc), 10))
            shoko.rows.insert(0, entry(11, 11))
            result = worker.run_once()
            self.assertEqual(result.file_ids, (11,))
            self.assertEqual(backlog.load()['pending_file_ids'], [11])
            self.assertEqual(cursor.load()['file_id'], 11)

    def test_bootstrap_refuses_existing_cursor_or_pending_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor = JsonSessionJournal(root / 'cursor.json')
            pending = JsonSessionJournal(root / 'pending.json')
            backlog = JsonSessionJournal(root / 'backlog.json')
            processor = ShokoFileSignalProcessor(
                FileShoko([]), Catalogue(), journal=pending)
            reader = ShokoRecentCatchup(Shoko({1: {
                'Total': 1, 'List': [entry(10, 10)]}}), page_size=1)
            worker = ShokoRecentCatchupWorker(
                reader, processor, cursor, backlog_journal=backlog)
            cursor.save({'version': 1,
                'imported_utc': '2026-10-13T10:00:00+00:00', 'file_id': 10})
            with self.assertRaisesRegex(ValueError, 'existing'):
                worker.bootstrap_before_start()
            cursor.path.unlink()
            backlog.save({'version': 1, 'pending_file_ids': [9]})
            restarted = ShokoRecentCatchupWorker(
                reader, processor, cursor, backlog_journal=backlog)
            with self.assertRaisesRegex(ValueError, 'pending'):
                restarted.bootstrap_before_start()

    def test_bootstrap_refuses_changing_head(self):
        class ShiftingShoko:
            def __init__(self):
                self.calls = 0

            def request_json(self, method, path):
                self.calls += 1
                return {'Total': 1, 'List': [entry(self.calls + 9, 10)]}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor = JsonSessionJournal(root / 'cursor.json')
            worker = ShokoRecentCatchupWorker(
                ShokoRecentCatchup(ShiftingShoko(), page_size=1),
                ShokoFileSignalProcessor(FileShoko([]), Catalogue(),
                    journal=JsonSessionJournal(root / 'pending.json')),
                cursor, backlog_journal=JsonSessionJournal(root / 'backlog.json'))
            with self.assertRaisesRegex(ValueError, 'head changed'):
                worker.bootstrap_before_start()
            self.assertEqual(cursor.load(), {})

    def test_501_file_gap_is_durable_beyond_live_queue_capacity(self):
        rows = [{'ID': file_id, 'Imported': '2026-10-13T12:00:00Z'}
                for file_id in range(502, 0, -1)]
        shoko = Shoko({page: {'Total': len(rows),
            'List': rows[(page - 1) * 256:page * 256]}
            for page in range(1, 3)})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor = JsonSessionJournal(root / 'cursor.json')
            pending = JsonSessionJournal(root / 'pending.json')
            backlog = JsonSessionJournal(root / 'backlog.json')
            cursor.save({'version': 1,
                'imported_utc': '2026-10-13T12:00:00+00:00', 'file_id': 1})
            processor = ShokoFileSignalProcessor(
                FileShoko([]), Catalogue(), journal=pending, max_pending=4)
            reader = ShokoRecentCatchup(shoko, page_size=256, max_pages=3)
            worker = ShokoRecentCatchupWorker(
                reader, processor, cursor, backlog_journal=backlog)

            result = worker.run_once()
            self.assertTrue(result.complete)
            self.assertEqual(result.file_ids, tuple(range(2, 503)))
            self.assertEqual(cursor.load()['file_id'], 502)
            self.assertEqual(processor.pending_ids(), ())
            self.assertEqual(backlog.load()['pending_file_ids'],
                             list(range(2, 503)))

    def test_backlog_save_failure_never_advances_import_cursor(self):
        class FailingBacklog:
            def load(self):
                return {}

            def save(self, value):
                raise OSError('backlog unavailable')

        with tempfile.TemporaryDirectory() as directory:
            cursor = JsonSessionJournal(Path(directory) / 'cursor.json')
            cursor.save({'version': 1,
                'imported_utc': '2026-10-13T10:00:00+00:00', 'file_id': 10})
            processor = ShokoFileSignalProcessor(
                FileShoko([]), Catalogue(),
                journal=JsonSessionJournal(Path(directory) / 'pending.json'))
            reader = ShokoRecentCatchup(Shoko({1: {'Total': 2, 'List': [
                entry(11, 11), entry(10, 10)]}}), page_size=2)
            with self.assertRaisesRegex(OSError, 'backlog unavailable'):
                ShokoRecentCatchupWorker(reader, processor, cursor,
                    backlog_journal=FailingBacklog()).run_once()
            self.assertEqual(cursor.load()['file_id'], 10)
            self.assertEqual(processor.pending_ids(), ())

    def test_cursor_save_failure_repeats_without_duplicating_backlog(self):
        class FailingCursor:
            def load(self):
                return {'version': 1,
                    'imported_utc': '2026-10-13T10:00:00+00:00', 'file_id': 10}

            def save(self, value):
                raise OSError('cursor unavailable')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backlog = JsonSessionJournal(root / 'backlog.json')
            pending = JsonSessionJournal(root / 'pending.json')
            processor = ShokoFileSignalProcessor(
                FileShoko([]), Catalogue(), journal=pending)
            reader = ShokoRecentCatchup(Shoko({1: {'Total': 2, 'List': [
                entry(11, 11), entry(10, 10)]}}), page_size=2)
            with self.assertRaisesRegex(OSError, 'cursor unavailable'):
                ShokoRecentCatchupWorker(reader, processor, FailingCursor(),
                    backlog_journal=backlog).run_once()
            self.assertEqual(backlog.load()['pending_file_ids'], [11])
            cursor = JsonSessionJournal(root / 'cursor.json')
            cursor.save(FailingCursor().load())
            restarted = ShokoRecentCatchupWorker(reader, processor, cursor,
                                                 backlog_journal=backlog)
            self.assertTrue(restarted.run_once().complete)
            self.assertEqual(backlog.load()['pending_file_ids'], [11])
            self.assertEqual(cursor.load()['file_id'], 11)

    def test_incomplete_scan_does_not_spool_partial_ids_or_advance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor = JsonSessionJournal(root / 'cursor.json')
            backlog = JsonSessionJournal(root / 'backlog.json')
            cursor.save({'version': 1,
                'imported_utc': '2026-10-13T10:00:00+00:00', 'file_id': 10})
            processor = ShokoFileSignalProcessor(
                FileShoko([]), Catalogue(),
                journal=JsonSessionJournal(root / 'pending.json'))
            reader = ShokoRecentCatchup(Shoko({1: {'Total': 4, 'List': [
                entry(14, 14), entry(13, 13)]}}), page_size=2, max_pages=1)
            result = ShokoRecentCatchupWorker(reader, processor, cursor,
                backlog_journal=backlog).run_once()
            self.assertFalse(result.complete)
            self.assertEqual(cursor.load()['file_id'], 10)
            self.assertEqual(backlog.load(), {})
            self.assertEqual(processor.pending_ids(), ())

    def test_invalid_backlog_refuses_startup(self):
        class BadBacklog:
            def load(self):
                return {'version': 1, 'pending_file_ids': [7, 7]}

            def save(self, value):
                raise AssertionError('must not save')

        class Reader:
            def scan_since(self, cursor):
                raise AssertionError('must not scan')

        class Processor:
            def enqueue_exact_file_ids(self, ids):
                raise AssertionError('must not enqueue')

        class Cursor:
            def load(self):
                raise AssertionError('must not load')

            def save(self, value):
                raise AssertionError('must not save')

        with self.assertRaisesRegex(ValueError, 'backlog'):
            ShokoRecentCatchupWorker(Reader(), Processor(), Cursor(),
                                     backlog_journal=BadBacklog())

    def test_drain_save_failure_retries_ids_without_losing_backlog(self):
        class Backlog:
            def __init__(self):
                self.value = {'version': 1, 'pending_file_ids': [2, 3, 4, 5, 6]}
                self.fail = True

            def load(self):
                return self.value

            def save(self, value):
                if self.fail:
                    raise OSError('trim failed')
                self.value = value

        class Processor:
            max_pending = 4

            def __init__(self):
                self.ids = []

            def pending_ids(self):
                return tuple(self.ids)

            def enqueue_exact_file_ids(self, ids):
                for file_id in ids:
                    if file_id not in self.ids:
                        self.ids.append(file_id)

        class Reader:
            def scan_since(self, cursor):
                raise AssertionError('must not scan')

        class Cursor:
            def load(self):
                raise AssertionError('must not load')

            def save(self, value):
                raise AssertionError('must not save')

        backlog = Backlog()
        processor = Processor()
        worker = ShokoRecentCatchupWorker(Reader(), processor, Cursor(),
                                          backlog_journal=backlog)
        with self.assertRaisesRegex(OSError, 'trim failed'):
            worker.drain_once()
        self.assertEqual(processor.pending_ids(), (2, 3))
        self.assertEqual(backlog.load()['pending_file_ids'], [2, 3, 4, 5, 6])
        backlog.fail = False
        self.assertEqual(worker.drain_once(), 2)
        self.assertEqual(backlog.load()['pending_file_ids'], [4, 5, 6])

    def test_held_queue_reserves_live_capacity_and_keeps_backlog(self):
        class Processor:
            max_pending = 4

            def pending_ids(self):
                return (9, 10, 11)

            def enqueue_exact_file_ids(self, ids):
                raise AssertionError('queue reserve exhausted')

        class Journal:
            def load(self):
                return {'version': 1, 'pending_file_ids': [2, 3]}

            def save(self, value):
                raise AssertionError('backlog must remain')

        class Reader:
            def scan_since(self, cursor):
                raise AssertionError('must not scan')

        class Cursor:
            def load(self):
                raise AssertionError('must not load')

            def save(self, value):
                raise AssertionError('must not save')

        worker = ShokoRecentCatchupWorker(Reader(), Processor(), Cursor(),
                                          backlog_journal=Journal())
        self.assertEqual(worker.drain_once(), 0)
        self.assertEqual(worker.backlog_count(), 2)

    def test_only_new_exact_file_ids_are_returned_oldest_first(self):
        shoko = Shoko({1: {'Total': 3, 'List': [
            entry(12, 12), entry(11, 11), entry(10, 10)]}})
        cursor = (datetime(2026, 10, 13, 10, tzinfo=timezone.utc), 10)
        result = ShokoRecentCatchup(shoko, page_size=3).scan_since(cursor)

        self.assertTrue(result.complete)
        self.assertEqual(result.file_ids, (11, 12))
        self.assertEqual(result.watermark,
                         (datetime(2026, 10, 13, 12,
                                   tzinfo=timezone.utc), 12))
        self.assertEqual(shoko.calls, [('GET',
            '/api/v3/File?pageSize=3&page=1&include=ImportLimbo&sortOrder=-ImportedAt,-FileID')])

    def test_catchup_crosses_pages_before_saved_watermark(self):
        shoko = Shoko({
            1: {'Total': 4, 'List': [entry(14, 14), entry(13, 13)]},
            2: {'Total': 4, 'List': [entry(12, 12), entry(11, 11)]},
        })
        cursor = (datetime(2026, 10, 13, 11, tzinfo=timezone.utc), 11)
        result = ShokoRecentCatchup(shoko, page_size=2).scan_since(cursor)
        self.assertTrue(result.complete)
        self.assertEqual(result.file_ids, (12, 13, 14))
        self.assertEqual(len(shoko.calls), 3)
        self.assertIn('page=1&', shoko.calls[-1][1])

    def test_page_limit_reports_incomplete_without_advancing_watermark(self):
        shoko = Shoko({1: {'Total': 4, 'List': [
            entry(14, 14), entry(13, 13)]}})
        cursor = (datetime(2026, 10, 13, 10, tzinfo=timezone.utc), 10)
        result = ShokoRecentCatchup(
            shoko, page_size=2, max_pages=1).scan_since(cursor)
        self.assertFalse(result.complete)
        self.assertEqual(result.file_ids, (13, 14))
        self.assertIsNone(result.watermark)

    def test_out_of_order_or_malformed_file_list_fails_closed(self):
        cursor = (datetime(2026, 10, 13, 10, tzinfo=timezone.utc), 10)
        bad_pages = [
            {'Total': 2, 'List': [entry(11, 11), entry(12, 12)]},
            {'Total': 1, 'List': [{'ID': 12,
                                  'Imported': '2026-10-13T12:00:00'}]},
            {'Total': 2, 'List': [entry(12, 12), entry(12, 12)]},
        ]
        for page in bad_pages:
            with self.subTest(page=page):
                with self.assertRaises(ValueError):
                    ShokoRecentCatchup(Shoko({1: page}),
                                       page_size=2).scan_since(cursor)

    def test_recent_ids_are_durable_before_watermark_advances(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor_journal = JsonSessionJournal(root / 'cursor.json')
            pending_journal = JsonSessionJournal(root / 'pending.json')
            cursor_journal.save({'version': 1,
                'imported_utc': '2026-10-13T10:00:00+00:00', 'file_id': 10})
            processor = ShokoFileSignalProcessor(
                FileShoko([]), Catalogue(), journal=pending_journal)
            reader = ShokoRecentCatchup(Shoko({1: {'Total': 3, 'List': [
                entry(12, 12), entry(11, 11), entry(10, 10)]}}), page_size=3)
            result = ShokoRecentCatchupWorker(
                reader, processor, cursor_journal).run_once()
            self.assertTrue(result.complete)
            self.assertEqual(cursor_journal.load()['file_id'], 12)
            self.assertEqual(ShokoFileSignalProcessor(
                FileShoko([]), Catalogue(), journal=pending_journal
                ).pending_ids(), (11, 12))

    def test_queue_failure_never_advances_recent_import_watermark(self):
        class FailingQueue:
            def enqueue_exact_file_ids(self, ids):
                raise OSError('pending journal unavailable')

        with tempfile.TemporaryDirectory() as directory:
            cursor_journal = JsonSessionJournal(Path(directory) / 'cursor.json')
            cursor_journal.save({'version': 1,
                'imported_utc': '2026-10-13T10:00:00+00:00', 'file_id': 10})
            reader = ShokoRecentCatchup(Shoko({1: {'Total': 2, 'List': [
                entry(11, 11), entry(10, 10)]}}), page_size=2)
            with self.assertRaises(OSError):
                ShokoRecentCatchupWorker(
                    reader, FailingQueue(), cursor_journal).run_once()
            self.assertEqual(cursor_journal.load()['file_id'], 10)

    def test_page_overflow_leaves_watermark_for_visible_catchup_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor_journal = JsonSessionJournal(root / 'cursor.json')
            pending_journal = JsonSessionJournal(root / 'pending.json')
            cursor_journal.save({'version': 1,
                'imported_utc': '2026-10-13T10:00:00+00:00', 'file_id': 10})
            processor = ShokoFileSignalProcessor(
                FileShoko([]), Catalogue(), journal=pending_journal)
            reader = ShokoRecentCatchup(Shoko({1: {'Total': 4, 'List': [
                entry(14, 14), entry(13, 13)]}}), page_size=2, max_pages=1)
            result = ShokoRecentCatchupWorker(
                reader, processor, cursor_journal).run_once()
            self.assertFalse(result.complete)
            self.assertEqual(cursor_journal.load()['file_id'], 10)
            self.assertEqual(processor.pending_ids(), (13, 14))

    def test_failed_cursor_save_keeps_ids_queued_for_safe_repeat(self):
        class FailingCursorJournal:
            def load(self):
                return {'version': 1,
                    'imported_utc': '2026-10-13T10:00:00+00:00',
                    'file_id': 10}

            def save(self, value):
                raise OSError('cursor journal unavailable')

        with tempfile.TemporaryDirectory() as directory:
            processor = ShokoFileSignalProcessor(
                FileShoko([]), Catalogue(),
                journal=JsonSessionJournal(Path(directory) / 'pending.json'))
            reader = ShokoRecentCatchup(Shoko({1: {'Total': 2, 'List': [
                entry(11, 11), entry(10, 10)]}}), page_size=2)
            with self.assertRaises(OSError):
                ShokoRecentCatchupWorker(
                    reader, processor, FailingCursorJournal()).run_once()
            self.assertEqual(processor.pending_ids(), (11,))

    def test_changed_page_total_fails_before_returning_candidates(self):
        shoko = Shoko({
            1: {'Total': 4, 'List': [entry(14, 14), entry(13, 13)]},
            2: {'Total': 5, 'List': [entry(12, 12), entry(11, 11)]},
        })
        cursor = (datetime(2026, 10, 13, 10, tzinfo=timezone.utc), 10)
        with self.assertRaisesRegex(ValueError, 'changed'):
            ShokoRecentCatchup(shoko, page_size=2).scan_since(cursor)

    def test_multi_page_scan_rejects_equal_total_head_replacement(self):
        class ReplacedHead:
            def __init__(self):
                self.calls = 0

            def request_json(self, method, path):
                self.calls += 1
                if self.calls == 1:
                    return {'Total': 4, 'List': [entry(14, 14), entry(13, 13)]}
                if self.calls == 2:
                    return {'Total': 4, 'List': [entry(12, 12), entry(11, 11)]}
                # A new import and a deletion can keep Total unchanged.
                return {'Total': 4, 'List': [entry(15, 15), entry(14, 14)]}

        shoko = ReplacedHead()
        cursor = (datetime(2026, 10, 13, 11, tzinfo=timezone.utc), 11)
        with self.assertRaisesRegex(ValueError, 'changed'):
            ShokoRecentCatchup(shoko, page_size=2).scan_since(cursor)
        self.assertEqual(shoko.calls, 3)


if __name__ == '__main__':
    unittest.main()
