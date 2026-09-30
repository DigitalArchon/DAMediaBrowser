# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""How the shelf shows whether a video's chapters are identified yet."""

from __future__ import annotations

from mediabrowser.core import naming

COLOURS = {
    naming.NAMED: "#9ece6a",
    naming.PARTLY: "#e0af68",
    naming.UNNAMED: "#f7768e",
    naming.UNSPLIT: "#f7768e",
    naming.UNVERIFIED: "#e0af68",
    naming.SINGLE: "#565c69",
}

TIPS = {
    naming.NAMED: "Every chapter has a name",
    naming.PARTLY: "Some chapters still have only a number",
    naming.UNNAMED: "No chapter has a name yet",
    naming.UNSPLIT: "A long video in one piece: it hasn't been split into songs",
    naming.UNVERIFIED: (
        "Named only after its file - searchable, but not checked. Mark the name as "
        "checked, rename it, or let Identify Library check it"
    ),
    naming.SINGLE: "A short video in one piece, and named",
}


def colour(state: str) -> str:
    return COLOURS.get(state, "#565c69")
