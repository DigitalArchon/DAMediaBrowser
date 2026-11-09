# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Privacy Mode: the AI asked only of models that run in an attested enclave.

nano-gpt offers two kinds, and this module is the one place that tells them
apart:

  TEE/*      Run inside a trusted execution environment. Before anything is
             sent, the enclave is attested (below), and every reply is
             checked to be signed by the key that attestation vouched for.
             The text still passes nano-gpt's gateway in the clear:
             attestation proves where the model runs, not that nano-gpt
             cannot read it. And most of them (all, in Nov 2026) are
             attested as RedPill's gateway, which passes requests on to the
             model's host: that attestation is partial, since it does not
             show the model's own enclave (TeeClient._attest_signing_key).
  private/*  nano-gpt's Private Mode: end-to-end encrypted. The body - the
             frames, the file and folder names, the tracklist - is sealed on
             this machine to a key only the attested enclave holds, and the
             reply comes back sealed the same way; nano-gpt relays
             ciphertext. See sealed.py.

Ported from DA Assistant's privacy.py (Nov 2026), itself from StoryLoom:
the attestation (TeeClient, and dcap.py, nras.py, ethsig.py under it) is
theirs line for line, with netguard's Session for requests; the session is
this app's, which asks one model per operation rather than holding a chat.
What matters here:

- `GET {base}/tee/attestation?model=&nonce=` returns the enclave's signing
  address, an Intel TDX quote whose report data is that address, zero
  padding, then our nonce - so the hardware quote itself binds the key that
  signs replies to *this* request - and NVIDIA GPU evidence, made with the
  same nonce. The quote is verified up to Intel's root, for revocation and
  for its TCB status (dcap.py); the GPU evidence goes to NVIDIA's
  attestation service, whose signed verdict is checked in turn (nras.py).
- `GET {base}/tee/signature/{completion id}` returns a text and an EIP-191
  signature; the signer recovered from them (ethsig.py) must be the
  attested address. That proves the attested key signed a record for this
  reply's id, not that the record matches the reply's content: a reply is
  "signed", never "verified".

An operation's privacy is decided once, when it starts (ai.settings_from
makes its PrivacySession), and never relaxed: its model is copied then, so
switching Privacy Mode off, or choosing another model, leaves a run already
going exactly as it was. So is whether its attestation is strict: an
attestation that proved less than it could is then refused rather than
used, and a TEE reply whose signature does not hold has its model attested
again before anything more is sent to it.
"""

import base64
import binascii
import json
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

from . import dcap, netguard, nras
from .ethsig import recover_personal

NRAS_URL = "https://nras.attestation.nvidia.com/v3/attest/gpu"
TIMEOUT_SECONDS = 30
PRIVATE_MODE_HOSTS = frozenset({"nano-gpt.com"})
# How old a TEE/ model's attestation may grow, on the boot clock, before the next send
# to it attests it again: an enclave restarted, moved or replaced since is found within
# this, as the end-to-end encrypted one is within sealed.MAX_AGE_SECONDS.
TEE_MAX_AGE_SECONDS = 15 * 60

TEE = "tee"
E2EE = "e2ee"

# What an attestation of RedPill's gateway lacks (TeeClient._attest_signing_key): it
# is shown in amber, and Strict refuses it.
GATEWAY_SHORTFALL = "RedPill's gateway, not the model's host"

# What one reply's check found. See Receipt.
SIGNED = "signed"            # TEE: the attested key signed this reply's record
MISMATCH = "mismatch"        # TEE: a different key did
UNSIGNED = "unsigned"        # TEE: no signature was kept, or it could not be fetched
SEALED = "sealed"            # E2EE: it came back sealed by the attested enclave

TEE_NOTE = ("TEE: the model runs in an attested enclave and its replies are signed, but "
            "the text passes nano-gpt's gateway in the clear.")
E2EE_NOTE = ("End-to-end encrypted: every message is sealed on this machine to a key only "
             "the model's attested enclave holds. nano-gpt relays ciphertext; it sees your "
             "account, the model, timing, sizes and cost, never the text.")


class PrivacyError(Exception):
    """A private request was refused before anything was sent."""


def unsealed_words(text, limit=200):
    """What nano-gpt wrote - its gateway, or Private Mode's relay, which carries what the
    enclave sealed and seals nothing of its own - as an error may quote it: on one line,
    short, and without what a reader cannot see. Such an error is shown in the window
    and the log of what was sent, beside what the model said, and nothing vouches
    for it: a line break, a bidi override, a zero-width or tag character let it pass for
    something else, or say more than it shows. Controls and line separators become
    spaces, format characters (Unicode's Cf) go. Cut before it is cleaned: an error
    body can be large. From DA Toolkit's relay_said (Nov 2026)."""
    kept = []
    for ch in str(text or "")[:4096]:
        category = unicodedata.category(ch)
        if category != "Cf":
            kept.append(" " if category in ("Cc", "Zl", "Zp") else ch)
    return " ".join("".join(kept).split())[:limit]


class ModelRetired(PrivacyError):
    """nano-gpt no longer offers this Private Mode model. Attesting says nothing about
    it - the enclave is Tinfoil's router whatever the model - so without this a retired
    model attested fine and its first message came back as a bare preflight refusal.
    `offered`: the private/ models the relay's list does have, to choose from instead."""

    def __init__(self, model, offered=()):
        super().__init__(f"{model} is no longer offered by nano-gpt's Private Mode, so "
                         f"nothing was sent to it; choose another private model")
        self.model = model
        self.offered = [m for m in offered if m != model]


# ---- which models qualify -------------------------------------------------------------

def is_tee(model):
    return bool(model) and model.startswith("TEE/")


def is_private_mode(model):
    return bool(model) and model.startswith("private/")


def sealed_only(model):
    """Whether `model` may only ever be sent sealed: a private/ model however it is
    written ("Private/...", a space before it), so that the plain path refuses it
    (ai.chat). When in doubt, sealed. From DA Vibe Manager (Nov 2026)."""
    return str(model or "").strip().lower().startswith("private/")


def searches(model):
    """Whether `model` carries nano-gpt's ":online" suffix, which has it run a web
    search with what is sent - however it is written."""
    return ":online" in str(model or "").lower()


def kind_of(model):
    """TEE, E2EE, or None for a model that runs nowhere in particular."""
    if is_private_mode(model):
        return E2EE
    if is_tee(model):
        return TEE
    return None


def offers_private_mode(api_url):
    try:
        host = urlparse(api_url or "").hostname
    except ValueError:      # "http://[fd00::5", half typed: netguard.problem says why
        return False
    return (host or "").lower() in PRIVATE_MODE_HOSTS


def describe(model):
    """(short label, explanation) for a model's privacy, or ("", "")."""
    kind = kind_of(model)
    if kind == E2EE:
        return "end-to-end encrypted", E2EE_NOTE
    if kind == TEE:
        return "TEE", TEE_NOTE
    return "", ""


_cryptography = None    # cryptography_available()'s answer, once it has been worked out


def cryptography_available():
    """Whether Python's cryptography package can be loaded. Every check of a TEE/
    model's attestation is a signature - Intel's, NVIDIA's, an instance's - so without
    it none of them is attested, and nothing is sent to one. Worked out once: it takes
    a few hundredths of a second the first time, and cannot change without a restart."""
    global _cryptography
    if _cryptography is None:
        try:
            from cryptography import x509  # noqa: F401  - and its compiled bindings
        except Exception:  # an ImportError, or a build for another Python
            _cryptography = False
        else:
            _cryptography = True
    return _cryptography



def problems(api_url, model, wait=True):
    """Why Privacy Mode cannot run with this endpoint and private model, as a
    list of sentences; [] when it can.

    Whether Tinfoil's client can be used takes most of a second to find out
    the first time (sealed.available). wait=False is for the GUI thread: while
    that is not known its sentence is left out."""
    found = []
    insecure = netguard.problem(api_url)
    if insecure:
        found.append(insecure)
    model = (model or "").strip()
    if not model:
        found.append("No private model is set (Settings → AI).")
    elif searches(model):
        found.append(f"{model} asks nano-gpt to search the web for it, which it can only "
                     f"do by reading the request; Privacy Mode never searches. Leave "
                     f"out the \":online\".")
    elif kind_of(model) is None:
        found.append(f"The private model, {model}, runs in no attested enclave: "
                     f"choose a private/ or TEE/ model in Settings → AI.")
    elif is_private_mode(model) and not offers_private_mode(api_url):
        found.append(f"{model} is end-to-end encrypted through nano-gpt, and the "
                     f"endpoint is not nano-gpt's.")
    elif is_private_mode(model):
        from . import sealed
        answer = sealed.available() if wait else sealed.known()
        if answer is not None and not answer[0]:
            found.append(answer[1])
    elif not cryptography_available():
        found.append(f"The cryptography package (python3-cryptography) is not "
                     f"installed, and without it {model} cannot be attested: what a "
                     f"TEE model proves is Intel's and NVIDIA's signatures.")
    return found


# ---- attestation ----------------------------------------------------------------------

@dataclass
class Attestation:
    """What an enclave proved before anything was sent to it."""

    model: str
    kind: str
    # full: everything this app can check was checked and held.
    # partial: nothing failed, but something could not be checked or is not current;
    # `shortfalls` names each in a few words, for the headline.
    level: str = "full"
    detail: str = ""
    summary: str = ""               # one line of `detail`, for the window
    checked_at: float = field(default_factory=time.time)
    shortfalls: list = field(default_factory=list)
    # TEE
    signing_address: str = ""
    gpu_ok: object = None           # True once NVIDIA's signed verdict held, else None
    gpus: int = 0
    tcb_status: str = ""            # Intel's, when its collateral could be checked
    # E2EE
    enclave: str = ""
    hardware: str = ""
    release: str = ""
    latest_release: str = ""        # set when the enclave runs an older release
    release_checked: bool = True    # False when the latest release could not be read
    # What the sealed client attested (sealed.Verified). Nothing is sealed to it until
    # the session has accepted this attestation and handed it back (_accept).
    proof: object = field(default=None, repr=False, compare=False)

    @property
    def headline(self):
        if self.kind == E2EE:
            return "End-to-end encrypted"
        if not self.shortfalls:
            return "TEE attested"
        return f"TEE attested ({', '.join(self.shortfalls)})"

    @property
    def fell_short(self):
        """What the enclave proved less of than it could, in a few words each - all
        that is shown in amber - or [] when it proved everything
        this app can check."""
        found = list(self.shortfalls)
        if self.latest_release:
            found.append(f"an older release than {self.latest_release}")
        elif not self.release_checked:
            # As Intel's or NVIDIA's services out of reach are for a TEE/ model: an
            # enclave on a release long superseded would have passed Strict, green.
            found.append("its release not checked against the latest")
        if self.level != "full" and not found:
            found.append("not everything could be checked")
        return found


@dataclass
class Receipt:
    """What was checked about one reply."""

    model: str
    kind: str
    state: str
    detail: str = ""


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _decodes_to(value, raw):
    """Whether `value` is base64 for exactly the bytes `raw`."""
    try:
        return base64.b64decode(value) == raw
    except (TypeError, ValueError, binascii.Error):
        return False


def _jwt_claims(value):
    """Every JWT's payload found in a JSON value, in order."""
    found = []
    if isinstance(value, str) and value.count(".") == 2:
        part = value.split(".")[1]
        try:
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        except (ValueError, binascii.Error):
            return found
        if isinstance(claims, dict):
            found.append(claims)
    elif isinstance(value, list):
        for item in value:
            found.extend(_jwt_claims(item))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_jwt_claims(item))
    return found


def api_base(api_url):
    """The API root the chat endpoint hangs from: .../api/v1 for .../api/v1/chat/completions."""
    parsed = urlparse(api_url)
    path = parsed.path
    for suffix in ("/chat/completions", "/completions", "/responses"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return f"{parsed.scheme}://{parsed.netloc}{path.rstrip('/')}"


def _bases(api_url):
    """Where the TEE endpoints may live: nano-gpt's docs give the path three ways."""
    parsed = urlparse(api_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    return list(dict.fromkeys([api_base(api_url), f"{root}/api/v1", f"{root}/v1"]))


def _error_text(response):
    """The provider's message from an error response, or "" (see unsealed_words)."""
    try:
        body = response.json()
    except ValueError:
        return unsealed_words(getattr(response, "text", ""))
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        return unsealed_words(error or body.get("message") or "")
    return ""


def _unchecked_without_cryptography(model):
    return (f"{model}'s attestation cannot be checked without Python's cryptography "
            f"package (python3-cryptography), which is not installed: what it proves is "
            f"Intel's and NVIDIA's signatures")


def _instance_verifier():
    """verify(certificate DER, body, signature) -> sha256 of the certificate's public
    key when its signature over body holds, else None. Raises ImportError without the
    cryptography package, before any evidence is looked at."""
    import hashlib

    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    def verify(certificate, body, signature):
        try:
            key = x509.load_der_x509_certificate(certificate).public_key()
            if isinstance(key, rsa.RSAPublicKey):
                key.verify(signature, body, padding.PKCS1v15(), hashes.SHA256())
            elif isinstance(key, ec.EllipticCurvePublicKey):
                key.verify(signature, body, ec.ECDSA(hashes.SHA256()))
            else:
                return None
        except (InvalidSignature, ValueError):
            return None
        return hashlib.sha256(key.public_bytes(
            Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).digest()

    return verify


class _Checks:
    """What one attestation's checks found, gathered across quotes and instances."""

    def __init__(self, *notes):
        self.notes = list(notes)
        self.shortfalls = []
        self.status = ""
        self.quotes = 0              # quotes Intel's checks were run on
        self.gpus = 0
        self.summary_gpu = set()

    def note(self, *notes):
        for note in notes:
            if note not in self.notes:
                self.notes.append(note)

    def fall_short(self, what):
        if what not in self.shortfalls:
            self.shortfalls.append(what)

    def tcb(self, status):
        """Keep the worst Intel TCB status seen."""
        def rank(value):
            return dcap.STATUSES.index(value) if value in dcap.STATUSES else len(dcap.STATUSES)
        if status and (not self.status or rank(status) > rank(self.status)):
            self.status = status

    def merge(self, other):
        self.note(*other.notes)
        for what in other.shortfalls:
            self.fall_short(what)
        self.tcb(other.status)
        self.quotes += other.quotes
        self.gpus += other.gpus
        self.summary_gpu |= other.summary_gpu

    def attestation(self, model, bound, signing_address=""):
        if self.quotes:
            summary = [f"Intel quote{'s' if self.quotes != 1 else ''} verified, "
                       f"TCB {self.status or 'unchecked'}"]
        else:
            summary = ["Intel quote unchecked"]
        if self.gpus:
            summary.append(f"NVIDIA verified {self.gpus} GPU{'s' if self.gpus != 1 else ''}")
        else:
            summary += sorted(self.summary_gpu)[:1]
        return Attestation(model=model, kind=TEE,
                           level="partial" if self.shortfalls else "full",
                           detail="; ".join(self.notes), shortfalls=self.shortfalls,
                           summary="; ".join(summary + [bound]),
                           signing_address=signing_address,
                           gpu_ok=True if self.gpus else None, gpus=self.gpus,
                           tcb_status=self.status)


class TeeClient:
    """Attestation and reply signatures for one TEE/ model."""

    def __init__(self, api_url, api_key, model, session=None, intel=None, nvidia=None):
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        # netguard's: a redirect is followed only where the request could have been
        # sent in the first place, and never from https to plain http.
        self.session = session or netguard.Session()
        self.signing_address = None
        self._base = None
        # The two verifiers, replaceable so tests can stand in for Intel and NVIDIA.
        self._intel = intel or dcap.verify
        self._nvidia = nvidia or nras.verify

    def _get(self, path, params):
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        last = ""
        for base in [self._base] if self._base else _bases(self.api_url):
            try:
                response = self.session.get(f"{base}{path}", params=params,
                                            headers=headers, timeout=TIMEOUT_SECONDS)
            except netguard.RequestError as e:
                last = str(e)
                continue
            if response.status_code == 404:
                last = f"{base}{path}: 404"
                continue
            if response.status_code >= 400:
                # nano-gpt's own words, when it gives any: "Unable to reach RedPill
                # attestation service" says far more than a status code.
                reason = _error_text(response)
                error = PrivacyError(f"nano-gpt answered HTTP {response.status_code}"
                                     + (f": {reason}" if reason else ""))
                error.status_code = response.status_code
                error.reason = reason
                raise error
            try:
                body = response.json()
            except ValueError:
                raise PrivacyError(f"{base}{path} returned a non-JSON body")
            if not isinstance(body, dict):
                raise PrivacyError(f"{base}{path} returned {type(body).__name__}")
            self._base = base
            return body
        raise PrivacyError(f"no TEE endpoint answered ({last})")

    def attest(self):
        """Check the enclave now, with a fresh nonce.

        nano-gpt's TEE/ models come from more than one provider, and they attest in
        two shapes (both measured live, Sept 2026):

          aci/1          one enclave, whose TDX quote binds its reply-signing key and
                         our nonce, with GPU evidence made with our nonce;
          per instance   a list of instances (Chutes: TEE/kimi-k3, TEE/qwen3.5-27b,
                         ...), each quote binding that instance's own key, which signs
                         a body carrying our nonce, the quote and the GPU evidence.

        Refuses - PrivacyError, and nothing is sent - when an enclave cannot be
        trusted: no attestation, a quote that does not bind our nonce (directly, or by
        a key the quote binds), a quote Intel's chain does not vouch for (forged,
        revoked, a debug TD, a TCB Intel has revoked or never listed), GPU evidence
        whose NVIDIA verdict is unsigned, stale, for another nonce or negative, or - in
        the per-instance shape - any instance that failed to attest, since a request
        could be routed to it. And without the `cryptography` package, always: every one
        of those checks is a signature, and a quote whose signatures go unchecked is
        bytes anyone could have written.

        What merely could not be checked - Intel's collateral or NVIDIA's service out of
        reach, no GPU evidence offered - or is not current (any Intel TCB status but
        UpToDate) makes the attestation partial, and the headline names it.

        Stricter than StoryLoom, which reads these verdicts without checking their
        signatures: a nonce or a verdict that merely comes back in JSON proves nothing,
        since anything can echo a string.
        """
        nonce = secrets.token_hex(32)
        try:
            body = self._get("/tee/attestation", {"model": self.model, "nonce": nonce})
        except PrivacyError as e:
            status = getattr(e, "status_code", None)
            if status == 400:
                # Live (Sept 2026): the Tinfoil-hosted TEE/ models offer no attestation
                # here - nano-gpt says fresh attestation for them only comes through
                # Tinfoil's own flow, which is the one their private/ twins use.
                reason = getattr(e, "reason", "")
                hint = ("; its end-to-end encrypted private/ version, where nano-gpt "
                        "offers one, is attested that way" if "tinfoil" in reason.lower()
                        else "")
                raise PrivacyError(f"{self.model} offers no attestation"
                                   + (f" (nano-gpt: {reason})" if reason else "") + hint)
            # 429 too: live, nano-gpt answers "Unable to reach Chutes TEE evidence
            # service" with it, and the same request passed a minute later.
            if status is not None and (status >= 500 or status == 429):
                raise PrivacyError(f"nano-gpt could not fetch {self.model}'s attestation "
                                   f"just now ({e}); this is on the provider's side, and "
                                   f"may pass if tried again")
            raise
        if isinstance(body.get("evidence"), list):
            return self._attest_instances(body, nonce)
        if isinstance(body.get("all_attestations"), list) and "signing_address" not in body:
            # RedPill relaying Chutes (live, Sept 2026: TEE/glm-5.1, TEE/qwen3.5-27b,
            # TEE/qwen3.5-397b-a17b). The quotes are made for a nonce the relay chose -
            # not ours, and not derived from it - and nothing signed ties them to ours,
            # so they would read the same if replayed from any earlier day.
            relayed = body.get("nonce")
            why = ("made for a nonce the relay chose, not ours" if relayed != nonce
                   else "carries no binding of our nonce this app can check")
            raise PrivacyError(f"{self.model}'s attestation, relayed by its provider "
                               f"({unsealed_words(body.get('attestation_type'), 40) or 'unnamed'}"
                               f"), was {why}, "
                               f"so it cannot show the enclave is live for this request")
        if body.get("signing_address") or body.get("intel_quote"):
            return self._attest_signing_key(body, nonce)
        if body.get("error"):
            raise PrivacyError(f"nano-gpt could not attest {self.model}: "
                               f"{unsealed_words(body['error'])}")
        raise PrivacyError(f"{self.model}'s attestation is in a shape this app does not "
                           f"recognise (it holds "
                           f"{unsealed_words(', '.join(sorted(body))) or 'nothing'})")

    def _attest_signing_key(self, body, nonce):
        """One enclave, its reply-signing key and our nonce in its quote.

        What nano-gpt relays for its TEE/ models in this shape is RedPill's gateway
        (aci/1, "serving": "aggregator"; live, Nov 2026, every one of them, one signing
        key for all), which passes each request on to the model's host. Its quote is the
        gateway's; the GPU evidence is the upstream node's, made with our nonce but not
        bound to that quote; that the model itself runs in an enclave is not in it -
        RedPill says that per reply, in a signed receipt nano-gpt does not pass on. So
        such an attestation is partial (GATEWAY_SHORTFALL), and Strict refuses it: a
        private/ model is sealed end to end, to an enclave that is attested itself.
        Keys RedPill attests that have expired are refused. From DA Vibe Manager
        (Nov 2026).
        """
        address = body.get("signing_address")
        address = address if isinstance(address, str) and address.startswith("0x") else None
        if address is None:
            raise PrivacyError(f"{self.model}'s attestation named no signing key")
        try:
            raw = bytes.fromhex(str(body.get("intel_quote") or ""))
            quote = dcap.parse_quote(raw)
        except (ValueError, dcap.DcapError) as e:
            raise PrivacyError(f"{self.model}'s Intel TDX quote could not be read ({e})")
        # Read from the quote's own bytes - the part Intel's chain signs - rather than
        # from nano-gpt's JSON copy of it: the signing address, zero padding, our nonce.
        report_data = quote.report_data
        if report_data[:20] != bytes.fromhex(address[2:]) or any(report_data[20:32]) \
                or report_data[32:] != bytes.fromhex(nonce):
            raise PrivacyError(f"{self.model}'s attestation did not bind its signing key "
                               f"to our nonce")

        check = _Checks("the Intel TDX quote binds the signing key to our nonce")
        attested = body.get("attestation")
        keyset = attested.get("workload_keyset") if isinstance(attested, dict) else None
        expires = keyset.get("not_after") if isinstance(keyset, dict) else None
        if isinstance(expires, (int, float)) and not isinstance(expires, bool) \
                and expires < time.time():
            raise PrivacyError(f"{self.model}'s attested keys expired on "
                               f"{time.strftime('%Y-%m-%d', time.gmtime(expires))}")
        serving = body.get("service_capabilities")
        if body.get("api_version") == "aci/1" or (
                isinstance(serving, dict) and serving.get("serving") == "aggregator"):
            check.fall_short(GATEWAY_SHORTFALL)
            check.note("this is RedPill's gateway, which passes each request on to the "
                       "model's host: the gateway's quote and the GPUs' evidence are "
                       "checked, not that the model itself runs in an enclave")
        self._check_quote(raw, check)
        payload = _json(body.get("nvidia_payload"))
        if isinstance(payload, dict):
            # Sent to NVIDIA with our nonce, whatever nano-gpt's copy names, so that its
            # service holds the evidence to that nonce itself: evidence from an earlier
            # request, passed on with the nonce it was made for, would otherwise come
            # back vouched for.
            payload = dict(payload, nonce=nonce)
        else:
            payload = None
        self._check_gpus(payload, nonce, check, "this request's nonce")
        self.signing_address = address
        return check.attestation(self.model, "signing key bound to our nonce",
                                 signing_address=address)

    def _attest_instances(self, body, nonce):
        """Per instance: each quote binds an instance key, which signed our nonce.

        For each instance, all of these before it counts:
          - its certificate's public key hashes to the second half of its quote's
            report data, so the key belongs to that TD (the certificate's own issuer,
            the provider's private CA, is not a trust anchor and is not relied on);
          - that key's signature over `attested_body` verifies, and the body carries
            our nonce and exactly this quote - so an instance whose key the hardware
            vouches for answered this request;
          - the quote passes Intel's checks, like any other;
          - the GPU evidence - the signed copy - passes NVIDIA's checks. It is made with
            the first half of the report data as its nonce (measured live: NVIDIA's
            verdict matches that and nothing else), so it is bound to the quote, and
            through the signature to this request.
        The instances are checked in parallel; the attestation takes the worst of them.
        """
        failed = [str(i) for i in body.get("failed_instance_ids") or []]
        instances = body["evidence"]
        if failed:
            raise PrivacyError(f"{len(failed)} of {len(failed) + len(instances)} of "
                               f"{self.model}'s instances could not be attested, and a "
                               f"request could be routed to one of them")
        if not instances:
            raise PrivacyError(f"{self.model}'s attestation listed no instances")
        try:
            _instance_key = _instance_verifier()
        except ImportError:
            raise PrivacyError(f"{self.model}'s instances can only be tied to our nonce by "
                               f"checking their signatures, which needs the cryptography "
                               f"package (python3-cryptography)")

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(instances))) as pool:
            results = list(pool.map(
                lambda item: self._check_instance(item, nonce, _instance_key), instances))
        check = _Checks(f"each of {len(instances)} instances' keys, bound into its TDX "
                        f"quote, signed our nonce")
        for result in results:
            check.merge(result)
        summary = (f"{len(instances)} instance{'s' if len(instances) != 1 else ''}, each "
                   f"key bound to our nonce")
        attestation = check.attestation(self.model, summary)
        attestation.detail += ("; this provider signs no replies, so answers show as "
                               "unsigned")
        return attestation

    def _check_instance(self, item, nonce, verify_signature):
        name = str(item.get("instance_id") or "an instance")[:8]
        try:
            raw = base64.b64decode(item["quote"])
            quote = dcap.parse_quote(raw)
            signed = base64.b64decode(item["attested_body"])
            certificate = base64.b64decode(item["certificate"])
            signature = base64.b64decode(item["signature"])
        except (KeyError, TypeError, ValueError, binascii.Error, dcap.DcapError) as e:
            raise PrivacyError(f"{self.model}'s instance {name} sent unreadable evidence "
                               f"({e})")
        key_hash = verify_signature(certificate, signed, signature)
        if key_hash is None:
            raise PrivacyError(f"{self.model}'s instance {name} did not sign its evidence "
                               f"with the key it presented")
        if quote.report_data[32:] != key_hash:
            raise PrivacyError(f"{self.model}'s instance {name} signed with a key its TDX "
                               f"quote does not bind")
        try:
            attested = json.loads(signed)
            evidence = attested["evidence"]
            if not isinstance(evidence, dict):
                raise TypeError(type(evidence).__name__)
        except (ValueError, KeyError, TypeError):
            raise PrivacyError(f"{self.model}'s instance {name} signed unreadable evidence")
        if attested.get("nonce") != nonce:
            raise PrivacyError(f"{self.model}'s instance {name} did not sign our nonce")
        # The quote as the instance signed it: "tdx_quote" until Nov 2026, "quote" since
        # (live). Every copy it signed must be this quote, and there must be one.
        signed_quotes = [evidence[field] for field in ("quote", "tdx_quote")
                         if evidence.get(field)]
        if not signed_quotes or not all(_decodes_to(copy, raw) for copy in signed_quotes):
            raise PrivacyError(f"{self.model}'s instance {name} signed a different quote")

        check = _Checks()
        self._check_quote(raw, check)
        gpu = _json(evidence.get("nvtrust_evidence"))
        payload = None
        if isinstance(gpu, list) and gpu:
            payload = {"nonce": quote.report_data[:32].hex(),
                       "arch": gpu[0].get("arch", "HOPPER"), "evidence_list": gpu}
        self._check_gpus(payload, quote.report_data[:32].hex(), check,
                         "the nonce its quote binds")
        return check

    def _check_quote(self, raw, check):
        """Intel's verification of one quote, into `check`. Refuses a bad quote, and
        one that cannot be checked at all."""
        try:
            intel = self._intel(raw, self.session)
        except ImportError:
            # Without its signatures checked, a quote is bytes anyone could have
            # written, and a reply "signed by the attested enclave" would be signed by
            # whatever key they named.
            raise PrivacyError(_unchecked_without_cryptography(self.model))
        except dcap.DcapError as e:
            raise PrivacyError(f"{self.model}'s Intel TDX quote failed verification: {e}")
        else:
            check.note(*intel.notes)
            check.tcb(intel.status)
            check.quotes += 1
            if not intel.collateral:
                check.fall_short("Intel TCB unchecked")
            elif intel.status != "UpToDate":
                check.fall_short(f"Intel TCB {intel.status}")

    def _check_gpus(self, payload, nonce, check, bound_to):
        """NVIDIA's verdict on one set of GPU evidence, into `check`."""
        if not (isinstance(payload, dict) and payload.get("evidence_list")):
            # Live, several TEE/ models send an empty evidence list: not a rejection,
            # but nothing for NVIDIA to vouch for either.
            check.fall_short("no GPU evidence")
            check.note("no GPU evidence was offered")
            check.summary_gpu.add("no GPU evidence")
            return
        answer = self._nras_answer(payload)
        if answer is None:
            check.fall_short("GPUs unverified")
            check.note("NVIDIA's attestation service could not be reached")
            check.summary_gpu.add("GPUs unverified")
            return
        try:
            gpus = self._nvidia(answer, nonce, self.session)
        except ImportError:
            raise PrivacyError(_unchecked_without_cryptography(self.model))
        except nras.KeysUnavailable as e:
            # NVIDIA's signatures go unchecked, but its answer came over TLS from its
            # own service, so what the answer says of itself is still held to: a
            # verdict that says no is believed, and one made for another nonce or
            # outside its time - evidence replayed from another request - is refused.
            claims = _jwt_claims(answer)
            if not any(c.get("x-nvidia-overall-att-result") is True for c in claims):
                raise PrivacyError(f"NVIDIA rejected {self.model}'s GPU evidence")
            try:
                for token in claims:
                    nras.check_claims(token, nonce, time.time())
            except nras.NrasError as refused:
                raise PrivacyError(f"{self.model}'s GPU attestation failed: {refused}")
            check.summary_gpu.add("NVIDIA's verdict unchecked")
            check.fall_short("NVIDIA signature unchecked")
            check.note(f"{e}, so NVIDIA's verdict is unchecked")
        except nras.NrasError as e:
            raise PrivacyError(f"{self.model}'s GPU attestation failed: {e}")
        else:
            check.gpus += gpus
            check.note(f"NVIDIA's signed verdict vouches for {gpus} GPU"
                       f"{'s' if gpus != 1 else ''}, for {bound_to}")

    def _nras_answer(self, payload):
        """NRAS's answer to the GPU evidence, or None if the service could not be
        reached. An answer of HTTP 4xx is NVIDIA refusing the evidence."""
        try:
            response = self.session.post(NRAS_URL, json=payload, timeout=TIMEOUT_SECONDS)
        except netguard.RequestError:
            return None
        if response.status_code >= 500:
            return None
        if response.status_code >= 400:
            raise PrivacyError(f"NVIDIA rejected {self.model}'s GPU evidence "
                               f"(HTTP {response.status_code})")
        try:
            return response.json()
        except ValueError:
            return None

    def check_reply(self, response_id):
        """SIGNED, MISMATCH or UNSIGNED, with a sentence saying why."""
        if not response_id:
            return UNSIGNED, "the reply carried no id to check"
        if self.signing_address is None:
            # The per-instance providers (Chutes) sign no replies: nano-gpt answers
            # "TEE signature is not available for this model". Their instances were each
            # attested instead.
            return UNSIGNED, ("this model's provider signs no replies; every instance "
                              "that could answer was attested before sending")
        try:
            body = self._get(f"/tee/signature/{quote(response_id, safe='')}",
                             {"model": self.model, "signing_algo": "ecdsa"})
        except PrivacyError:
            # Live: some TEE/ models' providers keep no signature for a reply.
            return UNSIGNED, "the provider kept no signature for this reply"
        text, signature = body.get("text"), body.get("signature")
        if not isinstance(text, str) or not isinstance(signature, str):
            return UNSIGNED, "the signature record held no text or signature"
        try:
            signer = recover_personal(text, signature)
        except ValueError:
            return MISMATCH, "the signature is not a valid signature"
        if signer.lower() != self.signing_address.lower():
            return MISMATCH, f"signed by {signer}, not the attested {self.signing_address}"
        return SIGNED, f"signed by the attested key {self.signing_address}"




# ---- one operation ------------------------------------------------------------------

class _Attesting:
    """The model's attestation while it is being made, for the threads that
    ask meanwhile: they wait for its answer rather than attest it again."""

    def __init__(self):
        self.done = threading.Event()
        self.attestation = None
        self.error = None


class PrivacySession:
    """One operation's Privacy Mode: its model, and what its enclave proved.

    Made once, when an operation starts - a Detect Chapters dialog opened, an
    Identify Library run, a test in Settings - from the settings at that
    moment (ai.settings_from), and kept by that operation to its end. Nothing
    reads the Privacy Mode settings again for it, which is what makes "an
    operation cannot leave Privacy Mode part way" true by construction.

    Thread-safe, and its lock is never held across the network: a worker
    thread does the attesting, which can take a minute, while the GUI thread
    reads `attestation` for what it shows. `clock` ages attestations: the boot
    clock, which runs on while the machine sleeps, unless a test gives another.

    `strict`: refuse an attestation that proved less than it could
    (Attestation.fell_short), and attest a TEE model again after a reply whose
    signature did not hold. Fixed for the operation, like its model.
    """

    def __init__(self, api_url, api_key, model, request_timeout=240,
                 tee_factory=TeeClient, sealed_factory=None, clock=dcap.since_boot,
                 strict=False):
        self.api_url = api_url
        self.api_key = api_key
        self.model = str(model or "").strip()
        self.kind = kind_of(self.model)
        self.request_timeout = request_timeout
        self.strict = bool(strict)
        self._tee_factory = tee_factory
        self._sealed_factory = sealed_factory
        self._clock = clock
        self._lock = threading.RLock()
        self._tee = None
        self._sealed = None
        self._attesting = None          # _Attesting, while the model is attested
        self.attestation = None         # the newest check that held
        self._attested_at = None        # the clock when that check began
        self._doubted = False           # a TEE model to attest again: see check_reply
        self._refused = False           # refused since it last attested well
        self._listed = False            # the relay's list was read for it
        self._receipts = []
        # Called with (event, detail) from whichever thread is working:
        # "attesting", "attested" (detail: Attestation), "refused" (detail:
        # message), "receipt" (detail: Receipt).
        self.listener = None

    # ---- events ---------------------------------------------------------------------

    def _emit(self, event, detail=None):
        listener = self.listener
        if listener is not None:
            listener(event, detail)

    # ---- attestation ----------------------------------------------------------------

    def sealed_client(self):
        """The end-to-end encrypted client the private/ model is sent through.
        One per operation, so its requests share one attestation of the
        enclave, renewed as sealed.py says, and its cache secret - ours, random,
        never stored - lasts exactly as long as the operation."""
        with self._lock:
            if self._sealed is None:
                factory = self._sealed_factory
                if factory is None:
                    from . import sealed
                    factory = sealed.PrivateModeClient
                self._sealed = factory(self.api_url, self.api_key, self.request_timeout)
                self._sealed.reattested = self._reattested
            return self._sealed

    def ensure(self):
        """Attest the model's enclave if it has not been, or not lately, and
        return what it proved.

        A TEE enclave is attested before its first use, and again once its
        attestation is TEE_MAX_AGE_SECONDS old on the session's clock; every
        reply's signature is checked against the key its newest attestation
        named. The end-to-end encrypted enclave is attested again by sealed.py
        whenever its proof is five minutes old, before anything is sealed.
        Raises PrivacyError, having sent nothing, when the enclave cannot be
        trusted - or, with strict attestation, when it proved less than it
        could.

        No lock is held while the enclave is asked. A thread that asks while
        another is attesting waits for that one's answer and is given it: the
        model is never attested twice at once."""
        if self.kind is None:
            raise PrivacyError(f"{self.model or 'No model'} runs in no attested enclave; "
                               f"nothing was sent")
        with self._lock:
            known = self.attestation
            if self.kind == TEE and known is not None and not self._due():
                return known
            attesting = self._attesting
            waiting = attesting is not None
            if not waiting:
                attesting = self._attesting = _Attesting()
        if waiting:
            attesting.done.wait()
            if attesting.error is not None:
                raise attesting.error
            if attesting.attestation is None:
                raise PrivacyError(f"{self.model}'s attestation was interrupted; nothing "
                                   f"was sent")
            return attesting.attestation
        try:
            attesting.attestation = self._attest(known)
        except PrivacyError as e:
            attesting.error = e
            raise
        finally:
            with self._lock:
                self._attesting = None
            attesting.done.set()
        return attesting.attestation

    def _due(self):
        """Whether a TEE model must be attested again before anything more is
        sent to it: its attestation is too old, or strict attestation doubted a
        reply."""
        if self._doubted or self._attested_at is None:
            return True
        age = self._clock() - self._attested_at
        return not 0 <= age < TEE_MAX_AGE_SECONDS

    def _attest(self, known):
        """Attest the enclave now, telling the listener. `known` is the
        attestation it last gave, if any."""
        model = self.model
        announce = known is None or self.kind == TEE
        if announce:
            self._emit("attesting")
        started = self._clock()
        try:
            if self.kind == E2EE:
                client = self.sealed_client()
                if not self._listed:
                    # The enclave is Tinfoil's router whatever the model, so a
                    # model nano-gpt has retired would attest: its list first.
                    client.check_offered(model)
                    self._listed = True
                attestation = client.attest(model)
            else:
                with self._lock:
                    if self._tee is None:
                        self._tee = self._tee_factory(self.api_url, self.api_key, model)
                    client = self._tee
                attestation = client.attest()
            lacking = attestation.fell_short if self.strict else []
            if lacking:
                raise PrivacyError(
                    f"{model}'s enclave proved less than it could "
                    f"({', '.join(lacking)}), and strict attestation is on"
                    + ("; nano-gpt's TEE/ models come through RedPill's gateway, which "
                       "cannot prove more: choose a private/ model, sealed end to end, "
                       "in Settings → AI" if GATEWAY_SHORTFALL in lacking else ""))
        except ModelRetired as e:
            self._refuse(str(e))
            raise
        except PrivacyError as e:
            self._refuse(str(e))
            raise PrivacyError(f"{e}; nothing was sent") from e
        except Exception as e:  # the SDK's own errors, network failures
            message = f"{model}'s enclave could not be attested: {unsealed_words(e, 500)}"
            self._refuse(message)
            raise PrivacyError(f"{message}; nothing was sent") from e
        self._accept(attestation, started, announce or known.detail != attestation.detail)
        return attestation

    def _refuse(self, message):
        with self._lock:
            self._refused = True
        self._emit("refused", message)

    def _accept(self, attestation, started, changed):
        """Keep `attestation`, and tell the listener when it says something new.
        One the sealed client made goes back to it: only now, Strict and all,
        may anything be sealed to its proof (PrivateModeClient.accept)."""
        with self._lock:
            self.attestation = attestation
            self._attested_at = started
            self._doubted = False
            recovered = self._refused
            self._refused = False
        if attestation.proof is not None:
            self._sealed.accept(attestation)
        if changed or recovered:
            self._emit("attested", attestation)

    def _reattested(self, model, attestation):
        """sealed.py is about to seal to a proof this session has not accepted:
        one it attested in the middle of a request, its key rotated or its
        proof aged since ensure(). It is held to what every attestation of the
        session is held to. Raises PrivacyError to refuse it."""
        lacking = attestation.fell_short if self.strict else []
        if lacking:
            message = (f"{model}'s enclave, attested again, proved less than it could "
                       f"({', '.join(lacking)}), and strict attestation is on")
            self._refuse(message)
            raise PrivacyError(f"{message}; nothing was sent")
        known = self.attestation
        self._accept(attestation, self._clock(),
                     known is None or known.detail != attestation.detail)

    def check_reply(self, response_id):
        """Record and return what can be said about one reply.

        With strict attestation, a TEE reply whose signature does not hold -
        signed by another key, or by none when the attestation named the key
        its replies are signed with - has its model attested again before
        anything more is sent to it."""
        if self.kind == E2EE:
            receipt = Receipt(self.model, E2EE, SEALED,
                              "decrypted here; only the attested enclave could have "
                              "sealed it")
        elif self._tee is None:
            receipt = Receipt(self.model, TEE, UNSIGNED, "the enclave had not been attested")
        else:
            state, detail = self._tee.check_reply(response_id)
            receipt = Receipt(self.model, TEE, state, detail)
        with self._lock:
            self._receipts.append(receipt)
            named = getattr(self.attestation, "signing_address", "")
            if self.strict and (receipt.state == MISMATCH
                                or (receipt.state == UNSIGNED and named)):
                self._doubted = True
        self._emit("receipt", receipt)
        return receipt

    def last_receipt(self):
        """The newest reply's receipt, or None."""
        with self._lock:
            return self._receipts[-1] if self._receipts else None

    # ---- what to show ---------------------------------------------------------------

    def caveats(self):
        """What was not checked, or stays visible, for this kind of model."""
        if self.kind == E2EE:
            return ("Before anything is sealed to it, the enclave's hardware report and "
                    "its software release, against Tinfoil's Sigstore record, are "
                    "checked, and every reply must come back sealed by it. nano-gpt "
                    "relays only ciphertext, but still sees your account, the model, "
                    "when you send, how much, and the cost.")
        if self.kind == TEE:
            return ("nano-gpt's gateway sees what is sent in the clear: the frames, the "
                    "names, the tracklist. Not checked by this app: whether a reply's "
                    "content matches its signed record, and which software the enclave "
                    "runs - its measurements are proved genuine by Intel's chain, but "
                    "not compared with a known build.")
        return ""

    def details(self):
        """The model and what its enclave proved, for the details window."""
        attestation = self.attestation
        lines = [self.model]
        if attestation is None:
            lines.append("    not used yet: it will be attested before anything is sent "
                         "to it")
        else:
            when = time.strftime("%H:%M:%S", time.localtime(attestation.checked_at))
            lines.append(f"    {attestation.headline}, checked at {when}")
            if attestation.fell_short:
                lines.append(f"    less than it could prove: "
                             f"{', '.join(attestation.fell_short)}")
            lines.append(f"    {attestation.detail}")
            if attestation.signing_address:
                lines.append(f"    signing key {attestation.signing_address}")
        receipt = self.last_receipt()
        if receipt is not None:
            lines.append(f"    last reply: {receipt_label(receipt)[0]} - {receipt.detail}")
        lines.append(f"    {describe(self.model)[1]}")
        caveats = self.caveats()
        if caveats:
            lines.append(f"    {caveats}")
        return "\n".join(lines)


# ---- labels ---------------------------------------------------------------------------

OK, WARN, BAD = "ok", "warning", "error"


def attestation_label(attestation):
    """(text, tone) for what an enclave proved."""
    if attestation is None:
        return "not attested yet", WARN
    lacking = attestation.fell_short
    if lacking:
        return f"{attestation.headline} - less than it could prove: {', '.join(lacking)}", WARN
    return attestation.headline, OK


def receipt_label(receipt):
    """(text, tone) for one reply, from its receipt."""
    if receipt is None:
        return "", OK
    if receipt.state == SEALED:
        return "✓ end-to-end encrypted", OK
    if receipt.state == SIGNED:
        return "✓ TEE, signed by the attested enclave", OK
    if receipt.state == MISMATCH:
        return "⚠ TEE, signature mismatch", BAD
    return "TEE, signature unavailable", WARN
