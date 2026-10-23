"""Private, read-only Shoko/Plex evidence for the Seerr duplicate guard.

The Plex card source is injected. It must read stock Plex episode details and
return the matching cards without making any media or catalogue changes.
This module has no listener and is not connected to production Nagisa.
"""

from __future__ import annotations

import re
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from nagisa.seerr_guard import AniDBCandidate, AnimeEpisode, GuardDecision, decide_request
from nagisa.anime_lists import (MappingHold, anchor_seerr_tvdb_ids,
                                resolve_anchored_request, tvdb_anime_ids,
                                verify_sonarr_tvdb_anchor)


_FILE_ID = re.compile(r"(?:-dup[1-9][0-9]*)? \[([1-9][0-9]*)\]\.[^/.]+$")
_EPISODE_GUID = re.compile(r'tv\.plex\.agents\.custom\.shoko://episode/e([1-9][0-9]*)(?:p([1-9][0-9]*))?')
_SEGMENT = re.compile(r'-pt([1-9][0-9]*)\.[^/.]+$')


def _id(value: Any) -> bool:
    return type(value) is int and value > 0


def _rows(value: Any) -> list[dict]:
    if (not isinstance(value, dict) or type(value.get('Total')) is not int
            or not isinstance(value.get('List'), list)
            or value['Total'] != len(value['List']) or value['Total'] > 10000
            or not all(isinstance(row, dict) for row in value['List'])):
        raise ValueError('incomplete native Shoko response')
    return value['List']


class ExactEpisodeCrosswalk:
    """Resolve a TVDB episode through provider IDs, never display metadata."""

    def __init__(self, tvdb: Any, tmdb: Any) -> None:
        self.tvdb = tvdb
        self.tmdb = tmdb

    @staticmethod
    def _result(value: Any, show_id: int) -> int | None:
        matches = value.get('tv_episode_results') if isinstance(value, dict) else None
        if not isinstance(matches, list) or len(matches) > 1:
            raise ValueError('TMDB episode lookup is incomplete or ambiguous')
        if not matches:
            return None
        row = matches[0]
        if (not isinstance(row, dict) or not _id(row.get('id'))
                or row.get('show_id') != show_id
                or type(row.get('season_number')) is not int
                or row['season_number'] < 0
                or type(row.get('episode_number')) is not int
                or row['episode_number'] < 0):
            raise ValueError('TMDB episode lookup points outside the exact show')
        return row['id']

    def resolve(self, tvdb_episode_id: int, tmdb_show_id: int) -> int:
        if not _id(tvdb_episode_id) or not _id(tmdb_show_id):
            raise ValueError('invalid provider episode lookup')
        direct = self.tmdb.request_json(
            'GET', f'/3/find/{tvdb_episode_id}?external_source=tvdb_id')
        episode_id = self._result(direct, tmdb_show_id)
        if episode_id is not None:
            return episode_id
        if self.tvdb is None:
            raise ValueError('TMDB has no exact episode and TVDB lookup is unconfigured')
        remote = self.tvdb.request_json(
            'GET', f'/v4/episodes/{tvdb_episode_id}/extended')
        data = remote.get('data') if isinstance(remote, dict) else None
        if (not isinstance(data, dict) or data.get('id') != tvdb_episode_id
                or not isinstance(data.get('remoteIds'), list)
                or len(data['remoteIds']) > 50):
            raise ValueError('TVDB episode remote IDs are unavailable')
        imdb_ids = [row.get('id') for row in data['remoteIds']
                    if isinstance(row, dict)
                    and isinstance(row.get('sourceName'), str)
                    and row['sourceName'].casefold() == 'imdb']
        if (len(imdb_ids) != 1 or not isinstance(imdb_ids[0], str)
                or re.fullmatch(r'tt[1-9][0-9]{3,11}', imdb_ids[0]) is None):
            raise ValueError('TVDB episode lacks one exact IMDb episode ID')
        by_imdb = self.tmdb.request_json(
            'GET', f'/3/find/{imdb_ids[0]}?external_source=imdb_id')
        episode_id = self._result(by_imdb, tmdb_show_id)
        if episode_id is None:
            raise ValueError('IMDb episode ID has no exact TMDB episode')
        return episode_id


class TvdbEpisodeTransport:
    """Authenticate with a separately configured TVDB key for exact reads."""

    def __init__(self, api_key: str, *, pin: str | None = None,
                 timeout: float = 10, transport_factory: Any = None) -> None:
        from nagisa.http import JsonTransport

        if (not isinstance(api_key, str) or len(api_key) < 8
                or any(char in api_key for char in '\r\n\x00')
                or (pin is not None and (not isinstance(pin, str)
                                        or not pin or any(char in pin for char in '\r\n\x00')))):
            raise ValueError('TVDB credentials are invalid')
        self._factory = transport_factory or JsonTransport
        self._api_key = api_key
        self._pin = pin
        self._timeout = timeout
        self._client = None

    def _authorize(self) -> None:
        login = self._factory('https://api4.thetvdb.com', timeout=self._timeout,
                              allowed_methods=frozenset({'POST'}))
        body = {'apikey': self._api_key}
        if self._pin is not None:
            body['pin'] = self._pin
        response = login.request_json('POST', '/v4/login', body)
        data = response.get('data') if isinstance(response, dict) else None
        token = data.get('token') if isinstance(data, dict) else None
        if (not isinstance(token, str) or len(token) < 8
                or any(char in token for char in '\r\n\x00')):
            raise ValueError('TVDB authentication did not return a token')
        self._client = self._factory(
            'https://api4.thetvdb.com', timeout=self._timeout,
            headers={'Authorization': 'Bearer ' + token},
            allowed_methods=frozenset({'GET'}))

    def request_json(self, method: str, path: str) -> Any:
        from nagisa.http import TransportError

        if (method != 'GET' or not isinstance(path, str)
                or re.fullmatch(r'/v4/episodes/[1-9][0-9]*/extended', path) is None):
            raise ValueError('TVDB exact episode read required')
        if self._client is None:
            self._authorize()
        try:
            return self._client.request_json('GET', path)
        except TransportError as error:
            if str(error) != 'request returned HTTP 401':
                raise
            self._authorize()
            return self._client.request_json('GET', path)


class NativePlexCards:
    """Adapt the existing stock Plex owner reader to exact episode cards."""

    def __init__(self, owner_reader: Any, *, access_probe: Any,
                 section_ids: tuple[int, ...]) -> None:
        if not callable(access_probe):
            raise ValueError('positive Plex part access probe required')
        if (not isinstance(section_ids, tuple) or len(section_ids) < 2
                or len(set(section_ids)) != len(section_ids)
                or any(not _id(section) for section in section_ids)):
            raise ValueError('explicit mirrored Plex sections required')
        self.owner_reader = owner_reader
        self.access_probe = access_probe
        self.section_ids = section_ids
        self._cache: dict[int, dict] = {}

    def begin_review(self) -> None:
        # Owner pages are consistent only within one request. Never reuse a
        # previous request's availability result after a new import or removal.
        self._cache.clear()

    @classmethod
    def from_transport(cls, plex_transport: Any, section_ids: tuple[int, ...],
                       *, access_probe: Any):
        from nagisa.reconciler.arrival_readers import StockPlexOwnerReader
        return cls(StockPlexOwnerReader(plex_transport, section_ids),
                   access_probe=access_probe, section_ids=section_ids)

    def cards_for_episode(self, episode_id: int, series_id: int) -> tuple[dict, ...]:
        if not _id(episode_id) or not _id(series_id):
            raise ValueError('invalid Plex episode or series identity')
        if series_id not in self._cache:
            self._cache[series_id] = self.owner_reader(series_id)
        sections = self._cache[series_id]
        if not isinstance(sections, dict) or set(sections) != set(self.section_ids):
            raise ValueError('invalid Plex owner sections')
        cards = []
        present_sections = set()
        playable_sections = set()
        section_guids = []
        for section in self.section_ids:
            rows = sections[section]
            if not isinstance(rows, list):
                raise ValueError('invalid Plex owner cards')
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError('invalid Plex episode card')
                guid = row.get('guid')
                match = _EPISODE_GUID.fullmatch(guid) if isinstance(guid, str) else None
                if match is None or int(match.group(1)) != episode_id:
                    continue
                present_sections.add(section)
                section_guids.append((section, guid))
                parts = []
                for media in row.get('Media', ()):  # already validated by StockPlexOwnerReader
                    for part in media['Part']:
                        accessible = self.access_probe(part['file'])
                        if type(accessible) is not bool:
                            raise ValueError('Plex part probe did not return a boolean')
                        parts.append({**part, 'accessible': accessible})
                if any(part['accessible'] for part in parts):
                    playable_sections.add(section)
                cards.append({'guid': guid, 'parts': tuple(parts)})
        if present_sections and (present_sections != set(self.section_ids)
                                 or playable_sections != set(self.section_ids)):
            raise ValueError('Plex episode is not playable in every Anime section')
        if present_sections and len({guid for _, guid in section_guids}) != 1:
            raise ValueError('Plex episode GUID differs across Anime sections')
        return tuple(cards)


class ReadOnlyPlexPartProbe:
    """Read one byte through an explicit read-only mount of Plex's VFS root."""

    def __init__(self, plex_root: str, mounted_root: str | Path) -> None:
        if (not isinstance(plex_root, str) or not plex_root.startswith('/')
                or not plex_root.endswith('/') or '..' in plex_root or '\\' in plex_root):
            raise ValueError('absolute Plex root required')
        self.plex_root = plex_root
        self.mounted_root = Path(mounted_root)
        if not self.mounted_root.is_absolute():
            raise ValueError('absolute read-only mount root required')

    def __call__(self, plex_path: str) -> bool:
        if (not isinstance(plex_path, str) or not plex_path.startswith(self.plex_root)
                or '\\' in plex_path or '\x00' in plex_path):
            return False
        relative = plex_path[len(self.plex_root):]
        segments = relative.split('/')
        if not relative or any(segment in ('', '.', '..') for segment in segments):
            return False
        candidate = self.mounted_root.joinpath(*segments)
        try:
            with candidate.open('rb') as stream:
                return bool(stream.read(1))
        except OSError:
            return False


class StockSonarrTvdbCatalogue:
    """Read only Sonarr's locally stored TVDB episode IDs for one exact show."""

    def __init__(self, transport: Any) -> None:
        self.transport = transport

    def episodes_for_tvdb_show(self, tvdb_show_id: int) -> list[dict]:
        if not _id(tvdb_show_id):
            raise ValueError('invalid TVDB show ID')
        series = self.transport.request_json('GET', '/api/v3/series')
        if (not isinstance(series, list) or len(series) > 10000
                or not all(isinstance(row, dict) for row in series)):
            raise ValueError('invalid Sonarr series catalogue')
        matches = [row for row in series if row.get('tvdbId') == tvdb_show_id]
        if not matches:
            raise NoSonarrSeries('Sonarr has not added this TVDB series')
        if len(matches) != 1 or not _id(matches[0].get('id')):
            raise ValueError('Sonarr lacks one exact TVDB series')
        episodes = self.transport.request_json(
            'GET', f"/api/v3/episode?seriesId={matches[0]['id']}&includeImages=false")
        if (not isinstance(episodes, list) or len(episodes) > 10000
                or not all(isinstance(row, dict) for row in episodes)):
            raise ValueError('invalid Sonarr episode catalogue')
        return episodes


class NoSonarrSeries(ValueError):
    """Sonarr has no local episode catalogue for an as-yet-unadded show."""


def verify_native_tvdb_anchor(
    anchored_ids: tuple[tuple[int, int, int], ...], provider: list[dict]
) -> tuple[tuple[int, int, int], ...]:
    """Use exact Shoko TVDB IDs when Sonarr has no series yet.

    This only establishes the episode-ID namespace. The pinned published
    rules and Shoko file/Plex Part joins still decide episode ownership.
    """
    requested = [row[0] for row in anchored_ids]
    if (not requested or len(requested) != len(set(requested))
            or not isinstance(provider, list) or len(provider) > 10000):
        raise ValueError('invalid native TVDB anchor')
    native: dict[int, list[dict]] = defaultdict(list)
    tmdb: dict[int, list[dict]] = defaultdict(list)
    for row in provider:
        if not isinstance(row, dict):
            raise ValueError('invalid native episode row')
        if _id(row.get('TvdbEpisodeID')):
            native[row['TvdbEpisodeID']].append(row)
        if _id(row.get('ID')):
            tmdb[row['ID']].append(row)
    for episode_id in requested:
        matches = native.get(episode_id, ())
        if (len(matches) != 1 or not _id(matches[0].get('ID'))
                or any(row is not matches[0] for row in tmdb.get(episode_id, ()))):
            raise ValueError('Seerr ID lacks one non-conflicting native TVDB match')
    return anchored_ids


class NativeEvidenceReader:
    """Join exact stock IDs; uncertainty is never interpreted as missing."""

    def __init__(self, shoko: Any, plex_cards: Any, seerr: Any,
                 *, plex_roots: tuple[str, ...] = ('/sonarr-anime/NativeLibrary/',),
                 unknown_policy: str = 'hold', exact_crosswalk: Any = None) -> None:
        if not plex_roots or any(not root.startswith('/') or not root.endswith('/')
                                 or '..' in root for root in plex_roots):
            raise ValueError('explicit Plex roots required')
        self.shoko = shoko
        self.plex_cards = plex_cards
        self.seerr = seerr
        self.exact_crosswalk = exact_crosswalk
        self.plex_roots = plex_roots
        self._review_lock = threading.Lock()
        if unknown_policy not in ('hold', 'stock_pass_through'):
            raise ValueError('unsupported unknown-show policy')
        self.unknown_policy = unknown_policy

    def _get(self, path: str) -> Any:
        return self.shoko.request_json('GET', path)

    def _verified_plex_files(self, episode_id: int, series_id: int,
                             shoko_file_ids: set[int]) -> tuple[int, ...]:
        cards = self.plex_cards.cards_for_episode(episode_id, series_id)
        if not isinstance(cards, (list, tuple)) or len(cards) > 20:
            raise ValueError('invalid Plex episode-card response')
        seen: set[int] = set()
        for card in cards:
            guid = card.get('guid') if isinstance(card, dict) else None
            match_guid = _EPISODE_GUID.fullmatch(guid) if isinstance(guid, str) else None
            if match_guid is None or int(match_guid.group(1)) != episode_id:
                raise ValueError('Plex episode-card identity differs')
            parts = card.get('parts')
            if not isinstance(parts, (list, tuple)) or len(parts) > 100:
                raise ValueError('invalid Plex parts')
            verified_in_card = set()
            segmented = False
            for part in parts:
                if not isinstance(part, dict) or not isinstance(part.get('file'), str):
                    raise ValueError('invalid Plex part')
                path = part['file']
                if (not any(path.startswith(root) for root in self.plex_roots)
                        or '\\' in path or '\x00' in path or '/..' in path):
                    raise ValueError('Plex part outside native library')
                match = _FILE_ID.search(path)
                if match is None:
                    segment = _SEGMENT.search(path)
                    if (match_guid.group(2) is None or segment is None
                            or segment.group(1) != match_guid.group(2)):
                        raise ValueError('Plex part has no native Shoko file ID')
                    segmented = True
                    if part.get('accessible') is not True:
                        raise ValueError('Plex segment accessibility is unverified')
                    continue
                file_id = int(match.group(1))
                if file_id not in shoko_file_ids:
                    raise ValueError('Plex part is not linked to this Shoko episode')
                # Stock Plex metadata proves a Part is indexed, not that its
                # target can still be read. A separate positive access check
                # must annotate the Part before it can certify ownership.
                if part.get('accessible') is not True:
                    raise ValueError('Plex part accessibility is unverified')
                seen.add(file_id)
                verified_in_card.add(file_id)
            if segmented and not verified_in_card:
                raise ValueError('segmented Plex card lacks an exact playable alternate')
        return tuple(sorted(seen))

    def _review(self, tvdb_show_id: int, seasons: tuple[int, ...]):
        if hasattr(self.plex_cards, 'begin_review'):
            self.plex_cards.begin_review()
        if (not _id(tvdb_show_id) or not isinstance(seasons, tuple) or not seasons
                or len(set(seasons)) != len(seasons)
                or not all(type(season) is int and season >= 0 for season in seasons)):
            raise ValueError('invalid Seerr show or season identity')
        shows = _rows(self._get('/api/v3/Tmdb/Show?page=1&pageSize=0'))
        matches = [row for row in shows if row.get('TvdbID') == tvdb_show_id]
        if not matches and self.unknown_policy == 'stock_pass_through':
            # An absent TMDB-show link does not prove absence of older AniDB
            # media. Report this as an explicit stock-routing risk, never as
            # an exact negative ownership decision.
            return 'stock_unverified_show', None
        if len(matches) != 1 or not _id(matches[0].get('ID')):
            raise ValueError('TVDB show has no unique Shoko TMDB identity')
        show_id = matches[0]['ID']
        if self.exact_crosswalk is not None:
            metadata_settings = self.seerr.request_json(
                'GET', '/api/v1/settings/metadatas')
            if (not isinstance(metadata_settings, dict)
                    or metadata_settings.get('anime') != 'tvdb'):
                raise ValueError('Seerr anime episode provider is no longer TVDB')
        seerr_show = self.seerr.request_json('GET', f'/api/v1/tv/{show_id}')
        if (not isinstance(seerr_show, dict) or seerr_show.get('id') != show_id
                or not isinstance(seerr_show.get('seasons'), list)):
            raise ValueError('Seerr TV metadata is incomplete')
        expected_coordinates: dict[tuple[int, int], int] = {}
        seen_seerr_ids: set[int] = set()
        for season in seasons:
            matches = [row for row in seerr_show['seasons']
                       if isinstance(row, dict) and row.get('seasonNumber') == season]
            if (len(matches) != 1 or type(matches[0].get('episodeCount')) is not int
                    or not 1 <= matches[0]['episodeCount'] <= 1000):
                raise ValueError('Seerr season count is missing or ambiguous')
            detail = self.seerr.request_json('GET', f'/api/v1/tv/{show_id}/season/{season}')
            # Pinned Seerr 3.5 currently fills the season response's
            # top-level seasonNumber with its episode count. Its individual
            # episode rows retain the correct seasonNumber, so verify those
            # against the independent TV-show season count instead.
            if (not isinstance(detail, dict)
                    or not isinstance(detail.get('episodes'), list)
                    or len(detail['episodes']) != matches[0]['episodeCount']):
                raise ValueError('Seerr episode census is incomplete')
            numbers = set()
            for episode in detail['episodes']:
                if (not isinstance(episode, dict) or not _id(episode.get('id'))
                        or type(episode.get('episodeNumber')) is not int
                        or episode['episodeNumber'] < 0
                        or episode.get('seasonNumber') != season
                        or episode['id'] in seen_seerr_ids
                        or episode['episodeNumber'] in numbers):
                    raise ValueError('Seerr episode census has ambiguous identity')
                seen_seerr_ids.add(episode['id'])
                numbers.add(episode['episodeNumber'])
                expected_coordinates[(season, episode['episodeNumber'])] = episode['id']
        base = f'/api/v3/Tmdb/Show/{show_id}'
        provider = _rows(self._get(base + '/Episode?page=1&pageSize=0'))
        # Seerr may number an anime by TVDB seasons while Shoko keeps TMDB
        # seasons. Join by an exact provider episode ID, never by a guessed
        # season offset, title or ordinal. One namespace must cover the whole
        # request and each Seerr episode must identify a unique Shoko row.
        lookups: dict[str, dict[int, list[dict]]] = {
            'tmdb': defaultdict(list), 'tvdb': defaultdict(list),
        }
        for row in provider:
            if _id(row.get('ID')):
                lookups['tmdb'][row['ID']].append(row)
            if _id(row.get('TvdbEpisodeID')):
                lookups['tvdb'][row['TvdbEpisodeID']].append(row)
        complete: dict[str, list[dict]] = {}
        resolved_tvdb_by_tmdb: dict[int, int] = {}
        for namespace, lookup in lookups.items():
            rows = []
            for seerr_id in expected_coordinates.values():
                matches = lookup.get(seerr_id, ())
                if len(matches) != 1:
                    break
                rows.append(matches[0])
            else:
                if len({row['ID'] for row in rows}) == len(rows):
                    complete[namespace] = rows
        if 'tvdb' not in complete and self.exact_crosswalk is not None:
            # Only an exact external identifier may close a missing TVDB-ID
            # join. Bound the *external lookups*, not the size of an existing
            # series: a long-running show can be short by just one ID.
            # Never align a TVDB season with TMDB by title or ordinal.
            missing_ids = [seerr_id for seerr_id in expected_coordinates.values()
                           if not lookups['tvdb'].get(seerr_id)]
            if len(expected_coordinates) > 2000 or len(missing_ids) > 50:
                raise ValueError('external episode lookup exceeds review bound')
            rows = []
            for seerr_id in expected_coordinates.values():
                direct = lookups['tvdb'].get(seerr_id, ())
                if len(direct) > 1:
                    raise ValueError('duplicate native TVDB episode identity')
                if direct:
                    row = direct[0]
                else:
                    tmdb_id = self.exact_crosswalk.resolve(seerr_id, show_id)
                    matches = lookups['tmdb'].get(tmdb_id, ())
                    if len(matches) != 1:
                        raise ValueError('external episode ID is absent from Shoko show')
                    row = matches[0]
                    native_tvdb = row.get('TvdbEpisodeID')
                    if native_tvdb not in (None, 0, seerr_id):
                        raise ValueError('external episode ID conflicts with Shoko')
                numeric_tmdb = lookups['tmdb'].get(seerr_id, ())
                if (numeric_tmdb and (len(numeric_tmdb) != 1
                                      or numeric_tmdb[0]['ID'] != row['ID'])):
                    raise ValueError('TVDB and TMDB numeric episode IDs conflict')
                rows.append(row)
                resolved_tvdb_by_tmdb[row['ID']] = seerr_id
            if len({row['ID'] for row in rows}) != len(rows):
                raise ValueError('external episode IDs converge on one Shoko row')
            complete['tvdb'] = rows
        if not complete:
            raise ValueError('Seerr episodes lack unique Shoko provider IDs')
        if (len(complete) == 2
                and [row['ID'] for row in complete['tmdb']]
                    != [row['ID'] for row in complete['tvdb']]):
            raise ValueError('conflicting Seerr episode ID namespaces')
        requested = complete.get('tvdb', complete.get('tmdb'))
        season_by_tmdb = {
            row['ID']: coordinate[0]
            for coordinate, row in zip(expected_coordinates, requested)
        }
        provider_ids = set()
        for row in requested:
            if not _id(row.get('ID')) or row['ID'] in provider_ids:
                raise ValueError('invalid or duplicate TMDB episode identity')
            provider_ids.add(row['ID'])
        xrefs = _rows(self._get(base + '/Episode/CrossReferences?page=1&pageSize=0'))
        by_tmdb: dict[int, list[dict]] = defaultdict(list)
        for row in xrefs:
            if row.get('TmdbShowID') == show_id and row.get('TmdbEpisodeID') in provider_ids:
                if not _id(row.get('AnidbEpisodeID')) or not _id(row.get('AnidbAnimeID')):
                    raise ValueError('invalid AniDB cross-reference')
                by_tmdb[row['TmdbEpisodeID']].append(row)
        series = self._get(base + '/Shoko/Series?page=1&pageSize=0')
        if not isinstance(series, list) or len(series) > 100 or not all(
                isinstance(row, dict) for row in series):
            raise ValueError('invalid Shoko series association')
        anime_to_series: dict[int, int] = {}
        for row in series:
            ids = row.get('IDs')
            if not isinstance(ids, dict) or not _id(ids.get('ID')) or not _id(ids.get('AniDB')):
                raise ValueError('invalid Shoko series identity')
            if ids['AniDB'] in anime_to_series:
                raise ValueError('duplicate AniDB series identity')
            anime_to_series[ids['AniDB']] = ids['ID']
        shoko_episodes: dict[tuple[int, int], int] = {}
        for anime_id, series_id in anime_to_series.items():
            # Shoko defaults to imported episodes. Include fileless episodes
            # so a valid AniDB link can be checked before a missing search.
            rows = _rows(self._get(
                f'/api/v3/Series/{series_id}/Episode?page=1&pageSize=0'
                '&includeMissing=true&includeUnaired=true&includeHidden=true&includeFiles=false'))
            for row in rows:
                ids = row.get('IDs')
                if not isinstance(ids, dict) or not _id(ids.get('ID')) or not _id(ids.get('AniDB')):
                    raise ValueError('invalid Shoko episode identity')
                key = (anime_id, ids['AniDB'])
                if key in shoko_episodes:
                    raise ValueError('duplicate Shoko episode identity')
                shoko_episodes[key] = ids['ID']
        evidence = []
        for row in requested:
            candidates = []
            for xref in by_tmdb[row['ID']]:
                shoko_id = shoko_episodes.get((xref['AnidbAnimeID'], xref['AnidbEpisodeID']))
                if shoko_id is None:
                    raise ValueError('AniDB episode absent from Shoko series')
                files = _rows(self._get(f'/api/v3/Episode/{shoko_id}/File?page=1&pageSize=0'))
                ids = set()
                for file in files:
                    file_id = file.get('ID')
                    if not _id(file_id) or file_id in ids:
                        raise ValueError('invalid or duplicate Shoko file identity')
                    ids.add(file_id)
                    locations = file.get('Locations')
                    if (not isinstance(locations, list) or not locations
                            or not any(isinstance(loc, dict) and loc.get('IsAccessible') is True
                                       for loc in locations)):
                        raise ValueError('Shoko file is not accessible')
                # Query Plex even when Shoko reports no file. An orphaned Plex
                # card is conflicting ownership evidence, not permission to
                # download the episode again.
                plex_ids = self._verified_plex_files(
                    shoko_id, anime_to_series[xref['AnidbAnimeID']], ids)
                candidates.append(AniDBCandidate(xref['AnidbEpisodeID'],
                                                 tuple(sorted(ids)), plex_ids))
            tvdb_episode_id = resolved_tvdb_by_tmdb.get(row['ID'], row.get('TvdbEpisodeID'))
            if tvdb_episode_id in (0, None):
                tvdb_episode_id = None
            evidence.append(AnimeEpisode(row['ID'], tvdb_episode_id,
                                         tuple(candidates), season_by_tmdb[row['ID']]))
        return 'guard_existing', decide_request(evidence)

    def review(self, tvdb_show_id: int, seasons: tuple[int, ...]):
        try:
            # NativePlexCards has a per-review cache. Do not let an arrival
            # request and the availability worker clear one another's cache.
            with self._review_lock:
                return self._review(tvdb_show_id, seasons)
        except (KeyError, TypeError, ValueError, RuntimeError, OSError):
            return 'hold_for_review', GuardDecision('hold_for_review', reason='native_evidence_incomplete')


class PublishedEvidenceReader(NativeEvidenceReader):
    """Private, fail-closed Anime-Lists fallback for missing provider IDs.

    The snapshot is injected and pinned by the caller. No live route uses this
    reader; it never writes Seerr, Shoko, Plex, Sonarr, or media.
    """

    def __init__(self, shoko: Any, plex_cards: Any, seerr: Any,
                 snapshot: bytes, *, sonarr_catalogue: Any,
                 negative_owner_proof: Any = None,
                 **kwargs: Any) -> None:
        super().__init__(shoko, plex_cards, seerr, **kwargs)
        if (not isinstance(snapshot, bytes)
                or not callable(getattr(sonarr_catalogue,
                                        'episodes_for_tvdb_show', None))):
            raise ValueError('pinned snapshot and independent Sonarr catalogue required')
        self.snapshot = snapshot
        self.sonarr_catalogue = sonarr_catalogue
        # No operational proof source is wired yet. A fileless first-time
        # episode must remain held until an independent, complete owner census
        # can exclude unmapped Shoko files for this exact provider episode.
        self.negative_owner_proof = negative_owner_proof

    def review_registered(self, tvdb_show_id: int, tmdb_show_id: int,
                          seasons: tuple[int, ...], sonarr_episodes: list[dict]):
        """Protect only positively owned episodes of a paused, unlinked show.

        An absent file is unknown until the separate negative-owner census is
        complete. This path therefore never authorizes a first-time search.
        """
        try:
            with self._review_lock:
                return self._review_registered(
                    tvdb_show_id, tmdb_show_id, seasons, sonarr_episodes)
        except (MappingHold, KeyError, TypeError, ValueError, RuntimeError, OSError):
            return 'hold_for_review', GuardDecision(
                'hold_for_review', reason='registered_ownership_incomplete')

    def _review_registered(self, tvdb_show_id: int, tmdb_show_id: int,
                           seasons: tuple[int, ...], sonarr_episodes: list[dict]):
        if hasattr(self.plex_cards, 'begin_review'):
            self.plex_cards.begin_review()
        if (not _id(tvdb_show_id) or not _id(tmdb_show_id)
                or not isinstance(seasons, tuple) or not seasons
                or len(seasons) > 100 or len(set(seasons)) != len(seasons)
                or any(type(number) is not int or number < 0 for number in seasons)
                or not isinstance(sonarr_episodes, list)
                or len(sonarr_episodes) > 10000):
            raise ValueError('invalid registered request scope')
        settings = self.seerr.request_json('GET', '/api/v1/settings/metadatas')
        show = self.seerr.request_json('GET', f'/api/v1/tv/{tmdb_show_id}')
        details = {season: self.seerr.request_json(
            'GET', f'/api/v1/tv/{tmdb_show_id}/season/{season}')
            for season in seasons}
        anchored = anchor_seerr_tvdb_ids(
            settings, show, details, tmdb_show_id, seasons)
        anchored = verify_sonarr_tvdb_anchor(anchored, sonarr_episodes)

        metadata: dict[int, dict[tuple[int, int], int]] = {}
        known: list[tuple[int, int, int]] = []
        for anime_id in tvdb_anime_ids(self.snapshot, tvdb_show_id):
            rows = _rows(self._get(
                f'/api/v3/Series/AniDB/{anime_id}/Episode?page=1&pageSize=0'))
            by_coordinate: dict[tuple[int, int], int] = {}
            seen_ids: set[int] = set()
            for row in rows:
                kind = {'Episode': 1, 'Special': 0}.get(row.get('Type'))
                number, episode_id = row.get('EpisodeNumber'), row.get('ID')
                if (row.get('AnimeID') != anime_id
                        or type(number) is not int or number <= 0
                        or not _id(episode_id) or episode_id in seen_ids
                        or (kind is not None and (kind, number) in by_coordinate)):
                    raise ValueError('ambiguous AniDB episode catalogue')
                seen_ids.add(episode_id)
                if kind is None:
                    if row.get('Type') not in ('Credits', 'Trailer'):
                        raise ValueError('unknown AniDB episode type')
                    continue
                by_coordinate[(kind, number)] = episode_id
                known.append((anime_id, kind, number))
            metadata[anime_id] = by_coordinate
        resolved = resolve_anchored_request(
            self.snapshot, tvdb_show_id, anchored, known)

        series_by_anime: dict[int, int] = {}
        for anime_id in {row[1] for row in resolved}:
            series = self._get(f'/api/v3/Series/AniDB/{anime_id}/Series')
            ids = series.get('IDs') if isinstance(series, dict) else None
            if (not isinstance(ids, dict) or ids.get('AniDB') != anime_id
                    or not _id(ids.get('ID'))):
                raise ValueError('AniDB contender has no exact Shoko series')
            series_by_anime[anime_id] = ids['ID']
        if len(set(series_by_anime.values())) != len(series_by_anime):
            raise ValueError('AniDB contenders share a Shoko series')

        shoko_episodes: dict[tuple[int, int], int] = {}
        for anime_id, series_id in series_by_anime.items():
            rows = _rows(self._get(
                f'/api/v3/Series/{series_id}/Episode?page=1&pageSize=0'
                '&includeMissing=true&includeUnaired=true&includeHidden=true&includeFiles=false'))
            for row in rows:
                ids = row.get('IDs')
                if (not isinstance(ids, dict) or not _id(ids.get('ID'))
                        or not _id(ids.get('AniDB'))
                        or (anime_id, ids['AniDB']) in shoko_episodes):
                    raise ValueError('ambiguous Shoko episode identity')
                shoko_episodes[(anime_id, ids['AniDB'])] = ids['ID']

        present: list[int] = []
        missing: list[int] = []
        coordinates = {episode_id: season for episode_id, season, _ in anchored}
        missing_seasons: set[int] = set()
        for tvdb_episode_id, anime_id, kind, number in resolved:
            anidb_episode_id = metadata[anime_id][(kind, number)]
            shoko_id = shoko_episodes.get((anime_id, anidb_episode_id))
            if shoko_id is None:
                raise ValueError('mapped AniDB episode is absent from Shoko')
            files = _rows(self._get(
                f'/api/v3/Episode/{shoko_id}/File?page=1&pageSize=0'))
            file_ids: set[int] = set()
            for file in files:
                file_id, locations = file.get('ID'), file.get('Locations')
                if (not _id(file_id) or file_id in file_ids
                        or not isinstance(locations, list) or not locations
                        or not any(isinstance(loc, dict)
                                   and loc.get('IsAccessible') is True
                                   for loc in locations)):
                    raise ValueError('Shoko file is not uniquely accessible')
                file_ids.add(file_id)
            plex_ids = self._verified_plex_files(
                shoko_id, series_by_anime[anime_id], file_ids)
            if file_ids and plex_ids:
                present.append(tvdb_episode_id)
            elif not file_ids and not plex_ids:
                proof = getattr(self.negative_owner_proof,
                                'prove_absent', None)
                if (not callable(proof) or proof(
                        tvdb_show_id, tmdb_show_id, tvdb_episode_id,
                        anime_id, anidb_episode_id) is not True):
                    raise ValueError('unmapped owners not excluded for exact episode')
                missing.append(tvdb_episode_id)
                missing_seasons.add(coordinates[tvdb_episode_id])
            else:
                raise ValueError('Shoko and Plex ownership disagree')
        if missing:
            decision = GuardDecision(
                'search_missing', tuple(missing),
                present_tvdb_ids=tuple(present),
                missing_seasons=tuple(sorted(missing_seasons)))
        else:
            decision = GuardDecision(
                'already_present', present_tvdb_ids=tuple(present))
        return 'guard_existing', decision

    def _review(self, tvdb_show_id: int, seasons: tuple[int, ...]):
        if hasattr(self.plex_cards, 'begin_review'):
            self.plex_cards.begin_review()
        if (not _id(tvdb_show_id) or not isinstance(seasons, tuple)
                or not seasons or len(seasons) > 100
                or len(set(seasons)) != len(seasons)
                or any(type(number) is not int or number < 0 for number in seasons)):
            raise ValueError('invalid requested TVDB show or seasons')
        shows = _rows(self._get('/api/v3/Tmdb/Show?page=1&pageSize=0'))
        matched = [row for row in shows if row.get('TvdbID') == tvdb_show_id]
        if not matched:
            # The boundary registers this exact stock Sonarr show paused,
            # then calls review_registered. This is not an absence finding.
            return 'stock_unverified_show', None
        if len(matched) != 1 or not _id(matched[0].get('ID')):
            raise ValueError('no unique Seerr TMDB show in Shoko')
        show_id = matched[0]['ID']
        settings = self.seerr.request_json('GET', '/api/v1/settings/metadatas')
        show = self.seerr.request_json('GET', f'/api/v1/tv/{show_id}')
        details = {season: self.seerr.request_json(
            'GET', f'/api/v1/tv/{show_id}/season/{season}') for season in seasons}
        anchored = anchor_seerr_tvdb_ids(settings, show, details, show_id, seasons)
        provider = _rows(self._get(
            f'/api/v3/Tmdb/Show/{show_id}/Episode?page=1&pageSize=0'))
        try:
            sonarr_episodes = self.sonarr_catalogue.episodes_for_tvdb_show(tvdb_show_id)
        except NoSonarrSeries:
            anchored = verify_native_tvdb_anchor(anchored, provider)
        else:
            anchored = verify_sonarr_tvdb_anchor(anchored, sonarr_episodes)

        # Read every published contender, not just the currently imported
        # Shoko series. Otherwise a missing competing entry could create a
        # false unique match.
        anidb_metadata: dict[int, dict[tuple[int, int], int]] = {}
        known: list[tuple[int, int, int]] = []
        for anime_id in tvdb_anime_ids(self.snapshot, tvdb_show_id):
            rows = _rows(self._get(
                f'/api/v3/Series/AniDB/{anime_id}/Episode?page=1&pageSize=0'))
            by_coordinate: dict[tuple[int, int], int] = {}
            seen_ids: set[int] = set()
            for row in rows:
                kind = {'Episode': 1, 'Special': 0}.get(row.get('Type'))
                number, episode_id = row.get('EpisodeNumber'), row.get('ID')
                if (row.get('AnimeID') != anime_id
                        or type(number) is not int or number <= 0
                        or not _id(episode_id) or episode_id in seen_ids
                        or (kind is not None and (kind, number) in by_coordinate)):
                    raise ValueError('ambiguous AniDB episode catalogue')
                seen_ids.add(episode_id)
                if kind is None:
                    # Shoko lists release extras in the same catalogue. They
                    # cannot satisfy a regular/Special TVDB episode mapping.
                    if row.get('Type') not in ('Credits', 'Trailer'):
                        raise ValueError('ambiguous AniDB episode catalogue')
                    continue
                by_coordinate[(kind, number)] = episode_id
                known.append((anime_id, kind, number))
            anidb_metadata[anime_id] = by_coordinate
        resolved = resolve_anchored_request(
            self.snapshot, tvdb_show_id, anchored, known)

        # A published mapping cannot overrule a contradictory exact identity
        # already present in Shoko's stock TMDB/TVDB cross-reference.
        resolved_by_tvdb = {
            tvdb_id: (anime_id, anidb_metadata[anime_id][(kind, number)])
            for tvdb_id, anime_id, kind, number in resolved
        }
        xrefs = _rows(self._get(
            f'/api/v3/Tmdb/Show/{show_id}/Episode/CrossReferences?page=1&pageSize=0'))
        refs_by_tmdb: dict[int, set[tuple[int, int]]] = defaultdict(set)
        for row in xrefs:
            if row.get('TmdbShowID') == show_id and _id(row.get('TmdbEpisodeID')):
                if not _id(row.get('AnidbAnimeID')) or not _id(row.get('AnidbEpisodeID')):
                    raise ValueError('invalid native AniDB cross-reference')
                refs_by_tmdb[row['TmdbEpisodeID']].add(
                    (row['AnidbAnimeID'], row['AnidbEpisodeID']))
        seen_native_tvdb: set[int] = set()
        for row in provider:
            tvdb_id = row.get('TvdbEpisodeID')
            if tvdb_id not in resolved_by_tvdb:
                continue
            if (not _id(row.get('ID')) or tvdb_id in seen_native_tvdb
                    or refs_by_tmdb.get(row['ID']) != {resolved_by_tvdb[tvdb_id]}):
                raise ValueError('published and native episode identities conflict')
            seen_native_tvdb.add(tvdb_id)

        series = self._get(
            f'/api/v3/Tmdb/Show/{show_id}/Shoko/Series?page=1&pageSize=0')
        if (not isinstance(series, list) or len(series) > 100
                or not all(isinstance(row, dict) for row in series)):
            raise ValueError('invalid Shoko series association')
        anime_to_series: dict[int, int] = {}
        for row in series:
            ids = row.get('IDs')
            if (not isinstance(ids, dict) or not _id(ids.get('ID'))
                    or not _id(ids.get('AniDB'))
                    or ids['AniDB'] in anime_to_series):
                raise ValueError('duplicate or invalid Shoko series')
            anime_to_series[ids['AniDB']] = ids['ID']

        needed = {anime_id for _, anime_id, _, _ in resolved}
        shoko_by_anidb: dict[tuple[int, int], int] = {}
        for anime_id in needed:
            series_id = anime_to_series.get(anime_id)
            if series_id is None:
                raise ValueError('mapped AniDB series absent from Shoko show')
            rows = _rows(self._get(
                f'/api/v3/Series/{series_id}/Episode?page=1&pageSize=0'
                '&includeMissing=true&includeUnaired=true&includeHidden=true&includeFiles=false'))
            for row in rows:
                ids = row.get('IDs')
                if (not isinstance(ids, dict) or not _id(ids.get('ID'))
                        or not _id(ids.get('AniDB'))
                        or (anime_id, ids['AniDB']) in shoko_by_anidb):
                    raise ValueError('ambiguous Shoko AniDB episode identity')
                shoko_by_anidb[(anime_id, ids['AniDB'])] = ids['ID']

        present: list[int] = []
        missing: list[int] = []
        missing_seasons: set[int] = set()
        coordinates = {episode_id: season for episode_id, season, _ in anchored}
        for tvdb_episode_id, anime_id, kind, number in resolved:
            anidb_episode_id = anidb_metadata[anime_id][(kind, number)]
            shoko_id = shoko_by_anidb.get((anime_id, anidb_episode_id))
            if shoko_id is None:
                raise ValueError('AniDB episode has no Shoko identity')
            files = _rows(self._get(
                f'/api/v3/Episode/{shoko_id}/File?page=1&pageSize=0'))
            file_ids: set[int] = set()
            for file in files:
                file_id = file.get('ID')
                locations = file.get('Locations')
                if (not _id(file_id) or file_id in file_ids
                        or not isinstance(locations, list) or not locations
                        or not any(isinstance(loc, dict)
                                   and loc.get('IsAccessible') is True
                                   for loc in locations)):
                    raise ValueError('Shoko file is not uniquely accessible')
                file_ids.add(file_id)
            plex_ids = self._verified_plex_files(
                shoko_id, anime_to_series[anime_id], file_ids)
            if file_ids and plex_ids:
                present.append(tvdb_episode_id)
            elif not file_ids and not plex_ids:
                proof = getattr(self.negative_owner_proof,
                                'prove_absent', None)
                if (not callable(proof) or proof(
                        tvdb_show_id, show_id, tvdb_episode_id,
                        anime_id, anidb_episode_id) is not True):
                    raise ValueError('unmapped owners not excluded for exact episode')
                missing.append(tvdb_episode_id)
                missing_seasons.add(coordinates[tvdb_episode_id])
            else:
                raise ValueError('Shoko and Plex ownership disagree')
        if missing:
            decision = GuardDecision('search_missing', tuple(missing),
                                     present_tvdb_ids=tuple(present),
                                     missing_seasons=tuple(sorted(missing_seasons)))
        else:
            decision = GuardDecision('already_present',
                                     present_tvdb_ids=tuple(present))
        return 'guard_existing', decision

    def review(self, tvdb_show_id: int, seasons: tuple[int, ...]):
        try:
            with self._review_lock:
                return self._review(tvdb_show_id, seasons)
        except (MappingHold, KeyError, TypeError, ValueError, RuntimeError, OSError):
            return 'hold_for_review', GuardDecision(
                'hold_for_review', reason='published_evidence_incomplete')


class GuardEvidenceFallback:
    """Prefer stock exact IDs; consult published rules only on a native hold."""

    def __init__(self, native: Any, published: Any) -> None:
        self.native = native
        self.published = published

    def review(self, tvdb_show_id: int, seasons: tuple[int, ...]):
        classification, decision = self.native.review(tvdb_show_id, seasons)
        if (classification != 'hold_for_review' or not isinstance(decision, GuardDecision)
                or decision.action != 'hold_for_review'):
            return classification, decision
        return self.published.review(tvdb_show_id, seasons)

    def review_registered(self, tvdb_show_id: int, tmdb_show_id: int,
                          seasons: tuple[int, ...], sonarr_episodes: list[dict]):
        return self.published.review_registered(
            tvdb_show_id, tmdb_show_id, seasons, sonarr_episodes)
