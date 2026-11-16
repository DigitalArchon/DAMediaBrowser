# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Qt must render offscreen here: the test suite has to pass on a machine
with no display, and in CI. Set before PySide6 is imported anywhere.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import keyring  # noqa: E402
import pytest  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402
from keyring.errors import PasswordDeleteError  # noqa: E402

from mediabrowser.core import config, musicbrainz  # noqa: E402
from tests.test_chaptergen import concert  # noqa: E402


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            raise PasswordDeleteError(username)
        del self.store[(service, username)]


@pytest.fixture(scope="session", autouse=True)
def _qts_settings_of_their_own(tmp_path_factory):
    """A QFileDialog keeps its folders in Qt's own settings (QtProject.conf) as it is
    deleted, the widgets' dialog and GTK's alike: the run's go to a folder of its own,
    never into the settings of whoever runs it."""
    from PySide6.QtCore import QSettings

    folder = str(tmp_path_factory.mktemp("qt-settings"))
    for kind in (QSettings.Format.NativeFormat, QSettings.Format.IniFormat):
        QSettings.setPath(kind, QSettings.Scope.UserScope, folder)
    yield


@pytest.fixture(autouse=True)
def _unanswered_file_dialogs_fail(monkeypatch):
    """Every file dialog of the app is run by gui/file_dialogs.py; one no test answers
    (by patching file_dialogs.save_file_name and the rest) fails at once, naming it,
    rather than waiting forever offscreen."""
    from PySide6.QtWidgets import QFileDialog

    def exec_(dialog):
        raise AssertionError(f"unanswered file dialog: {dialog.windowTitle()!r}")

    monkeypatch.setattr(QFileDialog, "exec", exec_)


@pytest.fixture(autouse=True)
def memory_keyring():
    """No test may read or write the real keyring."""
    backend = MemoryKeyring()
    old = keyring.get_keyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(old)


@pytest.fixture(autouse=True)
def own_settings(tmp_path, monkeypatch):
    """No test may read or write the real settings.json."""
    monkeypatch.setattr(config, "APP_SETTINGS_FILE", tmp_path / "settings.json")


@pytest.fixture(autouse=True)
def network_through_urlopen(monkeypatch):
    """netguard's transport is the one place a request leaves the app. The
    tests fake the network at urllib.request.urlopen, as they always have,
    so here the transport goes by way of it - looked up as each request is
    made, and only after every check netguard makes. Its own tests put the
    real transport back (tests/test_netguard.py)."""
    import urllib.request

    from mediabrowser.core import netguard

    monkeypatch.setattr(
        netguard, "_transport",
        lambda request, timeout: urllib.request.urlopen(request, timeout=timeout))


@pytest.fixture(autouse=True)
def no_network_but_this_machine(monkeypatch):
    """A request that reaches urllib's urlopen with no test standing in for it fails the
    test, unless it is to this computer (the local servers some tests run): a model list
    fetched by the way - Tinfoil's or Chutes' public one, read to see whether a model can
    look at pictures - would otherwise go to the real service. Tests that fake the
    network replace this as they always have."""
    import urllib.request
    from urllib.parse import urlparse

    real = urllib.request.urlopen

    def urlopen(request, *args, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        host = (urlparse(url).hostname or "").lower()
        if host in ("localhost", "::1") or host.startswith("127."):
            return real(request, *args, **kwargs)
        raise AssertionError(f"a test reached the network: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


@pytest.fixture(autouse=True)
def fresh_provider_caches(monkeypatch):
    """The providers' public lists and Chutes' attestations are kept a while for the
    process (ai._listings, ai._chutes_state): each test starts with none."""
    from mediabrowser.core import ai

    monkeypatch.setattr(ai, "_listings", {})
    monkeypatch.setattr(ai, "_chutes_shared", None)


@pytest.fixture(autouse=True)
def forget_what_was_sent():
    """The list of what was sent is the process's: each test starts with it
    empty."""
    from mediabrowser.core import outbound

    outbound.clear()
    yield
    outbound.clear()


@pytest.fixture(autouse=True)
def everything_allowed(monkeypatch):
    """The network is faked wherever a test reaches for it, so the tests
    are written as if Settings → Privacy allowed everything. The ones about
    privacy itself start from what ships instead (`shipped_privacy`)."""
    from mediabrowser.core import privacy

    monkeypatch.setattr(privacy, "DEFAULTS", dict.fromkeys(privacy.CHOICES, True))


@pytest.fixture
def shipped_privacy(monkeypatch):
    """Settings → Privacy as a fresh install has it: nothing allowed."""
    from mediabrowser.core import privacy

    monkeypatch.setattr(privacy, "DEFAULTS", dict.fromkeys(privacy.CHOICES, False))


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
    w.editor._dirty = False  # closing asks about edits left unsaved
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




@pytest.fixture(autouse=True)
def no_desktop_program_starts(monkeypatch):
    """A link or a folder is opened with the desktop's own program, started by
    core/hostenv._start: never from a test, which would open the real browser."""
    from mediabrowser.core import hostenv

    def refused(argv, env):
        raise AssertionError(f"a test would have started {argv[0]}")

    monkeypatch.setattr(hostenv, "_start", refused)


@pytest.fixture(autouse=True)
def _unanswered_questions_fail(monkeypatch):
    """gui/plain.question opens a modal box, which nothing answers offscreen: a test that
    asks one without answering it fails at once, naming it, rather than hang the run.
    A test that answers it patches plain.question itself (test_ai_gui's `asked`)."""
    from mediabrowser.gui import plain

    def unanswered(_parent, title, text, *_args, **_kwargs):
        raise AssertionError(f"a question nobody answers: {title}: {text[:120]}")

    monkeypatch.setattr(plain, "question", unanswered)
