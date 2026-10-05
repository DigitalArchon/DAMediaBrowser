# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Playing from the window: one mpv for the queue, video in the app with
fullscreen, removing several queued chapters at once, and playlists. mpv
is the stand-in from fake_mpv (conftest puts it in every window)."""

import pytest

from mediabrowser.core import playlists, store

PySide6 = pytest.importorskip("PySide6")


def _video(tmp_path, name, n=3):
    path = tmp_path / "media" / f"{name}.mkv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return {"type": "file", "path": str(path), "display_name": name, "duration": 100.0 * n,
            "chapters": [{"title": f"{name} {i + 1}", "start": i * 100.0,
                          "end": (i + 1) * 100.0, "source": "manual"} for i in range(n)]}


@pytest.fixture
def playing(window, tmp_path):
    window.data = store.default_library(str(tmp_path / "media"))
    window.data["videos"] = {"a": _video(tmp_path, "Budokan"), "b": _video(tmp_path, "Wembley")}
    window.refresh_library()
    return window, window.player


@pytest.fixture
def embeddable(monkeypatch):
    from mediabrowser.gui import main_window

    monkeypatch.setattr(main_window, "can_embed", lambda: True)


def loads(mpv):
    return [c[1:] for c in mpv.calls if c[0] == "load"]


class TestOneMpv:
    def test_a_video_plays_through_one_mpv_a_piece_ahead(self, playing):
        window, mpv = playing
        window.play_from("a", 1, audio_only=True)
        assert [c for c in mpv.calls if c[0] == "start"] == [("start", None)]
        assert loads(mpv) == [("a", (1, 2), False)], "chapters 2 and 3: one piece"
        assert window.now_playing_bar.title.text() == "Budokan 2"

    def test_the_window_follows_it_from_chapter_to_chapter(self, playing):
        window, mpv = playing
        window.play_from("a", 0, audio_only=True)
        mpv.state["position"] = 150.0
        window._tick()
        assert window.now_playing_bar.title.text() == "Budokan 2"
        assert window.queue.current_index() == 1
        assert window.now_playing_bar.slider.value() == 50, "audio: the bar spans the chapter"

    def test_across_videos_the_next_is_handed_over_early(self, playing):
        window, mpv = playing
        window.play_from("a", 2, audio_only=True)
        window.enqueue_chapters("b", [0])
        assert loads(mpv)[-1] == ("b", (3,), True)
        mpv.state["playing_id"] = mpv.next_id  # mpv went on to it
        window._tick()
        assert window.now_playing_bar.title.text() == "Wembley 1"

    def test_closing_mpvs_window_stops_and_playing_out_finishes(self, playing):
        window, mpv = playing
        window.play_from("a", 0, audio_only=True)
        mpv.active = False
        window._tick()
        assert window.now_playing is None
        window.play_from("a", 0, audio_only=True)
        mpv.state["idle"] = True
        window._tick()
        assert window.now_playing is None and window.status_label.text() == "Finished."


class TestVideoInTheApp:
    def test_it_plays_in_the_main_area(self, playing, embeddable):
        from mediabrowser.gui.main_window import PAGE_VIDEO

        window, mpv = playing
        window.play_from("b", 0, audio_only=False)
        window_id = [c for c in mpv.calls if c[0] == "start"][0][1]
        assert window_id == window.video_page.window_id()
        assert window.pages.currentIndex() == PAGE_VIDEO
        assert not window.now_playing_bar.fullscreen_button.isHidden()

    def test_leaving_it_plays_on_and_show_video_comes_back(self, playing, embeddable):
        from mediabrowser.gui.main_window import PAGE_SHELF, PAGE_VIDEO

        window, _mpv = playing
        window.play_from("b", 0, audio_only=False)
        window.video_page.back_button.click()
        assert window.pages.currentIndex() == PAGE_SHELF and window.now_playing is not None
        assert not window.now_playing_bar.show_video_button.isHidden()
        window.now_playing_bar.show_video_button.click()
        assert window.pages.currentIndex() == PAGE_VIDEO

    def test_fullscreen_is_the_picture_alone_and_esc_leaves_it(self, playing, embeddable):
        window, _mpv = playing
        window.show()
        window.play_from("b", 0, audio_only=False)
        window.toggle_fullscreen()
        assert window._fullscreen and window.menuBar().isHidden()
        assert window.video_page.header.isHidden()
        window.show_grid()  # Esc
        assert not window._fullscreen and not window.menuBar().isHidden()
        assert not window.video_page.header.isHidden()

    def test_its_own_window_when_asked_or_set(self, playing, embeddable):
        window, mpv = playing
        window.set_video_in_app(False)
        window.play_from("b", 0, audio_only=False)
        assert [c for c in mpv.calls if c[0] == "start"] == [("start", None)]
        assert window.now_playing_bar.fullscreen_button.isHidden()

    def test_audio_stays_where_it_was(self, playing, embeddable):
        from mediabrowser.gui.main_window import PAGE_VIDEO

        window, _mpv = playing
        window.play_from("a", 0, audio_only=True)
        assert window.pages.currentIndex() != PAGE_VIDEO

    def test_stopping_leaves_the_video(self, playing, embeddable):
        from mediabrowser.gui.main_window import PAGE_VIDEO

        window, _mpv = playing
        window.play_from("b", 0, audio_only=False)
        window.stop_playback()
        assert window.pages.currentIndex() != PAGE_VIDEO


class TestGettingBackToTheVideo:
    def test_show_video_in_the_playback_menu(self, playing, embeddable):
        from mediabrowser.gui.main_window import PAGE_VIDEO

        window, _mpv = playing
        assert not window.show_video_action.isEnabled()
        window.play_from("b", 0, audio_only=False)
        window.video_page.back_button.click()
        assert window.show_video_action.isEnabled()
        window.show_video_action.trigger()
        assert window.pages.currentIndex() == PAGE_VIDEO

    def test_clicking_whats_playing_goes_back_to_it(self, playing, embeddable):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        from mediabrowser.gui.main_window import PAGE_VIDEO

        window, _mpv = playing
        window.play_from("b", 0, audio_only=False)
        window.video_page.back_button.click()
        QTest.mouseClick(window.now_playing_bar.title, Qt.LeftButton, pos=QPoint(2, 2))
        assert window.pages.currentIndex() == PAGE_VIDEO

    def test_double_clicking_whats_playing_in_the_queue_restarts_it(self, playing,
                                                                    embeddable):
        from mediabrowser.gui.main_window import PAGE_VIDEO

        window, mpv = playing
        window.play_from("b", 1, audio_only=False)
        window.video_page.back_button.click()
        loaded = len(loads(mpv))
        window.play_queue_entry(window.queue.current_index())
        assert len(loads(mpv)) > loaded, "started again"
        assert window.pages.currentIndex() == PAGE_VIDEO


class TestPausedThenPlayed:
    def test_playing_something_else_unpauses(self, playing):
        window, mpv = playing
        window.play_from("a", 0, audio_only=True)
        window.toggle_play_pause()
        window._tick()
        assert window.now_playing_bar.play_button.text() == "▶"
        window.play_from("b", 0, audio_only=True)
        assert mpv.state["paused"] is False
        assert window.now_playing_bar.play_button.text() == "⏸"


class TestTheQueue:
    def test_several_can_be_removed_at_once(self, playing):
        window, mpv = playing
        window.play_from("a", 0, audio_only=True)
        window.enqueue_video("b")
        panel = window.queue_panel
        for row in (3, 5):
            panel.list.item(row).setSelected(True)
        assert panel.remove_button.text() == "Remove 2"
        panel.remove_button.click()
        assert [(e.video_id, e.chapter_index) for e in window.queue.entries()] == [
            ("a", 0), ("a", 1), ("a", 2), ("b", 1),
        ]
        assert window.now_playing is not None, "what plays wasn't among them"

    def test_dragging_reorders_what_plays_next(self, playing):
        window, mpv = playing
        window.play_from("a", 0, audio_only=True)
        window.enqueue_chapters("b", [0])
        panel = window.queue_panel
        item = panel.list.takeItem(3)
        panel.list.insertItem(1, item)
        seen = []
        panel.order_changed.connect(seen.append)
        panel._read_order()
        assert seen == [[0, 3, 1, 2]]
        assert window.queue.entries()[1].video_id == "b"
        assert loads(mpv)[-1] == ("b", (1,), True), "Wembley is next now"


class TestPlaylists:
    def test_a_queue_saved_and_played_again_as_video(self, playing, monkeypatch, embeddable):
        from PySide6.QtWidgets import QInputDialog

        window, mpv = playing
        window.play_from("a", 2, audio_only=True)  # the whole video, from its third
        window.enqueue_chapters("b", [1])
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Encores", True))
        window.save_queue_as_playlist()
        assert playlists.get(store.load_library_for_root(window.data["settings"]["library_root"]),
                             "Encores") is not None
        window.clear_queue()
        mpv.calls.clear()
        window.play_playlist("Encores", audio_only=False, in_app=False)
        assert [(e.video_id, e.chapter_index, e.audio_only) for e in window.queue.entries()] == [
            ("a", 0, False), ("a", 1, False), ("a", 2, False), ("b", 1, False),
        ]
        assert mpv.calls[0] == ("start", None), "in its own window, as asked"

    def test_the_menu_offers_each_way_to_play_it(self, playing, embeddable):
        from PySide6.QtWidgets import QMenu

        window, _mpv = playing
        playlists.save(window.data, "Mix", window.queue.entries(), audio_only=False)
        menu = QMenu()
        window._fill_playlists_menu(menu)
        sub = [a for a in menu.actions() if a.menu()][0].menu()
        assert [a.text() for a in sub.actions() if a.text()] == [
            "Play (Video)", "Play as Audio", "Play as Video Here",
            "Play as Video in Its Own Window", "Add to Queue", "Rename…", "Delete…",
        ]

    def test_it_plays_as_the_queue_did_when_saved(self, playing, monkeypatch, embeddable):
        from PySide6.QtWidgets import QInputDialog

        window, _mpv = playing
        window.play_from("a", 1, audio_only=False)
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Videos", True))
        window.save_queue_as_playlist()
        window.clear_queue()
        window.play_playlist("Videos")
        assert not window.queue.audio_only
        window.play_from("b", 0, audio_only=True)
        window.enqueue_playlist("Videos")
        assert {e.audio_only for e in window.queue.entries()} == {True}, "as the queue plays"


class TestPlayingVideoElsewhere:
    def test_every_play_video_offers_the_other_place(self, playing, embeddable):
        from PySide6.QtWidgets import QMenu

        window, mpv = playing
        window.set_view(1)
        menu = window.list.context_menu_for(window.list.topLevelItem(0))
        other = next(a for a in menu.actions() if a.text() == "Play Video in mpv's Own Window")
        other.trigger()
        assert [c for c in mpv.calls if c[0] == "start"] == [("start", None)]
        window.set_video_in_app(False)
        grid_menu = window.grid.context_menu_for(window.grid.item(0))
        assert "Play Video in the App" in [a.text() for a in grid_menu.actions()]
        assert isinstance(menu, QMenu)

    def test_the_detail_pages_play_video_has_it_on_its_arrow(self, playing, embeddable):
        window, mpv = playing
        window.open_video("a")
        detail = window.detail
        detail.tree.setCurrentItem(detail.tree.topLevelItem(1))
        detail._fill_video_menu()
        [other] = detail._video_menu.actions()
        other.trigger()
        assert [c for c in mpv.calls if c[0] == "start"] == [("start", None)]
        assert window.queue.current().chapter_index == 1

    def test_without_embedding_there_is_no_other_place(self, playing):
        window, _mpv = playing
        assert window._other_video_places() == []


class TestSeekBar:
    def test_a_click_on_the_bar_jumps_there(self, app, playing):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        window, mpv = playing
        window.show()
        window.play_from("a", 0, audio_only=True)
        slider = window.now_playing_bar.slider
        QTest.mouseClick(slider, Qt.LeftButton, Qt.NoModifier,
                         QPoint(int(slider.width() * 0.75), slider.height() // 2))
        seeks = [c for c in mpv.calls if c[0] == "seek"]
        assert seeks and 65 <= seeks[-1][1] <= 85, seeks

    def test_a_seek_back_from_where_video_began_names_that_chapter(self, playing):
        window, mpv = playing
        window.play_from("a", 2, audio_only=False)
        assert window.now_playing_bar.title.text() == "Budokan 3"
        mpv.state["position"] = 120.0
        window._tick()
        assert window.now_playing_bar.title.text() == "Budokan 2"
        assert window.queue.current_index() == 1

    def test_a_seek_to_a_chapter_the_queue_hasnt_got_still_names_it(self, playing):
        window, mpv = playing
        window.enqueue_chapters("a", [2], audio_only=False)
        window.play_queue_entry(0)
        mpv.state["position"] = 40.0
        window._tick()
        assert window.now_playing_bar.title.text() == "Budokan 1"
        assert window.video_page.title.text().startswith("Budokan 1")

    def test_playing_video_it_marks_where_each_chapter_starts(self, app, playing):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QToolTip

        window, _mpv = playing
        window.show()
        window.play_from("a", 0, audio_only=False)
        window._tick()  # mpv says how long the file is: 300s
        slider = window.now_playing_bar.slider
        marks = slider.marks()
        assert [index for index, _x in marks] == [1, 2], "the first starts at the start"
        assert marks[0][1] < marks[1][1]
        x = int(marks[1][1])
        assert slider.mark_at(x) == 2 and slider.mark_at(x - 40) is None
        # Named on hovering alone - no wait for Qt's tooltip event, which an
        # inactive window (mpv's has the focus) never gets.
        def hover(at):
            point = QPointF(at, slider.height() / 2)
            QApplication.sendEvent(slider, QMouseEvent(
                QEvent.MouseMove, point, QPointF(slider.mapToGlobal(point.toPoint())),
                Qt.NoButton, Qt.NoButton, Qt.NoModifier))

        hover(x)
        assert QToolTip.isVisible() and QToolTip.text() == "Chapter 3 · Budokan 3"
        hover(x - 40)
        QTest.qWait(500)  # Qt takes its tooltips down after a moment
        assert not QToolTip.isVisible()

    def test_audio_has_no_marks(self, playing):
        window, _mpv = playing
        window.play_from("a", 0, audio_only=True)
        window._tick()
        assert window.now_playing_bar.slider.marks() == []
        window.play_from("a", 0, audio_only=False)
        window._tick()
        window.stop_playback()
        assert window.now_playing_bar.slider.marks() == []


class TestSpace:
    def press_space(self, app, window, on):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        window.show()
        window.activateWindow()
        QTest.qWaitForWindowActive(window)
        on.setFocus()
        QTest.keyClick(on, Qt.Key_Space)

    def test_space_plays_and_pauses_from_anywhere_in_the_window(self, app, playing):
        window, mpv = playing
        window.play_from("a", 0, audio_only=True)
        self.press_space(app, window, window.now_playing_bar.next_button)
        assert mpv.calls.count(("toggle",)) == 1
        assert window.queue.current_index() == 0, "the focused button wasn't pressed"

    def test_but_not_while_typing_or_with_nothing_playing(self, app, playing):
        window, mpv = playing
        window.play_from("a", 0, audio_only=True)
        self.press_space(app, window, window.search)
        assert ("toggle",) not in mpv.calls and window.search.text() == " "
        window.search.clear()
        window.stop_playback()
        self.press_space(app, window, window.now_playing_bar.next_button)
        assert ("toggle",) not in mpv.calls


class TestAskingMpvOffTheGuiThread:
    def test_an_answer_from_before_playback_changed_is_ignored(self, playing):
        window, mpv = playing
        window.play_from("a", 0, audio_only=True)
        stale = window._ask_mpv()
        mpv.state["idle"] = True  # what an mpv between files might have said
        stale = (stale[0], dict(stale[1], idle=True))
        window.play_from("b", 0, audio_only=True)
        window._mpv_answered(stale)
        assert window.now_playing == ("b", 0, True), "not stopped by a stale 'idle'"

    def test_a_fresh_answer_is_followed(self, playing):
        window, mpv = playing
        window.play_from("a", 0, audio_only=True)
        mpv.state["position"] = 150.0
        window._mpv_answered(window._ask_mpv())
        assert window.now_playing_bar.title.text() == "Budokan 2"

    def test_the_window_keeps_going_while_mpv_is_stuck(self, app, playing, monkeypatch):
        import time

        from tests.test_mpv_poll import timer_ticks

        window, mpv = playing
        window.play_from("a", 0, audio_only=True)
        stuck, asked = dict(mpv.state), []

        def session_state():  # an mpv whose socket times out
            asked.append(1)
            time.sleep(1.0)
            return stuck

        monkeypatch.setattr(mpv, "session_state", session_state)
        ticks = timer_ticks(1500)
        window.stop_playback()
        assert asked, "mpv was asked"
        assert ticks >= 55, f"the event loop stalled: {ticks} of ~75"


class TestQueueMenu:
    def test_a_row_plays_or_goes(self, playing):
        window, _mpv = playing
        window.play_from("b", 0, audio_only=False)
        menu = window.queue_panel.menu_for(window.queue_panel.list.item(1))
        assert [a.text() for a in menu.actions() if a.text()] == ["Play", "Remove"]


class TestPoppingOut:
    def test_video_in_the_app_carries_on_in_mpvs_window_and_comes_back(self, playing,
                                                                        embeddable):
        from mediabrowser.gui.main_window import PAGE_SHELF, PAGE_VIDEO

        window, mpv = playing
        window.play_from("b", 1, audio_only=False)
        bar = window.now_playing_bar
        assert not bar.move_button.isHidden() and bar.move_button.text() == "Pop Out"
        mpv.state["position"] = 130.0
        mpv.calls.clear()
        bar.move_button.click()
        assert mpv.calls[0] == ("start", None)
        assert mpv.calls[1][0] == "resume" and mpv.calls[1][3]["time-pos"] == 130.0
        assert window.pages.currentIndex() == PAGE_SHELF
        assert bar.move_button.text() == "Into App" and bar.fullscreen_button.isHidden()
        assert window.move_video_action.text() == "Bring Video into the App"
        window.move_video_action.trigger()
        assert [c for c in mpv.calls if c[0] == "start"][-1] == (
            "start", window.video_page.window_id())
        assert window.pages.currentIndex() == PAGE_VIDEO
        assert bar.move_button.text() == "Pop Out"

    def test_popping_out_leaves_fullscreen(self, playing, embeddable):
        window, _mpv = playing
        window.show()
        window.play_from("b", 0, audio_only=False)
        window.toggle_fullscreen()
        window.move_video()
        assert not window._fullscreen and not window.menuBar().isHidden()

    def test_the_queue_after_it_stays_out_until_video_is_played_again(self, playing,
                                                                     embeddable):
        window, mpv = playing
        window.play_from("b", 0, audio_only=False)
        window.move_video()
        window._skip(1)
        assert window.session.window_id() is None
        window.play_from("a", 0, audio_only=False)
        assert window.session.window_id() == window.video_page.window_id()

    def test_audio_has_nothing_to_move(self, playing, embeddable):
        window, mpv = playing
        window.play_from("a", 0, audio_only=True)
        assert window.now_playing_bar.move_button.isHidden()
        assert not window.move_video_action.isEnabled()

    def test_without_embedding_video_cant_come_in(self, playing):
        window, _mpv = playing
        window.play_from("b", 0, audio_only=False)
        assert window.now_playing_bar.move_button.isHidden()

    def test_the_video_page_has_no_fullscreen_of_its_own(self, playing, embeddable):
        window, _mpv = playing
        assert not hasattr(window.video_page, "fullscreen_button"), "the bar's is the one"


class TestMpvKeysInTheApp:
    def _press(self, window, key, text=""):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        window.video_page.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier, text))

    def test_s_saves_a_screenshot_named_for_the_moment(self, playing, embeddable, tmp_path):
        from PySide6.QtCore import Qt

        window, mpv = playing
        window.player.screenshot_dir = tmp_path / "shots"
        window.play_from("b", 0, audio_only=False)
        window._last_position = 75.0
        self._press(window, Qt.Key_S, "s")
        [shot] = [c[1] for c in mpv.calls if c[0] == "screenshot"]
        assert shot == tmp_path / "shots" / "Wembley 1-15.png"
        assert str(shot) in window.status_label.text()

    def test_a_screenshot_never_overwrites_one(self, tmp_path):
        from mediabrowser.gui.main_window import _unused_path

        (tmp_path / "AC-DC 0-05.png").write_bytes(b"")
        assert _unused_path(tmp_path, "AC/DC", 5.0) == tmp_path / "AC-DC 0-05 (2).png"

    def test_v_j_and_hash_step_subtitles_and_audio(self, playing, embeddable):
        from PySide6.QtCore import Qt

        window, mpv = playing
        window.play_from("b", 0, audio_only=False)
        self._press(window, Qt.Key_V, "v")
        assert window.status_label.text() == "Subtitles hidden."
        self._press(window, Qt.Key_J, "j")
        assert window.status_label.text() == "Subtitles: eng · Commentary."
        self._press(window, Qt.Key_NumberSign, "#")
        assert [c[1] for c in mpv.calls if c[0] == "cycle"] == ["sub-visibility", "sub", "audio"]
        assert ("text", "Audio: eng · Commentary") in mpv.calls, "shown over the picture too"

    def test_they_do_nothing_with_no_video_in_the_app(self, playing, embeddable):
        window, mpv = playing
        window.play_from("a", 0, audio_only=True)
        window.take_screenshot()
        window.cycle_track("sub")
        assert not [c for c in mpv.calls if c[0] in ("screenshot", "cycle")]


class TestTheQueueAsAudioOrVideo:
    def test_the_button_says_how_it_plays(self, playing):
        window, _mpv = playing
        button = window.queue_panel.mode_button
        assert not button.isEnabled(), "nothing queued"
        window.play_from("a", 0, audio_only=True)
        assert button.isEnabled() and button.text() == "Audio" and not button.isChecked()
        window.play_from("b", 0, audio_only=False)
        assert button.text() == "Video" and button.isChecked()

    def test_switching_to_video_carries_on_in_the_app(self, playing, embeddable):
        from mediabrowser.gui.main_window import PAGE_VIDEO

        window, mpv = playing
        window.play_from("a", 0, audio_only=True)
        window.enqueue_video("b")
        mpv.state["position"] = 130.0
        window._tick()
        mpv.calls.clear()
        window.queue_panel.mode_button.click()
        assert {e.audio_only for e in window.queue.entries()} == {False}
        assert mpv.calls[0] == ("start", window.video_page.window_id())
        assert mpv.calls[1][0] == "resume" and mpv.calls[1][3]["time-pos"] == 130.0
        assert window.now_playing == ("a", 1, False)
        assert window.pages.currentIndex() == PAGE_VIDEO
        assert window.queue_panel.mode_button.text() == "Video"

    def test_switching_to_audio_leaves_the_video(self, playing, embeddable):
        from mediabrowser.gui.main_window import PAGE_VIDEO

        window, mpv = playing
        window.show()
        window.play_from("b", 0, audio_only=False)
        window.toggle_fullscreen()
        window.queue_panel.mode_button.click()
        assert window.now_playing == ("b", 0, True)
        assert [c for c in mpv.calls if c[0] == "start"][-1] == ("start", None)
        assert not window._fullscreen and window.pages.currentIndex() != PAGE_VIDEO

    def test_whatever_is_added_plays_as_the_queue_does(self, playing):
        window, _mpv = playing
        window.play_from("a", 0, audio_only=False)
        window.enqueue_chapters("b", [1], audio_only=True)
        assert {e.audio_only for e in window.queue.entries()} == {False}

    def test_with_nothing_playing_it_just_switches(self, playing):
        window, mpv = playing
        window.enqueue_video("a")
        window.stop_playback()
        mpv.calls.clear()
        window.set_queue_audio_only(False)
        assert not window.queue.audio_only and mpv.calls == []


class TestPlayingVideoByDefault:
    def test_audio_until_set_otherwise(self, playing):
        window, _mpv = playing
        assert window.default_audio_only() and window.default_actions[True].isChecked()
        window.set_default_audio_only(False)
        assert not window.default_audio_only() and window.default_actions[False].isChecked()

    def test_a_double_click_on_a_chapter_plays_video(self, playing):
        window, _mpv = playing
        window.set_default_audio_only(False)
        window.open_video("a")
        window.detail._on_activated(window.detail.tree.topLevelItem(1))
        assert window.now_playing == ("a", 1, False)
        window.set_view(1)
        window.list.chapter_activated.emit("b", 2)
        assert window.now_playing == ("b", 2, False)

    def test_add_to_queue_queues_video(self, playing):
        window, _mpv = playing
        window.set_default_audio_only(False)
        window.enqueue_video("a")
        assert {e.audio_only for e in window.queue.entries()} == {False}

    def test_play_video_becomes_the_main_button(self, playing):
        window, _mpv = playing
        detail = window.detail
        layout = detail._chapter_actions
        assert layout.indexOf(detail.play_audio_button) == 0
        window.set_default_audio_only(False)
        assert layout.indexOf(detail.play_video_button) == 0
        assert detail.play_video_button.property("primary") is True
        assert detail.play_audio_button.objectName() != "primaryButton"

    def test_it_is_remembered(self, playing):
        from mediabrowser.gui.main_window import MainWindow

        window, _mpv = playing
        window.set_default_audio_only(False)
        again = MainWindow()
        assert not again.default_audio_only()
        assert again.detail.play_video_button.property("primary") is True
        again.close()


class TestToolbarAndPanels:
    def test_add_folder_is_off_the_toolbar_but_in_the_file_menu(self, window):
        from PySide6.QtWidgets import QToolBar

        bar = window.findChild(QToolBar)
        assert window.choose_action not in bar.actions()
        file_menu = window.menuBar().actions()[0].menu()
        assert window.choose_action in file_menu.actions()

    def test_a_closed_panel_comes_back_from_the_view_menu(self, window):
        window.show()
        view_menu = next(a.menu() for a in window.menuBar().actions() if a.text() == "&View")
        toggles = {a.text(): a for a in view_menu.actions()}
        window.queue_dock.close()
        assert window.queue_dock.isHidden()
        toggles["Queue"].trigger()
        assert not window.queue_dock.isHidden()
        assert "Libraries" in toggles
