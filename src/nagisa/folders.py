"""Public, secret-free inventory of folders used by the running Nagisa setup."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any


def read_managed_folders(path: str | Path | None) -> tuple[dict[str, str], ...]:
    """Read an explicit NAS-side inventory; never infer host paths from mounts."""
    if path is None:
        return ()
    file = Path(path)
    if (not file.is_absolute() or file.is_symlink() or not file.is_file()
            or not 0 < file.stat().st_size <= 16384):
        raise ValueError('Nagisa folder inventory file is invalid')
    try:
        data = json.loads(file.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError('Nagisa folder inventory is not valid JSON') from None
    rows = data.get('managed_paths') if isinstance(data, dict) else None
    if (not isinstance(data, dict) or set(data) != {'managed_paths'}
            or not isinstance(rows, list) or len(rows) > 100):
        raise ValueError('Nagisa folder inventory format is invalid')
    seen: set[str] = set()
    managed = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'host_path', 'owner', 'purpose', 'change'}:
            raise ValueError('Nagisa managed folder record is invalid')
        host = row['host_path']
        if (not isinstance(host, str) or not host.startswith('/') or host.startswith('//')
                or '\x00' in host or '\\' in host or host.endswith('/')
                or str(PurePosixPath(host)) != host or host == '/'
                or any(part in ('.', '..') for part in host.split('/'))
                or host in seen):
            raise ValueError('Nagisa managed folder path is invalid or duplicate')
        if (row['change'] not in ('created', 'modified')
                or any(not isinstance(row[key], str) or not row[key].strip()
                       or len(row[key]) > 160 or '\x00' in row[key]
                       for key in ('owner', 'purpose'))):
            raise ValueError('Nagisa managed folder description is invalid')
        seen.add(host)
        managed.append({'path': host, 'purpose': row['purpose'].strip(),
                        'access': 'external', 'location': 'NAS',
                        'change': row['change'], 'owner': row['owner'].strip()})
    return tuple(managed)


def folder_inventory(config: Any, delivery: dict | None = None,
                     seerr_bindings: tuple[tuple[str, str, str], ...] = (),
                     managed_folders: tuple[dict[str, str], ...] = ()) -> list[dict]:
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(path: Any, purpose: str, access: str) -> None:
        value = str(path).strip() if path is not None else ''
        if not value or not value.startswith('/') or access not in ('read-only', 'read/write'):
            return
        key = value, purpose, access
        if key not in seen:
            rows.append({'path': value, 'purpose': purpose, 'access': access,
                         'location': 'container', 'change': 'existing',
                         'owner': 'Nagisa'})
            seen.add(key)

    add(getattr(config, 'config_dir', None), 'Nagisa settings, journal and history', 'read/write')
    add(getattr(config, 'import_root', None), 'Existing Shoko import folder', 'read-only')
    add(getattr(config, 'anime_root', None), 'Existing anime media folder', 'read-only')
    delivery = delivery if isinstance(delivery, dict) else {}
    for key, purpose in (
        ('managed_roots', 'Managed anime root'),
        ('plex_mounts', 'Plex source mapping'),
        ('subtitle_source_mounts', 'Original-download subtitle source'),
    ):
        bindings = delivery.get(key)
        if isinstance(bindings, dict):
            for original, mounted in bindings.items():
                add(mounted, f'{purpose} for {original}', 'read-only')
    add(delivery.get('subtitle_destination_mount'),
        'Original-release subtitle destination', 'read/write')
    add(delivery.get('movie_arrival_import_root'),
        'Existing movie import folder', 'read-only')
    add(delivery.get('movie_arrival_source_root'),
        'Existing movie media source', 'read-only')
    for binding in seerr_bindings:
        if (isinstance(binding, tuple) and len(binding) == 3
                and all(isinstance(item, str) for item in binding)):
            add(*binding)
    rows.extend(managed_folders)
    return sorted(rows, key=lambda row: (row['path'], row['purpose']))
