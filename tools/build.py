#!/usr/bin/env python3
"""Build distributable forms of vcfa-import.

    python tools/build.py            # .pyz always; .exe too if PyInstaller is installed
    python tools/build.py --no-exe   # just the .pyz

Outputs, under dist/:

    vcfa-import.pyz         single-file zipapp; runs anywhere with Python 3.11+:
                                python vcfa-import.pyz status
    vcfa-import.exe         Windows binary with Python embedded (PyInstaller);
                            built only on Windows, only when PyInstaller is present
    vcfa-import-<ver>-<platform>.zip
                            the above plus README, LAB-GUIDE, examples and source

The .pyz is the portable option and needs no build tooling. The .exe is for a
jump box where installing Python is not an option.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import zipapp
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC_FILES = ("README.md", "LAB-GUIDE.md")
# The .pyz runs with whatever `python3` the machine has; say plainly when it is too old.
PYZ_VERSION_CHECK = """# Checked before importing the package, so an old interpreter gets a clear
# message instead of a traceback. (On Ubuntu 22.04, python3 is 3.10.)
if sys.version_info < (3, 11):
    sys.exit("vcfa-import needs Python 3.11 or newer; this is Python {} ({}).\\n"
             "On Ubuntu 22.04: sudo apt install python3.11, then run it with python3.11, "
             "e.g. python3.11 vcfa-import.pyz --version".format(sys.version.split()[0], sys.executable))
"""
DIST = ROOT / "dist"
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
    for name in DOC_FILES:
        shutil.copy2(ROOT / name, staging / "vcfaimport" / "web" / "docs" / name)
    (staging / "__main__.py").write_text(
        "import sys\n\n" + PYZ_VERSION_CHECK + "\nfrom vcfaimport.cli import main\nsys.exit(main())\n",
        encoding="utf-8")
    out = DIST / "vcfa-import.pyz"
    zipapp.create_archive(staging, out, interpreter="/usr/bin/env python3", compressed=True)
    shutil.rmtree(staging)
    return out


def build_exe() -> Path | None:
    if platform.system() != "Windows":
        print("  (exe skipped: PyInstaller output is only built on Windows here)")
        return None
    try:
        subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                       capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("  (exe skipped: PyInstaller not installed -- pip install pyinstaller)")
        return None

    work = DIST / "_pyinstaller"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--console", "--clean", "--noconfirm",
        "--name", "vcfa-import",
        "--distpath", str(DIST),
        "--workpath", str(work),
        "--specpath", str(work),
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
        *[arg for name in DOC_FILES for arg in
          ("--add-data", "{}{}{}".format(ROOT / name, os.pathsep, "vcfaimport/web/docs"))],
        str(ROOT / "vcfa-import.py"),
    ]
    print("  running PyInstaller...")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-3000:])
        raise SystemExit("PyInstaller failed")
    shutil.rmtree(work, ignore_errors=True)
    return DIST / "vcfa-import.exe"


def build_bundle(pyz: Path, exe: Path | None) -> Path:
    tag = "{}-{}".format(platform.system().lower(), platform.machine().lower())
    out = DIST / "vcfa-import-{}-{}.zip".format(__version__, tag)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(pyz, pyz.name)
        if exe:
            zf.write(exe, exe.name)
        for name in ("README.md", "LAB-GUIDE.md", "vcfa-import.py"):
            if (ROOT / name).exists():
                zf.write(ROOT / name, name)
        for folder in ("vcfaimport", "examples", "tools", "tests"):
            for path in sorted((ROOT / folder).rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    zf.write(path, str(path.relative_to(ROOT)))
    return out


def smoke(pyz: Path, exe: Path | None) -> None:
    # The web console's UI ships as package data; a build without it serves a blank page.
    with zipfile.ZipFile(pyz) as zf:
        names = set(zf.namelist())
    missing = [f for f in ("index.html", "core.js", "skins.js", "fx.js", "views.js", "gov.js", "help.js", "app.css")
               if "vcfaimport/web/static/" + f not in names]
    missing += [f for f in DOC_FILES if "vcfaimport/web/docs/" + f not in names]
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
    ap.add_argument("--no-exe", action="store_true")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
