# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Marking a video's songs by hand, while it plays.

For a video nothing can help with - no chapters, no tracklist anywhere, a
show no database lists - the only way is to watch it: play, find where each
song starts, mark it, and say what it is. A mark sheet is what that works
on. It holds the starts marked so far and their names, and turns them into
chapters when the person is happy.

Names can come first. Someone who knows the setlist types it in order, and
each mark they then make takes the next name that isn't on a mark yet - so
marking through the show names it as it goes. Names can also be put on
marks in order all at once, for marks made before the names were.

Every operation leaves the sheet in time order with a mark at 0; nothing
here is applied to a video until chapters() is asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from . import chaptergen, utils

# Marks closer together than this are the same moment, not two songs.
MIN_GAP_SECONDS = 2.0


class MarkError(ValueError):
    pass


@dataclass(frozen=True)
class Mark:
    start: float
    title: str | None = None


class MarkSheet:
    def __init__(self, duration: float, chapters=None) -> None:
        """A sheet for a video `duration` seconds long, starting from its
        `chapters` if given (to refine them) or from a single mark at 0."""
        self.duration = float(duration)
        self._marks: list[Mark] = [Mark(0.0)]
        self._names: list[str] = []
        for chapter in chapters or []:
            if chapter.get("source") == "filename":
                # Named after the whole file, not a song in it.
                chapter = dict(chapter, title=None)
            if chapter["start"] > 0.0:
                self._marks.append(Mark(float(chapter["start"]), chapter.get("title")))
            elif chapter.get("title"):
                self._marks[0] = Mark(0.0, chapter["title"])
        self._marks.sort(key=lambda m: m.start)

    # --- reading ---------------------------------------------------------

    def marks(self) -> list[Mark]:
        return list(self._marks)

    def __len__(self) -> int:
        return len(self._marks)

    def names(self) -> list[str]:
        """The names typed in order, whether on a mark yet or not."""
        return list(self._names)

    def unused_names(self) -> list[str]:
        """Names in order that no mark has yet, in order."""
        used = [m.title for m in self._marks if m.title]
        remaining = []
        for name in self._names:
            if name in used:
                used.remove(name)
            else:
                remaining.append(name)
        return remaining

    def next_name(self) -> str | None:
        """What the next mark will be called."""
        unused = self.unused_names()
        return unused[0] if unused else None

    def at(self, position: float) -> int:
        """The index of the mark in force at `position`: the last one at or
        before it."""
        index = 0
        for i, mark in enumerate(self._marks):
            if mark.start <= position:
                index = i
        return index

    def length_of(self, index: int) -> float:
        end = self._marks[index + 1].start if index + 1 < len(self._marks) else self.duration
        return end - self._marks[index].start

    # --- changing --------------------------------------------------------

    def _check(self, position: float, ignore: int | None = None) -> float:
        position = float(position)
        if not 0.0 <= position < self.duration - MIN_GAP_SECONDS:
            raise MarkError("that's outside the video")
        for i, mark in enumerate(self._marks):
            if i != ignore and abs(mark.start - position) < MIN_GAP_SECONDS:
                raise MarkError(
                    f"there is already a mark at {utils.format_seconds(mark.start)}"
                )
        return position

    def mark(self, position: float, title: str | None = None) -> int:
        """Mark a song start at `position`; returns the mark's index.
        Without a title it takes the next unused name, if there is one."""
        position = self._check(position)
        title = (title or "").strip() or self.next_name()
        self._marks.append(Mark(position, title))
        self._marks.sort(key=lambda m: m.start)
        return self.at(position)

    def remove(self, index: int) -> None:
        """Take a mark away. The first can't go: the video starts there."""
        if index == 0:
            raise MarkError("the first mark is where the video starts")
        if not 0 < index < len(self._marks):
            raise MarkError("no such mark")
        del self._marks[index]

    def move(self, index: int, position: float) -> int:
        """Put mark `index` at `position` instead; returns its new index."""
        if index == 0:
            raise MarkError("the first mark is where the video starts")
        if not 0 < index < len(self._marks):
            raise MarkError("no such mark")
        position = self._check(position, ignore=index)
        self._marks[index] = replace(self._marks[index], start=position)
        self._marks.sort(key=lambda m: m.start)
        return self.at(position)

    def nudge(self, index: int, delta: float) -> int:
        return self.move(index, self._marks[index].start + delta)

    def rename(self, index: int, title: str | None) -> None:
        if not 0 <= index < len(self._marks):
            raise MarkError("no such mark")
        self._marks[index] = replace(self._marks[index], title=(title or "").strip() or None)

    def clear(self) -> None:
        """Start again: one mark at 0, unnamed. The names in order stay."""
        self._marks = [Mark(0.0)]

    def set_names(self, names) -> None:
        """The names in order, as typed: one per line, blanks ignored."""
        if isinstance(names, str):
            names = names.splitlines()
        self._names = [n.strip() for n in names if n and n.strip()]

    def apply_names_in_order(self, from_index: int = 0) -> int:
        """Put the names in order onto the marks from `from_index` on, the
        first name on that mark, replacing what they had. Returns how many
        marks were named."""
        count = 0
        for offset, name in enumerate(self._names):
            index = from_index + offset
            if index >= len(self._marks):
                break
            self._marks[index] = replace(self._marks[index], title=name)
            count += 1
        return count

    # --- result ----------------------------------------------------------

    def chapters(self) -> list[dict]:
        return chaptergen.chapters_from_starts(
            [m.start for m in self._marks], self.duration,
            [m.title or "" for m in self._marks], source="manual",
        )
