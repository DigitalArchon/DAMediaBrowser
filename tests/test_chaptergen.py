# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

from mediabrowser.core import chaptergen
from mediabrowser.core.audio_levels import Levels


def tracks(*pairs):
    return [{"title": title, "length": length} for title, length in pairs]


def medium(*lengths, fmt="CD"):
    return {
        "title": "",
        "format": fmt,
        "tracks": [{"title": f"Track {i + 1}", "length": n} for i, n in enumerate(lengths)],
    }


class TestChaptersFromTracks:
    def test_each_track_starts_where_the_ones_before_it_add_up_to(self):
        chapters = chaptergen.chapters_from_tracks(
            tracks(("One", 100), ("Two", 200), ("Three", 300)), 600, "musicbrainz"
        )
        assert [(c["start"], c["end"]) for c in chapters] == [
            (0, 100), (100, 300), (300, 600)
        ]
        assert [c["title"] for c in chapters] == ["One", "Two", "Three"]
        assert all(c["source"] == "musicbrainz" for c in chapters)

    def test_the_last_chapter_runs_to_the_end_of_the_video(self):
        # A few seconds of credits beyond the album's total still need to be
        # inside a chapter.
        chapters = chaptergen.chapters_from_tracks(tracks(("One", 100), ("Two", 200)), 307, "m")
        assert chapters[-1]["end"] == 307

    def test_tracks_starting_after_the_video_ends_are_left_out(self):
        chapters = chaptergen.chapters_from_tracks(
            tracks(("One", 100), ("Two", 200), ("Encore", 300)), 250, "m"
        )
        assert [c["title"] for c in chapters] == ["One", "Two"]
        assert chapters[-1]["end"] == 250

    def test_chapters_are_numbered_in_order(self):
        chapters = chaptergen.chapters_from_tracks(tracks(("A", 10), ("B", 10)), 20, "m")
        assert [c["index"] for c in chapters] == [0, 1]


class TestPickMedia:
    def test_the_discs_adding_up_to_the_video_are_chosen(self):
        # A box set: show one's two CDs and its video disc, then show two's.
        media = [
            medium(606, 253, 272),
            medium(260, 361),
            medium(None, None, fmt="Blu-ray"),
            medium(697, 254, 271),
            medium(285, 368),
        ]
        assert chaptergen.pick_media(media, 606 + 253 + 272 + 260 + 361 + 7) == [0, 1]

    def test_a_disc_without_lengths_breaks_a_run(self):
        media = [medium(100), medium(None, fmt="Blu-ray"), medium(100)]
        assert chaptergen.pick_media(media, 200) in ([0], [2])

    def test_nothing_is_chosen_when_nothing_has_lengths(self):
        assert chaptergen.pick_media([medium(None, None)], 300) == []

    def test_usable_media_skips_discs_missing_any_length(self):
        media = [medium(100, 200), medium(100, None), medium()]
        assert chaptergen.usable_media(media) == [0]


class TestSnapping:
    def _levels(self, quiet_at, length=100):
        full = [-10.0] * int(length / 0.5)
        full[int(quiet_at / 0.5)] = -40.0
        return Levels(step=0.5, full=full, bass=list(full))

    def test_a_boundary_moves_to_the_quiet_moment_nearby(self):
        assert chaptergen.snap_starts([0.0, 50.0], self._levels(52.0)) == [0.0, 52.0]

    def test_it_never_moves_further_than_the_radius(self):
        assert chaptergen.snap_starts([0.0, 50.0], self._levels(60.0)) == [0.0, 50.0]

    def test_the_first_chapter_stays_at_the_start(self):
        assert chaptergen.snap_starts([0.0, 50.0], self._levels(1.0))[0] == 0.0


def concert(song_lengths, gap=12.0, song_db=-10.0, gap_db=-40.0, step=0.5):
    """Levels for a concert: loud songs with a quiet gap before each one
    after the first. Returns (levels, true song start times).
    """
    bass = []
    starts = []
    for i, length in enumerate(song_lengths):
        if i:
            bass += [gap_db] * int(gap / step)
        starts.append(len(bass) * step)
        # A little movement inside the song, so it is not perfectly flat.
        bass += [song_db + (3.0 if (k // 20) % 2 else 0.0) for k in range(int(length / step))]
    return Levels(step=step, full=list(bass), bass=bass), starts


class TestEstimating:
    SONGS = [300, 240, 280, 200, 330, 260]

    def test_gaps_between_songs_are_found(self):
        levels, starts = concert(self.SONGS)
        found = chaptergen.estimate_starts(levels, levels.duration)
        assert len(found) == len(starts)
        for estimate, truth in zip(found, starts, strict=True):
            assert abs(estimate - truth) <= 1.0

    def test_a_song_count_limits_the_boundaries_to_the_strongest(self):
        levels, _starts = concert(self.SONGS)
        assert len(chaptergen.estimate_starts(levels, levels.duration, song_count=4)) == 4

    def test_the_first_chapter_starts_at_zero(self):
        levels, _ = concert(self.SONGS)
        assert chaptergen.estimate_starts(levels, levels.duration)[0] == 0.0

    def test_a_long_gap_puts_the_boundary_past_the_applause(self):
        # A minute between songs is an intro or interlude, and belongs to the
        # song after it: the boundary sits APPLAUSE_SECONDS in, not at the end.
        levels, starts = concert([300, 300], gap=60.0)
        gap_start = 300.0
        found = chaptergen.estimate_starts(levels, levels.duration)
        # Within a second: smoothing lets the quiet begin a moment early.
        assert abs(found[1] - (gap_start + chaptergen.APPLAUSE_SECONDS)) <= 1.0
        assert found[1] < starts[1]

    def test_a_short_breakdown_inside_a_song_is_not_a_boundary(self):
        levels, _ = concert([300, 300])
        bass = list(levels.bass)
        # One second of near-silence in the middle of the first song.
        for k in range(300, 302):
            bass[k] = -40.0
        levels = Levels(step=0.5, full=bass, bass=bass)
        assert len(chaptergen.estimate_starts(levels, levels.duration)) == 2

    def test_quiet_at_the_very_start_and_end_is_ignored(self):
        levels, _ = concert([300, 300])
        bass = [-60.0] * 30 + list(levels.bass) + [-60.0] * 60
        levels = Levels(step=0.5, full=bass, bass=bass)
        assert len(chaptergen.estimate_starts(levels, levels.duration)) == 2

    def test_boundaries_are_never_closer_than_a_song(self):
        levels, _ = concert([300, 30, 300], gap=12.0)
        found = chaptergen.estimate_starts(levels, levels.duration)
        gaps = [b - a for a, b in zip(found, found[1:], strict=False)]
        assert all(gap >= chaptergen.MIN_SONG_SECONDS for gap in gaps)

    def test_asking_for_more_songs_than_can_be_found_returns_what_there_is(self):
        levels, starts = concert([300, 300])
        assert len(chaptergen.estimate_starts(levels, levels.duration, song_count=9)) == 2


class TestChaptersFromStarts:
    def test_estimated_starts_are_marked_except_the_first(self):
        chapters = chaptergen.chapters_from_starts([0, 100, 200], 300, estimated=True)
        assert [c.get("estimated", False) for c in chapters] == [False, True, True]

    def test_titles_name_chapters_in_order_and_run_out_gracefully(self):
        chapters = chaptergen.chapters_from_starts([0, 100, 200], 300, ["One", "Two"])
        assert [c["title"] for c in chapters] == ["One", "Two", None]
        assert chapters[2]["source"] == "auto-numbered"


class TestStageLighting:
    """Checked against five concerts: a quiet stretch where the stage gets
    brighter is a breakdown within a song, and is marked down; one where it
    gets darker is left as the sound has it.
    """

    def _two_gaps(self):
        # Four songs; the first gap and the third are equally quiet.
        return concert([300, 300, 300, 300], gap=12.0)

    def _light(self, levels, brighter_gap_index, change):
        light = {}
        gaps = chaptergen._candidate_gaps(levels, levels.duration)
        for index, (start, end, _depth) in enumerate(gaps):
            inside, around = chaptergen._light_times(start, end)
            for t in around:
                light[t] = 100.0
            for t in inside:
                light[t] = 100.0 + (change if index == brighter_gap_index else -40.0)
        return light

    def test_the_moments_to_look_at_cover_every_quiet_stretch(self):
        levels, _ = self._two_gaps()
        times = chaptergen.light_sample_times(levels, levels.duration)
        for start, end, _depth in chaptergen._candidate_gaps(levels, levels.duration):
            assert any(start <= t <= end for t in times)
            assert any(start - 30 <= t < start for t in times)

    def test_a_gap_where_the_lights_come_up_scores_lower(self):
        levels, _ = self._two_gaps()
        plain = chaptergen.boundary_candidates(levels, levels.duration)
        lit = chaptergen.boundary_candidates(
            levels, levels.duration, self._light(levels, 1, +30.0)
        )
        assert lit[1][1] < plain[1][1]
        assert lit[0][1] == plain[0][1], "a darkening gap is left alone"

    def test_a_dark_gap_is_never_scored_up(self):
        levels, _ = self._two_gaps()
        plain = chaptergen.boundary_candidates(levels, levels.duration)
        lit = chaptergen.boundary_candidates(
            levels, levels.duration, self._light(levels, 99, 0.0)
        )
        assert [s for _, s in lit] == [s for _, s in plain]

    def test_with_a_count_the_brightening_gap_is_the_one_dropped(self):
        levels, starts = self._two_gaps()
        light = self._light(levels, 1, +30.0)
        found = chaptergen.estimate_starts(levels, levels.duration, song_count=3, light=light)
        assert len(found) == 3
        assert not any(abs(f - starts[2]) < 30 for f in found)

    def test_unknown_lighting_changes_nothing(self):
        levels, _ = self._two_gaps()
        assert chaptergen.estimate_starts(levels, levels.duration, light={}) == (
            chaptergen.estimate_starts(levels, levels.duration)
        )


def _stretch(seconds, full, bass, step=0.5):
    n = int(seconds / step)
    return [full] * n, [bass] * n


def _show(*parts):
    full, bass = [], []
    for seconds, f, b in parts:
        a, c = _stretch(seconds, f, b)
        full += a
        bass += c
    return Levels(step=0.5, full=full, bass=bass)


SONG = (-10.0, -12.0)
QUIET = (-45.0, -50.0)


class TestIntros:
    def test_a_track_starting_with_the_song_has_no_intro(self):
        # Applause in the gap, then straight into the song.
        levels = _show((200, *SONG), (10, *QUIET), (300, *SONG))
        assert chaptergen.find_intro(levels, 205.0, 510.0) is None

    def test_an_entrance_before_a_pause_is_an_intro(self):
        # Road of Resistance: music for the flag entrance, a pause, the song.
        levels = _show((200, *SONG), (10, *QUIET), (60, *SONG), (15, *QUIET), (300, *SONG))
        song = chaptergen.find_intro(levels, 205.0, 585.0)
        assert song is not None and abs(song - 285.0) <= 1.5

    def test_an_intermission_ends_at_the_dip_before_the_song(self):
        # THE ONE: quiet, a minute of intermission well below the song, a
        # short dip, then the song.
        levels = _show(
            (200, *SONG), (70, *QUIET), (50, -18.0, -25.0), (3, -30.0, -35.0), (300, *SONG)
        )
        song = chaptergen.find_intro(levels, 200.0, 623.0)
        assert song is not None and abs(song - 323.0) <= 2.0

    def test_a_dip_after_the_song_is_under_way_is_not_its_start(self):
        # Onedari: the song starts, then drops out briefly for a break.
        levels = _show(
            (200, *SONG), (70, *QUIET), (30, -13.0, -14.0), (3, -30.0, -35.0), (300, *SONG)
        )
        song = chaptergen.find_intro(levels, 200.0, 603.0)
        assert song is not None and abs(song - 270.0) <= 1.5

    def test_split_intros_names_the_intro_and_marks_the_songs_start(self):
        levels = _show((200, *SONG), (10, *QUIET), (60, *SONG), (15, *QUIET), (300, *SONG))
        chapters = chaptergen.chapters_from_starts(
            [0.0, 205.0], 585.0, ["Megitsune", "Road Of Resistance"], source="musicbrainz"
        )
        split = chaptergen.split_intros(chapters, levels)
        assert [c["title"] for c in split] == [
            "Megitsune", "Intro to Road Of Resistance", "Road Of Resistance"
        ]
        assert split[1]["start"] == 205.0 and not split[1].get("estimated")
        assert split[2].get("estimated") is True
        assert {c["source"] for c in split} == {"musicbrainz"}
