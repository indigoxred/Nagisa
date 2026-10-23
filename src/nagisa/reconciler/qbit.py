"""Read-only qBittorrent retention evidence."""

from __future__ import annotations

from typing import Any
from pathlib import PurePosixPath
from urllib.parse import urlencode


class QbitObserver:
    def __init__(self, transport: Any) -> None:
        self.transport = transport

    @staticmethod
    def normalize_torrents(payload: object) -> tuple[dict, ...]:
        if not isinstance(payload, list):
            raise ValueError("malformed qBittorrent torrent inventory")
        rows: list[dict] = []
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError("malformed qBittorrent torrent row")
            value = {
                "hash": str(row.get("hash", "")).lower(),
                "progress": float(row.get("progress", -1)),
                "save_path": str(row.get("save_path", "")),
                "content_path": str(row.get("content_path", "")),
                "amount_left": int(row.get("amount_left", -1)),
                "state": str(row.get("state", "")),
            }
            if len(value["hash"]) != 40 or any(c not in "0123456789abcdef" for c in value["hash"]):
                raise ValueError("qBittorrent hash identity is invalid")
            rows.append(value)
        return tuple(sorted(rows, key=lambda row: row["hash"]))

    def torrents(self) -> tuple[dict, ...]:
        return self.normalize_torrents(self.transport.request_json("GET", "/api/v2/torrents/info"))

    @staticmethod
    def normalize_files(payload: object) -> tuple[dict, ...]:
        if not isinstance(payload, list):
            raise ValueError("malformed qBittorrent file inventory")
        rows: list[dict] = []
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError("malformed qBittorrent file row")
            name = str(row.get("name", ""))
            relative = PurePosixPath(name)
            if not name or relative.is_absolute() or ".." in relative.parts or "\x00" in name:
                raise ValueError("qBittorrent file path is unsafe")
            rows.append({"name": name, "size": int(row.get("size", -1)), "progress": float(row.get("progress", -1))})
        return tuple(sorted(rows, key=lambda row: row["name"]))

    @staticmethod
    def retains(rows: tuple[dict, ...], files: tuple[dict, ...], torrent_hash: str,
                dropped_path: str, source_size: int) -> bool:
        matches = [row for row in rows if row["hash"] == torrent_hash.lower()]
        if len(matches) != 1:
            return False
        torrent = matches[0]
        if torrent["progress"] < 1 or torrent["amount_left"] != 0 or torrent["state"] in {"missingFiles", "error"}:
            return False
        dropped = PurePosixPath(dropped_path)
        if not dropped.is_absolute() or ".." in dropped.parts or source_size <= 0:
            return False
        suffixes = {"/".join(dropped.parts[index:]) for index in range(1, len(dropped.parts))}
        file_matches = [row for row in files if row["name"] in suffixes and row["size"] == source_size and row["progress"] >= 1]
        return len(file_matches) == 1

    def compatibility(self) -> bool:
        payload = self.transport.request_json("GET", "/api/v2/torrents/info?limit=1")
        return isinstance(payload, list)

    def retains_download(self, download_id: str, dropped_path: str, source_size: int) -> bool:
        normalized_hash = download_id.lower()
        query = urlencode({"hashes": normalized_hash})
        torrents = self.normalize_torrents(self.transport.request_json("GET", f"/api/v2/torrents/info?{query}"))
        if len([row for row in torrents if row["hash"] == normalized_hash]) != 1:
            return False
        files = self.normalize_files(self.transport.request_json("GET", f"/api/v2/torrents/files?{urlencode({'hash': normalized_hash})}"))
        return self.retains(torrents, files, download_id, dropped_path, source_size)
