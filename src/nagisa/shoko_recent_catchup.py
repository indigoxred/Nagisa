"""Private read-only candidate for bounded Shoko disconnect catch-up.

The installed Shoko response and server-side cost remain unqualified. A
caller must durably enqueue returned IDs before saving a complete watermark.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any


FileCursor = tuple[datetime, int]
_MAX_BACKLOG = 32768


@dataclass(frozen=True)
class RecentBatch:
    file_ids: tuple[int, ...]
    watermark: FileCursor | None
    complete: bool


def _import_cursor(row: Any) -> FileCursor:
    if (not isinstance(row, dict) or type(row.get('ID')) is not int
            or row['ID'] <= 0 or not isinstance(row.get('Imported'), str)):
        raise ValueError('Shoko recent file has no exact imported identity')
    try:
        timestamp = datetime.fromisoformat(row['Imported'].replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('Shoko recent file has invalid import time') from None
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError('Shoko recent file import time lacks zone')
    return timestamp.astimezone(timezone.utc), row['ID']


class ShokoRecentCatchup:
    def __init__(self, shoko: Any, *, page_size: int = 100,
                 max_pages: int = 5) -> None:
        if (not callable(getattr(shoko, 'request_json', None))
                or type(page_size) is not int or not 1 <= page_size <= 1000
                or type(max_pages) is not int or not 1 <= max_pages <= 64
                or page_size * max_pages > _MAX_BACKLOG):
            raise ValueError('invalid private Shoko recent-file settings')
        self.shoko = shoko
        self.page_size = page_size
        self.max_pages = max_pages

    def head_cursor(self) -> FileCursor:
        """Read a stable exact starting point before the listener is started.

        The first normal catch-up must still run after startup, so imports
        between these reads and the listener connection cannot be skipped.
        """
        path = ('/api/v3/File?pageSize=1&page=1&include=ImportLimbo'
                '&sortOrder=-ImportedAt,-FileID')
        observed: FileCursor | None = None
        total: int | None = None
        for _ in range(2):
            response = self.shoko.request_json('GET', path)
            if (not isinstance(response, dict)
                    or type(response.get('Total')) is not int
                    or response['Total'] <= 0
                    or not isinstance(response.get('List'), list)
                    or len(response['List']) != 1):
                raise ValueError('Shoko recent-file head is unavailable')
            current = _import_cursor(response['List'][0])
            if observed is not None and (current != observed
                                         or response['Total'] != total):
                raise ValueError('Shoko recent-file head changed during bootstrap')
            observed, total = current, response['Total']
        assert observed is not None
        return observed

    def scan_since(self, since: FileCursor) -> RecentBatch:
        if (not isinstance(since, tuple) or len(since) != 2
                or not isinstance(since[0], datetime)
                or since[0].tzinfo is None or since[0].utcoffset() is None
                or type(since[1]) is not int or since[1] <= 0):
            raise ValueError('exact prior Shoko import watermark required')
        since = since[0].astimezone(timezone.utc), since[1]
        found: list[int] = []
        seen: set[int] = set()
        newest: FileCursor | None = None
        previous: FileCursor | None = None
        total: int | None = None
        head: tuple[FileCursor, ...] | None = None
        first_path = (f'/api/v3/File?pageSize={self.page_size}&page=1'
                      '&include=ImportLimbo&sortOrder=-ImportedAt,-FileID')

        def verify_head() -> None:
            # An import and deletion can leave Total unchanged while moving
            # offset pages. Refuse to commit that inconsistent traversal.
            response = self.shoko.request_json('GET', first_path)
            if (not isinstance(response, dict)
                    or response.get('Total') != total
                    or not isinstance(response.get('List'), list)
                    or len(response['List']) != min(self.page_size, total)):
                raise ValueError('Shoko recent-file head changed during read')
            if tuple(_import_cursor(row) for row in response['List']) != head:
                raise ValueError('Shoko recent-file head changed during read')

        for page in range(1, self.max_pages + 1):
            path = (f'/api/v3/File?pageSize={self.page_size}&page={page}'
                    '&include=ImportLimbo&sortOrder=-ImportedAt,-FileID')
            response = self.shoko.request_json('GET', path)
            if (not isinstance(response, dict)
                    or type(response.get('Total')) is not int
                    or response['Total'] < 0
                    or not isinstance(response.get('List'), list)):
                raise ValueError('invalid Shoko recent-file page')
            if total is None:
                total = response['Total']
            if response['Total'] != total:
                raise ValueError('Shoko recent-file pages changed during read')
            expected = max(0, min(self.page_size,
                                  total - (page - 1) * self.page_size))
            rows = response['List']
            if len(rows) != expected:
                raise ValueError('incomplete Shoko recent-file page')
            if page == 1:
                head = tuple(_import_cursor(row) for row in rows)
            for row in rows:
                cursor = _import_cursor(row)
                if ((previous is not None and cursor >= previous)
                        or cursor[1] in seen):
                    raise ValueError('Shoko recent files are not strictly sorted')
                previous = cursor
                seen.add(cursor[1])
                if newest is None:
                    newest = cursor
                if cursor <= since:
                    if page > 1:
                        verify_head()
                    return RecentBatch(tuple(reversed(found)),
                                       max(since, newest), True)
                found.append(cursor[1])
            if page * self.page_size >= total:
                if page > 1:
                    verify_head()
                return RecentBatch(tuple(reversed(found)),
                                   max(since, newest) if newest else since,
                                   True)
        # A page limit must never silently advance a watermark past unseen IDs.
        return RecentBatch(tuple(reversed(found)), None, False)


class ShokoRecentCatchupWorker:
    """Durable ordering boundary for reconnect and low-cost periodic checks."""

    def __init__(self, reader: Any, processor: Any,
                 cursor_journal: Any, *, backlog_journal: Any = None) -> None:
        if (not callable(getattr(reader, 'scan_since', None))
                or not callable(getattr(processor, 'enqueue_exact_file_ids', None))
                or not callable(getattr(cursor_journal, 'load', None))
                or not callable(getattr(cursor_journal, 'save', None))
                or (backlog_journal is not None and
                    (not callable(getattr(backlog_journal, 'load', None))
                     or not callable(getattr(backlog_journal, 'save', None))))):
            raise ValueError('invalid private Shoko catch-up worker')
        self.reader = reader
        self.processor = processor
        self.cursor_journal = cursor_journal
        self.backlog_journal = backlog_journal
        self._lock = threading.RLock()
        self._backlog: list[int] = []
        if backlog_journal is not None:
            saved = backlog_journal.load()
            if saved:
                ids = saved.get('pending_file_ids') if isinstance(saved, dict) else None
                if (not isinstance(saved, dict)
                        or set(saved) != {'version', 'pending_file_ids'}
                        or type(saved['version']) is not int
                        or saved['version'] != 1 or not isinstance(ids, list)
                        or len(ids) > _MAX_BACKLOG
                        or any(type(item) is not int or item <= 0 for item in ids)
                        or len(set(ids)) != len(ids)):
                    raise ValueError('invalid Shoko catch-up backlog journal')
                self._backlog = list(ids)

    def _save_backlog(self, ids: list[int]) -> None:
        if self.backlog_journal is None:
            raise ValueError('Shoko catch-up backlog is unavailable')
        if len(ids) > _MAX_BACKLOG:
            raise ValueError('Shoko catch-up backlog exceeds bound')
        self.backlog_journal.save({'version': 1, 'pending_file_ids': ids})

    def backlog_count(self) -> int:
        with self._lock:
            return len(self._backlog)

    def bootstrap_before_start(self) -> FileCursor:
        """Initialize once, while the listener is stopped and journals empty.

        This deliberately excludes pre-existing files from the new event
        stream. The catalogue sweep handles existing Seerr rows separately.
        """
        with self._lock:
            if self.backlog_journal is None:
                raise ValueError('durable Shoko bootstrap backlog required')
            cursor_path = getattr(self.cursor_journal, 'path', None)
            if ((isinstance(cursor_path, Path) and cursor_path.exists())
                    or self.cursor_journal.load()):
                raise ValueError('existing Shoko import cursor cannot be replaced')
            pending = getattr(self.processor, 'pending_ids', None)
            if (not callable(pending) or pending() or self._backlog):
                raise ValueError('pending Shoko file work prevents bootstrap')
            head = getattr(self.reader, 'head_cursor', None)
            if not callable(head):
                raise ValueError('exact Shoko import head is unavailable')
            imported, file_id = head()
            self.cursor_journal.save({
                'version': 1,
                'imported_utc': imported.isoformat(),
                'file_id': file_id,
            })
            return imported, file_id

    def bootstrap_from_exact_cursor(self, value: Any) -> FileCursor:
        """Persist a separately reviewed baseline, never replace live work."""
        if (not isinstance(value, dict)
                or set(value) != {'imported_utc', 'file_id'}):
            raise ValueError('exact Shoko import starting cursor required')
        imported, file_id = _import_cursor({
            'Imported': value['imported_utc'], 'ID': value['file_id']})
        with self._lock:
            if self.backlog_journal is None:
                raise ValueError('durable Shoko bootstrap backlog required')
            cursor_path = getattr(self.cursor_journal, 'path', None)
            if ((isinstance(cursor_path, Path) and cursor_path.exists())
                    or self.cursor_journal.load()):
                raise ValueError('existing Shoko import cursor cannot be replaced')
            pending = getattr(self.processor, 'pending_ids', None)
            if (not callable(pending) or pending() or self._backlog):
                raise ValueError('pending Shoko file work prevents bootstrap')
            self.cursor_journal.save({
                'version': 1, 'imported_utc': imported.isoformat(),
                'file_id': file_id})
            return imported, file_id

    def drain_once(self) -> int:
        """Move one oldest-first slice to the durable live queue.

        The queue journal is saved before removing IDs from the backlog. A
        failed backlog save repeats IDs, which the queue coalesces.
        """
        if self.backlog_journal is None:
            return 0
        with self._lock:
            pending = getattr(self.processor, 'catchup_queued_ids', None)
            if not callable(pending):
                pending = getattr(self.processor, 'pending_ids', None)
            capacity = getattr(self.processor, 'max_pending', None)
            if (not callable(pending) or type(capacity) is not int
                    or capacity <= 0):
                raise ValueError('Shoko catch-up queue capacity unavailable')
            queued = pending()
            if (not isinstance(queued, tuple) or len(queued) > capacity
                    or any(type(item) is not int or item <= 0 for item in queued)
                    or len(set(queued)) != len(queued)):
                raise ValueError('invalid Shoko catch-up live queue')
            seen = set(queued)
            # Leave half of the bounded queue available for new live notices.
            slots = max(0, max(1, capacity // 2) - len(queued))
            added: list[int] = []
            consumed = 0
            for file_id in self._backlog:
                if file_id in seen:
                    consumed += 1
                elif len(added) < slots:
                    added.append(file_id)
                    seen.add(file_id)
                    consumed += 1
                else:
                    break
            if not consumed:
                return 0
            if added:
                self.processor.enqueue_exact_file_ids(tuple(added))
            remaining = self._backlog[consumed:]
            self._save_backlog(remaining)
            self._backlog = remaining
            return consumed

    def _watermark(self) -> FileCursor:
        saved = self.cursor_journal.load()
        if (not isinstance(saved, dict)
                or set(saved) != {'version', 'imported_utc', 'file_id'}
                or type(saved['version']) is not int
                or saved['version'] != 1):
            raise ValueError('initial Shoko import watermark is absent or invalid')
        return _import_cursor({'ID': saved['file_id'],
                               'Imported': saved['imported_utc']})

    def run_once(self) -> RecentBatch:
        with self._lock:
            previous = self._watermark()
            batch = self.reader.scan_since(previous)
            if not isinstance(batch, RecentBatch):
                raise ValueError('invalid Shoko recent-file review')
            if batch.complete:
                if batch.watermark is None:
                    raise ValueError('complete Shoko catch-up has no watermark')
                if self.backlog_journal is None:
                    # Legacy private mode queues the entire complete batch.
                    self.processor.enqueue_exact_file_ids(batch.file_ids)
                else:
                    seen = set(self._backlog)
                    merged = list(self._backlog)
                    for file_id in batch.file_ids:
                        if type(file_id) is not int or file_id <= 0:
                            raise ValueError('invalid Shoko catch-up file ID')
                        if file_id not in seen:
                            merged.append(file_id)
                            seen.add(file_id)
                    if merged != self._backlog:
                        # If the cursor save fails, a new scan safely merges
                        # the same IDs with this durable backlog.
                        self._save_backlog(merged)
                        self._backlog = merged
                if batch.watermark != previous:
                    imported, file_id = batch.watermark
                    self.cursor_journal.save({
                        'version': 1,
                        'imported_utc': imported.isoformat(),
                        'file_id': file_id,
                    })
            else:
                if batch.watermark is not None:
                    raise ValueError('incomplete Shoko catch-up advanced watermark')
                if self.backlog_journal is None:
                    self.processor.enqueue_exact_file_ids(batch.file_ids)
            return batch

    def run_if_head_changed(self) -> RecentBatch:
        """Avoid paging Shoko while its exact newest import is unchanged."""
        with self._lock:
            previous = self._watermark()
            head = getattr(self.reader, 'head_cursor', None)
            if not callable(head):
                raise ValueError('exact Shoko recent-file head is unavailable')
            current = head()
            if current < previous:
                raise ValueError('Shoko recent-file head is older than saved cursor')
            if current == previous:
                return RecentBatch((), previous, True)
            return self.run_once()
