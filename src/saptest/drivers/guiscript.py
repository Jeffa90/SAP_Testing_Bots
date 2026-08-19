"""SAP GUI Scripting driver.

Drives the real SAP GUI client through its COM automation interface. Because the
requests travel the ordinary dialog path, the SAP kernel applies exactly the
authorisation checks it would for a human: ``S_TCODE`` for the transaction, and
every authorisation object the dialog program checks. That is what makes this the
only robust option for role-aware testing -- calling BAPIs directly would bypass
those checks entirely.

Windows-only. The module imports on any platform; the COM dependency is resolved
lazily inside :meth:`GuiScriptDriver.connect`, so the rest of the library and its
test suite run anywhere.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from saptest.core.exceptions import (
    AuthorizationBlocked,
    DriverUnavailable,
    ElementNotFound,
    LogonFailed,
    LogonModeMismatch,
    NavigationError,
    SapGuiNotRunning,
    ScriptingDisabled,
)
from saptest.core.status import StatusMessage, from_statusbar
from saptest.drivers.base import Credentials, Driver, FieldRef, LogonMode, VKey

log = logging.getLogger(__name__)

EASY_ACCESS_TCODE = "SESSION_MANAGER"

# Logon screen elements. Stable across S/4HANA releases.
_LOGON_CLIENT = "wnd[0]/usr/txtRSYST-MANDT"
_LOGON_USER = "wnd[0]/usr/txtRSYST-BNAME"
_LOGON_PASSWORD = "wnd[0]/usr/pwdRSYST-BCODE"
_LOGON_LANGUAGE = "wnd[0]/usr/txtRSYST-LANGU"
_OKCODE = "wnd[0]/tbar[0]/okcd"
_STATUSBAR = "wnd[0]/sbar"

#: Multiple-logon dialog options. Defaulting to "without ending any other logons"
#: so a bot never terminates a human tester's session on the same bot account.
_MULTI_LOGON_END_OTHERS = "wnd[1]/usr/radMULTI_LOGON_OPT1"
_MULTI_LOGON_KEEP_OTHERS = "wnd[1]/usr/radMULTI_LOGON_OPT2"

#: Text fragments identifying an authorisation refusal, as a safety net for
#: messages whose class/number the catalogue does not yet carry.
_AUTH_HINTS = ("not authorized", "not authorised", "no authorization", "no authorisation")


class GuiScriptDriver(Driver):
    """Drives SAP GUI through the Scripting API."""

    name = "guiscript"
    structured_messages = True

    def __init__(
        self,
        *,
        sap_logon_path: str | None = None,
        launch_timeout_s: float = 60.0,
        step_delay_s: float = 0.0,
        end_other_logons: bool = False,
    ) -> None:
        self._app = None
        self._connection = None
        self._session = None
        self._opened_connection = False
        self.sap_logon_path = sap_logon_path
        self.launch_timeout_s = launch_timeout_s
        #: Optional pause after each interaction. Zero by default: scripting calls
        #: are synchronous, so the sleeps the original scripts needed are not.
        self.step_delay_s = step_delay_s
        self.end_other_logons = end_other_logons

    # --- COM plumbing ------------------------------------------------------------

    @staticmethod
    def _com():
        """Import pywin32 lazily and explain clearly when it is unavailable."""
        try:
            import win32com.client  # noqa: PLC0415
        except ImportError as exc:
            raise DriverUnavailable(
                "The guiscript driver needs pywin32, which is Windows-only. "
                "Install with: pip install 'saptest[windows]'. "
                "On other platforms use the fake driver for tests."
            ) from exc
        return win32com.client

    def _scripting_engine(self):
        """Attach to a running SAP GUI, launching SAP Logon first if needed."""
        com = self._com()
        # Late binding on purpose: EnsureDispatch writes a gen_py cache that does
        # not survive PyInstaller freezing, and is a classic packaging failure.
        for attempt in range(2):
            try:
                sap_gui = com.GetObject("SAPGUI")
            except Exception:  # noqa: BLE001 - COM raises com_error, not importable here
                if attempt == 0:
                    self._launch_sap_logon()
                    continue
                raise SapGuiNotRunning(
                    "SAP GUI is not running and SAP Logon could not be started. "
                    "Start SAP Logon and try again."
                ) from None
            engine = sap_gui.GetScriptingEngine
            if engine is None:
                raise ScriptingDisabled(
                    "SAP GUI Scripting is disabled on this client. Enable it in "
                    "SAP Logon > Options > Accessibility & Scripting > Scripting."
                )
            return engine
        raise SapGuiNotRunning("SAP GUI did not become available")

    def _launch_sap_logon(self) -> None:
        """Start SAP Logon and wait for its COM object to register."""
        exe = self.sap_logon_path or _find_sap_logon()
        if not exe:
            raise SapGuiNotRunning(
                "SAP Logon executable not found. Start SAP Logon manually, or set "
                "sap_logon_path in the region profile."
            )
        log.info("Starting SAP Logon: %s", exe)
        subprocess.Popen([exe], close_fds=True)  # noqa: S603 - path is operator-configured

        com = self._com()
        deadline = time.monotonic() + self.launch_timeout_s
        while time.monotonic() < deadline:
            try:
                com.GetObject("SAPGUI")
                time.sleep(1.0)  # let the scripting engine finish registering
                return
            except Exception:  # noqa: BLE001
                time.sleep(1.0)
        raise SapGuiNotRunning(
            f"SAP Logon did not become scriptable within {self.launch_timeout_s:.0f}s"
        )

    # --- lifecycle ---------------------------------------------------------------

    def connect(self, connection: str) -> None:
        """Attach to an open connection to ``connection``, or open one."""
        self._app = self._scripting_engine()

        existing = self._find_open_connection(connection)
        if existing is not None:
            log.info("Reusing open SAP connection %r", connection)
            self._connection = existing
        else:
            log.info("Opening SAP connection %r", connection)
            try:
                self._connection = self._app.OpenConnection(connection, True)
            except Exception as exc:  # noqa: BLE001
                raise SapGuiNotRunning(
                    f"Could not open SAP connection {connection!r}. Check that the entry "
                    f"exists in SAP Logon and the name matches exactly. ({exc})"
                ) from exc
            self._opened_connection = True

        if self._connection.Children.Count == 0:
            raise ScriptingDisabled(
                "The connection opened but exposes no scriptable session. This normally "
                "means sapgui/user_scripting is FALSE on the application server. Ask Basis "
                "to set it to TRUE (it can be changed dynamically with RZ11)."
            )
        self._session = self._connection.Children(0)

    def _find_open_connection(self, name: str):
        """Find an already-open connection matching the SAP Logon entry name."""
        try:
            for i in range(self._app.Children.Count):
                conn = self._app.Children(i)
                if name.lower() in str(conn.Description or "").lower():
                    return conn
        except Exception:  # noqa: BLE001 - enumeration is best-effort
            pass
        return None

    def login(self, creds: Credentials | None, mode: LogonMode = LogonMode.AUTO) -> str:
        """Authenticate, reconciling the expected logon mode with what SAP presented.

        The mode is *detected*, not assumed: the driver probes for the logon screen
        and raises :class:`LogonModeMismatch` when reality disagrees with the profile.
        """
        self._require_session()
        wants_sso = creds is None or creds.is_sso
        logon_screen = self._find(_LOGON_USER, required=False)

        if logon_screen is not None:
            if wants_sso:
                raise LogonModeMismatch(
                    "SSO was expected but SAP presented a logon screen. Either supply a "
                    "password for this user, or check that the SAP Logon entry is "
                    "configured for Single Sign-On."
                )
            self._enter_credentials(creds)
        else:
            if mode is LogonMode.EXPLICIT and not wants_sso:
                raise LogonModeMismatch(
                    "Credentials were supplied but the connection resolved via SSO, so "
                    f"the session is running as the Windows user, not {creds.user!r}. "
                    "Set logon_mode: sso in the region profile, or disable SSO on the "
                    "SAP Logon entry to test as a specific bot user."
                )
            log.info("Connection resolved via SSO; no credentials required")

        self._handle_post_logon()

        actual = self.sap_user()
        if not actual:
            raise LogonFailed("Logon did not establish a SAP user session")
        if creds and not creds.is_sso and actual.upper() != creds.user.upper():
            log.warning("Logged on as %r but %r was requested", actual, creds.user)
        log.info("Logged on as %s (client %s)", actual, self._info("Client"))
        return actual

    def _enter_credentials(self, creds: Credentials) -> None:
        if creds.client:
            self._set_text(_LOGON_CLIENT, creds.client, required=False)
        self._set_text(_LOGON_USER, creds.user)
        self._set_text(_LOGON_PASSWORD, creds.password)
        if creds.language:
            self._set_text(_LOGON_LANGUAGE, creds.language, required=False)
        self.press_key(VKey.ENTER)

        message = self.status()
        if message.is_problem and message.text:
            # Wrong password, locked account, expired password: all fatal for the run,
            # and all distinct from "this user lacks a role".
            raise LogonFailed(f"Logon rejected for {creds.user!r}: {message.text}")

    def _handle_post_logon(self, max_dialogs: int = 5) -> None:
        """Clear the dialogs SAP may interpose between logon and Easy Access.

        Replaces the background thread in the original scripts that blind-pressed
        Enter at whatever happened to be focused.
        """
        for _ in range(max_dialogs):
            if not self.has_modal():
                return
            title = str(self._active_window_text()).lower()

            if "multiple logon" in title or self._find(_MULTI_LOGON_KEEP_OTHERS, False):
                target = (
                    _MULTI_LOGON_END_OTHERS if self.end_other_logons else _MULTI_LOGON_KEEP_OTHERS
                )
                option = self._find(target, required=False)
                if option is not None:
                    option.Select()
                self.press_key(VKey.ENTER)
                continue

            if "password" in title and "change" in title:
                raise LogonFailed(
                    "SAP is demanding a password change for this user. Reset the bot "
                    "account's password in SU01 and update the test data."
                )

            # Copyright notice and similar informational dialogs.
            self.dismiss_modal(accept=True)

    def logoff(self) -> None:
        """End the session with /nex, which exits without a confirmation prompt."""
        if self._session is None:
            return
        try:
            self._set_text(_OKCODE, "/nex", required=False)
            self.press_key(VKey.ENTER)
        except Exception as exc:  # noqa: BLE001 - logoff must not mask a test result
            log.warning("Logoff did not complete cleanly: %s", exc)
        finally:
            self._session = None
            self._connection = None

    def close(self) -> None:
        """Release COM references. Safe to call repeatedly."""
        self._session = None
        self._connection = None
        self._app = None

    # --- navigation --------------------------------------------------------------

    def start_transaction(self, tcode: str) -> None:
        """Navigate to a transaction, discarding any in-progress document."""
        self._require_session()
        code = tcode.strip().upper()
        self._set_text(_OKCODE, f"/n{code}")
        self.press_key(VKey.ENTER)
        self._confirm_data_loss()

        message = self.status()
        if message.is_problem and _looks_like_auth(message.text):
            raise AuthorizationBlocked(
                f"User {self.sap_user()!r} is not authorised for transaction {code}: "
                f"{message.text}"
            )
        current = self.current_tcode()
        if current and current != code and not current.startswith(code):
            log.debug("After /n%s the session reports transaction %r", code, current)

    def go_home(self) -> None:
        """Return to SAP Easy Access from wherever the session is."""
        self._require_session()
        for okcode in ("/n", f"/n{EASY_ACCESS_TCODE}"):
            try:
                self._set_text(_OKCODE, okcode)
                self.press_key(VKey.ENTER)
                self._confirm_data_loss()
            except Exception as exc:  # noqa: BLE001
                log.debug("go_home via %s failed: %s", okcode, exc)
                continue
            if self.current_tcode() == EASY_ACCESS_TCODE:
                return
        raise NavigationError(
            f"Could not return to SAP Easy Access; session is on {self.current_tcode()!r}"
        )

    def _confirm_data_loss(self) -> None:
        """Accept the "Data will be lost" prompt that leaving a document raises."""
        if self.has_modal():
            text = str(self._active_window_text()).lower()
            if "exit" in text or "data will be lost" in text or "unsaved" in text:
                self.dismiss_modal(accept=True)

    # --- element resolution ------------------------------------------------------

    def _require_session(self):
        if self._session is None:
            raise SapGuiNotRunning("No SAP session; call connect() first")
        return self._session

    def _find(self, element_id: str, required: bool = True):
        """Resolve a SAP GUI element id.

        Uses ``findById(id, False)``, which returns ``None`` rather than raising when
        the element is absent -- making presence probes cheap and exception-free.
        """
        session = self._require_session()
        try:
            element = session.findById(element_id, False)
        except Exception as exc:  # noqa: BLE001
            if required:
                raise ElementNotFound(f"Error resolving {element_id!r}: {exc}") from exc
            return None
        if element is None and required:
            raise ElementNotFound(
                f"Element {element_id!r} is not on screen {self.window_title()!r}"
            )
        return element

    def _resolve(self, ref: FieldRef, required: bool = True):
        """Resolve a :class:`FieldRef`, preferring its id and falling back to its label."""
        if ref.id:
            element = self._find(ref.id, required=False)
            if element is not None:
                return element
        if ref.label:
            element = self._find_by_label(ref.label)
            if element is not None:
                return element
        if required:
            raise ElementNotFound(
                f"Could not locate {ref.name!r} ({ref}) on screen {self.window_title()!r}. "
                f"Tried id={ref.id!r}, label={ref.label!r}."
            )
        return None

    def _find_by_label(self, label: str):
        """Scan the current screen's user area for a labelled input.

        Slower than an id lookup and only used as a fallback, so a binding whose id
        has drifted degrades to a warning rather than a failed run.
        """
        user_area = self._find("wnd[0]/usr", required=False)
        if user_area is None:
            return None
        wanted = label.strip().lower().rstrip(":")
        try:
            children = user_area.Children
            for i in range(children.Count):
                child = children(i)
                if str(getattr(child, "Text", "")).strip().lower().rstrip(":") == wanted:
                    # SAP labels expose the input they annotate via LabelledBy/associated id.
                    for attr in ("LabelledBy", "AssociatedField"):
                        target = getattr(child, attr, None)
                        if target is not None:
                            return target
        except Exception as exc:  # noqa: BLE001
            log.debug("Label scan for %r failed: %s", label, exc)
        return None

    # --- interaction -------------------------------------------------------------

    def _set_text(self, element_id: str, value: str, required: bool = True) -> None:
        element = self._find(element_id, required=required)
        if element is None:
            return
        element.Text = value

    def set_field(self, ref: FieldRef, value: str) -> None:
        """Set a field, dispatching on its SAP GUI element type."""
        element = self._resolve(ref)
        kind = str(getattr(element, "Type", ""))
        try:
            if kind == "GuiCheckBox":
                element.Selected = _truthy(value)
            elif kind == "GuiRadioButton":
                if _truthy(value):
                    element.Select()
            elif kind == "GuiComboBox":
                element.Key = value
            else:
                # GuiTextField, GuiCTextField, GuiPasswordField, GuiTextEdit.
                try:
                    element.SetFocus()
                except Exception:  # noqa: BLE001 - focus is a nicety, not required
                    pass
                element.Text = value
        except Exception as exc:  # noqa: BLE001
            raise ElementNotFound(
                f"Could not set {ref.name!r} (type {kind or 'unknown'}) to {value!r}: {exc}"
            ) from exc
        self._pause()

    def read_field(self, ref: FieldRef) -> str:
        element = self._resolve(ref)
        kind = str(getattr(element, "Type", ""))
        if kind == "GuiCheckBox":
            return "X" if element.Selected else ""
        if kind == "GuiComboBox":
            return str(element.Key or "")
        return str(getattr(element, "Text", "") or "")

    def press_key(self, key: VKey | int) -> None:
        """Send a virtual key. Synchronous: returns after SAP's round trip completes."""
        window = self._find("wnd[0]")
        window.SendVKey(int(key))
        self._pause()

    def press_button(self, ref: FieldRef) -> None:
        self._resolve(ref).Press()
        self._pause()

    def select_tab(self, ref: FieldRef) -> None:
        self._resolve(ref).Select()
        self._pause()

    def set_checkbox(self, ref: FieldRef, checked: bool) -> None:
        self._resolve(ref).Selected = bool(checked)
        self._pause()

    def _pause(self) -> None:
        if self.step_delay_s:
            time.sleep(self.step_delay_s)

    # --- observation -------------------------------------------------------------

    def exists(self, ref: FieldRef) -> bool:
        try:
            return self._resolve(ref, required=False) is not None
        except Exception:  # noqa: BLE001 - a presence probe must never raise
            return False

    def status(self) -> StatusMessage:
        sbar = self._find(_STATUSBAR, required=False)
        return from_statusbar(sbar) if sbar is not None else StatusMessage()

    def _info(self, attribute: str) -> str:
        try:
            return str(getattr(self._require_session().Info, attribute, "") or "")
        except Exception:  # noqa: BLE001
            return ""

    def sap_user(self) -> str:
        """The SAP user this session authenticated as -- the ground truth for reports."""
        return self._info("User")

    def current_tcode(self) -> str:
        return self._info("Transaction").upper()

    def window_title(self) -> str:
        return self._active_window_text()

    def _active_window_text(self) -> str:
        try:
            return str(self._require_session().ActiveWindow.Text or "")
        except Exception:  # noqa: BLE001
            return ""

    def has_modal(self) -> bool:
        """Whether a popup window (wnd[1] or deeper) is open."""
        try:
            return self._require_session().Children.Count > 1
        except Exception:  # noqa: BLE001
            return False

    def dismiss_modal(self, accept: bool = True) -> None:
        """Confirm or cancel the topmost popup."""
        if not self.has_modal():
            return
        session = self._require_session()
        top = session.Children(session.Children.Count - 1)
        try:
            if accept:
                top.SendVKey(int(VKey.ENTER))
            else:
                top.Close()
        except Exception as exc:  # noqa: BLE001
            log.debug("Dismissing modal failed, falling back to Close(): %s", exc)
            try:
                top.Close()
            except Exception:  # noqa: BLE001
                pass
        self._pause()

    def screenshot(self) -> bytes:
        """Capture the SAP window via the native HardCopy call, normalised to PNG.

        HardCopy writes whichever format the installed SAP GUI supports, so the
        result is re-encoded to PNG for consistency. Captures the topmost popup when
        one is open, since that is the state the tester would be looking at.
        """
        session = self._require_session()
        index = session.Children.Count - 1 if self.has_modal() else 0
        window = session.Children(index)

        tmp = Path(tempfile.gettempdir()) / f"saptest_{os.getpid()}_{time.time_ns()}.png"
        try:
            # An absolute path is required: HardCopy resolves relative paths against
            # SAP GUI's own working directory, not the process's.
            written = window.HardCopy(str(tmp))
            source = Path(str(written)) if written and Path(str(written)).exists() else tmp
            data = source.read_bytes()
        finally:
            for leftover in (tmp, tmp.with_suffix(".bmp"), tmp.with_suffix(".jpg")):
                leftover.unlink(missing_ok=True)

        return _to_png(data)


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in ("x", "true", "1", "yes", "on")


def _looks_like_auth(text: str) -> bool:
    lowered = (text or "").lower()
    return any(hint in lowered for hint in _AUTH_HINTS)


def _to_png(data: bytes) -> bytes:
    """Normalise captured image bytes to PNG, passing through if already PNG."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(io.BytesIO(data)) as image:
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="PNG")
            return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 - keep the original bytes rather than lose evidence
        log.warning("Could not convert screenshot to PNG, storing as captured: %s", exc)
        return data


def _find_sap_logon() -> str | None:
    """Locate saplogon.exe in PATH or the usual install locations."""
    found = shutil.which("saplogon")
    if found:
        return found
    candidates = [
        r"C:\Program Files (x86)\SAP\FrontEnd\SAPgui\saplogon.exe",
        r"C:\Program Files\SAP\FrontEnd\SAPgui\saplogon.exe",
    ]
    return next((c for c in candidates if Path(c).exists()), None)
