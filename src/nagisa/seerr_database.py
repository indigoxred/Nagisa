"""Opt-in, version-locked availability writes for a stock Seerr database.

This never changes Seerr code, requests, media files, or creates catalogue rows.
The private candidate is not deployed. A replacement Seerr with a new
schema/migration set disables this optional adapter until reviewed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


_TABLES = ('media', 'media_request', 'migrations', 'season', 'season_request')
_STATUS = frozenset((3, 4, 5))


def _fingerprint(db: sqlite3.Connection) -> tuple[str, str]:
    definitions = db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' "
        "AND name IN (?,?,?,?,?) ORDER BY name", _TABLES).fetchall()
    if tuple(row[0] for row in definitions) != _TABLES:
        raise ValueError('Seerr availability tables are missing or renamed')
    migrations = db.execute(
        'SELECT timestamp,name FROM migrations ORDER BY timestamp,name').fetchall()
    if not migrations or any(type(row[0]) is not int or not isinstance(row[1], str)
                             for row in migrations):
        raise ValueError('Seerr migrations are missing or invalid')
    def digest(value: object) -> str:
        return hashlib.sha256(json.dumps(value, separators=(',', ':'),
            ensure_ascii=False).encode('utf-8')).hexdigest()
    return digest(definitions), digest(migrations)


def database_fingerprint(path: str | Path) -> tuple[str, str]:
    """Read a private database copy to pin one verified stock schema."""
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError('Seerr database must be an existing regular file')
    with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)) as db:
        return _fingerprint(db)


class SeerrDatabase:
    def __init__(self, path: str | Path, *, expected_schema: str,
                 expected_migrations: str, expected_version: str,
                 version: Callable[[], str],
                 write_enabled: bool = True) -> None:
        self.path = Path(path)
        if (not self.path.is_absolute() or self.path.is_symlink()
                or not callable(version)
                or not isinstance(expected_version, str)
                or not expected_version
                or any(not isinstance(value, str) or len(value) != 64
                       for value in (expected_schema, expected_migrations))):
            raise ValueError('Seerr database boundary is invalid')
        self.expected = expected_schema, expected_migrations
        self.expected_version = expected_version
        self.version = version
        self.write_enabled = write_enabled

    def apply(self, *, media_id: int, tmdb_id: int, tvdb_id: int,
              expected_parent: int, expected_seasons: dict[int, int],
              desired_seasons: dict[int, int]) -> str:
        """Atomically change only existing, exactly matched anime badges."""
        if (any(type(value) is not int or value <= 0
                for value in (media_id, tmdb_id, tvdb_id))
                or type(expected_parent) is not int
                or expected_parent not in range(1, 8)
                or not isinstance(expected_seasons, dict)
                or not isinstance(desired_seasons, dict)
                or not 1 <= len(expected_seasons) <= 100
                or set(expected_seasons) != set(desired_seasons)
                or any(type(number) is not int or number < 0
                       for number in expected_seasons)
                or any(type(status) is not int or status not in range(1, 8)
                       for status in expected_seasons.values())
                or any(type(status) is not int or status not in _STATUS
                       for status in desired_seasons.values())):
            return 'held'
        if self.version() != self.expected_version:
            raise ValueError('Seerr version changed; availability writer disabled')
        if not self.path.is_file() or self.path.is_symlink():
            raise ValueError('Seerr database is unavailable; availability writer disabled')
        uri = f'file:{self.path.as_posix()}?mode={"rw" if self.write_enabled else "ro"}'
        with closing(sqlite3.connect(uri, uri=True, timeout=2,
                                     isolation_level=None)) as db:
            try:
                db.execute('BEGIN IMMEDIATE' if self.write_enabled else 'BEGIN')
                if _fingerprint(db) != self.expected:
                    raise ValueError('Seerr schema or migrations changed; writer disabled')
                row = db.execute(
                    'SELECT mediaType,tmdbId,tvdbId,status FROM media WHERE id=?',
                    (media_id,)).fetchone()
                if row != ('tv', tmdb_id, tvdb_id, expected_parent):
                    return 'held'
                rows = db.execute(
                    'SELECT id,seasonNumber,status FROM season WHERE mediaId=?',
                    (media_id,)).fetchall()
                if (len(rows) != len(expected_seasons)
                        or len({row[1] for row in rows}) != len(rows)
                        or {number: status for _, number, status in rows}
                        != expected_seasons):
                    return 'held'
                statuses = tuple(desired_seasons.values())
                parent = (5 if all(status == 5 for status in statuses)
                          else 4 if any(status in (4, 5) for status in statuses)
                          else 3)
                if (parent == expected_parent
                        and desired_seasons == expected_seasons):
                    return 'unchanged'
                if not self.write_enabled:
                    return 'would_update'
                now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                for season_id, number, previous in rows:
                    status = desired_seasons[number]
                    if status != previous:
                        changed = db.execute(
                            'UPDATE season SET status=?,updatedAt=? '
                            'WHERE id=? AND mediaId=? AND status=?',
                            (status, now, season_id, media_id, previous)).rowcount
                        if changed != 1:
                            raise RuntimeError('Seerr season changed during transaction')
                if parent != expected_parent:
                    changed = db.execute(
                        'UPDATE media SET status=?,updatedAt=?,lastSeasonChange=? '
                        'WHERE id=? AND mediaType=? AND tmdbId=? AND tvdbId=? AND status=?',
                        (parent, now, now, media_id, 'tv', tmdb_id, tvdb_id,
                         expected_parent)).rowcount
                    if changed != 1:
                        raise RuntimeError('Seerr parent changed during transaction')
                db.execute('COMMIT')
                return 'updated'
            finally:
                if db.in_transaction:
                    db.execute('ROLLBACK')
