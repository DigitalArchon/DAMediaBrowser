# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Anthropic's own Messages API (assistant/anthropic.py), and Chutes' proxy of it: the
request built from the app's OpenAI-shaped payload, thinking shaped by generation, the
key's header, a scripted SSE stream read back as OpenAI's, the proxy's own error text,
refusals before anything is sent, and the paginated listing. Ported from SealedLore's
tests/test_anthropic.py."""

import json
import unittest

from mediabrowser.core import anthropic, safejson  # noqa: E402
from mediabrowser.core.anthropic import (  # noqa: E402
    AnthropicClient,
    AnthropicError,
    headers,
    messages_body,
    messages_url,
    openai_lines,
    thinking_fields,
)
from mediabrowser.core.netguard import Headers as CaseInsensitiveDict  # noqa: E402
from tests.shared_core import LLMClient  # noqa: E402

PICTURE = "data:image/png;base64,iVBORw0KGgo="
PAYLOAD = {"model": "claude-opus-5-5", "stream": True, "max_tokens": 500,
           "temperature": 0.4, "stop": ["END"],
           "messages": [{"role": "system", "content": "Be brief."},
                        {"role": "system", "content": "Answer in English."},
                        {"role": "user", "content": [{"type": "text", "text": "What is it?"},
                                                     {"type": "image_url",
                                                      "image_url": {"url": PICTURE}}]},
                        {"role": "user", "content": "And why?"},
                        {"role": "assistant", "content": "A cat."},
                        {"role": "user", "content": ""},
                        {"role": "user", "content": "Sure?"}]}


def sse(*events):
    lines = []
    for event in events:
        lines += [f"event: {event['type']}", "data: " + json.dumps(event), ""]
    return [line.encode() for line in lines]


STREAM = sse(
    {"type": "message_start", "message": {"id": "msg_1", "usage": {
        "input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 1}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "thinking_delta", "thinking": "Hm."}},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "A "}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "cat."}},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
     "usage": {"output_tokens": 7}},
    {"type": "message_stop"})


class TheRequest(unittest.TestCase):
    def test_the_payload_becomes_a_messages_request(self):
        body = messages_body(PAYLOAD)
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertTrue(body["stream"])
        self.assertEqual(body["system"], [{"type": "text", "text": "Be brief."},
                                          {"type": "text", "text": "Answer in English."}])
        self.assertEqual([m["role"] for m in body["messages"]],
                         ["user", "assistant", "user"])
        first = body["messages"][0]["content"]
        self.assertEqual(first[0], {"type": "text", "text": "What is it?"})
        self.assertEqual(first[1], {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}})
        self.assertEqual(first[2], {"type": "text", "text": "And why?"})
        self.assertEqual(body["stop_sequences"], ["END"])
        self.assertNotIn("temperature", body, "Claude 5 takes no sampling")

    def test_a_conversation_that_begins_with_the_models_message_is_refused(self):
        with self.assertRaisesRegex(AnthropicError, "begin with a user's message"):
            messages_body({"model": "claude-x", "messages": [
                {"role": "assistant", "content": "Hi"}, {"role": "user", "content": "Yo"}]})
        with self.assertRaisesRegex(AnthropicError, "nothing was sent"):
            messages_body({"model": "claude-x", "messages": []})

    def test_the_cache_marker_goes_on_the_last_user_turn(self):
        body = messages_body(PAYLOAD, cache_ttl="1h")
        last = body["messages"][-1]["content"][-1]
        self.assertEqual(last["cache_control"], {"type": "ephemeral", "ttl": "1h"})
        self.assertNotIn("cache_control", json.dumps(body["messages"][0]))
        self.assertEqual(headers("https://api.anthropic.com", "k", "1h")["anthropic-beta"],
                         anthropic.EXTENDED_TTL_BETA)
        self.assertNotIn("anthropic-beta", headers("https://api.anthropic.com", "k", "5m"))

    def test_thinking_is_shaped_for_the_models_generation(self):
        self.assertEqual(thinking_fields("claude-sonnet-4-5", "medium", 1000),
                         ({"thinking": {"type": "enabled", "budget_tokens": 4096}}, 5096,
                          False))
        self.assertEqual(thinking_fields("claude-sonnet-4-5", None, 1000), ({}, 1000, True))
        self.assertEqual(thinking_fields("claude-opus-4-6", "xhigh", 1000),
                         ({"thinking": {"type": "adaptive"},
                           "output_config": {"effort": "high"}}, 1000, False))
        self.assertEqual(thinking_fields("claude-opus-4-7", "low", 1000)[0]["thinking"],
                         {"type": "adaptive", "display": "summarized"})
        self.assertEqual(thinking_fields("claude-opus-4-7", None, 1000), ({}, 1000, False))
        for always in ("claude-opus-5-5", "claude-fable-1"):
            self.assertEqual(thinking_fields(always, "none", None),
                             ({"output_config": {"effort": "low"}}, 4000, False))
        older = messages_body({**PAYLOAD, "model": "claude-3-5-sonnet-20241022"})
        self.assertEqual(older["temperature"], 0.4)

    def test_the_key_goes_as_anthropic_takes_it_or_as_chutes_proxy_does(self):
        own = headers("https://api.anthropic.com/v1", "sk-ant")
        self.assertEqual((own["x-api-key"], own["anthropic-version"]),
                         ("sk-ant", anthropic.API_VERSION))
        self.assertNotIn("authorization", own)
        proxy = headers("https://claude.chutes.ai", "cpk")
        self.assertEqual(proxy["authorization"], "Bearer cpk")
        self.assertNotIn("x-api-key", proxy)

    def test_the_address_is_the_apis_root_however_typed(self):
        for typed in ("https://api.anthropic.com", "https://api.anthropic.com/",
                      "https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages"):
            with self.subTest(typed=typed):
                self.assertEqual(messages_url(typed), "https://api.anthropic.com/v1/messages")

    def test_an_address_with_a_password_in_it_loses_the_password_and_is_refused(self):
        """This app's netguard.without_password keeps the user name, so that the address
        is still refused (netguard.problem): it sends no name or password from one. DA
        Assistant's drops both, and keeps such a password in its keyring."""
        self.assertEqual(messages_url("https://u:p@api.anthropic.com/v1"),
                         "https://u@api.anthropic.com/v1/messages")
        with self.assertRaisesRegex(AnthropicError, "user name or password"):
            AnthropicClient("https://u:p@api.anthropic.com/v1", "k")


class TheReply(unittest.TestCase):
    def test_a_stream_reads_back_as_openais_and_the_app_reads_it(self):
        lines = list(openai_lines(STREAM))
        self.assertEqual(lines[-1], b"data: [DONE]")
        result = LLMClient._consume(iter(lines), None, None, [])
        self.assertEqual((result["text"], result["finish_reason"], result["done"],
                          result["id"]), ("A cat.", "stop", True, "msg_1"))
        reasoning = [json.loads(line[6:])["choices"][0]["delta"].get("reasoning")
                     for line in lines if b'"reasoning"' in line]
        self.assertEqual(reasoning, ["Hm."])
        usage = next(json.loads(line[6:])["usage"] for line in lines if b'"usage"' in line)
        self.assertEqual((usage["prompt_tokens"], usage["completion_tokens"]), (100, 7))

    def test_stop_reasons_by_the_apps_names(self):
        for stop, finish in (("max_tokens", "length"), ("stop_sequence", "stop"),
                             ("refusal", "refusal")):
            with self.subTest(stop=stop):
                lines = list(openai_lines(sse(
                    {"type": "message_start", "message": {"id": "m"}},
                    {"type": "message_delta", "delta": {"stop_reason": stop}},
                    {"type": "message_stop"})))
                self.assertEqual(LLMClient._consume(iter(lines), None, None,
                                                    [])["finish_reason"], finish)

    def test_a_stream_cut_off_has_no_end(self):
        lines = list(openai_lines(STREAM[:-6]))
        self.assertNotIn(b"data: [DONE]", lines)
        self.assertFalse(LLMClient._consume(iter(lines), None, None, [])["done"])

    def test_an_error_event_is_the_replys_error_on_one_line(self):
        lines = list(openai_lines(sse(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}},
            {"type": "error", "error": {"type": "overloaded_error",
                                        "message": "Overloaded\nnow\u202e"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta",
                                                      "text": "never"}})))
        result = LLMClient._consume(iter(lines), None, None, [])
        self.assertEqual(result["text"], "Hi")
        self.assertEqual(result["error"],
                         "Anthropic reported an error: overloaded_error: Overloaded now")

    def test_chutes_proxys_own_error_text_is_never_kept_as_the_models(self):
        text = anthropic.PROXY_ERROR_TEXT + ": backend said no"
        shown = []
        lines = list(openai_lines(sse(
            {"type": "content_block_start", "content_block": {"type": "text"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
            {"type": "message_delta", "delta": {"stop_reason": "error"}}), "Chutes' proxy"))
        result = LLMClient._consume(iter(lines), None,
                                    lambda piece, whole: shown.append(piece), [])
        self.assertEqual(shown, [])
        self.assertIn("Chutes' proxy ended the reply with an error of its own",
                      result["error"])
        self.assertIn("(unauthenticated): : backend said no", result["error"])
        # The same words, then a proper end: the model's after all.
        lines = list(openai_lines(sse(
            {"type": "content_block_start", "content_block": {"type": "text"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"})))
        self.assertEqual(LLMClient._consume(iter(lines), None, None, [])["text"], text)

    def test_a_reply_sent_whole_reads_as_openais(self):
        reply = anthropic.openai_reply({"id": "m", "stop_reason": "max_tokens",
                                        "content": [{"type": "thinking", "thinking": "x"},
                                                    {"type": "text", "text": "Hi"}],
                                        "usage": {"input_tokens": 3, "output_tokens": 1}})
        self.assertEqual(reply["choices"][0]["message"]["content"], "Hi")
        self.assertEqual(reply["choices"][0]["finish_reason"], "length")


class Response:
    def __init__(self, status, body=b"", headers=None):
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers or {})
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode()

    def json(self):
        return safejson.loads(self.content)

    def iter_content(self, chunk_size=512):
        for at in range(0, len(self.content), 5):
            yield self.content[at:at + 5]

    def close(self):
        pass


class Session:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def post(self, url, data=None, headers=None, **kwargs):
        self.asked.append(("POST", url, headers, data, kwargs))
        return self.answer(url, kwargs)

    def get(self, url, params=None, headers=None, **kwargs):
        self.asked.append(("GET", url, headers, params, kwargs))
        return self.answer(url, {**kwargs, "params": params})

    def close(self):
        pass


class TheClient(unittest.TestCase):
    def test_a_request_streams_and_follows_no_redirect(self):
        session = Session(lambda url, kw: Response(200, b"\n".join(STREAM)))
        client = AnthropicClient("https://api.anthropic.com", "sk-ant", session=session)
        with client.request(PAYLOAD) as response:
            result = LLMClient._consume(client.lines(response), None, None, [])
        self.assertEqual(result["text"], "A cat.")
        method, url, sent, data, kwargs = session.asked[0]
        self.assertEqual((method, url), ("POST", "https://api.anthropic.com/v1/messages"))
        self.assertEqual(sent["x-api-key"], "sk-ant")
        self.assertTrue(kwargs["stream"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(json.loads(data)["model"], "claude-opus-5-5")

    def test_a_refusal_or_a_redirect_is_said(self):
        for status, body, why in ((401, {"error": {"message": "invalid x-api-key"}},
                                   "HTTP 401: invalid x-api-key"),
                                  (307, b"", "redirected")):
            with self.subTest(status=status):
                session = Session(lambda url, kw: Response(
                    status, body, {"location": "https://elsewhere.example/"}))
                client = AnthropicClient("https://api.anthropic.com", "k", session=session)
                with self.assertRaisesRegex(AnthropicError, why):
                    with client.request(PAYLOAD):
                        pass

    def test_a_model_only_ever_sent_sealed_is_never_sent_here(self):
        session = Session(lambda url, kw: self.fail("sent"))
        for url, model in (("https://claude.chutes.ai", "a/b-TEE"),
                           ("https://api.anthropic.com", "private/x"),
                           ("https://inference.tinfoil.sh", "claude-x")):
            with self.subTest(url=url, model=model):
                client = AnthropicClient(url, "k", session=session)
                with self.assertRaisesRegex(AnthropicError, "nothing was sent"):
                    with client.request({**PAYLOAD, "model": model}):
                        pass
        self.assertEqual(session.asked, [])

    def test_only_https_and_a_clean_key(self):
        with self.assertRaises(AnthropicError):
            AnthropicClient("http://api.anthropic.com", "k")
        with self.assertRaises(AnthropicError):
            AnthropicClient("https://api.anthropic.com", "k\n")

    def test_the_listing_reads_every_page(self):
        pages = {None: {"data": [{"id": "claude-opus-5-5", "display_name": "Opus 5.5"}],
                        "has_more": True, "last_id": "claude-opus-5-5"},
                 "claude-opus-5-5": {"data": [{"id": "claude-sonnet-4-5"}], "has_more": False}}
        session = Session(lambda url, kw: Response(
            200, pages[(kw.get("params") or {}).get("after_id")]))
        found = AnthropicClient("https://api.anthropic.com", "k", session=session).models()
        self.assertEqual(set(found), {"claude-opus-5-5", "claude-sonnet-4-5"})
        self.assertEqual(found["claude-opus-5-5"]["name"], "Opus 5.5")
        self.assertIn("xhigh", found["claude-opus-5-5"]["reasoning_efforts"])
        self.assertNotIn("xhigh", found["claude-sonnet-4-5"]["reasoning_efforts"])
        self.assertTrue(all(asked[2]["x-api-key"] == "k" for asked in session.asked))

    def test_on_chutes_the_listing_is_chutes_public_one_without_the_key(self):
        listing = {"data": [{"id": "zai-org/GLM-5.3",
                             "chute_id": "9ecb73cc-909b-5334-9cd8-74985945cff5"}]}
        session = Session(lambda url, kw: Response(200, listing))
        found = AnthropicClient("https://claude.chutes.ai", "cpk", session=session).models()
        self.assertEqual(set(found), {"zai-org/GLM-5.3"})
        self.assertEqual(session.asked[0][1], "https://llm.chutes.ai/v1/models")
        self.assertFalse(session.asked[0][2])


if __name__ == "__main__":
    unittest.main()
