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
        playlists.save(window.data, "Mix", window.queue.entries())
        menu = QMenu()
        window._fill_playlists_menu(menu)
        sub = [a for a in menu.actions() if a.menu()][0].menu()
        assert [a.text() for a in sub.actions() if a.text()] == [
            "Play Audio", "Play Video Here", "Play Video in Its Own Window", "Add to Queue",
            "Rename…", "Delete…",
        ]


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


class TestQueueMenu:
    def test_a_queued_video_can_be_played_as_audio(self, playing, embeddable):
        from mediabrowser.gui.main_window import PAGE_VIDEO

        window, mpv = playing
        window.play_from("b", 0, audio_only=False)
        window.video_page.back_button.click()
        item = window.queue_panel.list.item(1)
        menu = window.queue_panel.menu_for(item)
        actions = {a.text(): a for a in menu.actions() if a.text()}
        assert {"Play", "Play Audio", "Play Video", "Remove"} <= set(actions)
        actions["Play Audio"].trigger()
        assert window.now_playing == ("b", 1, True)
        assert [e.audio_only for e in window.queue.entries()] == [False, True, True]
        assert window.pages.currentIndex() != PAGE_VIDEO
