"""Private Nagisa-owned link view; never writes or removes a source video.

An SQLite ledger gives each link a small durable row instead of rewriting a
catalogue-sized JSON manifest for every file in the initial export.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Iterator

from .seerr_compat_links import LinkProof


_SLOT = re.compile(r'([1-9][0-9]*)/(0|[1-9][0-9]*)/([1-9][0-9]*)\Z')
_VIDEO_SUFFIXES = {'.mkv', '.mp4', '.m4v', '.avi', '.ts'}
_COLUMNS = ('slot', 'file_id', 'series_id', 'anidb_episode_id',
            'tmdb_episode_id', 'tvdb_episode_id', 'tmdb_show_id',
            'suffix', 'link', 'target', 'state', 'old_target')


class CompatibilityLinkStore:
    """Owns one isolated root and refuses to replace unowned paths."""

    def __init__(self, root: Path, *, identity_provider: str = 'tvdb'):
        if identity_provider not in ('tvdb', 'tmdb'):
            raise ValueError('unsupported compatibility identity provider')
        self.identity_provider = identity_provider
        self.root = Path(root)
        if (not self.root.is_absolute() or not self.root.is_dir()
                or self.root.is_symlink()):
            raise ValueError('compatibility root must be an existing real directory')
        self.root = self.root.resolve()
        suffix = '' if identity_provider == 'tvdb' else '-tmdb'
        self.path = self.root / f'.nagisa-compat{suffix}.sqlite'
        self.lock_path = self.root / f'.nagisa-compat{suffix}.lock'
        if self.path.is_symlink() or self.lock_path.is_symlink():
            raise ValueError('compatibility ledger path cannot be a symlink')
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS links (
                slot TEXT PRIMARY KEY,
                file_id INTEGER NOT NULL UNIQUE,
                series_id INTEGER NOT NULL,
                anidb_episode_id INTEGER NOT NULL,
                tmdb_episode_id INTEGER NOT NULL,
                tvdb_episode_id INTEGER,
                tmdb_show_id INTEGER NOT NULL,
                suffix TEXT NOT NULL,
                link TEXT NOT NULL,
                target TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending','ready')),
                old_target TEXT
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS contested_claims (
                slot TEXT NOT NULL,
                file_id INTEGER NOT NULL,
                series_id INTEGER NOT NULL,
                shoko_episode_id INTEGER NOT NULL,
                PRIMARY KEY(slot, file_id)
            )''')
            if self.identity_provider == 'tmdb':
                db.execute('''CREATE TABLE IF NOT EXISTS quarantined_shows (
                    show_id INTEGER PRIMARY KEY,
                    group_key TEXT NOT NULL
                )''')
        self.manifest()  # Reject a changed or foreign ledger before use.
        self._recover_contests()
        if self.identity_provider == 'tmdb':
            self._recover_quarantine()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        os.close(self._open_private_file(self.path))
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if self.lock_path.is_symlink():
            raise ValueError('compatibility lock path cannot be a symlink')
        import fcntl  # Linux-only link writer; not imported during Windows tests.
        with os.fdopen(self._open_private_file(self.lock_path), 'r+b') as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _open_private_file(path: Path) -> int:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags, 0o600)
        try:
            if hasattr(os, 'fchmod'):
                os.fchmod(fd, 0o600)
            else:  # Windows-only fixture runs; operational store is Linux.
                path.chmod(0o600)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _link_for(self, slot: str, suffix: str) -> Path:
        if suffix not in _VIDEO_SUFFIXES:
            raise ValueError('unsupported compatibility video suffix')
        match = _SLOT.fullmatch(slot)
        if not match:
            raise ValueError('unsafe compatibility slot')
        show_id, season, episode = map(int, match.groups())
        if season > 999 or episode > 9999:
            raise ValueError('compatibility coordinate out of bounds')
        return (self.root / f'{self.identity_provider}-{show_id}' /
                f'Season {season:02d}' /
                f'S{season:02d}E{episode:02d}{suffix}')

    def proof_show_id(self, proof: LinkProof) -> int:
        if not isinstance(proof, LinkProof):
            raise ValueError('exact compatibility proof required')
        value = (proof.tmdb_show_id if self.identity_provider == 'tmdb'
                 else proof.tvdb_show_id)
        if type(value) is not int or value <= 0:
            raise ValueError('exact compatibility show identity required')
        return value

    def _row(self, row: sqlite3.Row) -> dict:
        value = {key: row[key] for key in _COLUMNS}
        expected = self._link_for(value['slot'], value['suffix'])
        if (value['link'] != str(expected.relative_to(self.root))
                or (self.identity_provider == 'tmdb' and
                    int(value['slot'].split('/')[0]) != value['tmdb_show_id'])
                or any(type(value[key]) is not int or value[key] <= 0 for key in
                       ('file_id', 'series_id', 'anidb_episode_id',
                        'tmdb_episode_id', 'tmdb_show_id'))
                or (value['tvdb_episode_id'] is not None
                    and (type(value['tvdb_episode_id']) is not int
                         or value['tvdb_episode_id'] <= 0))
                or not isinstance(value['target'], str)
                or not value['target'].startswith('/')
                or value['state'] not in ('pending', 'ready')
                or (value['old_target'] is not None
                    and (not isinstance(value['old_target'], str)
                         or not value['old_target'].startswith('/')))):
            raise ValueError('compatibility ledger contains unsafe entry')
        return value

    def manifest(self) -> dict:
        with self._connect() as db:
            rows = db.execute('SELECT * FROM links').fetchall()
        return {'version': 2,
                'links': {row['slot']: self._row(row) for row in rows}}

    def show_links(self, show_id: int) -> frozenset[str]:
        """Return only verified, ready link paths for one exact TVDB show."""
        if type(show_id) is not int or show_id <= 0:
            raise ValueError('exact compatibility show ID required')
        with self._connect() as db:
            contested = db.execute('''SELECT 1 FROM links AS l
                JOIN contested_claims AS c ON c.slot=l.slot
                WHERE l.slot LIKE ? LIMIT 1''',
                (f'{show_id}/%',)).fetchone()
            if contested:
                raise ValueError('contested compatibility link not withdrawn')
            rows = db.execute(
                'SELECT * FROM links WHERE slot LIKE ? AND state=?',
                (f'{show_id}/%', 'ready')).fetchall()
        links = set()
        for row in rows:
            item = self._row(row)
            link = self.root / item['link']
            if not link.is_symlink() or os.readlink(link) != item['target']:
                raise ValueError('compatibility link differs from owned ledger')
            links.add(item['link'])
        return frozenset(links)

    @staticmethod
    def _quarantine_key(show_ids: tuple[int, ...]) -> str:
        if (not isinstance(show_ids, tuple) or not 2 <= len(show_ids) <= 20
                or any(type(show) is not int or show <= 0 for show in show_ids)
                or tuple(sorted(set(show_ids))) != show_ids):
            raise ValueError('exact sorted ambiguous TMDB IDs required')
        return ','.join(map(str, show_ids))

    def quarantined_show_ids(self, show_ids: tuple[int, ...]) -> bool:
        if self.identity_provider != 'tmdb':
            raise ValueError('quarantine requires TMDB compatibility mode')
        key = self._quarantine_key(show_ids)
        with self._connect() as db:
            rows = db.execute('SELECT show_id,group_key FROM quarantined_shows '
                              'WHERE show_id IN (' + ','.join('?' for _ in show_ids)
                              + ')', show_ids).fetchall()
        return len(rows) == len(show_ids) and all(row['group_key'] == key for row in rows)

    def _withdraw_quarantined(self, db: sqlite3.Connection,
                              show_ids: tuple[int, ...]) -> None:
        for show_id in show_ids:
            rows = db.execute('SELECT * FROM links WHERE slot LIKE ? ORDER BY slot',
                              (f'{show_id}/%',)).fetchall()
            for stored in rows:
                row = self._row(stored)
                if row['state'] != 'ready':
                    raise ValueError('pending private link cannot be quarantined')
                link = self._link_for(row['slot'], row['suffix'])
                if self._check_existing_link(link, (row['target'],)):
                    link.unlink()
                db.execute('DELETE FROM links WHERE slot=?', (row['slot'],))
                db.commit()  # Restart finishes any remaining exact owned links.

    def _recover_quarantine(self) -> None:
        with self._locked():
            with self._connect() as db:
                keys = db.execute('SELECT DISTINCT group_key FROM quarantined_shows '
                                  'ORDER BY group_key').fetchall()
                for item in keys:
                    try:
                        ids = tuple(int(part) for part in item['group_key'].split(','))
                    except ValueError:
                        raise ValueError('private quarantine record is malformed') from None
                    if self._quarantine_key(ids) != item['group_key']:
                        raise ValueError('private quarantine group changed')
                    members = tuple(row['show_id'] for row in db.execute(
                        'SELECT show_id FROM quarantined_shows WHERE group_key=? '
                        'ORDER BY show_id', (item['group_key'],)))
                    if members != ids:
                        raise ValueError('private quarantine group is incomplete')
                    self._withdraw_quarantined(db, ids)

    def quarantine_card(self, show_ids: tuple[int, ...],
                        expected_links: frozenset[str]) -> tuple[int, ...]:
        if self.identity_provider != 'tmdb':
            raise ValueError('quarantine requires TMDB compatibility mode')
        key = self._quarantine_key(show_ids)
        if not isinstance(expected_links, frozenset) or not expected_links:
            raise ValueError('complete private card Parts required')
        with self._locked():
            with self._connect() as db:
                existing = db.execute(
                    'SELECT show_id FROM quarantined_shows WHERE show_id IN ('
                    + ','.join('?' for _ in show_ids) + ')', show_ids).fetchall()
                if existing:
                    raise ValueError('private card quarantine already exists')
                paths = set()
                file_ids = set()
                for show_id in show_ids:
                    if db.execute('SELECT 1 FROM contested_claims WHERE slot LIKE ? '
                                  'LIMIT 1', (f'{show_id}/%',)).fetchone():
                        raise ValueError('private card has a contested claim')
                    rows = db.execute('SELECT * FROM links WHERE slot LIKE ?',
                                      (f'{show_id}/%',)).fetchall()
                    for stored in rows:
                        row = self._row(stored)
                        if row['state'] != 'ready':
                            raise ValueError('private card has a pending claim')
                        link = self._link_for(row['slot'], row['suffix'])
                        if not self._check_existing_link(link, (row['target'],)):
                            raise ValueError('private card owned link is missing')
                        paths.add(row['link'])
                        file_ids.add(row['file_id'])
                if frozenset(paths) != expected_links or len(paths) != len(file_ids):
                    raise ValueError('private card Parts differ from owned union')
                db.executemany('INSERT INTO quarantined_shows VALUES (?,?)',
                               ((show_id, key) for show_id in show_ids))
                db.commit()  # Durable quarantine precedes any private unlink.
                self._withdraw_quarantined(db, show_ids)
                return tuple(sorted(file_ids))

    def lookup_file(self, file_id: int) -> dict | None:
        if type(file_id) is not int or file_id <= 0:
            raise ValueError('exact file ID required')
        with self._connect() as db:
            row = db.execute('SELECT * FROM links WHERE file_id=?',
                             (file_id,)).fetchone()
        return self._row(row) if row is not None else None

    def owned_file_ids_after(self, last_file_id: int,
                             limit: int) -> tuple[int, ...]:
        """Page owned and contested files without a catalogue-wide read."""
        if (type(last_file_id) is not int or last_file_id < 0
                or type(limit) is not int or not 1 <= limit <= 100):
            raise ValueError('bounded owned mapping page required')
        with self._connect() as db:
            rows = db.execute('''SELECT file_id FROM links
                WHERE file_id > ? UNION SELECT file_id FROM contested_claims
                WHERE file_id > ? ORDER BY file_id LIMIT ?''',
                (last_file_id, last_file_id, limit)).fetchall()
        ids = tuple(row['file_id'] for row in rows)
        if any(type(item) is not int or item <= last_file_id
               for item in ids):
            raise ValueError('owned mapping file page changed')
        return ids

    def owned_series_ids_after(self, last_series_id: int,
                               limit: int) -> tuple[int, ...]:
        """Page series with an owned or contested claim for bounded rechecks."""
        if (type(last_series_id) is not int or last_series_id < 0
                or type(limit) is not int or not 1 <= limit <= 25):
            raise ValueError('bounded owned series page required')
        with self._connect() as db:
            rows = db.execute('''SELECT series_id FROM links
                WHERE series_id > ? UNION SELECT series_id FROM contested_claims
                WHERE series_id > ? ORDER BY series_id LIMIT ?''',
                (last_series_id, last_series_id, limit)).fetchall()
        ids = tuple(row['series_id'] for row in rows)
        if any(type(item) is not int or item <= last_series_id
               for item in ids):
            raise ValueError('owned series page changed')
        return ids

    def owned_file_ids_for_shows(self, show_ids: tuple[int, ...]) -> tuple[int, ...]:
        """Read exact owned and contested IDs for a targeted mapping recheck."""
        if (not isinstance(show_ids, tuple) or len(show_ids) > 100
                or any(type(show) is not int or show <= 0 for show in show_ids)
                or tuple(sorted(set(show_ids))) != show_ids):
            raise ValueError('exact sorted TVDB show IDs required')
        found: set[int] = set()
        with self._connect() as db:
            for show in show_ids:
                for table in ('links', 'contested_claims'):
                    rows = db.execute(
                        f'SELECT slot,file_id FROM {table} WHERE slot LIKE ?',
                        (f'{show}/%',)).fetchall()
                    for row in rows:
                        match = _SLOT.fullmatch(row['slot'])
                        file_id = row['file_id']
                        if (not match or int(match.group(1)) != show
                                or type(file_id) is not int or file_id <= 0):
                            raise ValueError('unsafe owned mapping identity')
                        found.add(file_id)
                        if len(found) > 32_768:
                            raise ValueError('owned mapping recheck exceeds bound')
        return tuple(sorted(found))

    def lookup_slot(self, show_id: int, season: int,
                    episode: int) -> dict | None:
        if (type(show_id) is not int or show_id <= 0
                or type(season) is not int or season < 0
                or type(episode) is not int or episode <= 0):
            raise ValueError('exact compatibility slot required')
        slot = f'{show_id}/{season}/{episode}'
        with self._connect() as db:
            row = db.execute('SELECT * FROM links WHERE slot=?',
                             (slot,)).fetchone()
        return self._row(row) if row is not None else None

    @staticmethod
    def _slot(show_id: int, season: int, episode: int) -> str:
        if (type(show_id) is not int or show_id <= 0
                or type(season) is not int or not 0 <= season <= 999
                or type(episode) is not int or not 0 < episode <= 9999):
            raise ValueError('exact compatibility slot required')
        return f'{show_id}/{season}/{episode}'

    def contested_claims(self, show_id: int, season: int,
                         episode: int) -> tuple[int, ...]:
        slot = self._slot(show_id, season, episode)
        with self._connect() as db:
            rows = db.execute('''SELECT file_id FROM contested_claims
                WHERE slot=? ORDER BY file_id''', (slot,)).fetchall()
        return tuple(row['file_id'] for row in rows)

    def contested_records(self, show_id: int, season: int,
                          episode: int) -> tuple[dict, ...]:
        slot = self._slot(show_id, season, episode)
        with self._connect() as db:
            rows = db.execute('''SELECT file_id, series_id,
                shoko_episode_id FROM contested_claims
                WHERE slot=? ORDER BY file_id''', (slot,)).fetchall()
        return tuple(dict(row) for row in rows)

    def contested_slots_for_file(self, file_id: int) -> tuple[tuple[int, int, int], ...]:
        if type(file_id) is not int or file_id <= 0:
            raise ValueError('exact contested file ID required')
        with self._connect() as db:
            rows = db.execute('''SELECT slot FROM contested_claims
                WHERE file_id=? ORDER BY slot''', (file_id,)).fetchall()
        slots = []
        for row in rows:
            match = _SLOT.fullmatch(row['slot'])
            if not match:
                raise ValueError('unsafe contested compatibility slot')
            slots.append(tuple(map(int, match.groups())))
        return tuple(slots)

    def clear_empty_contest(self, show_id: int, season: int,
                            episode: int, *,
                            expected_file_ids: tuple[int, ...]) -> None:
        slot = self._slot(show_id, season, episode)
        if (not isinstance(expected_file_ids, tuple)
                or not 2 <= len(expected_file_ids) <= 50
                or tuple(sorted(set(expected_file_ids))) != expected_file_ids):
            raise ValueError('exact contested claim census required')
        with self._locked():
            with self._connect() as db:
                claims = tuple(row['file_id'] for row in db.execute(
                    '''SELECT file_id FROM contested_claims
                       WHERE slot=? ORDER BY file_id''', (slot,)))
                if claims != expected_file_ids:
                    raise ValueError('contested claim census changed')
                if db.execute('SELECT 1 FROM links WHERE slot=?',
                              (slot,)).fetchone() is not None:
                    raise ValueError('contested link was not withdrawn')
                db.execute('DELETE FROM contested_claims WHERE slot=?',
                           (slot,))
                db.commit()

    def add_contested_claim(self, proof: LinkProof) -> None:
        if (not isinstance(proof, LinkProof)
                or any(type(value) is not int or value <= 0 for value in
                       (proof.shoko_file_id, proof.shoko_series_id,
                        proof.shoko_episode_id))):
            raise ValueError('exact contested Shoko file proof required')
        slot = self._slot(self.proof_show_id(proof), proof.season, proof.episode)
        with self._locked():
            with self._connect() as db:
                if db.execute('''SELECT 1 FROM contested_claims
                    WHERE slot=? LIMIT 1''', (slot,)).fetchone() is None:
                    raise ValueError('compatibility slot is not contested')
                self._record_contested_claim(db, slot, proof)
                db.commit()

    @staticmethod
    def _record_contested_claim(db: sqlite3.Connection, slot: str,
                                proof: LinkProof) -> None:
        # A Shoko correction can change the episode identity while retaining
        # the file ID. The worker re-reads every claimant before resolving.
        db.execute('''INSERT INTO contested_claims
            (slot, file_id, series_id, shoko_episode_id) VALUES (?,?,?,?)
            ON CONFLICT(slot, file_id) DO UPDATE SET
            series_id=excluded.series_id,
            shoko_episode_id=excluded.shoko_episode_id''',
            (slot, proof.shoko_file_id,
             proof.shoko_series_id, proof.shoko_episode_id))

    def _withdraw_contested(self, db: sqlite3.Connection,
                            slot: str) -> None:
        stored = db.execute('SELECT * FROM links WHERE slot=?',
                            (slot,)).fetchone()
        if stored is None:
            return
        row = self._row(stored)
        if row['state'] != 'ready':
            raise ValueError('pending compatibility link cannot be contested')
        link = self._link_for(row['slot'], row['suffix'])
        if self._check_existing_link(link, (row['target'],)):
            link.unlink()
        db.execute('DELETE FROM links WHERE slot=?', (slot,))
        db.commit()

    def _recover_contests(self) -> None:
        with self._locked():
            with self._connect() as db:
                slots = db.execute('''SELECT DISTINCT slot
                    FROM contested_claims ORDER BY slot''').fetchall()
                for item in slots:
                    slot = item['slot']
                    if not _SLOT.fullmatch(slot):
                        raise ValueError('unsafe contested compatibility slot')
                    self._withdraw_contested(db, slot)

    def mark_contested(self, incumbent: LinkProof,
                       newcomer: LinkProof) -> None:
        if (not isinstance(incumbent, LinkProof)
                or not isinstance(newcomer, LinkProof)
                or incumbent.shoko_file_id == newcomer.shoko_file_id
                or any(type(value) is not int or value <= 0 for value in
                       (incumbent.shoko_file_id, newcomer.shoko_file_id,
                        incumbent.shoko_series_id, newcomer.shoko_series_id,
                        incumbent.shoko_episode_id,
                        newcomer.shoko_episode_id))):
            raise ValueError('two exact conflicting Shoko file proofs required')
        slot = self._slot(self.proof_show_id(incumbent), incumbent.season,
                          incumbent.episode)
        if slot != self._slot(self.proof_show_id(newcomer), newcomer.season,
                             newcomer.episode):
            raise ValueError('contested proofs must claim one slot')
        with self._locked():
            with self._connect() as db:
                stored = db.execute('SELECT * FROM links WHERE slot=?',
                                    (slot,)).fetchone()
                if stored is not None:
                    row = self._row(stored)
                    if (row['file_id'] != incumbent.shoko_file_id
                            or row['series_id'] != incumbent.shoko_series_id
                            or row['anidb_episode_id'] !=
                            incumbent.anidb_episode_id
                            or row['tmdb_episode_id'] !=
                            incumbent.tmdb_episode_id
                            or row['tvdb_episode_id'] !=
                            incumbent.tvdb_episode_id
                            or row['tmdb_show_id'] !=
                            incumbent.tmdb_show_id
                            or row['suffix'] !=
                            incumbent.source_path.suffix.lower()
                            or row['target'] != incumbent.plex_target):
                        raise ValueError('incumbent link changed before contest')
                for proof in (incumbent, newcomer):
                    self._record_contested_claim(db, slot, proof)
                db.commit()  # Durable hold precedes owned-link withdrawal.
                self._withdraw_contested(db, slot)

    def resolve_contest(self, proof: LinkProof, *,
                        expected_file_ids: tuple[int, ...]) -> str:
        """Replace an exact, externally rechecked contest with one survivor."""
        if (not isinstance(proof, LinkProof)
                or not proof.source_path.is_file()
                or proof.source_path.stat().st_size != proof.file_size
                or not proof.plex_target.startswith('/')
                or type(proof.shoko_file_id) is not int
                or proof.shoko_file_id <= 0
                or not isinstance(expected_file_ids, tuple)
                or not 2 <= len(expected_file_ids) <= 50
                or any(type(item) is not int or item <= 0
                       for item in expected_file_ids)
                or tuple(sorted(set(expected_file_ids))) != expected_file_ids
                or proof.shoko_file_id not in expected_file_ids):
            raise ValueError('exact surviving claim and file census required')
        slot = self._slot(self.proof_show_id(proof), proof.season, proof.episode)
        suffix = proof.source_path.suffix.lower()
        link = self._link_for(slot, suffix)
        with self._locked():
            with self._connect() as db:
                claims = tuple(row['file_id'] for row in db.execute(
                    '''SELECT file_id FROM contested_claims
                       WHERE slot=? ORDER BY file_id''', (slot,)))
                if claims != expected_file_ids:
                    raise ValueError('contested claim census changed')
                if db.execute('SELECT 1 FROM links WHERE slot=?',
                              (slot,)).fetchone() is not None:
                    raise ValueError('contested link was not withdrawn')
                if db.execute('SELECT 1 FROM links WHERE file_id=?',
                              (proof.shoko_file_id,)).fetchone() is not None:
                    raise ValueError('surviving file already owns another link')
                if os.path.lexists(link):
                    raise ValueError('contested compatibility path is occupied')
                self._prepare_directory(link, self.proof_show_id(proof))
                db.execute('''INSERT INTO links VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,NULL)''',
                    (slot, proof.shoko_file_id, proof.shoko_series_id,
                     proof.anidb_episode_id, proof.tmdb_episode_id,
                     proof.tvdb_episode_id, proof.tmdb_show_id, suffix,
                     str(link.relative_to(self.root)), proof.plex_target,
                     'pending'))
                db.execute('DELETE FROM contested_claims WHERE slot=?',
                           (slot,))
                db.commit()  # Pending owner and contest release are atomic.
                temporary = (link.parent /
                             f'.nagisa-{proof.shoko_file_id}-{os.getpid()}.tmp')
                if os.path.lexists(temporary):
                    raise ValueError('compatibility staging path occupied')
                try:
                    os.symlink(proof.plex_target, temporary)
                    os.replace(temporary, link)
                finally:
                    if os.path.lexists(temporary):
                        os.unlink(temporary)
                db.execute('''UPDATE links SET state='ready'
                    WHERE slot=?''', (slot,))
                db.commit()
                return 'created'

    def _prepare_directory(self, link: Path, show_id: int) -> None:
        show = self.root / f'{self.identity_provider}-{show_id}'
        for directory in (show, link.parent):
            if directory.is_symlink():
                raise ValueError('compatibility directory cannot be a symlink')
            directory.mkdir(mode=0o755, exist_ok=True)
            mode = stat.S_IMODE(directory.stat().st_mode)
            if directory.stat().st_uid == os.geteuid():
                directory.chmod((mode & 0o7000) | 0o755)
            elif mode & 0o055 != 0o055:
                raise ValueError('compatibility directory is not Plex readable')
        match = show / '.plexmatch'
        expected = self.plexmatch_text(show_id, provider=self.identity_provider)
        if match.is_symlink():
            raise ValueError('Plex match hint cannot be a symlink')
        if match.exists():
            if match.read_text(encoding='utf-8') != expected:
                raise ValueError('Plex match hint conflicts with verified identity')
        else:
            try:
                with match.open('x', encoding='utf-8') as stream:
                    stream.write(expected)
            except FileExistsError:
                raise ValueError('Plex match hint was concurrently replaced')
        mode = stat.S_IMODE(match.stat().st_mode)
        if match.stat().st_uid == os.geteuid():
            match.chmod(0o644)
        elif mode & 0o044 != 0o044:
            raise ValueError('Plex match hint is not Plex readable')

    @staticmethod
    def plexmatch_text(show_id: int, *, provider: str = 'tvdb') -> str:
        if (type(show_id) is not int or show_id <= 0
                or provider not in ('tvdb', 'tmdb')):
            raise ValueError('exact show ID and provider required for Plex match hint')
        return f'{provider}id: {show_id}\n'

    @staticmethod
    def _check_existing_link(link: Path, expected: tuple[str, ...]) -> bool:
        if not os.path.lexists(link):
            return False
        if not link.is_symlink() or os.readlink(link) not in expected:
            raise ValueError('compatibility link is foreign or changed')
        return True

    def upsert(self, proof: LinkProof) -> str:
        if (not isinstance(proof, LinkProof)
                or not proof.source_path.is_file()
                or proof.source_path.stat().st_size != proof.file_size
                or not proof.plex_target.startswith('/')
                or any(type(value) is not int or value <= 0 for value in
                       (proof.shoko_file_id, proof.shoko_series_id,
                        self.proof_show_id(proof), proof.episode))
                or type(proof.season) is not int or proof.season < 0):
            raise ValueError('current exact link proof required')
        slot = f'{self.proof_show_id(proof)}/{proof.season}/{proof.episode}'
        suffix = proof.source_path.suffix.lower()
        link = self._link_for(slot, suffix)
        with self._locked():
            with self._connect() as db:
                if (self.identity_provider == 'tmdb' and db.execute(
                        'SELECT 1 FROM quarantined_shows WHERE show_id=?',
                        (self.proof_show_id(proof),)).fetchone() is not None):
                    return 'quarantined'
                contest = db.execute('''SELECT 1 FROM contested_claims
                    WHERE slot=? LIMIT 1''', (slot,)).fetchone()
                if contest is not None:
                    if (type(proof.shoko_episode_id) is not int
                            or proof.shoko_episode_id <= 0):
                        raise ValueError('exact contested Shoko episode required')
                    self._record_contested_claim(db, slot, proof)
                    db.commit()
                    raise ValueError('compatibility slot contested')
                by_file = db.execute('SELECT slot FROM links WHERE file_id=?',
                                     (proof.shoko_file_id,)).fetchone()
                if by_file is not None and by_file['slot'] != slot:
                    raise ValueError('compatibility file moved episode slot')
                stored = db.execute('SELECT * FROM links WHERE slot=?',
                                    (slot,)).fetchone()
                old = self._row(stored) if stored is not None else None
                if old and old['file_id'] != proof.shoko_file_id:
                    raise ValueError('compatibility episode occupied by another file')
                if old and old['suffix'] != suffix:
                    raise ValueError('compatibility file extension changed')
                if old and (old['series_id'] != proof.shoko_series_id
                            or old['anidb_episode_id'] != proof.anidb_episode_id
                            or old['tmdb_episode_id'] != proof.tmdb_episode_id
                            or old['tvdb_episode_id'] != proof.tvdb_episode_id
                            or old['tmdb_show_id'] != proof.tmdb_show_id):
                    raise ValueError('compatibility identity changed')
                if old is None and os.path.lexists(link):
                    raise ValueError('compatibility link is foreign')
                if old is not None:
                    allowed = (old['target'],) if old['state'] == 'ready' else (
                        old['target'], old['old_target'])
                    exists = self._check_existing_link(
                        link, tuple(value for value in allowed if value))
                    if old['state'] == 'ready' and not exists:
                        raise ValueError('recorded compatibility link is missing')
                    if (old['state'] == 'pending' and not exists
                            and old['old_target'] is not None):
                        raise ValueError('replacement compatibility link vanished')
                    if (old['state'] == 'ready'
                            and old['target'] == proof.plex_target):
                        return 'unchanged'
                    if (old['state'] == 'pending'
                            and old['target'] != proof.plex_target):
                        raise ValueError('pending compatibility target changed')
                self._prepare_directory(link, self.proof_show_id(proof))
                if old is None:
                    db.execute('''INSERT INTO links VALUES
                        (?,?,?,?,?,?,?,?,?,?,?,NULL)''',
                        (slot, proof.shoko_file_id, proof.shoko_series_id,
                         proof.anidb_episode_id, proof.tmdb_episode_id,
                         proof.tvdb_episode_id, proof.tmdb_show_id, suffix,
                         str(link.relative_to(self.root)), proof.plex_target,
                         'pending'))
                elif old['state'] == 'ready':
                    db.execute('''UPDATE links SET target=?, state='pending',
                        old_target=? WHERE slot=?''',
                        (proof.plex_target, old['target'], slot))
                db.commit()  # Recovery intent precedes the filesystem change.
                if not (link.is_symlink()
                        and os.readlink(link) == proof.plex_target):
                    temporary = (link.parent /
                                 f'.nagisa-{proof.shoko_file_id}-{os.getpid()}.tmp')
                    if os.path.lexists(temporary):
                        raise ValueError('compatibility staging path occupied')
                    try:
                        os.symlink(proof.plex_target, temporary)
                        os.replace(temporary, link)
                    finally:
                        if os.path.lexists(temporary):
                            os.unlink(temporary)
                db.execute('''UPDATE links SET state='ready', old_target=NULL
                    WHERE slot=?''', (slot,))
                db.commit()
                return 'replaced' if old else 'created'

    def remove(self, file_id: int, *, confirmed: bool) -> bool:
        if type(file_id) is not int or file_id <= 0:
            raise ValueError('exact file ID required')
        if not confirmed:
            return False
        with self._locked():
            with self._connect() as db:
                stored = db.execute('SELECT * FROM links WHERE file_id=?',
                                    (file_id,)).fetchone()
                if stored is None:
                    return False
                row = self._row(stored)
                if row['state'] != 'ready':
                    raise ValueError('pending compatibility link removal held')
                link = self._link_for(row['slot'], row['suffix'])
                if self._check_existing_link(link, (row['target'],)):
                    link.unlink()
                # A crash after unlink but before DELETE is safe to retry.
                db.execute('DELETE FROM links WHERE file_id=?', (file_id,))
                db.commit()
                return True
