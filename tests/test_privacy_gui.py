# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Where things go, as the window says it: Privacy Mode in Settings, the
line under Ask AI, the status bar, and Help → What Was Sent."""

import threading

import pytest

from mediabrowser.core import ai, outbound, privacy, sealed, store
from tests.test_chapters_dialog import dialog_for, file_video


@pytest.fixture(autouse=True)
def client_known(monkeypatch):
    """Tinfoil's client, as Settings and the window ask after it: there."""
    monkeypatch.setattr(sealed, "_known", (True, "tinfoil"))


def save(**values):
    settings = store.load_app_settings()
    settings.update(values)
    store.save_app_settings(settings)


def settings_dialog(window):
    from mediabrowser.gui.dialogs.settings_dialog import TAB_PRIVACY, SettingsDialog

    return SettingsDialog(window, TAB_PRIVACY)


class TestSettings:
    def test_privacy_mode_is_kept_with_its_model_and_strictness(self, window):
        save(**{privacy.AI: True})
        dialog = settings_dialog(window)
        assert not dialog.privacy_mode_check.isChecked(), "off until it's ticked"
        dialog.privacy_mode_check.setChecked(True)
        dialog.private_model.setCurrentText("private/deepseek-v4-1-flash")
        dialog.strict_check.setChecked(True)
        dialog.accept()
        saved = store.load_app_settings()
        assert saved[ai.SETTING_PRIVACY_MODE] is True and saved[ai.SETTING_STRICT] is True
        assert saved[ai.SETTING_PRIVATE_MODEL] == "private/deepseek-v4-1-flash"
        assert ai.active_model(ai.load_settings()) == "private/deepseek-v4-1-flash"

    def test_a_model_in_no_enclave_isnt_saved_as_the_private_one(self, window):
        save(**{privacy.AI: True})
        dialog = settings_dialog(window)
        dialog.privacy_mode_check.setChecked(True)
        dialog.private_model.setCurrentText("anthropic/claude-sonnet-5")
        dialog.accept()
        assert "runs in no attested enclave" in dialog.attest_status.text()
        assert not store.load_app_settings().get(ai.SETTING_PRIVACY_MODE)

    def test_the_line_under_the_ai_says_where_it_goes_as_the_form_has_it(self, window):
        save(**{privacy.AI: True, privacy.WEB_SEARCH: True})
        dialog = settings_dialog(window)
        assert "through Nano-GPT" in dialog.where_label.text()
        assert "both can read it" in dialog.where_label.text()
        assert "search about the show" in dialog.where_label.text()
        dialog.privacy_mode_check.setChecked(True)
        assert "sealed end to end" in dialog.where_label.text()
        assert dialog.where_label.property("tone") == "ok"
        search = dialog.privacy_checks[privacy.WEB_SEARCH]
        assert not search.isEnabled(), "no web search in Privacy Mode"
        assert "Not in Privacy Mode" in dialog.web_search_note.text()
        dialog.private_model.setCurrentText("TEE/kimi-k3")
        assert "gateway can read" in dialog.where_label.text()
        assert dialog.where_label.property("tone") == "warning"
        assert not dialog.ai_privacy_note.isHidden(), "the AI tab says so too"
        dialog.privacy_checks[privacy.AI].setChecked(False)
        assert dialog.where_label.text() == "Nothing is sent to the AI."
        assert not dialog.privacy_mode_check.isEnabled()

    def test_without_tinfoils_client_the_model_line_says_how_to_get_it(self, window,
                                                                      monkeypatch):
        monkeypatch.setattr(sealed, "_known", (False, sealed.INSTALL_HINT))
        save(**{privacy.AI: True, ai.SETTING_PRIVACY_MODE: True})
        dialog = settings_dialog(window)
        assert "install-private-mode.sh" in dialog.private_model_note.text()
        assert dialog.private_model_note.property("tone") == "error"


class TestTheWindow:
    def test_the_status_bar_says_where_things_can_go(self, app, window, monkeypatch):
        from PySide6.QtWidgets import QApplication

        from mediabrowser.core import setlistfm
        from mediabrowser.gui import network_status

        monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
        monkeypatch.delenv(setlistfm.API_KEY_ENV, raising=False)
        line = window.network_status
        save(**dict.fromkeys(privacy.CHOICES, False))
        network_status.settings_changed()
        assert line.text() == "Nothing goes online"
        assert line.property("tone") == "off"
        assert "✗ MusicBrainz: off." in line.toolTip()
        save(**{privacy.MUSICBRAINZ: True, privacy.COVER_ART: True, privacy.AI: True,
                privacy.SETLISTFM: True, privacy.WEB_SEARCH: True,
                ai.SETTING_PRIVACY_MODE: True})
        monkeypatch.setenv(ai.API_KEY_ENV, "sk-1")
        network_status.settings_changed()
        # No setlist.fm key, and no web search in Privacy Mode: neither is listed.
        assert line.text() == "Online: MusicBrainz · Covers · AI, sealed end to end"
        tip = line.toolTip()
        assert "✓ MusicBrainz: to musicbrainz.org" in tip
        assert "✗ setlist.fm: off. Add a setlist.fm API key" in tip
        assert "only ciphertext" in tip and "Never in Privacy Mode" in tip
        assert "Nothing sent since the app started" in tip

        # A cover fetched in the background, from its own thread, shows as it goes.
        def fetched():
            outbound.record("Cover Art Archive", "https://coverartarchive.org/release/x",
                            "the cover of a release", outbound.CLEAR, "Over https")
        thread = threading.Thread(target=fetched)
        thread.start()
        thread.join()
        QApplication.processEvents()
        assert line.text() == "Sent to coverartarchive.org"
        assert "1 request sent since the app started" in line.toolTip()
        line._settle()
        assert line.text().startswith("Online: ")
        outbound.record("MusicBrainz", "https://musicbrainz.org/ws/2", "a search",
                        outbound.CLEAR, "Over https", outbound.REFUSED, "no")
        QApplication.processEvents()
        assert line.text() == "Refused, nothing sent: musicbrainz.org"
        line.click()
        assert window._sent_dialog.isVisible()
        window._sent_dialog.close()

    def test_settings_saved_anywhere_reach_the_status_bar(self, window, monkeypatch):
        monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
        save(**dict.fromkeys(privacy.CHOICES, False))
        dialog = settings_dialog(window)
        dialog.privacy_checks[privacy.MUSICBRAINZ].setChecked(True)
        dialog.accept()
        assert window.network_status.text() == "Online: MusicBrainz"

    def test_what_was_sent_lists_every_request_as_it_goes(self, window):
        from mediabrowser.gui.dialogs.sent_dialog import EMPTY

        window.show_what_was_sent()
        dialog = window._sent_dialog
        assert dialog.summary.text() == EMPTY
        with outbound.purpose("Detect Chapters: Glass Harbor - Live at Copperfield Hall"):
            outbound.record("MusicBrainz", "https://musicbrainz.org/ws/2/release",
                            "a release search: Glass Harbor", outbound.CLEAR, "Over https")

        # From another thread, as a job's request is.
        def sealed_one():
            outbound.record("Nano-GPT", "https://nano-gpt.com/api/v1", "2 frames and 900 "
                            "characters of text, for private/kimi-k3", outbound.SEALED,
                            "Sent it sealed end to end", content="Paper Lanterns?")
        thread = threading.Thread(target=sealed_one)
        thread.start()
        thread.join()
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
        assert dialog.tree.topLevelItemCount() == 2
        assert dialog.summary.text() == "2 requests: 1 sealed end to end, 1 over https."
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(1))
        shown = dialog.details.toPlainText()
        assert "Paper Lanterns?" in shown and "sealed end to end" in shown
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0))
        assert "For: Detect Chapters: Glass Harbor" in dialog.details.toPlainText()
        dialog.copy()
        assert "Paper Lanterns?" in QApplication.clipboard().text()
        # Told of late, across threads: one already listed isn't listed twice,
        # and one from before Clear doesn't come back after it.
        late = outbound.entries()[0]
        dialog._arrived(late)
        assert dialog.tree.topLevelItemCount() == 2
        dialog.clear()
        dialog._arrived(late)
        assert dialog.tree.topLevelItemCount() == 0 and outbound.entries() == []
        assert "✓ MusicBrainz: to musicbrainz.org" in dialog.allowed.text()
        dialog.close()
        outbound.record("MusicBrainz", "https://musicbrainz.org/", "after it was closed",
                        outbound.CLEAR, "Over https")
        window.show_what_was_sent()
        assert window._sent_dialog is dialog and dialog.tree.topLevelItemCount() == 1


class TestAskAi:
    def test_the_line_says_where_the_request_goes_and_who_can_read_it(self, window,
                                                                      tmp_path, monkeypatch):
        monkeypatch.setenv(ai.API_KEY_ENV, "sk-1")
        save(**{privacy.AI: True})
        dialog = dialog_for(window, file_video(tmp_path, 600.0, starts=(0.0, 200.0, 400.0)))
        hint = dialog.ai_hint.text()
        assert "through Nano-GPT" in hint and "both can read it" in hint
        assert "file and folder names" in hint

    def test_in_privacy_mode_it_is_sealed_and_never_searches(self, window, tmp_path,
                                                             monkeypatch):
        monkeypatch.setenv(ai.API_KEY_ENV, "sk-1")
        save(**{privacy.AI: True, privacy.WEB_SEARCH: True, ai.SETTING_PRIVACY_MODE: True})
        dialog = dialog_for(window, file_video(tmp_path, 600.0, starts=(0.0, 200.0, 400.0)))
        hint = dialog.ai_hint.text()
        assert "sealed end to end to kimi-k3's enclave" in hint
        assert not dialog.ai_online_check.isEnabled()
        assert not dialog.ai_online_check.isChecked()
        assert "Not in Privacy Mode" in dialog.ai_online_check.toolTip()


class TestSettingsButtonsSayWhere:
    def test_list_test_and_attest_name_where_they_go(self, window, monkeypatch):
        monkeypatch.setenv(ai.API_KEY_ENV, "sk-1")
        save(**{privacy.AI: True})
        dialog = settings_dialog(window)
        assert "nano-gpt.com" in dialog.fetch_button.toolTip()
        assert "API key goes with it" in dialog.fetch_button.toolTip()
        dialog.base_url.setText("https://llm.example.net/v1")
        assert "llm.example.net" in dialog.fetch_button.toolTip()
        assert "at llm.example.net, which can read it" in dialog.test_button.toolTip()
        dialog.base_url.setText(ai.DEFAULT_BASE_URL)
        dialog.privacy_mode_check.setChecked(True)
        assert "sealed end to end" in dialog.test_button.toolTip()
        assert "Sigstore" in dialog.attest_button.toolTip()
        dialog.private_model.setCurrentText("TEE/kimi-k3")
        assert "NVIDIA" in dialog.attest_button.toolTip()

    def test_the_web_search_note_names_who_searches(self, window):
        save(**{privacy.AI: True})
        dialog = settings_dialog(window)
        dialog.search.setCurrentIndex(list(ai.SEARCH_PROVIDERS).index("perplexity"))
        assert "Perplexity" in dialog.web_search_note.text()


class TestLinks:
    def test_a_link_says_where_it_goes_and_an_insecure_one_isnt_opened(self, window,
                                                                       monkeypatch):
        from PySide6.QtWidgets import QLabel

        from mediabrowser.gui import links

        opened, warned = [], []
        monkeypatch.setattr(links.QDesktopServices, "openUrl",
                            lambda url: opened.append(url.toString()) or True)
        monkeypatch.setattr(links.QMessageBox, "warning", lambda *a: warned.append(a))
        label = QLabel('<a href="https://www.setlist.fm/x">setlist.fm</a>')
        label.setToolTip("resting")
        links.guard(label)
        assert not label.openExternalLinks()
        label.linkHovered.emit("https://www.setlist.fm/x")
        assert label.toolTip() == "Opens https://www.setlist.fm/x in your web browser"
        label.linkHovered.emit("")
        assert label.toolTip() == "resting"
        label.linkActivated.emit("https://www.setlist.fm/x")
        assert opened == ["https://www.setlist.fm/x"]
        label.linkActivated.emit("http://www.setlist.fm/x")
        assert opened == ["https://www.setlist.fm/x"], "plain http isn't opened"
        assert "plain http" in warned[0][2]
        assert links.problem("http://192.168.1.5/") is None, "this computer's network"

    def test_every_label_with_a_link_is_guarded(self):
        """A label that opens links by itself would open one unannounced."""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "mediabrowser"
        found = [str(path) for path in root.rglob("*.py")
                 if "setOpenExternalLinks(True)" in path.read_text(encoding="utf-8")]
        assert not found


class TestDetectChaptersSaysWhere:
    def test_just_figure_it_out_names_only_what_it_will_ask(self, window, tmp_path,
                                                             monkeypatch):
        from mediabrowser.core import setlistfm

        monkeypatch.setenv(ai.API_KEY_ENV, "sk-1")
        monkeypatch.setenv(setlistfm.API_KEY_ENV, "fm-1")
        video = file_video(tmp_path, 600.0, starts=(0.0, 200.0, 400.0))
        note = dialog_for(window, video).figure_note.text()
        assert "looks the show up on MusicBrainz and setlist.fm" in note
        assert "Where it goes: the video's name, to musicbrainz.org and setlist.fm" in note
        assert "the release, to coverartarchive.org" in note
        save(**{privacy.MUSICBRAINZ: False})
        note = dialog_for(window, video).figure_note.text()
        assert "on setlist.fm," in note and "MusicBrainz isn't asked" in note
        assert "musicbrainz.org" not in note and "coverartarchive.org" not in note

    def test_each_lookup_says_what_it_sends_and_the_steps_here_stay_here(
            self, window, tmp_path, monkeypatch):
        from mediabrowser.core import setlistfm
        from mediabrowser.gui.dialogs.chapters_dialog import LOCAL_STEP

        monkeypatch.setenv(ai.API_KEY_ENV, "sk-1")
        monkeypatch.setenv(setlistfm.API_KEY_ENV, "fm-1")
        save(**{ai.SETTING_SEARCH: "perplexity"})
        dialog = dialog_for(window, file_video(tmp_path, 600.0, starts=(0.0, 200.0)))
        assert "goes to musicbrainz.org" in dialog.mb_status.text()
        assert "sends them to setlist.fm" in dialog.setlist_status.text()
        assert dialog.ai_online_check.text() == "Look the show up online (Perplexity)"
        assert "Perplexity search the web" in dialog.ai_online_check.toolTip()
        assert "on this computer" in LOCAL_STEP

    def test_the_menu_says_where_its_pictures_go_before_it_is_read(self, window, tmp_path,
                                                                    monkeypatch):
        monkeypatch.setenv(ai.API_KEY_ENV, "sk-1")
        video = file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0), kind="bluray")
        dialog = dialog_for(window, video)
        assert dialog.menu_status.text().startswith(
            "Sends pictures of the menu's buttons, and the file and folder names, to "
            "claude-sonnet-5 through Nano-GPT")
        save(**{privacy.AI: True, ai.SETTING_PRIVACY_MODE: True})
        dialog = dialog_for(window, video)
        assert "sealed end to end" in dialog.menu_status.text()
