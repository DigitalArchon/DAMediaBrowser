# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

from mediabrowser.core import proposal


def chapters(*spans):
    """Chapters from (start, end) pairs, all untitled."""
    return [{"title": None, "start": s, "end": e, "source": "auto-numbered"} for s, e in spans]


def tracks(*pairs):
    return [{"title": title, "length": length} for title, length in pairs]


class TestDurationMatching:
    def test_chapters_and_tracks_of_the_same_length_line_up(self):
        result = proposal.build(
            chapters((0, 200), (200, 500), (500, 800)),
            tracks(("One", 200), ("Two", 300), ("Three", 300)),
        )
        assert result.mapping() == [(0, "One"), (1, "Two"), (2, "Three")]
        assert all(row.flag == "" for row in result.rows)

    def test_durations_within_tolerance_still_match(self):
        result = proposal.build(
            chapters((0, 200), (200, 500)),
            tracks(("One", 203), ("Two", 298)),
        )
        assert result.mapping() == [(0, "One"), (1, "Two")]

    def test_a_track_matching_nothing_is_listed_as_unused(self):
        result = proposal.build(chapters((0, 200)), tracks(("One", 200), ("Spare", 999)))
        unused = [row for row in result.rows if row.flag == proposal.FLAG_UNUSED]
        assert [row.proposed for row in unused] == ["Spare"]
        # An unused track names nothing, so it must never reach the mapping.
        assert result.mapping() == [(0, "One")]


class TestPositionMarker:
    def _position_matched(self):
        # The second track's duration matches no chapter, so it cannot be
        # anchored by time; order puts it on the chapter left over, and the
        # aligner flags that as the lower-confidence guess it is.
        return proposal.build(
            chapters((0, 300), (300, 600)),
            tracks(("One", 300), ("Two", 999)),
        )

    def test_the_marker_is_shown_in_the_table(self):
        result = self._position_matched()
        flagged = [row for row in result.rows if row.flag == proposal.FLAG_POSITION]
        assert flagged
        assert all(row.proposed.startswith(proposal.POSITION_MARKER) for row in flagged)

    def test_the_marker_is_never_written_into_a_stored_title(self):
        # It means "matched by position, trust it less", not part of the name.
        result = self._position_matched()
        assert all(
            not title.startswith(proposal.POSITION_MARKER) for _, title in result.mapping()
        )


class TestNoDurations:
    def test_tracks_without_durations_fall_back_to_position(self):
        result = proposal.build(
            chapters((0, 200), (200, 500), (500, 800)),
            tracks(("One", None), ("Two", None), ("Three", None)),
        )
        assert result.mapping() == [(0, "One"), (1, "Two"), (2, "Three")]
        assert "matched by position only" in result.summary

    def test_extra_chapters_are_flagged_rather_than_guessed_at(self):
        result = proposal.build(
            chapters((0, 200), (200, 500), (500, 800)),
            tracks(("One", None), ("Two", None)),
        )
        assert result.mapping() == [(0, "One"), (1, "Two")]
        assert [row.flag for row in result.rows][-1] == proposal.FLAG_NO_MATCH

    def test_extra_tracks_are_listed_as_unused(self):
        result = proposal.build(
            chapters((0, 200)),
            tracks(("One", None), ("Two", None)),
        )
        assert [row.flag for row in result.rows] == ["", proposal.FLAG_UNUSED]


class TestGroupedChapters:
    # The last track anchors on the last chapter by time, leaving two chapters
    # and one track before it - so that track's name covers the pair.
    GROUPED_CHAPTERS = ((0, 200), (200, 500), (500, 1500))
    GROUPED_TRACKS = (("Long One", 777), ("Two", 1000))

    def _grouped(self):
        return proposal.build(
            chapters(*self.GROUPED_CHAPTERS), tracks(*self.GROUPED_TRACKS)
        )

    def test_a_track_spanning_several_chapters_names_only_the_first(self):
        mapped = dict(self._grouped().mapping())
        assert mapped[0] == "Long One"
        assert 1 not in mapped, "the second chapter of a group must keep what it had"

    def test_a_grouped_row_shows_the_chapter_range(self):
        assert self._grouped().rows[0].chapter == "1-2"

    def test_a_grouped_row_shows_the_combined_duration(self):
        assert self._grouped().rows[0].duration == "8:20"  # 200s + 300s


class TestEdgeCases:
    def test_no_tracks_proposes_nothing(self):
        result = proposal.build(chapters((0, 200)), [])
        assert result.mapping() == []

    def test_no_chapters_leaves_every_track_unused(self):
        result = proposal.build([], tracks(("One", 200)))
        assert result.mapping() == []
        assert all(row.flag == proposal.FLAG_UNUSED for row in result.rows)

    def test_the_summary_counts_chapters_and_tracks(self):
        result = proposal.build(chapters((0, 200), (200, 500)), tracks(("One", 200)))
        assert "2 chapter(s), 1 track(s)" in result.summary

    def test_every_row_that_names_a_chapter_carries_a_clean_title(self):
        result = proposal.build(
            chapters((0, 300), (300, 600)),
            tracks(("One", 300), ("Two", 300)),
        )
        for row in result.rows:
            if row.chapter_index is not None:
                assert row.title and proposal.POSITION_MARKER not in row.title


def chapters_of(*durations):
    """Back-to-back untitled chapters with the given durations."""
    spans, start = [], 0.0
    for d in durations:
        spans.append((start, start + d))
        start += d
    return chapters(*spans)


class TestAnchorsAreChosenAsAWhole:
    def test_a_coincidental_match_far_ahead_does_not_swallow_the_disc(self):
        # "Opener" (with its intro folded in) matches no single chapter nearby,
        # but happens to equal the last chapter. Anchoring it there would leave
        # nowhere for everything in between.
        result = proposal.build(
            chapters_of(100, 300, 200, 250, 402),
            tracks(("Opener", 400), ("Two", 200), ("Three", 250), ("Four", 402)),
        )
        mapped = dict(result.mapping())
        assert (mapped[2], mapped[3], mapped[4]) == ("Two", "Three", "Four")

    def test_the_other_night_of_a_two_night_release_is_left_unused(self):
        # A real disc: night 1 of a two-night release, whose MusicBrainz entry
        # lists both nights' audio discs and then both nights' video discs
        # (mostly without durations). Chapter 18 fits night 1's Headbanger and
        # night 2's opener equally well; order says it's Headbanger.
        disc = chapters_of(
            108.2, 297.8, 360.9, 262.7, 199.0, 146.3, 214.6, 251.9, 197.1, 321.0,
            410.8, 97.3, 333.3, 255.3, 245.0, 542.5, 117.7, 407.3, 536.3, 90.5,
        )
        night_1 = [
            ("BABYMETAL DEATH", 404.133), ("Megitsune", 361.373),
            ("DA DA DANCE", 262.693), ("Shanti Shanti Shanti", 198.786),
            ("Kagerou", 361.053), ("MAYA", 251.8), ("BxMxC", 197.133),
            ("Syncopation", 321.106), ("Monochrome", 413.92), ("Metali!!", 431.0),
            ("Gimme Chocolate!!", 254.813), ("Doki Doki ☆ Morning", 244.653),
            ("THE ONE", 659.64), ("Headbangeeeeerrrrr!!!!!", 407.96),
            ("Road of Resistance", 613.946),
        ]
        night_2 = [
            ("BABYMETAL DEATH", 407.866), ("Distortion", 302.36),
            ("PA PA YA!!", 239.826), ("Elevator Girl", 194.28), ("YAVA!", 377.693),
            ("Believing", 260.426), ("Brand New Day", 289.44), ("Starlight", 279.8),
            ("KARATE", 385.306), ("Metali!!", 429.68), ("Gimme Chocolate!!", 254.093),
            ("Doki Doki ☆ Morning", 246.266), ("THE ONE", 570.933),
            ("Meta Taro", 477.506), ("Arkadia", 564.52),
        ]
        video_discs = [(title, None) for title, _ in night_1 + night_2]
        result = proposal.build(disc, tracks(*night_1, *night_2, *video_discs))

        mapped = dict(result.mapping())
        assert mapped[1] == "BABYMETAL DEATH"
        assert mapped[2] == "Megitsune"
        assert mapped[3] == "DA DA DANCE"
        assert mapped[10] == "Monochrome"
        assert mapped[12] == "Metali!!"
        assert mapped[15] == "THE ONE"
        assert mapped[17] == "Headbangeeeeerrrrr!!!!!"
        assert mapped[18] == "Road of Resistance"
        # Intro, interlude, encore break and outro stay unnamed.
        assert {0, 11, 16, 19}.isdisjoint(mapped)


def chapters_at(starts, end):
    """Untitled chapters starting at `starts`, the last running to `end`."""
    return chapters(*zip(starts, list(starts[1:]) + [end], strict=True))


# Doomsday III & IV: the box set's two CDs for that show, which together are
# the whole video (5230s against 5222s), and the chapters Detect Chapters
# estimated from the video's audio - boundaries up to half a minute out, and
# some songs split in two or three.
DOOMSDAY_III_IV = [
    ("In The Name Of", 697.0), ("Distortion", 254.0), ("Pa Pa Ya!!", 271.0),
    ("Gimme Chocolate!!", 257.0), ("Doki Doki ☆ Morning", 233.0), ("Syncopation", 320.0),
    ("Megitsune", 416.0), ("Karate", 310.0), ("From Dusk Till Dawn", 285.0),
    ("Headbangeeeeerrrrr!!!!!", 368.0), ("Road Of Resistance", 469.0), ("The One", 581.0),
    ("Ijime, Dame, Zettai", 761.0),
]
ESTIMATED_III_IV = [
    0.0, 697.0, 953.0, 1204.0, 1492.0, 1721.5, 2043.5, 2460.5, 2741.0, 3027.5, 3410.0,
    3491.0, 3690.5, 3851.5, 4076.5, 4166.5, 4460.5, 5134.5,
]


class TestTracksThatAreTheWholeVideo:
    def _named(self, starts, pairs=DOOMSDAY_III_IV, end=5230.225):
        return proposal.build(chapters_at(starts, end), tracks(*pairs))

    def test_estimated_chapters_are_named_by_where_each_song_plays(self):
        # Named by length, chapter 2 came out as Gimme Chocolate!! - Distortion
        # and Gimme Chocolate!! are within seconds of each other's length.
        mapped = dict(self._named(ESTIMATED_III_IV).mapping())
        assert mapped[1] == "Distortion"
        assert mapped[4] == "Doki Doki ☆ Morning"
        assert mapped[8] == "From Dusk Till Dawn"
        songs = [t for t in mapped.values() if not t.startswith("Intro to ")]
        assert sorted(songs) == sorted(t for t, _ in DOOMSDAY_III_IV)

    def test_a_song_cut_in_pieces_is_named_where_it_starts(self):
        # THE ONE was estimated as three chapters (64:12, 69:26, 70:56 in
        # minutes); its name belongs on the first real piece, not the biggest.
        mapped = dict(self._named(ESTIMATED_III_IV).mapping())
        assert mapped[13] == "The One"

    def test_leftover_pieces_say_which_song_they_belong_to(self):
        rows = self._named(ESTIMATED_III_IV).rows
        piece = next(r for r in rows if r.chapter == "15")
        assert piece.flag == proposal.FLAG_NO_MATCH
        assert piece.note == "part of The One"

    def test_a_missed_boundary_says_where_the_song_should_start(self):
        # Without the boundary before Pa Pa Ya!!, Distortion's chapter runs on
        # through it; the song is left over, with where to split.
        starts = [s for s in ESTIMATED_III_IV if s != 953.0]
        result = self._named(starts)
        unused = [r for r in result.rows if r.flag == proposal.FLAG_UNUSED]
        assert [(r.proposed, r.note) for r in unused] == [
            ("Pa Pa Ya!!", "should start at 15:51")
        ]
        assert dict(result.mapping())[1] == "Distortion"

    def test_a_chapter_well_off_its_track_is_pointed_out(self):
        rows = self._named(ESTIMATED_III_IV).rows
        gimme = next(r for r in rows if r.title == "Gimme Chocolate!!")
        assert gimme.note == "starts 0:18 early"

    def test_an_intro_chapter_is_named_as_the_songs_intro(self):
        # A disc authored with the intro as its own chapter: the CD track
        # includes the intro, but the song's name belongs on the song.
        result = proposal.build(
            chapters((0, 108), (108, 406), (406, 767)),
            tracks(("BABYMETAL DEATH", 404.0), ("Megitsune", 361.0)),
        )
        assert result.mapping() == [
            (0, "Intro to BABYMETAL DEATH"), (1, "BABYMETAL DEATH"), (2, "Megitsune")
        ]

    def test_an_estimated_intro_is_named_too(self):
        # Road of Resistance's flag entrance was estimated as its own chapter
        # (56:50 to 58:11, in minutes), ahead of the song.
        mapped = dict(self._named(ESTIMATED_III_IV).mapping())
        assert mapped[10] == "Intro to Road Of Resistance"
        assert mapped[11] == "Road Of Resistance"

    def test_a_piece_after_the_song_starts_is_not_an_intro(self):
        mapped = dict(self._named(ESTIMATED_III_IV).mapping())
        assert 14 not in mapped and 15 not in mapped, "THE ONE's later pieces"

    def test_tracks_that_are_not_the_whole_video_are_matched_by_length(self):
        # Half a show against the whole video: the lengths decide, as before.
        result = proposal.build(
            chapters((0, 200), (200, 500), (500, 5000)),
            tracks(("One", 200), ("Two", 300)),
        )
        assert result.mapping() == [(0, "One"), (1, "Two")]
        assert all(row.note == "" for row in result.rows)

    def test_a_song_with_an_intro_is_not_called_late(self):
        rows = self._named(ESTIMATED_III_IV).rows
        ror = next(r for r in rows if r.title == "Road Of Resistance")
        assert ror.note == ""
