# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Making and fixing chapters from the detail view, driving the real window
offscreen. mpv is never started: where playback has got to is set directly,
which is all the window reads when splitting.
"""

import pytest

from mediabrowser.core import config, library, store

PySide6 = pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    from mediabrowser.gui.app import build_app

    existing = QApplication.instance()
    yield existing or build_app(["mediabrowser"])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    from mediabrowser.core import artwork
    from mediabrowser.gui.main_window import MainWindow

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LIBRARIES_DIR", tmp_path / "libraries")
    monkeypatch.setattr(config, "ARTWORK_DIR", tmp_path / "artwork")
    monkeypatch.setattr(config, "LEGACY_LIBRARY_FILE", tmp_path / "library.json")
    monkeypatch.setattr(config, "APP_SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(artwork, "is_resolved", lambda video_id: True)

    media = tmp_path / "media"
    media.mkdir()
    path = media / "Concert.mkv"
    path.write_bytes(b"")

    w = MainWindow()
    w.data = store.default_library(str(media))
    w.data["videos"] = {
        "v": {
            "type": "file",
            "path": str(path),
            "display_name": "Concert",
            "duration": 600.0,
            "chapters": [{
                "index": 0, "title": None, "start": 0.0, "end": 600.0,
                "source": "auto-numbered",
            }],
        },
    }
    w.refresh_library()
    w.open_video("v")
    yield w
    w.player.stop()
    w.close()


def _video(window):
    return window.data["videos"]["v"]


def _play_at(window, position, chapter_index=0):
    window.now_playing = ("v", chapter_index, False)
    window._last_position = position
    window.detail.set_playhead(window.playhead_in_open_video())


class TestSplitting:
    def test_split_is_offered_only_while_this_video_plays(self, window):
        assert not window.detail.split_button.isEnabled()
        _play_at(window, 125.0)
        assert window.detail.split_button.isEnabled()
        assert window.detail.split_button.text() == "Split at 2:05"

    def test_splitting_starts_a_chapter_at_the_playhead(self, window):
        _play_at(window, 125.0)
        window.split_at_playhead()
        assert [c["start"] for c in _video(window)["chapters"]] == [0.0, 125.0]
        assert _video(window)["chapter_origin"] == library.ORIGIN_EDITED

    def test_the_split_is_saved(self, window):
        _play_at(window, 125.0)
        window.split_at_playhead()
        stored = store.load_library_for_root(window.data["settings"]["library_root"])
        assert len(stored["videos"]["v"]["chapters"]) == 2

    def test_what_is_playing_keeps_pointing_at_the_same_music(self, window):
        _play_at(window, 125.0)
        window.split_at_playhead()
        _play_at(window, 300.0, chapter_index=1)
        window.split_at_playhead()
        # Playing chapter 1 (125s-600s) was split at 300s: still chapter 1.
        assert window.now_playing == ("v", 1, False)


class TestMergingAndNudging:
    @pytest.fixture
    def three(self, window):
        for position in (200.0, 400.0):
            _play_at(window, position)
            window.split_at_playhead()
        window.stop_playback()
        return window

    def test_merge_joins_a_chapter_with_the_next(self, three):
        three.merge_with_next(0)
        assert [(c["start"], c["end"]) for c in _video(three)["chapters"]] == [
            (0.0, 400.0), (400.0, 600.0)
        ]

    def test_nudging_moves_a_start_by_a_second(self, three):
        three.nudge_chapter_start(1, -1.0)
        chapters = _video(three)["chapters"]
        assert chapters[0]["end"] == chapters[1]["start"] == 199.0

    def test_the_buttons_follow_the_selection(self, three):
        tree = three.detail.tree
        tree.setCurrentItem(tree.topLevelItem(0))
        assert three.detail.merge_button.isEnabled()
        assert not three.detail.earlier_button.isEnabled(), "the first chapter can't move"
        tree.setCurrentItem(tree.topLevelItem(2))
        assert not three.detail.merge_button.isEnabled(), "nothing after the last"
        assert three.detail.later_button.isEnabled()


class TestMarkers:
    def test_estimated_starts_are_marked_in_the_table(self, window):
        from mediabrowser.core import chaptergen

        window.replace_chapters(
            chaptergen.chapters_from_starts([0, 300], 600, estimated=True),
            library.ORIGIN_ESTIMATED,
        )
        lengths = [window.detail.tree.topLevelItem(i).text(3) for i in range(2)]
        assert lengths == ["5:00", "~5:00"]
        assert "estimated from the audio" in window.detail.meta.text()

    def test_reset_is_offered_only_for_chapters_the_app_made(self, window):
        assert not window.detail.reset_action.isEnabled()
        _play_at(window, 125.0)
        window.split_at_playhead()
        assert window.detail.reset_action.isEnabled()
