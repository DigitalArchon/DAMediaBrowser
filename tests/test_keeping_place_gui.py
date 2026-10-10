# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Small things that got in the way: lists that jumped back to the top,
a window that couldn't be made narrow, names that lost where they came
from, settings that weren't kept - through the real window, offscreen.
"""

import pytest

from mediabrowser.core import config, store

PySide6 = pytest.importorskip("PySide6")


def _video(path, name, n_chapters=3, source="musicbrainz"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return {
        "type": "file",
        "path": str(path),
        "display_name": name,
        "duration": 100.0 * n_chapters,
        "chapters": [
            {"title": f"Song {i + 1}", "start": i * 100.0, "end": (i + 1) * 100.0,
             "source": source}
            for i in range(n_chapters)
        ],
    }


def _isolate(tmp_path, monkeypatch):
    from mediabrowser.core import artwork

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LIBRARIES_DIR", tmp_path / "libraries")
    monkeypatch.setattr(config, "ARTWORK_DIR", tmp_path / "artwork")
    monkeypatch.setattr(config, "LEGACY_LIBRARY_FILE", tmp_path / "library.json")
    monkeypatch.setattr(config, "APP_SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(artwork, "is_resolved", lambda video_id: True)


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    from mediabrowser.gui.main_window import MainWindow

    _isolate(tmp_path, monkeypatch)
    root = tmp_path / "media"
    w = MainWindow()
    w.data = store.default_library(str(root))
    w.data["videos"] = {
        "live": _video(root / "Salt & Iron" / "Glass Harbor.mkv", "Glass Harbor", 60),
        "other": _video(root / "Paper Lanterns.mkv", "Paper Lanterns"),
    }
    w.refresh_library()
    w.show()
    yield w
    w.player.stop()
    w.close()


def _settle(app):
    for _ in range(3):
        app.processEvents()


class TestWidth:
    def test_manual_edit_does_not_set_how_narrow_the_window_can_be(self, window):
        # Every page of the main area sets the narrowest the window can be;
        # the editor's chapter buttons, in one row, made it the widest.
        player_side = window.editor.splitter.widget(0).minimumSizeHint().width()
        chapters_side = window.editor.splitter.widget(1).minimumSizeHint().width()
        assert chapters_side < player_side / 2

    def test_a_long_title_playing_does_not_widen_the_window(self, app, window):
        _settle(app)
        before = window.minimumSizeHint().width()
        long = " / ".join(["Glass Harbor (Extended Live Version with Orchestra)"] * 4)
        window.now_playing_bar.show_playing(long, f"Audio · {long}", "live", "Glass Harbor")
        _settle(app)
        assert window.minimumSizeHint().width() == before


class TestChapterList:
    def test_renaming_keeps_the_list_where_it_was(self, app, window, monkeypatch):
        from PySide6.QtWidgets import QInputDialog

        window.resize(1400, 800)
        window.open_video("live")
        _settle(app)
        tree = window.detail.tree
        tree.verticalScrollBar().setValue(tree.verticalScrollBar().maximum() // 2)
        _settle(app)
        item = tree.itemAt(10, 40)
        tree.setCurrentItem(item)
        tree.topLevelItem(tree.indexOfTopLevelItem(item) + 1).setSelected(True)
        scrolled = tree.verticalScrollBar().value()
        picked = window.detail.selected_chapters()
        monkeypatch.setattr(QInputDialog, "getText",
                            staticmethod(lambda *args, **kwargs: ("Paper Lanterns", True)))
        window.rename_chapter(window.detail.selected_chapter())
        _settle(app)
        assert tree.verticalScrollBar().value() == scrolled
        assert window.detail.selected_chapters() == picked

    def test_ok_on_an_unchanged_name_keeps_where_it_came_from(self, window, monkeypatch):
        from PySide6.QtWidgets import QInputDialog

        window.open_video("other")
        monkeypatch.setattr(QInputDialog, "getText",
                            staticmethod(lambda *args, **kwargs: ("Song 2", True)))
        window.rename_chapter(1)
        assert window.data["videos"]["other"]["chapters"][1]["source"] == "musicbrainz"

    def test_edit_tracklist_marks_only_the_names_changed_as_yours(self, window, monkeypatch):
        from mediabrowser.gui.dialogs import edit_dialog

        window.open_video("other")
        monkeypatch.setattr(edit_dialog.EditTracklistDialog, "exec", lambda self: True)
        monkeypatch.setattr(edit_dialog.EditTracklistDialog, "titles",
                            lambda self: ["Song 1", "Glass Harbor", "Song 3"])
        window.edit_tracklist()
        chapters = window.data["videos"]["other"]["chapters"]
        assert [c["source"] for c in chapters] == ["musicbrainz", "manual", "musicbrainz"]
        assert chapters[1]["title"] == "Glass Harbor"


class TestEditTracklist:
    def test_editing_a_title_keeps_the_list_where_it_was(self, app):
        from mediabrowser.gui.dialogs.edit_dialog import EditTracklistDialog

        video = {"chapters": [{"title": f"Song {i}", "start": i * 100.0,
                               "end": (i + 1) * 100.0} for i in range(60)]}
        dialog = EditTracklistDialog(None, video)
        dialog.show()
        _settle(app)
        bar = dialog.tree.verticalScrollBar()
        bar.setValue(bar.maximum() // 2)
        _settle(app)
        scrolled = bar.value()
        item = dialog.tree.itemAt(20, 40)
        row = dialog.tree.indexOfTopLevelItem(item)
        item.setText(2, "Glass Harbor")
        _settle(app)
        assert bar.value() == scrolled
        assert dialog._selected() == row
        assert dialog.titles()[row] == "Glass Harbor"
        dialog.close()


class TestQueue:
    def _entries(self, count):
        from mediabrowser.core.playback import QueueEntry

        return [QueueEntry("live", i, True, f"Song {i}", "Glass Harbor", 60.0)
                for i in range(count)]

    def test_removing_an_entry_keeps_the_list_where_it_was(self, app):
        from mediabrowser.gui.queue_panel import QueuePanel

        panel = QueuePanel()
        panel.resize(250, 400)
        panel.show()
        entries = self._entries(40)
        panel.show_queue(entries, 2)
        _settle(app)
        bar = panel.list.verticalScrollBar()
        bar.setValue(bar.maximum() // 2)
        scrolled = bar.value()
        del entries[35]
        panel.show_queue(entries, 2)
        _settle(app)
        assert bar.value() == scrolled
        # The next entry playing is brought into view.
        panel.show_queue(entries, 3)
        _settle(app)
        assert bar.value() < scrolled

    def test_repeat_is_lit_while_it_repeats(self, app):
        from mediabrowser.gui.queue_panel import QueuePanel

        panel = QueuePanel()
        lit = []
        for _ in range(3):
            panel.repeat_button.click()
            lit.append((panel.repeat_button.text(), panel.repeat_button.isChecked()))
        assert lit == [("Repeat All", True), ("Repeat One", True), ("Repeat", False)]


class TestShelf:
    def test_the_video_picked_stays_picked(self, window):
        from mediabrowser.gui.grid_view import ROLE_VIDEO_ID

        for row in range(window.grid.count()):
            if window.grid.item(row).data(ROLE_VIDEO_ID) == "other":
                window.grid.setCurrentItem(window.grid.item(row))
        window.list.setCurrentItem(window.list.topLevelItem(1))
        picked = window.list.selected_video_id()
        window.hide_video("live")
        window.unhide_video("live")
        assert window.grid.selected_video_id() == "other"
        assert window.list.selected_video_id() == picked

    def test_a_folder_with_an_ampersand_keeps_it_in_the_menu(self, window):
        menu = window.grid.context_menu_for(window.grid.item(0))
        labels = [action.text() for action in menu.actions()]
        assert "Hide Folder “Salt && Iron”" in labels


class TestSearchBox:
    def test_ctrl_f_selects_what_is_there_and_esc_clears_it(self, app, window):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        window.activateWindow()
        QTest.qWaitForWindowActive(window)
        window.search.setText("glass")
        window.focus_search()
        assert window.search.selectedText() == "glass"
        QTest.keyClick(window.search, Qt.Key_Escape)
        assert window.search.text() == ""


class TestRemembered:
    def test_the_sort_and_the_window_are_kept_for_next_time(self, app, tmp_path, monkeypatch):
        from mediabrowser.gui import main_window

        _isolate(tmp_path, monkeypatch)
        first = main_window.MainWindow()
        first.show()
        first._choose_sort("duration", "Length")
        first.libraries_dock.hide()
        first.close()
        saved = store.load_app_settings()
        assert saved[main_window.SORT_SETTING] == "duration"
        assert set(saved[main_window.WINDOW_SETTING]) == {"geometry", "panels"}

        second = main_window.MainWindow()
        second.show()
        assert second.sort_button.text() == "Sort: Length"
        assert not second.libraries_dock.isVisible()
        second.close()

    def test_a_damaged_setting_is_ignored(self, app, tmp_path, monkeypatch):
        from mediabrowser.gui import main_window

        _isolate(tmp_path, monkeypatch)
        store.save_app_settings({main_window.WINDOW_SETTING: {"geometry": "not base64 ☃"},
                                 main_window.SORT_SETTING: "nonsense"})
        w = main_window.MainWindow()
        assert w.sort_button.text() == "Sort: Name"
        w.close()
