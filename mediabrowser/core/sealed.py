# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""nano-gpt Private Mode: end-to-end encrypted chat with an attested enclave.

nano-gpt's `private/*` models run in Tinfoil's enclaves. Unlike the `TEE/*` models,
whose prompts pass nano-gpt's gateway in the clear, a Private Mode request body is
sealed on this machine to a key only the attested enclave holds, and the reply comes
back sealed the same way. nano-gpt relays ciphertext: it sees the account, the model,
timing, sizes and cost.

nano-gpt ships this as a Node proxy (`@nanogpt/private-mode`), run with
`npx ...@latest`: the code that holds the plaintext would come from, and be updated by,
the party the encryption is meant to keep out. So, like StoryLoom (whose
providers/private_mode.py DA Assistant's sealed.py was ported from, and this from
that, Nov 2026), this client does what the proxy does with Tinfoil's own Python SDK,
pinned by hash in packaging/private-mode.lock, and runs no code from nano-gpt:

1. Attest (`Enclave.current`). Tinfoil's SecureClient.verify fetches the attestation
   bundle through nano-gpt's relay and checks it against the hardware vendor's roots
   (AMD SEV-SNP or Intel TDX) and Sigstore's record of the router release
   (tinfoilsh/confidential-model-router). The report binds the enclave's HPKE key.
   Through a relay the SDK accepts any genuine release, so the latest is looked up from
   Tinfoil directly and an older one is reported, not refused: rollouts lag.
2. Preflight. nano-gpt checks the balance, reserves a charge and names the upstream
   model. It issues a cache scope; we send its sha256 as the header it wants, but the
   sealed body carries our own random secret, so nobody else - nano-gpt included - can
   time our prompt cache.
3. Seal the body with EHBP (HPKE X25519, AES-256-GCM) to the attested key, and open the
   sealed reply. Only a 2xx reply is opened: one that is not sealed is refused by the
   transport, and any other status is refused here unread, since the transport hands
   it on as it came. So is a reply nano-gpt does not mark as Private Mode, and a stream
   that ends without its final chunk. Nothing is ever sent unsealed, and nothing falls
   back to the plain endpoint: ai.py refuses a private/ model everywhere but here.
   What the relay writes in an error - a refusal, an attestation bundle the SDK quotes,
   the title of a key mismatch - is never sealed, so an error quotes it as the relay's,
   short and on one line, with nothing in it a reader cannot see
   (private_ai.unsealed_words): it cannot pass for anything the model said. From DA
   Toolkit's copy of this module (Nov 2026).

The attestation is redone before a request once it is five minutes old (the proxy's
figure: laptop sleep, a key rotated underneath us), counted on a clock that runs on
while the machine sleeps (dcap.since_boot), and once more when the enclave says the
key has changed.
"""

import hashlib
import json
import secrets
import threading
import types
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from . import netguard
from .dcap import since_boot
from .private_ai import (
    E2EE,
    ENDPOINT_MOVED,
    Attestation,
    ModelRetired,
    PrivacyError,
    is_private_mode,
    offers_private_mode,
    unsealed_words,
)

RELAY_PATH = "/api/v1/private/tinfoil"
# Where the relay lists its models: public, no key is sent.
RELAY_MODELS_PATH = RELAY_PATH + "/models"
ROUTER_REPO = "tinfoilsh/confidential-model-router"
MAX_AGE_SECONDS = 5 * 60
PREFLIGHT_TIMEOUT_SECONDS = 30
CONNECT_TIMEOUT = 10
ENCLAVE_URL_HEADER = "X-Tinfoil-Enclave-Url"
REQUIRED_STEPS = ("verifyCode", "verifyEnclave", "compareMeasurements")
INSTALL_HINT = ("End-to-end encrypted models need Tinfoil's client, which is not "
                "installed: the AppImage carries it, and from source "
                "tools/install-private-mode.sh installs it; then restart the app.")

# The request fields Tinfoil's router takes (the proxy's
# PRIVATE_TINFOIL_CHAT_COMPLETION_BODY_FIELDS, less tools, which this app never sends).
# Anything else is dropped before sealing.
ALLOWED_FIELDS = frozenset({
    "chat_template_kwargs", "frequency_penalty", "logit_bias", "max_tokens", "messages",
    "model", "presence_penalty", "reasoning_effort", "response_format", "seed", "stop",
    "stream", "stream_options", "temperature", "top_p", "user",
})
# The router-only field that scopes the enclave's prompt cache: sealed with the body,
# never seen by nano-gpt.
CACHE_SECRET_FIELD = "user_cache_secret"


_known = None           # available()'s answer, once it has been worked out


def available():
    """(True, version) when Tinfoil's SDK can be imported, else (False, what to do).
    Worked out once: it cannot change without a restart, since vendored.py sets sys.path
    once.

    Slow the first time: importing the SDK brings in the OpenAI client and sigstore with
    it, some 1,200 modules and most of a second. The GUI thread asks known() instead, and
    has check() do this on a thread of its own."""
    global _known
    if _known is None:
        _known = _import_sdk()
    return _known


def _import_sdk():
    try:
        import ehbp  # noqa: F401
        import tinfoil  # noqa: F401
    except Exception:  # an ImportError, or a dependency built for another Python
        return False, INSTALL_HINT
    try:
        from importlib.metadata import version
        return True, f"tinfoil {version('tinfoil')}"
    except Exception:
        return True, "tinfoil"


def known():
    """available()'s answer if it has been worked out already, else None. Never imports
    anything, so it can be asked on the main loop."""
    return _known


def check(then=None):
    """Work out available() on a thread of its own, and call then(answer) from that
    thread. For a window that wants the answer without stopping for it: the import is
    the real one, so an SDK that is installed and will not load is still found here,
    not in the middle of a run."""
    def work():
        answer = available()
        if then is not None:
            then(answer)

    threading.Thread(target=work, daemon=True, name="sdk-check").start()


def relay_url(api_url):
    parsed = urlparse(api_url)
    return f"{parsed.scheme}://{parsed.netloc}{RELAY_PATH}"


def private_list_url(api_url):
    """Where the Private Mode relay lists its models: public, no key is sent."""
    parsed = urlparse(api_url)
    return f"{parsed.scheme}://{parsed.netloc}{RELAY_MODELS_PATH}"


def private_ids_in(body):
    """The private/ model ids in the relay's list, as JSON."""
    items = body.get("data") if isinstance(body, dict) else None
    return [item["id"] for item in items or []
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            and item["id"].startswith("private/")]


def twin_key(model_id):
    """What a TEE/ id and its private/ twin share: TEE/glm-5.3 <-> private/glm-5-3."""
    return model_id.split("/", 1)[-1].lower().replace(".", "-")


def offered(model_id, private_ids):
    """Whether the relay's list still has `model_id`: nano-gpt retires Private Mode
    models (private/glm-5-3-flash, Oct 2026). Its dotted alias is the same model, a
    :thinking one is not. None when the list is empty - one that could not be read -
    which says nothing either way. From DA Assistant's models.py."""
    private_ids = list(private_ids)
    if not private_ids:
        return None
    key = twin_key(model_id)
    return any(twin_key(i) == key for i in private_ids)


@dataclass(frozen=True)
class Verified:
    """What an attestation proved: the key to seal to, and the record."""

    hpke_public_key: str
    enclave: str
    hardware: str
    measurement: str
    release_digest: str
    release_tag: str
    latest_release: str
    latest_checked: bool


def _hardware(document):
    kind = str(((document.get("enclaveMeasurement") or {}).get("measurement") or {})
               .get("type"))
    if "sev-snp" in kind:
        return "AMD SEV-SNP"
    if "tdx" in kind:
        return "Intel TDX"
    return kind


class _GuardedRequests(types.ModuleType):
    """requests, as Tinfoil's SDK is given it: its functions each send through a session
    of netguard's (netguard.requests_session()), and Session is that session's class;
    everything else - its exceptions, Response, the rest - is requests' own."""

    def __init__(self, real):
        super().__init__(real.__name__, real.__doc__)
        self._real = real
        with netguard.requests_session() as made:
            self.Session = type(made)

    def __getattr__(self, name):
        if name == "_real":
            raise AttributeError(name)
        return getattr(self._real, name)

    def request(self, method, url, **kwargs):
        with netguard.requests_session() as session:
            return session.request(method=method, url=url, **kwargs)

    def get(self, url, params=None, **kwargs):
        return self.request("get", url, params=params, **kwargs)

    def options(self, url, **kwargs):
        return self.request("options", url, **kwargs)

    def head(self, url, **kwargs):
        kwargs.setdefault("allow_redirects", False)
        return self.request("head", url, **kwargs)

    def post(self, url, data=None, json=None, **kwargs):
        return self.request("post", url, data=data, json=json, **kwargs)

    def put(self, url, data=None, **kwargs):
        return self.request("put", url, data=data, **kwargs)

    def patch(self, url, data=None, **kwargs):
        return self.request("patch", url, data=data, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("delete", url, **kwargs)


# What a module may have taken from requests by name, as well as requests itself.
_REQUESTS_NAMES = ("request", "get", "options", "head", "post", "put", "patch", "delete",
                   "Session")


def guard_sdk_requests(package="tinfoil"):
    """Have Tinfoil's SDK fetch through netguard's session: a redirect then goes only
    where a request could have gone in the first place, and never from https to plain
    http. Returns the names of the modules given it.

    The SDK fetches with bare requests.get and .post - the router's latest release and
    its Sigstore bundle from GitHub's proxy (tinfoil.github), AMD's VCEK and Intel's
    collateral (attestation.verify_sev, .collateral_tdx), an enclave's report
    (attestation.attestation) - and requests follows a redirect anywhere, https to http
    included: a 30x from any of them, or from whatever answered at their address, and
    the next fetch went in the clear, through no check of the app's.

    It is not changed on disk. Each of its modules loaded is handed a stand-in for
    requests (_GuardedRequests) in place of the real one, and for any of its functions
    taken by name. Every module of the package is loaded first, so that one the SDK
    would import only when it needs it - another version may - is not missed; one that
    cannot be loaded is left, as the SDK could not use it either."""
    import importlib
    import pkgutil
    import sys

    import requests

    root = sys.modules.get(package)
    if root is None:
        return []
    try:
        for found in pkgutil.walk_packages(getattr(root, "__path__", []), package + ".",
                                           onerror=lambda _name: None):
            if found.name not in sys.modules:
                try:
                    importlib.import_module(found.name)
                except Exception:
                    pass
    except Exception:           # a package that cannot be walked: what is loaded is
        pass                    # still guarded below
    guarded, given = None, []
    for name, module in list(sys.modules.items()):
        if module is None or not (name == package or name.startswith(package + ".")):
            continue
        changed = False
        if getattr(module, "requests", None) is requests:
            guarded = guarded or _GuardedRequests(requests)
            module.requests = guarded
            changed = True
        for attribute in _REQUESTS_NAMES:
            if getattr(module, attribute, None) is getattr(requests, attribute):
                guarded = guarded or _GuardedRequests(requests)
                setattr(module, attribute, getattr(guarded, attribute))
                changed = True
        if changed or isinstance(getattr(module, "requests", None), _GuardedRequests):
            given.append(name)
    return sorted(given)


def tinfoil_verify(relay, cache_secret):
    """Attest Tinfoil's router through nano-gpt's relay (network; worker thread)."""
    # Imported here: the SDK pulls in sigstore and the OpenAI client, which nothing else
    # needs, and the app should not pay for them at startup.
    from tinfoil import SecureClient
    from tinfoil.github import fetch_latest_release
    guard_sdk_requests()

    # The secret is passed explicitly, or the SDK would create one in ~/.tinfoil; this
    # client only verifies, it never sends a request.
    client = SecureClient(base_url=relay + "/", attestation_bundle_url=relay,
                          transport="ehbp", user_cache_secret=cache_secret)
    truth = client.verify()
    document = client.get_verification_document().to_dict()
    steps = document.get("steps") or {}
    incomplete = [step for step in REQUIRED_STEPS
                  if (steps.get(step) or {}).get("status") != "success"]
    if document.get("securityVerified") is not True or incomplete \
            or not truth.hpke_public_key:
        raise PrivacyError("the enclave's attestation was incomplete"
                           + (f" ({', '.join(incomplete)} did not succeed)"
                              if incomplete else ""))
    digest = document.get("releaseDigest") or truth.digest
    tag, latest, checked = str(document.get("releaseTag") or ""), "", False
    try:
        release = fetch_latest_release(ROUTER_REPO)
        checked = True
        if release.digest == digest:
            tag = tag or release.tag
        else:
            latest = release.tag
    except Exception:  # reported, never fatal
        pass
    return Verified(hpke_public_key=truth.hpke_public_key, enclave=client.enclave,
                    hardware=_hardware(document),
                    measurement=str(document.get("enclaveFingerprint") or ""),
                    release_digest=str(digest or ""), release_tag=tag,
                    latest_release=latest, latest_checked=checked)


def _proxy_hint(error):
    """Why, where it is likely: Sigstore's trust root, which the attestation needs, not
    fetched through a proxy. Live through Tor (Nov 2026), Sigstore's servers refused 11
    exits in 12 with HTTP 403; another exit, later, may pass."""
    if "TUF" not in str(error) or not netguard.env_proxy_for("https://sigstore.dev"):
        return ""
    return (" - through the proxy, which Sigstore's servers refuse from most Tor exits; "
            "it may pass if tried again later")


def attestation_for(model, verified):
    release = verified.release_tag or f"release {verified.release_digest[:12]}"
    if verified.latest_release:
        currency = f"an older release than the latest, {verified.latest_release}"
    elif verified.latest_checked:
        currency = "the latest release"
    else:
        currency = "the latest release could not be looked up"
    measured = (f"measured by {verified.hardware}" if verified.hardware
                else "measured by its hardware")
    detail = (f"{verified.enclave} runs {ROUTER_REPO} {release} ({currency}), {measured}; "
              f"its HPKE key sha256 "
              f"{hashlib.sha256(verified.hpke_public_key.encode()).hexdigest()[:16]}…")
    return Attestation(model=model, kind=E2EE, level="full", detail=detail,
                       enclave=verified.enclave, hardware=verified.hardware,
                       release=release, latest_release=verified.latest_release,
                       release_checked=verified.latest_checked, proof=verified)


class Enclave:
    """The attested enclave one operation seals to. Thread-safe."""

    def __init__(self, relay, verifier=tinfoil_verify, clock=since_boot):
        self.relay = relay
        # Ours, not nano-gpt's: see the module docstring. One per operation, never
        # written anywhere.
        self.cache_secret = secrets.token_hex(32)
        self._verifier = verifier
        self._clock = clock
        self._lock = threading.Lock()
        self._verified = None
        self._at = 0.0

    def current(self):
        """The attested key, attesting first if there is none or it has aged."""
        with self._lock:
            fresh = (self._verified is not None
                     and 0 <= self._clock() - self._at < MAX_AGE_SECONDS)
            if not fresh:
                self._verified = None
                try:
                    self._verified = self._verifier(self.relay, self.cache_secret)
                except PrivacyError:
                    raise
                except Exception as e:  # the SDK's own errors
                    # They can quote the bundle, which came through the relay.
                    raise PrivacyError(
                        f"the private model's enclave could not be attested: "
                        f"{unsealed_words(e, 500)}" + _proxy_hint(e)) from e
                self._at = self._clock()
            return self._verified

    def invalidate(self, verified=None):
        """Forget the key (it was rotated): the next request attests again."""
        with self._lock:
            if verified is None or self._verified is verified:
                self._verified = None


def _sealing_transport(public_key, route=None):
    """EHBP over a transport that goes through `route`, the proxy, if there is one, and
    sends nothing EHBP did not seal.

    httpx takes no proxy from the environment for a client given a transport of its
    own, so this one is told (netguard.env_proxy_for): without it the sealed request,
    alone of all the app's, would go direct - from this machine's own address, Tor or no
    Tor, beside a preflight that had gone through the proxy.

    EHBP seals a request that has a body, and passes one it finds none in on as it came,
    unsealed, for nano-gpt's relay to read - as it would anything another version of it
    left unsealed. So the transport under it refuses any request that does not carry
    EHBP's encapsulated key: nothing goes to the enclave but what was sealed to it, as
    in the other apps' copies of this client (DA Vibe Manager's, Nov 2026)."""
    import httpx
    from ehbp import EHBPTransport
    from ehbp.protocol import ENCAPSULATED_KEY_HEADER

    class SealedOnly(httpx.HTTPTransport):
        def handle_request(self, request):
            if ENCAPSULATED_KEY_HEADER not in request.headers:  # read case-insensitively
                raise PrivacyError("a request to the enclave was not sealed, so it was "
                                   "refused, and nothing was sent")
            return super().handle_request(request)

    inner = SealedOnly(proxy=route, limits=httpx.Limits(max_keepalive_connections=0))
    return EHBPTransport.from_public_key_hex(public_key, inner=inner)


class PrivateModeClient:
    """Sends private/ models' requests sealed, through nano-gpt's relay."""

    def __init__(self, api_url, api_key, request_timeout=180, enclave=None,
                 transport_factory=_sealing_transport, preflight_client=None):
        if not offers_private_mode(api_url):
            raise PrivacyError("end-to-end encrypted models are nano-gpt's Private Mode, "
                               "and the API URL is not nano-gpt's")
        # The key goes with the preflight, which httpx sends, not netguard's session:
        # an address written http:// by hand, or brought back by a restore, is refused
        # here as well as where the session starts.
        insecure = netguard.problem(api_url)
        if insecure:
            raise PrivacyError(insecure)
        ok, message = available() if enclave is None else (True, "")
        if not ok:
            raise PrivacyError(message)
        self.api_url = api_url
        self.api_key = api_key
        self.request_timeout = request_timeout
        self.enclave = enclave or Enclave(relay_url(api_url))
        self._transport_factory = transport_factory
        self._preflight_client = preflight_client
        self._client = None
        self._client_key = None
        # The proof the session last accepted - accept() hands it over - and what to
        # tell of any other before anything is sealed to it, in request():
        # PrivacySession._reattested, which raises to refuse it.
        self._attested = None
        self.reattested = None

    @property
    def endpoint(self):
        return self.enclave.relay + "/v1/chat/completions"

    def attest(self, model):
        """What the enclave proved, attesting it first if it has not been lately. Not
        yet a proof to seal to: the session holds it to Strict, then accept()s it."""
        return attestation_for(model, self.enclave.current())

    def accept(self, attestation):
        """The session has taken `attestation`, Strict and all: requests may be sealed
        to the proof it was made from without asking the session again.

        Only once it has. attest() used to take the proof itself, before Strict had
        seen it, and a request already past its own check - on another thread, or for
        another private/ model of the operation - went out sealed to an enclave
        Strict refused a moment later."""
        self._attested = attestation.proof

    def private_ids(self):
        """nano-gpt's list of Private Mode models - public, no key is sent - read on the
        preflight's client. [] when it cannot be read."""
        import httpx
        client = self._preflight_client or httpx.Client(follow_redirects=False)
        try:
            response = client.get(private_list_url(self.api_url),
                                  timeout=PREFLIGHT_TIMEOUT_SECONDS)
            return private_ids_in(response.json() if response.status_code < 400 else {})
        except (httpx.HTTPError, ValueError):
            return []
        finally:
            if self._preflight_client is None:
                client.close()

    def check_offered(self, model):
        """Raise ModelRetired, with what is offered instead, if nano-gpt has retired
        `model`. A list that cannot be read claims nothing: the preflight still refuses
        a model that is gone."""
        ids = self.private_ids()
        if offered(model, ids) is False:
            raise ModelRetired(model, ids)

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            unfit = netguard.key_problem(self.api_key, "NanoGPT")
            if unfit:
                raise PrivacyError(unfit)
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _sealed_client(self, verified):
        import httpx
        # The environment's proxy, read at each request as urllib reads it for every
        # other request of the app's.
        route = netguard.env_proxy_for(self.endpoint)
        wanted = (verified.hpke_public_key, route)
        if self._client is None or self._client_key != wanted:
            if self._client is not None:
                self._client.close()
            self._client = httpx.Client(
                transport=self._transport_factory(verified.hpke_public_key, route),
                timeout=httpx.Timeout(self.request_timeout, connect=CONNECT_TIMEOUT),
                # A redirect would carry the sealed body somewhere else, so none is
                # followed: it comes back as the 3xx it is, which request() refuses
                # unread, as it refuses anything but a 2xx.
                follow_redirects=False)
            self._client_key = wanted
        return self._client

    def _preflight(self, model, body_bytes):
        """nano-gpt's check before a sealed request: (cache scope, upstream model)."""
        import httpx
        client = self._preflight_client or httpx.Client(follow_redirects=False)
        try:
            response = client.post(self.enclave.relay + "/preflight",
                                   json={"model": model, "requestBodyBytes": body_bytes},
                                   headers=self._headers(),
                                   timeout=PREFLIGHT_TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            raise PrivacyError(f"nano-gpt's Private Mode check failed: {e}") from e
        finally:
            if self._preflight_client is None:
                client.close()
        if response.status_code >= 400:
            # Every path's backstop - a model retired part way through a run, the connection
            # test: say why, not just that it was refused.
            self.check_offered(model)
            raise PrivacyError(f"nano-gpt refused the Private Mode request "
                               f"(HTTP {response.status_code}; its relay said: "
                               f"{_error_text(response)})")
        if response.status_code >= 300:
            raise _redirected(self.enclave.relay + "/preflight", response)
        try:
            data = response.json()
        except ValueError as e:
            raise PrivacyError("nano-gpt's Private Mode check was not JSON") from e
        scope = str(data.get("cacheScope") or "").strip()
        upstream = str(data.get("upstreamModel") or "").strip()
        if len(scope) != 64 or not upstream:
            raise PrivacyError("nano-gpt's Private Mode check did not say where to send it")
        return scope, upstream

    @contextmanager
    def request(self, model, payload):
        """Send one sealed, streamed request; yields the open response.

        Attested first: nothing, not even nano-gpt's charge, before the enclave has
        proved what it is. Retried once, after attesting again, if the enclave says the
        key has rotated or nano-gpt says the body was not sealed - the proxy's one safe
        retry. Anything else is an error, and nothing falls back.
        """
        import httpx
        from ehbp import EHBPError, KeyConfigMismatchError

        if not is_private_mode(model):
            raise PrivacyError(f"{model} is not a Private Mode model; nothing was sent")
        for attempt in (0, 1):
            verified = self.enclave.current()
            if verified is not self._attested:
                # Not the proof the session accepted: attested here - the key rotated
                # (the retry below), or the proof aged since - or for another request,
                # and perhaps refused there. It is held to Strict as any other is, and
                # the banner names the enclave that is sealed to, before anything is
                # sent.
                if self.reattested is not None:
                    self.reattested(model, attestation_for(model, verified))
                self._attested = verified
            client = self._sealed_client(verified)
            size = len(json.dumps(payload).encode("utf-8"))
            scope, upstream = self._preflight(model, size)
            body = shape_body(payload, model, upstream)
            body[CACHE_SECRET_FIELD] = self.enclave.cache_secret
            headers = {
                **self._headers(),
                "Accept": "text/event-stream",
                "x-nanogpt-private-model": model,
                "x-nanogpt-private-stream": "true",
                "x-nanogpt-private-cache-scope": hashlib.sha256(scope.encode()).hexdigest(),
                "x-query-source": "api",
                ENCLAVE_URL_HEADER: f"https://{verified.enclave}",
            }
            try:
                response = client.send(
                    client.build_request("POST", self.endpoint, json=body,
                                         headers=headers),
                    stream=True)
            except KeyConfigMismatchError as e:
                self.enclave.invalidate(verified)
                if attempt:
                    # Its title is the relay's: the 422 that says so is not sealed.
                    raise PrivacyError(f"the enclave refused our key twice: "
                                       f"{unsealed_words(e)}") from e
                continue
            except EHBPError as e:
                raise PrivacyError(f"the encrypted reply could not be opened: {e}") from e
            except httpx.TimeoutException as e:
                raise PrivacyError(f"no response within {self.request_timeout} "
                                   f"seconds") from e
            except httpx.HTTPError as e:
                raise PrivacyError(f"the sealed request failed: {e}") from e

            if response.status_code >= 400:
                response.read()
                text = _error_text(response)
                response.close()
                if attempt == 0 and response.status_code == 400 \
                        and "missing_encrypted_body_header" in text:
                    self.enclave.invalidate(verified)
                    continue
                raise PrivacyError(f"HTTP {response.status_code}; nano-gpt's relay said "
                                   f"(unauthenticated, not the model): {text}")
            if 300 <= response.status_code < 400:
                # Not followed (follow_redirects=False): it would carry the key and the
                # sealed body to wherever it points. Said, with where to.
                response.close()
                raise _redirected(self.endpoint, response)
            if not 200 <= response.status_code < 300:
                # Only a 2xx reply is opened. EHBP hands on any other that carries no
                # response nonce just as it came, unsealed (ehbp's
                # response_nonce_for_status), so a 3xx marked as Private Mode with a
                # plaintext stream in it would have been read, and shown, as a sealed
                # reply.
                response.close()
                raise PrivacyError(f"nano-gpt's relay answered HTTP "
                                   f"{response.status_code} instead of a sealed reply, "
                                   f"which was refused unread")
            if response.headers.get("x-nanogpt-private-mode") != "tinfoil":
                response.close()
                raise PrivacyError("the reply did not come back through Private Mode; "
                                   "refused")
            try:
                yield response
            finally:
                response.close()
            return
        raise PrivacyError("the enclave's key kept changing; nothing was sent")

    @staticmethod
    def lines(response):
        """The decrypted lines of a sealed stream."""
        import httpx
        from ehbp import EHBPError
        try:
            yield from response.iter_lines()
        except EHBPError as e:
            raise PrivacyError(f"the encrypted reply could not be opened: {e}") from e
        except httpx.HTTPError as e:
            raise PrivacyError(f"the sealed reply was cut off: {e}") from e

    @staticmethod
    def finish(finish_reason, saw_done):
        if not (finish_reason and saw_done):
            # The proxy's private_stream_incomplete: whatever came may be partial.
            raise PrivacyError("the encrypted reply ended before it was complete")


def _redirected(url, response):
    """A redirect from nano-gpt's relay, as the error to raise: the client follows none
    (each request carries the key, and a sealed one what was sent), so where it pointed
    is said, without a password, and where a moved endpoint's address goes. As
    netguard.Redirected says it for the app's other requests."""
    location = response.headers.get("location")
    target = urljoin(url, location) if location else None
    return PrivacyError(netguard.redirect_said(url, response.status_code, target,
                                               ENDPOINT_MOVED))


def _error_text(response):
    """What the relay wrote in an error, as an error may quote it: never sealed, so on
    one line, short and with nothing a reader cannot see (private_ai.unsealed_words)."""
    try:
        body = response.json()
    except ValueError:
        return unsealed_words(response.text) or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return unsealed_words(error["message"])
        if isinstance(error, str):
            return unsealed_words(error)
        if body.get("message"):
            return unsealed_words(body["message"])
    return unsealed_words(json.dumps(body))


# How each family's router spells reasoning effort (the proxy's requestTransforms.js).
# GLM and Kimi take low, high or max; medium has no spelling of its own there.
_EFFORT = {"low": "low", "minimal": "low", "medium": "high", "high": "high", "max": "max"}


def shape_body(payload, model, upstream):
    """The body the router takes, as nano-gpt's proxy shapes it for each model family
    (its requestTransforms.js), for the fields this app sends.

    `payload["reasoning"]`, when ai.py sets it, is {"effort": ...}: how hard a model
    that reasons should think. It never switches thinking on for a model that can do
    without it (deepseek-v4, Gemma): only the :thinking variants think there.
    """
    body = dict(payload)
    body["model"] = upstream
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    reasoning = body.pop("reasoning", None)
    effort = str(reasoning.get("effort") or "").lower() if isinstance(reasoning, dict) \
        else ""
    thinking_suffix = model.endswith(":thinking")
    kwargs = dict(body.get("chat_template_kwargs") or {})
    if upstream.startswith("glm-5"):
        # Always reasons, and at length unless told otherwise: live (Sept 2026), a
        # 5,000-character translation spent a 20,480-token limit reasoning and never
        # began its answer. The effort goes at the top level.
        kwargs["thinking"] = True
        if effort:
            body["reasoning_effort"] = _EFFORT.get(effort, "low")
    elif upstream.startswith("deepseek-v4"):
        kwargs["thinking"] = thinking_suffix
        if thinking_suffix and effort:
            kwargs["reasoning_effort"] = ("low" if effort in ("low", "minimal")
                                         else "high" if effort == "medium" else "xhigh")
    elif upstream.startswith("gemma"):
        kwargs["enable_thinking"] = thinking_suffix
    elif upstream.startswith("kimi"):
        # Kimi K3's template fixes its own sampling and always reasons.
        for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
            body.pop(key, None)
        if effort:
            body["reasoning_effort"] = _EFFORT.get(effort, "low")
    if kwargs:
        body["chat_template_kwargs"] = kwargs
    return {key: value for key, value in body.items() if key in ALLOWED_FIELDS}
