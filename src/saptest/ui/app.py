"""The local web interface.

Served on 127.0.0.1 by default: the run data includes SAP usernames and the
evidence includes screenshots of a live system, neither of which should be exposed
to the network without a deliberate decision.

The runner must execute on this machine -- SAP GUI Scripting drives the interactive
desktop session -- so this is a local console for a local runner, not a client for a
remote one.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from saptest import __version__
from saptest.config import discover_regions, load_region
from saptest.config.loader import project_root
from saptest.core.evidence import new_run_id, safe_name
from saptest.core.exceptions import SapTestError
from saptest.core.runner import RunConfig
from saptest.data.workbook import load_dataset, sheet_names
from saptest.doctor import run_diagnostics
from saptest.flows import get_flow
from saptest.flows.registry import flow_catalogue
from saptest.ui.runs import SENTINEL, RunBusy, RunManager

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))


def create_app(output_root: Path | str = "runs", upload_root: Path | str = "uploads") -> FastAPI:
    app = FastAPI(title="saptest", version=__version__, docs_url=None, redoc_url=None)
    manager = RunManager(output_root)
    uploads = Path(upload_root)
    uploads.mkdir(parents=True, exist_ok=True)

    static_dir = HERE / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def page(request: Request, template: str, **context) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, template, {"version": __version__, "active": manager.active, **context}
        )

    # --- new run ---------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return page(
            request,
            "new_run.html",
            regions=discover_regions(),
            flows=flow_catalogue(),
            recent=manager.history(limit=5),
            workbooks=sorted(p.name for p in uploads.glob("*.xlsx")),
        )

    @app.post("/upload")
    async def upload(file: UploadFile):
        """Store a data workbook so a run can reference it by name."""
        if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
            raise HTTPException(400, "Please upload an .xlsx or .xlsm workbook")
        target = uploads / safe_name(Path(file.filename).name)
        with target.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        try:
            sheets = sheet_names(target)
        except SapTestError as exc:
            target.unlink(missing_ok=True)
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse({"name": target.name, "sheets": sheets})

    @app.get("/sheets")
    def sheets(workbook: str):
        path = uploads / safe_name(workbook)
        if not path.exists():
            raise HTTPException(404, f"No such workbook: {workbook}")
        return JSONResponse({"sheets": sheet_names(path)})

    @app.post("/validate", response_class=HTMLResponse)
    def validate(
        request: Request,
        region: str = Form(...),
        flow: str = Form(...),
        workbook: str = Form(""),
        sheet: str = Form(""),
    ):
        """Preflight: exactly what `saptest doctor` checks, rendered for the browser."""
        try:
            profile = load_region(region)
            selected = get_flow(flow)
        except SapTestError as exc:
            return page(request, "_checks.html", error=str(exc), report=None)

        path = uploads / safe_name(workbook) if workbook else None
        report = run_diagnostics(profile, path, sheet or selected.data_sheet, flow)
        return page(request, "_checks.html", report=report, error=None)

    @app.post("/run")
    def start_run(
        region: str = Form(...),
        flow: str = Form(...),
        workbook: str = Form(...),
        sheet: str = Form(""),
        driver: str = Form(""),
        limit: str = Form(""),
        business_tester: str = Form(""),
        functional_tester: str = Form(""),
        change_control: str = Form(""),
    ):
        path = uploads / safe_name(workbook)
        try:
            profile = load_region(region)
            selected = get_flow(flow)
            data = load_dataset(path, sheet or selected.data_sheet)
        except SapTestError as exc:
            raise HTTPException(400, str(exc)) from exc

        config = RunConfig(
            profile=profile,
            flow=selected,
            data=data,
            run_id=new_run_id(),
            output_root=Path(output_root),
            driver=driver or None,
            limit=int(limit) if limit.strip().isdigit() else None,
        )
        header = {
            k: v
            for k, v in {
                "business_tester": business_tester,
                "functional_tester": functional_tester,
                "change_control": change_control,
            }.items()
            if v
        }
        try:
            live = manager.start(config, header)
        except RunBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse(f"/runs/{live.run_id}", status_code=303)

    # --- live view -------------------------------------------------------------

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str):
        live = manager.get(run_id)
        directory = Path(output_root) / safe_name(run_id)
        if live is None and not directory.exists():
            raise HTTPException(404, f"No such run: {run_id}")

        summary = None
        summary_path = directory / "summary.json"
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                summary = None

        return page(
            request,
            "run_detail.html",
            run_id=run_id,
            live=live,
            summary=summary,
            artifacts=sorted(manager.artifacts(run_id)),
            streaming=live is not None and live.status in ("queued", "running"),
        )

    @app.get("/runs/{run_id}/events")
    def run_events(run_id: str):
        """Server-sent events: live step-by-step progress."""
        live = manager.get(run_id)
        if live is None:
            raise HTTPException(404, f"No such run: {run_id}")

        channel = live.subscribe()

        def stream():
            try:
                while True:
                    event = channel.get()
                    if event is SENTINEL or event.get("kind") == "stream_closed":
                        yield "event: done\ndata: {}\n\n"
                        return
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                live.unsubscribe(channel)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/runs/{run_id}/file")
    def run_file(run_id: str, name: str):
        """Serve one artefact, refusing anything outside the run's own directory."""
        directory = (Path(output_root) / safe_name(run_id)).resolve()
        target = (directory / name).resolve()
        if not target.is_file() or not target.is_relative_to(directory):
            raise HTTPException(404, f"No such file: {name}")
        inline = target.suffix in (".html", ".png", ".json", ".log", ".jsonl")
        return FileResponse(
            target,
            filename=target.name,
            content_disposition_type="inline" if inline else "attachment",
        )

    # --- history and catalogue -------------------------------------------------

    @app.get("/runs", response_class=HTMLResponse)
    def history(request: Request):
        return page(request, "history.html", runs=manager.history())

    @app.get("/catalog", response_class=HTMLResponse)
    def catalog(request: Request, run: str = ""):
        """Review messages the catalogue did not recognise, and draft rules for them."""
        from saptest.errors.capture import UnknownMessageCapture

        runs = [
            r
            for r in manager.history()
            if (Path(r["path"]) / "unknown_messages.jsonl").exists()
        ]
        selected = run or (runs[0]["run_id"] if runs else "")
        groups: list[dict] = []
        drafts = ""
        if selected:
            capture = UnknownMessageCapture(
                Path(output_root) / safe_name(selected) / "unknown_messages.jsonl"
            )
            groups = capture.summarise()
            if groups:
                import yaml

                drafts = yaml.safe_dump(
                    {"entries": capture.draft_entries()}, sort_keys=False, width=100
                )
        return page(
            request, "catalog.html", runs=runs, selected=selected, groups=groups, drafts=drafts
        )

    @app.get("/health")
    def health():
        active = manager.active
        return {
            "status": "ok",
            "version": __version__,
            "active_run": active.run_id if active else None,
        }

    return app


def serve(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True) -> None:
    """Run the interface. Called by ``saptest ui`` and by the packaged executable."""
    import uvicorn

    root = project_root()
    app = create_app(output_root=root / "runs", upload_root=root / "uploads")

    url = f"http://{host}:{port}"
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    print(f"\n  saptest {__version__}")
    print(f"  Open {url}")
    print("  Press Ctrl+C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
