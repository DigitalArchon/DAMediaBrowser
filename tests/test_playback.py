# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

from mediabrowser.core import playback


def video(kind="file", n_chapters=3, **extra):
    chapters = [
        {"title": None, "start": i * 100.0, "end": (i + 1) * 100.0, "source": "auto-numbered"}
        for i in range(n_chapters)
    ]
    data = {
        "type": kind,
        "path": "/media/Concert.mkv",
        "display_name": "Concert",
        "duration": n_chapters * 100.0,
        "chapters": chapters,
    }
    data.update(extra)
    return data


class TestRemapAfterEditingChapters:
    def _queue(self, v):
        queue = playback.Queue()
        queue.set_entries(playback.queue_entries_for("v", v, True), start=1)
        return queue

    def test_entries_follow_their_music_through_a_split(self):
        v = video(n_chapters=3)
        queue = self._queue(v)
        # Chapter 0 was split in two, so what was chapter 1 is now chapter 2.
        v["chapters"].insert(1, {"title": None, "start": 50.0, "end": 100.0})
        v["chapters"][0]["end"] = 50.0
        queue.remap_video("v", v, lambda old: old if old <= 0 else old + 1)
        assert [e.chapter_index for e in queue.entries()] == [0, 2, 3]

    def test_titles_and_lengths_are_read_again(self):
        v = video(n_chapters=2)
        queue = self._queue(v)
        v["chapters"] = [{"title": "Everything", "start": 0.0, "end": 200.0}]
        queue.remap_video("v", v, lambda old: 0)
        assert [(e.title, e.duration) for e in queue.entries()] == [
            ("Everything", 200.0), ("Everything", 200.0)
        ]

    def test_what_is_playing_stays_playing(self):
        v = video(n_chapters=3)
        queue = self._queue(v)
        queue.remap_video("v", v, lambda old: old)
        assert queue.current_index() == 1

    def test_other_videos_are_untouched(self):
        v = video(n_chapters=2)
        queue = self._queue(v)
        queue.append(playback.queue_entries_for("w", video(n_chapters=2), True))
        queue.remap_video("v", v, lambda old: 0)
        assert [e.chapter_index for e in queue.entries() if e.video_id == "w"] == [0, 1]
