# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Text shown exactly as it is.

Qt takes a tooltip or a message for HTML whenever it looks like it, so a
file called <b>Live</b>.mkv came out bold and one with a stray tag lost
part of its name. Labels say setTextFormat(Qt.PlainText); tooltips can't,
so theirs is marked up as text on purpose.
"""

from __future__ import annotations

import html


def tooltip(text: str) -> str:
    """`text` as a tooltip that shows it as it is, line breaks and all."""
    return "<p style='white-space:pre-wrap'>" + html.escape(text) + "</p>"
