"""Extracting SAP document numbers from confirmation messages.

The original scripts captured a document number by pressing Shift+F5 to open the
"Select Document" popup, sending Ctrl+A and Ctrl+C, and reading the clipboard --
which depended on focus, on the popup pre-filling the last document, and on nothing
else touching the clipboard meanwhile.

SAP already reports the number in the confirmation message, both as a message
parameter and in the rendered text. Reading it there is exact and needs no
round trip.
"""

from __future__ import annotations

import re

from saptest.core.status import StatusMessage

#: SAP document numbers are 8-12 digits. Anchored on word boundaries so a date
#: like 31.10.2026 cannot be mistaken for one.
_NUMBER = re.compile(r"\b(\d{8,12})\b")


def extract_document_number(message: StatusMessage) -> str:
    """Pull a document number out of a creation confirmation.

    Message parameters are preferred: they are the values SAP substituted into the
    message, so no parsing of localised wording is involved. Falls back to scanning
    the rendered text.
    """
    for param in message.params:
        candidate = str(param).strip()
        if candidate.isdigit() and 8 <= len(candidate) <= 12:
            return candidate

    match = _NUMBER.search(message.text or "")
    return match.group(1) if match else ""


def line_numbers(count: int, step: int = 10) -> list[str]:
    """SAP item numbering: 10, 20, 30... as written back to the data workbook."""
    return [str((i + 1) * step) for i in range(count)]
