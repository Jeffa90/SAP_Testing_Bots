"""The remediation loop: what happens when SAP pushes back."""

from __future__ import annotations

import pytest

from saptest.core.outcomes import Outcome
from saptest.core.status import MessageType, StatusMessage
from saptest.errors.catalog import Catalog, CatalogEntry, ErrorPolicy, Match
from saptest.errors.resolver import MessageResolver, validate_catalog


def err(text: str = "", msg_id: str = "", number: str = "") -> StatusMessage:
    return StatusMessage(MessageType.ERROR, msg_id, number, text)


# --- the happy path ---------------------------------------------------------------


def test_clean_status_bar_resolves_with_no_work(driver, resolver):
    result = resolver.resolve(driver)
    assert result.resolved
    assert result.entry is None
    assert result.attempts == 0


def test_success_message_is_not_a_problem(driver, resolver):
    driver.queue_success("Purchase requisition 1100712093 created")
    driver.press_key(0)
    assert resolver.resolve(driver).resolved


# --- remediation ------------------------------------------------------------------


def test_known_error_is_remediated_from_the_region_profile(driver, resolver):
    """The tax code comes from the profile, not from code."""
    driver.queue_message(err("Enter Tax code"))
    driver.press_key(0)

    result = resolver.resolve(driver, tcode="ME51N")

    assert result.resolved
    assert result.remediated
    assert result.entry.name == "tax_code_required"
    assert ("set_field", "item.tax_code", "P1") in driver.calls


def test_remediation_retries_up_to_max_attempts_then_gives_up(driver, resolver):
    """Two attempts are configured, so the message is seen, retried once, then failed."""
    for _ in range(4):
        driver.queue_message(err("Enter Tax code"))
    driver.press_key(0)

    result = resolver.resolve(driver, tcode="ME51N")

    assert not result.resolved
    assert result.outcome is Outcome.FAIL
    assert result.policy is ErrorPolicy.FAIL_STEP
    assert result.attempts <= 2


def test_a_different_error_after_remediation_is_looked_up_afresh(driver, resolver):
    driver.queue_message(err("Enter Tax code"))
    driver.press_key(0)
    driver.queue_message(err("Net price must be greater than 0"))

    result = resolver.resolve(driver, tcode="ME51N")

    assert result.resolved
    handlers = [c for c in driver.calls if c[0] == "set_field"]
    assert ("set_field", "item.tax_code", "P1") in handlers
    assert ("set_field", "item.net_price", "15") in handlers


def test_the_delivery_date_rule_survives_the_date_passing(driver, resolver):
    """The original scripts keyed on a literal date and silently stopped matching."""
    driver.queue_message(err("Item 20 Delivery Date 31.01.2027 is in the past"))
    driver.press_key(0)

    result = resolver.resolve(driver, tcode="ME51N")

    assert result.entry.name == "delivery_date_in_past"
    assert ("set_field", "item.delivery_date", "31.10.2026") in driver.calls


# --- authorisation ----------------------------------------------------------------


def test_authorisation_failure_is_blocked_not_failed(driver, resolver):
    driver.queue_message(err("You are not authorized to use transaction ME51N"))
    driver.press_key(0)

    result = resolver.resolve(driver, tcode="ME51N")

    assert not result.resolved
    assert result.outcome is Outcome.BLOCKED_AUTH
    assert result.policy is ErrorPolicy.ABORT_CASE
    assert not result.actions_run, "an authorisation gap must never be remediated away"


# --- unknown messages -------------------------------------------------------------


def test_unknown_error_is_captured_for_catalogue_review(driver, resolver, tmp_path):
    driver.queue_message(err("Some message nobody has catalogued", "ZZ", "042"))
    driver.press_key(0)

    result = resolver.resolve(
        driver, tcode="ME51N", context={"case_id": "RG-24", "step_id": "1"}
    )

    assert not result.resolved
    assert result.unknown
    assert result.outcome is Outcome.FAIL

    captured = resolver.capture.read()
    assert len(captured) == 1
    assert captured[0]["key"] == "ZZ042"
    assert captured[0]["case_id"] == "RG-24"
    assert captured[0]["tcode"] == "ME51N"


def test_unknown_informational_message_is_ignored(driver, resolver):
    """Only problems are escalated; a stray info message is not a finding."""
    driver.queue_message(StatusMessage(MessageType.INFO, text="Please note something"))
    driver.press_key(0)

    result = resolver.resolve(driver)

    assert result.resolved
    assert not result.unknown
    assert resolver.capture.read() == []


# --- loop protection --------------------------------------------------------------


def test_a_rule_that_cannot_fix_its_own_error_stops_rather_than_looping(driver, fields):
    catalog = Catalog(
        entries=[
            CatalogEntry(
                name="futile",
                match=Match(message_id="ZZ", number="001"),
                actions=[{"handler": "press_key", "key": "ENTER"}],
                max_attempts=10,
            )
        ]
    )
    resolver = MessageResolver(catalog, fields, budget=6)
    for _ in range(50):
        driver.queue_error("ZZ", "001", "will not go away")
    driver.press_key(0)

    result = resolver.resolve(driver)

    assert not result.resolved
    assert result.attempts <= 6


def test_two_rules_ping_ponging_are_bounded_by_the_budget(driver, fields):
    """Rule A's fix triggers B, B's fix triggers A. The budget must stop it."""
    catalog = Catalog(
        entries=[
            CatalogEntry(
                name="a",
                match=Match(message_id="ZZ", number="001"),
                actions=[{"handler": "press_key", "key": "ENTER"}],
            ),
            CatalogEntry(
                name="b",
                match=Match(message_id="ZZ", number="002"),
                actions=[{"handler": "press_key", "key": "ENTER"}],
            ),
        ]
    )
    resolver = MessageResolver(catalog, fields, budget=4)
    for _ in range(20):
        driver.queue_error("ZZ", "001")
        driver.queue_error("ZZ", "002")
    driver.press_key(0)

    result = resolver.resolve(driver)

    assert not result.resolved
    assert result.attempts <= 4


def test_a_broken_rule_does_not_crash_the_run(driver, fields):
    """A rule referencing a field that is not bound fails the step, not the process."""
    catalog = Catalog(
        entries=[
            CatalogEntry(
                name="broken",
                match=Match(message_id="ZZ", number="003"),
                actions=[{"handler": "set_field", "field": "does.not.exist", "value": "x"}],
            )
        ]
    )
    resolver = MessageResolver(catalog, fields)
    driver.queue_error("ZZ", "003", "boom")
    driver.press_key(0)

    result = resolver.resolve(driver)

    assert not result.resolved
    assert "does not exist" in result.detail or "Unknown field" in result.detail


# --- benign messages --------------------------------------------------------------


def test_continue_policy_means_the_step_still_passes(driver, resolver):
    driver.queue_message(
        StatusMessage(MessageType.WARNING, text="Document is display-only")
    )
    driver.press_key(0)

    result = resolver.resolve(driver, tcode="ME53N")

    assert result.resolved
    assert result.outcome is Outcome.PASS


# --- catalogue validation ---------------------------------------------------------


def test_shipped_catalogue_is_valid(base_catalog, fields):
    assert validate_catalog(base_catalog, fields) == []


def test_validation_catches_an_unknown_handler(fields):
    catalog = Catalog(
        entries=[
            CatalogEntry(
                name="typo",
                match=Match(regex="x"),
                actions=[{"handler": "set_feild", "field": "item.plant", "value": "1"}],
            )
        ]
    )
    problems = validate_catalog(catalog, fields)
    assert len(problems) == 1
    assert "set_feild" in problems[0]


def test_validation_catches_an_unbound_field(fields):
    catalog = Catalog(
        entries=[
            CatalogEntry(
                name="bad_field",
                match=Match(regex="x"),
                actions=[{"handler": "set_field", "field": "item.nonexistent", "value": "1"}],
            )
        ]
    )
    assert any("item.nonexistent" in p for p in validate_catalog(catalog, fields))


@pytest.mark.parametrize("spec", [{"message_id": "ME"}, {"number": "083"}, {}])
def test_match_requires_a_usable_locator(spec):
    with pytest.raises(ValueError):
        Match(**spec)
