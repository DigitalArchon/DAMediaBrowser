# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Playlists: a queue saved by name, played back later as it was queued or
all as audio or video, and kept when the library moves or goes to another
device."""

import pytest

from mediabrowser.core import catalog, library, playback, playlists


def video(name, starts=(0.0, 100.0, 200.0), path=None):
    ends = list(starts[1:]) + [300.0]
    return {"type": "file", "path": path or f"/lib/{name}.mkv", "display_name": name,
            "duration": 300.0,
            "chapters": [{"title": f"{name} {i + 1}", "start": s, "end": e, "source": "manual"}
                         for i, (s, e) in enumerate(zip(starts, ends, strict=True))]}


@pytest.fixture
def data():
    return {"settings": {"library_root": "/lib"},
            "videos": {"a": video("Budokan"), "b": video("Wembley")}}


def entries(*pairs):
    return [playback.QueueEntry(vid, i, True, f"{vid}{i}", vid, 100.0) for vid, i in pairs]


class TestSaving:
    def test_a_queue_across_videos_comes_back_as_audio_or_video(self, data):
        playlists.save(data, "Favourites", entries(("a", 2), ("b", 0), ("a", 0)))
        playlist = playlists.get(data, "favourites")
        queue, missing = playlists.queue_entries(data, playlist, audio_only=False)
        assert missing == 0
        assert [(e.video_id, e.chapter_index, e.audio_only) for e in queue] == [
            ("a", 2, False), ("b", 0, False), ("a", 0, False),
        ]
        assert queue[1].title == "Wembley 1" and queue[1].video_name == "Wembley"

    def test_it_comes_back_playing_as_the_queue_did(self, data):
        playlists.save(data, "Videos", entries(("a", 0), ("b", 1)), audio_only=False)
        playlist = playlists.get(data, "Videos")
        queue, _missing = playlists.queue_entries(data, playlist)
        assert [e.audio_only for e in queue] == [False, False]
        audio, _missing = playlists.queue_entries(data, playlist, True)
        assert [e.audio_only for e in audio] == [True, True], "unless told otherwise"

    def test_one_saved_before_that_was_kept_plays_the_default_way(self, data):
        playlists.save(data, "Old", entries(("a", 0)))
        del playlists.get(data, "Old")["audio_only"]
        queue, _missing = playlists.queue_entries(data, playlists.get(data, "Old"),
                                                  default_audio_only=False)
        assert [e.audio_only for e in queue] == [False]

    def test_saving_under_a_name_in_use_replaces_it(self, data):
        playlists.save(data, "Mix", entries(("a", 0)))
        playlists.save(data, "mix ", entries(("b", 1), ("b", 2)))
        assert len(playlists.all_playlists(data)) == 1
        assert len(playlists.get(data, "Mix")["entries"]) == 2

    def test_a_playlist_needs_a_name(self, data):
        with pytest.raises(ValueError):
            playlists.save(data, "  ", entries(("a", 0)))

    def test_renaming_and_deleting(self, data):
        playlists.save(data, "One", entries(("a", 0)))
        playlists.save(data, "Two", entries(("a", 1)))
        with pytest.raises(ValueError):
            playlists.rename(data, "One", "two")
        playlists.rename(data, "One", "Encores")
        assert [p["name"] for p in playlists.all_playlists(data)] == ["Encores", "Two"]
        assert playlists.delete(data, "Two") and not playlists.delete(data, "Two")


class TestTheLibraryChanging:
    def test_a_chapter_split_since_is_found_by_where_it_starts(self, data):
        playlists.save(data, "Mix", entries(("a", 2)))  # starts at 200
        data["videos"]["a"] = video("Budokan", starts=(0.0, 50.0, 100.0, 200.0))
        queue, _missing = playlists.queue_entries(data, playlists.get(data, "Mix"), True)
        assert queue[0].chapter_index == 3

    def test_a_video_that_has_gone_is_left_out_and_counted(self, data):
        playlists.save(data, "Mix", entries(("a", 0), ("b", 1)))
        del data["videos"]["b"]
        queue, missing = playlists.queue_entries(data, playlists.get(data, "Mix"), True)
        assert len(queue) == 1 and missing == 1

    def test_a_library_moved_elsewhere_keeps_them_pointing_at_its_videos(self, data):
        from mediabrowser.core import store

        data["videos"] = {
            store.make_video_id("file", v["path"]): v
            for v in (video("Budokan"), video("Wembley"))
        }
        first = next(iter(data["videos"]))
        playlists.save(data, "Mix", entries((first, 1)))
        moved = library.relocate_data(data, "/mnt/nas/lib", move_artwork=False)
        new_id = playlists.get(moved, "Mix")["entries"][0]["video_id"]
        assert new_id in moved["videos"] and new_id != first
        assert playlists.get(data, "Mix")["entries"][0]["video_id"] == first, "untouched"

    def test_an_imported_catalog_brings_its_playlists(self, data):
        incoming = {"settings": {"library_root": "/lib"}, "videos": {}}
        playlists.save(incoming, "From the laptop", entries(("a", 0)))
        playlists.save(incoming, "Mix", entries(("b", 0)))
        playlists.save(data, "Mix", entries(("a", 1)))
        catalog.merge(data, incoming)
        names = [p["name"] for p in playlists.all_playlists(data)]
        assert names == ["From the laptop", "Mix"]
        assert playlists.get(data, "Mix")["entries"][0]["video_id"] == "a", "this device's kept"
