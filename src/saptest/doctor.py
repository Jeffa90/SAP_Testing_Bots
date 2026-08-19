"""Preflight checks: catch configuration problems before a run, not during one.

A run can take hours and consumes real document numbers in a test client. Every
check here is one that would otherwise surface halfway through and waste that.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path

from saptest.config.models import RegionProfile


@dataclass
class Check:
    """One diagnostic result."""

    name: str
    ok: bool
    detail: str = ""
    #: A problem that does not stop a run, e.g. bindings still awaiting id capture.
    warning: bool = False

    @property
    def symbol(self) -> str:
        return "!" if (self.warning and not self.ok) else ("OK" if self.ok else "X")


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", warning: bool = False) -> None:
        self.checks.append(Check(name, ok, detail, warning))

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and not c.warning]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.warning]

    @property
    def ok(self) -> bool:
        return not self.failures

    def render(self) -> str:
        width = max((len(c.name) for c in self.checks), default=10)
        lines = []
        for check in self.checks:
            lines.append(f"  [{check.symbol:>2}] {check.name:<{width}}  {check.detail}")
        return "\n".join(lines)


def check_environment(report: Report) -> None:
    """Platform and SAP GUI availability."""
    report.add("python", True, f"{sys.version.split()[0]} on {platform.system()}")

    if platform.system() != "Windows":
        report.add(
            "platform",
            False,
            "Not Windows. SAP GUI Scripting needs SAP GUI, which is Windows-only. "
            "Use --driver fake to rehearse a run here.",
            warning=True,
        )
        return
    report.add("platform", True, "Windows")

    try:
        import win32com.client  # noqa: F401, PLC0415

        report.add("pywin32", True, "COM bridge available")
    except ImportError:
        report.add(
            "pywin32", False,
            "Not installed. Run: pip install 'saptest[windows]'",
        )
        return

    try:
        import win32com.client as com  # noqa: PLC0415

        sap_gui = com.GetObject("SAPGUI")
        report.add("sap gui", True, "running")
        engine = sap_gui.GetScriptingEngine
        if engine is None:
            report.add(
                "client scripting", False,
                "Disabled. Enable in SAP Logon > Options > Accessibility & Scripting "
                "> Scripting > 'Enable scripting'.",
            )
        else:
            report.add("client scripting", True, "enabled")
            _check_open_connections(report, engine)
    except Exception:  # noqa: BLE001
        report.add(
            "sap gui", False,
            "Not running. Start SAP Logon before running a test.",
            warning=True,
        )

    _check_script_warnings(report)


def _check_open_connections(report: Report, engine) -> None:  # noqa: ANN001
    """Whether the application server permits scripting, visible via a live session."""
    try:
        count = engine.Children.Count
    except Exception as exc:  # noqa: BLE001
        report.add("server scripting", False, f"Could not enumerate connections: {exc}", True)
        return

    if count == 0:
        report.add(
            "server scripting", True,
            "No open connection to verify against; will be checked at logon.",
            warning=True,
        )
        return

    for index in range(count):
        try:
            connection = engine.Children(index)
            if connection.Children.Count == 0:
                report.add(
                    "server scripting", False,
                    "A connection is open but exposes no scriptable session. This usually "
                    "means sapgui/user_scripting is FALSE on the application server. Ask "
                    "Basis to set it to TRUE (changeable dynamically via RZ11).",
                )
                return
        except Exception:  # noqa: BLE001
            continue
    report.add("server scripting", True, "sessions are scriptable")


def _check_script_warnings(report: Report) -> None:
    """The two notification popups that otherwise interrupt every run.

    The original scripts fought these with a background thread that blind-pressed
    Enter. Turning them off is the actual fix.
    """
    try:
        import winreg  # noqa: PLC0415
    except ImportError:
        return

    path = r"Software\SAP\SAPGUI Front\SAP Frontend Server\Security"
    noisy = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            for value in ("WarnOnAttach", "WarnOnConnection"):
                try:
                    data, _ = winreg.QueryValueEx(key, value)
                    if data:
                        noisy.append(value)
                except FileNotFoundError:
                    continue
    except OSError:
        report.add(
            "script notifications", True,
            "Could not read SAP GUI settings; if a 'script is attaching' popup appears, "
            "disable the two notification options under Options > Accessibility & Scripting.",
            warning=True,
        )
        return

    if noisy:
        report.add(
            "script notifications", False,
            f"{' and '.join(noisy)} enabled -- SAP GUI will interrupt each run with a "
            "popup. Turn both off in Options > Accessibility & Scripting > Scripting.",
            warning=True,
        )
    else:
        report.add("script notifications", True, "suppressed")


def check_profile(report: Report, profile: RegionProfile) -> None:
    """Bindings, catalogue and templates named by a region profile."""
    from saptest.core.fields import load_fields  # noqa: PLC0415
    from saptest.errors.catalog import load_catalog  # noqa: PLC0415
    from saptest.errors.resolver import validate_catalog  # noqa: PLC0415

    report.add(
        "region profile", True,
        f"{profile.region} -> connection {profile.connection.name!r}, "
        f"client {profile.connection.client or '(default)'}, "
        f"logon {profile.connection.logon_mode.value}",
    )

    try:
        fields = load_fields(*profile.binding_paths())
    except Exception as exc:  # noqa: BLE001
        report.add("field bindings", False, str(exc))
        return

    unbound = [f.name for f in fields if not f.id]
    if unbound:
        report.add(
            "field bindings", False,
            f"{len(fields)} bound, {len(unbound)} still awaiting ids "
            f"(e.g. {', '.join(sorted(unbound)[:3])}). Capture them with: saptest inspect",
            warning=True,
        )
    else:
        report.add("field bindings", True, f"{len(fields)} fields bound")

    try:
        catalog = load_catalog(*profile.catalog_paths())
    except Exception as exc:  # noqa: BLE001
        report.add("error catalogue", False, str(exc))
        return

    problems = validate_catalog(catalog, fields)
    if problems:
        report.add("error catalogue", False, f"{len(problems)} problem(s): {problems[0]}")
    else:
        report.add("error catalogue", True, f"{len(catalog.entries)} rules, all valid")

    for name in profile.templates:
        path = profile.template_path(name)
        report.add(
            f"template {name}",
            bool(path and path.exists()),
            str(path) if path and path.exists() else f"missing: {path}",
            warning=True,
        )


def check_data(report: Report, path: Path, sheet: str | None, flow_name: str | None) -> None:
    """The data workbook: readable, has the columns the flow needs, has usable rows."""
    from saptest.data.workbook import load_dataset  # noqa: PLC0415
    from saptest.flows import get_flow  # noqa: PLC0415

    try:
        data = load_dataset(path, sheet)
    except Exception as exc:  # noqa: BLE001
        report.add("test data", False, str(exc))
        return

    report.add("test data", True, f"{len(data)} row(s) from sheet {data.sheet!r}")

    if not flow_name:
        return
    try:
        flow = get_flow(flow_name)
        flow.validate(data)
        report.add("required columns", True, f"all present for {flow_name}")
    except Exception as exc:  # noqa: BLE001
        report.add("required columns", False, str(exc))
        return

    with_password = sum(1 for r in data.rows if r.get_col("First Password"))
    without = len(data) - with_password
    if with_password and without:
        report.add(
            "logon mode", True,
            f"mixed: {with_password} row(s) with a password, {without} via SSO",
            warning=True,
        )
    elif without:
        report.add("logon mode", True, f"all {without} row(s) will use SSO")
    else:
        report.add("logon mode", True, f"all {with_password} row(s) use explicit credentials")


def run_diagnostics(
    profile: RegionProfile | None = None,
    data_path: Path | None = None,
    sheet: str | None = None,
    flow: str | None = None,
) -> Report:
    """Run every applicable check."""
    report = Report()
    check_environment(report)
    if profile is not None:
        check_profile(report, profile)
    if data_path is not None:
        check_data(report, data_path, sheet, flow)
    return report
