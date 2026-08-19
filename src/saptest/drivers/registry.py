"""Driver selection, including automatic fallback when scripting is unavailable."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from saptest.core.exceptions import ConfigError, DriverUnavailable, ScriptingDisabled
from saptest.drivers.base import Driver

log = logging.getLogger(__name__)

_FACTORIES: dict[str, Callable[..., Driver]] = {}


def register(name: str, factory: Callable[..., Driver]) -> None:
    _FACTORIES[name] = factory


def _guiscript(**kwargs: Any) -> Driver:
    from saptest.drivers.guiscript import GuiScriptDriver  # noqa: PLC0415

    return GuiScriptDriver(**kwargs)


def _legacy_ui(**kwargs: Any) -> Driver:
    from saptest.drivers.legacy_ui import LegacyUiDriver  # noqa: PLC0415

    return LegacyUiDriver(**kwargs)


def _fake(**kwargs: Any) -> Driver:
    from saptest.drivers.fake import FakeDriver  # noqa: PLC0415

    return FakeDriver(**kwargs)


def _demo(**kwargs: Any) -> Driver:
    from saptest.drivers.demo import DemoDriver  # noqa: PLC0415

    return DemoDriver(**kwargs)


register("guiscript", _guiscript)
register("legacy_ui", _legacy_ui)
register("fake", _fake)
register("demo", _demo)


def driver_names() -> list[str]:
    return sorted(_FACTORIES)


def make_driver(name: str, **kwargs: Any) -> Driver:
    """Build a driver by name."""
    try:
        factory = _FACTORIES[name]
    except KeyError:
        raise ConfigError(
            f"Unknown driver {name!r}. Available: {', '.join(driver_names())}"
        ) from None
    return factory(**kwargs)


def connect_with_fallback(
    connection: str,
    primary: str,
    fallback: str | None = None,
    **kwargs: Any,
) -> Driver:
    """Connect with ``primary``, falling back when scripting is unavailable.

    Only falls back for conditions the fallback driver can actually work around --
    scripting disabled on the client or the application server. A missing connection
    entry or a wrong password would fail identically on either driver, so those
    propagate rather than being retried slowly.
    """
    driver = make_driver(primary, **kwargs)
    try:
        driver.connect(connection)
        return driver
    except (ScriptingDisabled, DriverUnavailable) as exc:
        driver.close()
        if not fallback:
            raise
        log.warning(
            "Driver %r unavailable (%s); falling back to %r. Expect slower, "
            "less reliable execution.",
            primary,
            exc,
            fallback,
        )
        alternate = make_driver(fallback, **kwargs)
        alternate.connect(connection)
        return alternate
