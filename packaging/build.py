"""Build the Windows distributable and smoke-test it.

Run on a Windows machine with the project installed::

    pip install -e ".[windows,build]"
    python packaging/build.py

Produces ``dist/saptest/``. Copy that folder to any Windows machine -- no Python
installation, no pip, no admin rights.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "saptest.spec"
DIST = ROOT / "dist" / "saptest"
EXE = DIST / ("saptest.exe" if platform.system() == "Windows" else "saptest")


def build() -> None:
    print(f"Building from {SPEC.relative_to(ROOT)} ...")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--clean"],
        cwd=ROOT,
        check=True,
    )


def smoke_test() -> None:
    """Launch the built executable. Catches missing hidden imports and data files.

    A frozen build that imports cleanly in development and then fails on the tester's
    machine is the whole reason this step exists.
    """
    if not EXE.exists():
        raise SystemExit(f"Build produced no executable at {EXE}")

    checks = [
        ([str(EXE), "--version"], "version"),
        ([str(EXE), "flows"], "flow discovery"),
        ([str(EXE), "regions"], "profile loading"),
        ([str(EXE), "catalog", "validate"], "catalogue and bindings"),
    ]
    for command, label in checks:
        print(f"  checking {label} ...", end=" ", flush=True)
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print("FAILED")
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            raise SystemExit(f"Smoke test failed: {label}")
        print("ok")


def main() -> None:
    if platform.system() != "Windows":
        print(
            "Warning: building on "
            f"{platform.system()}. PyInstaller does not cross-compile, so this will "
            "not produce a Windows executable. Run this on Windows.\n"
        )
    build()
    smoke_test()
    print(f"\nDone. Distributable folder: {DIST}")
    print("Copy the whole folder to the test machine and run saptest.exe.\n")


if __name__ == "__main__":
    main()
