"""The driver contract, and the locator model that makes flows driver-agnostic.

A flow names a field once. Each driver resolves that name its own way: the
scripting driver by SAP GUI element id, the fallback driver by on-screen image or
tab offset. This is what lets a landscape with ``sapgui/user_scripting`` disabled
run the same test definitions, at the cost of robustness rather than rewriting.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntEnum, StrEnum


class VKey(IntEnum):
    """SAP GUI virtual keys for ``sendVKey``.

    The numbering is F-key based: 1-12 are F1-F12, then Shift, Ctrl and Ctrl+Shift
    banks of twelve. Named members cover the functions the P2P flows actually use;
    any raw code may still be passed as an ``int``.
    """

    ENTER = 0
    HELP = 1  # F1
    CHECK = 2  # F2
    BACK = 3  # F3
    POSSIBLE_ENTRIES = 4  # F4
    EXECUTE = 8  # F8
    PRINT = 9  # F9
    SAVE = 11  # F11 -- same function as Ctrl+S
    CANCEL = 12  # F12
    SHIFT_F1 = 13
    SHIFT_F2 = 14
    EXIT = 15  # Shift+F3
    SHIFT_F4 = 16
    OTHER_DOCUMENT = 17  # Shift+F5 -- "Select Document" in ME5xN / ME2xN
    SHIFT_F6 = 18
    SHIFT_F7 = 19
    SHIFT_F8 = 20
    SHIFT_F12 = 24
    CTRL_F1 = 25
    CTRL_F2 = 26
    CTRL_F3 = 27  # header/overview toggle in ME21N / ME51N
    CTRL_F4 = 28
    CTRL_F11 = 35
    CTRL_F12 = 36


class LogonMode(StrEnum):
    """How a connection authenticates."""

    #: Enter user and password on the SAP logon screen.
    EXPLICIT = "explicit"
    #: Single Sign-On; the connection resolves straight to SAP Easy Access.
    SSO = "sso"
    #: Decide at runtime from whether a password is present and what SAP presents.
    AUTO = "auto"


@dataclass(frozen=True, slots=True)
class Credentials:
    """SAP logon credentials for one test user.

    ``password`` is optional: a blank password means this row logs on via SSO, as
    the Windows user. ``user`` may still be recorded for reporting in that case,
    but the driver reports the user SAP actually authenticated.
    """

    user: str = ""
    password: str = ""
    client: str = ""
    language: str = ""

    @property
    def is_sso(self) -> bool:
        return not self.password

    def __repr__(self) -> str:
        """Never render the password -- these objects reach logs and tracebacks."""
        return f"Credentials(user={self.user!r}, sso={self.is_sso})"


@dataclass(frozen=True, slots=True)
class FieldRef:
    """A field, button or tab, described in every way a driver might locate it.

    At least one locator must be set. Drivers use what they support and ignore the
    rest, so a single definition serves both back-ends::

        FieldRef(
            name="item.material",
            id="wnd[0]/usr/tblSAPLMEGUITC_1211/ctxtMEPO1211-MATNR[3,0]",
            label="Material",
            image="MaterialField.png",
        )
    """

    #: Stable logical name used by flows, bindings and error-catalogue actions.
    name: str
    #: SAP GUI Scripting element id (guiscript driver).
    id: str | None = None
    #: Visible label text, for label-anchored lookup (both drivers).
    label: str | None = None
    #: Image asset filename under the region's image directory (legacy_ui driver).
    image: str | None = None
    #: Tab presses from the screen's default focus (legacy_ui driver, last resort).
    tab_offset: int | None = None
    #: Human description for logs and error messages.
    description: str = ""

    def __post_init__(self) -> None:
        if not (self.id or self.label or self.image or self.tab_offset is not None):
            raise ValueError(
                f"FieldRef {self.name!r} has no locator: set at least one of "
                "id, label, image or tab_offset"
            )

    def __str__(self) -> str:
        return self.description or self.name


class Driver(ABC):
    """What every back-end must provide.

    Implementations are stateful: :meth:`connect` then :meth:`login` establish a
    session that the remaining calls operate on. All calls are expected to be
    synchronous -- returning only once SAP has completed the round trip -- which is
    what removes the need for the window-title polling the original scripts relied on.
    """

    #: Short identifier used in profiles and reports.
    name: str = "abstract"
    #: Whether this driver can read structured message class/number from the status bar.
    structured_messages: bool = False

    # --- lifecycle ---------------------------------------------------------------

    @abstractmethod
    def connect(self, connection: str) -> None:
        """Attach to (or open) the named SAP Logon connection entry."""

    @abstractmethod
    def login(self, creds: Credentials | None, mode: LogonMode = LogonMode.AUTO) -> str:
        """Authenticate and return the SAP user the session is actually running as.

        ``creds`` of ``None``, or credentials with a blank password, mean SSO.
        Implementations must verify what SAP actually presented rather than trusting
        ``mode``, and raise :class:`~saptest.core.exceptions.LogonModeMismatch` on
        disagreement.
        """

    @abstractmethod
    def logoff(self) -> None:
        """End the SAP session cleanly, returning to SAP Logon."""

    @abstractmethod
    def close(self) -> None:
        """Release driver resources. Must be safe to call more than once."""

    # --- navigation --------------------------------------------------------------

    @abstractmethod
    def start_transaction(self, tcode: str) -> None:
        """Navigate to a transaction from wherever the session currently is."""

    @abstractmethod
    def go_home(self) -> None:
        """Return to SAP Easy Access, discarding any in-progress document."""

    # --- interaction -------------------------------------------------------------

    @abstractmethod
    def set_field(self, ref: FieldRef, value: str) -> None: ...

    @abstractmethod
    def read_field(self, ref: FieldRef) -> str: ...

    @abstractmethod
    def press_key(self, key: VKey | int) -> None: ...

    @abstractmethod
    def press_button(self, ref: FieldRef) -> None: ...

    @abstractmethod
    def select_tab(self, ref: FieldRef) -> None: ...

    @abstractmethod
    def set_checkbox(self, ref: FieldRef, checked: bool) -> None: ...

    # --- observation -------------------------------------------------------------

    @abstractmethod
    def exists(self, ref: FieldRef) -> bool:
        """Whether the field is present on the current screen. Must not raise."""

    @abstractmethod
    def status(self):  # -> StatusMessage
        """Read the status bar. Returns an empty message when there is none."""

    @abstractmethod
    def current_tcode(self) -> str: ...

    @abstractmethod
    def window_title(self) -> str: ...

    @abstractmethod
    def has_modal(self) -> bool:
        """Whether a modal popup (``wnd[1]`` or deeper) is open."""

    @abstractmethod
    def dismiss_modal(self, accept: bool = True) -> None:
        """Close the topmost modal popup by confirming or cancelling it."""

    @abstractmethod
    def screenshot(self) -> bytes:
        """Capture the SAP window as PNG bytes.

        Scoped to the SAP window rather than the whole desktop: audit evidence must
        not incidentally capture unrelated applications on the tester's screen.
        """

    # --- context manager ---------------------------------------------------------

    def __enter__(self) -> Driver:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
