"""Exception hierarchy.

Split along a single axis: whether the failure is a finding about the system under
test (:class:`TestFailure` and subclasses) or a failure of the harness itself
(:class:`HarnessError`). The runner maps the first to ``FAIL``/``BLOCKED_AUTH`` and
the second to ``ERROR``, so a broken bot is never reported as a broken SAP system.
"""

from __future__ import annotations


class SapTestError(Exception):
    """Base for everything this library raises."""


# --- Findings about the system under test -----------------------------------------


class TestFailure(SapTestError):
    """The system under test did not behave as the test expected.

    Carries the outcome to record and the control-flow policy to apply, so the
    runner does not have to re-derive them from the exception type.
    """

    #: Outcome name recorded for the step. Resolved lazily to avoid a circular import.
    default_outcome = "FAIL"
    #: Control-flow policy name applied once the step has failed.
    default_policy = "fail_step"

    def __init__(self, message: str, outcome: str = "", policy: str = "", detail: str = ""):
        super().__init__(message)
        self.outcome = outcome or self.default_outcome
        self.policy = policy or self.default_policy
        self.detail = detail or message


class AuthorizationBlocked(TestFailure):
    """The SAP user lacks the authorisation needed to perform this step.

    Reported as ``BLOCKED_AUTH``, never as ``FAIL`` -- the step was not tested, so
    it is not evidence of a defect. Aborts the case by default because later steps
    almost always depend on the blocked one.
    """

    default_outcome = "BLOCKED_AUTH"
    default_policy = "abort_case"


class UnhandledSapMessage(TestFailure):
    """SAP returned an error message with no catalogue entry, or remediation failed."""


# --- Failures of the harness ------------------------------------------------------


class HarnessError(SapTestError):
    """The harness could not carry out the step; the test result is unknown."""


class DriverUnavailable(HarnessError):
    """The requested driver cannot run here (missing dependency, wrong platform)."""


class ScriptingDisabled(HarnessError):
    """SAP GUI Scripting is not enabled on the client or the application server."""


class SapGuiNotRunning(HarnessError):
    """SAP GUI / SAP Logon is not running and could not be started."""


class LogonFailed(HarnessError):
    """Logon did not reach the SAP Easy Access screen."""


class LogonModeMismatch(LogonFailed):
    """The connection presented a different logon mode than the profile expected.

    Raised when SSO was expected but a logon screen appeared, or credentials were
    supplied but no logon screen was presented.
    """


class ElementNotFound(HarnessError):
    """A field could not be located by any strategy the driver supports."""


class NavigationError(HarnessError):
    """The session did not reach the expected screen."""


class ConfigError(SapTestError):
    """A profile, binding, or catalogue file is missing or invalid."""


class DataError(SapTestError):
    """The test-data workbook is missing required columns or holds unusable values."""
