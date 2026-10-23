"""Conservative, keyless Anime-Lists coordinate join for private Nagisa review.

This resolves coordinates only. It cannot establish file coverage or Plex
availability and is deliberately not connected to the live request route.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree


class MappingHold(ValueError):
    """The published relationship does not prove one episode identity."""


_PAIR = re.compile(r";([1-9][0-9]*)-([0-9]+)")


def _number(raw: str | None, *, allow_zero: bool = True) -> int:
    if raw is None or not raw.isascii() or not raw.isdecimal():
        raise MappingHold('unsupported mapping number')
    value = int(raw)
    if value == 0 and not allow_zero:
        raise MappingHold('zero episode number')
    return value


def _explicit_targets(entry: ElementTree.Element, source_season: int,
                      source_number: int) -> list[tuple[int, int]]:
    targets: list[tuple[int, int]] = []
    for mapping in entry.findall('./mapping-list/mapping'):
        if mapping.get('tvdbseason') is None:
            continue
        if _number(mapping.get('anidbseason')) != source_season:
            continue
        text = (mapping.text or '').strip()
        if not text:
            continue
        # A multipart or unknown token cannot be flattened to one episode.
        if '+' in text or not re.fullmatch(r'(;[1-9][0-9]*-[0-9]+)+;', text):
            raise MappingHold('unsupported explicit mapping syntax')
        season = _number(mapping.get('tvdbseason'))
        for match in _PAIR.finditer(text):
            if int(match.group(1)) == source_number:
                targets.append((season, int(match.group(2))))
    return targets


def _range_targets(entry: ElementTree.Element, source_season: int,
                   source_number: int) -> list[tuple[int, int]]:
    targets = []
    for mapping in entry.findall('./mapping-list/mapping'):
        if mapping.get('tvdbseason') is None:
            continue
        if _number(mapping.get('anidbseason')) != source_season:
            continue
        start, end = mapping.get('start'), mapping.get('end')
        if start is None and end is None:
            continue
        lower = _number(start, allow_zero=False)
        upper = _number(end, allow_zero=False)
        if lower > upper:
            raise MappingHold('inverted range')
        if lower <= source_number <= upper:
            offset = mapping.get('offset', '0')
            if not re.fullmatch(r'-?[0-9]+', offset):
                raise MappingHold('unsupported range offset')
            target_number = source_number + int(offset)
            if target_number <= 0:
                raise MappingHold('invalid range target')
            targets.append((_number(mapping.get('tvdbseason')),
                            target_number))
    return targets


def _forward_targets(entry: ElementTree.Element, source_season: int,
                     source_number: int) -> tuple[tuple[int, int], ...]:
    explicit = _explicit_targets(entry, source_season, source_number)
    if explicit:
        positives = {target for target in explicit if target[1] > 0}
        if len(positives) > 1:
            raise MappingHold('one AniDB episode has multiple TVDB targets')
        return tuple(positives)
    ranges = set(_range_targets(entry, source_season, source_number))
    if len(ranges) > 1:
        raise MappingHold('overlapping range mappings')
    if ranges:
        return tuple(ranges)
    # Anime-Lists does not promise that unlisted Specials share TVDB numbers.
    if source_season != 1:
        return ()
    season = _number(entry.get('defaulttvdbseason'))
    offset = entry.get('episodeoffset', '0')
    if not re.fullmatch(r'-?[0-9]+', offset):
        raise MappingHold('unsupported default offset')
    number = source_number + int(offset)
    if number <= 0:
        return ()
    return ((season, number),)


def _catalogue(known_anidb_episodes: list[tuple[int, int, int]]) -> set[tuple[int, int, int]]:
    if (not isinstance(known_anidb_episodes, list)
            or len(known_anidb_episodes) > 100_000):
        raise MappingHold('invalid AniDB episode catalogue')
    if any(
        not isinstance(row, tuple) or len(row) != 3
        or any(type(value) is not int for value in row)
        or row[0] <= 0 or row[1] not in (0, 1) or row[2] <= 0
        for row in known_anidb_episodes
    ):
        raise MappingHold('invalid AniDB episode catalogue')
    known = set(known_anidb_episodes)
    if len(known) != len(known_anidb_episodes):
        raise MappingHold('duplicate AniDB episode catalogue row')
    return known


def _parse(xml_bytes: bytes) -> ElementTree.Element:
    if not isinstance(xml_bytes, bytes) or len(xml_bytes) > 16_000_000 or not xml_bytes:
        raise MappingHold('invalid Anime-Lists snapshot')
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise MappingHold('invalid Anime-Lists XML') from exc
    if root.tag != 'anime-list':
        raise MappingHold('wrong Anime-Lists root')
    return root


def tvdb_anime_ids(xml_bytes: bytes, tvdb_series_id: int) -> tuple[int, ...]:
    """List every published AniDB candidate for a TVDB show, without picking one."""
    if type(tvdb_series_id) is not int or tvdb_series_id <= 0:
        raise MappingHold('invalid TVDB show ID')
    ids: set[int] = set()
    for entry in _parse(xml_bytes).findall('anime'):
        if entry.get('tvdbid') == str(tvdb_series_id):
            anime_id = _number(entry.get('anidbid'), allow_zero=False)
            if anime_id in ids:
                raise MappingHold('duplicate Anime-Lists AniDB entry')
            ids.add(anime_id)
    if not ids or len(ids) > 100:
        raise MappingHold('missing or excessive Anime-Lists candidates')
    return tuple(sorted(ids))


def _reverse_index(root: ElementTree.Element, tvdb_series_id: int,
                   known: set[tuple[int, int, int]]) -> dict[tuple[int, int], set[tuple[int, int, int]]]:
    by_anime: dict[int, list[tuple[int, int, int]]] = {}
    for candidate in known:
        by_anime.setdefault(candidate[0], []).append(candidate)
    targets: dict[tuple[int, int], set[tuple[int, int, int]]] = {}
    matching_entries = 0
    for entry in root.findall('anime'):
        if entry.get('tvdbid') != str(tvdb_series_id):
            continue
        matching_entries += 1
        anime_id = _number(entry.get('anidbid'), allow_zero=False)
        for candidate in by_anime.get(anime_id, ()):
            for target in _forward_targets(entry, candidate[1], candidate[2]):
                targets.setdefault(target, set()).add(candidate)
    if matching_entries == 0:
        raise MappingHold('TVDB show absent from Anime-Lists')
    return targets


def _unique_target(index: dict[tuple[int, int], set[tuple[int, int, int]]],
                   season_number: int, episode_number: int) -> tuple[int, int, int]:
    matches = index.get((season_number, episode_number), set())
    if len(matches) != 1:
        raise MappingHold('missing or competing AniDB episode ownership')
    return next(iter(matches))


def resolve_tvdb_episode(
    xml_bytes: bytes,
    tvdb_series_id: int,
    season_number: int,
    episode_number: int,
    known_anidb_episodes: list[tuple[int, int, int]],
) -> tuple[int, int, int]:
    """Map an anchored TVDB coordinate to one *known* AniDB episode tuple.

    XML revision pinning, requested TVDB episode-ID anchoring, Shoko file
    identity, and Plex Part checks are the caller's separate duties.
    """
    if (type(tvdb_series_id) is not int
            or tvdb_series_id <= 0 or type(season_number) is not int
            or season_number < 0 or type(episode_number) is not int
            or episode_number <= 0):
        raise MappingHold('invalid coordinate input')
    index = _reverse_index(_parse(xml_bytes), tvdb_series_id,
                           _catalogue(known_anidb_episodes))
    return _unique_target(index, season_number, episode_number)


def resolve_anchored_request(
    xml_bytes: bytes,
    tvdb_show_id: int,
    anchored_ids: tuple[tuple[int, int, int], ...],
    known_anidb_episodes: list[tuple[int, int, int]],
) -> tuple[tuple[int, int, int, int], ...]:
    """Join complete Seerr TVDB IDs through published rules to AniDB tuples."""
    if (type(tvdb_show_id) is not int or tvdb_show_id <= 0
            or not isinstance(anchored_ids, tuple) or not anchored_ids
            or len(anchored_ids) > 5000):
        raise MappingHold('invalid anchored TVDB request')
    seen_ids: set[int] = set()
    seen_coordinates: set[tuple[int, int]] = set()
    for row in anchored_ids:
        if (not isinstance(row, tuple) or len(row) != 3
                or any(type(value) is not int for value in row)
                or row[0] <= 0 or row[1] < 0 or row[2] <= 0
                or row[0] in seen_ids or row[1:] in seen_coordinates):
            raise MappingHold('duplicate or invalid requested episode')
        seen_ids.add(row[0])
        seen_coordinates.add(row[1:])
    index = _reverse_index(_parse(xml_bytes), tvdb_show_id,
                           _catalogue(known_anidb_episodes))
    return tuple((episode_id, *_unique_target(index, season, number))
                 for episode_id, season, number in anchored_ids)


def anchor_seerr_tvdb_ids(
    settings: dict,
    show: dict,
    season_details: dict[int, dict],
    show_id: int,
    requested_seasons: tuple[int, ...],
) -> tuple[tuple[int, int, int], ...]:
    """Anchor Seerr's TVDB episode IDs to its complete season coordinates.

    This is an identity read from Seerr's selected TVDB provider, not a
    conversion by equal numbers between providers. The resulting coordinates
    still require a published mapping and Shoko/Plex evidence.
    """
    if (not isinstance(settings, dict) or settings.get('anime') != 'tvdb'
            or not isinstance(show, dict) or type(show_id) is not int
            or show_id <= 0 or show.get('id') != show_id
            or not isinstance(show.get('seasons'), list)
            or not isinstance(season_details, dict)
            or not isinstance(requested_seasons, tuple)
            or not requested_seasons or len(requested_seasons) > 100
            or len(set(requested_seasons)) != len(requested_seasons)
            or any(type(number) is not int or number < 0
                   for number in requested_seasons)):
        raise MappingHold('Seerr TVDB request is unanchored')
    result: list[tuple[int, int, int]] = []
    seen_ids: set[int] = set()
    for season in sorted(requested_seasons):
        rows = [row for row in show['seasons']
                if isinstance(row, dict) and row.get('seasonNumber') == season]
        if len(rows) != 1:
            raise MappingHold('Seerr season is absent or duplicated')
        count = rows[0].get('episodeCount')
        detail = season_details.get(season)
        episodes = detail.get('episodes') if isinstance(detail, dict) else None
        if (type(count) is not int or not 1 <= count <= 1000
                or not isinstance(episodes, list) or len(episodes) != count):
            raise MappingHold('Seerr season episode census is incomplete')
        numbers: set[int] = set()
        for row in episodes:
            if not isinstance(row, dict):
                raise MappingHold('invalid Seerr episode row')
            episode_id = row.get('id')
            number = row.get('episodeNumber')
            if (type(episode_id) is not int or episode_id <= 0
                    or type(number) is not int or not 1 <= number <= count
                    or row.get('seasonNumber') != season
                    or episode_id in seen_ids or number in numbers):
                raise MappingHold('ambiguous Seerr TVDB episode identity')
            seen_ids.add(episode_id)
            numbers.add(number)
            result.append((episode_id, season, number))
        if numbers != set(range(1, count + 1)):
            raise MappingHold('Seerr season numbering has gaps')
    return tuple(sorted(result, key=lambda row: (row[1], row[2])))


def verify_sonarr_tvdb_anchor(
    anchored_ids: tuple[tuple[int, int, int], ...],
    sonarr_episodes: list[dict],
) -> tuple[tuple[int, int, int], ...]:
    """Require an independent Sonarr TVDB-ID match for every Seerr coordinate.

    Seerr may fall back to TMDB episode IDs even while its configured anime
    provider says TVDB. The caller must bind these rows to the requested
    TVDB show using Sonarr's exact series identity first.
    """
    if (not isinstance(anchored_ids, tuple) or not anchored_ids
            or len(anchored_ids) > 5000 or not isinstance(sonarr_episodes, list)
            or len(sonarr_episodes) > 10000):
        raise MappingHold('invalid Sonarr TVDB catalogue')
    by_coordinate: dict[tuple[int, int], int] = {}
    seen_ids: set[int] = set()
    requested = {(row[1], row[2]) for row in anchored_ids
                 if isinstance(row, tuple) and len(row) == 3}
    for row in sonarr_episodes:
        if not isinstance(row, dict):
            raise MappingHold('invalid Sonarr episode row')
        tvdb_id = row.get('tvdbId')
        season = row.get('seasonNumber')
        number = row.get('episodeNumber')
        if (type(season) is not int or type(number) is not int):
            raise MappingHold('invalid Sonarr coordinate')
        if (season, number) not in requested:
            continue
        if (type(tvdb_id) is not int or tvdb_id <= 0
                or season < 0 or number <= 0
                or tvdb_id in seen_ids or (season, number) in by_coordinate):
            raise MappingHold('ambiguous Sonarr TVDB episode catalogue')
        seen_ids.add(tvdb_id)
        by_coordinate[(season, number)] = tvdb_id
    for row in anchored_ids:
        if (not isinstance(row, tuple) or len(row) != 3
                or any(type(value) is not int for value in row)
                or row[0] <= 0 or row[1] < 0 or row[2] <= 0
                or by_coordinate.get((row[1], row[2])) != row[0]):
            raise MappingHold('Seerr episode IDs are not independently TVDB-anchored')
    return anchored_ids
