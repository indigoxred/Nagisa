"""Journaled one-time ID export into the private compatibility-link store."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .seerr_compat_links import NotRegularTVFile


class CompatibilityInitialExport:
    def __init__(self, reader: Any, store: Any, journal: Any, *,
                 write_enabled: bool = False, refresh: Any = None,
                 refresh_show: Any = None) -> None:
        if (not callable(getattr(reader, 'read', None))
                or not callable(getattr(store, 'upsert', None))
                or not callable(getattr(journal, 'load', None))
                or not callable(getattr(journal, 'save', None))
                or type(write_enabled) is not bool
                or (refresh is not None and not callable(refresh))
                or (refresh_show is not None and not callable(refresh_show))
                or (refresh is not None and refresh_show is not None)
                or (write_enabled and refresh is None
                    and refresh_show is None)):
            raise ValueError('invalid initial compatibility export')
        self.reader = reader
        self.store = store
        self.journal = journal
        self.write_enabled = write_enabled
        self.refresh = refresh
        self.refresh_show = refresh_show
        self._preflight_by_file = {}
        self._preflight_by_slot = {}
        if self._journal_exists():
            self._load_preflight(self._state())

    def _journal_exists(self) -> bool:
        path = getattr(self.journal, 'path', None)
        return ((isinstance(path, Path) and path.exists())
                or bool(self.journal.load()))

    def _show_id(self, proof: Any) -> int:
        choose = getattr(self.store, 'proof_show_id', None)
        return choose(proof) if callable(choose) else proof.tvdb_show_id

    def _state(self) -> dict:
        state = self.journal.load()
        version = state.get('version') if isinstance(state, dict) else None
        expected_keys = {'version', 'ids', 'next', 'held',
                         'refresh_pending', 'preflight'}
        if version == 3:
            expected_keys.add('refresh_shows')
        if (not isinstance(state, dict) or set(state) != expected_keys
                or version != (3 if self.refresh_show is not None else 2)):
            raise ValueError('initial compatibility export not initialized')
        ids, held = state['ids'], state['held']
        preflight = state['preflight']
        valid_ids = (isinstance(ids, list)
                     and all(type(item) is int for item in ids))
        id_set = set(ids) if valid_ids else set()
        processed_ids = (set(ids[:state['next']])
                         if valid_ids
                         and type(state['next']) is int
                         and 0 <= state['next'] <= len(ids) else set())
        shows = state.get('refresh_shows', [])
        if (not isinstance(ids, list) or len(ids) > 32768
                or any(type(item) is not int or item <= 0 for item in ids)
                or any(a >= b for a, b in zip(ids, ids[1:]))
                or type(state['next']) is not int
                or not 0 <= state['next'] <= len(ids)
                or not isinstance(held, list)
                or len(set(held)) != len(held)
                or any(item not in processed_ids for item in held)
                or type(state['refresh_pending']) is not bool
                or not isinstance(preflight, list)
                or len(preflight) > len(ids)
                or any(not isinstance(row, list) or len(row) != 5
                       or any(type(value) is not int for value in row)
                       or row[0] <= 0 or row[1] <= 0 or row[2] < 0
                       or row[3] <= 0 or row[4] <= 0
                       for row in preflight)
                or any(a[0] >= b[0] for a, b in zip(preflight, preflight[1:]))
                or any(row[0] not in id_set for row in preflight)):
            raise ValueError('initial compatibility export journal changed')
        if version == 3 and (not isinstance(shows, list)
                             or len(shows) > len(preflight)
                             or any(type(show) is not int or show <= 0
                                    for show in shows)
                             or len(set(shows)) != len(shows)
                             or state['refresh_pending'] != bool(shows)
                             or not set(shows).issubset(
                                 {row[1] for row in preflight})):
            raise ValueError('initial per-show refresh journal changed')
        return state

    def _load_preflight(self, state: dict) -> None:
        by_slot = {}
        self._preflight_by_file = {}
        for file_id, show, season, episode, shoko_episode in state['preflight']:
            identity = (show, season, episode, shoko_episode)
            self._preflight_by_file[file_id] = identity
            by_slot.setdefault(identity[:3], []).append((file_id, shoko_episode))
        self._preflight_by_slot = {}
        for slot, claims in by_slot.items():
            episodes = {episode for _, episode in claims}
            self._preflight_by_slot[slot] = (
                None if len(episodes) != 1
                else (claims[0][0], claims[0][1]))

    def begin(self, ids: tuple[int, ...]) -> None:
        if (not isinstance(ids, tuple) or len(ids) > 32768
                or any(type(item) is not int or item <= 0 for item in ids)
                or any(a >= b for a, b in zip(ids, ids[1:]))):
            raise ValueError('stable ascending Shoko IDs required')
        if self._journal_exists():
            state = self._state()
            if state['ids'] != list(ids):
                raise ValueError('initial Shoko ID census changed')
            self._load_preflight(state)
            return
        # Read the whole census before any link can be written. Shoko can give
        # distinct AniDB episodes one TVDB slot, so first-file-wins is unsafe.
        preflight = []
        for file_id in ids:
            try:
                proof = self.reader.read(file_id)
            except (NotRegularTVFile, ValueError, OSError):
                continue
            preflight.append([file_id, self._show_id(proof), proof.season,
                              proof.episode, proof.shoko_episode_id])
        state = {'version': 3 if self.refresh_show is not None else 2,
                 'ids': list(ids), 'next': 0,
                 'held': [], 'refresh_pending': False,
                 'preflight': preflight}
        if self.refresh_show is not None:
            state['refresh_shows'] = []
        self.journal.save(state)
        self._load_preflight(state)

    def begin_or_resume(self, census: Any) -> str:
        """Keep the first stable ID snapshot across later Shoko arrivals."""
        if not callable(getattr(census, 'read_ids', None)):
            raise ValueError('stable Shoko initial census required')
        if self._journal_exists():
            self._load_preflight(self._state())
            return 'resumed'
        self.begin(census.read_ids())
        return 'started'

    def _refresh_once(self) -> None:
        state = self._state()
        # Initial export can span many bounded batches and restarts. Keep one
        # durable refresh intent, then scan the stock Plex section only after
        # the complete stable ID census has been processed.
        if (not state['refresh_pending'] or not self.write_enabled
                or state['next'] != len(state['ids'])):
            return
        if self.refresh_show is not None:
            show_id = state['refresh_shows'][0]
            try:
                confirmed = self.refresh_show(show_id) is True
            except (ValueError, OSError) as error:
                # Keep this show's proof pending while allowing later shows
                # to make progress. The exact ID stays visible in the journal.
                logging.getLogger(__name__).warning(
                    'Private Plex show %s refresh held: %s', show_id, error)
                confirmed = False
            if not confirmed:
                if len(state['refresh_shows']) > 1:
                    state['refresh_shows'] = (state['refresh_shows'][1:]
                                              + [show_id])
                    self.journal.save(state)
                return
            state['refresh_shows'].pop(0)
            state['refresh_pending'] = bool(state['refresh_shows'])
            self.journal.save(state)
        else:
            self.refresh()
            state['refresh_pending'] = False
            self.journal.save(state)

    def _queue_refresh(self, state: dict, file_id: int) -> None:
        if self.refresh_show is None:
            state['refresh_pending'] = True
            return
        identity = self._preflight_by_file.get(file_id)
        if identity is None:
            raise ValueError('written file lacks preflight identity')
        show_id = identity[0]
        if show_id not in state['refresh_shows']:
            state['refresh_shows'].append(show_id)
        state['refresh_pending'] = True

    def _one(self, file_id: int) -> str:
        try:
            proof = self.reader.read(file_id)
            identity = (self._show_id(proof), proof.season, proof.episode,
                        proof.shoko_episode_id)
            expected = self._preflight_by_file.get(file_id)
            # A file that was unqualified during the full census cannot join
            # later: another not-yet-rechecked episode may claim its slot.
            # A fresh complete census must establish its ownership first.
            if expected is None or expected != identity:
                return 'held'
            owner = self._preflight_by_slot.get(identity[:3])
            if owner is None or owner[1] != proof.shoko_episode_id:
                return 'held'
            if owner[0] != file_id:
                return 'duplicate-version'
            return (self.store.upsert(proof) if self.write_enabled
                    else 'verified-report-only')
        except NotRegularTVFile:
            lookup = getattr(self.store, 'lookup_file', None)
            if self.write_enabled and (not callable(lookup)
                                       or lookup(file_id) is not None):
                return 'held'
            return 'excluded-non-tv'
        except (ValueError, OSError):
            return 'held'

    def run(self, *, limit: int = 100) -> list[tuple[int, str]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('bounded initial export work limit required')
        state = self._state()
        results = []
        for _ in range(limit):
            if state['next'] == len(state['ids']):
                break
            file_id = state['ids'][state['next']]
            outcome = self._one(file_id)
            state['next'] += 1
            if outcome == 'held':
                state['held'].append(file_id)
            elif self.write_enabled and outcome not in (
                    'excluded-non-tv', 'duplicate-version'):
                # A crash before the batch save replays a bounded number of
                # idempotent links and still schedules the stock Plex scan.
                self._queue_refresh(state, file_id)
            results.append((file_id, outcome))
        if results:
            self.journal.save(state)
        self._refresh_once()
        return results

    def retry_held(self, *, limit: int = 100) -> list[tuple[int, str]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('bounded held-ID retry limit required')
        state = self._state()
        results = []
        for file_id in state['held'][:limit]:
            outcome = self._one(file_id)
            if outcome != 'held':
                state['held'].remove(file_id)
                if self.write_enabled and outcome not in (
                        'excluded-non-tv', 'duplicate-version'):
                    self._queue_refresh(state, file_id)
            results.append((file_id, outcome))
        if any(outcome != 'held' for _, outcome in results):
            self.journal.save(state)
        self._refresh_once()
        return results

    def status(self) -> dict:
        state = self._state()
        return {'total': len(state['ids']), 'processed': state['next'],
                'held': tuple(state['held']),
                'refresh_pending': state['refresh_pending'],
                'complete': (state['next'] == len(state['ids'])
                             and not state['held']
                             and not state['refresh_pending'])}
