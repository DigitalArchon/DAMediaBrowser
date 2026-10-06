# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Libraries inside libraries, and libraries that move: the titles and
chapters given to a video stay with it whatever is done to the libraries
around it - added, forgotten, added again, renamed, moved to another NAS.

The folders are real (the scanner walks them); ffprobe is faked.
"""

import json
import os

import pytest

from mediabrowser.core import (
    config,
    ffprobe_chapters,
    library,
    protection,
    sharing,
    store,
)
from mediabrowser.core.shelf import Shelf


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LIBRARIES_DIR", tmp_path / "data" / "libraries")
    monkeypatch.setattr(config, "LEGACY_LIBRARY_FILE", tmp_path / "data" / "library.json")
    monkeypatch.setattr(config, "ARTWORK_DIR", tmp_path / "data" / "artwork")


@pytest.fixture(autouse=True)
def probes(monkeypatch):
    def fake_probe(target, **kw):
        return {"duration": 300.0, "chapters": [
            {"title": None, "start": 0.0, "end": 150.0},
            {"title": None, "start": 150.0, "end": 300.0},
        ]}

    monkeypatch.setattr(ffprobe_chapters, "read_regular_file_info", fake_probe)


@pytest.fixture
def share(tmp_path):
    """MusicVids on a NAS (192.168.1.5): BABYMETAL's two shows, and another
    band's."""
    music = tmp_path / "server=192.168.1.5" / "MusicVids"
    for i, rel in enumerate(("BABYMETAL/Budokan.mkv", "BABYMETAL/Wembley.mkv",
                             "Ado/Saitama.mkv")):
        path = music / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * (100 + i))
    return music


def resolved(path) -> str:
    return str(path.resolve())


def named(root, stem, titles=("Megitsune", "Karate")) -> None:
    """Name a video's chapters by hand, as the window would, and save."""
    data = store.load_library_for_root(root)
    video = next(v for v in data["videos"].values() if v["path"].endswith(f"{stem}.mkv"))
    for chapter, title in zip(video["chapters"], titles, strict=True):
        chapter["title"], chapter["source"] = title, "manual"
    store.save_library(data)


def titles(root, stem) -> list:
    data = store.load_library_for_root(root)
    video = next(v for v in data["videos"].values() if v["path"].endswith(f"{stem}.mkv"))
    return [chapter["title"] for chapter in video["chapters"]]


def library_files() -> list:
    return sorted(config.LIBRARIES_DIR.glob("*.json"))


class TestAddingAroundAndInside:
    def test_adding_the_folder_around_a_library_keeps_its_work(self, share):
        babymetal = share / "BABYMETAL"
        library.rescan(babymetal)
        named(babymetal, "Budokan")

        data = library.rescan(share)

        assert len(data["videos"]) == 3
        assert titles(share, "Budokan") == ["Megitsune", "Karate"]
        assert len(library_files()) == 1, "the two now share one file"
        listed = {lib["root"]: lib for lib in store.list_libraries()}
        assert listed[resolved(babymetal)]["within"] == resolved(share)
        assert listed[resolved(babymetal)]["video_count"] == 2
        assert listed[resolved(share)]["video_count"] == 3

    def test_adding_a_folder_inside_a_library_keeps_its_work(self, share):
        library.rescan(share)
        named(share, "Wembley")

        data = library.rescan(share / "BABYMETAL")

        assert len(data["videos"]) == 2
        assert titles(share / "BABYMETAL", "Wembley") == ["Megitsune", "Karate"]

    def test_naming_in_either_names_it_in_both(self, share):
        library.rescan(share / "BABYMETAL")
        library.rescan(share)

        named(share, "Budokan", ("Gimme Chocolate!!", "Road of Resistance"))
        assert titles(share / "BABYMETAL", "Budokan") == ["Gimme Chocolate!!",
                                                          "Road of Resistance"]
        named(share / "BABYMETAL", "Wembley", ("Iine!", "Distortion"))
        assert titles(share, "Wembley") == ["Iine!", "Distortion"]

    def test_an_older_copy_open_elsewhere_cant_undo_a_change(self, share):
        """Both libraries on the shelf: each holds its own copy of the videos
        they share, and saving one mustn't put back what the other changed."""
        library.rescan(share / "BABYMETAL")
        library.rescan(share)
        music = store.load_library_for_root(share)
        babymetal = store.load_library_for_root(share / "BABYMETAL")

        video_id = next(k for k, v in music["videos"].items() if "Budokan" in v["path"])
        music["videos"][video_id]["custom_name"] = "Budokan 2014"
        store.save_library(music)
        babymetal["settings"]["hidden_folders"] = []  # some other change
        store.save_library(babymetal)

        assert store.load_library_for_root(share)["videos"][video_id]["custom_name"] == (
            "Budokan 2014"
        )

    def test_on_one_shelf_a_shared_video_is_there_once(self, share):
        library.rescan(share / "BABYMETAL")
        library.rescan(share)
        shelf = Shelf([store.load_library_for_root(share),
                       store.load_library_for_root(share / "BABYMETAL")])
        assert len(shelf.view()["videos"]) == 3
        assert len(list(shelf.view()["videos"])) == 3

    def test_a_private_library_inside_keeps_its_videos_private_outside(self, share):
        library.rescan(share / "BABYMETAL")
        babymetal = store.load_library_for_root(share / "BABYMETAL")
        protection.set_library_flag(babymetal, protection.PRIVATE, True)
        store.save_library(babymetal)
        music = library.rescan(share)

        private = {v["path"].split("/")[-1] for v in music["videos"].values()
                   if protection.is_private(music, v)}
        assert private == {"Budokan.mkv", "Wembley.mkv"}
        video = next(v for v in music["videos"].values() if "Budokan" in v["path"])
        assert protection.label(music, video) == "Private (BABYMETAL)"


class TestForgetting:
    def test_forgetting_a_library_inside_another_loses_nothing(self, share):
        library.rescan(share)
        library.rescan(share / "BABYMETAL")
        named(share, "Budokan")

        plan = store.forget_plan(share / "BABYMETAL")
        assert plan["lost"] == {} and plan["kept_by"] == [resolved(share)]
        store.delete_library(share / "BABYMETAL")

        assert not store.has_library(share / "BABYMETAL")
        assert titles(share, "Budokan") == ["Megitsune", "Karate"]

    def test_and_adding_it_again_finds_it_all_there(self, share):
        library.rescan(share)
        library.rescan(share / "BABYMETAL")
        named(share / "BABYMETAL", "Wembley")
        store.delete_library(share / "BABYMETAL")

        assert "is inside" in sharing.adding(share / "BABYMETAL")
        data = library.rescan(share / "BABYMETAL")

        assert store.has_library(share / "BABYMETAL")
        assert len(data["videos"]) == 2
        assert titles(share / "BABYMETAL", "Wembley") == ["Megitsune", "Karate"]

    def test_forgetting_the_library_around_one_keeps_the_one(self, share):
        library.rescan(share)
        library.rescan(share / "BABYMETAL")
        named(share, "Budokan")
        named(share, "Saitama")

        plan = store.forget_plan(share)
        assert [v["path"].split("/")[-1] for v in plan["lost"].values()] == ["Saitama.mkv"]
        text = sharing.forgetting(share)
        assert "2 videos stay in BABYMETAL" in text
        assert "1 video - 1 with titles or chapters given here - are in no other" in text
        store.delete_library(share)

        assert not store.has_library(share) and store.has_library(share / "BABYMETAL")
        assert titles(share / "BABYMETAL", "Budokan") == ["Megitsune", "Karate"]
        assert list(store.backup_dir().glob("*.json")), "a copy is kept"

    def test_forgetting_the_last_library_says_what_goes(self, share):
        library.rescan(share)
        named(share, "Saitama")
        text = sharing.forgetting(share)
        assert "3 videos - 1 with titles or chapters given here" in text
        assert "never touched" in text


class TestStoredApartBefore:
    def test_overlapping_libraries_are_folded_together_at_start(self, share):
        """Libraries stored in files of their own, before overlapping ones
        shared: the outer takes the inner's work, the further-along copy of
        a video they both had winning."""
        library.rescan(share)
        named(share, "Budokan")
        music_file = library_files()[0]
        raw = json.loads(music_file.read_text())
        inner = {vid: v for vid, v in raw["videos"].items() if "BABYMETAL" in v["path"]}
        wembley = next(vid for vid, v in inner.items() if "Wembley" in v["path"])
        for chapter, title in zip(inner[wembley]["chapters"], ("Iine!", "Distortion"),
                                  strict=True):
            chapter["title"], chapter["source"] = title, "manual"
        babymetal_root = resolved(share / "BABYMETAL")
        inner_raw = {"settings": dict(raw["settings"], library_root=babymetal_root),
                     "videos": inner}
        store._file_for_root(babymetal_root).write_text(json.dumps(inner_raw))
        os.utime(store._file_for_root(babymetal_root), ns=(1, 1))

        assert store.merge_overlapping() == 1

        assert len(library_files()) == 1
        assert titles(share, "Wembley") == ["Iine!", "Distortion"]
        assert titles(share, "Budokan") == ["Megitsune", "Karate"]
        assert store.has_library(babymetal_root)


class TestMovedAndRenamed:
    def test_a_renamed_folder_inside_a_library_keeps_its_names(self, share):
        library.rescan(share)
        named(share, "Budokan")
        (share / "BABYMETAL").rename(share / "Babymetal")

        data = library.rescan(share)

        assert data[library.MOVED_KEY] == 2
        assert len(data["videos"]) == 3, "no missing copies left behind"
        assert library.missing_videos(data) == []
        assert titles(share, "Budokan") == ["Megitsune", "Karate"]

    def test_a_renamed_file_keeps_its_names(self, share):
        library.rescan(share)
        named(share, "Saitama")
        (share / "Ado" / "Saitama.mkv").rename(share / "Ado" / "Saitama 2024.mkv")

        library.rescan(share)

        assert titles(share, "Saitama 2024") == ["Megitsune", "Karate"]

    def test_two_files_alike_are_left_missing_rather_than_guessed(self, share):
        library.rescan(share)
        named(share, "Saitama")
        ado = share / "Ado" / "Saitama.mkv"
        stat = ado.stat()
        ado.rename(share / "Ado" / "One.mkv")
        twin = share / "Ado" / "Two.mkv"
        twin.write_bytes((share / "Ado" / "One.mkv").read_bytes())
        os.utime(twin, ns=(stat.st_atime_ns, stat.st_mtime_ns))

        data = library.rescan(share)

        assert data[library.MOVED_KEY] == 0
        assert len(library.missing_videos(data)) == 1, "kept, names and all"

    def test_a_renamed_library_folder_is_located(self, share, tmp_path):
        library.rescan(share)
        named(share, "Wembley")
        renamed = share.parent / "Music Videos"
        share.rename(renamed)

        assert "Of " not in sharing.locating(share, renamed), "every video is there"
        library.relocate(share, renamed)

        assert not store.has_library(share) and store.has_library(renamed)
        assert titles(renamed, "Wembley") == ["Megitsune", "Karate"]
        assert library.missing_videos(store.load_library_for_root(renamed)) == []

    def test_a_nas_with_a_new_address_moves_every_library_on_it(self, share, tmp_path):
        """Located from the library inside: the one around it is at the same
        place on the new address, so the whole share moved."""
        library.rescan(share)
        library.rescan(share / "BABYMETAL")
        named(share, "Saitama")
        named(share, "Budokan")
        new_share = tmp_path / "server=192.168.1.9" / "MusicVids"
        new_share.parent.mkdir()
        share.rename(new_share)

        assert library.relocation(share / "BABYMETAL", new_share / "BABYMETAL") == (
            resolved(share), resolved(new_share)
        )
        assert "the whole share moved" in sharing.locating(share / "BABYMETAL",
                                                            new_share / "BABYMETAL")
        library.relocate(share / "BABYMETAL", new_share / "BABYMETAL")

        roots = {lib["root"] for lib in store.list_libraries()}
        assert roots == {resolved(new_share), resolved(new_share / "BABYMETAL")}
        assert titles(new_share, "Saitama") == ["Megitsune", "Karate"]
        assert titles(new_share / "BABYMETAL", "Budokan") == ["Megitsune", "Karate"]

    def test_a_library_moved_to_another_nas_alone_leaves_the_rest(self, share, tmp_path):
        library.rescan(share)
        library.rescan(share / "BABYMETAL")
        named(share, "Budokan")
        named(share, "Saitama")
        other_nas = tmp_path / "server=nas2" / "Concerts" / "BABYMETAL"
        other_nas.parent.mkdir(parents=True)
        (share / "BABYMETAL").rename(other_nas)

        library.relocate(share / "BABYMETAL", other_nas)

        assert store.has_library(other_nas) and store.has_library(share)
        assert titles(other_nas, "Budokan") == ["Megitsune", "Karate"]
        music = store.load_library_for_root(share)
        assert [v["path"].split("/")[-1] for v in music["videos"].values()] == ["Saitama.mkv"]
        assert titles(share, "Saitama") == ["Megitsune", "Karate"]

    def test_a_share_that_moved_is_adopted_when_added_at_its_new_place(self, share,
                                                                       tmp_path):
        """Add Folder on the new place finds the work by itself, as long as
        the old place is gone."""
        from mediabrowser.core import catalog

        library.rescan(share)
        named(share, "Saitama")
        new_share = tmp_path / "server=192.168.1.9" / "MusicVids"
        new_share.parent.mkdir()
        share.rename(new_share)

        found = catalog.find_moved(new_share)
        assert found is not None and found.old_root == resolved(share)
        catalog.adopt(found)
        assert titles(new_share, "Saitama") == ["Megitsune", "Karate"]


# --- in the window -------------------------------------------------------------


class TestTheWindow:
    def test_adding_a_folder_around_a_library_asks_first(self, window, share, monkeypatch):
        from mediabrowser.gui import main_window

        library.rescan(share / "BABYMETAL")
        asked, scanned = [], []
        monkeypatch.setattr(main_window.QFileDialog, "getExistingDirectory",
                            lambda *a, **k: str(share))
        monkeypatch.setattr(window, "_confirm",
                            lambda title, text, detail, action: asked.append(detail)
                            or (False, False))
        monkeypatch.setattr(window, "rescan", lambda *a, **k: scanned.append(a))

        window.choose_folder()

        assert "contains a library you already have" in asked[0]
        assert "BABYMETAL - 2 videos" in asked[0] and "Nothing is lost" in asked[0]
        assert scanned == [], "cancelled: nothing scanned"

    def test_rescanning_a_library_doesnt_ask(self, window, share, monkeypatch):
        from mediabrowser.gui import main_window

        library.rescan(share)
        monkeypatch.setattr(main_window.QFileDialog, "getExistingDirectory",
                            lambda *a, **k: str(share))
        monkeypatch.setattr(window, "_confirm", lambda *a: pytest.fail("asked"))
        scanned = []
        monkeypatch.setattr(window, "rescan", lambda *a, **k: scanned.append(a))
        window.choose_folder()
        assert scanned == [(str(share),)]

    def test_the_panel_says_which_library_one_is_inside(self, window, share):
        library.rescan(share)
        library.rescan(share / "BABYMETAL")
        window.libraries.refresh()
        texts = [window.libraries.list.item(row).text()
                 for row in range(window.libraries.list.count())]
        assert "BABYMETAL\n2 videos · in MusicVids" in texts

    def test_forgetting_a_library_on_show_takes_it_off_the_shelf(self, window, share,
                                                                 monkeypatch):
        library.rescan(share)
        library.rescan(share / "BABYMETAL")
        window.show_libraries([resolved(share), resolved(share / "BABYMETAL")])
        panel = window.libraries
        monkeypatch.setattr(panel, "_confirm_forget", lambda root: True)
        monkeypatch.setattr(panel, "selected_root", lambda: resolved(share / "BABYMETAL"))

        panel._forget_selected()

        assert window.shown.roots() == [resolved(share)]
        window._save()  # nothing brings the forgotten library back
        assert not store.has_library(share / "BABYMETAL")
        assert len(window.data["videos"]) == 3

    def test_locating_a_renamed_library_moves_it(self, window, share, monkeypatch):
        from mediabrowser.gui import main_window

        library.rescan(share)
        named(share, "Saitama")
        renamed = share.parent / "Music Videos"
        share.rename(renamed)
        monkeypatch.setattr(main_window.QFileDialog, "getExistingDirectory",
                            lambda *a, **k: str(renamed))
        confirmed = []
        monkeypatch.setattr(window, "_confirm",
                            lambda title, text, detail, action: confirmed.append(text)
                            or (True, False))
        scanned = []
        monkeypatch.setattr(window, "rescan", lambda root, **k: scanned.append(root))

        window.locate_library(resolved(share))

        assert confirmed == ["Move the library to “Music Videos”?"]
        assert scanned == [resolved(renamed)]
        assert titles(renamed, "Saitama") == ["Megitsune", "Karate"]


class TestRestoring:
    def test_a_forgotten_library_is_restored_names_and_all(self, share):
        library.rescan(share)
        named(share, "Saitama")
        store.delete_library(share)
        assert store.list_libraries() == []

        backups = store.list_backups()
        assert len(backups) == 1
        assert backups[0]["reason"] == store.BACKUP_REASONS["forgotten"]
        assert "1 with titles or chapters given here" in sharing.backup_detail(backups[0])
        assert store.restore_backup(backups[0]["path"]) == [resolved(share)]

        assert store.has_library(share)
        assert titles(share, "Saitama") == ["Megitsune", "Karate"]

    def test_restoring_never_loses_work_done_since(self, share):
        library.rescan(share)
        library.rescan(share / "BABYMETAL")
        named(share, "Budokan", ("Old", "Names"))
        store.delete_library(share / "BABYMETAL")
        backup = store.list_backups()[0]
        named(share, "Wembley", ("Iine!", "Distortion"))  # done after the backup

        store.restore_backup(backup["path"])

        assert store.has_library(share / "BABYMETAL")
        assert titles(share, "Wembley") == ["Iine!", "Distortion"]
        assert titles(share, "Budokan") == ["Old", "Names"]
        assert store.list_backups()[0]["reason"] == store.BACKUP_REASONS["restored"], (
            "what it was restored into is backed up first"
        )

    def test_restoring_leaves_a_locked_video_as_it_is(self, share):
        library.rescan(share)
        named(share, "Saitama")
        store.delete_library(share)
        backup = store.list_backups()[0]
        data = library.rescan(share)  # unnamed again, and then locked
        video = next(v for v in data["videos"].values() if v["path"].endswith("Saitama.mkv"))
        protection.set_flag(video, protection.LOCKED, True)
        store.save_library(data)

        store.restore_backup(backup["path"])

        assert titles(share, "Saitama") == [None, None], "locked: nothing changes it"
        assert titles(share, "Budokan") == [None, None]

    def test_restoring_leaves_a_locked_librarys_videos_as_they_are(self, share):
        library.rescan(share)
        named(share, "Wembley")
        store.delete_library(share)
        backup = store.list_backups()[0]
        data = library.rescan(share)
        protection.set_library_flag(data, protection.LOCKED, True)
        store.save_library(data)

        store.restore_backup(backup["path"])

        assert titles(share, "Wembley") == [None, None]

    def test_a_library_here_already_keeps_its_settings(self, share):
        library.rescan(share)
        store.delete_library(share)
        backup = store.list_backups()[0]
        data = library.rescan(share)
        protection.set_library_flag(data, protection.PRIVATE, True)
        store.save_library(data)

        assert "is here already" in sharing.backup_detail(backup)
        store.restore_backup(backup["path"])
        assert protection.library_flag(store.load_library_for_root(share), protection.PRIVATE)

    def test_a_backup_can_be_deleted_but_nothing_else(self, share, tmp_path):
        library.rescan(share)
        store.delete_library(share)
        backup = store.list_backups()[0]
        with pytest.raises(ValueError):
            store.delete_backup(share / "Ado" / "Saitama.mkv")
        assert (share / "Ado" / "Saitama.mkv").exists()
        store.delete_backup(backup["path"])
        assert store.list_backups() == []


class TestDeletingEverything:
    def test_everything_is_deleted_for_good(self, share):
        from mediabrowser.core import artwork

        library.rescan(share)
        library.rescan(share / "BABYMETAL")
        named(share, "Saitama")
        store.delete_library(share / "BABYMETAL")  # leaves a backup
        config.ensure_artwork_dir()
        cover = artwork.cached_path("some-video")
        cover.write_bytes(b"jpeg")
        store.save_app_settings({"allow_musicbrainz": True})

        text = sharing.everything()
        assert "1 library and 3 videos" in text and "1 video have some" in text
        assert "all 1 backup - so nothing can be restored" in text
        assert "can't be undone" in text and "never touched" in text

        library.delete_all_data()

        assert store.list_libraries() == [] and store.list_backups() == []
        assert not cover.exists()
        assert store.load_app_settings()["allow_musicbrainz"] is True, "settings are kept"
        assert all(path.exists() for path in share.rglob("*.mkv")), "videos untouched"
        data = library.rescan(share)
        assert not any(sharing.has_work(v) for v in data["videos"].values()), (
            "added again, it starts from scratch"
        )


class TestRestoreAndDeleteInTheWindow:
    def test_the_restore_dialog_brings_a_library_back(self, window, share):
        from mediabrowser.gui.dialogs.restore_dialog import RestoreDialog

        library.rescan(share)
        named(share, "Saitama")
        store.delete_library(share)

        dialog = RestoreDialog(window)
        assert dialog.list.count() == 1 and dialog.restore_button.isEnabled()
        dialog.restore()
        assert dialog.restored == [resolved(share)]
        assert titles(share, "Saitama") == ["Megitsune", "Karate"]

    def test_with_no_backups_it_says_so(self, window):
        from mediabrowser.gui.dialogs.restore_dialog import NONE_YET, RestoreDialog

        dialog = RestoreDialog(window)
        assert dialog.detail.text() == NONE_YET and not dialog.restore_button.isEnabled()

    def test_restoring_from_the_window_shows_it(self, window, share, monkeypatch):
        from mediabrowser.gui.dialogs import restore_dialog

        library.rescan(share)
        named(share, "Saitama")
        store.delete_library(share)
        backup = store.list_backups()[0]

        class Restores:
            def __init__(self, parent):
                self.restored = []

            def exec(self):
                self.restored = store.restore_backup(backup["path"])
                return True

        monkeypatch.setattr(restore_dialog, "RestoreDialog", Restores)
        window.restore_library()

        assert window.shown.roots() == [resolved(share)]
        assert len(window.data["videos"]) == 3

    def test_delete_works_only_once_the_box_is_ticked(self, window, share, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        library.rescan(share)
        seen = {}

        def exec_(box):
            go = next(b for b in box.buttons() if b.text() == "Delete Everything")
            seen["before"] = go.isEnabled()
            box.checkBox().setChecked(True)
            seen["after"] = go.isEnabled()
            go.click()
            return 0

        monkeypatch.setattr(QMessageBox, "exec", exec_)
        assert window._confirm_delete_everything()
        assert seen == {"before": False, "after": True}

    def test_deleting_everything_empties_the_window(self, window, share, monkeypatch):
        library.rescan(share)
        window.show_libraries([resolved(share)])
        monkeypatch.setattr(window, "_confirm_delete_everything", lambda: False)
        window.delete_all_library_data()
        assert store.list_libraries(), "cancelled: nothing deleted"

        monkeypatch.setattr(window, "_confirm_delete_everything", lambda: True)
        window.delete_all_library_data()

        assert store.list_libraries() == []
        assert window.shown.roots() == [] and window.libraries.list.count() == 0
        assert not window.welcome.isHidden(), "back to the first launch"


class TestDeletingOneLibrarysData:
    def test_a_librarys_data_is_deleted_for_good(self, share):
        library.rescan(share)
        named(share, "Saitama")
        store.delete_library(share)  # forgotten: a backup is kept
        store.restore_backup(store.list_backups()[0]["path"])

        text = sharing.erasing(share)
        assert "3 videos, 1 with titles or chapters given here" in text
        assert "so Restore can't bring it back" in text and "never touched" in text
        erased = library.delete_library_data(share)

        assert len(erased) == 3
        assert store.list_libraries() == []
        assert store.list_backups() == [], "no backup is left to bring it back"
        assert all(p.exists() for p in share.rglob("*.mkv"))
        data = library.rescan(share)
        assert not any(sharing.has_work(v) for v in data["videos"].values()), (
            "added again, it starts from scratch"
        )

    def test_a_nested_librarys_videos_go_from_the_one_around_it_too(self, share):
        library.rescan(share)
        library.rescan(share / "BABYMETAL")
        named(share, "Budokan")
        named(share, "Saitama")
        store.delete_library(share / "BABYMETAL")
        store.restore_backup(store.list_backups()[0]["path"])

        text = sharing.erasing(share / "BABYMETAL")
        assert "BABYMETAL is inside MusicVids" in text and "deleted from MusicVids too" in text
        library.delete_library_data(share / "BABYMETAL")

        assert not store.has_library(share / "BABYMETAL") and store.has_library(share)
        assert titles(share, "Saitama") == ["Megitsune", "Karate"], "the rest is untouched"
        assert len(store.load_library_for_root(share)["videos"]) == 1
        for backup in store.list_backups():
            assert not any("BABYMETAL" in v["path"] for v in backup["videos"].values())
            assert resolved(share / "BABYMETAL") not in backup["libraries"]
        # MusicVids' next rescan finds them again, untitled.
        data = library.rescan(share)
        budokan = next(v for v in data["videos"].values() if "Budokan" in v["path"])
        assert len(data["videos"]) == 3 and not sharing.has_work(budokan)

    def test_deleting_the_library_around_one_deletes_that_one_too(self, share):
        library.rescan(share)
        library.rescan(share / "BABYMETAL")
        assert "the library inside it: BABYMETAL" in sharing.erasing(share)
        library.delete_library_data(share)
        assert store.list_libraries() == []

    def test_in_the_window(self, window, share, monkeypatch):
        library.rescan(share)
        library.rescan(share / "BABYMETAL")
        window.show_libraries([resolved(share)])
        video_id = next(k for k, v in window.data["videos"].items() if "Budokan" in v["path"])
        window.enqueue_video(video_id)
        assert window.queue.entries()
        monkeypatch.setattr(window, "_confirm_erase", lambda root: False)
        window.delete_library_data(resolved(share / "BABYMETAL"))
        assert store.has_library(share / "BABYMETAL"), "cancelled: nothing deleted"

        monkeypatch.setattr(window, "_confirm_erase", lambda root: True)
        window.delete_library_data(resolved(share / "BABYMETAL"))

        assert not store.has_library(share / "BABYMETAL")
        assert window.shown.roots() == [resolved(share)]
        assert len(window.data["videos"]) == 1, "its videos are gone from MusicVids too"
        assert not window.queue.entries(), "and from the queue"
