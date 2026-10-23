"""One-at-a-time, bounded ffprobe policy evidence."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from nagisa.reconciler.models import MediaPolicyObservation, MediaTrackEvidence


_JAPANESE = {"ja", "jpn", "japanese"}
_ENGLISH = {"en", "eng", "english"}
_UNSAFE_EXTENSIONS = {".exe", ".com", ".bat", ".cmd", ".ps1", ".js", ".vbs", ".scr", ".msi", ".dll", ".sh"}


def _language(stream: dict[str, Any]) -> str:
    return str(stream.get("tags", {}).get("language", "")).strip().lower()


def _bounded_text(value: object, *, fallback: str = "", maximum: int = 200) -> str:
    text = " ".join(str(value or "").split())
    return (text or fallback)[:maximum]


def _track(stream: dict[str, Any]) -> MediaTrackEvidence:
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
    return MediaTrackEvidence(
        codec=_bounded_text(stream.get("codec_name"), fallback="unknown", maximum=40).lower(),
        language=_bounded_text(tags.get("language"), fallback="und", maximum=40).lower(),
        title=_bounded_text(tags.get("title")),
        default=bool(disposition.get("default", 0)),
        forced=bool(disposition.get("forced", 0)),
    )


class MediaProbe:
    _lock = threading.Lock()

    def __init__(self, *, minimum_height: int = 720, timeout: int = 120,
                 maximum_output: int = 16 * 1024 * 1024,
                 runner: Callable[..., Any] = subprocess.run) -> None:
        if minimum_height < 1 or timeout < 1 or maximum_output < 1024:
            raise ValueError("invalid media probe bounds")
        self.minimum_height = minimum_height
        self.timeout = timeout
        self.maximum_output = maximum_output
        self.runner = runner

    @staticmethod
    def _failed(reason: str) -> MediaPolicyObservation:
        return MediaPolicyObservation(False, False, False, False, False, None, (reason,))

    def probe(self, path: Path) -> MediaPolicyObservation:
        arguments = [
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(path),
        ]
        try:
            with self._lock:
                completed = self.runner(
                    arguments,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout,
                    check=False,
                    shell=False,
                )
        except subprocess.TimeoutExpired:
            return self._failed("probe-timeout")
        except (OSError, subprocess.SubprocessError):
            return self._failed("probe-execution-failed")
        stdout = completed.stdout if isinstance(completed.stdout, bytes) else str(completed.stdout).encode("utf-8")
        if completed.returncode != 0:
            return self._failed("probe-nonzero-exit")
        if len(stdout) > self.maximum_output:
            return self._failed("probe-output-too-large")
        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._failed("probe-malformed-json")
        return self.from_json(payload)

    def from_json(self, payload: object) -> MediaPolicyObservation:
        streams = payload.get("streams") if isinstance(payload, dict) else None
        if not isinstance(streams, list) or not all(isinstance(row, dict) for row in streams):
            return self._failed("probe-shape-invalid")
        audio = [row for row in streams if row.get("codec_type") == "audio"]
        subtitles = [row for row in streams if row.get("codec_type") == "subtitle"]
        video = [row for row in streams if row.get("codec_type") == "video"]
        attachments = [row for row in streams if row.get("codec_type") == "attachment"]
        japanese = any(_language(row) in _JAPANESE for row in audio)
        full_english = False
        for row in subtitles:
            if _language(row) not in _ENGLISH:
                continue
            title = str(row.get("tags", {}).get("title", "")).lower()
            forced = bool(row.get("disposition", {}).get("forced", 0))
            if not forced and not any(marker in title for marker in ("forced", "sign", "song")):
                full_english = True
        unsafe_attachment = False
        for row in attachments:
            filename = str(row.get("tags", {}).get("filename", "")).lower()
            if Path(filename).suffix in _UNSAFE_EXTENSIONS:
                unsafe_attachment = True
        heights: list[int] = []
        for row in video:
            try:
                heights.append(int(row.get("height", 0)))
            except (TypeError, ValueError):
                pass
        height = max(heights) if heights else None
        floor = height is not None and height >= self.minimum_height
        reasons: list[str] = []
        if not japanese:
            reasons.append("missing-japanese-audio")
        if not full_english:
            reasons.append("missing-full-english-subtitle")
        if unsafe_attachment:
            reasons.append("unsafe-attachment")
        if not floor:
            reasons.append("below-resolution-floor")
        return MediaPolicyObservation(
            True, japanese, full_english, unsafe_attachment, floor, height, tuple(reasons),
            tuple(_track(row) for row in audio), tuple(_track(row) for row in subtitles),
        )
