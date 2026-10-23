"""Bounded, opt-in Seerr availability sweep for already verified anime requests.

The reconciler must run with ``promotions_only=True``. This module never
infers ownership from a Seerr badge and never creates a request or download.
"""

from __future__ import annotations

from typing import Any


class SeerrAvailabilitySweep:
    def __init__(self, seerr: Any, reconciler: Any, *, server_id: int = 1,
                 max_per_cycle: int = 5, poll_seconds: int = 60,
                 expected_version: str | None = None) -> None:
        if (type(server_id) is not int or server_id <= 0
                or type(max_per_cycle) is not int or not 1 <= max_per_cycle <= 10
                or type(poll_seconds) is not int or not 30 <= poll_seconds <= 3600
                or (expected_version is not None and
                    (not isinstance(expected_version, str) or
                     not 1 <= len(expected_version) <= 80 or
                     any(character.isspace() or ord(character) < 32
                         for character in expected_version)))):
            raise ValueError('Seerr availability sweep bounds are invalid')
        if getattr(reconciler, 'promotions_only', None) is not True:
            raise ValueError('Seerr availability sweep must not demote saved media')
        self.seerr = seerr
        self.reconciler = reconciler
        self.server_id = server_id
        self.max_per_cycle = max_per_cycle
        self.poll_seconds = poll_seconds
        self.expected_version = expected_version
        self._priority_cursor = 0
        self._green_cursor = 0
        self._single_turn_priority = True
        self._last = {'eligible': 0, 'examined': 0, 'available': 0,
                      'would_available': 0, 'unchanged': 0, 'held': 0}

    def _requests(self) -> list[dict]:
        rows: list[dict] = []
        total: int | None = None
        for skip in range(0, 2000, 100):
            page = self.seerr.request_json(
                'GET', f'/api/v1/request?take=100&skip={skip}')
            if not isinstance(page, dict) or not isinstance(page.get('pageInfo'), dict):
                raise ValueError('Seerr request page is incomplete')
            count = page['pageInfo'].get('results')
            batch = page.get('results')
            if (type(count) is not int or not 0 <= count <= 2000
                    or not isinstance(batch, list) or len(batch) > 100
                    or (total is not None and total != count)):
                raise ValueError('Seerr request census changed or is invalid')
            total = count
            rows.extend(batch)
            if len(rows) >= total:
                break
        if (total is None or len(rows) != total
                or any(not isinstance(row, dict)
                       or type(row.get('id')) is not int or row['id'] <= 0
                       for row in rows)
                or len({row['id'] for row in rows}) != total):
            raise ValueError('Seerr request census is incomplete or duplicate')
        return rows

    @staticmethod
    def _arguments(row: dict) -> dict | None:
        media = row.get('media')
        seasons = row.get('seasons')
        if (not isinstance(media, dict) or not isinstance(seasons, list)
                or not 1 <= len(seasons) <= 100):
            return None
        values = (media.get('id'), media.get('tmdbId'), media.get('tvdbId'))
        numbers = tuple(item.get('seasonNumber') if isinstance(item, dict)
                        else None for item in seasons)
        if (any(type(value) is not int or value <= 0 for value in values)
                or any(type(value) is not int or value < 0 for value in numbers)
                or len(set(numbers)) != len(numbers)):
            return None
        return {'media_id': values[0], 'tmdb_id': values[1],
                'tvdb_id': values[2], 'seasons': tuple(sorted(numbers)),
                'request_status': row['status']}

    def run_once(self) -> dict[str, int]:
        if self.expected_version is not None:
            status = self.seerr.request_json('GET', '/api/v1/status')
            if (not isinstance(status, dict) or
                    status.get('version') != self.expected_version):
                raise ValueError('Seerr version changed; availability write held')
        rows = self._requests()
        eligible = sorted((row for row in rows
                           if row.get('type') == 'tv'
                           and row.get('serverId') == self.server_id
                           and row.get('status') in (2, 5)),
                          key=lambda row: row['id'])
        if not eligible:
            self._last = {'eligible': 0, 'examined': 0, 'available': 0,
                          'would_available': 0, 'unchanged': 0, 'held': 0}
            return dict(self._last)
        # The stock availability job can demote a formerly available anime
        # when its old Plex/Sonarr owner disappears. Check those saved
        # non-Available badges first, while reserving capacity to audit
        # already-green requests. This affects scheduling only: every write
        # still requires fresh independent Shoko/Plex episode evidence.
        priority = [row for row in eligible
                    if not isinstance(row.get('media'), dict)
                    or row['media'].get('status') != 5]
        green = [row for row in eligible
                 if isinstance(row.get('media'), dict)
                 and row['media'].get('status') == 5]
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
            rotated = bucket[start:] + bucket[:start]
            return rotated[:limit]

        chosen_priority = next_rows(priority, self._priority_cursor, priority_limit)
        chosen_green = next_rows(
            green, self._green_cursor, self.max_per_cycle - len(chosen_priority))
        chosen = [(row, 'priority') for row in chosen_priority]
        chosen.extend((row, 'green') for row in chosen_green)
        outcomes = {'eligible': len(eligible), 'examined': 0,
                    'available': 0, 'would_available': 0,
                    'unchanged': 0, 'held': 0}
        for row, bucket in chosen:
            args = self._arguments(row)
            result = 'held' if args is None else self.reconciler.reconcile(**args)
            if result not in ('available', 'would_available', 'unchanged', 'held'):
                raise ValueError('Seerr availability result is invalid')
            outcomes[result] += 1
            outcomes['examined'] += 1
            if bucket == 'priority':
                self._priority_cursor = row['id']
            else:
                self._green_cursor = row['id']
        self._last = outcomes
        return dict(outcomes)

    def status_snapshot(self) -> dict[str, int]:
        return dict(self._last)
