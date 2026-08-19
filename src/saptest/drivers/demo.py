"""A simulated SAP system, for rehearsing a run without a landscape.

Not a test double -- :mod:`saptest.drivers.fake` is that. This one models plausible
S/4HANA behaviour so the whole pipeline (flows, remediation, evidence, reports) can
be exercised end to end on any machine: to evaluate the tool, to demonstrate a
report, or to check a new flow's shape before booking time on a test client.

Document numbers increment realistically, and a configurable share of cases hit a
recoverable error or an authorisation block, so the reports show every outcome the
real thing produces.

    saptest run --region au --flow p2p.pr_create --data <workbook> --driver demo
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from saptest.core.status import MessageType, StatusMessage
from saptest.drivers.base import Credentials, LogonMode, VKey
from saptest.drivers.fake import FakeDriver

#: Recoverable messages the shipped catalogue knows how to remediate.
_RECOVERABLE = [
    "Enter Tax code",
    "Net price must be greater than 0",
    "Item 10 Delivery Date 08.09.2025 is in the past",
]

_AUTH = "You are not authorized to use transaction {tcode}"

#: Each case gets a fresh driver, so behaviour is chosen by position rather than by
#: a seeded RNG -- a per-instance seed would make every case behave identically.
_CASE_COUNTER = itertools.count()

#: Deterministic rotation, so even a four-row demo exercises every outcome the real
#: runner can produce, and the resulting report is stable across runs.
BEHAVIOURS = ("clean", "recoverable", "blocked", "unknown")

#: Document numbers are also module-level: each case gets its own driver, so a
#: per-instance counter would hand every case the same PR number.
_DOCUMENT_COUNTER = itertools.count(1100712093)


def reset_demo_sequence(first_document: int = 1100712093) -> None:
    """Restart the behaviour rotation and document numbering, for determinism."""
    global _CASE_COUNTER, _DOCUMENT_COUNTER
    _CASE_COUNTER = itertools.count()
    _DOCUMENT_COUNTER = itertools.count(first_document)


@dataclass
class DemoDriver(FakeDriver):
    """A fake that behaves enough like SAP to produce a realistic report."""

    name: str = "demo"

    #: Force every case to behave the same way, instead of rotating. One of
    #: :data:`BEHAVIOURS`, or None to rotate.
    behaviour: str | None = None

    _behaviour: str = field(default="clean", repr=False)
    _document: str = field(default="", repr=False)

    # --- lifecycle ---------------------------------------------------------------

    def login(self, creds: Credentials | None, mode: LogonMode = LogonMode.AUTO) -> str:
        user = super().login(creds, mode)
        # Decide this case's fate once, at logon, so it behaves consistently throughout.
        self._behaviour = self.behaviour or BEHAVIOURS[next(_CASE_COUNTER) % len(BEHAVIOURS)]
        return user

    # --- navigation --------------------------------------------------------------

    def start_transaction(self, tcode: str) -> None:
        super().start_transaction(tcode)
        if self._behaviour == "blocked" and tcode.upper().startswith("ME5"):
            self._status = StatusMessage(MessageType.ERROR, text=_AUTH.format(tcode=tcode.upper()))

    # --- interaction -------------------------------------------------------------

    def press_key(self, key: VKey | int) -> None:
        code = int(key)

        if code == int(VKey.SAVE):
            number = str(next(_DOCUMENT_COUNTER))
            self._document = number
            self.fields["_document"] = number
            self.queue_message(
                StatusMessage(
                    MessageType.SUCCESS,
                    "ME",
                    "045",
                    f"Purchase requisition number {number} created",
                    (number,),
                )
            )

        elif code == int(VKey.ENTER) and self._behaviour == "recoverable":
            # Fire once per case, so remediation visibly clears it and the step passes.
            self._behaviour = "clean"
            self.queue_message(
                StatusMessage(
                    MessageType.ERROR,
                    text=_RECOVERABLE[next(_CASE_COUNTER) % len(_RECOVERABLE)],
                )
            )

        elif code == int(VKey.OTHER_DOCUMENT) and self._behaviour == "unknown":
            self._behaviour = "clean"
            self.queue_message(
                StatusMessage(
                    MessageType.ERROR, "ZV", "217",
                    "Document is locked by user BOTKCAREY (demo: not in the catalogue)",
                )
            )

        super().press_key(code)

    def read_field(self, ref) -> str:  # noqa: ANN001
        # The document-selection popup echoes back the number that was typed into it.
        if ref.name == "select.requisition_number":
            return self.fields.get(ref.name) or self.fields.get("_document", "")
        return super().read_field(ref)
