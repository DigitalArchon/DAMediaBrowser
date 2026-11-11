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

Two ways out, and every request says which it took (destination()):

- Ordinarily, over https to the endpoint on the AI tab, which can read it,
  and passes it on to whoever runs the model.
- In Privacy Mode, only to the private model set beside it, whose enclave
  is attested before anything is sent to it (private_ai): a private/ model
  sealed end to end, so that Nano-GPT relays only ciphertext (sealed.py), or
  a TEE/ model, which Nano-GPT's gateway still reads on the way. There is no
  web search then - Nano-GPT would have to read the request to run one -
  and nothing ever falls back to the ordinary model.

An operation's settings are loaded once, when it starts, and its Privacy
Mode is decided then (settings_from). What is switched off or tightened in
Settings since is still held to at every request (_tightened): a run that
began with the AI allowed sends nothing more once it isn't, and one that
began outside Privacy Mode sends nothing more to the ordinary model once
Privacy Mode is on.

Every request is listed as it is made in outbound, for Help → What Was Sent.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse

from . import creds, netguard, outbound, privacy, private_ai, store

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
# Privacy Mode: only the private model, attested first.
SETTING_PRIVACY_MODE = "ai_privacy_mode"
SETTING_PRIVATE_MODEL = "ai_private_model"
SETTING_STRICT = "ai_privacy_strict"
# After a TEE/ reply whose receipt holds, name the provider that ran the model, from
# RedPill's public record of its gateway's session (private_ai.TeeClient.served_by). Off
# by default: it connects to api.redpill.ai directly, not through Nano-GPT, so RedPill
# sees this computer's address (SealedLore's author, Nov 11 2026: a clear choice in
# Settings, said plainly beside it). Held to this app's rule that a run's privacy only
# ever tightens (_names_upstreams): it counts for a run only if it was on when the run
# began and is still on at the reply - off, nothing more is looked up, at once; on, from
# the next run.
SETTING_NAME_UPSTREAMS = "ai_tee_name_upstreams"
# Not settings: what settings_from adds for the operation they are loaded for.
SESSION = "_privacy_session"    # its private_ai.PrivacySession, in Privacy Mode
FROM_FORM = "_from_form"        # Settings' own form, which is newer than the file

# The private model Privacy Mode starts with: end-to-end encrypted, and it can
# see the frames. Kimi K3.1 Pro, post-trained from Kimi K3, chosen by the author
# when nano-gpt retired private/kimi-k3 (Nov 11 2026); it always reasons, which
# REASONING_EFFORT keeps short. Only for settings that name no private model: a
# saved private/kimi-k3 stays as it is, refused as no longer offered
# (private_ai.ModelRetired), never swapped for this unasked.
DEFAULT_PRIVATE_MODEL = "private/kimi-k3-1-pro"

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
    SETTING_PRIVATE_MODEL: DEFAULT_PRIVATE_MODEL,
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


class Refused(AIError):
    """Nothing was sent: the address, the key or a check said no."""


class SearchUnavailable(AIError):
    """The chosen web search provider is refused for this account: Nano-GPT
    allows only LinkUp when Zero Data Retention is switched on."""


def settings_from(app_settings: dict, form: bool = False) -> dict:
    """The AI settings in force: the stored ones over the defaults, with
    the environment's key over the stored one.

    In Privacy Mode, with the AI allowed, the operation these are loaded
    for gets its PrivacySession here, under SESSION: its private model and
    strictness are fixed from this moment. `form` is for Settings' own
    form, whose choices are newer than settings.json's (see _tightened)."""
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
    settings[SETTING_PRIVATE_MODEL] = str(settings[SETTING_PRIVATE_MODEL]).strip()
    settings[SETTING_PRIVACY_MODE] = _on(app_settings.get(SETTING_PRIVACY_MODE))
    settings[SETTING_STRICT] = _on(app_settings.get(SETTING_STRICT))
    # Unlike Privacy Mode and Strict, which only tighten, this one sends something more,
    # to RedPill: on only when plainly on.
    settings[SETTING_NAME_UPSTREAMS] = app_settings.get(SETTING_NAME_UPSTREAMS) is True
    settings[FROM_FORM] = form
    if settings[SETTING_PRIVACY_MODE] and settings[privacy.AI]:
        settings[SESSION] = _session(settings)
    return settings


def _on(value) -> bool:
    """A switch as settings.json has it. Privacy Mode and Strict only ever
    tighten, so a value that is neither true nor false - edited by hand, or
    damaged - reads as on: off, the next request would have gone to the
    ordinary model."""
    if isinstance(value, bool) or value is None:
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("false", "0", "no", "off", ""):
        return False
    return value != 0


def _session(settings: dict) -> private_ai.PrivacySession:
    session = private_ai.PrivacySession(
        settings[SETTING_BASE_URL], settings[SETTING_KEY], settings[SETTING_PRIVATE_MODEL],
        request_timeout=TIMEOUT_SECONDS, strict=settings[SETTING_STRICT],
        tee_factory=_tee_client,
    )
    session.listener = _attestation_listener(session)
    return session


def _tee_client(api_url, api_key, model) -> private_ai.TeeClient:
    """A TEE/ model's client, its requests through netguard as ever, and the one of
    them that goes somewhere of its own listed on What Was Sent (_ListedLookups)."""
    return private_ai.TeeClient(api_url, api_key, model,
                                session=_ListedLookups(endpoint=api_url))


class _ListedLookups(netguard.Session):
    """netguard's Session for a TEE/ model's checks, which lists on What Was Sent the
    one request among them that goes to a service of its own: RedPill's record of a
    reply's session, from api.redpill.ai (Settings → Privacy, "Name the provider behind
    each TEE reply"). The attestations, Intel's and NVIDIA's included, are listed once
    each by the session's listener."""

    WHAT = ("a TEE reply's session id, to name the provider that ran the model: nothing "
            "about your videos, and no key")
    HOW = ("Asked RedPill's api.redpill.ai directly, not through Nano-GPT: it sees this "
           "computer's internet address, or the proxy's")

    def __init__(self, endpoint=None):
        self.endpoint = endpoint

    def request(self, method, url, *args, **kwargs):
        if not str(url).startswith(private_ai.SESSION_RECORDS):
            try:
                return super().request(method, url, *args, **kwargs)
            except netguard.RequestError as exc:
                moved = exc.__cause__
                if isinstance(moved, netguard.Redirected) and self.endpoint and \
                        urlparse(str(url)).netloc == urlparse(self.endpoint).netloc:
                    # nano-gpt's TEE checks carry the key, so none follows a redirect,
                    # and the endpoint is the user's to set: said as the AI's are.
                    raise netguard.RequestError(
                        moved.said(private_ai.ENDPOINT_MOVED)) from moved
                raise
        try:
            response = super().request(method, url, *args, **kwargs)
        except netguard.RequestError as exc:
            refused = isinstance(exc.__cause__, netguard.REFUSALS)
            outbound.record("RedPill", url, self.WHAT, outbound.CLEAR, self.HOW,
                            outbound.REFUSED if refused else outbound.FAILED, str(exc))
            raise
        outbound.record("RedPill", url, self.WHAT, outbound.CLEAR, self.HOW, outbound.SENT,
                        f"HTTP {response.status_code}")
        return response


def _names_upstreams(settings: dict) -> bool:
    """Whether a TEE/ reply's provider is named now (SETTING_NAME_UPSTREAMS), held to
    this app's rule that a run's privacy only ever tightens: only if it was on when the
    run began (`settings`, loaded then) and is still on in settings.json at this reply.
    Switched off, the lookups stop at once, in a run already going too; switched on,
    they begin with the next run, never part way through one already going. (SealedLore
    and DA Assistant read it at each reply alone.) Settings' own form goes by what it
    shows."""
    if not settings.get(SETTING_NAME_UPSTREAMS):
        return False
    if settings.get(FROM_FORM):
        return True
    return store.load_app_settings().get(SETTING_NAME_UPSTREAMS) is True


def load_settings(app_settings: dict | None = None) -> dict:
    """The AI settings in force: settings.json's (or the app's settings
    given, already loaded), with the key from the keyring. A keyring that
    can't be reached, or stays locked, just means no key - the
    environment's, if it has one, doesn't need it."""
    app_settings = store.load_app_settings() if app_settings is None else dict(app_settings)
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


def in_privacy_mode(settings: dict) -> bool:
    """Whether these settings send only to the private model."""
    return bool(settings.get(SETTING_PRIVACY_MODE))


def session_of(settings: dict) -> private_ai.PrivacySession | None:
    """The operation's PrivacySession, in Privacy Mode."""
    return settings.get(SESSION)


def active_model(settings: dict) -> str:
    """The model requests go to: the private one in Privacy Mode."""
    session = session_of(settings)
    if session is not None:
        return session.model
    if in_privacy_mode(settings):
        return settings.get(SETTING_PRIVATE_MODEL) or DEFAULT_PRIVATE_MODEL
    return settings[SETTING_MODEL]


def is_configured(settings: dict) -> bool:
    """Whether the AI may be asked: it's allowed, there's a key that can be
    sent, and in Privacy Mode, nothing stands in its way."""
    return allowed(settings) and bool(settings.get(SETTING_KEY)) and not not_ready(settings)


def not_ready(settings: dict) -> str:
    """Why the AI can't be asked, for saying so; empty when it can."""
    if not allowed(settings):
        return privacy.OFF[privacy.AI]
    if not settings.get(SETTING_KEY):
        return "Set a Nano-GPT API key in Settings → AI."
    unfit = netguard.key_problem(settings.get(SETTING_KEY), "Nano-GPT")
    if unfit:
        return unfit
    if in_privacy_mode(settings):
        found = private_ai.problems(settings[SETTING_BASE_URL],
                                    settings.get(SETTING_PRIVATE_MODEL), wait=False)
        if found:
            return "Privacy Mode is on, but " + found[0][0].lower() + found[0][1:]
        return ""
    insecure = netguard.problem(settings[SETTING_BASE_URL])
    if insecure:
        return insecure
    if private_ai.sealed_only(settings[SETTING_MODEL]):
        return _sealed_only_message(settings[SETTING_MODEL])
    return ""


def _sealed_only_message(model: str) -> str:
    return (f"{str(model).strip()} is end-to-end encrypted, and is only ever sent sealed: "
            f"switch Privacy Mode on and choose it there (Settings → AI), or choose "
            f"another model.")


def short_model_name(model: str) -> str:
    """"anthropic/claude-sonnet-5:online/kagi" as "claude-sonnet-5"."""
    return model.split(":", 1)[0].rsplit("/", 1)[-1]


# --- where it goes, in words -------------------------------------------------


def _service(url: str) -> str:
    host = (urlparse(str(url or "")).hostname or "").lower()
    if host == NANO_GPT_HOST or host.endswith("." + NANO_GPT_HOST):
        return "Nano-GPT"
    return host or "the AI service"


def destination(settings: dict, online: bool = False) -> tuple[str, str]:
    """Where a request goes and who can read it, as the end of a sentence
    that begins "Sends ...", and how it travels (outbound's CLEAR, TEE or
    SEALED): for the window to say before anything is sent.

    "to claude-sonnet-5 through Nano-GPT, which passes it on to whoever runs
    the model: both can read it" ... "sealed end to end to kimi-k3-1-pro's
    enclave, once it has been attested: Nano-GPT relays only ciphertext"."""
    model = active_model(settings)
    short = short_model_name(model)
    if in_privacy_mode(settings):
        if private_ai.kind_of(model) == private_ai.E2EE:
            return (f"sealed end to end to {short}'s enclave, once it has been "
                    f"attested: Nano-GPT relays only ciphertext", outbound.SEALED)
        # Not "its enclave": most TEE/ models are attested as RedPill's gateway, and
        # the machine that runs the model is the gateway's word (what_was_attested).
        return (f"to {short}, attested first - most TEE/ models as RedPill's TEE "
                f"gateway, which passes the request on to the machine that runs the "
                f"model - and Nano-GPT's gateway can read it on the way", outbound.TEE)
    url = settings[SETTING_BASE_URL]
    service = _service(url)
    search = may_search(settings) and online
    if service == "Nano-GPT":
        where = (f"to {short} through Nano-GPT, which passes it on to whoever runs the "
                 f"model: both can read it")
        if private_ai.kind_of(model) == private_ai.TEE:
            where = (f"to {model} through Nano-GPT, which can read it: a TEE model, but "
                     f"outside Privacy Mode it isn't attested first")
        if search:
            where += (f", and {SEARCH_PROVIDERS.get(settings[SETTING_SEARCH], 'a search')} "
                      f"is sent a search about the show")
        return where, outbound.CLEAR
    host = urlparse(url).hostname or url
    if netguard.is_local(host):
        return f"to {short} on {host}, on this computer's own network", outbound.CLEAR
    return f"to {short} at {host}, which can read it", outbound.CLEAR


def protection_label(settings: dict) -> str:
    """A few words for where the AI's requests go: "Privacy Mode: end-to-end
    encrypted"; for a TEE/ model what its newest attestation was of, where the
    operation has one - "Privacy Mode: TEE gateway attested" for RedPill's
    gateway, which is not the machine that runs the model (what_was_attested) -
    else "Privacy Mode: TEE, attested first"; or "Nano-GPT"."""
    if in_privacy_mode(settings):
        if private_ai.kind_of(active_model(settings)) == private_ai.E2EE:
            return "Privacy Mode: end-to-end encrypted"
        session = session_of(settings)
        attestation = session.attestation if session is not None else None
        if attestation is not None and attestation.kind == private_ai.TEE:
            return f"Privacy Mode: {attestation.headline.split(' (', 1)[0]}"
        return "Privacy Mode: TEE, attested first"
    return _service(settings[SETTING_BASE_URL])


# --- message parts -----------------------------------------------------------


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def image_part(jpeg: bytes) -> dict:
    data = base64.b64encode(jpeg).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}


# --- requests ------------------------------------------------------------------


def endpoint_problem(url: str) -> str:
    """Why nothing may be sent to `url`, or empty when it may: https, or
    plain http only on this machine, its local network or an onion service
    (netguard)."""
    return netguard.problem(str(url or "")) or ""


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


def _prepared(url: str, api_key: str | None, body: dict | None = None,
              accept: str = "application/json") -> urllib.request.Request:
    """One request, ready to go by way of netguard - or Refused, before
    anything is sent, unless the address is https or on this machine's own
    network, and the key one that can be sent."""
    insecure = netguard.problem(url)
    if insecure:
        raise Refused(insecure)
    headers = {"Content-Type": "application/json", "Accept": accept}
    if api_key:
        unfit = netguard.key_problem(api_key, _service(url))
        if unfit:
            raise Refused(unfit)
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    return urllib.request.Request(url, data=data, headers=headers,
                                  method="POST" if data else "GET")


# What a request raises that _failed says in words: a malformed answer, cut
# short or too long a line, as much as a network that failed.
_REQUEST_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException, ValueError)


def _failed(exc: Exception, url: str) -> AIError:
    """What a request to `url` that went wrong raises (_REQUEST_ERRORS):
    Refused when netguard refused it before anything was sent, an AIError
    saying what went wrong otherwise - listed by the caller (chat) - in words
    that quote no password written into the address (netguard.reason). A
    redirect is followed by none of the AI's requests: it says where the
    endpoint pointed, and where to put its new address if it has moved."""
    if isinstance(exc, netguard.Redirected):
        return AIError(exc.said(private_ai.ENDPOINT_MOVED))
    if isinstance(exc, urllib.error.HTTPError):
        return AIError(_http_error_message(exc))
    if isinstance(exc, netguard.REFUSALS):
        return Refused(str(exc.reason))
    return AIError(netguard.reason(exc, url))


def _request(url: str, api_key: str | None, body: dict | None = None, timeout=TIMEOUT_SECONDS):
    """One request, by way of netguard (_prepared), and its answer as JSON. It
    follows no redirect, key or none: every request to the AI endpoint is
    one of the AI's (_failed says where it pointed)."""
    req = _prepared(url, api_key, body)
    try:
        with netguard.urlopen(req, timeout, follow_redirects=False) as resp:
            raw = resp.read()
    except _REQUEST_ERRORS as exc:
        raise _failed(exc, url) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AIError(f"the reply wasn't JSON: {exc}") from exc


def _tee_stream(url: str, api_key: str, body: dict, timeout=TIMEOUT_SECONDS) -> dict:
    """One request to a TEE/ model, streamed, its reply read whole: what
    _consume finds in it, with what the reply's check needs
    (private_ai.TeeClient.check_reply) - the hash of every byte the stream
    carried, [DONE] and whatever follows it too ("body_hash", None for a
    stream that did not arrive whole), and the receipt and route its headers
    name.

    Streamed because the record nano-gpt keeps of a TEE reply hashes the
    bytes of its stream as they came, while a reply asked for whole it
    rebuilds, so that its bytes match nothing. Its exact mode for whole
    replies (X-NanoGPT-TEE-Response: exact) would be the other way; but
    nearly every request here asks for a JSON object (response_format), which
    that mode is not known to take, and every other app of the family streams
    - DA Assistant and SealedLore, whose hashes were measured against every
    TEE/ model live. Nothing here shows a reply before it is whole, so
    streaming changes nothing else.

    An answer that carries no stream at all - a web page where the API was
    expected, a sign-in page on a local network - is an AIError naming its
    status and content type, never an empty reply. A body with "data:" lines
    is read whatever its content type says. From DA Assistant's
    llm._private_call and SealedLore's openai_compat (Nov 2026)."""
    request = _prepared(url, api_key, dict(body, stream=True), accept="text/event-stream")
    digest = hashlib.sha256()
    try:
        with netguard.urlopen(request, timeout, follow_redirects=False) as response:
            lines = _hashed_lines(response, digest)
            result = _consume(lines)
            whole = result["done"] and _read_to_the_end(lines)
            status = getattr(response, "status", None) or 200
            headers = getattr(response, "headers", None)
            answered = {str(name).lower(): str(value)
                        for name, value in (headers.items() if headers else ())}
            landed = response.geturl() if hasattr(response, "geturl") else url
    except _REQUEST_ERRORS as exc:
        raise _failed(exc, url) from exc
    if not result["data"]:
        kind = answered.get("content-type", "").split(";")[0].strip() or "no content type"
        moved = f", sent on to {netguard.shown(landed)}," if landed and landed != url else ""
        raise AIError(
            f"{netguard.shown(url)}{moved} answered HTTP {status} ({kind[:80]}) with no reply "
            f"in it, not the stream a chat endpoint sends. Check the endpoint's address in "
            f"Settings → AI: a web page answers like this where the API was expected (an "
            f"address without its /v1, say).")
    result["body_hash"] = "sha256:" + digest.hexdigest() if whole else None
    result["receipt_id"] = answered.get("x-receipt-id") or answered.get("x-tee-signature-id")
    result["route"] = answered.get("x-tee-verification-route")
    return result


def _hashed_lines(response, digest):
    """A stream's lines, every byte fed to `digest` as it is read: a TEE
    reply's record names the hash of the exact bytes the stream carried. As
    DA Assistant's llm._hashed_lines reads requests' chunks (Nov 2026), from
    urllib's answer."""
    pending = None
    while True:
        chunk = response.read(512)
        if not chunk:
            break
        digest.update(chunk)
        if pending is not None:
            chunk = pending + chunk
        lines = chunk.splitlines()
        if lines and lines[-1] and chunk and lines[-1][-1] == chunk[-1]:
            pending = lines.pop()
        else:
            pending = None
        yield from lines
    if pending is not None:
        yield pending


def _read_to_the_end(lines) -> bool:
    """Read what is left of a stream after its [DONE], so that its hash is of
    every byte it carried. Whether it could be."""
    try:
        for _rest in lines:
            pass
    except _REQUEST_ERRORS:
        return False
    return True


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    """What went wrong, from the body when it says (Nano-GPT returns
    {"error": {"message": ...}}) and from the status when it doesn't. The
    service's words go on one line, without what a reader cannot see."""
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
    detail = private_ai.unsealed_words(detail, 300)
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
    can, Settings → Privacy allows it, and it isn't Privacy Mode - where
    Nano-GPT would have to read the request to search for it."""
    if in_privacy_mode(settings):
        return False
    return searches_the_web(settings) and bool(
        settings.get(privacy.WEB_SEARCH, privacy.DEFAULTS[privacy.WEB_SEARCH])
    )


def model_name(settings: dict, online: bool = False) -> str:
    if in_privacy_mode(settings):
        return active_model(settings)
    model = settings[SETTING_MODEL]
    if not may_search(settings):
        # Not even when the model was set with the suffix already on.
        return model.split(ONLINE_SUFFIX, 1)[0] if searches_the_web(settings) else model
    if online and ONLINE_SUFFIX not in model:
        model += f"{ONLINE_SUFFIX}/{settings.get(SETTING_SEARCH) or DEFAULT_SEARCH}"
    return model


def _tightened(settings: dict) -> str:
    """Why these settings may send nothing more, now: what was switched off,
    or Privacy Mode switched on, in Settings since they were loaded. Empty
    when they may. Settings' own form is newer than the file, and is never
    held to it."""
    if settings.get(FROM_FORM):
        return ""
    live = store.load_app_settings()
    if not privacy.allowed(privacy.AI, live):
        return privacy.OFF[privacy.AI]
    if _on(live.get(SETTING_PRIVACY_MODE)) and session_of(settings) is None:
        return (f"Privacy Mode was switched on after this began, so nothing more goes to "
                f"{short_model_name(settings[SETTING_MODEL])}. Start it again to use the "
                f"private model.")
    return ""


def chat(messages, settings: dict, *, json_reply: bool = True, online: bool = False,
         max_tokens: int = MAX_REPLY_TOKENS, temperature: float = 0.0,
         reasoning: str = REASONING_EFFORT) -> str:
    """One chat completion; returns the assistant's text.

    `messages` is the OpenAI shape: [{"role", "content"}], content a string
    or a list of parts (text_part / image_part). With json_reply the model
    is asked for a JSON object, which every Claude model honours. With
    `online` Nano-GPT searches the web for it first - the difference
    between guessing a show's setlist and reading it off setlist.fm - but
    never in Privacy Mode, where it goes only to the operation's private
    model, attested first (_private_chat).
    """
    if not is_configured(settings):
        raise NotConfigured(not_ready(settings))
    stopped = _tightened(settings)
    if stopped:
        raise NotConfigured(stopped)
    session = session_of(settings)
    if session is None and not settings.get(FROM_FORM):
        # The web search switched off since this began is off for it too.
        online = online and privacy.allowed(privacy.WEB_SEARCH, store.load_app_settings())
    body = {
        "model": model_name(settings, online and session is None),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
        "reasoning_effort": reasoning,
    }
    if json_reply:
        body["response_format"] = {"type": "json_object"}
    if session is not None:
        return _private_chat(body, messages, session, settings)
    if private_ai.sealed_only(body["model"]):
        raise Refused(f"Nothing was sent. {_sealed_only_message(body['model'])}")
    url = f"{settings[SETTING_BASE_URL]}/chat/completions"
    where, protection = destination(settings, online)
    text, pictures = outbound.text_of(messages)
    what = _what(text, pictures, body["model"])
    try:
        reply = _request(url, settings[SETTING_KEY], body)
        answer = reply_text(reply)
    except AIError as exc:
        refused = isinstance(exc, Refused)
        outbound.record(_service(url), url, what, protection, f"Sends it {where}",
                        outbound.REFUSED if refused else outbound.FAILED, str(exc), text)
        if online and "web search provider" in str(exc).lower():
            raise SearchUnavailable(str(exc)) from exc
        raise
    outbound.record(_service(url), url, what, protection, f"Sent it {where}",
                    content=text)
    return answer


def _what(text: str, pictures: int, model: str) -> str:
    frames = f"{pictures} frame{'s' if pictures != 1 else ''} and " if pictures else ""
    return f"{frames}{len(text):,} characters of text, for {model}"


# --- Privacy Mode --------------------------------------------------------------


def _attestation_listener(session: private_ai.PrivacySession):
    """What a session's attestations are listed as: where the checks went,
    and what they found. They carry nothing about the videos."""

    def heard(event, detail):
        if event == "attested":
            text, tone = private_ai.attestation_label(detail)
            # What was attested first, and what it leaves to someone's word.
            what = private_ai.what_was_attested(detail)
            outbound.record(
                "Nano-GPT", session.api_url, f"an attestation of {session.model}'s enclave",
                outbound.CHECK, _checked_with(session), outbound.SENT,
                " ".join(part for part in (f"{text}.", what, detail.detail) if part))
        elif event == "refused":
            outbound.record(
                "Nano-GPT", session.api_url, f"an attestation of {session.model}'s enclave",
                outbound.CHECK, _checked_with(session), outbound.REFUSED, str(detail))

    return heard


def _checked_with(session) -> str:
    if session.kind == private_ai.E2EE:
        return ("Asked nano-gpt's relay for the enclave's report, and checked it against "
                "AMD's or Intel's certificates (through Tinfoil's proxy, for AMD's) and "
                "Tinfoil's release record on Sigstore: a fresh nonce, nothing about "
                "your videos")
    return ("Asked nano-gpt for the model's own report and RedPill's gateway's, and "
            "checked them with Intel's and NVIDIA's attestation services: a fresh nonce "
            "for each, nothing about your videos")


def _private_chat(body: dict, messages, session: private_ai.PrivacySession,
                  settings: dict | None = None) -> str:
    """One request in Privacy Mode. In order, each step before anything
    reaches the model:

      - the enclave is attested (PrivacySession.ensure), or nothing is sent;
      - a private/ model's body is sealed to that enclave's key (sealed.py)
        and never goes anywhere unsealed; a TEE/ model's goes to the
        endpoint the session was made with, never to another.

    Afterwards the reply is checked - a TEE reply, streamed (_tee_stream),
    against the record its attestation's keys signed, for the exact bytes its
    stream carried, with RedPill's receipt beside it, looked up by the receipt
    and the route its headers name - and the receipt kept on the session for
    the window; and, where Settings → Privacy asked for it when the run began
    and still does, who ran a TEE reply's model, from RedPill's record of its
    session (_names_upstreams, private_ai.TeeClient.served_by)."""
    model = session.model
    text, pictures = outbound.text_of(messages)
    what = _what(text, pictures, model)
    if private_ai.searches(model):
        message = (f"Nothing was sent. {model} asks nano-gpt to search the web, which "
                   "Privacy Mode never does.")
        outbound.record(_service(session.api_url), session.api_url, what, outbound.CLEAR,
                        "Not sent", outbound.REFUSED, message, text)
        raise Refused(message)
    sealed = session.kind == private_ai.E2EE
    protection = outbound.SEALED if sealed else outbound.TEE
    # Not "to its attested enclave": that a TEE/ request reached it is the reply's
    # record's to show (private_ai.sent_label).
    where = (f"sealed end to end to {model}'s attested enclave: Nano-GPT relays only "
             f"ciphertext" if sealed else
             f"to {model}, attested first, through Nano-GPT's gateway, which can read it")
    service = _service(session.api_url)
    try:
        attestation = session.ensure()
    except private_ai.PrivacyError as exc:
        outbound.record(service, session.api_url, what, protection, f"Sends it {where}",
                        outbound.REFUSED, str(exc), text)
        raise Refused(str(exc)) from exc
    body = dict(body, model=model)
    checked = []            # a TEE reply's check, once it has one (_how)

    def how(verb):
        """How it travelled, and - for a TEE/ reply that came - whether its record
        shows the attested TEE received it (private_ai.sent_label)."""
        if not checked:
            return f"{verb} {where}"
        return f"Sent it {where}: {private_ai.sent_label(checked[0])[0]}"

    try:
        if sealed:
            answer = _sealed_chat(body, session)
        else:
            result = _tee_stream(f"{session.api_url}/chat/completions", session.api_key,
                                 body)
            checked.append(session.check_reply(
                result["id"], receipt_id=result["receipt_id"],
                body_hash=result["body_hash"], route=result["route"],
                name_upstream=_names_upstreams(settings or {})))
            answer = _answer_of(result)
    except private_ai.PrivacyError as exc:
        refused = "nothing was sent" in str(exc).lower()
        outbound.record(service, session.api_url, what, protection, how("Sends it"),
                        outbound.REFUSED if refused else outbound.FAILED, str(exc), text)
        raise (Refused if refused else AIError)(
            f"{exc}. Privacy Mode: nothing was sent in the clear") from exc
    except AIError as exc:
        outbound.record(service, session.api_url, what, protection, how("Sends it"),
                        outbound.REFUSED if isinstance(exc, Refused) else outbound.FAILED,
                        str(exc), text)
        raise
    except Exception as exc:  # noqa: BLE001 - listed, then said, whatever it was
        outbound.record(service, session.api_url, what, protection, how("Sends it"),
                        outbound.FAILED, f"{type(exc).__name__}: {exc}", text)
        raise AIError(f"the private request failed: {exc}") from exc
    receipt = session.last_receipt()
    outbound.record(service, session.api_url, what, protection, how("Sent it"),
                    outbound.SENT,
                    f"{private_ai.attestation_label(attestation)[0]}; reply: "
                    f"{private_ai.receipt_label(receipt)[0]}. "
                    f"{private_ai.receipt_tip(receipt)}", text)
    return answer


def _sealed_chat(body: dict, session: private_ai.PrivacySession) -> str:
    """Seal `body` to the enclave, and open its reply. The router takes how
    hard a model thinks in each family's own spelling (sealed.shape_body),
    so the effort goes to it as {"effort": ...}, not as the top-level field
    the ordinary endpoint takes: asked that way, a model that thinks only
    when told to would think."""
    payload = dict(body)
    effort = payload.pop("reasoning_effort", None)
    if effort:
        payload["reasoning"] = {"effort": effort}
    client = session.sealed_client()
    with client.request(session.model, payload) as response:
        result = _consume(client.lines(response))
    client.finish(result["finish_reason"], result["done"])
    session.check_reply(result["id"])
    return _answer_of(result)


def _answer_of(result: dict) -> str:
    """The text of a private reply's stream (_consume), or AIError for one
    that is no answer: an error it carried, a stream that ended with neither
    [DONE] nor a finish reason, a model that ran out of room before it
    answered, or nothing at all."""
    if result["error"]:
        raise AIError(result["error"])
    if not (result["done"] or result["finish_reason"]):
        raise AIError("the reply was cut off before it was complete")
    if result["finish_reason"] == "length" and not result["text"].strip():
        raise AIError("the model ran out of room before it answered - try again, or "
                      "another private model in Settings → AI")
    if not result["text"].strip():
        raise AIError("the model returned an empty reply")
    return result["text"]


def _consume(lines) -> dict:
    """Read an SSE stream - a TEE reply's, or a sealed one's, its lines
    decrypted as they come (sealed.PrivateModeClient.lines): {text, id,
    finish_reason, done, error, data}. An error in it is never read as text;
    `data` is whether it held a "data:" line at all. From DA Assistant's
    llm._consume, less what only a chat window needs."""
    text, response_id, finish_reason, done, error = "", None, None, False, None
    data = False
    for raw in lines:
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if not line.startswith("data:"):
            continue
        data = True
        line = line[5:].strip()
        if line == "[DONE]":
            done = True
            break
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        if response_id is None and isinstance(chunk.get("id"), str):
            response_id = chunk["id"]
        problem = chunk.get("error")
        if problem:
            message = problem.get("message") if isinstance(problem, dict) else problem
            error = private_ai.unsealed_words(message or "the request failed", 300)
            break
        choices = chunk.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else None
        if choice is None:
            continue
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        delta = choice.get("delta") or {}
        piece = delta.get("content") if isinstance(delta, dict) else None
        if isinstance(piece, list):
            piece = "".join(p.get("text", "") for p in piece if isinstance(p, dict))
        if isinstance(piece, str):
            text += piece
    return {"text": text, "id": response_id, "finish_reason": finish_reason,
            "done": done, "error": error, "data": data}


def check_attestation(settings: dict) -> private_ai.PrivacySession:
    """Attest the private model's enclave now, sending nothing for it to read
    (and paying nothing): Settings' Check Attestation. Returns the session,
    whose `attestation` says what it proved; raises AIError when it didn't."""
    if not allowed(settings):
        raise NotConfigured(privacy.OFF[privacy.AI])
    found = private_ai.problems(settings[SETTING_BASE_URL],
                                settings.get(SETTING_PRIVATE_MODEL), wait=True)
    if found:
        raise Refused(" ".join(found))
    session = _session(settings)
    try:
        session.ensure()
    except private_ai.PrivacyError as exc:
        raise Refused(str(exc)) from exc
    return session


# --- replies -------------------------------------------------------------------


def with_search(settings: dict, provider: str) -> dict:
    return {**settings, SETTING_SEARCH: provider}


def reply_text(reply) -> str:
    """The assistant's text out of a chat-completions reply."""
    if not isinstance(reply, dict):
        raise AIError("unexpected reply")
    error = reply.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise AIError(private_ai.unsealed_words(message, 300) or "the request failed")
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


# --- the models on offer -------------------------------------------------------


# The models worth suggesting, and what to say about them. Every AI part of
# this app was tried out with the first; the others are the endpoint's own
# list, as it describes them.
RECOMMENDED = {
    DEFAULT_MODEL: "tested with this app - recommended",
    "anthropic/claude-opus-5.5": "more careful, several times the price",
}
# The same, for Privacy Mode: the end-to-end encrypted models that can see.
PRIVATE_RECOMMENDED = {
    DEFAULT_PRIVATE_MODEL: "post-trained from Kimi K3, always reasons - the default",
    "private/deepseek-v4-1-flash": "quicker and far cheaper, less sure of a setlist",
}
# What nano-gpt charges for a private/ model over its TEE/ twin (Nov 2026).
PRIVATE_MODE_MARKUP = 1.05
TEE_NOTE = "a TEE model: attested before anything is sent only in Privacy Mode"


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
        if private_ai.sealed_only(entry["id"]):
            continue  # only ever sent sealed: Privacy Mode's list has them
        capabilities = entry.get("capabilities")
        vision = bool(capabilities.get("vision")) if isinstance(capabilities, dict) else None
        if vision_only and known and not vision:
            continue
        models.append({
            "id": entry["id"],
            "name": entry.get("name") or entry["id"],
            "vision": vision,
            "price": _price(entry.get("pricing")),
            "note": RECOMMENDED.get(entry["id"], "") or (
                TEE_NOTE if private_ai.is_tee(entry["id"]) else ""),
        })
    order = list(RECOMMENDED)
    models.sort(key=lambda m: (
        order.index(m["id"]) if m["id"] in order else len(order),
        not m["id"].startswith("anthropic/"),
        m["id"],
    ))
    return models


def list_private_models(settings: dict) -> list[dict]:
    """The models Privacy Mode can use that can look at pictures, as
    list_models gives them, each with its "kind" (private_ai.E2EE or TEE):
    nano-gpt's Private Mode models first - end-to-end encrypted, from its
    relay's public list, with what each can do and costs from its TEE/ twin
    - then its TEE/ models. No key is sent for either list."""
    if not allowed(settings):
        raise NotConfigured(privacy.OFF[privacy.AI])
    from . import sealed

    base = settings[SETTING_BASE_URL]
    if not searches_the_web(settings):
        raise Refused("Privacy Mode's models are nano-gpt's, and the endpoint on the AI "
                      "tab is not nano-gpt's.")
    relay = _request(sealed.private_list_url(base), None, timeout=30)
    private_ids = sealed.private_ids_in(relay)
    listed = _request(f"{base}/models?detailed=true", None, timeout=30)
    entries = [e for e in listed.get("data") or [] if isinstance(e, dict)
               and isinstance(e.get("id"), str)]
    twins = {sealed.twin_key(e["id"]): e for e in entries if private_ai.is_tee(e["id"])}
    models = []
    for model_id in private_ids:
        twin = twins.get(sealed.twin_key(model_id)) or {}
        capabilities = twin.get("capabilities")
        vision = bool(capabilities.get("vision")) if isinstance(capabilities, dict) else None
        if vision is False:
            continue
        pricing = twin.get("pricing")
        if isinstance(pricing, dict):
            pricing = {k: v * PRIVATE_MODE_MARKUP if isinstance(v, int | float) else v
                       for k, v in pricing.items()}
        models.append({
            "id": model_id, "name": f"{twin.get('name') or model_id} (end-to-end encrypted)",
            "vision": vision, "price": _price(pricing), "kind": private_ai.E2EE,
            "note": PRIVATE_RECOMMENDED.get(model_id, "end-to-end encrypted"),
        })
    for entry in entries:
        if not private_ai.is_tee(entry["id"]):
            continue
        capabilities = entry.get("capabilities")
        if not (isinstance(capabilities, dict) and capabilities.get("vision")):
            continue
        models.append({
            "id": entry["id"], "name": entry.get("name") or entry["id"], "vision": True,
            "price": _price(entry.get("pricing")), "kind": private_ai.TEE,
            "note": "TEE: attested first - most as RedPill's gateway - but Nano-GPT's "
                    "gateway can read what is sent",
        })
    order = list(PRIVATE_RECOMMENDED)
    models.sort(key=lambda m: (m["kind"] != private_ai.E2EE,
                               order.index(m["id"]) if m["id"] in order else len(order),
                               m["id"]))
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
    word back. A private model that always thinks is given room to, which it
    is billed for only as it uses."""
    return chat(
        [{"role": "user", "content": "Reply with the single word OK."}],
        settings, json_reply=False, max_tokens=1024 if in_privacy_mode(settings) else 5,
    ).strip()
