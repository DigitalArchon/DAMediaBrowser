# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import PurePath

from mediabrowser.core import folders, store

ROOT = "/media/Videos"


def file_video(path):
    return {"type": "file", "path": path}


def disc(path):
    return {"type": "bluray", "path": path}


class TestOwnFolder:
    def test_a_file_belongs_to_the_folder_it_is_in(self):
        assert folders.own_folder(file_video(f"{ROOT}/Extras/a.mkv")) == PurePath(
            f"{ROOT}/Extras"
        )

    def test_a_disc_is_its_own_folder(self):
        # Hiding the folder it sits in would take every disc beside it too.
        assert folders.own_folder(disc(f"{ROOT}/Discs/Budokan")) == PurePath(
            f"{ROOT}/Discs/Budokan"
        )


class TestRevealTarget:
    def test_a_file_is_shown_in_its_folder(self):
        folder, item = folders.reveal_target(file_video(f"{ROOT}/Live/a.mkv"))
        assert (str(folder), str(item)) == (f"{ROOT}/Live", f"{ROOT}/Live/a.mkv")

    def test_a_disc_folder_is_shown_in_the_folder_holding_it(self):
        folder, item = folders.reveal_target(disc(f"{ROOT}/Discs/Budokan"))
        assert (str(folder), str(item)) == (f"{ROOT}/Discs", f"{ROOT}/Discs/Budokan")


class TestFolderChain:
    def test_every_level_up_to_but_not_including_the_root(self):
        chain = folders.folder_chain(file_video(f"{ROOT}/Extras/Trailers/a.mkv"), ROOT)
        assert [str(f) for f in chain] == [f"{ROOT}/Extras/Trailers", f"{ROOT}/Extras"]

    def test_a_file_in_the_root_itself_has_nothing_to_hide(self):
        assert folders.folder_chain(file_video(f"{ROOT}/a.mkv"), ROOT) == []

    def test_a_disc_can_hide_itself(self):
        chain = folders.folder_chain(disc(f"{ROOT}/Budokan"), ROOT)
        assert [str(f) for f in chain] == [f"{ROOT}/Budokan"]


class TestHiding:
    def test_a_hidden_folder_hides_what_is_inside_it_at_any_depth(self):
        hidden = [f"{ROOT}/Extras"]
        assert folders.is_hidden(file_video(f"{ROOT}/Extras/a.mkv"), hidden)
        assert folders.is_hidden(file_video(f"{ROOT}/Extras/Deep/b.mkv"), hidden)
        assert not folders.is_hidden(file_video(f"{ROOT}/Live/c.mkv"), hidden)

    def test_a_similarly_named_folder_is_not_caught(self):
        assert not folders.is_hidden(
            file_video(f"{ROOT}/Extras2/a.mkv"), [f"{ROOT}/Extras"]
        )

    def test_hide_and_unhide_round_trip(self):
        data = store.default_library()
        folders.hide(data, f"{ROOT}/Extras")
        folders.hide(data, f"{ROOT}/Extras")
        assert folders.hidden_folders(data) == [f"{ROOT}/Extras"]
        folders.unhide(data, f"{ROOT}/Extras")
        assert folders.hidden_folders(data) == []

    def test_a_library_without_the_setting_hides_nothing(self):
        assert folders.hidden_folders(store.default_library()) == []


def test_relative_names_read_from_the_library_root():
    assert folders.relative_name(f"{ROOT}/Extras/Trailers", ROOT) == "Extras/Trailers"


class TestHidingOneVideo:
    def test_a_flagged_video_is_hidden_without_any_folder(self):
        video = disc(f"{ROOT}/Discs/Budokan")
        folders.hide_video(video)
        assert folders.is_hidden(video, [])
        assert folders.hiding(video, []) == []

    def test_unhiding_clears_the_flag(self):
        video = disc(f"{ROOT}/Discs/Budokan")
        folders.hide_video(video)
        folders.unhide_video(video)
        assert not folders.is_hidden(video, [])
