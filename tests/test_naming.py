# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Whether a video's chapters are identified yet."""

import pytest

from mediabrowser.core import naming


def video(titles, duration=3600.0):
    return {"duration": duration, "chapters": [{"title": t} for t in titles]}


class TestPlaceholders:
    @pytest.mark.parametrize("title", [
        None, "", "  ", "Chapter 01", "chapter 1", "Ch. 3", "Scene 12", "Title 2", "07",
        "(01)00:00:00:000", "00:12:34.000", "1:02:03", "Track-4", "Chapitre 5",
    ])
    def test_these_are_only_numbers(self, title):
        assert naming.is_placeholder(title)

    @pytest.mark.parametrize("title", [
        "Megitsune", "Song 4", "Song 3 (BABYMETAL x Slaughter to Prevail)", "4 no Uta",
        "Intro to KARATE", "Chapter 01 - Opening", "Doki Doki☆Morning", "1999",
    ])
    def test_these_are_names(self, title):
        assert not naming.is_placeholder(title)


class TestStatus:
    def test_every_chapter_named(self):
        s = naming.status(video(["A", "B"]))
        assert (s.state, s.named, s.total, s.needs_work) == (naming.NAMED, 2, 2, False)
        assert s.describe() == "All named"

    def test_placeholders_dont_count(self):
        s = naming.status(video(["Chapter 01", "Megitsune", None]))
        assert (s.state, s.named) == (naming.PARTLY, 1)
        assert s.describe() == "1 of 3 named" and s.needs_work

    def test_nothing_named(self):
        assert naming.status(video(["Chapter 01", "Chapter 02"])).state == naming.UNNAMED

    def test_a_long_single_chapter_needs_splitting(self):
        long = naming.status(video([None], duration=5400.0))
        assert (long.state, long.needs_work, long.describe()) == (naming.UNSPLIT, True, "Not split")

    def test_a_short_one_named_only_after_its_file_is_unverified(self):
        clip = video(["Megitsune"], duration=240.0)
        clip["chapters"][0]["source"] = naming.FILENAME_SOURCE
        s = naming.status(clip)
        assert (s.state, s.needs_work, s.describe()) == (naming.UNVERIFIED, True, "Unverified")
        clip["chapters"][0]["source"] = "manual"
        s = naming.status(clip)
        assert (s.state, s.needs_work, s.describe()) == (naming.SINGLE, False, "—")

    def test_fraction_orders_least_named_first(self):
        order = sorted(
            [video(["A", "B"]), video([None], 5400.0), video(["A", None]), video([None], 60.0)],
            key=lambda v: naming.status(v).fraction,
        )
        assert [naming.status(v).state for v in order] == [
            naming.UNSPLIT, naming.PARTLY, naming.UNVERIFIED, naming.NAMED,
        ]


class TestNamedAfterTheFile:
    def test_a_single_chapter_takes_the_file_name_cleaned(self):
        from mediabrowser.core import library, utils

        data = {"videos": {
            "clip": {"display_name": "03. BABYMETAL - HEADBANGER!  [4K 50fps]",
                     "duration": 240.0, "chapters": [{"title": None, "source": "auto-numbered"}]},
            "named": {"display_name": "x", "duration": 240.0,
                      "chapters": [{"title": "Kept", "source": "manual"}]},
            "two": {"display_name": "y", "duration": 600.0,
                    "chapters": [{"title": None}, {"title": None}]},
            "extra": {"type": "bluray", "display_name": "Disc - Title 3", "duration": 60.0,
                      "chapters": [{"title": None, "source": "auto-numbered"}]},
        }}
        for video in data["videos"].values():
            video.setdefault("type", "file")
        assert library.name_single_chapters(data) == 1
        clip = data["videos"]["clip"]["chapters"][0]
        assert clip == {"title": "BABYMETAL - HEADBANGER!", "source": naming.FILENAME_SOURCE}
        assert data["videos"]["named"]["chapters"][0]["title"] == "Kept"
        assert data["videos"]["extra"]["chapters"][0]["title"] is None, "not a Blu-ray title"
        assert utils.matches_search(data["videos"]["clip"], data["videos"]["clip"]["chapters"],
                                    "headbanger")
        # Renamed, it follows; run again, nothing more to do.
        data["videos"]["clip"]["display_name"] = "Headbanger!!"
        assert library.name_single_chapters(data) == 1
        assert clip["title"] == "Headbanger!!"
        assert library.name_single_chapters(data) == 0

    def test_splitting_doesnt_hand_the_whole_files_name_to_its_first_part(self):
        from mediabrowser.core import chapter_edit, marking

        chapters = [{"index": 0, "start": 0.0, "end": 600.0, "title": "Whole Show",
                     "source": naming.FILENAME_SOURCE}]
        split, _ = chapter_edit.split(chapters, 300.0)
        assert [c["title"] for c in split] == [None, None]
        sheet = marking.MarkSheet(600.0, chapters)
        assert sheet.marks()[0].title is None
