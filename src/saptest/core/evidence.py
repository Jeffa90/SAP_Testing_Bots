"""Evidence capture: screenshots plus a tamper-evident manifest.

Three differences from the original ``Screenshot()`` helper, all of which matter for
audit evidence:

* Capture is scoped to the SAP window by the driver, not the whole desktop, so a
  tester's unrelated applications never land in a UAT evidence pack.
* Files are namespaced per run and per case, so two cases cannot overwrite each
  other's screenshots. The original wrote a flat ``screenshots/<name>.png``, which
  silently clobbered on a re-run.
* Every file is hashed and recorded with the transaction, window title and SAP user
  it was taken under, so a report can state what a screenshot actually shows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from saptest.core.outcomes import EvidenceRef
from saptest.drivers.base import Driver

log = logging.getLogger(__name__)

#: Characters Windows forbids in filenames, plus whitespace runs.
_UNSAFE = re.compile(r'[\\/:*?"<>|]+')
_SPACES = re.compile(r"\s+")


def safe_name(text: str, fallback: str = "unnamed") -> str:
    """Make a string safe as a Windows filename component."""
    cleaned = _SPACES.sub("_", _UNSAFE.sub("_", str(text)).strip())
    cleaned = cleaned.strip("._")
    return cleaned or fallback


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


class EvidenceStore:
    """Writes screenshots and the run manifest for one run."""

    def __init__(self, root: Path | str, run_id: str) -> None:
        self.run_id = run_id
        self.root = Path(root) / safe_name(run_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "evidence.jsonl"

    def case_dir(self, case_id: str) -> Path:
        d = self.root / safe_name(case_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def capture(
        self,
        driver: Driver,
        case_id: str,
        step_id: str,
        tag: str,
        sap_user: str = "",
    ) -> EvidenceRef | None:
        """Screenshot the SAP window and record it.

        Returns ``None`` if capture failed. A failed screenshot must never abort a
        test -- losing one piece of evidence is far cheaper than losing the run, and
        the failure is logged and surfaced in the report.
        """
        filename = f"{safe_name(step_id)}_{safe_name(tag)}.png"
        target = self.case_dir(case_id) / filename
        try:
            data = driver.screenshot()
        except Exception as exc:  # noqa: BLE001 - capture must never break a run
            log.warning("Screenshot failed for %s/%s (%s): %s", case_id, step_id, tag, exc)
            return None

        try:
            target.write_bytes(data)
        except OSError as exc:
            log.warning("Could not write screenshot %s: %s", target, exc)
            return None

        ref = EvidenceRef(
            path=str(target),
            tag=tag,
            tcode=_safe_call(driver.current_tcode),
            window_title=_safe_call(driver.window_title),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        self._append(case_id, step_id, ref, sap_user)
        return ref

    def _append(self, case_id: str, step_id: str, ref: EvidenceRef, sap_user: str) -> None:
        record = {
            "run_id": self.run_id,
            "case_id": case_id,
            "step_id": step_id,
            "sap_user": sap_user,
            **asdict(ref),
        }
        record["captured_at"] = ref.captured_at.isoformat()
        try:
            with self.manifest_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:
            log.warning("Could not append to evidence manifest: %s", exc)

    def read_manifest(self) -> list[dict]:
        """All manifest records, skipping any line corrupted by an interrupted run."""
        if not self.manifest_path.exists():
            return []
        records = []
        for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("Skipping malformed evidence manifest line")
        return records

    def verify(self) -> list[str]:
        """Re-hash every recorded file. Returns descriptions of any mismatches."""
        problems = []
        for record in self.read_manifest():
            path = Path(record["path"])
            if not path.exists():
                problems.append(f"missing: {path}")
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != record.get("sha256"):
                problems.append(f"changed since capture: {path}")
        return problems


def _safe_call(fn) -> str:  # noqa: ANN001
    try:
        return str(fn() or "")
    except Exception:  # noqa: BLE001 - context is best-effort metadata
        return ""


def utc_now() -> datetime:
    return datetime.now(UTC)
