# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""DA Media Browser inside firejail. Under firejail the desktop's own file dialog took the
whole app down at the first one (Ubuntu 26.04, firejail 0.9.72, Nov 2026): the portal refuses
a sandboxed process, Qt fell back to GTK's dialog, and GTK aborted when firejail wouldn't let
its icon loader start `bwrap`. Inside firejail Qt uses its own dialogs. Checked live on the
lab's Ubuntu; here, that the switch is made when, and only when, firejail says it is there,
and that build_app makes it before any dialog can be made."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QCoreApplication, Qt

from mediabrowser.gui import app as gui_app
from mediabrowser.gui.app import in_firejail, own_dialogs_in_firejail

_ORDER: list = []


@pytest.fixture
def native_dialogs_afterwards():
    yield
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, False)


def test_firejail_is_known_by_what_it_says_in_container():
    assert in_firejail({"container": "firejail"})
    assert not in_firejail({})
    # A Flatpak's portal works: it keeps the desktop's dialogs.
    assert not in_firejail({"container": "flatpak"})


def test_inside_firejail_every_dialog_is_qts_own(native_dialogs_afterwards):
    assert not own_dialogs_in_firejail({"container": "flatpak"})
    assert not QCoreApplication.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
    assert own_dialogs_in_firejail({"container": "firejail"})
    assert QCoreApplication.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)


def test_build_app_makes_the_switch_before_anything_else_is_made(monkeypatch):
    """From the environment it is given (the process's own by default), before Qt's settings
    are set aside and the application made."""
    order = _ORDER
    order.clear()
    monkeypatch.setattr(gui_app, "own_dialogs_in_firejail", lambda env: order.append(dict(env)))
    monkeypatch.setattr(gui_app.file_dialogs, "settings_aside", lambda: order.append("aside"))
    monkeypatch.setattr(gui_app, "QApplication", _NoApp)
    monkeypatch.setattr(gui_app, "pick_font", lambda app: None)
    gui_app.build_app(["mediabrowser"], env={"container": "firejail"})
    assert order == [{"container": "firejail"}, "aside", "made"]


class _NoApp:
    """QApplication as build_app uses it, the test's own application being there already."""

    def __init__(self, _argv):
        _ORDER.append("made")

    @staticmethod
    def setDesktopFileName(_name):  # noqa: N802 - Qt's name
        pass

    @staticmethod
    def setAttribute(*_args):  # noqa: N802 - Qt's name
        pass

    def setStyleSheet(self, _sheet):  # noqa: N802 - Qt's name
        pass

    def palette(self):
        from PySide6.QtGui import QPalette

        return QPalette()

    def setPalette(self, _palette):  # noqa: N802 - Qt's name
        pass
