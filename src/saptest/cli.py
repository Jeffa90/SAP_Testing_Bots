"""Command line interface."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import typer

from saptest import __version__
from saptest.core.exceptions import SapTestError

app = typer.Typer(
    add_completion=False,
    help="SAP S/4HANA role-aware UAT automation.",
    no_args_is_help=True,
)
catalog_app = typer.Typer(help="Inspect and grow the error catalogue.", no_args_is_help=True)
app.add_typer(catalog_app, name="catalog")


def _setup_logging(verbose: bool, log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def _fail(message: str) -> None:
    typer.secho(f"\n{message}\n", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"saptest {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the version and exit.",
        # Eager so it is handled before the "missing command" check fires.
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """SAP S/4HANA role-aware UAT automation."""


# --- discovery --------------------------------------------------------------------


@app.command("regions")
def list_regions() -> None:
    """List the region profiles available."""
    from saptest.config import discover_regions, load_region

    names = discover_regions()
    if not names:
        typer.echo("No region profiles found under profiles/regions/.")
        return
    for name in names:
        profile = load_region(name)
        typer.echo(f"  {name:<6} {profile.description or '(no description)'}")


@app.command("flows")
def list_flows() -> None:
    """List the test flows available."""
    from saptest.flows.registry import flow_catalogue

    for entry in flow_catalogue():
        typer.echo(f"  {entry['name']:<20} {entry['title']}  ({entry['steps']} steps)")


# --- diagnostics ------------------------------------------------------------------


@app.command()
def doctor(
    region: str = typer.Option(None, "--region", "-r", help="Region profile to check."),
    data: Path = typer.Option(None, "--data", "-d", help="Test data workbook to check."),
    sheet: str = typer.Option(None, "--sheet", "-s", help="Worksheet within the workbook."),
    flow: str = typer.Option(None, "--flow", "-f", help="Flow whose requirements to check."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Check the environment, profile and data before committing to a run."""
    _setup_logging(verbose)
    from saptest.config import load_region
    from saptest.doctor import run_diagnostics

    try:
        profile = load_region(region) if region else None
    except SapTestError as exc:
        _fail(str(exc))

    report = run_diagnostics(profile, data, sheet, flow)
    typer.echo("")
    typer.echo(report.render())
    typer.echo("")

    if report.failures:
        typer.secho(f"{len(report.failures)} problem(s) must be fixed before a run.",
                    fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if report.warnings:
        typer.secho(f"Ready, with {len(report.warnings)} warning(s).", fg=typer.colors.YELLOW)
    else:
        typer.secho("Ready.", fg=typer.colors.GREEN)


@app.command()
def inspect(
    filter: str = typer.Option("", "--filter", help="Only show elements matching this text."),
    yaml_out: bool = typer.Option(False, "--yaml", help="Print as pasteable binding YAML."),
    window: str = typer.Option("wnd[0]", "--window", help="Root window to walk."),
) -> None:
    """Dump the SAP GUI elements on the current screen, to build field bindings.

    Requires SAP GUI open on the screen you want to bind.
    """
    _setup_logging(False)
    from saptest.drivers.guiscript import GuiScriptDriver
    from saptest.inspect import filter_elements, render, walk

    driver = GuiScriptDriver()
    try:
        driver._app = driver._scripting_engine()  # noqa: SLF001 - inspector needs raw access
        if driver._app.Children.Count == 0:
            _fail("No open SAP connection. Log on in SAP GUI first, then run this again.")
        session = driver._app.Children(0).Children(0)
        typer.echo(
            f"\nScreen: {session.ActiveWindow.Text}  "
            f"(transaction {session.Info.Transaction})\n"
        )
        elements = filter_elements(walk(session, window), filter)
        typer.echo(render(elements, as_yaml=yaml_out))
        typer.echo(f"\n{len(elements)} element(s).\n")
    except SapTestError as exc:
        _fail(str(exc))
    finally:
        driver.close()


# --- catalogue --------------------------------------------------------------------


@catalog_app.command("validate")
def catalog_validate(
    region: str = typer.Option("au", "--region", "-r"),
) -> None:
    """Check every catalogue rule references a real handler and a bound field."""
    from saptest.config import load_region
    from saptest.core.fields import load_fields
    from saptest.errors.catalog import load_catalog
    from saptest.errors.resolver import validate_catalog

    try:
        profile = load_region(region)
        catalog = load_catalog(*profile.catalog_paths())
        problems = validate_catalog(catalog, load_fields(*profile.binding_paths()))
    except SapTestError as exc:
        _fail(str(exc))

    if problems:
        for problem in problems:
            typer.secho(f"  X  {problem}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho(f"OK - {len(catalog.entries)} rules, all valid.", fg=typer.colors.GREEN)


@catalog_app.command("review")
def catalog_review(
    run: Path = typer.Argument(..., help="Run directory, or an unknown_messages.jsonl file."),
) -> None:
    """Turn messages captured during a run into draft catalogue entries."""
    import yaml

    from saptest.errors.capture import UnknownMessageCapture

    path = run / "unknown_messages.jsonl" if run.is_dir() else run
    if not path.exists():
        _fail(f"No captured messages at {path}")

    capture = UnknownMessageCapture(path)
    summary = capture.summarise()
    if not summary:
        typer.secho("No unrecognised messages: the catalogue covered everything.",
                    fg=typer.colors.GREEN)
        return

    typer.echo(f"\n{len(summary)} unrecognised message(s):\n")
    for group in summary:
        typer.echo(
            f"  {group['identity'][:40]:<42} x{group['count']:<4} "
            f"{', '.join(group['tcodes']) or '-'}"
        )
        if group["sample_text"]:
            typer.echo(f"      {group['sample_text'][:100]}")

    typer.echo("\n--- draft entries: review, add actions, then paste into a catalogue file ---\n")
    typer.echo(yaml.safe_dump({"entries": capture.draft_entries()}, sort_keys=False, width=100))


# --- running ----------------------------------------------------------------------


@app.command()
def run(
    region: str = typer.Option(..., "--region", "-r", help="Region profile, e.g. au."),
    flow: str = typer.Option(..., "--flow", "-f", help="Flow, e.g. p2p.pr_create."),
    data: Path = typer.Option(..., "--data", "-d", help="Test data workbook."),
    sheet: str = typer.Option(None, "--sheet", "-s", help="Worksheet (defaults to the flow's)."),
    limit: int = typer.Option(None, "--limit", "-n", help="Stop after this many cases."),
    driver: str = typer.Option(None, "--driver", help="Override the profile's driver."),
    output: Path = typer.Option(Path("runs"), "--output", "-o", help="Where runs are written."),
    business_tester: str = typer.Option(None, "--business-tester"),
    functional_tester: str = typer.Option(None, "--functional-tester"),
    change_control: str = typer.Option(None, "--change-control"),
    skip_doctor: bool = typer.Option(False, "--skip-doctor", help="Do not preflight."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Execute a flow against a system and write the reports."""
    from saptest.config import load_region
    from saptest.core.evidence import new_run_id
    from saptest.core.runner import RunConfig, Runner
    from saptest.data.workbook import load_dataset
    from saptest.doctor import run_diagnostics
    from saptest.flows import get_flow
    from saptest.reporting import write_reports

    run_id = new_run_id()
    _setup_logging(verbose, Path(output) / run_id / "run.log")

    try:
        profile = load_region(region)
        selected = get_flow(flow)
        dataset = load_dataset(data, sheet or selected.data_sheet)
    except SapTestError as exc:
        _fail(str(exc))

    if not skip_doctor:
        report = run_diagnostics(profile, data, sheet or selected.data_sheet, flow)
        if report.failures:
            typer.echo("")
            typer.echo(report.render())
            _fail("Preflight found problems. Fix them, or re-run with --skip-doctor.")

    config = RunConfig(
        profile=profile,
        flow=selected,
        data=dataset,
        run_id=run_id,
        output_root=output,
        driver=driver,
        limit=limit,
        testers={
            k: v
            for k, v in {
                "business_tester": business_tester,
                "functional_tester": functional_tester,
                "change_control": change_control,
            }.items()
            if v
        },
    )

    typer.echo(f"\nRun {run_id}: {selected.title}")
    typer.echo(f"  region {profile.region}, system {profile.connection.name}, "
               f"{len(dataset)} data row(s)\n")

    runner = Runner(config, listener=_progress)
    try:
        result = runner.run()
    except SapTestError as exc:
        _fail(str(exc))

    bundle = write_reports(
        result, profile, selected, runner.run_dir, runner.capture, config.testers
    )

    typer.echo("")
    for outcome, count in result.tally().items():
        if count:
            typer.echo(f"  {outcome.value:<14} {count}")
    typer.echo(f"\n  Reports:  {bundle.summary_html}")
    typer.echo(f"  Evidence: {runner.evidence.root}")
    if bundle.test_plans:
        typer.echo(f"  Test plans written: {len(bundle.test_plans)}")
    for problem in bundle.problems:
        typer.secho(f"  ! {problem}", fg=typer.colors.YELLOW)

    unknown = runner.capture.summarise()
    if unknown:
        typer.secho(
            f"\n  {len(unknown)} unrecognised SAP message(s). "
            f"Run: saptest catalog review {runner.run_dir}",
            fg=typer.colors.YELLOW,
        )

    raise typer.Exit(code=0 if result.outcome.value in ("PASS", "MANUAL", "SKIPPED") else 2)


def _progress(kind: str, payload: dict) -> None:
    if kind == "case_started":
        typer.echo(f"  [{payload['index']}/{payload['total']}] case {payload['case_id']}")
    elif kind == "step_finished":
        colour = {
            "PASS": typer.colors.GREEN,
            "FAIL": typer.colors.RED,
            "ERROR": typer.colors.RED,
            "BLOCKED_AUTH": typer.colors.YELLOW,
            "MANUAL": typer.colors.BLUE,
        }.get(payload["outcome"], typer.colors.WHITE)
        typer.secho(
            f"        step {payload['step_id']} {payload['name']:<26} {payload['outcome']}",
            fg=colour,
        )


@app.command()
def ui(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """Start the local web interface."""
    from saptest.ui.app import serve

    serve(host=host, port=port, open_browser=open_browser)


if __name__ == "__main__":
    app()
