"""Conservative exact-item protection against implausibly early TV payloads."""

from __future__ import annotations

import threading
from datetime import datetime, timezone


def _utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must be UTC")
    parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class FutureEpisodeGuard:
    def __init__(self, sonarr, *, mode: str, threshold_hours: int = 24,
                 now=_now, audit=None) -> None:
        if mode not in {"report", "enabled"}:
            raise ValueError("future guard mode must be report or enabled")
        if not isinstance(threshold_hours, int) or not 6 <= threshold_hours <= 168:
            raise ValueError("future guard threshold must be between 6 and 168 hours")
        self.sonarr = sonarr
        self.mode = mode
        self.threshold_hours = threshold_hours
        self.now = now
        self.audit = audit
        self._lock = threading.Lock()
        self._last = {
            "mode": mode, "threshold_hours": threshold_hours, "compatible": None,
            "observed": 0, "allowed": 0, "would_reject": 0, "rejected": 0,
            "blocked": 0, "last_cycle": None, "decisions": [],
        }

    @staticmethod
    def _episode_id(row: dict) -> int:
        value = row.get("episodeId")
        if value in (None, 0) and isinstance(row.get("episode"), dict):
            value = row["episode"].get("id")
        try:
            result = int(value)
        except (TypeError, ValueError):
            return 0
        return result if result > 0 else 0

    def run_once(self) -> dict:
        if not self._lock.acquire(blocking=False):
            return self.status_snapshot() | {"skipped": True}
        try:
            timestamp = self.now()
            compatible, version, reason = self.sonarr.compatibility()
            result = {
                "mode": self.mode, "threshold_hours": self.threshold_hours,
                "compatible": compatible, "version": version, "reason": reason,
                "observed": 0, "allowed": 0, "would_reject": 0,
                "rejected": 0, "blocked": 0, "last_cycle": timestamp,
                "decisions": [],
            }
            if not compatible:
                result["blocked"] = 1
                self._last = result
                return dict(result)
            current = _utc(timestamp)
            for row in self.sonarr.queue():
                result["observed"] += 1
                try:
                    queue_id = int(row.get("id", 0))
                except (TypeError, ValueError):
                    queue_id = 0
                episode_id = self._episode_id(row)
                if queue_id <= 0 or episode_id <= 0:
                    result["blocked"] += 1
                    result["decisions"].append({"queue_id": queue_id or None, "status": "blocked", "reason": "ambiguous-episode-identity"})
                    continue
                try:
                    episode = self.sonarr.episode(episode_id)
                    air = _utc(episode.get("airDateUtc"))
                    hours = (air - current).total_seconds() / 3600
                    series_id = int(episode["seriesId"])
                    season = int(episode["seasonNumber"])
                    number = int(episode["episodeNumber"])
                except (KeyError, TypeError, ValueError):
                    result["blocked"] += 1
                    result["decisions"].append({"queue_id": queue_id, "episode_id": episode_id, "status": "blocked", "reason": "malformed-current-episode"})
                    continue
                decision = {"queue_id": queue_id, "episode_id": episode_id, "series_id": series_id,
                            "coordinate": f"S{season:02d}E{number:02d}", "hours_before_air": round(hours, 2)}
                if hours <= self.threshold_hours:
                    result["allowed"] += 1
                    result["decisions"].append(decision | {"status": "allowed"})
                    continue
                if self.mode == "report":
                    result["would_reject"] += 1
                    result["decisions"].append(decision | {"status": "would-reject"})
                    continue
                self.sonarr.reject_future_queue_item(queue_id)
                result["rejected"] += 1
                result["decisions"].append(decision | {"status": "rejected"})
                if self.audit is not None:
                    self.audit.record("future-guard", "queue-item-rejected", decision)
            self._last = result
            return dict(result)
        finally:
            self._lock.release()

    def status_snapshot(self) -> dict:
        return dict(self._last)
