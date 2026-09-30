# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""What the model is asked and how its answer is read. No network, no
ffmpeg: the frame grabber and the chat are stand-ins."""

import base64
import json

import pytest

from mediabrowser.core import ai, ai_chapters, frames


def chapters(starts, duration, titles=None):
    titles = titles or [None] * len(starts)
    ends = list(starts[1:]) + [duration]
    return [
        {"index": i, "start": s, "end": e, "title": t, "source": "auto-numbered"}
        for i, (s, e, t) in enumerate(zip(starts, ends, titles, strict=True))
    ]


def video(duration=1200.0, kind="file", path="/lib/BABYMETAL/Budokan/1. Doomsday.mkv"):
    return {
        "type": kind, "path": path, "display_name": "1. Doomsday",
        "duration": duration, "playlist": 3 if kind == "bluray" else None,
    }


def settings():
    return ai.settings_from({ai.SETTING_KEY: "k"})


def reply(entries, **extra):
    return json.dumps({"show": "BABYMETAL at Budokan", "chapters": entries, "notes": "", **extra})


class TestFramePlan:
    def test_each_chapter_gets_frames_just_after_its_start(self):
        s = ai_chapters.Situation(
            video(), chapters([0.0, 300.0, 600.0], 900.0), ai_chapters.NAME, frames_per_chapter=2
        )
        plan = ai_chapters.frame_plan(s)
        assert [t for t, _ in plan] == [3.0, 12.0, 303.0, 312.0, 603.0, 612.0]
        assert plan[2][1] == "chapter 2, 3s in"

    def test_nothing_past_a_chapters_end_or_the_video(self):
        s = ai_chapters.Situation(
            video(duration=310.0), chapters([0.0, 300.0], 310.0), ai_chapters.NAME,
            frames_per_chapter=4,
        )
        assert [t for t, _ in ai_chapters.frame_plan(s)] == [3.0, 12.0, 25.0, 45.0, 303.0]

    def test_placing_adds_one_frame_per_strong_unused_candidate(self):
        s = ai_chapters.Situation(
            video(), chapters([0.0, 600.0], 1200.0), ai_chapters.PLACE,
            candidates=[(598.0, 90.0), (300.0, 80.0), (900.0, 40.0)], frames_per_chapter=1,
        )
        plan = ai_chapters.frame_plan(s)
        assert [t for t, _ in plan] == [3.0, 303.0, 603.0, 903.0]
        assert plan[1][1] == "candidate at 5:00, 3s in"

    def test_context_is_the_folders_below_the_root(self):
        assert ai_chapters.situation_context(video(), "/lib") == "BABYMETAL / Budokan"
        assert ai_chapters.situation_context(video(), "/lib/BABYMETAL/Budokan") == ""


class TestTheRequest:
    def test_says_everything_it_knows_and_shows_the_frames(self):
        s = ai_chapters.Situation(
            video(), chapters([0.0, 300.0], 600.0, ["Opening", None]), ai_chapters.PLACE,
            tracks=[{"title": "Megitsune", "length": 306.0}], tracks_source="musicbrainz",
            candidates=[(300.0, 88.0)], context="BABYMETAL / Budokan", frames_per_chapter=1,
        )
        messages = ai_chapters.build_messages(s, {3.0: b"\xff\xd8one", 303.0: b"\xff\xd8two"})
        assert messages[0]["role"] == "system"
        parts = messages[1]["content"]
        text = "\n".join(p["text"] for p in parts if p["type"] == "text")
        assert "Folders: BABYMETAL / Budokan" in text
        assert "currently titled: 'Opening'" in text
        assert "Tracklist from MusicBrainz" in text and "Megitsune (5:06)" in text
        assert "5:00 (300s) score 88" in text
        assert "Frame at 0:03 (3s) (chapter 1, 3s in):" in text
        assert "romanisation" in text and '"chapter": 1' in text
        images = [p for p in parts if p["type"] == "image_url"]
        assert len(images) == 2
        assert base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1]) == b"\xff\xd8one"

    def test_naming_keeps_chapters_and_can_leave_titles_alone(self):
        s = ai_chapters.Situation(
            video(), chapters([0.0, 300.0], 600.0), ai_chapters.NAME, translate=False
        )
        text = "\n".join(
            p["text"] for p in ai_chapters.build_messages(s, {})[1]["content"]
            if p["type"] == "text"
        )
        assert "Keep them exactly where they are" in text
        assert "No frames could be taken" in text
        assert "leave original_title null" in text and "romanisation" not in text


class TestReadingTheAnswer:
    def test_naming_puts_titles_on_the_existing_chapters(self):
        s = ai_chapters.Situation(video(), chapters([0.0, 300.0, 600.0], 900.0), ai_chapters.NAME)
        result = ai_chapters.parse_reply(reply([
            {"chapter": 1, "title": "Opening", "confidence": "high"},
            {"chapter": "2", "title": "Megitsune", "original_title": "メギツネ",
             "confidence": "high", "note": "caption on screen"},
            {"chapter": 3, "title": "Gimme Chocolate!!", "original_title": "Gimme Chocolate!!",
             "confidence": "silly"},
            {"chapter": 9, "title": "nowhere"},
        ]), s, frames_sent=6, model="m")
        assert result.mode == ai_chapters.NAME
        assert [c["start"] for c in result.chapters] == [0.0, 300.0, 600.0]
        assert [c["title"] for c in result.chapters] == [
            "Opening", "Megitsune", "Gimme Chocolate!!"
        ]
        assert result.chapters[1]["original_title"] == "メギツネ"
        assert "original_title" not in result.chapters[2], "same as the title: not kept"
        assert all(c["source"] == "ai" for c in result.chapters)
        assert [r.confidence for r in result.rows] == ["high", "high", "medium"]
        assert result.rows[1].note == "caption on screen"
        assert result.mapping() == [
            (0, "Opening", None, "ai"), (1, "Megitsune", "メギツネ", "ai"),
            (2, "Gimme Chocolate!!", None, "ai"),
        ]
        assert result.show == "BABYMETAL at Budokan" and result.frames_sent == 6

    def test_a_chapter_the_model_skipped_is_shown_as_unnamed(self):
        s = ai_chapters.Situation(video(), chapters([0.0, 300.0], 600.0), ai_chapters.NAME)
        result = ai_chapters.parse_reply(reply([{"chapter": 2, "title": "Karate"}]), s)
        assert result.chapters[0]["title"] is None
        assert result.rows[0].note == "not named" and result.rows[0].confidence == "low"
        assert result.mapping() == [(1, "Karate", None, "ai")]

    def test_placing_snaps_to_known_moments_and_marks_what_moved(self):
        proposed = chapters([0.0, 310.0, 620.0], 1200.0)
        proposed[1]["estimated"] = True
        s = ai_chapters.Situation(
            video(), proposed, ai_chapters.PLACE, candidates=[(305.0, 80.0), (900.0, 70.0)]
        )
        result = ai_chapters.parse_reply(reply([
            {"start": 0, "title": "Opening"},
            {"start": "5:06", "title": "Megitsune", "confidence": "high"},  # 306 -> 305
            {"start": 620.4, "title": "Karate"},  # -> 620, as proposed
            {"start": 901, "title": "The One", "confidence": "medium"},  # -> 900
            {"start": 1150, "title": "Elsewhere"},  # no known moment near
        ]), s)
        assert [c["start"] for c in result.chapters] == [0.0, 305.0, 620.0, 900.0, 1150.0]
        assert [c["end"] for c in result.chapters][-1] == 1200.0
        assert [c.get("estimated", False) for c in result.chapters] == [
            False, True, False, True, True
        ]
        assert [r.moved for r in result.rows] == [False, True, False, True, True]
        assert result.moved_any
        assert result.chapters[1]["title"] == "Megitsune"
        assert result.chapters[4]["source"] == "ai"

    def test_placing_always_starts_at_zero_and_drops_doubles(self):
        s = ai_chapters.Situation(video(), chapters([0.0], 1200.0), ai_chapters.PLACE)
        result = ai_chapters.parse_reply(reply([
            {"start": 300, "title": "A"},
            {"start": 310, "title": "A again"},
            {"start": 5000, "title": "past the end"},
            {"start": -3, "title": "before it"},
        ]), s)
        assert [c["start"] for c in result.chapters] == [0.0, 300.0]
        assert result.chapters[0]["title"] is None
        assert result.rows[0].note == "added: the video starts here"

    def test_a_first_entry_just_after_zero_is_the_start(self):
        s = ai_chapters.Situation(video(), chapters([0.0], 1200.0), ai_chapters.PLACE)
        result = ai_chapters.parse_reply(reply([{"start": 4, "title": "Opening"}]), s)
        assert [c["start"] for c in result.chapters] == [0.0]
        assert result.chapters[0]["title"] == "Opening"

    def test_no_chapters_in_the_reply_is_an_error(self):
        s = ai_chapters.Situation(video(), chapters([0.0], 100.0), ai_chapters.NAME)
        with pytest.raises(ai.AIError):
            ai_chapters.parse_reply('{"show": "x"}', s)


class TestRunning:
    def test_grabs_asks_and_reads(self):
        s = ai_chapters.Situation(
            video(), chapters([0.0, 300.0], 600.0), ai_chapters.NAME, frames_per_chapter=1
        )
        seen = {}

        def grab(vid, times, progress_cb=None, cancel=None):
            seen["times"] = list(times)
            progress_cb(100)
            return {t: b"\xff\xd8" for t in times}

        def chat(messages, given_settings, **kwargs):
            seen["images"] = sum(
                1 for p in messages[1]["content"] if p["type"] == "image_url"
            )
            return reply([{"chapter": 1, "title": "A"}, {"chapter": 2, "title": "B"}])

        progress = []
        result = ai_chapters.run(s, settings(), progress_cb=progress.append, grab=grab, chat=chat)
        assert seen == {"times": [3.0, 303.0], "images": 2}
        assert [c["title"] for c in result.chapters] == ["A", "B"]
        assert result.frames_sent == 2 and result.model == ai.DEFAULT_MODEL + ":online/kagi"
        assert progress[-1] == 100 and progress[0] <= ai_chapters.FRAMES_PROGRESS

    def test_a_refused_search_falls_back_to_linkup_and_says_so(self):
        s = ai_chapters.Situation(video(), chapters([0.0], 600.0), ai_chapters.NAME,
                                  frames_per_chapter=0)
        asked = []

        def chat(messages, given, **kwargs):
            asked.append(given[ai.SETTING_SEARCH])
            if given[ai.SETTING_SEARCH] == "kagi":
                raise ai.SearchUnavailable("not compatible with Zero Data Retention")
            return reply([{"chapter": 1, "title": "A"}], notes="check chapter 1")

        result = ai_chapters.run(s, settings(), grab=lambda *a, **k: {}, chat=chat)
        assert asked == ["kagi", "linkup"]
        assert result.model.endswith(":online/linkup")
        assert result.notes.startswith("Kagi isn't allowed on this Nano-GPT account")
        assert result.notes.endswith("check chapter 1")
        assert [c["title"] for c in result.chapters] == ["A"]

    def test_cancelling_stops_before_asking(self):
        import threading

        s = ai_chapters.Situation(video(), chapters([0.0], 600.0), ai_chapters.NAME)
        cancel = threading.Event()

        def grab(vid, times, progress_cb=None, cancel=None):
            cancel.set()
            return {}

        with pytest.raises(frames.Cancelled):
            ai_chapters.run(
                s, settings(), cancel=cancel, grab=grab,
                chat=lambda *a, **k: pytest.fail("asked anyway"),
            )


class TestTranslatingTitles:
    def test_titles_come_back_in_order_with_originals(self):
        def chat(messages, given_settings, **kwargs):
            assert "1. メギツネ" in messages[1]["content"]
            return json.dumps({"titles": [
                {"n": 1, "title": "Megitsune", "original_title": "メギツネ"},
                {"n": 2, "title": "Karate"},
                {"n": 3, "title": "Road of Resistance", "original_title": None},
            ]})

        out = ai_chapters.translate_titles(
            ["メギツネ", "Karate", "ロード・オブ・レジスタンス"], settings(), chat=chat
        )
        assert out == [
            ("Megitsune", "メギツネ"),
            ("Karate", None),
            ("Road of Resistance", "ロード・オブ・レジスタンス"),
        ]

    def test_an_unanswered_title_is_left_alone(self):
        out = ai_chapters.translate_titles(
            ["A", "B"], settings(), chat=lambda *a, **k: '{"titles": [{"n": 2, "title": "Bee"}]}'
        )
        assert out == [("A", None), ("Bee", "B")]

    def test_nothing_to_translate_asks_nothing(self):
        assert ai_chapters.translate_titles(
            [], settings(), chat=lambda *a, **k: pytest.fail("asked")
        ) == []
