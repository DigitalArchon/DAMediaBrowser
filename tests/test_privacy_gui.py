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
    def test_the_status_bar_says_privacy_mode_and_where_to(self, window):
        window.update_privacy_indicator()
        assert not window.privacy_button.isVisibleTo(window)
        save(**{privacy.AI: True, ai.SETTING_PRIVACY_MODE: True})
        window.update_privacy_indicator()
        assert window.privacy_button.isVisibleTo(window)
        assert window.privacy_button.text() == "Privacy Mode: end-to-end encrypted"
        assert "only ciphertext" in window.privacy_button.toolTip()
        save(**{privacy.AI: False})
        window.update_privacy_indicator()
        assert not window.privacy_button.isVisibleTo(window), "no AI, nothing to say"

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
