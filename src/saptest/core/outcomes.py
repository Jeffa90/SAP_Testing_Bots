"""Test outcomes and result records.

``BLOCKED_AUTH`` is a first-class outcome, separate from ``FAIL``. The scripts this
library replaces tracked this distinction by hand, by moving rows into sheets named
"No Authorisation" and "Authorisation Issues". Modelling it properly is what makes
role-coverage reporting possible: "this user could not perform this step" is a
different finding from "the system misbehaved", and only one of them is a defect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from saptest.core.status import StatusMessage


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED_AUTH = "BLOCKED_AUTH"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
    MANUAL = "MANUAL"


#: Severity ranking used to roll step outcomes up to a case, and cases up to a run.
#: ERROR outranks FAIL because it means the harness itself could not complete the
#: check, so the step's result is unknown rather than negative. FAIL outranks
#: BLOCKED_AUTH because a defect is a stronger finding than an untested step.
_SEVERITY: dict[Outcome, int] = {
    Outcome.PASS: 0,
    Outcome.SKIPPED: 1,
    Outcome.MANUAL: 2,
    Outcome.BLOCKED_AUTH: 3,
    Outcome.FAIL: 4,
    Outcome.ERROR: 5,
}

#: Outcomes that mean the step did not do what the test intended.
UNSUCCESSFUL = frozenset({Outcome.FAIL, Outcome.ERROR, Outcome.BLOCKED_AUTH})


def rollup(outcomes: list[Outcome]) -> Outcome:
    """Reduce many outcomes to the most severe. Empty input is ``SKIPPED``."""
    if not outcomes:
        return Outcome.SKIPPED
    return max(outcomes, key=lambda o: _SEVERITY[o])


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class EvidenceRef:
    """A captured screenshot and the context it was taken in."""

    path: str
    tag: str
    tcode: str = ""
    window_title: str = ""
    sha256: str = ""
    captured_at: datetime = field(default_factory=_now)


@dataclass(slots=True)
class StepResult:
    """Result of one step of a test case."""

    step_id: str
    name: str
    outcome: Outcome = Outcome.SKIPPED
    tcode: str = ""
    detail: str = ""
    messages: list[StatusMessage] = field(default_factory=list)
    evidence: list[EvidenceRef] = field(default_factory=list)
    remediations: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None
    exception: str = ""

    @property
    def duration_s(self) -> float:
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def actual_result(self) -> str:
        """Text for the test plan's "Actual Result" column.

        Prefers an explicit detail set by the flow, then the last SAP message, and
        falls back to a generic statement so the cell is never blank.
        """
        if self.detail:
            return self.detail
        if self.messages:
            return self.messages[-1].text or str(self.messages[-1])
        if self.exception:
            return f"Harness error: {self.exception}"
        return {
            Outcome.PASS: "Completed successfully",
            Outcome.SKIPPED: "Not executed",
            Outcome.MANUAL: "Requires manual execution",
            Outcome.BLOCKED_AUTH: "User is not authorised for this step",
        }.get(self.outcome, "No detail captured")


@dataclass(slots=True)
class CaseResult:
    """Result of one test case (one requisition group, plant, or data row)."""

    case_id: str
    flow: str
    sap_user: str = ""
    steps: list[StepResult] = field(default_factory=list)
    context: dict[str, str] = field(default_factory=dict)
    started_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None
    aborted: bool = False
    abort_reason: str = ""

    @property
    def outcome(self) -> Outcome:
        return rollup([s.outcome for s in self.steps])

    @property
    def duration_s(self) -> float:
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def evidence(self) -> list[EvidenceRef]:
        return [e for step in self.steps for e in step.evidence]


@dataclass(slots=True)
class RunResult:
    """Result of a whole run: many cases of one flow against one system."""

    run_id: str
    flow: str
    region: str = ""
    system: str = ""
    cases: list[CaseResult] = field(default_factory=list)
    started_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None
    aborted: bool = False
    abort_reason: str = ""

    @property
    def outcome(self) -> Outcome:
        return rollup([c.outcome for c in self.cases])

    @property
    def duration_s(self) -> float:
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    def tally(self) -> dict[Outcome, int]:
        """Case counts per outcome, including zero-count outcomes for stable reports."""
        counts = dict.fromkeys(Outcome, 0)
        for case in self.cases:
            counts[case.outcome] += 1
        return counts

    def cases_with(self, outcome: Outcome) -> list[CaseResult]:
        return [c for c in self.cases if c.outcome is outcome]
