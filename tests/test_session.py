# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Playing the queue through one mpv: which pieces it's handed, how it
follows mpv from one to the next, and what's sent to mpv for them."""

from mediabrowser.core import letterbox, playback
from mediabrowser.core import player as player_module
from tests.fake_mpv import FakeMpv


def video(kind="file", starts=(0.0, 100.0, 200.0), duration=300.0, path="/v/a.mkv"):
    ends = list(starts[1:]) + [duration]
    return {"type": kind, "path": path, "display_name": path, "duration": duration,
            "title_idx": 3 if kind == "bluray" else None,
            "chapters": [{"title": f"c{i}", "start": s, "end": e, "source": "manual"}
                         for i, (s, e) in enumerate(zip(starts, ends, strict=True))]}


VIDEOS = {"a": video(), "b": video(path="/v/b.mkv"), "d": video("bluray", path="/disc")}


def entry(video_id, chapter, audio=True):
    return playback.QueueEntry(video_id, chapter, audio, f"{video_id}{chapter}", video_id, 100)


def queue_of(*entries, start=0):
    queue = playback.Queue()
    queue.set_entries(entries, start=start)
    return queue


class TestPieces:
    def test_chapters_one_after_another_are_one_piece(self):
        queue = queue_of(entry("a", 0), entry("a", 1), entry("b", 0))
        piece = playback.plan_segment(queue, VIDEOS.get, 0)
        assert piece.entries == (0, 1) and (piece.start, piece.end) == (0.0, 200.0)
        assert piece.entry_at(150.0) == 1 and piece.entry_at(20.0) == 0

    def test_a_gap_or_another_video_starts_a_new_one(self):
        for second in (entry("a", 2), entry("b", 1)):
            queue = queue_of(entry("a", 0), second)
            assert playback.plan_segment(queue, VIDEOS.get, 0).entries == (0,)

    def test_video_runs_on_to_the_end_of_the_file_when_nothing_follows(self):
        alone = queue_of(entry("a", 1, audio=False))
        assert playback.plan_segment(alone, VIDEOS.get, 0).end is None
        followed = queue_of(entry("a", 1, audio=False), entry("b", 0, audio=False))
        assert playback.plan_segment(followed, VIDEOS.get, 0).end == 200.0
        audio = queue_of(entry("a", 1))
        assert playback.plan_segment(audio, VIDEOS.get, 0).end == 200.0

    def test_repeat_one_plays_one_chapter_over(self):
        queue = queue_of(entry("a", 0), entry("a", 1))
        queue.repeat = playback.REPEAT_ONE
        assert playback.plan_segment(queue, VIDEOS.get, 0).entries == (0,)
        assert playback.following(queue, 0) == 0

    def test_repeat_all_goes_round(self):
        queue = queue_of(entry("a", 0), entry("b", 0))
        assert playback.following(queue, 1) is None
        queue.repeat = playback.REPEAT_ALL
        assert playback.following(queue, 1) == 0

    def test_a_video_that_has_gone_has_no_piece(self):
        queue = queue_of(entry("gone", 0))
        assert playback.plan_segment(queue, VIDEOS.get, 0) is None


class TestSession:
    def session(self, *entries):
        mpv = FakeMpv()
        queue = queue_of(*entries)
        return playback.Session(mpv, queue, VIDEOS.get), mpv, queue

    def test_it_holds_what_plays_and_what_comes_next(self):
        session, mpv, _queue = self.session(entry("a", 0), entry("a", 1), entry("b", 0))
        session.play()
        assert mpv.calls == [("start", None), ("load", "a", (0, 1), False),
                             ("load", "b", (2,), True)]

    def test_it_follows_mpv_through_a_piece_and_on_to_the_next(self):
        session, mpv, queue = self.session(entry("a", 0), entry("a", 1), entry("b", 0),
                                           entry("d", 0))
        session.play()
        mpv.state["position"] = 150.0
        tick = session.tick()
        assert tick.moved and queue.current_index() == 1
        mpv.calls.clear()
        mpv.state.update(playing_id=2, position=1.0)  # mpv went on to b by itself
        tick = session.tick()
        assert tick.moved and queue.current_index() == 2
        assert mpv.calls == [("remove", 0), ("load", "d", (3,), True)]

    def test_choosing_something_new_plays_it_even_when_paused(self):
        session, mpv, queue = self.session(entry("a", 0), entry("b", 0))
        session.play()
        assert ("paused", False) not in mpv.calls, "a new mpv starts playing anyway"
        mpv.toggle_pause()
        queue.jump_to(1)
        session.play()
        assert mpv.state["paused"] is False and ("start", None) not in mpv.calls[1:]

    def test_it_says_when_the_queue_has_played_out_or_mpv_was_closed(self):
        session, mpv, _queue = self.session(entry("a", 0))
        session.play()
        mpv.state["idle"] = True
        assert session.tick().finished
        session.play()
        mpv.active = False
        assert session.tick().closed

    def test_one_mpv_is_kept_for_the_whole_session(self):
        session, mpv, queue = self.session(entry("a", 0), entry("b", 0))
        session.play()
        queue.next()
        session.play()
        assert [c for c in mpv.calls if c[0] == "start"] == [("start", None)]
        session.play(window_id=42)  # somewhere else: a new one
        assert [c for c in mpv.calls if c[0] == "start"] == [("start", None), ("start", 42)]

    def test_an_edit_behind_what_plays_is_played_as_edited(self):
        session, mpv, queue = self.session(entry("a", 0), entry("a", 1), entry("a", 2),
                                           entry("b", 0))
        session.play()
        mpv.calls.clear()
        queue.remove(1)  # the chapter after the playing one
        session.queue_changed()
        assert mpv.calls == [("end", 100.0), ("clear",), ("load", "a", (1,), True)]
        assert session.current().entries == (0,)

    def test_a_missing_video_further_on_is_skipped_too(self):
        session, mpv, _queue = self.session(entry("a", 0), entry("gone", 0), entry("b", 0))
        session.play()
        assert mpv.calls[-1] == ("load", "b", (2,), True)

    def test_a_missing_video_is_skipped(self):
        session, mpv, queue = self.session(entry("gone", 0), entry("b", 0))
        session.play()
        assert queue.current_index() == 1 and mpv.calls[1] == ("load", "b", (1,), False)


class TestBlackBars:
    CROP = (1920, 800, 0, 140)

    def session(self, *entries):
        mpv = FakeMpv()
        return playback.Session(mpv, queue_of(*entries), VIDEOS.get), mpv

    def test_a_video_measured_already_is_cropped_from_the_start(self):
        letterbox.remember("a", self.CROP)
        video = playback.plan_segment(queue_of(entry("a", 0, audio=False)), VIDEOS.get, 0)
        audio = playback.plan_segment(queue_of(entry("a", 0)), VIDEOS.get, 0)
        assert video.crop == self.CROP and audio.crop is None

    def test_measured_while_it_plays_its_cropped_then_and_handed_over_again(self):
        session, mpv = self.session(entry("a", 0, audio=False), entry("a", 2, audio=False))
        session.play()
        mpv.calls.clear()
        letterbox.remember("a", self.CROP)
        session.crop_measured("a")
        assert mpv.calls == [("crop", self.CROP), ("clear",), ("load", "a", (1,), True)]
        assert session.current().crop == session.upcoming().crop == self.CROP

    def test_another_videos_bars_change_nothing(self):
        session, mpv = self.session(entry("a", 0, audio=False))
        session.play()
        mpv.calls.clear()
        letterbox.remember("b", self.CROP)
        session.crop_measured("b")
        assert mpv.calls == []


class TestQueueEdits:
    def test_several_can_be_removed_at_once(self):
        queue = queue_of(*(entry("a", i % 3) for i in range(5)), start=3)
        queue.remove_many([0, 2, 4])
        assert len(queue) == 2 and queue.current_index() == 1

    def test_reordering_keeps_what_plays(self):
        queue = queue_of(entry("a", 0), entry("a", 1), entry("a", 2), start=1)
        queue.reorder([2, 0, 1])
        assert [e.chapter_index for e in queue.entries()] == [2, 0, 1]
        assert queue.current().chapter_index == 1


class TestWhatMpvIsTold:
    def test_a_file_and_a_blu_ray_title(self):
        queue = queue_of(entry("a", 1), entry("d", 0))
        piece = playback.plan_segment(queue, VIDEOS.get, 0)
        assert player_module.mpv_target(piece) == (
            "/v/a.mkv", {"start": "100.000", "end": "200.000", "vid": "no"}
        )
        disc = playback.plan_segment(queue_of(entry("d", 0, audio=False)), VIDEOS.get, 0)
        assert player_module.mpv_target(disc) == (
            "bd://3//disc", {"start": "0.000", "end": "none", "vid": "auto"}
        )

    def test_a_crop_as_this_mpv_takes_it(self, monkeypatch):
        letterbox.remember("a", (1920, 800, 0, 140))
        piece = playback.plan_segment(queue_of(entry("a", 0, audio=False)), VIDEOS.get, 0)
        monkeypatch.setattr(player_module, "_crops_at_output", lambda: True)
        assert player_module.mpv_target(piece)[1]["video-crop"] == "1920x800+0+140"
        monkeypatch.setattr(player_module, "_crops_at_output", lambda: False)
        assert player_module.mpv_target(piece)[1]["vf"] == "crop=1920:800:0:140"

    def test_a_crop_found_while_playing_is_for_that_file_alone(self, monkeypatch):
        sent = []
        p = player_module.Player()
        monkeypatch.setattr(p, "_call", lambda command, attempts=20: sent.append(command))
        monkeypatch.setattr(player_module, "_crops_at_output", lambda: False)
        p.set_crop((1920, 800, 0, 140))
        assert sent == [["set_property", "file-local-options/vf", "crop=1920:800:0:140"]]

    def test_loading_uses_named_arguments(self, monkeypatch):
        """mpv 0.38 put an index before loadfile's options; named arguments
        read the same on either side of that."""
        sent = []
        p = player_module.Player()
        monkeypatch.setattr(p, "_call", lambda command, attempts=20: sent.append(command) or
                            {"playlist_entry_id": 7})
        piece = playback.plan_segment(queue_of(entry("a", 0)), VIDEOS.get, 0)
        assert p.load(piece, append=True) == 7
        assert sent == [{"name": "loadfile", "url": "/v/a.mkv", "flags": "append",
                         "options": "start=0.000,end=100.000,vid=no"}]

    def test_an_embedded_session_has_no_controls_of_its_own(self, monkeypatch):
        runs = []

        class Proc:
            def __init__(self, args, env=None, **kwargs):
                runs.append(args)

            def poll(self):
                return None

        monkeypatch.setattr(player_module.subprocess, "Popen", Proc)
        player_module.Player().start_session(window_id=9)
        args = runs[0]
        for flag in ("--idle=yes", "--prefetch-playlist=yes", "--wid=9", "--osc=no",
                     "--input-default-bindings=no"):
            assert flag in args


class TestMovingToAnotherWindow:
    def test_a_new_mpv_carries_on_where_the_old_one_was(self):
        mpv = FakeMpv()
        queue = queue_of(entry("a", 0, audio=False), entry("a", 1, audio=False),
                         entry("b", 0, audio=False))
        session = playback.Session(mpv, queue, VIDEOS.get)
        session.play(window_id=99)
        mpv.state.update(position=142.0, paused=True)
        mpv.calls.clear()
        moved = session.move_to(None)
        assert moved.entries == (0, 1) and session.window_id() is None
        assert mpv.calls[0] == ("start", None)
        assert mpv.calls[1] == ("resume", "a", (0, 1), {"time-pos": 142.0, "pause": True})
        assert mpv.calls[2][:2] == ("load", "b"), "what comes next is lined up again"
        assert session.current().start == 0.0, "the piece still starts where it did"

    def test_nothing_playing_has_nothing_to_move(self):
        mpv = FakeMpv()
        session = playback.Session(mpv, queue_of(entry("a", 0)), VIDEOS.get)
        assert session.move_to(None) is None and mpv.calls == []

    def test_switching_to_video_carries_on_as_video_from_the_same_moment(self):
        mpv = FakeMpv()
        queue = queue_of(entry("a", 0), entry("a", 1), entry("b", 0))
        session = playback.Session(mpv, queue, VIDEOS.get)
        session.play()
        mpv.state["position"] = 130.0
        queue.jump_to(1)
        queue.set_audio_only(False)
        mpv.calls.clear()
        piece = session.switch(77)
        assert not piece.audio_only and piece.entries == (1,), "from the chapter playing"
        assert mpv.calls[0] == ("start", 77)
        assert mpv.calls[1][0] == "resume" and mpv.calls[1][3]["time-pos"] == 130.0

    def test_a_moment_past_where_the_piece_now_ends_starts_it_over(self):
        mpv = FakeMpv()
        queue = queue_of(entry("a", 1, audio=False))
        session = playback.Session(mpv, queue, VIDEOS.get)
        session.play()
        mpv.state["position"] = 260.0  # video ran on past chapter 2's end
        queue.set_audio_only(True)
        session.switch(None)
        assert "time-pos" not in mpv.calls[-1][3]
