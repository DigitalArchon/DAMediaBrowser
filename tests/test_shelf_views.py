# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The shelf's two views and what a search does to them.

These drive the real window offscreen. They are about behaviour a person
would notice - which view is showing, what a search puts in it - not about
pixels.
"""

import pytest

from mediabrowser.core import config, store

PySide6 = pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    from mediabrowser.gui.app import build_app

    existing = QApplication.instance()
    yield existing or build_app(["mediabrowser"])


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LIBRARIES_DIR", tmp_path / "libraries")
    monkeypatch.setattr(config, "ARTWORK_DIR", tmp_path / "artwork")
    monkeypatch.setattr(config, "LEGACY_LIBRARY_FILE", tmp_path / "library.json")
    monkeypatch.setattr(config, "APP_SETTINGS_FILE", tmp_path / "settings.json")
    return tmp_path


def _video(folder, name, *titles):
    """A video backed by a real (empty) file, so the window's missing-file
    check doesn't flag it and relabel every row.
    """
    path = folder / f"{name}.mkv"
    path.write_bytes(b"")
    return {
        "type": "file",
        "path": str(path),
        "display_name": name,
        "duration": 300.0 * len(titles),
        "chapters": [
            {"title": t, "start": i * 300.0, "end": (i + 1) * 300.0, "source": "manual"}
            for i, t in enumerate(titles)
        ],
    }


@pytest.fixture
def window(app, data_dir, tmp_path, monkeypatch):
    from mediabrowser.core import artwork
    from mediabrowser.gui.main_window import MainWindow

    # These files have no content to find art in, and starting a worker per
    # test would leave threads running at teardown.
    monkeypatch.setattr(artwork, "is_resolved", lambda video_id: True)

    media = tmp_path / "media"
    media.mkdir()

    w = MainWindow()
    w.data = store.default_library(str(media))
    w.data["videos"] = {
        "a": _video(media, "Live At Budokan", "Megitsune", "Karate"),
        "b": _video(
            media, "A Very Long Concert Title That Would Otherwise Be Cut Off", "Karate"
        ),
    }
    yield w
    w.player.stop()
    w.close()


class TestViewToggle:
    def test_it_starts_on_tiles(self, window):
        from mediabrowser.gui.main_window import SHELF_GRID

        assert window.shelf.currentIndex() == SHELF_GRID

    def test_it_switches_to_the_list(self, window):
        from mediabrowser.gui.main_window import SHELF_LIST

        window.set_view(SHELF_LIST)
        assert window.shelf.currentIndex() == SHELF_LIST
        assert window.list_button.isChecked()
        assert not window.grid_button.isChecked()

    def test_both_views_hold_every_video(self, window):
        window.refresh_library()
        assert window.grid.count() == 2
        assert window.list.topLevelItemCount() == 2


class TestFullTitles:
    def test_neither_view_shortens_a_title(self, window):
        from PySide6.QtCore import Qt

        assert window.grid.textElideMode() == Qt.ElideNone
        assert window.list.textElideMode() == Qt.ElideNone

    def test_a_long_title_is_present_in_full(self, window):
        window.refresh_library()
        long_name = window.data["videos"]["b"]["display_name"]
        assert any(window.grid.item(i).text() == long_name for i in range(window.grid.count()))
        assert any(
            window.list.topLevelItem(i).text(0) == long_name
            for i in range(window.list.topLevelItemCount())
        )

    def test_tiles_are_tall_enough_for_the_longest_title(self, window):
        window.refresh_library()
        from mediabrowser.gui.grid_view import CHROME_HEIGHT

        # A title that wraps to several lines must have pushed the cell past
        # the height the chrome alone would need.
        assert window.grid.gridSize().height() > CHROME_HEIGHT


class TestSearch:
    def test_searching_switches_to_the_list(self, window):
        from mediabrowser.gui.main_window import SHELF_LIST

        window.search.setText("karate")
        window.refresh_library()
        assert window.shelf.currentIndex() == SHELF_LIST

    def test_it_shows_the_matching_chapters_under_their_video(self, window):
        window.search.setText("megitsune")
        window.refresh_library()

        assert window.list.topLevelItemCount() == 1
        video_row = window.list.topLevelItem(0)
        assert video_row.text(0) == "Live At Budokan"
        assert video_row.childCount() == 1
        assert video_row.child(0).text(0) == "Megitsune"
        assert video_row.isExpanded(), "matches must be visible without expanding anything"

    def test_a_chapter_in_two_videos_is_listed_under_both(self, window):
        window.search.setText("karate")
        window.refresh_library()
        assert window.list.topLevelItemCount() == 2
        assert all(
            window.list.topLevelItem(i).childCount() == 1
            for i in range(window.list.topLevelItemCount())
        )

    def test_no_chapters_are_listed_without_a_search(self, window):
        window.refresh_library()
        assert all(
            window.list.topLevelItem(i).childCount() == 0
            for i in range(window.list.topLevelItemCount())
        )

    def test_clearing_a_search_restores_the_previous_view(self, window):
        from mediabrowser.gui.main_window import SHELF_GRID

        window.search.setText("karate")
        window.refresh_library()
        window.search.setText("")
        window.refresh_library()
        assert window.shelf.currentIndex() == SHELF_GRID

    def test_a_chosen_list_view_survives_a_search(self, window):
        from mediabrowser.gui.main_window import SHELF_LIST

        window.set_view(SHELF_LIST)
        window.search.setText("karate")
        window.refresh_library()
        window.search.setText("")
        window.refresh_library()
        assert window.shelf.currentIndex() == SHELF_LIST


class TestPlayingFromSearch:
    def test_activating_a_found_chapter_plays_it(self, window, monkeypatch):
        played = []
        monkeypatch.setattr(window, "play_chapter", lambda *a, **k: played.append(a))

        window.search.setText("megitsune")
        window.refresh_library()
        window.list._on_activated(window.list.topLevelItem(0).child(0))

        assert played == [("a", 0, True)]

    def test_it_queues_the_rest_of_that_video_behind_it(self, window, monkeypatch):
        monkeypatch.setattr(window, "play_chapter", lambda *a, **k: None)

        window.search.setText("karate")
        window.refresh_library()
        # Karate is the second chapter of Live At Budokan.
        row = next(
            window.list.topLevelItem(i)
            for i in range(window.list.topLevelItemCount())
            if window.list.topLevelItem(i).text(0) == "Live At Budokan"
        )
        window.list._on_activated(row.child(0))

        assert len(window.queue) == 2, "the whole video should be queued, not just the match"
        assert window.queue.current().title == "Karate"

    def test_activating_a_video_row_opens_it_instead(self, window):
        from mediabrowser.gui.main_window import PAGE_DETAIL

        window.search.setText("megitsune")
        window.refresh_library()
        window.list._on_activated(window.list.topLevelItem(0))

        assert window.pages.currentIndex() == PAGE_DETAIL
        assert window.detail.tree.topLevelItemCount() == 2


class TestRememberedView:
    def test_the_chosen_view_is_restored_on_reopening(self, window):
        from mediabrowser.gui.main_window import SHELF_LIST, MainWindow

        window.set_view(SHELF_LIST)
        reopened = MainWindow()
        try:
            assert reopened.shelf.currentIndex() == SHELF_LIST
            assert reopened.list_button.isChecked()
            assert not reopened.grid_button.isChecked()
        finally:
            reopened.close()

    def test_a_search_does_not_change_the_saved_view(self, window):
        window.search.setText("karate")
        window.refresh_library()
        assert "shelf_view" not in store.load_app_settings()


def _trigger(menu, label):
    action = next(a for a in menu.actions() if a.text() == label)
    action.trigger()


class TestPlayingFromTheMenu:
    @pytest.fixture
    def played(self, window, monkeypatch):
        calls = []
        monkeypatch.setattr(window, "play_chapter", lambda *a, **k: calls.append(a))
        return calls

    def test_a_found_chapter_plays_as_video_without_opening(self, window, played):
        from mediabrowser.gui.main_window import PAGE_DETAIL

        window.search.setText("karate")
        window.refresh_library()
        row = next(
            window.list.topLevelItem(i)
            for i in range(window.list.topLevelItemCount())
            if window.list.topLevelItem(i).text(0) == "Live At Budokan"
        )
        _trigger(window.list.context_menu_for(row.child(0)), "Play Video")

        assert played == [("a", 1, False)]
        assert window.pages.currentIndex() != PAGE_DETAIL

    def test_a_list_row_plays_from_the_start_while_browsing(self, window, played):
        window.refresh_library()
        row = next(
            window.list.topLevelItem(i)
            for i in range(window.list.topLevelItemCount())
            if window.list.topLevelItem(i).text(0) == "Live At Budokan"
        )
        _trigger(window.list.context_menu_for(row), "Play Audio")
        assert played == [("a", 0, True)]

    def test_a_tile_plays_video_while_browsing(self, window, played):
        window.refresh_library()
        item = next(
            window.grid.item(i)
            for i in range(window.grid.count())
            if window.grid.item(i).text() == "Live At Budokan"
        )
        _trigger(window.grid.context_menu_for(item), "Play Video")
        assert played == [("a", 0, False)]
        assert len(window.queue) == 2

    def test_a_chapter_in_an_open_video_plays_from_the_menu(self, window, played):
        window.open_video("a")
        _trigger(window.detail.context_menu_for(window.detail.tree.topLevelItem(1)), "Play Video")
        assert played == [("a", 1, False)]



def test_a_name_joined_by_underscores_wraps_rather_than_being_cut_off():
    from mediabrowser.gui import grid_view

    label = grid_view._label_for({"display_name": "BABYMETAL_LEGEND_MM_20NIGHT - Title 2"}, False)
    assert label.replace(grid_view._WRAP_HERE, "") == "BABYMETAL_LEGEND_MM_20NIGHT - Title 2"
    assert label.count(grid_view._WRAP_HERE) == 3
