"""Restartable bounded processing of an initial Shoko ID census."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from nagisa.seerr_compat_export import CompatibilityInitialExport
from nagisa.seerr_compat_links import NotRegularTVFile
from nagisa.seerr_journal import JsonSessionJournal


class Reader:
    def __init__(self):
        self.hold = set()
        self.excluded = set()
        self.calls = []
        self.proofs = {}

    def read(self, file_id):
        self.calls.append(file_id)
        if file_id in self.hold:
            raise ValueError('identity not ready')
        if file_id in self.excluded:
            raise NotRegularTVFile('known movie')
        return self.proofs.get(file_id, SimpleNamespace(
            shoko_file_id=file_id, shoko_episode_id=file_id,
            tvdb_show_id=370761, season=1, episode=file_id))


class Store:
    def __init__(self):
        self.calls = []

    def upsert(self, proof):
        self.calls.append(proof)
        return 'created'

    def lookup_file(self, file_id):
        return None


class CountingJournal:
    def __init__(self, inner):
        self.inner = inner
        self.path = inner.path
        self.saves = 0

    def load(self):
        return self.inner.load()

    def save(self, value):
        self.saves += 1
        self.inner.save(value)


class CompatibilityExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.journal = CountingJournal(JsonSessionJournal(
            Path(self.temp.name) / 'export.json'))
        self.reader = Reader()
        self.store = Store()
        self.scans = []

    def exporter(self):
        return CompatibilityInitialExport(self.reader, self.store,
                                          self.journal, refresh=lambda:
                                          self.scans.append('refresh'),
                                          write_enabled=True)

    def test_interrupted_export_resumes_without_repeating_completed_ids(self):
        exporter = self.exporter()
        exporter.begin((11, 12, 13))
        self.assertEqual(exporter.run(limit=2), [(11, 'created'),
                                                  (12, 'created')])
        self.assertEqual(self.journal.saves, 2)  # begin, batch; no partial scan
        self.assertEqual(self.scans, [])
        self.assertTrue(exporter.status()['refresh_pending'])
        restarted = self.exporter()
        self.assertEqual(restarted.run(limit=2), [(13, 'created')])
        self.assertEqual(self.reader.calls, [11, 12, 13, 11, 12, 13])
        self.assertEqual(self.scans, ['refresh'])
        self.assertTrue(restarted.status()['complete'])

    def test_quarantined_file_is_acknowledged_without_retry_on_export_resume(self):
        self.store.upsert = lambda proof: 'quarantined'
        exporter = self.exporter()
        exporter.begin((11,))
        self.assertEqual(exporter.run(limit=1), [(11, 'quarantined')])
        self.assertEqual(exporter.status()['held'], ())
        self.assertTrue(exporter.status()['complete'])
        self.assertEqual(self.exporter().run(limit=1), [])

    def test_resume_keeps_original_census_when_new_shoko_ids_arrive(self):
        class Census:
            def __init__(self):
                self.ids = (11, 12)
                self.calls = 0

            def read_ids(self):
                self.calls += 1
                return self.ids

        census = Census()
        exporter = self.exporter()
        self.assertEqual(exporter.begin_or_resume(census), 'started')
        self.assertEqual(exporter.run(limit=1), [(11, 'created')])
        census.ids = (11, 12, 13)
        restarted = self.exporter()
        self.assertEqual(restarted.begin_or_resume(census), 'resumed')
        self.assertEqual(census.calls, 1)
        self.assertEqual(restarted.run(limit=2), [(12, 'created')])
        self.assertTrue(restarted.status()['complete'])
        self.assertEqual([proof.shoko_file_id for proof in self.store.calls],
                         [11, 12])

    def test_existing_empty_journal_cannot_be_overwritten_as_new_census(self):
        self.journal.path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'not initialized'):
            self.exporter()
        self.assertEqual(self.journal.path.read_text(), '{}')

    def test_final_refresh_failure_recovers_without_rewriting_links(self):
        scans = []
        def refresh():
            scans.append('attempt')
            if len(scans) == 1:
                raise RuntimeError('Plex unavailable')
        exporter = CompatibilityInitialExport(self.reader, self.store,
                                              self.journal, refresh=refresh,
                                              write_enabled=True)
        exporter.begin((11, 12))
        exporter.run(limit=1)
        self.assertEqual(scans, [])
        with self.assertRaisesRegex(RuntimeError, 'Plex unavailable'):
            exporter.run(limit=1)
        self.assertEqual([p.shoko_file_id for p in self.store.calls], [11, 12])
        self.assertTrue(exporter.status()['refresh_pending'])
        restarted = CompatibilityInitialExport(self.reader, self.store,
                                               self.journal, refresh=refresh,
                                               write_enabled=True)
        self.assertEqual(restarted.run(limit=1), [])
        self.assertEqual(scans, ['attempt', 'attempt'])
        self.assertEqual([p.shoko_file_id for p in self.store.calls], [11, 12])
        self.assertTrue(restarted.status()['complete'])

    def test_unqualified_preflight_file_stays_held_until_new_census(self):
        self.reader.hold.add(12)
        exporter = self.exporter()
        exporter.begin((11, 12, 13))
        self.assertEqual(exporter.run(limit=3), [(11, 'created'),
                                                  (12, 'held'),
                                                  (13, 'created')])
        self.assertEqual(exporter.status()['held'], (12,))
        self.reader.hold.clear()
        self.assertEqual(exporter.retry_held(limit=1), [(12, 'held')])
        self.assertEqual([proof.shoko_file_id for proof in self.store.calls],
                         [11, 13])
        self.assertFalse(exporter.status()['complete'])

    def test_newly_resolved_file_cannot_collide_with_preflight_owner(self):
        self.reader.hold.add(12)
        exporter = self.exporter()
        exporter.begin((11, 12))
        self.assertEqual(exporter.run(limit=2),
                         [(11, 'created'), (12, 'held')])
        self.reader.hold.clear()
        self.reader.proofs[12] = SimpleNamespace(
            shoko_file_id=12, shoko_episode_id=999,
            tvdb_show_id=370761, season=1, episode=11)
        self.assertEqual(exporter.retry_held(limit=1), [(12, 'held')])
        self.assertEqual([proof.shoko_file_id for proof in self.store.calls],
                         [11])

    def test_known_non_tv_file_is_excluded_without_link(self):
        self.reader.excluded.add(12)
        exporter = self.exporter()
        exporter.begin((11, 12))
        self.assertEqual(exporter.run(limit=2), [(11, 'created'),
                                                  (12, 'excluded-non-tv')])
        self.assertEqual([proof.shoko_file_id for proof in self.store.calls], [11])
        self.assertTrue(exporter.status()['complete'])

    def test_distinct_episodes_claiming_one_slot_never_write_a_link(self):
        for file_id, episode_id in ((11, 2412), (12, 9871)):
            self.reader.proofs[file_id] = SimpleNamespace(
                shoko_file_id=file_id, shoko_episode_id=episode_id,
                tvdb_show_id=76703, season=19, episode=107)
        exporter = self.exporter()
        exporter.begin((11, 12, 13))
        self.assertEqual(self.store.calls, [])
        self.assertEqual(exporter.run(limit=3),
                         [(11, 'held'), (12, 'held'), (13, 'created')])
        self.assertEqual([proof.shoko_file_id for proof in self.store.calls], [13])

    def test_tmdb_export_preflight_uses_native_tmdb_show_not_tvdb_show(self):
        self.store.proof_show_id = lambda proof: proof.tmdb_show_id
        self.reader.proofs[11] = SimpleNamespace(
            shoko_file_id=11, shoko_episode_id=2412,
            tvdb_show_id=22001, tmdb_show_id=11001,
            season=1, episode=1)
        self.reader.proofs[12] = SimpleNamespace(
            shoko_file_id=12, shoko_episode_id=9871,
            tvdb_show_id=22001, tmdb_show_id=11002,
            season=1, episode=1)
        exporter = self.exporter()
        exporter.begin((11, 12))
        self.assertEqual(exporter.run(limit=2),
                         [(11, 'created'), (12, 'created')])

    def test_tmdb_refresh_is_bounded_and_waits_for_each_exact_show(self):
        self.store.proof_show_id = lambda proof: proof.tmdb_show_id
        for file_id, show_id in ((11, 11001), (12, 11002)):
            self.reader.proofs[file_id] = SimpleNamespace(
                shoko_file_id=file_id, shoko_episode_id=file_id,
                tvdb_show_id=22001, tmdb_show_id=show_id,
                season=1, episode=1)
        attempts = []

        def refresh_show(show_id):
            attempts.append(show_id)
            return attempts.count(show_id) > 1 or show_id == 11002

        def make():
            return CompatibilityInitialExport(
                self.reader, self.store, self.journal,
                refresh_show=refresh_show, write_enabled=True)

        exporter = make()
        exporter.begin((11, 12))
        self.assertEqual(exporter.run(limit=2),
                         [(11, 'created'), (12, 'created')])
        self.assertEqual(attempts, [11001])
        self.assertTrue(exporter.status()['refresh_pending'])
        self.assertEqual(make().run(limit=1), [])
        self.assertEqual(attempts, [11001, 11002])
        self.assertTrue(make().status()['refresh_pending'])
        self.assertEqual(make().run(limit=1), [])
        self.assertEqual(attempts, [11001, 11002, 11001])
        self.assertTrue(make().status()['complete'])
        self.assertEqual([proof.shoko_file_id for proof in self.store.calls],
                         [11, 12])

    def test_ambiguous_show_refresh_does_not_block_other_shows(self):
        self.store.proof_show_id = lambda proof: proof.tmdb_show_id
        for file_id, show_id in ((11, 11001), (12, 11002)):
            self.reader.proofs[file_id] = SimpleNamespace(
                shoko_file_id=file_id, shoko_episode_id=file_id,
                tvdb_show_id=22001, tmdb_show_id=show_id,
                season=1, episode=1)
        attempts = []

        def refresh_show(show_id):
            attempts.append(show_id)
            if show_id == 11001:
                raise ValueError('ambiguous Plex show identity')
            return True

        exporter = CompatibilityInitialExport(
            self.reader, self.store, self.journal,
            refresh_show=refresh_show, write_enabled=True)
        exporter.begin((11, 12))
        self.assertEqual(exporter.run(limit=2),
                         [(11, 'created'), (12, 'created')])
        self.assertTrue(exporter.status()['refresh_pending'])
        self.assertEqual(exporter.run(limit=1), [])
        self.assertEqual(attempts, [11001, 11002])
        self.assertTrue(exporter.status()['refresh_pending'])

    def test_same_episode_multiple_files_choose_stable_lowest_file_id(self):
        for file_id in (11, 12):
            self.reader.proofs[file_id] = SimpleNamespace(
                shoko_file_id=file_id, shoko_episode_id=2412,
                tvdb_show_id=76703, season=19, episode=107)
        exporter = self.exporter()
        exporter.begin((11, 12))
        self.assertEqual(exporter.run(limit=2),
                         [(11, 'created'), (12, 'duplicate-version')])
        self.assertEqual([proof.shoko_file_id for proof in self.store.calls], [11])

    def test_changed_episode_identity_after_preflight_is_held(self):
        exporter = self.exporter()
        exporter.begin((11,))
        self.reader.proofs[11] = SimpleNamespace(
            shoko_file_id=11, shoko_episode_id=999,
            tvdb_show_id=370761, season=1, episode=11)
        self.assertEqual(exporter.run(limit=1), [(11, 'held')])
        self.assertEqual(self.store.calls, [])

    def test_preexisting_exporter_loads_newly_saved_collision_preflight(self):
        for file_id, episode_id in ((11, 2412), (12, 9871)):
            self.reader.proofs[file_id] = SimpleNamespace(
                shoko_file_id=file_id, shoko_episode_id=episode_id,
                tvdb_show_id=76703, season=19, episode=107)
        second = self.exporter()
        first = self.exporter()
        first.begin((11, 12))
        second.begin((11, 12))
        self.assertEqual(second.run(limit=2), [(11, 'held'), (12, 'held')])
        self.assertEqual(self.store.calls, [])


if __name__ == '__main__':
    unittest.main()
