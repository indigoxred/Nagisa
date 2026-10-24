"""Confirm one private compatibility show's stock Plex scan before acking it."""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote


class VerifiedShowRefresh:
    def __init__(self, plex: Any, store: Any, section_id: int,
                 plex_root: str, require_section: Any, post_refresh: Any,
                 *, identity_provider: str = 'tvdb',
                 refresh_item: Any = None, empty_trash: Any = None):
        if (type(section_id) is not int or section_id <= 0
                or not plex_root.startswith('/') or plex_root == '/'
                or not callable(require_section)
                or not callable(post_refresh)
                or (refresh_item is not None and not callable(refresh_item))
                or (empty_trash is not None and (not callable(empty_trash)
                    or identity_provider != 'tmdb'))
                or identity_provider not in ('tvdb', 'tmdb')):
            raise ValueError('invalid compatibility Plex refresh boundary')
        self.plex = plex
        self.store = store
        self.section_id = section_id
        self.plex_root = plex_root.rstrip('/')
        self.require_section = require_section
        self.post_refresh = post_refresh
        self.identity_provider = identity_provider
        self.refresh_item = refresh_item
        self.empty_trash = empty_trash

    def _rows(self, path: str, *, maximum: int) -> list[dict]:
        rows = []
        offset = 0
        while True:
            reply = self.plex.request_json(
                'GET', f'{path}&X-Plex-Container-Size=1000'
                f'&X-Plex-Container-Start={offset}')
            body = reply.get('MediaContainer') if isinstance(reply, dict) else None
            page = body.get('Metadata', []) if isinstance(body, dict) else None
            size = body.get('size') if isinstance(body, dict) else None
            total = body.get('totalSize') if isinstance(body, dict) else None
            if (not isinstance(page, list)
                    or any(not isinstance(row, dict) for row in page)
                    or type(size) is not int or size != len(page)
                    or (total is not None
                        and (type(total) is not int or total < 0))
                    or len(rows) + len(page) > maximum):
                raise ValueError('compatibility Plex listing is incomplete')
            rows.extend(page)
            offset += len(page)
            if total is not None:
                if offset == total:
                    return rows
                if offset > total or not page:
                    raise ValueError('compatibility Plex listing changed')
            elif len(page) < 1000:
                return rows
            elif not page:
                raise ValueError('compatibility Plex listing is unbounded')

    def __call__(self, show_id: int) -> bool:
        if type(show_id) is not int or show_id <= 0:
            # An old v3 whole-section intent has no exact expected show. It
            # stays durable for an explicit migration, never auto-acked.
            raise ValueError('exact compatibility show required for scan proof')
        self.require_section()
        expected = {f'{self.plex_root}/{link.replace(chr(92), "/")}'
                    for link in self.store.show_links(show_id)}
        shows = self._rows(
            f'/library/sections/{self.section_id}/all?type=2'
            '&includeGuids=1&skipRefresh=1', maximum=5000)
        matches = [row for row in shows
                   if row.get('type') == 'show'
                   and any(isinstance(guid, dict)
                           and guid.get('id') ==
                           f'{self.identity_provider}://{show_id}'
                           for guid in row.get('Guid', []))]
        if len(matches) > 1:
            raise ValueError('compatibility Plex show identity is ambiguous')
        def part_paths(show: dict) -> list[str]:
            key = show.get('ratingKey')
            if not isinstance(key, str) or not key.isdecimal():
                raise ValueError('compatibility Plex show key is invalid')
            leaves = self._rows(
                f'/library/metadata/{key}/allLeaves?skipRefresh=1',
                maximum=10000)
            paths = []
            for row in leaves:
                if row.get('type') != 'episode':
                    raise ValueError('compatibility Plex show has non-episode leaf')
                for media in row.get('Media', []):
                    if not isinstance(media, dict):
                        raise ValueError('compatibility Plex media is invalid')
                    for part in media.get('Part', []):
                        path = part.get('file') if isinstance(part, dict) else None
                        if not isinstance(path, str):
                            raise ValueError('compatibility Plex part is invalid')
                        paths.append(path)
            return paths

        observed = part_paths(matches[0]) if matches else []
        quarantine_group = None
        if self.identity_provider == 'tmdb' and matches:
            tmdb_tags = [guid.get('id') for guid in matches[0].get('Guid', [])
                         if isinstance(guid, dict)
                         and isinstance(guid.get('id'), str)
                         and guid['id'].startswith('tmdb://')]
            if tmdb_tags != [f'tmdb://{show_id}']:
                quarantine = getattr(self.store, 'quarantine_card', None)
                is_quarantined = getattr(self.store,
                                         'quarantined_show_ids', None)
                try:
                    ids = tuple(sorted(int(tag[7:]) for tag in tmdb_tags))
                except ValueError:
                    ids = ()
                if (not 2 <= len(ids) <= 20 or len(set(ids)) != len(ids)
                        or any(identity <= 0 for identity in ids)
                        or show_id not in ids):
                    raise ValueError('compatibility Plex TMDB identity is ambiguous')
                if (not expected and callable(is_quarantined)
                        and is_quarantined(ids)):
                    quarantine_group = ids
                    if matches[0].get('deletedAt') is None:
                        for identity in ids:
                            target = f'{self.plex_root}/tmdb-{identity}'
                            self.post_refresh(
                                f'/library/sections/{self.section_id}/refresh'
                                f'?path={quote(target, safe="/")}')
                        return False
                elif (not callable(quarantine)
                      or matches[0].get('deletedAt') is not None):
                    raise ValueError('compatibility Plex TMDB identity is ambiguous')
                if quarantine_group is None:
                    owned = frozenset(link for identity in ids
                                      for link in self.store.show_links(identity))
                    paths = {f'{self.plex_root}/{link.replace(chr(92), "/")}'
                             for link in owned}
                    if (not owned or len(observed) != len(set(observed))
                            or set(observed) != paths):
                        raise ValueError('compatibility Plex TMDB identity is ambiguous')
                    file_ids = quarantine(ids, owned)
                    if (not isinstance(file_ids, tuple) or not file_ids
                            or any(type(file_id) is not int or file_id <= 0
                                   for file_id in file_ids)
                            or tuple(sorted(set(file_ids))) != file_ids):
                        raise ValueError('private card quarantine record is invalid')
                    logging.getLogger(__name__).warning(
                        'private Plex card quarantined tmdb_ids=%s file_ids=%s',
                        ids, file_ids)
                    for identity in ids:
                        target = f'{self.plex_root}/tmdb-{identity}'
                        self.post_refresh(
                            f'/library/sections/{self.section_id}/refresh'
                            f'?path={quote(target, safe="/")}')
                    return False
        if not expected and self.empty_trash is not None:
            owned_ids_for = getattr(self.store, 'owned_file_ids_for_shows', None)
            if not callable(owned_ids_for):
                raise ValueError('private Plex owned claim check is unavailable')
            owned_ids = owned_ids_for(quarantine_group or (show_id,))
            if not isinstance(owned_ids, tuple) or owned_ids:
                raise ValueError('private Plex show still has an owned claim')
        if not expected and matches and self.empty_trash is not None:
            targets = tuple(
                f'{self.plex_root}/{self.identity_provider}-{identity}'
                for identity in (quarantine_group or (show_id,)))
            trashed_shows = [row for row in shows
                             if row.get('deletedAt') is not None]
            if trashed_shows and trashed_shows != matches:
                raise ValueError('private Plex Trash includes another show')
            if matches[0].get('deletedAt') is not None:
                if (type(matches[0]['deletedAt']) is not int
                        or matches[0]['deletedAt'] <= 0):
                    raise ValueError('private Plex show Trash marker is invalid')
                key = matches[0]['ratingKey']
                all_seasons = self._rows(
                    f'/library/sections/{self.section_id}/all?type=3'
                    '&skipRefresh=1', maximum=5000)
                seasons = [season for season in all_seasons
                           if season.get('deletedAt') is not None]
                season_keys = set()
                for season in seasons:
                    season_key = season.get('ratingKey')
                    if (season.get('type') != 'season'
                            or not isinstance(season_key, str)
                            or not season_key.isdecimal()
                            or season_key in season_keys
                            or str(season.get('parentRatingKey')) != key
                            or type(season.get('deletedAt')) is not int
                            or season['deletedAt'] <= 0):
                        raise ValueError('private Plex Trash contains an unrelated season')
                    season_keys.add(season_key)
                if not seasons:
                    raise ValueError('private Plex Trash season proof is absent')
                trashed = self._rows(
                    f'/library/sections/{self.section_id}/all?type=4'
                    '&episode.trash=1&skipRefresh=1', maximum=1000)
                paths = []
                episode_keys = set()
                for episode in trashed:
                    episode_key = episode.get('ratingKey')
                    if (episode.get('type') != 'episode'
                            or not isinstance(episode_key, str)
                            or not episode_key.isdecimal()
                            or episode_key in episode_keys
                            or str(episode.get('grandparentRatingKey')) != key
                            or type(episode.get('deletedAt')) is not int
                            or episode['deletedAt'] <= 0
                            or not isinstance(episode.get('Media'), list)
                            or not episode['Media']):
                        raise ValueError('private Plex Trash contains an unrelated episode')
                    episode_keys.add(episode_key)
                    for media in episode['Media']:
                        if (not isinstance(media, dict)
                                or not isinstance(media.get('Part'), list)
                                or not media['Part']):
                            raise ValueError('private Plex Trash episode lacks Parts')
                        for part in media['Part']:
                            path = part.get('file') if isinstance(part, dict) else None
                            if (not isinstance(path, str)
                                    or not any(path.startswith(target + '/')
                                               for target in targets)
                                    or '..' in PurePosixPath(path).parts):
                                raise ValueError('private Plex Trash Part is outside owned show')
                            paths.append(path)
                if (paths and len(paths) == len(set(paths))
                        and len(observed) == len(set(observed))
                        and set(observed) <= set(paths)):
                    # Plex clears every record in a section. Re-read the
                    # complete bounded Trash view immediately before PUT so
                    # a newly trashed foreign item cannot be cleared using
                    # an earlier snapshot.
                    if (self._rows(
                            f'/library/sections/{self.section_id}/all?type=2'
                            '&includeGuids=1&skipRefresh=1', maximum=5000)
                            != shows
                            or self._rows(
                                f'/library/sections/{self.section_id}/all?type=3'
                                '&skipRefresh=1', maximum=5000)
                            != all_seasons
                            or self._rows(
                                f'/library/sections/{self.section_id}/all?type=4'
                                '&episode.trash=1&skipRefresh=1', maximum=1000)
                            != trashed):
                        raise ValueError('private Plex Trash changed before clear')
                    final_owned_ids = owned_ids_for(
                        quarantine_group or (show_id,))
                    if (not isinstance(final_owned_ids, tuple)
                            or final_owned_ids):
                        raise ValueError('private Plex owned claim changed before clear')
                    self.empty_trash(self.section_id)
                    return False
                if paths:
                    raise ValueError('private Plex Trash Parts are ambiguous')
        if not expected and not matches and self.empty_trash is not None:
            target = f'{self.plex_root}/{self.identity_provider}-{show_id}/'
            trashed = self._rows(
                f'/library/sections/{self.section_id}/all?type=4'
                '&episode.trash=1&skipRefresh=1', maximum=1000)
            for episode in trashed:
                media_rows = episode.get('Media')
                if not isinstance(media_rows, list) or not media_rows:
                    raise ValueError('private Plex unmatched Trash is incomplete')
                for media in media_rows:
                    parts = media.get('Part') if isinstance(media, dict) else None
                    if not isinstance(parts, list) or not parts:
                        raise ValueError('private Plex unmatched Trash Parts are incomplete')
                    for part in parts:
                        path = part.get('file') if isinstance(part, dict) else None
                        if not isinstance(path, str):
                            raise ValueError('private Plex unmatched Trash path is invalid')
                        if path.startswith(target):
                            raise ValueError('private Plex unmatched show remains in Trash')
        if (not matches and expected and self.refresh_item is not None
                and self.identity_provider == 'tmdb'):
            tagless = [row for row in shows
                       if row.get('type') == 'show'
                       and not any(isinstance(guid, dict)
                                   and isinstance(guid.get('id'), str)
                                   and guid['id'].startswith('tmdb://')
                                   for guid in row.get('Guid', []))]
            candidates = []
            for row in tagless:
                paths = part_paths(row)
                if len(paths) == len(set(paths)) and set(paths) == expected:
                    candidates.append(row)
            if len(candidates) > 1:
                raise ValueError('compatibility Plex tagless card is ambiguous')
            if candidates:
                self.refresh_item(candidates[0]['ratingKey'])
                return False
        # An empty file list does not prove a deleted show is gone: Plex can
        # retain a show card with no Parts until its scan finishes.
        if not expected and not matches:
            return True
        if expected and len(observed) == len(set(observed)) and set(observed) == expected:
            return True
        target = f'{self.plex_root}/{self.identity_provider}-{show_id}'
        self.post_refresh(
            f'/library/sections/{self.section_id}/refresh'
            f'?path={quote(target, safe="/")}')
        return False


class VerifiedMovieRefresh(VerifiedShowRefresh):
    """Acknowledge only an exact TMDB movie card with every owned Part."""

    def __call__(self, tmdb_id: int) -> bool:
        if type(tmdb_id) is not int or tmdb_id <= 0:
            raise ValueError('exact TMDB movie required for scan proof')
        self.require_section()
        relative = self.store.ready_links(tmdb_id)
        folders = {str(PurePosixPath(link).parent) for link in relative}
        if relative and len(folders) != 1:
            raise ValueError('movie compatibility film spans multiple folders')
        expected = {f'{self.plex_root}/{link}' for link in relative}
        movies = self._rows(
            f'/library/sections/{self.section_id}/all?type=1'
            '&includeGuids=1&skipRefresh=1', maximum=5000)
        matches = [row for row in movies
                   if row.get('type') == 'movie'
                   and any(isinstance(guid, dict)
                           and guid.get('id') == f'tmdb://{tmdb_id}'
                           for guid in row.get('Guid', []))]
        if len(matches) > 1:
            raise ValueError('compatibility Plex film identity is ambiguous')
        observed = []
        if matches:
            key = matches[0].get('ratingKey')
            if not isinstance(key, str) or not key.isdecimal():
                raise ValueError('compatibility Plex film key is invalid')
            reply = self.plex.request_json(
                'GET', f'/library/metadata/{key}?includeGuids=1&skipRefresh=1')
            body = reply.get('MediaContainer') if isinstance(reply, dict) else None
            rows = body.get('Metadata') if isinstance(body, dict) else None
            if not isinstance(rows, list) or len(rows) != 1:
                raise ValueError('compatibility Plex film detail is incomplete')
            movie = rows[0]
            if (movie.get('type') != 'movie'
                    or str(movie.get('ratingKey')) != key
                    or movie.get('librarySectionID', self.section_id)
                    != self.section_id
                    or not any(isinstance(guid, dict)
                               and guid.get('id') == f'tmdb://{tmdb_id}'
                               for guid in movie.get('Guid', []))):
                raise ValueError('compatibility Plex film detail changed')
            for media in movie.get('Media', []):
                if not isinstance(media, dict):
                    raise ValueError('compatibility Plex film Media is invalid')
                for part in media.get('Part', []):
                    path = part.get('file') if isinstance(part, dict) else None
                    if not isinstance(path, str):
                        raise ValueError('compatibility Plex film Part is invalid')
                    observed.append(path)
        if not relative:
            if not matches:
                return True
            if not observed:
                raise ValueError('movie removal card has no provable Part path')
            removed_folders = set()
            for path in observed:
                if not path.startswith(self.plex_root + '/'):
                    raise ValueError('movie removal Part is outside owned view')
                remainder = path[len(self.plex_root) + 1:]
                pieces = PurePosixPath(remainder).parts
                if (len(pieces) != 2
                        or any(piece in ('', '.', '..') for piece in pieces)):
                    raise ValueError('movie removal Part has unsafe folder')
                removed_folders.add(pieces[0])
            if len(removed_folders) != 1:
                raise ValueError('movie removal spans multiple folders')
            target = f'{self.plex_root}/{next(iter(removed_folders))}'
            self.post_refresh(
                f'/library/sections/{self.section_id}/refresh'
                f'?path={quote(target, safe="/")}')
            return False
        if len(observed) == len(set(observed)) and set(observed) == expected:
            return True
        target = f'{self.plex_root}/{next(iter(folders))}'
        self.post_refresh(
            f'/library/sections/{self.section_id}/refresh'
            f'?path={quote(target, safe="/")}')
        return False
