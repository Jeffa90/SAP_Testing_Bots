"""Named field registry.

Flows and catalogue actions refer to fields by logical name (``item.material``).
A binding profile maps those names to the locators a driver needs. Changing a
region's screen variant or field order is then a YAML edit, not a code change --
which is what replaces the hardcoded ``field_order`` tab-traversal lists and the
``for _ in range(7): press('tab')  # 7 or 8 some users`` in the original scripts.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import yaml

from saptest.core.exceptions import ConfigError
from saptest.drivers.base import FieldRef


class FieldRegistry:
    """Logical field name -> :class:`FieldRef`."""

    def __init__(self, fields: dict[str, FieldRef] | None = None) -> None:
        self._fields: dict[str, FieldRef] = dict(fields or {})

    def __contains__(self, name: object) -> bool:
        return name in self._fields

    def __len__(self) -> int:
        return len(self._fields)

    def __iter__(self) -> Iterator[FieldRef]:
        return iter(self._fields.values())

    def get(self, name: str) -> FieldRef:
        """Resolve a name, with a helpful message naming near-misses when it is absent."""
        try:
            return self._fields[name]
        except KeyError:
            near = sorted(n for n in self._fields if name.split(".")[-1] in n)
            hint = f" Did you mean: {', '.join(near[:5])}?" if near else ""
            raise ConfigError(f"Unknown field {name!r} in binding profile.{hint}") from None

    def add(self, ref: FieldRef) -> None:
        self._fields[ref.name] = ref

    def names(self) -> set[str]:
        return set(self._fields)

    def merge(self, other: FieldRegistry) -> FieldRegistry:
        """Overlay another registry; ``other`` wins on conflict."""
        return FieldRegistry({**self._fields, **other._fields})


def load_fields(*paths: str | Path) -> FieldRegistry:
    """Load field bindings from YAML.

    Expected shape::

        fields:
          item.material:
            id: "wnd[0]/usr/tblSAPLMEGUITC_1211/ctxtMEPO1211-MATNR[3,0]"
            label: Material
            image: MaterialField.png
    """
    registry = FieldRegistry()
    for path in paths:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"Field binding file not found: {p}")
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML in field bindings {p}: {exc}") from exc

        section = raw.get("fields", raw) if isinstance(raw, dict) else None
        if not isinstance(section, dict):
            raise ConfigError(f"Field bindings {p} must contain a 'fields' mapping")

        loaded = FieldRegistry()
        for name, spec in section.items():
            if isinstance(spec, str):
                # Shorthand: a bare string is a SAP GUI element id.
                spec = {"id": spec}
            if not isinstance(spec, dict):
                raise ConfigError(f"Field {name!r} in {p} must be a mapping or an id string")
            try:
                loaded.add(FieldRef(name=name, **spec))
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"Invalid field {name!r} in {p}: {exc}") from exc
        registry = registry.merge(loaded)
    return registry
