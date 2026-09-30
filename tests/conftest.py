# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Qt must render offscreen here: the test suite has to pass on a machine
with no display, and in CI. Set before PySide6 is imported anywhere.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from mediabrowser.core import config, musicbrainz  # noqa: E402
from tests.test_chaptergen import concert  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_mpv(monkeypatch):
    """No test's window may start a real mpv: its player is a stand-in."""
    try:
        from mediabrowser.gui import main_window
    except ImportError:  # no PySide6
        return
    from tests.fake_mpv import FakeMpv

    monkeypatch.setattr(main_window, "Player", FakeMpv)


# --- shared GUI fixtures ----------------------------------------------------
# A window on a throwaway data directory, with MusicBrainz and the audio
# faked, for every test that drives a dialog.


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

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LIBRARIES_DIR", tmp_path / "data" / "libraries")
    monkeypatch.setattr(config, "ARTWORK_DIR", tmp_path / "data" / "artwork")
    monkeypatch.setattr(config, "LEGACY_LIBRARY_FILE", tmp_path / "data" / "library.json")
    monkeypatch.setattr(config, "APP_SETTINGS_FILE", tmp_path / "data" / "settings.json")
    monkeypatch.setattr(artwork, "is_resolved", lambda video_id: True)
    w = MainWindow()
    yield w
    w.player.stop()
    w.close()


@pytest.fixture
def measured(monkeypatch):
    """Audio for four 300s songs with 12s gaps, measured already; no lighting."""
    from mediabrowser.gui import audio_analysis

    levels, starts = concert([300, 300, 300, 300], gap=12.0)
    calls = {"levels": 0, "light": 0}

    def start(parent, video, cancel, *, on_progress, on_done, on_failed):
        calls["levels"] += 1
        on_done(levels)

    def start_light(parent, video, lv, cancel, *, on_progress, on_done, on_failed):
        calls["light"] += 1
        on_done({})

    monkeypatch.setattr(audio_analysis, "cached", lambda video: None)
    monkeypatch.setattr(audio_analysis, "start", start)
    monkeypatch.setattr(audio_analysis, "cached_light", lambda video, lv: None)
    monkeypatch.setattr(audio_analysis, "start_light", start_light)
    return levels, starts, calls


@pytest.fixture
def no_searching(monkeypatch):
    calls = []
    monkeypatch.setattr(
        musicbrainz, "search_releases", lambda query, **k: calls.append(query) or []
    )
    return calls


