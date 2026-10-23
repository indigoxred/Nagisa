"""Private delivery settings and assembly, without changing existing root meanings."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate delivery setting')
        result[key] = value
    return result


def _directory(value):
    if not isinstance(value, str):
        raise ValueError('Explicit mounted directory required')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or path == Path(path.anchor) or path.resolve(strict=True) != path or not path.is_dir():
        raise ValueError('Canonical mounted directory required')
    return path


def _native(value):
    if not isinstance(value, str) or '\\' in value or '\x00' in value:
        raise ValueError('Canonical native root required')
    root = PurePosixPath(value)
    if not root.is_absolute() or '..' in root.parts or str(root) != value or value == '/' or value.startswith('//'):
        raise ValueError('Canonical native root required')
    return value


class DeliverySettingsStore:
    CONTROLS = frozenset({'arrival_enabled', 'subtitles_enabled', 'reconciliation_enabled', 'arrival_poll_seconds',
        'subtitle_poll_seconds', 'subtitle_max_file_bytes', 'subtitle_max_total_bytes',
        'catchup_enabled', 'catchup_poll_seconds', 'catchup_include_backlog', 'movie_arrival_enabled', 'movie_arrival_poll_seconds'})
    CATCHUP_DEFAULTS = {'catchup_enabled':False, 'catchup_poll_seconds':300, 'catchup_include_backlog':False}
    MOVIE_DEFAULTS = {'movie_arrival_enabled':False, 'movie_arrival_poll_seconds':15,
        'movie_arrival_import_root':None, 'movie_arrival_source_root':None, 'movie_arrival_initial_watermark':None}
    BINDINGS = frozenset({'managed_roots','plex_mounts','subtitle_source_mounts','subtitle_destination_root','subtitle_destination_mount','subtitle_plex_section_ids',
        'movie_arrival_import_root','movie_arrival_source_root','movie_arrival_initial_watermark'})

    @staticmethod
    def defaults():
        return {'schema': 1, 'arrival_enabled': False, 'subtitles_enabled': False, 'reconciliation_enabled':True,
            'arrival_poll_seconds': 15, 'subtitle_poll_seconds': 30,
            'subtitle_max_file_bytes': 16777216, 'subtitle_max_total_bytes': 67108864,
            'managed_roots': {}, 'plex_mounts': {}, 'subtitle_source_mounts': {},
            'subtitle_destination_root': None, 'subtitle_destination_mount': None,
            'subtitle_initial_watermark': None, 'subtitle_plex_section_ids': [],
            **DeliverySettingsStore.CATCHUP_DEFAULTS, **DeliverySettingsStore.MOVIE_DEFAULTS}

    def __init__(self, path, config_dir, *, arrival_configured=False):
        self.path = Path(path)
        self.base = Path(config_dir).resolve(strict=True)
        self.arrival_configured = arrival_configured
        self._lock = threading.Lock()
        self.active = self._read()[0]

    def _read(self):
        path = self.path
        if not path.is_absolute() or '..' in path.parts or path.is_symlink() or path.resolve(strict=True) != path or not path.is_relative_to(self.base):
            raise ValueError('Delivery settings must be inside CONFIG_DIR')
        info = path.stat()
        if not path.is_file() or info.st_nlink != 1 or info.st_size > 16384:
            raise ValueError('Unsafe delivery settings file')
        raw = path.read_bytes()
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
        # Older reviewed schema1 files retain their exact bytes and revision.
        # Only the new optional recovery controls receive disabled defaults.
        if isinstance(value,dict):
            value={**self.CATCHUP_DEFAULTS,**self.MOVIE_DEFAULTS,**value}
        self._validate(value)
        return value, hashlib.sha256(raw).hexdigest(), (info.st_dev, info.st_ino)

    def _validate(self, value):
        if not isinstance(value, dict) or set(value) != set(self.defaults()) or type(value['schema']) is not int or value['schema'] != 1:
            raise ValueError('Unsupported delivery settings')
        for key in ('arrival_enabled', 'subtitles_enabled', 'reconciliation_enabled', 'catchup_enabled', 'catchup_include_backlog', 'movie_arrival_enabled'):
            if type(value[key]) is not bool:
                raise ValueError('Delivery enable controls must be boolean')
        for key, low, high in (('arrival_poll_seconds',15,300), ('movie_arrival_poll_seconds',15,300), ('subtitle_poll_seconds',15,300), ('catchup_poll_seconds',300,3600),
            ('subtitle_max_file_bytes',1024,16777216), ('subtitle_max_total_bytes',1024,67108864)):
            if type(value[key]) is not int or not low <= value[key] <= high:
                raise ValueError('Delivery limit outside supported range')
        if value['subtitle_max_total_bytes'] < value['subtitle_max_file_bytes']:
            raise ValueError('Total subtitle limit must cover one file')
        for key in ('managed_roots', 'plex_mounts', 'subtitle_source_mounts'):
            mapping = value[key]
            if not isinstance(mapping, dict) or len(mapping) > 32:
                raise ValueError('Bounded explicit root mapping required')
            for native, local in mapping.items():
                if key == 'managed_roots':
                    if not native.isdigit() or int(native) <= 0 or str(int(native)) != native:
                        raise ValueError('Positive managed folder ID required')
                else:
                    _native(native)
                _directory(local)
        if value['subtitle_destination_root'] is not None:
            _native(value['subtitle_destination_root'])
        if value['subtitle_destination_mount'] is not None:
            destination = _directory(value['subtitle_destination_mount'])
            if destination.is_relative_to(self.base) or self.base.is_relative_to(destination):
                raise ValueError('Subtitle destination cannot contain configuration')
        watermark = value['subtitle_initial_watermark']
        if watermark is not None and (type(watermark) is not int or watermark < 0):
            raise ValueError('Explicit nonnegative import frontier required')
        ids = value['subtitle_plex_section_ids']
        if not isinstance(ids, list) or len(ids)>8 or any(type(x) is not int or x<=0 for x in ids) or len(set(ids))!=len(ids):
            raise ValueError('Distinct positive Plex sections required')
        if value['arrival_enabled'] and not self.arrival_configured:
            raise ValueError('Arrival requires its reviewed root configuration')
        if value['movie_arrival_import_root'] is not None:_native(value['movie_arrival_import_root'])
        if value['movie_arrival_source_root'] is not None:_directory(value['movie_arrival_source_root'])
        frontier=value['movie_arrival_initial_watermark']
        if frontier is not None and (type(frontier) is not int or frontier<0):raise ValueError('Explicit movie import frontier required')
        if value['movie_arrival_enabled'] and (not self.arrival_configured or frontier is None or value['movie_arrival_import_root'] is None or value['movie_arrival_source_root'] is None):
            raise ValueError('Movie arrivals require reviewed preservation, roots and a starting frontier')
        if value['subtitles_enabled'] and (not value['subtitle_source_mounts'] or not value['plex_mounts'] or not ids or watermark is None or value['subtitle_destination_root'] is None or value['subtitle_destination_mount'] is None):
            raise ValueError('Subtitles require their reviewed roots and import frontier')
        if value['subtitles_enabled'] and not self.arrival_configured:
            raise ValueError('Subtitle Relay refresh requires its reviewed preservation configuration')

    def snapshot(self):
        with self._lock:
            saved, revision, _ = self._read()
            return {'configured': True, 'active': copy.deepcopy(self.active),
                'saved': saved, 'revision': revision, 'restart_required': saved != self.active}

    def save_controls(self, controls, revision, bindings=None):
        if set(controls) != self.CONTROLS:
            raise ValueError('Only the displayed delivery controls can be changed')
        with self._lock:
            current, actual, inode = self._read()
            if revision != actual:
                raise ValueError('Settings changed; reload this page')
            updated = {**current, **controls}
            if bindings is not None:
                if not isinstance(bindings,dict) or set(bindings)!=self.BINDINGS:
                    raise ValueError('Only displayed delivery bindings can be changed')
                for key in ('managed_roots','plex_mounts','subtitle_source_mounts'):
                    if not isinstance(bindings[key],dict) or not set(bindings[key].values()) <= set(self.active[key].values()):
                        raise ValueError('New mounts require deployment review')
                if bindings['subtitle_destination_mount']!=self.active['subtitle_destination_mount'] or not set(bindings['subtitle_plex_section_ids'])<=set(self.active['subtitle_plex_section_ids']):
                    raise ValueError('New destination or Plex library requires deployment review')
                movie_source=bindings['movie_arrival_source_root']
                if movie_source!=self.active['movie_arrival_source_root'] and movie_source is not None:
                    candidate=_directory(movie_source)
                    if not any(candidate.is_relative_to(_directory(root)) for root in self.active['managed_roots'].values()):
                        raise ValueError('New movie source mount requires deployment review')
                movie_journal=self.base/'movie-arrival.sqlite3'
                if (movie_journal.exists() or movie_journal.is_symlink()) and any(bindings[key]!=self.active[key] for key in ('movie_arrival_import_root','movie_arrival_source_root','movie_arrival_initial_watermark')):
                    raise ValueError('Movie source and starting frontier are bound to their existing journal')
                updated.update(bindings)
            self._validate(updated)
            # Never accept externally changed mount/frontier bindings in a running process.
            if any(current[key] != self.active[key] for key in current if key not in self.CONTROLS):
                raise ValueError('Delivery bindings changed; reviewed restart required')
            descriptor, temporary = tempfile.mkstemp(prefix='.delivery-', dir=self.base)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write((json.dumps(updated, sort_keys=True, indent=2)+'\n').encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                if self._read()[1:] != (actual, inode):
                    raise ValueError('Settings changed; reload this page')
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)


class PlexSubtitleVisibility:
    def __init__(self, plex, mounts, sections, max_bytes):
        self.plex, self.mounts, self.sections, self.max_bytes = plex, mounts, sections, max_bytes

    def _local(self, native):
        path=PurePosixPath(native)
        if not path.is_absolute() or '..' in path.parts or '\\' in native or '\x00' in native:
            raise ValueError('Invalid Plex media path')
        matches=[]
        for prefix,root in self.mounts.items():
            if path.is_relative_to(PurePosixPath(prefix)):
                local=root / path.relative_to(PurePosixPath(prefix)).as_posix()
                matches.append(local)
        if len(matches)!=1:
            raise ValueError('Ambiguous Plex media mount')
        return matches[0]

    def __call__(self, verified, destination, expected):
        from nagisa.subtitles import _bounded_read, _stamp
        guid=f'tv.plex.agents.custom.shoko://episode/e{verified.shoko_episode_id}'
        if not expected:
            return False
        for section in self.sections:
            query=urlencode({'type':4,'guid':guid,'includeGuids':1,'X-Plex-Container-Start':0,'X-Plex-Container-Size':100})
            container=self.plex.request_json('GET',f'/library/sections/{section}/all?{query}').get('MediaContainer',{})
            rows=container.get('Metadata',[])
            if not isinstance(rows,list) or container.get('totalSize',container.get('size',0))!=len(rows) or len(rows)>100:
                return False
            found=0
            for row in rows:
                if not isinstance(row,dict) or row.get('guid')!=guid:
                    continue
                key=str(row.get('ratingKey',''))
                if not key.isdigit() or int(key)<=0:
                    return False
                detail=self.plex.request_json('GET',f'/library/metadata/{key}?includeGuids=1').get('MediaContainer',{}).get('Metadata',[])
                if len(detail)!=1 or detail[0].get('guid')!=guid:
                    return False
                for media in detail[0].get('Media',[]):
                    for part in media.get('Part',[]):
                        try:
                            local=self._local(part['file'])
                            if local.resolve(strict=True)!=destination or part.get('size')!=destination.stat().st_size:
                                continue
                            # Native VFS sidecar links must resolve to these exact originals.
                            for path,digest in expected:
                                sidecar=local.with_name(local.stem+path.name[len(destination.stem):])
                                stamp=_stamp(path)
                                if stamp[2]<=0 or stamp[2]>self.max_bytes:
                                    return False
                                data=_bounded_read(path,stamp[2])
                                if sidecar.resolve(strict=True)!=path or _stamp(path)!=stamp or len(data)!=stamp[2] or hashlib.sha256(data).hexdigest()!=digest:
                                    return False
                            digests=set()
                            for stream in part.get('Stream',[]):
                                if stream.get('streamType')!=3 or not stream.get('key'):
                                    continue
                                data=self.plex.request_bytes(stream['key'],maximum_bytes=self.max_bytes)
                                digests.add(hashlib.sha256(data).hexdigest())
                            if not {digest for _,digest in expected} <= digests:
                                return False
                            found+=1
                        except (KeyError,OSError,ValueError):
                            return False
            if found!=1:
                return False
        return True


class _SubtitleAssembly:
    def __init__(self, worker, relay):
        self.worker,self.relay=worker,relay
        self.poll_seconds=worker.poll_seconds
    def run_once(self): return self.worker.run_once()
    def status_snapshot(self): return self.worker.status_snapshot()
    def close(self):
        try: self.worker.close()
        finally: self.relay.close()


def build_subtitle_service(config, settings, *, dispatch_lock=None):
    if settings is None or not settings['subtitles_enabled']:
        return None
    from nagisa.http import JsonTransport, QbitTransport
    from nagisa.reconciler.qbit import QbitObserver
    from nagisa.reconciler.sonarr import SonarrClient
    from nagisa.subtitles import OriginalSubtitleDelivery
    from nagisa.reconciler.arrival_startup import build_arrival_service

    transport = JsonTransport(config.sonarr_url, headers={'X-Api-Key': config.sonarr_token}, timeout=config.request_timeout_seconds)
    sonarr = SonarrClient(transport, allowed_versions=config.sonarr_allowed_versions, import_root=settings['subtitle_destination_root'])
    qbit = QbitObserver(QbitTransport(config.qbit_url, username=config.qbit_username,
        password=config.qbit_password, timeout=config.request_timeout_seconds))
    plex = JsonTransport(config.plex_url, headers={'X-Plex-Token': config.plex_token}, timeout=config.request_timeout_seconds)
    mounts = {native: Path(local) for native, local in settings['plex_mounts'].items()}
    sections = settings['subtitle_plex_section_ids']
    dispatch_lock = dispatch_lock or threading.Lock()
    relay = build_arrival_service(config, sonarr, journal_name='subtitle-relay.sqlite3')
    if relay is None:
        raise ValueError('Reviewed Relay preservation configuration required')
    relay.loop.db.execute('CREATE TABLE IF NOT EXISTS subtitle_relay_delivery(attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,history_id INTEGER NOT NULL,fingerprint TEXT NOT NULL,binding TEXT NOT NULL,UNIQUE(history_id,fingerprint))')
    relay.loop.db.commit()
    visibility = PlexSubtitleVisibility(plex,mounts,sections,settings['subtitle_max_file_bytes'])
    worker = None

    def refresh(event, destination):
        destination = Path(destination).resolve(strict=True)
        with dispatch_lock, relay._cycle, relay.loop.lock:
            if relay.sonarr.compatibility()[0] is not True:
                return False
            verified=relay.loop.identity_reader(event)
            if verified.source!=destination:
                return False
            expected=[(Path(path),digest) for path,digest in worker.db.execute('SELECT destination,digest FROM subtitle_files WHERE history_id=?',(event.history_id,))]
            fingerprint=hashlib.sha256(json.dumps([(str(path),digest) for path,digest in sorted(expected)],sort_keys=True).encode()).hexdigest()
            binding=json.dumps([verified.shoko_file_id,verified.shoko_series_id,verified.shoko_episode_id,str(verified.source),list(verified.source_stat)])
            bindings=relay.loop.db.execute('SELECT binding FROM subtitle_relay_delivery WHERE history_id=?',(event.history_id,)).fetchall()
            if any(row[0]!=binding for row in bindings):
                return False
            relay.loop.db.execute('INSERT OR IGNORE INTO subtitle_relay_delivery(history_id,fingerprint,binding) VALUES(?,?,?)',(event.history_id,fingerprint,binding))
            relay.loop.db.commit()
            attempt_id=relay.loop.db.execute('SELECT attempt_id FROM subtitle_relay_delivery WHERE history_id=? AND fingerprint=?',(event.history_id,fingerprint)).fetchone()[0]
            prior=relay.loop.db.execute('SELECT phase,series_id FROM arrival_attempts WHERE history_id=?',(attempt_id,)).fetchone()
            if prior and (prior[1]!=verified.shoko_series_id or prior[0] in ('ambiguous','dispatching')):
                return False
            if not prior or prior[0]=='not-dispatched':
                def dispatch():
                    current=relay.loop.identity_reader(event)
                    if current!=verified:
                        from nagisa.reconciler.arrival import NotDispatched
                        raise NotDispatched('Subtitle source identity changed')
                    return relay.loop.dispatch(verified.shoko_series_id)
                relay.loop.attempt.begin(attempt_id,verified.shoko_series_id,dispatch,
                    preflight=lambda inventory:relay.loop.owner_preflight(verified,inventory))
            else:
                relay.loop.attempt.observe(attempt_id)
            # Native Relay requests scans of its generated folders. Confirm indexed
            # originals, exact VFS sidecars and Plex's actual subtitle bytes.
            visible=visibility(verified,destination,expected)
            return relay.loop.attempt.finish('completed',visible=visible)

    journal = config.config_dir / 'subtitles.sqlite3'
    if journal.exists() or journal.is_symlink():
        if journal.is_symlink() or journal.resolve(strict=True) != journal or not journal.is_file() or journal.stat().st_nlink != 1:
            raise ValueError('Unsafe existing subtitle journal')
    connection = sqlite3.connect(journal, isolation_level=None, check_same_thread=False)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and 'subtitle_meta' not in tables:
            raise ValueError('Existing database is not subtitle delivery state')
        journal.chmod(0o600)
        worker = OriginalSubtitleDelivery(sonarr, qbit, refresh,
            source_mounts=settings['subtitle_source_mounts'], destination_root=settings['subtitle_destination_root'],
            destination_mount=settings['subtitle_destination_mount'], connection=connection,
            initial_watermark=settings['subtitle_initial_watermark'], poll_seconds=settings['subtitle_poll_seconds'],
            max_file_bytes=settings['subtitle_max_file_bytes'], max_total_bytes=settings['subtitle_max_total_bytes'])
        return _SubtitleAssembly(worker,relay)
    except BaseException:
        connection.close()
        relay.close()
        raise
