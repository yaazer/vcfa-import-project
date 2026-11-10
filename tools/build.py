#!/usr/bin/env python3
"""Build distributable forms of vcfa-import.

    python tools/build.py                 # everything this platform can build
    python tools/build.py --no-exe        # just the .pyz
    python tools/build.py --no-installer  # skip the platform installer package

Outputs, under dist/:

    vcfa-import.pyz         single-file zipapp; runs anywhere with Python 3.11+:
                                python vcfa-import.pyz status
    vcfa-import(.exe)       single-file native binary, Python embedded (PyInstaller)
    vcfa-import-<ver>-<platform>.zip / .tar.gz
                            the above plus README, LAB-GUIDE, examples and source
    vcfa-import-<ver>-windows-amd64-setup.exe
                            Windows: a per-user installer (Inno Setup) -- Start Menu
                            shortcut, adds itself to PATH, proper uninstaller
    vcfa-import-<ver>-linux-<arch>-installer.tar.gz
                            Linux: the onedir build plus install.sh/uninstall.sh
                            (installs to ~/.local or, run as root, /opt)

The .pyz is the portable option and needs no build tooling. The native binary and
the installer need no Python on the target machine at all -- for a jump box where
installing Python is not an option, or an air-gapped one.

PyInstaller does not cross-compile: the installer/native binary for a platform can
only be built by running this script *on* that platform. On Windows this script
also needs Inno Setup's ISCC.exe (https://jrsoftware.org/isinfo.php, or
`winget install JRSoftware.InnoSetup`) to build the installer; without it, that
step is skipped. The Linux installer is normally built by CI (see
.github/workflows/build-linux.yml), not by hand.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipapp
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC_FILES = ("README.md", "LAB-GUIDE.md")
# Terms of use and licences: shipped in every build. The tool refuses to run without
# DISCLAIMER.md (nobody can accept terms they cannot read), so a build missing it is useless.
LEGAL_FILES = ("DISCLAIMER.md", "LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.md")
# The .pyz runs with whatever `python3` the machine has; say plainly when it is too old.
PYZ_VERSION_CHECK = """# Checked before importing the package, so an old interpreter gets a clear
# message instead of a traceback. (On Ubuntu 22.04, python3 is 3.10.)
if sys.version_info < (3, 11):
    sys.exit("vcfa-import needs Python 3.11 or newer; this is Python {} ({}).\\n"
             "On Ubuntu 22.04: sudo apt install python3.11, then run it with python3.11, "
             "e.g. python3.11 vcfa-import.pyz --version".format(sys.version.split()[0], sys.executable))
"""
DIST = ROOT / "dist"
ONEDIR_DIST = DIST / "onedir"
sys.path.insert(0, str(ROOT))
from vcfaimport import __version__  # noqa: E402


def build_pyz() -> Path:
    """A zipapp containing only the package -- tests and tools are excluded."""
    staging = DIST / "_pyz"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(ROOT / "vcfaimport", staging / "vcfaimport",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # The console's Help page shows these guides; they ship as package data.
    (staging / "vcfaimport" / "web" / "docs").mkdir()
    for name in DOC_FILES + LEGAL_FILES:
        shutil.copy2(ROOT / name, staging / "vcfaimport" / "web" / "docs" / name)
    (staging / "__main__.py").write_text(
        "import sys\n\n" + PYZ_VERSION_CHECK + "\nfrom vcfaimport.cli import main\nsys.exit(main())\n",
        encoding="utf-8")
    out = DIST / "vcfa-import.pyz"
    zipapp.create_archive(staging, out, interpreter="/usr/bin/env python3", compressed=True)
    shutil.rmtree(staging)
    return out


def _native_name() -> str:
    return "vcfa-import.exe" if platform.system() == "Windows" else "vcfa-import"


def _pyinstaller_available() -> bool:
    try:
        subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                       capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _icon_path() -> Path:
    # Kept in dist/ itself (not a work dir that gets cleaned up): both the
    # PyInstaller builds and the Inno Setup script need to find the same file.
    return DIST / "vcfa-import.ico"


def _make_icon() -> Path | None:
    # PyInstaller only applies --icon on Windows/macOS; skip it elsewhere.
    if platform.system() != "Windows":
        return None
    sys.path.insert(0, str(ROOT / "tools"))
    import make_icons  # the app icon, rasterised with the stdlib
    ico = _icon_path()
    ico.write_bytes(make_icons.ico())
    return ico


def _pyinstaller_cmd(mode: str, distpath: Path, workpath: Path, ico: Path | None) -> list[str]:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--{}".format(mode), "--console", "--clean", "--noconfirm",
        "--name", "vcfa-import",
        "--distpath", str(distpath),
        "--workpath", str(workpath),
        "--specpath", str(workpath),
        # Everything the CLI imports is stdlib, but be explicit about the
        # modules PyInstaller's static analysis sometimes misses.
        "--hidden-import", "sqlite3",
        "--hidden-import", "tomllib",
        "--hidden-import", "getpass",
        "--hidden-import", "ssl",
        "--hidden-import", "concurrent.futures",
        "--hidden-import", "http.server",
        # `serve` imports the web console lazily; its UI is data, not code.
        "--hidden-import", "vcfaimport.web.api",
        "--hidden-import", "vcfaimport.web.server",
        "--add-data", "{}{}{}".format(ROOT / "vcfaimport" / "web" / "static", os.pathsep,
                                      "vcfaimport/web/static"),
        *[arg for name in DOC_FILES + LEGAL_FILES for arg in
          ("--add-data", "{}{}{}".format(ROOT / name, os.pathsep, "vcfaimport/web/docs"))],
        # the third-party licences of what PyInstaller embeds (THIRD-PARTY-NOTICES.md)
        "--add-data", "{}{}{}".format(ROOT / "licenses", os.pathsep, "vcfaimport/web/docs/licenses"),
    ]
    if ico is not None:
        cmd += ["--icon", str(ico)]
    cmd.append(str(ROOT / "vcfa-import.py"))
    return cmd


def _run_pyinstaller(mode: str, distpath: Path, label: str) -> bool:
    work = DIST / "_pyinstaller_{}".format(mode)
    work.mkdir(parents=True, exist_ok=True)
    ico = _make_icon()
    print("  running PyInstaller ({})...".format(label))
    proc = subprocess.run(_pyinstaller_cmd(mode, distpath, work, ico), capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-3000:])
        raise SystemExit("PyInstaller failed")
    shutil.rmtree(work, ignore_errors=True)
    return True


def build_exe() -> Path | None:
    """Single-file native binary: no Python needed on the target machine."""
    if not _pyinstaller_available():
        print("  (native binary skipped: PyInstaller not installed -- pip install pyinstaller)")
        return None
    _run_pyinstaller("onefile", DIST, "onefile")
    return DIST / _native_name()


def build_onedir() -> Path | None:
    """Directory build: what the installer ships. Onefile extracts itself to a temp
    directory on every run, which fails on a jump box with /tmp mounted noexec and
    trips antivirus more often; the installed copy should not do either."""
    if not _pyinstaller_available():
        print("  (onedir build skipped: PyInstaller not installed)")
        return None
    _run_pyinstaller("onedir", ONEDIR_DIST, "onedir, for the installer")
    return ONEDIR_DIST / "vcfa-import"


def _find_iscc() -> str | None:
    found = shutil.which("iscc") or shutil.which("ISCC")
    if found:
        return found
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def build_windows_setup(onedir: Path) -> Path | None:
    iscc = _find_iscc()
    if iscc is None:
        print("  (installer skipped: Inno Setup's ISCC.exe not found -- "
              "winget install JRSoftware.InnoSetup)")
        return None
    cmd = [iscc, "/DMyAppVersion={}".format(__version__), "/DSourceDir={}".format(onedir),
           "/DIcoFile={}".format(_icon_path()),
           str(ROOT / "tools" / "windows-installer.iss")]
    print("  running Inno Setup...")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout[-3000:])
        print(proc.stderr[-2000:])
        raise SystemExit("Inno Setup failed")
    out = DIST / "vcfa-import-{}-windows-amd64-setup.exe".format(__version__)
    return out if out.exists() else None


def build_linux_installer(onedir: Path) -> Path | None:
    tag = "linux-{}".format(platform.machine().lower())
    staging = DIST / "_linux_installer"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copytree(onedir, staging / "vcfa-import")
    (staging / "vcfa-import" / "vcfa-import").chmod(0o755)
    scripts_src = ROOT / "tools" / "linux-installer"
    for name in ("install.sh", "uninstall.sh"):
        dst = staging / name
        shutil.copy2(scripts_src / name, dst)
        dst.chmod(0o755)
    for name in DOC_FILES + LEGAL_FILES:
        shutil.copy2(ROOT / name, staging / name)
    out = DIST / "vcfa-import-{}-{}-installer.tar.gz".format(__version__, tag)
    arcroot = "vcfa-import-{}".format(tag)
    with tarfile.open(out, "w:gz") as tar:
        tar.add(staging, arcname=arcroot)
    shutil.rmtree(staging)
    return out


def build_installer(onedir: Path | None) -> Path | None:
    if onedir is None:
        return None
    system = platform.system()
    if system == "Windows":
        return build_windows_setup(onedir)
    if system == "Linux":
        return build_linux_installer(onedir)
    print("  (installer skipped: no package defined for {})".format(system))
    return None


def build_bundle(pyz: Path, exe: Path | None) -> Path:
    """The portable bundle: pyz (+ native binary) plus docs and source, for someone
    who wants everything but doesn't want (or can't use) an installer."""
    tag = "{}-{}".format(platform.system().lower(), platform.machine().lower())
    top_files = [pyz] + ([exe] if exe else [])
    docs = [ROOT / name for name in ("README.md", "LAB-GUIDE.md", "vcfa-import.py") + LEGAL_FILES
            if (ROOT / name).exists()]
    folders = ("vcfaimport", "examples", "tools", "tests", "licenses")

    def entries():
        for f in top_files:
            yield f, f.name
        for d in docs:
            yield d, d.name
        for folder in folders:
            for path in sorted((ROOT / folder).rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    yield path, str(path.relative_to(ROOT))

    if platform.system() == "Windows":
        out = DIST / "vcfa-import-{}-{}.zip".format(__version__, tag)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, arcname in entries():
                zf.write(path, arcname)
    else:
        # tarfile preserves the executable bit; zipfile does not without extra
        # work, and the whole point of the native binary is "just run it".
        out = DIST / "vcfa-import-{}-{}.tar.gz".format(__version__, tag)
        with tarfile.open(out, "w:gz") as tar:
            for path, arcname in entries():
                tar.add(path, arcname=arcname)
    return out


def smoke(pyz: Path, exe: Path | None) -> None:
    # The web console's UI ships as package data; a build without it serves a blank page.
    with zipfile.ZipFile(pyz) as zf:
        names = set(zf.namelist())
    missing = [f for f in ("index.html", "core.js", "skins.js", "fx.js", "views.js", "gov.js", "help.js", "app.css",
                           "favicon.svg", "appicon.svg")
               if "vcfaimport/web/static/" + f not in names]
    missing += [f for f in DOC_FILES + LEGAL_FILES if "vcfaimport/web/docs/" + f not in names]
    if missing:
        raise SystemExit("web console assets missing from the .pyz: " + ", ".join(missing))
    print("  smoke web  ok   console assets present")
    for label, cmd in (("pyz", [sys.executable, str(pyz), "--version"]),
                       ("exe", [str(exe), "--version"] if exe else None)):
        if not cmd:
            continue
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        ok = proc.returncode == 0 and __version__ in proc.stdout
        print("  smoke {:<4} {}  {}".format(label, "ok " if ok else "FAIL", proc.stdout.strip()[:60]))
        if not ok:
            print(proc.stderr[-1500:])
            raise SystemExit("smoke test failed for " + label)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-exe", action="store_true", help="skip the single-file native binary")
    ap.add_argument("--no-installer", action="store_true", help="skip the platform installer package")
    args = ap.parse_args()

    DIST.mkdir(exist_ok=True)
    print("building vcfa-import {}".format(__version__))
    pyz = build_pyz()
    print("  wrote {} ({:.0f} KB)".format(pyz.name, pyz.stat().st_size / 1024))
    exe = None if args.no_exe else build_exe()
    if exe:
        print("  wrote {} ({:.1f} MB)".format(exe.name, exe.stat().st_size / 1e6))
    smoke(pyz, exe)
    bundle = build_bundle(pyz, exe)
    print("  wrote {} ({:.1f} MB)".format(bundle.name, bundle.stat().st_size / 1e6))

    if not args.no_installer:
        onedir = build_onedir()
        installer = build_installer(onedir)
        if installer:
            print("  wrote {} ({:.1f} MB)".format(installer.name, installer.stat().st_size / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
