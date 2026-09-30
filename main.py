#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
import sys

try:
    import PySide6  # noqa: F401
except ImportError:
    print(
        "PySide6 is not installed - this app's window is built with Qt.\n"
        "Install it into the project's virtualenv and re-run:\n"
        "  python3 -m venv .venv\n"
        "  .venv/bin/pip install -e .\n"
        "  .venv/bin/mediabrowser\n",
        file=sys.stderr,
    )
    sys.exit(1)

from mediabrowser.gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
