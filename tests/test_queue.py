# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

from mediabrowser.core import playback
from mediabrowser.core.playback import REPEAT_ALL, REPEAT_OFF, REPEAT_ONE, Queue


def entries(*names, video_id="v"):
    return [
        playback.QueueEntry(
            video_id=video_id,
            chapter_index=i,
            audio_only=True,
            title=name,
            video_name="Album",
            duration=100.0,
        )
        for i, name in enumerate(names)
    ]


def titles_played(queue, steps):
    """Walk the queue `steps` times, collecting what comes out."""
    out = [queue.current().title]
    for _ in range(steps):
        entry = queue.next()
        if entry is None:
            break
        out.append(entry.title)
    return out


class TestBasics:
    def test_a_new_queue_is_empty(self):
        queue = Queue()
        assert queue.is_empty()
        assert queue.current() is None
        assert len(queue) == 0

    def test_setting_entries_starts_at_the_first(self):
        queue = Queue()
        assert queue.set_entries(entries("A", "B", "C")).title == "A"

    def test_it_can_start_part_way_in(self):
        queue = Queue()
        assert queue.set_entries(entries("A", "B", "C"), start=2).title == "C"

    def test_a_start_past_the_end_clamps(self):
        queue = Queue()
        assert queue.set_entries(entries("A", "B"), start=9).title == "B"

    def test_setting_no_entries_leaves_it_empty(self):
        queue = Queue()
        assert queue.set_entries([]) is None
        assert queue.is_empty()

    def test_clearing_empties_it(self):
        queue = Queue()
        queue.set_entries(entries("A", "B"))
        queue.clear()
        assert queue.is_empty()
        assert queue.current() is None


class TestWalking:
    def test_next_walks_forward_then_stops(self):
        queue = Queue()
        queue.set_entries(entries("A", "B", "C"))
        assert titles_played(queue, 5) == ["A", "B", "C"]

    def test_previous_walks_back_and_stops_at_the_start(self):
        queue = Queue()
        queue.set_entries(entries("A", "B", "C"), start=2)
        assert queue.previous().title == "B"
        assert queue.previous().title == "A"
        assert queue.previous() is None

    def test_jumping_moves_the_cursor(self):
        queue = Queue()
        queue.set_entries(entries("A", "B", "C"))
        assert queue.jump_to(2).title == "C"
        assert queue.current().title == "C"

    def test_jumping_somewhere_that_is_not_there_does_nothing(self):
        queue = Queue()
        queue.set_entries(entries("A", "B"))
        assert queue.jump_to(9) is None
        assert queue.current().title == "A"


class TestRepeat:
    def test_repeat_all_wraps_at_the_end(self):
        queue = Queue()
        queue.set_entries(entries("A", "B"))
        queue.repeat = REPEAT_ALL
        assert titles_played(queue, 3) == ["A", "B", "A", "B"]

    def test_repeat_all_wraps_backwards_too(self):
        queue = Queue()
        queue.set_entries(entries("A", "B", "C"))
        queue.repeat = REPEAT_ALL
        assert queue.previous().title == "C"

    def test_repeat_one_replays_the_same_entry_when_it_ends(self):
        queue = Queue()
        queue.set_entries(entries("A", "B"))
        queue.repeat = REPEAT_ONE
        assert queue.advance().title == "A"
        assert queue.advance().title == "A"

    def test_pressing_next_still_moves_on_under_repeat_one(self):
        # Otherwise the Next button looks broken.
        queue = Queue()
        queue.set_entries(entries("A", "B"))
        queue.repeat = REPEAT_ONE
        assert queue.next().title == "B"

    def test_repeat_off_ends_the_queue(self):
        queue = Queue()
        queue.set_entries(entries("A"))
        queue.repeat = REPEAT_OFF
        assert queue.advance() is None


class TestShuffle:
    def test_shuffle_keeps_playing_what_is_playing(self):
        queue = Queue()
        queue.set_entries(entries("A", "B", "C", "D", "E"), start=2)
        queue.set_shuffle(True)
        assert queue.current().title == "C"

    def test_shuffle_still_reaches_everything_exactly_once(self):
        queue = Queue()
        queue.set_entries(entries("A", "B", "C", "D", "E"))
        queue.set_shuffle(True)
        assert sorted(titles_played(queue, 10)) == ["A", "B", "C", "D", "E"]

    def test_turning_shuffle_off_returns_to_insertion_order(self):
        queue = Queue()
        queue.set_entries(entries("A", "B", "C", "D"))
        queue.set_shuffle(True)
        queue.set_shuffle(False)
        queue.jump_to(0)
        assert titles_played(queue, 5) == ["A", "B", "C", "D"]

    def test_entries_keep_their_written_order_whatever_the_play_order(self):
        queue = Queue()
        queue.set_entries(entries("A", "B", "C"))
        queue.set_shuffle(True)
        assert [e.title for e in queue.entries()] == ["A", "B", "C"]


class TestEditing:
    def test_appending_adds_to_the_end(self):
        queue = Queue()
        queue.set_entries(entries("A"))
        queue.append(entries("B", "C"))
        assert titles_played(queue, 5) == ["A", "B", "C"]

    def test_appending_to_an_empty_queue_starts_it(self):
        queue = Queue()
        queue.append(entries("A", "B"))
        assert queue.current().title == "A"

    def test_removing_something_else_keeps_playing_what_is_playing(self):
        queue = Queue()
        queue.set_entries(entries("A", "B", "C"), start=2)
        queue.remove(0)
        assert queue.current().title == "C"

    def test_removing_what_is_playing_moves_to_the_next(self):
        queue = Queue()
        queue.set_entries(entries("A", "B", "C"), start=1)
        queue.remove(1)
        assert queue.current().title == "C"

    def test_removing_the_last_entry_empties_the_queue(self):
        queue = Queue()
        queue.set_entries(entries("A"))
        queue.remove(0)
        assert queue.is_empty()
        assert queue.current() is None

    def test_removing_out_of_range_does_nothing(self):
        queue = Queue()
        queue.set_entries(entries("A", "B"))
        queue.remove(9)
        assert len(queue) == 2

    def test_moving_reorders_the_queue(self):
        queue = Queue()
        queue.set_entries(entries("A", "B", "C"))
        queue.move(2, 0)
        assert [e.title for e in queue.entries()] == ["C", "A", "B"]

    def test_moving_keeps_playing_what_is_playing(self):
        queue = Queue()
        queue.set_entries(entries("A", "B", "C"), start=0)
        queue.move(2, 0)
        assert queue.current().title == "A"

    def test_moving_what_is_playing_follows_it(self):
        queue = Queue()
        queue.set_entries(entries("A", "B", "C"), start=0)
        queue.move(0, 2)
        assert queue.current().title == "A"
        assert [e.title for e in queue.entries()] == ["B", "C", "A"]


class TestQueueEntriesFor:
    def test_it_makes_one_entry_per_chapter(self):
        video = {
            "display_name": "Album",
            "chapters": [
                {"title": "One", "start": 0.0, "end": 100.0},
                {"title": None, "start": 100.0, "end": 250.0},
            ],
        }
        made = playback.queue_entries_for("vid", video, audio_only=True)
        assert [e.title for e in made] == ["One", "Chapter 2"]
        assert [e.duration for e in made] == [100.0, 150.0]
        assert all(e.video_id == "vid" and e.audio_only for e in made)


class TestPlayingAs:
    def test_an_entry_and_the_rest_of_its_video_after_it_switch(self):
        queue = Queue()
        queue.set_entries(entries("a", "b", "c") + entries("x", video_id="w"))
        changed = queue.set_audio_only(1, False)
        assert changed == [1, 2], "not the chapter before it, nor the next video"
        assert [e.audio_only for e in queue.entries()] == [True, False, False, True]
        assert queue.set_audio_only(9, False) == []
