# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The pure half of the track-matching preview.

Given a video's chapters and a candidate tracklist, decide which title lands
on which chapter and describe the result in a form a table can render
directly. This used to live inside the preview dialog, interleaved with the
Treeview writes, which meant the MusicBrainz and pasted-tracklist paths could
only be checked by opening a window and looking at it.

Nothing here applies anything; building a proposal and accepting one are
deliberately separate steps.
"""

from dataclasses import dataclass

from . import matching, utils

# Marks a title matched only by its position in the list, not by its
# duration lining up - shown in the table, never written into a title.
POSITION_MARKER = "*"

NO_MATCH = "(no match)"

# What a chapter before a song, but belonging to it, is named: the story
# video before Akatsuki, the flag entrance before Road of Resistance. The
# song's own name goes where the band starts.
INTRO_TITLE = "Intro to {title}"

FLAG_POSITION = "position match"
FLAG_NO_MATCH = "no match"
FLAG_UNUSED = "unused track"


@dataclass(frozen=True)
class ProposalRow:
    """One line of the preview table."""

    chapter: str  # "3", "3-4", or "-" for a track that matched nothing
    duration: str  # formatted, or "-"
    proposed: str  # as displayed, so carrying POSITION_MARKER where it applies
    flag: str
    chapter_index: int | None  # where `title` would be written, None if nowhere
    title: str | None  # the title as it would be stored, without the marker
    # Where the tracks are the whole video: how far this chapter starts from
    # where its track should, or where an unused track should have started.
    note: str = ""


# A chapter starting further than this from where its track should is worth
# pointing out: its boundary is probably the thing that's wrong.
OFFSET_NOTE_SECONDS = 10.0


@dataclass(frozen=True)
class Proposal:
    rows: list[ProposalRow]
    summary: str

    def mapping(self) -> list[tuple[int, str]]:
        """(chapter_index, title) for every row that would name something -
        exactly what applying this proposal writes.
        """
        return [
            (r.chapter_index, r.title)
            for r in self.rows
            if r.chapter_index is not None and r.title
        ]


def build(chapters, tracks) -> Proposal:
    """Match `tracks` onto `chapters` and describe the result.

    Chapters carry real durations; tracks may or may not. With durations on
    both sides the alignment is matching.align's job. Without them there is
    nothing to align on, so the fallback is strict position - track 1 names
    chapter 1 - which is right often enough to be worth offering and is
    flagged as the guess it is.
    """
    n_chapters = len(chapters)
    chapter_durations = [ch["end"] - ch["start"] for ch in chapters]
    track_durations = [t["length"] for t in tracks]
    has_durations = any(d is not None for d in track_durations)
    video_duration = chapters[-1]["end"] if chapters else 0.0
    # Tracks that are the whole video are placed by where each one plays,
    # which doesn't depend on the chapters' boundaries being right.
    whole_video = has_durations and matching.covers_whole_video(
        track_durations, video_duration
    )
    expected = matching.expected_starts(track_durations) if whole_video else []

    if whole_video:
        track_to_chapters, unused_tracks, confidence = matching.align_by_overlap(
            [(ch["start"], ch["end"]) for ch in chapters], track_durations
        )
    elif has_durations:
        track_to_chapters, unused_tracks, confidence = matching.align(
            chapter_durations, track_durations
        )
    else:
        track_to_chapters = {i: [i] for i in range(min(n_chapters, len(tracks)))}
        unused_tracks = set(range(len(tracks))) - set(track_to_chapters)
        confidence = {}

    # A track's title goes to the first chapter of its matched group only. The
    # rest of the group (an intro segment folded into the same song, say) keeps
    # whatever it had rather than being given a duplicate of the same name.
    group_head = {
        chapter_indices[0]: (track_idx, chapter_indices)
        for track_idx, chapter_indices in track_to_chapters.items()
    }

    # Pieces of a song that come before where its name went: its intro.
    named_at = {track: indices[0] for track, indices in track_to_chapters.items()}
    owners = {
        i: _owner(chapters[i], tracks, expected)
        for i in range(n_chapters) if expected and i not in group_head
    }
    intro_start = {}
    for i, owner in sorted(owners.items()):
        if owner is not None and named_at.get(owner, -1) > i:
            intro_start.setdefault(owner, chapters[i]["start"])

    rows: list[ProposalRow] = []
    i = 0
    while i < n_chapters:
        if i not in group_head:
            owner = owners.get(i)
            if owner is not None and named_at.get(owner, -1) > i:
                # A piece of a song that comes before where its name went:
                # the song's intro.
                intro = INTRO_TITLE.format(title=tracks[owner]["title"])
                rows.append(ProposalRow(
                    chapter=str(i + 1),
                    duration=utils.format_seconds(chapter_durations[i]),
                    proposed=intro,
                    flag="",
                    chapter_index=i,
                    title=intro,
                ))
            else:
                rows.append(ProposalRow(
                    chapter=str(i + 1),
                    duration=utils.format_seconds(chapter_durations[i]),
                    proposed=NO_MATCH,
                    flag=FLAG_NO_MATCH,
                    chapter_index=None,
                    title=None,
                    note=f"part of {tracks[owner]['title']}" if owner is not None else "",
                ))
            i += 1
            continue

        track_idx, chapter_indices = group_head[i]
        last = chapter_indices[-1]
        by_position = confidence.get(track_idx) == "position"
        title = tracks[track_idx]["title"]
        rows.append(ProposalRow(
            chapter=str(i + 1) if len(chapter_indices) == 1 else f"{i + 1}-{last + 1}",
            duration=utils.format_seconds(sum(chapter_durations[c] for c in chapter_indices)),
            proposed=(POSITION_MARKER if by_position else "") + title,
            flag=FLAG_POSITION if by_position else "",
            chapter_index=i,
            title=title,
            # With an intro named, it's the intro that should start where
            # the track does; the song itself starts later by design.
            note=_offset_note(
                intro_start.get(track_idx, chapters[i]["start"]) - expected[track_idx]
            ) if expected else "",
        ))
        i = last + 1

    for j in sorted(unused_tracks):
        rows.append(ProposalRow(
            chapter="-",
            duration="-",
            proposed=tracks[j]["title"],
            flag=FLAG_UNUSED,
            chapter_index=None,
            title=None,
            note=f"should start at {utils.format_seconds(expected[j])}" if expected else "",
        ))

    return Proposal(rows=rows, summary=_summarise(
        n_chapters, len(tracks), confidence, len(unused_tracks), has_durations, whole_video
    ))


def _owner(chapter, tracks, expected) -> int | None:
    """For a chapter no track was given to, the track it's a piece of - an
    intro, or a song the estimate cut in two - if most of it is in one."""
    best, most = None, 0.0
    for j, start in enumerate(expected):
        end = start + (tracks[j]["length"] or 0.0)
        overlap = min(chapter["end"], end) - max(chapter["start"], start)
        if overlap > most:
            best, most = j, overlap
    if best is None or most < 0.5 * (chapter["end"] - chapter["start"]):
        return None
    return best


def _offset_note(offset: float) -> str:
    if abs(offset) < OFFSET_NOTE_SECONDS:
        return ""
    return f"starts {utils.format_seconds(abs(offset))} {'late' if offset > 0 else 'early'}"


def _summarise(n_chapters, n_tracks, confidence, n_unused, has_durations,
               whole_video=False) -> str:
    if whole_video:
        named = len(confidence)
        text = (
            f"{n_chapters} chapter(s), {n_tracks} track(s) making up the whole video: "
            f"each track named the chapter where it plays. {named} named, {n_unused} unused."
        )
        if n_unused:
            text += (
                " No chapter starts where the unused tracks should - split the video "
                "there to give them one, or create the chapters from the track lengths "
                "instead."
            )
        return text
    if not has_durations:
        return (
            f"{n_chapters} chapter(s), {n_tracks} track(s). "
            "No durations given - matched by position only."
        )
    by_time = sum(1 for c in confidence.values() if c == "time")
    by_position = sum(1 for c in confidence.values() if c == "position")
    return (
        f"{n_chapters} chapter(s), {n_tracks} track(s): "
        f"{by_time} matched by time, {by_position} by position ({POSITION_MARKER}), "
        f"{n_unused} unused."
    )
