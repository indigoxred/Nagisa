"""Pure, deterministic, series-atomic reconciliation planner."""

from __future__ import annotations

import hashlib
import json

from nagisa.reconciler.models import QueueStatus, SeriesDecision, SeriesObservation


def _normalized(observation: SeriesObservation) -> dict:
    return {
        "series_id": observation.series_id,
        "observation_complete": observation.observation_complete,
        "dependencies_healthy": observation.dependencies_healthy,
        "import_root_healthy": observation.import_root_healthy,
        "anime_root_healthy": observation.anime_root_healthy,
        "future_fingerprint": observation.future_fingerprint,
        "unrelated_fingerprint": observation.unrelated_fingerprint,
        "queue_empty": observation.queue_empty,
        "unexpected_attached_file_ids": observation.unexpected_attached_file_ids,
        "files": [
            {
                "sonarr_file_id": row.sonarr_file_id,
                "source_present": row.source_present,
                "source_sha1": row.source_sha1,
                "source_size": row.source_size,
                "destination": {
                    "relative": row.destination.relative,
                    "safe": row.destination.safe,
                    "exists": row.destination.exists,
                    "size": row.destination.size,
                    "link_count": row.destination.link_count,
                    "reasons": row.destination.reasons,
                },
                "shoko": None if row.shoko is None else {
                    "sha1": row.shoko.sha1,
                    "file_id": row.shoko.file_id,
                    "relative_paths": row.shoko.relative_paths,
                    "regular": [(item.anidb_series_id, item.anidb_episode_number, item.kind, item.shoko_series_id, item.shoko_episode_id) for item in row.shoko.regular],
                    "specials": [(item.anidb_series_id, item.anidb_episode_number, item.kind, item.shoko_series_id, item.shoko_episode_id) for item in row.shoko.specials],
                },
                "plex_playable": row.plex_playable,
                "media": {
                    "probe_ok": row.media.probe_ok,
                    "japanese_audio": row.media.japanese_audio,
                    "full_english_subtitle": row.media.full_english_subtitle,
                    "unsafe_attachment": row.media.unsafe_attachment,
                    "meets_resolution_floor": row.media.meets_resolution_floor,
                    "height": row.media.height,
                    "reasons": row.media.reasons,
                    "audio_tracks": [
                        (track.codec, track.language, track.title, track.default, track.forced)
                        for track in row.media.audio_tracks
                    ],
                    "subtitle_tracks": [
                        (track.codec, track.language, track.title, track.default, track.forced)
                        for track in row.media.subtitle_tracks
                    ],
                },
                "qbit_retained": row.qbit_retained,
                "provider_identity_exact": row.provider_identity_exact,
                "semantic_identity_exact": row.semantic_identity_exact,
            }
            for row in sorted(observation.files, key=lambda item: item.sonarr_file_id)
        ],
    }


def observation_fingerprint(observation: SeriesObservation) -> str:
    payload = json.dumps(_normalized(observation), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ReconciliationPlanner:
    def evaluate(self, observation: SeriesObservation) -> SeriesDecision:
        if observation.series_id <= 0:
            raise ValueError("series ID must be positive")
        fingerprint = observation_fingerprint(observation)
        blocked: set[str] = set()
        waiting: set[str] = set()
        if not observation.observation_complete:
            blocked.add("observation-incomplete")
        if not observation.dependencies_healthy:
            blocked.add("dependency-unhealthy")
        if not observation.import_root_healthy:
            blocked.add("import-root-unhealthy")
        if not observation.anime_root_healthy:
            blocked.add("anime-root-unhealthy")
        if not observation.future_fingerprint:
            blocked.add("future-monitoring-snapshot-missing")
        if not observation.unrelated_fingerprint:
            blocked.add("unrelated-state-snapshot-missing")
        if not observation.queue_empty:
            blocked.add("series-queue-not-empty")
        if observation.unexpected_attached_file_ids:
            blocked.add("unrelated-sonarr-files-attached")
        if not observation.files:
            status = QueueStatus.BLOCKED if blocked else QueueStatus.COMPLETED
            return SeriesDecision(observation.series_id, status, tuple(sorted(blocked)), fingerprint)
        ids: set[int] = set()
        for row in observation.files:
            if row.sonarr_file_id <= 0 or row.sonarr_file_id in ids:
                blocked.add("duplicate-or-invalid-sonarr-file")
            ids.add(row.sonarr_file_id)
            if row.source_present:
                waiting.add("source-still-present")
                if row.source_size <= 0:
                    blocked.add("source-identity-invalid")
                continue
            if len(row.source_sha1) != 40 or row.source_size <= 0:
                blocked.add("source-identity-invalid")
        if blocked:
            return SeriesDecision(observation.series_id, QueueStatus.BLOCKED, tuple(sorted(blocked | waiting)), fingerprint)
        if waiting:
            return SeriesDecision(observation.series_id, QueueStatus.WAITING, tuple(sorted(waiting)), fingerprint)
        for row in observation.files:
            if not row.destination.safe:
                blocked.update(row.destination.reasons or ("destination-unsafe",))
            if row.destination.size != row.source_size:
                blocked.add("destination-size-mismatch")
            if row.shoko is None:
                blocked.add("shoko-file-identity-missing")
            else:
                if row.shoko.sha1 != row.source_sha1:
                    blocked.add("shoko-hash-mismatch")
                if row.destination.relative not in row.shoko.relative_paths:
                    blocked.add("shoko-destination-mismatch")
                if len(row.shoko.regular) != 1:
                    blocked.add("regular-provider-conflict")
            if not row.provider_identity_exact:
                blocked.add("provider-identity-not-exact")
            if not row.semantic_identity_exact:
                blocked.add("sonarr-shoko-episode-mismatch")
            if not row.plex_playable:
                blocked.add("plex-part-not-playable")
            if not row.media.compliant:
                blocked.update(row.media.reasons or ("media-policy-failed",))
            if not row.qbit_retained:
                blocked.add("torrent-retention-unproven")
        if blocked:
            return SeriesDecision(observation.series_id, QueueStatus.BLOCKED, tuple(sorted(blocked | waiting)), fingerprint)
        if waiting:
            return SeriesDecision(observation.series_id, QueueStatus.WAITING, tuple(sorted(waiting)), fingerprint)
        return SeriesDecision(observation.series_id, QueueStatus.READY, (), fingerprint)
