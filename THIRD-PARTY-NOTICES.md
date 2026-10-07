# Third-party notices

vcfa-import's own code is licensed under the [Apache License 2.0](LICENSE).
Which third-party software you receive depends on how you run the tool.

| How you run it | Third-party software included |
|---|---|
| From the source tree (`python vcfa-import.py`) | None. It uses the Python you already have, under that installation's own licence. |
| `vcfa-import.pyz` | None. The zipapp contains only vcfa-import's own code and documents, and runs on the Python already on the machine. |
| `vcfa-import.exe` (Windows, built with PyInstaller) | The components below, embedded in the executable. |

Your use of each component is governed by its own licence. Those licences
disclaim all warranties in their own terms. The authors of vcfa-import make no
promises about any of these components (see [DISCLAIMER.md](DISCLAIMER.md),
section 11).

## Components embedded in vcfa-import.exe

The versions are those of the Python 3.12.10 runtime the release was built with.

| Component | Version | Licence | Full text |
|---|---|---|---|
| CPython interpreter and standard library (`python312.dll`, `*.pyd`) | 3.12.10 | Python Software Foundation License Version 2 (PSF-2.0) | [licenses/PYTHON-3.12-LICENSE.txt](licenses/PYTHON-3.12-LICENSE.txt) |
| Microsoft Visual C++ runtime (`VCRUNTIME140.dll`) and Microsoft Distributable Code linked into the binaries | — | Microsoft Distributable Code terms (see below) | [licenses/PYTHON-3.12-LICENSE.txt](licenses/PYTHON-3.12-LICENSE.txt), "Additional Conditions for this Windows binary build" |
| OpenSSL (`libssl-3.dll`, `libcrypto-3.dll`) | 3.0.16 | Apache License 2.0 | [licenses/PYTHON-3.12-LICENSE.txt](licenses/PYTHON-3.12-LICENSE.txt), [licenses/CPYTHON-3.12-INCORPORATED-SOFTWARE.rst](licenses/CPYTHON-3.12-INCORPORATED-SOFTWARE.rst) ("OpenSSL") |
| libffi (`libffi-8.dll`) | 3.4 | MIT | [licenses/PYTHON-3.12-LICENSE.txt](licenses/PYTHON-3.12-LICENSE.txt), [licenses/CPYTHON-3.12-INCORPORATED-SOFTWARE.rst](licenses/CPYTHON-3.12-INCORPORATED-SOFTWARE.rst) ("libffi") |
| bzip2 / libbzip2 (in `_bz2.pyd`) | 1.0.8 | bzip2 licence (BSD-style) | [licenses/PYTHON-3.12-LICENSE.txt](licenses/PYTHON-3.12-LICENSE.txt) |
| Expat XML parser (in `pyexpat.pyd`) | 2.7.1 | MIT | [licenses/CPYTHON-3.12-INCORPORATED-SOFTWARE.rst](licenses/CPYTHON-3.12-INCORPORATED-SOFTWARE.rst) ("expat") |
| libmpdec (in `_decimal.pyd`) | 2.5.1 | BSD-2-Clause | [licenses/CPYTHON-3.12-INCORPORATED-SOFTWARE.rst](licenses/CPYTHON-3.12-INCORPORATED-SOFTWARE.rst) ("libmpdec") |
| zlib (in `python312.dll`) | 1.3.1 | zlib licence | [licenses/CPYTHON-3.12-INCORPORATED-SOFTWARE.rst](licenses/CPYTHON-3.12-INCORPORATED-SOFTWARE.rst) ("zlib") |
| Other code incorporated in CPython (Mersenne Twister, SipHash, strtod/dtoa and others listed in the file) | as shipped with 3.12.10 | various permissive licences | [licenses/CPYTHON-3.12-INCORPORATED-SOFTWARE.rst](licenses/CPYTHON-3.12-INCORPORATED-SOFTWARE.rst) |
| SQLite (`sqlite3.dll`) | 3.49.1 | Public domain | SQLite is dedicated to the public domain by its authors; no licence text is required. |
| XZ Utils / liblzma (in `_lzma.pyd`) | as shipped with 3.12.10 | Public domain | liblzma in the XZ Utils releases used by CPython 3.12 is in the public domain. |
| PyInstaller bootloader | 6.22.2 | GPL-2.0-or-later with the PyInstaller Bootloader Exception | [licenses/PYINSTALLER-COPYING.txt](licenses/PYINSTALLER-COPYING.txt) |

**PyInstaller.** The bootloader exception in PyInstaller's licence allows the
bootloader to be distributed inside programs under any terms, including vcfa-import's.
PyInstaller itself is a build tool and is not part of the distributed software.

**Microsoft Distributable Code.** The Windows build of Python links Microsoft
Distributable Code into its binaries and depends on the Visual C++ runtime.
Microsoft requires anyone who redistributes that code to have end users accept
terms that protect it at least as much as Microsoft's own terms. vcfa-import's
terms of use ([DISCLAIMER.md](DISCLAIMER.md), section 11), which every user must
accept before the software runs, do this. In short, you must not:

- alter any copyright, trademark or patent notice in Microsoft's Distributable Code;
- use Microsoft's trademarks in a way that suggests vcfa-import comes from, or is
  endorsed by, Microsoft;
- run Microsoft's Distributable Code on a platform other than Microsoft operating
  systems, run-time technologies or application platforms; or
- include it in malicious, deceptive or unlawful programs.

## Not included

vcfa-import calls programs you install yourself, chiefly `kubectl`, and talks to
VMware vCenter and VMware Cloud Foundation over their APIs. None of these is
distributed with vcfa-import. Their own licences and terms apply.

The development and test tooling in `tools/` and `tests/` may use PyYAML and
PyInstaller when you install them yourself. Neither is redistributed.

The console's "VMware Modern" and "vCenter Classic" skins use icons drawn for
vcfa-import, not copies of VMware artwork. They name fonts such as Clarity City
and Metropolis, which are used only if they are already installed on the
viewer's computer. No fonts are distributed.
