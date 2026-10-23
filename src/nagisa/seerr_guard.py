"""Private, side-effect-free anime request ownership decision.

This module is deliberately not wired to the live Nagisa server or Seerr.
It consumes independently collected stock Shoko and Plex evidence.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class AniDBCandidate:
    episode_id: int
    shoko_video_ids: tuple[int, ...]
    # Caller must verify Plex Part accessibility *and* card episode identity.
    plex_verified_video_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class AnimeEpisode:
    tmdb_episode_id: int
    tvdb_episode_id: int | None
    candidates: tuple[AniDBCandidate, ...]
    season_number: int | None = None


@dataclass(frozen=True, slots=True)
class GuardDecision:
    action: str
    missing_tvdb_ids: tuple[int, ...] = ()
    reason: str = ""
    present_tvdb_ids: tuple[int, ...] = ()
    missing_seasons: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class SonarrEpisodePlan:
    action: str
    monitor_sonarr_ids: tuple[int, ...] = ()
    unmonitor_sonarr_ids: tuple[int, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ShowFootprint:
    catalogue_complete: bool
    shoko_video_ids: tuple[int, ...]
    plex_video_ids: tuple[int, ...]


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def classify_show(footprint: ShowFootprint) -> str:
    """Only a complete negative catalogue check permits stock new-show routing."""
    if not isinstance(footprint, ShowFootprint):
        raise ValueError('invalid show footprint')
    if not footprint.catalogue_complete:
        return 'hold_for_review'
    if not all(_positive(value) for value in
               footprint.shoko_video_ids + footprint.plex_video_ids):
        raise ValueError('invalid show video identity')
    if footprint.shoko_video_ids or footprint.plex_video_ids:
        return 'guard_existing'
    return 'stock_new_show'


def prepare_series_add(payload: dict, classification: str,
                       decision: GuardDecision | None) -> dict:
    """Pause the series, not its seasons, until exact ownership is bound.

    Stock Sonarr gives future episodes the season's monitored flag. Leaving a
    guarded season off would silently lose future automatic arrivals. Keeping
    the series off prevents RSS grabs while its present episodes are protected.
    """
    if not isinstance(payload, dict) or not _positive(payload.get('tvdbId')):
        raise ValueError('invalid Sonarr series add')
    seasons = payload.get('seasons')
    if not isinstance(seasons, list) or not all(
        isinstance(row, dict) and type(row.get('seasonNumber')) is int
        and row['seasonNumber'] >= 0 for row in seasons
    ):
        raise ValueError('invalid Sonarr seasons')
    if classification == 'stock_new_show':
        return copy.deepcopy(payload)
    if (classification != 'guard_existing' or decision is None
            or decision.action not in {'already_present', 'search_missing'}):
        raise ValueError('guard evidence is not sufficient to add series')
    return prepare_paused_series_add(payload)


def prepare_paused_series_add(payload: dict) -> dict:
    """Register an uncertain series without any add-time acquisition."""
    if not isinstance(payload, dict) or not _positive(payload.get('tvdbId')):
        raise ValueError('invalid Sonarr series add')
    seasons = payload.get('seasons')
    if (not isinstance(seasons, list) or not seasons
            or not all(isinstance(row, dict)
                       and type(row.get('seasonNumber')) is int
                       and row['seasonNumber'] >= 0
                       and type(row.get('monitored')) is bool for row in seasons)
            or not any(row['monitored'] for row in seasons)):
        raise ValueError('uncertain Sonarr add lacks monitored seasons')
    safe = copy.deepcopy(payload)
    if safe.get('monitored') is not True:
        raise ValueError('guarded automatic request must monitor the series')
    safe['monitored'] = False
    safe['monitorNewItems'] = 'all'
    options = safe.get('addOptions')
    if not isinstance(options, dict):
        options = {}
        safe['addOptions'] = options
    options['searchForMissingEpisodes'] = False
    options['searchForCutoffUnmetEpisodes'] = False
    options['ignoreEpisodesWithFiles'] = True
    options['monitor'] = 'skip'
    return safe


def prepare_series_update(payload: dict, guarded_seasons: tuple[int, ...],
                          decision: GuardDecision) -> dict:
    """Pause the series while retaining future-episode season monitoring."""
    if (not isinstance(payload, dict) or not _positive(payload.get('id'))
            or not _positive(payload.get('tvdbId'))
            or decision.action not in {'already_present', 'search_missing'}):
        raise ValueError('invalid guarded Sonarr series update')
    if (not isinstance(guarded_seasons, tuple) or not guarded_seasons
            or len(set(guarded_seasons)) != len(guarded_seasons)
            or not all(type(number) is int and number >= 0
                       for number in guarded_seasons)):
        raise ValueError('invalid guarded seasons')
    safe = copy.deepcopy(payload)
    if safe.get('monitored') is not True:
        raise ValueError('guarded automatic request must monitor the series')
    safe['monitored'] = False
    safe['monitorNewItems'] = 'all'
    seasons = safe.get('seasons')
    if not isinstance(seasons, list):
        raise ValueError('invalid Sonarr seasons')
    seen: set[int] = set()
    for row in seasons:
        if not isinstance(row, dict) or type(row.get('seasonNumber')) is not int:
            raise ValueError('invalid Sonarr season')
        number = row['seasonNumber']
        if number < 0 or number in seen:
            raise ValueError('duplicate or invalid Sonarr season')
        seen.add(number)
        if number in guarded_seasons:
            row['monitored'] = True
    if not set(guarded_seasons).issubset(seen):
        raise ValueError('guarded Sonarr season is absent')
    return safe


def guarded_seasons_from_series(payload: dict) -> tuple[int, ...]:
    """Treat every submitted monitored season as in scope; never guess a delta."""
    seasons = payload.get('seasons') if isinstance(payload, dict) else None
    if not isinstance(seasons, list):
        raise ValueError('invalid Sonarr season list')
    seen: set[int] = set()
    guarded: list[int] = []
    for row in seasons:
        if (not isinstance(row, dict)
                or type(row.get('seasonNumber')) is not int
                or row['seasonNumber'] < 0
                or type(row.get('monitored')) is not bool):
            raise ValueError('invalid Sonarr season evidence')
        number = row['seasonNumber']
        if number in seen:
            raise ValueError('duplicate Sonarr season')
        seen.add(number)
        if row['monitored']:
            guarded.append(number)
    if not guarded:
        raise ValueError('no monitored season in Seerr request')
    return tuple(sorted(guarded))


def narrow_monitor_request(payload: dict, plan: SonarrEpisodePlan) -> dict:
    """Remove Seerr's attempts to re-monitor already present episodes."""
    if (not isinstance(payload, dict) or payload.get('monitored') is not True
            or not isinstance(payload.get('episodeIds'), list)
            or not all(_positive(value) for value in payload['episodeIds'])
            or plan.action not in {'search_missing', 'already_present'}):
        raise ValueError('invalid guarded episode-monitor request')
    allowed = set(plan.monitor_sonarr_ids)
    return {'episodeIds': sorted(allowed.intersection(payload['episodeIds'])),
            'monitored': True}


def rewrite_search_command(payload: dict, series_id: int,
                           plan: SonarrEpisodePlan) -> dict | None:
    """Replace Seerr's broad series search with exact missing episode IDs."""
    if (not _positive(series_id) or not isinstance(payload, dict)
            or payload.get('name') != 'MissingEpisodeSearch'
            or payload.get('seriesId') != series_id
            or plan.action not in {'search_missing', 'already_present'}):
        raise ValueError('guarded series search is not safe')
    if plan.action == 'already_present':
        return None
    if not plan.monitor_sonarr_ids:
        raise ValueError('no exact missing episode to search')
    return {'name': 'EpisodeSearch',
            'episodeIds': list(plan.monitor_sonarr_ids)}


def decide_request(episodes: Iterable[AnimeEpisode]) -> GuardDecision:
    """Classify a bounded, enumerated request; uncertain evidence never searches."""
    rows = tuple(episodes)
    if not rows:
        return GuardDecision('hold_for_review', reason='no_requested_episodes')
    if len(rows) > 1000:
        raise ValueError('episode request exceeds bound')

    seen_tmdb: set[int] = set()
    seen_tvdb: set[int] = set()
    missing: list[int] = []
    present: list[int] = []
    missing_seasons: set[int] = set()
    missing_season_unknown = False
    present_without_tvdb = False
    for row in rows:
        if not isinstance(row, AnimeEpisode) or not _positive(row.tmdb_episode_id):
            raise ValueError('invalid TMDB episode evidence')
        if (row.season_number is not None and
                (type(row.season_number) is not int or row.season_number < 0)):
            raise ValueError('invalid TMDB episode season')
        if row.tmdb_episode_id in seen_tmdb:
            return GuardDecision('hold_for_review', reason='duplicate_tmdb_episode')
        seen_tmdb.add(row.tmdb_episode_id)
        if row.tvdb_episode_id is not None:
            if not _positive(row.tvdb_episode_id):
                raise ValueError('invalid TVDB episode evidence')
            if row.tvdb_episode_id in seen_tvdb:
                return GuardDecision('hold_for_review', reason='duplicate_tvdb_episode')
            seen_tvdb.add(row.tvdb_episode_id)
        if not row.candidates:
            return GuardDecision('hold_for_review', reason='ambiguous_anidb_identity')
        all_candidates_playable = True
        for candidate in row.candidates:
            if not isinstance(candidate, AniDBCandidate) or not _positive(candidate.episode_id):
                raise ValueError('invalid AniDB episode evidence')
            files = set(candidate.shoko_video_ids)
            plex = set(candidate.plex_verified_video_ids)
            if (not all(map(_positive, files | plex)) or
                    len(files) != len(candidate.shoko_video_ids) or
                    len(plex) != len(candidate.plex_verified_video_ids) or
                    not plex.issubset(files)):
                raise ValueError('inconsistent Shoko/Plex file evidence')
            if not files or not plex:
                all_candidates_playable = False
        if len(row.candidates) > 1:
            # A many-to-one provider link is safe to protect only when every
            # linked programme has a verified playable copy. Never choose one.
            if not all_candidates_playable:
                return GuardDecision('hold_for_review', reason='ambiguous_anidb_identity')
            if row.tvdb_episode_id is None:
                present_without_tvdb = True
            else:
                present.append(row.tvdb_episode_id)
            continue
        if files:
            if not plex:
                return GuardDecision('hold_for_review', reason='file_not_playable')
            if row.tvdb_episode_id is None:
                present_without_tvdb = True
            else:
                present.append(row.tvdb_episode_id)
            continue
        if not _positive(row.tvdb_episode_id):
            return GuardDecision('hold_for_review', reason='missing_tvdb_identity')
        missing.append(row.tvdb_episode_id)
        if row.season_number is None:
            missing_season_unknown = True
        else:
            missing_seasons.add(row.season_number)

    if present_without_tvdb:
        return GuardDecision('hold_for_review', reason='present_episode_lacks_tvdb_identity')
    if missing:
        return GuardDecision('search_missing', tuple(sorted(missing)),
                             present_tvdb_ids=tuple(sorted(present)),
                             missing_seasons=(tuple(sorted(missing_seasons))
                                              if not missing_season_unknown else ()))
    return GuardDecision('already_present', present_tvdb_ids=tuple(sorted(present)))


def bind_sonarr_episodes(decision: GuardDecision,
                         sonarr_episodes: Iterable[dict],
                         guarded_seasons: tuple[int, ...] | None = None) -> SonarrEpisodePlan:
    """Bind a safe partial request to stock Sonarr's actual episode IDs.

    The caller must fetch the series episode list after a non-searching add/update.
    This function never performs that add/update or a download search.
    """
    if decision.action not in {'search_missing', 'already_present'}:
        return SonarrEpisodePlan('hold_for_review', reason='not_a_guarded_decision')
    if decision.action == 'already_present' and not decision.present_tvdb_ids:
        return SonarrEpisodePlan('hold_for_review', reason='present_tvdb_identity_missing')
    needed = set(decision.missing_tvdb_ids) | set(decision.present_tvdb_ids)
    if guarded_seasons is not None and not guarded_seasons:
        raise ValueError('empty guarded season scope')
    found: dict[int, dict] = {}
    scoped: set[int] = set()
    for row in sonarr_episodes:
        if not isinstance(row, dict):
            raise ValueError('invalid Sonarr episode evidence')
        tvdb_id = row.get('tvdbId')
        if guarded_seasons is not None and row.get('seasonNumber') in guarded_seasons:
            if not _positive(tvdb_id):
                return SonarrEpisodePlan('hold_for_review', reason='sonarr_episode_lacks_tvdb_identity')
            scoped.add(tvdb_id)
        if tvdb_id not in needed:
            continue
        sonarr_id = row.get('id')
        if not _positive(sonarr_id) or type(row.get('hasFile')) is not bool:
            raise ValueError('invalid Sonarr episode identity or file state')
        if tvdb_id in found:
            return SonarrEpisodePlan('hold_for_review', reason='duplicate_sonarr_tvdb_identity')
        found[tvdb_id] = row
    if set(found) != needed:
        return SonarrEpisodePlan('hold_for_review', reason='sonarr_identity_missing')
    if guarded_seasons is not None and scoped != needed:
        return SonarrEpisodePlan('hold_for_review', reason='sonarr_season_census_mismatch')
    if any(found[identity]['hasFile'] for identity in decision.missing_tvdb_ids):
        return SonarrEpisodePlan('hold_for_review', reason='sonarr_file_not_delivered')
    if decision.action == 'already_present':
        return SonarrEpisodePlan(
            'already_present',
            unmonitor_sonarr_ids=tuple(sorted(
                found[identity]['id'] for identity in decision.present_tvdb_ids)),
        )
    return SonarrEpisodePlan(
        'search_missing',
        tuple(sorted(found[identity]['id'] for identity in decision.missing_tvdb_ids)),
        tuple(sorted(found[identity]['id'] for identity in decision.present_tvdb_ids)),
    )
