"""Pure ordering evaluation; this module performs no I/O or mutation."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class Decision:
    outcome: str
    selected_ordering_id: str | None
    passing_ordering_ids: tuple[str, ...]
    failures: Mapping[str, tuple[str, ...]]

    def normalized(self) -> tuple[object, ...]:
        return (
            self.outcome,
            self.selected_ordering_id,
            self.passing_ordering_ids,
            tuple(sorted((key, tuple(value)) for key, value in self.failures.items())),
        )


class OrderingEvaluator:
    """Object interface used by Nagisa services and WebUI previews."""

    def evaluate(self, evidence: Mapping[str, object]) -> Decision:
        return evaluate_orderings(
            evidence["required_episodes"],  # type: ignore[arg-type]
            evidence["orderings"],  # type: ignore[arg-type]
            evidence["provider_structure"],  # type: ignore[arg-type]
        )


def _required_by_id(required_episodes: Sequence[Mapping[str, object]]) -> dict[int, Mapping[str, object]]:
    indexed: dict[int, Mapping[str, object]] = {}
    for episode in required_episodes:
        episode_id = int(episode["shoko_episode_id"])
        if episode_id <= 0 or episode_id in indexed:
            raise ValueError("required Shoko episode IDs must be unique positive integers")
        if episode.get("kind") not in {"regular", "special"}:
            raise ValueError("required episode kind must be regular or special")
        if int(episode["series_id"]) <= 0 or int(episode["series_episode_number"]) <= 0:
            raise ValueError("required series identity and local number must be positive")
        indexed[episode_id] = episode
    if not indexed:
        raise ValueError("at least one required episode is needed")
    return indexed


def _provider_coordinates(
    provider_structure: Mapping[str, object] | None,
    required_regular_count: int,
) -> frozenset[tuple[int, int]] | None:
    if (
        provider_structure is None
        or provider_structure.get("evidence_complete") is not True
        or required_regular_count <= 0
    ):
        return None
    try:
        tmdb_id = int(provider_structure["tmdb_id"])
        series_id = int(provider_structure["series_id"])
        rows = provider_structure["coordinates"]
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            return None
        coordinates = [
            (int(row["season"]), int(row["episode"]))  # type: ignore[index]
            for row in rows
        ]
    except (KeyError, TypeError, ValueError):
        return None
    if tmdb_id <= 0 or series_id <= 0 or not coordinates:
        return None
    if any(season <= 0 or episode <= 0 for season, episode in coordinates):
        return None
    if len(coordinates) != len(set(coordinates)):
        return None
    seasons = sorted({season for season, _ in coordinates})
    if seasons != list(range(1, max(seasons) + 1)):
        return None
    for season in seasons:
        episodes = sorted(episode for candidate, episode in coordinates if candidate == season)
        if episodes != list(range(1, max(episodes) + 1)):
            return None
    if len(coordinates) < required_regular_count:
        return None
    return frozenset(sorted(coordinates)[:required_regular_count])


def _evaluate_one(
    required: Mapping[int, Mapping[str, object]],
    ordering: Mapping[str, object],
    provider_coordinates: frozenset[tuple[int, int]] | None = None,
) -> tuple[str, ...]:
    failures: set[str] = set()
    rows = list(ordering.get("episodes", ()))
    by_shoko: dict[int, Mapping[str, object]] = {}
    provider_ids: list[int] = []
    coordinates: list[tuple[int, int]] = []

    for row in rows:
        shoko_id = int(row["shoko_episode_id"])
        if shoko_id in by_shoko:
            failures.add("reused_shoko_episode")
        else:
            by_shoko[shoko_id] = row
        provider_id = int(row["provider_episode_id"])
        season = int(row["season"])
        episode = int(row["episode"])
        if provider_id <= 0:
            failures.add("invalid_provider_episode")
        provider_ids.append(provider_id)
        coordinates.append((season, episode))

    required_ids = set(required)
    present_ids = set(by_shoko)
    if required_ids - present_ids:
        failures.add("missing_required_episode")
    if present_ids - required_ids:
        failures.add("unexpected_shoko_episode")
    if len(provider_ids) != len(set(provider_ids)):
        failures.add("reused_provider_episode")
    if len(coordinates) != len(set(coordinates)):
        failures.add("coordinate_collision")

    regular_target_seasons: dict[int, set[int]] = {}
    regular_numbers_by_season: dict[int, set[int]] = {}
    candidate_regular_coordinates: set[tuple[int, int]] = set()
    for shoko_id in sorted(required_ids & present_ids):
        source = required[shoko_id]
        row = by_shoko[shoko_id]
        season = int(row["season"])
        episode = int(row["episode"])
        if bool(row.get("hidden")) and (bool(source.get("file_backed")) or bool(source.get("linked"))):
            failures.add("hidden_required_episode")

        if source["kind"] == "special":
            if season != 0 or episode <= 0:
                failures.add("special_not_season_zero")
            continue

        if season <= 0 or episode <= 0:
            failures.add("regular_episode_not_positive")
            continue
        candidate_regular_coordinates.add((season, episode))
        series_id = int(source["series_id"])
        local_number = int(source["series_episode_number"])
        regular_target_seasons.setdefault(series_id, set()).add(season)
        regular_numbers_by_season.setdefault(season, set()).add(episode)
        if provider_coordinates is None and episode != local_number:
            failures.add("series_local_number_drift")

    if provider_coordinates is None:
        for target_seasons in regular_target_seasons.values():
            if len(target_seasons) != 1:
                failures.add("series_spans_target_seasons")
        one_season_per_series = [next(iter(values)) for values in regular_target_seasons.values() if len(values) == 1]
        if len(one_season_per_series) != len(set(one_season_per_series)):
            failures.add("target_season_reused")
        if one_season_per_series:
            unique_seasons = sorted(set(one_season_per_series))
            if unique_seasons != list(range(1, max(unique_seasons) + 1)):
                failures.add("target_season_gap")
    elif candidate_regular_coordinates != provider_coordinates:
        failures.add("provider_coordinate_mismatch")
    for numbers in regular_numbers_by_season.values():
        if numbers and sorted(numbers) != list(range(1, max(numbers) + 1)):
            failures.add("season_coordinate_gap")

    return tuple(sorted(failures))


def evaluate_orderings(
    required_episodes: Sequence[Mapping[str, object]],
    orderings: Iterable[Mapping[str, object]],
    provider_structure: Mapping[str, object] | None = None,
) -> Decision:
    """Return a deterministic decision without changing Shoko or any media."""

    required = _required_by_id(required_episodes)
    candidates = list(orderings)
    defaults = [candidate for candidate in candidates if bool(candidate.get("is_default"))]
    if len(defaults) != 1 or not candidates:
        return Decision("INCOMPLETE_EVIDENCE", None, (), MappingProxyType({}))

    required_regular_count = sum(
        1 for row in required.values() if row["kind"] == "regular"
    )
    provider_coordinates = _provider_coordinates(
        provider_structure, required_regular_count
    )
    if provider_coordinates is None:
        return Decision("INCOMPLETE_EVIDENCE", None, (), MappingProxyType({}))

    evaluated: dict[str, tuple[str, ...]] = {}
    complete: dict[str, bool] = {}
    conclusive_incomplete_rejections: dict[str, bool] = {}
    ordered_ids: list[str] = []
    required_regular_ids = {
        shoko_id for shoko_id, row in required.items() if row["kind"] == "regular"
    }
    for candidate in candidates:
        ordering_id = str(candidate.get("id", ""))
        if not ordering_id or ordering_id in evaluated:
            return Decision("INCOMPLETE_EVIDENCE", None, (), MappingProxyType({}))
        ordered_ids.append(ordering_id)
        complete[ordering_id] = bool(candidate.get("evidence_complete"))
        failures = set(_evaluate_one(required, candidate, provider_coordinates))
        if not complete[ordering_id]:
            failures.add("incomplete_evidence")
        evaluated[ordering_id] = tuple(sorted(failures))
        present_regular_ids = {
            int(row["shoko_episode_id"])
            for row in candidate.get("episodes", ())
            if int(row["shoko_episode_id"]) in required_regular_ids
        }
        conclusive_incomplete_rejections[ordering_id] = (
            not complete[ordering_id]
            and present_regular_ids == required_regular_ids
            and "provider_coordinate_mismatch" in failures
        )

    default_id = str(defaults[0]["id"])
    passing = tuple(ordering_id for ordering_id in ordered_ids if complete[ordering_id] and not evaluated[ordering_id])
    frozen_failures = MappingProxyType(dict(evaluated))
    if default_id in passing:
        return Decision("PASS_DEFAULT", None, passing, frozen_failures)
    if not any(complete.values()) or any(
        not complete[ordering_id]
        and not conclusive_incomplete_rejections[ordering_id]
        for ordering_id in ordered_ids
    ):
        return Decision("INCOMPLETE_EVIDENCE", None, passing, frozen_failures)
    alternates = tuple(ordering_id for ordering_id in passing if ordering_id != default_id)
    if len(alternates) == 1:
        return Decision("SELECT_ALTERNATE", alternates[0], passing, frozen_failures)
    return Decision("REVIEW_REQUIRED", None, passing, frozen_failures)
