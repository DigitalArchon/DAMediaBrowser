# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Text from outside - a video's or a folder's name, a server's words, the AI's
notes - is shown as it is, never taken for HTML: Qt reads a tooltip or a
message box's text as HTML whenever it looks like it, so a tag in a name came
out bold, hid part of it, or had Qt load an image from a local file."""

import html
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMessageBox

from mediabrowser.core import outbound
from mediabrowser.gui import links, plain

NAME = '<img src="/etc/hosts"><b>Live</b> at Glass Harbor'


def shows_as_text(tip: str, text: str) -> bool:
    """Whether `tip` is a tooltip that shows `text` as it is (plain.tooltip)."""
    return tip == plain.tooltip(text) and html.escape(text) in tip


def test_no_message_box_takes_its_text_as_html():
    """QMessageBox's own warning, critical and information read their text as HTML
    and have no way to say otherwise: every message goes through plain's."""
    root = Path(__file__).resolve().parent.parent / "mediabrowser" / "gui"
    found = [f"{path.name}:{number}"
             for path in sorted(root.rglob("*.py")) if path.name != "plain.py"
             for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
             if any(f"QMessageBox.{kind}(" in line
                    for kind in ("warning", "critical", "information"))]
    assert not found


def test_a_message_shows_its_text_as_it_is(app, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box))
    plain.warning(None, "Import Catalog", f"That file couldn't be read: {NAME}")
    plain.critical(None, "Export failed", NAME)
    plain.information(None, "No folder chosen", NAME)
    assert [box.textFormat() for box in shown] == [Qt.PlainText] * 3
    assert [box.icon() for box in shown] == [QMessageBox.Warning, QMessageBox.Critical,
                                             QMessageBox.Information]
    assert NAME in shown[0].text()


def test_what_was_sent_shows_its_entries_tooltips_as_text(app):
    from mediabrowser.gui.dialogs.sent_dialog import SentDialog

    outbound.record("MusicBrainz", "https://musicbrainz.org/ws/2/release",
                    f"a search for {NAME}", outbound.CLEAR, "Over https", outbound.FAILED,
                    detail=f"the server said {NAME}")
    dialog = SentDialog()
    try:
        item = dialog.tree.topLevelItem(0)
        assert shows_as_text(item.toolTip(2), f"a search for {NAME}")
        assert shows_as_text(item.toolTip(4), f"the server said {NAME}")
    finally:
        dialog.done(0)


def test_missing_videos_show_their_paths_as_text(app):
    from mediabrowser.gui.dialogs.missing_dialog import MissingDialog

    folder = f"/media/{NAME}"
    dialog = MissingDialog(None, [{"folder": folder, "exists": False,
                                   "videos": [("v1", NAME, f"{folder}/{NAME}.mkv")]}])
    try:
        top = dialog.tree.topLevelItem(0)
        assert shows_as_text(top.toolTip(0), folder)
        assert shows_as_text(top.child(0).toolTip(1), f"{folder}/{NAME}.mkv")
    finally:
        dialog.done(0)


@pytest.mark.parametrize("url", [f"https://www.setlist.fm/{NAME}"])
def test_a_links_address_is_said_as_text(app, url):
    label = QLabel("<a href='https://www.setlist.fm/'>setlist.fm</a>")
    links.guard(label)
    label.linkHovered.emit(url)
    assert shows_as_text(label.toolTip(), links.tip(url))
