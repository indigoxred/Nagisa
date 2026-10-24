"""Private, dependency-free Shoko SignalR file-feed transport.

The server's file feed does not replay messages. Every fresh connection must
complete the durable recent-file catch-up before it can report as connected.
This module is not wired into Nagisa's production startup.
"""

from __future__ import annotations

import json
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


_HUB = '/signalr/aggregate'
_MAX_BODY = 65536


class ShokoLongPollHttp:
    """HTTP half-transports using Nagisa's existing Shoko API token."""

    def __init__(self, origin: str, api_token: str) -> None:
        parsed = urlsplit(origin)
        if (parsed.scheme not in ('http', 'https') or not parsed.netloc
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or not isinstance(api_token, str) or not api_token):
            raise ValueError('invalid Shoko event origin or credential')
        self.origin = origin.rstrip('/')
        self.api_token = api_token

    def request(self, method: str, path: str, body: bytes = b'',
                *, timeout: int = 10) -> tuple[int, bytes]:
        if (method not in ('GET', 'POST', 'DELETE')
                or not path.startswith(_HUB)
                or not isinstance(body, bytes)
                or len(body) > 4096
                or type(timeout) is not int or not 1 <= timeout <= 120):
            raise ValueError('invalid Shoko event request')
        headers = {'apikey': self.api_token, 'Accept': '*/*'}
        if method == 'POST':
            headers['Content-Type'] = 'text/plain;charset=UTF-8'
        request = Request(self.origin + path, data=body if method == 'POST'
                          else None, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                data = response.read(_MAX_BODY + 1)
                status = response.status
        except HTTPError as error:
            # Do not include the URL or connection token in an exception.
            raise ConnectionError(f'Shoko event HTTP {error.code}') from None
        except (URLError, OSError):
            raise ConnectionError('Shoko event connection failed') from None
        if len(data) > _MAX_BODY:
            raise ValueError('Shoko event response exceeds bound')
        return status, data


class ShokoLongPoll:
    def __init__(self, http: Any, processor: Any, catchup: Any) -> None:
        if (not callable(getattr(http, 'request', None))
                or not callable(getattr(processor, 'ingest', None))
                or not callable(getattr(catchup, 'run_once', None))):
            raise ValueError('Shoko event transport requires catch-up')
        self.http = http
        self.processor = processor
        self.catchup = catchup
        self._token: str | None = None
        self._state_lock = threading.Lock()
        self._closed = False
        self.gap_detected = True

    def _require_open(self) -> None:
        with self._state_lock:
            if self._closed or self._token is None:
                raise ConnectionError('Shoko event connection was closed')

    def _ingest_if_open(self, frames: str) -> None:
        # A long poll can finish after stop() has closed the connection. Keep
        # the close decision and pending-journal write in one critical section.
        with self._state_lock:
            if self._closed or self._token is None:
                raise ConnectionError('Shoko event connection was closed')
            self.processor.ingest(frames)

    def _request(self, method: str, path: str, body: bytes = b'',
                 *, timeout: int = 10) -> tuple[int, bytes]:
        status, response = self.http.request(method, path, body, timeout=timeout)
        if (type(status) is not int or not isinstance(response, bytes)
                or len(response) > _MAX_BODY):
            raise ValueError('invalid Shoko event HTTP response')
        return status, response

    def connect(self) -> None:
        with self._state_lock:
            if self._closed or self._token is not None:
                raise ValueError('Shoko event connection already used')
        status, body = self._request(
            'POST', _HUB + '/negotiate?negotiateVersion=1')
        if status != 200:
            raise ConnectionError('Shoko event negotiation failed')
        try:
            negotiation = json.loads(body)
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError('invalid Shoko event negotiation') from None
        if (not isinstance(negotiation, dict)
                or negotiation.get('negotiateVersion') != 1
                or not isinstance(negotiation.get('connectionToken'), str)
                or not 1 <= len(negotiation['connectionToken']) <= 512
                or not isinstance(negotiation.get('availableTransports'), list)
                or not any(isinstance(row, dict)
                           and row.get('transport') == 'LongPolling'
                           and isinstance(row.get('transferFormats'), list)
                           and 'Text' in row['transferFormats']
                           for row in negotiation['availableTransports'])):
            raise ValueError('Shoko event long-poll transport unavailable')
        with self._state_lock:
            if self._closed:
                raise ConnectionError('Shoko event connection was closed')
            self._token = negotiation['connectionToken']
        self.gap_detected = True
        path = _HUB + '?id=' + quote(self._token, safe='')
        try:
            # ASP.NET uses the first GET to establish LongPolling; it returns
            # immediately and carries no hub payload.
            status, first = self._request('GET', _HUB + '?feeds=file&id='
                                          + quote(self._token, safe=''), timeout=30)
            self._require_open()
            if status != 200 or first:
                raise ValueError('Shoko event initial poll was not empty')
            handshake = b'{"protocol":"json","version":1}\x1e'
            status, _ = self._request('POST', path, handshake)
            self._require_open()
            if status != 200:
                raise ConnectionError('Shoko event handshake send failed')
            status, reply = self._request('GET', path, timeout=105)
            self._require_open()
            if status != 200 or not reply.endswith(b'\x1e'):
                raise ValueError('Shoko event handshake response is incomplete')
            frames = reply.decode('utf-8').split('\x1e')
            try:
                response = json.loads(frames[0])
            except json.JSONDecodeError:
                raise ValueError('invalid Shoko event handshake response') from None
            if response != {}:
                raise ValueError('Shoko event handshake rejected')
            remaining = '\x1e'.join(frames[1:])
            if remaining:
                self._ingest_if_open(remaining)
            self._require_open()
            batch = self.catchup.run_once()
            self._require_open()
            if getattr(batch, 'complete', None) is not True:
                raise ValueError('Shoko event gap catch-up incomplete')
            with self._state_lock:
                if self._closed or self._token is None:
                    raise ConnectionError('Shoko event connection was closed')
                self.gap_detected = False
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise

    def poll(self) -> int:
        if self._token is None or self.gap_detected:
            raise ValueError('Shoko event connection requires catch-up')
        path = _HUB + '?id=' + quote(self._token, safe='')
        try:
            status, body = self._request('GET', path, timeout=105)
            if status == 204:
                self.gap_detected = True
                return 0
            if status != 200:
                raise ConnectionError('Shoko event poll failed')
            if not body:
                return 0
            if not body.endswith(b'\x1e'):
                raise ValueError('Shoko event poll frame is incomplete')
            self._ingest_if_open(body.decode('utf-8'))
            return 1
        except Exception:
            self.gap_detected = True
            raise

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            token, self._token = self._token, None
            self.gap_detected = True
        if token is not None:
            path = _HUB + '?id=' + quote(token, safe='')
            status, _ = self._request('DELETE', path)
            if status not in (200, 202, 204, 404):
                raise ConnectionError('Shoko event close failed')
