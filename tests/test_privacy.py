# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings → Privacy: nothing goes out until it's allowed, wherever in
the app it would go from - and the first launch says where to allow it."""

import io
import urllib.request

import pytest

from mediabrowser.core import (
    ai,
    artwork,
    autoname,
    library,
    musicbrainz,
    privacy,
    store,
)
from tests.test_chapters_dialog import dialog_for, file_video


@pytest.fixture
def requests(monkeypatch):
    """Every request anything makes, answered with nothing useful."""
    sent = []

    def urlopen(req, timeout=None):
        sent.append(req.full_url)
        return io.BytesIO(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(musicbrainz, "_last_request_time", 0.0)
    monkeypatch.setattr(musicbrainz, "_MIN_INTERVAL", 0.0)
    return sent


def allow(*choices):
    settings = store.load_app_settings()
    for choice in choices:
        privacy.set_allowed(settings, choice, True)
    store.save_app_settings(settings)


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(ai.API_KEY_ENV, "test-key")


class TestNothingGoesOutUntilAllowed:
    def test_a_fresh_install_allows_nothing(self, shipped_privacy):
        assert not any(privacy.allowed(choice) for choice in privacy.CHOICES)

    def test_musicbrainz(self, shipped_privacy, requests):
        with pytest.raises(musicbrainz.MusicBrainzError, match="Settings → Privacy"):
            musicbrainz.search_releases("Babymetal Budokan", fmt="json")
        assert requests == []
        allow(privacy.MUSICBRAINZ)
        musicbrainz.search_releases("Babymetal Budokan", fmt="json")
        assert len(requests) == 1 and "musicbrainz.org" in requests[0]

    def test_cover_art(self, shipped_privacy, requests, tmp_path):
        target = tmp_path / "cover.jpg"
        assert not artwork._from_cover_art_archive("release-1", target)
        assert requests == []
        allow(privacy.COVER_ART)
        artwork._from_cover_art_archive("release-1", target)
        assert len(requests) == 1 and "coverartarchive.org" in requests[0]

    def test_the_ai(self, shipped_privacy, requests, keyed):
        settings = ai.load_settings()
        assert not ai.is_configured(settings)
        assert "Settings → Privacy" in ai.not_ready(settings)
        with pytest.raises(ai.NotConfigured, match="Settings → Privacy"):
            ai.ping(settings)
        with pytest.raises(ai.NotConfigured):
            ai.list_models(settings)
        assert requests == []
        allow(privacy.AI)
        assert ai.is_configured(ai.load_settings())

    def test_a_key_alone_is_not_enough(self, shipped_privacy, monkeypatch):
        monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
        allow(privacy.AI)
        assert ai.not_ready(ai.load_settings()) == "Set a Nano-GPT API key in Settings → AI."

    def test_the_web_search(self, shipped_privacy):
        allow(privacy.AI)
        settings = ai.load_settings()
        assert ai.model_name(settings, online=True) == ai.DEFAULT_MODEL
        # Nor when the model was typed with the search already on.
        typed = {**settings, ai.SETTING_MODEL: ai.DEFAULT_MODEL + ":online/kagi"}
        assert ai.model_name(typed, online=True) == ai.DEFAULT_MODEL
        allow(privacy.WEB_SEARCH)
        assert ai.model_name(ai.load_settings(), online=True) == (
            ai.DEFAULT_MODEL + ":online/kagi"
        )


class TestIdentify:
    def test_musicbrainz_isnt_chosen_while_it_isnt_allowed(self, shipped_privacy):
        from mediabrowser.gui.dialogs.identify_dialog import SETTINGS_KEY, saved_options

        store.save_app_settings({SETTINGS_KEY: {"methods": ["musicbrainz", "audio"]}})
        assert saved_options(False).methods == {autoname.AUDIO}
        allow(privacy.MUSICBRAINZ)
        assert saved_options(False).methods == {autoname.MUSICBRAINZ, autoname.AUDIO}

    def test_the_dialog_switches_it_off_and_keeps_it_wanted(self, window, shipped_privacy):
        from mediabrowser.gui.dialogs.identify_dialog import (
            SETTINGS_KEY,
            IdentifyDialog,
        )

        store.save_app_settings({SETTINGS_KEY: {"methods": ["musicbrainz", "audio"]}})
        dialog = IdentifyDialog(window)
        check = dialog.method_checks[autoname.MUSICBRAINZ]
        assert not check.isEnabled() and "Settings → Privacy" in check.toolTip()
        assert dialog.options().methods == {autoname.AUDIO}
        dialog._save_options()
        assert "musicbrainz" in store.load_app_settings()[SETTINGS_KEY]["methods"]


class TestTheWindows:
    def test_settings_saves_the_ticks(self, window, shipped_privacy):
        from mediabrowser.gui.dialogs.settings_dialog import SettingsDialog

        dialog = SettingsDialog(window)
        assert dialog.windowTitle() == "Settings"
        web = dialog.privacy_checks[privacy.WEB_SEARCH]
        assert not web.isEnabled(), "only the AI searches, so it's allowed first"
        dialog.privacy_checks[privacy.MUSICBRAINZ].setChecked(True)
        dialog.privacy_checks[privacy.AI].setChecked(True)
        assert web.isEnabled()
        dialog.accept()
        assert privacy.allowed(privacy.MUSICBRAINZ) and privacy.allowed(privacy.AI)
        assert not privacy.allowed(privacy.COVER_ART)
        assert not privacy.allowed(privacy.WEB_SEARCH)

    def test_settings_wont_test_an_ai_that_isnt_allowed(self, window, shipped_privacy,
                                                         requests, keyed):
        from mediabrowser.gui.dialogs.settings_dialog import TAB_AI, SettingsDialog

        dialog = SettingsDialog(window, TAB_AI)
        assert dialog.tabs.currentIndex() == TAB_AI
        dialog.test()
        dialog.fetch_models()
        assert "Privacy tab" in dialog.status.text()
        assert requests == []

    def test_the_first_launch_says_where_to_start(self, window):
        assert window.pages.currentWidget().isAncestorOf(window.welcome)
        assert not window.welcome.isHidden() and window.empty_label.isHidden()

    def test_an_empty_library_says_so_instead(self, window, tmp_path):
        window.data = store.default_library(str(tmp_path / "media"))
        window.refresh_library()
        assert window.welcome.isHidden() and not window.empty_label.isHidden()

    def test_chapters_cant_search_musicbrainz(self, window, shipped_privacy, tmp_path):
        from mediabrowser.gui.dialogs.chapters_dialog import MUSICBRAINZ_OFF_NOTE

        dialog = dialog_for(window, file_video(tmp_path, 900.0))
        assert not dialog.search_button.isEnabled()
        assert dialog.mb_status.text() == MUSICBRAINZ_OFF_NOTE
        assert not dialog.ai_online_check.isEnabled()

    def test_allowing_cover_art_fetches_the_sleeves(self, window, shipped_privacy,
                                                    tmp_path, monkeypatch):
        from mediabrowser.gui.dialogs import settings_dialog

        matched = file_video(tmp_path, 900.0)
        matched["musicbrainz_release_id"] = "release-1"
        unmatched = file_video(tmp_path, 600.0)
        window.data = store.default_library(str(tmp_path / "media"))
        window.data["videos"] = {"matched": matched, "unmatched": unmatched}
        for video in window.data["videos"].values():
            video["media_type"] = library.MEDIA_CHAPTERED
        forgotten = []
        monkeypatch.setattr(artwork, "forget", forgotten.append)

        class Allows:
            def __init__(self, parent, tab):
                pass

            def exec(self):
                allow(privacy.COVER_ART)
                return True

        monkeypatch.setattr(settings_dialog, "SettingsDialog", Allows)
        window.open_settings()
        assert forgotten == ["matched"]
        window.open_settings()  # allowed already: nothing more to fetch
        assert forgotten == ["matched"]
