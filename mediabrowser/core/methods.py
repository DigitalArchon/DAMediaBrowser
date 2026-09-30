# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Which way to give a video its chapters and names, from what there is to
go on.

Three ways, each best in its own case:

- NAME the video's existing chapters from a tracklist. For chapters a disc
  or file came with - their author put them where the songs are - and for
  chapters someone has already estimated and fixed by hand.
- Place chapters by the tracklist's LENGTHS. When the tracks are the whole
  video, their running total is where each song starts, to within a second
  or two: better than any estimate, and the audio can then split each
  song's intro off.
- DETECT chapters from the audio. With a tracklist of titles only, it knows
  how many songs there are and names them in order; with nothing, it works
  out the count itself.
"""

from . import library, matching

NAME = "name"
LENGTHS = "lengths"
DETECT = "detect"


def _has_lengths(tracks) -> bool:
    return bool(tracks) and all(t.get("length") for t in tracks)


def _whole_video(video, tracks) -> bool:
    return matching.covers_whole_video([t.get("length") for t in tracks], video["duration"])


def _own_chapters(video) -> bool:
    """Chapters worth keeping: more than one, and not an estimate."""
    return (
        len(video["chapters"]) > 1
        and video.get("chapter_origin") != library.ORIGIN_ESTIMATED
    )


def available(video, tracks, can_analyse: bool) -> list[str]:
    """The methods that can do anything with this video and tracklist."""
    methods = []
    if tracks and len(video["chapters"]) > 1:
        methods.append(NAME)
    if _has_lengths(tracks):
        methods.append(LENGTHS)
    if can_analyse:
        methods.append(DETECT)
    return methods


def choose(video, tracks, can_analyse: bool) -> str | None:
    """The method most likely to give the best result, or None if there's
    nothing useful to do (a disc's chapters, with no tracklist to name them).
    """
    tracks = tracks or []
    if tracks and _has_lengths(tracks) and _whole_video(video, tracks):
        # A disc's own chapters stay where their author put them; anything
        # else - one whole-file chapter, or an estimate - is bettered by
        # the lengths.
        return NAME if _own_chapters(video) else LENGTHS
    if tracks and len(video["chapters"]) > 1:
        # Estimated chapters someone may have fixed by hand are named, not
        # thrown away, unless the lengths can place better ones (above).
        return NAME
    if can_analyse and (tracks or not _own_chapters(video)):
        return DETECT
    if _has_lengths(tracks):
        return LENGTHS
    return None
