"""Queue bounded rechecks of owned native-TMDB links after Shoko remaps.

This does not infer identity or alter a link. The existing exact-file worker
re-proves each queued file and records a withdrawal before a changed slot is
published. A failed enqueue leaves the durable cursor unchanged.
"""

from __future__ import annotations

import time
from typing import Any


class OwnedMappingAudit:
    def __init__(self, store: Any, events: Any, journal: Any, *,
                 batch_size: int = 100, interval_seconds: int = 600,
                 clock: Any = time.time) -> None:
        if (not callable(getattr(store, 'owned_file_ids_after', None))
                or not callable(getattr(events, 'enqueue_exact_file_ids', None))
                or not callable(getattr(journal, 'load', None))
                or not callable(getattr(journal, 'save', None))
                or type(batch_size) is not int or not 1 <= batch_size <= 100
                or type(interval_seconds) is not int
                or not 60 <= interval_seconds <= 86400
                or not callable(clock)):
            raise ValueError('invalid native mapping audit boundary')
        self.store = store
        self.events = events
        self.journal = journal
        self.batch_size = batch_size
        self.interval_seconds = interval_seconds
        self.clock = clock
        self._next_due = 0.0
        self._state()

    def _state(self) -> int:
        saved = self.journal.load()
        if saved == {}:
            path = getattr(self.journal, 'path', None)
            if path is not None and not path.exists():
                return 0
        if (not isinstance(saved, dict)
                or set(saved) != {'version', 'last_file_id'}
                or saved['version'] != 1
                or type(saved['last_file_id']) is not int
                or saved['last_file_id'] < 0):
            raise ValueError('native mapping audit cursor is invalid')
        return saved['last_file_id']

    def run_once(self) -> tuple[int, ...] | None:
        now = self.clock()
        if now < self._next_due:
            return None
        try:
            previous = self._state()
            ids = self.store.owned_file_ids_after(previous, self.batch_size)
            if not ids and previous:
                ids = self.store.owned_file_ids_after(0, self.batch_size)
            if (not isinstance(ids, tuple) or len(ids) > self.batch_size
                    or any(type(item) is not int or item <= 0 for item in ids)
                    or any(left >= right for left, right in zip(ids, ids[1:]))):
                raise ValueError('owned mapping file census is incomplete')
            if ids:
                self.events.enqueue_exact_file_ids(ids)
                self.journal.save({'version': 1, 'last_file_id': ids[-1]})
            elif previous:
                self.journal.save({'version': 1, 'last_file_id': 0})
        except (ValueError, OSError, RuntimeError):
            self._next_due = now + 60
            raise
        self._next_due = now + self.interval_seconds
        return ids
