"""Bounded JSON transport that never exposes authentication material."""

from __future__ import annotations

import json
from http.cookiejar import CookieJar
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener


class TransportError(RuntimeError):
    pass


class _SameOriginRedirects(HTTPRedirectHandler):
    def __init__(self, origin: str) -> None:
        self.origin = origin

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        target = f"{parsed.scheme}://{parsed.netloc}"
        if target != self.origin:
            raise TransportError("cross-origin redirect refused")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


class JsonTransport:
    def __init__(self, base_url: str, *, headers: Mapping[str, str] | None = None,
                 timeout: float = 10, maximum_bytes: int = 4 * 1024 * 1024,
                 allowed_methods: frozenset[str] = frozenset({'GET', 'POST', 'DELETE'})) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("transport URL must be a plain HTTP(S) origin")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self._headers = dict(headers or {})
        self.timeout = timeout
        self.maximum_bytes = maximum_bytes
        if (not isinstance(allowed_methods, frozenset) or not allowed_methods
                or not allowed_methods.issubset({'GET', 'POST', 'PUT', 'DELETE'})):
            raise ValueError('invalid transport method allowlist')
        self.allowed_methods = allowed_methods
        self._opener = build_opener(_SameOriginRedirects(self.origin))

    def request_json(self, method: str, path: str, body: object | None = None) -> Any:
        if method not in self.allowed_methods or not path.startswith("/") or "\r" in path or "\n" in path:
            raise ValueError("unsafe HTTP request")
        headers = {"Accept": "application/json", **self._headers}
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(urljoin(self.origin, path), data=payload, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(self.maximum_bytes + 1)
        except TransportError:
            raise
        except HTTPError as error:
            code = error.code
            error.close()
            raise TransportError(f"request returned HTTP {code}") from None
        except (URLError, TimeoutError, OSError):
            raise TransportError("request endpoint was unavailable") from None
        if len(raw) > self.maximum_bytes:
            raise TransportError("JSON response exceeded the size limit")
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TransportError("endpoint returned malformed JSON") from None

    def request_bytes(self, path: str, *, maximum_bytes: int) -> bytes:
        """Bounded same-origin GET for Plex's original external subtitle resource."""
        import re
        if not isinstance(path, str) or re.fullmatch(r'/library/streams/[1-9][0-9]*(?:\.(?:ass|ssa|srt|sub|idx|vtt))?', path) is None:
            raise ValueError('Unsafe Plex subtitle resource')
        if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= 16777216:
            raise ValueError('Invalid subtitle response bound')
        request = Request(self.origin + path, headers=self._headers, method='GET')
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(maximum_bytes + 1)
        except HTTPError as error:
            code=error.code; error.close()
            raise TransportError(f'Subtitle resource returned HTTP {code}') from None
        except (URLError,TimeoutError,OSError):
            raise TransportError('Subtitle resource unavailable') from None
        if len(raw)>maximum_bytes:
            raise TransportError('Subtitle resource exceeded its bound')
        return raw


class QbitTransport:
    """Authenticated, cookie-backed, read-only qBittorrent JSON transport."""

    def __init__(self, base_url: str, *, username: str, password: str,
                 timeout: float = 10, maximum_bytes: int = 4 * 1024 * 1024) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.path or parsed.query or parsed.fragment
        ):
            raise ValueError("transport URL must be a plain HTTP(S) origin")
        if not username or not password or "\x00" in username or "\x00" in password:
            raise ValueError("qBittorrent credentials are invalid")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.timeout = timeout
        self.maximum_bytes = maximum_bytes
        self._username = username
        self._password = password
        self._authenticate()

    def _authenticate(self) -> None:
        self._opener = build_opener(
            HTTPCookieProcessor(CookieJar()), _SameOriginRedirects(self.origin)
        )
        payload = urlencode({"username": self._username, "password": self._password}).encode("utf-8")
        request = Request(
            self.origin + "/api/v2/auth/login", data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.origin,
                "Referer": self.origin + "/",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
                raw = response.read(32)
        except HTTPError as error:
            error.close()
            raise TransportError("qBittorrent authentication failed") from None
        except (URLError, TimeoutError, OSError, TransportError):
            raise TransportError("qBittorrent authentication failed") from None
        if status == 204 and raw == b"":
            try:
                probe = self._request_json_once("/api/v2/torrents/info?limit=1")
            except (HTTPError, URLError, TimeoutError, OSError, TransportError, ValueError):
                raise TransportError("qBittorrent authentication failed") from None
            if not isinstance(probe, list):
                raise TransportError("qBittorrent authentication failed")
        elif status != 200 or raw.strip() != b"Ok.":
            raise TransportError("qBittorrent authentication failed")

    def _request_json_once(self, path: str) -> Any:
        request = Request(
            urljoin(self.origin, path),
            headers={"Accept": "application/json", "Referer": self.origin + "/"},
            method="GET",
        )
        with self._opener.open(request, timeout=self.timeout) as response:
            raw = response.read(self.maximum_bytes + 1)
        if len(raw) > self.maximum_bytes:
            raise TransportError("qBittorrent response exceeded the size limit")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TransportError("qBittorrent returned malformed JSON") from None

    def request_json(self, method: str, path: str, body: object | None = None) -> Any:
        if method != "GET" or body is not None or not path.startswith("/") or "\r" in path or "\n" in path:
            raise ValueError("unsafe qBittorrent read request")
        try:
            return self._request_json_once(path)
        except HTTPError as error:
            status = error.code
            error.close()
            if status in {401, 403}:
                try:
                    self._authenticate()
                    return self._request_json_once(path)
                except HTTPError as retry_error:
                    retry_error.close()
                except (URLError, TimeoutError, OSError, TransportError):
                    pass
            raise TransportError("qBittorrent endpoint was unavailable") from None
        except (URLError, TimeoutError, OSError, TransportError):
            raise TransportError("qBittorrent endpoint was unavailable") from None
