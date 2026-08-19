"""Keystroke and image-matching fallback driver (planned, not yet implemented).

For landscapes where Basis has not enabled ``sapgui/user_scripting``. It executes
the same flow definitions as the scripting driver by resolving :class:`FieldRef`
through its ``image`` and ``tab_offset`` locators instead of ``id``, which is why
flows need no changes to run on it.

This is the approach the original scripts used throughout, and it carries their
weaknesses: sensitivity to screen resolution, DPI scaling, SAP GUI theme and
version, and the requirement that the SAP window stay focused for the whole run.
It is a compatibility path, not a peer of the scripting driver.

Scheduled for phase 7. Until then, requesting it raises a clear error rather than
failing obscurely partway through a run.
"""

from __future__ import annotations

from saptest.core.exceptions import DriverUnavailable


class LegacyUiDriver:
    """Placeholder that fails loudly at construction time."""

    name = "legacy_ui"
    structured_messages = False

    def __init__(self, **_: object) -> None:
        raise DriverUnavailable(
            "The legacy_ui fallback driver is not implemented yet (planned for phase 7). "
            "Enable SAP GUI Scripting for this landscape and use the guiscript driver, "
            "or set fallback_driver: null in the region profile to fail fast instead."
        )
