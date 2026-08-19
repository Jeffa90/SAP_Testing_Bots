"""Shared fixtures. Everything here runs without SAP GUI, Windows or a network."""

from __future__ import annotations

from pathlib import Path

import pytest

from saptest.core.fields import FieldRegistry
from saptest.drivers.base import FieldRef
from saptest.drivers.fake import FakeDriver
from saptest.errors.capture import UnknownMessageCapture
from saptest.errors.catalog import load_catalog
from saptest.errors.resolver import MessageResolver

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def driver() -> FakeDriver:
    return FakeDriver()


@pytest.fixture
def fields() -> FieldRegistry:
    registry = FieldRegistry()
    for name in (
        "item.tax_code",
        "item.net_price",
        "item.gl_account",
        "item.cost_centre",
        "item.plant",
        "item.delivery_date",
        "item.material",
        "select.requisition_number",
    ):
        registry.add(FieldRef(name=name, id=f"wnd[0]/usr/{name}"))
    return registry


@pytest.fixture
def profile_values() -> dict:
    return {
        "defaults": {
            "tax_code": "P1",
            "net_price": "15",
            "gl_account": "5514300",
            "delivery_date": "31.10.2026",
        },
        "region": "AU",
    }


@pytest.fixture
def base_catalog():
    return load_catalog(REPO_ROOT / "catalog" / "messages.yaml")


@pytest.fixture
def resolver(base_catalog, fields, profile_values, tmp_path) -> MessageResolver:
    return MessageResolver(
        catalog=base_catalog,
        fields=fields,
        profile=profile_values,
        capture=UnknownMessageCapture(tmp_path / "unknown.jsonl"),
    )
