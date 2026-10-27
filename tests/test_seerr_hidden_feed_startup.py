"""The stock-Seerr feed is an explicit, TMDB-only Nagisa opt-in."""

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nagisa.seerr_compat_links import (LinkProof, LinkProofReader,
                                      OwnedPublishedCoordinate)
from nagisa.seerr_compat_plex_refresh import VerifiedShowRefresh
from nagisa.seerr_compat_store import CompatibilityLinkStore
from nagisa.seerr_hidden_feed_startup import (build_tmdb_event_worker,
                                              build_configured_tmdb_feed)
from nagisa.seerr_startup import attach_hidden_feed


class NoNetwork:
    def request_json(self, method, path):
        raise AssertionError('assembly must not contact a live service')


class HiddenFeedStartupTests(unittest.TestCase):
    def setUp(self):
        recovery = patch.object(CompatibilityLinkStore, '_recover_contests')
        recovery.start()
        self.addCleanup(recovery.stop)
        quarantine = patch.object(CompatibilityLinkStore,
                                  '_recover_quarantine')
        quarantine.start()
        self.addCleanup(quarantine.stop)
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config_dir = root / 'config'
        self.config_dir.mkdir()
        self.link_root = root / 'feed'
        self.link_root.mkdir()
        self.config = SimpleNamespace(
            config_dir=self.config_dir, shoko_url='http://shoko.invalid',
            shoko_token='private', plex_url='http://plex.invalid',
            plex_token='private', request_timeout_seconds=5,
            plex_section_ids=(102, 103))
        self.shoko = NoNetwork()
        self.reader = LinkProofReader(
            self.shoko, lambda series: {102: []}, NoNetwork(), NoNetwork(),
            sections=(102,), allowed_folders={2: (root, '/source-pure/Anime')},
            part_probe=lambda path: False, coordinate_mode='tmdb')
        self.store = CompatibilityLinkStore(self.link_root, identity_provider='tmdb')
        self.cursor = {'imported_utc': '2026-10-24T00:00:00+00:00',
                       'file_id': 42}

    def test_report_assembly_is_idle_and_durable(self):
        worker, bindings = build_tmdb_event_worker(
            self.config, self.shoko, self.reader, self.store,
            {'mode': 'report', 'initial_cursor': self.cursor})
        self.assertFalse(worker.processor.write_enabled)
        self.assertIsNone(worker.processor.movie_events)
        self.assertEqual(worker.snapshot()['connections'], 0)
        self.assertTrue(any('seerr-compat-tmdb-cursor.json' in row[0]
                            for row in bindings))
        self.assertEqual(worker.catchup._watermark()[1], 42)

    def test_exact_seerr_episode_order_keeps_tmdb_show_identity(self):
        reader = LinkProofReader(
            self.shoko, lambda series: {102: []}, NoNetwork(), NoNetwork(),
            sections=(102,),
            allowed_folders={2: (Path(self.temp.name), '/source-pure/Anime')},
            part_probe=lambda path: False, coordinate_mode='tvdb')
        with self.assertRaises(ValueError):
            build_tmdb_event_worker(
                self.config, self.shoko, reader, self.store,
                {'mode': 'report', 'initial_cursor': self.cursor})
        worker, _ = build_tmdb_event_worker(
            self.config, self.shoko, reader, self.store,
            {'mode': 'report', 'initial_cursor': self.cursor,
             'episode_ordering': 'seerr_tvdb'})
        self.assertEqual(worker.processor.reader.coordinate_mode, 'tvdb')
        self.assertEqual(worker.processor.store.identity_provider, 'tmdb')
        self.assertFalse(worker.processor.write_enabled)
        verifier = VerifiedShowRefresh(
            NoNetwork(), self.store, 130, '/TestData/feed', lambda: None,
            lambda path: None, identity_provider='tmdb')
        with self.assertRaisesRegex(ValueError, 'stage'):
            build_tmdb_event_worker(
                self.config, self.shoko, reader, self.store,
                {'mode': 'apply', 'initial_cursor': self.cursor,
                 'episode_ordering': 'seerr_tvdb'}, refresh=verifier)
        with self.assertRaisesRegex(ValueError, 'episode order'):
            build_tmdb_event_worker(
                self.config, self.shoko, self.reader, self.store,
                {'mode': 'report', 'initial_cursor': self.cursor,
                 'episode_ordering': 'seerr_tvdb'})

    def test_canonical_apply_requires_matching_stage_marker_and_cursor(self):
        reader = LinkProofReader(
            self.shoko, lambda series: {102: []}, NoNetwork(), NoNetwork(),
            sections=(102,),
            allowed_folders={2: (Path(self.temp.name), '/source-pure/Anime')},
            part_probe=lambda path: False, coordinate_mode='tvdb')
        verifier = VerifiedShowRefresh(
            NoNetwork(), self.store, 130, '/TestData/feed', lambda: None,
            lambda path: None, identity_provider='tmdb')
        receipt = 'a' * 64
        settings = {'mode': 'apply', 'initial_cursor': self.cursor,
                    'episode_ordering': 'seerr_tvdb',
                    'canonical_stage_receipt_sha256': receipt}
        marker = self.link_root / '.nagisa-canonical-stage.json'
        with self.assertRaisesRegex(ValueError, 'marker is absent'):
            build_tmdb_event_worker(
                self.config, self.shoko, reader, self.store, settings,
                refresh=verifier)
        marker.write_text(json.dumps({
            'schema': 1, 'episode_ordering': 'seerr_tvdb',
            'receipt_sha256': receipt, 'initial_cursor': self.cursor,
            'initial_ready': 1}), encoding='utf-8')
        with patch.object(self.store, 'manifest', return_value={
                'version': 2, 'links': {'30984/17/50': {'state': 'ready'}}}):
            worker, bindings = build_tmdb_event_worker(
                self.config, self.shoko, reader, self.store, settings,
                refresh=verifier)
            self.assertTrue(worker.processor.write_enabled)
            self.assertTrue(any('seerr-compat-canonical-cursor.json' in row[0]
                                for row in bindings))
            with self.assertRaisesRegex(ValueError, 'stage'):
                build_tmdb_event_worker(
                    self.config, self.shoko, reader, self.store,
                    settings | {'initial_cursor': {
                        'imported_utc': self.cursor['imported_utc'],
                        'file_id': 43}}, refresh=verifier)
            with self.assertRaisesRegex(ValueError, 'stage'):
                build_tmdb_event_worker(
                    self.config, self.shoko, reader, self.store,
                    settings | {'canonical_stage_receipt_sha256': 'b' * 64},
                    refresh=verifier)
            (self.config_dir / 'seerr-compat-canonical-cursor.json').unlink()
            marker.write_text(json.dumps({
                'schema': 1, 'episode_ordering': 'seerr_tvdb',
                'receipt_sha256': receipt, 'initial_cursor': self.cursor,
                'initial_ready': 2}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'initial ledger'):
                build_tmdb_event_worker(
                    self.config, self.shoko, reader, self.store, settings,
                    refresh=verifier)

    @unittest.skipIf(os.name == 'nt', 'canonical links require Linux symlinks')
    def test_canonical_stage_catches_next_import_without_touching_tmdb_journal(self):
        source = Path(self.temp.name) / 'original.mkv'
        source.write_bytes(b'original')
        initial = LinkProof(42, 99, 1051, 225738, 1948394, 7398592,
                            94404, 370761, 1, 5, source, str(source), 8)
        next_file = replace(initial, shoko_file_id=43,
                            shoko_episode_id=1052, anidb_episode_id=225739,
                            tmdb_episode_id=1948395, tvdb_episode_id=7398593,
                            episode=6)
        self.assertEqual(self.store.upsert(initial), 'created')
        old_cursor = self.config_dir / 'seerr-compat-tmdb-cursor.json'
        old_cursor.write_text('running TMDB journal', encoding='utf-8')
        receipt = 'a' * 64
        (self.link_root / '.nagisa-canonical-stage.json').write_text(
            json.dumps({'schema': 1, 'episode_ordering': 'seerr_tvdb',
                        'receipt_sha256': receipt,
                        'initial_cursor': self.cursor, 'initial_ready': 1}),
            encoding='utf-8')
        reader = LinkProofReader(
            self.shoko, lambda series: {102: []}, NoNetwork(), NoNetwork(),
            sections=(102,),
            allowed_folders={2: (Path(self.temp.name), '/source-pure/Anime')},
            part_probe=lambda path: False, coordinate_mode='tvdb')
        reader.read = lambda file_id: {43: next_file}[file_id]
        verifier = VerifiedShowRefresh(
            NoNetwork(), self.store, 130, '/TestData/feed', lambda: None,
            lambda path: None, identity_provider='tmdb')
        worker, _ = build_tmdb_event_worker(
            self.config, self.shoko, reader, self.store,
            {'mode': 'apply', 'initial_cursor': self.cursor,
             'episode_ordering': 'seerr_tvdb',
             'canonical_stage_receipt_sha256': receipt}, refresh=verifier)
        recent = {'Total': 2, 'List': [
            {'ID': 43, 'Imported': '2026-10-24T00:00:01+00:00'},
            {'ID': 42, 'Imported': self.cursor['imported_utc']}]}
        with patch.object(self.shoko, 'request_json', return_value=recent):
            batch = worker.catchup.run_once()
        self.assertEqual(batch.file_ids, (43,))
        self.assertEqual(worker.catchup.drain_once(), 1)
        self.assertEqual(worker.processor.run_pending(limit=1), [(43, 'created')])
        self.assertEqual(worker.catchup._watermark()[1], 43)
        self.assertEqual(self.store.lookup_file(42)['slot'], '94404/1/5')
        self.assertEqual(self.store.lookup_file(43)['slot'], '94404/1/6')
        self.assertTrue((self.link_root / 'tmdb-94404' / 'Season 01' /
                         'S01E06.mkv').is_symlink())
        self.assertEqual(old_cursor.read_text(encoding='utf-8'),
                         'running TMDB journal')
        self.assertEqual(source.read_bytes(), b'original')

    def test_apply_requires_exact_verifier_and_cursor(self):
        with self.assertRaisesRegex(ValueError, 'exact Plex'):
            build_tmdb_event_worker(
                self.config, self.shoko, self.reader, self.store,
                {'mode': 'apply', 'initial_cursor': self.cursor},
                refresh=lambda show_id: True)
        verifier = VerifiedShowRefresh(
            NoNetwork(), self.store, 130, '/TestData/feed', lambda: None,
            lambda path: None, identity_provider='tmdb')
        worker, _ = build_tmdb_event_worker(
            self.config, self.shoko, self.reader, self.store,
            {'mode': 'apply', 'initial_cursor': self.cursor},
            refresh=verifier)
        self.assertTrue(worker.processor.write_enabled)
        self.config.config_dir = Path(self.temp.name) / 'fresh-config'
        self.config.config_dir.mkdir()
        with self.assertRaisesRegex(ValueError, 'cursor'):
            build_tmdb_event_worker(
                self.config, self.shoko, self.reader, self.store,
                {'mode': 'apply'}, refresh=verifier)

    def test_wrong_identity_or_root_is_rejected(self):
        self.store.identity_provider = 'tvdb'
        with self.assertRaisesRegex(ValueError, 'TMDB'):
            build_tmdb_event_worker(
                self.config, self.shoko, self.reader, self.store,
                {'mode': 'report', 'initial_cursor': self.cursor})
        self.store.identity_provider = 'tmdb'
        self.config.config_dir = self.link_root
        with self.assertRaisesRegex(ValueError, 'separate'):
            build_tmdb_event_worker(
                self.config, self.shoko, self.reader, self.store,
                {'mode': 'report', 'initial_cursor': self.cursor})

    def feed_settings(self):
        return {
            'mode': 'report', 'link_root': str(self.link_root),
            'plex_link_root': '/TestData/feed', 'section_id': 130,
            'source_section_ids': [102, 103],
            'managed_folders': [{'id': 2, 'root': self.temp.name,
                                 'plex_alias': '/source-pure/Anime'}],
            'ignored_folders': [], 'initial_cursor': self.cursor,
        }

    def test_configured_feed_requires_exact_hidden_stock_plex_section(self):
        class Plex:
            agent = 'tv.plex.agents.series'
            ordering = 'tmdbAiring'
            def request_json(self, method, path):
                assert method == 'GET' and path.startswith('/library/sections?')
                return {'MediaContainer': {'Directory': [{
                    'key': '130', 'type': 'show', 'agent': self.agent,
                    'Location': [{'path': '/TestData/feed'}],
                    'Preferences': {'Setting': [
                        {'id': 'hidden', 'value': '2'},
                        {'id': 'showOrdering', 'value': self.ordering}]},
                }]}}

        plex = Plex()
        worker, bindings = build_configured_tmdb_feed(
            self.config, self.shoko, plex, NoNetwork(),
            self.feed_settings(), lambda path: True)
        self.assertFalse(worker.processor.write_enabled)
        self.assertEqual(worker.processor.reader.coordinate_mode, 'tmdb')
        self.assertIsNone(worker.processor.reader.published_resolver)
        self.assertIsNotNone(worker.owned_audit)
        self.assertIsNotNone(worker.mapping_audit)
        self.assertIsNotNone(worker.series_recheck)
        self.assertIsNotNone(worker.series_audit)
        self.assertEqual(worker.series_recheck.batch_size, 5)
        self.assertEqual(worker.series_audit.batch_size, 5)
        self.assertEqual(worker.client_factory().feeds, 'file,metadata')
        self.assertIs(worker.processor.series_recheck, worker.series_recheck)
        self.assertEqual(worker.mapping_audit.batch_size, 25)
        self.assertEqual(worker.review_limit, 5)
        self.assertTrue(any(row[0] == str(self.link_root.resolve())
                            for row in bindings))
        exact_settings = self.feed_settings()
        exact_settings['episode_ordering'] = 'seerr_tvdb'
        exact_worker, exact_bindings = build_configured_tmdb_feed(
            self.config, self.shoko, plex, NoNetwork(),
            exact_settings, lambda path: True)
        self.assertEqual(exact_worker.processor.reader.coordinate_mode, 'tvdb')
        self.assertEqual(exact_worker.processor.store.identity_provider, 'tmdb')
        self.assertEqual(exact_worker.processor.reader.seerr_episodes.cache_seconds,
                         3600)
        self.assertIsInstance(exact_worker.processor.reader.published_resolver,
                              OwnedPublishedCoordinate)
        self.assertIs(exact_worker.processor.reader.published_resolver.store,
                      exact_worker.processor.store)
        default_journals = {row[0] for row in bindings
                            if row[0].endswith('.json')
                            and 'movie' not in row[0]}
        canonical_journals = {row[0] for row in exact_bindings
                              if row[0].endswith('.json')
                              and 'movie' not in row[0]}
        self.assertTrue(default_journals)
        self.assertTrue(canonical_journals)
        self.assertFalse(default_journals & canonical_journals,
                         'canonical order must not reuse live TMDB journals')
        exact_settings['episode_ordering'] = 'unreviewed'
        with self.assertRaises(ValueError):
            build_configured_tmdb_feed(
                self.config, self.shoko, plex, NoNetwork(),
                exact_settings, lambda path: True)
        plex.agent = 'tv.plex.agents.custom.shoko'
        with self.assertRaisesRegex(ValueError, 'private stock Plex'):
            build_configured_tmdb_feed(
                self.config, self.shoko, plex, NoNetwork(),
                self.feed_settings(), lambda path: True)
        plex.agent = 'tv.plex.agents.series'
        plex.ordering = 'dvd'
        with self.assertRaisesRegex(ValueError, 'private stock Plex'):
            build_configured_tmdb_feed(
                self.config, self.shoko, plex, NoNetwork(),
                self.feed_settings(), lambda path: True)

    def test_apply_wires_only_private_section_trash_clear(self):
        class Plex:
            def request_json(self, method, path):
                self_section = {'key': '130', 'type': 'show',
                    'agent': 'tv.plex.agents.series',
                    'Location': [{'path': '/TestData/feed'}],
                    'Preferences': {'Setting': [
                        {'id': 'hidden', 'value': '2'},
                        {'id': 'showOrdering', 'value': 'tmdbAiring'}]}}
                return {'MediaContainer': {'Directory': [self_section]}}

        settings = self.feed_settings()
        settings['mode'] = 'apply'
        calls = []
        with patch('nagisa.seerr_hidden_feed_startup.JsonTransport') as transport:
            transport.return_value.request_json.side_effect = (
                lambda method, path: calls.append((method, path)))
            worker, _ = build_configured_tmdb_feed(
                self.config, self.shoko, Plex(), NoNetwork(),
                settings, lambda path: True)
            worker.processor.refresh.empty_trash(130)
        self.assertEqual(calls, [('PUT', '/library/sections/130/emptyTrash')])

    def test_configured_feed_rejects_unmounted_or_public_source(self):
        class Plex:
            def request_json(self, method, path):
                return {'MediaContainer': {'Directory': []}}

        settings = self.feed_settings()
        settings['link_root'] = str(self.config_dir / 'missing')
        with self.assertRaises(ValueError):
            build_configured_tmdb_feed(
                self.config, self.shoko, Plex(), NoNetwork(),
                settings, lambda path: True)

    def test_configured_feed_never_creates_an_empty_ledger_on_existing_feed(self):
        class Plex:
            def request_json(self, method, path):
                return {'MediaContainer': {'Directory': [{
                    'key': '130', 'type': 'show',
                    'agent': 'tv.plex.agents.series',
                    'Location': [{'path': '/TestData/feed'}],
                    'Preferences': {'Setting': [
                        {'id': 'hidden', 'value': '2'},
                        {'id': 'showOrdering', 'value': 'tmdbAiring'}]},
                }]}}

        empty_root = Path(self.temp.name) / 'existing-feed-without-ledger'
        empty_root.mkdir()
        (empty_root / 'tmdb-42').mkdir()
        settings = self.feed_settings()
        settings['link_root'] = str(empty_root)
        with self.assertRaisesRegex(ValueError, 'ownership ledger'):
            build_configured_tmdb_feed(
                self.config, self.shoko, Plex(), NoNetwork(),
                settings, lambda path: True)
        self.assertFalse((empty_root / '.nagisa-compat-tmdb.sqlite').exists())
        settings = self.feed_settings()
        settings['source_section_ids'] = [130]
        with self.assertRaises(ValueError):
            build_configured_tmdb_feed(
                self.config, self.shoko, Plex(), NoNetwork(),
                settings, lambda path: True)

    def test_seerr_app_attachment_is_explicit_and_preserves_bindings(self):
        app = SimpleNamespace()
        original = [('source', 'existing guard source', 'read-only')]
        with patch('nagisa.seerr_hidden_feed_startup.build_configured_tmdb_feed') as build:
            self.assertEqual(attach_hidden_feed(
                self.config, {}, app, self.shoko, NoNetwork(), NoNetwork(),
                lambda path: True, original), original)
            self.assertIsNone(app.file_event_worker)
            build.assert_not_called()
            marker = object()
            build.return_value = (marker, (('feed', 'owned link root', 'read/write'),))
            result = attach_hidden_feed(
                self.config, {'hidden_feed': self.feed_settings()}, app,
                self.shoko, NoNetwork(), NoNetwork(), lambda path: True,
                original)
            self.assertIs(app.file_event_worker, marker)
            self.assertEqual(result, original + [
                ('feed', 'owned link root', 'read/write')])
