"""Private Sonarr-compatible request dispatcher for the approved Seerr guard.

The private guard can be injected into Nagisa's existing HTTP server, but its
route is absent by default. Production credentials and upstream transport are
deliberately not configured here.
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit


@dataclass(frozen=True, slots=True)
class EndpointResponse:
    status: int
    body: bytes
    content_type: str = 'application/json'


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


class SonarrCompatibleApp:
    """Permit only Seerr's known writes; guard all of them before forwarding."""

    _WRITES = {
        ('POST', '/api/v3/series'): 'add_series',
        ('PUT', '/api/v3/series'): 'update_series',
        ('PUT', '/api/v3/episode/monitor'): 'monitor_episodes',
        ('POST', '/api/v3/command'): 'search_series',
    }

    def __init__(self, boundary: Any, upstream: Any, api_key: str) -> None:
        if not isinstance(api_key, str) or len(api_key) < 8 or any(c in api_key for c in '\r\n\x00'):
            raise ValueError('private API key is invalid')
        self.boundary = boundary
        self.upstream = upstream
        self._api_key = api_key

    @staticmethod
    def _answer(status: int, payload: object) -> EndpointResponse:
        return EndpointResponse(status, json.dumps(payload, separators=(',', ':')).encode())

    def handle(self, method: str, target: str, body: bytes,
               headers: dict[str, str]) -> EndpointResponse:
        if not isinstance(headers, dict) or not isinstance(body, bytes):
            return self._answer(400, {'error': 'invalid request'})
        if not isinstance(method, str) or not isinstance(target, str) or len(target) > 2048:
            return self._answer(405, {'error': 'unsupported route'})
        if any(c in target for c in '\r\n\\\x00') or '//' in target or '..' in unquote(target):
            return self._answer(405, {'error': 'unsupported route'})
        parsed = urlsplit(target)
        if (parsed.scheme or parsed.netloc or parsed.fragment
                or not parsed.path.startswith('/api/v3/')):
            return self._answer(405, {'error': 'unsupported route'})
        try:
            pairs = parse_qsl(parsed.query, keep_blank_values=True,
                              strict_parsing=True, max_num_fields=50)
        except ValueError:
            return self._answer(400, {'error': 'invalid query'})
        keys = [value for name, value in headers.items() if name.casefold() == 'x-api-key']
        keys.extend(value for name, value in pairs if name == 'apikey')
        if (len(keys) != 1 or not isinstance(keys[0], str)
                or not secrets.compare_digest(keys[0], self._api_key)):
            return self._answer(401, {'error': 'authentication required'})
        non_auth = [(name, value) for name, value in pairs if name != 'apikey']
        if method == 'GET':
            if body:
                return self._answer(400, {'error': 'GET body refused'})
            try:
                path = parsed.path
                if non_auth:
                    path += '?' + urlencode(non_auth)
                result = self.upstream.request_json('GET', path)
            except (ValueError, RuntimeError, OSError, TypeError):
                return self._answer(503, {'error': 'Sonarr read unavailable'})
            return self._answer(200, result)
        action = self._WRITES.get((method, parsed.path))
        if action is None or non_auth:
            return self._answer(405, {'error': 'unsupported mutation'})
        if len(body) > 1024 * 1024:
            return self._answer(413, {'error': 'request exceeds bound'})
        try:
            payload = json.loads(body.decode('utf-8'), object_pairs_hook=_unique)
        except (UnicodeError, json.JSONDecodeError, ValueError):
            return self._answer(400, {'error': 'invalid JSON'})
        if not isinstance(payload, dict):
            return self._answer(400, {'error': 'JSON object required'})
        try:
            result = getattr(self.boundary, action)(payload)
        except (ValueError, RuntimeError, OSError, TypeError, KeyError):
            return self._answer(409, {'error': 'request held by anime ownership guard'})
        return self._answer(200, result)


class PrivateGuardServer:
    """Loopback-only rehearsal server; never registered in Nagisa runtime."""

    def __init__(self, app: SonarrCompatibleApp, host: str = '127.0.0.1',
                 port: int = 0) -> None:
        if host not in ('127.0.0.1', '::1'):
            raise ValueError('private guard rehearsal must bind to loopback')
        self._app = app

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                keys = self.headers.get_all('X-Api-Key', [])
                if len(keys) > 1:
                    answer = EndpointResponse(401, b'{"error":"authentication required"}')
                else:
                    try:
                        length = int(self.headers.get('Content-Length', '0'))
                    except ValueError:
                        length = -1
                    if length < 0 or length > 1024 * 1024:
                        answer = EndpointResponse(413, b'{"error":"request exceeds bound"}')
                    else:
                        headers = {'X-Api-Key': keys[0]} if keys else {}
                        answer = app.handle(self.command, self.path,
                                            self.rfile.read(length), headers)
                self.send_response(answer.status)
                self.send_header('Content-Type', answer.content_type)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(answer.body)))
                self.end_headers()
                self.wfile.write(answer.body)

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_DELETE = _handle

            def log_message(self, *_):
                return

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name='private-seerr-guard', daemon=True)

    @property
    def address(self):
        return self._server.server_address

    def start(self) -> None:
        preflight = getattr(self._app, 'preflight', None)
        recover = getattr(self._app.boundary, 'recover_pending_pause', None)
        try:
            if preflight is not None:
                preflight()
            if recover is not None:
                recover()
        except Exception:
            self._server.server_close()
            raise
        self._thread.start()

    def stop(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()
        self._server.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
