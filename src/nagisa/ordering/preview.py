"""Read-only comparison of current and candidate TMDB coordinates."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from nagisa.ordering.evaluator import Decision


@dataclass(frozen=True, slots=True, order=True)
class Coordinate:
    season: int
    episode: int


@dataclass(frozen=True, slots=True)
class EpisodePreviewRow:
    shoko_episode_id: int
    anidb_series_id: int
    anidb_episode_number: int
    kind: str
    file_backed: bool
    current: Coordinate | None
    candidate: Coordinate | None


@dataclass(frozen=True, slots=True)
class OrderingPreview:
    show_id: int | None
    current_ordering_id: str | None
    candidate_ordering_id: str | None
    outcome: str
    manual_protected: bool
    current_tree: Mapping[int, int]
    candidate_tree: Mapping[int, int]
    rows_by_anidb: Mapping[tuple[int, str, int], EpisodePreviewRow]
    reasons: tuple[str, ...]


def _candidate(evidence: Mapping[str, object], ordering_id: str | None) -> Mapping[str, object] | None:
    if ordering_id is None:
        return None
    for row in evidence.get("orderings", ()):  # type: ignore[union-attr]
        if str(row.get("id")) == ordering_id:  # type: ignore[union-attr]
            return row  # type: ignore[return-value]
    return None


def _coordinates(ordering: Mapping[str, object] | None) -> dict[int, Coordinate]:
    if ordering is None:
        return {}
    return {
        int(row["shoko_episode_id"]): Coordinate(int(row["season"]), int(row["episode"]))
        for row in ordering.get("episodes", ())  # type: ignore[union-attr]
    }


def _tree(coordinates: Mapping[int, Coordinate]) -> Mapping[int, int]:
    tree: dict[int, int] = {}
    for coordinate in coordinates.values():
        tree[coordinate.season] = max(tree.get(coordinate.season, 0), coordinate.episode)
    return MappingProxyType(dict(sorted(tree.items())))


def build_preview(
    evidence: Mapping[str, object],
    evaluation: Decision,
    candidate_ordering_id: str | None = None,
) -> OrderingPreview:
    current_id = evidence.get("current_preference")
    current_id = str(current_id) if current_id is not None else None
    candidate_id = candidate_ordering_id or evaluation.selected_ordering_id or current_id
    current_coordinates = _coordinates(_candidate(evidence, current_id))
    candidate_coordinates = _coordinates(_candidate(evidence, candidate_id))
    rows: dict[tuple[int, str, int], EpisodePreviewRow] = {}
    reasons: set[str] = set()
    for source in evidence.get("required_episodes", ()):  # type: ignore[union-attr]
        shoko_id = int(source["shoko_episode_id"])  # type: ignore[index]
        kind = str(source["kind"])  # type: ignore[index]
        key = (int(source["series_id"]), kind, int(source["series_episode_number"]))  # type: ignore[index]
        if key in rows:
            reasons.add("duplicate-anidb-coordinate")
        rows[key] = EpisodePreviewRow(
            shoko_episode_id=shoko_id,
            anidb_series_id=key[0],
            anidb_episode_number=key[2],
            kind=kind,
            file_backed=bool(source.get("file_backed")),  # type: ignore[union-attr]
            current=current_coordinates.get(shoko_id),
            candidate=candidate_coordinates.get(shoko_id),
        )
    if current_id is None:
        reasons.add("missing-current-preference")
    if candidate_id is None:
        reasons.add("no-unique-candidate")
    for failure in evaluation.failures.get(candidate_id or "", ()):
        reasons.add(failure)
    return OrderingPreview(
        show_id=int(evidence["show_id"]) if evidence.get("show_id") is not None else None,
        current_ordering_id=current_id,
        candidate_ordering_id=candidate_id,
        outcome=evaluation.outcome,
        manual_protected=current_id not in {None, "default"},
        current_tree=_tree(current_coordinates),
        candidate_tree=_tree(candidate_coordinates),
        rows_by_anidb=MappingProxyType(dict(sorted(rows.items()))),
        reasons=tuple(sorted(reasons)),
    )
