# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Changing where chapters are, as opposed to what they are called.

Estimated chapters are only ever roughly right, and a tracklist's lengths
can drift from the video by a few seconds, so the boundaries need to be
repairable by hand: split a chapter where a song really starts (usually at
wherever playback has got to), merge one that should not have been split,
and nudge a start a second at a time.

Every operation returns a new chapter list and leaves the one it was given
alone, along with a function mapping an old chapter index to its new one -
the queue and the now-playing state refer to chapters by index, and need
to keep pointing at the same music afterwards.
"""

from .chaptergen import MIN_CHAPTER_SECONDS


class EditError(ValueError):
    pass


def _renumbered(chapters):
    for i, chapter in enumerate(chapters):
        chapter["index"] = i
    return chapters


def chapter_at(chapters, position: float) -> int | None:
    """The index of the chapter playing at `position` seconds."""
    for i, chapter in enumerate(chapters):
        if chapter["start"] <= position < chapter["end"]:
            return i
    if chapters and position >= chapters[-1]["end"]:
        return len(chapters) - 1
    return None


def split(chapters, position: float):
    """Split whichever chapter `position` falls in, there.

    The first half keeps the name - it is usually the song that was already
    playing - and the second half starts out numbered. Its start was put
    there by a person, so it is no longer an estimate.
    """
    index = chapter_at(chapters, position)
    if index is None:
        raise EditError("that position is not inside any chapter")
    chapter = chapters[index]
    if (position - chapter["start"] < MIN_CHAPTER_SECONDS
            or chapter["end"] - position < MIN_CHAPTER_SECONDS):
        raise EditError("too close to where this chapter already starts or ends")

    first = dict(chapter, end=position)
    if chapter.get("source") == "filename":
        # The file's name was for the whole video, not its first part.
        first.update(title=None, source="auto-numbered")
    second = {
        "start": position,
        "end": chapter["end"],
        "title": None,
        "source": "auto-numbered",
    }
    result = [dict(c) for c in chapters[:index]] + [first, second] + [
        dict(c) for c in chapters[index + 1:]
    ]
    return _renumbered(result), (lambda old: old if old <= index else old + 1)


def merge_with_next(chapters, index: int):
    """Fold the chapter after `index` into it. The merged chapter keeps the
    first one's name, or the second's if only that one had a name.
    """
    if not 0 <= index < len(chapters) - 1:
        raise EditError("there is no next chapter to merge with")
    first, second = chapters[index], chapters[index + 1]
    merged = dict(first, end=second["end"])
    if not first["title"] and second["title"]:
        merged["title"] = second["title"]
        merged["source"] = second.get("source", "manual")
    result = [dict(c) for c in chapters[:index]] + [merged] + [
        dict(c) for c in chapters[index + 2:]
    ]
    return _renumbered(result), (lambda old: old if old <= index else old - 1)


def move_start(chapters, index: int, delta: float):
    """Move where chapter `index` starts (and so where the one before it
    ends) by `delta` seconds, keeping both at least MIN_CHAPTER_SECONDS long.
    """
    if not 0 < index < len(chapters):
        raise EditError("the first chapter always starts at the beginning")
    before, chapter = chapters[index - 1], chapters[index]
    start = chapter["start"] + delta
    start = max(before["start"] + MIN_CHAPTER_SECONDS, start)
    start = min(chapter["end"] - MIN_CHAPTER_SECONDS, start)
    if start == chapter["start"]:
        raise EditError("the chapter can't move any further that way")
    result = [dict(c) for c in chapters]
    result[index - 1]["end"] = start
    result[index]["start"] = start
    result[index].pop("estimated", None)
    return _renumbered(result), (lambda old: old)
