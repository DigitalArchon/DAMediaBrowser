# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The core must stay free of any GUI toolkit.

That separation is what let this app move from Tkinter to Qt by rewriting
only the window, and it is worth keeping: a stray `from PySide6 import ...`
in core/ costs nothing today and everything the next time the UI changes.
Borrowed from StoryLoom's test of the same name.
"""

import ast
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / "mediabrowser" / "core"

FORBIDDEN = {"PySide6", "shiboken6", "tkinter"}


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_core_imports_no_gui_toolkit():
    offenders = {}
    for path in sorted(CORE.glob("*.py")):
        banned = _imported_roots(path) & FORBIDDEN
        if banned:
            offenders[path.name] = sorted(banned)
    assert not offenders, f"GUI toolkit imported inside core/: {offenders}"


def test_core_is_not_empty():
    # Guards the test above against silently passing on a moved or renamed
    # package, which would make it assert nothing at all.
    assert len(list(CORE.glob("*.py"))) > 5
