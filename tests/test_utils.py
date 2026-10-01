# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

from mediabrowser.core import utils


def chapter(title=None, start=0.0, end=60.0, source="embedded"):
    return {"title": title, "start": start, "end": end, "source": source}


class TestFormatSeconds:
    def test_under_an_hour_omits_the_hour(self):
        assert utils.format_seconds(0) == "0:00"
        assert utils.format_seconds(65) == "1:05"
        assert utils.format_seconds(599.4) == "9:59"

    def test_an_hour_and_over_includes_it(self):
        assert utils.format_seconds(3600) == "1:00:00"
        assert utils.format_seconds(3725) == "1:02:05"

    def test_negative_durations_clamp_rather_than_render_backwards(self):
        assert utils.format_seconds(-10) == "0:00"


class TestChapterLabel:
    def test_a_titled_chapter_shows_its_title(self):
        assert utils.chapter_label(0, chapter("Megitsune")) == "Megitsune"

    def test_an_untitled_chapter_falls_back_to_its_number(self):
        assert utils.chapter_label(2, chapter(None)) == "Chapter 3"
        assert utils.chapter_label(0, chapter("")) == "Chapter 1"


class TestSetChapterTitle:
    def test_a_title_is_stored_with_its_source(self):
        ch = chapter()
        utils.set_chapter_title(ch, "Gimme Chocolate", "musicbrainz")
        assert ch["title"] == "Gimme Chocolate"
        assert ch["source"] == "musicbrainz"

    def test_surrounding_whitespace_is_stripped(self):
        ch = chapter()
        utils.set_chapter_title(ch, "  Road of Resistance  ")
        assert ch["title"] == "Road of Resistance"

    def test_clearing_a_title_returns_the_chapter_to_its_number(self):
        ch = chapter("Karate", source="manual")
        utils.set_chapter_title(ch, "")
        assert ch["title"] is None
        assert ch["source"] == "auto-numbered"

    def test_a_whitespace_only_title_counts_as_clearing_it(self):
        # Otherwise a rescan would carry a blank 'manual' title forward for good.
        ch = chapter("Karate", source="manual")
        utils.set_chapter_title(ch, "   ")
        assert ch["title"] is None
        assert ch["source"] == "auto-numbered"

    def test_the_default_source_is_manual(self):
        ch = chapter()
        utils.set_chapter_title(ch, "Typed by hand")
        assert ch["source"] == "manual"


class TestParsePastedTracklist:
    def test_plain_titles_parse_without_durations(self):
        assert utils.parse_pasted_tracklist("One\nTwo") == [
            {"title": "One", "length": None},
            {"title": "Two", "length": None},
        ]

    def test_a_duration_is_read_from_anywhere_in_the_line(self):
        for line in ("8:23 Title", "Title - 8:23", "Title (8:23)"):
            assert utils.parse_pasted_tracklist(line) == [{"title": "Title", "length": 503.0}]

    def test_hour_long_durations_parse(self):
        assert utils.parse_pasted_tracklist("1:02:05 Long")[0]["length"] == 3725.0

    def test_leading_track_numbers_are_stripped(self):
        for line in ("1. Title", "2) Title", "03- Title", "4: Title"):
            assert utils.parse_pasted_tracklist(line)[0]["title"] == "Title"

    def test_blank_and_junk_lines_are_skipped(self):
        assert utils.parse_pasted_tracklist("One\n\n   \n5:00\nTwo") == [
            {"title": "One", "length": None},
            {"title": "Two", "length": None},
        ]

    def test_a_number_that_is_not_a_duration_stays_in_the_title(self):
        assert utils.parse_pasted_tracklist("Nineteen 99")[0]["title"] == "Nineteen 99"


class TestExportNamedChapters:
    def test_named_chapters_round_trip_back_through_the_parser(self):
        chapters = [
            chapter("Megitsune", 0, 263),
            chapter(None, 263, 400),
            chapter("Karate", 400, 700),
        ]
        text = utils.export_named_chapters(chapters)
        assert text == "4:23 Megitsune\n5:00 Karate"

        parsed = utils.parse_pasted_tracklist(text)
        assert [t["title"] for t in parsed] == ["Megitsune", "Karate"]
        assert [t["length"] for t in parsed] == [263.0, 300.0]

    def test_untitled_chapters_are_omitted(self):
        assert utils.export_named_chapters([chapter(None)]) == ""


class TestSearch:
    def test_an_empty_search_matches_everything(self):
        video = {"display_name": "Live at Budokan"}
        assert utils.matches_search(video, [], "")

    def test_a_video_matches_on_its_own_name(self):
        video = {"display_name": "Live at Budokan"}
        assert utils.matches_search(video, [], "budokan")

    def test_a_video_matches_on_a_chapter_title(self):
        video = {"display_name": "Live at Budokan"}
        assert utils.matches_search(video, [chapter("Megitsune")], "megi")

    def test_a_video_matching_nothing_is_excluded(self):
        video = {"display_name": "Live at Budokan"}
        assert not utils.matches_search(video, [chapter("Megitsune")], "karate")

    def test_untitled_chapters_are_searchable_by_number(self):
        video = {"display_name": "Disc"}
        assert utils.matches_search(video, [chapter(None)], "chapter 1")

    def test_matching_chapters_returns_indices_with_the_chapters(self):
        chapters = [chapter("Megitsune"), chapter("Karate"), chapter("Megitsune Reprise")]
        assert [i for i, _ in utils.matching_chapters(chapters, "megi")] == [0, 2]

    def test_matching_chapters_with_no_search_returns_them_all(self):
        chapters = [chapter("A"), chapter("B")]
        assert len(utils.matching_chapters(chapters, "")) == 2


class TestSuggestedSearch:
    ROOT = "/media/MusicVids"

    def _suggest(self, path, name, kind="file"):
        return utils.suggest_search_query(
            {"path": f"{self.ROOT}/{path}", "type": kind, "display_name": name}, self.ROOT
        )

    def test_a_numbered_file_borrows_the_album_from_its_folder(self):
        suggestion = self._suggest(
            "BABYMETAL/10-BABYMETAL-BUDOKAN_THE-ONE-EDITION/1. Doomsday I, II.mkv",
            "1. Doomsday I, II",
        )
        assert suggestion == "10 BABYMETAL BUDOKAN THE ONE EDITION Doomsday I, II"

    def test_what_the_search_would_read_as_syntax_is_left_out(self):
        # "-THE" is "without the" to MusicBrainz: the release was unfindable.
        name = "[TM] BABYMETAL AWAKENS -THE SUN ALSO RISES- [BDRip 1920x1080 x264 FLAC]"
        suggestion = self._suggest(f"BABYMETAL/{name}/{name}.mkv", name)
        assert suggestion == "BABYMETAL AWAKENS THE SUN ALSO RISES"
        assert utils.search_words('AC/DC "Live" +1 (x)') == "AC DC Live 1 x"

    def test_a_folder_already_in_the_name_adds_nothing(self):
        suggestion = self._suggest(
            "BABYMETAL/BABYMETAL - LIVE AT INTUIT DOME.mkv", "BABYMETAL - LIVE AT INTUIT DOME"
        )
        assert suggestion == "BABYMETAL LIVE AT INTUIT DOME"

    def test_the_library_root_is_never_used(self):
        assert self._suggest("Showtime.mkv", "Showtime") == "Showtime"

    def test_a_blu_ray_title_number_is_dropped(self):
        suggestion = self._suggest(
            "Worlds Collide Live", "Worlds Collide Live - Title 8", kind="bluray"
        )
        assert suggestion == "Worlds Collide Live"

    def test_only_two_folders_are_borrowed(self):
        suggestion = self._suggest("A/B/C/D/song.mkv", "song")
        assert suggestion == "C D song"

    def test_a_name_that_is_only_a_number_is_kept(self):
        assert self._suggest("1.mkv", "1") == "1"
