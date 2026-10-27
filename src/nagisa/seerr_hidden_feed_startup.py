"""Opt-in TMDB-only file feed for a private stock Plex Series library."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

from .http import JsonTransport
from .reconciler.arrival_readers import StockPlexOwnerReader
from .seerr_compat_events import CompatibilityLinkEvents
from .seerr_compat_census import ShokoInitialCensus
from .seerr_compat_links import LinkProofReader, OwnedPublishedCoordinate
from .seerr_compat_mapping_audit import OwnedMappingAudit
from .seerr_compat_series_recheck import ScopedSeriesRecheck, OwnedSeriesAudit
from .seerr_compat_owned_audit import OwnedDeletionAudit
from .seerr_compat_plex_refresh import VerifiedShowRefresh
from .seerr_compat_store import CompatibilityLinkStore
from .seerr_journal import JsonSessionJournal
from .shoko_event_worker import ShokoEventWorker
from .shoko_long_poll import ShokoLongPoll, ShokoLongPollHttp
from .shoko_recent_catchup import ShokoRecentCatchup, ShokoRecentCatchupWorker


def _require_canonical_stage(store: CompatibilityLinkStore, settings: dict,
                             config_root: Path) -> Path:
    """Identify a separately reviewed stage before canonical writes are wired."""
    receipt = settings.get('canonical_stage_receipt_sha256')
    cursor = settings.get('initial_cursor')
    if (not isinstance(receipt, str)
            or re.fullmatch(r'[0-9a-f]{64}', receipt) is None
            or not isinstance(cursor, dict)
            or set(cursor) != {'imported_utc', 'file_id'}
            or type(cursor['file_id']) is not int or cursor['file_id'] <= 0
            or not isinstance(cursor['imported_utc'], str)):
        raise ValueError('canonical stage receipt and exact cursor required')
    try:
        imported = datetime.fromisoformat(
            cursor['imported_utc'].replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('canonical stage cursor is invalid') from None
    if imported.tzinfo is None or imported.utcoffset() is None:
        raise ValueError('canonical stage cursor lacks a time zone')
    marker = store.root / '.nagisa-canonical-stage.json'
    if (marker.is_symlink() or not marker.is_file()
            or not 0 < marker.stat().st_size <= 4096):
        raise ValueError('canonical stage marker is absent')
    try:
        saved = json.loads(marker.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError('canonical stage marker is invalid') from None
    if (not isinstance(saved, dict)
            or set(saved) != {'schema', 'episode_ordering', 'receipt_sha256',
                              'initial_cursor', 'initial_ready'}
            or type(saved['schema']) is not int or saved['schema'] != 1
            or saved['episode_ordering'] != 'seerr_tvdb'
            or saved['receipt_sha256'] != receipt
            or saved['initial_cursor'] != cursor
            or type(saved['initial_ready']) is not int
            or not 0 < saved['initial_ready'] <= 32768):
        raise ValueError('canonical stage marker differs from settings')
    manifest = store.manifest()
    links = manifest.get('links') if isinstance(manifest, dict) else None
    if not isinstance(links, dict) or not links:
        raise ValueError('canonical stage ledger is empty')
    if not (config_root / 'seerr-compat-canonical-cursor.json').exists():
        if (len(links) != saved['initial_ready']
                or any(not isinstance(row, dict)
                       or row.get('state') != 'ready'
                       for row in links.values())):
            raise ValueError('canonical stage initial ledger differs')
    return marker


def build_tmdb_event_worker(config: Any, shoko: Any, reader: LinkProofReader,
                            store: CompatibilityLinkStore, settings: dict, *,
                            refresh: VerifiedShowRefresh | None = None
                            ) -> tuple[ShokoEventWorker, tuple]:
    """Assemble a worker without starting a listener, scan, or census."""
    options = {'mode', 'initial_cursor', 'page_size', 'max_pages',
               'refresh_debounce_seconds', 'episode_ordering',
               'canonical_stage_receipt_sha256'}
    if (not isinstance(settings, dict) or not set(settings).issubset(options)
            or settings.get('mode') not in ('report', 'apply')
            or not isinstance(reader, LinkProofReader)
            or not isinstance(store, CompatibilityLinkStore)
            or reader.shoko is not shoko
            or store.identity_provider != 'tmdb'):
        raise ValueError('TMDB hidden feed reader and settings required')
    episode_ordering = settings.get('episode_ordering', 'tmdb')
    if (episode_ordering not in ('tmdb', 'seerr_tvdb')
            or reader.coordinate_mode != ('tvdb' if episode_ordering == 'seerr_tvdb'
                                           else 'tmdb')):
        raise ValueError('private episode order does not match reader')
    if (episode_ordering != 'seerr_tvdb'
            and 'canonical_stage_receipt_sha256' in settings):
        raise ValueError('canonical stage receipt requires exact episode order')
    if settings['mode'] == 'apply' and (not isinstance(refresh, VerifiedShowRefresh)
                                       or refresh.store is not store
                                       or refresh.identity_provider != 'tmdb'):
        raise ValueError('exact Plex TMDB verifier required for apply')
    if settings['mode'] == 'report' and refresh is not None:
        raise ValueError('report feed must not refresh Plex')

    root = Path(config.config_dir)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError('Nagisa configuration directory is invalid')
    if store.root == root or store.root.is_relative_to(root):
        raise ValueError('hidden feed requires a separate media root')
    stage_marker = None
    if episode_ordering == 'seerr_tvdb' and settings['mode'] == 'apply':
        stage_marker = _require_canonical_stage(store, settings, root)

    journal_prefix = ('seerr-compat-canonical'
                      if episode_ordering == 'seerr_tvdb'
                      else 'seerr-compat-tmdb')
    pending = JsonSessionJournal(root / f'{journal_prefix}-pending.json')
    cursor = JsonSessionJournal(root / f'{journal_prefix}-cursor.json')
    backlog = JsonSessionJournal(root / f'{journal_prefix}-backlog.json')
    processor = CompatibilityLinkEvents(
        reader, store, pending, write_enabled=settings['mode'] == 'apply',
        refresh=refresh,
        refresh_debounce_seconds=settings.get('refresh_debounce_seconds', 30))
    series_journal = JsonSessionJournal(
        root / f'{journal_prefix}-series-recheck.json')
    series_recheck = ScopedSeriesRecheck(
        shoko, processor, series_journal, batch_size=5)
    processor.attach_series_recheck(series_recheck)
    series_audit_journal = JsonSessionJournal(
        root / f'{journal_prefix}-series-audit.json')
    series_audit = OwnedSeriesAudit(
        store, series_recheck, series_audit_journal,
        batch_size=5, interval_seconds=600)
    recent = ShokoRecentCatchup(
        shoko, page_size=settings.get('page_size', 100),
        max_pages=settings.get('max_pages', 5))
    catchup = ShokoRecentCatchupWorker(
        recent, processor, cursor, backlog_journal=backlog)
    if not cursor.path.exists() and 'initial_cursor' in settings:
        catchup.bootstrap_from_exact_cursor(settings['initial_cursor'])
    try:
        catchup._watermark()
    except ValueError as error:
        raise ValueError('reviewed Shoko import cursor required') from error

    def client_factory() -> ShokoLongPoll:
        http = ShokoLongPollHttp(config.shoko_url, config.shoko_token)
        return ShokoLongPoll(http, processor, catchup, feeds='file,metadata')

    # Five reviews per 15-second runtime tick clear a 25-file mapping slice
    # in 75 seconds while leaving room for new Shoko file notices.
    worker = ShokoEventWorker(
        client_factory, processor, catchup=catchup,
        series_recheck=series_recheck, series_audit=series_audit,
        review_limit=5)
    access = 'read/write' if settings['mode'] == 'apply' else 'read-only'
    bindings = (
        (str(store.root), 'Nagisa private Plex TMDB source', access),
        (str(pending.path), 'Private feed pending Shoko file IDs', 'read/write'),
        (str(cursor.path), 'Private feed Shoko import cursor', 'read/write'),
        (str(backlog.path), 'Private feed catch-up backlog', 'read/write'),
        (str(series_journal.path), 'Private TMDB series recheck queue',
         'read/write'),
        (str(series_audit_journal.path), 'Private TMDB series audit cursor',
         'read/write'),
    ) + (((str(stage_marker), 'Reviewed canonical stage identity', 'read-only'),)
         if stage_marker is not None else ())
    return worker, bindings


def build_configured_tmdb_feed(config: Any, shoko: Any, plex: Any,
                               seerr: Any, settings: dict,
                               part_probe: Any
                               ) -> tuple[ShokoEventWorker, tuple]:
    """Validate a private stock-Plex source, then assemble one TMDB worker."""
    required = {'mode', 'link_root', 'plex_link_root', 'section_id',
                'source_section_ids', 'managed_folders', 'ignored_folders'}
    optional = {'initial_cursor', 'page_size', 'max_pages',
                'refresh_debounce_seconds', 'episode_ordering',
                'canonical_stage_receipt_sha256'}
    if (not isinstance(settings, dict)
            or not required.issubset(settings)
            or not set(settings).issubset(required | optional)
            or settings['mode'] not in ('report', 'apply')
            or settings.get('episode_ordering', 'tmdb') not in
               ('tmdb', 'seerr_tvdb')
            or type(settings['section_id']) is not int
            or settings['section_id'] <= 0
            or not isinstance(settings['source_section_ids'], list)
            or not 1 <= len(settings['source_section_ids']) <= 2
            or any(type(section) is not int or section <= 0
                   for section in settings['source_section_ids'])
            or len(set(settings['source_section_ids']))
               != len(settings['source_section_ids'])
            or settings['section_id'] in settings['source_section_ids']
            or not set(settings['source_section_ids']).issubset(
                config.plex_section_ids)
            or not callable(part_probe)):
        raise ValueError('unsafe TMDB hidden feed settings')

    link_root = Path(settings['link_root'])
    plex_root = settings['plex_link_root']
    if (not link_root.is_absolute() or not link_root.is_dir()
            or link_root.is_symlink()
            or not isinstance(plex_root, str)
            or not plex_root.startswith('/') or plex_root == '/'
            or '\\' in plex_root or '\x00' in plex_root
            or any(part in ('', '.', '..')
                   for part in plex_root[1:].rstrip('/').split('/'))):
        raise ValueError('private feed root is unsafe or unmounted')

    def folders(value: Any, *, ignored: bool) -> dict:
        if (not isinstance(value, list) or len(value) > 8
                or (not ignored and not value)):
            raise ValueError('private Shoko source folders are invalid')
        rows = {}
        for entry in value:
            keys = {'id', 'root'} if ignored else {'id', 'root', 'plex_alias'}
            if (not isinstance(entry, dict) or set(entry) != keys
                    or type(entry['id']) is not int or entry['id'] <= 0
                    or entry['id'] in rows or not isinstance(entry['root'], str)):
                raise ValueError('private Shoko source folder is invalid')
            root = Path(entry['root'])
            if not root.is_absolute() or not root.is_dir() or root.is_symlink():
                raise ValueError('private Shoko source root is unsafe')
            rows[entry['id']] = root if ignored else (
                root, entry['plex_alias'])
        return rows

    managed = folders(settings['managed_folders'], ignored=False)
    ignored = folders(settings['ignored_folders'], ignored=True)
    if set(managed) & set(ignored):
        raise ValueError('private Shoko source roles overlap')

    section_id = settings['section_id']

    def require_section() -> None:
        response = plex.request_json(
            'GET', '/library/sections?includeDetails=1&includePreferences=1&skipRefresh=1')
        body = response.get('MediaContainer') if isinstance(response, dict) else None
        rows = body.get('Directory') if isinstance(body, dict) else None
        matches = ([row for row in rows if isinstance(row, dict)
                    and str(row.get('key')) == str(section_id)]
                   if isinstance(rows, list) else [])
        if len(matches) != 1:
            raise ValueError('private stock Plex section is absent')
        row = matches[0]
        locations = row.get('Location')
        preferences = row.get('Preferences')
        values = preferences.get('Setting') if isinstance(preferences, dict) else None
        chosen = ({item.get('id'): item.get('value') for item in values
                   if isinstance(item, dict)} if isinstance(values, list) else {})
        if (row.get('type') != 'show'
                or row.get('agent') != 'tv.plex.agents.series'
                or not isinstance(locations, list) or len(locations) != 1
                or not isinstance(locations[0], dict)
                or locations[0].get('path') != plex_root
                or str(chosen.get('hidden')) != '2'
                or chosen.get('showOrdering') != 'tmdbAiring'):
            raise ValueError('private stock Plex section changed')

    require_section()
    sections = tuple(settings['source_section_ids'])
    ledger = link_root / '.nagisa-compat-tmdb.sqlite'
    if (ledger.is_symlink() or not ledger.is_file()
            or ledger.stat().st_size == 0):
        raise ValueError('private feed ownership ledger is absent')
    store = CompatibilityLinkStore(link_root, identity_provider='tmdb')
    owner = StockPlexOwnerReader(
        plex, sections, batch_details=True, exact_guid_filter=True)
    reader = LinkProofReader(
        shoko, owner, plex, seerr, sections=sections,
        allowed_folders=managed, ignored_source_folders=ignored,
        part_probe=part_probe,
        seerr_cache_seconds=(3600 if settings.get('episode_ordering') == 'seerr_tvdb'
                             else 0),
        published_resolver=(OwnedPublishedCoordinate(store)
                            if settings.get('episode_ordering') == 'seerr_tvdb'
                            else None),
        coordinate_mode=('tvdb' if settings.get('episode_ordering') == 'seerr_tvdb'
                         else 'tmdb'))
    refresh = None
    if settings['mode'] == 'apply':
        transport = JsonTransport(
            config.plex_url, headers={'X-Plex-Token': config.plex_token},
            timeout=config.request_timeout_seconds,
            allowed_methods=frozenset({'POST', 'PUT'}))

        def post_refresh(path: str) -> None:
            transport.request_json('POST', path)

        def refresh_item(key: str) -> None:
            if not isinstance(key, str) or not key.isdecimal() or int(key) <= 0:
                raise ValueError('exact Plex show key required')
            transport.request_json('PUT', f'/library/metadata/{key}/refresh')

        def empty_trash(checked_section: int) -> None:
            if checked_section != section_id:
                raise ValueError('private Plex Trash section changed')
            require_section()
            transport.request_json(
                'PUT', f'/library/sections/{section_id}/emptyTrash')

        refresh = VerifiedShowRefresh(
            plex, store, section_id, plex_root, require_section,
            post_refresh, identity_provider='tmdb',
            refresh_item=refresh_item, empty_trash=empty_trash)
    worker_settings = {key: settings[key] for key in optional | {'mode'}
                       if key in settings}
    worker, bindings = build_tmdb_event_worker(
        config, shoko, reader, store, worker_settings, refresh=refresh)
    worker.owned_audit = OwnedDeletionAudit(
        ShokoInitialCensus(shoko, page_size=1000, max_files=32768),
        store, worker.processor)
    mapping_prefix = ('seerr-compat-canonical'
                      if settings.get('episode_ordering') == 'seerr_tvdb'
                      else 'seerr-compat-tmdb')
    mapping_journal = JsonSessionJournal(
        Path(config.config_dir) / f'{mapping_prefix}-mapping.json')
    worker.mapping_audit = OwnedMappingAudit(
        store, worker.processor, mapping_journal, batch_size=25)
    source_bindings = tuple(
        (str(root), f'Shoko managed source folder {folder_id}', 'read-only')
        for folder_id, (root, _) in managed.items())
    return worker, bindings + source_bindings + ((
        str(mapping_journal.path), 'Private TMDB mapping audit cursor',
        'read/write'),)
