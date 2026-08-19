"""The error catalogue: SAP messages mapped to remediation and control flow.

Entries match on message class + number (``ME`` ``083``) wherever possible, because
that identity is stable across logon languages and does not embed runtime data.
Regex over rendered text is supported as a fallback for the legacy_ui driver, which
cannot read the structured properties, but a text rule only ever wins when no
structured rule matched.

Matching precedence, most specific first:

1. class+number, scoped to a transaction
2. class+number, global
3. regex, scoped to a transaction
4. regex, global

Within a tier the first entry declared in the file wins, so overrides can be layered
by loading a region catalogue after the base one.
"""

from __future__ import annotations

import logging
import re
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from saptest.core.exceptions import ConfigError
from saptest.core.outcomes import Outcome
from saptest.core.status import MessageType, StatusMessage

log = logging.getLogger(__name__)


class ErrorPolicy(StrEnum):
    """What the runner does once remediation is exhausted."""

    #: Treat as harmless; the step continues and may still pass.
    CONTINUE = "continue"
    #: Record the step's outcome and move to the next step, subject to step policy.
    FAIL_STEP = "fail_step"
    #: Stop this case, run teardown, continue with the next case.
    ABORT_CASE = "abort_case"
    #: Stop the whole run.
    ABORT_RUN = "abort_run"


class Match(BaseModel):
    """Criteria identifying a SAP message. At least one of key or regex is required."""

    model_config = ConfigDict(extra="forbid")

    message_id: str | None = Field(default=None, description="Message class, e.g. 'ME'")
    number: str | None = Field(default=None, description="Message number, e.g. '083'")
    regex: str | None = Field(default=None, description="Fallback match on rendered text")
    type: MessageType | None = Field(default=None, description="Constrain to a message type")
    tcode: str | None = Field(default=None, description="Only match inside this transaction")

    @field_validator("message_id", "tcode")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return v.strip().upper() if v else v

    @field_validator("number")
    @classmethod
    def _pad(cls, v: str | None) -> str | None:
        # SAP message numbers are three digits; accept 83 and normalise to 083.
        return v.strip().zfill(3) if v else v

    @model_validator(mode="after")
    def _needs_a_locator(self) -> Match:
        has_key = bool(self.message_id and self.number)
        if not has_key and not self.regex:
            raise ValueError("match requires message_id + number, or regex")
        if bool(self.message_id) != bool(self.number):
            raise ValueError("message_id and number must be given together")
        return self

    @property
    def key(self) -> str:
        return f"{self.message_id}{self.number}" if self.message_id and self.number else ""

    @property
    def specificity(self) -> int:
        """Higher wins. Structured beats text; transaction-scoped beats global."""
        return (2 if self.key else 0) + (1 if self.tcode else 0)


class Action(BaseModel):
    """One remediation step: a named handler plus its arguments."""

    model_config = ConfigDict(extra="forbid")

    handler: str
    args: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _inline_args(cls, data: Any) -> Any:
        """Allow flat form ``{handler: set_field, field: x, value: y}``."""
        if isinstance(data, str):
            return {"handler": data, "args": {}}
        if isinstance(data, dict) and "handler" in data:
            known = {"handler", "args"}
            inline = {k: v for k, v in data.items() if k not in known}
            if inline:
                merged = dict(data.get("args") or {})
                merged.update(inline)
                return {"handler": data["handler"], "args": merged}
        return data


class CatalogEntry(BaseModel):
    """A catalogue rule: how to recognise a message, and what to do about it."""

    model_config = ConfigDict(extra="forbid")

    name: str
    match: Match
    #: Remediation attempted, in order, before the message is considered unresolved.
    actions: list[Action] = Field(default_factory=list)
    #: How many times remediation may be attempted for one occurrence.
    max_attempts: int = 1
    #: Outcome recorded when the message is never resolved.
    outcome: Outcome = Outcome.FAIL
    #: Control flow once remediation is exhausted.
    on_exhausted: ErrorPolicy = ErrorPolicy.FAIL_STEP
    #: Free text shown in reports to explain the finding to a human reader.
    notes: str = ""

    @field_validator("max_attempts")
    @classmethod
    def _sane_attempts(cls, v: int) -> int:
        if not 1 <= v <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        return v

    def matches(self, message: StatusMessage, tcode: str = "") -> bool:
        m = self.match
        if m.type is not None and message.type is not m.type:
            return False
        if m.tcode and m.tcode != (tcode or "").upper():
            return False
        if m.key:
            return bool(message.key) and message.key == m.key
        return bool(m.regex) and re.search(m.regex, message.text or "", re.IGNORECASE) is not None


class Catalog(BaseModel):
    """An ordered collection of catalogue entries."""

    model_config = ConfigDict(extra="forbid")

    entries: list[CatalogEntry] = Field(default_factory=list)

    def lookup(self, message: StatusMessage, tcode: str = "") -> CatalogEntry | None:
        """Most specific matching entry, or ``None`` when the message is unknown."""
        if message.is_empty:
            return None
        hits = [e for e in self.entries if e.matches(message, tcode)]
        if not hits:
            return None
        # max() keeps the first entry at the winning specificity, so declaration
        # order breaks ties and later files can be layered as overrides.
        return max(hits, key=lambda e: e.match.specificity)

    def merge(self, other: Catalog) -> Catalog:
        """Overlay another catalogue. Entries from ``other`` are considered first."""
        return Catalog(entries=[*other.entries, *self.entries])

    def names(self) -> set[str]:
        return {e.name for e in self.entries}


def load_catalog(*paths: str | Path) -> Catalog:
    """Load and merge catalogue YAML files, earliest path lowest priority."""
    catalog = Catalog()
    for path in paths:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"Error catalogue not found: {p}")
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or []
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML in error catalogue {p}: {exc}") from exc

        # Accept either a bare list of entries or {entries: [...]}.
        items = raw.get("entries", []) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise ConfigError(f"Error catalogue {p} must contain a list of entries")
        try:
            loaded = Catalog(entries=[CatalogEntry.model_validate(i) for i in items])
        except Exception as exc:
            raise ConfigError(f"Invalid entry in error catalogue {p}: {exc}") from exc

        overridden = catalog.names() & loaded.names()
        if overridden:
            # Layering a region overlay over the base catalogue is the intended way to
            # localise remediation, so this is a note rather than an error.
            log.info("%s overrides catalogue entries: %s", p.name, ", ".join(sorted(overridden)))
        catalog = catalog.merge(loaded)
    return catalog
