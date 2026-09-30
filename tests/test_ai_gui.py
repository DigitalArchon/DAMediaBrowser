# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The AI in the windows: Ask AI in Detect Chapters, Romanise Titles, and
the settings. The model is a stand-in; nothing leaves the machine."""

import pytest

from mediabrowser.core import ai, ai_chapters, library, methods, store
from tests.test_chapters_dialog import (
    _finished,
    dialog_for,
    file_video,
    no_tracklist,
    paste,
    pump,
)

PySide6 = pytest.importorskip("PySide6")


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(ai.API_KEY_ENV, "test-key")


@pytest.fixture
def model(monkeypatch):
    """A stand-in for ai_chapters.run that answers from `reply` and
    records the situation it was given."""
    state = {"situations": [], "reply": None, "error": None}

    def run(situation, settings, progress_cb=None, cancel=None, **kwargs):
        state["situations"].append(situation)
        if state["error"]:
            raise ai.AIError(state["error"])
        if progress_cb:
            progress_cb(100)
        return state["reply"](situation)

    monkeypatch.setattr(ai_chapters, "run", run)
    return state


def names_reply(*titles):
    def build(situation):
        chapters = [dict(ch) for ch in situation.chapters]
        rows = []
        for i, title in enumerate(titles):
            if i < len(chapters):
                chapters[i]["title"] = title
                chapters[i]["source"] = "ai"
                if title == "Megitsune":
                    chapters[i]["original_title"] = "メギツネ"
                rows.append(ai_chapters.Row(i + 1, chapters[i]["start"], title,
                                            chapters[i].get("original_title"),
                                            "high", "", False))
        return ai_chapters.Result(
            mode=situation.mode, chapters=chapters, rows=rows, show="BABYMETAL at Budokan",
            notes="", frames_sent=6, model="anthropic/claude-sonnet-5",
        )
    return build


def wait_for_answer(app, dialog):
    return pump(app, lambda: dialog._ai_job is None and dialog._ai_result is not None)


class TestAskingFromChaptersAndNames:
    def test_without_a_key_it_says_so(self, window, tmp_path, measured, no_searching,
                                      monkeypatch):
        monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
        dialog = dialog_for(window, file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0)))
        assert not dialog.ask_ai_button.isEnabled()
        assert "AI Settings" in dialog.ai_hint.text()

    def test_a_discs_chapters_are_named_where_they_are(
        self, app, window, tmp_path, measured, no_searching, configured, model
    ):
        model["reply"] = names_reply("Opening", "Megitsune", "Karate")
        video = file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0))
        dialog = dialog_for(window, video, str(tmp_path / "media"))
        assert dialog.ask_ai_button.isEnabled()
        assert "6 frames" in dialog.ai_hint.text() and "claude-sonnet-5" in dialog.ai_hint.text()
        dialog.ask_ai()
        assert wait_for_answer(app, dialog)
        situation = model["situations"][0]
        assert situation.mode == ai_chapters.NAME
        assert situation.context == "BABYMETAL / 10-BABYMETAL-BUDOKAN_THE-ONE-EDITION"
        assert situation.translate and situation.frames_per_chapter == 2
        assert dialog.result() == ("name", [
            (0, "Opening", None, "ai"), (1, "Megitsune", "メギツネ", "ai"),
            (2, "Karate", None, "ai"),
        ])
        assert dialog.title_source() == "ai"
        assert dialog.status.text().startswith("It thinks this is BABYMETAL at Budokan.")
        assert dialog.tree.topLevelItem(1).text(3) == "high · メギツネ"

    def test_applying_stores_the_original_title_too(
        self, app, window, tmp_path, measured, no_searching, configured, model
    ):
        model["reply"] = names_reply("Opening", "Megitsune", "Karate")
        video = file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0))
        window.data = store.default_library(str(tmp_path / "media"))
        window.data["videos"] = {"v": video}
        window.refresh_library()
        window.open_video("v")
        dialog = dialog_for(window, video)
        dialog.ask_ai()
        assert wait_for_answer(app, dialog)
        window.apply_chapters_result(dialog)
        assert [c["title"] for c in video["chapters"]] == ["Opening", "Megitsune", "Karate"]
        assert video["chapters"][1]["original_title"] == "メギツネ"
        assert all(c["source"] == "ai" for c in video["chapters"])
        window.search.setText("メギツネ")
        assert [vid for vid, _ in window._sorted_videos()] == ["v"]

    def test_the_proposal_and_the_audio_go_with_a_placed_estimate(
        self, app, window, tmp_path, measured, no_searching, configured, model
    ):
        levels, starts, _ = measured
        model["reply"] = names_reply("A", "B", "C", "D")
        dialog = no_tracklist(dialog_for(window, file_video(tmp_path, levels.duration)))
        assert dialog._method == methods.DETECT
        dialog.ask_ai()
        assert wait_for_answer(app, dialog)
        situation = model["situations"][0]
        assert situation.mode == ai_chapters.PLACE
        assert len(situation.chapters) == 4 and situation.candidates
        kind, (chapters, origin) = dialog.result()
        assert kind == "replace" and origin == library.ORIGIN_ESTIMATED
        assert [c["title"] for c in chapters] == ["A", "B", "C", "D"]

    def test_the_tracklist_goes_too_and_frames_can_be_left_out(
        self, app, window, tmp_path, measured, no_searching, configured, model
    ):
        model["reply"] = names_reply("Megitsune", "Karate", "Starlight")
        dialog = dialog_for(window, file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0)))
        paste(dialog, "メギツネ\nKarate\nStarlight")
        dialog.ai_frames_check.setChecked(False)
        dialog.ai_translate_check.setChecked(False)
        assert "frames" not in dialog.ai_hint.text() and "tracklist" in dialog.ai_hint.text()
        dialog.ask_ai()
        assert wait_for_answer(app, dialog)
        situation = model["situations"][0]
        assert [t["title"] for t in situation.tracks] == ["メギツネ", "Karate", "Starlight"]
        assert situation.tracks_source == "pasted"
        assert situation.frames_per_chapter == 0 and not situation.translate

    def test_changing_anything_drops_the_answer(
        self, app, window, tmp_path, measured, no_searching, configured, model
    ):
        model["reply"] = names_reply("Opening", "Megitsune", "Karate")
        dialog = dialog_for(window, file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0)))
        dialog.ask_ai()
        assert wait_for_answer(app, dialog)
        paste(dialog, "One\nTwo\nThree")
        assert dialog._ai_result is None and dialog.title_source() == "manual"
        assert dialog.result() == ("name", [(0, "One"), (1, "Two"), (2, "Three")])

    def test_a_failure_is_said_and_the_proposal_stays(
        self, app, window, tmp_path, measured, no_searching, configured, model
    ):
        model["error"] = "the Nano-GPT balance is empty"
        dialog = dialog_for(window, file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0)))
        paste(dialog, "One\nTwo\nThree")
        dialog.ask_ai()
        assert pump(app, lambda: dialog._ai_job is None and "balance" in dialog.ai_hint.text())
        assert dialog.result() == ("name", [(0, "One"), (1, "Two"), (2, "Three")])
        assert dialog.ask_ai_button.isEnabled()

    def test_a_single_chapter_blu_ray_has_nothing_to_go_on(
        self, window, tmp_path, measured, no_searching, configured
    ):
        dialog = dialog_for(window, file_video(tmp_path, 900.0, kind="bluray"))
        assert not dialog.ask_ai_button.isEnabled()
        assert "needs something to look at" in dialog.ai_hint.text()


class TestTranslating:
    def test_titles_are_shown_and_applied_with_originals(
        self, app, window, tmp_path, configured, monkeypatch
    ):
        from mediabrowser.gui.dialogs.translate_dialog import TranslateDialog

        video = file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0))
        video["chapters"][0]["title"] = "メギツネ"
        video["chapters"][2]["title"] = "Karate"
        window.data = store.default_library(str(tmp_path / "media"))
        window.data["videos"] = {"v": video}
        window.refresh_library()
        window.open_video("v")
        monkeypatch.setattr(
            ai_chapters, "translate_titles",
            lambda titles, settings: [("Megitsune", "メギツネ"), ("Karate", None)],
        )
        dialog = TranslateDialog(window, video)
        assert pump(app, lambda: dialog.apply_button.isEnabled())
        assert dialog.tree.topLevelItem(0).text(2) == "Megitsune"
        assert dialog.mapping() == [(0, "Megitsune", "メギツネ")]
        window.apply_track_mapping(dialog.mapping(), "ai")
        assert video["chapters"][0] == {
            "index": 0, "title": "Megitsune", "start": 0.0, "end": 300.0,
            "source": "ai", "original_title": "メギツネ",
        }
        assert video["chapters"][2]["source"] == "auto-numbered", "untouched"

    def test_without_a_key_it_points_at_the_settings(self, window, tmp_path, monkeypatch):
        from mediabrowser.gui.dialogs.translate_dialog import TranslateDialog

        monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
        video = file_video(tmp_path, 900.0, starts=(0.0, 300.0))
        video["chapters"][0]["title"] = "x"
        dialog = TranslateDialog(window, video)
        assert "AI Settings" in dialog.status.text()
        assert not dialog.apply_button.isEnabled()


class TestSettings:
    def test_saving_keeps_the_key_and_model(self, window, monkeypatch):
        from mediabrowser.gui.dialogs.ai_settings_dialog import AISettingsDialog

        monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
        dialog = AISettingsDialog(window)
        dialog.key.setText(" sk-abc ")
        dialog.model.setCurrentText("anthropic/claude-opus-5.5")
        dialog.frames.setValue(3)
        dialog.search.setCurrentIndex(list(ai.SEARCH_PROVIDERS).index("perplexity"))
        dialog.accept()
        settings = ai.settings_from(store.load_app_settings())
        assert settings[ai.SETTING_KEY] == "sk-abc"
        assert settings[ai.SETTING_MODEL] == "anthropic/claude-opus-5.5"
        assert settings[ai.SETTING_FRAMES] == 3
        assert settings[ai.SETTING_SEARCH] == "perplexity"
        assert settings[ai.SETTING_BASE_URL] == ai.DEFAULT_BASE_URL

    def test_listing_models_fills_the_box(self, app, window, monkeypatch):
        from mediabrowser.gui.dialogs.ai_settings_dialog import AISettingsDialog

        monkeypatch.setattr(ai, "list_models", lambda settings: [
            {"id": "anthropic/claude-sonnet-5", "name": "Claude Sonnet 5", "vision": True,
             "price": "$2 in / $10 out per million tokens",
             "note": "tested with this app - recommended"},
            {"id": "openai/gpt-x", "name": "GPT", "vision": True, "price": "", "note": ""},
        ])
        dialog = AISettingsDialog(window)
        dialog.fetch_models()
        assert pump(app, lambda: dialog.model.count() == 2)
        assert dialog.status.text() == "2 models can look at pictures."
        dialog.model.setCurrentText("anthropic/claude-sonnet-5")
        assert dialog.model_note.text() == (
            "Claude Sonnet 5 · $2 in / $10 out per million tokens · "
            "tested with this app - recommended"
        )
        assert pump(app, lambda: _finished(dialog._jobs[-1][0]))

    def test_web_search_is_off_for_another_endpoint(self, app, window):
        from mediabrowser.gui.dialogs.ai_settings_dialog import AISettingsDialog

        dialog = AISettingsDialog(window)
        assert dialog.search.isEnabled()
        dialog.base_url.setText("https://api.openai.com/v1")
        assert not dialog.search.isEnabled()

    def test_a_test_reports_the_answer(self, app, window, monkeypatch):
        from mediabrowser.gui.dialogs.ai_settings_dialog import AISettingsDialog

        monkeypatch.setattr(ai, "ping", lambda settings: "OK")
        dialog = AISettingsDialog(window)
        dialog.key.setText("k")
        dialog.test()
        assert pump(app, lambda: dialog.status.text().endswith("answered: OK"))
        assert pump(app, lambda: _finished(dialog._jobs[-1][0]))


class TestTheDiscMenuTab:
    @pytest.fixture
    def menu_stub(self, monkeypatch):
        from mediabrowser.core import menu_chapters

        state = {"read": 0, "asked": 0, "error": None}
        disc = menu_chapters.DiscMenu(
            pages=[menu_chapters.MenuPage("00006.m2ts", False, 0, None)],
            buttons=[menu_chapters.ChapterButton(1, 0, None, b""),
                     menu_chapters.ChapterButton(2, 0, None, b"")],
        )

        def read(video, progress_cb=None, cancel=None):
            state["read"] += 1
            if state["error"]:
                raise menu_chapters.MenuError(state["error"])
            return disc

        def ask(video, disc, settings, context="", translate=True):
            state["asked"] += 1
            chapters = [dict(c) for c in video["chapters"]]
            rows = []
            for i, (title, source, note) in enumerate([
                ("Opening", "ai", "not on the menu"),
                ("BABYMETAL DEATH", "menu", menu_chapters.MENU_NOTE),
                ("Megitsune", "menu", menu_chapters.MENU_NOTE),
            ]):
                chapters[i].update(title=title, source=source)
                rows.append(ai_chapters.Row(i + 1, chapters[i]["start"], title, None,
                                            "high" if source == "menu" else "medium", note, False))
            return ai_chapters.Result(ai_chapters.NAME, chapters, rows, "", "", 3,
                                      "anthropic/claude-sonnet-5")

        monkeypatch.setattr(menu_chapters, "read", read)
        monkeypatch.setattr(menu_chapters, "ask", ask)
        return state

    def test_a_file_has_no_menu_tab_to_use(self, window, tmp_path, measured, no_searching):
        from mediabrowser.gui.dialogs.chapters_dialog import TAB_MENU, TAB_TRACKLIST

        dialog = dialog_for(window, file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0)))
        assert not dialog.tabs.isTabEnabled(TAB_MENU)
        assert dialog.tabs.tabText(TAB_MENU) == "Extract Disc Menu (Blu-ray only)"
        assert dialog.tabs.currentIndex() == TAB_TRACKLIST

    def test_a_blu_ray_opens_on_its_menu(self, window, tmp_path, measured, no_searching):
        from mediabrowser.gui.dialogs.chapters_dialog import TAB_MENU

        video = file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0), kind="bluray")
        dialog = dialog_for(window, video)
        assert dialog.tabs.currentIndex() == TAB_MENU
        assert "disc's own menu" in dialog.status.text()
        assert dialog.result() is None

    def test_reading_names_the_chapters_the_menu_names(
        self, app, window, tmp_path, measured, no_searching, configured, menu_stub
    ):
        video = file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0), kind="bluray")
        window.data = store.default_library(str(tmp_path / "media"))
        window.data["videos"] = {"v": video}
        window.refresh_library()
        window.open_video("v")
        dialog = dialog_for(window, video)
        dialog.menu_button.click()
        assert pump(app, lambda: dialog._menu_result is not None)
        assert menu_stub == {"read": 1, "asked": 1, "error": None}
        assert dialog.table_caption.text() == (
            "Names from the disc's menu, read by claude-sonnet-5"
        )
        assert dialog.status.text().startswith("2 chapter(s) named as the disc's menu names")
        assert dialog.menu_status.text() == "2 chapter(s) named from the menu."
        assert dialog.menu_button.text() == "Read It Again"
        window.apply_chapters_result(dialog)
        assert [(c["title"], c["source"]) for c in video["chapters"]] == [
            ("Opening", "ai"), ("BABYMETAL DEATH", "menu"), ("Megitsune", "menu"),
        ]

    def test_the_reading_survives_a_look_at_another_tab(
        self, app, window, tmp_path, measured, no_searching, configured, menu_stub
    ):
        from mediabrowser.gui.dialogs.chapters_dialog import TAB_MENU, TAB_TRACKLIST

        dialog = dialog_for(
            window, file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0), kind="bluray")
        )
        dialog.read_menu()
        assert pump(app, lambda: dialog._menu_result is not None)
        dialog.tabs.setCurrentIndex(TAB_TRACKLIST)
        assert dialog.result() is None
        dialog.tabs.setCurrentIndex(TAB_MENU)
        assert dialog.result()[0] == "name"
        dialog.read_menu()  # again: the menu isn't read twice
        assert pump(app, lambda: menu_stub["asked"] == 2 and dialog._menu_job is None)
        assert menu_stub["read"] == 1

    def test_without_a_key_it_says_so_after_reading(
        self, app, window, tmp_path, measured, no_searching, menu_stub, monkeypatch
    ):
        monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
        dialog = dialog_for(
            window, file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0), kind="bluray")
        )
        dialog.read_menu()
        assert pump(app, lambda: "AI Settings" in dialog.menu_status.text())
        assert dialog.menu_status.text().startswith("2 button(s) on 1 menu page(s) play 2 of")
        assert menu_stub["asked"] == 0 and dialog.menu_button.isEnabled()

    def test_no_menu_is_explained(
        self, app, window, tmp_path, measured, no_searching, configured, menu_stub
    ):
        menu_stub["error"] = (
            "This disc's menus are written in Java (BD-J), which can't be read yet."
        )
        dialog = dialog_for(
            window, file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0), kind="bluray")
        )
        dialog.read_menu()
        assert pump(app, lambda: "Java" in dialog.menu_status.text())
        assert dialog.menu_button.isEnabled() and dialog.result() is None
