"""Loading region profiles from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from saptest.config.models import RegionProfile
from saptest.core.exceptions import ConfigError


def project_root(start: Path | None = None) -> Path:
    """Find the project root by walking up for a marker.

    Works both from a source checkout and from a PyInstaller bundle, where the
    profiles ship alongside the executable rather than inside the package.
    """
    import sys

    if getattr(sys, "frozen", False):  # PyInstaller
        return Path(sys.executable).parent

    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").exists() or (parent / "profiles").is_dir():
            return parent
    return Path.cwd()


def load_region(path: str | Path, root: Path | None = None) -> RegionProfile:
    """Load one region profile.

    ``path`` may be a file path or a bare region name, in which case
    ``profiles/regions/<name>.yaml`` under the project root is used.
    """
    base = root or project_root()
    p = Path(path)
    if not p.suffix:
        p = base / "profiles" / "regions" / f"{p.name.lower()}.yaml"
    if not p.is_absolute():
        p = base / p

    if not p.exists():
        available = ", ".join(sorted(r.stem for r in _region_files(base))) or "none found"
        raise ConfigError(f"Region profile not found: {p}. Available regions: {available}")

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in region profile {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Region profile {p} must be a mapping")

    raw["root"] = base
    try:
        return RegionProfile.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid region profile {p}:\n{exc}") from exc


def _region_files(root: Path) -> list[Path]:
    directory = root / "profiles" / "regions"
    return sorted(directory.glob("*.yaml")) if directory.is_dir() else []


def discover_regions(root: Path | None = None) -> list[str]:
    """Region names available under the project root, for the UI's region picker."""
    base = root or project_root()
    return [p.stem.upper() for p in _region_files(base)]
