"""Flow discovery, so the CLI and UI can offer what is available."""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

from saptest.core.exceptions import ConfigError

if TYPE_CHECKING:
    from saptest.flows.base import Flow

_FLOWS: dict[str, type[Flow]] = {}


def register_flow(cls: type[Flow]) -> type[Flow]:
    """Class decorator that makes a flow addressable by name."""
    if not getattr(cls, "name", ""):
        raise ConfigError(f"{cls.__name__} must set a 'name' before registration")
    _FLOWS[cls.name] = cls
    return cls


def _load_all() -> None:
    """Import every module under ``saptest.flows`` so decorators run."""
    import saptest.flows as package  # noqa: PLC0415

    for module in pkgutil.walk_packages(package.__path__, f"{package.__name__}."):
        if module.name.rsplit(".", 1)[-1] in ("base", "registry"):
            continue
        importlib.import_module(module.name)


def flow_names() -> list[str]:
    _load_all()
    return sorted(_FLOWS)


def get_flow(name: str) -> Flow:
    """Instantiate a flow by name."""
    _load_all()
    try:
        return _FLOWS[name]()
    except KeyError:
        raise ConfigError(
            f"Unknown flow {name!r}. Available: {', '.join(sorted(_FLOWS)) or 'none'}"
        ) from None


def flow_catalogue() -> list[dict[str, str]]:
    """Summary of every flow, for the UI's flow picker."""
    _load_all()
    entries = []
    for name, cls in sorted(_FLOWS.items()):
        instance = cls()
        entries.append(
            {
                "name": name,
                "title": instance.title or name,
                "template": instance.template,
                "data_sheet": instance.data_sheet or "",
                "steps": str(len(instance.steps())),
            }
        )
    return entries
