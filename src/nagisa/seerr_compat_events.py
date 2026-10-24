"""Durable private wake-up queue for the separate stock-Plex link view.

No listener or production scheduler is installed by this module. The existing
Shoko event and recent-file catch-up sources can feed exact file IDs here.
"""

from __future__ import annotations

from collections import OrderedDict
from functools import wraps
import threading
import time
from typing import Any

from .shoko_file_signal import parse_signalr_frames
from .seerr_compat_links import NotRegularTVFile

_MAX_HELD = 8192  # Stays comfortably inside JsonSessionJournal's 1 MiB cap.


def _synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class CompatibilityLinkEvents:
    def __init__(self, reader: Any, store: Any, journal: Any, *,
                 write_enabled: bool = False, max_pending: int = 512,
                 refresh: Any = None,
                 refresh_debounce_seconds: int = 30,
                 movie_events: Any = None,
                 movie_resolver: Any = None,
                 clock: Any = time.time) -> None:
        if (not callable(getattr(reader, 'read', None))
                or not callable(getattr(store, 'upsert', None))
                or not callable(getattr(journal, 'load', None))
                or not callable(getattr(journal, 'save', None))
                or type(write_enabled) is not bool
                or (write_enabled and not callable(refresh))
                or (refresh is not None and not callable(refresh))
                or type(refresh_debounce_seconds) is not int
                or not 0 <= refresh_debounce_seconds <= 3600
                or type(max_pending) is not int or not 1 <= max_pending <= 4096):
            raise ValueError('invalid compatibility event configuration')
        if ((movie_events is None) != (movie_resolver is None)
                or (movie_events is not None and
                    (not callable(movie_resolver)
                     or not all(callable(getattr(movie_events, name, None))
                                for name in ('enqueue', 'run_pending',
                                             'refresh_if_pending'))))):
            raise ValueError('movie compatibility handoff is incomplete')
        if not callable(clock):
            raise ValueError('compatibility retry clock required')
        self.reader = reader
        self.store = store
        self.journal = journal
        self.write_enabled = write_enabled
        self.max_pending = max_pending
        self.clock = clock
        self.refresh = refresh
        self.movie_events = movie_events
        self.movie_resolver = movie_resolver
        self.refresh_debounce_seconds = refresh_debounce_seconds
        self._lock = threading.RLock()
        self._refresh_due = time.monotonic() + refresh_debounce_seconds
        self._refresh_attempted: set[int] = set()
        saved = journal.load()
        if saved:
            rows = saved.get('pending') if isinstance(saved, dict) else None
            held = saved.get('held') if isinstance(saved, dict) else None
            version = saved.get('version') if isinstance(saved, dict) else None
            shows = saved.get('refresh_show_ids') if version == 4 else None
            if (not isinstance(saved, dict)
                    or version not in (3, 4)
                    or set(saved) != ({'version', 'pending', 'held', 'refresh_pending'}
                                      if version == 3 else
                                      {'version', 'pending', 'held',
                                       'refresh_pending', 'refresh_show_ids'})
                    or type(saved['refresh_pending']) is not bool
                    or (version == 4 and
                        (not isinstance(shows, list) or len(shows) > 4096
                         or any(type(item) is not int or item < 0
                                for item in shows)
                         or len(set(shows)) != len(shows)
                         or (0 in shows and len(shows) != 1)
                         or bool(shows) != saved['refresh_pending']))
                    or not isinstance(rows, list)
                    or len(rows) > max_pending
                    or any(not isinstance(row, list) or len(row) != 2
                           or type(row[0]) is not int or row[0] <= 0
                           or row[1] not in ('check', 'deleted')
                           for row in rows)
                    or len({row[0] for row in rows}) != len(rows)
                    or not isinstance(held, list) or len(held) > _MAX_HELD
                    or any(not isinstance(row, list) or len(row) != 4
                           or type(row[0]) is not int or row[0] <= 0
                           or row[1] not in ('check', 'deleted')
                           or type(row[2]) is not int or row[2] <= 0
                           or type(row[3]) not in (int, float)
                           or not 0 <= row[3] < 10**12 for row in held)
                    or len({row[0] for row in held}) != len(held)
                    or {row[0] for row in rows} & {row[0] for row in held}):
                raise ValueError('invalid compatibility pending journal')
            self._pending = OrderedDict((row[0], row[1]) for row in rows)
            self._held = OrderedDict((row[0], (row[1], row[2], row[3]))
                                     for row in held)
            # A v3 journal has only a whole-section refresh intent. Preserve
            # that intent on upgrade rather than guessing the affected show.
            self._refresh_show_ids = ([0] if saved['refresh_pending'] else []) \
                if version == 3 else list(shows)
        else:
            self._pending = OrderedDict()
            self._held = OrderedDict()
            self._refresh_show_ids = []

    def _save(self, pending: OrderedDict[int, str], *,
              held: OrderedDict[int, tuple[str, int, float]] | None = None,
              refresh_show_ids: list[int] | None = None) -> None:
        selected_held = self._held if held is None else held
        selected_shows = (self._refresh_show_ids if refresh_show_ids is None
                          else refresh_show_ids)
        self.journal.save({'version': 4,
                           'pending': [[file_id, action]
                                       for file_id, action in pending.items()],
                           'held': [[file_id, action, attempts, due]
                                    for file_id, (action, attempts, due)
                                    in selected_held.items()],
                           'refresh_pending': bool(selected_shows),
                           'refresh_show_ids': selected_shows})

    def _queue_refresh(self, show_id: int) -> None:
        if type(show_id) is not int or show_id <= 0:
            show_id = 0  # Legacy or test-only proof: preserve a safe full scan.
        if 0 in self._refresh_show_ids or show_id in self._refresh_show_ids:
            return
        shows = [0] if show_id == 0 else self._refresh_show_ids + [show_id]
        if len(shows) > 4096:
            raise ValueError('compatibility refresh queue full')
        self._save(self._pending, refresh_show_ids=shows)
        if not self._refresh_show_ids:
            self._refresh_due = (time.monotonic()
                                 + self.refresh_debounce_seconds)
        self._refresh_show_ids = shows

    def _show_id(self, proof: Any) -> int:
        choose = getattr(self.store, 'proof_show_id', None)
        return choose(proof) if callable(choose) else getattr(proof, 'tvdb_show_id', 0)

    @staticmethod
    def _record_show_id(record: Any) -> int:
        slot = record.get('slot') if isinstance(record, dict) else None
        if isinstance(slot, str):
            head = slot.split('/', 1)[0]
            if head.isdecimal() and int(head) > 0:
                return int(head)
        return 0

    @_synchronized
    def refresh_pending(self) -> bool:
        return bool(self._refresh_show_ids)

    def _defer_refresh(self, show_id: int) -> None:
        rotated = self._refresh_show_ids[1:] + [show_id]
        if rotated != self._refresh_show_ids:
            self._save(self._pending, refresh_show_ids=rotated)
            self._refresh_show_ids = rotated
        self._refresh_attempted.add(show_id)
        if self._refresh_attempted.issuperset(rotated):
            self._refresh_attempted.clear()
            self._refresh_due = time.monotonic() + max(
                30, self.refresh_debounce_seconds)
        else:
            self._refresh_due = time.monotonic()

    @_synchronized
    def refresh_if_pending(self) -> bool:
        if (not self._refresh_show_ids or not self.write_enabled
                or time.monotonic() < self._refresh_due):
            return False
        show_id = self._refresh_show_ids[0]
        try:
            confirmed = self.refresh(show_id) is True
        except Exception:
            self._defer_refresh(show_id)
            raise
        if not confirmed:
            # Plex accepts scans asynchronously. Preserve the show's intent,
            # rotating it behind other shows so one slow scan cannot starve
            # unrelated arrivals.
            self._defer_refresh(show_id)
            return False
        remaining = self._refresh_show_ids[1:]
        self._save(self._pending, refresh_show_ids=remaining)
        self._refresh_show_ids = remaining
        self._refresh_attempted.discard(show_id)
        return True

    @_synchronized
    def pending_ids(self) -> tuple[int, ...]:
        return tuple(self._pending) + tuple(self._held)

    @_synchronized
    def catchup_queued_ids(self) -> tuple[int, ...]:
        """Only immediate work consumes the bounded catch-up queue.

        A later import of a held file must wake it, rather than being
        discarded as an already-queued backlog ID.
        """
        return tuple(self._pending)

    @_synchronized
    def enqueue_exact_file_ids(self, ids: Any) -> None:
        if (not isinstance(ids, (list, tuple)) or len(ids) > self.max_pending
                or any(type(item) is not int or item <= 0 for item in ids)):
            raise ValueError('bounded exact Shoko file IDs required')
        pending = self._pending.copy()
        held = self._held.copy()
        for file_id in ids:
            held.pop(file_id, None)
            pending[file_id] = 'check'
        if len(pending) > self.max_pending:
            raise ValueError('compatibility event queue full')
        self._save(pending, held=held)
        self._pending = pending
        self._held = held

    @_synchronized
    def enqueue_deleted_file_ids(self, ids: Any) -> None:
        """Persist possible missed deletions for the existing proof path."""
        if (not isinstance(ids, (list, tuple)) or len(ids) > self.max_pending
                or any(type(item) is not int or item <= 0 for item in ids)):
            raise ValueError('bounded exact deleted file IDs required')
        pending = self._pending.copy()
        held = self._held.copy()
        for file_id in ids:
            held.pop(file_id, None)
            pending[file_id] = 'deleted'
        if len(pending) > self.max_pending:
            raise ValueError('compatibility deletion queue full')
        self._save(pending, held=held)
        self._pending = pending
        self._held = held

    @_synchronized
    def ingest(self, frame: str) -> None:
        events = parse_signalr_frames(frame)
        pending = self._pending.copy()
        held = self._held.copy()
        for subject, file_id in events:
            held.pop(file_id, None)
            pending[file_id] = 'deleted' if subject == 'deleted' else 'check'
        if len(pending) > self.max_pending:
            raise ValueError('compatibility event queue full')
        self._save(pending, held=held)
        self._pending = pending
        self._held = held

    def _defer(self, file_id: int, action: str) -> None:
        pending = self._pending.copy()
        pending.pop(file_id, None)
        held = self._held.copy()
        attempts = held[file_id][1] + 1 if file_id in held else 1
        delay = min(30 * 2 ** min(attempts - 1, 10), 21600)
        held[file_id] = (action, attempts, self.clock() + delay)
        if len(held) > _MAX_HELD:
            raise ValueError('compatibility held-file journal full')
        self._save(pending, held=held)
        self._pending, self._held = pending, held

    def _resolve_contested_slot(self, proof: Any | None, *,
                                slot_override: tuple[int, int, int] | None = None) -> str | None:
        records_for = getattr(self.store, 'contested_records', None)
        if not callable(records_for):
            return None
        if proof is None:
            if slot_override is None:
                raise ValueError('contested slot required for deleted file')
            current_slot = None
        elif all(hasattr(proof, key) for key in
                 ('tvdb_show_id', 'season', 'episode', 'shoko_file_id')):
            current_slot = (self._show_id(proof), proof.season, proof.episode)
        else:
            return None
        slot = current_slot if slot_override is None else slot_override
        records = records_for(*slot)
        if not records:
            return None
        add_claim = getattr(self.store, 'add_contested_claim', None)
        resolve = getattr(self.store, 'resolve_contest', None)
        if not callable(add_claim) or not callable(resolve):
            raise ValueError('contested-slot recovery unavailable')
        if proof is not None and slot == current_slot:
            add_claim(proof)
        records = records_for(*slot)
        ids = tuple(row['file_id'] for row in records)
        if (not isinstance(records, tuple) or not 2 <= len(ids) <= 50
                or ids != tuple(sorted(set(ids)))
                or any(type(row.get('series_id')) is not int
                       or row['series_id'] <= 0
                       or type(row.get('shoko_episode_id')) is not int
                       or row['shoko_episode_id'] <= 0 for row in records)):
            raise ValueError('contested Shoko claim census changed')
        live = []
        changed = []
        confirm_removed = getattr(self.reader, 'confirm_removed', None)
        for row in records:
            file_id = row['file_id']
            try:
                current = self.reader.read(file_id)
            except NotRegularTVFile:
                changed.append(file_id)
                continue
            except (ValueError, OSError):
                if (not callable(confirm_removed)
                        or confirm_removed(file_id, row['series_id'])
                        is not True):
                    raise ValueError('contested claimant remains uncertain')
                continue
            if (getattr(current, 'shoko_file_id', None) != file_id
                    or getattr(current, 'shoko_series_id', None)
                    != row['series_id']):
                raise ValueError('contested claimant file identity changed')
            if (getattr(current, 'shoko_episode_id', None)
                    != row['shoko_episode_id']):
                raise ValueError('contested claimant episode identity changed')
            if (self._show_id(current), current.season,
                    current.episode) != slot:
                changed.append(file_id)
            else:
                live.append(current)
        identities = {(
            item.shoko_episode_id, item.shoko_series_id,
            item.anidb_episode_id, item.tmdb_episode_id,
            item.tvdb_episode_id, item.tmdb_show_id)
            for item in live}
        if not identities:
            clear = getattr(self.store, 'clear_empty_contest', None)
            if not callable(clear):
                raise ValueError('empty contested-slot recovery unavailable')
            current_file_id = proof.shoko_file_id if proof is not None else -1
            to_recheck = sorted(set(changed) - {current_file_id})
            if to_recheck:
                self.enqueue_exact_file_ids(to_recheck)
            self._queue_refresh(slot[0])
            clear(*slot, expected_file_ids=ids)
            return 'contest-cleared'
        if len(identities) != 1:
            raise ValueError('contested episode remains ambiguous')
        survivor = min(live, key=lambda item: item.shoko_file_id)
        current_file_id = proof.shoko_file_id if proof is not None else -1
        to_recheck = sorted(set(changed) - {current_file_id})
        if to_recheck:
            self.enqueue_exact_file_ids(to_recheck)
        self._queue_refresh(slot[0])
        return resolve(survivor, expected_file_ids=ids)

    @_synchronized
    def run_pending(self, *, limit: int = 50) -> list[tuple[int, str]]:
        if type(limit) is not int or not 1 <= limit <= self.max_pending:
            raise ValueError('bounded compatibility work limit required')
        results = []
        work = list(self._pending.items())[:limit]
        if len(work) < limit:
            work.extend((file_id, row[0]) for file_id, row in self._held.items()
                        if row[2] <= self.clock())
            work = work[:limit]
        for file_id, action in work:
            if action == 'deleted':
                # The event alone cannot establish an authoritative removal.
                # Only the exact Shoko 404 plus absence from both complete
                # public Plex owner reads may remove our own link.
                newly_queued_alternates: tuple[int, ...] = ()
                held_before_promotion = None
                try:
                    if not self.write_enabled:
                        outcome = 'deletion-report-only'
                    else:
                        lookup = getattr(self.store, 'lookup_file', None)
                        confirm = getattr(self.reader, 'confirm_removed', None)
                        remove = getattr(self.store, 'remove', None)
                        if not all(callable(item) for item in
                                   (lookup, confirm, remove)):
                            raise ValueError('removal verifier unavailable')
                        record = lookup(file_id)
                        if record is None:
                            outcome = 'no-owned-link'
                            slots_for_file = getattr(
                                self.store, 'contested_slots_for_file', None)
                            contested_slots = (slots_for_file(file_id)
                                               if callable(slots_for_file) else ())
                            if contested_slots:
                                outcomes = [self._resolve_contested_slot(
                                    None, slot_override=slot)
                                    for slot in contested_slots]
                                outcome = ('contest-resolved'
                                           if 'created' in outcomes
                                           else 'contest-cleared')
                            elif self.movie_events is not None:
                                enqueue_deleted = getattr(
                                    self.movie_events,
                                    'enqueue_file_deletion', None)
                                if not callable(enqueue_deleted):
                                    raise ValueError(
                                        'movie deletion handoff unavailable')
                                if enqueue_deleted(file_id) is not None:
                                    outcome = 'queued-movie-deletion'
                        elif confirm(file_id, record['series_id']) is True:
                            alternates = getattr(self.reader, 'alternate_file_ids', None)
                            if not callable(alternates):
                                raise ValueError('alternate-file verifier unavailable')
                            candidates = alternates(
                                record['series_id'], record['anidb_episode_id'],
                                exclude_file_id=file_id)
                            if (not isinstance(candidates, tuple)
                                    or len(candidates) > 50
                                    or len(set(candidates)) != len(candidates)
                                    or any(type(candidate) is not int
                                           or candidate <= 0 or candidate == file_id
                                           for candidate in candidates)):
                                raise ValueError('alternate-file census is ambiguous')
                            if candidates:
                                # Durable intent precedes removing the old
                                # link. After a crash, deletion remains ahead
                                # of the alternate checks in this journal.
                                pending_with_alternates = self._pending.copy()
                                held_before_promotion = self._held.copy()
                                held_without_alternates = self._held.copy()
                                newly_queued_alternates = tuple(
                                    candidate for candidate in candidates
                                    if candidate not in pending_with_alternates)
                                for candidate in candidates:
                                    pending_with_alternates.setdefault(
                                        candidate, 'check')
                                    held_without_alternates.pop(candidate, None)
                                if len(pending_with_alternates) > self.max_pending:
                                    raise ValueError('alternate queue is full')
                                self._save(pending_with_alternates,
                                           held=held_without_alternates)
                                self._pending = pending_with_alternates
                                self._held = held_without_alternates
                            self._queue_refresh(self._record_show_id(record))
                            outcome = ('removed' if remove(file_id, confirmed=True)
                                       else 'no-owned-link')
                        else:
                            raise ValueError('authoritative removal not proven')
                except (ValueError, OSError):
                    if held_before_promotion is not None:
                        pending = self._pending.copy()
                        for candidate in newly_queued_alternates:
                            pending.pop(candidate, None)
                        self._save(pending, held=held_before_promotion)
                        self._pending = pending
                        self._held = held_before_promotion
                    self._defer(file_id, action)
                    results.append((file_id, 'deletion-held'))
                    continue
                pending = self._pending.copy()
                # A contested claim may have been positively reclassified
                # while this deletion notice was processed. Preserve its
                # newly queued check instead of acknowledging that work.
                if pending.get(file_id) == action:
                    pending.pop(file_id, None)
                held = self._held.copy()
                held.pop(file_id, None)
                self._save(pending, held=held)
                self._pending = pending
                self._held = held
                results.append((file_id, outcome))
                continue
            try:
                try:
                    proof = self.reader.read(file_id)
                except ValueError:
                    # A public Plex delay can make the full proof fail after
                    # Shoko has positively remapped an owned TMDB episode.
                    # Withdraw only that ledger-owned link; never infer a new
                    # slot until the ordinary complete proof succeeds.
                    lookup = getattr(self.store, 'lookup_file', None)
                    changed = getattr(self.reader,
                                      'confirm_native_mapping_changed', None)
                    remove = getattr(self.store, 'remove', None)
                    owned = (lookup(file_id) if self.write_enabled
                             and callable(lookup) else None)
                    if (owned is None or not callable(changed)
                            or changed(file_id, owned) is not True
                            or not callable(remove)):
                        raise
                    self._queue_refresh(self._record_show_id(owned))
                    if not remove(file_id, confirmed=True):
                        raise ValueError('owned native mapping link disappeared')
                    self._defer(file_id, action)
                    results.append((file_id, 'identity-withdrawn'))
                    continue
                if (hasattr(proof, 'shoko_file_id')
                        and proof.shoko_file_id != file_id):
                    raise ValueError('event proof belongs to another file')
                if self.write_enabled:
                    lookup_file = getattr(self.store, 'lookup_file', None)
                    remove = getattr(self.store, 'remove', None)
                    if not callable(lookup_file) or not callable(remove):
                        raise ValueError('owned-file verifier unavailable')
                    old = lookup_file(file_id)
                    if old is not None:
                        identity = (
                            ('series_id', 'shoko_series_id'),
                            ('anidb_episode_id', 'anidb_episode_id'),
                            ('tmdb_episode_id', 'tmdb_episode_id'),
                            ('tvdb_episode_id', 'tvdb_episode_id'),
                            ('tmdb_show_id', 'tmdb_show_id'))
                        if (not all(key in old for key, _ in identity)
                                or 'slot' not in old):
                            raise ValueError('owned-file identity incomplete')
                        new_slot = (f'{self._show_id(proof)}/'
                                    f'{proof.season}/{proof.episode}')
                        changed_identity = (
                            old['slot'] != new_slot
                            or any(old[key] != getattr(proof, attr)
                                   for key, attr in identity))
                        if changed_identity:
                            # Persist the refresh intent before withdrawing
                            # the old link. A crash then cannot leave a stale
                            # Plex card without a scheduled scan.
                            self._queue_refresh(self._record_show_id(old))
                            if not remove(file_id, confirmed=True):
                                raise ValueError('owned link disappeared')
                            self._defer(file_id, action)
                            results.append((file_id, 'identity-withdrawn'))
                            continue
                    slots_for_file = getattr(
                        self.store, 'contested_slots_for_file', None)
                    if callable(slots_for_file) and hasattr(proof, 'shoko_file_id'):
                        current_slot = (self._show_id(proof),
                                        proof.season, proof.episode)
                        for old_slot in slots_for_file(file_id):
                            if old_slot != current_slot:
                                self._resolve_contested_slot(
                                    proof, slot_override=old_slot)
                    contested_outcome = self._resolve_contested_slot(proof)
                    if contested_outcome is not None:
                        pending = self._pending.copy()
                        pending.pop(file_id, None)
                        held = self._held.copy()
                        held.pop(file_id, None)
                        self._save(pending, held=held)
                        self._pending, self._held = pending, held
                        results.append((file_id, contested_outcome))
                        continue
                    lookup_slot = getattr(self.store, 'lookup_slot', None)
                    if not callable(lookup_slot):
                        raise ValueError('occupied-slot verifier unavailable')
                    # The initial export selects one file version. A later
                    # copy of that same episode must not replace it or spin
                    # forever in the retry queue; a different episode may
                    # never take the existing availability slot.
                    if all(hasattr(proof, key) for key in (
                            'tvdb_show_id', 'season', 'episode',
                            'shoko_series_id', 'anidb_episode_id',
                            'tmdb_episode_id', 'tmdb_show_id')):
                        occupied = lookup_slot(
                            self._show_id(proof), proof.season, proof.episode)
                    else:
                        occupied = None
                    if occupied is not None and occupied['file_id'] != file_id:
                        same = all(key in occupied
                                   and occupied[key] == getattr(proof, value, None)
                                   for key, value in (
                                       ('series_id', 'shoko_series_id'),
                                       ('anidb_episode_id', 'anidb_episode_id'),
                                       ('tmdb_episode_id', 'tmdb_episode_id'),
                                       ('tvdb_episode_id', 'tvdb_episode_id'),
                                       ('tmdb_show_id', 'tmdb_show_id')))
                        incumbent_proof = self.reader.read(occupied['file_id'])
                        if (getattr(incumbent_proof, 'shoko_file_id', None)
                                != occupied['file_id']
                                or occupied.get('slot') != (
                                    f'{self._show_id(incumbent_proof)}/'
                                    f'{incumbent_proof.season}/'
                                    f'{incumbent_proof.episode}')
                                or any(getattr(incumbent_proof, value, None)
                                       != occupied[key] for key, value in (
                                           ('series_id', 'shoko_series_id'),
                                           ('anidb_episode_id', 'anidb_episode_id'),
                                           ('tmdb_episode_id', 'tmdb_episode_id'),
                                           ('tvdb_episode_id', 'tvdb_episode_id'),
                                           ('tmdb_show_id', 'tmdb_show_id')))):
                            raise ValueError('incumbent file proof changed')
                        incumbent_episode = getattr(
                            incumbent_proof, 'shoko_episode_id', None)
                        newcomer_episode = getattr(
                            proof, 'shoko_episode_id', None)
                        if (type(incumbent_episode) is not int
                                or incumbent_episode <= 0
                                or type(newcomer_episode) is not int
                                or newcomer_episode <= 0):
                            raise ValueError('exact Shoko episode IDs required')
                        if not same or incumbent_episode != newcomer_episode:
                            contest = getattr(self.store,
                                              'mark_contested', None)
                            if not callable(contest):
                                raise ValueError('durable contested-slot store unavailable')
                            self._queue_refresh(self._show_id(proof))
                            contest(incumbent_proof, proof)
                            raise ValueError('compatibility slot contested by another episode')
                        outcome = 'alternate-version'
                    else:
                        if (old is None or old.get('target') !=
                                getattr(proof, 'plex_target', None)):
                            self._queue_refresh(self._show_id(proof))
                        outcome = self.store.upsert(proof)
                else:
                    outcome = 'verified-report-only'
            except NotRegularTVFile as non_tv:
                lookup = getattr(self.store, 'lookup_file', None)
                try:
                    movie_id = (self.movie_resolver(file_id)
                                if self.movie_resolver is not None else None)
                    if movie_id is not None and (
                            type(movie_id) is not int or movie_id <= 0):
                        raise ValueError('exact movie identity required')
                except (ValueError, OSError):
                    self._defer(file_id, action)
                    results.append((file_id, 'held'))
                    continue
                if self.write_enabled:
                    remove = getattr(self.store, 'remove', None)
                    try:
                        if not callable(lookup) or not callable(remove):
                            raise ValueError('owned-file removal unavailable')
                        owned = lookup(file_id)
                        if owned is not None:
                            self._queue_refresh(self._record_show_id(owned))
                            if not remove(file_id, confirmed=True):
                                raise ValueError('owned link disappeared')
                    except (ValueError, OSError):
                        self._defer(file_id, action)
                        results.append((file_id, 'held'))
                        continue
                if (self.movie_events is not None
                        and non_tv.kind == 'Movie'
                        and movie_id is None):
                    # A true movie may gain its provider link after import.
                    # Withdraw any prior TV link above, but retain this file
                    # until the exact movie identity can be read and queued.
                    self._defer(file_id, action)
                    results.append((file_id, 'held'))
                    continue
                if movie_id is not None:
                    try:
                        # The movie queue persists this exact film before
                        # the Shoko file event is acknowledged below.
                        self.movie_events.enqueue(movie_id)
                    except (ValueError, OSError):
                        self._defer(file_id, action)
                        results.append((file_id, 'held'))
                        continue
                    outcome = 'queued-movie'
                else:
                    outcome = 'excluded-non-tv'
            except (ValueError, OSError):
                self._defer(file_id, action)
                results.append((file_id, 'held'))
                continue
            pending = self._pending.copy()
            pending.pop(file_id, None)
            held = self._held.copy()
            held.pop(file_id, None)
            self._save(pending, held=held)
            self._pending = pending
            self._held = held
            results.append((file_id, outcome))
        return results

    def review_one(self) -> tuple[int, str] | None:
        """Match the existing supervised Shoko worker's bounded interface."""
        rows = self.run_pending(limit=1)
        self.refresh_if_pending()
        movie_rows = []
        if self.movie_events is not None:
            movie_rows = self.movie_events.run_pending(limit=1)
            self.movie_events.refresh_if_pending()
        return rows[0] if rows else (movie_rows[0] if movie_rows else None)
