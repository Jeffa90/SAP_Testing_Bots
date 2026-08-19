"""Profile models.

A region profile is the whole answer to "what is different about this landscape":
connection details, date and decimal formats, default values used by remediation,
plant exclusions, which drivers to try, where the bindings and templates live, and
how this region's test-plan workbook is laid out.

Adding a region is a YAML file. It should never require a code change.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from saptest.drivers.base import LogonMode


class ConnectionProfile(BaseModel):
    """How to reach one SAP system."""

    model_config = ConfigDict(extra="forbid")

    #: SAP Logon connection entry name, as it appears in SAP Logon.
    name: str
    #: Client, e.g. "800". Left blank to accept the connection's default.
    client: str = ""
    language: str = "EN"
    #: Expected logon mode. ``auto`` decides from whether a password is supplied.
    logon_mode: LogonMode = LogonMode.AUTO
    #: Human label used in reports, e.g. "S/4HANA UAT".
    description: str = ""

    @field_validator("language")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class Formats(BaseModel):
    """Locale-dependent formatting.

    The original scripts hardcoded ``31.10.2025`` and assumed a comma decimal
    separator. Both are user- and region-dependent in SAP and must be configuration.
    """

    model_config = ConfigDict(extra="forbid")

    #: strftime pattern matching the SAP user's date format.
    date: str = "%d.%m.%Y"
    #: Decimal separator the SAP user profile is set to.
    decimal: str = ","
    #: Thousands separator, or empty when none is used.
    thousands: str = "."


class StepColumns(BaseModel):
    """Column letters of the step table in the test-plan workbook."""

    model_config = ConfigDict(extra="forbid")

    step: str = "A"
    name: str = "B"
    action: str = "C"
    how: str = "D"
    expected: str = "E"
    actual: str = "F"
    result: str = "G"


class TestPlanMapping(BaseModel):
    """Where things live in this region's test-plan template.

    Defaults match the observed ``AU_PTP_Inventory PO.xlsx`` layout: a "Test Plan"
    sheet with header fields in column G, a step table whose header is row 10 and
    whose steps start at row 11, and numbered sheets "1".."7" holding screenshots.
    """

    model_config = ConfigDict(extra="forbid")

    sheet: str = "Test Plan"
    #: Header cell references, keyed by logical name.
    header: dict[str, str] = Field(
        default_factory=lambda: {
            "test_date": "G1",
            "change_control": "G2",
            "service_desk": "G3",
            "problem_id": "G4",
            "project_id": "G5",
            "business_tester": "G6",
            "functional_tester": "G7",
            "environment": "G8",
        }
    )
    #: First row of the step table (the row below the header row).
    first_step_row: int = 11
    columns: StepColumns = Field(default_factory=StepColumns)
    #: Text written into the Pass/Fail column, per outcome name.
    result_text: dict[str, str] = Field(
        default_factory=lambda: {
            "PASS": "Pass",
            "FAIL": "Fail",
            "BLOCKED_AUTH": "Blocked - No Authorisation",
            "ERROR": "Fail",
            "SKIPPED": "Not Executed",
            "MANUAL": "Manual",
        }
    )
    #: Screenshots go on a sheet named after the step id ("1".."7").
    evidence_sheet_per_step: bool = True
    #: Max width in pixels for embedded screenshots.
    image_max_width_px: int = 1000


class RegionProfile(BaseModel):
    """Everything that varies between regions and landscapes."""

    model_config = ConfigDict(extra="forbid")

    region: str
    description: str = ""
    connection: ConnectionProfile
    formats: Formats = Field(default_factory=Formats)

    #: Values remediation handlers pull from, e.g. ``{"tax_code": "P1"}``.
    defaults: dict[str, str] = Field(default_factory=dict)
    #: Data rows to skip, e.g. plants where a second approver does not exist.
    exclusions: dict[str, list[str]] = Field(default_factory=dict)

    driver: str = "guiscript"
    #: Used when the primary driver reports scripting is unavailable.
    fallback_driver: str | None = "legacy_ui"

    #: Field binding YAML files, later files overriding earlier ones.
    bindings: list[str] = Field(default_factory=list)
    #: Error catalogue YAML files, later files overriding earlier ones.
    catalogs: list[str] = Field(default_factory=list)
    #: Directory of image assets for the legacy_ui driver.
    images: str | None = None
    #: Test-plan templates by logical name, e.g. ``{"standard_po": "templates/..."}``.
    templates: dict[str, str] = Field(default_factory=dict)
    testplan: TestPlanMapping = Field(default_factory=TestPlanMapping)

    #: Filled by the loader: directory paths in this profile resolve against it.
    root: Path = Field(default=Path("."), exclude=True)

    @field_validator("region")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    def path(self, value: str) -> Path:
        """Resolve a profile-relative path against the project root."""
        p = Path(value)
        return p if p.is_absolute() else (self.root / p)

    def binding_paths(self) -> list[Path]:
        return [self.path(b) for b in self.bindings]

    def catalog_paths(self) -> list[Path]:
        return [self.path(c) for c in self.catalogs]

    def template_path(self, name: str) -> Path | None:
        value = self.templates.get(name)
        return self.path(value) if value else None

    def is_excluded(self, kind: str, value: object) -> bool:
        """Whether a data value is in an exclusion list.

        Compared as strings so that ``1240`` in YAML matches ``"1240"`` read from a
        spreadsheet, which is where the original ``excludePlants`` list went wrong.
        """
        values = self.exclusions.get(kind)
        if not values:
            return False
        return str(value).strip() in {str(v).strip() for v in values}

    def handler_context_profile(self) -> dict[str, object]:
        """The dict remediation handlers resolve ``@profile.*`` references against."""
        return {
            "defaults": dict(self.defaults),
            "formats": self.formats.model_dump(),
            "region": self.region,
            "client": self.connection.client,
            "language": self.connection.language,
        }
