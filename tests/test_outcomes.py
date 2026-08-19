import pytest

from saptest.core.outcomes import CaseResult, Outcome, RunResult, StepResult, rollup


def test_rollup_takes_the_most_severe():
    assert rollup([Outcome.PASS, Outcome.BLOCKED_AUTH, Outcome.PASS]) is Outcome.BLOCKED_AUTH
    assert rollup([Outcome.FAIL, Outcome.BLOCKED_AUTH]) is Outcome.FAIL
    assert rollup([Outcome.ERROR, Outcome.FAIL]) is Outcome.ERROR


def test_rollup_of_nothing_is_skipped():
    assert rollup([]) is Outcome.SKIPPED


def test_all_passing_is_a_pass():
    assert rollup([Outcome.PASS, Outcome.PASS]) is Outcome.PASS


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (Outcome.BLOCKED_AUTH, "User is not authorised for this step"),
        (Outcome.SKIPPED, "Not executed"),
        (Outcome.MANUAL, "Requires manual execution"),
    ],
)
def test_actual_result_never_blank(outcome, expected):
    assert StepResult("1", "step", outcome=outcome).actual_result == expected


def test_actual_result_prefers_explicit_detail():
    step = StepResult("1", "step", outcome=Outcome.PASS, detail="PR 1100712093 created")
    assert step.actual_result == "PR 1100712093 created"


def test_run_tally_covers_every_outcome():
    run = RunResult(
        "r1",
        "flow",
        cases=[
            CaseResult("a", "flow", steps=[StepResult("1", "s", outcome=Outcome.PASS)]),
            CaseResult("b", "flow", steps=[StepResult("1", "s", outcome=Outcome.FAIL)]),
        ],
    )
    tally = run.tally()
    assert tally[Outcome.PASS] == 1
    assert tally[Outcome.FAIL] == 1
    assert set(tally) == set(Outcome)
    assert run.outcome is Outcome.FAIL
    assert [c.case_id for c in run.cases_with(Outcome.FAIL)] == ["b"]
