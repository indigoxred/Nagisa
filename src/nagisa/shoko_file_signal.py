"""Private Shoko file-event wake-up adapter; never an availability verdict."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import json
import math
import threading
import time
from typing import Any


_SUBJECTS = {'detected', 'hashed', 'relocated', 'deleted'}


def parse_signalr_frames(data: str) -> list[tuple[str, int]]:
    """Read bounded SignalR JSON messages, ignoring non-file protocol frames."""
    if not isinstance(data, str) or len(data) > 65536 or not data.endswith('\x1e'):
        raise ValueError('incomplete or excessive Shoko signal frame')
    events = []
    for raw in data[:-1].split('\x1e'):
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError('malformed Shoko signal frame') from None
        if not isinstance(message, dict) or message.get('type') != 1:
            continue
        target = message.get('target')
        if not isinstance(target, str) or not target.startswith('file:'):
            continue
        subject = target.removeprefix('file:')
        if subject not in _SUBJECTS:
            continue
        args = message.get('arguments')
        if subject == 'detected':
            # Shoko emits only path and managed-folder ID at detection time.
            # Neither is a stable file identity; wait for hashed/relocated.
            if (not isinstance(args, list) or len(args) != 1
                    or not isinstance(args[0], dict)
                    or not isinstance(args[0].get('RelativePath'), str)
                    or not args[0]['RelativePath']
                    or type(args[0].get('ManagedFolderID')) is not int
                    or args[0]['ManagedFolderID'] <= 0):
                raise ValueError('malformed Shoko detection signal')
            continue
        if (not isinstance(args, list) or len(args) != 1
                or not isinstance(args[0], dict)
                or type(args[0].get('FileID')) is not int
                or args[0]['FileID'] <= 0):
            raise ValueError('Shoko file signal has no exact file ID')
        events.append((subject, args[0]['FileID']))
    return events


@contextmanager
def _report_only_guard():
    # The live feed and persistent binding journal have not been qualified.
    # A private wake-up may exercise evidence reads, never the DB writer.
    yield False


@contextmanager
def _native_proof_guard():
    # The event's exact Shoko binding is checked inside each write guard.
    # TV and movie reconcilers separately repeat their source proof.
    yield True


def _episode_binding(rows: Any) -> tuple[str, ...] | None:
    """Fingerprint only the episode identities used to route a file event."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        return None
    binding = []
    for row in rows:
        ids = row.get('IDs') if isinstance(row, dict) else None
        anidb = row.get('AniDB') if isinstance(row, dict) else None
        if (not isinstance(ids, dict) or not isinstance(anidb, dict)
                or type(ids.get('ID')) is not int or ids['ID'] <= 0
                or type(ids.get('ParentSeries')) is not int
                or ids['ParentSeries'] <= 0
                or anidb.get('Type') not in ('Episode', 'Special')):
            return None
        binding.append(json.dumps((ids, anidb['Type']), sort_keys=True))
    return tuple(sorted(binding))


class ShokoFileSignalProcessor:
    def __init__(self, shoko: Any, catalogue: Any, *, max_pending: int = 512,
                 retry_seconds: int = 60, now: Any = time.monotonic,
                 journal: Any = None, before_write: Any = None,
                 movie_catalogue: Any = None,
                 before_movie_write: Any = None,
                 request_retry: Any = None,
                 movie_link_queue: Any = None):
        if (not callable(getattr(shoko, 'request_json', None))
                or not callable(getattr(catalogue, 'run_for_shoko_series_id', None))
                or type(max_pending) is not int or not 1 <= max_pending <= 4096
                or type(retry_seconds) is not int or not 5 <= retry_seconds <= 3600
                or not callable(now)
                or (before_write is not None and not callable(before_write))
                or (before_movie_write is not None
                    and not callable(before_movie_write))
                or (movie_catalogue is not None and not callable(
                    getattr(movie_catalogue, 'run_for_tmdb_id', None)))
                or (request_retry is not None and not callable(
                    getattr(request_retry, 'enqueue', None)))
                or (movie_link_queue is not None and not callable(
                    getattr(movie_link_queue, 'enqueue', None)))
                or (journal is not None and
                    (not callable(getattr(journal, 'load', None)) or
                     not callable(getattr(journal, 'save', None))))):
            raise ValueError('invalid private Shoko file signal inputs')
        catalogue_reconciler = getattr(catalogue, 'reconciler', None)
        catalogue_writer = getattr(catalogue_reconciler, 'database', None)
        native_tv_write = (getattr(catalogue_writer, 'write_enabled', False)
                           is True and getattr(catalogue_reconciler,
                           'native_write_guarded', False) is True)
        movie_reconciler = getattr(movie_catalogue, 'reconciler', None)
        movie_write_enabled = getattr(movie_reconciler, 'write_enabled', False) is True
        row_provisioner = getattr(movie_catalogue, 'row_provisioner', None)
        native_row_provision = False
        if row_provisioner is not None:
            from nagisa.seerr_movie_row import MovieMediaProvisioner
            native_row_provision = (
                isinstance(row_provisioner, MovieMediaProvisioner)
                and row_provisioner.write_enabled is True
                and row_provisioner.seerr is getattr(movie_catalogue, 'seerr', None)
                and row_provisioner.seerr is getattr(movie_reconciler,
                                                     'seerr', None)
                and row_provisioner.evidence is getattr(movie_reconciler,
                                                        'evidence', None)
                and row_provisioner.expected_version == getattr(
                    movie_catalogue, 'expected_version', None)
                and row_provisioner.expected_version == getattr(
                    movie_reconciler, 'expected_version', None))
        native_movie_write = (movie_write_enabled
                              and callable(getattr(movie_reconciler,
                                                   'evidence', None))
                              and (row_provisioner is None
                                   or native_row_provision))
        if movie_write_enabled and before_movie_write is None and not native_movie_write:
            raise ValueError('Shoko file feed requires a movie write guard')
        self.shoko = shoko
        self.catalogue = catalogue
        self.movie_catalogue = movie_catalogue
        self.request_retry = request_retry
        self.movie_link_queue = movie_link_queue
        self.before_movie_write = (before_movie_write or
                                   (_native_proof_guard if native_movie_write else None))
        self._movie_write_capable = movie_write_enabled
        self.max_pending = max_pending
        self.retry_seconds = retry_seconds
        self.now = now
        self.journal = journal
        self.before_write = (before_write or
                             (_native_proof_guard if native_tv_write
                              else _report_only_guard))
        self._write_capable = before_write is not None or native_tv_write
        self._lock = threading.RLock()
        self._pending: OrderedDict[int, None] = OrderedDict()
        self._due: dict[int, float] = {}
        self._retry_count: dict[int, int] = {}
        self._generation: dict[int, int] = {}
        self._serial = 0
        self._inflight: set[int] = set()
        if journal is not None:
            saved = journal.load()
            if saved:
                ids = saved.get('pending_file_ids') if isinstance(saved, dict) else None
                if (not isinstance(saved, dict)
                        or set(saved) != {'version', 'pending_file_ids'}
                        or type(saved.get('version')) is not int
                        or saved['version'] != 1 or not isinstance(ids, list)
                        or len(ids) > max_pending
                        or any(type(item) is not int or item <= 0 for item in ids)
                        or len(set(ids)) != len(ids)):
                    raise ValueError('invalid private Shoko file signal journal')
                self._pending.update((item, None) for item in ids)
                moment = self._time()
                self._due.update((item, moment) for item in ids)
                for item in ids:
                    self._serial += 1
                    self._generation[item] = self._serial

    def _save_pending(self, pending: OrderedDict[int, None]) -> None:
        if self.journal is not None:
            self.journal.save({'version': 1,
                               'pending_file_ids': list(pending)})

    def _time(self) -> float:
        value = self.now()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('invalid Shoko signal clock')
        return float(value)

    def ingest(self, data: str) -> None:
        events = parse_signalr_frames(data)
        with self._lock:
            next_pending = self._pending.copy()
            next_due = self._due.copy()
            next_retry_count = self._retry_count.copy()
            next_generation = self._generation.copy()
            next_serial = self._serial
            for subject, file_id in events:
                next_serial += 1
                if subject == 'deleted':
                    next_pending.pop(file_id, None)
                    next_due.pop(file_id, None)
                    next_retry_count.pop(file_id, None)
                    next_generation.pop(file_id, None)
                else:
                    if file_id not in next_pending:
                        if len(next_pending) >= self.max_pending:
                            raise ValueError('Shoko file signal queue is full; catch-up required')
                        next_pending[file_id] = None
                    # A new native signal may complete recognition/location;
                    # it supersedes any earlier review of this same file.
                    next_due[file_id] = self._time()
                    next_retry_count.pop(file_id, None)
                    next_generation[file_id] = next_serial
            if tuple(next_pending) != tuple(self._pending):
                self._save_pending(next_pending)
            self._pending = next_pending
            self._due = next_due
            self._retry_count = next_retry_count
            self._generation = next_generation
            self._serial = next_serial

    def pending_ids(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._pending)

    def enqueue_exact_file_ids(self, ids: tuple[int, ...] | list[int]) -> None:
        """Durably queue exact IDs from a separately validated Shoko API read."""
        if (not isinstance(ids, (tuple, list)) or len(ids) > 4096
                or any(type(file_id) is not int or file_id <= 0
                       for file_id in ids)):
            raise ValueError('invalid exact Shoko catch-up file IDs')
        with self._lock:
            next_pending = self._pending.copy()
            next_due = self._due.copy()
            next_retry_count = self._retry_count.copy()
            next_generation = self._generation.copy()
            next_serial = self._serial
            moment = self._time()
            for file_id in ids:
                if file_id not in next_pending:
                    if len(next_pending) >= self.max_pending:
                        raise ValueError('Shoko file signal queue is full; catch-up required')
                    next_pending[file_id] = None
                next_serial += 1
                next_due[file_id] = moment
                next_retry_count.pop(file_id, None)
                next_generation[file_id] = next_serial
            if tuple(next_pending) != tuple(self._pending):
                self._save_pending(next_pending)
            self._pending = next_pending
            self._due = next_due
            self._retry_count = next_retry_count
            self._generation = next_generation
            self._serial = next_serial

    def catch_up_once(self) -> dict[str, int]:
        """Run one catalogue-limited report pass after a feed gap.

        Scheduling and durable cursor/reconnect policy are intentionally not
        claimed here; the catalogue owns its per-cycle limit.
        """
        run = getattr(self.catalogue, 'run_once', None)
        if not callable(run):
            raise ValueError('bounded catalogue catch-up is unavailable')
        return run(before_write=_report_only_guard)

    def review_one(self) -> dict[int | str, str] | None:
        with self._lock:
            if not self._pending:
                return None
            moment = self._time()
            file_id = next((candidate for candidate in self._pending
                            if candidate not in self._inflight
                            and self._due[candidate] <= moment), None)
            if file_id is None:
                return None
            generation = self._generation[file_id]
            self._inflight.add(file_id)

        def defer() -> None:
            # A file can wait for Shoko recognition or Plex visibility for a
            # while; let later arrivals get a turn during that wait.
            with self._lock:
                if (file_id not in self._pending
                        or self._generation.get(file_id) != generation):
                    return
                attempts = self._retry_count.get(file_id, 0) + 1
                self._retry_count[file_id] = attempts
                delay = min(self.retry_seconds * (1 << min(attempts - 1, 4)),
                            max(900, self.retry_seconds))
                self._due[file_id] = self._time() + delay
                self._pending.move_to_end(file_id)

        try:
            episode_path = (f'/api/v3/ReverseTree/File/{file_id}/Episode'
                            '?includeXRefs=true&includeDataFrom=AniDB')
            episodes = self.shoko.request_json('GET', episode_path)
            binding = _episode_binding(episodes)
            if binding is None:
                defer()
                return None
            owners = set()
            movie_ids = set()
            for row in episodes:
                ids = row.get('IDs') if isinstance(row, dict) else None
                anidb = row.get('AniDB') if isinstance(row, dict) else None
                if (not isinstance(ids, dict) or not isinstance(anidb, dict)
                        or type(ids.get('ID')) is not int or ids['ID'] <= 0
                        or type(ids.get('ParentSeries')) is not int
                        or ids['ParentSeries'] <= 0
                        or anidb.get('Type') not in ('Episode', 'Special')):
                    defer()
                    return None
                if anidb['Type'] == 'Episode':
                    owners.add(ids['ParentSeries'])
                if (self.movie_catalogue is not None
                        or self.movie_link_queue is not None):
                    tmdb = ids.get('TMDB')
                    if tmdb is not None:
                        movies = tmdb.get('Movie') if isinstance(tmdb, dict) else None
                        if (not isinstance(movies, list) or len(movies) > 8
                                or any(type(value) is not int or value <= 0
                                       for value in movies)):
                            defer()
                            return None
                        movie_ids.update(movies)
            if (not owners and not movie_ids) or len(owners) > 8 or len(movie_ids) > 1:
                defer()
                return None

            @contextmanager
            def write_guard():
                with self.before_write() as allowed:
                    if allowed is not True:
                        yield False
                        return
                    with self._lock:
                        current_event = (file_id in self._pending
                                         and self._generation.get(file_id)
                                         == generation)
                    if not current_event:
                        yield False
                        return
                    current = self.shoko.request_json('GET', episode_path)
                    yield _episode_binding(current) == binding

            @contextmanager
            def movie_write_guard():
                with self.before_movie_write() as allowed:
                    if allowed is not True:
                        yield False
                        return
                    with self._lock:
                        current_event = (file_id in self._pending
                                         and self._generation.get(file_id)
                                         == generation)
                    if not current_event:
                        yield False
                        return
                    current = self.shoko.request_json('GET', episode_path)
                    yield _episode_binding(current) == binding

            results = {}
            for owner in sorted(owners):
                status = self.catalogue.run_for_shoko_series_id(
                    owner, before_write=write_guard)
                if (not isinstance(status, dict) or not status
                        or any(type(tmdb_id) is not int or tmdb_id <= 0
                               or state not in ('held', 'unchanged', 'would_update', 'updated')
                               for tmdb_id, state in status.items())
                        or set(status).intersection(results)):
                    defer()
                    return None
                results.update(status)
            # A Shoko file event wakes the optional request reconciler; it
            # does not claim Plex delivery. Its own durable retry waits for
            # both libraries before changing any request state.
            if self.request_retry is not None:
                try:
                    for tmdb_id in sorted(results):
                        self.request_retry.enqueue(tmdb_id)
                except (OSError, RuntimeError, ValueError):
                    defer()
                    return None
            if self.movie_link_queue is not None:
                try:
                    for tmdb_id in sorted(movie_ids):
                        self.movie_link_queue.enqueue(tmdb_id)
                except (OSError, RuntimeError, ValueError):
                    defer()
                    return None
            for tmdb_id in sorted(movie_ids):
                if self.movie_catalogue is None:
                    continue
                if self._movie_write_capable:
                    with movie_write_guard() as may_write:
                        if may_write is not True:
                            defer()
                            return None
                        state = self.movie_catalogue.run_for_tmdb_id(
                            tmdb_id, before_write=movie_write_guard)
                else:
                    state = self.movie_catalogue.run_for_tmdb_id(tmdb_id)
                if state not in ('held', 'unchanged', 'would_available',
                                 'available', 'stock_conflict'):
                    defer()
                    return None
                results[f'movie:{tmdb_id}'] = state
            if any(state in ('held', 'stock_conflict') or
                   (state == 'updated' and not self._write_capable) or
                   (state == 'available' and not self._movie_write_capable)
                   for state in results.values()):
                defer()
                return None
            if any(state in ('would_update', 'would_available')
                   for state in results.values()):
                defer()
                return results
            if all(state == 'unchanged' or
                   (self._write_capable and state == 'updated') or
                   (self._movie_write_capable and state == 'available')
                   for state in results.values()):
                # Even a no-op review must not consume an event whose Shoko
                # episode association changed while other services were read.
                current = self.shoko.request_json('GET', episode_path)
                if _episode_binding(current) != binding:
                    defer()
                    return None
                with self._lock:
                    if (file_id in self._pending
                            and self._generation.get(file_id) == generation):
                        next_pending = self._pending.copy()
                        next_pending.pop(file_id)
                        self._save_pending(next_pending)
                        self._pending.pop(file_id)
                        self._due.pop(file_id, None)
                        self._retry_count.pop(file_id, None)
                        self._generation.pop(file_id, None)
            else:
                defer()
            return results
        except Exception:
            # Preserve the exact pending ID and avoid hammering an unhealthy
            # stock service; the caller still receives the original error.
            defer()
            raise
        finally:
            with self._lock:
                self._inflight.discard(file_id)
