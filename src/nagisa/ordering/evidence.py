"""Canonical, redacted and immutable selector evidence archives."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
REQUIRED_FIELDS = (
    "shoko_episode_id",
    "series_id",
    "series_episode_number",
    "kind",
    "file_backed",
    "linked",
)
ORDERING_FIELDS = ("id", "is_default", "evidence_complete")
ORDERING_EPISODE_FIELDS = (
    "shoko_episode_id",
    "provider_episode_id",
    "season",
    "episode",
    "hidden",
)
PROVIDER_FIELDS = ("evidence_complete", "tmdb_id", "series_id")
PROVIDER_COORDINATE_FIELDS = ("season", "episode")


def normalized_evidence(inputs: dict[str, Any]) -> dict[str, Any]:
    required = [
        {key: row[key] for key in REQUIRED_FIELDS}
        for row in inputs["required_episodes"]
    ]
    required.sort(key=lambda row: int(row["shoko_episode_id"]))
    orderings = []
    for source in inputs["orderings"]:
        ordering = {key: source[key] for key in ORDERING_FIELDS}
        episodes = [
            {key: row[key] for key in ORDERING_EPISODE_FIELDS}
            for row in source["episodes"]
        ]
        episodes.sort(
            key=lambda row: (
                int(row["shoko_episode_id"]),
                int(row["provider_episode_id"]),
            )
        )
        ordering["episodes"] = episodes
        orderings.append(ordering)
    orderings.sort(key=lambda row: str(row["id"]))
    provider_source = inputs["provider_structure"]
    provider = {key: provider_source[key] for key in PROVIDER_FIELDS}
    coordinates = [
        {key: row[key] for key in PROVIDER_COORDINATE_FIELDS}
        for row in provider_source["coordinates"]
    ]
    coordinates.sort(key=lambda row: (int(row["season"]), int(row["episode"])))
    provider["coordinates"] = coordinates
    return {
        "required_episodes": required,
        "orderings": orderings,
        "provider_structure": provider,
    }


def evidence_fingerprint(inputs: dict[str, Any]) -> str:
    encoded = json.dumps(
        normalized_evidence(inputs),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class EvidenceArchive:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        show_id: int,
        fingerprint: str,
        inputs: dict[str, Any],
    ) -> Path:
        if not isinstance(show_id, int) or show_id <= 0:
            raise ValueError("show ID must be a positive integer")
        if not isinstance(fingerprint, str) or not FINGERPRINT.fullmatch(fingerprint):
            raise ValueError("evidence fingerprint must be 64 lowercase hex characters")
        normalized = normalized_evidence(inputs)
        if evidence_fingerprint(normalized) != fingerprint:
            raise ValueError("evidence fingerprint does not match normalized inputs")
        payload = json.dumps(
            {
                "evidence_fingerprint": fingerprint,
                "inputs": normalized,
                "show_id": show_id,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8") + b"\n"
        destination = self.path / f"show-{show_id}-{fingerprint}.json"
        if destination.exists():
            if destination.read_bytes() != payload:
                raise ValueError("existing evidence archive content has drifted")
            return destination
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination

