#!/usr/bin/env python3
"""Entry point shim so the tool can be run without installing it.

    python vcfa-import.py status
"""

import sys
from pathlib import Path

# Checked before importing the package, so an old interpreter gets a clear
# message instead of a traceback. (On Ubuntu 22.04, python3 is 3.10.)
if sys.version_info < (3, 11):
    sys.exit("vcfa-import needs Python 3.11 or newer; this is Python {} ({}).\n"
             "On Ubuntu 22.04: sudo apt install python3.11, then run it with python3.11, "
             "e.g. python3.11 vcfa-import.pyz --version".format(sys.version.split()[0], sys.executable))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vcfaimport.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
