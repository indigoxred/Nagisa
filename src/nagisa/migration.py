"""Exact, backup-first migration from the standalone ordering selector."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from nagisa.state import StateStore


MIGRATION_NAME = "legacy-selector-v1"
TABLES = ("metadata", "baseline", "shows", "decisions", "decision_events")


class ChangedMigrationSource(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MigrationResult:
    migrated: bool
    source_counts: dict[str, int]
    source_sha256: str
    backup_path: Path
    backup_sha256: str


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_source(path: Path) -> tuple[sqlite3.Connection, dict[str, int]]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("legacy selector database must be a regular non-symlink file")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(TABLES).issubset(names):
        connection.close()
        raise ValueError("legacy selector schema is incomplete")
    counts = {name: int(connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]) for name in TABLES}
    return connection, counts


def _backup(source: sqlite3.Connection, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    return sha256_file(destination)


def migrate_legacy_selector(legacy_db: str | Path, store: StateStore,
                            backup_dir: str | Path) -> MigrationResult:
    source_path = Path(legacy_db)
    source_sha = sha256_file(source_path)
    source, counts = _read_source(source_path)
    backup_path = Path(backup_dir) / f"legacy-selector-{source_sha[:16]}.sqlite3"
    try:
        backup_sha = _backup(source, backup_path)
        prior = store.connection.execute(
            "SELECT source_sha256,detail_json FROM schema_migrations WHERE name=?", (MIGRATION_NAME,)
        ).fetchone()
        if prior is not None:
            if prior["source_sha256"] != source_sha:
                raise ChangedMigrationSource("completed selector migration source changed")
            detail = json.loads(prior["detail_json"])
            return MigrationResult(False, counts, source_sha, Path(detail["backup_path"]), detail["backup_sha256"])

        with store.transaction("migration") as target:
            for row in source.execute("SELECT key,value FROM metadata ORDER BY key"):
                target.execute("INSERT INTO ordering_metadata(key,value) VALUES(?,?)", tuple(row))
            for row in source.execute("SELECT show_id,preference,recorded_at FROM baseline ORDER BY show_id"):
                target.execute("INSERT INTO ordering_baseline VALUES(?,?,?)", tuple(row))
            for row in source.execute("SELECT show_id,first_seen_at,initial_preference,locked,lock_reason,locked_at FROM shows ORDER BY show_id"):
                target.execute("INSERT INTO ordering_shows VALUES(?,?,?,?,?,?)", tuple(row))
            for row in source.execute("SELECT id,show_id,outcome,evidence_fingerprint,prior_ordering,selected_ordering,recorded_at FROM decisions ORDER BY id"):
                target.execute("INSERT INTO ordering_decisions VALUES(?,?,?,?,?,?,?)", tuple(row))
            for row in source.execute("SELECT id,decision_id,event_type,evidence_fingerprint,ordering_value,recorded_at FROM decision_events ORDER BY id"):
                target.execute("INSERT INTO ordering_decision_events VALUES(?,?,?,?,?,?)", tuple(row))
            detail = json.dumps({"source_counts": counts, "backup_path": str(backup_path), "backup_sha256": backup_sha}, sort_keys=True)
            target.execute(
                "INSERT INTO schema_migrations(name,source_sha256,detail_json,completed_at) VALUES(?,?,?,?)",
                (MIGRATION_NAME, source_sha, detail, _utc_now()),
            )
        if store.ordering.parity_counts() != counts or store.quick_check() != "ok":
            raise RuntimeError("selector migration parity or integrity check failed")
        return MigrationResult(True, counts, source_sha, backup_path, backup_sha)
    finally:
        source.close()

