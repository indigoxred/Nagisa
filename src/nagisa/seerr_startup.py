"""Opt-in assembly of the private Seerr guard inside the existing Nagisa app."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from nagisa.http import JsonTransport
from nagisa.seerr_boundary import PrivateSonarrBoundary
from nagisa.seerr_endpoint import SonarrCompatibleApp
from nagisa.seerr_evidence import (ExactEpisodeCrosswalk, GuardEvidenceFallback,
                                  NativeEvidenceReader, NativePlexCards,
                                  PublishedEvidenceReader, ReadOnlyPlexPartProbe,
                                  StockSonarrTvdbCatalogue, TvdbEpisodeTransport)
from nagisa.seerr_journal import JsonSessionJournal


_FIELDS = frozenset({
    'seerr_url', 'seerr_token_file', 'plex_mounts', 'unknown_policy',
})


def require_prevent_search(seerr: Any, service_id: int) -> None:
    """Check the exact stock Seerr service before accepting anime requests."""
    if type(service_id) is not int or service_id <= 0:
        raise ValueError('Seerr anime Sonarr service ID is invalid')
    rows = seerr.request_json('GET', '/api/v1/settings/sonarr')
    if not isinstance(rows, list):
        raise ValueError('Seerr Sonarr settings are unavailable')
    matches = [row for row in rows if isinstance(row, dict)
               and row.get('id') == service_id]
    if len(matches) != 1 or matches[0].get('preventSearch') is not True:
        raise ValueError('Seerr anime Sonarr service must prevent search')


def _private_file(value: str | Path, root: Path, maximum: int) -> Path:
    path = Path(value)
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or not 0 < path.stat().st_size <= maximum):
        raise ValueError('Seerr guard file must be a small regular config file')
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        raise ValueError('Seerr guard file must remain inside CONFIG_DIR') from None
    return path


def _origin(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError('Seerr URL must be a plain origin')
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment):
        raise ValueError('Seerr URL must be a plain origin')
    return value.rstrip('/')


def build_seerr_app(config: Any, settings_path: str | Path | None,
                    audit: Any) -> SonarrCompatibleApp | None:
    """No settings file means no route. Invalid opt-in fails startup closed."""
    if settings_path is None:
        return None
    root = Path(config.config_dir)
    settings_file = _private_file(settings_path, root, 8192)
    try:
        settings = json.loads(settings_file.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError('Seerr guard settings are not valid JSON') from None
    if (not isinstance(settings, dict) or not _FIELDS.issubset(settings)
            or not set(settings).issubset(_FIELDS | {
                'availability', 'exact_ids', 'published_mappings',
                'seerr_sonarr_service_id'})):
        raise ValueError('Seerr guard settings are incomplete or unexpected')
    seerr_url = _origin(settings['seerr_url'])
    token_file = _private_file(settings['seerr_token_file'], root, 4096)
    token = token_file.read_text(encoding='utf-8').strip()
    if len(token) < 8 or any(character in token for character in '\r\n\x00'):
        raise ValueError('Seerr API token is invalid')
    mounted = settings['plex_mounts']
    if not isinstance(mounted, list) or not 1 <= len(mounted) <= 8:
        raise ValueError('Seerr guard requires bounded Plex mounts')
    probes = []
    for entry in mounted:
        if not isinstance(entry, dict) or set(entry) != {'plex_path_root', 'media_mount'}:
            raise ValueError('Seerr guard Plex mount entry is invalid')
        plex_root = entry['plex_path_root']
        if (not isinstance(plex_root, str) or not plex_root.startswith('/')
                or not plex_root.endswith('/') or plex_root == '/'
                or '\\' in plex_root or '\x00' in plex_root
                or any(segment in ('', '.', '..') for segment in plex_root[1:-1].split('/'))):
            raise ValueError('Seerr guard Plex root is invalid')
        media_mount = entry['media_mount']
        if not isinstance(media_mount, str):
            raise ValueError('Seerr guard media mount is invalid')
        mount = Path(media_mount)
        if not mount.is_absolute() or mount.is_symlink() or not mount.is_dir():
            raise ValueError('Seerr guard media mount must be an existing directory')
        if any(plex_root.startswith(existing_root) or existing_root.startswith(plex_root)
               for existing_root, _ in probes):
            raise ValueError('Seerr guard Plex roots overlap')
        probes.append((plex_root, ReadOnlyPlexPartProbe(plex_root, mount)))
    plex_roots = tuple(root for root, _ in probes)

    def probe_part(plex_path: str) -> bool:
        for prefix, probe in probes:
            if isinstance(plex_path, str) and plex_path.startswith(prefix):
                return probe(plex_path)
        return False
    unknown_policy = settings['unknown_policy']
    if unknown_policy not in ('hold', 'stock_pass_through'):
        raise ValueError('Seerr guard unknown-show policy is invalid')
    service_id = settings.get('seerr_sonarr_service_id')
    if unknown_policy == 'stock_pass_through' and (
            type(service_id) is not int or service_id <= 0):
        raise ValueError('first-time anime requires a Seerr Sonarr service ID')
    if service_id is not None and (type(service_id) is not int or service_id <= 0):
        raise ValueError('Seerr anime Sonarr service ID is invalid')

    timeout = config.request_timeout_seconds
    shoko = JsonTransport(config.shoko_url, headers={'apikey': config.shoko_token},
                          timeout=timeout, allowed_methods=frozenset({'GET'}))
    plex = JsonTransport(config.plex_url, headers={'X-Plex-Token': config.plex_token},
                         timeout=timeout, allowed_methods=frozenset({'GET'}))
    seerr = JsonTransport(seerr_url, headers={'X-Api-Key': token},
                          timeout=timeout, allowed_methods=frozenset({'GET'}))
    upstream = JsonTransport(config.sonarr_url,
                             headers={'X-Api-Key': config.sonarr_token},
                             timeout=timeout,
                             allowed_methods=frozenset({'GET', 'POST', 'PUT'}))
    cards = NativePlexCards.from_transport(
        plex, config.plex_section_ids,
        access_probe=probe_part)
    crosswalk = None
    credential_bindings = []
    exact_ids = settings.get('exact_ids')
    if exact_ids is not None:
        if (not isinstance(exact_ids, dict)
                or 'tmdb_read_token_file' not in exact_ids
                or ('tvdb_pin_file' in exact_ids and 'tvdb_api_key_file' not in exact_ids)
                or not set(exact_ids).issubset({
                    'tvdb_api_key_file', 'tvdb_pin_file', 'tmdb_read_token_file'})):
            raise ValueError('exact provider IDs require explicit credential files')
        tmdb_token_file = _private_file(exact_ids['tmdb_read_token_file'], root, 4096)
        tmdb_token = tmdb_token_file.read_text(encoding='utf-8').strip()
        if (len(tmdb_token) < 8
                or any(char in tmdb_token for char in '\r\n\x00')):
            raise ValueError('TMDB read token is invalid')
        tvdb = None
        if 'tvdb_api_key_file' in exact_ids:
            tvdb_key_file = _private_file(exact_ids['tvdb_api_key_file'], root, 4096)
            tvdb_key = tvdb_key_file.read_text(encoding='utf-8').strip()
            pin = None
            if 'tvdb_pin_file' in exact_ids:
                pin_file = _private_file(exact_ids['tvdb_pin_file'], root, 4096)
                pin = pin_file.read_text(encoding='utf-8').strip()
                credential_bindings.append((str(pin_file), 'TVDB subscriber PIN', 'read-only'))
            tvdb = TvdbEpisodeTransport(tvdb_key, pin=pin, timeout=timeout)
            credential_bindings.append((str(tvdb_key_file), 'TVDB API key', 'read-only'))
        tmdb = JsonTransport(
            'https://api.themoviedb.org',
            headers={'Authorization': 'Bearer ' + tmdb_token},
            timeout=timeout, allowed_methods=frozenset({'GET'}))
        crosswalk = ExactEpisodeCrosswalk(tvdb, tmdb)
        credential_bindings.append(
            (str(tmdb_token_file), 'TMDB read token', 'read-only'))
    evidence = NativeEvidenceReader(
        shoko, cards, seerr, plex_roots=plex_roots,
        unknown_policy=unknown_policy, exact_crosswalk=crosswalk)
    published_settings = settings.get('published_mappings')
    if published_settings is not None:
        if (not isinstance(published_settings, dict)
                or set(published_settings) != {'snapshot_file', 'sha256'}
                or not isinstance(published_settings['sha256'], str)
                or len(published_settings['sha256']) != 64
                or any(char not in '0123456789abcdef'
                       for char in published_settings['sha256'])):
            raise ValueError('published Anime-Lists pin is invalid')
        snapshot_file = _private_file(
            published_settings['snapshot_file'], root, 16_000_000)
        snapshot = snapshot_file.read_bytes()
        if hashlib.sha256(snapshot).hexdigest() != published_settings['sha256']:
            raise ValueError('published Anime-Lists snapshot hash differs')
        sonarr_readonly = JsonTransport(
            config.sonarr_url, headers={'X-Api-Key': config.sonarr_token},
            timeout=timeout, allowed_methods=frozenset({'GET'}))
        published = PublishedEvidenceReader(
            shoko, cards, seerr, snapshot,
            sonarr_catalogue=StockSonarrTvdbCatalogue(sonarr_readonly),
            plex_roots=plex_roots, unknown_policy=unknown_policy)
        evidence = GuardEvidenceFallback(evidence, published)

    def report_unknown(risk: dict) -> None:
        if (not isinstance(risk, dict)
                or set(risk) != {'operation', 'tvdb_id', 'tmdb_id', 'risk'}
                or risk['operation'] not in ('add', 'update')
                or risk['risk'] != 'unlinked_existing_media'
                or any(type(risk[key]) is not int or risk[key] <= 0
                       for key in ('tvdb_id', 'tmdb_id'))):
            raise ValueError('invalid Seerr unverified-show risk')
        audit.record('system', 'seerr_ownership_held', risk)

    boundary = PrivateSonarrBoundary(
        upstream, evidence,
        journal=JsonSessionJournal(root / 'seerr-guard-sessions.json'),
        unknown_reporter=report_unknown if unknown_policy == 'stock_pass_through' else None)
    app = SonarrCompatibleApp(boundary, upstream, config.sonarr_token)
    if service_id is not None:
        app.preflight = lambda: require_prevent_search(seerr, service_id)
    folder_bindings = [(entry['media_mount'],
                        f"Seerr anime ownership source for {entry['plex_path_root']}",
                        'read-only') for entry in mounted] + credential_bindings
    if published_settings is not None:
        folder_bindings.append((str(snapshot_file),
                                'Pinned Anime-Lists episode mapping source',
                                'read-only'))
    app.availability_sweep = None
    availability = settings.get('availability')
    if availability is not None:
        expected = {'server_id', 'poll_seconds', 'max_per_cycle'}
        if (not isinstance(availability, dict)
                or not expected.issubset(availability)
                or not set(availability).issubset(expected | {'mode', 'database', 'expected_version'})
                or availability.get('mode', 'report') not in ('report', 'apply')):
            raise ValueError('Seerr availability settings are invalid')
        database = availability.get('database')
        if database is not None and availability.get('mode', 'report') == 'apply':
            raise ValueError('direct Seerr database writes are not supported')
        expected_version = availability.get('expected_version')
        if database is None and availability.get('mode', 'report') == 'apply' and expected_version is None:
            raise ValueError('Seerr availability apply requires an expected version')
        if expected_version is not None and (not isinstance(expected_version, str)
                or not 1 <= len(expected_version) <= 80
                or any(character.isspace() or ord(character) < 32 for character in expected_version)):
            raise ValueError('Seerr expected version is invalid')
        status_transport = JsonTransport(
            seerr_url, headers={'X-Api-Key': token}, timeout=timeout,
            allowed_methods=frozenset({'GET'} if database is not None
                                       else {'GET', 'POST'}))
        if database is None:
            from nagisa.seerr_availability import NativeSeerrAvailability
            from nagisa.seerr_availability_sweep import SeerrAvailabilitySweep
            availability_reconciler = NativeSeerrAvailability(
                status_transport, evidence, promotions_only=True,
                write_enabled=availability.get('mode', 'report') == 'apply')
            app.availability_sweep = SeerrAvailabilitySweep(
                status_transport, availability_reconciler,
                server_id=availability['server_id'],
                poll_seconds=availability['poll_seconds'],
                max_per_cycle=availability['max_per_cycle'],
                expected_version=expected_version)
        else:
            if (not isinstance(database, dict)
                    or set(database) != {'path', 'schema_sha256',
                                         'migrations_sha256', 'version'}):
                raise ValueError('Seerr database settings are invalid')
            from nagisa.seerr_database import SeerrDatabase
            from nagisa.seerr_database_reconcile import SeerrDatabaseReconciler
            from nagisa.seerr_database_sweep import SeerrDatabaseSweep

            def installed_version() -> str:
                status = status_transport.request_json('GET', '/api/v1/status')
                if not isinstance(status, dict) or not isinstance(status.get('version'), str):
                    raise ValueError('Seerr version response is invalid')
                return status['version']

            writer = SeerrDatabase(
                database['path'], expected_schema=database['schema_sha256'],
                expected_migrations=database['migrations_sha256'],
                expected_version=database['version'], version=installed_version,
                write_enabled=availability.get('mode', 'report') == 'apply')
            folder_bindings.append((str(writer.path), 'Seerr availability records',
                                    'read/write' if writer.write_enabled else 'read-only'))
            availability_reconciler = SeerrDatabaseReconciler(
                status_transport, evidence, writer)
            app.availability_sweep = SeerrDatabaseSweep(
                status_transport, availability_reconciler,
                server_id=availability['server_id'],
                poll_seconds=availability['poll_seconds'],
                max_per_cycle=availability['max_per_cycle'])
    app.folder_bindings = tuple(folder_bindings)
    return app
