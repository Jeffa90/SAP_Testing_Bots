"""The run loop: cases, steps, policy and teardown.

The central correctness property here is that **teardown always runs**. The original
scripts did ``except Exception: break``, which left SAP sitting on whatever screen
the failure happened on, still logged in as the previous user -- so the next case
started from an unknown state and usually failed too. One real defect became a
cascade of phantom ones.

Here, a case that aborts still returns to Easy Access, logs off and releases the
session, so the next case begins clean regardless of how the previous one ended.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from saptest.config.models import RegionProfile
from saptest.core.evidence import EvidenceStore, new_run_id
from saptest.core.exceptions import (
    HarnessError,
    SapTestError,
    TestFailure,
)
from saptest.core.fields import FieldRegistry, load_fields
from saptest.core.outcomes import CaseResult, Outcome, RunResult, StepResult
from saptest.data.workbook import DataSet
from saptest.drivers.base import Driver
from saptest.drivers.registry import connect_with_fallback
from saptest.errors.capture import UnknownMessageCapture
from saptest.errors.catalog import ErrorPolicy, load_catalog
from saptest.errors.resolver import MessageResolver
from saptest.flows.base import Case, Flow, StepContext, StepSpec, strictest

log = logging.getLogger(__name__)

#: Called with (event_kind, payload). Used by the UI to stream live progress.
Listener = Callable[[str, dict], None]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class RunConfig:
    """Everything needed to execute a run."""

    profile: RegionProfile
    flow: Flow
    data: DataSet
    run_id: str = field(default_factory=new_run_id)
    output_root: Path = Path("runs")
    #: Override the profile's driver, e.g. "fake" for a rehearsal.
    driver: str | None = None
    #: Stop after this many cases. Useful for a first smoke run.
    limit: int | None = None
    #: Report header values written into the test plan.
    testers: dict[str, str] = field(default_factory=dict)
    #: Extra keyword arguments passed to the driver factory.
    driver_options: dict = field(default_factory=dict)
    #: Supply a ready-made driver instead of building one from the registry.
    #: Used by tests and by a rehearsal run against a pre-programmed fake.
    driver_factory: Callable[[], Driver] | None = None

    @property
    def driver_name(self) -> str:
        return self.driver or self.profile.driver

    @property
    def fallback_driver(self) -> str | None:
        # An explicit driver override disables fallback: the operator asked for one.
        return None if self.driver else self.profile.fallback_driver


class Runner:
    """Executes a flow over a dataset, producing a :class:`RunResult`."""

    def __init__(self, config: RunConfig, listener: Listener | None = None) -> None:
        self.config = config
        self.listener = listener
        self.profile = config.profile
        self.flow = config.flow

        self.run_dir = Path(config.output_root) / config.run_id
        self.evidence = EvidenceStore(config.output_root, config.run_id)
        self.capture = UnknownMessageCapture(self.evidence.root / "unknown_messages.jsonl")

        self.fields: FieldRegistry = load_fields(*self.profile.binding_paths())
        catalog = load_catalog(*self.profile.catalog_paths())
        self.resolver = MessageResolver(
            catalog=catalog,
            fields=self.fields,
            profile=self.profile.handler_context_profile(),
            capture=self.capture,
        )

    # --- events ---------------------------------------------------------------

    def _emit(self, kind: str, **payload: object) -> None:
        if self.listener is None:
            return
        try:
            self.listener(kind, dict(payload))
        except Exception as exc:  # noqa: BLE001 - a broken listener must not stop a run
            log.debug("Listener raised on %s: %s", kind, exc)

    # --- entry point ----------------------------------------------------------

    def run(self) -> RunResult:
        """Execute every case, returning the collected result."""
        self.flow.validate(self.config.data)
        cases = self.flow.build_cases(self.config.data, self.profile)
        if self.config.limit is not None:
            cases = cases[: self.config.limit]

        result = RunResult(
            run_id=self.config.run_id,
            flow=self.flow.name,
            region=self.profile.region,
            system=self.profile.connection.name,
        )
        self._emit(
            "run_started",
            run_id=result.run_id,
            flow=self.flow.name,
            region=self.profile.region,
            cases=len(cases),
            steps=len(self.flow.steps()),
        )
        log.info(
            "Run %s: flow %s, region %s, %d case(s)",
            result.run_id,
            self.flow.name,
            self.profile.region,
            len(cases),
        )

        for index, case in enumerate(cases, start=1):
            self._emit("case_started", case_id=case.id, index=index, total=len(cases))
            case_result = self._run_case(case)
            result.cases.append(case_result)
            self._emit(
                "case_finished",
                case_id=case.id,
                index=index,
                total=len(cases),
                outcome=case_result.outcome.value,
                duration_s=round(case_result.duration_s, 1),
            )

            if case_result.abort_reason == ErrorPolicy.ABORT_RUN.value:
                result.aborted = True
                result.abort_reason = (
                    f"Case {case.id} raised a stop-the-run condition: "
                    f"{case_result.abort_reason}"
                )
                log.error("Aborting run after case %s", case.id)
                break

        result.finished_at = _now()
        self._emit(
            "run_finished",
            run_id=result.run_id,
            outcome=result.outcome.value,
            tally={k.value: v for k, v in result.tally().items()},
            duration_s=round(result.duration_s, 1),
        )
        return result

    # --- one case -------------------------------------------------------------

    def _run_case(self, case: Case) -> CaseResult:
        specs = self.flow.steps()
        case_result = CaseResult(case_id=case.id, flow=self.flow.name, context=dict(case.context))

        if case.skip_reason:
            log.info("Skipping case %s: %s", case.id, case.skip_reason)
            case_result.steps = [
                StepResult(
                    step_id=s.id,
                    name=s.name,
                    tcode=s.tcode,
                    outcome=Outcome.SKIPPED,
                    detail=case.skip_reason,
                    finished_at=_now(),
                )
                for s in specs
            ]
            case_result.finished_at = _now()
            return case_result

        driver: Driver | None = None
        try:
            if self.config.driver_factory is not None:
                driver = self.config.driver_factory()
                driver.connect(self.profile.connection.name)
            else:
                driver = connect_with_fallback(
                    self.profile.connection.name,
                    self.config.driver_name,
                    self.config.fallback_driver,
                    **self.config.driver_options,
                )
            case_result.sap_user = driver.login(
                case.credentials, self.profile.connection.logon_mode
            )
            self._emit("case_logged_on", case_id=case.id, sap_user=case_result.sap_user)
            self.flow.setup_case(driver, case)

            self._run_steps(driver, case, case_result, specs)

        except SapTestError as exc:
            # Logon or connection failure: no step ran, so record the whole case.
            log.error("Case %s could not start: %s", case.id, exc)
            case_result.aborted = True
            case_result.abort_reason = str(exc)
            outcome = Outcome.BLOCKED_AUTH if isinstance(exc, TestFailure) else Outcome.ERROR
            case_result.steps = case_result.steps or [
                StepResult(
                    step_id=s.id,
                    name=s.name,
                    tcode=s.tcode,
                    outcome=outcome,
                    detail=str(exc),
                    finished_at=_now(),
                )
                for s in specs
            ]
        finally:
            self._teardown(driver, case)

        # Snapshot the context *after* the steps have run, so values the flow
        # captured along the way -- PR number, PO number, material document -- reach
        # the report. Taking it at construction time would always show it empty.
        case_result.context = dict(case.context)
        case_result.finished_at = _now()
        return case_result

    def _run_steps(
        self, driver: Driver, case: Case, case_result: CaseResult, specs: list[StepSpec]
    ) -> None:
        """Execute steps in order, applying policy after each failure."""
        stopped = False
        for spec in specs:
            if stopped:
                case_result.steps.append(
                    StepResult(
                        step_id=spec.id,
                        name=spec.name,
                        tcode=spec.tcode,
                        outcome=Outcome.SKIPPED,
                        detail="Not executed: an earlier step stopped this case",
                        finished_at=_now(),
                    )
                )
                continue

            step_result, policy = self._run_step(driver, case, spec, case_result.sap_user)
            case_result.steps.append(step_result)
            self._emit(
                "step_finished",
                case_id=case.id,
                step_id=spec.id,
                name=spec.name,
                outcome=step_result.outcome.value,
                detail=step_result.actual_result,
            )

            if policy in (ErrorPolicy.ABORT_CASE, ErrorPolicy.ABORT_RUN):
                case_result.aborted = True
                case_result.abort_reason = policy.value
                log.warning(
                    "Case %s stopped at step %s (%s): %s",
                    case.id,
                    spec.id,
                    policy.value,
                    step_result.actual_result,
                )
                stopped = True

    def _run_step(
        self, driver: Driver, case: Case, spec: StepSpec, sap_user: str
    ) -> tuple[StepResult, ErrorPolicy]:
        """Execute one step. Returns its result and the control-flow policy to apply."""
        result = StepResult(step_id=spec.id, name=spec.name, tcode=spec.tcode)
        self._emit("step_started", case_id=case.id, step_id=spec.id, name=spec.name)

        if spec.manual:
            # Fiori approval steps until the web driver lands: recorded honestly as
            # requiring a human, never silently passed.
            result.outcome = Outcome.MANUAL
            result.detail = "Requires manual execution (not automated in this flow)"
            result.finished_at = _now()
            return result, ErrorPolicy.CONTINUE

        ctx = StepContext(
            driver=driver,
            fields=self.fields,
            resolver=self.resolver,
            evidence=self.evidence,
            profile=self.profile,
            case=case,
            spec=spec,
            result=result,
            run_id=self.config.run_id,
            sap_user=sap_user,
        )

        policy = ErrorPolicy.CONTINUE
        try:
            if spec.tcode:
                driver.start_transaction(spec.tcode)
                ctx.check()
            spec.fn(self.flow, ctx)
            result.outcome = Outcome.PASS

        except TestFailure as exc:
            # A finding about SAP: the catalogue decided the outcome and policy.
            result.outcome = Outcome(exc.outcome)
            result.detail = result.detail or exc.detail
            policy = strictest(spec.on_error, ErrorPolicy(exc.policy))
            log.warning("Step %s (%s) -> %s: %s", spec.id, spec.name, result.outcome, exc)
            self._failure_shot(ctx)

        except HarnessError as exc:
            # The harness could not complete the check; the result is unknown, not bad.
            result.outcome = Outcome.ERROR
            result.exception = f"{type(exc).__name__}: {exc}"
            policy = spec.on_error
            log.error("Step %s (%s) could not run: %s", spec.id, spec.name, exc)
            self._failure_shot(ctx)

        except Exception as exc:  # noqa: BLE001 - an unexpected bug must not kill the run
            result.outcome = Outcome.ERROR
            result.exception = f"{type(exc).__name__}: {exc}"
            policy = spec.on_error
            log.exception("Unexpected error in step %s (%s)", spec.id, spec.name)
            self._failure_shot(ctx)

        finally:
            result.finished_at = _now()

        return result, policy

    def _failure_shot(self, ctx: StepContext) -> None:
        """Capture what the screen looked like when a step failed."""
        try:
            ctx.shot("FAILURE")
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not capture failure screenshot: %s", exc)

    # --- teardown -------------------------------------------------------------

    def _teardown(self, driver: Driver | None, case: Case) -> None:
        """Return SAP to a known state. Every part is independently guarded.

        A failure in any one of these must not prevent the others: leaving a session
        logged on would corrupt the next case, which is exactly the failure mode this
        replaces.
        """
        if driver is None:
            return
        for label, action in (
            ("flow teardown", lambda: self.flow.teardown_case(driver, case)),
            ("return to Easy Access", driver.go_home),
            ("log off", driver.logoff),
            ("close session", driver.close),
        ):
            try:
                action()
            except Exception as exc:  # noqa: BLE001
                log.warning("Teardown step %r failed for case %s: %s", label, case.id, exc)
