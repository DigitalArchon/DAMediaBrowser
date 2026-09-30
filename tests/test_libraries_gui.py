# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Several libraries on the shelf at once, and resetting a title or a
library to defaults (and undoing it) - through the real window, offscreen."""

import pytest

from mediabrowser.core import library, playback, playlists, reset, store

PySide6 = pytest.importorskip("PySide6")


def _video(path, name):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return {"type": "file", "path": str(path), "display_name": name, "auto_name": name,
            "duration": 200.0, "media_type": "chaptered",
            "chapters": [{"title": f"{name} {i + 1}", "start": i * 100.0,
                          "end": (i + 1) * 100.0, "source": "manual"} for i in range(2)]}


@pytest.fixture
def two(window, tmp_path):
    """Two stored libraries, the first on show."""
    roots = []
    for name in ("Concerts", "Festivals"):
        root = tmp_path / name
        data = store.default_library(str(root))
        vid = store.make_video_id("file", root / f"{name}.mkv")
        data["videos"] = {vid: _video(root / f"{name}.mkv", name)}
        store.save_library(data)
        roots.append(str(root))
    window.load_root(roots[0])
    return window, roots


def ids(window):
    return set(window.data["videos"])


class TestSeveralAtOnce:
    def test_ticking_a_second_library_adds_it_to_the_shelf(self, two):
        window, roots = two
        assert len(ids(window)) == 1
        panel = window.libraries
        for row in range(panel.list.count()):
            panel.list.item(row).setCheckState(PySide6.QtCore.Qt.Checked)
        assert len(ids(window)) == 2 and window.list.topLevelItemCount() == 2
        assert "2 libraries" in window.status_label.text()
        assert sorted(panel.ticked()) == sorted(roots)

    def test_each_video_is_saved_in_its_own_library(self, two, monkeypatch):
        from PySide6.QtWidgets import QInputDialog

        window, roots = two
        window.show_libraries(roots)
        festival = store.make_video_id("file", f"{roots[1]}/Festivals.mkv")
        window.open_video(festival)
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Megitsune", True))
        window.rename_chapter(0)
        stored = store.load_library_for_root(roots[1])
        assert stored["videos"][festival]["chapters"][0]["title"] == "Megitsune"
        assert festival not in store.load_library_for_root(roots[0])["videos"]

    def test_a_librarys_lock_holds_only_for_its_own_videos(self, two):
        from PySide6.QtWidgets import QMenu

        window, roots = two
        window.show_libraries(roots)
        window.set_library_flag(roots[1], "locked", True)
        assert window.locked_ids() == {store.make_video_id("file", f"{roots[1]}/Festivals.mkv")}
        concert = store.make_video_id("file", f"{roots[0]}/Concerts.mkv")
        menu = QMenu()
        window._add_folder_actions(menu, concert)
        assert "Locked" in [a.text() for a in menu.actions()], "not the whole-library one"

    def test_what_was_on_show_is_on_show_after_a_restart(self, two, app):
        from mediabrowser.gui.main_window import MainWindow

        window, roots = two
        window.show_libraries(roots)
        reopened = MainWindow()
        try:
            assert reopened.shown.roots() == roots
        finally:
            reopened.close()

    def test_rescan_rescans_every_library_on_show(self, two, monkeypatch, app):
        from tests.test_chapters_dialog import pump

        window, roots = two
        window.show_libraries(roots)
        scanned = []

        def rescan(root, **kwargs):
            scanned.append(root)
            return store.load_library_for_root(root)

        monkeypatch.setattr(library, "rescan", rescan)
        window.rescan()
        assert pump(app, lambda: not window._scanning)
        assert scanned == roots and window.shown.roots() == roots

    def test_a_playlist_can_span_them(self, two, monkeypatch):
        from PySide6.QtWidgets import QInputDialog

        window, roots = two
        window.show_libraries(roots)
        concert = store.make_video_id("file", f"{roots[0]}/Concerts.mkv")
        festival = store.make_video_id("file", f"{roots[1]}/Festivals.mkv")
        window.queue.set_entries([playback.QueueEntry(vid, 1, True, "t", "v", 100.0)
                                  for vid in (concert, festival)])
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Both", True))
        window.save_queue_as_playlist()
        window.clear_queue()
        window.play_playlist("Both", audio_only=True)
        assert [e.video_id for e in window.queue.entries()] == [concert, festival]
        # With only the first library on show, the other's entry is left out - and said.
        window.load_root(roots[0])
        assert playlists.get(window.data, "Both") is not None
        window.play_playlist("Both", audio_only=True)
        assert [e.video_id for e in window.queue.entries()] == [concert]
        assert "left out" in window.status_label.text()


class TestResetting:
    @pytest.fixture
    def agree(self, monkeypatch):
        from mediabrowser.gui.main_window import MainWindow

        answers = {"accept": True, "check": False}
        monkeypatch.setattr(MainWindow, "_confirm",
                            lambda self, *a, **k: (answers["accept"], answers["check"]))
        from PySide6.QtWidgets import QMessageBox

        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        return answers

    def test_a_title_is_reset_and_undone(self, two, agree, monkeypatch, app):
        from tests.test_chapters_dialog import pump

        window, roots = two
        vid = next(iter(ids(window)))
        monkeypatch.setattr(library, "original_chapters", lambda video, minimum: [
            {"title": None, "start": 0.0, "end": 200.0, "source": "auto-numbered", "index": 0}
        ])
        window.reset_video(vid)
        assert pump(app, lambda: reset.last(window.data) is not None)
        assert len(window.data["videos"][vid]["chapters"]) == 1
        assert "Undo Reset" in window.status_label.text()
        [file_menu] = [menu for menu in window.menuBar().findChildren(PySide6.QtWidgets.QMenu)
                       if window.undo_reset_action in menu.actions()]
        file_menu.aboutToShow.emit()
        assert window.undo_reset_action.text().startswith("Undo the reset of Concerts ("), \
            "the name as it's written"
        window.undo_reset()
        assert window.data["videos"][vid]["chapters"][0]["title"] == "Concerts 1"
        assert reset.last(store.load_library_for_root(roots[0])) is None

    def test_cancelling_the_warning_changes_nothing(self, two, agree):
        window, _roots = two
        agree["accept"] = False
        vid = next(iter(ids(window)))
        window.reset_video(vid)
        assert reset.last(window.data) is None

    def test_a_locked_title_is_not_offered_it(self, two):
        from PySide6.QtWidgets import QMenu

        window, _roots = two
        vid = next(iter(ids(window)))
        window.set_video_flag(vid, "locked", True)
        menu = QMenu()
        window._add_folder_actions(menu, vid)
        assert not [a for a in menu.actions() if a.text() == "Reset to Defaults…"][0].isEnabled()

    def test_a_library_is_reset_and_undone(self, two, agree, monkeypatch, app):
        from tests.test_chapters_dialog import pump

        window, roots = two

        def fresh_scan(root, **kwargs):
            data = store.load_library_for_root(root)
            for video in data["videos"].values():
                for chapter in video["chapters"]:
                    chapter.update(title=None, source="auto-numbered")
            return data

        monkeypatch.setattr(library, "rescan", fresh_scan)
        window.reset_library(roots[0])
        assert pump(app, lambda: not window._scanning)
        vid = next(iter(ids(window)))
        assert window.data["videos"][vid]["chapters"][0]["title"] is None
        window.undo_reset()
        assert window.data["videos"][vid]["chapters"][0]["title"] == "Concerts 1"
