"""Linux symlink tests for the private, isolated compatibility view."""

import os
import sqlite3
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nagisa.seerr_compat_events import CompatibilityLinkEvents
from nagisa.seerr_compat_links import LinkProof, NotRegularTVFile
from nagisa.seerr_compat_store import CompatibilityLinkStore
from nagisa.seerr_journal import JsonSessionJournal


class CompatibilityPlexHintTests(unittest.TestCase):
    def test_show_hint_uses_supported_tvdbid_directive(self):
        self.assertEqual(CompatibilityLinkStore.plexmatch_text(370761),
                         'tvdbid: 370761\n')

    def test_show_hint_rejects_invalid_id(self):
        with self.assertRaises(ValueError):
            CompatibilityLinkStore.plexmatch_text(0)

    def test_tmdb_store_has_separate_ledger_folder_and_stock_hint(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            from unittest.mock import patch
            with patch.object(CompatibilityLinkStore, '_recover_contests'):
                tvdb = CompatibilityLinkStore(root)
                tmdb = CompatibilityLinkStore(root, identity_provider='tmdb')
            self.assertNotEqual(tvdb.path, tmdb.path)
            self.assertEqual(tmdb._link_for('94404/2/3', '.mkv'),
                             root / 'tmdb-94404' / 'Season 02' / 'S02E03.mkv')
            self.assertEqual(tmdb.plexmatch_text(94404, provider='tmdb'),
                             'tmdbid: 94404\n')
            self.assertEqual(tvdb.manifest()['links'], {})
            self.assertEqual(tmdb.manifest()['links'], {})

    def test_owned_mapping_pages_include_contested_claimants(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            with patch.object(CompatibilityLinkStore, '_recover_contests'):
                store = CompatibilityLinkStore(Path(directory),
                                               identity_provider='tmdb')
            db = sqlite3.connect(store.path)
            try:
                db.execute('''INSERT INTO links VALUES
                    ('94404/1/1',11,1,1,1,NULL,94404,'.mkv',
                     'tmdb-94404/Season 01/S01E01.mkv','/source',
                     'ready',NULL)''')
                db.execute('''INSERT INTO contested_claims VALUES
                    ('94404/1/2',12,1,2)''')
                db.commit()
            finally:
                db.close()
            self.assertEqual(store.owned_file_ids_after(0, 1), (11,))
            self.assertEqual(store.owned_file_ids_after(11, 2), (12,))
            self.assertEqual(store.owned_series_ids_after(0, 2), (1,))

    def test_mapping_update_reads_owned_and_contested_ids_for_exact_shows(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            store = object.__new__(CompatibilityLinkStore)
            store.root = Path(directory)
            store.path = store.root / 'ledger.sqlite'
            db = sqlite3.connect(store.path)
            try:
                db.execute('CREATE TABLE links (slot TEXT, file_id INTEGER)')
                db.execute('CREATE TABLE contested_claims (slot TEXT, file_id INTEGER)')
                db.executemany('INSERT INTO links VALUES (?,?)',
                               [('100/1/1', 41), ('1000/1/1', 99),
                                ('200/2/1', 42)])
                db.executemany('INSERT INTO contested_claims VALUES (?,?)',
                               [('100/1/2', 43), ('300/1/1', 77)])
                db.commit()
            finally:
                db.close()
            self.assertEqual(store.owned_file_ids_for_shows((100, 200)),
                             (41, 42, 43))
            with self.assertRaises(ValueError):
                store.owned_file_ids_for_shows((100, 100))


@unittest.skipIf(os.name == 'nt', 'Plex compatibility links require Linux symlinks')
class CompatibilityLinkStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'view'
        self.root.mkdir()
        self.source = self.base / 'original.mkv'
        self.source.write_bytes(b'original')
        self.store = CompatibilityLinkStore(self.root)

    def proof(self, *, file_id=42, plex_target=None):
        return LinkProof(file_id, 99, 1051, 225738, 1948394, 7398592, 94404,
                         370761, 1, 5, self.source,
                         plex_target or str(self.source), 8)

    def test_new_link_directories_and_hint_are_plex_readable_with_private_umask(self):
        link = self.store._link_for('370761/1/5', '.mkv')
        previous = os.umask(0o077)
        try:
            self.store._prepare_directory(link, 370761)
        finally:
            os.umask(previous)
        show = self.root / 'tvdb-370761'
        self.assertEqual(stat.S_IMODE(show.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(link.parent.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((show / '.plexmatch').stat().st_mode),
                         0o644)

    def test_ledger_and_lock_stay_private_with_permissive_umask(self):
        root = self.base / 'open-umask-view'
        root.mkdir()
        previous = os.umask(0)
        try:
            store = CompatibilityLinkStore(root)
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(store.lock_path.stat().st_mode),
                         0o600)

    def test_tmdb_mixed_card_quarantines_exact_owned_union_and_future_event(self):
        store = CompatibilityLinkStore(self.root, identity_provider='tmdb')
        first = self.proof()
        second = replace(first, shoko_file_id=43, tmdb_show_id=95555,
                         tmdb_episode_id=1948395)
        self.assertEqual(store.upsert(first), 'created')
        self.assertEqual(store.upsert(second), 'created')
        links = frozenset(store.show_links(94404) | store.show_links(95555))

        self.assertEqual(store.quarantine_card((94404, 95555), links),
                         (42, 43))
        self.assertTrue(store.quarantined_show_ids((94404, 95555)))
        self.assertEqual(store.show_links(94404), frozenset())
        self.assertEqual(store.show_links(95555), frozenset())
        self.assertFalse((self.root / 'tmdb-94404' / 'Season 01' /
                          'S01E05.mkv').is_symlink())
        self.assertFalse((self.root / 'tmdb-95555' / 'Season 01' /
                          'S01E05.mkv').is_symlink())
        self.assertEqual(store.upsert(first), 'quarantined')
        self.assertEqual(store.upsert(second), 'quarantined')
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_tmdb_quarantine_refuses_incomplete_union_and_contested_claim(self):
        store = CompatibilityLinkStore(self.root, identity_provider='tmdb')
        first = self.proof()
        second = replace(first, shoko_file_id=43, tmdb_show_id=95555,
                         tmdb_episode_id=1948395)
        store.upsert(first)
        store.upsert(second)
        links = frozenset(store.show_links(94404) | store.show_links(95555))
        with self.assertRaisesRegex(ValueError, 'differ'):
            store.quarantine_card((94404, 95555),
                                  frozenset(store.show_links(94404)))
        self.assertFalse(store.quarantined_show_ids((94404, 95555)))
        self.assertEqual(frozenset(store.show_links(94404) |
                                       store.show_links(95555)), links)
        with sqlite3.connect(store.path) as db:
            db.execute('INSERT INTO contested_claims VALUES (?,?,?,?)',
                       ('95555/1/2', 44, 1051, 101))
        with self.assertRaisesRegex(ValueError, 'contested'):
            store.quarantine_card((94404, 95555), links)
        self.assertFalse(store.quarantined_show_ids((94404, 95555)))
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_tmdb_quarantine_recovers_after_durable_record_before_unlink(self):
        store = CompatibilityLinkStore(self.root, identity_provider='tmdb')
        first = self.proof()
        second = replace(first, shoko_file_id=43, tmdb_show_id=95555,
                         tmdb_episode_id=1948395)
        store.upsert(first)
        store.upsert(second)
        links = frozenset(store.show_links(94404) | store.show_links(95555))
        with patch.object(store, '_withdraw_quarantined',
                          side_effect=OSError('simulated interruption')):
            with self.assertRaisesRegex(OSError, 'simulated interruption'):
                store.quarantine_card((94404, 95555), links)
        restarted = CompatibilityLinkStore(self.root, identity_provider='tmdb')
        self.assertTrue(restarted.quarantined_show_ids((94404, 95555)))
        self.assertEqual(restarted.show_links(94404), frozenset())
        self.assertEqual(restarted.show_links(95555), frozenset())
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_tmdb_quarantine_refuses_changed_link_without_deleting_it(self):
        store = CompatibilityLinkStore(self.root, identity_provider='tmdb')
        first = self.proof()
        second = replace(first, shoko_file_id=43, tmdb_show_id=95555,
                         tmdb_episode_id=1948395)
        store.upsert(first)
        store.upsert(second)
        links = frozenset(store.show_links(94404) | store.show_links(95555))
        changed = self.root / 'tmdb-95555' / 'Season 01' / 'S01E05.mkv'
        changed.unlink()
        changed.symlink_to('/foreign-video.mkv')
        with self.assertRaisesRegex(ValueError, 'foreign or changed'):
            store.quarantine_card((94404, 95555), links)
        self.assertEqual(os.readlink(changed), '/foreign-video.mkv')
        self.assertFalse(store.quarantined_show_ids((94404, 95555)))
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_quarantined_event_acks_without_link_and_reclassified_file_can_link(self):
        store = CompatibilityLinkStore(self.root, identity_provider='tmdb')
        first = self.proof()
        second = replace(first, shoko_file_id=43, tmdb_show_id=95555,
                         tmdb_episode_id=1948395)
        store.upsert(first)
        store.upsert(second)
        store.quarantine_card(
            (94404, 95555),
            frozenset(store.show_links(94404) | store.show_links(95555)))
        proofs = {42: first}
        reader = SimpleNamespace(read=lambda file_id: proofs[file_id],
                                 confirm_removed=lambda *_: False)
        worker = CompatibilityLinkEvents(
            reader, store, JsonSessionJournal(self.base / 'events.json'),
            write_enabled=True, refresh=lambda *_: True,
            refresh_debounce_seconds=0, clock=lambda: 1000.0)
        worker.enqueue_exact_file_ids([42])
        self.assertEqual(worker.run_pending(limit=1), [(42, 'quarantined')])
        self.assertEqual(worker.pending_ids(), ())
        self.assertIsNone(store.lookup_file(42))
        proofs[42] = replace(first, tmdb_show_id=97777,
                             tmdb_episode_id=1948396)
        worker.enqueue_exact_file_ids([42])
        self.assertEqual(worker.run_pending(limit=1), [(42, 'created')])
        self.assertEqual(store.lookup_file(42)['tmdb_show_id'], 97777)
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_create_retry_and_confirmed_remove_preserve_source(self):
        self.assertEqual(self.store.upsert(self.proof()), 'created')
        link = self.root / 'tvdb-370761' / 'Season 01' / 'S01E05.mkv'
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(self.source))
        self.assertEqual((self.root / 'tvdb-370761' / '.plexmatch').read_text(),
                         'tvdbid: 370761\n')
        self.assertEqual(self.store.upsert(self.proof()), 'unchanged')
        self.assertFalse(self.store.remove(42, confirmed=False))
        self.assertTrue(link.is_symlink())
        self.assertTrue(self.store.remove(42, confirmed=True))
        self.assertFalse(link.exists())
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_tmdb_store_creates_and_removes_only_its_owned_link(self):
        store = CompatibilityLinkStore(self.root, identity_provider='tmdb')
        proof = self.proof()
        self.assertEqual(store.upsert(proof), 'created')
        link = self.root / 'tmdb-94404' / 'Season 01' / 'S01E05.mkv'
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(self.source))
        self.assertEqual((link.parent.parent / '.plexmatch').read_text(),
                         'tmdbid: 94404\n')
        self.assertEqual(store.show_links(94404),
                         frozenset({'tmdb-94404/Season 01/S01E05.mkv'}))
        self.assertEqual(store.upsert(proof), 'unchanged')
        self.assertTrue(store.remove(42, confirmed=True))
        self.assertFalse(link.is_symlink())
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_second_file_for_same_episode_is_held(self):
        self.store.upsert(self.proof())
        with self.assertRaisesRegex(ValueError, 'occupied'):
            self.store.upsert(self.proof(file_id=43))
        self.assertEqual(self.store.manifest()['links']['370761/1/5']['file_id'],
                         42)

    def test_distinct_episode_contest_withdraws_owned_link_and_survives_restart(self):
        from dataclasses import replace
        incumbent = self.proof()
        newcomer = replace(incumbent, shoko_file_id=43,
                           shoko_episode_id=100,
                           anidb_episode_id=226000)
        self.store.upsert(incumbent)
        link = self.root / 'tvdb-370761' / 'Season 01' / 'S01E05.mkv'
        self.store.mark_contested(incumbent, newcomer)
        self.assertFalse(link.is_symlink())
        self.assertIsNone(self.store.lookup_slot(370761, 1, 5))
        restarted = CompatibilityLinkStore(self.root)
        self.assertEqual(restarted.contested_claims(370761, 1, 5),
                         (42, 43))
        with self.assertRaisesRegex(ValueError, 'contested'):
            restarted.upsert(newcomer)
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_same_shoko_episode_with_conflicting_provider_id_is_held(self):
        from dataclasses import replace
        incumbent = self.proof()
        newcomer = replace(incumbent, shoko_file_id=43,
                           tmdb_episode_id=999999)
        self.store.upsert(incumbent)
        self.store.mark_contested(incumbent, newcomer)
        self.assertEqual(self.store.contested_claims(370761, 1, 5),
                         (42, 43))
        self.assertIsNone(self.store.lookup_slot(370761, 1, 5))

    def test_contest_refuses_incumbent_target_changed_since_read(self):
        from dataclasses import replace
        incumbent = self.proof()
        newcomer = replace(incumbent, shoko_file_id=43,
                           shoko_episode_id=100,
                           anidb_episode_id=226000)
        self.store.upsert(incumbent)
        stale = replace(incumbent, plex_target='/wrong-file.mkv')
        with self.assertRaisesRegex(ValueError, 'incumbent'):
            self.store.mark_contested(stale, newcomer)
        self.assertEqual(self.store.contested_claims(370761, 1, 5), ())
        self.assertEqual(self.store.lookup_slot(370761, 1, 5)['file_id'],
                         42)

    def test_restart_finishes_contest_withdrawal_after_committed_intent(self):
        self.store.upsert(self.proof())
        link = self.root / 'tvdb-370761' / 'Season 01' / 'S01E05.mkv'
        with sqlite3.connect(self.store.path) as db:
            db.execute('''INSERT INTO contested_claims VALUES
                ('370761/1/5',42,99,99),('370761/1/5',43,99,100)''')
        self.assertTrue(link.is_symlink())
        restarted = CompatibilityLinkStore(self.root)
        self.assertFalse(link.is_symlink())
        self.assertIsNone(restarted.lookup_slot(370761, 1, 5))
        self.assertEqual(restarted.contested_claims(370761, 1, 5),
                         (42, 43))

    def test_later_claimant_cannot_reopen_contested_slot(self):
        from dataclasses import replace
        incumbent = self.proof()
        newcomer = replace(incumbent, shoko_file_id=43,
                           shoko_episode_id=100,
                           anidb_episode_id=226000)
        self.store.upsert(incumbent)
        self.store.mark_contested(incumbent, newcomer)
        third = replace(incumbent, shoko_file_id=44,
                        shoko_episode_id=101,
                        anidb_episode_id=226001)
        with self.assertRaisesRegex(ValueError, 'contested'):
            self.store.upsert(third)
        self.assertEqual(CompatibilityLinkStore(self.root).contested_claims(
            370761, 1, 5), (42, 43, 44))

    def test_exact_survivor_resolves_contest_atomically(self):
        from dataclasses import replace
        incumbent = self.proof()
        newcomer = replace(incumbent, shoko_file_id=43,
                           shoko_episode_id=100,
                           anidb_episode_id=226000)
        self.store.upsert(incumbent)
        self.store.mark_contested(incumbent, newcomer)
        with self.assertRaisesRegex(ValueError, 'claim'):
            self.store.resolve_contest(newcomer, expected_file_ids=(43,))
        self.assertEqual(self.store.contested_claims(370761, 1, 5),
                         (42, 43))
        self.assertEqual(self.store.resolve_contest(
            newcomer, expected_file_ids=(42, 43)), 'created')
        restarted = CompatibilityLinkStore(self.root)
        self.assertEqual(restarted.contested_claims(370761, 1, 5), ())
        self.assertEqual(restarted.lookup_slot(370761, 1, 5)['file_id'],
                         43)
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_restart_finishes_pending_survivor_after_resolution_commit(self):
        from dataclasses import replace
        incumbent = self.proof()
        newcomer = replace(incumbent, shoko_file_id=43,
                           shoko_episode_id=100,
                           anidb_episode_id=226000)
        self.store.upsert(incumbent)
        self.store.mark_contested(incumbent, newcomer)
        with sqlite3.connect(self.store.path) as db:
            db.execute('''INSERT INTO links VALUES
                (?,?,?,?,?,?,?,?,?,?,?,NULL)''',
                ('370761/1/5', 43, newcomer.shoko_series_id,
                 newcomer.anidb_episode_id, newcomer.tmdb_episode_id,
                 newcomer.tvdb_episode_id, newcomer.tmdb_show_id,
                 '.mkv', 'tvdb-370761/Season 01/S01E05.mkv',
                 str(self.source), 'pending'))
            db.execute('DELETE FROM contested_claims WHERE slot=?',
                       ('370761/1/5',))
        restarted = CompatibilityLinkStore(self.root)
        self.assertEqual(restarted.lookup_file(43)['state'], 'pending')
        self.assertEqual(restarted.upsert(newcomer), 'replaced')
        self.assertEqual(restarted.lookup_file(43)['state'], 'ready')
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_event_recheck_resolves_real_store_after_relink(self):
        from dataclasses import replace
        incumbent = self.proof()
        newcomer = replace(incumbent, shoko_file_id=43,
                           shoko_episode_id=100,
                           anidb_episode_id=226000)
        proofs = {42: incumbent, 43: newcomer}
        reader = SimpleNamespace(
            read=lambda file_id: proofs[file_id],
            confirm_removed=lambda *_: False)
        journal = JsonSessionJournal(self.base / 'events.json')
        refreshes = []
        worker = CompatibilityLinkEvents(
            reader, self.store, journal, write_enabled=True,
            refresh=lambda show_id: refreshes.append(show_id) or True,
            refresh_debounce_seconds=0, clock=lambda: 1000.0)
        self.store.upsert(incumbent)
        worker.enqueue_exact_file_ids([43])
        self.assertEqual(worker.run_pending(limit=1), [(43, 'held')])
        self.assertEqual(self.store.contested_claims(370761, 1, 5),
                         (42, 43))
        self.assertIsNone(self.store.lookup_slot(370761, 1, 5))
        proofs[42] = replace(incumbent, episode=6)
        restarted = CompatibilityLinkEvents(
            reader, CompatibilityLinkStore(self.root), journal,
            write_enabled=True,
            refresh=lambda show_id: refreshes.append(show_id) or True,
            refresh_debounce_seconds=0, clock=lambda: 1000.0)
        restarted.enqueue_exact_file_ids([43])
        self.assertEqual(restarted.run_pending(limit=1), [(43, 'created')])
        self.assertEqual(self.store.contested_claims(370761, 1, 5), ())
        self.assertEqual(self.store.lookup_slot(370761, 1, 5)['file_id'],
                         43)
        self.assertIn(42, restarted.pending_ids())
        self.assertTrue(restarted.refresh_pending())
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_event_clears_empty_old_slot_and_checks_new_coordinates(self):
        from dataclasses import replace
        incumbent = self.proof()
        newcomer = replace(incumbent, shoko_file_id=43,
                           shoko_episode_id=100,
                           anidb_episode_id=226000)
        proofs = {42: incumbent, 43: newcomer}
        reader = SimpleNamespace(
            read=lambda file_id: proofs[file_id],
            confirm_removed=lambda *_: False)
        journal = JsonSessionJournal(self.base / 'events.json')
        worker = CompatibilityLinkEvents(
            reader, self.store, journal, write_enabled=True,
            refresh=lambda *_: True, refresh_debounce_seconds=0,
            clock=lambda: 1000.0)
        self.store.upsert(incumbent)
        worker.enqueue_exact_file_ids([43])
        self.assertEqual(worker.run_pending(limit=1), [(43, 'held')])
        proofs[42] = replace(incumbent, episode=6)
        proofs[43] = replace(newcomer, episode=7)
        worker.enqueue_exact_file_ids([43])
        self.assertEqual(worker.run_pending(limit=1), [(43, 'created')])
        self.assertEqual(self.store.contested_claims(370761, 1, 5), ())
        self.assertEqual(self.store.lookup_slot(370761, 1, 7)['file_id'],
                         43)
        self.assertIn(42, worker.pending_ids())
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_corrected_shoko_episode_on_same_file_resolves_contest(self):
        from dataclasses import replace
        incumbent = self.proof()
        newcomer = replace(incumbent, shoko_file_id=43,
                           shoko_episode_id=100,
                           anidb_episode_id=226000)
        proofs = {42: incumbent, 43: newcomer}
        reader = SimpleNamespace(
            read=lambda file_id: proofs[file_id],
            confirm_removed=lambda *_: False)
        journal = JsonSessionJournal(self.base / 'events.json')
        worker = CompatibilityLinkEvents(
            reader, self.store, journal, write_enabled=True,
            refresh=lambda *_: True, refresh_debounce_seconds=0,
            clock=lambda: 1000.0)
        self.store.upsert(incumbent)
        worker.enqueue_exact_file_ids([43])
        self.assertEqual(worker.run_pending(limit=1), [(43, 'held')])
        proofs[43] = replace(incumbent, shoko_file_id=43)
        worker.enqueue_exact_file_ids([43])
        self.assertEqual(worker.run_pending(limit=1), [(43, 'created')])
        self.assertEqual(self.store.lookup_slot(370761, 1, 5)['file_id'],
                         42)
        self.assertEqual(self.store.contested_claims(370761, 1, 5), ())

    def test_corrected_shoko_episode_does_not_hide_provider_conflict(self):
        from dataclasses import replace
        incumbent = self.proof()
        newcomer = replace(incumbent, shoko_file_id=43,
                           shoko_episode_id=100,
                           anidb_episode_id=226000)
        proofs = {42: incumbent, 43: newcomer}
        reader = SimpleNamespace(
            read=lambda file_id: proofs[file_id],
            confirm_removed=lambda *_: False)
        worker = CompatibilityLinkEvents(
            reader, self.store, JsonSessionJournal(self.base / 'events.json'),
            write_enabled=True, refresh=lambda *_: True,
            refresh_debounce_seconds=0, clock=lambda: 1000.0)
        self.store.upsert(incumbent)
        worker.enqueue_exact_file_ids([43])
        self.assertEqual(worker.run_pending(limit=1), [(43, 'held')])
        proofs[43] = replace(newcomer, shoko_episode_id=99)
        worker.enqueue_exact_file_ids([43])
        self.assertEqual(worker.run_pending(limit=1), [(43, 'held')])
        self.assertIsNone(self.store.lookup_slot(370761, 1, 5))
        self.assertEqual(self.store.contested_claims(370761, 1, 5), (42, 43))
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_deleted_claim_requires_positive_removal_before_resolution(self):
        from dataclasses import replace
        incumbent = self.proof()
        newcomer = replace(incumbent, shoko_file_id=43,
                           shoko_episode_id=100,
                           anidb_episode_id=226000)
        removed = [False]
        confirmed = [False]

        def read(file_id):
            if file_id == 42 and removed[0]:
                raise ValueError('file no longer present')
            return {42: incumbent, 43: newcomer}[file_id]

        reader = SimpleNamespace(
            read=read,
            confirm_removed=lambda file_id, series_id: (
                confirmed[0] and file_id == 42 and series_id == 1051))
        journal = JsonSessionJournal(self.base / 'events.json')
        worker = CompatibilityLinkEvents(
            reader, self.store, journal, write_enabled=True,
            refresh=lambda *_: True, refresh_debounce_seconds=0,
            clock=lambda: 1000.0)
        self.store.upsert(incumbent)
        worker.enqueue_exact_file_ids([43])
        self.assertEqual(worker.run_pending(limit=1), [(43, 'held')])
        removed[0] = True
        worker.enqueue_exact_file_ids([43])
        self.assertEqual(worker.run_pending(limit=1), [(43, 'held')])
        self.assertEqual(self.store.contested_claims(370761, 1, 5),
                         (42, 43))
        confirmed[0] = True
        worker.enqueue_exact_file_ids([43])
        self.assertEqual(worker.run_pending(limit=1), [(43, 'created')])
        self.assertEqual(self.store.lookup_slot(370761, 1, 5)['file_id'],
                         43)
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_two_confirmed_deletions_clear_empty_contest(self):
        from dataclasses import replace
        incumbent = self.proof()
        newcomer = replace(incumbent, shoko_file_id=43,
                           shoko_episode_id=100,
                           anidb_episode_id=226000)
        deleted = [False]

        def read(file_id):
            if deleted[0]:
                raise ValueError('Shoko file no longer present')
            return {42: incumbent, 43: newcomer}[file_id]

        reader = SimpleNamespace(
            read=read,
            confirm_removed=lambda file_id, series_id: (
                deleted[0] and file_id in (42, 43)
                and series_id == 1051))
        journal = JsonSessionJournal(self.base / 'events.json')
        worker = CompatibilityLinkEvents(
            reader, self.store, journal, write_enabled=True,
            refresh=lambda *_: True, refresh_debounce_seconds=0,
            clock=lambda: 1000.0)
        self.store.upsert(incumbent)
        worker.enqueue_exact_file_ids([43])
        self.assertEqual(worker.run_pending(limit=1), [(43, 'held')])
        deleted[0] = True
        worker.enqueue_deleted_file_ids([43])
        self.assertEqual(worker.run_pending(limit=1),
                         [(43, 'contest-cleared')])
        self.assertEqual(self.store.contested_claims(370761, 1, 5), ())
        self.assertIsNone(self.store.lookup_slot(370761, 1, 5))
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_deleted_signal_requeues_file_reclassified_as_movie(self):
        from dataclasses import replace
        incumbent = self.proof()
        newcomer = replace(incumbent, shoko_file_id=43,
                           shoko_episode_id=100,
                           anidb_episode_id=226000)
        reclassified = [False]

        def read(file_id):
            if reclassified[0]:
                if file_id == 42:
                    raise ValueError('confirmed removed')
                raise NotRegularTVFile('now a movie', kind='Movie')
            return {42: incumbent, 43: newcomer}[file_id]

        reader = SimpleNamespace(
            read=read,
            confirm_removed=lambda file_id, series_id: (
                reclassified[0] and file_id == 42
                and series_id == 1051))
        journal = JsonSessionJournal(self.base / 'events.json')
        worker = CompatibilityLinkEvents(
            reader, self.store, journal, write_enabled=True,
            refresh=lambda *_: True, refresh_debounce_seconds=0,
            clock=lambda: 1000.0)
        self.store.upsert(incumbent)
        worker.enqueue_exact_file_ids([43])
        self.assertEqual(worker.run_pending(limit=1), [(43, 'held')])
        reclassified[0] = True
        worker.enqueue_deleted_file_ids([43])
        self.assertEqual(worker.run_pending(limit=1),
                         [(43, 'contest-cleared')])
        self.assertIn(43, worker.pending_ids())

    def test_same_file_cannot_leave_old_slot_link_when_episode_changes(self):
        from dataclasses import replace
        self.store.upsert(self.proof())
        with self.assertRaisesRegex(ValueError, 'moved episode slot'):
            self.store.upsert(replace(self.proof(), episode=6))
        self.assertIsNotNone(self.store.lookup_slot(370761, 1, 5))
        self.assertIsNone(self.store.lookup_slot(370761, 1, 6))

    def test_new_mp4_uses_original_container_extension(self):
        movie = self.base / 'original.mp4'
        movie.write_bytes(b'original')
        proof = self.proof()
        from dataclasses import replace
        self.assertEqual(self.store.upsert(replace(proof, source_path=movie,
                                                   plex_target=str(movie))),
                         'created')
        self.assertTrue((self.root / 'tvdb-370761' / 'Season 01' /
                         'S01E05.mp4').is_symlink())

    def test_exact_seerr_special_uses_season_zero(self):
        from dataclasses import replace
        proof = replace(self.proof(), season=0, episode=8)
        self.assertEqual(self.store.upsert(proof), 'created')
        self.assertTrue((self.root / 'tvdb-370761' / 'Season 00' /
                         'S00E08.mkv').is_symlink())

    def test_retarget_only_same_file_and_source_remains(self):
        self.store.upsert(self.proof())
        other = self.base / 'replacement.mkv'
        other.write_bytes(b'replace!')
        self.assertEqual(self.store.upsert(self.proof(plex_target=str(other))),
                         'replaced')
        link = self.root / 'tvdb-370761' / 'Season 01' / 'S01E05.mkv'
        self.assertEqual(os.readlink(link), str(other))
        self.assertTrue(self.source.exists())

    def test_recovers_pending_create_after_interrupted_link_write(self):
        self.store.upsert(self.proof())
        link = self.root / 'tvdb-370761' / 'Season 01' / 'S01E05.mkv'
        link.unlink()
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE links SET state='pending' WHERE slot=?",
                       ('370761/1/5',))
        self.assertEqual(self.store.upsert(self.proof()), 'replaced')
        self.assertEqual(os.readlink(link), str(self.source))
        self.assertEqual(self.store.lookup_file(42)['state'], 'ready')

    def test_recovers_pending_retarget_without_replacing_foreign_link(self):
        self.store.upsert(self.proof())
        other = self.base / 'replacement.mkv'
        other.write_bytes(b'replace!')
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE links SET target=?, old_target=?, state='pending' WHERE slot=?",
                       (str(other), str(self.source), '370761/1/5'))
        self.assertEqual(self.store.upsert(self.proof(plex_target=str(other))),
                         'replaced')
        link = self.root / 'tvdb-370761' / 'Season 01' / 'S01E05.mkv'
        self.assertEqual(os.readlink(link), str(other))
        link.unlink()
        os.symlink(str(self.base / 'foreign.mkv'), link)
        with self.assertRaisesRegex(ValueError, 'foreign'):
            self.store.upsert(self.proof(plex_target=str(other)))

    def test_refuse_tampered_manifest_or_foreign_link(self):
        self.store.upsert(self.proof())
        with sqlite3.connect(self.store.path) as db:
            db.execute('UPDATE links SET link=? WHERE slot=?',
                       ('../outside.mkv', '370761/1/5'))
        with self.assertRaises(ValueError):
            CompatibilityLinkStore(self.root)


if __name__ == '__main__':
    unittest.main()
