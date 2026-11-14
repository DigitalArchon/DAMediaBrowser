# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every file that matters is written whole or not at all: a temp file of a
name of its own beside it (0600), flushed to the disk, swapped in, the folder
flushed after. And every folder of the app's own data is its owner's alone."""

import json
import os
import stat
from pathlib import Path

import pytest

from mediabrowser.core import (
    artwork,
    atomic,
    audio_export,
    catalog,
    chapter_export,
    config,
    store,
)
from tests.test_chapter_export import video


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "LIBRARIES_DIR", data / "libraries")
    monkeypatch.setattr(config, "ARTWORK_DIR", data / "artwork")
    monkeypatch.setattr(config, "LEGACY_LIBRARY_FILE", data / "library.json")
    monkeypatch.setattr(config, "APP_SETTINGS_FILE", data / "settings.json")
    return data


@pytest.fixture
def synced(monkeypatch):
    """What os.fsync was asked to flush: "file" or "folder", in order."""
    flushed = []
    real = os.fsync

    def fsync(fd):
        flushed.append("folder" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    return flushed


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def leftovers(folder) -> list[str]:
    return sorted(path.name for path in Path(folder).iterdir()
                  if path.name.endswith((".tmp", atomic.PART)))


class TestWrittenWhole:
    def test_a_file_is_flushed_swapped_in_and_its_folder_flushed(self, tmp_path, synced):
        target = tmp_path / "settings.json"
        atomic.write_json(target, {"b": 1, "a": [2]})
        assert json.loads(target.read_text()) == {"a": [2], "b": 1}
        assert synced == ["file", "folder"]
        assert mode(target) == 0o600
        assert leftovers(tmp_path) == []

    def test_a_write_that_fails_leaves_the_file_as_it_was_and_no_temp_file(
            self, tmp_path, monkeypatch):
        target = tmp_path / "settings.json"
        target.write_text('{"kept": true}')

        def fail(fd):
            raise OSError("the disk is full")

        monkeypatch.setattr(os, "fsync", fail)
        with pytest.raises(OSError, match="full"):
            atomic.write_json(target, {"lost": True})
        assert json.loads(target.read_text()) == {"kept": True}
        assert leftovers(tmp_path) == []

    def test_each_write_has_a_temp_file_of_its_own(self, tmp_path, monkeypatch):
        """Two writers of one file never share one temp file, as settings.json.tmp
        was: each truncated the other's."""
        made = []
        real = atomic.tempfile.mkstemp

        def mkstemp(*args, **kwargs):
            made.append(real(*args, **kwargs))
            return made[-1]

        monkeypatch.setattr(atomic.tempfile, "mkstemp", mkstemp)
        atomic.write_text(tmp_path / "a.json", "one")
        atomic.write_text(tmp_path / "a.json", "two")
        assert len({name for _fd, name in made}) == 2
        assert all(Path(name).parent == tmp_path for _fd, name in made)

    def test_an_export_has_the_permissions_any_new_file_has(self, tmp_path):
        target = tmp_path / "out.txt"
        atomic.write_text(target, "x", mode=atomic.AS_A_NEW_FILE)
        assert mode(target) == 0o666 & ~atomic.umask()

    def test_the_apps_own_files(self, data_dir, synced, tmp_path):
        store.save_app_settings({"ai_model": "m"})
        data = store.default_library(str(tmp_path / "Videos"))
        store.save_library(data)
        library_file = next((data_dir / "libraries").glob("*.json"))
        store._backup({"path": library_file}, "moved")
        backup = next(store.backup_dir().glob("*.json"))
        for path in (config.APP_SETTINGS_FILE, library_file, backup):
            assert mode(path) == 0o600, path
        assert backup.read_bytes() == library_file.read_bytes()
        assert synced.count("folder") >= 3
        for folder in (data_dir, data_dir / "libraries", store.backup_dir()):
            assert leftovers(folder) == [], folder

    def test_exports_are_written_whole(self, tmp_path, synced):
        chapters = chapter_export.export(video(), chapter_export.CUE, tmp_path / "chapters.cue")
        assert synced[-2:] == ["file", "folder"]
        data = store.default_library(str(tmp_path / "Videos"))
        catalog.export(data, tmp_path / "catalog.json")
        assert synced[-2:] == ["file", "folder"]
        assert leftovers(tmp_path) == []
        for path in (chapters, tmp_path / "catalog.json"):
            assert mode(path) == 0o666 & ~atomic.umask()

    def test_a_cover_stopped_part_way_is_never_left_half_drawn(self, tmp_path,
                                                               monkeypatch):
        """ffmpeg writes a cover as it goes: stopped then - the app killed, the
        machine off - the cache held half a picture, shown at every launch after."""
        video_path = tmp_path / "Glass Harbor.mkv"
        video_path.write_bytes(b"")

        def drawing_then_killed(folder, target):
            Path(target).write_bytes(b"\xff\xd8 half a cover")
            raise KeyboardInterrupt

        monkeypatch.setattr(artwork, "_from_sibling_file", drawing_then_killed)
        with pytest.raises(KeyboardInterrupt):
            artwork.find("abc123", {"path": str(video_path), "type": "file"})
        assert artwork.lookup("abc123") is None
        assert sorted(path.name for path in config.ARTWORK_DIR.iterdir()) == []

    def test_a_cover_found_is_put_in_place_whole(self, tmp_path, monkeypatch, synced):
        video_path = tmp_path / "Glass Harbor.mkv"
        video_path.write_bytes(b"")
        drawn = []

        def drawn_by_ffmpeg(folder, target):
            drawn.append(Path(target))
            Path(target).write_bytes(b"\xff\xd8 a whole cover")
            return True

        monkeypatch.setattr(artwork, "_from_sibling_file", drawn_by_ffmpeg)
        found = artwork.find("abc123", {"path": str(video_path), "type": "file"})
        assert found == artwork.cached_path("abc123") and found.read_bytes().endswith(b"cover")
        assert drawn[0] != found and drawn[0].parent == found.parent
        assert drawn[0].suffix == ".jpg", "ffmpeg tells what to write by the ending"
        assert synced[-2:] == ["file", "folder"]
        assert sorted(path.name for path in config.ARTWORK_DIR.iterdir()) == ["abc123.jpg"]

    def test_a_song_is_written_under_a_name_of_its_own(self, tmp_path):
        song = tmp_path / "Paper Lanterns.opus"
        first, second = audio_export._partial(song), audio_export._partial(song)
        assert first != second and first.parent == song.parent
        assert first.name.startswith(".Paper Lanterns.opus.") and first.name.endswith(".part")


class TestFoldersAreTheOwnersAlone:
    def test_each_folder_is_made_0700_and_one_there_already_is_made_so(self, data_dir):
        for folder in (data_dir, data_dir / "libraries", data_dir / "artwork"):
            folder.mkdir(parents=True, exist_ok=True)
            os.chmod(folder, 0o755)
        config.ensure_data_dir()
        config.ensure_artwork_dir()
        store.config.make_own_folder(store.backup_dir())
        for folder in (data_dir, data_dir / "libraries", data_dir / "artwork",
                       store.backup_dir()):
            assert mode(folder) == 0o700, folder
