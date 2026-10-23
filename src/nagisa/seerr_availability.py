"""Private Seerr availability candidate using stock status and evidence APIs.

Only a positively identified, complete Seerr season census can change its
saved media status. This is not scheduled or configured in running Nagisa.
"""

from __future__ import annotations

from typing import Any


class NativeSeerrAvailability:
    _STATUS = {'processing': 3, 'partial': 4, 'available': 5}

    @staticmethod
    def _available_seasons(media: dict, seasons: tuple[int, ...]) -> bool:
        rows = media.get('seasons')
        if not isinstance(rows, list):
            return False
        return all(sum(isinstance(row, dict)
                       and row.get('seasonNumber') == number
                       and row.get('status') == 5 for row in rows) == 1
                   for number in seasons)

    def __init__(self, seerr: Any, evidence: Any, *, promotions_only: bool = False,
                 write_enabled: bool = True) -> None:
        self.seerr = seerr
        self.evidence = evidence
        self.promotions_only = promotions_only
        self.write_enabled = write_enabled

    def reconcile(self, *, media_id: int, tmdb_id: int, tvdb_id: int,
                  seasons: tuple[int, ...], request_status: int) -> str:
        if (request_status not in (2, 5) or type(request_status) is not int
                or any(type(value) is not int or value <= 0
                       for value in (media_id, tmdb_id, tvdb_id))
                or not isinstance(seasons, tuple) or not seasons
                or len(seasons) > 100
                or any(type(value) is not int or value < 0 for value in seasons)
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
            classification, decision = self.evidence.review(tvdb_id, seasons)
            if classification != 'guard_existing' or decision is None:
                return 'held'
            if decision.action == 'already_present' and decision.present_tvdb_ids:
                requested_available=self._available_seasons(media,seasons)
                saved_seasons = media.get('seasons')
                if (not isinstance(saved_seasons, list)
                        or any(not isinstance(row, dict)
                               or type(row.get('seasonNumber')) is not int
                               or type(row.get('status')) is not int
                               or row['status'] not in range(1, 8)
                               for row in saved_seasons)
                        or len({row['seasonNumber'] for row in saved_seasons})
                        != len(saved_seasons)
                        or not set(seasons).issubset(
                            {row['seasonNumber'] for row in saved_seasons})):
                    return 'unchanged' if requested_available else 'held'
                all_saved=tuple(sorted(row['seasonNumber'] for row in saved_seasons))
                if (requested_available and media['status']==self._STATUS['available']
                        and self._available_seasons(media,all_saved)):
                    return 'unchanged'
                promote_seasons=seasons
                if any(number not in seasons for number in all_saved):
                    # The stock endpoint promotes the parent as well as the
                    # selected seasons. Prove every saved season, including
                    # those with an existing green badge, before promotion.
                    whole_class,whole_decision=self.evidence.review(tvdb_id,all_saved)
                    if (whole_class!='guard_existing' or whole_decision is None
                            or whole_decision.action!='already_present'
                            or whole_decision.missing_tvdb_ids
                            or whole_decision.missing_seasons
                            or len(set(whole_decision.present_tvdb_ids))<len(all_saved)):
                        return 'unchanged' if requested_available else 'held'
                    promote_seasons=all_saved
                status = 'available'
                body = {'is4k': False, 'seasons': [
                    {'seasonNumber': season} for season in promote_seasons]}
            elif (not self.promotions_only and decision.action == 'search_missing'
                  and decision.missing_tvdb_ids):
                status = 'partial' if decision.present_tvdb_ids else 'processing'
                body = {'is4k': False}
            else:
                return 'held'
            if status != 'available':
                saved_seasons = media.get('seasons')
                if not isinstance(saved_seasons, list):
                    return 'held'
                available = {row.get('seasonNumber') for row in saved_seasons
                             if isinstance(row, dict)
                             and row.get('seasonNumber') in seasons
                             and row.get('status') == 5}
                if (available and (not decision.missing_seasons
                                   or not set(decision.missing_seasons).issubset(seasons)
                                   or available.intersection(decision.missing_seasons))):
                    # Stock partial/processing changes only the parent. Keep
                    # an Available season only if every missing episode is
                    # proven to belong to another requested season.
                    return 'held'
            if (media['status'] == self._STATUS[status]
                    and (status != 'available'
                         or self._available_seasons(media, promote_seasons))):
                return 'unchanged'
            if not self.write_enabled:
                return f'would_{status}'
            result = self.seerr.request_json(
                'POST', f'/api/v1/media/{media_id}/{status}', body)
            if (not isinstance(result, dict) or result.get('id') != media_id
                    or result.get('status') != self._STATUS[status]
                    or (status == 'available'
                        and not self._available_seasons(result, seasons))):
                return 'held'
            if status == 'available':
                returned_seasons = result.get('seasons')
                if (not isinstance(returned_seasons, list)
                        or len(returned_seasons) != len(saved_seasons)
                        or any(not isinstance(row, dict)
                               or type(row.get('seasonNumber')) is not int
                               or row.get('status') != 5
                               for row in returned_seasons)
                        or {row['seasonNumber'] for row in returned_seasons}
                        != {row['seasonNumber'] for row in saved_seasons}):
                    return 'held'
            return status
        except (KeyError, ValueError, TypeError, RuntimeError, OSError):
            return 'held'
