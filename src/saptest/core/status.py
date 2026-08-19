"""SAP status-bar messages, identified by message class and number.

The scripts this library replaces keyed their error handling on English status-bar
*text*, which breaks on any non-English logon language and on messages that embed
runtime data (a hardcoded date in the key stops matching once the date passes).

SAP exposes a stable, language-independent identity for every message: the message
class (``MessageId``) plus the message number (``MessageNumber``), e.g. ``ME 083``.
That pair is what the error catalogue matches on. Rendered text is retained for
reporting and as a fallback for drivers that cannot read the structured fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class MessageType(StrEnum):
    """SAP message types as reported by ``GuiStatusbar.MessageType``."""

    SUCCESS = "S"
    WARNING = "W"
    ERROR = "E"
    ABORT = "A"
    INFO = "I"
    NONE = ""

    @classmethod
    def parse(cls, raw: str | None) -> MessageType:
        value = (raw or "").strip().upper()
        try:
            return cls(value)
        except ValueError:
            return cls.NONE

    @property
    def is_problem(self) -> bool:
        """True for message types that indicate the action did not succeed."""
        return self in (MessageType.ERROR, MessageType.ABORT)


@dataclass(frozen=True, slots=True)
class StatusMessage:
    """A single message read from the SAP status bar.

    ``id`` and ``number`` are empty when the driver could only obtain rendered text
    (the legacy_ui fallback driver). In that case catalogue matching falls back to
    regex over ``text``.
    """

    type: MessageType = MessageType.NONE
    id: str = ""
    number: str = ""
    text: str = ""
    params: tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> str:
        """Language-independent identity, e.g. ``ME083``. Empty when unstructured."""
        if not self.id or not self.number:
            return ""
        return f"{self.id.strip().upper()}{self.number.strip()}"

    @property
    def is_empty(self) -> bool:
        """True when the status bar carried nothing at all."""
        return not self.text and not self.key and self.type is MessageType.NONE

    @property
    def is_problem(self) -> bool:
        return self.type.is_problem

    def __str__(self) -> str:
        if self.key:
            return f"[{self.type.value or '-'} {self.key}] {self.text}"
        return f"[{self.type.value or '-'}] {self.text}"


#: ``findById("wnd[0]/sbar")`` exposes these properties on the SAP GUI Scripting API.
_SBAR_FIELDS = ("MessageType", "MessageId", "MessageNumber", "MessageParameter", "Text")


def from_statusbar(sbar) -> StatusMessage:  # noqa: ANN001 - COM object, no stub available
    """Build a :class:`StatusMessage` from a SAP GUI Scripting ``GuiStatusbar``.

    Every property access is guarded: some SAP GUI versions omit individual
    properties on certain screens, and a missing property must degrade to an empty
    field rather than abort the step.
    """

    def get(name: str) -> str:
        try:
            value = getattr(sbar, name)
        except Exception:  # noqa: BLE001 - COM raises assorted, undeclared errors
            return ""
        return "" if value is None else str(value)

    raw_params = get("MessageParameter")
    params = tuple(p for p in raw_params.split(";") if p) if raw_params else ()

    return StatusMessage(
        type=MessageType.parse(get("MessageType")),
        id=get("MessageId").strip(),
        number=get("MessageNumber").strip(),
        text=get("Text").strip(),
        params=params,
    )


#: Matches "ME 083", "ME083", "ME-083" as they appear in long-text / dialog captures.
_TEXT_KEY_RE = re.compile(r"\b([A-Z0-9_/]{1,20}?)[\s\-]?(\d{3})\b")


def from_text(text: str, type_: MessageType = MessageType.NONE) -> StatusMessage:
    """Build a :class:`StatusMessage` from rendered text alone.

    Used by the legacy_ui driver, which has no access to the structured properties.
    If the text happens to embed a message key, it is recovered so that catalogue
    entries keyed on class+number still match.
    """
    text = (text or "").strip()
    match = _TEXT_KEY_RE.search(text)
    if match:
        return StatusMessage(
            type=type_, id=match.group(1), number=match.group(2), text=text
        )
    return StatusMessage(type=type_, text=text)
