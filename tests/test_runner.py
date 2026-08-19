"""End-to-end runner behaviour, driven entirely by the fake driver.

These are the tests that matter most: they exercise the whole path a real run takes
-- cases, steps, remediation, policy, teardown -- without SAP, Windows or a network.
"""

from __future__ import annotations

import pytest

from saptest.config import load_region
from saptest.core.outcomes import Outcome
from saptest.core.runner import RunConfig, Runner
from saptest.core.status import MessageType, StatusMessage
from saptest.data.workbook import load_dataset
from saptest.drivers.base import VKey
from saptest.drivers.fake import FakeDriver
from saptest.flows import get_flow

PR_NUMBER = "1100712093"


def sap(driver: FakeDriver, key: int) -> None:
    """Model the parts of SAP the flow depends on: saving returns a PR number."""
    if key == int(VKey.SAVE):
        driver.queue_message(
            StatusMessage(
                MessageType.SUCCESS,
                "ME",
                "045",
                f"Purchase requisition number {PR_NUMBER} created",
                (PR_NUMBER,),
            )
        )


@pytest.fixture
def run_config(repo_root, tmp_path):
    def build(driver_factory, limit=None):
        return RunConfig(
            profile=load_region("au", root=repo_root),
            flow=get_flow("p2p.pr_create"),
            data=load_dataset(repo_root / "tests" / "fixtures" / "pr_test_data.xlsx", "PR"),
            run_id="testrun",
            output_root=tmp_path / "runs",
            driver_factory=driver_factory,
            limit=limit,
        )

    return build


def make_driver(**kwargs) -> FakeDriver:
    return FakeDriver(on_press=sap, **kwargs)


# --- the happy path ---------------------------------------------------------------


def test_a_clean_run_passes_every_automatable_step(run_config):
    result = Runner(run_config(make_driver, limit=1)).run()

    assert len(result.cases) == 1
    case = result.cases[0]
    assert [s.outcome for s in case.steps] == [Outcome.PASS, Outcome.PASS, Outcome.MANUAL]
    assert case.sap_user == "BOTKCAREY"


def test_the_pr_number_is_read_from_the_confirmation_message(run_config):
    """No clipboard round trip: the number comes from the message SAP already sent."""
    result = Runner(run_config(make_driver, limit=1)).run()
    create = result.cases[0].steps[0]
    assert PR_NUMBER in create.actual_result
    assert result.cases[0].context["pr_number"] == PR_NUMBER


def test_multi_line_requisitions_enter_every_line(run_config):
    drivers = []

    def factory():
        driver = make_driver()
        drivers.append(driver)
        return driver

    Runner(run_config(factory, limit=1)).run()
    materials = [c[2] for c in drivers[0].calls_of("set_field") if c[1] == "item.material"]
    assert materials == ["500212331", "700107520"], "both lines of group 1 must be entered"


def test_evidence_is_captured_per_step(run_config, tmp_path):
    result = Runner(run_config(make_driver, limit=1)).run()
    tags = [e.tag for step in result.cases[0].steps for e in step.evidence]
    assert tags == ["1A", "1B", "2A"]
    assert all(__import__("pathlib").Path(e.path).exists() for e in result.cases[0].evidence)


def test_fiori_step_is_reported_as_manual_not_silently_skipped(run_config):
    result = Runner(run_config(make_driver, limit=1)).run()
    approve = result.cases[0].steps[2]
    assert approve.outcome is Outcome.MANUAL
    assert "manual" in approve.actual_result.lower()


# --- remediation ------------------------------------------------------------------


def test_a_recoverable_error_is_remediated_and_the_step_still_passes(run_config):
    def factory():
        driver = make_driver()
        driver.queue_message(StatusMessage(MessageType.ERROR, text="Enter Tax code"))
        return driver

    result = Runner(run_config(factory, limit=1)).run()
    create = result.cases[0].steps[0]
    assert create.outcome is Outcome.PASS
    assert "set_field" in create.remediations


# --- authorisation ----------------------------------------------------------------


def test_authorisation_failure_blocks_the_case_and_skips_later_steps(run_config):
    def factory():
        driver = make_driver()
        driver.queue_message(
            StatusMessage(MessageType.ERROR, text="You are not authorized to use transaction ME51N")
        )
        return driver

    result = Runner(run_config(factory, limit=1)).run()
    case = result.cases[0]

    assert case.steps[0].outcome is Outcome.BLOCKED_AUTH
    assert case.steps[1].outcome is Outcome.SKIPPED
    assert case.steps[2].outcome is Outcome.SKIPPED
    assert case.outcome is Outcome.BLOCKED_AUTH
    assert case.aborted


def test_a_blocked_case_does_not_stop_the_run(run_config):
    """The original `except: break` ended the entire run on the first problem."""

    def factory():
        driver = make_driver()
        driver.queue_message(StatusMessage(MessageType.ERROR, text="You are not authorised"))
        return driver

    result = Runner(run_config(factory)).run()
    assert len(result.cases) == 4, "every case must still be attempted"
    assert not result.aborted


# --- per-step policy --------------------------------------------------------------


def test_a_continue_step_failing_does_not_stop_the_case(run_config):
    """Step 2 is declared on_error=CONTINUE, so step 3 must still be reached."""

    def factory():
        driver = make_driver()

        def on_press(d: FakeDriver, key: int) -> None:
            sap(d, key)
            if key == int(VKey.OTHER_DOCUMENT):
                d.queue_message(StatusMessage(MessageType.ERROR, "ZZ", "500", "Display broke"))

        driver.on_press = on_press
        return driver

    result = Runner(run_config(factory, limit=1)).run()
    case = result.cases[0]

    assert case.steps[0].outcome is Outcome.PASS
    assert case.steps[1].outcome is Outcome.FAIL
    assert case.steps[2].outcome is Outcome.MANUAL, "step 3 must still be reached"
    assert not case.aborted


# --- teardown ---------------------------------------------------------------------


def test_teardown_runs_even_when_a_case_aborts(run_config):
    drivers = []

    def factory():
        driver = make_driver()
        driver.queue_message(StatusMessage(MessageType.ERROR, text="You are not authorised"))
        drivers.append(driver)
        return driver

    Runner(run_config(factory, limit=1)).run()

    kinds = [c[0] for c in drivers[0].calls]
    assert "go_home" in kinds
    assert "logoff" in kinds
    assert drivers[0].closed


def test_teardown_failure_does_not_lose_the_result(run_config):
    def factory():
        driver = make_driver()

        def explode() -> None:
            raise RuntimeError("session already gone")

        driver.go_home = explode
        return driver

    result = Runner(run_config(factory, limit=1)).run()
    assert result.cases[0].steps[0].outcome is Outcome.PASS


def test_each_case_gets_its_own_session(run_config):
    drivers = []

    def factory():
        driver = make_driver()
        drivers.append(driver)
        return driver

    result = Runner(run_config(factory)).run()
    logged_on = [c.sap_user for c in result.cases if c.sap_user]
    assert logged_on == ["BOTKCAREY", "BOTJKELLY", "BOTHNGUYEN2"]
    assert all(d.closed for d in drivers)


# --- exclusions and SSO -----------------------------------------------------------


def test_an_excluded_plant_is_skipped_with_a_reason_and_never_logs_on(run_config):
    drivers = []

    def factory():
        driver = make_driver()
        drivers.append(driver)
        return driver

    result = Runner(run_config(factory)).run()
    excluded = next(c for c in result.cases if c.case_id == "3")

    assert excluded.outcome is Outcome.SKIPPED
    assert "1240" in excluded.steps[0].detail
    assert len(drivers) == 3, "the excluded case must not open a session"


def test_a_row_without_a_password_logs_on_via_sso(run_config):
    def factory():
        return make_driver(sso_connection=True)

    result = Runner(run_config(factory)).run()
    sso_case = next(c for c in result.cases if c.case_id == "4")
    assert sso_case.sap_user == "WINDOWSSSO"


# --- failure evidence and unknown capture ------------------------------------------


def test_a_failing_step_captures_a_screenshot_of_the_failure(run_config):
    def factory():
        driver = make_driver()
        driver.queue_message(StatusMessage(MessageType.ERROR, "ZZ", "911", "Unknown problem"))
        return driver

    result = Runner(run_config(factory, limit=1)).run()
    create = result.cases[0].steps[0]
    assert create.outcome is Outcome.FAIL
    assert any(e.tag == "FAILURE" for e in create.evidence)


def test_unknown_messages_are_captured_for_catalogue_review(run_config, tmp_path):
    def factory():
        driver = make_driver()
        driver.queue_message(StatusMessage(MessageType.ERROR, "ZZ", "911", "Never seen this"))
        return driver

    runner = Runner(run_config(factory, limit=1))
    runner.run()

    captured = runner.capture.read()
    assert captured and captured[0]["key"] == "ZZ911"
    assert captured[0]["case_id"] == "1"


# --- progress events --------------------------------------------------------------


def test_the_runner_emits_events_for_the_live_ui(run_config):
    events = []
    Runner(run_config(make_driver, limit=1), listener=lambda k, p: events.append((k, p))).run()

    kinds = [k for k, _ in events]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    assert kinds.count("step_finished") == 3
    # The case rolls up to MANUAL: its Fiori step still needs a human.
    assert dict(events)["run_finished"]["tally"]["MANUAL"] == 1


def test_a_broken_listener_cannot_stop_a_run(run_config):
    def bad_listener(kind, payload):
        raise RuntimeError("UI went away")

    result = Runner(run_config(make_driver, limit=1), listener=bad_listener).run()
    assert result.cases[0].steps[0].outcome is Outcome.PASS
