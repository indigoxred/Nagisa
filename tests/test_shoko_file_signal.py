"""Private Shoko file-event wake-ups; no live listener or Seerr writes."""

import json
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace

from nagisa.shoko_file_signal import ShokoFileSignalProcessor, parse_signalr_frames
from nagisa.seerr_journal import JsonSessionJournal


def frame(target, payload):
    return json.dumps({'type': 1, 'target': target,
                       'arguments': [payload]}) + '\x1e'


class Shoko:
    def __init__(self, episodes):
        self.episodes = episodes
        self.calls = []

    def request_json(self, method, path):
        self.calls.append((method, path))
        if path.startswith('/api/v3/ReverseTree/File/') and path.endswith(
                '/Episode?includeXRefs=true&includeDataFrom=AniDB'):
            if isinstance(self.episodes, dict):
                return self.episodes[int(path.split('/')[5])]
            if path.startswith('/api/v3/ReverseTree/File/42/'):
                return self.episodes
        raise AssertionError(path)


class Catalogue:
    def __init__(self, outcome=None):
        self.outcome = outcome or {84669: 'would_update'}
        self.calls = []

    def run_for_shoko_series_id(self, series_id, *, before_write=None):
        self.calls.append((series_id, before_write))
        return self.outcome

    def run_once(self, *, before_write=None):
        self.calls.append(('catchup', before_write))
        return {'eligible': 1, 'examined': 1}


class MovieCatalogue:
    def __init__(self, outcome='would_available'):
        self.outcome = outcome
        self.calls = []

    def run_for_tmdb_id(self, tmdb_id, *, before_write=None):
        if before_write is not None:
            with before_write() as current:
                if not current:
                    return 'held'
        self.calls.append(tmdb_id)
        return self.outcome


class FileSignalTests(unittest.TestCase):
    def test_exact_show_arrival_wakes_private_request_retry_even_when_plex_lags(self):
        shoko = Shoko([{'IDs': {'ID': 99, 'ParentSeries': 1083},
                        'AniDB': {'Type': 'Episode'}}])
        class Retry:
            def __init__(self):
                self.ids = []
            def enqueue(self, tmdb_id):
                self.ids.append(tmdb_id)
        retry = Retry()
        processor = ShokoFileSignalProcessor(
            shoko, Catalogue({84669: 'held'}), request_retry=retry)
        processor.ingest(frame('file:hashed', {'FileID': 42}))
        self.assertIsNone(processor.review_one())
        self.assertEqual(retry.ids, [84669])
        self.assertEqual(processor.pending_ids(), (42,))

    def test_signalr_record_separator_parses_only_named_file_events(self):
        data = '{}\x1e' + frame('file:relocated', {'FileID': 42})
        self.assertEqual(parse_signalr_frames(data), [('relocated', 42)])

    def test_detected_event_has_no_file_id_and_is_not_guessed_from_path(self):
        data = frame('file:detected', {
            'RelativePath': 'new.mkv', 'ManagedFolderID': 7})
        self.assertEqual(parse_signalr_frames(data), [])

    def test_relocated_file_uses_current_shoko_episode_not_event_claims(self):
        shoko = Shoko([{'IDs': {'ID': 99, 'ParentSeries': 1083},
                        'AniDB': {'Type': 'Episode'}}])
        catalogue = Catalogue()
        processor = ShokoFileSignalProcessor(shoko, catalogue)
        processor.ingest(frame('file:relocated', {
            'FileID': 42, 'CrossReferences': [{'SeriesID': 9999}]}))

        self.assertEqual(processor.review_one(), {84669: 'would_update'})
        self.assertEqual(shoko.calls, [('GET',
            '/api/v3/ReverseTree/File/42/Episode?includeXRefs=true&includeDataFrom=AniDB')])
        self.assertEqual([series for series, _ in catalogue.calls], [1083])
        self.assertEqual(processor.pending_ids(), (42,))
        with catalogue.calls[0][1]() as may_write:
            self.assertFalse(may_write)

    def test_early_unrecognized_file_remains_pending_without_status_review(self):
        shoko = Shoko([])
        catalogue = Catalogue()
        processor = ShokoFileSignalProcessor(shoko, catalogue)
        processor.ingest(frame('file:hashed', {'FileID': 42}))

        self.assertIsNone(processor.review_one())
        self.assertEqual(processor.pending_ids(), (42,))
        self.assertEqual(catalogue.calls, [])

    def test_unrecognized_first_file_does_not_starve_later_recognized_file(self):
        shoko = Shoko({42: [], 43: [{
            'IDs': {'ID': 100, 'ParentSeries': 1084},
            'AniDB': {'Type': 'Episode'}}]})
        catalogue = Catalogue({84670: 'unchanged'})
        processor = ShokoFileSignalProcessor(shoko, catalogue)
        processor.ingest(frame('file:hashed', {'FileID': 42})
                         + frame('file:hashed', {'FileID': 43}))

        self.assertIsNone(processor.review_one())
        self.assertEqual(processor.pending_ids(), (43, 42))
        self.assertEqual(processor.review_one(), {84670: 'unchanged'})
        self.assertEqual(processor.pending_ids(), (42,))

    def test_held_file_waits_for_retry_but_new_signal_wakes_it(self):
        moment = [100.0]
        shoko = Shoko([])
        processor = ShokoFileSignalProcessor(
            shoko, Catalogue(), now=lambda: moment[0], retry_seconds=60)
        notice = frame('file:hashed', {'FileID': 42})
        processor.ingest(notice)

        self.assertIsNone(processor.review_one())
        self.assertEqual(len(shoko.calls), 1)
        self.assertIsNone(processor.review_one())
        self.assertEqual(len(shoko.calls), 1)
        moment[0] = 159.0
        self.assertIsNone(processor.review_one())
        self.assertEqual(len(shoko.calls), 1)
        processor.ingest(frame('file:relocated', {'FileID': 42}))
        self.assertIsNone(processor.review_one())
        self.assertEqual(len(shoko.calls), 2)
        moment[0] = 219.0
        self.assertIsNone(processor.review_one())
        self.assertEqual(len(shoko.calls), 3)

    def test_unrecognized_file_retries_taper_but_new_signal_wakes_it(self):
        moment = [100.0]
        shoko = Shoko([])
        processor = ShokoFileSignalProcessor(
            shoko, Catalogue(), now=lambda: moment[0], retry_seconds=60)
        processor.ingest(frame('file:hashed', {'FileID': 42}))

        for at, expected_calls in ((100, 1), (160, 2), (219, 2),
                                   (280, 3), (519, 3), (520, 4),
                                   (1000, 5), (1899, 5), (1900, 6),
                                   (2799, 6), (2800, 7)):
            moment[0] = float(at)
            self.assertIsNone(processor.review_one())
            self.assertEqual(len(shoko.calls), expected_calls)

        moment[0] = 2801.0
        processor.ingest(frame('file:relocated', {'FileID': 42}))
        self.assertIsNone(processor.review_one())
        self.assertEqual(len(shoko.calls), 8)
        moment[0] = 2860.0
        self.assertIsNone(processor.review_one())
        self.assertEqual(len(shoko.calls), 8)
        moment[0] = 2861.0
        self.assertIsNone(processor.review_one())
        self.assertEqual(len(shoko.calls), 9)

    def test_duplicate_notices_coalesce_and_delete_cancels_pending_file(self):
        processor = ShokoFileSignalProcessor(Shoko([]), Catalogue())
        processor.ingest(frame('file:detected', {
            'RelativePath': 'new.mkv', 'ManagedFolderID': 7})
                         + frame('file:hashed', {'FileID': 42}))
        self.assertEqual(processor.pending_ids(), (42,))
        processor.ingest(frame('file:deleted', {'FileID': 42}))
        self.assertEqual(processor.pending_ids(), ())

    def test_special_only_file_does_not_assert_a_regular_season(self):
        shoko = Shoko([{'IDs': {'ID': 99, 'ParentSeries': 1083},
                        'AniDB': {'Type': 'Special'}}])
        catalogue = Catalogue()
        processor = ShokoFileSignalProcessor(shoko, catalogue)
        processor.ingest(frame('file:relocated', {'FileID': 42}))

        self.assertIsNone(processor.review_one())
        self.assertEqual(catalogue.calls, [])

    def test_special_movie_file_wakes_only_its_exact_movie(self):
        shoko = Shoko([{'IDs': {'ID': 99, 'ParentSeries': 1083,
                                'TMDB': {'Movie': [1418665]}},
                        'AniDB': {'Type': 'Special'}}])
        movies = MovieCatalogue()
        processor = ShokoFileSignalProcessor(
            shoko, Catalogue(), movie_catalogue=movies)
        processor.ingest(frame('file:hashed', {'FileID': 42}))
        self.assertEqual(processor.review_one(),
                         {'movie:1418665': 'would_available'})
        self.assertEqual(movies.calls, [1418665])
        self.assertEqual(processor.pending_ids(), (42,))

    def test_hidden_movie_queue_uses_same_exact_file_signal_without_seerr_writer(self):
        shoko = Shoko([{'IDs': {'ID': 99, 'ParentSeries': 1083,
                                'TMDB': {'Movie': [1418665]}},
                        'AniDB': {'Type': 'Special'}}])
        class MovieQueue:
            def __init__(self):
                self.ids = []
            def enqueue(self, tmdb_id):
                self.ids.append(tmdb_id)
        queue = MovieQueue()
        processor = ShokoFileSignalProcessor(
            shoko, Catalogue(), movie_link_queue=queue)
        processor.ingest(frame('file:hashed', {'FileID': 42}))
        self.assertEqual(processor.review_one(), {})
        self.assertEqual(queue.ids, [1418665])
        self.assertEqual(processor.pending_ids(), ())

    def test_report_only_file_feed_refuses_write_enabled_movie_catalogue(self):
        movies = MovieCatalogue(outcome='available')
        movies.reconciler = SimpleNamespace(write_enabled=True)
        with self.assertRaisesRegex(ValueError, 'movie write guard'):
            ShokoFileSignalProcessor(
                Shoko([]), Catalogue(), movie_catalogue=movies)

    def test_private_movie_write_requires_explicit_guard_per_event(self):
        @contextmanager
        def allow_movie_write():
            yield True

        @contextmanager
        def deny_movie_write():
            yield False

        shoko = Shoko([{'IDs': {'ID': 99, 'ParentSeries': 1083,
                                'TMDB': {'Movie': [1418665]}},
                        'AniDB': {'Type': 'Special'}}])
        movies = MovieCatalogue(outcome='available')
        movies.reconciler = SimpleNamespace(write_enabled=True)
        denied = ShokoFileSignalProcessor(
            shoko, Catalogue(), movie_catalogue=movies,
            before_movie_write=deny_movie_write)
        denied.ingest(frame('file:hashed', {'FileID': 42}))
        self.assertIsNone(denied.review_one())
        self.assertEqual(movies.calls, [])
        self.assertEqual(denied.pending_ids(), (42,))

        allowed = ShokoFileSignalProcessor(
            shoko, Catalogue(), movie_catalogue=movies,
            before_movie_write=allow_movie_write)
        allowed.ingest(frame('file:hashed', {'FileID': 42}))
        self.assertEqual(allowed.review_one(),
                         {'movie:1418665': 'available'})
        self.assertEqual(movies.calls, [1418665])
        self.assertEqual(allowed.pending_ids(), ())

    def test_movie_write_rechecks_file_binding_after_evidence_review(self):
        @contextmanager
        def allow_movie_write():
            yield True

        initial = [{'IDs': {'ID': 99, 'ParentSeries': 1083,
                            'TMDB': {'Movie': [1418665]}},
                    'AniDB': {'Type': 'Special'}}]
        changed = [{'IDs': {'ID': 100, 'ParentSeries': 1083,
                            'TMDB': {'Movie': [1418665]}},
                    'AniDB': {'Type': 'Special'}}]
        shoko = Shoko(initial)

        class RelinkingMovieCatalogue(MovieCatalogue):
            def __init__(self):
                super().__init__(outcome='available')
                self.reconciler = SimpleNamespace(write_enabled=True)
                self.writes = 0

            def run_for_tmdb_id(self, tmdb_id, *, before_write=None):
                shoko.episodes = changed
                if before_write is None:
                    self.writes += 1
                    return 'available'
                with before_write() as current:
                    if not current:
                        return 'held'
                    self.writes += 1
                    return 'available'

        movies = RelinkingMovieCatalogue()
        processor = ShokoFileSignalProcessor(
            shoko, Catalogue(), movie_catalogue=movies,
            before_movie_write=allow_movie_write)
        processor.ingest(frame('file:hashed', {'FileID': 42}))
        self.assertIsNone(processor.review_one())
        self.assertEqual(movies.writes, 0)
        self.assertEqual(processor.pending_ids(), (42,))

    def test_conflicting_movie_ids_do_not_wake_either_film(self):
        shoko = Shoko([{'IDs': {'ID': 99, 'ParentSeries': 1083,
                                'TMDB': {'Movie': [1418665, 1515151]}},
                        'AniDB': {'Type': 'Special'}}])
        movies = MovieCatalogue()
        processor = ShokoFileSignalProcessor(
            shoko, Catalogue(), movie_catalogue=movies)
        processor.ingest(frame('file:hashed', {'FileID': 42}))
        self.assertIsNone(processor.review_one())
        self.assertEqual(movies.calls, [])

    def test_private_file_wakeup_cannot_pass_the_database_write_boundary(self):
        class GuardedCatalogue(Catalogue):
            def run_for_shoko_series_id(self, series_id, *, before_write=None):
                with before_write() as may_write:
                    if may_write:
                        raise AssertionError('private feed was allowed to write')
                return {84669: 'held'}

        shoko = Shoko([{'IDs': {'ID': 99, 'ParentSeries': 1083},
                        'AniDB': {'Type': 'Episode'}}])
        processor = ShokoFileSignalProcessor(shoko, GuardedCatalogue())
        processor.ingest(frame('file:relocated', {'FileID': 42}))

        self.assertIsNone(processor.review_one())
        self.assertEqual(processor.pending_ids(), (42,))

    def test_private_write_capable_review_retires_only_committed_result(self):
        @contextmanager
        def private_guard():
            yield True

        class GuardedCatalogue(Catalogue):
            def run_for_shoko_series_id(self, series_id, *, before_write=None):
                with before_write() as may_write:
                    return {84669: 'updated' if may_write else 'would_update'}

        episodes = [{'IDs': {'ID': 99, 'ParentSeries': 1083},
                     'AniDB': {'Type': 'Episode'}}]
        report = ShokoFileSignalProcessor(Shoko(episodes), GuardedCatalogue())
        report.ingest(frame('file:hashed', {'FileID': 42}))
        self.assertEqual(report.review_one(), {84669: 'would_update'})
        self.assertEqual(report.pending_ids(), (42,))

        private = ShokoFileSignalProcessor(
            Shoko(episodes), GuardedCatalogue(), before_write=private_guard)
        private.ingest(frame('file:hashed', {'FileID': 42}))
        self.assertEqual(private.review_one(), {84669: 'updated'})
        self.assertEqual(private.pending_ids(), ())

        class NativeCatalogue(GuardedCatalogue):
            reconciler = type('Reconciler', (), {
                'database': type('Database', (), {'write_enabled': True})(),
                'native_write_guarded': True})()

        native = ShokoFileSignalProcessor(Shoko(episodes), NativeCatalogue())
        native.ingest(frame('file:hashed', {'FileID': 42}))
        self.assertEqual(native.review_one(), {84669: 'updated'})
        self.assertEqual(native.pending_ids(), ())

    def test_private_write_holds_if_shoko_relinks_file_during_review(self):
        @contextmanager
        def private_guard():
            yield True

        shoko = Shoko([{'IDs': {'ID': 99, 'ParentSeries': 1083},
                        'AniDB': {'Type': 'Episode'}}])

        class RelinkingCatalogue(Catalogue):
            def run_for_shoko_series_id(self, series_id, *, before_write=None):
                shoko.episodes = [{'IDs': {'ID': 100, 'ParentSeries': 1084},
                                   'AniDB': {'Type': 'Episode'}}]
                with before_write() as may_write:
                    return {84669: 'updated' if may_write else 'held'}

        processor = ShokoFileSignalProcessor(
            shoko, RelinkingCatalogue(), before_write=private_guard)
        processor.ingest(frame('file:hashed', {'FileID': 42}))
        self.assertIsNone(processor.review_one())
        self.assertEqual(processor.pending_ids(), (42,))

    def test_unchanged_review_keeps_file_pending_after_shoko_relink(self):
        shoko = Shoko([{'IDs': {'ID': 99, 'ParentSeries': 1083},
                        'AniDB': {'Type': 'Episode'}}])

        class RelinkingCatalogue(Catalogue):
            def run_for_shoko_series_id(self, series_id, *, before_write=None):
                shoko.episodes = [{'IDs': {'ID': 100, 'ParentSeries': 1084},
                                   'AniDB': {'Type': 'Episode'}}]
                return {84669: 'unchanged'}

        processor = ShokoFileSignalProcessor(shoko, RelinkingCatalogue())
        processor.ingest(frame('file:hashed', {'FileID': 42}))
        self.assertIsNone(processor.review_one())
        self.assertEqual(processor.pending_ids(), (42,))

    def test_disconnect_catchup_is_bounded_and_report_only(self):
        catalogue = Catalogue()
        processor = ShokoFileSignalProcessor(Shoko([]), catalogue)

        self.assertEqual(processor.catch_up_once(),
                         {'eligible': 1, 'examined': 1})
        with catalogue.calls[0][1]() as may_write:
            self.assertFalse(may_write)

    def test_pending_file_survives_private_processor_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = JsonSessionJournal(Path(directory) / 'file-events.json')
            first = ShokoFileSignalProcessor(Shoko([]), Catalogue(), journal=journal)
            first.ingest(frame('file:hashed', {'FileID': 42}))
            self.assertEqual(first.pending_ids(), (42,))

            restarted = ShokoFileSignalProcessor(Shoko([]), Catalogue(), journal=journal)
            self.assertEqual(restarted.pending_ids(), (42,))
            restarted.ingest(frame('file:deleted', {'FileID': 42}))
            self.assertEqual(ShokoFileSignalProcessor(
                Shoko([]), Catalogue(), journal=journal).pending_ids(), ())

    def test_completed_review_clears_private_pending_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = JsonSessionJournal(Path(directory) / 'file-events.json')
            shoko = Shoko([{'IDs': {'ID': 99, 'ParentSeries': 1083},
                            'AniDB': {'Type': 'Episode'}}])
            catalogue = Catalogue({84669: 'unchanged'})
            processor = ShokoFileSignalProcessor(shoko, catalogue, journal=journal)
            processor.ingest(frame('file:hashed', {'FileID': 42}))
            self.assertEqual(processor.review_one(), {84669: 'unchanged'})
            self.assertEqual(ShokoFileSignalProcessor(
                shoko, catalogue, journal=journal).pending_ids(), ())

    def test_corrupt_private_pending_journal_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = JsonSessionJournal(Path(directory) / 'file-events.json')
            journal.save({'version': 1, 'pending_file_ids': [42, 42]})
            with self.assertRaises(ValueError):
                ShokoFileSignalProcessor(Shoko([]), Catalogue(), journal=journal)

    def test_boolean_pending_journal_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = JsonSessionJournal(Path(directory) / 'file-events.json')
            journal.save({'version': True, 'pending_file_ids': [42]})
            with self.assertRaises(ValueError):
                ShokoFileSignalProcessor(Shoko([]), Catalogue(), journal=journal)

    def test_failed_journal_save_never_accepts_unrecorded_event(self):
        class UnwritableJournal:
            def load(self):
                return {}

            def save(self, value):
                raise OSError('private journal unavailable')

        processor = ShokoFileSignalProcessor(
            Shoko([]), Catalogue(), journal=UnwritableJournal())
        with self.assertRaises(OSError):
            processor.ingest(frame('file:hashed', {'FileID': 42}))
        self.assertEqual(processor.pending_ids(), ())

    def test_failed_completion_save_retains_file_for_retry(self):
        class FailingCompletionJournal:
            def load(self):
                return {'version': 1, 'pending_file_ids': [42]}

            def save(self, value):
                raise OSError('private journal unavailable')

        shoko = Shoko([{'IDs': {'ID': 99, 'ParentSeries': 1083},
                        'AniDB': {'Type': 'Episode'}}])
        processor = ShokoFileSignalProcessor(
            shoko, Catalogue({84669: 'unchanged'}),
            journal=FailingCompletionJournal())
        with self.assertRaises(OSError):
            processor.review_one()
        self.assertEqual(processor.pending_ids(), (42,))

    def test_exact_catchup_ids_enter_same_durable_pending_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = JsonSessionJournal(Path(directory) / 'file-events.json')
            processor = ShokoFileSignalProcessor(
                Shoko([]), Catalogue(), journal=journal, max_pending=2)
            processor.enqueue_exact_file_ids((42, 43, 42))
            self.assertEqual(processor.pending_ids(), (42, 43))
            self.assertEqual(ShokoFileSignalProcessor(
                Shoko([]), Catalogue(), journal=journal,
                max_pending=2).pending_ids(), (42, 43))
            with self.assertRaises(ValueError):
                processor.enqueue_exact_file_ids((44,))
            self.assertEqual(processor.pending_ids(), (42, 43))

    def test_new_notice_during_review_is_not_dropped_by_old_result(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingCatalogue(Catalogue):
            def run_for_shoko_series_id(self, series_id, *, before_write=None):
                entered.set()
                if not release.wait(2):
                    raise AssertionError('review was not released')
                return {84669: 'unchanged'}

        shoko = Shoko([{'IDs': {'ID': 99, 'ParentSeries': 1083},
                        'AniDB': {'Type': 'Episode'}}])
        processor = ShokoFileSignalProcessor(shoko, BlockingCatalogue())
        processor.ingest(frame('file:hashed', {'FileID': 42}))
        failures = []
        worker = threading.Thread(target=lambda: self._review_in_thread(
            processor, failures))
        worker.start()
        self.assertTrue(entered.wait(2))
        processor.ingest(frame('file:relocated', {'FileID': 42}))
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(processor.pending_ids(), (42,))

    def test_delete_during_held_review_does_not_resurrect_file(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingCatalogue(Catalogue):
            def run_for_shoko_series_id(self, series_id, *, before_write=None):
                entered.set()
                if not release.wait(2):
                    raise AssertionError('review was not released')
                return {84669: 'would_update'}

        shoko = Shoko([{'IDs': {'ID': 99, 'ParentSeries': 1083},
                        'AniDB': {'Type': 'Episode'}}])
        processor = ShokoFileSignalProcessor(shoko, BlockingCatalogue())
        processor.ingest(frame('file:hashed', {'FileID': 42}))
        failures = []
        worker = threading.Thread(target=lambda: self._review_in_thread(
            processor, failures))
        worker.start()
        self.assertTrue(entered.wait(2))
        processor.ingest(frame('file:deleted', {'FileID': 42}))
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(processor.pending_ids(), ())

    def test_shoko_read_failure_is_backed_off_not_tight_retried(self):
        moment = [100.0]

        class FailingShoko:
            def __init__(self):
                self.calls = 0

            def request_json(self, method, path):
                self.calls += 1
                raise OSError('temporary Shoko outage')

        shoko = FailingShoko()
        processor = ShokoFileSignalProcessor(
            shoko, Catalogue(), now=lambda: moment[0], retry_seconds=60)
        processor.ingest(frame('file:hashed', {'FileID': 42}))
        with self.assertRaises(OSError):
            processor.review_one()
        self.assertIsNone(processor.review_one())
        self.assertEqual(shoko.calls, 1)
        moment[0] = 160.0
        with self.assertRaises(OSError):
            processor.review_one()
        self.assertEqual(shoko.calls, 2)

    @staticmethod
    def _review_in_thread(processor, failures):
        try:
            processor.review_one()
        except Exception as exc:
            failures.append(exc)
