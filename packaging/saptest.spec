# PyInstaller spec: one-folder Windows build.
#
# --onedir rather than --onefile: one-file extracts the whole bundle to a temp
# directory on every launch, which is slow and trips corporate antivirus policy on
# locked-down Windows builds.
#
# Build with:   pyinstaller packaging/saptest.spec --noconfirm
# Output:       dist/saptest/  -- copy the whole folder to the test machine.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src" / "saptest"

# Configuration ships beside the executable so an operator can edit a region
# profile, a binding or the error catalogue without a rebuild.
datas = [
    (str(SRC / "ui" / "templates"), "saptest/ui/templates"),
    (str(SRC / "ui" / "static"), "saptest/ui/static"),
    (str(ROOT / "profiles"), "profiles"),
    (str(ROOT / "catalog"), "catalog"),
    (str(ROOT / "templates"), "templates"),
]

hiddenimports = [
    # pywin32: COM is reached by name at runtime, so nothing imports these statically.
    "win32com",
    "win32com.client",
    "win32com.client.dynamic",
    "pythoncom",
    "pywintypes",
    "win32api",
    "win32gui",
    "win32con",
    # uvicorn resolves its protocol implementations by string.
    *collect_submodules("uvicorn"),
    # Flows are discovered by walking the package, which PyInstaller cannot see.
    *collect_submodules("saptest.flows"),
    "saptest.drivers.guiscript",
    "saptest.drivers.legacy_ui",
    "saptest.drivers.demo",
]

analysis = Analysis(
    [str(SRC / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Trim what a console tool never needs; keeps the folder to a sensible size.
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "pytest", "IPython"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="saptest",
    console=True,          # the run log is genuinely useful to watch
    debug=False,
    strip=False,
    upx=False,             # UPX-packed binaries are a common false positive for AV
)

COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="saptest",
)
