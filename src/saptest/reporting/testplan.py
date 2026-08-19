"""Writing results and screenshots back into the team's test-plan workbook.

Generalises ``build_testplan_workbook`` from the original scripts. Three changes:

* **Steps are matched by number, not by filename suffix.** The original derived a
  step's tab from the first character of a screenshot filename
  (``AU_PTP_Standard PO_24_4C`` -> tab "4"), which broke as soon as a plan had more
  than nine steps and depended on every screenshot being named exactly right.
* **Results are written, not just images.** The Actual Result and Pass/Fail columns
  are filled from the run, so the workbook is a finished record rather than a
  screenshot dump someone still has to annotate.
* **The layout is configuration.** Cell references come from the region profile, so
  a region with a different template changes YAML rather than code.
"""

from __future__ import annotations

import logging
import math
from datetime import date
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from PIL import Image as PILImage

from saptest.config.models import RegionProfile, TestPlanMapping
from saptest.core.evidence import safe_name
from saptest.core.exceptions import ConfigError
from saptest.core.outcomes import CaseResult, EvidenceRef, Outcome
from saptest.flows.base import StepSpec

log = logging.getLogger(__name__)

#: Approximate pixel height of a default-height Excel row, used to space stacked images.
_PX_PER_ROW = 18
_PAD_ROWS = 6


def write_testplan(
    case: CaseResult,
    specs: list[StepSpec],
    profile: RegionProfile,
    template_path: Path | str,
    output_dir: Path | str,
    header: dict[str, str] | None = None,
    filename: str | None = None,
) -> Path:
    """Produce one completed test-plan workbook for one case.

    Returns the path written. Never overwrites: a suffix is added if the target
    exists, so a re-run cannot destroy the previous run's signed evidence.
    """
    template = Path(template_path)
    if not template.exists():
        raise ConfigError(
            f"Test plan template not found: {template}. Check the 'templates' section "
            f"of region profile {profile.region}."
        )

    mapping = profile.testplan
    workbook = load_workbook(template)

    if mapping.sheet not in workbook.sheetnames:
        raise ConfigError(
            f"Template {template.name} has no {mapping.sheet!r} sheet. "
            f"Sheets present: {', '.join(workbook.sheetnames)}"
        )
    plan = workbook[mapping.sheet]

    _write_header(plan, mapping, case, profile, header or {})
    _write_steps(plan, mapping, case, specs)
    _place_evidence(workbook, mapping, case)

    target = _output_path(Path(output_dir), filename or _default_filename(case, profile, template))
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(target)
    log.info("Test plan written: %s", target)
    return target


# --- header ------------------------------------------------------------------------


def _write_header(
    plan: Worksheet,
    mapping: TestPlanMapping,
    case: CaseResult,
    profile: RegionProfile,
    header: dict[str, str],
) -> None:
    """Fill the header block, defaulting the fields the run already knows."""
    values = {
        "test_date": date.today().strftime("%d/%m/%Y"),
        "environment": profile.connection.description or profile.connection.name,
        "business_tester": case.sap_user,
        **{k: v for k, v in header.items() if v},
    }
    for name, cell in mapping.header.items():
        if name in values and values[name]:
            try:
                plan[cell] = values[name]
            except (ValueError, KeyError) as exc:
                log.warning("Could not write header %r to %s: %s", name, cell, exc)


# --- step table --------------------------------------------------------------------


def _write_steps(
    plan: Worksheet, mapping: TestPlanMapping, case: CaseResult, specs: list[StepSpec]
) -> None:
    """Write Actual Result and Pass/Fail against each step.

    Rows are located by the step number in the Step column rather than by position,
    so a template with extra spacing rows, or steps in a different order, still lands
    the results in the right place.
    """
    columns = mapping.columns
    rows_by_step = _index_step_rows(plan, mapping)
    results = {s.step_id: s for s in case.steps}

    for offset, spec in enumerate(specs):
        row = rows_by_step.get(str(spec.id).strip())
        if row is None:
            row = mapping.first_step_row + offset
            log.warning(
                "Step %s not found in the template's Step column; writing to row %d",
                spec.id,
                row,
            )

        result = results.get(spec.id)
        if result is None:
            continue

        plan[f"{columns.actual}{row}"] = result.actual_result
        plan[f"{columns.result}{row}"] = mapping.result_text.get(
            result.outcome.value, result.outcome.value
        )
        # Fill the descriptive columns only where the template left them blank, so a
        # hand-written test plan is never overwritten by generated text.
        for column, text in (
            (columns.name, spec.name),
            (columns.action, spec.action_text),
            (columns.how, spec.how),
            (columns.expected, spec.expected),
        ):
            cell = f"{column}{row}"
            if text and plan[cell].value in (None, ""):
                plan[cell] = text


def _index_step_rows(plan: Worksheet, mapping: TestPlanMapping) -> dict[str, int]:
    """Map the value of the Step column to its row number."""
    column = mapping.columns.step
    found: dict[str, int] = {}
    for row in range(mapping.first_step_row, plan.max_row + 1):
        value = plan[f"{column}{row}"].value
        if value is None:
            continue
        key = str(value).strip()
        if isinstance(value, float) and value.is_integer():
            key = str(int(value))
        if key and key not in found:
            found[key] = row
    return found


# --- screenshots -------------------------------------------------------------------


def _place_evidence(workbook, mapping: TestPlanMapping, case: CaseResult) -> None:
    """Stack each step's screenshots on the sheet named after that step."""
    if not mapping.evidence_sheet_per_step:
        return

    by_step: dict[str, list[EvidenceRef]] = {}
    for step in case.steps:
        if step.evidence:
            by_step.setdefault(str(step.step_id), []).extend(step.evidence)

    for step_id, refs in by_step.items():
        sheet = (
            workbook[step_id]
            if step_id in workbook.sheetnames
            else workbook.create_sheet(step_id)
        )
        row = 2
        for ref in refs:
            row = _add_image(sheet, ref, row, mapping.image_max_width_px)


def _add_image(sheet: Worksheet, ref: EvidenceRef, row: int, max_width_px: int) -> int:
    """Embed one screenshot, returning the row to place the next one at."""
    path = Path(ref.path)
    anchor = f"A{row}"

    if not path.exists():
        sheet[anchor] = f"[Missing screenshot: {ref.tag}]"
        log.warning("Screenshot missing when writing report: %s", path)
        return row + 8

    try:
        with PILImage.open(path) as image:
            width, height = image.size

        scale = 1.0 if width <= max_width_px else max_width_px / float(width)
        embedded = XLImage(str(path))
        embedded.width = int(round(width * scale))
        embedded.height = int(round(height * scale))

        sheet[anchor] = f"{ref.tag} - {ref.tcode or ''} {ref.window_title or ''}".strip()
        sheet.add_image(embedded, f"A{row + 1}")
        return row + math.ceil(embedded.height / _PX_PER_ROW) + _PAD_ROWS
    except Exception as exc:  # noqa: BLE001 - one bad image must not lose the report
        sheet[anchor] = f"[Error inserting {ref.tag}: {exc}]"
        log.error("Could not embed screenshot %s: %s", path, exc)
        return row + 8


# --- naming ------------------------------------------------------------------------


def _default_filename(case: CaseResult, profile: RegionProfile, template: Path) -> str:
    """Name the output after the template plus what identifies the case."""
    parts = [template.stem]
    for key in ("plant", "purchasing_group", "requisition_group"):
        value = case.context.get(key)
        if value:
            parts.append(safe_name(value))
    if len(parts) == 1:
        parts.append(safe_name(case.case_id))
    return "_".join(parts) + ".xlsx"


def _output_path(directory: Path, filename: str) -> Path:
    """Never clobber: add ' (1)', ' (2)'... if the name is taken."""
    target = directory / filename
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    index = 1
    while True:
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def outcome_columns(mapping: TestPlanMapping) -> tuple[str, str]:
    """The (Actual Result, Pass/Fail) column letters, for tests and the UI."""
    return mapping.columns.actual, mapping.columns.result


def column_index(letter: str) -> int:
    from openpyxl.utils import column_index_from_string  # noqa: PLC0415

    return column_index_from_string(letter)


__all__ = ["write_testplan", "outcome_columns", "column_index", "get_column_letter", "Outcome"]
