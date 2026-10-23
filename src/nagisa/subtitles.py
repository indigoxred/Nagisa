"""Private Nagisa duty delivering only exact manifested original release sidecars.

No worker starts on construction. The caller owns scheduling; this service owns
its dedicated SQLite connection. refresh(event, retained_video_path) must return
exactly True only after the caller verifies indexed source/subtitle visibility;
False or None leaves a durable waiting-refresh retry. Source mounts are never written. Public status
contains counts and exception classes only, never native paths or payloads.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
import threading
import time
from urllib.parse import urlencode

from nagisa.reconciler.models import ImportHistoryEvent
from nagisa.worklist import import_item

_SUBS = {'.srt', '.ass', '.ssa', '.idx', '.sub'}
_VIDEOS = {'.mkv', '.mp4', '.avi', '.m4v', '.ts', '.webm', '.mov', '.mpeg', '.mpg'}
_STATUSES = {'pending', 'waiting-download', 'blocked-source', 'blocked-owner',
             'held-unlinked-source', 'skipped-outside-root',
             'ambiguous', 'conflict', 'blocked-limit', 'waiting-refresh', 'delivered'}


class _Blocked(Exception):
    def __init__(self, status):
        self.status = status


def _native(raw):
    if not isinstance(raw, str) or not raw.startswith('/') or '\\' in raw or '\x00' in raw:
        raise ValueError('unsafe native path')
    p = PurePosixPath(raw)
    if '..' in p.parts or str(p) != raw.rstrip('/'):
        raise ValueError('noncanonical native path')
    return p


def _root(raw):
    p = Path(raw)
    if not p.is_absolute() or '..' in p.parts or p == Path(p.anchor):
        raise ValueError('invalid local root')
    _safe(p, p)
    if not p.is_dir():
        raise ValueError('local root must exist')
    return p


def _safe(path, root):
    """Reject every symlink/junction component and noncanonical path."""
    path.relative_to(root)
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('linked path is forbidden')
    if path.resolve(strict=True) != path:
        raise ValueError('noncanonical local path')
    return path


def _bounded_read(path, size):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        return stream.read(size + 1)


def _stamp(path):
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


class OriginalSubtitleDelivery:
    def __init__(self, sonarr, qbit, refresh, *, source_mounts, destination_root,
                 destination_mount, connection, initial_watermark, poll_seconds=30,
                 max_file_bytes=16777216, max_total_bytes=67108864):
        if type(poll_seconds) is not int or not 15 <= poll_seconds <= 300:
            raise ValueError('subtitle poll interval must be 15 to 300 seconds')
        if any(type(x) is not int or x <= 0 for x in (max_file_bytes, max_total_bytes)):
            raise ValueError('subtitle byte limits must be positive integers')
        if type(initial_watermark) is not int or initial_watermark < 0:
            raise ValueError('subtitle watermark must be nonnegative')
        if not isinstance(source_mounts, dict) or not source_mounts or not callable(refresh):
            raise ValueError('source mappings and refresh callback required')
        self.sources = {_native(k): _root(v) for k, v in source_mounts.items()}
        if PurePosixPath('/') in self.sources:
            raise ValueError('source prefix cannot be filesystem root')
        self.destination_root = _native(destination_root)
        if str(self.destination_root) == '/':
            raise ValueError('destination prefix cannot be filesystem root')
        self.destination_mount = _root(destination_mount)
        for root in self.sources.values():
            if root.is_relative_to(self.destination_mount) or self.destination_mount.is_relative_to(root):
                raise ValueError('source and destination roots must be separate')
        self.sonarr, self.qbit, self.refresh, self.db = sonarr, qbit, refresh, connection
        self.poll_seconds, self.max_file_bytes, self.max_total_bytes = poll_seconds, max_file_bytes, max_total_bytes
        self._cycle, self._lock = threading.Lock(), threading.Lock()
        self._closed, self._last_error, self._last_cycle, self._counts = False, None, None, {}
        fingerprint = json.dumps({'sources': sorted((str(k), str(v)) for k, v in self.sources.items()),
                                  'destination_root': str(self.destination_root),
                                  'destination_mount': str(self.destination_mount)}, sort_keys=True)
        # Dedicated table names ensure existing arrival/ordering cursors are untouched.
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS subtitle_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            old = self.db.execute("SELECT value FROM subtitle_meta WHERE key='roots'").fetchone()
            if old and old[0] != fingerprint:
                raise ValueError('subtitle roots differ from saved journal; reviewed migration required')
            self.db.execute("INSERT OR IGNORE INTO subtitle_meta VALUES('roots',?)", (fingerprint,))
            self.db.execute("INSERT OR IGNORE INTO subtitle_meta VALUES('watermark',?)", (str(initial_watermark),))
            self.db.execute("INSERT OR IGNORE INTO subtitle_meta VALUES('retry_cursor','0')")
            self.db.execute('CREATE TABLE IF NOT EXISTS subtitle_events(history_id INTEGER PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL,refreshed TEXT NOT NULL DEFAULT \'\')')
            self.db.execute('CREATE TABLE IF NOT EXISTS subtitle_destination(history_id INTEGER PRIMARY KEY,identity TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS subtitle_video(history_id INTEGER PRIMARY KEY,identity TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS subtitle_files(history_id INTEGER NOT NULL,source TEXT NOT NULL,destination TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(history_id,source))')
        self._counts_update()

    def _map(self, raw):
        native = _native(raw)
        matches = [(prefix, root) for prefix, root in self.sources.items() if native.is_relative_to(prefix)]
        if not matches:
            raise ValueError('unmapped source')
        # Overlapping prefixes may only describe the same exact local identity.
        paths = {root.joinpath(*native.relative_to(prefix).parts) for prefix, root in matches}
        if len(paths) != 1:
            raise ValueError('ambiguous source mappings')
        path = paths.pop()
        return _safe(path, matches[0][1])

    def _owner(self, event):
        rows = self.sonarr.episode_files(event.series_id)
        files = [r for r in rows if r.get('id') == event.sonarr_file_id]
        episode = self.sonarr.episode(event.episode_id)
        if (len(files) != 1 or files[0].get('seriesId') != event.series_id
                or files[0].get('path') != event.imported_path
                or files[0].get('size') != event.source_size
                or episode.get('seriesId') != event.series_id
                or episode.get('episodeFileId') != event.sonarr_file_id):
            raise _Blocked('blocked-owner')
        native = _native(event.imported_path)
        relative = native.relative_to(self.destination_root)
        if not relative.parts or native.suffix.lower() not in _VIDEOS:
            raise _Blocked('blocked-owner')
        dest = _safe(self.destination_mount.joinpath(*relative.parts), self.destination_mount)
        if not dest.is_file() or dest.stat().st_size != event.source_size:
            raise _Blocked('blocked-owner')
        self._retained_destination(event, dest)
        return dest

    def _retained_destination(self, event, dest):
        _safe(dest, self.destination_mount)
        identity = json.dumps([str(dest), _stamp(dest)])
        saved = self.db.execute('SELECT identity FROM subtitle_destination WHERE history_id=?', (event.history_id,)).fetchone()
        if saved and saved[0] != identity:
            raise _Blocked('blocked-owner')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO subtitle_destination VALUES(?,?)', (event.history_id,identity))

    def _manifest(self, event):
        if event.download_id is None:
            raise _Blocked('held-unlinked-source')
        query = urlencode({'hashes': event.download_id})
        torrents = self.qbit.normalize_torrents(self.qbit.transport.request_json('GET', '/api/v2/torrents/info?' + query))
        matches = [r for r in torrents if r['hash'] == event.download_id]
        if len(matches) != 1 or matches[0]['state'] in {'missingFiles', 'error'}:
            raise _Blocked('blocked-source')
        torrent = matches[0]
        save = _native(torrent['save_path'])
        rows = self.qbit.normalize_files(self.qbit.transport.request_json('GET', '/api/v2/torrents/files?' + urlencode({'hash':event.download_id})))
        if not rows or len(rows) > 10000 or len({r['name'].casefold() for r in rows}) != len(rows):
            raise _Blocked('ambiguous')
        paths = {}
        for row in rows:
            name = row['name']
            rel = PurePosixPath(name)
            if '\\' in name or str(rel) != name or rel.is_absolute() or '..' in rel.parts or ':' in name:
                raise _Blocked('blocked-source')
            # Incomplete files need not exist yet; resolve only when reading.
            paths[name] = str(save / rel)
        dropped = self._map(event.dropped_path)
        videos = []
        for row in rows:
            if PurePosixPath(row['name']).suffix.lower() in _VIDEOS:
                try:
                    if self._map(paths[row['name']]) == dropped:
                        videos.append(row)
                except (ValueError, OSError):
                    continue
        if len(videos) != 1:
            raise _Blocked('ambiguous')
        video = videos[0]
        if video['size'] != event.source_size or dropped.stat().st_size != event.source_size:
            raise _Blocked('blocked-source')
        if video['progress'] != 1:
            raise _Blocked('waiting-download')
        identity = json.dumps([video['name'], str(dropped), _stamp(dropped)])
        saved = self.db.execute('SELECT identity FROM subtitle_video WHERE history_id=?', (event.history_id,)).fetchone()
        if saved and saved[0] != identity:
            raise _Blocked('blocked-source')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO subtitle_video VALUES(?,?)', (event.history_id,identity))
        return rows, paths, video, dropped

    def _prepare(self, event):
        dest = self._owner(event)
        rows, paths, video, dropped = self._manifest(event)
        vp = PurePosixPath(video['name'])
        videos = [PurePosixPath(r['name']) for r in rows if PurePosixPath(r['name']).suffix.lower() in _VIDEOS]
        candidates = []
        unrelated = False
        for row in rows:
            sp = PurePosixPath(row['name'])
            if sp.suffix.lower() not in _SUBS:
                continue
            owners = []
            for v in videos:
                flat = sp.parent == v.parent and (sp.stem == v.stem or sp.stem.startswith(v.stem + '.'))
                nested = sp.is_relative_to(v.parent / v.stem)
                if flat or nested:
                    owners.append(v)
            if len(owners) != 1:
                unrelated = True
                continue
            if owners[0] != vp:
                continue
            if sp.parent == vp.parent:
                suffix = sp.name[len(vp.stem):]
            else:
                rel = sp.relative_to(vp.parent / vp.stem)
                stem = rel.stem
                if stem == vp.stem:
                    stem = ''
                elif stem.startswith(vp.stem + '.'):
                    stem = stem[len(vp.stem)+1:]
                labels = list(rel.parts[:-1]) + ([stem] if stem else [])
                suffix = ('.' + '.'.join(labels) if labels else '') + sp.suffix
            out = dest.with_name(dest.stem + suffix)
            candidates.append((row, paths[row['name']], out))
        if unrelated:
            raise _Blocked('ambiguous')
        if not candidates:
            raise _Blocked('pending')
        if len({str(x[2]).casefold() for x in candidates}) != len(candidates):
            raise _Blocked('ambiguous')
        names = {r['name']: r for r, _, _ in candidates}
        for r, _, _ in candidates:
            p = PurePosixPath(r['name'])
            if p.suffix.lower() in {'.idx', '.sub'}:
                other = str(p.with_suffix('.sub' if p.suffix.lower() == '.idx' else '.idx'))
                if other not in names:
                    raise _Blocked('ambiguous')
        if any(r['progress'] != 1 for r, _, _ in candidates):
            raise _Blocked('waiting-download')
        if (any(r['size'] <= 0 or r['size'] > self.max_file_bytes for r, _, _ in candidates)
                or sum(r['size'] for r, _, _ in candidates) > self.max_total_bytes):
            raise _Blocked('blocked-limit')
        prepared = []
        for row, native, out in candidates:
            source = self._map(native)
            stamp = _stamp(source)
            if not source.is_file() or stamp[2] != row['size']:
                raise _Blocked('blocked-source')
            data = _bounded_read(source, row['size'])
            if len(data) != row['size'] or _stamp(source) != stamp:
                raise _Blocked('blocked-source')
            digest = hashlib.sha256(data).hexdigest()
            saved = self.db.execute('SELECT destination,digest FROM subtitle_files WHERE history_id=? AND source=?', (event.history_id,row['name'])).fetchone()
            if saved and saved != (str(out), digest):
                raise _Blocked('blocked-source')
            if os.path.lexists(out):
                _safe(out, self.destination_mount)
                if not out.is_file() or out.stat().st_size != len(data) or _bounded_read(out, len(data)) != data:
                    raise _Blocked('conflict')
            prepared.append((row['name'],source,stamp,out,data,digest))
        return dest, dropped, _stamp(dropped), prepared

    def _deliver(self, event):
        dest, dropped, video_stamp, prepared = self._prepare(event)
        # Re-read retained native ownership, immutable history identity and qBit
        # manifest immediately before each atomic install. No stale poll permits writes.
        for name, source, stamp, out, data, digest in prepared:
            if self._owner(event) != dest:
                raise _Blocked('blocked-owner')
            current = [e for e in self.sonarr.import_history_after(event.history_id - 1) if e.history_id == event.history_id]
            if current != [event]:
                raise _Blocked('blocked-source')
            rows, paths, video, current_dropped = self._manifest(event)
            matches = [r for r in rows if r['name'] == name]
            if (len(matches) != 1 or matches[0]['progress'] != 1 or matches[0]['size'] != len(data)
                    or self._map(paths[name]) != source or current_dropped != dropped
                    or _stamp(dropped) != video_stamp or _stamp(source) != stamp):
                raise _Blocked('blocked-source')
            _safe(source, next(root for root in self.sources.values() if source.is_relative_to(root)))
            _safe(out.parent, self.destination_mount)
            fd, temporary = tempfile.mkstemp(prefix='.nagisa-subtitle-', dir=out.parent)
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(data); f.flush(); os.fsync(f.fileno())
                _safe(out.parent, self.destination_mount)
                _safe(source, next(root for root in self.sources.values() if source.is_relative_to(root)))
                # Re-run the full selection against the latest manifest, not only
                # this file row: a newly listed video can make its origin ambiguous.
                latest = self._prepare(event)
                if latest != (dest, dropped, video_stamp, prepared):
                    raise _Blocked('blocked-source')
                # Network reads above cannot allow a same-size local replacement
                # of the retained video between ownership checks and installation.
                self._retained_destination(event, dest)
                if not os.path.lexists(out):
                    # Persist scan debt before install, including crash recovery.
                    with self.db:
                        self.db.execute("UPDATE subtitle_events SET refreshed='' WHERE history_id=?", (event.history_id,))
                try:
                    os.link(temporary, out)  # atomic create-if-absent; never replace
                except FileExistsError:
                    _safe(out, self.destination_mount)
                    if not out.is_file() or out.stat().st_size != len(data) or _bounded_read(out, len(data)) != data:
                        raise _Blocked('conflict')
            finally:
                os.unlink(temporary)
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO subtitle_files VALUES(?,?,?,?)', (event.history_id,name,str(out),digest))
        fingerprint = hashlib.sha256(json.dumps([(p[0],p[5]) for p in prepared], sort_keys=True).encode()).hexdigest()
        refreshed = self.db.execute('SELECT refreshed FROM subtitle_events WHERE history_id=?', (event.history_id,)).fetchone()[0]
        if refreshed != fingerprint:
            self._set_status(event.history_id, 'waiting-refresh')
            try:
                # Only an explicit True qualifies exact indexed source/sidecar
                # visibility. False, None or a scan-only response retains debt.
                if self.refresh(event, dest) is not True:
                    raise _Blocked('waiting-refresh')
            except Exception:
                raise _Blocked('waiting-refresh') from None
            with self.db:
                self.db.execute('UPDATE subtitle_events SET refreshed=? WHERE history_id=?', (fingerprint,event.history_id))
        self._set_status(event.history_id, 'delivered')

    def _set_status(self, ident, status):
        with self.db:
            self.db.execute('UPDATE subtitle_events SET status=? WHERE history_id=?', (status,ident))

    def _counts_update(self):
        counts = {s if s in _STATUSES else 'unknown':n for s,n in self.db.execute('SELECT status,COUNT(*) FROM subtitle_events GROUP BY status')}
        items=[import_item(identity,payload,state,_STATUSES) for identity,payload,state in self.db.execute("SELECT history_id,payload,status FROM subtitle_events WHERE status!='delivered' ORDER BY history_id LIMIT 100")]
        with self._lock:
            self._counts = counts
            self._work_items = items

    def run_once(self):
        if not self._cycle.acquire(blocking=False):
            return False
        try:
            if self._closed:
                return False
            try:
                if self.sonarr.compatibility()[0] is not True:
                    raise RuntimeError('subtitle Sonarr contract unsupported')
                watermark = int(self.db.execute("SELECT value FROM subtitle_meta WHERE key='watermark'").fetchone()[0])
                events = self.sonarr.import_history_after(watermark)
                with self.db:
                    for event in events:
                        accepted=self.sonarr.accepts_import(event)
                        payload = json.dumps(asdict(event), sort_keys=True)
                        prior=self.db.execute('SELECT payload,status FROM subtitle_events WHERE history_id=?',(event.history_id,)).fetchone()
                        if prior and prior[0]!=payload:
                            raise ValueError('replayed import identity changed')
                        if prior and (prior[1]=='skipped-outside-root')!=(not accepted):
                            raise ValueError('replayed import disposition changed')
                        if not prior:
                            status=('held-unlinked-source' if event.download_id is None else 'pending') if accepted else 'skipped-outside-root'
                            self.db.execute('INSERT INTO subtitle_events(history_id,payload,status) VALUES(?,?,?)', (event.history_id,payload,status))
                    if events:
                        self.db.execute("UPDATE subtitle_meta SET value=? WHERE key='watermark'", (str(max(watermark,max(e.history_id for e in events))),))
                retry_cursor = int(self.db.execute("SELECT value FROM subtitle_meta WHERE key='retry_cursor'").fetchone()[0])
                # Completed events are terminal: torrent retention is no longer
                # required after verified delivery. Rotate unfinished work fairly
                # and persist progress, so a large blocked cohort cannot starve
                # later original subtitle completions after restart.
                pending = self.db.execute("SELECT history_id,payload FROM subtitle_events WHERE status NOT IN ('delivered','held-unlinked-source','skipped-outside-root') ORDER BY CASE WHEN history_id>? THEN 0 ELSE 1 END,history_id LIMIT 100", (retry_cursor,)).fetchall()
                for ident, payload in pending:
                    try:
                        self._deliver(ImportHistoryEvent(**json.loads(payload)))
                    except _Blocked as error:
                        self._set_status(ident,error.status)
                    except (ValueError, OSError, KeyError, TypeError):
                        self._set_status(ident,'blocked-source')
                    finally:
                        with self.db:
                            self.db.execute("UPDATE subtitle_meta SET value=? WHERE key='retry_cursor'", (str(ident),))
                self._counts_update()
                with self._lock:
                    self._last_error, self._last_cycle = None, time.time()
                return True
            except Exception as error:
                with self._lock:
                    self._last_error, self._last_cycle = type(error).__name__, time.time()
                raise
        finally:
            self._cycle.release()

    def status_snapshot(self):
        with self._lock:
            return {'state':'stopped' if self._closed else 'enabled','poll_seconds':self.poll_seconds,
                    'last_cycle':self._last_cycle,'last_error':self._last_error,'counts':dict(self._counts),
                    'work_items':[dict(item) for item in self._work_items]}

    def close(self):
        with self._cycle:
            if self._closed:
                return
            self.db.close()
            with self._lock:
                self._closed = True
