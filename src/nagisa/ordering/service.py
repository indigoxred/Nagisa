"""Future-only orchestration around the pure ordering evaluator."""

from __future__ import annotations

import threading
from typing import Any, Callable

from .evidence import evidence_fingerprint
from .evaluator import Decision, evaluate_orderings
from .preview import Coordinate, build_preview
from .shoko import ShokoClientError
from nagisa.state import OrderingState


class UndoRefused(RuntimeError):
    pass


class ApplyRefused(RuntimeError):
    pass


LATE_DISCOVERY_LOCK = "Plex presented the show before selector decision"


class OrderingService:
    def __init__(
        self,
        store: OrderingState,
        shoko: Any,
        plex_observer: Any,
        provider_observer: Any,
        *,
        mode: str,
        now: Callable[[], str],
        evidence_archive: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        if mode not in {"report", "enabled"}:
            raise ValueError("selector mode must be report or enabled")
        self.store = store
        self.shoko = shoko
        self.plex_observer = plex_observer
        self.provider_observer = provider_observer
        self.mode = mode
        self.now = now
        self.evidence_archive = evidence_archive
        self.audit = audit
        self._poll_lock = threading.Lock()

    @staticmethod
    def _fingerprint(inputs: dict[str, Any]) -> str:
        return evidence_fingerprint(inputs)

    def _inputs(self, show_id: int) -> dict[str, Any]:
        inputs = dict(self.shoko.get_evaluation_inputs(show_id))
        inputs["provider_structure"] = self.provider_observer.ordering_evidence(show_id)
        return inputs

    def _record(
        self,
        show_id: int,
        decision: Decision | str,
        fingerprint: str,
        prior: str | None,
        selected: str | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> int:
        outcome = decision.outcome if isinstance(decision, Decision) else decision
        selected_ordering = (
            decision.selected_ordering_id if isinstance(decision, Decision) else selected
        )
        if self.evidence_archive is not None:
            if inputs is None:
                raise ValueError("normalized evidence is required for archival")
            self.evidence_archive.record(show_id, fingerprint, inputs)
        decision_id = self.store.record_decision(
            show_id,
            outcome=outcome,
            evidence_fingerprint=fingerprint,
            prior_ordering=prior,
            selected_ordering=selected_ordering,
            recorded_at=self.now(),
        )
        if self.audit is not None:
            self.audit.record(
                "ordering",
                "decision",
                {
                    "show_id": show_id,
                    "decision_id": decision_id,
                    "outcome": outcome,
                    "fingerprint": fingerprint,
                    "prior_ordering": prior,
                    "selected_ordering": selected_ordering,
                },
            )
        return decision_id

    @staticmethod
    def _latest_active_applied(events: list[dict]) -> dict | None:
        active: dict | None = None
        for event in events:
            if event["event_type"] == "APPLIED":
                active = event
            elif event["event_type"] == "UNDO":
                active = None
        return active

    def _evaluate_show(self, show_id: int, *, force: bool = False) -> int | None:
        first = self._inputs(show_id)
        preference = first.get("current_preference")
        self.store.discover(
            show_id, current_preference=preference, observed_at=self.now()
        )
        state = self.store.show(show_id)
        if state["historical"]:
            if not force:
                return None
            fingerprint = self._fingerprint(first)
            second = self._inputs(show_id)
            second_fingerprint = self._fingerprint(second)
            if (
                second_fingerprint != fingerprint
                or second.get("current_preference") != preference
            ):
                return self._record(
                    show_id,
                    "INCOMPLETE_EVIDENCE",
                    second_fingerprint,
                    preference,
                    inputs=second,
                )
            decision = evaluate_orderings(
                first["required_episodes"], first["orderings"], first["provider_structure"]
            )
            return self._record(
                show_id,
                "HISTORICAL_REVIEW",
                fingerprint,
                preference,
                selected=decision.selected_ordering_id,
                inputs=first,
            )
        late_protected = bool(
            state["locked"] and state["lock_reason"] == LATE_DISCOVERY_LOCK
        )
        if state["locked"] and not late_protected:
            return None
        if preference is None:
            fingerprint = self._fingerprint(first)
            return self._record(
                show_id, "INCOMPLETE_EVIDENCE", fingerprint, None, inputs=first
            )

        latest = self.store.latest_decision(show_id)
        if latest is not None:
            active_applied = self._latest_active_applied(
                self.store.events(latest["id"])
            )
            if active_applied is not None:
                if preference == active_applied["ordering_value"]:
                    return None
                self.store.lock_show(
                    show_id,
                    "current preference drifted after selector apply",
                    recorded_at=self.now(),
                )
                return None

        if state["initial_preference"] != "default" or preference != "default":
            self.store.lock_show(
                show_id,
                "manual or non-default preference takes precedence",
                recorded_at=self.now(),
            )
            return None

        fingerprint = self._fingerprint(first)
        series_ids = {
            int(row["series_id"]) for row in first["required_episodes"]
        }
        presented = self.plex_observer.has_presented(show_id, series_ids)
        if late_protected:
            presented = True
        elif presented is None:
            return self._record(
                show_id, "INCOMPLETE_EVIDENCE", fingerprint, preference, inputs=first
            )
        if presented:
            decision = evaluate_orderings(
                first["required_episodes"], first["orderings"], first["provider_structure"]
            )
            second = self._inputs(show_id)
            second_fingerprint = self._fingerprint(second)
            stable = (
                second_fingerprint == fingerprint
                and second.get("current_preference") == preference
            )
            outcome = (
                "LATE_DISCOVERY_REVIEW"
                if stable and decision.outcome != "INCOMPLETE_EVIDENCE"
                else "INCOMPLETE_EVIDENCE"
            )
            selected = decision.selected_ordering_id if stable else None
            if (
                latest is not None
                and latest["evidence_fingerprint"] == (
                    fingerprint if stable else second_fingerprint
                )
                and latest["outcome"] == outcome
                and latest["selected_ordering"] == selected
            ):
                return None
            decision_id = self._record(
                show_id,
                outcome,
                fingerprint if stable else second_fingerprint,
                preference,
                selected=selected,
                inputs=first if stable else second,
            )
            if not late_protected:
                self.store.lock_show(
                    show_id,
                    LATE_DISCOVERY_LOCK,
                    recorded_at=self.now(),
                )
            return decision_id

        decision = evaluate_orderings(
            first["required_episodes"], first["orderings"], first["provider_structure"]
        )
        second = self._inputs(show_id)
        second_fingerprint = self._fingerprint(second)
        if second_fingerprint != fingerprint or second.get("current_preference") != preference:
            return self._record(
                show_id, "INCOMPLETE_EVIDENCE", second_fingerprint, preference, inputs=second
            )

        if latest is not None and not force:
            same_evidence = latest["evidence_fingerprint"] == fingerprint
            terminal = latest["outcome"] in {
                "PASS_DEFAULT",
                "REVIEW_REQUIRED",
                "LATE_DISCOVERY_REVIEW",
            }
            report_selection = (
                self.mode == "report"
                and latest["outcome"] == "SELECT_ALTERNATE"
            )
            if same_evidence and (terminal or report_selection):
                return None

        compatibility = self.shoko.compatibility()
        if not compatibility.supported and decision.outcome == "SELECT_ALTERNATE":
            return self._record(
                show_id, "INCOMPLETE_EVIDENCE", fingerprint, preference, inputs=first
            )

        decision_id = self._record(
            show_id, decision, fingerprint, preference, inputs=first
        )
        if decision.outcome != "SELECT_ALTERNATE" or self.mode == "report":
            return decision_id

        selected = decision.selected_ordering_id
        if selected is None:
            return decision_id
        try:
            self.shoko.set_preferred(show_id, selected)
        except ShokoClientError:
            self.store.lock_show(
                show_id,
                "selector mutation failed verification; automatic retry forbidden",
                recorded_at=self.now(),
            )
            return decision_id
        after = self._inputs(show_id)
        if (
            after.get("current_preference") != selected
            or self._fingerprint(after) != fingerprint
        ):
            return decision_id
        self.store.record_applied(
            decision_id,
            evidence_fingerprint=fingerprint,
            ordering=selected,
            recorded_at=self.now(),
        )
        if self.audit is not None:
            self.audit.record(
                "ordering",
                "apply-verified",
                {"show_id": show_id, "decision_id": decision_id, "ordering": selected},
            )
        return decision_id

    def poll_once(self) -> list[int]:
        if not self._poll_lock.acquire(blocking=False):
            return []
        try:
            shows = self.shoko.list_shows()
            show_ids = sorted(int(row["ID"]) for row in shows)
            if not self.store.baseline_initialized():
                baseline = [
                    (show_id, self.shoko.get_current_preference(show_id))
                    for show_id in show_ids
                ]
                self.store.record_initial_baseline(
                    baseline, recorded_at=self.now()
                )
                return []
            decisions: list[int] = []
            historical = set(self.store.baseline())
            for show_id in show_ids:
                if show_id in historical:
                    continue
                try:
                    decision_id = self._evaluate_show(show_id)
                except (ShokoClientError, KeyError, TypeError, ValueError):
                    continue
                if decision_id is not None:
                    decisions.append(decision_id)
            return decisions
        finally:
            self._poll_lock.release()

    def evaluate_now(self, show_id: int) -> int | None:
        with self._poll_lock:
            return self._evaluate_show(show_id, force=True)

    def apply_now(self, show_id: int) -> int:
        """Apply one operator-reviewed recommendation without relaxing auto guards."""
        if not isinstance(show_id, int) or show_id <= 0:
            raise ValueError("show ID must be a positive integer")
        with self._poll_lock:
            if self.mode != "enabled":
                raise ApplyRefused("ordering writes are not enabled")

            first = self._inputs(show_id)
            preference = first.get("current_preference")
            self.store.discover(
                show_id, current_preference=preference, observed_at=self.now()
            )
            state = self.store.show(show_id)
            if state["locked"] and state["lock_reason"] != LATE_DISCOVERY_LOCK:
                raise ApplyRefused("show is locked by an operator or safety guard")
            if state["initial_preference"] != "default" or preference != "default":
                raise ApplyRefused(
                    "manual or non-default preference takes precedence"
                )

            compatibility = self.shoko.compatibility()
            if not compatibility.supported:
                raise ApplyRefused("Shoko version or schema is not write-compatible")

            fingerprint = self._fingerprint(first)
            decision = evaluate_orderings(
                first["required_episodes"], first["orderings"], first["provider_structure"]
            )
            if decision.outcome == "INCOMPLETE_EVIDENCE":
                raise ApplyRefused("complete ordering evidence is required")
            if decision.outcome != "SELECT_ALTERNATE":
                raise ApplyRefused("exactly one safe alternate ordering is required")
            selected = decision.selected_ordering_id
            if selected is None:
                raise ApplyRefused("exactly one safe alternate ordering is required")

            second = self._inputs(show_id)
            second_fingerprint = self._fingerprint(second)
            second_decision = evaluate_orderings(
                second["required_episodes"], second["orderings"], second["provider_structure"]
            )
            if (
                second_fingerprint != fingerprint
                or second.get("current_preference") != preference
                or second_decision.outcome != "SELECT_ALTERNATE"
                or second_decision.selected_ordering_id != selected
            ):
                raise ApplyRefused("ordering evidence changed during the safety re-check")

            decision_id = self._record(
                show_id, decision, fingerprint, preference, inputs=first
            )
            try:
                self.shoko.set_preferred(show_id, selected)
            except ShokoClientError as error:
                self.store.lock_show(
                    show_id,
                    "selector mutation failed verification; automatic retry forbidden",
                    recorded_at=self.now(),
                )
                raise ApplyRefused("Shoko rejected the ordering change") from error

            after = self._inputs(show_id)
            if (
                after.get("current_preference") != selected
                or self._fingerprint(after) != fingerprint
            ):
                self.store.lock_show(
                    show_id,
                    "selector mutation failed verification; automatic retry forbidden",
                    recorded_at=self.now(),
                )
                raise ApplyRefused("ordering change could not be verified")

            self.store.record_applied(
                decision_id,
                evidence_fingerprint=fingerprint,
                ordering=selected,
                recorded_at=self.now(),
            )
            if self.audit is not None:
                self.audit.record(
                    "ordering",
                    "apply-verified",
                    {
                        "show_id": show_id,
                        "decision_id": decision_id,
                        "ordering": selected,
                        "trigger": "operator",
                    },
                )
            return decision_id

    def lock(self, show_id: int, reason: str = "operator") -> bool:
        changed = self.store.lock_show(show_id, reason, recorded_at=self.now())
        if changed and self.audit is not None:
            self.audit.record(
                "ordering", "operator-lock", {"show_id": show_id, "reason": reason}
            )
        return changed

    def status_snapshot(self) -> dict[str, Any]:
        with self._poll_lock:
            compatibility = self.shoko.compatibility()
            titles: dict[int, str] = {}
            try:
                for row in self.shoko.list_shows():
                    show_id = int(row["ID"])
                    title = row.get("Title")
                    if show_id > 0 and isinstance(title, str) and title.strip():
                        titles[show_id] = title.strip()
            except (ShokoClientError, KeyError, TypeError, ValueError):
                # Titles improve presentation but never weaken the ordering
                # decision path when Shoko's inventory is temporarily unreadable.
                titles = {}
            history = self.store.decisions()
            latest_by_show: dict[int, dict] = {}
            for row in history:
                latest_by_show[int(row["show_id"])] = row
            review = []
            for show in self.store.shows():
                latest = latest_by_show.get(int(show["show_id"]))
                if show["locked"] or (
                    latest is not None
                    and latest["outcome"] in {
                        "REVIEW_REQUIRED",
                        "INCOMPLETE_EVIDENCE",
                        "LATE_DISCOVERY_REVIEW",
                        "HISTORICAL_REVIEW",
                    }
                ):
                    review.append(
                        {
                            "show_id": int(show["show_id"]),
                            "title": titles.get(
                                int(show["show_id"]),
                                f"Shoko show {int(show['show_id'])}",
                            ),
                            "reason": show["lock_reason"]
                            or (latest["outcome"] if latest is not None else "review"),
                            "historical": bool(show["historical"]),
                            "first_seen_at": show["first_seen_at"],
                        }
                    )
            return {
                "mode": self.mode,
                "compatibility": {
                    "supported": compatibility.supported,
                    "server_version": compatibility.server_version,
                    "reason": compatibility.reason,
                },
                "baseline_count": len(self.store.baseline()),
                "history": history[-200:],
                "review": review,
            }

    @staticmethod
    def _coordinate(value: Coordinate | None) -> str | None:
        if value is None:
            return None
        return f"S{value.season:02d}E{value.episode:02d}"

    def comparison_snapshot(self, show_id: int) -> dict[str, Any]:
        """Build a read-only, exact-ID comparison for every current TMDB ordering."""
        if not isinstance(show_id, int) or show_id <= 0:
            raise ValueError("show ID must be a positive integer")
        with self._poll_lock:
            evidence = self._inputs(show_id)
            evidence = dict(evidence) | {"show_id": show_id}
            evaluation = evaluate_orderings(
                evidence["required_episodes"], evidence["orderings"], evidence["provider_structure"]
            )
            current = evidence.get("current_preference")
            protection_kind: str | None = None
            manual_protected = current not in {None, "default"}
            try:
                state = self.store.show(show_id)
            except KeyError:
                state = None
            if state is not None:
                if state["initial_preference"] not in {None, "default"}:
                    manual_protected = True
                if state["locked"]:
                    protection_kind = (
                        "late-discovery"
                        if state["lock_reason"] == LATE_DISCOVERY_LOCK
                        else "manual"
                    )
                elif state["historical"]:
                    protection_kind = "historical"
            if manual_protected:
                protection_kind = "manual"
            compatibility = self.shoko.compatibility()
            operator_apply_allowed = bool(
                self.mode == "enabled"
                and compatibility.supported
                and current == "default"
                and not manual_protected
                and protection_kind in {None, "historical", "late-discovery"}
                and evaluation.outcome == "SELECT_ALTERNATE"
                and evaluation.selected_ordering_id is not None
            )
            candidates: list[dict[str, Any]] = []
            for ordering in evidence["orderings"]:
                ordering_id = str(ordering["id"])
                preview = build_preview(evidence, evaluation, ordering_id)
                rows = []
                for identity, row in preview.rows_by_anidb.items():
                    rows.append(
                        {
                            "anidb_series_id": identity[0],
                            "kind": identity[1],
                            "anidb_episode_number": identity[2],
                            "shoko_episode_id": row.shoko_episode_id,
                            "file_backed": row.file_backed,
                            "current": self._coordinate(row.current),
                            "candidate": self._coordinate(row.candidate),
                        }
                    )
                candidates.append(
                    {
                        "ordering_id": ordering_id,
                        "is_current": ordering_id == current,
                        "is_selected": ordering_id == evaluation.selected_ordering_id,
                        "evidence_complete": ordering.get("evidence_complete") is True,
                        "tree": dict(preview.candidate_tree),
                        "failures": list(evaluation.failures.get(ordering_id, ())),
                        "rows": rows,
                    }
                )
            return {
                "show_id": show_id,
                "outcome": evaluation.outcome,
                "current_ordering_id": current,
                "selected_ordering_id": evaluation.selected_ordering_id,
                "manual_protected": manual_protected,
                "protection_kind": protection_kind,
                "operator_apply_allowed": operator_apply_allowed,
                "candidates": candidates,
            }

    def undo(self, decision_id: int) -> None:
        with self._poll_lock:
            decision = self.store.decision(decision_id)
            events = self.store.events(decision_id)
            active = self._latest_active_applied(events)
            if decision["outcome"] != "SELECT_ALTERNATE" or active is None:
                raise UndoRefused("decision has no active selector-owned apply")
            show_id = int(decision["show_id"])
            before = self._inputs(show_id)
            fingerprint = self._fingerprint(before)
            if (
                before.get("current_preference") != decision["selected_ordering"]
                or fingerprint != decision["evidence_fingerprint"]
            ):
                raise UndoRefused("current preference or evidence has drifted")
            compatibility = self.shoko.compatibility()
            if not compatibility.supported:
                raise UndoRefused("Shoko version or schema is not write-compatible")
            prior = decision["prior_ordering"] or "default"
            self.shoko.set_preferred(show_id, prior)
            after = self._inputs(show_id)
            if (
                after.get("current_preference") != prior
                or self._fingerprint(after) != fingerprint
            ):
                raise UndoRefused("undo could not be verified")
            self.store.record_undo(
                decision_id,
                evidence_fingerprint=fingerprint,
                ordering=prior,
                recorded_at=self.now(),
            )
            self.store.lock_show(
                show_id,
                "selector decision was undone by operator",
                recorded_at=self.now(),
            )
            if self.audit is not None:
                self.audit.record(
                    "ordering",
                    "undo-verified",
                    {"show_id": show_id, "decision_id": decision_id, "ordering": prior},
                )
