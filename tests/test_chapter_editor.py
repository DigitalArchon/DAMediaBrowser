# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual Edit in the main area, driven offscreen with a stand-in for
mpv that remembers what it was told and reports a playhead."""

import pytest
from PySide6.QtCore import Qt

from mediabrowser.core import library, store
from tests.test_chapters_dialog import file_video

PySide6 = pytest.importorskip("PySide6")


class FakePlayer:
    def __init__(self):
        self.calls = []
        self.position_value = 0.0
        self.paused = True
        self.running = False

    def open_for_editing(self, video, start=0.0, window_id=None, paused=True):
        self.calls.append(("open", start, window_id, paused))
        self.position_value, self.paused, self.running = start, paused, True

    def stop(self):
        self.calls.append(("stop",))
        self.running = False

    def is_active(self):
        return self.running

    def take_exit(self):
        return None

    def position(self):
        return self.position_value

    def is_paused(self):
        return self.paused

    def toggle_pause(self):
        self.calls.append(("toggle",))
        self.paused = not self.paused

    def set_paused(self, paused):
        self.paused = paused

    def seek_exact(self, seconds):
        self.calls.append(("seek", round(seconds, 2)))
        self.position_value = seconds

    def frame_step(self, forward=True):
        self.calls.append(("frame", forward))
        self.position_value += 0.04 if forward else -0.04


@pytest.fixture
def editor(app, window, tmp_path):
    """The window's editor on a 900s video with chapters at 0 and 300."""
    player = FakePlayer()
    window.editor._player_factory = lambda: player
    video = file_video(tmp_path, 900.0, starts=(0.0, 300.0))
    video["chapters"][1].update(title="Megitsune", source="menu")
    window.data = store.default_library(str(tmp_path / "media"))
    window.data["videos"] = {"v": video}
    window.refresh_library()
    window.open_video("v")
    window.edit_chapters()
    app.processEvents()  # the player starts once the page is up
    return window.editor, player, video, window


def at(editor, player, seconds):
    player.position_value = seconds
    editor._tick()


class TestOpening:
    def test_it_takes_the_main_area_with_the_video_paused(self, editor):
        ed, player, _video, window = editor
        assert window.pages.currentWidget() is ed
        assert window.now_playing_bar.isHidden()
        assert player.calls[0][0] == "open" and player.calls[0][3] is True
        assert ed.tree.topLevelItemCount() == 2
        assert ed.timeline.starts == [0.0, 300.0]

    def test_offscreen_it_says_the_video_is_elsewhere(self, editor):
        ed, player, _video, _window = editor
        assert not ed.elsewhere.isHidden() and ed.surface.isHidden()
        assert player.calls[0][2] is None, "no window to draw into offscreen"


class TestTheWindowToItself:
    """Playing from the queue while editing would start a second mpv over
    the editor's: the same song twice at once. And anything that changed
    the library changed it under the chapters being edited."""

    def test_nothing_else_can_be_played_while_editing(self, editor):
        ed, player, _video, window = editor
        playback, file_menu, chapters = (
            {a.text(): a for a in window.menuBar().actions()}[name]
            for name in ("&Playback", "&File", "&Chapters")
        )
        playlists = next(a for a in playback.menu().actions() if a.text() == "Playlists")
        assert not playlists.isEnabled(), "Playback → Playlists played one"
        assert not file_menu.isEnabled() and not chapters.isEnabled()
        assert playback.isEnabled(), "its Play / Pause drives the editor"
        window.play_from("v", 1)
        assert window.now_playing is None and not window.session.running()
        assert "Finish with Manual Edit" in window.status_label.text()
        ed.request_close()
        assert playlists.isEnabled() and file_menu.isEnabled() and chapters.isEnabled()
        window.play_from("v", 1)
        assert window.now_playing is not None

    def test_the_library_cant_be_changed_under_it(self, editor):
        ed, _player, _video, window = editor
        assert not window.libraries.isEnabled() and not window.toolbar.isEnabled()
        ed.request_close()
        assert window.libraries.isEnabled() and window.toolbar.isEnabled()

    def test_the_keys_of_the_menus_switched_off_do_nothing(self, editor, monkeypatch):
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication

        ed, player, _video, window = editor
        calls = []
        monkeypatch.setattr(window, "rescan", lambda *a, **k: calls.append("rescan"))
        monkeypatch.setattr(window, "_merge_selected", lambda: calls.append("merge"))
        window.show()
        window.activateWindow()
        QApplication.processEvents()
        at(ed, player, 600.0)
        for key in (Qt.Key_R, Qt.Key_M, Qt.Key_K):
            QTest.keyClick(window, key, Qt.ControlModifier)
        assert calls == []
        assert [m.start for m in ed.sheet.marks()] == [0.0, 300.0, 600.0], (
            "Ctrl+K still inserts a chapter"
        )

    def test_closing_the_window_asks_about_unsaved_chapters(self, editor, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        ed, player, video, window = editor
        at(ed, player, 600.0)
        ed.insert_here()
        window.show()
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
        assert window.close() is False
        assert ed.active() and window.isVisible(), "still editing"
        assert ("stop",) not in player.calls
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Discard)
        assert window.close() is True
        assert not ed.active() and len(video["chapters"]) == 2

    def test_a_save_for_a_video_no_longer_there_says_so(self, editor):
        ed, player, _video, window = editor
        at(ed, player, 600.0)
        ed.insert_here()
        del window.data["videos"]["v"]
        ed.save()
        assert "weren't saved" in window.status_label.text()

    def test_the_queue_hides_while_editing_and_comes_back(self, editor):
        ed, _player, _video, window = editor
        assert window.queue_dock.isHidden()
        assert not window.queue_dock.toggleViewAction().isEnabled(), "nor from the View menu"
        ed.request_close()
        assert not window.queue_dock.isHidden()
        assert window.queue_dock.toggleViewAction().isEnabled()

    def test_it_comes_back_after_saving_too(self, editor):
        ed, _player, _video, window = editor
        ed.save()
        assert not window.queue_dock.isHidden()

    def test_a_queue_that_was_closed_stays_closed(self, editor):
        ed, _player, _video, window = editor
        ed.request_close()
        window.queue_dock.hide()
        window.edit_chapters()
        window.editor.request_close()
        assert window.queue_dock.isHidden()

    def test_closing_the_window_while_editing_remembers_the_queue_as_it_was(self, editor):
        _ed, _player, _video, window = editor
        window.close()
        assert not window.queue_dock.isHidden()


class TestAskingMpvOffTheGuiThread:
    def test_a_position_asked_before_a_seek_cant_undo_it(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 100.0)
        stale = ed._ask_mpv()  # asked at 100s ...
        ed.seek(250.0)  # ... and the seek made before it arrived
        ed._mpv_answered(stale)
        assert ed._position == 250.0

    def test_a_fresh_answer_moves_the_playhead(self, editor):
        ed, player, _video, _window = editor
        player.position_value = 120.0
        ed._mpv_answered(ed._ask_mpv())
        assert ed._position == 120.0


class TestMovingAround:
    def test_steps_and_frames(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 100.0)
        ed.handle_key(Qt.Key_Right, Qt.NoModifier)
        ed.handle_key(Qt.Key_Right, Qt.ShiftModifier)
        ed.handle_key(Qt.Key_Left, Qt.ControlModifier)
        ed.handle_key(Qt.Key_Period, Qt.NoModifier)
        assert [c for c in player.calls if c[0] in ("seek", "frame")] == [
            ("seek", 101.0), ("seek", 106.0), ("seek", 96.0), ("frame", True),
        ]
        assert ed.position_label.text().startswith("1:36.0 / 15:00")

    def test_chapter_to_chapter(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 120.0)
        ed.handle_key(Qt.Key_PageDown, Qt.NoModifier)
        assert player.position_value == 300.0
        ed.handle_key(Qt.Key_PageUp, Qt.NoModifier)
        assert player.position_value == 0.0

    def test_the_window_keys_drive_it_while_it_is_open(self, editor):
        ed, player, _video, window = editor
        at(ed, player, 50.0)
        window._nudge(10.0)
        window.toggle_play_pause()
        assert ("seek", 60.0) in player.calls and ("toggle",) in player.calls
        window.split_at_playhead()
        assert [m.start for m in ed.sheet.marks()] == [0.0, 60.0, 300.0]

    def test_space_plays_and_pauses(self, editor):
        ed, player, _video, _window = editor
        ed.handle_key(Qt.Key_Space, Qt.NoModifier)
        assert player.paused is False and ed.play_button.text() == "Pause"


class TestClickingAChapter:
    def test_a_click_goes_to_where_it_starts_without_playing(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 120.0)
        ed.tree.itemClicked.emit(ed.tree.topLevelItem(1), 2)
        assert player.position_value == 300.0 and player.paused is True
        assert ed.timeline.playhead == 300.0

    def test_a_click_while_playing_carries_on_from_there(self, editor):
        ed, player, _video, _window = editor
        ed.play_pause()
        ed.tree.itemClicked.emit(ed.tree.topLevelItem(1), 2)
        assert player.position_value == 300.0 and player.paused is False

    def test_a_double_click_plays_from_there(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 120.0)
        ed.tree.itemDoubleClicked.emit(ed.tree.topLevelItem(1), 2)
        assert player.position_value == 300.0 and player.paused is False
        assert ed.play_button.text() == "Pause"

    def test_real_clicks_do_the_same_and_edit_nothing(self, editor):
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QAbstractItemView

        ed, player, _video, window = editor
        window.show()
        at(ed, player, 120.0)
        for column in range(4):
            spot = ed.tree.visualItemRect(ed.tree.topLevelItem(1)).center()
            spot.setX(ed.tree.columnViewportPosition(column) + 5)
            QTest.mouseClick(ed.tree.viewport(), Qt.LeftButton, Qt.NoModifier, spot)
            assert player.position_value == 300.0 and player.paused is True
            QTest.mouseDClick(ed.tree.viewport(), Qt.LeftButton, Qt.NoModifier, spot)
            assert ed.tree.state() != QAbstractItemView.EditingState, f"column {column}"
            assert player.paused is False
            ed.play_pause()
            at(ed, player, 120.0)


class TestTimesAddUp:
    def shown(self, ed):
        from mediabrowser.core import utils

        def tenths(text):
            clock, tenth = text.split(".")
            total = 0
            for part in clock.split(":"):
                total = total * 60 + int(part)
            return total * 10 + int(tenth)

        rows = [ed.tree.topLevelItem(i) for i in range(ed.tree.topLevelItemCount())]
        starts = [tenths(row.text(1)) for row in rows]
        lengths = [tenths(row.text(3)) for row in rows]
        return starts, lengths, utils.format_tenths(sum(lengths) / 10)

    def test_lengths_add_up_to_the_video_through_every_kind_of_change(self, editor):
        ed, player, _video, _window = editor

        def check():
            starts, lengths, total = self.shown(ed)
            assert total == "15:00.0", (starts, lengths)
            for i in range(len(starts) - 1):
                assert starts[i] + lengths[i] == starts[i + 1], (starts, lengths)
            assert ed.total.text() == f"{len(starts)} chapters · 15:00.0 in all"

        check()
        for position in (100.44, 512.55, 700.66):
            at(ed, player, position)
            ed.insert_here()
            check()
        ed._on_boundary_moved(2, 250.55)
        check()
        ed._select(3)
        ed.nudge_selected(1.0)
        check()
        at(ed, player, 60.77)
        ed.end_here()
        check()
        ed.remove_selected()
        check()
        ed.undo()
        check()
        assert sum(c["end"] - c["start"] for c in ed.result()[0]) == pytest.approx(900.0)

    def test_one_chapter_is_the_whole_video(self, editor):
        ed, _player, _video, _window = editor
        ed._select(1)
        ed.remove_selected()
        assert ed.total.text() == "1 chapter · 15:00.0 in all"

    def test_the_playhead_and_the_list_say_the_same_time(self, editor):
        """The list rounded to the second what the playhead showed to the
        tenth: a chapter put at 7:29.6 was listed at 7:30."""
        ed, player, _video, _window = editor
        at(ed, player, 449.67)
        assert ed.position_label.text() == "7:29.7 / 15:00.0"
        ed.insert_here()
        assert ed.tree.topLevelItem(2).text(1) == "7:29.7"
        assert ed.message.text() == "Chapter 3 starts at 7:29.7."


class TestEndingAChapterHere:
    def test_the_next_chapter_starts_at_the_playhead_instead(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 280.0)
        ed.handle_key(Qt.Key_E, Qt.NoModifier)
        marks = ed.sheet.marks()
        assert [m.start for m in marks] == [0.0, 280.0]
        assert marks[1].title == "Megitsune", "the same chapter, starting sooner"
        assert ed.selected() == 1 and ed.message.text() == "Chapter 2 starts at 4:40.0."
        assert ed.sheet.length_of(0) + ed.sheet.length_of(1) == 900.0

    def test_it_is_the_chapter_playing_that_ends_not_the_one_selected(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 600.0)
        ed.insert_here()
        at(ed, player, 200.0)
        ed._select(2)
        ed.end_here()
        assert [m.start for m in ed.sheet.marks()] == [0.0, 200.0, 600.0]

    def test_the_last_chapter_ends_where_the_video_does(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 100.0)
        assert ed.end_button.isEnabled()
        at(ed, player, 500.0)
        assert not ed.end_button.isEnabled()
        ed.end_here()
        assert [m.start for m in ed.sheet.marks()] == [0.0, 300.0]
        assert "ends where the video does" in ed.message.text()

    def test_too_close_to_where_the_chapter_starts_is_refused(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 1.0)
        ed.end_here()
        assert [m.start for m in ed.sheet.marks()] == [0.0, 300.0]
        assert "already a mark at 0:00" in ed.message.text()


class TestBoundariesKeepTheirOrder:
    def test_a_chapter_cant_be_started_past_the_next(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 600.0)
        ed.insert_here()
        ed.name.setText("Northbound")
        ed._commit_name()
        ed._select(1)
        at(ed, player, 700.0)
        ed._select(1)
        ed.move_selected_here()
        marks = ed.sheet.marks()
        assert [(m.start, m.title) for m in marks] == [
            (0.0, None), (300.0, "Megitsune"), (600.0, "Northbound"),
        ]
        assert ed.message.text() == "Can't move it there: the next one starts at 10:00."

    def test_a_drag_stops_short_of_the_boundaries_either_side(self, editor):
        from PySide6.QtCore import QPoint
        from PySide6.QtTest import QTest

        ed, player, _video, window = editor
        at(ed, player, 600.0)
        ed.insert_here()
        window.show()
        timeline = ed.timeline
        y = timeline.height() // 2
        QTest.mousePress(timeline, Qt.LeftButton, Qt.NoModifier,
                         QPoint(round(timeline.x_for(300.0)), y))
        QTest.mouseMove(timeline, QPoint(round(timeline.x_for(880.0)), y))
        QTest.mouseRelease(timeline, Qt.LeftButton, Qt.NoModifier,
                           QPoint(round(timeline.x_for(880.0)), y))
        assert [m.start for m in ed.sheet.marks()] == [0.0, 598.0, 600.0]
        assert ed.sheet.marks()[1].title == "Megitsune", "still the second chapter"


class TestUndo:
    def test_each_change_can_be_taken_back_and_made_again(self, editor):
        ed, player, _video, _window = editor
        opened = ed.sheet.marks()
        assert not ed.undo_button.isEnabled() and not ed.redo_button.isEnabled()
        at(ed, player, 600.0)
        ed.insert_here()
        ed.name.setText("Northbound")
        ed._commit_name()
        ed.nudge_selected(1.0)
        ed.remove_selected()
        removed = ed.sheet.marks()
        assert len(removed) == 2

        ed.handle_key(Qt.Key_Z, Qt.ControlModifier)
        assert [(m.start, m.title) for m in ed.sheet.marks()][2] == (601.0, "Northbound")
        assert ed.selected() == 2, "on the chapter that came back"
        assert ed.tree.topLevelItemCount() == 3 and ed.timeline.starts[2] == 601.0
        ed.undo()
        ed.undo()
        assert ed.sheet.marks()[2] == type(opened[0])(600.0, None)
        ed.undo()
        assert ed.sheet.marks() == opened
        assert not ed.undo_button.isEnabled() and ed.redo_button.isEnabled()

        for _ in range(4):
            ed.handle_key(Qt.Key_Z, Qt.ControlModifier | Qt.ShiftModifier)
        assert ed.sheet.marks() == removed and not ed.redo_button.isEnabled()

    def test_a_new_change_ends_what_could_be_redone(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 600.0)
        ed.insert_here()
        ed.undo()
        at(ed, player, 700.0)
        ed.insert_here()
        ed.redo()
        assert [m.start for m in ed.sheet.marks()] == [0.0, 300.0, 700.0]

    def test_everything_undone_is_nothing_to_discard(self, editor, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        ed, player, _video, _window = editor
        asked = []
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: asked.append(1) or QMessageBox.Cancel)
        at(ed, player, 600.0)
        ed.insert_here()
        ed.undo()
        assert ed.request_close() and asked == []

    def test_a_change_refused_is_nothing_to_undo(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 301.0)
        ed.insert_here()
        assert not ed.undo_button.isEnabled()


class TestHearingTheJoin:
    def test_it_plays_from_a_few_seconds_before_the_chapter_starts(self, editor):
        ed, player, _video, _window = editor
        ed._select(1)
        ed.handle_key(Qt.Key_J, Qt.NoModifier)
        assert player.position_value == 295.0 and player.paused is False

    def test_the_first_chapter_plays_from_the_start(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 500.0)
        ed._select(0)
        ed.hear_join()
        assert player.position_value == 0.0 and player.paused is False


class TestTheSelectionKeepsUpWithPlayback:
    def test_it_goes_to_the_chapter_playback_runs_into(self, editor):
        ed, player, _video, _window = editor
        assert ed.selected() == 0
        at(ed, player, 299.0)
        assert ed.selected() == 0
        at(ed, player, 300.5)
        assert ed.selected() == 1 and ed.name.text() == "Megitsune"
        assert ed.timeline.selected == 1

    def test_a_chapter_picked_stays_picked_until_playback_moves_on(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 100.0)
        ed._select(1)
        at(ed, player, 101.0)
        assert ed.selected() == 1, "still in the same chapter: nothing to keep up with"

    def test_not_from_under_a_name_being_typed(self, editor, monkeypatch):
        ed, player, _video, _window = editor
        monkeypatch.setattr(ed.name, "hasFocus", lambda: True)
        at(ed, player, 300.5)
        assert ed.selected() == 0

    def test_a_chapter_put_in_ahead_isnt_playback_moving(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 600.0)
        ed.insert_here()
        ed._select(1)
        ed.remove_selected()
        # What plays is now the second chapter, not the third - by the list
        # changing, not by playback going anywhere.
        assert ed.selected() == 0
        at(ed, player, 601.0)
        assert ed.selected() == 0


class TestTheTimeline:
    def test_a_click_on_it_leaves_a_name_being_typed(self, editor):
        from PySide6.QtCore import QPoint
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication

        ed, _player, _video, window = editor
        window.show()
        window.activateWindow()
        QApplication.processEvents()
        ed.name.setFocus()
        ed.name.setText("Northbound")
        QTest.mouseClick(ed.timeline, Qt.LeftButton, Qt.NoModifier,
                         QPoint(round(ed.timeline.x_for(100.0)), 30))
        assert not ed._typing() and ed.sheet.marks()[0].title == "Northbound"

    def test_keys_zoom_it_about_the_playhead(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 450.0)
        assert not ed.zoom_out_button.isEnabled() and ed.zoom_in_button.isEnabled()
        ed.handle_key(Qt.Key_Plus, Qt.NoModifier)
        ed.handle_key(Qt.Key_Equal, Qt.NoModifier)
        start, span = ed.timeline.view()
        assert span == pytest.approx(400.0) and start == pytest.approx(250.0)
        assert ed.zoom_out_button.isEnabled() and ed.zoom_all_button.isEnabled()
        ed.handle_key(Qt.Key_Minus, Qt.NoModifier)
        assert ed.timeline.view()[1] == pytest.approx(600.0)
        ed.handle_key(Qt.Key_0, Qt.NoModifier)
        assert ed.timeline.view() == (0.0, 900.0) and not ed.zoom_out_button.isEnabled()

    def test_zoomed_in_it_goes_where_a_chapter_clicked_is(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 800.0)
        for _ in range(4):
            ed.timeline.zoom_in()
        assert not ed.timeline.in_view(300.0)
        ed.tree.itemClicked.emit(ed.tree.topLevelItem(1), 2)
        assert ed.timeline.in_view(300.0)

    def test_it_opens_on_the_whole_video_each_time(self, editor):
        ed, _player, _video, window = editor
        ed.timeline.zoom_in()
        ed.request_close()
        window.edit_chapters()
        assert not ed.timeline.zoomed()


class TestTheAudioOnTheTimeline:
    """Measuring reads the whole file, and most libraries are on a network
    share: it happens when asked for, never by opening the editor."""

    @pytest.fixture
    def measuring(self, monkeypatch):
        """The measuring itself held here: what was started, to deliver
        what ffmpeg would have."""
        from mediabrowser.core.audio_levels import Levels
        from mediabrowser.gui import audio_analysis

        held = {"cached": None, "started": []}

        def start(parent, video, cancel, *, on_progress, on_done, on_failed):
            held["started"].append((cancel, on_progress, on_done, on_failed))
            return object()

        monkeypatch.setattr(audio_analysis, "cached", lambda video: held["cached"])
        monkeypatch.setattr(audio_analysis, "start", start)
        held["levels"] = Levels(step=0.5, full=[-20.0] * 1800, bass=[-30.0] * 1800)
        return held

    def test_opening_the_editor_measures_nothing(self, measuring, editor):
        ed, _player, _video, _window = editor
        assert measuring["started"] == []
        assert not ed.levels_button.isHidden()
        assert ed.levels_button.text() == "Show Audio Levels"
        assert "network share" in ed.levels_button.toolTip()
        assert not ed.timeline.has_levels() and ed.levels_note.text() == ""

    def test_asked_for_it_is_measured_and_drawn_when_done(self, measuring, editor):
        ed, _player, _video, _window = editor
        ed.levels_button.click()
        (cancel, on_progress, on_done, _on_failed), = measuring["started"]
        assert ed.levels_note.text() == "Measuring the audio…"
        assert ed.levels_button.text() == "Stop Measuring"
        on_progress(40)
        assert ed.levels_note.text().endswith("40%")
        on_done(measuring["levels"])
        assert ed.timeline.has_levels() and ed.levels_note.text() == ""
        assert ed.levels_button.isHidden(), "nothing left to ask for"
        assert not cancel.is_set()

    def test_it_can_be_stopped(self, measuring, editor):
        ed, _player, _video, _window = editor
        ed.levels_button.click()
        ed.levels_button.click()
        (cancel, _on_progress, on_done, on_failed), = measuring["started"]
        assert cancel.is_set()
        assert ed.levels_button.text() == "Show Audio Levels" and ed.levels_note.text() == ""
        on_failed("audio analysis cancelled")
        on_done(measuring["levels"])
        assert not ed.timeline.has_levels() and ed.levels_note.text() == ""
        ed.levels_button.click()
        assert len(measuring["started"]) == 2, "and asked for again"

    def test_measured_already_it_is_there_at_once(self, measuring, editor):
        ed, _player, _video, window = editor
        ed.request_close()
        measuring["cached"] = measuring["levels"]
        window.edit_chapters()
        assert ed.timeline.has_levels() and ed.levels_button.isHidden()
        assert measuring["started"] == []

    def test_closing_stops_the_measuring_and_what_comes_late_is_dropped(
            self, measuring, editor):
        ed, _player, _video, window = editor
        ed.levels_button.click()
        (cancel, _on_progress, on_done, _on_failed), = measuring["started"]
        ed.request_close()
        assert cancel.is_set()
        window.edit_chapters()
        on_done(measuring["levels"])
        assert not ed.timeline.has_levels()
        assert ed.levels_button.text() == "Show Audio Levels"
        assert len(measuring["started"]) == 1, "not started again by reopening"

    def test_a_disc_has_nothing_to_measure(self, measuring, editor):
        ed, _player, video, window = editor
        ed.request_close()
        video["type"] = "bluray"
        window.edit_chapters()
        assert ed.levels_button.isHidden()
        ed.measure_audio()
        assert measuring["started"] == []

    def test_audio_that_cant_be_measured_says_so(self, measuring, editor):
        ed, _player, _video, _window = editor
        ed.levels_button.click()
        measuring["started"][0][3]("could not run ffmpeg")
        assert ed.levels_note.text() == "The audio couldn't be measured."
        assert ed.levels_button.text() == "Show Audio Levels", "to try again"


class TestMarking:
    def test_insert_then_name_it(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 512.3)
        ed.handle_key(Qt.Key_M, Qt.NoModifier)
        assert [m.start for m in ed.sheet.marks()] == [0.0, 300.0, 512.3]
        assert ed.selected() == 2
        ed.name.setText("Karate")
        ed._name_entered()
        assert ed.sheet.marks()[2].title == "Karate"
        assert ed.timeline.titles[2] == "Karate"

    def test_enter_in_the_name_gives_the_keys_back(self, editor):
        """It left the keyboard in the name box - the same Enter, passed on
        up by the box, was taken for the one that names a chapter - so
        Space and the rest went on being typed into the name."""
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication

        ed, player, _video, window = editor
        window.show()
        window.activateWindow()
        QApplication.processEvents()
        at(ed, player, 512.3)
        QTest.keyClick(window, Qt.Key_M)
        assert ed.name.hasFocus()
        QTest.keyClicks(ed.name, "Song 7")
        QTest.keyClick(ed.name, Qt.Key_Return)
        QApplication.processEvents()
        assert ed.sheet.marks()[2].title == "Song 7"
        assert not ed.name.hasFocus() and not ed._typing()
        QTest.keyClick(QApplication.focusWidget(), Qt.Key_Space)
        assert player.paused is False and ed.name.text() == "Song 7"

    def test_names_in_order_name_each_new_chapter(self, editor):
        ed, player, _video, _window = editor
        ed.names.setPlainText("Opening\nMegitsune\nKarate")
        ed._on_names_changed()
        at(ed, player, 600.0)
        ed.insert_here()
        assert ed.sheet.marks()[2].title == "Opening", "the first name not on a chapter"
        assert ed.next_name.text() == "Next: Karate"

    def test_a_chapter_on_top_of_another_is_refused(self, editor):
        ed, player, _video, _window = editor
        at(ed, player, 301.0)
        ed.insert_here()
        assert len(ed.sheet) == 2 and "already a mark at 5:00" in ed.message.text()

    def test_moving_nudging_removing(self, editor):
        ed, player, _video, _window = editor
        ed._select(1)
        ed.nudge_selected(1.0)
        assert ed.sheet.marks()[1].start == 301.0
        at(ed, player, 290.0)
        ed.move_selected_here()
        assert ed.sheet.marks()[1].start == 290.0
        ed._on_boundary_moved(1, 280.0)
        assert ed.sheet.marks()[1].start == 280.0
        ed.handle_key(Qt.Key_Delete, Qt.NoModifier)
        assert len(ed.sheet) == 1


class TestSaving:
    def test_only_names_changed_keeps_the_origin_and_the_rest_of_the_sources(self, editor):
        ed, _player, video, window = editor
        ed._select(0)
        ed.name.setText("Opening")
        ed._commit_name()
        ed.save()
        assert window.pages.currentIndex() == 1, "back to the chapters"
        assert not window.now_playing_bar.isHidden()
        assert "chapter_origin" not in video, "still the file's own chapters"
        assert [(c["title"], c["source"]) for c in video["chapters"]] == [
            ("Opening", "manual"), ("Megitsune", "menu"),
        ]

    def test_new_chapters_are_marked_by_hand(self, editor):
        ed, player, video, _window = editor
        at(ed, player, 600.0)
        ed.insert_here()
        ed.save()
        assert video["chapter_origin"] == library.ORIGIN_MARKED
        assert [c["start"] for c in video["chapters"]] == [0.0, 300.0, 600.0]
        assert video["chapters"][1]["source"] == "menu", "untouched: its source kept"
        assert ("stop",) in player.calls

    def test_cancelling_with_changes_asks(self, editor, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        ed, player, video, window = editor
        at(ed, player, 600.0)
        ed.insert_here()
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
        window.show_grid()  # Esc
        assert ed.active(), "kept open"
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Discard)
        window.show_grid()
        assert not ed.active() and len(video["chapters"]) == 2


class TestNativeWindows:
    def test_only_the_video_surface_is_a_native_window(self, app, window):
        """A native widget made its neighbours native too, and menus opened
        from those were parented to a child window ("... must be a top level
        window")."""
        window.show()
        window.editor.surface.winId()
        app.processEvents()
        assert window.editor.surface.internalWinId()
        for widget in (window.pages, window.detail, window.shelf, window.editor):
            assert not widget.internalWinId(), widget.objectName() or type(widget).__name__
