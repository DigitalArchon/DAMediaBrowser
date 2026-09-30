# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""A title that is a translation keeps the song's own title beside it, so
a search in either script finds the song, and a rescan keeps both."""

from mediabrowser.core import library, utils


def chapter(title=None, **extra):
    return {"index": 0, "start": 0.0, "end": 100.0, "title": title, "source": "manual", **extra}


class TestSettingTitles:
    def test_a_translation_keeps_the_original(self):
        ch = chapter()
        utils.set_chapter_title(ch, "Megitsune", "ai", original_title="メギツネ")
        assert ch["title"] == "Megitsune" and ch["original_title"] == "メギツネ"
        assert ch["source"] == "ai"

    def test_an_original_the_same_as_the_title_is_not_kept(self):
        ch = chapter()
        utils.set_chapter_title(ch, "Karate", "ai", original_title="Karate")
        assert "original_title" not in ch

    def test_renaming_by_hand_drops_an_original_that_no_longer_fits(self):
        ch = chapter("Megitsune", original_title="メギツネ")
        utils.set_chapter_title(ch, "Megitsune (Live)")
        assert "original_title" not in ch

    def test_saving_the_same_title_again_keeps_it(self):
        ch = chapter("Megitsune", original_title="メギツネ")
        utils.set_chapter_title(ch, "Megitsune")
        assert ch["original_title"] == "メギツネ"
        assert ch["source"] == "manual"

    def test_clearing_the_title_clears_the_original(self):
        ch = chapter("Megitsune", original_title="メギツネ")
        utils.set_chapter_title(ch, "")
        assert ch["title"] is None and "original_title" not in ch


class TestSearching:
    VIDEO = {"display_name": "Budokan"}
    CHAPTERS = [
        {"title": "Megitsune", "original_title": "メギツネ"},
        {"title": "Karate"},
    ]

    def test_either_script_finds_the_song(self):
        assert utils.matches_search(self.VIDEO, self.CHAPTERS, "メギツネ")
        assert utils.matches_search(self.VIDEO, self.CHAPTERS, "megitsune")
        assert not utils.matches_search(self.VIDEO, self.CHAPTERS, "chocolate")

    def test_the_matching_chapters_too(self):
        assert [i for i, _ in utils.matching_chapters(self.CHAPTERS, "ギツ")] == [0]


class TestRescanning:
    def test_ai_names_and_originals_survive_a_rescan(self):
        old = [chapter("Megitsune", source="ai", original_title="メギツネ")]
        new = [{"start": 0.0, "end": 100.0, "title": None}]
        merged = library._merge_chapters(new, old)
        assert merged[0]["title"] == "Megitsune"
        assert merged[0]["source"] == "ai"
        assert merged[0]["original_title"] == "メギツネ"

    def test_embedded_names_are_still_re_read(self):
        old = [chapter("Old name", source="embedded")]
        new = [{"start": 0.0, "end": 100.0, "title": "New name"}]
        assert library._merge_chapters(new, old)[0]["title"] == "New name"
