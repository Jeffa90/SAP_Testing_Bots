"""The local web interface: pages, uploads, run lifecycle and access control."""

from __future__ import annotations

import shutil
import time

import pytest
from fastapi.testclient import TestClient

from saptest.ui.app import create_app


@pytest.fixture
def client(tmp_path, repo_root, monkeypatch):
    # The app resolves profiles through the project root, so run from the repo.
    monkeypatch.chdir(repo_root)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    shutil.copy(repo_root / "tests" / "fixtures" / "pr_test_data.xlsx", uploads / "data.xlsx")
    return TestClient(create_app(output_root=tmp_path / "runs", upload_root=uploads))


def wait_for(client, run_id, seconds=20):
    """Poll until the background run finishes."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if client.get("/health").json()["active_run"] is None:
            return
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not finish within {seconds}s")


# --- pages --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/runs", "/catalog", "/health"])
def test_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_the_new_run_page_offers_the_available_regions_and_flows(client):
    body = client.get("/").text
    assert "AU" in body
    assert "p2p.pr_create" in body
    assert "data.xlsx" in body


def test_an_unknown_run_is_a_404(client):
    assert client.get("/runs/run_does_not_exist").status_code == 404


# --- uploads ------------------------------------------------------------------------


def test_uploading_a_workbook_returns_its_sheets(client, repo_root):
    source = repo_root / "tests" / "fixtures" / "pr_test_data.xlsx"
    with source.open("rb") as fh:
        response = client.post("/upload", files={"file": ("uploaded.xlsx", fh)})
    assert response.status_code == 200
    assert response.json()["sheets"] == ["PR"]


def test_a_non_workbook_upload_is_rejected(client):
    response = client.post("/upload", files={"file": ("notes.txt", b"hello")})
    assert response.status_code == 400


def test_sheets_can_be_listed_for_a_stored_workbook(client):
    assert client.get("/sheets", params={"workbook": "data.xlsx"}).json()["sheets"] == ["PR"]


def test_listing_sheets_of_an_unknown_workbook_is_a_404(client):
    assert client.get("/sheets", params={"workbook": "nope.xlsx"}).status_code == 404


# --- preflight ----------------------------------------------------------------------


def test_preflight_renders_the_same_checks_the_cli_runs(client):
    response = client.post(
        "/validate",
        data={"region": "au", "flow": "p2p.pr_create", "workbook": "data.xlsx", "sheet": "PR"},
    )
    assert response.status_code == 200
    assert "region profile" in response.text
    assert "error catalogue" in response.text


def test_preflight_reports_a_bad_region_without_crashing(client):
    response = client.post("/validate", data={"region": "atlantis", "flow": "p2p.pr_create"})
    assert response.status_code == 200
    assert "Available regions" in response.text


# --- the run lifecycle --------------------------------------------------------------


def test_a_run_executes_and_produces_reports(client, tmp_path):
    response = client.post(
        "/run",
        data={
            "region": "au", "flow": "p2p.pr_create", "workbook": "data.xlsx",
            "sheet": "PR", "driver": "demo", "business_tester": "J. Judd",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    run_id = response.headers["location"].rsplit("/", 1)[-1]
    wait_for(client, run_id)

    detail = client.get(f"/runs/{run_id}")
    assert detail.status_code == 200
    assert "summary.html" in detail.text

    directory = tmp_path / "runs" / run_id
    assert (directory / "summary.html").exists()
    assert (directory / "summary.xlsx").exists()
    assert list((directory / "test_plans").glob("*.xlsx"))


def test_a_finished_run_appears_in_history(client):
    response = client.post(
        "/run",
        data={"region": "au", "flow": "p2p.pr_create", "workbook": "data.xlsx",
              "sheet": "PR", "driver": "demo", "limit": "1"},
        follow_redirects=False,
    )
    run_id = response.headers["location"].rsplit("/", 1)[-1]
    wait_for(client, run_id)
    assert run_id in client.get("/runs").text


def test_run_files_can_be_downloaded(client):
    response = client.post(
        "/run",
        data={"region": "au", "flow": "p2p.pr_create", "workbook": "data.xlsx",
              "sheet": "PR", "driver": "demo", "limit": "1"},
        follow_redirects=False,
    )
    run_id = response.headers["location"].rsplit("/", 1)[-1]
    wait_for(client, run_id)

    summary = client.get(f"/runs/{run_id}/file", params={"name": "summary.html"})
    assert summary.status_code == 200
    assert "<!doctype html>" in summary.text.lower()


def test_a_bad_workbook_is_rejected_before_a_run_starts(client):
    response = client.post(
        "/run",
        data={"region": "au", "flow": "p2p.pr_create", "workbook": "missing.xlsx", "sheet": "PR"},
        follow_redirects=False,
    )
    assert response.status_code == 400


# --- concurrency --------------------------------------------------------------------


def test_only_one_run_may_use_sap_gui_at_a_time(client, tmp_path, repo_root):
    """Two concurrent runs would fight over the same desktop SAP GUI session."""
    from saptest.config import load_region
    from saptest.core.runner import RunConfig
    from saptest.data.workbook import load_dataset
    from saptest.flows import get_flow
    from saptest.ui.runs import RunBusy, RunManager

    manager = RunManager(tmp_path / "runs2")

    def config(run_id):
        return RunConfig(
            profile=load_region("au", root=repo_root),
            flow=get_flow("p2p.pr_create"),
            data=load_dataset(repo_root / "tests" / "fixtures" / "pr_test_data.xlsx", "PR"),
            run_id=run_id,
            output_root=tmp_path / "runs2",
            driver="demo",
        )

    manager.start(config("run_first"))
    with pytest.raises(RunBusy, match="one run"):
        manager.start(config("run_second"))


# --- access control -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["../../../etc/passwd", "../summary.html", "/etc/passwd", "..\\..\\secrets.txt"],
)
def test_files_outside_the_run_directory_are_refused(client, name):
    """The download endpoint must not become a file browser for the whole disk."""
    response = client.post(
        "/run",
        data={"region": "au", "flow": "p2p.pr_create", "workbook": "data.xlsx",
              "sheet": "PR", "driver": "demo", "limit": "1"},
        follow_redirects=False,
    )
    run_id = response.headers["location"].rsplit("/", 1)[-1]
    wait_for(client, run_id)

    assert client.get(f"/runs/{run_id}/file", params={"name": name}).status_code == 404


# --- live events --------------------------------------------------------------------


def test_the_event_stream_replays_what_a_late_subscriber_missed(client, tmp_path, repo_root):
    from saptest.config import load_region
    from saptest.core.runner import RunConfig
    from saptest.data.workbook import load_dataset
    from saptest.flows import get_flow
    from saptest.ui.runs import RunManager

    manager = RunManager(tmp_path / "runs3")
    live = manager.start(
        RunConfig(
            profile=load_region("au", root=repo_root),
            flow=get_flow("p2p.pr_create"),
            data=load_dataset(repo_root / "tests" / "fixtures" / "pr_test_data.xlsx", "PR"),
            run_id="run_events",
            output_root=tmp_path / "runs3",
            driver="demo",
            limit=1,
        )
    )
    deadline = time.monotonic() + 20
    while live.status not in ("finished", "failed") and time.monotonic() < deadline:
        time.sleep(0.05)

    # Subscribing after the run ended must still deliver the whole history.
    channel = live.subscribe()
    replayed = []
    while not channel.empty():
        replayed.append(channel.get_nowait())

    kinds = [e["kind"] for e in replayed]
    assert "run_started" in kinds
    assert "step_finished" in kinds
    assert kinds[-1] == "stream_closed"
