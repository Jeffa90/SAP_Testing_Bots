"""The flow model: steps, cases, and the API a flow author writes against.

A flow is the ordered list of transactions a test case performs, with the metadata
the test-plan report needs (step number, name, transaction, expected result) and a
per-step policy for what happens if it fails.

Everything else -- logon, navigation, screenshots, status-bar checking, remediation,
retry, teardown -- is the runner's job, so a flow reads as the test it describes::

    @step(id="1", name="Create Purchase Req.", tcode="ME51N",
          expected="PR saves with correct information",
          on_error=ErrorPolicy.ABORT_CASE)
    def create_pr(self, ctx: StepContext) -> None:
        ctx.shot("1A")
        for row in ctx.case.rows:
            ctx.enter_line(row)
        ctx.save()
        ctx.detail(f"PR {ctx.remember('pr_number')} created")
        ctx.shot("1B")
"""

from __future__ import annotations

import itertools
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from saptest.core.exceptions import AuthorizationBlocked, TestFailure, UnhandledSapMessage
from saptest.core.outcomes import Outcome, StepResult
from saptest.drivers.base import Credentials, VKey
from saptest.errors.catalog import ErrorPolicy

if TYPE_CHECKING:
    from saptest.config.models import RegionProfile
    from saptest.core.evidence import EvidenceStore
    from saptest.core.fields import FieldRegistry
    from saptest.data.workbook import DataSet, Row
    from saptest.drivers.base import Driver
    from saptest.errors.resolver import MessageResolver, Resolution

log = logging.getLogger(__name__)

_ORDER = itertools.count()

#: Relative severity of control-flow policies, used to combine a step's declared
#: policy with the one a catalogue entry recommends. The stricter of the two wins,
#: so an authorisation rule can stop a case even inside a lenient step.
_POLICY_RANK: dict[ErrorPolicy, int] = {
    ErrorPolicy.CONTINUE: 0,
    ErrorPolicy.FAIL_STEP: 1,
    ErrorPolicy.ABORT_CASE: 2,
    ErrorPolicy.ABORT_RUN: 3,
}


def strictest(*policies: ErrorPolicy) -> ErrorPolicy:
    return max(policies, key=lambda p: _POLICY_RANK[p])


@dataclass(slots=True)
class StepSpec:
    """Declarative metadata for one step, and the function that performs it."""

    id: str
    name: str
    fn: Callable[..., None]
    tcode: str = ""
    #: Test plan "Action Taken" column. Defaults to the transaction code.
    action: str = ""
    #: Test plan "How the Action is executed" column.
    how: str = ""
    #: Test plan "Expected Result" column.
    expected: str = ""
    on_error: ErrorPolicy = ErrorPolicy.ABORT_CASE
    #: Steps a driver cannot perform (Fiori approvals today) are recorded MANUAL.
    manual: bool = False
    order: int = 0

    @property
    def action_text(self) -> str:
        return self.action or self.tcode


def step(
    *,
    id: str,  # noqa: A002 - matches the test plan's "Step" column
    name: str,
    tcode: str = "",
    action: str = "",
    how: str = "",
    expected: str = "",
    on_error: ErrorPolicy = ErrorPolicy.ABORT_CASE,
    manual: bool = False,
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    """Mark a method as a test step and attach its report metadata.

    ``on_error`` is what delivers "exit out of some tests if required and continue
    on": ``CONTINUE`` logs and moves to the next step, ``ABORT_CASE`` stops this
    case but still runs teardown and starts the next one, ``ABORT_RUN`` stops
    everything.
    """

    def decorate(fn: Callable[..., None]) -> Callable[..., None]:
        fn.__step__ = StepSpec(  # type: ignore[attr-defined]
            id=str(id),
            name=name,
            fn=fn,
            tcode=tcode.upper(),
            action=action,
            how=how,
            expected=expected,
            on_error=on_error,
            manual=manual,
            order=next(_ORDER),
        )
        return fn

    return decorate


@dataclass
class Case:
    """One test case: the data, the user, and how to identify it in reports."""

    id: str
    rows: list[Row] = field(default_factory=list)
    credentials: Credentials | None = None
    #: Values available to remediation as ``@case.<key>`` and shown in reports.
    context: dict[str, str] = field(default_factory=dict)
    #: When set, the case is recorded SKIPPED without connecting to SAP.
    skip_reason: str = ""

    @property
    def first(self) -> Row:
        if not self.rows:
            raise ValueError(f"Case {self.id!r} has no data rows")
        return self.rows[0]


class StepContext:
    """What a step function is handed. The whole flow-author API lives here."""

    def __init__(
        self,
        driver: Driver,
        fields: FieldRegistry,
        resolver: MessageResolver,
        evidence: EvidenceStore,
        profile: RegionProfile,
        case: Case,
        spec: StepSpec,
        result: StepResult,
        run_id: str = "",
        sap_user: str = "",
    ) -> None:
        self.driver = driver
        self.fields = fields
        self.resolver = resolver
        self.evidence = evidence
        self.profile = profile
        self.case = case
        self.spec = spec
        self.result = result
        self.run_id = run_id
        self.sap_user = sap_user
        self.log = logging.getLogger(f"saptest.flow.{spec.id}")
        #: Values a step captures for later steps in the same case (PR number, PO number).
        self.memory: dict[str, str] = case.context

    # --- interaction -------------------------------------------------------------

    def ref(self, field_name: str, row: int | None = None):
        """Resolve a field, substituting a table row index into its id if it has one.

        SAP table-control ids carry a ``[column,row]`` suffix. A binding writes the
        row as ``{row}``::

            item.material:
              id: "wnd[0]/usr/tbl.../ctxtMEREQ3211-MATNR[3,{row}]"

        so one binding serves every line of a multi-line requisition.
        """
        located = self.fields.get(field_name)
        if row is not None and located.id and "{row}" in located.id:
            located = replace(located, id=located.id.format(row=row))
        return located

    def set(self, field_name: str, value: Any, row: int | None = None) -> None:
        """Set a bound field, skipping blanks so empty cells leave defaults alone."""
        text = "" if value is None else str(value).strip()
        if not text or text.lower() == "nan":
            return
        self.driver.set_field(self.ref(field_name, row), text)

    def read(self, field_name: str, row: int | None = None) -> str:
        return self.driver.read_field(self.ref(field_name, row))

    def exists(self, field_name: str, row: int | None = None) -> bool:
        return self.driver.exists(self.ref(field_name, row))

    def fill_row(self, data_row: Row, column_map: dict[str, str], row: int | None = None) -> int:
        """Enter one spreadsheet row into the screen via the column -> field map.

        Returns how many fields were actually populated. Unmapped columns and blank
        cells are skipped silently, so a workbook may carry extra bookkeeping columns
        (PR Number, First Password, Type) without the flow needing to know about them.
        """
        from saptest.data.workbook import normalise  # noqa: PLC0415

        filled = 0
        for column, value in data_row.items():
            field_name = column_map.get(normalise(column))
            if not field_name or field_name not in self.fields:
                continue
            text = str(value).strip()
            if not text or text.lower() == "nan":
                continue
            self.driver.set_field(self.ref(field_name, row), text)
            filled += 1
        return filled

    def press(self, key: VKey | int, check: bool = True) -> Resolution | None:
        """Send a key and, by default, check what SAP said about it.

        Checking is the default because every key press is a round trip that can
        raise a message. Making the flow author opt *in* to checking is how a "not
        authorised" on an innocuous-looking keystroke gets swallowed.
        """
        self.driver.press_key(key)
        return self.check() if check else None

    def enter(self) -> Resolution:
        """Submit the screen and check what SAP said. The most common step action."""
        return self.press(VKey.ENTER)

    def save(self) -> Resolution:
        """Save the document and check the result."""
        return self.press(VKey.SAVE)

    def okcode(self, code: str) -> Resolution:
        self.driver.set_field(self.fields.get("system.okcode"), code)
        return self.press(VKey.ENTER)

    # --- verification ------------------------------------------------------------

    def check(self, allow_unresolved: bool = False) -> Resolution:
        """Run the error catalogue over the status bar and act on what it finds.

        Raises when a message could not be resolved, carrying the outcome and policy
        the catalogue declared, so the runner records ``BLOCKED_AUTH`` rather than
        ``FAIL`` for an authorisation gap without inspecting the message itself.
        """
        resolution = self.resolver.resolve(
            self.driver,
            tcode=self.spec.tcode or self.driver.current_tcode(),
            case=dict(self.case.context),
            context={
                "case_id": self.case.id,
                "step_id": self.spec.id,
                "run_id": self.run_id,
                "sap_user": self.sap_user,
            },
        )

        if resolution.message and not resolution.message.is_empty:
            self.result.messages.append(resolution.message)
        if resolution.remediated:
            self.result.remediations.extend(resolution.actions_run)
            self.log.info(
                "Recovered from %s using %r", resolution.initial, resolution.entry.name
            )

        if resolution.resolved or allow_unresolved:
            return resolution

        detail = resolution.detail or str(resolution.message)
        if resolution.outcome is Outcome.BLOCKED_AUTH:
            raise AuthorizationBlocked(detail, policy=resolution.policy.value)
        raise UnhandledSapMessage(
            detail, outcome=resolution.outcome.value, policy=resolution.policy.value
        )

    def expect(self, condition: bool, detail: str) -> None:
        """Assert something the test requires, as a test failure rather than a crash."""
        if not condition:
            raise TestFailure(detail)

    # --- evidence and reporting --------------------------------------------------

    def shot(self, tag: str) -> None:
        """Capture a screenshot into the evidence pack for this step."""
        ref = self.evidence.capture(
            self.driver, self.case.id, self.spec.id, tag, sap_user=self.sap_user
        )
        if ref is not None:
            self.result.evidence.append(ref)

    def detail(self, text: str) -> None:
        """Set the text written into the test plan's "Actual Result" column."""
        self.result.detail = text

    def remember(self, key: str, value: str | None = None) -> str:
        """Store or retrieve a value shared across steps of the same case."""
        if value is not None:
            self.memory[key] = value
            self.log.info("%s = %s", key, value)
        return self.memory.get(key, "")

    # --- data --------------------------------------------------------------------

    def value(self, column: str, default: str = "") -> str:
        """A column from the case's first row."""
        return self.case.first.get_col(column, default)

    def default(self, name: str, fallback: str = "") -> str:
        """A value from the region profile's defaults."""
        return self.profile.defaults.get(name, fallback)


class Flow(ABC):
    """Base class for a test flow."""

    #: Dotted identifier used on the command line and in reports.
    name: str = ""
    #: Human title, used as the report heading.
    title: str = ""
    #: Logical template key resolved through the region profile's ``templates``.
    template: str = ""
    #: Default worksheet in the data workbook when the operator does not pick one.
    data_sheet: str | None = None
    #: Columns the flow cannot run without; validated before the run starts.
    required_columns: tuple[str, ...] = ()

    @abstractmethod
    def build_cases(self, data: DataSet, profile: RegionProfile) -> list[Case]:
        """Turn spreadsheet rows into test cases."""

    def steps(self) -> list[StepSpec]:
        """Declared steps, in source order, including any inherited from a base class."""
        specs: dict[str, StepSpec] = {}
        for klass in reversed(type(self).__mro__):
            for attribute in vars(klass).values():
                spec = getattr(attribute, "__step__", None)
                if spec is not None:
                    specs[spec.id] = spec
        return sorted(specs.values(), key=lambda s: s.order)

    def validate(self, data: DataSet) -> None:
        """Check the data workbook before a run starts, rather than mid-case."""
        if self.required_columns:
            data.require(*self.required_columns)

    def setup_case(self, ctx_driver: Driver, case: Case) -> None:
        """Hook run after logon, before the first step."""

    def teardown_case(self, ctx_driver: Driver, case: Case) -> None:
        """Hook run after the last step, before logoff. Runs even when a case aborts."""
