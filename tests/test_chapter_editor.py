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
