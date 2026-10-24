"""Private exact-evidence reader for a stock Seerr compatibility library.

This module does not create links or call a Seerr writer. Its only output is
an immutable proof for a separate link store to consider.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
import os
from pathlib import Path
import re
import time
from typing import Any, Callable


_PART_FILE_ID = re.compile(
    r'(?:-dup[1-9][0-9]*)? \[([1-9][0-9]*)\](?:\[variation\])?\.[^/.]+$')


class NotRegularTVFile(ValueError):
    """A positively identified movie or Special excluded from the TV view."""

    def __init__(self, message: str, *, kind: str | None = None):
        super().__init__(message)
        self.kind = kind


def _id(value: Any) -> bool:
    return type(value) is int and value > 0


@dataclass(frozen=True)
class LinkProof:
    shoko_file_id: int
    shoko_episode_id: int
    shoko_series_id: int
    anidb_episode_id: int
    tmdb_episode_id: int
    tvdb_episode_id: int | None
    tmdb_show_id: int
    tvdb_show_id: int | None
    season: int
    episode: int
    source_path: Path
    plex_target: str
    file_size: int


class SeerrEpisodeResolver:
    """Resolve exact TVDB episode IDs to Seerr coordinates, not title/order."""

    def __init__(self, seerr: Any, *, cache_seconds: int = 0) -> None:
        if (not callable(getattr(seerr, 'request_json', None))
                or type(cache_seconds) is not int
                or not 0 <= cache_seconds <= 3600):
            raise ValueError('invalid Seerr episode resolver')
        self.seerr = seerr
        self.cache_seconds = cache_seconds
        self._cache: OrderedDict[int, tuple[float, tuple, dict]] = OrderedDict()

    def resolve(self, tmdb_show_id: int, tvdb_show_id: int,
                tvdb_episode_id: int) -> tuple[int, int]:
        if not all(_id(value) for value in
                   (tmdb_show_id, tvdb_show_id, tvdb_episode_id)):
            raise ValueError('exact Seerr provider identities required')
        settings = self.seerr.request_json('GET', '/api/v1/settings/metadatas')
        if not isinstance(settings, dict) or settings.get('anime') != 'tvdb':
            raise ValueError('Seerr anime episode provider changed')
        show = self.seerr.request_json('GET', f'/api/v1/tv/{tmdb_show_id}')
        external = show.get('externalIds') if isinstance(show, dict) else None
        seasons = show.get('seasons') if isinstance(show, dict) else None
        if (not isinstance(show, dict) or show.get('id') != tmdb_show_id
                or not isinstance(external, dict)
                or external.get('tvdbId') != tvdb_show_id
                or not isinstance(seasons, list)
                or len(seasons) > 100):
            raise ValueError('Seerr show provider identity differs')
        summary = []
        for row in seasons:
            if (not isinstance(row, dict)
                    or type(row.get('seasonNumber')) is not int
                    or not 0 <= row['seasonNumber'] <= 999
                    or type(row.get('episodeCount')) is not int
                    or not 0 <= row['episodeCount'] <= 1000):
                raise ValueError('Seerr season census is malformed')
            summary.append((row['seasonNumber'], row['episodeCount']))
        if len(set(number for number, _ in summary)) != len(summary):
            raise ValueError('Seerr season census is ambiguous')
        signature = tuple(sorted(summary))
        current = self._cache.get(tmdb_show_id)
        if (current is not None and time.monotonic() < current[0]
                and signature == current[1]):
            self._cache.move_to_end(tmdb_show_id)
            index = current[2]
        else:
            index: dict[int, tuple[int, int]] = {}
            coordinates = set()
            for season, count in signature:
                if not count:
                    continue
                detail = self.seerr.request_json(
                    'GET', f'/api/v1/tv/{tmdb_show_id}/season/{season}')
                episodes = detail.get('episodes') if isinstance(detail, dict) else None
                if not isinstance(episodes, list) or len(episodes) != count:
                    raise ValueError('Seerr season episode census is incomplete')
                for row in episodes:
                    if (not isinstance(row, dict) or not _id(row.get('id'))
                            or row.get('seasonNumber') != season
                            or type(row.get('episodeNumber')) is not int
                            or not 1 <= row['episodeNumber'] <= 9999
                            or row['id'] in index
                            or (season > 0 and
                                (season, row['episodeNumber']) in coordinates)):
                        raise ValueError('Seerr episode identity is ambiguous')
                    coordinate = season, row['episodeNumber']
                    index[row['id']] = coordinate
                    if season > 0:
                        coordinates.add(coordinate)
            if len(index) > 10000:
                raise ValueError('Seerr show episode census exceeds bound')
            self._cache[tmdb_show_id] = (
                time.monotonic() + self.cache_seconds, signature, index)
            self._cache.move_to_end(tmdb_show_id)
            if len(self._cache) > 128:
                self._cache.popitem(last=False)
        coordinate = index.get(tvdb_episode_id)
        if coordinate is None:
            raise ValueError('exact TVDB episode absent from Seerr')
        if coordinate[0] == 0:
            raise ValueError('regular TV episode cannot use Seerr special coordinate')
        return coordinate

    def require_coordinate(self, tmdb_show_id: int, tvdb_show_id: int,
                           season: int, episode: int) -> None:
        """Check Seerr's season slot without treating its episode ID as TVDB."""
        if (not _id(tmdb_show_id) or not _id(tvdb_show_id)
                or type(season) is not int or season < 0
                or not _id(episode)):
            raise ValueError('invalid published episode coordinate')
        settings = self.seerr.request_json('GET', '/api/v1/settings/metadatas')
        show = self.seerr.request_json('GET', f'/api/v1/tv/{tmdb_show_id}')
        external = show.get('externalIds') if isinstance(show, dict) else None
        seasons = show.get('seasons') if isinstance(show, dict) else None
        if (not isinstance(settings, dict) or settings.get('anime') != 'tvdb'
                or not isinstance(show, dict) or show.get('id') != tmdb_show_id
                or not isinstance(external, dict)
                or external.get('tvdbId') != tvdb_show_id
                or not isinstance(seasons, list) or len(seasons) > 100):
            raise ValueError('published show conflicts with Seerr identity')
        summaries = [row for row in seasons if isinstance(row, dict)
                     and row.get('seasonNumber') == season]
        if (len(summaries) != 1
                or type(summaries[0].get('episodeCount')) is not int
                or not 1 <= summaries[0]['episodeCount'] <= 1000):
            raise ValueError('published season absent from Seerr')
        count = summaries[0]['episodeCount']
        detail = self.seerr.request_json(
            'GET', f'/api/v1/tv/{tmdb_show_id}/season/{season}')
        rows = detail.get('episodes') if isinstance(detail, dict) else None
        if not isinstance(rows, list) or len(rows) != count:
            raise ValueError('published season incomplete in Seerr')
        numbers = [row.get('episodeNumber') for row in rows
                   if isinstance(row, dict) and row.get('seasonNumber') == season
                   and _id(row.get('id'))]
        if (len(numbers) != count or len(set(numbers)) != count
                or any(not _id(number) or number > 9999 for number in numbers)):
            raise ValueError('published season ambiguous in Seerr')
        if episode not in numbers:
            raise ValueError('published coordinate absent from Seerr')


class LinkProofReader:
    """Require one Shoko file and cards in every selected public Plex section."""

    def __init__(self, shoko: Any, owner_reader: Callable[[int], Any],
                 plex: Any, seerr: Any, *, sections: tuple[int, ...],
                 allowed_folders: dict[int, tuple[Path, str]],
                 part_probe: Callable[[str], bool],
                 ignored_source_folders: dict[int, Path] | None = None,
                 seerr_cache_seconds: int = 0,
                 published_resolver: Callable[..., tuple[int, int]] | None = None,
                 coordinate_mode: str = 'tvdb') -> None:
        if (not callable(getattr(shoko, 'request_json', None))
                or not callable(owner_reader)
                or not callable(getattr(plex, 'request_json', None))
                or not callable(getattr(seerr, 'request_json', None))
                or not isinstance(sections, tuple)
                or not 1 <= len(sections) <= 2
                or len(set(sections)) != len(sections)
                or not all(map(_id, sections))
                or not allowed_folders or not callable(part_probe)
                or coordinate_mode not in ('tvdb', 'tmdb')
                or (coordinate_mode == 'tmdb' and published_resolver is not None)
                or (published_resolver is not None
                    and not callable(published_resolver))):
            raise ValueError('invalid compatibility-link reader configuration')
        configured: dict[int, tuple[Path, str]] = {}
        for folder_id, (source_root, plex_alias) in allowed_folders.items():
            root = Path(source_root)
            if (not _id(folder_id) or not root.is_absolute()
                    or not isinstance(plex_alias, str)
                    or not plex_alias.startswith('/')
                    or '\\' in plex_alias or '\x00' in plex_alias
                    or any(part in ('', '.', '..') for part in plex_alias[1:].rstrip('/').split('/'))):
                raise ValueError('unsafe compatibility-link source mount')
            configured[folder_id] = root, plex_alias.rstrip('/')
        ignored: dict[int, Path] = {}
        if ignored_source_folders is not None:
            if not isinstance(ignored_source_folders, dict):
                raise ValueError('invalid ignored Shoko source-folder config')
            for folder_id, source_root in ignored_source_folders.items():
                root = Path(source_root)
                if (not _id(folder_id) or folder_id in configured
                        or not root.is_absolute()):
                    raise ValueError('unsafe ignored Shoko source folder')
                ignored[folder_id] = root
        self.shoko = shoko
        self.owner_reader = owner_reader
        self.plex = plex
        self.seerr = seerr
        self.seerr_episodes = SeerrEpisodeResolver(
            seerr, cache_seconds=seerr_cache_seconds)
        self.sections = sections
        self.allowed_folders = configured
        self.ignored_source_folders = ignored
        self.part_probe = part_probe
        self.published_resolver = published_resolver
        self.coordinate_mode = coordinate_mode

    def _location(self, file_id: int, record: dict) -> tuple[tuple[Path, str], ...]:
        locations = record.get('Locations')
        if not isinstance(locations, list) or len(locations) > 20:
            raise ValueError('Shoko file locations are incomplete')
        accessible = [row for row in locations if isinstance(row, dict)
                      and row.get('IsAccessible') is True]
        destinations = []
        for row in accessible:
            folder_id = row.get('ManagedFolderID')
            if (row.get('FileID') != file_id or not _id(folder_id)
                    or folder_id not in (self.allowed_folders
                                         | self.ignored_source_folders)):
                raise ValueError('Shoko file location is outside configured roots')
            is_source = folder_id in self.ignored_source_folders
            root = (self.ignored_source_folders[folder_id] if is_source
                    else self.allowed_folders[folder_id][0])
            folder = self.shoko.request_json(
                'GET', f'/api/v3/ManagedFolder/{folder_id}')
            if (not isinstance(folder, dict) or folder.get('ID') != folder_id
                    or (folder.get('DropFolderType') == 'Source') != is_source
                    or not isinstance(folder.get('Path'), str)
                    or Path(folder['Path']).resolve() != root.resolve()):
                raise ValueError('Shoko managed-folder root changed')
            relative = row.get('RelativePath')
            if (not isinstance(relative, str) or not relative.startswith('/')
                    or '\\' in relative or '\x00' in relative):
                raise ValueError('invalid Shoko relative path')
            segments = relative[1:].split('/')
            if not segments or any(part in ('', '.', '..') for part in segments):
                raise ValueError('Shoko relative path escapes managed root')
            source = root.joinpath(*segments)
            if (not source.is_file()
                    or not source.resolve().is_relative_to(root.resolve())
                    or source.stat().st_size != record['Size']):
                raise ValueError('Shoko source is missing, changed or outside root')
            if not is_source:
                destinations.append((source,
                                     self.allowed_folders[folder_id][1]
                                     + '/' + '/'.join(segments)))
        if not destinations or len(set(destinations)) != len(destinations):
            raise ValueError('Shoko file lacks distinct managed destinations')
        for source, _ in destinations:
            with source.open('rb') as stream:
                if not stream.read(1):
                    raise ValueError('Shoko source cannot be read')
        return tuple(destinations)

    def _show_ids(self, key: Any, section: int, series_id: int) -> tuple[int, int | None]:
        if not str(key).isdigit() or int(key) <= 0:
            raise ValueError('Plex show key is invalid')
        response = self.plex.request_json(
            'GET', f'/library/metadata/{key}?includeGuids=1&skipRefresh=1')
        body = response.get('MediaContainer') if isinstance(response, dict) else None
        rows = body.get('Metadata') if isinstance(body, dict) else None
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError('Plex show details are incomplete')
        show = rows[0]
        if (not isinstance(show, dict) or str(show.get('ratingKey')) != str(key)
                or show.get('librarySectionID') != section
                or show.get('type') != 'show'
                or show.get('guid') != f'tv.plex.agents.custom.shoko://show/{series_id}'
                or show.get('deletedAt')):
            raise ValueError('Plex show identity changed')
        guids = show.get('Guid')
        if not isinstance(guids, list) or len(guids) > 20:
            raise ValueError('Plex external show IDs are missing')
        values = [row.get('id') for row in guids if isinstance(row, dict)]
        def one(prefix: str, *, required: bool = True) -> int | None:
            matches = [value[len(prefix):] for value in values
                       if isinstance(value, str) and value.startswith(prefix)]
            if not matches and not required:
                return None
            if len(matches) != 1 or not matches[0].isdigit() or int(matches[0]) <= 0:
                raise ValueError('Plex external show ID is ambiguous')
            return int(matches[0])
        return one('tmdb://'), one('tvdb://', required=self.coordinate_mode == 'tvdb')

    def read(self, file_id: int) -> LinkProof:
        if not _id(file_id):
            raise ValueError('exact positive Shoko file ID required')
        record = self.shoko.request_json('GET', f'/api/v3/File/{file_id}')
        if (not isinstance(record, dict) or record.get('ID') != file_id
                or record.get('IsIgnored') is not False
                or not _id(record.get('Size'))):
            raise ValueError('Shoko file is not an identified playable file')
        rows = self.shoko.request_json(
            'GET', f'/api/v3/File/{file_id}/Episode?includeDataFrom=AniDB')
        if isinstance(rows, list) and 1 < len(rows) <= 20:
            movie_ids = []
            for entry in rows:
                ids = entry.get('IDs') if isinstance(entry, dict) else None
                tmdb = ids.get('TMDB') if isinstance(ids, dict) else None
                movies = tmdb.get('Movie') if isinstance(tmdb, dict) else None
                if not isinstance(movies, list) or len(movies) != 1 \
                        or not _id(movies[0]):
                    break
                movie_ids.append(movies[0])
            if len(movie_ids) == len(rows) and len(set(movie_ids)) == 1:
                # One full movie file may cover several Shoko episode rows.
                # Let the movie reader verify every association and Part.
                raise NotRegularTVFile(
                    'multi-episode movie is outside TV view', kind='Movie')
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError('Shoko file episode association is ambiguous')
        row = rows[0]
        ids = row.get('IDs')
        anidb = row.get('AniDB')
        tmdb = ids.get('TMDB') if isinstance(ids, dict) else None
        if (isinstance(anidb, dict) and isinstance(tmdb, dict)
                and (anidb.get('Type') in ('Special', 'Movie')
                     or (isinstance(tmdb.get('Movie'), list)
                         and len(tmdb['Movie']) == 1
                         and _id(tmdb['Movie'][0])))):
            raise NotRegularTVFile(
                'identified movie or Special is outside TV view',
                kind='Movie' if anidb.get('Type') == 'Movie' else 'Special')
        if (not isinstance(ids, dict) or not isinstance(anidb, dict)
                or not isinstance(tmdb, dict) or anidb.get('Type') != 'Episode'
                or not all(_id(ids.get(k)) for k in ('ID', 'ParentSeries', 'AniDB'))
                or ids['AniDB'] != anidb.get('ID')
                or not isinstance(tmdb.get('Show'), list)
                or not isinstance(tmdb.get('Episode'), list)
                or len(tmdb['Show']) != 1 or len(tmdb['Episode']) != 1
                or not _id(tmdb['Show'][0]) or not _id(tmdb['Episode'][0])
                or tmdb.get('Movie') != []):
            raise ValueError('Shoko episode lacks one regular provider identity')
        destinations = self._location(file_id, record)
        owner = self.owner_reader(ids['ParentSeries'])
        if not isinstance(owner, dict) or set(owner) != set(self.sections):
            raise ValueError('configured public Plex sections are required')
        coordinates: set[tuple[int, int]] = set()
        show_ids: set[tuple[int, int | None]] = set()
        selected_destinations: set[tuple[Path, str]] = set()
        for section in self.sections:
            cards = owner[section]
            if not isinstance(cards, list) or len(cards) > 10000:
                raise ValueError('Plex owner cards are incomplete')
            matched = []
            for card in cards:
                if not isinstance(card, dict):
                    raise ValueError('Plex card is malformed')
                parts = [part for media in card.get('Media', ())
                         if isinstance(media, dict)
                         for part in media.get('Part', ()) if isinstance(part, dict)]
                relevant = [part for part in parts
                            if isinstance(part.get('file'), str)
                            and (match := _PART_FILE_ID.search(part['file']))
                            and int(match.group(1)) == file_id]
                if relevant:
                    matched.append((card, relevant))
            if len(matched) != 1:
                raise ValueError('Plex file belongs to zero or multiple cards')
            card, parts = matched[0]
            if (card.get('guid') !=
                    f"tv.plex.agents.custom.shoko://episode/e{ids['ID']}"
                    or type(card.get('parentIndex')) is not int
                    or type(card.get('index')) is not int
                    or card['parentIndex'] < 0 or card['index'] < 1
                    or len(parts) != 1
                    or int(parts[0].get('size', 0)) != record['Size']
                    or self.part_probe(parts[0]['file']) is not True):
                raise ValueError('Plex episode or file evidence differs')
            matching_destinations = []
            for source, alias in destinations:
                try:
                    same_physical_file = os.path.samefile(parts[0]['file'], source)
                except OSError:
                    same_physical_file = False
                if same_physical_file:
                    matching_destinations.append((source, alias))
            if not matching_destinations:
                raise ValueError('Plex Part and Shoko destination are not the same physical file')
            if len(matching_destinations) != 1:
                raise ValueError('Plex Part matches multiple Shoko physical destinations')
            selected_destinations.add(matching_destinations[0])
            coordinates.add((card['parentIndex'], card['index']))
            show_ids.add(self._show_ids(card.get('grandparentRatingKey'),
                                        section, ids['ParentSeries']))
        if len(selected_destinations) != 1:
            raise ValueError('mirrored Plex physical destinations disagree')
        if len(coordinates) != 1 or len(show_ids) != 1:
            raise ValueError('mirrored Plex identity or coordinates disagree')
        source, target = next(iter(selected_destinations))
        tmdb_show_id, tvdb_show_id = next(iter(show_ids))
        if tmdb_show_id != tmdb['Show'][0]:
            raise ValueError('Plex show differs from Shoko episode provider ID')
        # The selected public cards must agree with each other, but their Shoko
        # season need not equal Seerr's TVDB season for the same episode ID.
        # Public Relay cards and Shoko derive from the same association. Check
        # Seerr's independent episode namespace before creating a TV-season
        # link in its availability source.
        provider = self.shoko.request_json(
            'GET', f'/api/v3/Tmdb/Show/{tmdb_show_id}/Episode?page=1&pageSize=0')
        provider_rows = provider.get('List') if isinstance(provider, dict) else None
        if not isinstance(provider_rows, list) or len(provider_rows) > 10000:
            raise ValueError('Shoko provider episode crosswalk is incomplete')
        if (self.coordinate_mode == 'tmdb'
                and (type(provider.get('Total')) is not int
                     or provider['Total'] != len(provider_rows))):
            raise ValueError('Shoko TMDB episode census is incomplete')
        matches = [item for item in provider_rows if isinstance(item, dict)
                   and item.get('ID') == tmdb['Episode'][0]]
        if len(matches) > 1:
            raise ValueError('TMDB episode crosswalk is ambiguous')
        if self.coordinate_mode == 'tmdb':
            if len(matches) != 1:
                raise ValueError('exact TMDB episode is absent from Shoko')
            native = matches[0]
            season = native.get('SeasonNumber')
            episode = native.get('EpisodeNumber')
            if (type(season) is not int or not 1 <= season <= 999
                    or type(episode) is not int or not 1 <= episode <= 9999
                    or native.get('IsHidden') is not False
                    or sum(1 for item in provider_rows
                           if isinstance(item, dict)
                           and item.get('SeasonNumber') == season
                           and item.get('EpisodeNumber') == episode
                           and item.get('IsHidden') is False) != 1):
                raise ValueError('TMDB episode coordinate is hidden or ambiguous')
            tvdb_episode_id = native.get('TvdbEpisodeID')
            if tvdb_episode_id is not None and not _id(tvdb_episode_id):
                raise ValueError('TMDB episode has malformed TVDB cross-reference')
            return LinkProof(file_id, ids['ID'], ids['ParentSeries'],
                             ids['AniDB'], tmdb['Episode'][0],
                             tvdb_episode_id, tmdb_show_id, tvdb_show_id,
                             season, episode, source, target, record['Size'])
        tvdb_episode_id = (matches[0].get('TvdbEpisodeID')
                           if matches else None)
        published = None
        if self.published_resolver is not None:
            result = self.published_resolver(
                tmdb_show_id=tmdb_show_id, tvdb_show_id=tvdb_show_id,
                shoko_series_id=ids['ParentSeries'],
                anidb_episode_id=ids['AniDB'])
            if (result is not None and
                    (not isinstance(result, tuple) or len(result) != 2
                     or type(result[0]) is not int or result[0] < 0
                     or not _id(result[1]))):
                raise ValueError('published episode result is invalid')
            published = result
        if _id(tvdb_episode_id):
            season, episode = self.seerr_episodes.resolve(
                tmdb_show_id, tvdb_show_id, tvdb_episode_id)
            if published is not None and published != (season, episode):
                raise ValueError('published episode differs from Shoko crosswalk')
        else:
            if published is None:
                raise ValueError('TMDB episode has no unique TVDB episode identity')
            season, episode = published
            self.seerr_episodes.require_coordinate(
                tmdb_show_id, tvdb_show_id, season, episode)
        return LinkProof(file_id, ids['ID'], ids['ParentSeries'], ids['AniDB'],
                         tmdb['Episode'][0], tvdb_episode_id,
                         tmdb_show_id, tvdb_show_id,
                         season, episode, source, target, record['Size'])

    def confirm_native_mapping_changed(self, file_id: int,
                                       owned: dict) -> bool:
        """Confirm a changed owned TMDB claim from Shoko alone, never a timeout."""
        if self.coordinate_mode != 'tmdb' or not _id(file_id):
            return False
        if (not isinstance(owned, dict) or owned.get('file_id') != file_id
                or not all(_id(owned.get(key)) for key in
                           ('tmdb_show_id', 'tmdb_episode_id', 'series_id',
                            'anidb_episode_id'))
                or not isinstance(owned.get('slot'), str)):
            return False
        try:
            record = self.shoko.request_json('GET', f'/api/v3/File/{file_id}')
            if not isinstance(record, dict) or record.get('ID') != file_id:
                return False
            if record.get('IsIgnored') is True:
                return True
            if record.get('IsIgnored') is not False:
                return False
            rows = self.shoko.request_json(
                'GET', f'/api/v3/File/{file_id}/Episode?includeDataFrom=AniDB')
            if (not isinstance(rows, list) or len(rows) != 1
                    or not isinstance(rows[0], dict)):
                return False
            ids = rows[0].get('IDs')
            tmdb = ids.get('TMDB') if isinstance(ids, dict) else None
            if (not isinstance(tmdb, dict)
                    or not isinstance(tmdb.get('Show'), list)
                    or not isinstance(tmdb.get('Episode'), list)
                    or len(tmdb['Show']) != 1 or len(tmdb['Episode']) != 1
                    or not _id(tmdb['Show'][0])
                    or not _id(tmdb['Episode'][0])):
                return False
            if (ids.get('ParentSeries') != owned['series_id']
                    or ids.get('AniDB') != owned['anidb_episode_id']
                    or tmdb['Show'][0] != owned['tmdb_show_id']
                    or tmdb['Episode'][0] != owned['tmdb_episode_id']):
                return True
            provider = self.shoko.request_json(
                'GET', f"/api/v3/Tmdb/Show/{owned['tmdb_show_id']}"
                       '/Episode?page=1&pageSize=0')
            listed = provider.get('List') if isinstance(provider, dict) else None
            if (not isinstance(listed, list) or len(listed) > 10000
                    or type(provider.get('Total')) is not int
                    or provider['Total'] != len(listed)):
                return False
            matches = [item for item in listed if isinstance(item, dict)
                       and item.get('ID') == owned['tmdb_episode_id']]
            if len(matches) != 1:
                return False
            row = matches[0]
            if row.get('IsHidden') is True:
                return True
            if (row.get('IsHidden') is not False
                    or type(row.get('SeasonNumber')) is not int
                    or type(row.get('EpisodeNumber')) is not int):
                return False
            return owned['slot'] != (
                f"{owned['tmdb_show_id']}/{row['SeasonNumber']}/"
                f"{row['EpisodeNumber']}")
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def alternate_file_ids(self, series_id: int, anidb_episode_id: int, *,
                           exclude_file_id: int) -> tuple[int, ...]:
        """Find versions of the exact episode after one source is removed."""
        if (not _id(series_id) or not _id(anidb_episode_id)
                or not _id(exclude_file_id)):
            raise ValueError('exact alternate episode identities required')
        reply = self.shoko.request_json(
            'GET', f'/api/v3/Series/{series_id}/Episode?'
                   'page=1&pageSize=0&includeFiles=true')
        rows = reply.get('List') if isinstance(reply, dict) else None
        if (not isinstance(rows, list) or len(rows) > 10000
                or reply.get('Total') != len(rows)):
            raise ValueError('Shoko episode file catalogue is incomplete')
        matches = []
        for row in rows:
            ids = row.get('IDs') if isinstance(row, dict) else None
            if (not isinstance(ids, dict) or ids.get('ParentSeries') != series_id
                    or not _id(ids.get('ID')) or not _id(ids.get('AniDB'))):
                raise ValueError('Shoko episode file identity is ambiguous')
            if ids['AniDB'] == anidb_episode_id:
                matches.append(row)
        if not matches:
            return ()
        if len(matches) != 1:
            raise ValueError('Shoko alternate episode is ambiguous')
        files = matches[0].get('Files')
        if (not isinstance(files, list) or len(files) > 50
                or any(not isinstance(item, dict) or not _id(item.get('ID'))
                       for item in files)):
            raise ValueError('Shoko alternate file list is ambiguous')
        ids = [item['ID'] for item in files]
        if len(set(ids)) != len(ids):
            raise ValueError('Shoko alternate file list repeats a file')
        return tuple(sorted(file_id for file_id in ids
                            if file_id != exclude_file_id))

    def confirm_removed(self, file_id: int, series_id: int) -> bool:
        """Require a Shoko 404 and absence in every configured public section."""
        if not _id(file_id) or not _id(series_id):
            raise ValueError('exact removal identities required')
        try:
            self.shoko.request_json('GET', f'/api/v3/File/{file_id}')
        except Exception as exc:
            code = getattr(exc, 'code', None)
            response = getattr(exc, 'response', None)
            if code != 404 and getattr(response, 'status_code', None) != 404:
                raise
        else:
            return False
        owner = self.owner_reader(series_id)
        if not isinstance(owner, dict) or set(owner) != set(self.sections):
            raise ValueError('public Plex removal census is incomplete')
        for section in self.sections:
            cards = owner[section]
            if not isinstance(cards, list) or len(cards) > 10000:
                raise ValueError('public Plex removal census is incomplete')
            for card in cards:
                if not isinstance(card, dict):
                    raise ValueError('public Plex card is malformed')
                for media in card.get('Media', ()):
                    if not isinstance(media, dict):
                        raise ValueError('public Plex media is malformed')
                    for part in media.get('Part', ()):
                        if not isinstance(part, dict):
                            raise ValueError('public Plex part is malformed')
                        path = part.get('file')
                        if isinstance(path, str):
                            match = _PART_FILE_ID.search(path)
                            if match and int(match.group(1)) == file_id:
                                return False
        return True
