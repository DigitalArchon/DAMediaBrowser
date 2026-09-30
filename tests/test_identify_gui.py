# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Identify Library, driven through the real dialog and window with the
outside world faked: what it offers, what a run does to the library, and
undoing it."""

import pytest

from mediabrowser.core import ai, autoname, naming, store
from tests.test_autoname import FakeServices, file_video, release
from tests.test_chapters_dialog import pump

PySide6 = pytest.importorskip("PySide6")


@pytest.fixture
def library(window, tmp_path):
    media = tmp_path / "lib"
    media.mkdir()
    videos = {}
    for name, starts, titles in (
        ("done", (0.0, 600.0), ["A", "B"]),
        ("whole", (0.0,), None),
        ("partly", (0.0, 600.0), ["A", None]),
    ):
        path = media / f"{name}.mkv"
        path.write_bytes(b"")
        video = file_video(starts=starts, titles=titles)
        video.update(path=str(path), display_name=f"Artist - Budokan {name}")
        videos[name] = video
    window.data = store.default_library(str(media))
    window.data["videos"] = videos
    store.save_library(window.data)
    window.refresh_library()
    return window


@pytest.fixture
def fake(monkeypatch):
    services = FakeServices()
    rid, media = release([("Megitsune", 300.0), ("Karate", 312.0), ("The One", 600.0)])
    services.releases = [{"id": rid, "title": "Budokan", "artist": "Artist"}]
    services.media[rid] = media
    monkeypatch.setattr(autoname, "Services", lambda *a, **k: services)
    return services


def dialog(window):
    window.identify_library()
    return window._identify_dialog


class TestTheChoices:
    def test_it_starts_free_and_says_so(self, app, library, monkeypatch):
        monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
        d = dialog(library)
        assert d.options().methods == {autoname.MUSICBRAINZ, autoname.AUDIO}
        assert d.estimate.text() == "2 video(s) to identify. No AI: this run costs nothing."
        assert not d.method_checks[autoname.MENU].isEnabled(), "no key, no AI"

    def test_ai_methods_show_a_ceiling_on_the_cost(self, app, library, monkeypatch):
        monkeypatch.setenv(ai.API_KEY_ENV, "k")
        d = dialog(library)
        d.method_checks[autoname.AI_LOOK].setChecked(True)
        d.budget.setValue(10)
        assert ("At most 2 AI request(s), paid for from your Nano-GPT credit - under $0.12 "
                "with claude-sonnet-5") in d.estimate.text()
        d.include_partly.setChecked(False)
        assert d.estimate.text().startswith("1 video(s) to identify")

    def test_the_choices_are_remembered(self, app, library, monkeypatch):
        monkeypatch.setenv(ai.API_KEY_ENV, "k")
        d = dialog(library)
        d.accurate_first.setChecked(True)
        d.method_checks[autoname.AUDIO].setChecked(False)
        d._save_options()
        library._identify_dialog = None
        again = dialog(library)
        assert again.accurate_first.isChecked()
        assert again.options().methods == {autoname.MUSICBRAINZ}


class TestARun:
    def test_it_names_what_it_can_and_applies_as_it_goes(self, app, library, fake):
        d = dialog(library)
        d.method_checks[autoname.AUDIO].setChecked(False)
        d.start()
        assert pump(app, lambda: not d.running() and d.summary.text(), timeout=10)
        whole = library.data["videos"]["whole"]
        assert [c["title"] for c in whole["chapters"]] == ["Megitsune", "Karate", "The One"]
        assert naming.status(whole).state == naming.NAMED
        partly = library.data["videos"]["partly"]
        assert [c["title"] for c in partly["chapters"]] == ["A", "The One"], "A is kept"
        assert d.summary.text() == "Done: 2 identified, 0 improved, 0 unchanged. No AI used."
        assert d.log.count() == 2
        assert "MusicBrainz: placed 3 chapters" in d.log.item(0).text() + d.log.item(1).text()
        saved = store.load_library_for_root(library.data["settings"]["library_root"])
        assert saved["videos"]["whole"]["chapter_origin"] == "tracklist"
        assert "Identify Library - Done" in library.status_label.text()
        assert d.options_panel.isHidden() and d.start_button.text() == "Start Another Run"
        d.start()
        assert not d.options_panel.isHidden() and not d.running(), "back to the choices"

    def test_a_run_can_be_undone(self, app, library, fake):
        d = dialog(library)
        d.method_checks[autoname.AUDIO].setChecked(False)
        d.start()
        assert pump(app, lambda: not d.running() and d.summary.text(), timeout=10)
        assert d.undo_button.isVisible() or library.can_undo_identify()
        d.undo()
        whole = library.data["videos"]["whole"]
        assert [c["title"] for c in whole["chapters"]] == [None]
        assert "chapter_origin" not in whole
        assert not library.can_undo_identify()

    def test_nothing_else_can_swap_the_library_out_meanwhile(self, app, library, fake,
                                                             monkeypatch):
        d = dialog(library)
        monkeypatch.setattr(d, "running", lambda: True)
        library.rescan()
        assert "running" in library.status_label.text()
