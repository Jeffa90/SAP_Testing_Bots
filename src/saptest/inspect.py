"""Screen inspector: dump the SAP GUI element tree of whatever is on screen.

This is what turns binding creation from guesswork into a two-minute job. SAP GUI
element ids embed dynpro and subscreen numbers that vary by release and
configuration, so they cannot be shipped -- they have to be read off the system
being tested.

Run it with SAP GUI open on the screen you want to bind::

    saptest inspect --filter MATNR

and paste the ids it prints into the region's binding file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: Element types worth binding. Containers are walked but not listed.
INTERESTING = {
    "GuiTextField",
    "GuiCTextField",
    "GuiPasswordField",
    "GuiComboBox",
    "GuiCheckBox",
    "GuiRadioButton",
    "GuiButton",
    "GuiTab",
    "GuiTextEdit",
    "GuiTableControl",
    "GuiShell",
}


@dataclass(slots=True)
class Element:
    """One element found on screen."""

    id: str
    type: str
    name: str = ""
    text: str = ""
    tooltip: str = ""
    changeable: bool = True

    @property
    def suggested_binding(self) -> str:
        """A ready-to-paste YAML fragment for this element."""
        label = self.tooltip or self.text or self.name
        lines = [f'  {_suggest_name(self)}:', f'    id: "{self.id}"']
        if label:
            lines.append(f'    label: "{label}"')
        return "\n".join(lines)


def _suggest_name(element: Element) -> str:
    """Guess a logical field name from the SAP structure-field name.

    ``MEPO1211-MATNR`` -> ``item.matnr``, which the operator renames to
    ``item.material``. A starting point, not an answer.
    """
    source = element.name or element.id.rsplit("/", 1)[-1]
    field = source.split("-")[-1].split("[")[0].strip().lower()
    prefix = "item" if any(k in element.id for k in ("tbl", "1211", "ITEM")) else "field"
    return f"{prefix}.{field}" if field else "field.unnamed"


def walk(session, root_id: str = "wnd[0]", max_depth: int = 12) -> list[Element]:
    """Walk the element tree of a live SAP GUI session.

    Guarded throughout: SAP GUI raises on some containers depending on the control,
    and an inspector that dies partway through is useless.
    """
    found: list[Element] = []
    seen: set[str] = set()

    def visit(node, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            element_id = str(node.Id)
        except Exception:  # noqa: BLE001
            return
        if element_id in seen:
            return
        seen.add(element_id)

        kind = _attr(node, "Type")
        if kind in INTERESTING:
            found.append(
                Element(
                    id=element_id,
                    type=kind,
                    name=_attr(node, "Name"),
                    text=_attr(node, "Text"),
                    tooltip=_attr(node, "Tooltip"),
                    changeable=_bool(node, "Changeable", default=True),
                )
            )

        try:
            children = node.Children
            count = children.Count
        except Exception:  # noqa: BLE001 - leaf nodes have no Children
            return
        for index in range(count):
            try:
                visit(children(index), depth + 1)
            except Exception as exc:  # noqa: BLE001
                log.debug("Could not visit child %d of %s: %s", index, element_id, exc)

    try:
        visit(session.findById(root_id), 0)
    except Exception as exc:  # noqa: BLE001
        log.error("Could not walk %s: %s", root_id, exc)
    return found


def _attr(node, name: str) -> str:  # noqa: ANN001
    try:
        value = getattr(node, name, "")
        return "" if value is None else str(value).strip()
    except Exception:  # noqa: BLE001
        return ""


def _bool(node, name: str, default: bool = True) -> bool:  # noqa: ANN001
    try:
        value = getattr(node, name, default)
        return bool(default if value is None else value)
    except Exception:  # noqa: BLE001
        return default


def filter_elements(elements: list[Element], pattern: str) -> list[Element]:
    """Case-insensitive substring match across id, name, text and tooltip."""
    if not pattern:
        return elements
    needle = pattern.lower()
    return [
        e
        for e in elements
        if needle in e.id.lower()
        or needle in e.name.lower()
        or needle in e.text.lower()
        or needle in e.tooltip.lower()
    ]


def render(elements: list[Element], as_yaml: bool = False) -> str:
    """Format the findings for the terminal, or as pasteable binding YAML."""
    if not elements:
        return "No matching elements found on the current screen."
    if as_yaml:
        return "fields:\n" + "\n".join(e.suggested_binding for e in elements)

    width = max(len(e.type) for e in elements)
    lines = []
    for element in elements:
        label = element.tooltip or element.text or element.name
        flag = "" if element.changeable else "  [read-only]"
        lines.append(f"{element.type:<{width}}  {element.id}")
        if label:
            lines.append(f"{'':<{width}}    {label}{flag}")
    return "\n".join(lines)
