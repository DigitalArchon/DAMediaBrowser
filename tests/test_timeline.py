# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The chapter editor's timeline: what part of the video it shows, and
what it draws there."""

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent

from mediabrowser.core.audio_levels import Levels

PySide6 = pytest.importorskip("PySide6")


@pytest.fixture
def timeline(app):
    """A 2-hour video's, 816 wide: 800 of track, nine seconds to the pixel."""
    from mediabrowser.gui.timeline import Timeline

    strip = Timeline(min_gap=2.0)
    strip.resize(816, 50)
    strip.set_marks([0.0, 1800.0, 3600.0], [None, "Northbound", "Song 7"], 7200.0)
    return strip


def wheel(strip, x, steps, modifiers=Qt.NoModifier):
    position = QPointF(x, 25)
    strip.wheelEvent(QWheelEvent(
        position, strip.mapToGlobal(position), QPoint(0, 0), QPoint(0, 120 * steps),
        Qt.NoButton, modifiers, Qt.NoScrollPhase, False,
    ))


class TestZoom:
    def test_it_starts_on_the_whole_video(self, timeline):
        assert timeline.view() == (0.0, 7200.0) and not timeline.zoomed()
        assert timeline.x_for(3600.0) == 408.0
        assert timeline.seconds_at(408.0) == 3600.0

    def test_the_wheel_zooms_about_the_pointer(self, timeline):
        wheel(timeline, 408.0, 1)
        start, span = timeline.view()
        assert span == pytest.approx(4800.0) and start == pytest.approx(1200.0)
        assert timeline.seconds_at(408.0) == pytest.approx(3600.0), "what was under it still is"
        wheel(timeline, 408.0, -1)
        assert timeline.view() == (0.0, 7200.0)

    def test_it_zooms_in_to_half_a_minute_and_no_further(self, timeline):
        for _ in range(40):
            timeline.zoom_in()
        assert timeline.view()[1] == 30.0
        # A pixel is now well under a tenth of a second.
        assert timeline.seconds_at(409.0) - timeline.seconds_at(408.0) < 0.05

    def test_it_never_shows_past_either_end(self, timeline):
        timeline.zoom(4.0, about=7200.0)
        assert timeline.view() == (5400.0, 1800.0)
        timeline.pan(-99999.0)
        assert timeline.view() == (0.0, 1800.0)

    def test_shift_and_the_wheel_move_along(self, timeline):
        timeline.zoom(4.0, about=0.0)
        wheel(timeline, 408.0, -2, Qt.ShiftModifier)
        assert timeline.view() == (360.0, 1800.0)

    def test_a_video_shorter_than_the_least_cant_be_zoomed(self, timeline):
        timeline.set_marks([0.0], [None], 20.0)
        timeline.zoom_in()
        assert not timeline.zoomed()


class TestKeepingThePlayheadInSight:
    def test_the_view_goes_with_a_playhead_that_runs_off_it(self, timeline):
        timeline.zoom(4.0, about=0.0)
        timeline.set_playhead(1799.0)
        timeline.set_playhead(1801.0)
        start, span = timeline.view()
        assert start == pytest.approx(1801.0 - 180.0) and span == 1800.0

    def test_but_not_after_one_it_was_moved_away_from(self, timeline):
        timeline.zoom(4.0, about=0.0)
        timeline.set_playhead(100.0)
        timeline.pan(3600.0)
        timeline.set_playhead(101.0)
        assert timeline.view() == (3600.0, 1800.0)
        timeline.reveal(101.0)
        assert timeline.in_view(101.0), "until it is asked for"


class TestBoundaries:
    def test_only_a_boundary_on_show_can_be_taken_hold_of(self, timeline):
        assert timeline.mark_at(timeline.x_for(1800.0) + 3) == 1
        assert timeline.mark_at(8.0) is None, "never the first"
        timeline.zoom(8.0, about=3600.0)
        assert timeline.mark_at(timeline.x_for(1800.0)) is None
        assert timeline.mark_at(timeline.x_for(3600.0)) == 2

    def test_how_far_one_can_be_dragged(self, timeline):
        assert timeline.room(1) == (2.0, 3598.0)
        assert timeline.room(2) == (1802.0, 7198.0)


class TestLevels:
    def test_they_are_drawn_a_point_to_a_pixel_the_loudest_the_full_height(self, timeline):
        loud, quiet = [-10.0] * 7200, [-46.0] * 7200  # the first hour, the second
        timeline.set_levels(Levels(step=0.5, full=loud + quiet, bass=quiet + loud))
        assert timeline.has_levels()
        track = timeline._track()
        loudness, bass = timeline._levels_drawn(track)
        assert loudness.size() == 802 and bass.size() == 802
        assert loudness.at(1).y() == track.top() and loudness.at(800).y() == track.bottom()
        assert bass.at(1).y() == track.bottom() and bass.at(800).y() == track.top()
        timeline.grab()  # and painting them doesn't fall over

    def test_zoomed_they_are_drawn_for_the_part_on_show(self, timeline):
        timeline.set_levels(Levels(step=0.5, full=[-10.0] * 7200 + [-46.0] * 7200,
                                   bass=[-10.0] * 14400))
        timeline.zoom(4.0, about=7200.0)
        track = timeline._track()
        loudness, _bass = timeline._levels_drawn(track)
        assert loudness.at(400).y() == track.bottom(), "the quiet second hour"

    def test_a_gap_narrower_than_a_pixel_still_shows(self, timeline):
        """Nine seconds to the pixel: under the peak of each, a six-second
        gap between songs was nowhere to be seen."""
        full = [-10.0] * 14400
        full[3600:3612] = [-46.0] * 12  # six quiet seconds at half an hour
        timeline.set_levels(Levels(step=0.5, full=full, bass=full))
        track = timeline._track()
        loudness, _bass = timeline._levels_drawn(track)
        assert loudness.at(1 + 200).y() > track.top() + track.height() / 2
        assert loudness.at(1 + 190).y() == track.top()

    def test_none_is_none(self, timeline):
        timeline.set_levels(Levels(step=0.5, full=[-10.0], bass=[-10.0]))
        timeline.set_levels(None)
        assert not timeline.has_levels()
        timeline.grab()
