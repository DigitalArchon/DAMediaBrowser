# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tinfoil's own API, end-to-end encrypted, with no relay in between. Preliminary.

SealedLore's providers/tinfoil_direct.py (built against captures of Tinfoil's public
listing and attestation bundle, Nov 14 2026, then run with its author's account on Nov
15 2026), in this app's shape: the sealed client of sealed.py, which reaches the same
router through nano-gpt's relay, pointed at https://inference.tinfoil.sh/v1 instead.
Shared with DA Assistant line for line (this copy differs only in this docstring, its
imports and the hooks tests/test_shared_code.py lists). Like sealed.py it needs
Tinfoil's SDK, which lives in Privacy Mode's own environment (vendored.py: inside the
AppImage, or tools/install-private-mode.sh from source); the listing does not.

What differs from the relay (SealedLore's design, e2ee-providers.md section 1):

- **Attestation** from Tinfoil's own bundle service, `https://atc.tinfoil.sh`
  (`direct_verify`): the SDK's bundle path, under the same guard (sdk_guard.py), never
  its router lookup (`get_router_address`, plain urllib). The bundle names the enclave;
  it must be this endpoint's host, or nothing is attested: the key and the headers go
  only where the key was bound. The whole attempt has ATTEMPT_SECONDS.
- **The TLS key is pinned** (`TlsPin`): the router terminates TLS inside the enclave,
  and the attestation binds its key. As the handshake with the host completes, before a
  byte of the request leaves, the certificate's key must be the attested one, behind a
  proxy's tunnel too; and no request's headers go out on a connection whose key was not
  checked. A mismatch sends nothing and attests again next time. Each request has a pin
  of its own: what one handshake found is never another's.
- **Requests**: `POST {base}/chat/completions`, sealed by EHBP, with the key and nothing
  else of ours in the clear; no preflight, no relay's headers. A reply counts only as a
  2xx EHBP opened, a stream only with its finish reason and `[DONE]`.
- **The key** goes to services.TINFOIL_KEY_ORIGIN, https://inference.tinfoil.sh:443,
  and nowhere else; the SDK's attestation fetches carry none.
- **The listing** (`GET {base}/models`) is the one unsealed request to the host, and
  goes without the key: its chat models only, their prices per million tokens (a reply's
  cost is estimated from them: Tinfoil reports none in a stream), context windows and
  reasoning efforts. `reasoning_effort` is sent only as one the model lists (anything
  else is a 400). A model gone from it is ModelRetired.
- **The prompt cache's secret** is ours, random per client and kept in memory only
  (sealed.Enclave.cache_secret).

What is not proved here: that the router hands a request only to attested model
enclaves (its published code, whose build the attestation checks, not something this
app checks), and what Tinfoil's servers see: the account, the model, timing, sizes and
cost, never the text.
"""

import hashlib
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlparse

from . import netguard, services
from .private_ai import ModelRetired, PrivacyError, unsealed_words
from .sealed import (
    CACHE_SECRET_FIELD,
    CONNECT_TIMEOUT,
    INSTALL_HINT,
    Enclave,
    PrivateModeClient,
    _body,
    _error_text,
    _redirected,
    _sealing_transport,
    _unasked_coding,
    attestation_for,
    available,
    bundle_verify,
)

# Tinfoil's attestation bundle service: one GET of /attestation is the whole trust root,
# checked by the SDK.
ATC_URL = "https://atc.tinfoil.sh"
SERVICE = "tinfoil"
SERVICE_NAME = "Tinfoil"
# A whole attestation, the SDK's every fetch in it, as the standard gives one.
ATTEMPT_SECONDS = 180.0
LISTING_TIMEOUT_SECONDS = 30
# The listing is read again after this long (a model retired meanwhile).
LISTING_MAX_AGE_SECONDS = 10 * 60
# The fields a request may carry, the router's for a direct client: nano-gpt's list
# (sealed.ALLOWED_FIELDS) less `chat_template_kwargs`, which is the relay's own
# per-family shaping. The cache secret is added after.
DIRECT_FIELDS = frozenset({
    "frequency_penalty", "logit_bias", "max_tokens", "messages", "model",
    "presence_penalty", "reasoning_effort", "response_format", "seed", "stop", "stream",
    "stream_options", "temperature", "top_p", "user",
})
# Reasoning efforts, least to most, as the listing names them.
EFFORT_SCALE = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
PIN_MISMATCH = ("Tinfoil's TLS key is not the attested one: nothing was sent. The "
                "enclave is attested again before the next try.")
NOT_PINNED = ("the connection to Tinfoil was not checked against the attested TLS key, "
              "so nothing was sent")


def _host(url):
    return services.host_of(url)


def base_of(api_url):
    """Tinfoil's API base for an address as typed: its /v1 added when no path was, any
    /chat/completions typed after it taken off, the host as a request reaches it
    (lowercase, no trailing dot: what the attestation and the pin name), and no user
    name or password."""
    try:
        parsed = urlparse(str(api_url or "").strip())
        port = parsed.port
    except ValueError:
        return str(api_url or "").strip()
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/models"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
    if not path.strip("/"):
        path = "/v1"
    host = services.host_of(api_url)
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme.lower()}://{host}{f':{port}' if port else ''}{path}"


# ---- the attestation ------------------------------------------------------------------

def within(seconds, work):
    """`work` on a thread of its own, given `seconds`: what it returns, or what it
    raised, or a refusal once the time is up (the thread is left to finish on its own;
    nothing it brings is used)."""
    done = threading.Event()
    result, failure = [], []

    def run():
        try:
            result.append(work())
        except BaseException as e:      # handed back to the caller
            failure.append(e)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True, name="tinfoil-attest").start()
    if not done.wait(seconds):
        raise PrivacyError(f"Tinfoil's router was not attested within {seconds / 60:g} "
                           f"minutes; nothing was sent")
    if failure:
        raise failure[0]
    return result[0]


def direct_verify(host, bundle_url, cache_secret, seconds=ATTEMPT_SECONDS,
                  verify=bundle_verify):
    """Attest Tinfoil's router from its own bundle service (network; worker thread), for
    the endpoint at `host`, the bundle asked for by that host (ATC serves either
    router's to a plain request, and each has a TLS key of its own): refused unless the
    bundle's enclave, as the SDK verified it (its certificate's names included), is
    `host`, and the attestation names a TLS key to pin."""
    verified = within(seconds, lambda: verify(bundle_url, cache_secret,
                                              base_url=f"https://{host}", enclave=host))
    attested = _host(f"https://{verified.enclave}")
    if attested != host:
        raise PrivacyError(
            f"Tinfoil's attestation is for {attested or 'no host'}, not {host}, this "
            f"provider's address: its key goes only where it was saved for, so nothing "
            f"was sent. Set the address to https://{attested}/v1 if that is Tinfoil's.")
    key = verified.tls_public_key
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise PrivacyError("Tinfoil's attestation names no TLS key to check the "
                           "connection against, so nothing was sent")
    return verified


# ---- the TLS pin ----------------------------------------------------------------------

class TlsPinMismatch(PrivacyError):
    """The host's TLS key is not the attested one, or was not checked."""


def spki_sha256(certificate_der):
    """The sha256 of a certificate's key (its SubjectPublicKeyInfo, DER), in hex: what
    Tinfoil's attestation binds, as its SDK compares it."""
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    certificate = x509.load_der_x509_certificate(certificate_der)
    key = certificate.public_key().public_bytes(Encoding.DER,
                                                PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(key).hexdigest()


class TlsPin:
    """For httpcore's `trace` extension: the TLS key of `host` must be `expected`,
    checked as each handshake with it completes, before anything is written on the
    connection; and no request's headers go out until one was (a CONNECT to a proxy
    excepted: it carries none of ours). Behind a proxy the handshake with the host is
    the one inside the tunnel; the proxy's own (an https proxy) is told apart by the
    name it was made for."""

    def __init__(self, host, expected):
        self.host = host.lower().rstrip(".")
        self.expected = expected.lower()
        self.checked = False
        self._making = None

    def __call__(self, event_name, info):
        if event_name.endswith(".start_tls.started"):
            self._making = str(info.get("server_hostname") or "").lower().rstrip(".")
        elif event_name.endswith(".start_tls.complete"):
            name, self._making = self._making, None
            if name and name != self.host:
                return          # a proxy's own TLS: the host's is made inside it
            self._check(info.get("return_value"))
        elif event_name.endswith(".send_request_headers.started"):
            request = info.get("request")
            if getattr(request, "method", b"") == b"CONNECT":
                return
            if not self.checked:
                raise TlsPinMismatch(NOT_PINNED)

    def _check(self, stream):
        extra = getattr(stream, "get_extra_info", None)
        ssl_object = extra("ssl_object") if callable(extra) else None
        try:
            certificate = ssl_object.getpeercert(True) if ssl_object is not None else None
            found = spki_sha256(certificate) if certificate else ""
        except Exception:       # anything unreadable is a mismatch
            found = ""
        if found != self.expected:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            raise TlsPinMismatch(PIN_MISMATCH)
        self.checked = True


# ---- the listing ----------------------------------------------------------------------

def _price(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def listing_entries(data):
    """Tinfoil's `/v1/models`, its chat models only, by id: each with its prices per
    million tokens, context window and the efforts it takes (`reasoning_efforts`,
    Tinfoil's `supported_efforts`)."""
    items = data.get("data") if isinstance(data, dict) else None
    entries = {}
    for item in items or []:
        if not isinstance(item, dict) or item.get("type") != "chat":
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        efforts = item.get("supported_efforts")
        efforts = [e for e in efforts if e in EFFORT_SCALE] if isinstance(efforts, list) \
            else []
        entry = {"id": model_id,
                 "name": item["name"] if isinstance(item.get("name"), str) else model_id,
                 "description": item["description"]
                 if isinstance(item.get("description"), str) else "",
                 "owned_by": "Tinfoil", "reasoning_efforts": efforts,
                 "capabilities": {"reasoning": item.get("reasoning") is True,
                                  "vision": item.get("multimodal") is True}}
        context = item.get("context_window")
        if isinstance(context, int) and not isinstance(context, bool) and context > 0:
            entry["context_length"] = context
        pricing = item.get("pricing")
        if isinstance(pricing, dict):
            prompt = _price(pricing.get("inputTokenPricePer1M"))
            completion = _price(pricing.get("outputTokenPricePer1M"))
            if prompt is not None and completion is not None:
                prices = {"prompt": prompt, "completion": completion,
                          "unit": "per_million_tokens"}
                cached = _price(pricing.get("cachedInputTokenPricePer1M"))
                if cached is not None:
                    prices["input_cache_read"] = cached
                entry["pricing"] = prices
        entries[model_id] = entry
    return entries


class Listing:
    """Tinfoil's listing, fetched without the key and kept a while. `session`: a
    requests-like Session (netguard.Session()), whose get() reads an answer only so far
    and follows no redirect."""

    def __init__(self, base_url, session=None, clock=time.monotonic):
        self.url = base_url.rstrip("/") + "/models"
        self._session = session
        self._clock = clock
        self._lock = threading.Lock()
        self._entries = None
        self._at = 0.0

    def fetch(self):
        """The chat models; PrivacyError when the listing cannot be read."""
        with self._lock:
            if self._entries is not None and \
                    0 <= self._clock() - self._at < LISTING_MAX_AGE_SECONDS:
                return dict(self._entries)
        insecure = netguard.problem(self.url)
        if insecure:
            raise PrivacyError(insecure)
        session = self._session or netguard.Session()
        try:
            # No key, no header of ours: it is public.
            response = session.get(self.url, timeout=LISTING_TIMEOUT_SECONDS,
                                   allow_redirects=False)
            if response.status_code >= 300:
                raise PrivacyError(f"Tinfoil's model list answered HTTP "
                                   f"{response.status_code}")
            entries = listing_entries(response.json())
        except PrivacyError:
            raise
        except ValueError as e:
            raise PrivacyError("Tinfoil's model list was not JSON") from e
        except requests_errors() as e:
            raise PrivacyError(f"Tinfoil's model list could not be read: "
                               f"{unsealed_words(e, 300)}") from e
        finally:
            if self._session is None:
                session.close()
        with self._lock:
            self._entries, self._at = entries, self._clock()
        return dict(entries)

    def known(self):
        """The listing as last read, fetched once if never; None when it cannot be."""
        try:
            return self.fetch()
        except PrivacyError:
            return None


def requests_errors():
    """What the listing's session raises for a request that failed (requests')."""
    return netguard.RequestError


# ---- the request ----------------------------------------------------------------------

def chosen_effort(wanted, efforts):
    """The `reasoning_effort` to send: `wanted` if the model lists it, else the nearest it
    lists (the higher on a tie; "none" only when asked for), and nothing when it lists
    none or the listing is unknown: a value the model does not list is refused with a
    400."""
    if not wanted or not efforts:
        return None
    if wanted in efforts:
        return wanted
    if wanted == "none" or wanted not in EFFORT_SCALE:
        return None
    listed = [e for e in efforts if e in EFFORT_SCALE and e != "none"]
    if not listed:
        return None
    target = EFFORT_SCALE.index(wanted)
    return min(listed, key=lambda e: (abs(EFFORT_SCALE.index(e) - target),
                                      -EFFORT_SCALE.index(e)))


def direct_body(payload, efforts):
    """The body sealed to the router: its own fields, a stream with its usage, and the
    effort only as the model lists one. `payload["reasoning"]`, when llm.py sets it, is
    {"effort": ...}."""
    body = dict(payload)
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    reasoning = body.pop("reasoning", None)
    wanted = reasoning.get("effort") if isinstance(reasoning, dict) else None
    effort = chosen_effort(str(wanted).lower() if wanted else None, efforts)
    if effort:
        body["reasoning_effort"] = effort
    else:
        body.pop("reasoning_effort", None)
    return {key: value for key, value in body.items() if key in DIRECT_FIELDS}


class TinfoilDirectClient:
    """Sends a Tinfoil model's requests sealed to Tinfoil's attested router, over a
    connection pinned to its attested TLS key. The same shape as sealed.PrivateModeClient
    (attest, accept, request, lines, finish), so a private conversation sends through
    either alike.

    `key_saved_for`: the origin the key was saved for (config key_origins), held to
    TINFOIL_KEY_ORIGIN with the address."""

    def __init__(self, api_url, api_key, key_saved_for="", request_timeout=180,
                 enclave=None, transport_factory=_sealing_transport, listing=None,
                 session=None):
        self.base = base_of(api_url)
        self.host = _host(self.base)
        insecure = netguard.problem(self.base)
        if insecure:
            raise PrivacyError(insecure)
        refused = services.tinfoil_key_refusal(self.base, key_saved_for)
        if refused:
            raise PrivacyError(refused)
        unfit = netguard.key_problem(api_key, SERVICE_NAME)
        if unfit:
            raise PrivacyError(unfit)
        if not api_key:
            raise PrivacyError("Tinfoil's models need a Tinfoil API key; nothing was sent")
        ok, message = available() if enclave is None else (True, "")
        if not ok:
            raise PrivacyError(message or INSTALL_HINT)
        self.api_key = api_key
        self.request_timeout = request_timeout
        self.enclave = enclave or Enclave(
            ATC_URL, verifier=lambda bundle, secret: direct_verify(self.host, bundle,
                                                                   secret))
        self._transport_factory = transport_factory
        self.listing = listing or Listing(self.base, session=session)
        self._client = None
        self._client_key = None
        self._attested = None
        self.reattested = None

    @property
    def endpoint(self):
        return self.base + "/chat/completions"

    def attest(self, model):
        """What the router proved, attesting it first if it has not been lately. Not yet
        a proof to seal to: the session holds it to Strict, then accept()s it."""
        return attestation_for(model, self.enclave.current(), service=SERVICE)

    def accept(self, attestation):
        """The session has taken `attestation`: requests may be sealed to its proof."""
        self._attested = attestation.proof

    def models(self):
        """Tinfoil's chat models, fetched without the key."""
        return self.listing.fetch()

    def check_offered(self, model):
        """Raise ModelRetired, with what is offered instead, if Tinfoil no longer lists
        `model`. A list that cannot be read claims nothing."""
        entries = self.listing.known()
        if entries is not None and model not in entries:
            raise ModelRetired(model, sorted(entries), service=SERVICE_NAME)

    def _headers(self):
        return {"Content-Type": "application/json",
                "Accept-Encoding": netguard.ACCEPT_ENCODING,
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {self.api_key}"}

    def _sealed_client(self, verified):
        import httpx
        route = netguard.env_proxy_for(self.endpoint)
        wanted = (verified.hpke_public_key, route)
        if self._client is None or self._client_key != wanted:
            if self._client is not None:
                self._client.close()
            self._client = httpx.Client(
                transport=self._transport_factory(verified.hpke_public_key, route),
                timeout=httpx.Timeout(self.request_timeout, connect=CONNECT_TIMEOUT),
                follow_redirects=False)
            self._client_key = wanted
        return self._client

    @staticmethod
    def pin_for(host, verified):
        """A fresh pin for one request, on the attestation its client was made for."""
        return TlsPin(host, verified.tls_public_key)

    @contextmanager
    def request(self, model, payload):
        """Send one sealed, streamed request; yields the open response.

        Attested first: nothing goes to the host before the router has proved what it
        is, and the pin is the attested key. Retried once, after attesting again, if the
        router says the key has rotated (a 422). Anything else is an error, and nothing
        falls back."""
        import httpx
        from ehbp import EHBPError, KeyConfigMismatchError

        model = str(model).strip()
        for attempt in (0, 1):
            verified = self.enclave.current()
            if verified is not self._attested:
                if self.reattested is not None:
                    self.reattested(model, attestation_for(model, verified,
                                                           service=SERVICE))
                self._attested = verified
            entries = self.listing.known()
            if entries is not None and model not in entries:
                raise ModelRetired(model, sorted(entries), service=SERVICE_NAME)
            efforts = list((entries or {}).get(model, {}).get("reasoning_efforts") or [])
            body = direct_body({**payload, "model": model}, efforts)
            body[CACHE_SECRET_FIELD] = self.enclave.cache_secret
            client = self._sealed_client(verified)
            pin = self.pin_for(self.host, verified)
            try:
                response = client.send(
                    client.build_request("POST", self.endpoint, json=body,
                                         headers=self._headers(),
                                         extensions={"trace": pin}),
                    stream=True)
            except TlsPinMismatch:
                # Whatever answers at the host is not the enclave attested: attest
                # again before anything more is tried.
                self.enclave.invalidate(verified)
                raise
            except KeyConfigMismatchError as e:
                self.enclave.invalidate(verified)
                if attempt:
                    raise PrivacyError(f"Tinfoil's router refused our key twice: "
                                       f"{unsealed_words(e)}") from e
                continue
            except EHBPError as e:
                raise PrivacyError(f"the encrypted reply could not be opened: {e}") from e
            except httpx.TimeoutException as e:
                raise PrivacyError(f"no response within {self.request_timeout} "
                                   f"seconds") from e
            except httpx.HTTPError as e:
                cause = e.__context__ or e.__cause__
                if isinstance(cause, TlsPinMismatch):
                    self.enclave.invalidate(verified)
                    raise cause from e
                raise PrivacyError(f"the sealed request failed: {e}") from e
            coding = _unasked_coding(response)
            if coding:
                response.close()
                raise PrivacyError(f"Tinfoil answered compressed as {coding}, which was "
                                   f"not asked for; it was not read")
            if response.status_code >= 400:
                try:
                    text = _error_text(_body(response, netguard.MAX_ERROR_BYTES),
                                       response.status_code)
                except (httpx.HTTPError, PrivacyError):
                    text = f"HTTP {response.status_code}"
                response.close()
                if response.status_code in (404, 400):
                    self.check_offered(model)
                raise PrivacyError(f"HTTP {response.status_code}; Tinfoil said "
                                   f"(unauthenticated, not the model): {text}")
            if not 200 <= response.status_code < 300:
                response.close()
                if 300 <= response.status_code < 400:
                    raise PrivacyError(_redirected(self.endpoint, response) + " The "
                                       "sealed request was refused unread")
                raise PrivacyError(f"Tinfoil answered HTTP {response.status_code} "
                                   f"instead of a sealed reply, which was refused unread")
            try:
                yield response
            finally:
                response.close()
            return
        raise PrivacyError("Tinfoil's key kept changing; nothing was sent")

    lines = staticmethod(PrivateModeClient.lines)
    finish = staticmethod(PrivateModeClient.finish)

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None
