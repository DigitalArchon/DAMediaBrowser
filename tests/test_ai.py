# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The Nano-GPT wire: settings, one request, one reply. The network is
faked at urllib."""

import io
import json
import urllib.error

import pytest

from mediabrowser.core import ai


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def http(monkeypatch):
    """Records requests and answers them with whatever `reply` holds."""
    calls = []
    state = {"reply": {}, "error": None}

    def urlopen(req, timeout=None):
        calls.append(req)
        if state["error"] is not None:
            raise state["error"]
        return FakeResponse(json.dumps(state["reply"]).encode("utf-8"))

    monkeypatch.setattr(ai.urllib.request, "urlopen", urlopen)
    state["calls"] = calls
    return state


def settings(**overrides):
    base = {ai.SETTING_KEY: "key-123", ai.SETTING_MODEL: "anthropic/claude-sonnet-5"}
    base.update(overrides)
    return ai.settings_from(base)


class TestSettings:
    def test_defaults_fill_in_what_isnt_stored(self, monkeypatch):
        monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
        s = ai.settings_from({})
        assert s[ai.SETTING_MODEL] == ai.DEFAULT_MODEL
        assert s[ai.SETTING_BASE_URL] == ai.DEFAULT_BASE_URL
        assert s[ai.SETTING_FRAMES] == ai.DEFAULT_FRAMES_PER_CHAPTER
        assert not ai.is_configured(s)

    def test_the_environment_key_wins(self, monkeypatch):
        monkeypatch.setenv(ai.API_KEY_ENV, "env-key")
        assert ai.settings_from({ai.SETTING_KEY: "stored"})[ai.SETTING_KEY] == "env-key"

    def test_frames_are_kept_sane(self, monkeypatch):
        monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
        assert ai.settings_from({ai.SETTING_FRAMES: 99})[ai.SETTING_FRAMES] == 4
        assert ai.settings_from({ai.SETTING_FRAMES: "x"})[ai.SETTING_FRAMES] == 2
        assert ai.settings_from({ai.SETTING_BASE_URL: "https://x/v1/"})[ai.SETTING_BASE_URL] == (
            "https://x/v1"
        )

    def test_online_names_the_search_provider(self, monkeypatch):
        monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
        s = ai.settings_from({})
        assert s[ai.SETTING_SEARCH] == "kagi"
        assert ai.model_name(s) == ai.DEFAULT_MODEL
        assert ai.model_name(s, online=True) == ai.DEFAULT_MODEL + ":online/kagi"
        s = ai.settings_from({ai.SETTING_SEARCH: "perplexity"})
        assert ai.model_name(s, online=True).endswith(":online/perplexity")
        assert ai.settings_from({ai.SETTING_SEARCH: "bing"})[ai.SETTING_SEARCH] == "kagi"
        already = ai.settings_from({ai.SETTING_MODEL: "x/y:online/linkup"})
        assert ai.model_name(already, online=True) == "x/y:online/linkup"

    def test_short_model_name(self):
        assert ai.short_model_name("anthropic/claude-opus-5.5") == "claude-opus-5.5"
        assert ai.short_model_name("anthropic/claude-sonnet-5:online/kagi") == "claude-sonnet-5"


class TestChat:
    def test_sends_the_openai_shape_with_the_key(self, http):
        http["reply"] = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        text = ai.chat([{"role": "user", "content": "hi"}], settings())
        assert text == '{"ok": true}'
        req = http["calls"][0]
        assert req.full_url == f"{ai.DEFAULT_BASE_URL}/chat/completions"
        assert req.get_header("Authorization") == "Bearer key-123"
        body = json.loads(req.data)
        assert body["model"] == "anthropic/claude-sonnet-5"
        assert body["response_format"] == {"type": "json_object"}
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        assert body["reasoning_effort"] == "low"

    def test_a_reply_cut_off_before_it_answered_says_so(self, http):
        http["reply"] = {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
        with pytest.raises(ai.AIError, match="ran out of room"):
            ai.chat([], settings())

    def test_without_a_key_nothing_is_sent(self, http, monkeypatch):
        monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
        with pytest.raises(ai.NotConfigured):
            ai.chat([], ai.settings_from({}))
        assert http["calls"] == []

    def test_json_that_doesnt_parse_is_asked_for_once_more(self, http, monkeypatch):
        replies = iter(['{"title": "Say "Hi""}', '{"title": "Say \\"Hi\\""}'])
        sent = []

        def chat(messages, settings, **kwargs):
            sent.append(messages)
            return next(replies)

        monkeypatch.setattr(ai, "chat", chat)
        assert ai.chat_json([{"role": "user", "content": "q"}], settings()) == {"title": 'Say "Hi"'}
        assert len(sent) == 2 and "isn't valid JSON" in sent[1][-1]["content"]
        assert sent[1][-2] == {"role": "assistant", "content": '{"title": "Say "Hi""}'}

    def test_content_parts_are_joined(self, http):
        http["reply"] = {"choices": [{"message": {"content": [
            {"type": "text", "text": "a"}, {"type": "text", "text": "b"},
        ]}}]}
        assert ai.chat([], settings()) == "ab"

    def test_an_error_in_the_body_is_raised(self, http):
        http["reply"] = {"error": {"message": "no such model"}}
        with pytest.raises(ai.AIError, match="no such model"):
            ai.chat([], settings())

    def test_http_errors_say_what_they_mean(self, http):
        http["error"] = urllib.error.HTTPError(
            "url", 402, "Payment Required", {}, io.BytesIO(b'{"error":{"message":"empty"}}')
        )
        with pytest.raises(ai.AIError, match="balance is empty: empty"):
            ai.chat([], settings())

    def test_a_refused_search_provider_is_its_own_error(self, http):
        http["error"] = urllib.error.HTTPError(
            "url", 400, "Bad Request", {}, io.BytesIO(
                b'{"error":{"message":"The selected web search provider is not compatible '
                b'with Zero Data Retention."}}'
            )
        )
        with pytest.raises(ai.SearchUnavailable):
            ai.chat([], settings(), online=True)
        with pytest.raises(ai.AIError) as caught:
            ai.chat([], settings(), online=False)
        assert not isinstance(caught.value, ai.SearchUnavailable)

    def test_network_failure_is_an_ai_error(self, http):
        http["error"] = urllib.error.URLError("name or service not known")
        with pytest.raises(ai.AIError, match="name or service"):
            ai.chat([], settings())

    def test_image_parts_are_data_urls(self):
        part = ai.image_part(b"\xff\xd8jpeg")
        assert part["type"] == "image_url"
        assert part["image_url"]["url"].startswith("data:image/jpeg;base64,")


class TestParsingReplies:
    def test_plain_json(self):
        assert ai.parse_json_reply('{"a": 1}') == {"a": 1}

    def test_fenced_or_chatty_json_is_found(self):
        assert ai.parse_json_reply('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}

    def test_not_json_is_an_error(self):
        with pytest.raises(ai.AIError):
            ai.parse_json_reply("no idea")
        with pytest.raises(ai.AIError):
            ai.parse_json_reply("[1, 2]")


class TestModels:
    def test_only_models_that_see_the_recommended_first(self, http):
        http["reply"] = {"data": [
            {"id": "openai/gpt-x", "name": "GPT X", "capabilities": {"vision": True}},
            {"id": "deepseek/chat", "capabilities": {"vision": False}},
            {"id": "anthropic/claude-haiku-4.5", "capabilities": {"vision": True}},
            {"id": "anthropic/claude-opus-5.5", "capabilities": {"vision": True}},
            {"id": "anthropic/claude-sonnet-5", "name": "Claude Sonnet 5",
             "capabilities": {"vision": True},
             "pricing": {"prompt": 2, "completion": 10}},
        ]}
        models = ai.list_models(settings())
        assert [m["id"] for m in models] == [
            "anthropic/claude-sonnet-5", "anthropic/claude-opus-5.5",
            "anthropic/claude-haiku-4.5", "openai/gpt-x",
        ]
        assert models[0]["note"] == "tested with this app - recommended"
        assert models[0]["price"] == "$2 in / $10 out per million tokens"
        assert ai.vision_known(models)
        assert http["calls"][0].full_url.endswith("/models?detailed=true")
        assert http["calls"][0].get_method() == "GET"

    def test_an_endpoint_that_doesnt_say_lists_everything(self, http):
        http["reply"] = {"data": [{"id": "llava"}, {"id": "llama3"}]}
        models = ai.list_models(settings())
        assert [m["id"] for m in models] == ["llama3", "llava"]
        assert not ai.vision_known(models)

    def test_web_search_only_where_nano_gpt_can_do_it(self):
        nano = settings()
        other = settings(**{ai.SETTING_BASE_URL: "https://api.openai.com/v1"})
        assert ai.searches_the_web(nano) and not ai.searches_the_web(other)
        assert ai.model_name(nano, online=True).endswith(":online/kagi")
        assert ai.model_name(other, online=True) == "anthropic/claude-sonnet-5"
