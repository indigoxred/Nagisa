"""Bounded recovery for owned links whose Shoko deletion notice was missed.

The census reads Shoko's file-ID index, never media contents. Absence only
queues the ordinary exact deletion verifier; it cannot remove a link itself.
"""

from __future__ import annotations

import time
from typing import Any


class OwnedDeletionAudit:
    def __init__(self, census: Any, tv_store: Any, tv_events: Any,
                 movie_store: Any = None, movie_events: Any = None, *,
                 interval_seconds: int = 21600,
                 retry_seconds: int = 300,
                 clock: Any = time.time) -> None:
        if (not callable(getattr(census, 'read_ids', None))
                or not callable(getattr(tv_store, 'manifest', None))
                or not callable(getattr(tv_events,
                                        'enqueue_deleted_file_ids', None))
                or ((movie_store is None) != (movie_events is None))
                or (movie_store is not None
                    and (not callable(getattr(movie_store, 'manifest', None))
                         or not callable(getattr(
                             movie_events, 'enqueue_file_deletion', None))))
                or type(interval_seconds) is not int
                or not 3600 <= interval_seconds <= 86400
                or type(retry_seconds) is not int
                or not 60 <= retry_seconds <= 3600
                or not callable(clock)):
            raise ValueError('invalid owned-link deletion audit')
        self.census = census
        self.tv_store = tv_store
        self.tv_events = tv_events
        self.movie_store = movie_store
        self.movie_events = movie_events
        self.interval_seconds = interval_seconds
        self.retry_seconds = retry_seconds
        self.clock = clock
        self._next_due = 0.0

    @staticmethod
    def _owned_tv_ids(manifest: Any) -> set[int]:
        links = manifest.get('links') if isinstance(manifest, dict) else None
        if not isinstance(links, dict):
            raise ValueError('TV owned-link manifest is invalid')
        result = set()
        for record in links.values():
            file_id = record.get('file_id') if isinstance(record, dict) else None
            if type(file_id) is not int or file_id <= 0:
                raise ValueError('TV owned file ID is invalid')
            result.add(file_id)
        return result

    @staticmethod
    def _owned_movie_ids(manifest: Any) -> set[int]:
        if not isinstance(manifest, dict):
            raise ValueError('Movie owned-link manifest is invalid')
        result = set()
        for record in manifest.values():
            if not isinstance(record, dict):
                raise ValueError('Movie owned-link record is invalid')
            for plan in (record.get('desired'), record.get('previous')):
                if plan is None:
                    continue
                links = plan.get('links') if isinstance(plan, dict) else None
                if not isinstance(links, list):
                    raise ValueError('Movie owned-link plan is invalid')
                for link in links:
                    file_id = link.get('file_id') if isinstance(link, dict) else None
                    if type(file_id) is not int or file_id <= 0:
                        raise ValueError('Movie owned file ID is invalid')
                    result.add(file_id)
        return result

    def run_once(self) -> tuple[int, int] | None:
        now = self.clock()
        if now < self._next_due:
            return None
        try:
            current = self.census.read_ids()
            if (not isinstance(current, tuple) or len(current) > 32768
                    or any(type(item) is not int or item <= 0
                           for item in current)
                    or any(left >= right for left, right in
                           zip(current, current[1:]))):
                raise ValueError('Shoko owned-link census is incomplete')
            current_ids = set(current)
            tv_missing = sorted(
                self._owned_tv_ids(self.tv_store.manifest()) - current_ids)
            movie_missing = (sorted(self._owned_movie_ids(
                self.movie_store.manifest()) - current_ids)
                if self.movie_store is not None else [])
            # A failed enqueue leaves the audit due for a retry. Already
            # queued IDs coalesce in the durable event journals.
            for start in range(0, len(tv_missing), 100):
                self.tv_events.enqueue_deleted_file_ids(
                    tuple(tv_missing[start:start + 100]))
            for file_id in movie_missing:
                self.movie_events.enqueue_file_deletion(file_id)
        except (ValueError, OSError, RuntimeError):
            self._next_due = now + self.retry_seconds
            raise
        self._next_due = now + self.interval_seconds
        return len(tv_missing), len(movie_missing)
