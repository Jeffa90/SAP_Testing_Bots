"""Remediation handlers and the argument resolver used by catalogue actions.

The original scripts defined ``enter_tax``, ``enter_price``, ``enter_gl`` and
``delivery_date`` separately in each file, with different bodies and hardcoded
values (``P1``, ``15``, ``5514300``, ``31.10.2025``). Here there is one small set of
generic handlers, and the values come from the region profile -- so a different
region supplies different defaults without new code.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from saptest.core.exceptions import ConfigError
from saptest.core.fields import FieldRegistry
from saptest.core.status import StatusMessage
from saptest.drivers.base import Driver, VKey

log = logging.getLogger(__name__)

Handler = Callable[..., None]
_REGISTRY: dict[str, Handler] = {}


def handler(name: str) -> Callable[[Handler], Handler]:
    """Register a remediation handler under a catalogue-visible name."""

    def decorate(fn: Handler) -> Handler:
        if name in _REGISTRY:
            raise ConfigError(f"Duplicate handler registration: {name!r}")
        _REGISTRY[name] = fn
        return fn

    return decorate


def get_handler(name: str) -> Handler:
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise ConfigError(f"Unknown handler {name!r}. Available: {known}") from None


def handler_names() -> set[str]:
    return set(_REGISTRY)


@dataclass(slots=True)
class HandlerContext:
    """Everything a handler may touch."""

    driver: Driver
    fields: FieldRegistry
    message: StatusMessage
    #: Region profile values, e.g. ``{"defaults": {"tax_code": "P1"}}``.
    profile: dict[str, Any] = field(default_factory=dict)
    #: Values for the case being executed, e.g. ``{"plant": "1205"}``.
    case: dict[str, Any] = field(default_factory=dict)

    def resolve(self, value: Any) -> Any:
        """Expand ``@`` references against the profile, case data and message.

        ``@profile.defaults.tax_code``  -> region profile lookup
        ``@case.plant``                 -> current data row
        ``@msg.param.0``                -> SAP message parameter by index
        ``@msg.text``                   -> rendered message text

        Anything else is returned unchanged, so literals stay literal.
        """
        if not isinstance(value, str) or not value.startswith("@"):
            return value

        path = value[1:].split(".")
        root, rest = path[0], path[1:]

        if root == "msg":
            if rest[:1] == ["param"]:
                try:
                    return self.message.params[int(rest[1])]
                except (IndexError, ValueError):
                    raise ConfigError(
                        f"{value!r}: message has {len(self.message.params)} parameter(s)"
                    ) from None
            if rest == ["text"]:
                return self.message.text
            if rest == ["key"]:
                return self.message.key
            raise ConfigError(f"Unknown message reference {value!r}")

        source = {"profile": self.profile, "case": self.case}.get(root)
        if source is None:
            raise ConfigError(f"Unknown reference root {root!r} in {value!r}")

        cursor: Any = source
        for part in rest:
            if not isinstance(cursor, dict) or part not in cursor:
                raise ConfigError(f"{value!r} does not resolve; missing {part!r}")
            cursor = cursor[part]
        return cursor


def _vkey(value: Any) -> int:
    """Accept ``ENTER``, ``"11"`` or ``11`` as a virtual key."""
    if isinstance(value, int):
        return value
    text = str(value).strip().upper()
    if text.isdigit():
        return int(text)
    try:
        return int(VKey[text])
    except KeyError:
        raise ConfigError(
            f"Unknown key {value!r}. Use a VKey name ({', '.join(k.name for k in VKey)}) "
            "or a numeric code."
        ) from None


# --- handlers ---------------------------------------------------------------------


@handler("none")
def _none(ctx: HandlerContext) -> None:
    """No remediation. For entries that only classify a message (e.g. authorisation)."""


@handler("retry")
def _retry(ctx: HandlerContext) -> None:
    """Re-submit the current screen without changing anything."""
    ctx.driver.press_key(VKey.ENTER)


@handler("set_field")
def _set_field(ctx: HandlerContext, field: str, value: Any, press: Any = "ENTER") -> None:
    """Fill a field and submit. The workhorse: covers tax code, price, GL, date."""
    ref = ctx.fields.get(field)
    resolved = ctx.resolve(value)
    if resolved is None:
        raise ConfigError(f"set_field on {field!r} resolved to null")
    ctx.driver.set_field(ref, str(resolved))
    if press is not None:
        ctx.driver.press_key(_vkey(press))


@handler("set_checkbox")
def _set_checkbox(ctx: HandlerContext, field: str, checked: Any = True) -> None:
    ref = ctx.fields.get(field)
    value = ctx.resolve(checked)
    ctx.driver.set_checkbox(ref, bool(value) and str(value).lower() not in ("false", "0", ""))


@handler("press_key")
def _press_key(ctx: HandlerContext, key: Any) -> None:
    ctx.driver.press_key(_vkey(ctx.resolve(key)))


@handler("press_button")
def _press_button(ctx: HandlerContext, field: str) -> None:
    ctx.driver.press_button(ctx.fields.get(field))


@handler("select_tab")
def _select_tab(ctx: HandlerContext, field: str) -> None:
    ctx.driver.select_tab(ctx.fields.get(field))


@handler("dismiss_modal")
def _dismiss_modal(ctx: HandlerContext, accept: Any = True) -> None:
    """Close a popup. Replaces the background thread that blind-pressed Enter."""
    if ctx.driver.has_modal():
        ctx.driver.dismiss_modal(accept=bool(accept))


@handler("go_home")
def _go_home(ctx: HandlerContext) -> None:
    ctx.driver.go_home()


def run_action(ctx: HandlerContext, name: str, args: dict[str, Any]) -> None:
    """Invoke a handler by name, resolving nothing here -- handlers resolve their own args."""
    fn = get_handler(name)
    log.debug("remediation %s(%s)", name, args)
    fn(ctx, **args)
