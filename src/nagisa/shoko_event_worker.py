"""Private supervised Shoko file-feed lifecycle, not wired into startup."""

from __future__ import annotations

import threading
from typing import Any, Callable


class ShokoEventWorker:
    def __init__(self, client_factory: Callable[[], Any], processor: Any,
                 *, stop: threading.Event | None = None,
                 catchup: Any = None,
                 request_retry: Any = None,
                 request_catchup: Any = None,
                 owned_audit: Any = None,
                 mapping_audit: Any = None,
                 series_recheck: Any = None,
                 series_audit: Any = None,
                 review_limit: int = 1,
                 retry_seconds: int = 30, max_retry_seconds: int = 3600) -> None:
        if type(review_limit) is not int or not 1 <= review_limit <= 10:
            raise ValueError('invalid Shoko file review limit')
        if (not callable(client_factory)
                or not callable(getattr(processor, 'review_one', None))
                or (catchup is not None and not callable(
                    getattr(catchup, 'drain_once', None)))
                or (catchup is not None and not callable(
                    getattr(catchup, 'backlog_count', None)))
                or (request_retry is not None and not callable(
                    getattr(request_retry, 'run_once', None)))
                or (request_catchup is not None and not callable(
                    getattr(request_catchup, 'run_once', None)))
                or (owned_audit is not None and not callable(
                    getattr(owned_audit, 'run_once', None)))
                or (mapping_audit is not None and not callable(
                    getattr(mapping_audit, 'run_once', None)))
                or (series_recheck is not None and not callable(
                    getattr(series_recheck, 'run_once', None)))
                or (series_audit is not None and not callable(
                    getattr(series_audit, 'run_once', None)))
                or (stop is not None and not isinstance(stop, threading.Event))
                or type(retry_seconds) is not int
                or type(max_retry_seconds) is not int
                or not 5 <= retry_seconds <= max_retry_seconds <= 3600):
            raise ValueError('invalid Shoko event worker settings')
        self.client_factory = client_factory
        self.processor = processor
        self.catchup = catchup
        self.request_retry = request_retry
        self.request_catchup = request_catchup
        self.owned_audit = owned_audit
        self.mapping_audit = mapping_audit
        self.series_recheck = series_recheck
        self.series_audit = series_audit
        self.review_limit = review_limit
        self.stop_event = stop or threading.Event()
        self.retry_seconds = retry_seconds
        self.max_retry_seconds = max_retry_seconds
        self._lock = threading.Lock()
        self._active: Any = None
        self._connections = 0
        self._last_error: str | None = None

    def run_connection(self) -> None:
        if self.stop_event.is_set():
            return
        try:
            client = self.client_factory()
            if not all(callable(getattr(client, name, None))
                       for name in ('connect', 'poll', 'close')):
                raise ValueError('invalid Shoko event client')
        except Exception as error:
            with self._lock:
                self._last_error = type(error).__name__
            raise
        with self._lock:
            if self._active is not None:
                raise RuntimeError('Shoko event worker already connected')
            self._active = client
        try:
            client.connect()  # Includes durable gap catch-up, or raises.
            with self._lock:
                self._connections += 1
                self._last_error = None
            while not self.stop_event.is_set() and not client.gap_detected:
                # Exact-file reviews have their own worker. They may take
                # longer than a poll and must not block receipt of new events.
                client.poll()
        except Exception as error:
            with self._lock:
                self._last_error = type(error).__name__
            raise
        finally:
            try:
                client.close()
            finally:
                with self._lock:
                    self._active = None

    def run_forever(self) -> None:
        delay = self.retry_seconds
        while not self.stop_event.is_set():
            failed = False
            try:
                self.run_connection()
            except Exception:
                failed = True
                pass  # Snapshot retains only the error type, never a secret.
            if not failed:
                delay = self.retry_seconds
            if self.stop_event.wait(delay):
                break
            if failed:
                delay = min(delay * 2, self.max_retry_seconds)

    def review_pending_once(self) -> object:
        """One bounded exact-file review for a separate Nagisa worker."""
        if self.stop_event.is_set():
            return None
        if self.catchup is not None:
            self.catchup.drain_once()
        result = None
        for _ in range(self.review_limit):
            if self.stop_event.is_set():
                break
            reviewed = self.processor.review_one()
            if reviewed is None:
                break
            if result is None:
                result = reviewed
        if self.request_retry is not None:
            self.request_retry.run_once()
        if self.request_catchup is not None:
            self.request_catchup.run_once()
        if self.owned_audit is not None:
            self.owned_audit.run_once()
        if self.mapping_audit is not None:
            self.mapping_audit.run_once()
        # Drain existing exact-file work before adding one bounded series
        # slice. Otherwise a full durable queue could prevent its own drain.
        if self.series_audit is not None:
            self.series_audit.run_once()
        if self.series_recheck is not None:
            self.series_recheck.run_once()
        return result

    def check_recent_once(self) -> None:
        """Bounded missed-notice check, independent of live event receipt."""
        if self.stop_event.is_set() or self.catchup is None:
            return
        batch = self.catchup.run_if_head_changed()
        if getattr(batch, 'complete', None) is not True:
            raise ValueError('Shoko recent-file catch-up incomplete')

    def stop(self) -> None:
        self.stop_event.set()
        with self._lock:
            client = self._active
        if client is not None:
            # SignalR DELETE should terminate an outstanding long poll with
            # HTTP 204, allowing the runtime to join before closing state.
            try:
                client.close()
            except Exception as error:
                # A failed DELETE must not prevent Nagisa's other workers and
                # databases from closing. Keep the failure visible in health.
                with self._lock:
                    self._last_error = type(error).__name__

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            client = self._active
            return {
                'connected': client is not None and not client.gap_detected,
                'connections': self._connections,
                'last_error': self._last_error,
                'stopped': self.stop_event.is_set(),
                'backlog_file_ids': (self.catchup.backlog_count()
                                     if self.catchup is not None else 0),
            }
