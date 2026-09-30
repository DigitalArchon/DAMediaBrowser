# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Help → About shows what the GPL asks an interactive program to: the
copyright, that there's no warranty, and the licence."""

from pathlib import Path

import pytest

from mediabrowser.core import config

PySide6 = pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parent.parent


def test_it_gives_the_version_copyright_warranty_and_licence():
    from mediabrowser.gui.dialogs.about_dialog import LICENSE_URL, about_text

    text = about_text()
    assert config.VERSION in text and config.COPYRIGHT in text
    assert "without any warranty" in text and "version 3" in text and LICENSE_URL in text


def test_it_is_on_the_help_menu(window, monkeypatch):
    from mediabrowser.gui.dialogs import about_dialog

    shown = []
    monkeypatch.setattr(about_dialog.QMessageBox, "about", lambda *a: shown.append(a))
    [action] = [a for a in window.menuBar().actions() if a.text() == "&Help"][0].menu().actions()
    action.trigger()
    assert shown


def test_every_source_file_carries_the_licence():
    assert "GNU GENERAL PUBLIC LICENSE" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert 'license = "GPL-3.0-or-later"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    header = "SPDX-License-Identifier: GPL-3.0-or-later"
    missing = [str(path) for folder in ("mediabrowser", "tests")
               for path in (ROOT / folder).rglob("*.py")
               if header not in path.read_text(encoding="utf-8")]
    assert not missing
