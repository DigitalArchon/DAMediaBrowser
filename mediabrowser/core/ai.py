# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Talking to a language model through Nano-GPT.

Nano-GPT is a pay-as-you-go gateway that speaks the OpenAI chat-completions
dialect and fronts the Claude models (among hundreds of others), which is
what lets this app send a few frames of a concert and a tracklist to a
model that can read the song caption on screen, recall a setlist, and
translate a title - without a subscription to anyone.

This module is only the wire: settings, one request, one reply. What to
ask is ai_chapters' business. The API key lives in the OS keyring (see
creds), never in settings.json; NANOGPT_API_KEY in the environment wins
over it. Nothing is sent unless Settings → Privacy allows the AI, and no
web search is asked for unless it allows that too (see privacy).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse

from . import creds, privacy, store

DEFAULT_BASE_URL = "https://nano-gpt.com/api/v1"
NANO_GPT_HOST = "nano-gpt.com"
# Sonnet reads captions and knows setlists well enough, at a fraction of
# Opus's price; the model is a setting for anyone who wants to spend more.
DEFAULT_MODEL = "anthropic/claude-sonnet-5"
# Frames per chapter start sent to the model. Two catches a song caption
# that appears a few seconds in or a dozen.
DEFAULT_FRAMES_PER_CHAPTER = 2

API_KEY_ENV = "NANOGPT_API_KEY"
# The key's name in the keyring.
KEYRING_NAME = "nanogpt-api-key"

SETTING_KEY = "ai_api_key"
SETTING_MODEL = "ai_model"
SETTING_BASE_URL = "ai_base_url"
SETTING_FRAMES = "ai_frames_per_chapter"
SETTING_SEARCH = "ai_search"

# Who runs the web search when the model is asked to look a show up, as
# Nano-GPT names them in a model's ":online/<provider>" suffix. Kagi and
# Perplexity find a setlist.fm page for a show where LinkUp (Nano-GPT's
# default) often doesn't.
SEARCH_PROVIDERS = {
    "kagi": "Kagi",
    "perplexity": "Perplexity",
    "perplexity-deep": "Perplexity (deep)",
    "linkup": "LinkUp",
    "linkup-deep": "LinkUp (deep)",
}
DEFAULT_SEARCH = "kagi"

DEFAULT_SETTINGS = {
    SETTING_KEY: "",
    SETTING_MODEL: DEFAULT_MODEL,
    SETTING_BASE_URL: DEFAULT_BASE_URL,
    SETTING_FRAMES: DEFAULT_FRAMES_PER_CHAPTER,
    SETTING_SEARCH: DEFAULT_SEARCH,
}

# A reply for a long concert is a few thousand tokens; this is headroom,
# not a target.
MAX_REPLY_TOKENS = 8192
# How hard the model may think before answering. Claude models on Nano-GPT
# think by default, and on a confusing request one spent all 8,192 of its
# tokens thinking - 90 seconds and 8 cents for no answer. "low" still
# thinks a little; reading and naming don't need more.
REASONING_EFFORT = "low"
# Sending forty images and waiting on a big model can take a while.
TIMEOUT_SECONDS = 240


class AIError(Exception):
    pass


class NotConfigured(AIError):
    """No API key: nothing can be asked until one is set."""


class SearchUnavailable(AIError):
    """The chosen web search provider is refused for this account: Nano-GPT
    allows only LinkUp when Zero Data Retention is switched on."""


def settings_from(app_settings: dict) -> dict:
    """The AI settings in force: the stored ones over the defaults, with
    the environment's key over the stored one."""
    settings = dict(DEFAULT_SETTINGS)
    for key in DEFAULT_SETTINGS:
        if app_settings.get(key) not in (None, ""):
            settings[key] = app_settings[key]
    env_key = os.environ.get(API_KEY_ENV, "").strip()
    if env_key:
        settings[SETTING_KEY] = env_key
    settings[SETTING_BASE_URL] = str(settings[SETTING_BASE_URL]).rstrip("/")
    try:
        settings[SETTING_FRAMES] = max(1, min(4, int(settings[SETTING_FRAMES])))
    except (TypeError, ValueError):
        settings[SETTING_FRAMES] = DEFAULT_FRAMES_PER_CHAPTER
    if settings[SETTING_SEARCH] not in SEARCH_PROVIDERS:
        settings[SETTING_SEARCH] = DEFAULT_SEARCH
    for choice in (privacy.AI, privacy.WEB_SEARCH):
        settings[choice] = privacy.allowed(choice, app_settings)
    return settings


def load_settings() -> dict:
    """The AI settings in force: settings.json's, with the key from the
    keyring. A keyring that can't be reached, or stays locked, just means
    no key - the environment's, if it has one, doesn't need it."""
    app_settings = store.load_app_settings()
    app_settings[SETTING_KEY] = "" if os.environ.get(API_KEY_ENV, "").strip() else stored_key()
    return settings_from(app_settings)


def stored_key() -> str:
    try:
        return creds.get_secret(KEYRING_NAME) or ""
    except Exception:  # noqa: BLE001 - keyring locked or unavailable
        return ""


def store_key(key: str) -> None:
    """Keep the key in the keyring, or forget it when empty. Raises when
    the keyring can't be written."""
    if key:
        creds.set_secret(KEYRING_NAME, key)
    else:
        creds.delete_secret(KEYRING_NAME)


def allowed(settings: dict) -> bool:
    """Whether Settings → Privacy lets anything be sent to the AI."""
    return bool(settings.get(privacy.AI, privacy.DEFAULTS[privacy.AI]))


def is_configured(settings: dict) -> bool:
    """Whether the AI may be asked: it's allowed, and there's a key."""
    return allowed(settings) and bool(settings.get(SETTING_KEY))


def not_ready(settings: dict) -> str:
    """Why the AI can't be asked, for saying so; empty when it can."""
    if not allowed(settings):
        return privacy.OFF[privacy.AI]
    if not settings.get(SETTING_KEY):
        return "Set a Nano-GPT API key in Settings → AI."
    return ""


def short_model_name(model: str) -> str:
    """"anthropic/claude-sonnet-5:online/kagi" as "claude-sonnet-5"."""
    return model.split(":", 1)[0].rsplit("/", 1)[-1]


# --- message parts -----------------------------------------------------------


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def image_part(jpeg: bytes) -> dict:
    data = base64.b64encode(jpeg).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}


# --- requests ------------------------------------------------------------------


# Services on this machine may be plain http: a local model server, say.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def endpoint_problem(url: str) -> str:
    """Why the key mustn't be sent to `url`, or empty when it may: the
    endpoint has to be https, so the key crosses the network encrypted -
    unless the service is on this machine."""
    parts = urlparse(str(url or ""))
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host:
        return ""
    if parts.scheme == "http" and host in LOCAL_HOSTS:
        return ""
    return (f"the endpoint {url} isn't https, so the key would be sent in the clear "
            "(only a service on this machine may be plain http)")


def _origin(url: str) -> tuple:
    parts = urlparse(url)
    return (parts.scheme.lower(), (parts.hostname or "").lower(), parts.port)


class _KeysStayHome(urllib.request.HTTPRedirectHandler):
    """urllib follows a redirect with every header it was given, the key
    included - so a server answering with a redirect to another host would
    be handed it. A redirect to another origin is followed without the
    Authorization and API-key headers."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(newurl) != _origin(req.full_url):
            for name in ("Authorization", "X-api-key"):
                new.remove_header(name)
        return new


urllib.request.install_opener(urllib.request.build_opener(_KeysStayHome))


def _without_frames(messages) -> list[dict]:
    """The messages with their pictures taken out: asking again about an
    answer already given needs no second look at the video, nor a second
    payment for the frames."""
    stripped = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            content = [
                text_part("(a frame of the video, shown before)")
                if isinstance(part, dict) and part.get("type") == "image_url" else part
                for part in content
            ]
            message = {**message, "content": content}
        stripped.append(message)
    return stripped


def _request(url: str, api_key: str | None, body: dict | None = None, timeout=TIMEOUT_SECONDS):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        problem = endpoint_problem(url)
        if problem:
            raise AIError(problem)
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise AIError(_http_error_message(exc)) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AIError(str(exc.reason if hasattr(exc, "reason") else exc) or repr(exc)) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AIError(f"the reply wasn't JSON: {exc}") from exc


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    """What went wrong, from the body when it says (Nano-GPT returns
    {"error": {"message": ...}}) and from the status when it doesn't."""
    detail = ""
    try:
        body = json.loads(exc.read().decode("utf-8"))
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            detail = error.get("message") or ""
        elif isinstance(error, str):
            detail = error
    except Exception:
        pass
    hints = {
        401: "the API key was refused",
        402: "the Nano-GPT balance is empty",
        403: "the API key was refused",
        404: "no such endpoint or model",
        429: "rate limited - try again in a moment",
    }
    text = hints.get(exc.code, f"HTTP {exc.code}")
    return f"{text}: {detail}" if detail else text


# Nano-GPT runs a web search for the model when this is on the model's name,
# with the provider after a slash.
ONLINE_SUFFIX = ":online"


def searches_the_web(settings: dict) -> bool:
    """Whether the endpoint can search the web for the model. The
    ":online" suffix is Nano-GPT's own; any other OpenAI-compatible service
    would take it for part of a model's name and refuse every request."""
    host = urlparse(str(settings.get(SETTING_BASE_URL) or "")).hostname or ""
    return host == NANO_GPT_HOST or host.endswith("." + NANO_GPT_HOST)


def may_search(settings: dict) -> bool:
    """Whether the model may have the web searched for it: the endpoint
    can, and Settings → Privacy allows it."""
    return searches_the_web(settings) and bool(
        settings.get(privacy.WEB_SEARCH, privacy.DEFAULTS[privacy.WEB_SEARCH])
    )


def model_name(settings: dict, online: bool = False) -> str:
    model = settings[SETTING_MODEL]
    if not may_search(settings):
        # Not even when the model was set with the suffix already on.
        return model.split(ONLINE_SUFFIX, 1)[0] if searches_the_web(settings) else model
    if online and ONLINE_SUFFIX not in model:
        model += f"{ONLINE_SUFFIX}/{settings.get(SETTING_SEARCH) or DEFAULT_SEARCH}"
    return model


def chat(messages, settings: dict, *, json_reply: bool = True, online: bool = False,
         max_tokens: int = MAX_REPLY_TOKENS, temperature: float = 0.0,
         reasoning: str = REASONING_EFFORT) -> str:
    """One chat completion; returns the assistant's text.

    `messages` is the OpenAI shape: [{"role", "content"}], content a string
    or a list of parts (text_part / image_part). With json_reply the model
    is asked for a JSON object, which every Claude model honours. With
    `online` Nano-GPT searches the web for it first - the difference
    between guessing a show's setlist and reading it off setlist.fm.
    """
    if not is_configured(settings):
        raise NotConfigured(not_ready(settings))
    body = {
        "model": model_name(settings, online),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
        "reasoning_effort": reasoning,
    }
    if json_reply:
        body["response_format"] = {"type": "json_object"}
    try:
        reply = _request(
            f"{settings[SETTING_BASE_URL]}/chat/completions", settings[SETTING_KEY], body
        )
    except AIError as exc:
        if online and "web search provider" in str(exc).lower():
            raise SearchUnavailable(str(exc)) from exc
        raise
    return reply_text(reply)


def with_search(settings: dict, provider: str) -> dict:
    return {**settings, SETTING_SEARCH: provider}


def reply_text(reply) -> str:
    """The assistant's text out of a chat-completions reply."""
    if not isinstance(reply, dict):
        raise AIError("unexpected reply")
    error = reply.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise AIError(message or "the request failed")
    choices = reply.get("choices") or []
    if not choices:
        raise AIError("the model returned no reply")
    message = choices[0].get("message") or {}
    if choices[0].get("finish_reason") == "length" and not (message.get("content") or "").strip():
        raise AIError("the model ran out of room before it answered - try again, or "
                      "a bigger model in Settings → AI")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str) or not content.strip():
        raise AIError("the model returned an empty reply")
    return content


def chat_json(messages, settings: dict, **kwargs) -> dict:
    """chat(), parsed as a JSON object - asking once more, with what was
    wrong, if it isn't one. Claude has no JSON mode of its own, and a song
    title with a quote in it ("Road of Resistance" in quotes, say) now and
    then comes back unescaped."""
    text = chat(messages, settings, **kwargs)
    try:
        return parse_json_reply(text)
    except AIError as exc:
        retry = [*_without_frames(messages), {"role": "assistant", "content": text},
                 {"role": "user", "content": (
                     f"That isn't valid JSON ({exc}). Send the same answer again as one "
                     "valid JSON object, escaping any double quotes inside strings, and "
                     "nothing else."
                 )}]
        return parse_json_reply(chat(retry, settings, **kwargs))


def parse_json_reply(text: str) -> dict:
    """The JSON object in a reply, tolerating a model that wrapped it in a
    ``` fence or said a few words first."""
    text = text.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise AIError("the model's reply wasn't JSON") from None
        try:
            value = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise AIError(f"the model's reply wasn't valid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise AIError("the model's reply wasn't a JSON object")
    return value


# The models worth suggesting, and what to say about them. Every AI part of
# this app was tried out with the first; the others are the endpoint's own
# list, as it describes them.
RECOMMENDED = {
    DEFAULT_MODEL: "tested with this app - recommended",
    "anthropic/claude-opus-5.5": "more careful, several times the price",
}


def list_models(settings: dict, vision_only: bool = True) -> list[dict]:
    """The models the endpoint offers that can look at pictures - which
    everything here needs - as [{"id", "name", "vision", "price", "note"}]:
    the recommended ones first, then the other Claude models, then the rest.
    `vision` is None for an endpoint that doesn't say, whose models are all
    listed (see vision_known). The key is optional but gives real prices."""
    if not allowed(settings):
        raise NotConfigured(privacy.OFF[privacy.AI])
    reply = _request(
        f"{settings[SETTING_BASE_URL]}/models?detailed=true",
        settings.get(SETTING_KEY) or None,
        timeout=30,
    )
    entries = [e for e in reply.get("data") or [] if isinstance(e, dict) and e.get("id")]
    known = any(isinstance(e.get("capabilities"), dict) for e in entries)
    models = []
    for entry in entries:
        capabilities = entry.get("capabilities")
        vision = bool(capabilities.get("vision")) if isinstance(capabilities, dict) else None
        if vision_only and known and not vision:
            continue
        models.append({
            "id": entry["id"],
            "name": entry.get("name") or entry["id"],
            "vision": vision,
            "price": _price(entry.get("pricing")),
            "note": RECOMMENDED.get(entry["id"], ""),
        })
    order = list(RECOMMENDED)
    models.sort(key=lambda m: (
        order.index(m["id"]) if m["id"] in order else len(order),
        not m["id"].startswith("anthropic/"),
        m["id"],
    ))
    return models


def vision_known(models: list[dict]) -> bool:
    """Whether the endpoint said which of its models can see pictures."""
    return any(m.get("vision") is not None for m in models)


def _price(pricing) -> str:
    """"$2 / $10 per million tokens" from an endpoint's pricing, if given."""
    if not isinstance(pricing, dict):
        return ""
    prompt, completion = pricing.get("prompt"), pricing.get("completion")
    if not isinstance(prompt, int | float) or not isinstance(completion, int | float):
        return ""
    return f"${prompt:g} in / ${completion:g} out per million tokens"


def ping(settings: dict) -> str:
    """A tiny request to check the key and model work; returns the model's
    word back."""
    return chat(
        [{"role": "user", "content": "Reply with the single word OK."}],
        settings, json_reply=False, max_tokens=5,
    ).strip()
