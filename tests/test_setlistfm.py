# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""setlist.fm: the API answered by a stand-in, the dates and artists read
from a video's name, the Identify step, and the lookup in Detect Chapters
- credited wherever its setlist is used."""

import datetime
import io
import json
import threading
import urllib.error

import pytest

from mediabrowser.core import ai, ai_chapters, autoname, privacy, setlistfm
from tests.test_autoname import FakeServices, file_video, options, result

SHOW = {
    "id": "63de4613", "eventDate": "19-04-2026",
    "artist": {"mbid": "m1", "name": "Glass Harbor"},
    "venue": {"name": "Copperfield Hall",
              "city": {"name": "Leeds", "country": {"code": "GB", "name": "United Kingdom"}}},
    "tour": {"name": "Northbound Tour"},
    "sets": {"set": [
        {"song": [{"name": "Intro", "tape": True}, {"name": "Paper Lanterns"},
                  {"name": ""}, {"name": "Northbound", "info": "acoustic"}]},
        {"encore": 1, "song": [{"name": "Song 7", "cover": {"name": "Someone Else"}}]},
    ]},
    "url": "https://www.setlist.fm/setlist/glass-harbor/2026/copperfield-hall-63de4613.html",
}


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def api(monkeypatch):
    """setlist.fm as a stand-in: answers from `pages` (a function of the
    query), records every request."""
    monkeypatch.setattr(setlistfm, "_MIN_INTERVAL", 0.0)
    monkeypatch.setenv(setlistfm.API_KEY_ENV, "secret")
    calls = []
    state = {"answer": lambda query: {"setlist": [SHOW]}}

    def urlopen(request, timeout):
        calls.append(request)
        answer = state["answer"](request.full_url)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(request.full_url, answer, "x", {}, None)
        return Reply(json.dumps(answer).encode())

    monkeypatch.setattr(setlistfm.urllib.request, "urlopen", urlopen)
    state["calls"] = calls
    return state


def test_a_setlist_is_read_with_its_songs_in_order():
    setlist = setlistfm.parse_setlist(SHOW)
    assert setlist.date == datetime.date(2026, 4, 19)
    assert setlist.where() == "Copperfield Hall, Leeds, United Kingdom"
    assert [s.title for s in setlist.songs] == ["Intro", "Paper Lanterns", "Northbound", "Song 7"]
    assert setlist.songs[0].tape and setlist.songs[3].encore == 1
    assert setlist.songs[3].cover_of == "Someone Else"
    assert setlist.tracks() == [{"title": t, "length": None}
                                for t in ("Paper Lanterns", "Northbound", "Song 7")]
    assert len(setlist.tracks(include_tape=True)) == 4
    assert setlist.describe() == ("19 Apr 2026 · Copperfield Hall, Leeds, United Kingdom · "
                                  "Glass Harbor · Northbound Tour · 3 songs")


def test_a_search_sends_the_key_the_artist_and_the_date(api):
    found = setlistfm.search("Glass Harbor", date=datetime.date(2026, 4, 19))
    assert [s.id for s in found] == ["63de4613"]
    request = api["calls"][0]
    assert request.get_header("X-api-key") == "secret"
    assert request.get_header("Accept") == "application/json"
    assert "artistName=Glass+Harbor" in request.full_url and "date=19-04-2026" in request.full_url
    assert "year=" not in request.full_url


def test_a_place_is_tried_as_the_venue_then_the_city(api):
    api["answer"] = lambda url: 404 if "venueName" in url else {"setlist": [SHOW]}
    assert setlistfm.search("Glass Harbor", year=2026, place="Leeds")
    assert ["venueName=Leeds" in api["calls"][0].full_url,
            "cityName=Leeds" in api["calls"][1].full_url] == [True, True]


def test_nothing_found_is_no_setlists_and_a_refused_key_says_so(api):
    api["answer"] = lambda url: 404
    assert setlistfm.search("Nobody", date=datetime.date(2026, 1, 1)) == []
    api["answer"] = lambda url: 403
    with pytest.raises(setlistfm.SetlistError, match="key was refused"):
        setlistfm.search("Glass Harbor", year=2026)


def test_nothing_is_sent_without_a_key_or_permission(api, shipped_privacy, monkeypatch):
    with pytest.raises(setlistfm.SetlistError, match="switched off"):
        setlistfm.search("Glass Harbor", year=2026)
    assert setlistfm.not_ready() == privacy.OFF[privacy.SETLISTFM]
    monkeypatch.setattr(privacy, "DEFAULTS", dict.fromkeys(privacy.CHOICES, True))
    monkeypatch.delenv(setlistfm.API_KEY_ENV)
    with pytest.raises(setlistfm.NotConfigured):
        setlistfm.search("Glass Harbor", year=2026)
    assert api["calls"] == []


def test_the_key_lives_in_the_keyring():
    setlistfm.store_key("abc")
    assert setlistfm.stored_key() == "abc" == setlistfm.api_key()
    setlistfm.store_key("")
    assert setlistfm.stored_key() == ""


@pytest.mark.parametrize("texts, expected", [
    (["20260419 2100 Glass Harbor - Channel Nine Live"], (datetime.date(2026, 4, 19), None)),
    (["Glass Harbor 2026-04-19 Leeds"], (datetime.date(2026, 4, 19), None)),
    (["Glass Harbor 19.04.2026"], (datetime.date(2026, 4, 19), None)),
    (["Glass Harbor 04.05.2026"], (None, 2026)),  # April or May?
    (["Copperfield Hall", "Glass Harbor 2024"], (None, 2024)),
    (["Song 7"], (None, None)),
])
def test_dates_from_a_videos_name(texts, expected):
    assert setlistfm.guess_date(*texts) == expected


def test_what_a_person_types_as_the_date():
    assert setlistfm.parse_when("19/04/2026") == (datetime.date(2026, 4, 19), None)
    assert setlistfm.parse_when("2026-04-19") == (datetime.date(2026, 4, 19), None)
    assert setlistfm.parse_when("2026") == (None, 2026)
    assert setlistfm.parse_when("") == (None, None)
    with pytest.raises(ValueError):
        setlistfm.parse_when("last spring")


def test_the_same_artist_written_differently():
    assert setlistfm.same_artist("The Glass Harbor", "glass harbor")
    assert setlistfm.same_artist("Glass-Harbor!", "Glass Harbor")
    assert not setlistfm.same_artist("Glass Harbor Tribute", "Glass Harbor")


# --- Identify ------------------------------------------------------------------------


class SetlistServices(FakeServices):
    def __init__(self, found=None, ready=""):
        super().__init__()
        self.found = [setlistfm.parse_setlist(SHOW)] if found is None else found
        self.ready = ready

    def setlistfm_ready(self):
        return self.ready

    def setlists(self, artist, date):
        self.calls.append(("setlists", artist, date))
        return self.found


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
    return ai.settings_from({ai.SETTING_KEY: "k"})


def broadcast(starts, duration=1200.0):
    video = file_video(starts, duration=duration)
    video["path"] = "/lib/Glass Harbor/20260419 Glass Harbor - Copperfield Hall.ts"
    video["display_name"] = "20260419 Glass Harbor - Copperfield Hall"
    return video


def identify(video, services, settings, *methods, **kw):
    return autoname.identify("v", video, options(*methods), settings, services,
                             autoname.Budget(4), library_root="/lib", **kw)


def test_a_setlist_names_one_chapter_per_song_in_order_and_is_credited(settings):
    services = SetlistServices()
    outcome = identify(broadcast([0.0, 300.0, 700.0]), services, settings, autoname.SETLISTFM)
    assert ("setlists", "Glass Harbor", datetime.date(2026, 4, 19)) in services.calls
    assert [(c["title"], c["source"]) for c in outcome.change.chapters] == [
        ("Paper Lanterns", "setlistfm"), ("Northbound", "setlistfm"), ("Song 7", "setlistfm")]
    assert outcome.setlist_url == SHOW["url"]
    assert any("setlist from setlist.fm" in line for line in outcome.log)
    assert outcome.report_text().endswith(f"{setlistfm.CREDIT}: {SHOW['url']}")


def test_the_tape_counts_when_the_chapters_say_so(settings):
    services = SetlistServices()
    outcome = identify(broadcast([0.0, 60.0, 300.0, 700.0]), services, settings,
                       autoname.SETLISTFM)
    assert [c["title"] for c in outcome.change.chapters] == [
        "Intro", "Paper Lanterns", "Northbound", "Song 7"]


def test_a_different_number_of_chapters_goes_to_the_ai_without_a_web_search(settings):
    services = SetlistServices()
    seen = {}

    def look(situation):
        seen.update(source=situation.tracks_source, online=situation.online,
                    titles=[t["title"] for t in situation.tracks])
        return result(situation.chapters, [])

    services.look_answer = look
    outcome = identify(broadcast([0.0, 100.0, 300.0, 500.0, 700.0]), services, settings,
                       autoname.SETLISTFM, autoname.AI_LOOK)
    assert seen == {"source": "setlistfm", "online": False,
                    "titles": ["Intro", "Paper Lanterns", "Northbound", "Song 7"]}
    assert outcome.change is None and outcome.setlist_url == ""


def test_only_the_artist_asked_about_and_only_with_a_date(settings):
    other = dict(SHOW, artist={"name": "Glass Harbor Tribute"})
    services = SetlistServices(found=[setlistfm.parse_setlist(other)])
    outcome = identify(broadcast([0.0, 300.0, 700.0]), services, settings, autoname.SETLISTFM)
    assert outcome.change is None
    undated = broadcast([0.0, 300.0, 700.0])
    undated["display_name"] = "Glass Harbor - Copperfield Hall 2026"
    undated["path"] = "/lib/Glass Harbor/Glass Harbor - Copperfield Hall 2026.mkv"
    services = SetlistServices()
    outcome = identify(undated, services, settings, autoname.SETLISTFM)
    assert "setlists" not in [call[0] for call in services.calls]
    assert any("doesn't say the date" in line for line in outcome.log)


def test_not_without_a_key_nor_for_a_private_video(settings):
    services = SetlistServices(ready="Add a setlist.fm API key")
    identify(broadcast([0.0, 300.0, 700.0]), services, settings, autoname.SETLISTFM)
    services2 = SetlistServices()
    identify(broadcast([0.0, 300.0, 700.0]), services2, settings, autoname.SETLISTFM,
             private=True)
    assert services.calls == services2.calls == []


def test_the_audio_finds_as_many_songs_as_the_setlist_has(settings, monkeypatch):
    from mediabrowser.core import chaptergen

    asked = []
    real = chaptergen.estimate_starts

    def estimate(levels, duration, song_count=None, **kw):
        asked.append(song_count)
        return [0.0, 400.0, 800.0] if song_count else [0.0]

    monkeypatch.setattr(chaptergen, "estimate_starts", estimate)
    services = SetlistServices()
    services.levels_value = object()
    video = broadcast([0.0], duration=1200.0)
    monkeypatch.setattr(chaptergen, "boundary_candidates", lambda *a, **k: [])
    outcome = identify(video, services, settings, autoname.SETLISTFM, autoname.AUDIO)
    assert asked == [None, 3]
    assert [c["title"] for c in outcome.change.chapters] == ["Paper Lanterns", "Northbound",
                                                             "Song 7"]
    assert real is not None


# --- Detect Chapters ---------------------------------------------------------------------


def wait_for(app, condition, seconds=5.0):
    for _ in range(int(seconds / 0.02)):
        if condition():
            return True
        app.processEvents()
        threading.Event().wait(0.02)
    return condition()


def test_a_setlist_names_the_chapters_in_detect_chapters_and_is_credited(app, api, monkeypatch):
    from mediabrowser.gui.dialogs.chapters_dialog import ChaptersDialog

    video = broadcast([0.0, 300.0, 700.0])
    dialog = ChaptersDialog(None, video, "/lib")
    assert dialog.setlist_artist.text() == "Glass Harbor"
    assert dialog.setlist_when.text() == "2026-04-19"
    dialog.search_setlists()
    assert wait_for(app, lambda: dialog.setlist_results.count() == 1)
    assert dialog.title_source() == setlistfm.SOURCE
    assert [t["title"] for t in dialog.tracks()] == ["Paper Lanterns", "Northbound", "Song 7"]
    assert SHOW["url"] in dialog.setlist_credit.text()
    assert "setlist.fm" in dialog.setlist_credit.text()
    assert not dialog.ai_online_check.isChecked()
    situation = dialog._ai_situation()
    assert situation is None or situation.tracks_source == "setlistfm"
    assert dialog.result()[0] == "name"
    assert dialog.credit() == f"Song names from setlist.fm: {SHOW['url']}"
    dialog.setlist_tape.setChecked(True)
    assert len(dialog.tracks()) == 4
    dialog.clear_proposal()
    assert dialog.tracks() == [] and dialog.credit() == ""


def test_detect_chapters_says_why_setlist_fm_is_off(app, monkeypatch, shipped_privacy):
    from mediabrowser.gui.dialogs.chapters_dialog import ChaptersDialog

    dialog = ChaptersDialog(None, broadcast([0.0, 300.0]), "/lib")
    assert not dialog.setlist_button.isEnabled()
    assert dialog.setlist_status.text() == privacy.OFF[privacy.SETLISTFM]
    assert not dialog.setlist_settings_button.isHidden()
    private = ChaptersDialog(None, broadcast([0.0, 300.0]), "/lib", private=True)
    assert "private" in private.setlist_status.text()


def test_the_ai_is_told_where_a_setlist_came_from():
    situation = ai_chapters.Situation(
        video=file_video(), chapters=[], mode=ai_chapters.NAME,
        tracks=[{"title": "Northbound", "length": None}], tracks_source="setlistfm")
    assert "from setlist.fm" in ai_chapters._describe_tracks(situation)


def test_settings_keeps_the_key_in_the_keyring(app, monkeypatch):
    from mediabrowser.gui.dialogs.settings_dialog import TAB_SETLISTFM, SettingsDialog

    monkeypatch.delenv(setlistfm.API_KEY_ENV, raising=False)
    dialog = SettingsDialog(None, TAB_SETLISTFM)
    assert dialog.tabs.tabText(TAB_SETLISTFM) == "setlist.fm"
    dialog.setlist_key.setText("  my-key ")
    dialog.accept()
    assert setlistfm.stored_key() == "my-key"
    again = SettingsDialog(None, TAB_SETLISTFM)
    assert again.setlist_key.text() == "my-key"
    assert privacy.SETLISTFM in again.privacy_checks
