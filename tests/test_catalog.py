# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""A catalog made on one device, used on another that mounts the same
folder somewhere else."""

import shutil

import pytest

from mediabrowser.core import catalog, config, library, store


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LIBRARIES_DIR", tmp_path / "data" / "libraries")
    monkeypatch.setattr(config, "ARTWORK_DIR", tmp_path / "data" / "artwork")
    monkeypatch.setattr(config, "LEGACY_LIBRARY_FILE", tmp_path / "data" / "library.json")


FILES = ["Artist/Show One.mkv", "Artist/Show Two.mkv", "Other/Clip.mp4"]


def folder_with_files(root):
    for name in FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    return root


def catalog_at(root, **extra):
    """A stored catalog of `root`, its first video named."""
    data = store.default_library(root)
    for i, name in enumerate(FILES):
        path = str((root / name).resolve())
        video = {"type": "file", "path": path, "display_name": name, "duration": 600.0,
                 "chapters": [
                     {"start": 0.0, "end": 300.0, "title": None, "source": "auto-numbered"},
                     {"start": 300.0, "end": 600.0, "title": None, "source": "auto-numbered"},
                 ]}
        if i == 0:
            video["chapters"][0].update(title="Megitsune", source="manual")
            video["chapters"][1].update(title="Karate", source="manual")
            video.update(extra)
        data["videos"][store.make_video_id("file", path)] = video
    store.save_library(data)
    return data


@pytest.fixture
def two_devices(tmp_path):
    """A catalog made at device A's mount, and the same files at device B's."""
    a = folder_with_files(tmp_path / "gvfs" / "smb-share:server=nas,share=media" / "Concerts")
    catalog_at(a)
    b = folder_with_files(tmp_path / "mnt" / "nas" / "Concerts")
    shutil.rmtree(tmp_path / "gvfs")  # device A's mount isn't on device B
    return a.resolve(), b.resolve()


def first_video(data, root):
    return data["videos"][store.make_video_id("file", root / FILES[0])]


class TestMatching:
    def test_a_folder_holding_the_videos_matches(self, two_devices):
        a, b = two_devices
        found = catalog.match(store.load_library_for_root(a), b)
        assert found.new_root == str(b) and found.fraction == 1.0

    def test_a_folder_without_them_doesnt(self, two_devices, tmp_path):
        a, _ = two_devices
        (tmp_path / "elsewhere").mkdir()
        assert catalog.match(store.load_library_for_root(a), tmp_path / "elsewhere") is None

    def test_a_catalog_whose_folder_is_here_isnt_taken(self, tmp_path):
        a = folder_with_files(tmp_path / "a")
        catalog_at(a)
        b = folder_with_files(tmp_path / "b")
        assert catalog.find_moved(b) is None, "a's folder is still here: it's a's catalog"


class TestAddingTheFolderOnTheOtherDevice:
    def test_the_catalog_moves_onto_it_names_and_all(self, two_devices):
        a, b = two_devices
        found = catalog.find_moved(b)
        assert found is not None and found.old_root == str(a)
        catalog.adopt(found)
        assert not store.has_library(a) and store.has_library(b)
        data = library.rescan(b)
        video = first_video(data, b)
        assert [c["title"] for c in video["chapters"]] == ["Megitsune", "Karate"]
        assert video["path"] == str(b / FILES[0])


class TestWhatACatalogCarries:
    def test_an_export_leaves_out_what_is_this_devices_or_nobodys(self, tmp_path):
        root = folder_with_files(tmp_path / "Concerts")
        data = catalog_at(root)
        data["settings"]["identify_run"] = {"started": "now", "before": {"a": {}}}
        data["settings"]["reset_undo"] = {"kind": "library"}
        data["settings"]["network_uri"] = "smb://jo:s3cret@nas/Concerts"
        path = tmp_path / "out.json"
        catalog.export(data, path)
        settings = catalog.read(path)["settings"]
        assert "identify_run" not in settings and "reset_undo" not in settings
        assert settings["network_uri"] == "smb://jo@nas/Concerts"

    def test_a_catalog_whose_shape_is_wrong_is_refused(self, tmp_path):
        import json

        for broken in (
            {"settings": {"library_root": ["/x"]}, "videos": {}},
            {"settings": {"library_root": "/x"}, "videos": {"a": "not a video"}},
            {"settings": {"library_root": "/x"}, "videos": {"a": {"path": 7}}},
            {"settings": {"library_root": "/x"}, "videos": {"a": {"path": "/x/a", "chapters": 3}}},
        ):
            bad = tmp_path / "bad.json"
            bad.write_text(json.dumps(broken), encoding="utf-8")
            with pytest.raises(catalog.CatalogError):
                catalog.read(bad)


class TestExportAndImport:
    def test_a_catalog_round_trips_through_a_file(self, two_devices, tmp_path):
        a, b = two_devices
        path = tmp_path / "Concerts catalog.json"
        catalog.export(store.load_library_for_root(a), path)
        store.delete_library(a)
        incoming = catalog.read(path)
        data, changed = catalog.import_into(incoming, b)
        assert changed == 3
        assert first_video(data, b)["chapters"][0]["title"] == "Megitsune"
        assert store.load_library_for_root(b)["settings"]["library_root"] == str(b)

    def test_it_merges_with_what_this_device_already_has(self, two_devices, tmp_path):
        a, b = two_devices
        incoming = store.load_library_for_root(a)
        first_video(incoming, a).update(custom_name="Show One (Budokan)", hidden=True)
        local = catalog_at(b)
        mine = local["videos"][store.make_video_id("file", b / FILES[1])]
        mine["chapters"][0].update(title="Named here", source="manual")
        first_video(local, b)["chapters"][0].update(title=None, source="auto-numbered")
        store.save_library(local)
        data, _changed = catalog.import_into(incoming, b)
        assert [c["title"] for c in first_video(data, b)["chapters"]] == ["Megitsune", "Karate"]
        assert first_video(data, b)["display_name"] == "Show One (Budokan)"
        assert first_video(data, b)["hidden"] is True
        second = data["videos"][store.make_video_id("file", b / FILES[1])]
        assert second["chapters"][0]["title"] == "Named here", "further along here: kept"

    def test_the_wrong_folder_is_refused(self, two_devices, tmp_path):
        a, _ = two_devices
        (tmp_path / "wrong").mkdir()
        with pytest.raises(catalog.CatalogError, match="Too few"):
            catalog.import_into(store.load_library_for_root(a), tmp_path / "wrong")

    def test_a_file_that_isnt_a_catalog(self, tmp_path):
        bad = tmp_path / "x.json"
        bad.write_text('{"hello": 1}')
        with pytest.raises(catalog.CatalogError, match="isn't"):
            catalog.read(bad)
        with pytest.raises(catalog.CatalogError, match="couldn't be read"):
            catalog.read(tmp_path / "missing.json")
