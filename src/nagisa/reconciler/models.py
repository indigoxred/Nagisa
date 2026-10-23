"""Typed reconciliation observations; no model performs I/O."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class QueueStatus(StrEnum):
    READY = "ready"
    WAITING = "waiting"
    BLOCKED = "blocked"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ImportHistoryEvent:
    history_id: int
    series_id: int
    episode_id: int
    sonarr_file_id: int
    download_id: str
    imported_path: str
    dropped_path: str
    source_size: int


@dataclass(frozen=True, slots=True)
class FileObservation:
    root: Path
    relative: str
    resolved: Path | None
    safe: bool
    exists: bool
    size: int | None
    link_count: int | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ShokoEpisodeIdentity:
    anidb_series_id: int
    anidb_episode_number: int
    kind: str
    shoko_series_id: int = 0
    shoko_episode_id: int = 0


@dataclass(frozen=True, slots=True)
class ShokoFileIdentity:
    sha1: str
    file_id: int
    relative_paths: tuple[str, ...]
    regular: tuple[ShokoEpisodeIdentity, ...]
    specials: tuple[ShokoEpisodeIdentity, ...]
    anidb_audio_languages: tuple[str, ...] = ()
    anidb_subtitle_languages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MediaTrackEvidence:
    codec: str
    language: str
    title: str
    default: bool
    forced: bool


@dataclass(frozen=True, slots=True)
class MediaPolicyObservation:
    probe_ok: bool
    japanese_audio: bool
    full_english_subtitle: bool
    unsafe_attachment: bool
    meets_resolution_floor: bool
    height: int | None
    reasons: tuple[str, ...]
    audio_tracks: tuple[MediaTrackEvidence, ...] = ()
    subtitle_tracks: tuple[MediaTrackEvidence, ...] = ()

    @property
    def compliant(self) -> bool:
        return (
            self.probe_ok
            and self.japanese_audio
            and self.full_english_subtitle
            and not self.unsafe_attachment
            and self.meets_resolution_floor
        )


@dataclass(frozen=True, slots=True)
class FileGateObservation:
    sonarr_file_id: int
    source_present: bool
    source_sha1: str
    source_size: int
    destination: FileObservation
    shoko: ShokoFileIdentity | None
    plex_playable: bool
    media: MediaPolicyObservation
    qbit_retained: bool
    provider_identity_exact: bool
    semantic_identity_exact: bool


@dataclass(frozen=True, slots=True)
class SeriesObservation:
    series_id: int
    observation_complete: bool
    dependencies_healthy: bool
    import_root_healthy: bool
    anime_root_healthy: bool
    files: tuple[FileGateObservation, ...]
    future_fingerprint: str
    unrelated_fingerprint: str
    queue_empty: bool
    unexpected_attached_file_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class SeriesDecision:
    series_id: int
    status: QueueStatus
    reasons: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ActionVerification:
    complete: bool
    stale_detached: bool
    delivered_unmonitored: bool
    future_unchanged: bool
    unrelated_unchanged: bool
    qbit_retained: bool
    no_reacquisition: bool
    queue_empty: bool
    reasons: tuple[str, ...]

    @property
    def successful(self) -> bool:
        return self.complete and all((
            self.stale_detached,
            self.delivered_unmonitored,
            self.future_unchanged,
            self.unrelated_unchanged,
            self.qbit_retained,
            self.no_reacquisition,
            self.queue_empty,
        ))
