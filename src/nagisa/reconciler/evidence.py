"""Exact, read-only evidence collection for series-atomic reconciliation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from nagisa.reconciler.filesystem import observe_file
from nagisa.reconciler.models import (
    ActionVerification, FileGateObservation, FileObservation, ImportHistoryEvent,
    MediaPolicyObservation, SeriesObservation,
)
from nagisa.reconciler.shoko import MissingTvdbEpisodeIdentity


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha1(path: Path) -> str:
    digest = hashlib.sha1(usedforsecurity=False)
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(rows: object) -> str:
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _failed_media(reason: str) -> MediaPolicyObservation:
    return MediaPolicyObservation(False, False, False, False, False, None, (reason,))


def _failed_destination(root: Path, relative: str, reason: str) -> FileObservation:
    return FileObservation(root, relative, None, False, False, None, None, (reason,))


def _with_exact_anidb_language_evidence(
    media: MediaPolicyObservation, identity,
) -> MediaPolicyObservation:
    """Fill only genuinely untagged media languages from the exact AniDB file.

    The Shoko file was already matched by exact hash, size and accessible path.
    Explicit stream tags remain authoritative: AniDB never overrides a tagged
    English-only audio track or a tagged French-only subtitle payload.
    """
    if not media.probe_ok:
        return media
    untagged = {"", "und", "none", "unknown"}
    explicit_audio = {row.language for row in media.audio_tracks if row.language not in untagged}
    explicit_subtitles = {
        row.language for row in media.subtitle_tracks if row.language not in untagged
    }
    japanese_audio = media.japanese_audio
    if (
        not japanese_audio and media.audio_tracks and not explicit_audio
        and "japanese" in identity.anidb_audio_languages
    ):
        japanese_audio = True
    full_english_subtitle = media.full_english_subtitle
    if (
        not full_english_subtitle and media.subtitle_tracks and not explicit_subtitles
        and "english" in identity.anidb_subtitle_languages
    ):
        full_english_subtitle = True
    reasons = tuple(
        reason for reason in media.reasons
        if not (reason == "missing-japanese-audio" and japanese_audio)
        and not (reason == "missing-full-english-subtitle" and full_english_subtitle)
    )
    return MediaPolicyObservation(
        media.probe_ok, japanese_audio, full_english_subtitle,
        media.unsafe_attachment, media.meets_resolution_floor, media.height,
        reasons, media.audio_tracks, media.subtitle_tracks,
    )


def _positive_integer(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _queue_episode_ids(row: dict) -> set[int]:
    """Return only explicit Sonarr episode identities from one queue row."""
    episode_ids: set[int] = set()
    direct = _positive_integer(row.get("episodeId"))
    if direct is not None:
        episode_ids.add(direct)
    nested = row.get("episode")
    if isinstance(nested, dict):
        nested_id = _positive_integer(nested.get("id"))
        if nested_id is not None:
            episode_ids.add(nested_id)
    for key in ("episodeIds", "episodes"):
        values = row.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            candidate = value.get("id") if isinstance(value, dict) else value
            episode_id = _positive_integer(candidate)
            if episode_id is not None:
                episode_ids.add(episode_id)
    return episode_ids


def _queue_conflicts(
    queue_rows: list[dict], series_id: int, delivered_episode_ids: set[int]
) -> bool:
    """Block the exact delivered episodes, or an ambiguous row for their series.

    A different, explicitly identified episode in the same airing series is not
    a reacquisition of the imported episode and must not prevent retirement.
    """
    for row in queue_rows:
        if not isinstance(row, dict):
            continue
        row_series_id = _positive_integer(row.get("seriesId"))
        row_episode_ids = _queue_episode_ids(row)
        if row_episode_ids & delivered_episode_ids:
            return True
        if row_series_id == series_id and not row_episode_ids:
            return True
    return False


class ReconcilerEvidenceCollector:
    """Bind one Sonarr import to Shoko, the destination and Plex without title parsing."""

    def __init__(
        self, *, sonarr, shoko, plex, qbit, probe, import_root: Path,
        anime_root: Path, plex_section_ids: tuple[int, ...],
        hash_file: Callable[[Path], str] = _sha1,
        now: Callable[[], str] = _utc_now,
        event_provider: Callable[[int, int], tuple[ImportHistoryEvent, ...]] | None = None,
    ) -> None:
        self.sonarr = sonarr
        self.shoko = shoko
        self.plex = plex
        self.qbit = qbit
        self.probe = probe
        self.import_root = Path(import_root)
        self.anime_root = Path(anime_root)
        self.plex_section_ids = tuple(plex_section_ids)
        self.hash_file = hash_file
        self.now = now
        self.event_provider = event_provider or (lambda action_id, series_id: ())

    @classmethod
    def from_config(cls, config, *, sonarr, event_provider=None, managed_roots=None, source_mounts=None, source_aliases=None):
        from nagisa.http import JsonTransport, QbitTransport
        from nagisa.reconciler.media import MediaProbe
        from nagisa.reconciler.plex import PlexObserver
        from nagisa.reconciler.qbit import QbitObserver
        from nagisa.reconciler.shoko import ShokoObserver

        shoko = ShokoObserver(
            JsonTransport(
                config.shoko_url, headers={"apikey": config.shoko_token},
                timeout=config.request_timeout_seconds, maximum_bytes=16 * 1024 * 1024,
            ),
            allowed_versions=config.shoko_allowed_versions,
            managed_roots=managed_roots,
            destination_root=config.anime_root if managed_roots else None,
        )
        plex = PlexObserver(JsonTransport(
            config.plex_url, headers={"X-Plex-Token": config.plex_token},
            timeout=config.request_timeout_seconds, maximum_bytes=32 * 1024 * 1024,
        ), source_mounts=source_mounts, source_aliases=source_aliases)
        qbit = QbitObserver(QbitTransport(
            config.qbit_url, username=config.qbit_username, password=config.qbit_password,
            timeout=config.request_timeout_seconds,
        ))
        return cls(
            sonarr=sonarr, shoko=shoko, plex=plex, qbit=qbit, probe=MediaProbe(),
            import_root=config.import_root, anime_root=config.anime_root,
            plex_section_ids=config.plex_section_ids, event_provider=event_provider,
        )

    @staticmethod
    def _healthy_root(root: Path) -> bool:
        return root.is_dir() and not root.is_symlink()

    @staticmethod
    def _root_relative(imported_path: str, roots: Iterable[dict]) -> PurePosixPath:
        imported = PurePosixPath(imported_path)
        if not imported.is_absolute() or ".." in imported.parts or "\x00" in imported_path:
            raise ValueError("Sonarr imported path is unsafe")
        matches: list[PurePosixPath] = []
        for row in roots:
            raw = row.get("path") if isinstance(row, dict) else None
            if not isinstance(raw, str):
                continue
            root = PurePosixPath(raw)
            if not root.is_absolute() or ".." in root.parts:
                continue
            try:
                matches.append(imported.relative_to(root))
            except ValueError:
                continue
        if len(matches) != 1 or not str(matches[0]):
            raise ValueError("Sonarr import root mapping is ambiguous")
        return matches[0]

    @staticmethod
    def _dependencies_healthy(*dependencies) -> bool:
        try:
            return all(dependency.compatibility() is True for dependency in dependencies)
        except Exception:
            return False

    @staticmethod
    def _episode_snapshots(episodes: list[dict], delivered: set[int], now: str) -> tuple[str, str]:
        future: list[dict] = []
        unrelated: list[dict] = []
        seen: set[int] = set()
        for row in episodes:
            try:
                episode_id = int(row["id"])
                monitored = row["monitored"] is True
                has_file = row["hasFile"] is True
                episode_file_id = int(row.get("episodeFileId", 0))
                air_date = str(row.get("airDateUtc", ""))
            except (KeyError, TypeError, ValueError):
                raise ValueError("Sonarr episode snapshot is malformed") from None
            if episode_id <= 0 or episode_id in seen:
                raise ValueError("Sonarr episode snapshot has duplicate identity")
            seen.add(episode_id)
            normalized = {
                "id": episode_id, "monitored": monitored,
                "has_file": has_file, "episode_file_id": episode_file_id,
            }
            if episode_id not in delivered:
                unrelated.append(normalized)
                future.append({"id": episode_id, "monitored": monitored, "air_date": air_date})
        return _fingerprint(sorted(future, key=lambda row: row["id"])), _fingerprint(sorted(unrelated, key=lambda row: row["id"]))

    def _waiting_file(self, event: ImportHistoryEvent, source: FileObservation) -> FileGateObservation:
        return FileGateObservation(
            event.sonarr_file_id, True, "", event.source_size,
            _failed_destination(self.anime_root, PurePosixPath(event.imported_path).name, "destination-awaiting-shoko"),
            None, False, _failed_media("media-awaiting-shoko"),
            self.qbit.retains_download(event.download_id, event.dropped_path, event.source_size), False, False,
        )

    def _moved_file(
        self, event: ImportHistoryEvent, expected_shoko_series_ids: frozenset[int],
        expected_tmdb_show_id: int, expected_tvdb_episode_id: int,
        expected_season_number: int, expected_episode_number: int,
    ) -> FileGateObservation:
        filename = PurePosixPath(event.imported_path).name
        try:
            identity = self.shoko.by_path_ending(filename, expected_size=event.source_size)
        except (OSError, RuntimeError, ValueError):
            return FileGateObservation(
                event.sonarr_file_id, False, "", event.source_size,
                _failed_destination(self.anime_root, filename, "shoko-destination-unresolved"),
                None, False, _failed_media("destination-unavailable"),
                self.qbit.retains_download(event.download_id, event.dropped_path, event.source_size), False, False,
            )

        matches: list[tuple[FileObservation, str]] = []
        for relative in identity.relative_paths:
            observation = observe_file(self.anime_root, PurePosixPath(relative))
            if not observation.safe or observation.resolved is None or observation.size != event.source_size:
                continue
            # Shoko already hashed and owns this exact accessible path. Re-reading
            # whole media files here duplicates native work on every observation;
            # path, size, provider, Plex and physical stream gates remain separate.
            matches.append((observation, identity.sha1))
        if len(matches) != 1:
            relative = identity.relative_paths[0] if identity.relative_paths else filename
            return FileGateObservation(
                event.sonarr_file_id, False, identity.sha1, event.source_size,
                _failed_destination(self.anime_root, relative, "destination-not-unique-or-size-mismatch"),
                identity, False, _failed_media("destination-unavailable"),
                self.qbit.retains_download(event.download_id, event.dropped_path, event.source_size), False, False,
            )
        destination, observed_hash = matches[0]
        regular = identity.regular[0]
        exact_episode_identity = False
        if (
            expected_tmdb_show_id > 0
            and expected_tvdb_episode_id > 0
            and regular.shoko_series_id in expected_shoko_series_ids
        ):
            try:
                exact_episode_identity = self.shoko.has_exact_tmdb_episode_identity(
                    regular.shoko_episode_id, expected_tmdb_show_id,
                    expected_tvdb_episode_id,
                )
            except MissingTvdbEpisodeIdentity:
                try:
                    exact_episode_identity = self.shoko.preferred_tmdb_coordinate(
                        regular.shoko_episode_id, expected_tmdb_show_id,
                    ) == (expected_season_number, expected_episode_number)
                except (OSError, RuntimeError, ValueError):
                    pass
            except (OSError, RuntimeError, ValueError):
                pass
        semantic_identity_exact = (
            regular.shoko_series_id in expected_shoko_series_ids
            and exact_episode_identity is True
        )
        native_observer = getattr(self.plex, 'has_exact_identity_destination', None)
        if native_observer is not None:
            plex_exact = native_observer(
                destination.relative, identity, self.plex_section_ids, destination.resolved,
            )
        else:
            plex_exact = self.plex.has_exact_destination(
                destination.relative, regular.shoko_series_id, self.plex_section_ids
            )
        media = _with_exact_anidb_language_evidence(
            self.probe.probe(destination.resolved), identity,
        )
        return FileGateObservation(
            event.sonarr_file_id, False, observed_hash, event.source_size,
            destination, identity, plex_exact,
            media,
            self.qbit.retains_download(event.download_id, event.dropped_path, event.source_size),
            plex_exact, semantic_identity_exact,
        )

    def observe_series(self, series_id: int, events: tuple[ImportHistoryEvent, ...]) -> SeriesObservation:
        if series_id <= 0 or not events or any(event.series_id != series_id for event in events):
            raise ValueError("series reconciliation events are invalid")
        import_healthy = self._healthy_root(self.import_root)
        anime_healthy = self._healthy_root(self.anime_root)
        dependencies_healthy = self._dependencies_healthy(self.shoko, self.plex, self.qbit)
        roots = self.sonarr.root_folders()
        sonarr_series = self.sonarr.series(series_id)
        membership_complete = True
        sonarr_tmdb_id = 0
        expected_shoko_series_ids: frozenset[int] = frozenset()
        try:
            sonarr_tmdb_id = int(sonarr_series["tmdbId"])
            if sonarr_tmdb_id <= 0:
                raise ValueError("Sonarr TMDB identity is invalid")
            expected_shoko_series_ids = self.shoko.series_ids_for_tmdb(sonarr_tmdb_id)
        except (KeyError, TypeError, ValueError, OSError, RuntimeError):
            membership_complete = False
        episodes = self.sonarr.episodes(series_id)
        episodes_by_id: dict[int, dict] = {}
        for row in episodes:
            try:
                episode_id = int(row["id"])
                season_number = int(row["seasonNumber"])
                episode_number = int(row["episodeNumber"])
                tvdb_episode_id = int(row.get("tvdbId", 0))
            except (KeyError, TypeError, ValueError):
                raise ValueError("Sonarr episode coordinate is malformed") from None
            if (
                episode_id <= 0 or season_number < 0 or episode_number <= 0
                or episode_id in episodes_by_id
            ):
                raise ValueError("Sonarr episode coordinate is invalid")
            episodes_by_id[episode_id] = row
        files = self.sonarr.episode_files(series_id)
        files_by_id: dict[int, dict] = {}
        for row in files:
            try:
                file_id = int(row["id"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("Sonarr episode file is malformed") from None
            if file_id <= 0 or file_id in files_by_id:
                raise ValueError("Sonarr episode file identity is invalid")
            files_by_id[file_id] = row
        observations: list[FileGateObservation] = []
        complete = import_healthy and anime_healthy and dependencies_healthy and membership_complete
        seen_files: set[int] = set()
        for event in sorted(events, key=lambda item: item.history_id):
            if event.sonarr_file_id in seen_files:
                complete = False
                continue
            seen_files.add(event.sonarr_file_id)
            stale = files_by_id.get(event.sonarr_file_id)
            if stale is None:
                continue
            try:
                stale_path = str(stale["path"])
                stale_size = int(stale["size"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("Sonarr episode file is malformed") from None
            if stale_path != event.imported_path or stale_size != event.source_size:
                complete = False
                observations.append(FileGateObservation(
                    event.sonarr_file_id, False, "", event.source_size,
                    _failed_destination(self.anime_root, PurePosixPath(event.imported_path).name, "sonarr-history-file-mismatch"),
                    None, False, _failed_media("sonarr-history-file-mismatch"), False, False, False,
                ))
                continue
            try:
                source_relative = self._root_relative(event.imported_path, roots)
            except ValueError:
                complete = False
                observations.append(FileGateObservation(
                    event.sonarr_file_id, False, "", event.source_size,
                    _failed_destination(self.anime_root, PurePosixPath(event.imported_path).name, "sonarr-import-root-ambiguous"),
                    None, False, _failed_media("sonarr-import-root-ambiguous"), False, False, False,
                ))
                continue
            source = observe_file(self.import_root, source_relative)
            expected_episode = episodes_by_id.get(event.episode_id)
            if expected_episode is None:
                complete = False
                observations.append(FileGateObservation(
                    event.sonarr_file_id, source.exists, "", event.source_size,
                    _failed_destination(self.anime_root, PurePosixPath(event.imported_path).name,
                                        "sonarr-episode-coordinate-missing"),
                    None, False, _failed_media("sonarr-episode-coordinate-missing"),
                    False, False, False,
                ))
                continue
            expected_tvdb_episode_id = int(expected_episode.get("tvdbId", 0))
            if expected_tvdb_episode_id <= 0:
                complete = False
            row = (
                self._waiting_file(event, source)
                if source.exists
                else self._moved_file(
                    event, expected_shoko_series_ids, sonarr_tmdb_id,
                    expected_tvdb_episode_id,
                    int(expected_episode["seasonNumber"]),
                    int(expected_episode["episodeNumber"]),
                )
            )
            if source.exists and not source.safe:
                complete = False
            if not source.exists and not row.destination.safe:
                complete = False
            observations.append(row)
        delivered = {event.episode_id for event in events}
        future_fingerprint, unrelated_fingerprint = self._episode_snapshots(episodes, delivered, self.now())
        queue_rows = self.sonarr.queue()
        queue_empty = not _queue_conflicts(queue_rows, series_id, delivered)
        expected_file_ids = {event.sonarr_file_id for event in events}
        unexpected_attached_file_ids = tuple(sorted(set(files_by_id) - expected_file_ids))
        return SeriesObservation(
            series_id, complete, dependencies_healthy, import_healthy, anime_healthy,
            tuple(observations), future_fingerprint, unrelated_fingerprint, queue_empty,
            unexpected_attached_file_ids,
        )

    def verify_action(self, action: dict) -> ActionVerification:
        try:
            series_id = int(action["series_id"])
            command_id = int(action["command_id"])
            future_before = str(action["future_fingerprint"])
            unrelated_before = str(action["unrelated_fingerprint"])
            created_at = str(action["created_at"])
            action_id = int(action["id"])
            events = tuple(self.event_provider(action_id, series_id))
            if not events or any(event.series_id != series_id for event in events):
                raise ValueError("action event evidence is unavailable")
            command = self.sonarr.command(command_id)
            command_complete = (
                isinstance(command, dict) and int(command.get("id", 0)) == command_id
                and str(command.get("status", "")).lower() == "completed"
            )
            episode_files = self.sonarr.episode_files(series_id)
            current_file_ids = {int(row["id"]) for row in episode_files}
            stale_detached = all(event.sonarr_file_id not in current_file_ids for event in events)
            episodes = self.sonarr.episodes(series_id)
            episode_by_id = {int(row["id"]): row for row in episodes}
            delivered_unmonitored = all(
                event.episode_id in episode_by_id
                and episode_by_id[event.episode_id].get("monitored") is False
                and episode_by_id[event.episode_id].get("hasFile") is False
                and int(episode_by_id[event.episode_id].get("episodeFileId", 0)) == 0
                for event in events
            )
            delivered = {event.episode_id for event in events}
            future_now, unrelated_now = self._episode_snapshots(episodes, delivered, self.now())
            future_unchanged = future_now == future_before
            unrelated_unchanged = unrelated_now == unrelated_before
            queue_rows = self.sonarr.queue()
            queue_empty = not _queue_conflicts(queue_rows, series_id, delivered)
            recent = self.sonarr.history_since(created_at)
            reacquired = any(
                isinstance(row, dict)
                and str(row.get("eventType", "")) in {"grabbed", "downloadFolderImported"}
                and int(row.get("episodeId", 0)) in delivered
                for row in recent
            )
            no_reacquisition = queue_empty and not reacquired and stale_detached
            sonarr_series = self.sonarr.series(series_id)
            try:
                sonarr_tmdb_id = int(sonarr_series["tmdbId"])
                expected_shoko_series_ids = self.shoko.series_ids_for_tmdb(sonarr_tmdb_id)
            except (KeyError, TypeError, ValueError, OSError, RuntimeError):
                expected_shoko_series_ids = frozenset()
            destination_rows = []
            for event in events:
                expected_episode = episode_by_id.get(event.episode_id)
                if expected_episode is None:
                    raise ValueError("Sonarr episode coordinate is missing")
                try:
                    expected_tvdb_episode_id = int(expected_episode["tvdbId"])
                except (KeyError, TypeError, ValueError):
                    raise ValueError("Sonarr provider episode identity is malformed") from None
                if expected_tvdb_episode_id <= 0:
                    raise ValueError("Sonarr provider episode identity is invalid")
                destination_rows.append(self._moved_file(
                    event, expected_shoko_series_ids, sonarr_tmdb_id,
                    expected_tvdb_episode_id,
                    int(expected_episode["seasonNumber"]),
                    int(expected_episode["episodeNumber"]),
                ))
            qbit_retained = all(row.qbit_retained for row in destination_rows)
            destination_intact = all(
                row.destination.safe and row.shoko is not None and row.plex_playable
                and row.provider_identity_exact and row.semantic_identity_exact
                and row.media.compliant
                for row in destination_rows
            )
            reasons: list[str] = []
            if not command_complete:
                reasons.append("sonarr-command-incomplete")
            if not stale_detached:
                reasons.append("stale-file-still-attached")
            if not delivered_unmonitored:
                reasons.append("delivered-episode-not-unmonitored")
            if not future_unchanged:
                reasons.append("future-monitoring-changed")
            if not unrelated_unchanged:
                reasons.append("unrelated-state-changed")
            if not qbit_retained:
                reasons.append("torrent-retention-lost")
            if not destination_intact:
                reasons.append("shoko-plex-media-evidence-changed")
            if any(not row.semantic_identity_exact for row in destination_rows):
                reasons.append("sonarr-shoko-episode-mismatch")
            if not no_reacquisition:
                reasons.append("reacquisition-detected")
            if not queue_empty:
                reasons.append("series-queue-not-empty")
            complete = command_complete and destination_intact
            return ActionVerification(
                complete, stale_detached, delivered_unmonitored, future_unchanged,
                unrelated_unchanged, qbit_retained, no_reacquisition, queue_empty,
                tuple(reasons),
            )
        except (KeyError, TypeError, ValueError, OSError, RuntimeError):
            return ActionVerification(
                False, False, False, False, False, False, False, False,
                ("post-action-observation-incomplete",),
            )
