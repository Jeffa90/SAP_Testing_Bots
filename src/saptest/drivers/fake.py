"""In-memory driver for testing flows, error handling and reporting without SAP.

This is the main regression net. The runner, catalogue matching, remediation loop,
policy engine and report writers are all exercised against :class:`FakeDriver` in
CI on any platform -- no SAP GUI, no Windows, no network.

Programme SAP's responses with :meth:`queue_message`, or install an ``on_press``
hook to model a screen that reacts to what was typed into it.
"""

from __future__ import annotations

import struct
import zlib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from saptest.core.exceptions import ElementNotFound, LogonFailed, LogonModeMismatch
from saptest.core.status import MessageType, StatusMessage
from saptest.drivers.base import Credentials, Driver, FieldRef, LogonMode, VKey

EASY_ACCESS = "SAP Easy Access"


def _png(width: int = 8, height: int = 8, colour: tuple[int, int, int] = (32, 64, 128)) -> bytes:
    """Build a tiny valid PNG, so evidence code paths get real image bytes."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(colour) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@dataclass
class FakeDriver(Driver):
    """A scriptable stand-in for a real SAP session.

    Attributes:
        present: field names the current screen exposes. ``None`` means every field
            resolves, which is the convenient default for flow tests.
        fields: current field values, readable back via :meth:`read_field`.
        on_press: called as ``on_press(driver, key)`` after each key press, so a
            test can mutate state the way SAP would (populate a document number,
            raise a message, change screen).
    """

    name: str = "fake"
    structured_messages: bool = True

    present: set[str] | None = None
    fields: dict[str, str] = field(default_factory=dict)
    tcode: str = ""
    title: str = EASY_ACCESS
    modal_depth: int = 0
    sap_user: str = ""
    #: When set, ``login`` behaves as an SSO connection regardless of credentials.
    sso_connection: bool = False
    #: When set, ``login`` rejects the given user with an authentication failure.
    reject_users: set[str] = field(default_factory=set)

    on_press: Callable[[FakeDriver, int], None] | None = None

    _messages: deque[StatusMessage] = field(default_factory=deque, repr=False)
    _status: StatusMessage = field(default_factory=StatusMessage, repr=False)
    calls: list[tuple] = field(default_factory=list, repr=False)
    closed: bool = False
    connected_to: str = ""

    # --- programming the fake ----------------------------------------------------

    def queue_message(self, message: StatusMessage) -> FakeDriver:
        """Queue a status message to surface after the next key press."""
        self._messages.append(message)
        return self

    def queue_error(self, msg_id: str, number: str, text: str = "") -> FakeDriver:
        return self.queue_message(
            StatusMessage(MessageType.ERROR, msg_id, number, text or f"{msg_id} {number}")
        )

    def queue_success(self, text: str, msg_id: str = "", number: str = "") -> FakeDriver:
        return self.queue_message(StatusMessage(MessageType.SUCCESS, msg_id, number, text))

    def calls_of(self, kind: str) -> list[tuple]:
        """All recorded calls of one kind, e.g. ``"set_field"``."""
        return [c for c in self.calls if c[0] == kind]

    # --- lifecycle ---------------------------------------------------------------

    def connect(self, connection: str) -> None:
        self.calls.append(("connect", connection))
        self.connected_to = connection

    def login(self, creds: Credentials | None, mode: LogonMode = LogonMode.AUTO) -> str:
        self.calls.append(("login", creds.user if creds else "", mode))
        wants_sso = creds is None or creds.is_sso

        if self.sso_connection and not wants_sso and mode is LogonMode.EXPLICIT:
            raise LogonModeMismatch(
                "Credentials supplied but the connection resolved via SSO; "
                "no logon screen was presented"
            )
        if not self.sso_connection and wants_sso and mode is LogonMode.SSO:
            raise LogonModeMismatch(
                "SSO expected but SAP presented a logon screen; a password is required"
            )
        if creds and creds.user in self.reject_users:
            raise LogonFailed(f"Name or password is incorrect for {creds.user}")

        self.sap_user = "WINDOWSSSO" if self.sso_connection else (creds.user if creds else "")
        self.title = EASY_ACCESS
        self.tcode = "SESSION_MANAGER"
        return self.sap_user

    def logoff(self) -> None:
        self.calls.append(("logoff",))
        self.sap_user = ""
        self.tcode = ""
        self.title = "SAP Logon"

    def close(self) -> None:
        self.calls.append(("close",))
        self.closed = True

    # --- navigation --------------------------------------------------------------

    def start_transaction(self, tcode: str) -> None:
        self.calls.append(("start_transaction", tcode))
        self.tcode = tcode.upper()
        self.title = f"{self.tcode} screen"
        self._pump()

    def go_home(self) -> None:
        self.calls.append(("go_home",))
        self.tcode = "SESSION_MANAGER"
        self.title = EASY_ACCESS
        self.modal_depth = 0

    # --- interaction -------------------------------------------------------------

    def _check(self, ref: FieldRef) -> None:
        if self.present is not None and ref.name not in self.present:
            raise ElementNotFound(f"{ref.name!r} is not on screen {self.title!r}")

    def set_field(self, ref: FieldRef, value: str) -> None:
        self.calls.append(("set_field", ref.name, value))
        self._check(ref)
        self.fields[ref.name] = value

    def read_field(self, ref: FieldRef) -> str:
        self.calls.append(("read_field", ref.name))
        self._check(ref)
        return self.fields.get(ref.name, "")

    def press_key(self, key: VKey | int) -> None:
        self.calls.append(("press_key", int(key)))
        if self.on_press is not None:
            self.on_press(self, int(key))
        self._pump()

    def press_button(self, ref: FieldRef) -> None:
        self.calls.append(("press_button", ref.name))
        self._check(ref)
        self._pump()

    def select_tab(self, ref: FieldRef) -> None:
        self.calls.append(("select_tab", ref.name))
        self._check(ref)

    def set_checkbox(self, ref: FieldRef, checked: bool) -> None:
        self.calls.append(("set_checkbox", ref.name, checked))
        self._check(ref)
        self.fields[ref.name] = "X" if checked else ""

    # --- observation -------------------------------------------------------------

    def exists(self, ref: FieldRef) -> bool:
        return self.present is None or ref.name in self.present

    def _pump(self) -> None:
        """Surface the next queued message, mirroring SAP clearing the bar each round trip."""
        self._status = self._messages.popleft() if self._messages else StatusMessage()

    def status(self) -> StatusMessage:
        return self._status

    def current_tcode(self) -> str:
        return self.tcode

    def window_title(self) -> str:
        return self.title

    def has_modal(self) -> bool:
        return self.modal_depth > 0

    def dismiss_modal(self, accept: bool = True) -> None:
        self.calls.append(("dismiss_modal", accept))
        self.modal_depth = max(0, self.modal_depth - 1)

    def screenshot(self) -> bytes:
        self.calls.append(("screenshot",))
        return _png()
