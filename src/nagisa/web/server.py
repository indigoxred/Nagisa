"""Guarded WebUI application and HTTP server."""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from nagisa.web import views


SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
MUTATIONS = {"/ordering/evaluate", "/ordering/lock", "/ordering/apply", "/ordering/undo", "/reconciler/run", '/delivery/settings'}


@dataclass(frozen=True, slots=True)
class WebResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class NagisaWebApp:
    def __init__(self, runtime, *, shoko_origin: str, csrf_token: str | None = None,
                 seerr_app=None) -> None:
        self.runtime = runtime
        self.shoko_origin = shoko_origin.rstrip("/")
        self.csrf_token = csrf_token or secrets.token_urlsafe(32)
        self.seerr_app = seerr_app

    @staticmethod
    def _response(status: int, body: bytes, content_type="text/html; charset=utf-8", **headers) -> WebResponse:
        return WebResponse(status, {**SECURITY_HEADERS, "Content-Type": content_type, **headers}, body)

    @staticmethod
    def _positive(values: dict[str, list[str]], key: str) -> int:
        try:
            value = int(values[key][0])
        except (KeyError, IndexError, TypeError, ValueError):
            raise ValueError(f"{key} must be a positive integer") from None
        if value <= 0:
            raise ValueError(f"{key} must be a positive integer")
        return value

    def handle(self, method: str, target: str, body: bytes, headers: dict[str, str]) -> WebResponse:
        parsed_target = urlsplit(target)
        path = parsed_target.path
        if path.startswith('/api/v3/') and self.seerr_app is not None:
            answer = self.seerr_app.handle(method, target, body, headers)
            return self._response(answer.status, answer.body, answer.content_type)
        if method == "GET" and path in MUTATIONS:
            return self._response(405, b"Method not allowed")
        if method == "GET":
            query_values = parse_qs(parsed_target.query, keep_blank_values=True)
            activity_query = query_values.get("q", [""])[0].strip()
            if len(activity_query) > 100:
                return self._response(400, b"Search is too long")
            if path == "/healthz":
                health = self.runtime.health_snapshot()
                status = 200 if health.get("healthy") else 503
                return self._response(status, json.dumps(health, default=str).encode(), "application/json")
            renderers = {
                "/": ("Overview", lambda: views.overview(self.runtime)),
                "/ordering": ("TMDB Ordering", lambda: views.ordering(self.runtime, self.shoko_origin, self.csrf_token)),
                "/reconciler": (
                    "Reconciler",
                    lambda: views.reconciler(
                        self.runtime, self.shoko_origin, self.csrf_token
                    ),
                ),
                "/arrival": ("Arrival delivery", lambda: views.arrival(self.runtime)),
                '/delivery': ('Delivery settings', lambda: views.delivery(self.runtime, self.csrf_token)),
                '/folders': ('Configured folders', lambda: views.folders(self.runtime)),
                "/activity": ("Activity Log", lambda: views.activity(self.runtime, activity_query)),
                "/system": ("Settings / Updates", lambda: views.system(self.runtime)),
            }
            if path.startswith("/ordering/show/"):
                try:
                    show_id = int(path.removeprefix("/ordering/show/"))
                    if show_id <= 0:
                        raise ValueError
                except ValueError:
                    return self._response(404, b"Not found")
                renderers[path] = (
                    "TMDB Ordering Detail",
                    lambda: views.ordering_detail(
                        self.runtime, show_id, self.shoko_origin, self.csrf_token
                    ),
                )
            if path not in renderers:
                return self._response(404, b"Not found")
            title, renderer = renderers[path]
            return self._response(200, views.page(title, renderer(), self.csrf_token))
        if method != "POST" or path not in MUTATIONS:
            return self._response(404, b"Not found")
        if len(body) > 8192:
            return self._response(413, b"Request too large")
        try:
            values = parse_qs(body.decode("utf-8", errors="strict"), keep_blank_values=True)
        except UnicodeError:
            return self._response(400, b'Invalid request encoding')
        if values.get("csrf", [""])[0] != self.csrf_token or values.get("confirm", [""])[0] != "CONFIRM":
            return self._response(403, b"CSRF or confirmation refused")
        if path == '/delivery/settings':
            expected = {'csrf','confirm','access_key','revision','arrival_enabled','subtitles_enabled','reconciliation_enabled',
                'arrival_poll_seconds','subtitle_poll_seconds','subtitle_max_file_bytes','subtitle_max_total_bytes',
                'catchup_enabled','catchup_include_backlog','catchup_poll_seconds','movie_arrival_enabled','movie_arrival_poll_seconds'}
            if set(values) not in (expected, expected|{'bindings'}) or any(len(rows)!=1 for rows in values.values()):
                return self._response(400, b'Invalid delivery controls')
            try:
                controls = {key: values[key][0]=='true' for key in ('arrival_enabled','subtitles_enabled','reconciliation_enabled','catchup_enabled','catchup_include_backlog','movie_arrival_enabled')}
                if any(values[key][0] not in ('true','false') for key in controls):
                    raise ValueError('Invalid enable control')
                for key in ('arrival_poll_seconds','subtitle_poll_seconds','subtitle_max_file_bytes','subtitle_max_total_bytes','catchup_poll_seconds','movie_arrival_poll_seconds'):
                    controls[key] = int(values[key][0])
                from nagisa.delivery import _unique
                bindings=json.loads(values['bindings'][0],object_pairs_hook=_unique) if 'bindings' in values else None
                self.runtime.save_delivery_controls(controls, values['revision'][0], values['access_key'][0], bindings)
            except PermissionError:
                return self._response(403, b'Delivery settings authentication refused')
            except (OSError, ValueError, RuntimeError, TypeError):
                return self._response(409, b'Settings not saved. Reload and check the limits and configured roots.')
            return self._response(303, b'', Location='/delivery')
        try:
            if path == "/reconciler/run":
                self.runtime.run_reconciler_now()
                location = "/reconciler"
            elif path == "/ordering/evaluate":
                self.runtime.ordering.evaluate_now(self._positive(values, "show_id"))
                location = "/ordering"
            elif path == "/ordering/lock":
                reason = values.get("reason", [""])[0].strip()
                if not reason or len(reason) > 200:
                    raise ValueError("lock reason is required")
                self.runtime.ordering.lock(self._positive(values, "show_id"), reason)
                location = "/ordering"
            elif path == "/ordering/undo":
                self.runtime.ordering.undo(self._positive(values, "decision_id"))
                location = "/ordering"
            else:
                if self.runtime.ordering.status_snapshot().get("mode") != "enabled":
                    return self._response(409, b"Ordering apply is report-only")
                show_id = self._positive(values, "show_id")
                self.runtime.ordering.apply_now(show_id)
                location = f"/ordering/show/{show_id}"
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            return self._response(409, str(error).encode("utf-8", errors="replace"))
        return self._response(303, b"", Location=location)


class NagisaWebServer:
    def __init__(self, app: NagisaWebApp, host: str, port: int) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                limit = 1024 * 1024 if outer.app.seerr_app is not None and self.path.startswith('/api/v3/') else 8192
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if length < 0 or length > limit:
                    response = outer.app._response(413, b"Request too large")
                elif len(self.headers.get_all('X-Api-Key', [])) > 1:
                    response = outer.app._response(401, b"Authentication required")
                else:
                    response = outer.app.handle(self.command, self.path, self.rfile.read(length), dict(self.headers))
                self.send_response(response.status)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(response.body)))
                self.end_headers()
                self.wfile.write(response.body)

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle

            def log_message(self, format, *args):
                return

        self.app = app
        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="nagisa-web", daemon=True)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        if self.app.seerr_app is not None:
            preflight = getattr(self.app.seerr_app, 'preflight', None)
            if preflight is not None:
                preflight()
            boundary = getattr(self.app.seerr_app, 'boundary', None)
            recover = getattr(boundary, 'recover_pending_pause', None)
            if not callable(recover):
                raise ValueError('Seerr guard requires a recoverable boundary')
            recover()
        self._thread.start()

    def stop(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()
        self._server.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
