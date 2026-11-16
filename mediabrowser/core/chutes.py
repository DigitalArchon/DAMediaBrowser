# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Chutes: its ordinary models, and its end-to-end encrypted -TEE models, each message
sealed on this computer to the key of one Chutes instance whose TDX quote, for our nonce,
binds that key. Preliminary, as SealedLore's is (built and tested without an account,
then run with its author's on Nov 15 2026: five instances discovered and each attested,
its key binding held live, sealed replies opened, a nonce never used twice).

SealedLore's providers/chutes_provider.py and chutes_catalog.py, in this app's shape:
requests through a requests-like Session (netguard.Session(): no redirect followed, an
answer read only so far, the environment's proxy honoured), the instance checks
private_ai.TeeClient's. Shared with DA Assistant line for line (this copy differs only
in this docstring, its imports and the hooks tests/test_shared_code.py lists). Needs
nothing of Privacy Mode's own environment but cryptography with ML-KEM-768, which both
of this app's locks pin (50.0.2); where the cryptography in use has none, it refuses in
words (chutes_e2ee.mlkem_refusal).

**Ordinary models** (`fetch_models`, `listed_models`): a Chutes endpoint (llm.chutes.ai,
or claude.chutes.ai speaking Anthropic's Messages API) offers every model Chutes lists,
from its public list at llm.chutes.ai/v1/models, read without the key. Its -TEE models
(services.chutes_tee) come here, always, whatever the endpoint speaks; the others go
through the endpoint's ordinary client, which refuses a -TEE one (levels.plain_refusal).

**The end-to-end encrypted models** (`ChutesClient`), for each request (SealedLore's
design, e2ee-providers.md 2.3):

1. Discovery (`_discover`): `GET api.chutes.ai/e2e/instances/{chute_id}`, with the key:
   at most five instances, each with its ML-KEM-768 key and ten single-use nonces, good
   for 60 seconds. The chute comes from the model list, its confidential models alone.
2. Attestation (`_attest_pool`), before any nonce of an instance is used: every instance
   not attested in the last REATTEST_SECONDS (by its id and its key's hash, on a clock
   that runs on while the machine sleeps) is. Chutes' evidence is limited to 60 calls a
   minute for all its users together, so as few are made as will do: one for the whole
   chute (`/chutes/{id}/evidence?nonce=`, keyless), or, for an instance first seen
   since the last of those, its own (`/instances/{id}/evidence`, with the key). A 429
   is waited out within ATTEMPT_SECONDS only. Each instance's evidence goes through
   private_ai.TeeClient's checks (Intel's DCAP chain, no debug TD, the TLS certificate's
   key bound into the quote and signing our nonce and that quote, NVIDIA's verdict on
   the GPUs for the nonce the quote binds), then two of this client's own: the quote's
   report data begins with sha256(nonce + its key), so the hardware vouches for the key
   a request is sealed to (`key_bound`), and its MRTD and RTMR0-3 are a set Chutes
   publishes (`measured`). An instance whose GPUs NVIDIA did not vouch for is left out.
   One that fails is left out, said, and never sealed to; with none left the model is
   refused. Leaving one out is safe here, unlike for TEE/ models through NanoGPT: this
   client chooses the instance, and what is sealed to one key cannot be opened by
   another.
3. The request (`request`): sealed to the chosen instance, `POST /e2e/invoke`, streamed,
   the nonce used once and never again. The reply's pieces are each opened with the
   stream key made for our response key, and each held to its number
   (chutes_e2ee.StreamOpener), so a piece dropped, repeated or out of its place, and a
   stream that ends before the sealed end, is refused in words and nothing of the
   refused piece is shown. A seal that does not hold refuses and doubts every
   instance's attestation, so they are attested again before the next request
   (`ChutesState.doubt`). Anything Chutes writes in refusing is said as its,
   unauthenticated.

The attestation is always partial (private_ai.WORKLOAD_UNMEASURED, MEASUREMENTS_LISTED):
the workload that makes and uses the key (chutes-aegis.so, a closed binary) is admitted
by Chutes' signature, not measured, and the measured sets are Chutes' list.

**The key** goes from an endpoint at one of Chutes' own origins, the key saved for one
of them (services.CHUTES_KEY_ORIGINS: llm.chutes.ai, api.chutes.ai, claude.chutes.ai),
to https://api.chutes.ai, over https, and nowhere else (services.chutes_key_refusal):
the one stated exception to "a key goes only to the origin it was entered for". The
attestation's fetches of Intel's and NVIDIA's carry no key. Nothing is ever redirected,
and nothing is sent unsealed but the discovery, the evidence and the two public lists.
"""

import hashlib
import json
import random
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urljoin

from . import netguard, safejson, services
from .chutes_e2ee import (
    SealBroken,
    SealError,
    StreamOpener,
    confidential_models,
    is_chute_id,
    key_bound,
    listed_models,
    measured,
    mlkem_refusal,
    public_key,
    published_measurements,
    seal_request,
)
from .dcap import since_boot
from .netguard import LineTooLong
from .netguard import lines_of as read_lines
from .private_ai import (
    ATTEMPT_SECONDS,
    ATTESTATION_TIMEOUT_SECONDS,
    E2EE,
    GPU_SHORTFALLS,
    GPUS_UNVERIFIED,
    KEY_BINDING_UNCHECKED,
    MEASUREMENTS_LISTED,
    NO_GPU_EVIDENCE,
    TEE_MAX_AGE_SECONDS,
    WORKLOAD_UNMEASURED,
    ModelRetired,
    PrivacyError,
    TeeClient,
    _Checks,
    unsealed_words,
)

SERVICE = "chutes"
SERVICE_NAME = "Chutes"
CHUTES_ORIGIN = services.CHUTES_E2EE_ORIGIN
MODELS_URL = "https://llm.chutes.ai/v1/models"
MEASUREMENTS_URL = CHUTES_ORIGIN + "/servers/tdx/measurements"
INVOKE_URL = CHUTES_ORIGIN + "/e2e/invoke"
# What the instance's workload maps to its model's own route (chutes run.py).
E2E_PATH = "/v1/chat/completions"
# Discovery: at most this many instances, each with this many nonces (chutes-api
# router.py), good for NONCE_LIFE_SECONDS; a nonce is used only while this much of its
# life is left, for the request to reach Chutes in time.
MAX_INSTANCES = 5
MAX_NONCES = 10
NONCE_LIFE_SECONDS = 60
NONCE_MARGIN_SECONDS = 10
DISCOVERY_TIMEOUT_SECONDS = 30
LISTING_TIMEOUT_SECONDS = 30
# The model list and the published measurements are read again after this.
LISTING_SECONDS = 60 * 60
# An instance's attestation stands this long (privacy.TEE_MAX_AGE_SECONDS, as a TEE/
# model's): one started since is attested before it is used.
REATTEST_SECONDS = TEE_MAX_AGE_SECONDS
# Since the last whole chute's evidence, an instance first seen is attested with its own
# evidence, while there are this many or fewer: more, and the chute's again.
INSTANCE_LEVEL_MOST = 2
# A 429 asking to wait this long, or more than the attempt has left, is not waited out.
EVIDENCE_WAIT_DEFAULT_SECONDS = 5.0
SAID_CHARS = 200
# What a sealed request carries of the body the app builds (the rest is dropped before
# sealing): what vLLM's chat route takes.
ALLOWED_FIELDS = frozenset({
    "chat_template_kwargs", "frequency_penalty", "logit_bias", "max_tokens", "messages",
    "model", "presence_penalty", "reasoning_effort", "response_format", "seed", "stop",
    "stream", "stream_options", "temperature", "top_p", "user",
})
# Chutes' models run on vLLM, which takes no reasoning effort for them: their chat
# template switches thinking on or off. Live (SealedLore, Nov 15 2026, every -TEE model,
# a one-line question): sent nothing, the Qwen models thought 435-468 tokens and ran out
# of a 400-token limit before a word; with these, none at all. A template that knows
# neither name is unchanged.
THINKING_OFF = {"enable_thinking": False, "thinking": False}
THINKING_OFF_EFFORTS = frozenset({"none", "minimal", "low"})
# A discovery nonce as Chutes makes them (secrets.token_urlsafe(24)): nothing else goes
# into a header.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}")
EVIDENCE_LIMIT = ("Chutes allows 60 calls a minute for everyone's evidence together, and "
                  "the limit was reached; this may pass if tried again in a minute")


class NotOffered(ModelRetired):
    """Chutes does not list the model as one that runs in a TEE: never sent. A variant
    ("...-TEE:THINKING") is none of those Chutes lists either: its router reads the
    variant on the ordinary route alone, which is in the clear."""

    def __init__(self, model, offered):
        super().__init__(model, offered, service="Chutes' end-to-end encryption")
        variant = ":" in model
        self.args = (f"{unsealed_words(model, 120)} is not one of Chutes' end-to-end "
                     f"encrypted models (those it lists as confidential compute); "
                     f"nothing was sent"
                     + (". A variant after a colon is not one Chutes lists: choose the "
                        "model itself" if variant else ""),)


# ---- the public list, for an ordinary Chutes endpoint too -------------------------------

def _public_json(session, url, what, timeout=LISTING_TIMEOUT_SECONDS, params=None):
    """A public list (no key, nothing of ours in it): its JSON, or PrivacyError."""
    try:
        response = session.get(url, params=params, timeout=timeout, allow_redirects=False)
    except netguard.RequestError as e:
        raise PrivacyError(f"{what} could not be read: {unsealed_words(e, 300)}") from e
    if 300 <= response.status_code < 400:
        location = response.headers.get("location")
        raise PrivacyError(f"{what} could not be read: " + netguard.redirected(
            url, urljoin(url, location) if location else "", response.status_code))
    if response.status_code != 200:
        raise PrivacyError(f"{what} could not be read (HTTP {response.status_code})")
    try:
        return response.json()
    except ValueError as e:
        raise PrivacyError(f"{what} is not JSON") from e


def fetch_models(session=None):
    """Every model Chutes lists, by id (chutes_e2ee.listed_models), read without the key
    and with nothing of ours in the request: for an ordinary Chutes endpoint's model list,
    whatever it speaks (claude.chutes.ai's own listing is not documented)."""
    own = session is None
    session = session or netguard.Session()
    try:
        return listed_models(_public_json(session, MODELS_URL, "Chutes' model list"))
    finally:
        if own:
            session.close()


# ---- the state a client and its copies share --------------------------------------------

@dataclass
class _Instance:
    """One instance as discovery lists it: its id, key and unused nonces."""

    instance_id: str
    pubkey: str
    nonces: list

    @property
    def key_sha256(self):
        return hashlib.sha256(self.pubkey.encode()).hexdigest()


@dataclass
class _Pool:
    """A discovery's instances, good until `until` (on the state's clock)."""

    chute_id: str
    instances: list
    until: float
    unusable: dict = field(default_factory=dict)


@dataclass(frozen=True)
class InstanceState:
    """What attesting one instance found: attested (with what its checks found and the
    published set it matched) or left out, and why."""

    instance_id: str
    key_sha256: str
    attested_at: float
    passed: bool
    why: str = ""
    checks: object = None
    measurement: str = ""


class ChutesState:
    """What a client and its copies share: the lists, the instances' attestations,
    discovery's nonces. Thread-safe."""

    def __init__(self, clock=since_boot):
        self.clock = clock
        self.lock = threading.Lock()
        # One attestation at a time: a second request waits, then finds it done.
        self.attesting = threading.Lock()
        self.models = None
        self.models_at = 0.0
        self.measurements = None
        self.measurements_at = 0.0
        self.instances = {}
        self.pools = {}
        self.chute_level_at = {}
        self.attestations = {}

    def fresh(self, instance):
        """The instance's attestation while it is in force (REATTEST_SECONDS)."""
        state = self.instances.get((instance.instance_id, instance.key_sha256))
        if state is not None and 0 <= self.clock() - state.attested_at < REATTEST_SECONDS:
            return state
        return None

    def doubt(self):
        """A reply's seal did not hold: every instance is attested again, with the whole
        chute's evidence, and discovered again, before the next request."""
        with self.lock:
            self.instances.clear()
            self.pools.clear()
            self.chute_level_at.clear()
            self.attestations.clear()


def gpus_unproved(check, short):
    """Why an instance whose GPUs NVIDIA did not vouch for is left out, and whether only
    for now (NVIDIA's service or its keys out of reach: tried again next time); None
    when its verdict held."""
    found = [what for what in check.shortfalls if what in GPU_SHORTFALLS]
    if not found:
        return None
    if NO_GPU_EVIDENCE in found:
        return f"instance {short} offered no GPU evidence for NVIDIA to check", False
    if GPUS_UNVERIFIED in found:
        return (f"instance {short}'s GPUs could not be checked just now: NVIDIA's "
                "attestation service could not be reached"), True
    return (f"instance {short}'s GPUs could not be checked just now: NVIDIA's signing "
            "keys could not be fetched, so its verdict is unchecked"), True


class ChutesStream:
    """One sealed request's open reply, and what opens it."""

    def __init__(self, response, opener, instance_id):
        self.response = response
        self.opener = opener
        self.instance_id = instance_id

    @property
    def usage(self):
        """The model server's own count, sealed; else Chutes' unsealed copy, its word."""
        return self.opener.usage or self.opener.plain_usage


class ChutesClient:
    """Sends a Chutes -TEE model's requests sealed to an attested instance of it. The same
    shape as sealed.PrivateModeClient (attest, accept, request, lines, finish), so a
    private conversation sends through either alike.

    `api_url`: the Chutes endpoint's address as set (llm.chutes.ai/v1 or
    claude.chutes.ai); `key_saved_for`: the origin the key was saved for (config
    key_origins). `session`: a requests-like Session for Chutes' own requests (follows no
    redirect, reads an answer only so far); `checks_session`, `intel` and `nvidia`:
    TeeClient's, for Intel's and NVIDIA's checks (replaceable for tests)."""

    def __init__(self, api_url, api_key, key_saved_for="", request_timeout=180,
                 session=None, checks_session=None, intel=None, nvidia=None, state=None,
                 clock=since_boot):
        self.api_url = str(api_url or "").strip()
        self.api_key = (api_key or "").strip()
        self.key_saved_for = key_saved_for
        self.request_timeout = request_timeout
        self.session = session or netguard.Session()
        self.state = state or ChutesState(clock)
        self._checks_session = checks_session
        self._intel = intel
        self._nvidia = nvidia
        self._wake = threading.Event()
        self.last_instance = None
        self.reattested = None
        self._accepted = None

    def stop(self):
        """Stop waiting out Chutes' evidence limit, at once."""
        self._wake.set()

    # ---- the public lists, keyless -----------------------------------------------------

    def models(self):
        """Chutes' end-to-end encrypted models (confidential_models), priced."""
        state = self.state
        with state.lock:
            if state.models is not None and \
                    0 <= state.clock() - state.models_at < LISTING_SECONDS:
                return state.models
        try:
            listing = _public_json(self.session, MODELS_URL, "Chutes' model list")
        except PrivacyError:
            with state.lock:
                if state.models is not None:
                    return state.models     # an older list still says which is which
            raise
        found = confidential_models(listing)
        with state.lock:
            state.models, state.models_at = found, state.clock()
        return found

    def _measurements(self):
        state = self.state
        with state.lock:
            if state.measurements is not None and \
                    0 <= state.clock() - state.measurements_at < LISTING_SECONDS:
                return state.measurements
        try:
            sets = published_measurements(_public_json(
                self.session, MEASUREMENTS_URL, "Chutes' published measurements"))
        except PrivacyError as e:
            raise PrivacyError(f"{unsealed_words(e, 500)}, so no instance of Chutes' "
                               f"can be checked; nothing was sent") from e
        with state.lock:
            state.measurements, state.measurements_at = sets, state.clock()
        return sets

    def check_offered(self, model):
        """Raise NotOffered (with what is offered) if Chutes does not list `model`."""
        self._chute_for(model)

    def _chute_for(self, model):
        bare = services.bare_model(model)
        found = self.models()
        entry = found.get(bare)
        if entry is None:
            raise NotOffered(bare, sorted(found))
        return bare, str(entry["chute_id"])

    # ---- HTTP -----------------------------------------------------------------------------

    def _key(self):
        """The key, once it may go to api.chutes.ai: refused, nothing sent, otherwise."""
        refused = services.chutes_key_refusal(self.api_url, self.key_saved_for)
        if refused:
            raise PrivacyError(refused)
        if not self.api_key:
            raise PrivacyError("Chutes' end-to-end encrypted models need a Chutes API "
                               "key; nothing was sent")
        unfit = netguard.key_problem(self.api_key, SERVICE_NAME)
        if unfit:
            raise PrivacyError(unfit)
        return self.api_key

    def _request(self, method, url, keyed=False, timeout=30, params=None, data=None,
                 headers=None, stream=False):
        """One request to Chutes: its response (a refusal's words read to 64 KB, any
        other answer to netguard.MAX_BODY_BYTES unless streamed). The key goes only to
        api.chutes.ai; no redirect is followed, nor read as anything but a refusal."""
        headers = dict(headers or {})
        if keyed:
            key = self._key()
            if services.origin(url) != services.origin(CHUTES_ORIGIN):
                raise PrivacyError(f"refusing to send the Chutes key to "
                                   f"{netguard.shown(url)}")
            headers["Authorization"] = f"Bearer {key}"
        try:
            response = self.session.request(method, url, params=params, data=data,
                                            headers=headers, timeout=timeout,
                                            stream=stream, allow_redirects=False)
        except netguard.RequestError as e:
            raise PrivacyError(f"{services.host_of(url)} could not be reached: "
                               f"{unsealed_words(e, 300)}") from e
        if 300 <= response.status_code < 400:
            location = response.headers.get("location")
            response.close()
            raise PrivacyError(netguard.redirected(
                url, urljoin(url, location) if location else "", response.status_code))
        return response

    @staticmethod
    def _said(body):
        """What Chutes wrote in refusing, as it may be quoted (never sealed)."""
        try:
            data = safejson.loads(body)
        except ValueError:
            return unsealed_words(body.decode("utf-8", errors="replace"), SAID_CHARS)
        if isinstance(data, dict):
            data = data.get("detail") or data.get("error") or data.get("message") or ""
            if isinstance(data, dict):
                data = data.get("message") or ""
        return unsealed_words(str(data), SAID_CHARS) \
            if isinstance(data, (str, int, float)) else ""

    def _refusal(self, what, response):
        said = self._said(response.content or b"")
        error = PrivacyError(f"{what} (HTTP {response.status_code})"
                             + (f"; Chutes said (unauthenticated): {said}" if said else ""))
        error.status_code = response.status_code
        retry = response.headers.get("retry-after")
        try:
            error.retry_after = float(retry) if retry is not None else None
        except ValueError:
            error.retry_after = None
        return error

    @staticmethod
    def _json(response, what):
        try:
            data = response.json()
        except ValueError as e:
            raise PrivacyError(f"{what} is not JSON") from e
        return data

    # ---- discovery ------------------------------------------------------------------------

    def _discover(self, model, chute_id):
        """Discovery's instances, keys and nonces (with the key), held to their shape."""
        asked = self.state.clock()
        response = self._request("GET", f"{CHUTES_ORIGIN}/e2e/instances/{chute_id}",
                                 keyed=True, timeout=DISCOVERY_TIMEOUT_SECONDS)
        if response.status_code != 200:
            raise self._refusal(f"Chutes listed no instance of {model} to seal to",
                                response)
        data = self._json(response, "Chutes' list of instances")
        listed = data.get("instances") if isinstance(data, dict) else None
        if not isinstance(listed, list) or not listed:
            raise PrivacyError(f"Chutes listed no instance of {model} to seal to")
        life = data.get("nonce_expires_in")
        if isinstance(life, bool) or not isinstance(life, (int, float)) or life <= 0:
            life = NONCE_LIFE_SECONDS
        pool = _Pool(chute_id=chute_id, instances=[],
                     until=asked + min(float(life), NONCE_LIFE_SECONDS)
                     - NONCE_MARGIN_SECONDS)
        seen = set()
        for item in listed[:MAX_INSTANCES]:
            if not isinstance(item, dict) or not is_chute_id(item.get("instance_id")):
                continue
            instance_id = item["instance_id"]
            if instance_id in seen:
                continue
            seen.add(instance_id)
            nonces = [n for n in dict.fromkeys(item.get("nonces") or [])
                      if isinstance(n, str) and _TOKEN.fullmatch(n)][:MAX_NONCES]
            pubkey = item.get("e2e_pubkey")
            try:
                public_key(pubkey)
            except SealError as e:
                pool.unusable[instance_id] = str(e)
                continue
            pool.instances.append(_Instance(instance_id, pubkey, nonces))
        if not pool.instances:
            reasons = "; ".join(sorted(set(pool.unusable.values()))) or \
                "none had a usable id"
            raise PrivacyError(f"Chutes listed no instance of {model} with a key to seal "
                               f"to ({reasons}); nothing was sent")
        return pool

    def _pool(self, model, chute_id):
        with self.state.lock:
            pool = self.state.pools.get(chute_id)
            if pool is not None and self.state.clock() < pool.until:
                return pool
        pool = self._discover(model, chute_id)
        with self.state.lock:
            self.state.pools[chute_id] = pool
        return pool

    # ---- attestation ----------------------------------------------------------------------

    def attest(self, model):
        """Discover `model`'s instances and attest any not attested in the last
        REATTEST_SECONDS (network; a worker thread): what was proved. Takes no nonce."""
        refused = mlkem_refusal()
        if refused:
            raise PrivacyError(refused)
        self._key()     # before anything is fetched: the address and the key may go there
        bare, chute_id = self._chute_for(model)
        return self._attest_pool(bare, self._pool(bare, chute_id))

    def accept(self, attestation):
        """The session has taken `attestation`, Strict and all."""
        self._accepted = attestation

    def _tee(self, model):
        return TeeClient(CHUTES_ORIGIN, "", model, session=self._checks_session,
                         intel=self._intel, nvidia=self._nvidia)

    def _attest_pool(self, model, pool):
        """Every instance of `pool` attested, or left out and said; the record."""
        state = self.state
        with state.attesting:
            needing = [i for i in pool.instances if state.fresh(i) is None]
            left_out = dict(pool.unusable)
            if needing:
                deadline = time.monotonic() + ATTEMPT_SECONDS
                tee = self._tee(model)
                with tee.attempt(deadline):
                    left_out.update(self._attest(tee, model, pool.chute_id, needing,
                                                 deadline))
            attestation = self._record(model, pool, left_out)
            with state.lock:
                state.attestations[pool.chute_id] = attestation
            return attestation

    def _attest(self, tee, model, chute_id, needing, deadline):
        """Attest `needing`; what could not even be checked this time, by id (not kept:
        tried again next time). What was checked is kept in the state."""
        sets = self._measurements()
        state = self.state
        with state.lock:
            last = state.chute_level_at.get(chute_id)
        recent = last is not None and 0 <= state.clock() - last < REATTEST_SECONDS
        whole = len(needing) > INSTANCE_LEVEL_MOST or not recent
        found, unheard = {}, {}
        if whole:
            try:
                body, nonce = self._evidence(f"/chutes/{chute_id}/evidence", model,
                                             deadline)
            except PrivacyError as e:
                if time.monotonic() >= deadline:
                    raise
                why = f"its evidence could not be had ({unsealed_words(e, 300)})"
                return {i.instance_id: why for i in needing}
            with state.lock:
                state.chute_level_at[chute_id] = state.clock()
            items = body.get("evidence") if isinstance(body.get("evidence"), list) else []
            for item in items:
                if isinstance(item, dict) and is_chute_id(item.get("instance_id")):
                    found.setdefault(item["instance_id"], (item, nonce))
            for name, why in (("failed_instance_ids", "Chutes could not fetch its evidence"),
                              ("excluded_instance_ids", "Chutes left its evidence out")):
                listed = body.get(name)
                for instance_id in listed if isinstance(listed, list) else []:
                    if isinstance(instance_id, str) and instance_id not in found:
                        unheard[instance_id] = why
        for instance in needing:
            if instance.instance_id in found or instance.instance_id in unheard:
                continue
            # First seen since the chute's last evidence: its own, with the key.
            try:
                item, nonce = self._evidence(f"/instances/{instance.instance_id}/evidence",
                                             model, deadline, keyed=True)
            except PrivacyError as e:
                if time.monotonic() >= deadline:
                    raise
                unheard[instance.instance_id] = (f"its evidence could not be had "
                                                 f"({unsealed_words(e, 300)})")
                continue
            found[instance.instance_id] = (item, nonce)
        checkable = [i for i in needing if i.instance_id in found]
        if checkable:
            with ThreadPoolExecutor(max_workers=max(1, min(8, len(checkable)))) as pool:
                results = list(pool.map(
                    lambda i: self._check(tee, i, *found[i.instance_id], sets, deadline),
                    checkable))
        else:
            results = []
        with state.lock:
            for instance, result in zip(checkable, results):
                if isinstance(result, str):
                    unheard[instance.instance_id] = result  # not checked: next time
                else:
                    state.instances[(result.instance_id, result.key_sha256)] = result
        for instance in needing:
            if instance.instance_id not in found:
                unheard.setdefault(instance.instance_id, "Chutes sent no evidence for it")
        return {i.instance_id: unheard[i.instance_id] for i in needing
                if i.instance_id in unheard}

    def _evidence(self, path, model, deadline, keyed=False):
        """Chutes' evidence for a fresh nonce of ours, and the nonce. A 429 (Chutes' 60 a
        minute for everyone) is waited out while the attempt has time; past that,
        refused in words."""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise PrivacyError(f"{model}'s attestation was not done within "
                                   f"{ATTEMPT_SECONDS / 60:g} minutes, and was given up; "
                                   f"this may pass if tried again")
            nonce = secrets.token_hex(32)
            response = self._request("GET", CHUTES_ORIGIN + path, keyed=keyed,
                                     params={"nonce": nonce},
                                     timeout=min(ATTESTATION_TIMEOUT_SECONDS, left))
            if response.status_code == 429:
                error = self._refusal("Chutes' evidence", response)
                wait = max(1.0, error.retry_after or EVIDENCE_WAIT_DEFAULT_SECONDS)
                if wait >= deadline - time.monotonic() - 1:
                    raise PrivacyError(EVIDENCE_LIMIT)
                if self._wake.wait(wait):
                    raise PrivacyError("stopped while waiting for Chutes' evidence; "
                                       "nothing was sent")
                continue
            if response.status_code != 200:
                raise self._refusal(f"Chutes did not give {model}'s evidence", response)
            data = self._json(response, f"Chutes' evidence for {model}")
            if not isinstance(data, dict):
                raise PrivacyError(f"Chutes' evidence for {model} is not an object")
            return data, nonce

    def _check(self, tee, instance, item, nonce, sets, deadline):
        """TeeClient's checks of one instance's evidence, then the key binding and the
        measurements: attested, or left out and why (kept until it is REATTEST_SECONDS
        old); or, when it could not be checked at all this time, why, as text (not
        kept). The attempt's time running out refuses the whole attestation."""
        now = self.state.clock()
        short = instance.instance_id[:8]

        def left_out(why):
            return InstanceState(instance.instance_id, instance.key_sha256, now, False, why)

        try:
            check = tee.check_instance(item, nonce)
        except PrivacyError as e:
            if time.monotonic() >= deadline:
                raise
            return left_out(unsealed_words(e, 300))
        gpus = gpus_unproved(check, short)
        if gpus is not None:
            why, for_now = gpus
            return why if for_now else left_out(why)
        facts = check.quote
        if facts is None or not key_bound(facts.report_data, nonce, instance.pubkey):
            return left_out(f"instance {short}'s quote does not bind the key Chutes lists "
                            f"for it to our nonce")
        found = measured(facts.mrtd, facts.rtmrs, sets)
        if found is None:
            return left_out(f"instance {short}'s measurements are not among the sets "
                            f"Chutes publishes")
        return InstanceState(instance.instance_id, instance.key_sha256, now, True,
                             checks=check,
                             measurement=f"{found.name} ({found.version})"
                             if found.version else found.name)

    def _record(self, model, pool, left_out):
        """What was proved of `pool`'s instances: always partial. Refuses when none was
        attested."""
        attested = []
        for instance in pool.instances:
            state = self.state.fresh(instance)
            if state is None:
                left_out.setdefault(instance.instance_id, "it could not be attested")
            elif state.passed:
                attested.append(state)
            else:
                left_out[instance.instance_id] = state.why
        if not attested:
            reasons = "; ".join(f"{i[:8]}: {why}" for i, why in left_out.items())
            raise PrivacyError(f"none of the instances Chutes listed for {model} could be "
                               f"attested ({reasons}); nothing was sent")
        total = len(pool.instances) + len(pool.unusable)
        check = _Checks(f"{len(attested)} of {total} instances Chutes listed for {model} "
                        f"attested: each one's TDX quote binds the key a request is "
                        f"sealed to, for our nonce")
        for state in attested:
            if state.checks is not None:
                check.merge(state.checks)
        check.fall_short(WORKLOAD_UNMEASURED)
        check.fall_short(MEASUREMENTS_LISTED)
        names = sorted({state.measurement for state in attested if state.measurement})
        check.note("attested: " + ", ".join(f"{state.instance_id[:8]} ({state.measurement})"
                                            for state in attested))
        for instance_id, why in left_out.items():
            check.note(f"left out, never sealed to: {instance_id[:8]} ({why})")
        return self._attestation(check, model, len(attested), names)

    @staticmethod
    def _attestation(check, model, count, names):
        attestation = check.attestation(
            model, f"{count} instance{'s' if count != 1 else ''}, each key bound to our "
                   f"nonce", instances=count, attested="instance")
        attestation.kind = E2EE
        attestation.service = SERVICE
        attestation.hardware = "Intel TDX"
        attestation.enclave = CHUTES_ORIGIN
        attestation.release = "; ".join(names)
        return attestation

    def attest_public(self, model):
        """What can be checked of `model`'s instances without the key (network; a worker
        thread): Chutes' public evidence for the whole chute, for a nonce of ours, the
        first MAX_INSTANCES of it through TeeClient's checks and the published
        measurements. The key binding is not checked: only discovery, with the key,
        names each instance's key (KEY_BINDING_UNCHECKED). For a keyless check alone,
        never for a request: nothing is sealed to what it found."""
        bare, chute_id = self._chute_for(model)
        sets = self._measurements()
        deadline = time.monotonic() + ATTEMPT_SECONDS
        tee = self._tee(bare)
        with tee.attempt(deadline):
            body, nonce = self._evidence(f"/chutes/{chute_id}/evidence", bare, deadline)
            listed = body.get("evidence") if isinstance(body.get("evidence"), list) else []
            items = [item for item in listed if isinstance(item, dict)
                     and is_chute_id(item.get("instance_id"))][:MAX_INSTANCES]

            def check(item):
                short = item["instance_id"][:8]
                try:
                    found = tee.check_instance(item, nonce)
                except PrivacyError as e:
                    if time.monotonic() >= deadline:
                        raise
                    return item["instance_id"], unsealed_words(e, 300), ""
                gpus = gpus_unproved(found, short)
                if gpus is not None:
                    return item["instance_id"], gpus[0], ""
                facts = found.quote
                known = measured(facts.mrtd, facts.rtmrs, sets) if facts is not None \
                    else None
                if known is None:
                    why = f"instance {short}'s measurements are not a set Chutes publishes"
                    return item["instance_id"], why, ""
                name = f"{known.name} ({known.version})" if known.version else known.name
                return item["instance_id"], found, name

            if items:
                with ThreadPoolExecutor(max_workers=max(1, min(8, len(items)))) as pool:
                    results = list(pool.map(check, items))
            else:
                results = []
        attested = [(i, c, n) for i, c, n in results if isinstance(c, _Checks)]
        left_out = {i: c for i, c, _n in results if isinstance(c, str)}
        if not attested:
            reasons = "; ".join(f"{i[:8]}: {why}" for i, why in left_out.items()) or \
                "Chutes' evidence listed none"
            raise PrivacyError(f"none of {bare}'s instances in Chutes' public evidence "
                               f"hold up ({reasons})")
        record = _Checks(f"{len(attested)} of the {len(results)} instances of {bare} "
                         f"checked from Chutes' public evidence, for our nonce, hold up: "
                         f"Intel's chain, NVIDIA's verdict and a published image")
        for _instance, found, _name in attested:
            record.merge(found)
        record.fall_short(WORKLOAD_UNMEASURED)
        record.fall_short(MEASUREMENTS_LISTED)
        record.fall_short(KEY_BINDING_UNCHECKED)
        record.note("attested: " + ", ".join(f"{i[:8]} ({name})"
                                             for i, _c, name in attested))
        for instance_id, why in left_out.items():
            record.note(f"left out: {instance_id[:8]} ({why})")
        return self._attestation(record, bare, len(attested),
                                 sorted({name for _i, _c, name in attested}))

    def _instance_and_nonce(self, model, chute_id):
        """An attested instance and one of its nonces, taken: never used again, whatever
        becomes of the request. Discovered again when the nonces have run out or aged
        past their use."""
        for _round in range(2):
            pool = self._pool(model, chute_id)
            attestation = self._attest_pool(model, pool)
            if attestation is not self._accepted and self.reattested is not None:
                # Not the attestation the session accepted: held to it as any other is,
                # before anything is sealed (PrivacySession._reattested).
                self.reattested(model, attestation)
                self._accepted = attestation
            with self.state.lock:
                if self.state.clock() < pool.until:
                    ready = [i for i in pool.instances
                             if i.nonces and (s := self.state.fresh(i)) is not None
                             and s.passed]
                    if ready:
                        instance = random.choice(ready)
                        return instance, instance.nonces.pop(0)
                if self.state.pools.get(chute_id) is pool:
                    del self.state.pools[chute_id]
        raise PrivacyError(f"Chutes' nonces for {model} ran out or expired before a "
                           f"request could be sealed; nothing was sent")

    # ---- one request ----------------------------------------------------------------------

    @staticmethod
    def body_for(model, payload):
        """The body sealed to the instance (less our response key): the app's request
        streamed with the model server's usage at its end, only the fields vLLM's route
        takes. The app's lowest reasoning (none, minimal or low) switches thinking off
        (THINKING_OFF); a higher level leaves the model's own."""
        body = dict(payload)
        body["model"] = services.bare_model(model)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        reasoning = body.pop("reasoning", None)
        effort = str(reasoning.get("effort") or "").lower() \
            if isinstance(reasoning, dict) else ""
        if effort in THINKING_OFF_EFFORTS:
            own = body.get("chat_template_kwargs")
            body["chat_template_kwargs"] = {**THINKING_OFF,
                                            **(own if isinstance(own, dict) else {})}
        return {key: value for key, value in body.items() if key in ALLOWED_FIELDS}

    @contextmanager
    def request(self, model, payload):
        """Send one sealed, streamed request; yields a ChutesStream, whose `lines` are
        the model server's own chunks once opened. Attested first; nothing falls back."""
        refused = mlkem_refusal()
        if refused:
            raise PrivacyError(refused)
        key = self._key()
        bare, chute_id = self._chute_for(model)
        body = self.body_for(bare, payload)
        instance, nonce = self._instance_and_nonce(bare, chute_id)
        sealed = seal_request(instance.pubkey, body)
        opener = StreamOpener(sealed.response_key)
        self.last_instance = instance.instance_id
        response = self._request(
            "POST", INVOKE_URL, data=sealed.blob, stream=True,
            timeout=(10, self.request_timeout),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/octet-stream",
                     "Accept": "text/event-stream",
                     "X-Chute-Id": chute_id, "X-Instance-Id": instance.instance_id,
                     "X-E2E-Nonce": nonce, "X-E2E-Stream": "true",
                     "X-E2E-Path": E2E_PATH})
        try:
            if response.status_code != 200:
                if response.status_code == 403:
                    # "Invalid, expired, or already-used nonce": the rest of this
                    # discovery's nonces are no better; the next request discovers again.
                    with self.state.lock:
                        self.state.pools.pop(chute_id, None)
                raise self._refusal("Chutes refused the sealed request", response)
            yield ChutesStream(response, opener, instance.instance_id)
        finally:
            response.close()

    def lines(self, stream):
        """The model server's own chunks, opened in their counted order, as the lines of
        an OpenAI stream (bytes), its `[DONE]` only once the sealed end has been read and
        held (StreamOpener.finish). A piece that does not open, or is out of its place,
        refuses (PrivacyError) and doubts every instance's attestation; nothing of it is
        read as the model's."""
        try:
            for line in read_lines(stream.response.iter_content(chunk_size=512)):
                for chunk in self._opened(stream.opener, line):
                    yield b"data: " + json.dumps(chunk).encode()
        except netguard.RequestError as e:
            raise PrivacyError(f"the sealed reply was cut off: {e}") from e
        except LineTooLong as e:
            raise PrivacyError(f"the sealed reply was not read on: {e}") from e
        try:
            stream.opener.finish()
        except SealError as e:
            raise PrivacyError(f"the encrypted reply could not be read ({e}); "
                               f"refused") from e
        yield b"data: [DONE]"

    def _opened(self, opener, line):
        try:
            return opener.feed(line)
        except SealBroken as e:
            self.state.doubt()
            raise PrivacyError(f"the encrypted reply could not be opened ({e}); refused, "
                               f"and Chutes' instances are attested again before "
                               f"anything more is sent") from e
        except SealError as e:
            raise PrivacyError(f"the encrypted reply could not be read ({e}); "
                               f"refused") from e

    @staticmethod
    def finish(finish_reason, saw_done):
        if not (finish_reason and saw_done):
            raise PrivacyError("the encrypted reply ended before it was complete")
