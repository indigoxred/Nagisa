"""Isolated candidate: observe preservation around a stock Relay rebuild.

No numbering, relinking, media copying, or deletion is performed here. Not
wired into the production runtime. A post-build guard detects losses; it does
not turn Relay's destructive cleanup into a non-destructive API.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading


class PreservationError(RuntimeError):
    pass


class UnqualifiedOwner(PreservationError):
    """Owner preflight refused before any durable dispatch reservation."""


class NotDispatched(PreservationError):
    """The stock rebuild request was never issued; a later attempt is safe."""


def require_success(response):
    data = response.get('Data') if isinstance(response,dict) else None
    if not isinstance(response,dict) or response.get('Status') != 'ok' or not isinstance(data,dict) or data.get('Errors') != []:
        raise PreservationError('direct stock rebuild success not proven')


class InventoryGuard:
    ASSET_EXTENSIONS = frozenset({'.ass','.ssa','.srt','.vtt','.idx','.sub','.sup',
                                 '.jpg','.jpeg','.png','.webp','.json','.xml',
                                 '.chp','.ttf','.otf','.woff','.txt'})

    def __init__(self, roots, source_roots, *, maximum_entries=50000,
                 maximum_asset_bytes=32*1024*1024, optional_roots=()):
        self.roots = tuple(Path(p).absolute() for p in roots)
        self.source_roots = tuple(Path(p).resolve(strict=True) for p in source_roots)
        self.maximum_entries = maximum_entries
        self.maximum_asset_bytes = maximum_asset_bytes
        self.optional_roots=frozenset(map(Path,optional_roots))
        if not self.optional_roots<=set(self.roots):raise ValueError('optional roots must be protected roots')
        if not self.roots or not self.source_roots or len(set(self.roots)) != len(self.roots):
            raise ValueError('explicit distinct presentation/source roots required')
        for root in self.roots:
            if root == Path(root.anchor) or root.is_symlink():
                raise ValueError('unsafe presentation root')
            if any(root != other and root.is_relative_to(other) for other in self.roots):
                raise ValueError('overlapping presentation roots')

    def capture(self):
        rows = {}
        try:
            for root in self.roots:
                if root in self.optional_roots and not os.path.lexists(root):
                    if root.parent.resolve(strict=True)!=root.parent or not root.parent.is_dir():
                        raise PreservationError('optional presentation parent unavailable or redirected')
                    continue
                if not root.is_dir() or root.is_symlink():
                    raise PreservationError('configured presentation root unavailable')
                def failed(exc):
                    raise PreservationError('presentation inventory unreadable') from exc
                for folder, directories, files in os.walk(root, followlinks=False, onerror=failed):
                    # A directory link must not hide descendants from the guard.
                    if any((Path(folder)/name).is_symlink() for name in directories):
                        raise PreservationError('directory link requires explicit qualification')
                    for name in files:
                        path = Path(folder)/name
                        key = str(path)
                        if len(rows) >= self.maximum_entries:
                            raise PreservationError('inventory entry cap exceeded')
                        if path.is_symlink():
                            target = path.resolve(strict=True)
                            if not any(target.is_relative_to(base) for base in self.source_roots):
                                raise PreservationError('presentation link escapes approved sources')
                            info = target.stat()
                            if not target.is_file():
                                raise PreservationError('presentation target is not a file')
                            rows[key] = ['link',str(target),info.st_dev,info.st_ino,
                                         info.st_size,info.st_mtime_ns]
                            if path.suffix.lower() in self.ASSET_EXTENSIONS:
                                if info.st_size > self.maximum_asset_bytes:
                                    raise PreservationError('linked asset exceeds qualification cap')
                                rows[key].append(hashlib.sha256(target.read_bytes()).hexdigest())
                                final=target.stat()
                                if (info.st_size,info.st_mtime_ns,info.st_ino) != (final.st_size,final.st_mtime_ns,final.st_ino):
                                    raise PreservationError('linked asset changed during inventory')
                        else:
                            info = path.stat()
                            stock_marker = path.name=='.ignore' and info.st_size==0
                            if not path.is_file() or (path.suffix.lower() not in self.ASSET_EXTENSIONS and not stock_marker) or info.st_size > self.maximum_asset_bytes:
                                raise PreservationError('unqualified regular file in generated root')
                            digest = hashlib.sha256(path.read_bytes()).hexdigest()
                            final = path.stat()
                            if (info.st_size,info.st_mtime_ns) != (final.st_size,final.st_mtime_ns):
                                raise PreservationError('asset changed during inventory')
                            rows[key] = ['asset',digest,info.st_size]
            return rows
        except (OSError, RuntimeError) as exc:
            if isinstance(exc, PreservationError):
                raise
            raise PreservationError('presentation inventory could not be proven') from exc

    def verify(self, before):
        after = self.capture()
        if any(after.get(path) != evidence for path,evidence in before.items()):
            raise PreservationError('existing presentation lost or changed')
        return after


class SourceCoverageGuard(InventoryGuard):
    """Verify video coverage within each root, independently of Plex state.

    This post-build observer does not calculate destinations, mutate output or
    guarantee rollback. Assets/sidecars/markers keep exact path protection until
    their consumer associations are qualified. InventoryGuard remains default.
    """
    VIDEO_EXTENSIONS=frozenset({'.mkv','.mp4','.m4v','.avi','.mov','.webm',
                                '.ts','.m2ts','.mpg','.mpeg','.wmv','.flv','.ogm'})

    def __init__(self,roots,source_roots,*,strict_roots=(),**kwargs):
        roots=tuple(Path(p) for p in roots)
        if any(not p.is_absolute() or '..' in p.parts for p in roots):
            raise ValueError('canonical absolute presentation roots required')
        super().__init__(roots,source_roots,**kwargs)
        self.strict_roots=tuple(Path(root) for root in strict_roots)
        if len(set(self.strict_roots))!=len(self.strict_roots) or not set(self.strict_roots)<=set(self.roots):
            raise ValueError('strict output roots must be a distinct exact subset of protected roots')
        if any(not base.is_dir() for base in self.source_roots):
            raise ValueError('source roots must be directories')
        if any(type(cap) is not int or cap<=0 for cap in (self.maximum_entries,self.maximum_asset_bytes)):
            raise ValueError('positive inventory qualification caps required')

    def capture(self):
        try:
            if any(root.resolve(strict=True)!=root for root in self.roots if os.path.lexists(root) or root not in self.optional_roots):
                raise PreservationError('presentation root has a redirected parent')
            inventory=super().capture()
            coverage={}
            for path,evidence in inventory.items():
                extension=Path(path).suffix.lower()
                if evidence[0]=='link':
                    target=Path(evidence[1])
                    if any(target.is_relative_to(root) for root in self.roots):
                        raise PreservationError('output cannot stand in for an original source')
                    if extension in self.ASSET_EXTENSIONS:
                        if target.suffix.lower() not in self.ASSET_EXTENSIONS:
                            raise PreservationError('unqualified linked asset source type')
                        coverage[path]=['sidecar-source',extension,*evidence[1:]]
                    elif extension in self.VIDEO_EXTENSIONS:
                        if target.suffix.lower() not in self.VIDEO_EXTENSIONS:
                            raise PreservationError('unqualified linked video source type')
                        root=next(root for root in self.roots if Path(path).is_relative_to(root))
                        coverage[path]=['video-source',str(root),*evidence[1:]]
                    else:
                        raise PreservationError('unqualified linked content type')
                elif Path(path).name=='.ignore':
                    coverage[path]=['stock-marker',*evidence[1:]]
                else:
                    coverage[path]=['asset-bytes',extension,*evidence[1:]]
            return coverage
        except (OSError,RuntimeError) as error:
            if isinstance(error,PreservationError):raise
            raise PreservationError('source coverage could not be proven') from error

    def verify(self,before):
        after=self.capture()
        try:
            available={tuple(evidence) for evidence in after.values() if evidence[0]=='video-source'}
            if not isinstance(before,dict):
                raise PreservationError('invalid source coverage snapshot')
            for path,evidence in before.items():
                if not isinstance(evidence,list) or not evidence:
                    raise PreservationError('invalid source coverage snapshot')
                preserved=tuple(evidence) in available if evidence[0]=='video-source' else after.get(path)==evidence
                # Hidden MovieVFS consumers own literal file paths. Preserve
                # those paths and their target stamps, without changing the
                # independently qualified TV source-coverage rename behavior.
                if any(Path(path).is_relative_to(root) for root in self.strict_roots):
                    preserved=preserved and after.get(path)==evidence
                if not preserved:
                    raise PreservationError('protected source or asset content lost or changed')
        except TypeError as error:
            raise PreservationError('invalid source coverage snapshot') from error
        return after


class ArrivalAttempt:
    """Persistent single-flight reservation; an uncertain dispatch never retries.

    The caller must poll visibility, not repeatedly call begin(). Store this table
    in Nagisa's existing state connection when eventually wired into its runtime.
    """
    def __init__(self, guard, connection, *, native_guard=None):
        self.guard = guard
        self.native_guard = native_guard
        if not isinstance(connection,sqlite3.Connection) or not any(row[1]=='main' and row[2] for row in connection.execute('PRAGMA database_list')):
            raise ValueError('explicit durable journal connection required')
        self.connection = connection
        self._lock = threading.Lock()
        self.connection.execute('''CREATE TABLE IF NOT EXISTS arrival_attempts(
          history_id INTEGER PRIMARY KEY, series_id INTEGER NOT NULL,
          phase TEXT NOT NULL, inventory_json TEXT NOT NULL)''')
        self.connection.execute('CREATE TABLE IF NOT EXISTS arrival_native_snapshots(history_id INTEGER PRIMARY KEY,snapshot_json TEXT NOT NULL)')
        self.history_id = None
        self.before = None

    def begin(self, history_id, series_id, dispatch, *, preflight=None):
        if type(history_id) is not int or type(series_id) is not int or min(history_id,series_id)<=0:
            raise ValueError('positive event and stock Shoko series identities required')
        with self._lock:
            before = self.guard.capture()
            # The persisted native baseline must be the evidence qualified by
            # this preflight, never a later unchecked native API capture.
            native_snapshot = None
            if preflight is None and self.native_guard is not None:
                raise UnqualifiedOwner('native evidence preflight required')
            if preflight is not None:
                try:
                    if not callable(preflight):raise UnqualifiedOwner('owner snapshot not qualified')
                    qualified=preflight(before)
                    if self.native_guard is None:
                        if qualified is not True:raise UnqualifiedOwner('owner snapshot not qualified')
                    else:
                        self.native_guard.validate(qualified,series_id)
                        native_snapshot=qualified
                except (ValueError,OSError,RuntimeError,TypeError,AttributeError) as error:
                    if isinstance(error,UnqualifiedOwner):raise
                    raise UnqualifiedOwner('owner snapshot preflight failed') from error
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                if self.connection.execute("SELECT 1 FROM arrival_attempts WHERE (history_id=? AND phase!='not-dispatched') OR phase NOT IN ('complete','not-dispatched') LIMIT 1",(history_id,)).fetchone():
                    raise PreservationError('existing attempt needs observation or review; do not redispatch')
                self.connection.execute('INSERT INTO arrival_attempts VALUES(?,?,?,?) ON CONFLICT(history_id) DO UPDATE SET series_id=excluded.series_id, phase=excluded.phase, inventory_json=excluded.inventory_json',
                    (history_id,series_id,'dispatching',json.dumps(before,separators=(',',':'))))
                if self.native_guard is not None:
                    self.connection.execute('INSERT INTO arrival_native_snapshots VALUES(?,?) ON CONFLICT(history_id) DO UPDATE SET snapshot_json=excluded.snapshot_json',
                        (history_id,json.dumps(native_snapshot,separators=(',',':'),allow_nan=False)))
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            self.history_id,self.before = history_id,before
            # Reservation remains durable even when the HTTP result is unknown.
            try:
                response = dispatch()
                require_success(response)
            except NotDispatched:
                self.connection.execute("UPDATE arrival_attempts SET phase='not-dispatched' WHERE history_id=?",(history_id,))
                self.connection.commit()
                raise
            except Exception:
                self.connection.execute("UPDATE arrival_attempts SET phase='ambiguous' WHERE history_id=?",(history_id,))
                self.connection.commit()
                raise
            self.connection.execute("UPDATE arrival_attempts SET phase='awaiting-verification' WHERE history_id=?",(history_id,))
            self.connection.commit()
            return response

    def observe(self, history_id):
        """Resume verification only; never infer success from Relay's shared task log."""
        row = self.connection.execute('SELECT inventory_json FROM arrival_attempts WHERE history_id=?',(history_id,)).fetchone()
        if row is None:
            raise PreservationError('attempt not found')
        self.history_id,self.before = history_id,json.loads(row[0])

    def finish(self, stock_status, *, visible):
        if self.history_id is None or self.before is None:
            raise PreservationError('no observed attempt')
        if stock_status not in {'running','completed','failed'}:
            raise PreservationError('unknown stock outcome')
        if stock_status == 'failed':
            raise PreservationError('stock build failed')
        if stock_status == 'running':
            return False
        row = self.connection.execute('SELECT phase FROM arrival_attempts WHERE history_id=?',(self.history_id,)).fetchone()
        if row is None or row[0] not in {'awaiting-verification','complete'}:
            raise PreservationError('direct successful response missing; review uncertain attempt')
        self.guard.verify(self.before)
        if self.native_guard is not None:
            try:
                native_row=self.connection.execute('SELECT snapshot_json FROM arrival_native_snapshots WHERE history_id=?',(self.history_id,)).fetchone()
                if native_row is None:raise PreservationError('saved native baseline missing; never recapture after dispatch')
                snapshot=json.loads(native_row[0])
                owner=self.connection.execute('SELECT series_id FROM arrival_attempts WHERE history_id=?',(self.history_id,)).fetchone()[0]
                if not isinstance(snapshot,dict) or type(snapshot.get('owner')) is not int or snapshot['owner']!=owner:raise PreservationError('saved native owner differs from issued attempt')
                self.native_guard.verify(snapshot)
            except (KeyError,TypeError,ValueError,OSError,AttributeError,RuntimeError) as error:
                if isinstance(error,PreservationError):raise
                raise PreservationError('saved native preservation evidence unavailable') from error
        if not visible:
            return False
        self.connection.execute("UPDATE arrival_attempts SET phase='complete' WHERE history_id=?",(self.history_id,))
        self.connection.commit()
        return True


class RelayArrivalClient:
    def __init__(self, transport):
        self.transport = transport

    def rebuild(self, series_id):
        if type(series_id) is not int or series_id <= 0:
            raise ValueError('positive stock Shoko series ID required')
        try:
            active = self.transport.request_json('GET','/api/plugin/ShokoRelay/tasks/active')
        except Exception as exc:
            raise NotDispatched('Relay idle observation failed before rebuild dispatch') from exc
        if not isinstance(active,list) or active:
            raise NotDispatched('stock Relay task idle state unproven; rebuild not issued')
        result = self.transport.request_json('GET',f'/api/plugin/ShokoRelay/vfs?run=true&clean=false&filter={series_id}')
        require_success(result)
        return result
