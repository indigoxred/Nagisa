"""Durably recheck exact Shoko files when a series mapping changes.

This queues the existing guarded file worker. A series event cannot itself
create or withdraw a private Plex link, and no media directory is scanned.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any


_METADATA_TARGETS = {
    'links.changed',
    'series.added', 'series.updated', 'series.removed',
    'episode.added', 'episode.updated', 'episode.removed',
}


def parse_metadata_series_frames(data: str) -> tuple[int, ...]:
    """Accept only affected Shoko series IDs from pinned metadata events."""
    if not isinstance(data, str) or len(data) > 65536 or not data.endswith('\x1e'):
        raise ValueError('incomplete or excessive Shoko metadata frame')
    series: set[int] = set()
    for raw in data[:-1].split('\x1e'):
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError('malformed Shoko metadata frame') from None
        if not isinstance(message, dict) or message.get('type') != 1:
            continue
        target = message.get('target')
        if (not isinstance(target, str) or not target.startswith('metadata:')
                or target.removeprefix('metadata:') not in _METADATA_TARGETS):
            continue
        args = message.get('arguments')
        payload = args[0] if isinstance(args, list) and len(args) == 1 else None
        ids = payload.get('ShokoSeriesIDs') if isinstance(payload, dict) else None
        if (not isinstance(ids, list) or len(ids) > 100
                or any(type(item) is not int or item <= 0 for item in ids)):
            raise ValueError('Shoko metadata event lacks exact series IDs')
        series.update(ids)
        if len(series) > 100:
            raise ValueError('Shoko metadata event exceeds series bound')
    return tuple(sorted(series))


class ScopedSeriesRecheck:
    def __init__(self, shoko: Any, events: Any, journal: Any, *,
                 batch_size: int = 25) -> None:
        if (not callable(getattr(shoko, 'request_json', None))
                or not callable(getattr(events, 'enqueue_exact_file_ids', None))
                or not callable(getattr(journal, 'load', None))
                or not callable(getattr(journal, 'save', None))
                or type(batch_size) is not int or not 1 <= batch_size <= 100):
            raise ValueError('invalid series recheck boundary')
        self.shoko = shoko
        self.events = events
        self.journal = journal
        self.batch_size = batch_size
        self._lock = threading.RLock()
        self._series, self._pending = self._load()
        self._retry_after: dict[int, float] = {}

    def _load(self) -> tuple[list[int], list[int]]:
        saved = self.journal.load()
        if saved == {}:
            return [], []
        if not isinstance(saved, dict) or set(saved) != {
                'version', 'series_ids', 'pending_file_ids'} or saved['version'] != 1:
            raise ValueError('series recheck journal changed')
        series, pending = saved['series_ids'], saved['pending_file_ids']
        if (not isinstance(series, list) or len(series) > 1000
                or any(type(item) is not int or item <= 0 for item in series)
                or len(set(series)) != len(series)
                or not isinstance(pending, list) or len(pending) > 32768
                or any(type(item) is not int or item <= 0 for item in pending)
                or any(left >= right for left, right in
                       zip(pending, pending[1:]))):
            raise ValueError('series recheck journal is invalid')
        return list(series), list(pending)

    def _save(self, series: list[int], pending: list[int]) -> None:
        self.journal.save({'version': 1, 'series_ids': series,
                           'pending_file_ids': pending})

    def enqueue_series_ids(self, ids: tuple[int, ...]) -> None:
        if (not isinstance(ids, tuple) or len(ids) > 100
                or any(type(item) is not int or item <= 0 for item in ids)):
            raise ValueError('bounded exact Shoko series IDs required')
        with self._lock:
            series = list(self._series)
            for item in ids:
                if item not in series:
                    series.append(item)
            if len(series) > 1000:
                raise ValueError('series recheck queue full')
            if series != self._series:
                self._save(series, self._pending)
                self._series = series

    def _file_ids(self, series_id: int) -> list[int]:
        reply = self.shoko.request_json(
            'GET', f'/api/v3/Series/{series_id}/Episode?'
                   'page=1&pageSize=0&includeFiles=true')
        rows = reply.get('List') if isinstance(reply, dict) else None
        if (not isinstance(rows, list) or len(rows) > 10000
                or type(reply.get('Total')) is not int
                or reply['Total'] != len(rows)):
            raise ValueError('Shoko series episode census is incomplete')
        ids: set[int] = set()
        for row in rows:
            episode = row.get('IDs') if isinstance(row, dict) else None
            files = row.get('Files') if isinstance(row, dict) else None
            if (not isinstance(episode, dict)
                    or episode.get('ParentSeries') != series_id
                    or not isinstance(files, list) or len(files) > 50):
                raise ValueError('Shoko series file identity is ambiguous')
            for file in files:
                file_id = file.get('ID') if isinstance(file, dict) else None
                if type(file_id) is not int or file_id <= 0:
                    raise ValueError('Shoko series has invalid file ID')
                ids.add(file_id)
                if len(ids) > 32768:
                    raise ValueError('Shoko series recheck exceeds bound')
        return sorted(ids)

    def run_once(self) -> tuple[int, ...]:
        with self._lock:
            if not self._pending:
                if not self._series:
                    return ()
                series_id = self._series[0]
                if time.time() < self._retry_after.get(series_id, 0):
                    if len(self._series) > 1:
                        rotated = self._series[1:] + [series_id]
                        self._save(rotated, [])
                        self._series = rotated
                    return ()
                try:
                    ids = self._file_ids(series_id)
                except (ValueError, OSError) as error:
                    # Keep a failed series durable, but let later series
                    # progress. A restart may try it once immediately.
                    self._retry_after[series_id] = time.time() + 300
                    if len(self._series) > 1:
                        rotated = self._series[1:] + [series_id]
                        self._save(rotated, [])
                        self._series = rotated
                    logging.getLogger(__name__).warning(
                        'Shoko series %s recheck held: %s',
                        series_id, type(error).__name__)
                    return ()
                self._retry_after.pop(series_id, None)
                remaining = self._series[1:]
                # Save the exact file list before acknowledging the series.
                self._save(remaining, ids)
                self._series, self._pending = remaining, ids
            batch = tuple(self._pending[:self.batch_size])
            if not batch:
                return ()
            # The existing worker's queue is durable. A crash before our next
            # save can replay these IDs harmlessly rather than losing them.
            self.events.enqueue_exact_file_ids(batch)
            remaining = self._pending[len(batch):]
            self._save(self._series, remaining)
            self._pending = remaining
            return batch


class OwnedSeriesAudit:
    """Slow bounded fallback when Shoko changes ordering without a notice."""

    def __init__(self, store: Any, recheck: ScopedSeriesRecheck,
                 journal: Any, *, batch_size: int = 10,
                 interval_seconds: int = 600, clock: Any = time.time) -> None:
        if (not callable(getattr(store, 'owned_series_ids_after', None))
                or not callable(getattr(recheck, 'enqueue_series_ids', None))
                or not callable(getattr(journal, 'load', None))
                or not callable(getattr(journal, 'save', None))
                or type(batch_size) is not int or not 1 <= batch_size <= 25
                or type(interval_seconds) is not int
                or not 60 <= interval_seconds <= 86400
                or not callable(clock)):
            raise ValueError('invalid owned series audit boundary')
        self.store = store
        self.recheck = recheck
        self.journal = journal
        self.batch_size = batch_size
        self.interval_seconds = interval_seconds
        self.clock = clock
        self._next_due = 0.0
        self._cursor()

    def _cursor(self) -> int:
        saved = self.journal.load()
        if saved == {}:
            return 0
        if (not isinstance(saved, dict)
                or set(saved) != {'version', 'last_series_id'}
                or saved['version'] != 1
                or type(saved['last_series_id']) is not int
                or saved['last_series_id'] < 0):
            raise ValueError('owned series audit cursor is invalid')
        return saved['last_series_id']

    def run_once(self) -> tuple[int, ...] | None:
        now = self.clock()
        if now < self._next_due:
            return None
        try:
            last = self._cursor()
            ids = self.store.owned_series_ids_after(last, self.batch_size)
            if not ids and last:
                ids = self.store.owned_series_ids_after(0, self.batch_size)
            if (not isinstance(ids, tuple) or len(ids) > self.batch_size
                    or any(type(item) is not int or item <= 0 for item in ids)
                    or any(a >= b for a, b in zip(ids, ids[1:]))):
                raise ValueError('owned series page is incomplete')
            if ids:
                self.recheck.enqueue_series_ids(ids)
                self.journal.save({'version': 1, 'last_series_id': ids[-1]})
            elif last:
                self.journal.save({'version': 1, 'last_series_id': 0})
        except (ValueError, OSError, RuntimeError):
            self._next_due = now + 60
            raise
        self._next_due = now + self.interval_seconds
        return ids
