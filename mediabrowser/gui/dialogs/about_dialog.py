# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Help → About: what this is, whose it is, and the licence it comes under.

The GPL asks an interactive program to show its copyright, that there is
no warranty, and where the licence is - this is where.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget

from mediabrowser.core import config

LICENSE_URL = "https://www.gnu.org/licenses/gpl-3.0.html"


def about_text() -> str:
    return (
        f"<h3>DA Media Browser {config.VERSION}</h3>"
        "<p>Browse, name and play the chapters of video files and Blu-ray discs.</p>"
        f'<p><a href="{config.PROJECT_URL}">{config.PROJECT_URL}</a></p>'
        f"<p>Copyright © {config.COPYRIGHT}</p>"
        "<p>This program is free software: you can redistribute it and/or modify it "
        "under the terms of the GNU General Public License as published by the Free "
        "Software Foundation, either version 3 of the License, or (at your option) any "
        "later version.</p>"
        "<p>This program is distributed in the hope that it will be useful, but "
        "<b>without any warranty</b>; without even the implied warranty of "
        "merchantability or fitness for a particular purpose. See the "
        f'<a href="{LICENSE_URL}">GNU General Public License</a> for more details.</p>'
        '<p>Tracklists from <a href="https://musicbrainz.org">MusicBrainz</a> and covers '
        'from the <a href="https://coverartarchive.org">Cover Art Archive</a>. Plays with '
        '<a href="https://mpv.io">mpv</a>; reads with '
        '<a href="https://ffmpeg.org">FFmpeg</a> and libbluray.</p>'
    )


def show_about(parent: QWidget) -> None:
    QMessageBox.about(parent, "About DA Media Browser", about_text())
