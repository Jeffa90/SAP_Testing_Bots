"""The remediation loop: match a message, act on it, decide what happens next.

This replaces the dispatch in the original scripts::

    error_message = session.findById("wnd[0]/sbar").Text
    if error_message in function_dict:
        function_dict[error_message]()
    else:
        print(f"Error occured with text: {error_message}")

which matched on English text, ran exactly one remediation attempt, never checked
whether the remediation worked, and had no way to express "this one should stop the
case" versus "log it and carry on".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from saptest.core.exceptions import ConfigError, SapTestError
from saptest.core.fields import FieldRegistry
from saptest.core.outcomes import Outcome
from saptest.core.status import StatusMessage
from saptest.drivers.base import Driver
from saptest.errors.capture import UnknownMessageCapture
from saptest.errors.catalog import Catalog, CatalogEntry, ErrorPolicy
from saptest.errors.handlers import HandlerContext, run_action

log = logging.getLogger(__name__)

#: Ceiling on remediation actions for a single check, across all messages seen.
#: Guards against two rules that each "fix" the other's error indefinitely.
DEFAULT_BUDGET = 6


@dataclass(slots=True)
class Resolution:
    """What happened when the resolver looked at the status bar."""

    resolved: bool
    message: StatusMessage
    entry: CatalogEntry | None = None
    initial: StatusMessage = field(default_factory=StatusMessage)
    attempts: int = 0
    actions_run: list[str] = field(default_factory=list)
    messages_seen: list[StatusMessage] = field(default_factory=list)
    unknown: bool = False
    outcome: Outcome = Outcome.PASS
    policy: ErrorPolicy = ErrorPolicy.CONTINUE
    detail: str = ""

    @property
    def remediated(self) -> bool:
        """Resolved, but only because remediation actions ran."""
        return self.resolved and bool(self.actions_run)


class MessageResolver:
    """Applies the catalogue to whatever the status bar currently holds."""

    def __init__(
        self,
        catalog: Catalog,
        fields: FieldRegistry,
        profile: dict | None = None,
        capture: UnknownMessageCapture | None = None,
        budget: int = DEFAULT_BUDGET,
    ) -> None:
        self.catalog = catalog
        self.fields = fields
        self.profile = profile or {}
        self.capture = capture
        self.budget = budget

    def resolve(
        self,
        driver: Driver,
        tcode: str = "",
        case: dict | None = None,
        context: dict[str, str] | None = None,
    ) -> Resolution:
        """Read the status bar and remediate what it holds.

        ``context`` is metadata (case id, step id, run id, screenshot path) attached
        to any unknown message recorded for later catalogue review.
        """
        case = case or {}
        context = context or {}

        message = driver.status()
        result = Resolution(resolved=True, message=message, initial=message)
        if message.is_empty:
            return result

        result.messages_seen.append(message)
        seen_keys: set[str] = set()
        spend = 0

        while True:
            entry = self.catalog.lookup(message, tcode)

            if entry is None:
                if not message.is_problem:
                    # An informational or success message with no rule is just noise.
                    result.resolved = True
                    result.message = message
                    return result
                return self._unknown(result, message, driver, tcode, context)

            result.entry = entry
            identity = message.key or message.text

            if not entry.actions:
                # A classify-only rule (authorisation, or a known-benign message).
                return self._finish(result, entry, message)

            if identity in seen_keys:
                # The same message came back after its own remediation: the rule is
                # not working here. Stop rather than loop.
                result.detail = f"Remediation for {entry.name!r} did not clear {identity!r}"
                return self._finish(result, entry, message)
            seen_keys.add(identity)

            for attempt in range(1, entry.max_attempts + 1):
                if spend >= self.budget:
                    result.detail = (
                        f"Remediation budget of {self.budget} actions exhausted; "
                        f"last message {message}"
                    )
                    return self._finish(result, entry, message)

                result.attempts += 1
                spend += 1
                log.info(
                    "Remediating %s via %r (attempt %d/%d)",
                    message,
                    entry.name,
                    attempt,
                    entry.max_attempts,
                )
                try:
                    self._run(entry, driver, message, case)
                except SapTestError as exc:
                    result.detail = f"Remediation {entry.name!r} failed: {exc}"
                    return self._finish(result, entry, message)
                except Exception as exc:  # noqa: BLE001 - a bad rule must not kill the run
                    result.detail = f"Remediation {entry.name!r} raised: {exc}"
                    return self._finish(result, entry, message)

                result.actions_run.extend(a.handler for a in entry.actions)

                message = driver.status()
                result.message = message
                if not message.is_empty:
                    result.messages_seen.append(message)

                if message.is_empty or not message.is_problem:
                    log.info("Remediation %r cleared the message", entry.name)
                    result.resolved = True
                    return result

                if (message.key or message.text) != identity:
                    break  # a different problem: re-enter the outer loop and re-look-up
            else:
                # max_attempts exhausted on the same message.
                return self._finish(result, entry, message)

    def _run(self, entry: CatalogEntry, driver: Driver, message: StatusMessage, case: dict) -> None:
        ctx = HandlerContext(
            driver=driver,
            fields=self.fields,
            message=message,
            profile=self.profile,
            case=case,
        )
        for action in entry.actions:
            run_action(ctx, action.handler, action.args)

    def _finish(
        self, result: Resolution, entry: CatalogEntry, message: StatusMessage
    ) -> Resolution:
        """Apply an entry's declared outcome and policy to an unresolved message."""
        result.message = message
        result.entry = entry
        result.outcome = entry.outcome
        result.policy = entry.on_exhausted
        # CONTINUE means the message is known and tolerable, so the step is not failed.
        result.resolved = entry.on_exhausted is ErrorPolicy.CONTINUE
        if result.resolved:
            result.outcome = Outcome.PASS
        if not result.detail:
            result.detail = entry.notes or message.text
        return result

    def _unknown(
        self,
        result: Resolution,
        message: StatusMessage,
        driver: Driver,
        tcode: str,
        context: dict[str, str],
    ) -> Resolution:
        """Record a message with no catalogue rule and fail the step conservatively."""
        result.resolved = False
        result.unknown = True
        result.message = message
        result.outcome = Outcome.FAIL
        result.policy = ErrorPolicy.FAIL_STEP
        result.detail = message.text or str(message)

        if self.capture is not None:
            try:
                self.capture.record(
                    message,
                    tcode=tcode or _safe(driver.current_tcode),
                    window_title=_safe(driver.window_title),
                    **context,
                )
            except Exception as exc:  # noqa: BLE001 - capture is diagnostics, not the test
                log.warning("Could not capture unknown message: %s", exc)
        else:
            log.warning("Unrecognised SAP message (not captured): %s", message)
        return result


def _safe(fn) -> str:  # noqa: ANN001
    try:
        return str(fn() or "")
    except Exception:  # noqa: BLE001
        return ""


def validate_catalog(catalog: Catalog, fields: FieldRegistry | None = None) -> list[str]:
    """Check every rule references a real handler and, where given, a real field.

    Run by ``saptest doctor`` and by the test suite, so a typo in a catalogue file
    surfaces before a run rather than halfway through one.
    """
    from saptest.errors.handlers import handler_names  # noqa: PLC0415

    known = handler_names()
    problems: list[str] = []
    for entry in catalog.entries:
        for action in entry.actions:
            if action.handler not in known:
                problems.append(
                    f"{entry.name}: unknown handler {action.handler!r} "
                    f"(available: {', '.join(sorted(known))})"
                )
            field_name = action.args.get("field")
            if fields is not None and isinstance(field_name, str):
                try:
                    fields.get(field_name)
                except ConfigError as exc:
                    problems.append(f"{entry.name}: {exc}")
    return problems
