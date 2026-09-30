# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Whether a video's chapters have been identified yet.

A chapter counts as identified when it has a real name. Plenty of embedded
names aren't: "Chapter 01", "(01)00:00:00:000", "Scene 3" and bare numbers
are what an authoring tool writes when nobody named anything, and treating
them as names hid exactly the videos that still needed work.

Videos with a single chapter are two different things: a short one is a
song or an extra; a long one is a concert that was never split, which
needs it more than anything. A single chapter is named after its file to
begin with (source "filename") - a music video's file is usually the
song's name - so it can be found, but that name is only provisional until
someone or something has checked it: Unverified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PLACEHOLDER = re.compile(
    r"""^\s*(?:
        (?:chapter|chap|ch|scene|title|track|part|kapitel|chapitre|cap[ií]tulo)?
            \s*[-_.:#]?\s*\d{1,3}                                  # Chapter 01, 01, Scene 3
      | \(\d+\)\s*\d{1,2}:\d{2}:\d{2}(?:[:.]\d{1,3})?               # (01)00:00:00:000
      | \d{1,2}:\d{2}(?::\d{2})?(?:[:.]\d{1,3})?                    # 00:12:34.000
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# A single-chapter video longer than this is a show nobody has split yet:
# even a long live song rarely passes ten and a half minutes, and a
# festival set of three songs runs twelve.
LONG_SINGLE_SECONDS = 11 * 60

# A name taken from the file's name: searchable, but not yet a real name.
FILENAME_SOURCE = "filename"

NAMED = "named"  # every chapter has a real name
PARTLY = "partly"  # some have
UNNAMED = "unnamed"  # none has
UNSPLIT = "unsplit"  # one chapter, and long: a show never split into songs
UNVERIFIED = "unverified"  # one chapter, short, named only from its file
SINGLE = "single"  # one chapter, short, and really named: nothing to do

# The states that still want work doing.
NEEDS_WORK = (PARTLY, UNNAMED, UNSPLIT, UNVERIFIED)


def is_placeholder(title: str | None) -> bool:
    """Whether a chapter title is only a number dressed as a name."""
    return not (title or "").strip() or bool(_PLACEHOLDER.match(title))


def is_named(chapter) -> bool:
    """A real name: not a placeholder, and not only the file's name."""
    return (chapter.get("source") != FILENAME_SOURCE
            and not is_placeholder(chapter.get("title")))


@dataclass(frozen=True)
class Status:
    state: str
    named: int
    total: int

    @property
    def needs_work(self) -> bool:
        return self.state in NEEDS_WORK

    @property
    def fraction(self) -> float:
        """How far along, 0 to 1, for sorting: a single short video counts
        as done."""
        if self.state == SINGLE:
            return 1.0
        if self.state == UNSPLIT:
            return 0.0
        if self.state == UNVERIFIED:
            return 0.5
        return self.named / max(1, self.total)

    def describe(self) -> str:
        if self.state == NAMED:
            return "All named"
        if self.state == PARTLY:
            return f"{self.named} of {self.total} named"
        if self.state == UNNAMED:
            return "Not named"
        if self.state == UNSPLIT:
            return "Not split"
        if self.state == UNVERIFIED:
            return "Unverified"
        return "—"


def status(video) -> Status:
    chapters = video.get("chapters") or []
    total = len(chapters)
    named = sum(1 for chapter in chapters if is_named(chapter))
    if total <= 1:
        if (video.get("duration") or 0.0) >= LONG_SINGLE_SECONDS:
            return Status(UNSPLIT, named, total)
        return Status(SINGLE if named else UNVERIFIED, named, total)
    if named == total:
        return Status(NAMED, named, total)
    return Status(PARTLY if named else UNNAMED, named, total)
