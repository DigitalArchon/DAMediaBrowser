# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

import pytest

from mediabrowser.core import chapter_edit


def chapters(*spans, titles=None):
    titles = titles or [None] * len(spans)
    return [
        {
            "index": i,
            "start": s,
            "end": e,
            "title": t,
            "source": "manual" if t else "auto-numbered",
        }
        for i, ((s, e), t) in enumerate(zip(spans, titles, strict=True))
    ]


class TestSplit:
    def test_it_splits_the_chapter_the_position_is_in(self):
        before = chapters((0, 100), (100, 300), titles=["Intro", "Megitsune"])
        after, _ = chapter_edit.split(before, 150)
        assert [(c["start"], c["end"]) for c in after] == [(0, 100), (100, 150), (150, 300)]

    def test_the_first_half_keeps_the_name(self):
        before = chapters((0, 300), titles=["Megitsune"])
        after, _ = chapter_edit.split(before, 120)
        assert after[0]["title"] == "Megitsune"
        assert after[1]["title"] is None
        assert after[1]["source"] == "auto-numbered"

    def test_chapters_after_the_split_shift_along_one(self):
        before = chapters((0, 100), (100, 200), (200, 300))
        after, index_map = chapter_edit.split(before, 150)
        assert [c["index"] for c in after] == [0, 1, 2, 3]
        assert [index_map(i) for i in range(3)] == [0, 1, 3]

    def test_the_original_list_is_left_alone(self):
        before = chapters((0, 300))
        chapter_edit.split(before, 120)
        assert len(before) == 1 and before[0]["end"] == 300

    def test_a_split_placed_by_hand_is_not_an_estimate(self):
        before = chapters((0, 100), (100, 300))
        before[1]["estimated"] = True
        after, _ = chapter_edit.split(before, 200)
        assert after[1].get("estimated") is True, "its start was not touched"
        assert "estimated" not in after[2]

    def test_it_refuses_to_split_right_at_a_boundary(self):
        with pytest.raises(chapter_edit.EditError):
            chapter_edit.split(chapters((0, 100), (100, 200)), 100.5)


class TestMerge:
    def test_a_chapter_absorbs_the_next(self):
        after, index_map = chapter_edit.merge_with_next(
            chapters((0, 100), (100, 200), (200, 300)), 0
        )
        assert [(c["start"], c["end"]) for c in after] == [(0, 200), (200, 300)]
        assert [index_map(i) for i in range(3)] == [0, 0, 1]

    def test_the_first_name_wins(self):
        after, _ = chapter_edit.merge_with_next(
            chapters((0, 100), (100, 200), titles=["One", "Two"]), 0
        )
        assert after[0]["title"] == "One"

    def test_the_second_name_is_kept_if_the_first_had_none(self):
        after, _ = chapter_edit.merge_with_next(
            chapters((0, 100), (100, 200), titles=[None, "Two"]), 0
        )
        assert after[0]["title"] == "Two"
        assert after[0]["source"] == "manual"

    def test_the_last_chapter_has_nothing_to_merge_with(self):
        with pytest.raises(chapter_edit.EditError):
            chapter_edit.merge_with_next(chapters((0, 100), (100, 200)), 1)


class TestMoveStart:
    def test_moving_a_start_moves_the_previous_end_with_it(self):
        after, _ = chapter_edit.move_start(chapters((0, 100), (100, 200)), 1, -2.0)
        assert after[0]["end"] == 98.0
        assert after[1]["start"] == 98.0

    def test_a_moved_start_is_no_longer_an_estimate(self):
        before = chapters((0, 100), (100, 200))
        before[1]["estimated"] = True
        after, _ = chapter_edit.move_start(before, 1, 1.0)
        assert "estimated" not in after[1]

    def test_neither_chapter_can_be_squeezed_to_nothing(self):
        after, _ = chapter_edit.move_start(chapters((0, 100), (100, 200)), 1, -500.0)
        assert after[1]["start"] == 1.0

    def test_the_first_chapter_cannot_move(self):
        with pytest.raises(chapter_edit.EditError):
            chapter_edit.move_start(chapters((0, 100), (100, 200)), 0, 1.0)


class TestChapterAt:
    def test_it_finds_the_chapter_playing(self):
        assert chapter_edit.chapter_at(chapters((0, 100), (100, 200)), 150) == 1

    def test_past_the_end_is_the_last_chapter(self):
        assert chapter_edit.chapter_at(chapters((0, 100), (100, 200)), 250) == 1
