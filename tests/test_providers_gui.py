# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings → AI's provider list (NanoGPT, Chutes, Tinfoil, Anthropic, another
OpenAI-compatible service) and what follows from it in the windows: the address and
protocol filled in, whose key the field holds, one key per provider kept, the provider
recognised from an address however it gets there, each model's privacy where it is
chosen, and Detect Chapters held to its privacy once it has asked. Nothing leaves the
machine."""

import pytest

from mediabrowser.core import ai, privacy, services, store
from tests.test_ai_gui import model, names_reply, wait_for_answer  # noqa: F401
from tests.test_chapters_dialog import dialog_for, file_video

PySide6 = pytest.importorskip("PySide6")

TINFOIL = services.TINFOIL_BASE_URL
CHUTES = services.CHUTES_BASE_URL


@pytest.fixture
def no_env_key(monkeypatch):
    monkeypatch.delenv(ai.API_KEY_ENV, raising=False)


def choose(dialog, provider):
    """Pick `provider` from the list, as a click does."""
    index = list(ai.PROVIDERS).index(provider)
    dialog.provider.setCurrentIndex(index)
    dialog.provider.activated.emit(index)


def test_the_list_offers_the_five_and_each_fills_in_its_address_and_protocol(
        window, no_env_key):
    from mediabrowser.gui.dialogs.settings_dialog import SettingsDialog

    dialog = SettingsDialog(window)
    assert [dialog.provider.itemText(i) for i in range(dialog.provider.count())] == [
        "NanoGPT", "Chutes", "Tinfoil", "Anthropic", "Other (OpenAI-compatible)"]
    assert dialog.provider.currentData() == ai.NANOGPT
    for provider, address, words, label in (
            (ai.TINFOIL, TINFOIL, "end-to-end encrypted", "Tinfoil API key:"),
            (ai.CHUTES, CHUTES, "OpenAI's chat", "Chutes API key:"),
            (ai.ANTHROPIC, services.ANTHROPIC_URL, "Anthropic's Messages API",
             "Anthropic API key:"),
            (ai.NANOGPT, ai.DEFAULT_BASE_URL, "OpenAI's chat", "NanoGPT API key:")):
        choose(dialog, provider)
        assert dialog.base_url.text() == address
        assert words in dialog.protocol.text()
        assert dialog.key_label.text() == label
        assert dialog.model.currentText() == ai.PROVIDER_MODELS[provider]
    choose(dialog, ai.OTHER)
    assert dialog.base_url.text() == "" and dialog.key_label.text() == "API key:"
    dialog.accept()
    assert dialog.status.text().startswith("Not saved: set the service's address")


def test_switching_provider_and_back_loses_no_key(window, no_env_key):
    from mediabrowser.gui.dialogs.settings_dialog import SettingsDialog

    ai.store_key("sk-nano", ai.DEFAULT_BASE_URL)
    dialog = SettingsDialog(window)
    assert dialog.key.text() == "sk-nano"
    choose(dialog, ai.TINFOIL)
    assert dialog.key.text() == "", "Nano-GPT's key is not Tinfoil's"
    assert "inference.tinfoil.sh" in dialog.key_note.text()
    dialog.key.setText("tk-tinfoil")
    choose(dialog, ai.CHUTES)
    dialog.key.setText("cpk-chutes")
    choose(dialog, ai.NANOGPT)
    assert dialog.key.text() == "sk-nano"
    choose(dialog, ai.TINFOIL)
    assert dialog.key.text() == "tk-tinfoil", "what was typed for it comes back"
    dialog.accept()
    assert ai.stored_key(ai.NANOGPT) == "sk-nano"
    assert ai.stored_key(ai.TINFOIL) == "tk-tinfoil"
    assert ai.stored_key(ai.CHUTES) == "cpk-chutes"
    assert ai.key_origin(slot=ai.CHUTES) == "https://llm.chutes.ai:443"
    settings = ai.load_settings()
    assert settings[ai.SETTING_BASE_URL] == TINFOIL
    assert settings[ai.SETTING_KEY] == "tk-tinfoil"


@pytest.mark.parametrize("typed", ["https://INFERENCE.tinfoil.sh/v1",
                                   "https://inference.tinfoil.sh.:443/v1/chat/completions"])
def test_a_tinfoil_address_typed_by_hand_is_tinfoils(window, no_env_key, typed):
    from mediabrowser.gui.dialogs.settings_dialog import SettingsDialog

    dialog = SettingsDialog(window)
    dialog.privacy_checks[privacy.AI].setChecked(True)
    dialog.model.setCurrentText("gemma4-31b")
    dialog.base_url.setText(typed)
    assert dialog.provider.currentData() == ai.TINFOIL
    assert "end-to-end encrypted (EHBP)" in dialog.protocol.text()
    assert dialog.key_label.text() == "Tinfoil API key:"
    assert "Highest privacy" in dialog.model_note.text()
    assert "Tinfoil's router" in dialog.where_label.text()
    assert dialog.where_label.text().endswith("Highest privacy.")


def test_without_a_keyring_only_nano_gpts_key_note_names_the_environment(window, no_env_key,
                                                                         monkeypatch):
    """NANOGPT_API_KEY goes to Nano-GPT alone: the note offers it there, and says for any
    other provider that its key can't be kept without a keyring."""
    from mediabrowser.core import creds
    from mediabrowser.gui.dialogs.settings_dialog import SettingsDialog

    monkeypatch.setattr(creds, "backend_error", lambda: "There's no keyring to keep it in.")
    dialog = SettingsDialog(window)
    assert dialog.key_note.text().endswith(f"set {ai.API_KEY_ENV} in the environment.")
    for provider, whose in ((ai.TINFOIL, "a Tinfoil key"), (ai.CHUTES, "a Chutes key"),
                            (ai.ANTHROPIC, "an Anthropic key"), (ai.OTHER, "the service's key")):
        choose(dialog, provider)
        note = dialog.key_note.text()
        assert f"set {ai.API_KEY_ENV}" not in note, provider
        assert f"Until then, {whose} can't be kept" in note
        assert "goes to Nano-GPT alone" in note
    choose(dialog, ai.NANOGPT)
    assert dialog.key_note.text().endswith(f"set {ai.API_KEY_ENV} in the environment.")


def test_an_older_settings_file_opens_on_its_provider(window, no_env_key):
    from mediabrowser.gui.dialogs.settings_dialog import SettingsDialog

    store.save_app_settings({ai.SETTING_BASE_URL: services.CHUTES_ANTHROPIC_URL,
                             ai.SETTING_MODEL: "Qwen/Qwen3.6-27B-TEE"})
    dialog = SettingsDialog(window)
    assert dialog.provider.currentData() == ai.CHUTES
    assert "Anthropic's Messages API" in dialog.protocol.text()
    assert "Partial privacy" in dialog.model_note.text()


def test_the_private_models_level_shows_where_it_is_chosen(window, no_env_key):
    from mediabrowser.gui.dialogs.settings_dialog import SettingsDialog

    dialog = SettingsDialog(window)
    dialog.privacy_checks[privacy.AI].setChecked(True)
    dialog.privacy_mode_check.setChecked(True)
    dialog.private_model.setCurrentText("TEE/glm-5.3-flash")
    assert "Partial privacy" in dialog.private_model_note.text()
    choose(dialog, ai.CHUTES)
    dialog.private_model.setCurrentText("zai-org/GLM-5.3")
    assert "not one of Chutes' -TEE models" in dialog.private_model_note.text()
    dialog.strict_check.setChecked(True)
    dialog.private_model.setCurrentText("Qwen/Qwen3.6-27B-TEE")
    assert "always partial privacy" in dialog.private_model_note.text()


def test_detect_chapters_once_it_has_asked_moves_to_nothing_less_private(
        app, window, tmp_path, measured, no_searching, model, monkeypatch):  # noqa: F811
    """Asked in Privacy Mode with a private/ model (the highest privacy), Settings changed
    from the window to an ordinary model: what it has worked on goes on only to as
    private a model, and the window says so in words naming it and both levels."""
    from mediabrowser.gui.dialogs import settings_dialog

    monkeypatch.setenv(ai.API_KEY_ENV, "test-key")
    store.save_app_settings({privacy.AI: True, ai.SETTING_PRIVACY_MODE: True,
                             ai.SETTING_PRIVATE_MODEL: "private/kimi-k3-1-pro"})
    model["reply"] = names_reply("Opening", "Glass Harbor", "Lantern")
    video = file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0))
    dialog = dialog_for(window, video, str(tmp_path / "media"))
    dialog.ask_ai()
    assert wait_for_answer(app, dialog)

    def changed(self):
        settings = store.load_app_settings()
        settings[ai.SETTING_PRIVACY_MODE] = False
        store.save_app_settings(settings)
        return True

    monkeypatch.setattr(settings_dialog.SettingsDialog, "exec", changed)
    dialog._open_settings()
    said = dialog.status.text()
    assert said.startswith("Nothing was sent: Detect Chapters began with the highest privacy")
    assert "has no privacy" in said
    assert not dialog.ask_ai_button.isEnabled()
    assert "began with the highest privacy" in dialog.ai_hint.text()
    assert len(model["situations"]) == 1, "nothing more was asked"
