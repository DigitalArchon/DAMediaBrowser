# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Where the app can send things right now, as the windows and the status
bar say it (core.reach)."""

import pytest

from mediabrowser.core import ai, privacy, reach, sealed, setlistfm


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
    monkeypatch.delenv(setlistfm.API_KEY_ENV, raising=False)
    # Tinfoil's client, as Privacy Mode asks after it: there.
    monkeypatch.setattr(sealed, "_known", (True, "tinfoil"))


def settings(**on):
    found = dict.fromkeys(privacy.CHOICES, False)
    found.update(on)
    return found


def test_nothing_allowed_nothing_goes_online():
    found = reach.services(settings())
    assert not any(service.on for service in found.values())
    assert reach.status_text(found) == "Nothing goes online"
    assert found[privacy.MUSICBRAINZ].off == privacy.OFF[privacy.MUSICBRAINZ]


def test_a_switch_alone_isnt_enough_without_its_key(monkeypatch):
    found = reach.services(settings(**{privacy.SETLISTFM: True, privacy.AI: True,
                                       privacy.WEB_SEARCH: True}))
    assert not found[privacy.SETLISTFM].on and "setlist.fm API key" in found[privacy.SETLISTFM].off
    assert not found[privacy.AI].on and "Nano-GPT API key" in found[privacy.AI].off
    assert "the AI can't be asked" in found[privacy.WEB_SEARCH].off
    monkeypatch.setenv(setlistfm.API_KEY_ENV, "fm")
    monkeypatch.setenv(ai.API_KEY_ENV, "sk")
    found = reach.services(settings(**{privacy.SETLISTFM: True, privacy.AI: True}))
    assert found[privacy.SETLISTFM].on and found[privacy.AI].on
    # Without the AI's own switch for it, the search is off for that reason.
    assert found[privacy.WEB_SEARCH].off == privacy.OFF[privacy.WEB_SEARCH]


def test_the_status_bar_names_each_and_how_the_ai_travels(monkeypatch):
    monkeypatch.setenv(ai.API_KEY_ENV, "sk")
    on = settings(**{privacy.MUSICBRAINZ: True, privacy.COVER_ART: True, privacy.AI: True,
                     privacy.WEB_SEARCH: True})
    assert reach.status_text(reach.services(on)) == (
        "Online: MusicBrainz · Covers · AI · Web search")
    private = dict(on, **{ai.SETTING_PRIVACY_MODE: True})
    found = reach.services(private)
    assert reach.status_text(found) == "Online: MusicBrainz · Covers · AI, sealed end to end"
    assert "Never in Privacy Mode" in found[privacy.WEB_SEARCH].off
    tee = dict(private, **{ai.SETTING_PRIVATE_MODEL: "TEE/kimi-k3"})
    assert reach.status_text(reach.services(tee)).endswith("AI, TEE attested first")


def test_web_search_is_nano_gpts_own(monkeypatch):
    monkeypatch.setenv(ai.API_KEY_ENV, "sk")
    elsewhere = settings(**{privacy.AI: True, privacy.WEB_SEARCH: True,
                            ai.SETTING_BASE_URL: "https://llm.example.net/v1"})
    found = reach.services(elsewhere)
    assert found[privacy.AI].on and "llm.example.net" in found[privacy.AI].where
    assert not found[privacy.WEB_SEARCH].on and "Nano-GPT's own" in found[privacy.WEB_SEARCH].off


def test_the_overview_lists_every_place_and_says_why_one_is_off():
    text = reach.overview(reach.services(settings(**{privacy.MUSICBRAINZ: True})))
    lines = text.splitlines()
    assert lines[0] == "✓ MusicBrainz: to musicbrainz.org"
    assert lines[1].startswith("✗ The Cover Art Archive: off. Fetching cover art")
    assert lines[2].startswith("✗ setlist.fm: off.")
    # And RedPill's session records, which only Privacy Mode's own switch sends to.
    assert len(lines) == len(privacy.CHOICES) + 1
    assert lines[-1] == ("✗ RedPill's session records: off. Settings → Privacy's \"Name "
                         "the provider behind each TEE reply\" isn't ticked.")


def test_redpill_is_named_only_while_its_switch_is_on_for_a_tee_model(monkeypatch):
    """The provider behind each TEE reply, from api.redpill.ai directly: off by default,
    in Privacy Mode only, and only for a TEE/ model (SealedLore 44e145d)."""
    monkeypatch.setenv(ai.API_KEY_ENV, "sk")
    tee = settings(**{privacy.AI: True, ai.SETTING_PRIVACY_MODE: True,
                      ai.SETTING_PRIVATE_MODEL: "TEE/glm-5.3-flash"})
    assert not reach.services(tee)[reach.UPSTREAMS].on, "off by default"
    named = dict(tee, **{ai.SETTING_NAME_UPSTREAMS: True})
    found = reach.services(named)
    assert found[reach.UPSTREAMS].on
    assert found[reach.UPSTREAMS].where.startswith("to api.redpill.ai, directly")
    assert reach.status_text(found).endswith("AI, TEE attested first · RedPill")
    assert reach.names([reach.UPSTREAMS], found) == "api.redpill.ai"
    sealed_one = dict(named, **{ai.SETTING_PRIVATE_MODEL: "private/kimi-k3"})
    assert "sealed end to end" in reach.services(sealed_one)[reach.UPSTREAMS].off
    outside = dict(named, **{ai.SETTING_PRIVACY_MODE: False})
    assert "Only in Privacy Mode" in reach.services(outside)[reach.UPSTREAMS].off
    garbled = dict(tee, **{ai.SETTING_NAME_UPSTREAMS: "yes"})
    assert not reach.services(garbled)[reach.UPSTREAMS].on, "on only when plainly on"


def test_names_are_the_hosts_of_those_on():
    found = reach.services(settings(**{privacy.MUSICBRAINZ: True}))
    assert reach.names([privacy.MUSICBRAINZ, privacy.SETLISTFM], found) == "musicbrainz.org"
    assert reach.names([privacy.SETLISTFM], found) == ""
    assert reach.and_list(["a", "b", "c"]) == "a, b and c"
    assert reach.and_list([]) == ""
