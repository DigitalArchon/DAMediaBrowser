# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The AI's help with a MusicBrainz search: what it is shown, and what is
made of its answer. The chat is a stand-in."""

import json

import pytest

from mediabrowser.core import ai, ai_musicbrainz


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
    return ai.settings_from({ai.SETTING_KEY: "k"})


def video():
    return {"type": "file", "path": "/lib/Perfume/Budokan/Concert.mkv",
            "display_name": "Concert", "duration": 1212.0,
            "chapters": [{"title": "Polyrhythm", "source": "embedded"},
                         {"title": None, "source": "auto-numbered"}]}


class Chat:
    def __init__(self, reply):
        self.reply, self.asked = reply, []

    def __call__(self, messages, settings, **kwargs):
        self.asked.append((messages, kwargs))
        return json.dumps(self.reply)

    def prompt(self):
        return self.asked[0][0][-1]["content"]


def candidate(rid, title, recognised=False):
    return ai_musicbrainz.Candidate(
        {"id": rid, "title": title, "artist": "Perfume", "date": "2008-11-06"},
        [{"title": "Polyrhythm", "length": 600.0}, {"title": "Chocolate Disco", "length": 612.0}],
        "Disc 2 of 3", recognised,
    )


class TestBetterQueries:
    def test_it_searches_the_web_and_skips_what_was_tried(self, settings):
        chat = Chat({"what": "Perfume at Budokan, 2008",
                     "queries": ["Perfume Budokan", "  ", 'release:"BUDOUKANん"', "x", "y"]})
        what, queries = ai_musicbrainz.better_queries(
            video(), "Perfume / Budokan", ["perfume budokan"],
            [{"id": "r", "title": "GAME", "artist": "Perfume", "track_count": 12}],
            settings, chat=chat,
        )
        assert what == "Perfume at Budokan, 2008"
        assert queries == ['release:"BUDOUKANん"', "x"], "the tried one out; at most two"
        assert chat.asked[0][1]["online"] is True
        prompt = chat.prompt()
        assert "Searched MusicBrainz for: perfume budokan" in prompt
        assert "“GAME” by Perfume, 12 tracks" in prompt
        assert "some named: Polyrhythm" in prompt

    def test_a_reply_without_queries_is_none(self, settings):
        assert ai_musicbrainz.better_queries(video(), "", ["q"], [], settings,
                                             chat=Chat({})) == ("", [])


class TestChooseRelease:
    def test_its_choice_is_a_release_id(self, settings):
        chat = Chat({"n": 2, "confidence": "medium", "reason": "the Budokan one"})
        choice = ai_musicbrainz.choose_release(
            video(), "Perfume / Budokan",
            [candidate("a", "GAME"), candidate("b", "BUDOUKANん", recognised=True)],
            settings, chat=chat,
        )
        assert choice == ai_musicbrainz.Choice("b", "medium", "the Budokan one")
        prompt = chat.prompt()
        assert "2. “BUDOUKANん” by Perfume (2008)" in prompt
        assert "Disc 2 of 3: 2 tracks" in prompt
        assert "Tracks: Polyrhythm; Chocolate Disco" in prompt
        assert prompt.count("artist and title are both in") == 1

    @pytest.mark.parametrize("n", [None, 0, 3, "two", True])
    def test_none_or_nonsense_is_no_choice(self, settings, n):
        chat = Chat({"n": n, "confidence": "certain"})
        choice = ai_musicbrainz.choose_release(
            video(), "", [candidate("a", "A"), candidate("b", "B")], settings, chat=chat,
        )
        assert choice.release_id is None and choice.confidence == "low"

    def test_nothing_to_choose_from_asks_nothing(self, settings):
        chat = Chat({"n": 1})
        assert ai_musicbrainz.choose_release(video(), "", [], settings,
                                             chat=chat).release_id is None
        assert chat.asked == []

    def test_a_search_provider_the_account_refuses_is_swapped_for_linkup(self, settings):
        used = []

        def chat(messages, settings, **kwargs):
            used.append(settings.get(ai.SETTING_SEARCH))
            if len(used) == 1:
                raise ai.SearchUnavailable("web search provider not allowed")
            return json.dumps({"queries": ["q2"]})

        _what, queries = ai_musicbrainz.better_queries(video(), "", ["q"], [], settings,
                                                       chat=chat)
        assert queries == ["q2"] and used[-1] == "linkup"
