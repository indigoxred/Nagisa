"""Supported Sonarr reads and the sole permitted RescanSeries mutation."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Iterable

from nagisa.reconciler.models import ImportHistoryEvent


class SonarrClient:
    def __init__(self, transport: Any, *, allowed_versions: Iterable[str],
                 import_root: str = "/Shoko") -> None:
        self.transport = transport
        self.allowed_versions = frozenset(allowed_versions)
        root = PurePosixPath(import_root)
        if not root.is_absolute() or ".." in root.parts or str(root) == "/":
            raise ValueError("Sonarr import root is invalid")
        self.import_root = root

    def accepts_import(self, event: ImportHistoryEvent) -> bool:
        try:
            relative = PurePosixPath(event.imported_path).relative_to(self.import_root)
        except (AttributeError, ValueError):
            return False
        return bool(relative.parts) and ".." not in relative.parts

    def compatibility(self) -> tuple[bool, str | None, str]:
        payload = self.transport.request_json("GET", "/api/v3/system/status")
        version = payload.get("version") if isinstance(payload, dict) else None
        if not isinstance(version, str):
            return False, None, "malformed-status"
        if version not in self.allowed_versions:
            return False, version, "unsupported-version"
        return True, version, "supported"

    def ordering_evidence(self, tmdb_id: int) -> dict[str, Any]:
        """Return an exact, read-only regular-season structure for one TMDB show."""
        if not isinstance(tmdb_id, int) or tmdb_id <= 0:
            raise ValueError("TMDB ID must be positive")
        incomplete = {
            "evidence_complete": False,
            "tmdb_id": tmdb_id,
            "series_id": None,
            "coordinates": [],
        }
        compatible, _version, _reason = self.compatibility()
        if not compatible:
            return incomplete
        payload = self.transport.request_json("GET", "/api/v3/series")
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            return incomplete
        matches: list[dict] = []
        for row in payload:
            try:
                series_id = int(row["id"])
                candidate_tmdb = int(row["tmdbId"])
            except (KeyError, TypeError, ValueError):
                return incomplete
            if series_id <= 0 or candidate_tmdb <= 0:
                return incomplete
            if candidate_tmdb == tmdb_id:
                matches.append(row)
        if len(matches) != 1:
            return incomplete
        series_id = int(matches[0]["id"])
        rows = self.episodes(series_id)
        seen_ids: set[int] = set()
        coordinates: list[tuple[int, int]] = []
        for row in rows:
            try:
                episode_id = int(row["id"])
                row_series_id = int(row["seriesId"])
                season = int(row["seasonNumber"])
                episode = int(row["episodeNumber"])
            except (KeyError, TypeError, ValueError):
                return incomplete
            if (
                episode_id <= 0
                or episode_id in seen_ids
                or row_series_id != series_id
                or season < 0
                or episode <= 0
            ):
                return incomplete
            seen_ids.add(episode_id)
            if season > 0:
                coordinates.append((season, episode))
        if not coordinates or len(coordinates) != len(set(coordinates)):
            return incomplete
        seasons = sorted({season for season, _ in coordinates})
        if seasons != list(range(1, max(seasons) + 1)):
            return incomplete
        for season in seasons:
            episodes = sorted(episode for candidate, episode in coordinates if candidate == season)
            if episodes != list(range(1, max(episodes) + 1)):
                return incomplete
        return {
            "evidence_complete": True,
            "tmdb_id": tmdb_id,
            "series_id": series_id,
            "coordinates": [
                {"season": season, "episode": episode}
                for season, episode in sorted(coordinates)
            ],
        }

    def import_history_after(self, watermark: int) -> list[ImportHistoryEvent]:
        if not isinstance(watermark, int) or watermark < 0:
            raise ValueError("history watermark must be non-negative")
        page_size = 250
        events: list[ImportHistoryEvent] = []
        seen: set[int] = set()
        reached_watermark = False
        for page in range(1, 101):
            path = (
                f"/api/v3/history?page={page}&pageSize={page_size}"
                "&sortKey=id&sortDirection=descending"
            )
            payload = self.transport.request_json("GET", path)
            records = payload.get("records") if isinstance(payload, dict) else None
            try:
                total_records = int(payload["totalRecords"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("malformed Sonarr history") from None
            if (
                not isinstance(records, list) or total_records < 0
                or not all(isinstance(row, dict) for row in records)
            ):
                raise ValueError("malformed Sonarr history")
            if not records:
                break
            page_ids: list[int] = []
            for row in records:
                try:
                    history_id = int(row["id"])
                except (KeyError, TypeError, ValueError):
                    raise ValueError("malformed Sonarr history row") from None
                if history_id <= 0 or history_id in seen:
                    raise ValueError("invalid or duplicate Sonarr history identity")
                seen.add(history_id)
                page_ids.append(history_id)
                if history_id <= watermark:
                    reached_watermark = True
                    break
                if row.get("eventType") != "downloadFolderImported":
                    continue
                try:
                    series_id = int(row["seriesId"])
                    episode_id = int(row["episodeId"])
                    download_id = str(row["downloadId"]).lower()
                    data = row["data"]
                    sonarr_file_id = int(data["fileId"])
                    imported_path = str(data["importedPath"])
                    dropped_path = str(data["droppedPath"])
                    source_size = int(data["size"])
                except (KeyError, TypeError, ValueError):
                    raise ValueError("malformed Sonarr import history row") from None
                if (
                    series_id <= 0 or episode_id <= 0
                    or sonarr_file_id <= 0 or source_size <= 0
                    or not imported_path.startswith("/") or not dropped_path.startswith("/")
                    or "\x00" in imported_path or "\x00" in dropped_path
                    or len(download_id) != 40
                    or any(c not in "0123456789abcdef" for c in download_id)
                ):
                    raise ValueError("invalid Sonarr import history identity")
                events.append(ImportHistoryEvent(
                    history_id, series_id, episode_id, sonarr_file_id, download_id,
                    imported_path, dropped_path, source_size,
                ))
            if page_ids != sorted(page_ids, reverse=True):
                raise ValueError("Sonarr history order is not descending")
            if reached_watermark or page * page_size >= total_records:
                break
            if len(records) != page_size:
                raise ValueError("Sonarr history pagination did not advance")
        else:
            raise ValueError("Sonarr history pagination exceeded the safety limit")
        return sorted(events, key=lambda row: row.history_id)

    def series(self, series_id: int) -> dict:
        payload = self.transport.request_json("GET", f"/api/v3/series/{int(series_id)}")
        if not isinstance(payload, dict) or int(payload.get("id", 0)) != int(series_id):
            raise ValueError("malformed Sonarr series")
        return payload

    def root_folders(self) -> list[dict]:
        payload = self.transport.request_json("GET", "/api/v3/rootfolder")
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError("malformed Sonarr root folders")
        return payload

    def episodes(self, series_id: int) -> list[dict]:
        payload = self.transport.request_json("GET", f"/api/v3/episode?seriesId={int(series_id)}&includeImages=false")
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError("malformed Sonarr episodes")
        return payload

    def episode(self, episode_id: int) -> dict:
        if not isinstance(episode_id, int) or episode_id <= 0:
            raise ValueError("episode ID must be positive")
        payload = self.transport.request_json("GET", f"/api/v3/episode/{episode_id}")
        if not isinstance(payload, dict) or int(payload.get("id", 0)) != episode_id:
            raise ValueError("malformed Sonarr episode")
        return payload

    def episode_files(self, series_id: int) -> list[dict]:
        payload = self.transport.request_json("GET", f"/api/v3/episodefile?seriesId={int(series_id)}")
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError("malformed Sonarr episode files")
        return payload

    def queue(self) -> list[dict]:
        payload = self.transport.request_json("GET", "/api/v3/queue?page=1&pageSize=1000&includeUnknownSeriesItems=true")
        rows = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("malformed Sonarr queue")
        return rows

    def reject_future_queue_item(self, queue_id: int) -> None:
        if not isinstance(queue_id, int) or queue_id <= 0:
            raise ValueError("queue ID must be positive")
        path = (
            f"/api/v3/queue/{queue_id}?removeFromClient=true&blocklist=true"
            "&skipRedownload=true&changeCategory=false"
        )
        payload = self.transport.request_json("DELETE", path)
        if payload not in ({}, None):
            raise ValueError("unexpected Sonarr queue deletion response")

    def command(self, command_id: int) -> dict:
        payload = self.transport.request_json("GET", f"/api/v3/command/{int(command_id)}")
        if not isinstance(payload, dict) or int(payload.get("id", 0)) != int(command_id):
            raise ValueError("malformed Sonarr command")
        return payload

    def history_since(self, timestamp: str) -> list[dict]:
        if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
            raise ValueError("history timestamp must be UTC")
        path = "/api/v3/history?page=1&pageSize=1000&sortKey=id&sortDirection=descending"
        payload = self.transport.request_json("GET", path)
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
            raise ValueError("malformed Sonarr history")
        return [row for row in records if isinstance(row.get("date"), str) and row["date"] >= timestamp]

    def rescan_series(self, series_id: int) -> int:
        if not isinstance(series_id, int) or series_id <= 0:
            raise ValueError("series ID must be positive")
        payload = self.transport.request_json("POST", "/api/v3/command", {"name": "RescanSeries", "seriesId": series_id})
        try:
            command_id = int(payload["id"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Sonarr did not return a command ID") from None
        if command_id <= 0:
            raise ValueError("Sonarr returned an invalid command ID")
        return command_id
