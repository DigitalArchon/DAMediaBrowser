# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Marking songs by hand: the sheet the Mark Songs window works on."""

import pytest

from mediabrowser.core import marking


class TestMarking:
    def test_a_fresh_sheet_is_one_mark_at_the_start(self):
        sheet = marking.MarkSheet(600.0)
        assert [m.start for m in sheet.marks()] == [0.0]
        assert sheet.chapters() == [
            {"index": 0, "start": 0.0, "end": 600.0, "title": None, "source": "auto-numbered"}
        ]

    def test_marks_go_in_time_order_whatever_order_they_are_made(self):
        sheet = marking.MarkSheet(600.0)
        assert sheet.mark(400.0) == 1
        assert sheet.mark(200.0) == 1
        assert [m.start for m in sheet.marks()] == [0.0, 200.0, 400.0]
        assert [c["end"] for c in sheet.chapters()] == [200.0, 400.0, 600.0]

    def test_a_mark_on_top_of_another_is_refused(self):
        sheet = marking.MarkSheet(600.0)
        sheet.mark(200.0)
        with pytest.raises(marking.MarkError, match="already a mark at 3:20"):
            sheet.mark(201.0)
        with pytest.raises(marking.MarkError, match="outside"):
            sheet.mark(599.5)
        with pytest.raises(marking.MarkError, match="outside"):
            sheet.mark(-1.0)

    def test_naming_removing_moving(self):
        sheet = marking.MarkSheet(600.0)
        sheet.mark(200.0, "Two")
        sheet.mark(400.0)
        sheet.rename(2, "  Three ")
        assert [m.title for m in sheet.marks()] == [None, "Two", "Three"]
        assert sheet.nudge(2, -1.0) == 2
        assert sheet.marks()[2].start == 399.0
        assert sheet.move(2, 100.0) == 1, "moved before Two, so it's now second"
        assert [m.title for m in sheet.marks()] == [None, "Three", "Two"]
        sheet.remove(1)
        assert [m.title for m in sheet.marks()] == [None, "Two"]
        with pytest.raises(marking.MarkError):
            sheet.remove(0)
        with pytest.raises(marking.MarkError):
            sheet.move(0, 5.0)

    def test_which_mark_is_in_force(self):
        sheet = marking.MarkSheet(600.0)
        sheet.mark(200.0)
        assert sheet.at(0.0) == 0 and sheet.at(199.9) == 0 and sheet.at(200.0) == 1
        assert sheet.length_of(0) == 200.0 and sheet.length_of(1) == 400.0

    def test_starting_from_existing_chapters(self):
        chapters = [
            {"start": 0.0, "end": 100.0, "title": "Opening"},
            {"start": 100.0, "end": 300.0, "title": None},
        ]
        sheet = marking.MarkSheet(300.0, chapters)
        assert [(m.start, m.title) for m in sheet.marks()] == [(0.0, "Opening"), (100.0, None)]
        sheet.clear()
        assert [(m.start, m.title) for m in sheet.marks()] == [(0.0, None)]


class TestNamesInOrder:
    def test_each_new_mark_takes_the_next_unused_name(self):
        sheet = marking.MarkSheet(900.0)
        sheet.set_names("Opening\n\nMegitsune\n  Karate  \n")
        assert sheet.names() == ["Opening", "Megitsune", "Karate"]
        assert sheet.next_name() == "Opening"
        sheet.rename(0, "Opening")
        assert sheet.next_name() == "Megitsune"
        sheet.mark(200.0)
        sheet.mark(400.0)
        sheet.mark(600.0)
        assert [m.title for m in sheet.marks()] == ["Opening", "Megitsune", "Karate", None]
        assert sheet.unused_names() == [] and sheet.next_name() is None

    def test_a_typed_name_beats_the_list(self):
        sheet = marking.MarkSheet(900.0)
        sheet.set_names(["A", "B"])
        sheet.mark(100.0, "Encore break")
        assert sheet.unused_names() == ["A", "B"]

    def test_names_can_be_put_on_marks_made_first(self):
        sheet = marking.MarkSheet(900.0)
        for t in (100.0, 200.0, 300.0):
            sheet.mark(t)
        sheet.set_names(["One", "Two", "Three", "Four", "Five"])
        assert sheet.apply_names_in_order() == 4
        assert [m.title for m in sheet.marks()] == ["One", "Two", "Three", "Four"]
        assert sheet.unused_names() == ["Five"]
        assert sheet.apply_names_in_order(from_index=1) == 3
        assert [m.title for m in sheet.marks()] == ["One", "One", "Two", "Three"]

    def test_the_same_name_twice_in_the_list_is_two_songs(self):
        sheet = marking.MarkSheet(900.0)
        sheet.set_names(["Song", "Song"])
        sheet.mark(100.0)
        assert sheet.next_name() == "Song"
        sheet.mark(200.0)
        assert sheet.next_name() is None

    def test_chapters_carry_the_names(self):
        sheet = marking.MarkSheet(300.0)
        sheet.set_names(["First", "Second"])
        sheet.rename(0, "First")
        sheet.mark(150.0)
        assert [(c["title"], c["source"]) for c in sheet.chapters()] == [
            ("First", "manual"), ("Second", "manual")
        ]
