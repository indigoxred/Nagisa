"""Private Seerr→Sonarr mutation boundary, without an HTTP listener.

This is an isolated integration increment. Sessions are in memory and this
module is intentionally not wired into the running Nagisa service.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from nagisa.seerr_guard import (
    GuardDecision, SonarrEpisodePlan, bind_sonarr_episodes,
    guarded_seasons_from_series, narrow_monitor_request,
    prepare_paused_series_add, prepare_series_add, prepare_series_update,
    rewrite_search_command,
)


@dataclass(frozen=True, slots=True)
class _Session:
    tvdb_id: int
    series_id: int
    episode_ids: frozenset[int]
    plan: SonarrEpisodePlan


class PrivateSonarrBoundary:
    """Guard Seerr's known mutating call sequence against one stock Sonarr."""

    def __init__(self, upstream: Any, evidence: Any, *, journal: Any = None,
                 unknown_reporter: Any = None) -> None:
        self.upstream = upstream
        self.evidence = evidence
        self.journal = journal
        if unknown_reporter is not None and not callable(unknown_reporter):
            raise ValueError('unknown-show risk reporter must be callable')
        self.unknown_reporter = unknown_reporter
        self._sessions: dict[int, _Session] = {}
        self._pending_pause: dict | None = None
        self._lock = threading.RLock()
        if journal is not None:
            raw = journal.load()
            if not isinstance(raw, dict) or len(raw) > 1000:
                raise ValueError('invalid recovered Seerr sessions')
            pending = raw.pop('_pause', None)
            if pending is not None:
                self._validate_pause(pending)
                self._pending_pause = pending
            for key, value in raw.items():
                session = self._decode_session(key, value)
                self._sessions[session.series_id] = session

    @staticmethod
    def _validate_pause(value: object) -> None:
        if (not isinstance(value, dict) or set(value) !=
                {'tvdb_id', 'series_id', 'guarded_seasons', 'kind'}
                or type(value['tvdb_id']) is not int or value['tvdb_id'] <= 0
                or type(value['series_id']) is not int or value['series_id'] < 0
                or value['kind'] not in {'add', 'update'}
                or (value['kind'] == 'add' and value['series_id'] != 0)
                or (value['kind'] == 'update' and value['series_id'] == 0)
                or not isinstance(value['guarded_seasons'], list)
                or not value['guarded_seasons']
                or len(value['guarded_seasons']) > 100
                or any(type(number) is not int or number < 0
                       for number in value['guarded_seasons'])
                or len(set(value['guarded_seasons'])) != len(value['guarded_seasons'])):
            raise ValueError('invalid interrupted Sonarr pause')

    @staticmethod
    def _decode_session(key: object, value: object) -> _Session:
        if not isinstance(key, str) or not key.isdigit() or not isinstance(value, dict):
            raise ValueError('invalid recovered Seerr session')
        required = {'tvdb_id', 'series_id', 'episode_ids', 'action',
                    'monitor_ids', 'unmonitor_ids'}
        if set(value) != required:
            raise ValueError('incomplete recovered Seerr session')
        tvdb_id, series_id = value['tvdb_id'], value['series_id']
        if (type(tvdb_id) is not int or tvdb_id <= 0 or type(series_id) is not int
                or series_id <= 0 or str(series_id) != key):
            raise ValueError('invalid recovered Seerr series identity')
        groups = [value[name] for name in ('episode_ids', 'monitor_ids', 'unmonitor_ids')]
        if any(not isinstance(group, list) or len(group) > 1000 or
               any(type(item) is not int or item <= 0 for item in group) or
               len(group) != len(set(group))
               for group in groups):
            raise ValueError('invalid recovered Seerr episode IDs')
        all_ids, monitor, unmonitor = map(set, groups)
        if (value['action'] not in ('already_present', 'search_missing',
                                   'ownership_held')
                or monitor & unmonitor or not (monitor | unmonitor).issubset(all_ids)
                or (value['action'] == 'already_present' and monitor)
                or (value['action'] == 'search_missing' and not monitor)
                or (value['action'] == 'ownership_held'
                    and (not all_ids or monitor or unmonitor))):
            raise ValueError('unsafe recovered Seerr episode plan')
        plan = SonarrEpisodePlan(value['action'], tuple(sorted(monitor)),
                                 tuple(sorted(unmonitor)))
        return _Session(tvdb_id, series_id, frozenset(all_ids), plan)

    @staticmethod
    def _encode_session(session: _Session) -> dict:
        return {'tvdb_id': session.tvdb_id, 'series_id': session.series_id,
                'episode_ids': sorted(session.episode_ids), 'action': session.plan.action,
                'monitor_ids': list(session.plan.monitor_sonarr_ids),
                'unmonitor_ids': list(session.plan.unmonitor_sonarr_ids)}

    def _save_state(self, sessions: dict[int, _Session], pause: dict | None) -> None:
        if self.journal is not None:
            raw = {str(key): self._encode_session(session)
                   for key, session in sessions.items()}
            if pause is not None:
                raw['_pause'] = pause
            self.journal.save(raw)

    def _replace_sessions(self, sessions: dict[int, _Session]) -> None:
        self._save_state(sessions, self._pending_pause)
        self._sessions = sessions

    def _begin_pause(self, tvdb_id: int, series_id: int,
                     seasons: tuple[int, ...], kind: str) -> None:
        if self._pending_pause is not None:
            raise ValueError('another Sonarr pause is unfinished')
        pause = {'tvdb_id': tvdb_id, 'series_id': series_id,
                 'guarded_seasons': list(seasons), 'kind': kind}
        self._validate_pause(pause)
        self._save_state(self._sessions, pause)
        self._pending_pause = pause

    def _end_pause(self) -> None:
        self._save_state(self._sessions, None)
        self._pending_pause = None

    def _record_held(self, series_id: int, tvdb_id: int, rows: object) -> None:
        ids = self._episode_ids(rows)
        if not ids:
            raise ValueError('held registration lacks Sonarr episode IDs')
        updated = dict(self._sessions)
        existing = updated.get(series_id)
        if (existing is not None and
                (existing.tvdb_id != tvdb_id or existing.episode_ids != ids
                 or existing.plan.action != 'ownership_held')):
            raise ValueError('held registration conflicts with saved session')
        updated[series_id] = _Session(
            tvdb_id, series_id, ids, SonarrEpisodePlan('ownership_held'))
        self._replace_sessions(updated)
        self._end_pause()

    def _recheck_held(self, session: _Session, seasons: tuple[int, ...]) -> dict:
        series_id = session.series_id
        series = self.upstream.request_json(
            'GET', f'/api/v3/series/{series_id}')
        if (not isinstance(series, dict) or series.get('id') != series_id
                or series.get('tvdbId') != session.tvdb_id
                or type(series.get('tmdbId')) is not int
                or series['tmdbId'] <= 0
                or series.get('monitored') is not False):
            raise ValueError('held Sonarr series identity or pause differs')
        rows = self.upstream.request_json(
            'GET', f'/api/v3/episode?seriesId={series_id}')
        if self._episode_ids(rows) != session.episode_ids:
            raise ValueError('held Sonarr episode catalogue changed')
        classification, decision = self.evidence.review_registered(
            session.tvdb_id, series['tmdbId'], seasons, rows)
        if (classification != 'guard_existing' or decision is None
                or decision.action not in {'already_present', 'search_missing'}):
            # A repeated Seerr add must keep its Approved/Processing request
            # attached to the already registered paused series. The durable
            # held session stays in place and no search is dispatched.
            return series
        response = {'id': series_id, 'tvdbId': session.tvdb_id}
        self._record(response, session.tvdb_id, decision, seasons)
        self.search_series({'name': 'MissingEpisodeSearch',
                            'seriesId': series_id})
        return response

    def _complete_committed_present(self, session: _Session,
                                    seasons: tuple[int, ...]) -> dict:
        if session.plan.action != 'already_present':
            raise ValueError('saved anime request is not fully present')
        series_id = session.series_id
        series = self.upstream.request_json(
            'GET', f'/api/v3/series/{series_id}')
        if (not isinstance(series, dict) or series.get('id') != series_id
                or series.get('tvdbId') != session.tvdb_id
                or series.get('monitored') is not True
                or not isinstance(series.get('seasons'), list)
                or not set(seasons).issubset({row.get('seasonNumber')
                    for row in series['seasons'] if isinstance(row, dict)
                    and row.get('monitored') is True})):
            raise ValueError('saved anime series state differs')
        rows = self.upstream.request_json(
            'GET', f'/api/v3/episode?seriesId={series_id}')
        if self._episode_ids(rows) != session.episode_ids:
            raise ValueError('saved anime episode catalogue differs')
        flags = {row.get('id'): row.get('monitored')
                 for row in rows if isinstance(row, dict)}
        if any(flags.get(item) is not False
               for item in session.plan.unmonitor_sonarr_ids):
            raise ValueError('saved anime episode protection differs')
        self.search_series({'name': 'MissingEpisodeSearch',
                            'seriesId': series_id})
        return {'id': series_id, 'tvdbId': session.tvdb_id}

    def recover_pending_pause(self) -> str:
        """Re-evaluate ownership before completing an interrupted pause.

        A caller must invoke this before serving Seerr traffic. Any failed
        evidence check leaves the durable pause intact and writes nothing to
        Sonarr. No blind re-monitor or broad search is permitted.
        """
        with self._lock:
            pending = self._pending_pause
            if pending is None:
                return 'nothing_pending'
            tvdb_id = pending['tvdb_id']
            seasons = tuple(pending['guarded_seasons'])
            classification, decision = self.evidence.review(tvdb_id, seasons)
            if (classification == 'stock_unverified_show'
                    and pending['kind'] == 'add'):
                rows = self.upstream.request_json('GET', '/api/v3/series')
                if not isinstance(rows, list):
                    raise ValueError('Sonarr series list unavailable during recovery')
                matches = [row for row in rows if isinstance(row, dict)
                           and row.get('tvdbId') == tvdb_id]
                if not matches:
                    self._end_pause()
                    return 'add_never_reached_sonarr'
                if len(matches) != 1 or type(matches[0].get('id')) is not int:
                    raise ValueError('registered anime series is ambiguous')
                series_id = matches[0]['id']
                series = self.upstream.request_json(
                    'GET', f'/api/v3/series/{series_id}')
                if (not isinstance(series, dict) or series.get('id') != series_id
                        or series.get('tvdbId') != tvdb_id
                        or type(series.get('tmdbId')) is not int
                        or series['tmdbId'] <= 0):
                    raise ValueError('registered anime identity differs')
                episodes = self.upstream.request_json(
                    'GET', f'/api/v3/episode?seriesId={series_id}')
                classification, decision = self.evidence.review_registered(
                    tvdb_id, series['tmdbId'], seasons, episodes)
                if series.get('monitored') is True:
                    if (classification != 'guard_existing' or decision is None
                            or decision.action not in {'already_present', 'search_missing'}):
                        raise ValueError('resumed anime lacks exact ownership')
                    plan = bind_sonarr_episodes(decision, episodes, seasons)
                    flags = {row.get('id'): row.get('monitored')
                             for row in episodes if isinstance(row, dict)}
                    watched_seasons = {row.get('seasonNumber')
                                       for row in series.get('seasons', [])
                                       if isinstance(row, dict)
                                       and row.get('monitored') is True}
                    if (plan.action not in {'already_present', 'search_missing'}
                            or any(flags.get(item) is not False
                                   for item in plan.unmonitor_sonarr_ids)
                            or any(flags.get(item) is not True
                                   for item in plan.monitor_sonarr_ids)
                            or not set(seasons).issubset(watched_seasons)):
                        raise ValueError('resumed anime protection differs')
                    saved = self._sessions.get(series_id)
                    if saved is not None and (
                            saved.tvdb_id != tvdb_id
                            or saved.episode_ids != self._episode_ids(episodes)
                            or saved.plan != plan):
                        raise ValueError('resumed anime session differs')
                    self._end_pause()
                    if saved is not None:
                        self.search_series({'name': 'MissingEpisodeSearch',
                                            'seriesId': series_id})
                    return 'guarded_series_resumed'
                if series.get('monitored') is not False:
                    raise ValueError('registered anime pause differs')
                if (classification != 'guard_existing' or decision is None
                        or decision.action not in {'already_present', 'search_missing'}):
                    self._record_held(series_id, tvdb_id, episodes)
                    return 'ownership_held'
                self._record({'id': series_id, 'tvdbId': tvdb_id}, tvdb_id,
                             decision, seasons)
                self._end_pause()
                self.search_series({'name': 'MissingEpisodeSearch',
                                    'seriesId': series_id})
                return 'guarded_series_resumed'
            if (classification != 'guard_existing' or decision is None
                    or decision.action not in {'already_present', 'search_missing'}):
                raise ValueError('interrupted Sonarr pause lacks current ownership evidence')
            if pending['kind'] == 'add':
                rows = self.upstream.request_json('GET', '/api/v3/series')
                if not isinstance(rows, list):
                    raise ValueError('Sonarr series list unavailable during recovery')
                matches = [row for row in rows if isinstance(row, dict)
                           and row.get('tvdbId') == tvdb_id]
                if not matches:
                    self._end_pause()
                    return 'add_never_reached_sonarr'
                if len(matches) != 1:
                    raise ValueError('interrupted add has ambiguous Sonarr ownership')
                series_id = matches[0].get('id')
            else:
                series_id = pending['series_id']
            if type(series_id) is not int or series_id <= 0:
                raise ValueError('interrupted Sonarr series ID invalid')
            series = self.upstream.request_json('GET', f'/api/v3/series/{series_id}')
            if (not isinstance(series, dict) or series.get('id') != series_id
                    or series.get('tvdbId') != tvdb_id):
                raise ValueError('interrupted Sonarr series identity changed')
            if series.get('monitored') is True:
                safe = prepare_series_update(series, seasons, decision)
                self.upstream.request_json('PUT', '/api/v3/series', safe)
            elif series.get('monitored') is not False:
                raise ValueError('interrupted Sonarr series monitor flag invalid')
            self._record({'id': series_id, 'tvdbId': tvdb_id}, tvdb_id,
                         decision, seasons)
            self._end_pause()
            return 'guarded_series_resumed'

    @staticmethod
    def _episode_ids(rows: object) -> frozenset[int]:
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError('invalid Sonarr episode response')
        ids = []
        for row in rows:
            if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0:
                raise ValueError('invalid Sonarr episode ID')
            ids.append(row['id'])
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate Sonarr episode ID')
        return frozenset(ids)

    def _record(self, response: object, tvdb_id: int,
                decision: GuardDecision, guarded_seasons: tuple[int, ...]) -> None:
        if not isinstance(response, dict):
            raise ValueError('invalid Sonarr series response')
        series_id = response.get('id')
        if (type(series_id) is not int or series_id <= 0
                or response.get('tvdbId') != tvdb_id):
            raise ValueError('Sonarr returned the wrong series')
        rows = self.upstream.request_json(
            'GET', f'/api/v3/episode?seriesId={series_id}')
        ids = self._episode_ids(rows)
        plan = bind_sonarr_episodes(decision, rows, guarded_seasons)
        if plan.action not in {'already_present', 'search_missing'}:
            raise ValueError(f'Sonarr episode binding held: {plan.reason}')
        self._complete_monitoring(series_id, tvdb_id, guarded_seasons, plan)
        updated = dict(self._sessions)
        updated[series_id] = _Session(tvdb_id, series_id, ids, plan)
        self._replace_sessions(updated)

    def _complete_monitoring(self, series_id: int, tvdb_id: int,
                             guarded_seasons: tuple[int, ...],
                             plan: SonarrEpisodePlan) -> None:
        """Protect owned episodes before resuming RSS for future arrivals.

        A failed check leaves the private Sonarr series paused. Durable
        transaction recovery is still required before any production route.
        """
        path = f'/api/v3/series/{series_id}'
        series = self.upstream.request_json('GET', path)
        if (not isinstance(series, dict) or series.get('tvdbId') != tvdb_id
                or series.get('monitored') is not False):
            raise ValueError('Sonarr series was not paused')
        seasons = series.get('seasons')
        if not isinstance(seasons, list):
            raise ValueError('Sonarr season state is missing')
        monitored = {row.get('seasonNumber') for row in seasons
                     if isinstance(row, dict) and row.get('monitored') is True}
        if not set(guarded_seasons).issubset(monitored):
            raise ValueError('guarded seasons would not monitor future episodes')
        if plan.unmonitor_sonarr_ids:
            self.upstream.request_json('PUT', '/api/v3/episode/monitor',
                                       {'episodeIds': list(plan.unmonitor_sonarr_ids),
                                        'monitored': False})
        if plan.monitor_sonarr_ids:
            self.upstream.request_json('PUT', '/api/v3/episode/monitor',
                                       {'episodeIds': list(plan.monitor_sonarr_ids),
                                        'monitored': True})
        rows = self.upstream.request_json(
            'GET', f'/api/v3/episode?seriesId={series_id}')
        if not isinstance(rows, list):
            raise ValueError('Sonarr episode state is missing')
        flags = {row.get('id'): row.get('monitored') for row in rows
                 if isinstance(row, dict)}
        if (any(flags.get(value) is not False for value in plan.unmonitor_sonarr_ids)
                or any(flags.get(value) is not True
                       for value in plan.monitor_sonarr_ids)):
            raise ValueError('Sonarr did not retain exact episode protection')
        series['monitored'] = True
        self.upstream.request_json('PUT', '/api/v3/series', series)
        resumed = self.upstream.request_json('GET', path)
        if (not isinstance(resumed, dict) or resumed.get('monitored') is not True
                or resumed.get('tvdbId') != tvdb_id):
            raise ValueError('Sonarr series did not resume')
        resumed_seasons = resumed.get('seasons')
        if (not isinstance(resumed_seasons, list) or not set(guarded_seasons).issubset(
                {row.get('seasonNumber') for row in resumed_seasons
                 if isinstance(row, dict) and row.get('monitored') is True})):
            raise ValueError('resuming Sonarr disabled future seasons')
        rows = self.upstream.request_json(
            'GET', f'/api/v3/episode?seriesId={series_id}')
        if not isinstance(rows, list):
            raise ValueError('Sonarr post-resume episode state is missing')
        flags = {row.get('id'): row.get('monitored') for row in rows
                 if isinstance(row, dict)}
        if (any(flags.get(value) is not False for value in plan.unmonitor_sonarr_ids)
                or any(flags.get(value) is not True
                       for value in plan.monitor_sonarr_ids)):
            raise ValueError('resuming Sonarr re-monitored an owned episode')

    def add_series(self, payload: dict) -> object:
        with self._lock:
            if self._pending_pause is not None:
                raise ValueError('interrupted Sonarr pause must recover first')
            seasons = guarded_seasons_from_series(payload)
            tvdb_id = payload.get('tvdbId')
            held = [session for session in self._sessions.values()
                    if session.tvdb_id == tvdb_id
                    and session.plan.action == 'ownership_held']
            if held:
                if len(held) != 1:
                    raise ValueError('ambiguous held anime registration')
                return self._recheck_held(held[0], seasons)
            committed = [session for session in self._sessions.values()
                         if session.tvdb_id == tvdb_id
                         and session.plan.action == 'already_present']
            if committed:
                if len(committed) != 1:
                    raise ValueError('ambiguous saved anime request')
                return self._complete_committed_present(committed[0], seasons)
            classification, decision = self.evidence.review(tvdb_id, seasons)
            if classification == 'stock_unverified_show':
                if (type(tvdb_id) is not int or tvdb_id <= 0
                        or not callable(getattr(self.evidence,
                                                'review_registered', None))):
                    raise ValueError('unverified anime lacks exact review')
                lookup = self.upstream.request_json(
                    'GET', f'/api/v3/series/lookup?term=tvdb:{tvdb_id}')
                if (not isinstance(lookup, list) or len(lookup) != 1
                        or not isinstance(lookup[0], dict)
                        or lookup[0].get('tvdbId') != tvdb_id
                        or type(lookup[0].get('tmdbId')) is not int
                        or lookup[0]['tmdbId'] <= 0):
                    raise ValueError('unverified anime lacks exact Sonarr show IDs')
                tmdb_id = lookup[0]['tmdbId']
                supplied = payload.get('tmdbId')
                if supplied is not None and supplied != tmdb_id:
                    raise ValueError('Seerr and Sonarr TMDB show IDs conflict')
                existing_id = lookup[0].get('id')
                if existing_id not in (None, 0):
                    if type(existing_id) is not int or existing_id <= 0:
                        raise ValueError('Sonarr lookup series ID is invalid')
                    existing = self.upstream.request_json(
                        'GET', f'/api/v3/series/{existing_id}')
                    if (not isinstance(existing, dict)
                            or existing.get('id') != existing_id
                            or existing.get('tvdbId') != tvdb_id
                            or existing.get('tmdbId') != tmdb_id
                            or existing.get('monitored') is not True
                            or not isinstance(existing.get('seasons'), list)):
                        raise ValueError('existing Sonarr anime is not verified')
                    rows = self.upstream.request_json(
                        'GET', f'/api/v3/episode?seriesId={existing_id}')
                    classification, decision = self.evidence.review_registered(
                        tvdb_id, tmdb_id, seasons, rows)
                    if (classification != 'guard_existing' or decision is None
                            or decision.action not in {'already_present', 'search_missing'}):
                        raise ValueError('existing anime ownership is incomplete')
                    plan = bind_sonarr_episodes(decision, rows, seasons)
                    flags = {row.get('id'): row.get('monitored')
                             for row in rows if isinstance(row, dict)}
                    watched = {row.get('seasonNumber') for row in existing['seasons']
                               if isinstance(row, dict)
                               and row.get('monitored') is True}
                    if (plan.action != 'already_present'
                            or any(flags.get(item) is not False
                                   for item in plan.unmonitor_sonarr_ids)
                            or not set(seasons).issubset(watched)):
                        raise ValueError('existing anime protection is incomplete')
                    return {'id': existing_id, 'tvdbId': tvdb_id}
                safe = prepare_paused_series_add(payload)
                self._begin_pause(tvdb_id, 0, seasons, 'add')
                response = self.upstream.request_json('POST', '/api/v3/series', safe)
                if (not isinstance(response, dict)
                        or type(response.get('id')) is not int
                        or response['id'] <= 0
                        or response.get('tvdbId') != tvdb_id):
                    raise ValueError('paused Sonarr registration differs')
                series_id = response['id']
                series = self.upstream.request_json(
                    'GET', f'/api/v3/series/{series_id}')
                if (not isinstance(series, dict) or series.get('id') != series_id
                        or series.get('tvdbId') != tvdb_id
                        or series.get('tmdbId') != tmdb_id
                        or series.get('monitored') is not False):
                    raise ValueError('paused Sonarr show identity differs')
                rows = self.upstream.request_json(
                    'GET', f'/api/v3/episode?seriesId={series_id}')
                classification, decision = self.evidence.review_registered(
                    tvdb_id, tmdb_id, seasons, rows)
                if (classification != 'guard_existing' or decision is None
                        or decision.action not in {'already_present', 'search_missing'}):
                    self._record_held(series_id, tvdb_id, rows)
                    if self.unknown_reporter is not None:
                        try:
                            self.unknown_reporter({
                                'operation': 'add', 'tvdb_id': tvdb_id,
                                'tmdb_id': tmdb_id,
                                'risk': 'unlinked_existing_media'})
                        except Exception:
                            # The durable held session remains authoritative;
                            # an audit write failure must not fail Seerr's add.
                            pass
                    return response
                self._record(response, tvdb_id, decision, seasons)
                self._end_pause()
                self.search_series({'name': 'MissingEpisodeSearch',
                                    'seriesId': series_id})
                return response
            if classification == 'stock_new_show':
                safe = prepare_series_add(payload, classification, decision)
                return self.upstream.request_json('POST', '/api/v3/series', safe)
            if any(session.tvdb_id == tvdb_id for session in self._sessions.values()):
                raise ValueError('guarded Sonarr request already pending')
            safe = prepare_series_add(payload, classification, decision)
            self._begin_pause(tvdb_id, 0, seasons, 'add')
            response = self.upstream.request_json('POST', '/api/v3/series', safe)
            self._record(response, tvdb_id, decision, seasons)
            self._end_pause()
            # Seerr's new-series client requests its search inside POST /series.
            # The guarded POST disables that broad search, so dispatch the
            # verified exact search here; Seerr sends no later command on add.
            self.search_series({'name': 'MissingEpisodeSearch',
                                'seriesId': response['id']})
            return response

    def update_series(self, payload: dict) -> object:
        with self._lock:
            if self._pending_pause is not None:
                raise ValueError('interrupted Sonarr pause must recover first')
            seasons = guarded_seasons_from_series(payload)
            tvdb_id = payload.get('tvdbId')
            series_id = payload.get('id')
            held = self._sessions.get(series_id)
            if held is not None and held.plan.action == 'ownership_held':
                if held.tvdb_id != tvdb_id:
                    raise ValueError('held anime retry has different TVDB ID')
                return self._recheck_held(held, seasons)
            if held is not None and held.plan.action == 'already_present':
                if held.tvdb_id != tvdb_id:
                    raise ValueError('saved anime retry has different TVDB ID')
                return self._complete_committed_present(held, seasons)
            classification, decision = self.evidence.review(tvdb_id, seasons)
            if classification == 'stock_unverified_show':
                raise ValueError('unverified anime ownership requires exact episode review')
            if classification != 'guard_existing':
                raise ValueError('existing-series request lacks guard evidence')
            if any(session.tvdb_id == tvdb_id for session in self._sessions.values()):
                raise ValueError('guarded Sonarr request already pending')
            safe = prepare_series_update(payload, seasons, decision)
            self._begin_pause(tvdb_id, payload['id'], seasons, 'update')
            response = self.upstream.request_json('PUT', '/api/v3/series', safe)
            self._record(response, tvdb_id, decision, seasons)
            self._end_pause()
            return response

    def monitor_episodes(self, payload: dict) -> object:
        with self._lock:
            if self._pending_pause is not None:
                raise ValueError('interrupted Sonarr pause must recover first')
            incoming = payload.get('episodeIds') if isinstance(payload, dict) else None
            if not isinstance(incoming, list) or not incoming:
                raise ValueError('episode-monitor request lacks IDs')
            if not all(type(value) is int and value > 0 for value in incoming):
                raise ValueError('episode-monitor request has invalid IDs')
            requested = set(incoming)
            matches = [session for session in self._sessions.values()
                       if requested.issubset(session.episode_ids)]
            if len(matches) != 1:
                raise ValueError('episode-monitor session is missing or ambiguous')
            if matches[0].plan.action == 'ownership_held':
                if payload.get('monitored') is not True:
                    raise ValueError('held anime monitor request differs')
                return {'status': 'suppressed'}
            narrowed = narrow_monitor_request(payload, matches[0].plan)
            if not narrowed['episodeIds']:
                return {'status': 'suppressed'}
            return self.upstream.request_json(
                'PUT', '/api/v3/episode/monitor', narrowed)

    def search_series(self, payload: dict) -> object:
        with self._lock:
            if self._pending_pause is not None:
                raise ValueError('interrupted Sonarr pause must recover first')
            series_id = payload.get('seriesId') if isinstance(payload, dict) else None
            session = self._sessions.get(series_id)
            if session is None:
                raise ValueError('guarded search session is missing')
            if session.plan.action == 'ownership_held':
                if (set(payload) != {'name', 'seriesId'}
                        or payload.get('name') != 'MissingEpisodeSearch'):
                    raise ValueError('held anime search command differs')
                return {'status': 'suppressed'}
            rewritten = rewrite_search_command(payload, series_id, session.plan)
            # Consume the plan before dispatch. A crash here may miss a search
            # but cannot replay it and cause a duplicate acquisition.
            updated = dict(self._sessions)
            updated.pop(series_id)
            self._replace_sessions(updated)
            if rewritten is None:
                return {'status': 'suppressed'}
            result = self.upstream.request_json('POST', '/api/v3/command', rewritten)
            return result
