"""Permanent redacted decision and action audit records."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from nagisa.state import StateStore


class UnsafeAuditData(ValueError):
    pass


_FORBIDDEN_KEYS = {"authorization", "cookie", "set-cookie", "token", "secret", "password", "apikey", "api_key"}


def _validate(value: Any, key: str = "") -> None:
    lowered = key.lower().replace("-", "_")
    if lowered in _FORBIDDEN_KEYS or any(part in lowered for part in ("token", "secret", "password", "authorization", "cookie", "apikey")):
        raise UnsafeAuditData(f"forbidden audit field: {key}")
    if isinstance(value, dict):
        for child_key, child in value.items():
            _validate(child, str(child_key))
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate(child, key)
    elif isinstance(value, str) and value.startswith(("http://", "https://")):
        parsed = urlsplit(value)
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise UnsafeAuditData("audit URLs must be plain origins or paths")


class AuditLog:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def record(self, module: str, event_type: str, evidence: dict[str, Any]) -> int:
        if module not in {"ordering", "reconciler", "future-guard", "system"}:
            raise ValueError("unsupported audit module")
        _validate(evidence)
        payload = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        cursor = self.store.connection.execute(
            "INSERT INTO audit_events(module,event_type,recorded_at,evidence_json) VALUES(?,?,?,?)",
            (module, event_type, timestamp, payload),
        )
        return int(cursor.lastrowid)

    def rows(self, limit: int = 1000) -> list[dict[str, Any]]:
        if limit < 1 or limit > 10000:
            raise ValueError("audit limit is out of range")
        rows = self.store.connection.execute(
            "SELECT id,module,event_type,recorded_at,evidence_json FROM audit_events ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) | {"evidence": json.loads(row["evidence_json"])} for row in rows]
