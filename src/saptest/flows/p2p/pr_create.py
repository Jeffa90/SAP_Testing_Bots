"""Purchase requisition: create (ME51N) and display (ME53N).

Mirrors steps 1-3 of the P2P test plan. Step 3 (release in Fiori) is declared but
marked manual until the web driver lands, so the report shows it honestly as
requiring a human rather than quietly omitting it.

This is the vertical slice: what the original ``Testing Create PR.py`` did, but with
the login, screenshots, error handling, retries and teardown lifted into the runner.
What is left is the test itself.
"""

from __future__ import annotations

import logging

from saptest.config.models import RegionProfile
from saptest.core.exceptions import DataError
from saptest.data.workbook import DataSet, load_column_map
from saptest.drivers.base import VKey
from saptest.errors.catalog import ErrorPolicy
from saptest.flows.base import Case, Flow, StepContext, step
from saptest.flows.documents import extract_document_number, line_numbers
from saptest.flows.registry import register_flow

log = logging.getLogger(__name__)

#: Column carrying the grouping key: all rows sharing a value are lines of one PR.
GROUP_COLUMN = "Requisition Group"


@register_flow
class PrCreateFlow(Flow):
    """Create a purchase requisition, then display it back."""

    name = "p2p.pr_create"
    title = "P2P - Create and Display Purchase Requisition"
    template = "standard_po"
    data_sheet = "PR"
    required_columns = (GROUP_COLUMN,)

    def __init__(self) -> None:
        self._columns: dict[str, str] = {}

    # --- cases ---------------------------------------------------------------

    def build_cases(self, data: DataSet, profile: RegionProfile) -> list[Case]:
        """One case per requisition group; its rows are the requisition's lines."""
        self._columns = load_column_map(*profile.binding_paths())
        if not self._columns:
            raise DataError(
                "No column mapping found. Add a 'columns:' section to a binding file "
                f"listed in profile {profile.region} so spreadsheet columns can be "
                "matched to SAP fields."
            )

        groups = data.group_by(GROUP_COLUMN)
        cases: list[Case] = []

        for group_id, rows in groups.items():
            first = rows[0]
            plant = first.get_col("Plant")
            case = Case(
                id=str(group_id),
                rows=rows,
                credentials=first.credentials(
                    "first",
                    client=profile.connection.client,
                    language=profile.connection.language,
                ),
                context={
                    "requisition_group": str(group_id),
                    "plant": plant,
                    "purchasing_group": first.get_col("PGr"),
                    "type": first.get_col("Type", "Standard PO"),
                    "cost_centre": first.get_col("Cost Centre"),
                    "lines": str(len(rows)),
                },
            )
            if profile.is_excluded("plants", plant):
                # Recorded as SKIPPED with a reason, not silently dropped the way the
                # original `if row["Plant"] not in excludePlants` did.
                case.skip_reason = (
                    f"Plant {plant} is excluded for region {profile.region} "
                    "(no second approver available)"
                )
            cases.append(case)

        log.info("Built %d case(s) from %d row(s)", len(cases), len(data))
        return cases

    # --- steps ---------------------------------------------------------------

    @step(
        id="1",
        name="Create Purchase Req.",
        tcode="ME51N",
        how="Create a new requisition with the lines from the test data",
        expected="PR saves with correct information and correct release appears",
        on_error=ErrorPolicy.ABORT_CASE,
    )
    def create_pr(self, ctx: StepContext) -> None:
        """Enter every line of the requisition, save, and capture the PR number."""
        ctx.shot("1A")

        for index, data_row in enumerate(ctx.case.rows):
            filled = ctx.fill_row(data_row, self._columns, row=index)
            ctx.log.info("Line %d: populated %d field(s)", index + 1, filled)
            # Submit each line so SAP validates it and the catalogue can remediate
            # per line, rather than surfacing every problem at once on save.
            ctx.enter()

        result = ctx.save()
        number = extract_document_number(result.message)
        ctx.expect(
            bool(number),
            "The requisition was saved but SAP returned no document number: "
            f"{result.message}",
        )

        ctx.remember("pr_number", number)
        for line_no, data_row in zip(line_numbers(len(ctx.case.rows)), ctx.case.rows, strict=False):
            ctx.remember(f"line_{data_row.index}", line_no)

        ctx.detail(f"PR {number} created successfully with {len(ctx.case.rows)} line(s)")
        ctx.shot("1B")

    @step(
        id="2",
        name="Display Purchase Req",
        tcode="ME53N",
        how="Display the requisition created in step 1",
        expected="PR opens the correct PR with correct information",
        # Display failing does not invalidate the create, and later cases are
        # independent, so a failure here is recorded and the run carries on.
        on_error=ErrorPolicy.CONTINUE,
    )
    def display_pr(self, ctx: StepContext) -> None:
        """Open the requisition by number and evidence that it displays."""
        number = ctx.remember("pr_number")
        ctx.expect(bool(number), "No PR number was captured in step 1")

        # Shift+F5 is "Other Purchase Requisition": opens the selection popup.
        ctx.press(VKey.OTHER_DOCUMENT)
        ctx.set("select.requisition_number", number)
        ctx.enter()

        shown = ctx.read("select.requisition_number") or number
        ctx.detail(f"PR {shown} displayed without any issues")
        ctx.shot("2A")

    @step(
        id="3",
        name="Approve a Requisition",
        action="Fiori",
        how="Release the requisition in the Fiori approval app",
        expected="Triggers, releases and is reflected in SAP",
        manual=True,
    )
    def approve_pr(self, ctx: StepContext) -> None:
        """Fiori release. Automated once the web driver lands (phase 7)."""
