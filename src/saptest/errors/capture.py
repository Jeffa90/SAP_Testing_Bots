"""Capture of SAP messages that the catalogue does not yet recognise.

The catalogue cannot be written up front -- nobody knows every message a landscape
will produce. So every unrecognised message is recorded with the context needed to
write a rule for it: transaction, screen, the SAP user, the case and step, and the
message's own class and number.

``saptest catalog review`` turns these records into draft catalogue entries. The
catalogue therefore grows from what runs actually hit, rather than from guesswork,
and each region's rules accumulate as that region is exercised.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from saptest.core.status import StatusMessage

log = logging.getLogger(__name__)


@dataclass(slots=True)
class UnknownMessage:
    """One occurrence of an unrecognised message."""

    key: str
    message_id: str
    number: str
    type: str
    text: str
    tcode: str = ""
    window_title: str = ""
    sap_user: str = ""
    case_id: str = ""
    step_id: str = ""
    run_id: str = ""
    screenshot: str = ""
    seen_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @classmethod
    def from_message(cls, message: StatusMessage, **context: str) -> UnknownMessage:
        return cls(
            key=message.key,
            message_id=message.id,
            number=message.number,
            type=message.type.value,
            text=message.text,
            **context,
        )


class UnknownMessageCapture:
    """Append-only JSONL log of unrecognised messages."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, message: StatusMessage, **context: str) -> UnknownMessage:
        entry = UnknownMessage.from_message(message, **context)
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(entry), default=str) + "\n")
        except OSError as exc:
            log.warning("Could not record unknown message: %s", exc)
        log.info("Unrecognised SAP message captured: %s", message)
        return entry

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("Skipping malformed unknown-message line")
        return records

    def summarise(self) -> list[dict]:
        """Group occurrences by message identity, most frequent first.

        Identity is the message key when available, falling back to the text -- so
        the legacy_ui driver's text-only messages still group sensibly.
        """
        records = self.read()
        groups: dict[str, dict] = {}
        for record in records:
            identity = record.get("key") or record.get("text", "")
            if not identity:
                continue
            group = groups.setdefault(
                identity,
                {
                    "identity": identity,
                    "key": record.get("key", ""),
                    "message_id": record.get("message_id", ""),
                    "number": record.get("number", ""),
                    "type": record.get("type", ""),
                    "count": 0,
                    "texts": Counter(),
                    "tcodes": Counter(),
                    "users": Counter(),
                    "examples": [],
                },
            )
            group["count"] += 1
            if record.get("text"):
                group["texts"][record["text"]] += 1
            if record.get("tcode"):
                group["tcodes"][record["tcode"]] += 1
            if record.get("sap_user"):
                group["users"][record["sap_user"]] += 1
            if len(group["examples"]) < 3:
                group["examples"].append(record)

        summary = []
        for group in groups.values():
            summary.append(
                {
                    **{k: v for k, v in group.items() if k not in ("texts", "tcodes", "users")},
                    "sample_text": group["texts"].most_common(1)[0][0] if group["texts"] else "",
                    "tcodes": [t for t, _ in group["tcodes"].most_common()],
                    "users": [u for u, _ in group["users"].most_common()],
                }
            )
        return sorted(summary, key=lambda g: g["count"], reverse=True)

    def draft_entries(self) -> list[dict]:
        """Draft catalogue entries for review, ready to paste into a catalogue file.

        Deliberately conservative: every draft is ``fail_step`` with no remediation.
        A human decides what action is safe, because a wrong remediation silently
        changes what the test proves.
        """
        drafts = []
        for group in self.summarise():
            if group["key"]:
                match = {"message_id": group["message_id"], "number": group["number"]}
                name = f"{group['message_id'].lower()}_{group['number']}"
            else:
                # No structured identity: anchor a regex on the observed text.
                match = {"regex": _escape_for_yaml_regex(group["sample_text"])}
                name = _slug(group["sample_text"])

            drafts.append(
                {
                    "name": name,
                    "match": match,
                    "actions": [],
                    "outcome": "FAIL",
                    "on_exhausted": "fail_step",
                    "notes": (
                        f"DRAFT - seen {group['count']}x in "
                        f"{', '.join(group['tcodes']) or 'unknown transaction'}. "
                        f"Text: {group['sample_text']!r}. "
                        "Review and set an action before relying on this."
                    ),
                }
            )
        return drafts


def _escape_for_yaml_regex(text: str) -> str:
    """Turn observed message text into a regex that tolerates embedded data.

    Digits are replaced with ``\\d+`` so a rule written from "Item 10 delivery date
    31.10.2025 is in the past" still matches item 20 next month -- the exact failure
    mode of the hardcoded text keys in the original scripts.
    """
    import re

    escaped = re.escape(text.strip()[:120])
    return re.sub(r"(?:\\d|\d)+", r"\\d+", escaped)


def _slug(text: str) -> str:
    import re

    words = re.findall(r"[a-z0-9]+", text.lower())[:6]
    return "_".join(words) or "unnamed_message"
