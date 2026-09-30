# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Resetting a title or a library to how it was first scanned, and undoing
it - with ffprobe and the scanner stubbed out."""

import copy

import pytest

from mediabrowser.core import (
    catalog,
    config,
    ffprobe_chapters,
    library,
    playlists,
    reset,
    scanner,
    store,
)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LIBRARIES_DIR", tmp_path / "data" / "libraries")
    monkeypatch.setattr(config, "LEGACY_LIBRARY_FILE", tmp_path / "data" / "library.json")
    monkeypatch.setattr(config, "ARTWORK_DIR", tmp_path / "data" / "artwork")


@pytest.fixture
def media(tmp_path, monkeypatch):
    root = tmp_path / "Videos"
    root.mkdir()
    paths = [root / "Budokan.mkv", root / "Wembley.mkv"]
    for path in paths:
        path.write_bytes(b"x" * 100)

    def probe(target, **kw):
        return {"duration": 300.0, "chapters": [
            {"title": None, "start": 0.0, "end": 150.0},
            {"title": None, "start": 150.0, "end": 300.0},
        ]}

    monkeypatch.setattr(ffprobe_chapters, "read_regular_file_info", probe)
    monkeypatch.setattr(scanner, "find_media_units",
                        lambda root, **kw: [("file", p) for p in paths if p.exists()])
    return root, paths


def worked_on(root):
    """A library someone has spent time on."""
    data = library.rescan(root)
    for video in data["videos"].values():
        video["chapters"][0].update(title="Megitsune", source="manual")
        library.set_custom_name(video, f"My {video['display_name']}")
        video["musicbrainz_release_id"] = "rel"
    data["settings"]["hidden_folders"] = [str(root / "Extras")]
    playlists.save(data, "Mix", [])
    store.save_library(data)
    return data


class TestATitle:
    def test_it_goes_back_to_its_own_chapters_and_can_come_back(self, media):
        root, _paths = media
        data = worked_on(root)
        video_id, video = next(iter(data["videos"].items()))
        before = copy.deepcopy(video)
        assert reset.describe_video(video) == [
            "1 chapter name(s) given here", f"its name “{video['custom_name']}”",
            "the MusicBrainz release it was named from",
        ]
        reset.reset_video(data, video_id, library.original_chapters(video, 20))
        assert video["chapters"][0]["title"] is None
        assert video["display_name"] == video["auto_name"] and "custom_name" not in video
        assert "musicbrainz_release_id" not in video
        assert reset.undo(data).startswith("the reset of My")
        assert data["videos"][video_id] == before
        assert reset.last(data) is None

    def test_a_locked_one_is_refused(self, media):
        root, _paths = media
        data = worked_on(root)
        video_id, video = next(iter(data["videos"].items()))
        video["locked"] = True
        with pytest.raises(reset.ResetError):
            reset.reset_video(data, video_id, [])


class TestALibrary:
    def test_everything_added_is_cleared_and_undo_brings_it_back(self, media):
        root, _paths = media
        before = worked_on(root)
        data = reset.reset_library(root)
        for video in data["videos"].values():
            assert video["chapters"][0]["title"] is None
            assert "custom_name" not in video and "musicbrainz_release_id" not in video
        assert "hidden_folders" not in data["settings"]
        assert not playlists.all_playlists(data), "playlists go unless kept"
        # The undo survives a restart: it's in the stored library.
        stored = store.load_library_for_root(root)
        assert reset.describe(reset.last(stored)).startswith("the reset of the whole library")
        reset.undo(stored)
        assert stored["videos"] == before["videos"]
        assert stored["settings"]["hidden_folders"] == [str(root / "Extras")]
        assert playlists.get(stored, "Mix") is not None

    def test_playlists_can_be_kept(self, media):
        root, _paths = media
        worked_on(root)
        assert playlists.get(reset.reset_library(root, keep_playlists=True), "Mix")

    def test_locked_and_private_are_kept(self, media):
        root, _paths = media
        data = worked_on(root)
        (locked_id, locked), (private_id, private) = list(data["videos"].items())
        locked["locked"] = True
        private["private"] = True
        kept = copy.deepcopy(locked)
        store.save_library(data)
        data = reset.reset_library(root)
        assert data["videos"][locked_id] == kept
        assert data["videos"][private_id]["private"] is True
        assert data["videos"][private_id]["chapters"][0]["title"] is None

    def test_a_video_that_cannot_be_read_now_is_kept(self, media):
        root, paths = media
        data = worked_on(root)
        gone = paths[1]
        gone_id = store.make_video_id("file", gone)
        kept = copy.deepcopy(data["videos"][gone_id])
        gone.unlink()
        assert reset.reset_library(root)["videos"][gone_id] == kept

    def test_a_locked_library_is_refused(self, media):
        root, _paths = media
        data = worked_on(root)
        data["settings"]["locked"] = True
        store.save_library(data)
        with pytest.raises(reset.ResetError):
            reset.reset_library(root)

    def test_a_catalog_exported_after_does_not_carry_the_undo(self, media, tmp_path):
        root, _paths = media
        worked_on(root)
        data = reset.reset_library(root)
        catalog.export(data, tmp_path / "out.json")
        assert reset.last(catalog.read(tmp_path / "out.json")) is None
