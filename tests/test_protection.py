# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Locked and private videos and libraries, and each video's media type:
what the core does with them. The windows' side is in test_protection_gui."""

import copy

import pytest

from mediabrowser.core import (
    autoname,
    catalog,
    config,
    ffprobe_chapters,
    library,
    naming,
    protection,
    scanner,
    store,
)
from tests.test_autoname import FakeServices, file_video


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LIBRARIES_DIR", tmp_path / "data" / "libraries")
    monkeypatch.setattr(config, "LEGACY_LIBRARY_FILE", tmp_path / "data" / "library.json")
    monkeypatch.setattr(config, "ARTWORK_DIR", tmp_path / "data" / "artwork")


@pytest.fixture
def media(tmp_path):
    root = tmp_path / "Videos"
    root.mkdir()
    path = root / "Concert.mkv"
    path.write_bytes(b"x" * 100)
    return root, path


def probing(monkeypatch, path, chapter_count=2, duration=300.0):
    calls = []

    def fake_probe(target, **kw):
        calls.append(str(target))
        step = duration / chapter_count
        return {"duration": duration, "chapters": [
            {"title": None, "start": i * step, "end": (i + 1) * step}
            for i in range(chapter_count)
        ]}

    monkeypatch.setattr(ffprobe_chapters, "read_regular_file_info", fake_probe)
    monkeypatch.setattr(scanner, "find_media_units", lambda root, **kw: [("file", path)])
    return calls


class TestFlags:
    def test_a_librarys_setting_overrules_the_videos(self):
        data = {"settings": {}, "videos": {}}
        video = {}
        assert not protection.is_locked(data, video)
        protection.set_flag(video, protection.LOCKED, True)
        assert protection.is_locked(data, video) and protection.label(data, video) == "Locked"
        protection.set_flag(video, protection.LOCKED, False)
        protection.set_library_flag(data, protection.LOCKED, True)
        assert protection.is_locked(data, video)
        assert protection.label(data, video) == "Locked (library)"
        assert "whole library" in protection.tip(data, video)

    def test_private_keeps_it_offline_and_locked_more_so(self):
        data = {"settings": {}, "videos": {}}
        private, locked, plain = {"private": True}, {"locked": True}, {}
        assert not protection.may_go_online(data, private)
        assert protection.may_change(data, private)
        assert not protection.may_go_online(data, locked)
        assert not protection.may_change(data, locked)
        assert protection.may_go_online(data, plain)


class TestLockedThroughARescan:
    def test_a_locked_video_is_kept_exactly_as_stored(self, media, monkeypatch):
        root, path = media
        calls = probing(monkeypatch, path)
        data = library.rescan(root)
        video_id, video = next(iter(data["videos"].items()))
        video["chapters"][0].update(title="Megitsune", source="manual")
        video["locked"] = True
        store.save_library(data)
        kept = copy.deepcopy(video)
        path.write_bytes(b"changed" * 50)  # would be probed again
        data = library.rescan(root, force=True)
        assert data["videos"][video_id] == kept
        assert len(calls) == 1, "not even read again"

    def test_a_locked_library_keeps_everything(self, media, monkeypatch):
        root, path = media
        calls = probing(monkeypatch, path)
        data = library.rescan(root)
        protection.set_library_flag(data, protection.LOCKED, True)
        store.save_library(data)
        library.rescan(root, force=True)
        assert len(calls) == 1

    def test_a_locked_video_is_kept_even_when_no_longer_found(self, media, monkeypatch):
        root, path = media
        probing(monkeypatch, path)
        data = library.rescan(root)
        video_id = next(iter(data["videos"]))
        data["videos"][video_id]["locked"] = True
        store.save_library(data)
        monkeypatch.setattr(scanner, "find_media_units", lambda root, **kw: [])
        assert video_id in library.rescan(root)["videos"]

    def test_it_is_not_named_after_its_file(self):
        video = {"type": "file", "display_name": "01. Megitsune", "locked": True,
                 "chapters": [{"title": None, "source": "auto-numbered",
                               "start": 0.0, "end": 200.0}]}
        assert library.name_single_chapters({"settings": {}, "videos": {"v": video}}) == 0
        assert video["chapters"][0]["title"] is None

    def test_the_flags_survive_a_reprobe(self, media, monkeypatch):
        root, path = media
        probing(monkeypatch, path)
        data = library.rescan(root)
        video_id = next(iter(data["videos"]))
        data["videos"][video_id]["private"] = True
        store.save_library(data)
        path.write_bytes(b"changed" * 50)
        assert library.rescan(root)["videos"][video_id]["private"] is True


class TestMediaType:
    def test_a_file_with_chapters_of_its_own(self, media, monkeypatch):
        root, path = media
        probing(monkeypatch, path, chapter_count=5)
        video = next(iter(library.rescan(root)["videos"].values()))
        assert video["media_type"] == library.MEDIA_CHAPTERED
        assert library.media_label(video) == "File with chapters"

    def test_a_file_without_stays_one_when_chapters_are_added(self, media, monkeypatch):
        root, path = media
        probing(monkeypatch, path, chapter_count=1)
        data = library.rescan(root)
        video = next(iter(data["videos"].values()))
        assert video["media_type"] == library.MEDIA_PLAIN
        video["chapters"] = [
            {"title": "A", "start": 0.0, "end": 150.0, "source": "manual"},
            {"title": "B", "start": 150.0, "end": 300.0, "source": "manual"},
        ]
        video["chapter_origin"] = library.ORIGIN_MARKED
        store.save_library(data)
        video = next(iter(library.rescan(root)["videos"].values()))
        assert library.media_label(video) == "File, no chapters"

    def test_an_older_library_is_worked_out_or_read_once(self, media, monkeypatch):
        root, path = media
        calls = probing(monkeypatch, path, chapter_count=1)
        data = library.rescan(root)
        video = next(iter(data["videos"].values()))
        del video["media_type"]
        # The file's own chapters: plain to see.
        assert library.media_type(video) == library.MEDIA_PLAIN
        # Chapters the app made: the file has to be read to know.
        video["chapter_origin"] = library.ORIGIN_ESTIMATED
        assert library.media_type(video) is None and library.media_label(video) == "File"
        store.save_library(data)
        video = next(iter(library.rescan(root)["videos"].values()))
        assert video["media_type"] == library.MEDIA_PLAIN and len(calls) == 2
        library.rescan(root)
        assert len(calls) == 2, "only once"

    def test_a_blu_ray_title(self):
        assert library.media_label({"type": "bluray", "chapters": []}) == "Blu-ray"


class TestIdentifying:
    def test_a_private_video_gets_only_local_methods(self):
        services = FakeServices()
        video = file_video(duration=900.0)
        options = autoname.Options(methods=set(autoname.METHODS),
                                   ai_policy=autoname.ACCURATE_FIRST)
        outcome = autoname.identify("v", video, options, {}, services,
                                    autoname.Budget(10), private=True)
        assert all(call[0] == "levels" for call in services.calls)
        assert outcome.log[0].startswith("Private")
        assert outcome.ai_used == 0

    def test_a_run_passes_it_on(self):
        services = FakeServices()
        options = autoname.Options(methods={autoname.MUSICBRAINZ})
        seen = []
        autoname.run([("v", file_video(duration=900.0))], options, {}, services,
                     on_outcome=seen.append, private={"v"})
        assert seen[0].log[0].startswith("Private") and services.calls == []


class TestCatalogs:
    def test_a_locked_video_here_keeps_its_own_copy(self):
        mine = {"type": "file", "path": "/x", "locked": True,
                "chapters": [{"title": None, "source": "auto-numbered",
                              "start": 0.0, "end": 1.0}]}
        theirs = {"type": "file", "path": "/x",
                  "chapters": [{"title": "Megitsune", "source": "manual",
                                "start": 0.0, "end": 1.0}]}
        current = {"settings": {}, "videos": {"v": mine}}
        catalog.merge(current, {"settings": {}, "videos": {"v": theirs}})
        assert current["videos"]["v"]["chapters"][0]["title"] is None
        assert naming.status(current["videos"]["v"]).state != naming.NAMED
