# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The Detect Chapters dialog: one tracklist, from wherever, used the best
way there is. Driven offscreen; MusicBrainz and the audio are faked.
"""

import time

import pytest

from mediabrowser.core import library, methods, store

PySide6 = pytest.importorskip("PySide6")


def file_video(tmp_path, duration, starts=(0.0,), origin=None, kind="file"):
    folder = tmp_path / "media" / "BABYMETAL" / "10-BABYMETAL-BUDOKAN_THE-ONE-EDITION"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "1. Doomsday I, II.mkv"
    path.write_bytes(b"")
    ends = list(starts[1:]) + [duration]
    video = {
        "type": kind, "path": str(path), "display_name": "1. Doomsday I, II",
        "duration": duration,
        "chapters": [
            {"index": i, "title": None, "start": s, "end": e, "source": "auto-numbered"}
            for i, (s, e) in enumerate(zip(starts, ends, strict=True))
        ],
    }
    if origin:
        video["chapter_origin"] = origin
    return video


def dialog_for(window, video, root=None):
    from mediabrowser.gui.dialogs.chapters_dialog import ChaptersDialog

    return ChaptersDialog(window, video, root)


def no_tracklist(dialog):
    """Nothing searched for, nothing pasted: the Tracklist page as it opens."""
    from mediabrowser.gui.dialogs.chapters_dialog import TAB_TRACKLIST

    dialog.tabs.setCurrentIndex(TAB_TRACKLIST)
    return dialog


def paste(dialog, text):
    from mediabrowser.gui.dialogs.chapters_dialog import TAB_TRACKLIST

    dialog.tabs.setCurrentIndex(TAB_TRACKLIST)
    dialog.paste_toggle.setChecked(True)
    dialog.paste.setPlainText(text)
    dialog._on_paste_changed()


def pump(app, until, timeout=5.0):
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()  # what the job sent as it ended
    return until()


def _finished(thread) -> bool:
    return not thread.is_alive()


class TestMusicBrainzWaitsToBeAsked:
    """MusicBrainz rate-limits hard: nothing is sent until asked for."""

    def test_opening_searches_nothing(self, app, window, tmp_path, measured, no_searching):
        dialog = dialog_for(window, file_video(tmp_path, 1212.0), str(tmp_path / "media"))
        pump(app, lambda: False, timeout=0.2)
        assert no_searching == []
        assert dialog.query.text() == "10 BABYMETAL BUDOKAN THE ONE EDITION Doomsday I, II"

    def test_search_sends_what_was_typed(self, app, window, tmp_path, measured, no_searching):
        dialog = dialog_for(window, file_video(tmp_path, 1212.0))
        dialog.query.setText("10 BABYMETAL BUDOKAN")
        dialog.search_button.click()
        thread = dialog._jobs[-1][0]
        assert pump(app, lambda: no_searching == ["10 BABYMETAL BUDOKAN"])
        assert pump(app, lambda: _finished(thread))


class TestChoosingTheMethod:
    def test_with_nothing_yet_it_detects_and_a_tracklist_takes_over(
        self, window, tmp_path, measured, no_searching
    ):
        """One page, easiest first: a file in one piece is detected from the
        audio as the dialog opens, and a tracklist pasted later replaces
        that with the better way."""
        levels, _, calls = measured
        dialog = dialog_for(window, file_video(tmp_path, levels.duration))
        assert dialog._method == methods.DETECT and dialog.result()[0] == "replace"
        assert calls["levels"] == 1
        paste(dialog, "Intro 5:00\nSong 5:12\nOther 5:00\nLast 5:00")
        assert dialog._method == methods.LENGTHS

    def test_a_chapterless_file_with_nothing_else_is_detected(
        self, window, tmp_path, measured, no_searching
    ):
        levels, starts, calls = measured
        dialog = no_tracklist(dialog_for(window, file_video(tmp_path, levels.duration)))
        assert dialog._method == methods.DETECT
        assert calls["levels"] == 1
        kind, (chapters, origin) = dialog.result()
        assert kind == "replace" and origin == library.ORIGIN_ESTIMATED
        assert len(chapters) == 4

    def test_pasted_lengths_that_are_the_whole_video_place_the_chapters(
        self, window, tmp_path, measured, no_searching
    ):
        levels, starts, _ = measured
        dialog = dialog_for(window, file_video(tmp_path, levels.duration))
        paste(dialog, "Megitsune 5:06\nKarate 5:12\nStarlight 5:12\nThe One 5:00")
        assert dialog._method == methods.LENGTHS
        kind, (chapters, origin) = dialog.result()
        assert origin == library.ORIGIN_TRACKLIST
        assert [c["title"] for c in chapters] == ["Megitsune", "Karate", "Starlight", "The One"]
        assert [c["start"] for c in chapters] == [0.0, 306.0, 618.0, 930.0]

    def test_pasted_titles_tell_detection_how_many_and_what(
        self, window, tmp_path, measured, no_searching
    ):
        levels, _, _ = measured
        dialog = dialog_for(window, file_video(tmp_path, levels.duration))
        paste(dialog, "Megitsune\nKarate\nStarlight")
        assert dialog._method == methods.DETECT
        assert dialog.count.value() == 3 and not dialog.count.isEnabled()
        kind, (chapters, _origin) = dialog.result()
        assert [c["title"] for c in chapters] == ["Megitsune", "Karate", "Starlight"]

    def test_a_discs_own_chapters_are_named(self, window, tmp_path, measured, no_searching):
        video = file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0))
        dialog = dialog_for(window, video)
        paste(dialog, "One 5:00\nTwo 5:00\nThree 5:00")
        assert dialog._method == methods.NAME
        assert dialog.result() == ("name", [(0, "One"), (1, "Two"), (2, "Three")])

    def test_a_method_picked_by_hand_is_kept(self, window, tmp_path, measured, no_searching):
        levels, _, _ = measured
        dialog = dialog_for(window, file_video(tmp_path, levels.duration))
        paste(dialog, "Megitsune 5:06\nKarate 5:12\nStarlight 5:12\nThe One 5:00")
        dialog.method_radios[methods.DETECT].click()
        paste(dialog, "Megitsune 5:06\nKarate 5:12\nStarlight 5:12\nThe One 5:01")
        assert dialog._method == methods.DETECT

    def test_a_blu_ray_title_is_never_measured(self, window, tmp_path, measured, no_searching):
        levels, _, calls = measured
        dialog = dialog_for(window, file_video(tmp_path, 600.0, kind="bluray"))
        assert not dialog.method_radios[methods.DETECT].isEnabled()
        paste(dialog, "One 5:00\nTwo 5:00")
        assert dialog._method == methods.LENGTHS
        assert calls["levels"] == 0


class TestTheBoxSet:
    """Two shows of near-identical songs, the second one this video."""

    MEDIA = [
        {"title": "Night one", "format": "CD", "tracks": [
            {"title": "Intro", "length": 600.0}, {"title": "Distortion", "length": 254.0},
            {"title": "Gimme Chocolate!!", "length": 256.0}]},
        {"title": "Night one", "format": "Blu-ray", "tracks": [
            {"title": "Intro", "length": None}]},
        {"title": "Night two", "format": "CD", "tracks": [
            {"title": "Intro", "length": 690.0}, {"title": "Gimme Chocolate!!", "length": 255.0},
            {"title": "Syncopation", "length": 320.0}]},
    ]

    def test_only_this_shows_discs_are_ticked(self, window, tmp_path, measured, no_searching):
        dialog = dialog_for(window, file_video(tmp_path, 1265.0, starts=(0.0, 690.0, 945.0)))
        dialog.show_media(self.MEDIA)
        assert not dialog.media_list.isHidden()
        night_two = ["Intro", "Gimme Chocolate!!", "Syncopation"]
        assert [t["title"] for t in dialog.tracks()] == night_two
        assert dialog._method == methods.NAME
        assert [t for _, t in dialog.result()[1]] == night_two

    def test_an_estimate_is_replaced_from_the_lengths(
        self, window, tmp_path, measured, no_searching
    ):
        video = file_video(
            tmp_path, 1265.0, starts=(0.0, 700.0, 950.0), origin=library.ORIGIN_ESTIMATED
        )
        dialog = dialog_for(window, video)
        dialog.show_media(self.MEDIA)
        assert dialog._method == methods.LENGTHS
        chapters, origin = dialog.result()[1]
        assert [c["start"] for c in chapters][:3] == [0.0, 690.0, 945.0]

    def test_applying_keeps_the_release_for_cover_art(
        self, window, tmp_path, measured, no_searching
    ):
        video = file_video(tmp_path, 1265.0, starts=(0.0, 690.0, 945.0))
        window.data = store.default_library(str(tmp_path / "media"))
        window.data["videos"] = {"v": video}
        window.refresh_library()
        window.open_video("v")
        dialog = dialog_for(window, video)
        dialog._release_id = "mbid-box-set"
        dialog.show_media(self.MEDIA)
        window.apply_chapters_result(dialog)
        assert video["musicbrainz_release_id"] == "mbid-box-set"
        assert [c["title"] for c in video["chapters"]] == [
            "Intro", "Gimme Chocolate!!", "Syncopation"
        ]
        assert all(c["source"] == "musicbrainz" for c in video["chapters"])


class TestTheLighting:
    def test_detection_checks_the_lighting_by_default(
        self, window, tmp_path, measured, no_searching
    ):
        levels, _, calls = measured
        dialog = no_tracklist(dialog_for(window, file_video(tmp_path, levels.duration)))
        assert dialog.light_check.isChecked() and calls["light"] == 1

    def test_unticking_estimates_from_the_sound_alone(
        self, window, tmp_path, measured, no_searching
    ):
        levels, _, calls = measured
        dialog = no_tracklist(dialog_for(window, file_video(tmp_path, levels.duration)))
        dialog.light_check.setChecked(False)
        assert dialog.status.text().startswith("4 chapter(s) estimated from the sound.")
        assert calls["light"] == 1
