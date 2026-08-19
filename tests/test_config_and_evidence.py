"""Profiles, field bindings, evidence capture and unknown-message review."""

from __future__ import annotations

import pytest

from saptest.config import load_region
from saptest.core.evidence import EvidenceStore, new_run_id, safe_name
from saptest.core.exceptions import ConfigError
from saptest.core.fields import load_fields
from saptest.core.status import MessageType, StatusMessage
from saptest.errors.capture import UnknownMessageCapture

# --- region profiles --------------------------------------------------------------


def test_au_profile_loads(repo_root):
    profile = load_region("au", root=repo_root)
    assert profile.region == "AU"
    assert profile.connection.client == "800"
    assert profile.defaults["tax_code"] == "P1"


def test_plant_exclusions_compare_as_strings(repo_root):
    """Excel yields '1240', YAML yields 1240. Both must match the same exclusion."""
    profile = load_region("au", root=repo_root)
    assert profile.is_excluded("plants", 1240)
    assert profile.is_excluded("plants", "1240")
    assert profile.is_excluded("plants", " 1240 ")
    assert not profile.is_excluded("plants", "1205")


def test_unknown_exclusion_kind_is_not_an_exclusion(repo_root):
    assert not load_region("au", root=repo_root).is_excluded("materials", "X")


def test_missing_region_lists_what_is_available(repo_root):
    with pytest.raises(ConfigError, match="Available regions"):
        load_region("atlantis", root=repo_root)


def test_profile_paths_resolve_against_the_project_root(repo_root):
    profile = load_region("au", root=repo_root)
    assert profile.template_path("inventory_po").is_absolute()
    assert all(p.is_absolute() for p in profile.binding_paths())


# --- field bindings ---------------------------------------------------------------


def test_shipped_bindings_load(repo_root):
    fields = load_fields(
        repo_root / "profiles" / "bindings" / "common.yaml",
        repo_root / "profiles" / "bindings" / "pr_au.yaml",
    )
    assert fields.get("system.okcode").id == "wnd[0]/tbar[0]/okcd"
    assert "item.material" in fields


def test_every_unbound_field_has_a_label_fallback(repo_root):
    """A field with neither an id nor a label could never be located."""
    fields = load_fields(
        repo_root / "profiles" / "bindings" / "common.yaml",
        repo_root / "profiles" / "bindings" / "pr_au.yaml",
    )
    orphans = [f.name for f in fields if not f.id and not f.label and not f.image]
    assert orphans == []


def test_unknown_field_error_suggests_near_matches(repo_root):
    fields = load_fields(repo_root / "profiles" / "bindings" / "pr_au.yaml")
    with pytest.raises(ConfigError, match="Did you mean"):
        fields.get("material")


def test_later_binding_files_override_earlier_ones(tmp_path):
    (tmp_path / "a.yaml").write_text("fields:\n  x: {id: 'first'}\n")
    (tmp_path / "b.yaml").write_text("fields:\n  x: {id: 'second'}\n")
    assert load_fields(tmp_path / "a.yaml", tmp_path / "b.yaml").get("x").id == "second"


# --- evidence ---------------------------------------------------------------------


def test_evidence_is_namespaced_per_case(driver, tmp_path):
    """The original wrote a flat screenshots/<name>.png and clobbered on re-runs."""
    store = EvidenceStore(tmp_path, new_run_id())
    first = store.capture(driver, "RG-1", "1", "1A")
    second = store.capture(driver, "RG-2", "1", "1A")
    assert first.path != second.path
    assert len(store.read_manifest()) == 2


def test_evidence_records_context_and_hash(driver, tmp_path):
    driver.tcode = "ME51N"
    driver.title = "Create Purchase Requisition"
    store = EvidenceStore(tmp_path, "run1")
    ref = store.capture(driver, "RG-1", "1", "1A", sap_user="BOTKCAREY")
    assert ref.tcode == "ME51N"
    assert ref.window_title == "Create Purchase Requisition"
    assert len(ref.sha256) == 64
    assert store.read_manifest()[0]["sap_user"] == "BOTKCAREY"


def test_verify_detects_a_changed_file(driver, tmp_path):
    from pathlib import Path

    store = EvidenceStore(tmp_path, "run1")
    ref = store.capture(driver, "RG-1", "1", "1A")
    assert store.verify() == []
    Path(ref.path).write_bytes(b"not the original")
    assert len(store.verify()) == 1


def test_screenshot_failure_never_breaks_a_run(driver, tmp_path):
    def explode() -> bytes:
        raise RuntimeError("display disconnected")

    driver.screenshot = explode
    store = EvidenceStore(tmp_path, "run1")
    assert store.capture(driver, "RG-1", "1", "1A") is None


def test_safe_name_strips_windows_forbidden_characters():
    assert safe_name('AU_PTP_Standard PO/1:2*') == "AU_PTP_Standard_PO_1_2"
    assert safe_name("") == "unnamed"


# --- unknown-message review -------------------------------------------------------


def test_draft_entries_generalise_embedded_numbers(tmp_path):
    """A rule drafted from one message must still match the next one."""
    capture = UnknownMessageCapture(tmp_path / "u.jsonl")
    capture.record(
        StatusMessage(MessageType.ERROR, text="Item 10 Delivery Date 08.09.2025 is in the past"),
        tcode="ME21N",
    )
    draft = capture.draft_entries()[0]
    import re

    pattern = draft["match"]["regex"]
    assert re.search(pattern, "Item 20 Delivery Date 31.12.2027 is in the past")


def test_structured_messages_draft_a_keyed_rule(tmp_path):
    capture = UnknownMessageCapture(tmp_path / "u.jsonl")
    capture.record(StatusMessage(MessageType.ERROR, "ME", "083", "whatever"), tcode="ME51N")
    draft = capture.draft_entries()[0]
    assert draft["match"] == {"message_id": "ME", "number": "083"}
    assert draft["on_exhausted"] == "fail_step", "drafts must be conservative"
    assert draft["actions"] == [], "a human decides what remediation is safe"


def test_summary_groups_and_counts(tmp_path):
    capture = UnknownMessageCapture(tmp_path / "u.jsonl")
    for _ in range(3):
        capture.record(StatusMessage(MessageType.ERROR, "ME", "083"), tcode="ME51N")
    capture.record(StatusMessage(MessageType.ERROR, "ME", "999"), tcode="ME21N")
    summary = capture.summarise()
    assert summary[0]["identity"] == "ME083"
    assert summary[0]["count"] == 3
    assert summary[0]["tcodes"] == ["ME51N"]
