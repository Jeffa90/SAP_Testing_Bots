"""Producing every artefact a finished run should leave behind."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from saptest.config.models import RegionProfile
from saptest.core.exceptions import ConfigError
from saptest.core.outcomes import RunResult
from saptest.errors.capture import UnknownMessageCapture
from saptest.flows.base import Flow
from saptest.reporting.summary import summary_json, write_run_summary, write_summary_workbook
from saptest.reporting.testplan import write_testplan

log = logging.getLogger(__name__)


@dataclass
class ReportBundle:
    """Where everything a run produced ended up."""

    summary_html: Path | None = None
    summary_xlsx: Path | None = None
    summary_json: Path | None = None
    test_plans: list[Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def all_paths(self) -> list[Path]:
        return [
            p
            for p in (self.summary_html, self.summary_xlsx, self.summary_json, *self.test_plans)
            if p is not None
        ]


def write_reports(
    result: RunResult,
    profile: RegionProfile,
    flow: Flow,
    output_dir: Path | str,
    capture: UnknownMessageCapture | None = None,
    header: dict[str, str] | None = None,
) -> ReportBundle:
    """Write the run summary and one test plan per case.

    Report generation never raises: a run's results are far too expensive to lose to
    a formatting problem. Anything that goes wrong is collected in
    :attr:`ReportBundle.problems` and surfaced to the operator.
    """
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    bundle = ReportBundle()
    unknown = capture.summarise() if capture is not None else []

    try:
        bundle.summary_html = write_run_summary(result, directory / "summary.html", unknown)
    except Exception as exc:  # noqa: BLE001
        bundle.problems.append(f"HTML summary: {exc}")
        log.exception("Could not write the HTML summary")

    try:
        bundle.summary_xlsx = write_summary_workbook(result, directory / "summary.xlsx")
    except Exception as exc:  # noqa: BLE001
        bundle.problems.append(f"Summary workbook: {exc}")
        log.exception("Could not write the summary workbook")

    try:
        path = directory / "summary.json"
        path.write_text(summary_json(result), encoding="utf-8")
        bundle.summary_json = path
    except Exception as exc:  # noqa: BLE001
        bundle.problems.append(f"Summary JSON: {exc}")

    bundle.test_plans = _write_test_plans(result, profile, flow, directory, header, bundle.problems)
    return bundle


def _write_test_plans(
    result: RunResult,
    profile: RegionProfile,
    flow: Flow,
    directory: Path,
    header: dict[str, str] | None,
    problems: list[str],
) -> list[Path]:
    """One completed test-plan workbook per case that actually ran."""
    template = profile.template_path(flow.template) if flow.template else None
    if template is None:
        problems.append(
            f"Flow {flow.name!r} declares template {flow.template!r}, which region "
            f"{profile.region} does not define. No test plans were written."
        )
        return []
    if not template.exists():
        problems.append(f"Test plan template not found: {template}")
        return []

    plans_dir = directory / "test_plans"
    specs = flow.steps()
    written: list[Path] = []

    for case in result.cases:
        if not case.steps:
            continue
        try:
            written.append(
                write_testplan(case, specs, profile, template, plans_dir, header=header)
            )
        except ConfigError as exc:
            problems.append(f"Test plan for case {case.case_id}: {exc}")
            log.error("Test plan for case %s failed: %s", case.case_id, exc)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"Test plan for case {case.case_id}: {exc}")
            log.exception("Test plan for case %s failed", case.case_id)
    return written
