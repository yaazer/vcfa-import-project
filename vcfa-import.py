#!/usr/bin/env python3
"""Entry point shim so the tool can be run without installing it.

    python vcfa-import.py status
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vcfaimport.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
