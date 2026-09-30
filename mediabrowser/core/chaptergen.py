# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Chapters for a video that has none of its own.

Two ways to make them, both stored only in the library - the video file is
never written to:

- From a tracklist's lengths. A live album's CD tracks are usually cut from
  the same master as the video, so their running total is where each song
  starts. Checked against a Blu-ray rip with no chapter marks, every
  boundary made this way landed in the gap between two songs.

- Estimated from the audio, when there is no tracklist with lengths. This is
  a starting point for a person to correct, not an answer: songs with quiet
  intros and breakdowns look much like the gaps between songs.

The estimate listens for the bass dropping out. Between songs the kick drum
and bass guitar stop even when a crowd is roaring, so a long, deep drop in
the bass (relative to the music around it) is a gap; a short one is more
likely a breakdown. On a 13-song concert, told how many songs there were,
it found 11 of the 12 boundaries; left to decide for itself it found all 12
and added 6 that were not there - which is the right way round, as merging
two chapters is one click and finding a missed boundary is not.

The stage lighting can be a second opinion (see stage_light). Checked
against five 13-song concerts, a dark stage turned out to say little - the
lights go down for quiet passages inside songs as often as between them, and
favouring dark gaps made the estimate worse - but a quiet stretch where the
stage gets *brighter* is nearly always a staged breakdown within a song.
Marking those down left every real boundary found and cut the chapters
that weren't songs by a fifth (35 to 28). Placing boundaries by the lighting
was no better than by the sound, so the lighting only ever scores.
"""

import math
import statistics

from .audio_levels import Levels

# --- chapters from track lengths --------------------------------------------

# Tracks whose running total starts later than this before the end of the
# video are dropped rather than given a chapter a few seconds long.
MIN_CHAPTER_SECONDS = 1.0

# How far snapping may move a boundary. Further than a few seconds and it
# starts pulling boundaries into the middle of a spoken intro or prelude
# because that happened to be quieter.
SNAP_RADIUS_SECONDS = 3.0


def usable_media(media) -> list[int]:
    """Indices of the media whose every track has a length."""
    return [
        i for i, medium in enumerate(media)
        if medium["tracks"] and all(t["length"] for t in medium["tracks"])
    ]


def media_total(medium) -> float:
    return sum(t["length"] for t in medium["tracks"])


def pick_media(media, duration: float) -> list[int]:
    """The run of consecutive media whose tracks best add up to `duration`.

    A box set lists every show's CDs and videos as separate media, and one
    video is usually one show: two CDs next to each other, say. Media with
    missing lengths (typically the video discs themselves) break a run.
    Returns [] when nothing has lengths.
    """
    usable = set(usable_media(media))
    best: list[int] = []
    best_error = math.inf
    for start in range(len(media)):
        total = 0.0
        for end in range(start, len(media)):
            if end not in usable:
                break
            total += media_total(media[end])
            error = abs(total - duration)
            if error < best_error:
                best, best_error = list(range(start, end + 1)), error
    return best


def chapters_from_tracks(tracks, duration: float, source: str) -> list[dict]:
    """One chapter per track, starting at the running total of the lengths
    before it. The last chapter runs to the end of the video, whatever the
    lengths add up to, so nothing is left outside a chapter.
    """
    starts = []
    titles = []
    position = 0.0
    for track in tracks:
        if position > duration - MIN_CHAPTER_SECONDS:
            break
        starts.append(position)
        titles.append(track["title"])
        position += track["length"] or 0.0
    return chapters_from_starts(starts, duration, titles, source=source)


def chapters_from_starts(starts, duration: float, titles=None, source="manual",
                         estimated: bool = False) -> list[dict]:
    """Chapters from their start times. Titles, where given, name them in
    order; any beyond the list are left numbered.
    """
    titles = list(titles or [])
    starts = sorted(starts) or [0.0]
    starts[0] = 0.0
    chapters = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else duration
        title = (titles[i] or "").strip() if i < len(titles) else ""
        chapter = {
            "index": i,
            "start": start,
            "end": end,
            "title": title or None,
            "source": source if title else "auto-numbered",
        }
        if estimated and i > 0:
            chapter["estimated"] = True
        chapters.append(chapter)
    return chapters


def snap_starts(starts, levels: Levels, radius: float = SNAP_RADIUS_SECONDS) -> list[float]:
    """Move each start (the first stays at 0) to the quietest moment within
    `radius` seconds, never past its neighbours.
    """
    snapped = list(starts)
    full = levels.full
    step = levels.step
    for i in range(1, len(snapped)):
        centre = snapped[i]
        low = max(snapped[i - 1] + MIN_CHAPTER_SECONDS, centre - radius)
        high = centre + radius
        if i + 1 < len(snapped):
            high = min(high, snapped[i + 1] - MIN_CHAPTER_SECONDS)
        first = max(0, math.ceil(low / step))
        last = min(len(full) - 1, math.floor(high / step))
        if first > last:
            continue
        # Quietest, and of equally quiet moments the one nearest where the
        # tracklist put it.
        best = min(
            range(first, last + 1),
            key=lambda k: (full[k], abs(k * step - centre)),
        )
        snapped[i] = best * step
    return snapped


# --- estimating from the audio ----------------------------------------------

# How far below the surrounding music the bass must drop to count as quiet.
QUIET_DB = 12.0
# "The surrounding music": this percentile of the smoothed bass within this
# many seconds either side. A percentile rather than an average, so a quiet
# song does not make its own quiet passages look normal.
REFERENCE_WINDOW_SECONDS = 120.0
REFERENCE_PERCENTILE = 0.75
# Shorter quiet stretches are ignored outright, and ones this close together
# are one gap interrupted by a clap or a shout.
MIN_GAP_SECONDS = 2.0
JOIN_GAPS_WITHIN_SECONDS = 4.0
# Gaps this close to either end are the show starting and finishing, not a
# boundary between songs.
EDGE_SECONDS = 20.0
# No song is shorter than this; boundaries closer together are one of them
# wrong.
MIN_SONG_SECONDS = 60.0
# A gap's score is how deep it is times the square root of how long (capped),
# and without a song count, gaps scoring below this are ignored.
LENGTH_CAP_SECONDS = 60.0
AUTO_THRESHOLD = 90.0
# Where in a gap the boundary goes. A short gap is applause and belongs to
# the song before, so the boundary is where the music comes back. A long one
# is a spoken intro, a prelude or a video interlude, which belongs to the
# song after - so the boundary goes this far into it, past the applause.
APPLAUSE_SECONDS = 20.0


def _smooth(values, radius):
    prefix = [0.0]
    for v in values:
        prefix.append(prefix[-1] + v)
    n = len(values)
    out = []
    for i in range(n):
        lo, hi = max(0, i - radius), min(n, i + radius + 1)
        out.append((prefix[hi] - prefix[lo]) / (hi - lo))
    return out


def _reference(values, radius, stride=4):
    """A rolling percentile. Sampling every `stride`th value keeps this
    quick on a two-hour concert and makes no difference to a percentile of
    several hundred values.
    """
    n = len(values)
    out = []
    for i in range(n):
        window = sorted(values[max(0, i - radius):i + radius + 1:stride])
        out.append(window[int(REFERENCE_PERCENTILE * (len(window) - 1))])
    return out


def find_gaps(levels: Levels):
    """The quiet stretches in the bass: [(start, end, depth_db), ...]."""
    step = levels.step
    bass = _smooth(levels.bass, max(1, round(1.0 / step)))
    reference = _reference(bass, round(REFERENCE_WINDOW_SECONDS / step))
    n = len(bass)

    gaps = []
    i = 0
    while i < n:
        if bass[i] >= reference[i] - QUIET_DB:
            i += 1
            continue
        j = i
        while j < n and bass[j] < reference[j] - QUIET_DB:
            j += 1
        if (j - i) * step >= MIN_GAP_SECONDS:
            depth = sum(reference[k] - bass[k] for k in range(i, j)) / (j - i)
            gaps.append((i * step, j * step, depth))
        i = j

    joined = []
    for start, end, depth in gaps:
        if joined and start - joined[-1][1] <= JOIN_GAPS_WITHIN_SECONDS:
            prev_start, prev_end, prev_depth = joined[-1]
            a, b = prev_end - prev_start, end - start
            joined[-1] = (prev_start, end, (prev_depth * a + depth * b) / (a + b))
        else:
            joined.append((start, end, depth))
    return joined


# The stage lighting in and around a quiet stretch: a frame every
# LIGHT_STEP_SECONDS through it (at most LIGHT_SAMPLES_INSIDE of them), and
# at each of LIGHT_AROUND_SECONDS before and after it. Each frame is a seek,
# about 0.15s over a network share however many run at once, so these are
# the fewest that did as well as ten times as many: about 340 frames for a
# 90-minute concert.
LIGHT_STEP_SECONDS = 2.0
LIGHT_SAMPLES_INSIDE = 4
LIGHT_AROUND_SECONDS = (10, 20, 30)
# How much brighter than its surroundings a quiet stretch must get for its
# score to be at its lowest (a quarter). Brightness is 0-255.
LIGHT_PENALTY_WIDTH = 15.0
LIGHT_PENALTY_FLOOR = 0.25


def _candidate_gaps(levels: Levels, duration: float):
    return [
        (start, end, depth) for start, end, depth in find_gaps(levels)
        if start >= EDGE_SECONDS and end <= duration - EDGE_SECONDS
    ]


def _light_times(start: float, end: float):
    step = max(LIGHT_STEP_SECONDS, (end - start) / LIGHT_SAMPLES_INSIDE)
    count = int((end - start) / step) + 1
    inside = [round(start + i * step, 1) for i in range(count)]
    around = [round(start - s, 1) for s in LIGHT_AROUND_SECONDS] + [
        round(end + s, 1) for s in LIGHT_AROUND_SECONDS
    ]
    return inside, around


def light_sample_times(levels: Levels, duration: float) -> list[float]:
    """Every moment whose stage lighting the estimate would look at."""
    times = set()
    for start, end, _depth in _candidate_gaps(levels, duration):
        inside, around = _light_times(start, end)
        times.update(t for t in inside + around if 0.0 <= t < duration)
    return sorted(times)


def lit_up(light, start: float, end: float) -> float | None:
    """How much brighter the stage is during a quiet stretch than either
    side of it (negative: darker), from sampled lighting; None if unknown."""
    inside, around = _light_times(start, end)
    during = [light[t] for t in inside if t in light]
    either_side = [light[t] for t in around if t in light]
    if not during or not either_side:
        return None
    return statistics.median(during) - statistics.median(either_side)


def boundary_candidates(levels: Levels, duration: float, light=None):
    """[(boundary_seconds, score), ...] in time order.

    `light`, if given, is sampled stage lighting ({seconds: brightness},
    from stage_light at light_sample_times): a quiet stretch where the
    stage gets brighter scores lower.
    """
    candidates = []
    for start, end, depth in _candidate_gaps(levels, duration):
        position = min(start + APPLAUSE_SECONDS, end)
        score = depth * math.sqrt(min(end - start, LENGTH_CAP_SECONDS))
        if light:
            brighter = lit_up(light, start, end)
            if brighter is not None and brighter > 0:
                score *= max(LIGHT_PENALTY_FLOOR, 1 - brighter / LIGHT_PENALTY_WIDTH)
        candidates.append((position, score))
    return candidates


def _best_subset(candidates, wanted: int):
    """The `wanted` candidates with the highest total score, no two closer
    than MIN_SONG_SECONDS. Fewer if there aren't enough to go round.
    """
    m = len(candidates)
    wanted = min(wanted, m)
    if wanted <= 0:
        return []
    neg = -math.inf
    # best[j][k]: top score choosing k boundaries, the last of them j.
    best = [[neg] * (wanted + 1) for _ in range(m)]
    back = [[None] * (wanted + 1) for _ in range(m)]
    for j in range(m):
        best[j][1] = candidates[j][1]
        for k in range(2, wanted + 1):
            for i in range(j):
                if candidates[j][0] - candidates[i][0] < MIN_SONG_SECONDS:
                    continue
                if best[i][k - 1] == neg:
                    continue
                score = best[i][k - 1] + candidates[j][1]
                if score > best[j][k]:
                    best[j][k] = score
                    back[j][k] = i
    # Spacing can make `wanted` impossible; take the most that fit.
    for k in range(wanted, 0, -1):
        last = max(range(m), key=lambda j: best[j][k])
        if best[last][k] > neg:
            break
    picked = []
    j = last
    while j is not None and k > 0:
        picked.append(candidates[j][0])
        j = back[j][k]
        k -= 1
    return sorted(picked)


def estimate_starts(levels: Levels, duration: float, song_count: int | None = None,
                    light=None):
    """Estimated chapter start times, beginning with 0.

    With a song count the best song_count - 1 boundaries are chosen; without
    one, every gap that scores well enough. `light` is optional sampled
    stage lighting (see boundary_candidates).
    """
    candidates = boundary_candidates(levels, duration, light)
    if song_count:
        boundaries = _best_subset(candidates, song_count - 1)
    else:
        boundaries = []
        for position, score in sorted(candidates, key=lambda c: -c[1]):
            if score < AUTO_THRESHOLD:
                break
            if all(abs(position - p) >= MIN_SONG_SECONDS for p in boundaries):
                boundaries.append(position)
        boundaries.sort()
    return [0.0] + boundaries


# --- song intros inside tracks ------------------------------------------------

# A live album's track often starts well before the band does: a story
# video, a flag entrance, an intermission. Where the lengths place a track,
# the audio can say where the song proper starts, and the part before it
# becomes "Intro to <song>". Tuned on five concerts' 65 tracks against what
# the video shows (and, for THE ONE, the person who knows the show): it
# found the 25 intros there, and no false ones.
#
# The song proper starts where the first real quiet stretch (INTRO_MIN_GAP
# seconds or more, INTRO_MIN_DEPTH dB deep) ends - provided that is at least
# INTRO_MIN_SECONDS into the track, since the gap between two songs itself
# always ends within a few seconds of the track starting, and at most
# INTRO_MAX_SECONDS in (or halfway through a short track).
INTRO_MIN_SECONDS = 45.0
INTRO_MAX_SECONDS = 180.0
INTRO_MIN_GAP_SECONDS = 8.0
INTRO_MIN_DEPTH_DB = 15.0
# An intermission can bring the sound partway back before the song: a crowd,
# a soft backing track, the bass still well below the song's (by at least
# INTRO_UNDERWAY_DB). Then the song starts after the next dip in it - a drop
# of INTRO_DIP_DB - once the sound reaches the song's level (within
# INTRO_SONG_LEVEL_DB of its median) within INTRO_DIP_SECONDS. Looked for
# only up to INTRO_DIP_SEARCH_SECONDS past the window, and never once the
# song is under way.
INTRO_UNDERWAY_DB = 7.0
INTRO_DIP_DB = 5.0
INTRO_SONG_LEVEL_DB = 6.0
INTRO_DIP_SECONDS = 6.0
INTRO_DIP_SEARCH_SECONDS = 60.0

INTRO_TITLE = "Intro to {title}"


def find_intro(levels: Levels, start: float, end: float, gaps=None) -> float | None:
    """Where the song proper starts inside a track placed at start-end, if
    there is an intro before it; None when the song starts with the track.
    `gaps` is find_gaps(levels), passed in when checking many tracks.
    """
    length = end - start
    window = start + min(INTRO_MAX_SECONDS, length / 2)
    if window < start + INTRO_MIN_SECONDS:
        return None
    gaps = find_gaps(levels) if gaps is None else gaps
    quiet_end = next(
        (
            gap_end for gap_start, gap_end, depth in gaps
            if gap_end - gap_start >= INTRO_MIN_GAP_SECONDS
            and depth >= INTRO_MIN_DEPTH_DB
            and start + INTRO_MIN_SECONDS <= gap_end <= window
        ),
        None,
    )
    if quiet_end is None:
        return None

    step = levels.step
    per_second = round(1.0 / step)
    full = _smooth(levels.full, per_second)
    bass = _smooth(levels.bass, per_second)
    lo, hi = int(window / step), min(len(full), int(end / step))
    if hi - lo < per_second:
        return quiet_end
    song_full = statistics.median(full[lo:hi])
    song_bass = statistics.median(bass[lo:hi])

    recent = 8 * per_second  # the last few seconds, to judge the sound by
    i = int(quiet_end / step) + recent
    stop = min(len(full) - 1, int((window + INTRO_DIP_SEARCH_SECONDS) / step))
    while i < stop:
        if statistics.median(bass[i - recent:i]) >= song_bass - INTRO_UNDERWAY_DB:
            break  # the song is under way
        before = statistics.median(full[i - 10 * per_second:i])
        if full[i] < before - INTRO_DIP_DB:
            j = i
            limit = min(len(full) - 1, i + int(INTRO_DIP_SECONDS / step))
            while j < limit and full[j] < song_full - INTRO_SONG_LEVEL_DB:
                j += 1
            if full[j] >= song_full - INTRO_SONG_LEVEL_DB:
                return j * step
        i += 1
    return quiet_end


def split_intros(chapters, levels: Levels) -> list[dict]:
    """Chapters placed by track lengths, with each track's intro (where it
    has one) split off as "Intro to <title>". The song's own start comes
    from the audio, so it is marked estimated; the intro's is the track's.
    """
    gaps = find_gaps(levels)
    starts, titles, estimated = [], [], []
    source = "manual"
    for chapter in chapters:
        title = chapter["title"]
        if chapter.get("source") not in (None, "auto-numbered"):
            source = chapter["source"]
        song = find_intro(levels, chapter["start"], chapter["end"], gaps) if title else None
        if song is not None:
            starts += [chapter["start"], song]
            titles += [INTRO_TITLE.format(title=title), title]
            estimated += [False, True]
        else:
            starts.append(chapter["start"])
            titles.append(title)
            estimated.append(bool(chapter.get("estimated")))
    duration = chapters[-1]["end"] if chapters else 0.0
    result = chapters_from_starts(starts, duration, titles, source=source)
    for chapter, flag in zip(result, estimated, strict=True):
        if flag:
            chapter["estimated"] = True
    return result
