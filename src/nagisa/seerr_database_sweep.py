"""Bounded private sweep of saved anime-TV requests for database availability."""

from __future__ import annotations

from typing import Any

from nagisa.seerr_availability_sweep import SeerrAvailabilitySweep


class SeerrDatabaseSweep:
    def __init__(self, seerr: Any, reconciler: Any, *, server_id: int,
                 max_per_cycle: int = 5, poll_seconds: int = 60) -> None:
        if (type(server_id) is not int or server_id <= 0
                or type(max_per_cycle) is not int or not 1 <= max_per_cycle <= 10
                or type(poll_seconds) is not int or not 30 <= poll_seconds <= 3600):
            raise ValueError('Seerr database sweep bounds are invalid')
        self.seerr = seerr
        self.reconciler = reconciler
        self.server_id = server_id
        self.max_per_cycle = max_per_cycle
        self.poll_seconds = poll_seconds
        self._priority_cursor = 0
        self._green_cursor = 0
        self._single_turn_priority = True
        self._last = {'eligible': 0, 'examined': 0, 'updated': 0,
                      'would_update': 0, 'unchanged': 0, 'held': 0}

    # Reuse the already-tested, strict request pagination and row validation.
    _requests = SeerrAvailabilitySweep._requests
    _arguments = staticmethod(SeerrAvailabilitySweep._arguments)

    def run_once(self) -> dict[str, int]:
        rows = self._requests()
        eligible = sorted((row for row in rows
                           if row.get('type') == 'tv'
                           and row.get('serverId') == self.server_id
                           and row.get('status') in (2, 5)),
                          key=lambda row: row['id'])
        result = {'eligible': len(eligible), 'examined': 0, 'updated': 0,
                  'would_update': 0, 'unchanged': 0, 'held': 0}
        if eligible:
            def needs_review(row: dict) -> bool:
                media = row.get('media')
                if not isinstance(media, dict) or media.get('status') != 5:
                    return True
                seasons = media.get('seasons')
                return (isinstance(seasons, list)
                        and any(not isinstance(season, dict)
                                or season.get('status') != 5 for season in seasons))

            priority = [row for row in eligible if needs_review(row)]
            green = [row for row in eligible if not needs_review(row)]
            if priority and green and self.max_per_cycle == 1:
                priority_limit = 1 if self._single_turn_priority else 0
                self._single_turn_priority = not self._single_turn_priority
            elif priority and green:
                priority_limit = self.max_per_cycle - max(1, self.max_per_cycle // 4)
            else:
                priority_limit = self.max_per_cycle

            def next_rows(bucket: list[dict], cursor: int, limit: int) -> list[dict]:
                if not bucket or not limit:
                    return []
                start = next((index for index, row in enumerate(bucket)
                              if row['id'] > cursor), 0)
                return (bucket[start:] + bucket[:start])[:limit]

            chosen_priority = next_rows(priority, self._priority_cursor,
                                        priority_limit)
            chosen_green = next_rows(green, self._green_cursor,
                                     self.max_per_cycle - len(chosen_priority))
            chosen = [(row, 'priority') for row in chosen_priority]
            chosen.extend((row, 'green') for row in chosen_green)
            for row, bucket in chosen:
                args = self._arguments(row)
                outcome = 'held' if args is None else self.reconciler.reconcile(**args)
                if outcome not in ('updated', 'would_update', 'unchanged', 'held'):
                    raise ValueError('Seerr database result is invalid')
                result[outcome] += 1
                result['examined'] += 1
                if bucket == 'priority':
                    self._priority_cursor = row['id']
                else:
                    self._green_cursor = row['id']
        self._last = result
        return dict(result)

    def status_snapshot(self) -> dict[str, int]:
        return dict(self._last)
