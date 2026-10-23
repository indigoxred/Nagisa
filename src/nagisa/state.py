"""Transactional SQLite state with isolated module namespaces."""

from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
SCHEMA_VERSION = "2"


class UnsupportedStateVersion(RuntimeError):
    pass


class BaselineAlreadyInitialized(RuntimeError):
    pass


class OrderingState:
    def __init__(self, store: "StateStore") -> None:
        self._store = store

    @property
    def _connection(self) -> sqlite3.Connection:
        return self._store.connection

    @staticmethod
    def _time(value: str) -> str:
        if not isinstance(value, str) or not value.endswith("Z") or len(value) < 20:
            raise ValueError("timestamps must be UTC ISO-8601 values ending in Z")
        return value

    @staticmethod
    def _fingerprint(value: str) -> str:
        if not isinstance(value, str) or not FINGERPRINT.fullmatch(value):
            raise ValueError("evidence fingerprint must be 64 lowercase hex characters")
        return value

    def baseline_initialized(self) -> bool:
        return self._connection.execute(
            "SELECT 1 FROM ordering_metadata WHERE key='baseline_initialized'"
        ).fetchone() is not None

    def record_initial_baseline(self, entries, *, recorded_at: str) -> None:
        timestamp = self._time(recorded_at)
        rows = list(entries)
        with self._store.transaction("ordering") as connection:
            if self.baseline_initialized():
                raise BaselineAlreadyInitialized("the historical baseline is immutable")
            seen: set[int] = set()
            for show_id, preference in rows:
                if not isinstance(show_id, int) or show_id <= 0 or show_id in seen:
                    raise ValueError("baseline show IDs must be unique positive integers")
                if preference is not None and (not isinstance(preference, str) or not preference):
                    raise ValueError("preference must be null or a non-empty string")
                seen.add(show_id)
                connection.execute(
                    "INSERT INTO ordering_baseline(show_id,preference,recorded_at) VALUES(?,?,?)",
                    (show_id, preference, timestamp),
                )
                connection.execute(
                    "INSERT INTO ordering_shows(show_id,first_seen_at,initial_preference) VALUES(?,?,?)",
                    (show_id, timestamp, preference),
                )
            connection.execute(
                "INSERT INTO ordering_metadata(key,value) VALUES('baseline_initialized',?)",
                (timestamp,),
            )

    def baseline(self) -> dict[int, str | None]:
        rows = self._connection.execute(
            "SELECT show_id,preference FROM ordering_baseline ORDER BY show_id"
        ).fetchall()
        return {int(row["show_id"]): row["preference"] for row in rows}

    def discover(self, show_id: int, *, current_preference: str | None, observed_at: str) -> dict:
        if not self.baseline_initialized():
            raise RuntimeError("initial baseline must be recorded before discovery")
        if not isinstance(show_id, int) or show_id <= 0:
            raise ValueError("show ID must be a positive integer")
        timestamp = self._time(observed_at)
        if current_preference is not None and (not isinstance(current_preference, str) or not current_preference):
            raise ValueError("preference must be null or a non-empty string")
        self._connection.execute(
            "INSERT OR IGNORE INTO ordering_shows(show_id,first_seen_at,initial_preference) VALUES(?,?,?)",
            (show_id, timestamp, current_preference),
        )
        return self.show(show_id)

    def show(self, show_id: int) -> dict:
        row = self._connection.execute(
            "SELECT show_id,first_seen_at,initial_preference,locked,lock_reason,locked_at "
            "FROM ordering_shows WHERE show_id=?",
            (show_id,),
        ).fetchone()
        if row is None:
            raise KeyError(show_id)
        result = dict(row)
        result["locked"] = bool(result["locked"])
        result["historical"] = self._connection.execute(
            "SELECT 1 FROM ordering_baseline WHERE show_id=?", (show_id,)
        ).fetchone() is not None
        return result

    def shows(self) -> list[dict]:
        rows = self._connection.execute("SELECT show_id FROM ordering_shows ORDER BY show_id")
        return [self.show(int(row["show_id"])) for row in rows]

    def lock_show(self, show_id: int, reason: str, *, recorded_at: str) -> bool:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("lock reason is required")
        cursor = self._connection.execute(
            "UPDATE ordering_shows SET locked=1,lock_reason=?,locked_at=? WHERE show_id=? AND locked=0",
            (reason.strip(), self._time(recorded_at), show_id),
        )
        if cursor.rowcount == 0:
            self.show(show_id)
            return False
        return True

    def record_decision(self, show_id: int, *, outcome: str, evidence_fingerprint: str,
                        prior_ordering: str | None, selected_ordering: str | None,
                        recorded_at: str) -> int:
        self.show(show_id)
        allowed = {
            "PASS_DEFAULT",
            "SELECT_ALTERNATE",
            "REVIEW_REQUIRED",
            "INCOMPLETE_EVIDENCE",
            "LATE_DISCOVERY_REVIEW",
            "HISTORICAL_REVIEW",
        }
        if outcome not in allowed:
            raise ValueError("unsupported decision outcome")
        cursor = self._connection.execute(
            "INSERT INTO ordering_decisions(show_id,outcome,evidence_fingerprint,prior_ordering,selected_ordering,recorded_at) VALUES(?,?,?,?,?,?)",
            (show_id, outcome, self._fingerprint(evidence_fingerprint), prior_ordering, selected_ordering, self._time(recorded_at)),
        )
        return int(cursor.lastrowid)

    def _record_event(self, decision_id: int, event_type: str, *, evidence_fingerprint: str,
                      ordering: str | None, recorded_at: str) -> None:
        self._connection.execute(
            "INSERT INTO ordering_decision_events(decision_id,event_type,evidence_fingerprint,ordering_value,recorded_at) VALUES(?,?,?,?,?)",
            (decision_id, event_type, self._fingerprint(evidence_fingerprint), ordering, self._time(recorded_at)),
        )

    def record_applied(self, decision_id: int, *, evidence_fingerprint: str,
                       ordering: str, recorded_at: str) -> None:
        if not ordering:
            raise ValueError("applied ordering is required")
        self._record_event(decision_id, "APPLIED", evidence_fingerprint=evidence_fingerprint,
                           ordering=ordering, recorded_at=recorded_at)

    def record_undo(self, decision_id: int, *, evidence_fingerprint: str,
                    ordering: str | None, recorded_at: str) -> None:
        self._record_event(decision_id, "UNDO", evidence_fingerprint=evidence_fingerprint,
                           ordering=ordering, recorded_at=recorded_at)

    def decisions(self) -> list[dict]:
        rows = self._connection.execute(
            "SELECT id,show_id,outcome,evidence_fingerprint,prior_ordering,selected_ordering,recorded_at FROM ordering_decisions ORDER BY id"
        )
        return [dict(row) for row in rows]

    def decision(self, decision_id: int) -> dict:
        row = self._connection.execute(
            "SELECT id,show_id,outcome,evidence_fingerprint,prior_ordering,selected_ordering,recorded_at FROM ordering_decisions WHERE id=?",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise KeyError(decision_id)
        return dict(row)

    def latest_decision(self, show_id: int) -> dict | None:
        row = self._connection.execute(
            "SELECT id,show_id,outcome,evidence_fingerprint,prior_ordering,selected_ordering,recorded_at FROM ordering_decisions WHERE show_id=? ORDER BY id DESC LIMIT 1",
            (show_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def events(self, decision_id: int) -> list[dict]:
        rows = self._connection.execute(
            "SELECT id,decision_id,event_type,evidence_fingerprint,ordering_value,recorded_at FROM ordering_decision_events WHERE decision_id=? ORDER BY id",
            (decision_id,),
        )
        return [dict(row) for row in rows]

    def parity_counts(self) -> dict[str, int]:
        return {
            name: int(self._connection.execute(f"SELECT COUNT(*) FROM ordering_{name}").fetchone()[0])
            for name in ("metadata", "baseline", "shows", "decisions", "decision_events")
        }


class ReconcilerState:
    def __init__(self, store: "StateStore") -> None:
        self._store = store

    def get(self, key: str) -> str | None:
        return self._store.get_metadata("reconciler", key)

    def set(self, key: str, value: str) -> None:
        self._store.connection.execute(
            "INSERT INTO reconciler_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def watermark(self) -> int:
        value = self.get("history_watermark")
        return 0 if value is None else int(value)

    def set_watermark(self, value: int) -> None:
        if value < self.watermark():
            raise ValueError("reconciler watermark cannot move backward")
        self.set("history_watermark", str(value))

    def ingest_event(self, event, *, recorded_at: str) -> bool:
        payload = __import__("json").dumps(
            {
                "history_id": event.history_id, "series_id": event.series_id,
                "episode_id": event.episode_id, "sonarr_file_id": event.sonarr_file_id,
                "download_id": event.download_id, "imported_path": event.imported_path,
                "dropped_path": event.dropped_path, "source_size": event.source_size,
            },
            sort_keys=True, separators=(",", ":"),
        )
        cursor = self._store.connection.execute(
            "INSERT OR IGNORE INTO reconciler_events(history_id,series_id,payload_json,status,fingerprint,first_seen_at,updated_at) VALUES(?,?,?,'pending',NULL,?,?)",
            (event.history_id, event.series_id, payload, recorded_at, recorded_at),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _latest_episode_payloads(payloads: list[dict]) -> list[dict]:
        """Return Sonarr's newest imported file for each exact episode.

        Sonarr can record several successful imports for one episode when a
        release is upgraded.  The earlier rows remain immutable history, but
        they are no longer the episode file that reconciliation must prove.
        Invalid/non-positive episode identities are never collapsed together.
        """
        selected: dict[tuple[str, int], dict] = {}
        for payload in payloads:
            history_id = int(payload["history_id"])
            episode_id = int(payload["episode_id"])
            key = ("episode", episode_id) if episode_id > 0 else ("history", history_id)
            prior = selected.get(key)
            if prior is None or int(prior["history_id"]) < history_id:
                selected[key] = payload
        return sorted(selected.values(), key=lambda payload: int(payload["history_id"]))

    def pending_events_by_series(self) -> dict[int, list[dict]]:
        rows = self._store.connection.execute(
            "SELECT history_id,series_id,payload_json,status,fingerprint FROM reconciler_events WHERE status NOT IN ('completed','action-issued','verification-blocked') ORDER BY history_id"
        )
        grouped: dict[int, list[dict]] = {}
        for row in rows:
            payload = __import__("json").loads(row["payload_json"])
            grouped.setdefault(int(row["series_id"]), []).append(payload | {"status": row["status"]})
        return {
            series_id: self._latest_episode_payloads(payloads)
            for series_id, payloads in grouped.items()
        }

    def pending_series_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self.pending_events_by_series()))

    def series_status_rows(self) -> list[dict]:
        rows = self._store.connection.execute(
            """
            WITH current_event AS (
                SELECT e.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY e.series_id
                           ORDER BY e.updated_at DESC,e.id DESC
                       ) AS position
                FROM reconciler_events e
            )
            SELECT e.series_id,e.status,e.fingerprint,e.updated_at,
                   (SELECT o.evidence_json FROM reconciler_observations o
                    WHERE o.series_id=e.series_id ORDER BY o.id DESC LIMIT 1) AS evidence_json,
                   (SELECT a.verification_json FROM reconciler_actions a
                    WHERE a.series_id=e.series_id ORDER BY a.updated_at DESC,a.id DESC LIMIT 1) AS verification_json
            FROM current_event e
            WHERE e.position=1
            ORDER BY e.updated_at DESC,e.series_id
            """
        )
        result: list[dict] = []
        for row in rows:
            evidence = {}
            if row["evidence_json"]:
                try:
                    evidence = __import__("json").loads(row["evidence_json"])
                except (TypeError, ValueError):
                    evidence = {"reasons": ["stored-evidence-unreadable"]}
            reasons = list(evidence.get("reasons", []))
            if row["status"] == "verification-blocked" and row["verification_json"]:
                try:
                    verification = __import__("json").loads(row["verification_json"])
                    reasons = list(verification.get("reasons", []))
                except (TypeError, ValueError):
                    reasons = ["stored-verification-unreadable"]
            episode_ids = sorted({
                int(payload["episode_id"])
                for payload in self.event_payloads_for_series(int(row["series_id"]))
            })
            result.append(
                {
                    "series_id": int(row["series_id"]),
                    "status": str(row["status"]),
                    "fingerprint": row["fingerprint"],
                    "updated_at": str(row["updated_at"]),
                    "reasons": reasons,
                    "episode_ids": episode_ids,
                    "files": list(evidence.get("files", [])) if isinstance(evidence.get("files", []), list) else [],
                    "rescan_preflight": (
                        dict(evidence.get("rescan_preflight", {}))
                        if isinstance(evidence.get("rescan_preflight", {}), dict)
                        else {}
                    ),
                }
            )
        return result

    def event_payloads_for_series(self, series_id: int) -> list[dict]:
        rows = self._store.connection.execute(
            "SELECT payload_json FROM reconciler_events WHERE series_id=? ORDER BY history_id",
            (series_id,),
        )
        return self._latest_episode_payloads([
            __import__("json").loads(row["payload_json"]) for row in rows
        ])

    def event_payloads_for_action(self, action_id: int, series_id: int) -> list[dict]:
        if action_id <= 0 or series_id <= 0:
            raise ValueError("action and series identities must be positive")
        rows = self._store.connection.execute(
            "SELECT payload_json FROM reconciler_events "
            "WHERE action_id=? AND series_id=? AND status='action-issued' "
            "ORDER BY history_id",
            (action_id, series_id),
        )
        return self._latest_episode_payloads([
            __import__("json").loads(row["payload_json"]) for row in rows
        ])

    def record_observation(self, series_id: int, fingerprint: str, complete: bool,
                           evidence_json: str, *, observed_at: str) -> None:
        self._store.connection.execute(
            "INSERT INTO reconciler_observations(series_id,fingerprint,complete,observed_at,evidence_json) VALUES(?,?,?,?,?)",
            (series_id, fingerprint, int(complete), observed_at, evidence_json),
        )

    def last_observation_fingerprint(self, series_id: int) -> str | None:
        row = self._store.connection.execute(
            "SELECT fingerprint FROM reconciler_observations WHERE series_id=? ORDER BY id DESC LIMIT 1 OFFSET 1",
            (series_id,),
        ).fetchone()
        return None if row is None else str(row["fingerprint"])

    def latest_series_status(self, series_id: int) -> tuple[str | None, tuple[str, ...]]:
        """Return Nagisa's latest stored state before a new observation is recorded."""
        row = self._store.connection.execute(
            """
            SELECT e.status,
                   (SELECT o.evidence_json FROM reconciler_observations o
                    WHERE o.series_id=e.series_id ORDER BY o.id DESC LIMIT 1) AS evidence_json
            FROM reconciler_events e
            WHERE e.series_id=?
            ORDER BY e.updated_at DESC,e.id DESC
            LIMIT 1
            """,
            (series_id,),
        ).fetchone()
        if row is None:
            return None, ()
        reasons: tuple[str, ...] = ()
        if row["evidence_json"]:
            try:
                evidence = __import__("json").loads(row["evidence_json"])
                raw_reasons = evidence.get("reasons", [])
                if isinstance(raw_reasons, list):
                    reasons = tuple(str(reason) for reason in raw_reasons)
            except (TypeError, ValueError):
                reasons = ("stored-evidence-unreadable",)
        return str(row["status"]), reasons

    def update_series(self, series_id: int, status: str, fingerprint: str | None,
                      *, updated_at: str) -> None:
        self._store.connection.execute(
            "UPDATE reconciler_events SET status=?,fingerprint=?,updated_at=? WHERE series_id=? AND status NOT IN ('completed','action-issued')",
            (status, fingerprint, updated_at, series_id),
        )

    def record_action(self, series_id: int, command_id: int, fingerprint: str,
                      future_fingerprint: str, unrelated_fingerprint: str,
                      history_ids: tuple[int, ...], *, created_at: str) -> int:
        exact_history_ids = tuple(sorted(set(int(value) for value in history_ids)))
        if (
            series_id <= 0 or command_id <= 0 or not exact_history_ids
            or any(value <= 0 for value in exact_history_ids)
        ):
            raise ValueError("action requires exact positive identities")
        with self._store.transaction("reconciler") as connection:
            cursor = connection.execute(
                "INSERT INTO reconciler_actions(series_id,command_id,fingerprint,future_fingerprint,unrelated_fingerprint,status,created_at,updated_at,verification_json) VALUES(?,?,?,?,?,'pending',?,?,NULL)",
                (series_id, command_id, fingerprint, future_fingerprint, unrelated_fingerprint, created_at, created_at),
            )
            action_id = int(cursor.lastrowid)
            placeholders = ",".join("?" for _ in exact_history_ids)
            claimed = connection.execute(
                f"UPDATE reconciler_events SET status='action-issued',fingerprint=?,updated_at=?,action_id=? "
                f"WHERE series_id=? AND history_id IN ({placeholders}) "
                "AND status NOT IN ('completed','action-issued','verification-blocked')",
                (fingerprint, created_at, action_id, series_id, *exact_history_ids),
            )
            if claimed.rowcount != len(exact_history_ids):
                raise RuntimeError("action event cohort could not be claimed exactly")
        return action_id

    def pending_actions(self) -> list[dict]:
        rows = self._store.connection.execute(
            "SELECT id,series_id,command_id,fingerprint,future_fingerprint,unrelated_fingerprint,status,created_at FROM reconciler_actions WHERE status='pending' ORDER BY id"
        )
        return [dict(row) for row in rows]

    def finish_action(self, action_id: int, series_id: int, successful: bool,
                      verification_json: str, *, updated_at: str) -> None:
        status = "verified" if successful else "blocked"
        event_status = "completed" if successful else "verification-blocked"
        self._store.connection.execute(
            "UPDATE reconciler_actions SET status=?,updated_at=?,verification_json=? WHERE id=? AND status='pending'",
            (status, updated_at, verification_json, action_id),
        )
        self._store.connection.execute(
            "UPDATE reconciler_events SET status=?,updated_at=? "
            "WHERE series_id=? AND action_id=? AND status='action-issued'",
            (event_status, updated_at, series_id, action_id),
        )


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._create_schema()
        self.ordering = OrderingState(self)
        self.reconciler = ReconcilerState(self)
        self.audit = None

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, source_sha256 TEXT, detail_json TEXT NOT NULL, completed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ordering_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ordering_baseline (show_id INTEGER PRIMARY KEY CHECK(show_id > 0), preference TEXT, recorded_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ordering_shows (show_id INTEGER PRIMARY KEY CHECK(show_id > 0), first_seen_at TEXT NOT NULL, initial_preference TEXT, locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1)), lock_reason TEXT, locked_at TEXT);
            CREATE TABLE IF NOT EXISTS ordering_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, show_id INTEGER NOT NULL REFERENCES ordering_shows(show_id), outcome TEXT NOT NULL, evidence_fingerprint TEXT NOT NULL, prior_ordering TEXT, selected_ordering TEXT, recorded_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ordering_decision_events (id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id INTEGER NOT NULL REFERENCES ordering_decisions(id), event_type TEXT NOT NULL CHECK(event_type IN ('APPLIED','UNDO')), evidence_fingerprint TEXT NOT NULL, ordering_value TEXT, recorded_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reconciler_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reconciler_events (id INTEGER PRIMARY KEY AUTOINCREMENT, history_id INTEGER NOT NULL UNIQUE, series_id INTEGER NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL, fingerprint TEXT, first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL, action_id INTEGER REFERENCES reconciler_actions(id));
            CREATE TABLE IF NOT EXISTS reconciler_observations (id INTEGER PRIMARY KEY AUTOINCREMENT, series_id INTEGER NOT NULL, fingerprint TEXT NOT NULL, complete INTEGER NOT NULL CHECK(complete IN (0,1)), observed_at TEXT NOT NULL, evidence_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reconciler_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, series_id INTEGER NOT NULL, command_id INTEGER NOT NULL UNIQUE, fingerprint TEXT NOT NULL, future_fingerprint TEXT NOT NULL, unrelated_fingerprint TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, verification_json TEXT);
            CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, module TEXT NOT NULL, event_type TEXT NOT NULL, recorded_at TEXT NOT NULL, evidence_json TEXT NOT NULL);
            """
        )
        current = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if current == 0:
            self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        elif current == 1:
            with self.transaction("migration") as connection:
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(reconciler_events)")
                }
                if "action_id" not in columns:
                    connection.execute(
                        "ALTER TABLE reconciler_events ADD COLUMN action_id INTEGER "
                        "REFERENCES reconciler_actions(id)"
                    )
                connection.execute(
                    "UPDATE reconciler_events SET action_id=("
                    "SELECT MIN(a.id) FROM reconciler_actions a "
                    "WHERE a.series_id=reconciler_events.series_id "
                    "AND a.status='pending' AND a.created_at=reconciler_events.updated_at"
                    ") WHERE status='action-issued' AND action_id IS NULL AND 1=("
                    "SELECT COUNT(*) FROM reconciler_actions a "
                    "WHERE a.series_id=reconciler_events.series_id "
                    "AND a.status='pending' AND a.created_at=reconciler_events.updated_at)"
                )
                connection.execute("PRAGMA user_version=2")
        elif str(current) != SCHEMA_VERSION:
            raise UnsupportedStateVersion(str(current))
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS reconciler_events_action_id "
            "ON reconciler_events(action_id,history_id)"
        )

    @contextmanager
    def transaction(self, module: str) -> Iterator[sqlite3.Connection]:
        if module not in {"ordering", "reconciler", "audit", "migration"}:
            raise ValueError("unknown transaction namespace")
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")

    def get_metadata(self, module: str, key: str) -> str | None:
        if module not in {"ordering", "reconciler"}:
            raise ValueError("unknown metadata namespace")
        row = self.connection.execute(f"SELECT value FROM {module}_metadata WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def quick_check(self) -> str:
        return str(self.connection.execute("PRAGMA quick_check").fetchone()[0])

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
