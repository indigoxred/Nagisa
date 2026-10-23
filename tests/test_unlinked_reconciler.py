import tempfile
import unittest
from pathlib import Path

from nagisa.reconciler.models import ImportHistoryEvent
from nagisa.reconciler.service import ReconcilerService, Trigger
from nagisa.reconciler.sonarr import SonarrClient
from nagisa.audit import AuditLog
from nagisa.state import StateStore


def event(history_id, download_id=None, episode_id=3):
    return ImportHistoryEvent(history_id, 2, episode_id, history_id + 100,
                              download_id, f'/Anime/{history_id}.mkv',
                              f'/Import/{history_id}.mkv', 100)


class UnlinkedReconcilerTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / '.test-tmp'
        scratch.mkdir(exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'state.db'
        self.store = StateStore(self.path)
        self.addCleanup(self.store.close)
        self.state = self.store.reconciler

    def test_hold_and_cursor_commit_together_and_later_upgrade_is_pending(self):
        first = event(8431)
        linked = event(8461, 'a' * 40)
        self.assertEqual(self.state.ingest_import_window(
            [(first, True), (linked, True)], recorded_at='2026-10-22T00:00:00Z'), 8461)
        rows = self.store.connection.execute(
            'SELECT history_id,status FROM reconciler_events ORDER BY history_id').fetchall()
        self.assertEqual([(row['history_id'], row['status']) for row in rows],
                         [(8431, 'held-unlinked-source'), (8461, 'pending')])
        self.assertEqual(self.state.watermark(), 8461)
        self.assertEqual([row['history_id'] for row in self.state.pending_events_by_series()[2]],
                         [8461])
        self.state.update_series(2, 'completed', None, updated_at='2026-10-22T00:01:00Z')
        status = self.store.connection.execute(
            'SELECT status FROM reconciler_events WHERE history_id=8431').fetchone()[0]
        self.assertEqual(status, 'held-unlinked-source')
        self.store.close()
        reopened=StateStore(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.reconciler.watermark(),8461)
        self.assertEqual(reopened.connection.execute(
            'SELECT status FROM reconciler_events WHERE history_id=8431').fetchone()[0],
            'held-unlinked-source')

    def test_replay_conflict_and_failed_batch_leave_cursor_unchanged(self):
        first = event(8431)
        self.state.ingest_import_window([(first, True)], recorded_at='2026-10-22T00:00:00Z')
        with self.assertRaises(ValueError):
            self.state.ingest_import_window(
                [(event(8431, episode_id=999), True)], recorded_at='2026-10-22T00:01:00Z')
        self.assertEqual(self.state.watermark(), 8431)
        with self.assertRaises(ValueError):
            self.state.ingest_import_window(
                [(event(8432), True), (event(8431, episode_id=999), True)],
                recorded_at='2026-10-22T00:01:00Z')
        self.assertEqual(self.state.watermark(), 8431)
        self.assertIsNone(self.store.connection.execute(
            'SELECT 1 FROM reconciler_events WHERE history_id=8432').fetchone())
        self.assertEqual(self.store.quick_check(), 'ok')

    def test_outside_root_is_journaled_but_not_processed(self):
        self.state.ingest_import_window(
            [(event(8431), False)], recorded_at='2026-10-22T00:00:00Z')
        self.assertEqual(self.state.watermark(), 8431)
        self.assertEqual(self.state.pending_events_by_series(), {})
        self.assertEqual(self.store.connection.execute(
            'SELECT status FROM reconciler_events WHERE history_id=8431').fetchone()[0],
            'skipped-outside-root')
        with self.assertRaises(ValueError):
            self.state.ingest_import_window(
                [(event(8431), True)], recorded_at='2026-10-22T00:01:00Z')
        self.assertEqual(self.state.watermark(),8431)

    def test_service_records_thirty_unlinked_imports_without_observing_source(self):
        class Transport:
            def request_json(_self, method, path):
                if path == '/api/v3/system/status':
                    return {'version': 'test'}
                if path == '/api/v3/series/2':
                    return {'id': 2, 'title': 'Fixture show'}
                if path == '/api/v3/episode?seriesId=2&includeImages=false':
                    return []
                if path.startswith('/api/v3/history?'):
                    rows = [{
                        'id': 8431+i, 'seriesId': 2, 'episodeId': 300+i,
                        'eventType': 'downloadFolderImported',
                        'data': {'fileId': 900+i%13,
                                 'importedPath': f'/Anime/{i%13:02}.mkv',
                                 'droppedPath': f'/Import/{i%13:02}.mkv', 'size': 100},
                    } for i in range(30)]
                    return {'totalRecords': 30,
                            'records': sorted(rows, key=lambda row: row['id'], reverse=True)}
                raise AssertionError(path)
        class Observer:
            def observe_series(_self, *_args):
                raise AssertionError('held rows must not be observed')
        self.state.set_watermark(8430)
        service = ReconcilerService(
            self.state, SonarrClient(Transport(), allowed_versions=['test'],
                                     import_root='/Anime'), Observer(), mode='report',
            audit=AuditLog(self.store))
        service.run_once(Trigger.SCHEDULED)
        self.assertEqual(self.state.watermark(), 8460)
        self.assertEqual(self.state.pending_events_by_series(), {})
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM reconciler_events WHERE status='held-unlinked-source'").fetchone()[0], 30)
        self.assertEqual(service.status_snapshot()['held_unlinked_imports'], 30)
        cycles = [row for row in AuditLog(self.store).rows() if row['event_type'] == 'cycle']
        self.assertEqual(cycles[0]['evidence']['held_unlinked_imports'], 30)
