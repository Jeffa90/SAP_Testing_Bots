"""Background run management for the web interface.

One constraint shapes this whole module: **a machine can only execute one run at a
time**. SAP GUI Scripting drives the interactive desktop session, so two concurrent
runs would fight over the same SAP GUI windows and corrupt each other. The manager
enforces that with a lock and says so plainly, rather than letting an operator start
a second run that quietly ruins the first.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from saptest.core.outcomes import RunResult
from saptest.core.runner import RunConfig, Runner
from saptest.reporting import ReportBundle, write_reports

log = logging.getLogger(__name__)

#: Sent to every subscriber when a run ends, so browsers stop listening.
SENTINEL = {"kind": "stream_closed", "payload": {}}


@dataclass
class LiveRun:
    """A run in progress, or the record of one that finished this session."""

    run_id: str
    flow: str
    region: str
    status: str = "queued"  # queued | running | finished | failed
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    events: list[dict] = field(default_factory=list)
    result: RunResult | None = None
    bundle: ReportBundle | None = None
    error: str = ""
    run_dir: Path | None = None
    _subscribers: list[queue.Queue] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def publish(self, kind: str, payload: dict) -> None:
        event = {"kind": kind, "payload": payload}
        with self._lock:
            self.events.append(event)
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                log.debug("Dropping event for a slow subscriber")

    def subscribe(self) -> queue.Queue:
        """Attach a listener, replaying what it missed so a late browser is not blank."""
        channel: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            for event in self.events:
                channel.put_nowait(event)
            if self.status in ("finished", "failed"):
                channel.put_nowait(SENTINEL)
            else:
                self._subscribers.append(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)

    def close(self) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(SENTINEL)
            except queue.Full:
                pass

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "flow": self.flow,
            "region": self.region,
            "status": self.status,
            "started_at": self.started_at.strftime("%Y-%m-%d %H:%M"),
            "outcome": self.result.outcome.value if self.result else "",
            "tally": (
                {k.value: v for k, v in self.result.tally().items()} if self.result else {}
            ),
            "error": self.error,
        }


class RunBusy(RuntimeError):
    """Raised when a run is requested while another is still executing."""


class RunManager:
    """Owns the single run slot and the history on disk."""

    def __init__(self, output_root: Path | str = "runs") -> None:
        self.output_root = Path(output_root)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._runs: dict[str, LiveRun] = {}
        self._active: str | None = None
        self._lock = threading.Lock()

    # --- state ----------------------------------------------------------------

    @property
    def active(self) -> LiveRun | None:
        with self._lock:
            return self._runs.get(self._active) if self._active else None

    def get(self, run_id: str) -> LiveRun | None:
        return self._runs.get(run_id)

    def start(self, config: RunConfig, header: dict[str, str] | None = None) -> LiveRun:
        """Begin a run in a background thread. Raises :class:`RunBusy` if one is live."""
        with self._lock:
            if self._active is not None:
                current = self._runs.get(self._active)
                if current and current.status in ("queued", "running"):
                    raise RunBusy(
                        f"Run {current.run_id} is still executing. Only one run can use "
                        "SAP GUI on this machine at a time -- wait for it to finish, or "
                        "stop it before starting another."
                    )
            live = LiveRun(
                run_id=config.run_id, flow=config.flow.name, region=config.profile.region
            )
            self._runs[config.run_id] = live
            self._active = config.run_id

        thread = threading.Thread(
            target=self._execute,
            args=(live, config, header or {}),
            name=f"saptest-run-{config.run_id}",
            daemon=True,
        )
        thread.start()
        return live

    def _execute(self, live: LiveRun, config: RunConfig, header: dict[str, str]) -> None:
        live.status = "running"
        runner = Runner(config, listener=live.publish)
        live.run_dir = runner.run_dir
        try:
            live.result = runner.run()
            live.bundle = write_reports(
                live.result, config.profile, config.flow, runner.run_dir, runner.capture, header
            )
            live.status = "finished"
            live.publish(
                "reports_ready",
                {
                    "summary": str(live.bundle.summary_html or ""),
                    "test_plans": len(live.bundle.test_plans),
                    "problems": live.bundle.problems,
                },
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator, not swallowed
            live.status = "failed"
            live.error = f"{type(exc).__name__}: {exc}"
            log.exception("Run %s failed", live.run_id)
            live.publish("run_failed", {"error": live.error})
        finally:
            live.close()
            with self._lock:
                if self._active == live.run_id:
                    self._active = None

    # --- history --------------------------------------------------------------

    def history(self, limit: int = 50) -> list[dict]:
        """Past runs, read from the summary.json each run leaves behind.

        Reading from disk rather than memory means history survives a restart of the
        UI, which matters when the evidence is the point of the exercise.
        """
        entries: list[dict] = []
        for directory in sorted(self.output_root.glob("run_*"), reverse=True)[:limit]:
            summary = directory / "summary.json"
            record: dict[str, Any] = {
                "run_id": directory.name,
                "path": str(directory),
                "flow": "",
                "region": "",
                "outcome": "",
                "started_at": "",
                "tally": {},
                "cases": 0,
            }
            if summary.exists():
                try:
                    data = json.loads(summary.read_text(encoding="utf-8"))
                    record.update(
                        flow=data.get("flow", ""),
                        region=data.get("region", ""),
                        outcome=data.get("outcome", ""),
                        started_at=data.get("started_at", ""),
                        tally=data.get("tally", {}),
                        cases=len(data.get("cases", [])),
                    )
                except (json.JSONDecodeError, OSError):
                    record["outcome"] = "unreadable"
            else:
                live = self._runs.get(directory.name)
                record["outcome"] = live.status if live else "incomplete"
            entries.append(record)
        return entries

    def artifacts(self, run_id: str) -> dict[str, Path]:
        """Downloadable files a finished run produced."""
        directory = self.output_root / run_id
        found: dict[str, Path] = {}
        for label, name in (
            ("summary.html", "summary.html"),
            ("summary.xlsx", "summary.xlsx"),
            ("summary.json", "summary.json"),
            ("run.log", "run.log"),
            ("unknown_messages.jsonl", "unknown_messages.jsonl"),
        ):
            path = directory / name
            if path.exists():
                found[label] = path
        plans = directory / "test_plans"
        if plans.is_dir():
            for plan in sorted(plans.glob("*.xlsx")):
                found[f"test_plans/{plan.name}"] = plan
        return found
