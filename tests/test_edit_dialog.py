# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Edit Tracklist: names dragged to the chapter they belong on, while the
chapters stay put."""

import pytest

PySide6 = pytest.importorskip("PySide6")

from mediabrowser.gui.dialogs.edit_dialog import (  # noqa: E402
    ABOVE,
    BELOW,
    FROM_TABLE,
    FROM_UNUSED,
    ONTO,
    EditTracklistDialog,
)


@pytest.fixture
def dialog(app):
    def make(*titles):
        video = {"chapters": [{"title": t, "start": i * 60.0, "end": (i + 1) * 60.0}
                              for i, t in enumerate(titles)]}
        return EditTracklistDialog(None, video)
    return make


class TestDragging:
    def test_onto_an_unnamed_chapter_it_fills_it(self, dialog):
        d = dialog("Intro", "Megitsune", None, "Karate")
        d.drop(FROM_TABLE, 1, 2, ONTO)
        assert d.titles() == ["Intro", "", "Megitsune", "Karate"]

    def test_onto_a_named_one_it_goes_there_and_the_rest_close_up(self, dialog):
        d = dialog("A", "B", "C", "D", "E")
        d.drop(FROM_TABLE, 1, 3, ONTO)
        assert d.titles() == ["A", "C", "D", "B", "E"]
        d.drop(FROM_TABLE, 3, 0, ONTO)
        assert d.titles() == ["B", "A", "C", "D", "E"]

    def test_between_two_it_goes_between_them(self, dialog):
        d = dialog("A", "B", "C", "D")
        d.drop(FROM_TABLE, 0, 2, BELOW)
        assert d.titles() == ["B", "C", "A", "D"]
        d.drop(FROM_TABLE, 3, 1, ABOVE)
        assert d.titles() == ["B", "D", "C", "A"]

    def test_the_chapters_stay_put(self, dialog):
        d = dialog("A", "B", "C")
        lengths = [d.tree.topLevelItem(i).text(1) for i in range(3)]
        d.drop(FROM_TABLE, 0, 2, ONTO)
        assert [d.tree.topLevelItem(i).text(1) for i in range(3)] == lengths
        assert d.tree.topLevelItemCount() == 3

    def test_out_to_unused_and_back_in(self, dialog):
        d = dialog("A", "B", "C")
        d.set_aside(1)
        assert d.titles() == ["A", "", "C"] and d._unused == ["B"]
        d.drop(FROM_UNUSED, 0, 1, ONTO)
        assert d.titles() == ["A", "B", "C"] and d._unused == []

    def test_an_unused_name_between_named_ones_closes_the_next_gap(self, dialog):
        d = dialog("A", "C", None, "D")
        d._unused = ["B"]
        d.drop(FROM_UNUSED, 0, 0, BELOW)
        assert d.titles() == ["A", "B", "C", "D"] and d._unused == []

    def test_with_no_gap_the_last_name_waits_in_unused(self, dialog):
        d = dialog("A", "C", "D")
        d._unused = ["B"]
        d.drop(FROM_UNUSED, 0, 1, ABOVE)
        assert d.titles() == ["A", "B", "C"] and d._unused == ["D"]

    def test_an_unnamed_chapter_has_nothing_to_drag(self, dialog):
        d = dialog("A", None)
        d.drop(FROM_TABLE, 1, 0, ONTO)
        assert d.titles() == ["A", ""]
