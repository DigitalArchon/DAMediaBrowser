# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Anthropic's own Messages API, natively: POST {origin}/v1/messages with the key as
`x-api-key`, streamed, an answer read only so far. And Chutes' own open-source proxy of
it (claude.chutes.ai, github.com/chutesai/claude-proxy), which takes the key as a bearer
token instead.

SealedLore's providers/anthropic.py and anthropic_models.py, in this app's shape: the
request is built from the OpenAI-shaped payload the app builds for every model
(`messages_body`), and the reply's SSE is read back as OpenAI's stream (`openai_lines`),
so the app reads one shape whoever answered. Shared with DA Assistant line for line
(this copy differs only in this docstring, its imports and the hooks
tests/test_shared_code.py lists); the HTTP is the caller's, or `AnthropicClient` with a
requests-like Session (netguard.Session(): no redirect followed, an answer read only so
far, the environment's proxy honoured).

- The request: the system messages as `system` blocks; user and assistant turns strictly
  alternating (consecutive messages of one role are one message; a conversation that
  begins with the model's message is refused before anything is sent); text and
  pictures (data URLs) as blocks; `max_tokens` on every request; the last user turn
  marked for Anthropic's prompt cache when asked (`cache_ttl` "5m" or "1h", the one-hour
  marker with its beta header). Extended thinking is shaped for the model's generation
  (`thinking_fields`; the API refuses the wrong shape with a 400): through Claude 4.5 a
  token budget, from 4.6 adaptive thinking with an effort, from 4.7 no sampling, from 5
  (and Fable, Mythos) thinking always on, so the least is effort "low".
- The reply: Anthropic's SSE (message_start, content blocks of text or thinking as
  deltas, message_delta with the stop reason and usage, message_stop) as OpenAI's
  chunks, `[DONE]` only on message_stop; end_turn and stop_sequence are "stop",
  max_tokens "length", anything else kept by its own name. An `error` event is the
  reply's error. Chutes' proxy says a refusal of its backend mid-stream as text of its
  own ("⚠️ Backend Error", then the stop reason "error", one Anthropic never gives): held
  back until the stop reason says what it was, and then said as the proxy's,
  unauthenticated, never kept as the model's.
- Usage: Anthropic's `input_tokens` exclude cached tokens, so `prompt_tokens` is input +
  cache creation + cache read. No prices: Anthropic's listing names none.
- The listing: GET /v1/models, every page (ten at most). On Chutes, Chutes' public list,
  without the key (chutes.fetch_models).
- What is never sent this way: a model only ever sent sealed - one of Chutes' -TEE models
  on Chutes' hosts, a private/ one, anything on Tinfoil's - is refused in words
  (levels.plain_refusal), and nothing goes.
"""

import json
import re
from contextlib import contextmanager
from urllib.parse import urljoin, urlparse

from . import levels, netguard, safejson, services
from .private_ai import unsealed_words

API_VERSION = "2023-06-01"
EXTENDED_TTL_BETA = "extended-cache-ttl-2025-04-11"
MAX_LISTING_PAGES = 10
LISTING_PAGE = 100
LISTING_TIMEOUT_SECONDS = 30
DEFAULT_MAX_TOKENS = 4000
PROXY_ERROR_TEXT = "⚠️ Backend Error"
_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}


class AnthropicError(Exception):
    """A request to Anthropic's API (or Chutes' proxy of it) was refused or failed."""


# ---- what each generation takes (anthropic_models.py) ---------------------------------

FAMILIES = ("opus", "sonnet", "haiku", "fable", "mythos")
ALWAYS_THINKING = ("fable", "mythos")
_EFFORTS = {"none": "low", "minimal": "low", "low": "low", "medium": "medium",
            "high": "high", "xhigh": "xhigh", "max": "max"}
BUDGETS = {"low": 1024, "medium": 4096, "high": 16000, "xhigh": 32000, "max": 32000}
_NUMBER = re.compile(r"^\d+$")


def parse_model(model):
    """A model id's family and version: ("sonnet", (4, 6)) for "claude-sonnet-4-6",
    ("opus", (4, 1)) for "claude-opus-4-1-20250805". Unknown parts read as "" and
    (0, 0)."""
    name = str(model or "").strip().lower().rsplit("/", 1)[-1]
    tokens = re.split(r"[-.]", name)
    family = next((token for token in tokens if token in FAMILIES), "")
    numbers = [int(token) for token in tokens if _NUMBER.match(token) and len(token) < 8]
    return family, (numbers[0] if numbers else 0, numbers[1] if len(numbers) > 1 else 0)


def surface_of(model):
    """(adaptive, samples, xhigh, thinks_by_default, quiet) for `model`: what its requests
    may carry. An id that cannot be read is taken as the newest shape."""
    family, version = parse_model(model)
    if family in ALWAYS_THINKING:
        return True, False, True, True, True
    if not family and version == (0, 0):
        return True, False, True, False, True
    if version < (4, 6):
        return False, True, False, False, False
    if version < (4, 7):
        return True, True, False, False, False
    return True, False, True, version >= (5, 0), True


def thinking_fields(model, effort, max_tokens):
    """(fields, max_tokens, sampling): what a request for `model` carries for the
    reasoning `effort` ("" or None for none asked), the max_tokens that goes with it, and
    whether sampling settings may go too. Nothing asked sends nothing, but on a model
    whose thinking cannot be turned off: there the least is effort "low"."""
    adaptive, samples, xhigh, thinks_by_default, quiet = surface_of(model)
    limit = max_tokens or DEFAULT_MAX_TOKENS
    effort = str(effort or "").lower()
    if effort in ("", "none"):
        if thinks_by_default:
            return {"output_config": {"effort": "low"}}, limit, False
        return {}, limit, samples
    level = _EFFORTS.get(effort, "high")
    if adaptive:
        if level == "xhigh" and not xhigh:
            level = "high"
        thinking = {"type": "adaptive"}
        if quiet:
            thinking["display"] = "summarized"
        return {"thinking": thinking, "output_config": {"effort": level}}, limit, False
    budget = BUDGETS[level]
    return {"thinking": {"type": "enabled", "budget_tokens": budget}}, budget + limit, False


def efforts_of(model):
    """The levels to list for `model`, in the app's words."""
    return ["low", "medium", "high", *(["xhigh"] if surface_of(model)[2] else []), "max"]


# ---- the request ------------------------------------------------------------------------

def origin(api_url):
    """The API's root, with or without the /v1 (or /v1/messages) typed after it, and no
    user name or password."""
    parsed = urlparse(netguard.without_password(str(api_url or "").strip()))
    path = parsed.path.rstrip("/")
    for suffix in ("/messages", "/v1"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def messages_url(api_url):
    return origin(api_url) + "/v1/messages"


def headers(api_url, api_key, cache_ttl=None):
    """The request's headers: the key as `x-api-key`, or on Chutes as a bearer token (its
    proxy took Anthropic's own header as no key at all, live, Nov 15 2026)."""
    found = {"content-type": "application/json", "anthropic-version": API_VERSION,
             "accept": "text/event-stream",
             "accept-encoding": netguard.ACCEPT_ENCODING}
    if api_key:
        if services.on_chutes(api_url):
            found["authorization"] = f"Bearer {api_key}"
        else:
            found["x-api-key"] = api_key
    if cache_ttl == "1h":
        found["anthropic-beta"] = EXTENDED_TTL_BETA
    return found


def _picture(url):
    """A picture sent as data (a data URL) as the Messages API takes one; None for one
    that is not data (a link, which this app never sends)."""
    head, _, data = str(url or "").partition(",")
    if not head.startswith("data:") or ";base64" not in head or not data:
        return None
    media_type = head[len("data:"):].split(";")[0] or "image/png"
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": data}}


def _blocks(content):
    """A message's content (text, or OpenAI's list of parts) as Messages API blocks. An
    empty text makes no block: the API refuses one."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and part.get("text"):
            blocks.append({"type": "text", "text": str(part["text"])})
        elif part.get("type") == "image_url":
            image = part.get("image_url")
            picture = _picture(image.get("url") if isinstance(image, dict) else image)
            if picture is not None:
                blocks.append(picture)
    return blocks


def messages_body(payload, cache_ttl=None):
    """The /v1/messages body for the app's OpenAI-shaped `payload` (model, messages,
    max_tokens, reasoning, stop, temperature, top_p), streamed. Raises AnthropicError,
    before anything is sent, for a conversation that does not begin with a user's
    message. `cache_ttl` ("5m" or "1h") marks the last user turn for the prompt cache."""
    model = str(payload.get("model") or "").strip()
    system, wire = [], []
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        blocks = _blocks(message.get("content"))
        if role == "system":
            system.extend(block for block in blocks if block["type"] == "text")
            continue
        if role not in ("user", "assistant") or not blocks:
            continue
        if wire and wire[-1]["role"] == role:
            wire[-1]["content"].extend(blocks)
        else:
            wire.append({"role": role, "content": blocks})
    if not wire or wire[0]["role"] != "user":
        raise AnthropicError("Anthropic's API needs the conversation to begin with a "
                             "user's message, and this one does not; nothing was sent")
    if cache_ttl in ("5m", "1h"):
        last = max(i for i, message in enumerate(wire) if message["role"] == "user")
        marker = {"type": "ephemeral"} if cache_ttl == "5m" else {"type": "ephemeral",
                                                                   "ttl": "1h"}
        wire[last]["content"][-1] = {**wire[last]["content"][-1], "cache_control": marker}
    reasoning = payload.get("reasoning")
    effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    fields, limit, sampling = thinking_fields(model, effort, payload.get("max_tokens"))
    body = {"model": model, "max_tokens": limit, "messages": wire, "stream": True}
    if system:
        body["system"] = system
    if sampling:
        if payload.get("temperature") is not None:
            body["temperature"] = payload["temperature"]
        elif payload.get("top_p") is not None:
            body["top_p"] = payload["top_p"]
    stop = payload.get("stop")
    if stop:
        body["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    body.update(fields)
    return body


def refusal(api_url, model):
    """Why `model` must not be sent to `api_url` through this client, which sends in the
    clear, or None (levels.plain_refusal)."""
    return levels.plain_refusal(api_url, "this conversation", model=model)


# ---- the reply --------------------------------------------------------------------------

def app_usage(raw):
    """Anthropic's usage with OpenAI's two names beside its own: `prompt_tokens` counts
    the prompt whole, cached tokens included."""
    def count(key):
        value = raw.get(key)
        return int(value) if isinstance(value, (int, float)) and \
            not isinstance(value, bool) else 0
    return {**raw, "prompt_tokens": count("input_tokens")
            + count("cache_creation_input_tokens") + count("cache_read_input_tokens"),
            "completion_tokens": count("output_tokens")}


def _line(chunk):
    return b"data: " + json.dumps(chunk).encode()


def _delta(**delta):
    return _line({"choices": [{"index": 0, "delta": delta}]})


def openai_lines(lines, origin_name="Anthropic"):
    """Anthropic's SSE lines (bytes or text) as an OpenAI stream's (bytes): each text or
    thinking delta a chunk of its own, the stop reason as the finish reason with the
    usage, `[DONE]` on message_stop and never before. An error event is the stream's
    error, and nothing after it is read."""
    usage, response_id, held, block_has_text = {}, None, None, False
    for raw in lines:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if not line.startswith("data:"):
            continue            # "event:" lines name what the data line says itself
        data = line[len("data:"):].strip()
        if not data:
            continue
        try:
            event = safejson.loads(data)
        except ValueError:
            yield _line({"error": {"message": "the reply carried an event that is not "
                                              "JSON"}})
            return
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "error":
            error = event.get("error")
            detail = error if isinstance(error, dict) else {"message": str(error)}
            said = unsealed_words(f"{detail.get('type', 'error')}: "
                                  f"{detail.get('message', '')}", 300).rstrip(": ")
            yield _line({"error": {"message": f"{origin_name} reported an error: {said}"}})
            return
        if kind == "message_start":
            message = event.get("message") if isinstance(event.get("message"), dict) \
                else {}
            if isinstance(message.get("id"), str):
                response_id = message["id"]
                yield _line({"id": response_id, "choices": []})
            if isinstance(message.get("usage"), dict):
                usage.update(message["usage"])
        elif kind == "content_block_start":
            block_has_text = False
        elif kind == "content_block_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            if delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    if held is not None or (not block_has_text
                                            and text.startswith(PROXY_ERROR_TEXT)):
                        held = (held or "") + text      # perhaps the proxy's error
                    else:
                        yield _delta(content=text)
                    block_has_text = True
            elif delta.get("type") == "thinking_delta":
                thinking = delta.get("thinking")
                if isinstance(thinking, str) and thinking:
                    yield _delta(reasoning=thinking)
        elif kind == "message_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            if isinstance(event.get("usage"), dict):
                usage.update(event["usage"])
            stop = delta.get("stop_reason")
            if stop == "error":
                said = unsealed_words((held or "")[len(PROXY_ERROR_TEXT):], 300)
                yield _line({"error": {"message": (
                    f"{origin_name} ended the reply with an error of its own, not the "
                    f"model's words" + (f"; it said (unauthenticated): {said}"
                                        if said else ""))}})
                return
            if held is not None:
                yield _delta(content=held)
                held = None
            if isinstance(stop, str):
                yield _line({"choices": [{"index": 0, "delta": {},
                                          "finish_reason": _FINISH.get(stop, stop)}],
                             "usage": app_usage(usage)})
        elif kind == "message_stop":
            if held is not None:
                yield _delta(content=held)
            yield b"data: [DONE]"
            return


def openai_reply(body):
    """A Messages API reply sent whole, in OpenAI's shape (choices[0].message.content,
    finish_reason, usage)."""
    if not isinstance(body, dict):
        raise AnthropicError("the reply was not a JSON object")
    if isinstance(body.get("error"), dict):
        raise AnthropicError(f"Anthropic reported an error: "
                             f"{unsealed_words(body['error'].get('message'), 300)}")
    text = "".join(block.get("text", "") for block in body.get("content") or []
                   if isinstance(block, dict) and block.get("type") == "text")
    stop = body.get("stop_reason")
    return {"id": body.get("id"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": _FINISH.get(stop, stop)}],
            "usage": app_usage(body.get("usage") or {})}


def error_text(body):
    """The `error.message` of an Anthropic error body, as it may be shown, or ""."""
    try:
        data = safejson.loads(body)
    except ValueError:
        return unsealed_words(bytes(body or b"").decode("utf-8", "replace"), 300)
    error = data.get("error") if isinstance(data, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return unsealed_words(message, 300) if isinstance(message, str) else ""


# ---- the listing ------------------------------------------------------------------------

def listing_entry(item):
    """A /v1/models entry as the app's model list holds one: Anthropic's fields, the names
    the app reads beside them, and the levels of thinking it takes. No prices."""
    entry = dict(item)
    entry.setdefault("name", item.get("display_name", ""))
    entry.setdefault("owned_by", "anthropic")
    entry.setdefault("context_length", item.get("max_input_tokens"))
    entry.setdefault("max_output_tokens", item.get("max_tokens"))
    entry["reasoning_efforts"] = efforts_of(str(item.get("id", "")))
    return entry


def list_models(session, api_url, api_key):
    """Every model the API lists, by id, every page (MAX_LISTING_PAGES at most); on
    Chutes, Chutes' public list, without the key. Raises AnthropicError."""
    if services.on_chutes(api_url):
        from . import chutes
        from . import private_ai as privacy
        try:
            return chutes.fetch_models(session)
        except privacy.PrivacyError as e:
            raise AnthropicError(str(e)) from e
    url = origin(api_url) + "/v1/models"
    found, after = {}, None
    for _page in range(MAX_LISTING_PAGES):
        params = {"limit": LISTING_PAGE}
        if after:
            params["after_id"] = after
        try:
            response = session.get(url, params=params, headers=headers(api_url, api_key),
                                   timeout=LISTING_TIMEOUT_SECONDS, allow_redirects=False)
        except netguard.RequestError as e:
            raise AnthropicError(f"the models list could not be fetched: "
                                 f"{unsealed_words(e, 300)}") from e
        if response.status_code >= 300:
            raise AnthropicError(f"the models list answered HTTP {response.status_code}"
                                 + (f": {error_text(response.content)}"
                                    if response.status_code >= 400 else ""))
        try:
            data = response.json()
        except ValueError as e:
            raise AnthropicError("the models list was not JSON") from e
        items = data.get("data") if isinstance(data, dict) else None
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                found[item["id"]] = listing_entry(item)
        last = data.get("last_id") if isinstance(data, dict) else None
        if not (isinstance(data, dict) and data.get("has_more") and isinstance(last, str)):
            break
        after = last
    return found


class AnthropicClient:
    """One endpoint speaking Anthropic's Messages API, over `session` (a requests-like
    Session that follows no redirect and reads an answer only so far)."""

    def __init__(self, api_url, api_key, session=None, request_timeout=180):
        insecure = netguard.problem(messages_url(api_url))
        if insecure:
            raise AnthropicError(insecure)
        unfit = netguard.key_problem(api_key, "Anthropic")
        if unfit:
            raise AnthropicError(unfit)
        self.api_url = api_url
        self.api_key = api_key
        self.session = session or netguard.Session()
        self.request_timeout = request_timeout

    @property
    def endpoint(self):
        return messages_url(self.api_url)

    @contextmanager
    def request(self, payload, cache_ttl=None):
        """Send one streamed request; yields the open response (its `openai_lines` are
        the app's). Refused before anything is sent for a model sent only sealed, and for
        a conversation that does not begin with a user's message."""
        refused = refusal(self.api_url, payload.get("model"))
        if refused:
            raise AnthropicError(refused)
        body = messages_body(payload, cache_ttl)
        try:
            response = self.session.post(self.endpoint, data=json.dumps(body).encode(),
                                         headers=headers(self.api_url, self.api_key,
                                                         cache_ttl),
                                         stream=True, allow_redirects=False,
                                         timeout=(10, self.request_timeout))
        except netguard.RequestError as e:
            raise AnthropicError(f"the request failed: {unsealed_words(e, 300)}") from e
        try:
            if 300 <= response.status_code < 400:
                location = response.headers.get("location")
                raise AnthropicError(netguard.redirected(
                    self.endpoint, urljoin(self.endpoint, location) if location else "",
                    response.status_code))
            if response.status_code >= 400:
                said = error_text(response.content)
                raise AnthropicError(f"HTTP {response.status_code}"
                                     + (f": {said}" if said else ""))
            yield response
        finally:
            response.close()

    def lines(self, response):
        """The reply as an OpenAI stream's lines (bytes)."""
        from .netguard import lines_of as read_lines
        name = "Chutes' proxy" if services.on_chutes(self.api_url) else "Anthropic"
        return openai_lines(read_lines(response.iter_content(chunk_size=512)), name)

    def models(self):
        return list_models(self.session, self.api_url, self.api_key)
