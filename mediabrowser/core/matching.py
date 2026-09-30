# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Align a video's chapters to a MusicBrainz release's tracks, trusting
track ORDER over individual track durations.

In practice MusicBrainz almost always has the track order right, and gets
most individual track durations right too - but not all of them:

- A track's official duration sometimes includes a fluff chapter (spoken
  intro, count-in, applause outro) that the Blu-ray author split off as
  its own chapter, so that one track's duration won't line up with any
  single chapter.
- A release sometimes lists several discs' worth of tracks as one long
  combined list (e.g. a 3-concert box set returning all 3 setlists when
  you only want disc 1), so there can be more tracks than this video
  actually has chapters for.

So instead of the old approach (grouping consecutive chapters and summing
their durations to try to match a track's official length), this trusts
order first:

Phase 1 - anchor by time: pick the largest set of (chapter, track) pairs
whose durations agree within TIME_TOLERANCE_SECONDS while keeping both
sides in order. Ties go to the alignment that skips the fewest tracks
between its first and last anchor - surplus tracks from another disc sit
before or after this video's run, not in the middle of it (a two-night
release repeats most of a setlist, so the other night can offer just as
many matches) - and after that to the smallest total duration error. A track
with no matching chapter is simply left for now - a later track may still
match further down the disc, which is exactly what should happen when
it's this track's duration (not the order) that's wrong.

This is solved as a whole rather than greedily track by track: a greedy
"first chapter that fits" scan lets one coincidental match far down the
disc (say, the opener's official length happening to equal the encore's
chapter) consume every chapter before it, and nothing else can anchor.

Phase 2 - fill the gaps by position: whatever chapters and tracks are
left over between two time-anchors (or before the first / after the last)
are assigned to each other purely by position, on the assumption that
order is trustworthy even when duration isn't:
  - equal counts on both sides: paired up 1:1.
  - more leftover chapters than tracks: assumed to be fluff chapters
    padding out a smaller number of real songs, so the leftover chapters
    are split into contiguous groups (one group per leftover track) and
    every chapter in a group gets that track's name.
  - more leftover tracks than chapters: a likely sign the release listed
    extra tracks that belong to a different disc. There's nowhere for the
    surplus to go, so those tracks are simply left unused.
Everything placed this way is flagged "position" (as opposed to "time")
so the caller can mark it as a lower-confidence guess (e.g. a "*" prefix)
for the user to manually verify.

Phase 3 - fluff filter: a leftover chapter under MIN_SONG_SECONDS is
assumed to be non-song filler by default (almost no real song is that
short) and is dropped from the position-filling pool entirely, rather
than being forced to absorb a track name.

When the tracks are the whole video - a live album's CDs cut from the same
master as its Blu-ray, their lengths adding up to within seconds of it -
there is a better way than any of that: align_by_overlap(), at the end,
places each track where its running total says it plays.
"""

TIME_TOLERANCE_SECONDS = 5.0
MIN_SONG_SECONDS = 120.0


def _find_time_anchors(chapter_durations, track_durations):
    """Phase 1: the best in-order set of single-chapter time matches.

    Returns a list of (chapter_index, track_index) pairs, both strictly
    increasing.
    """
    n_chapters = len(chapter_durations)
    n_tracks = len(track_durations)

    def error(c, t):
        track_duration = track_durations[t]
        if track_duration is None:
            return None
        diff = abs(chapter_durations[c] - track_duration)
        return diff if diff <= TIME_TOLERANCE_SECONDS else None

    # Scores are (anchors, -tracks skipped between anchors, -total error),
    # compared as tuples so each only breaks ties in the ones before it.
    # started[c][t] scores chapters[c:] against tracks[t:] once an anchor has
    # already been placed, where skipping a track costs; fresh[c][t] is the
    # same before any anchor, where it doesn't. Stopping is always allowed,
    # so trailing tracks are never counted as skipped either.
    stop = ((0, 0, 0.0), None)
    started = [[stop] * (n_tracks + 1) for _ in range(n_chapters + 1)]
    fresh = [[stop] * (n_tracks + 1) for _ in range(n_chapters + 1)]
    for c in range(n_chapters - 1, -1, -1):
        for t in range(n_tracks - 1, -1, -1):
            match = None
            err = error(c, t)
            if err is not None:
                (a, s, e), _ = started[c + 1][t + 1]
                match = ((a + 1, s, e - err), "match")
            for table, skip_cost in ((started, 1), (fresh, 0)):
                (a, s, e), _ = table[c][t + 1]
                # max() keeps the first of equals, so a full tie goes to
                # matching here rather than to a later chapter that fits
                # just as well.
                options = [] if match is None else [match]
                options += [
                    (table[c + 1][t][0], "chapter"),
                    ((a, s - skip_cost, e), "track"),
                    stop,
                ]
                table[c][t] = max(options, key=lambda option: option[0])

    anchors = []
    table = fresh
    c = t = 0
    while c < n_chapters and t < n_tracks:
        move = table[c][t][1]
        if move is None:
            break
        if move == "match":
            anchors.append((c, t))
            table = started
            c += 1
            t += 1
        elif move == "chapter":
            c += 1
        else:
            t += 1
    return anchors


def _split_into_groups(items, n_groups):
    """Splits items into n_groups ordered, contiguous groups.

    Fewer items than groups: one item per group, trailing groups empty
    (there's nowhere for those extra groups' tracks to go). More items
    than groups: split into n_groups contiguous chunks, with any
    remainder chapters going to the earliest groups.
    """
    if n_groups <= 0:
        return []
    if len(items) <= n_groups:
        groups = [[item] for item in items]
        groups += [[] for _ in range(n_groups - len(items))]
        return groups
    base, remainder = divmod(len(items), n_groups)
    groups = []
    pos = 0
    for g in range(n_groups):
        size = base + (1 if g < remainder else 0)
        groups.append(items[pos:pos + size])
        pos += size
    return groups


def align(chapter_durations, track_durations):
    """Returns (track_to_chapters, unused_track_indices, confidence).

    track_to_chapters: {track_index: [chapter_index, ...]} - one or more
        consecutive chapter indices assigned to that track.
    unused_track_indices: set of track indices with nowhere to go.
    confidence: {track_index: "time" | "position"}.
    """
    n_chapters = len(chapter_durations)
    n_tracks = len(track_durations)

    anchors = _find_time_anchors(chapter_durations, track_durations)

    track_to_chapters = {}
    confidence = {}
    for chapter_index, track_index in anchors:
        track_to_chapters[track_index] = [chapter_index]
        confidence[track_index] = "time"

    boundaries = [(-1, -1)] + anchors + [(n_chapters, n_tracks)]
    for k in range(len(boundaries) - 1):
        prev_chapter, prev_track = boundaries[k]
        next_chapter, next_track = boundaries[k + 1]

        gap_tracks = list(range(prev_track + 1, next_track))
        if not gap_tracks:
            continue

        gap_chapters = list(range(prev_chapter + 1, next_chapter))
        real_chapters = [
            c for c in gap_chapters if chapter_durations[c] >= MIN_SONG_SECONDS
        ]
        if not real_chapters:
            continue

        groups = _split_into_groups(real_chapters, len(gap_tracks))
        for track_index, chapter_group in zip(gap_tracks, groups, strict=True):
            if not chapter_group:
                continue
            track_to_chapters[track_index] = chapter_group
            confidence[track_index] = "position"

    unused_tracks = set(range(n_tracks)) - set(track_to_chapters.keys())
    return track_to_chapters, unused_tracks, confidence


# --- when the tracks are the whole video --------------------------------

# How close a tracklist's total must be to the video's length for the tracks
# to be taken as the video itself, start to end.
WHOLE_VIDEO_FRACTION = 0.03
WHOLE_VIDEO_MIN_SECONDS = 60.0

# A chapter must overlap this much of a track's expected span - or be this
# much inside it - to be named after it at all.
MIN_OVERLAP_FRACTION = 0.5
# A chapter holding at least this much of a track, or nearly all of a short
# one, is the song itself rather than an intro or a stray piece of it.
SUBSTANTIAL_SECONDS = MIN_SONG_SECONDS
SUBSTANTIAL_FRACTION = 0.8


def covers_whole_video(track_durations, video_duration) -> bool:
    """Whether these tracks, end to end, are the whole video: every one has a
    length and together they come to within a few percent of it.
    """
    if not track_durations or any(d is None for d in track_durations):
        return False
    total = sum(track_durations)
    tolerance = max(WHOLE_VIDEO_MIN_SECONDS, WHOLE_VIDEO_FRACTION * video_duration)
    return abs(total - video_duration) <= tolerance


def align_by_overlap(chapter_spans, track_durations):
    """Name chapters by where each track should be, when the tracks are the
    whole video (covers_whole_video).

    Each track's running total says where in the video it plays, and it is
    given to a chapter covering that span, in order and one to one. Unlike
    align(), which compares each chapter's length with each track's, this
    doesn't depend on the chapter boundaries being right - so it names
    chapters estimated from the audio, whose boundaries can be half a minute
    out, and whose lengths match a different song's by chance as often as
    not.

    Of the chapters covering a track, the name goes to the first one that
    holds a real part of the song (SUBSTANTIAL_SECONDS of it), not simply
    the biggest: a song the estimate cut into three keeps its name where it
    starts, while a short intro chapter before a song is passed over for
    the song itself.

    Returns (track_to_chapters, unused_track_indices, confidence) like
    align(), each track getting at most one chapter.
    """
    spans = []
    position = 0.0
    for duration in track_durations:
        spans.append((position, position + duration))
        position += duration

    def score(chapter, track):
        (c_start, c_end), (t_start, t_end) = chapter_spans[chapter], spans[track]
        overlap = min(c_end, t_end) - max(c_start, t_start)
        if overlap <= 0:
            return None
        shorter = min(c_end - c_start, t_end - t_start)
        if shorter <= 0 or overlap < MIN_OVERLAP_FRACTION * shorter:
            return None
        substantial = (
            overlap >= SUBSTANTIAL_SECONDS
            or overlap >= SUBSTANTIAL_FRACTION * (t_end - t_start)
        )
        # Compared in order: as many tracks named as possible, then as many
        # of them on a real part of their song, then each on the chapter
        # starting nearest where it should - which also settles a chapter
        # spanning two songs (a missed boundary) on the first of them.
        return (1, int(substantial), -abs(c_start - t_start))

    n, m = len(chapter_spans), len(spans)
    zero = (0, 0, 0.0)
    # best[c][t]: the best total for chapters[c:] against tracks[t:].
    best = [[(zero, None)] * (m + 1) for _ in range(n + 1)]
    for c in range(n - 1, -1, -1):
        for t in range(m - 1, -1, -1):
            options = [(best[c + 1][t][0], "chapter"), (best[c][t + 1][0], "track")]
            pair = score(c, t)
            if pair is not None:
                rest = best[c + 1][t + 1][0]
                total = tuple(a + b for a, b in zip(rest, pair, strict=True))
                options.insert(0, (total, "match"))
            best[c][t] = max(options, key=lambda option: option[0])

    track_to_chapters = {}
    c = t = 0
    while c < n and t < m:
        move = best[c][t][1]
        if move == "match":
            track_to_chapters[t] = [c]
            c += 1
            t += 1
        elif move == "chapter":
            c += 1
        else:
            t += 1

    unused = set(range(m)) - set(track_to_chapters)
    confidence = {t: "time" for t in track_to_chapters}
    return track_to_chapters, unused, confidence


def expected_starts(track_durations) -> list[float]:
    """Where each track starts, by the running total of those before it."""
    starts, position = [], 0.0
    for duration in track_durations:
        starts.append(position)
        position += duration or 0.0
    return starts
