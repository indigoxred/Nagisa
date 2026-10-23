"""Private evidence-to-database bridge; not deployed in running Nagisa."""

from __future__ import annotations

from typing import Any


class SeerrDatabaseReconciler:
    def __init__(self, seerr: Any, evidence: Any, database: Any) -> None:
        self.seerr = seerr
        self.evidence = evidence
        self.database = database

    def reconcile(self, *, media_id: int, tmdb_id: int, tvdb_id: int,
                  seasons: tuple[int, ...], request_status: int) -> str:
        if (request_status not in (2, 5) or type(request_status) is not int
                or any(type(value) is not int or value <= 0
                       for value in (media_id, tmdb_id, tvdb_id))
                or not isinstance(seasons, tuple) or not 1 <= len(seasons) <= 100
                or any(type(number) is not int or number < 0
                       for number in seasons)
                or len(set(seasons)) != len(seasons)):
            return 'held'
        try:
            show = self.seerr.request_json('GET', f'/api/v1/tv/{tmdb_id}')
            media = show.get('mediaInfo') if isinstance(show, dict) else None
            if (not isinstance(show, dict) or show.get('id') != tmdb_id
                    or not isinstance(media, dict)
                    or media.get('id') != media_id
                    or media.get('tmdbId') != tmdb_id
                    or media.get('tvdbId') != tvdb_id
                    or type(media.get('status')) is not int
                    or media['status'] not in range(1, 8)):
                return 'held'
            saved = media.get('seasons')
            if (not isinstance(saved, list) or not len(seasons) <= len(saved) <= 100
                    or any(not isinstance(row, dict)
                           or type(row.get('seasonNumber')) is not int
                           or type(row.get('status')) is not int
                           or row['status'] not in range(1, 8)
                           for row in saved)):
                return 'held'
            expected = {row['seasonNumber']: row['status'] for row in saved}
            if len(expected) != len(saved) or not set(seasons).issubset(expected):
                return 'held'
            desired = {}
            for season in sorted(expected):
                classification, decision = self.evidence.review(tvdb_id, (season,))
                if classification != 'guard_existing' or decision is None:
                    return 'held'
                if (decision.action == 'already_present'
                        and decision.present_tvdb_ids):
                    desired[season] = 5
                elif (decision.action == 'search_missing'
                      and decision.missing_tvdb_ids
                      and decision.present_tvdb_ids
                      and decision.missing_seasons == (season,)):
                    desired[season] = 4
                else:
                    # No verified playable episode and uncertain mappings are
                    # not permission to publish a false badge or search.
                    return 'held'
        except (KeyError, TypeError, ValueError, RuntimeError, OSError):
            return 'held'
        # A changed Seerr version/schema must reach Nagisa's module health,
        # rather than being mistaken for an ordinary uncertain episode.
        return self.database.apply(media_id=media_id, tmdb_id=tmdb_id,
            tvdb_id=tvdb_id, expected_parent=media['status'],
            expected_seasons=expected, desired_seasons=desired)
