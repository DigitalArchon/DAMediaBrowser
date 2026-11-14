# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Text shown exactly as it is.

Qt takes a tooltip or a message for HTML whenever it looks like it, so a
file called <b>Live</b>.mkv came out bold and one with a stray tag lost
part of its name - or had Qt load an image from a local file. Labels say
setTextFormat(Qt.PlainText); tooltips can't, so theirs is marked up as text
on purpose; and a message box is shown by warning(), critical() and
information() here, never QMessageBox's own, which take their text as HTML
too and offer no way to say otherwise.
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox


def tooltip(text: str) -> str:
    """`text` as a tooltip that shows it as it is, line breaks and all."""
    return "<p style='white-space:pre-wrap'>" + html.escape(text) + "</p>"


def message(parent, icon, title: str, text: str) -> None:
    """A message box with an OK button that shows `text` as it is."""
    box = QMessageBox(icon, title, text, QMessageBox.Ok, parent)
    box.setTextFormat(Qt.PlainText)
    box.exec()


def warning(parent, title: str, text: str) -> None:
    message(parent, QMessageBox.Warning, title, text)


def critical(parent, title: str, text: str) -> None:
    message(parent, QMessageBox.Critical, title, text)


def information(parent, title: str, text: str) -> None:
    message(parent, QMessageBox.Information, title, text)


def question(parent, title: str, text: str, buttons, default):
    """QMessageBox.question's answer, the text shown as it is: an address typed into
    Settings may hold what Qt would otherwise take for markup."""
    return question_box(parent, title, text, buttons, default).exec()


def question_box(parent, title: str, text: str, buttons, default) -> QMessageBox:
    """The box question() asks with: plain text, `default` the default button."""
    box = QMessageBox(QMessageBox.Question, title, text, buttons, parent)
    box.setTextFormat(Qt.PlainText)
    box.setDefaultButton(default)
    return box
