"""Persistent history-driven orchestration with a two-observation gate."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from nagisa.reconciler.models import ImportHistoryEvent, QueueStatus, SeriesDecision
from nagisa.reconciler.planner import ReconciliationPlanner


class Trigger(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"


_PERSISTENT_REVIEW_REASONS = frozenset({
    "below-resolution-floor",
    "missing-full-english-subtitle",
    "missing-japanese-audio",
    "provider-identity-not-exact",
    "regular-provider-conflict",
    "sonarr-shoko-episode-mismatch",
    "sonarr-shoko-series-mismatch",
    "unsafe-attachment",
})


@dataclass(frozen=True, slots=True)
class CycleResult:
    ready: int = 0
    waiting: int = 0
    blocked: int = 0
    completed: int = 0
    skipped: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _track_evidence(track) -> dict:
    return {
        "codec": track.codec,
        "language": track.language,
        "title": track.title,
        "default": track.default,
        "forced": track.forced,
    }


def _observation_evidence(observation, decision) -> dict:
    files = []
    for row in sorted(observation.files, key=lambda item: item.sonarr_file_id):
        relative = row.destination.relative.lstrip("/")
        files.append({
            "sonarr_file_id": row.sonarr_file_id,
            "evidence_path": f"/Anime/{relative}" if relative else "/Anime",
            "evidence_id": decision.fingerprint,
            "policy": {
                "compliant": row.media.compliant,
                "probe_ok": row.media.probe_ok,
                "japanese_audio": row.media.japanese_audio,
                "full_english_subtitle": row.media.full_english_subtitle,
                "unsafe_attachment": row.media.unsafe_attachment,
                "meets_resolution_floor": row.media.meets_resolution_floor,
                "height": row.media.height,
                "reasons": list(row.media.reasons),
            },
            "audio_tracks": [_track_evidence(track) for track in row.media.audio_tracks],
            "subtitle_tracks": [_track_evidence(track) for track in row.media.subtitle_tracks],
        })
    return {
        "status": decision.status.value,
        "reasons": list(decision.reasons),
        "rescan_preflight": {
            "exact_event_cohort": not observation.unexpected_attached_file_ids,
            "unexpected_attached_file_ids": list(observation.unexpected_attached_file_ids),
        },
        "files": files,
    }


class ReconcilerService:
    def __init__(self, state, sonarr, observer, *, mode: str,
                 planner: ReconciliationPlanner | None = None, now=_now,
                 audit=None) -> None:
        if mode not in {"report", "enabled"}:
            raise ValueError("reconciler mode must be report or enabled")
        self.state = state
        self.sonarr = sonarr
        self.observer = observer
        self.mode = mode
        self.planner = planner or ReconciliationPlanner()
        self.now = now
        self.audit = audit
        self._cycle_lock = threading.Lock()

    def run_once(self, trigger: Trigger) -> CycleResult:
        if not isinstance(trigger, Trigger):
            raise ValueError("invalid reconciler trigger")
        if not self._cycle_lock.acquire(blocking=False):
            return CycleResult(skipped=True)
        try:
            compatible, _, _ = self.sonarr.compatibility()
            if not compatible:
                return CycleResult(blocked=max(1, len(self.state.pending_series_ids())))
            watermark = self.state.watermark()
            new_events = self.sonarr.import_history_after(watermark)
            timestamp = self.now()
            for event in new_events:
                if self.sonarr.accepts_import(event):
                    self.state.ingest_event(event, recorded_at=timestamp)
                elif self.audit is not None:
                    self.audit.record(
                        "reconciler", "event-skipped",
                        {"history_id": event.history_id, "series_id": event.series_id,
                         "reason": "outside-shoko-import-root"},
                    )
            if new_events:
                self.state.set_watermark(max(event.history_id for event in new_events))
            counts = {QueueStatus.READY: 0, QueueStatus.WAITING: 0, QueueStatus.BLOCKED: 0, QueueStatus.COMPLETED: 0}
            active_action_series: set[int] = set()
            for action in self.state.pending_actions():
                verification = self.observer.verify_action(action)
                if not verification.complete:
                    active_action_series.add(int(action["series_id"]))
                    counts[QueueStatus.WAITING] += 1
                    continue
                verification_json = json.dumps(
                    {"successful": verification.successful, "reasons": verification.reasons},
                    sort_keys=True, separators=(",", ":"),
                )
                self.state.finish_action(
                    int(action["id"]), int(action["series_id"]), verification.successful,
                    verification_json, updated_at=timestamp,
                )
                if self.audit is not None:
                    self.audit.record(
                        "reconciler",
                        "action-verification",
                        {
                            "series_id": int(action["series_id"]),
                            "command_id": int(action["command_id"]),
                            "successful": verification.successful,
                            "reasons": list(verification.reasons),
                        },
                    )
                counts[QueueStatus.COMPLETED if verification.successful else QueueStatus.BLOCKED] += 1
            for series_id, payloads in self.state.pending_events_by_series().items():
                if series_id in active_action_series:
                    counts[QueueStatus.WAITING] += 1
                    continue
                events = tuple(ImportHistoryEvent(
                    int(row["history_id"]), int(row["series_id"]), int(row["episode_id"]),
                    int(row["sonarr_file_id"]), str(row["download_id"]),
                    str(row["imported_path"]), str(row["dropped_path"]), int(row["source_size"]),
                ) for row in payloads)
                prior_status, prior_reasons = self.state.latest_series_status(series_id)
                observation = self.observer.observe_series(series_id, events)
                decision = self.planner.evaluate(observation)
                if (
                    decision.status == QueueStatus.COMPLETED
                    and not observation.files
                    and prior_status == QueueStatus.BLOCKED.value
                ):
                    review_reasons = tuple(sorted(
                        set(prior_reasons) & _PERSISTENT_REVIEW_REASONS
                    ))
                    if review_reasons:
                        decision = SeriesDecision(
                            series_id,
                            QueueStatus.BLOCKED,
                            review_reasons,
                            decision.fingerprint,
                        )
                evidence = json.dumps(
                    _observation_evidence(observation, decision),
                    sort_keys=True, separators=(",", ":"),
                )
                self.state.record_observation(series_id, decision.fingerprint, observation.observation_complete, evidence, observed_at=timestamp)
                prior = self.state.last_observation_fingerprint(series_id)
                effective = decision.status
                if decision.status == QueueStatus.READY and prior != decision.fingerprint:
                    effective = QueueStatus.WAITING
                counts[effective] += 1
                self.state.update_series(series_id, effective.value, decision.fingerprint, updated_at=timestamp)
                if decision.status == QueueStatus.READY and prior == decision.fingerprint and self.mode == "enabled":
                    command_id = self.sonarr.rescan_series(series_id)
                    self.state.record_action(
                        series_id, command_id, decision.fingerprint,
                        observation.future_fingerprint, observation.unrelated_fingerprint,
                        tuple(event.history_id for event in events),
                        created_at=timestamp,
                    )
                    if self.audit is not None:
                        self.audit.record(
                            "reconciler",
                            "rescan-issued",
                            {
                                "series_id": series_id,
                                "command_id": command_id,
                                "fingerprint": decision.fingerprint,
                            },
                        )
            result = CycleResult(
                ready=counts[QueueStatus.READY], waiting=counts[QueueStatus.WAITING],
                blocked=counts[QueueStatus.BLOCKED], completed=counts[QueueStatus.COMPLETED],
            )
            if self.audit is not None:
                self.audit.record(
                    "reconciler",
                    "cycle",
                    {
                        "trigger": trigger.value,
                        "mode": self.mode,
                        "watermark": self.state.watermark(),
                        "ready": result.ready,
                        "waiting": result.waiting,
                        "blocked": result.blocked,
                        "completed": result.completed,
                    },
                )
            return result
        finally:
            self._cycle_lock.release()

    def status_snapshot(self) -> dict:
        queues = {status.value: [] for status in QueueStatus}
        status_map = {
            "pending": QueueStatus.WAITING,
            "action-issued": QueueStatus.WAITING,
            "verification-blocked": QueueStatus.BLOCKED,
            "verified": QueueStatus.COMPLETED,
        }
        for row in self.state.series_status_rows():
            series_id = int(row["series_id"])
            row["title"] = f"Sonarr item {series_id}"
            row["episodes"] = []
            try:
                series = self.sonarr.series(series_id)
                if isinstance(series.get("title"), str) and series["title"].strip():
                    row["title"] = series["title"].strip()
                episode_ids = {int(value) for value in row.get("episode_ids", [])}
                for episode in self.sonarr.episodes(series_id):
                    episode_id = int(episode.get("id", 0))
                    if episode_id not in episode_ids:
                        continue
                    season_number = int(episode.get("seasonNumber", 0))
                    episode_number = int(episode.get("episodeNumber", 0))
                    title = str(episode.get("title") or "Untitled episode")
                    row["episodes"].append({
                        "episode_id": episode_id,
                        "coordinate": f"S{season_number:02d}E{episode_number:02d}",
                        "title": title,
                    })
            except (KeyError, TypeError, ValueError, OSError, RuntimeError):
                pass
            raw = row["status"]
            try:
                status = QueueStatus(raw)
            except ValueError:
                status = status_map.get(raw, QueueStatus.BLOCKED)
            queues[status.value].append(row)
        return {
            "mode": self.mode,
            "watermark": self.state.watermark(),
            "pending_series": self.state.pending_series_ids(),
            "queues": queues,
        }
