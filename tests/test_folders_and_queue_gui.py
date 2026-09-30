# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hiding folders, the containing-folder entry, and queueing single
chapters - through the real window, offscreen.
"""

import pytest

from mediabrowser.core import config, folders, store

PySide6 = pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    from mediabrowser.gui.app import build_app

    existing = QApplication.instance()
    yield existing or build_app(["mediabrowser"])


def _video(path, name, n_chapters=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return {
        "type": "file",
        "path": str(path),
        "display_name": name,
        "duration": 100.0 * n_chapters,
        "chapters": [
            {"title": f"Song {i + 1}", "start": i * 100.0, "end": (i + 1) * 100.0,
             "source": "manual"}
            for i in range(n_chapters)
        ],
    }


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    from mediabrowser.core import artwork
    from mediabrowser.gui.main_window import MainWindow

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LIBRARIES_DIR", tmp_path / "libraries")
    monkeypatch.setattr(config, "ARTWORK_DIR", tmp_path / "artwork")
    monkeypatch.setattr(config, "LEGACY_LIBRARY_FILE", tmp_path / "library.json")
    monkeypatch.setattr(config, "APP_SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(artwork, "is_resolved", lambda video_id: True)

    root = tmp_path / "media"
    w = MainWindow()
    w.data = store.default_library(str(root))
    w.data["videos"] = {
        "live": _video(root / "Live" / "Budokan.mkv", "Budokan"),
        "trailer": _video(root / "Extras" / "Trailers" / "Teaser.mkv", "Teaser", 1),
    }
    w.refresh_library()
    yield w
    w.player.stop()
    w.close()


def _labels(menu):
    labels = []
    for action in menu.actions():
        if action.menu() is not None:
            labels.append(action.text())
            labels += [f"  {a.text()}" for a in action.menu().actions()]
        elif action.text():
            labels.append(action.text())
    return labels


def _trigger(menu, label):
    for action in menu.actions():
        if action.text() == label:
            action.trigger()
            return
        if action.menu() is not None:
            for sub in action.menu().actions():
                if sub.text() == label:
                    sub.trigger()
                    return
    raise AssertionError(f"no {label!r} in {_labels(menu)}")


def _grid_item(window, video_id):
    from mediabrowser.gui.grid_view import ROLE_VIDEO_ID

    for row in range(window.grid.count()):
        if window.grid.item(row).data(ROLE_VIDEO_ID) == video_id:
            return window.grid.item(row)
    return None


class TestMenus:
    def test_open_is_gone_from_a_video_menu(self, window):
        # Double-clicking already does it.
        assert "Open" not in _labels(window.grid.context_menu_for(_grid_item(window, "live")))
        row = window.list.topLevelItem(0)
        assert "Open" not in _labels(window.list.context_menu_for(row))

    def test_every_video_menu_can_show_its_folder(self, window, monkeypatch):
        from mediabrowser.gui import reveal

        shown = []
        monkeypatch.setattr(reveal, "show_in_file_manager", lambda v: shown.append(v) or True)
        _trigger(
            window.grid.context_menu_for(_grid_item(window, "live")), "Open Containing Folder"
        )
        window.open_video("live")
        item = window.detail.tree.topLevelItem(0)
        _trigger(window.detail.context_menu_for(item), "Open Containing Folder")
        assert [v["display_name"] for v in shown] == ["Budokan", "Budokan"]

    def test_a_deep_video_offers_every_level_to_hide(self, window):
        labels = _labels(window.grid.context_menu_for(_grid_item(window, "trailer")))
        assert "Hide Folder" in labels
        assert "  Extras/Trailers" in labels and "  Extras" in labels


class TestHiding:
    def test_hiding_takes_the_folder_off_the_shelf(self, window):
        _trigger(window.grid.context_menu_for(_grid_item(window, "trailer")), "Extras")
        assert _grid_item(window, "trailer") is None
        assert _grid_item(window, "live") is not None
        assert window.status_label.text().startswith("Hid “Extras” (1 video).")
        window.refresh_library()
        assert "1 hidden" in window.status_label.text()

    def test_it_is_remembered(self, window):
        window.hide_folder(window.data["videos"]["trailer"]["path"].rsplit("/", 2)[0])
        stored = store.load_library_for_root(window.data["settings"]["library_root"])
        assert folders.hidden_folders(stored)

    def test_hidden_videos_are_not_found_by_search(self, window):
        window.hide_folder(window.data["videos"]["trailer"]["path"].rsplit("/", 1)[0])
        window.search.setText("Teaser")
        window.refresh_library()
        assert window.list.topLevelItemCount() == 0

    def test_show_hidden_brings_them_back_marked(self, window):
        window.hide_folder(window.data["videos"]["trailer"]["path"].rsplit("/", 1)[0])
        window.show_hidden_check.setChecked(True)
        item = _grid_item(window, "trailer")
        assert item is not None and "(hidden)" in item.text()

    def test_unhiding_from_the_menu(self, window):
        folder = window.data["videos"]["trailer"]["path"].rsplit("/", 1)[0]
        window.hide_folder(folder)
        window.show_hidden_check.setChecked(True)
        _trigger(
            window.grid.context_menu_for(_grid_item(window, "trailer")),
            "Unhide Folder “Extras/Trailers”",
        )
        window.show_hidden_check.setChecked(False)
        assert _grid_item(window, "trailer") is not None
        assert "(hidden)" not in _grid_item(window, "trailer").text()


class TestQueueingChapters:
    def test_a_single_chapter_can_be_queued(self, window):
        window.open_video("live")
        _trigger(window.detail.context_menu_for(window.detail.tree.topLevelItem(1)),
                 "Add to Queue")
        assert [(e.video_id, e.chapter_index) for e in window.queue.entries()] == [("live", 1)]

    def test_several_selected_chapters_queue_together_in_order(self, window):
        window.open_video("live")
        tree = window.detail.tree
        tree.topLevelItem(2).setSelected(True)
        tree.topLevelItem(0).setSelected(True)
        menu = window.detail.context_menu_for(tree.topLevelItem(2))
        _trigger(menu, "Add 2 Chapters to Queue")
        assert [e.chapter_index for e in window.queue.entries()] == [0, 2]

    def test_the_button_queues_the_selection(self, window):
        window.open_video("live")
        window.detail.tree.topLevelItem(1).setSelected(True)
        window.detail.queue_button.click()
        assert [e.chapter_index for e in window.queue.entries()] == [1]

    def test_queueing_behind_something_leaves_it_playing(self, window):
        window.open_video("live")
        window.enqueue_chapters("live", [0])
        window.enqueue_chapters("live", [2])
        assert window.queue.current_index() == 0
        assert [e.chapter_index for e in window.queue.entries()] == [0, 2]

    def test_a_found_chapter_can_be_queued_from_the_search_list(self, window):
        window.search.setText("Song 3")
        window.refresh_library()
        row = window.list.topLevelItem(0)
        _trigger(window.list.context_menu_for(row.child(0)), "Add to Queue")
        assert [(e.video_id, e.chapter_index) for e in window.queue.entries()] == [("live", 2)]


class TestMissingVideos:
    def test_a_missing_video_can_be_removed_from_its_menu(self, window, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        from pathlib import Path

        Path(window.data["videos"]["trailer"]["path"]).unlink()
        window.refresh_library()
        _trigger(
            window.grid.context_menu_for(_grid_item(window, "trailer")), "Remove from Library…"
        )
        assert "trailer" not in window.data["videos"]

    def test_a_present_video_offers_no_removal(self, window):
        # It would only come straight back on the next rescan.
        labels = _labels(window.grid.context_menu_for(_grid_item(window, "live")))
        assert "Remove from Library…" not in labels

    def test_remove_missing_forgets_only_the_missing(self, window, monkeypatch):
        from pathlib import Path

        from PySide6.QtWidgets import QMessageBox

        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        Path(window.data["videos"]["trailer"]["path"]).unlink()
        window.refresh_library()
        window.remove_missing_videos()
        assert list(window.data["videos"]) == ["live"]

    def test_declining_removes_nothing(self, window, monkeypatch):
        from pathlib import Path

        from PySide6.QtWidgets import QMessageBox

        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
        Path(window.data["videos"]["trailer"]["path"]).unlink()
        window.refresh_library()
        window.remove_missing_videos()
        assert "trailer" in window.data["videos"]


class TestHidingOneTitle:
    """A disc's titles share its folder, so hiding the folder hides them all.
    One title - an extra beside the concert - has to be hideable alone.
    """

    @pytest.fixture
    def disc(self, window, tmp_path):
        root = tmp_path / "media"
        folder = root / "Live" / "Worlds Collide"
        folder.mkdir(parents=True)
        for idx in (0, 7, 9):
            window.data["videos"][f"title{idx}"] = {
                "type": "bluray", "path": str(folder), "title_idx": idx,
                "display_name": f"Worlds Collide - Title {idx + 1}",
                "duration": 600.0,
                "chapters": [{"title": None, "start": 0.0, "end": 600.0,
                              "source": "auto-numbered"}],
            }
        window.refresh_library()
        return window

    def test_a_title_offers_to_hide_just_itself(self, disc):
        labels = _labels(disc.grid.context_menu_for(_grid_item(disc, "title0")))
        assert "Hide This Title" in labels

    def test_hiding_one_title_leaves_the_others(self, disc):
        _trigger(disc.grid.context_menu_for(_grid_item(disc, "title0")), "Hide This Title")
        assert _grid_item(disc, "title0") is None
        assert _grid_item(disc, "title7") is not None
        assert _grid_item(disc, "title9") is not None

    def test_it_is_remembered(self, disc):
        disc.hide_video("title9")
        stored = store.load_library_for_root(disc.data["settings"]["library_root"])
        assert stored["videos"]["title9"]["hidden"] is True

    def test_a_hidden_title_can_be_unhidden(self, disc):
        disc.hide_video("title9")
        disc.show_hidden_check.setChecked(True)
        assert "(hidden)" in _grid_item(disc, "title9").text()
        _trigger(disc.grid.context_menu_for(_grid_item(disc, "title9")), "Unhide This Title")
        disc.show_hidden_check.setChecked(False)
        assert _grid_item(disc, "title9") is not None

    def test_an_ordinary_file_says_video(self, window):
        labels = _labels(window.grid.context_menu_for(_grid_item(window, "live")))
        assert "Hide This Video" in labels


class TestShelfKeepsItsPlace:
    """Hiding something halfway down must not throw the shelf to the top."""

    @pytest.fixture
    def long_shelf(self, window, tmp_path):
        root = tmp_path / "media"
        for i in range(60):
            path = root / "Many" / f"{i:02}.mkv"
            window.data["videos"][f"v{i:02}"] = _video(path, f"Show {i:02}")
        window.resize(900, 600)
        window.show()
        window.refresh_library()
        return window

    @pytest.mark.parametrize("view", ["grid", "list"])
    def test_hiding_keeps_the_scroll_position(self, long_shelf, view):
        from mediabrowser.gui.main_window import SHELF_GRID, SHELF_LIST

        long_shelf.set_view(SHELF_GRID if view == "grid" else SHELF_LIST)
        widget = getattr(long_shelf, view)
        bar = widget.verticalScrollBar()
        assert bar.maximum() > 0, "the shelf needs to be long enough to scroll"
        bar.setValue(bar.maximum() // 2)
        before = bar.value()
        long_shelf.hide_video("v30")
        assert bar.value() == before

    def test_a_new_search_starts_at_the_top(self, long_shelf):
        from mediabrowser.gui.main_window import SHELF_LIST

        long_shelf.set_view(SHELF_LIST)
        bar = long_shelf.list.verticalScrollBar()
        bar.setValue(bar.maximum() // 2)
        long_shelf.search.setText("Show")
        long_shelf.refresh_library()
        assert bar.value() == 0


class TestIdentifiedOnTheShelf:
    def test_the_list_says_how_far_along_each_video_is(self, window):
        from mediabrowser.core import store

        window.data = store.default_library("/lib")
        window.data["videos"] = {
            "done": {"type": "file", "path": "/lib/a.mkv", "display_name": "A",
                     "duration": 600.0, "chapters": [
                         {"title": "One", "start": 0.0, "end": 300.0},
                         {"title": "Two", "start": 300.0, "end": 600.0}]},
            "todo": {"type": "file", "path": "/lib/b.mkv", "display_name": "B",
                     "duration": 5400.0, "chapters": [
                         {"title": None, "start": 0.0, "end": 5400.0}]},
        }
        window.refresh_library()
        from mediabrowser.gui.list_view import ROLE_NAME

        rows = {window.list.topLevelItem(i).data(0, ROLE_NAME): window.list.topLevelItem(i)
                for i in range(window.list.topLevelItemCount())}
        from mediabrowser.gui.list_view import COL_IDENTIFIED

        assert rows["A"].text(COL_IDENTIFIED) == "● All named"
        assert rows["B"].text(COL_IDENTIFIED) == "● Not split"
        assert "1 to identify" in window.status_label.text()
        window.needs_work_check.setChecked(True)
        assert [window.list.topLevelItem(i).data(0, ROLE_NAME)
                for i in range(window.list.topLevelItemCount())] == ["B"]
