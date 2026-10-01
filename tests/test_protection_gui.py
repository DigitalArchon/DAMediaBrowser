# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Locked and private in the windows, the Type column, Identify from a
video's right-click menu, and Just Figure It Out in Detect Chapters."""

import pytest

from mediabrowser.core import ai, autoname, library, naming, store
from tests.test_chapters_dialog import dialog_for, file_video, pump

PySide6 = pytest.importorskip("PySide6")


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(ai.API_KEY_ENV, "test-key")


@pytest.fixture
def shelf(window, tmp_path):
    """A library of one video with three unnamed chapters, open."""
    video = file_video(tmp_path, 900.0, starts=(0.0, 300.0, 600.0))
    video["media_type"] = library.MEDIA_CHAPTERED
    window.data = store.default_library(str(tmp_path / "media"))
    window.data["videos"] = {"v": video}
    window.refresh_library()
    window.open_video("v")
    return window, video


def menu_texts(menu):
    return {action.text(): action for action in menu.actions() if action.text()}


def a_change():
    chapters = [
        {"index": i, "title": title, "source": "menu", "start": s, "end": e}
        for i, (title, s, e) in enumerate(
            [("Opening", 0.0, 300.0), ("Megitsune", 300.0, 600.0), ("Karate", 600.0, 900.0)]
        )
    ]
    return autoname.Change(chapters, None, "release-1")


def outcome_for(video, change):
    before = naming.status(video)
    after = naming.status(dict(video, chapters=change.chapters)) if change else before
    return autoname.Outcome("v", video["display_name"], before, after, change,
                            ["Disc menu: named 3 chapter(s)"])


class TestMarkNamed:
    def test_its_menu_marks_it_named_and_unmarks_it(self, shelf):
        from PySide6.QtWidgets import QMenu

        from mediabrowser.gui.list_view import COL_IDENTIFIED

        window, video = shelf
        menu = QMenu()
        window._add_folder_actions(menu, "v")
        mark = menu_texts(menu)["Mark Named"]
        assert not mark.isChecked()
        mark.trigger()
        assert video[naming.MARKED_KEY] is True
        assert naming.status(video).state == naming.MARKED
        assert window.list.topLevelItem(0).text(COL_IDENTIFIED).endswith("Marked named")
        assert autoname.candidates(window.data["videos"].items(), autoname.Options()) == [], \
            "Identify Library leaves it alone"
        window.mark_video_named("v", False)
        assert naming.MARKED_KEY not in video

    def test_a_locked_video_cant_be_marked(self, shelf):
        window, video = shelf
        window.set_video_flag("v", "locked", True)
        window.mark_video_named("v", True)
        assert naming.MARKED_KEY not in video


class TestLocked:
    def test_nothing_changes_it_by_hand(self, shelf, monkeypatch):
        from PySide6.QtWidgets import QInputDialog

        window, video = shelf
        window.set_video_flag("v", "locked", True)
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Megitsune", True))
        window.rename_chapter(0)
        window.merge_with_next(0)
        window.rename_video("v")
        window.edit_chapters()
        assert video["chapters"][0]["title"] is None and len(video["chapters"]) == 3
        assert video["display_name"] == "1. Doomsday I, II"
        assert not window.editor.active()
        assert "locked" in window.status_label.text()

    def test_the_detail_view_only_plays_it(self, shelf):
        window, _video = shelf
        window.set_video_flag("v", "locked", True)
        detail = window.detail
        detail.tree.setCurrentItem(detail.tree.topLevelItem(0))
        assert detail.play_audio_button.isEnabled()
        for button in (detail.rename_button, detail.chapters_button, detail.mark_button,
                       detail.edit_button, detail.merge_button):
            assert not button.isEnabled()
        assert detail.meta.text().endswith("Locked")

    def test_its_menu_offers_nothing_that_changes_it(self, shelf):
        from PySide6.QtWidgets import QMenu

        window, _video = shelf
        window.set_video_flag("v", "locked", True)
        menu = QMenu()
        window._add_folder_actions(menu, "v")
        actions = menu_texts(menu)
        assert not actions["Identify"].isEnabled()
        assert not actions["Rename Video…"].isEnabled()
        assert not actions["Hide This Video"].isEnabled()
        assert actions["Locked"].isChecked() and actions["Locked"].isEnabled()

    def test_a_locked_library_locks_every_video(self, shelf):
        from PySide6.QtWidgets import QMenu

        window, video = shelf
        root = window.data["settings"]["library_root"]
        window.set_library_flag(root, "locked", True)
        assert window.list.topLevelItem(0).text(5) == "Locked (library)"
        menu = QMenu()
        window._add_folder_actions(menu, "v")
        whole = menu_texts(menu)["Locked (whole library)"]
        assert whole.isChecked() and not whole.isEnabled()
        assert store.load_library_for_root(root)["settings"]["locked"] is True
        assert window.identify_skips() == {"v"}

    def test_the_library_panel_shows_and_sets_it(self, shelf):
        window, _video = shelf
        root = window.data["settings"]["library_root"]
        window.libraries.refresh(root)
        menu = window.libraries.context_menu_for(root)
        menu_texts(menu)["Make Library Private"].setChecked(True)
        assert window.data["settings"]["private"] is True
        assert "Private" in window.libraries.list.item(0).text()


class TestPrivate:
    def test_its_cover_is_not_fetched_by_its_release(self, app, shelf, monkeypatch):
        from mediabrowser.core import artwork

        window, video = shelf
        video["musicbrainz_release_id"] = "release-1"  # matched before it was private
        window.set_video_flag("v", "private", True)
        asked = []
        monkeypatch.setattr(artwork, "is_resolved", lambda video_id: bool(asked))
        monkeypatch.setattr(artwork, "find",
                            lambda video_id, video, release_id=None: asked.append(release_id))
        window._artwork_job = None
        window._resolve_artwork()
        assert pump(app, lambda: window._artwork_job is None)
        assert asked == [None]

    def test_translating_is_refused(self, shelf, configured):
        window, video = shelf
        window.set_video_flag("v", "private", True)
        window.translate_titles()
        assert "private" in window.status_label.text()

    def test_detect_chapters_keeps_it_offline(self, window, tmp_path, measured,
                                             no_searching, configured):
        from mediabrowser.gui.dialogs.chapters_dialog import ChaptersDialog

        dialog = ChaptersDialog(window, file_video(tmp_path, 900.0, starts=(0.0, 300.0)),
                                private=True)
        assert not dialog.figure_button.isEnabled()
        assert not dialog.search_button.isEnabled()
        assert not dialog.ask_ai_button.isEnabled()
        dialog.search()
        assert no_searching == []
        assert "private" in dialog.mb_status.text()


class TestTheList:
    def test_type_and_protection_columns(self, shelf):
        from mediabrowser.gui.list_view import COL_PROTECTION, COL_TYPE

        window, _video = shelf
        row = window.list.topLevelItem(0)
        assert row.text(COL_TYPE) == "File with chapters"
        assert row.text(COL_PROTECTION) == ""
        window.set_video_flag("v", "private", True)
        assert window.list.topLevelItem(0).text(COL_PROTECTION) == "Private"


class TestIdentifyFromTheMenu:
    def test_it_identifies_in_the_background_and_can_be_undone(
        self, app, shelf, monkeypatch
    ):
        window, video = shelf
        seen = {}

        def identify(video_id, snapshot, options, settings, services, budget,
                     library_root=None, cancel=None, private=False):
            seen.update(video_id=video_id, private=private, methods=options.methods)
            return outcome_for(snapshot, a_change())

        monkeypatch.setattr(autoname, "identify", identify)
        window.identify_video("v")
        assert pump(app, lambda: not window._identifying_videos)
        assert seen["video_id"] == "v" and seen["private"] is False
        assert [c["title"] for c in video["chapters"]] == ["Opening", "Megitsune", "Karate"]
        assert video["musicbrainz_release_id"] == "release-1"
        assert "Undo Last Identification" in window.status_label.text()
        assert window.undo_identify() == 1
        assert video["chapters"][0]["title"] is None

    def test_a_private_video_is_identified_locally(self, app, shelf, monkeypatch):
        window, _video = shelf
        seen = {}

        def identify(video_id, snapshot, options, *args, private=False, **kwargs):
            seen["private"] = private
            return outcome_for(snapshot, None)

        monkeypatch.setattr(autoname, "identify", identify)
        store.save_app_settings({"identify_options": {"methods": ["audio", "musicbrainz"]}})
        window.set_video_flag("v", "private", True)
        window.identify_video("v")
        assert pump(app, lambda: not window._identifying_videos)
        assert seen["private"] is True
        assert "nothing more found" in window.status_label.text()

    def test_it_says_when_it_will_use_the_ai_and_spends_little(
        self, app, shelf, monkeypatch, configured
    ):
        from PySide6.QtWidgets import QMenu

        window, _video = shelf
        seen = {}

        def identify(video_id, snapshot, options, settings, services, budget, *a, **k):
            seen["budget"] = budget.remaining
            return outcome_for(snapshot, None)

        monkeypatch.setattr(autoname, "identify", identify)
        store.save_app_settings({"identify_options": {"methods": ["musicbrainz"]}})
        menu = QMenu()
        window._add_folder_actions(menu, "v")
        assert "Identify" in menu_texts(menu)
        store.save_app_settings({"identify_options": {"methods": ["musicbrainz", "ai_look"],
                                                      "ai_budget": 40}})
        menu = QMenu()
        window._add_folder_actions(menu, "v")
        action = menu_texts(menu)["Identify (Uses AI)"]
        assert "cents" in action.toolTip()
        window.identify_video("v")
        assert "the AI may be asked up to 6 times" in window.status_label.text()
        assert pump(app, lambda: not window._identifying_videos)
        assert seen["budget"] == autoname.ONE_VIDEO_AI_BUDGET, "not a whole run's 40"
        # A private video never uses it, so doesn't say it will.
        window.set_video_flag("v", "private", True)
        menu = QMenu()
        window._add_folder_actions(menu, "v")
        assert "Identify" in menu_texts(menu)


class TestJustFigureItOut:
    def test_it_is_offered_with_what_it_costs(self, window, tmp_path, measured,
                                              no_searching, configured):
        dialog = dialog_for(window, file_video(tmp_path, 900.0, starts=(0.0, 300.0)))
        assert dialog.figure_button.isEnabled()
        assert "Uses the AI" in dialog.figure_note.text()

    def test_without_the_ai_it_says_why_not(self, window, tmp_path, measured,
                                            no_searching, monkeypatch):
        monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
        dialog = dialog_for(window, file_video(tmp_path, 900.0, starts=(0.0, 300.0)))
        assert not dialog.figure_button.isEnabled()
        assert "isn't set up" in dialog.figure_note.text()

    def test_its_answer_is_shown_then_applied(self, app, shelf, measured, no_searching,
                                              configured, monkeypatch):
        window, video = shelf
        asked = {}

        def identify(video_id, snapshot, options, *args, **kwargs):
            asked["options"] = options
            return outcome_for(snapshot, a_change())

        monkeypatch.setattr(autoname, "identify", identify)
        dialog = dialog_for(window, video)
        dialog.figure_it_out()
        assert pump(app, lambda: dialog._figured is not None)
        assert asked["options"].ai_policy == autoname.ACCURATE_FIRST
        assert asked["options"].only_sure is False, "someone is here to check"
        assert dialog.tree.topLevelItem(1).text(2) == "Megitsune"
        assert dialog.tree.topLevelItem(1).text(3) == "disc menu"
        assert dialog.result()[0] == "change"
        # The AI can be asked to check what it found.
        assert [c["title"] for c in dialog._proposed_chapters()][1] == "Megitsune"
        window.apply_chapters_result(dialog)
        assert [c["title"] for c in video["chapters"]] == ["Opening", "Megitsune", "Karate"]
        assert autoname.last_run(window.data) is None, "not a run's to undo"

    def test_a_tracklist_given_afterwards_takes_over(self, app, shelf, measured,
                                                      no_searching, configured,
                                                      monkeypatch):
        from tests.test_chapters_dialog import paste

        window, video = shelf
        monkeypatch.setattr(autoname, "identify",
                            lambda vid, snapshot, *a, **k: outcome_for(snapshot, a_change()))
        dialog = dialog_for(window, video)
        dialog.figure_it_out()
        assert pump(app, lambda: dialog._figured is not None)
        paste(dialog, "A\nB\nC")
        dialog._on_paste_changed()
        assert dialog._figured is None and dialog.result()[0] == "name"
