# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Privacy Mode: the AI asked only of models that run in an attested enclave.

nano-gpt offers two kinds, and this module is the one place that tells them
apart:

  TEE/*      Run inside a trusted execution environment. Before anything is
             sent, the enclave is attested (below), and every reply is
             checked against the record of it that the attested key signed.
             The text still passes nano-gpt's gateway in the clear:
             attestation proves where the model runs, not that nano-gpt
             cannot read it. And most of them (all but Chutes' own, in Nov
             2026) are attested as RedPill's gateway, which passes requests
             on to the model's host: that attestation is partial, since it
             does not show the model's own enclave
             (TeeClient._attest_signing_key, _attest_gateway).
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
  With `&report=aci` (Nov 10 2026) it is RedPill's gateway's own report,
  whose quote binds the gateway's whole keyset - the keys that sign each
  reply's record among them - to our nonce (TeeClient._attest_gateway).
  Both are asked for, each with its own nonce.
- `GET {base}/tee/signature/{receipt or completion id}` returns a text and
  an EIP-191 signature; the signer recovered from them (ethsig.py) must be
  a key the attestation bound: the attested address, or a key of the
  keyset the gateway's quote binds. Since Nov 2026 (RedPill's signed
  receipts, and nano-gpt's lookup by the receipt id a reply's headers name)
  the text's response hash is that of the exact bytes the stream carried,
  so a reply is "verified" for the text that came, and RedPill's receipt
  beside it is held to the same hashes (TeeClient.check_reply). Neither hash
  is of what this app sent: the request is hashed as nano-gpt passed it on.
  Every TEE request is streamed (ai._tee_stream), so nano-gpt's exact mode
  for whole replies (`X-NanoGPT-TEE-Response: exact`), whose bytes it
  otherwise rebuilds, is not needed: the bytes hashed are the stream's, as
  they came.

What attestation proves is genuine, current Intel hardware (and NVIDIA GPUs,
where their evidence is offered) holding a live key bound to this request's
nonce. Not which software runs there: no measurement in the quote is
compared with a published one.

An operation's privacy is decided once, when it starts (ai.settings_from
makes its PrivacySession), and never relaxed: its model is copied then, so
switching Privacy Mode off, or choosing another model, leaves a run already
going exactly as it was. So is whether its attestation is strict: an
attestation that proved less than it could is then refused rather than
used, and a TEE reply that proved less than a verified one has its model
attested again before anything more is sent to it.
"""

import base64
import binascii
import hashlib
import json
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

from . import dcap, netguard, nras
from .ethsig import keccak256, recover_personal

NRAS_URL = "https://nras.attestation.nvidia.com/v3/attest/gpu"
TIMEOUT_SECONDS = 30
# Chutes' evidence can take about a minute (nano-gpt, Nov 10 2026; SealedLore measured
# 6-25 s for 12-43 instances that day), so an attestation is waited for longer than the
# other requests.
ATTESTATION_TIMEOUT_SECONDS = 90
PRIVATE_MODE_HOSTS = frozenset({"nano-gpt.com"})
# How old a TEE/ model's attestation may grow, on the boot clock, before the next send
# to it attests it again: an enclave restarted, moved or replaced since is found within
# this, as the end-to-end encrypted one is within sealed.MAX_AGE_SECONDS.
TEE_MAX_AGE_SECONDS = 15 * 60

TEE = "tee"
E2EE = "e2ee"

# What an attestation of RedPill's gateway lacks (TeeClient._attest_signing_key,
# _attest_gateway): it is shown in amber, and Strict refuses it.
GATEWAY_SHORTFALL = "RedPill's gateway, not the model's host"
# And what one lacks whose quote binds the key that signs each reply's record but not
# the gateway's receipt keys beside it: one nano-gpt has no report of the gateway's own
# for, or whose keys that report does not include the signing key among.
RECEIPT_KEYS_LOOSE = "receipt keys not in the quote"
# And what one of a model Chutes serves lacks when nano-gpt gave no report of RedPill's
# gateway for it, for any reason but that RedPill does not serve it (a 400): whether the
# gateway answers it instead now and then, unattested, is not known (_attest_instances).
GATEWAY_UNCHECKED = "RedPill's gateway unchecked"

# A stream's receipt is complete just after the stream ends (nano-gpt's own example
# waits up to about 20 seconds): a reply that names one is looked up again meanwhile.
RECEIPT_WAIT_SECONDS = 20
RECEIPT_RETRY_SECONDS = 2
# Models whose streams nano-gpt rebuilds from the upstream's, so that their bytes never
# match the record's hash (nano-gpt, Nov 2026).
REBUILT_STREAMS = frozenset({"TEE/gemma-4-31b-it"})

# What one reply's check found (TeeClient.check_reply). See Receipt.
VERIFIED = "verified"        # TEE: the attested key signed its record, for these bytes
SIGNED = "signed"            # TEE: the attested key signed its record; bytes not compared
FAILED = "failed"            # TEE: another key signed it, other bytes, or its receipt fails
UNCHECKED = "unchecked"      # TEE: no record could be read, or it could not be judged
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
    signing_address: str = ""       # the key the model's own quote bound, if one did
    # Every key whose signature on a reply's record counts (lower case addresses): the
    # signing key, or the keys of the keyset the gateway's quote binds. None for a
    # provider whose instances sign no replies, with no gateway attested beside them.
    signers: tuple = ()
    instances: int = 0              # Chutes' instances attested, whose replies are unsigned
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


def _canonical(value):
    """RFC 8785 JSON, which is this simple for documents of ASCII keys, strings and
    integers (RedPill's keysets and receipts)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


def _digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _hex(value):
    if not isinstance(value, str):
        raise ValueError("not hex")
    return bytes.fromhex(value.removeprefix("0x"))


def _keyset_of(body):
    """RedPill's aci/1 `workload_keyset`, as nano-gpt relays it, or None."""
    report = body.get("attestation")
    keyset = report.get("workload_keyset") if isinstance(report, dict) else None
    return keyset if isinstance(keyset, dict) else None


def _statement(keyset, nonce):
    """The report data RedPill's quote holds for a keyset and a nonce: the SHA-256 of
    its attestation statement, zero padded (its verifier's computeReportData, read Nov
    2026)."""
    statement = (f'{{"keyset_digest":"{_digest(_canonical(keyset))}","nonce":"{nonce}",'
                 f'"purpose":"aci.report_data.v1"}}').encode()
    return hashlib.sha256(statement).digest() + bytes(32)


def _holds_key(keyset, address):
    """Whether the keyset lists the secp256k1 key whose address is `address`: the
    gateway's e2ee key is the one it signs reply records with (live, Nov 2026)."""
    return any(_address_of_public(entry.get("public_key")) == address.lower()
               for entry in keyset.get("e2ee_public_keys") or []
               if isinstance(entry, dict))


def _address_of_public(value):
    """The address of an uncompressed secp256k1 public key (04...), lower case."""
    try:
        key = _hex(value)
    except ValueError:
        return None
    if len(key) != 65 or key[0] != 4:
        return None
    return "0x" + keccak256(key[1:])[-20:].hex()


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

    def attestation(self, model, bound, signing_address="", signers=(), instances=0):
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
                           signers=tuple(sorted(signers)), instances=instances,
                           gpu_ok=True if self.gpus else None, gpus=self.gpus,
                           tcb_status=self.status)


@dataclass(frozen=True)
class _Gateway:
    """RedPill's gateway, attested for our nonce (TeeClient._attest_gateway)."""

    keyset: dict
    # The addresses of the secp256k1 keys its keyset lists: those that sign each reply's
    # record (the gateway's e2ee keys, live Nov 2026), lower case.
    signers: frozenset
    check: _Checks


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
        # Who may sign a reply's record: the attested signing key, or every key of the
        # keyset the gateway's quote binds (lower case addresses).
        self.signers = frozenset()
        # Attested by its instances (Chutes), whose replies carry no record.
        self.instances = False
        # The gateway's receipt keys, when tied to the attested quote (_take_keyset).
        self.keyset = None
        self.keyset_digest = None
        self._base = None
        # The two verifiers, replaceable so tests can stand in for Intel and NVIDIA.
        self._intel = intel or dcap.verify
        self._nvidia = nvidia or nras.verify

    def _get(self, path, params, route=None, timeout=TIMEOUT_SECONDS):
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        if route:
            # nano-gpt names a route on a reply now and then: the lookup of its record
            # names it too (check_reply). Not the attestation: see attest().
            headers["X-TEE-Verification-Route"] = route
        last, last_status = "", None
        for base in [self._base] if self._base else _bases(self.api_url):
            try:
                response = self.session.get(f"{base}{path}", params=params,
                                            headers=headers, timeout=timeout)
            except netguard.RequestError as e:
                last, last_status = str(e), None
                continue
            if response.status_code == 404:
                last, last_status = f"{base}{path}: 404", 404
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
        error = PrivacyError(f"no TEE endpoint answered ({last})")
        error.status_code = last_status         # 404 when that was the last answer
        error.reason = ""
        raise error

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

        Beside the model's own report, RedPill's gateway's is asked for, with a nonce of
        its own (_attest_gateway, Nov 10 2026). Where it binds the gateway's keys to that
        nonce, they are the ones a reply's record and receipt are checked against: a
        signing key among them binds the receipt keys too; a model whose own report is
        relayed for a nonce not ours, or timed out twice over, is attested through the
        gateway that answers it (_attest_gateway_alone); and a reply RedPill answers for
        a model Chutes serves first is checked against them. One in RedPill's shape for
        another nonce refuses the model; none - Chutes or Tinfoil alone, an outage -
        leaves things as they were, but for a model Chutes serves (GATEWAY_UNCHECKED).

        Stricter than SealedLore, the gateway stands in only in those two cases: the
        model's own report relayed by RedPill for a nonce not ours, or a 504 still a 504
        when asked again. Any other failure of it refuses as before - a 429, a 400, a
        502 or 503, the network, a body holding nothing to check: live (Nov 10 2026),
        nano-gpt rate-limited the report of TEE/glm-5.2, which Chutes answers first, and
        attested through the gateway it would have been sent to Chutes' instances with
        none of them attested.

        Neither report is asked with the route a reply named (X-TEE-Verification-Route),
        as SealedLore's are not: an attestation is made before anything is sent, for
        whatever answers the next request, not for the backend one earlier reply came
        from - and a route expires after an hour, when nano-gpt answers an attestation
        naming it with a 400, which reads as a model that offers none.

        Stricter than StoryLoom, which reads these verdicts without checking their
        signatures: a nonce or a verdict that merely comes back in JSON proves nothing,
        since anything can echo a string.
        """
        gateway, unheard = self._attest_gateway()
        try:
            body, nonce = self._fetch_attestation()
        except PrivacyError as e:
            # Live (Nov 10 2026, TEE/qwen3.6-27b): "RedPill attestation service took too
            # long to respond", a 504 still after it was asked again (_fetch_attestation).
            # Any other failure refuses, gateway or none: see above.
            if gateway is not None and getattr(e, "status_code", None) == 504:
                return self._attest_gateway_alone(gateway, "its own attestation timed out")
            raise self._refused(e)
        if isinstance(body.get("evidence"), list):
            return self._attest_instances(body, nonce, gateway, unheard)
        if isinstance(body.get("all_attestations"), list) and "signing_address" not in body:
            # RedPill relaying Chutes (live, Sept 2026: TEE/glm-5.1, TEE/qwen3.5-27b,
            # TEE/qwen3.5-397b-a17b; Nov 10 2026: TEE/deepseek-v3.2). The quotes are made
            # for a nonce the relay chose - not ours, and not derived from it - and
            # nothing signed ties them to ours, so they would read the same if replayed
            # from any earlier day. The gateway that answers, attested for our nonce,
            # can stand in (_attest_gateway_alone).
            if gateway is not None:
                return self._attest_gateway_alone(gateway, "its upstream's evidence was "
                                                           "relayed for a nonce not ours")
            relayed = body.get("nonce")
            why = ("made for a nonce the relay chose, not ours" if relayed != nonce
                   else "carries no binding of our nonce this app can check")
            raise PrivacyError(f"{self.model}'s attestation, relayed by its provider "
                               f"({unsealed_words(body.get('attestation_type'), 40) or 'unnamed'}"
                               f"), was {why}, "
                               f"so it cannot show the enclave is live for this request")
        if body.get("signing_address") or body.get("intel_quote"):
            return self._attest_signing_key(body, nonce, gateway)
        if body.get("error"):
            raise PrivacyError(f"nano-gpt could not attest {self.model}: "
                               f"{unsealed_words(body['error'])}")
        raise PrivacyError(f"{self.model}'s attestation is in a shape this app does not "
                           f"recognise (it holds "
                           f"{unsealed_words(', '.join(sorted(body))) or 'nothing'})")

    def _fetch_attestation(self, aci=False):
        """nano-gpt's attestation for a fresh nonce, and the nonce: the model's own, or
        with `aci` the gateway's. A 504 - the provider's evidence took too long,
        `tee_attestation_upstream_timeout` - is asked once more with a new nonce, as
        nano-gpt says to (Nov 10 2026). From SealedLore."""
        for attempt in range(2):
            nonce = secrets.token_hex(32)
            params = {"model": self.model, "nonce": nonce}
            if aci:
                params["report"] = "aci"
            try:
                body = self._get("/tee/attestation", params,
                                 timeout=ATTESTATION_TIMEOUT_SECONDS)
            except PrivacyError as e:
                if getattr(e, "status_code", None) == 504 and not attempt:
                    continue
                raise
            return body, nonce
        raise AssertionError("unreachable")

    def _refused(self, e):
        """Why the model's attestation could not be had (`e`, from _get), as a refusal."""
        status = getattr(e, "status_code", None)
        if status == 400:
            # Live (Sept 2026): the Tinfoil-hosted TEE/ models offer no attestation
            # here - nano-gpt says fresh attestation for them only comes through
            # Tinfoil's own flow, which is the one their private/ twins use.
            reason = getattr(e, "reason", "")
            hint = ("; its end-to-end encrypted private/ version, where nano-gpt "
                    "offers one, is attested that way" if "tinfoil" in reason.lower()
                    else "")
            return PrivacyError(f"{self.model} offers no attestation"
                                + (f" (nano-gpt: {reason})" if reason else "") + hint)
        # 429 too: live, nano-gpt answers "Unable to reach Chutes TEE evidence
        # service" with it, and the same request passed a minute later.
        if status is not None and (status >= 500 or status == 429):
            return PrivacyError(f"nano-gpt could not fetch {self.model}'s attestation "
                                f"just now ({e}); this is on the provider's side, and "
                                f"may pass if tried again")
        return e

    def _attest_gateway(self):
        """(gateway, unheard): RedPill's gateway, attested for our nonce (`report=aci`,
        nano-gpt, Nov 10 2026), or None where nano-gpt has no such report - a model only
        Chutes or Tinfoil serves (HTTP 400), an outage, or another shape - and then, but
        for a 400, why not (`unheard`, else "").

        Its body is aci/1: `attestation.workload_keyset`, and the TDX quote in
        `attestation.evidence.quote`. The quote's report data, read from its own bytes,
        is the SHA-256 of RedPill's attestation statement of that keyset and our nonce,
        zero padded (_statement): the hardware binds the receipt key and the keys that
        sign each reply's record, which nano-gpt's other report does not (it binds one
        signing key, the keyset published beside it). Every model RedPill serves is
        answered by the same gateway image (SealedLore, live: 14 of 14 quotes alike,
        plain and aci). It carries no GPU evidence.

        Refuses - PrivacyError - a report in RedPill's shape that does not bind our
        nonce, whose quote Intel does not vouch for, or whose keys are past their time.
        What it proves is RedPill's gateway, so it falls short as the gateway always
        does here (GATEWAY_SHORTFALL). From SealedLore (Nov 2026)."""
        try:
            body, nonce = self._fetch_attestation(aci=True)
        except PrivacyError as e:
            # "report=aci is only available for models served through RedPill"
            return None, "" if getattr(e, "status_code", None) == 400 else str(e)
        report = body.get("attestation") if body.get("api_version") == "aci/1" else None
        evidence = report.get("evidence") if isinstance(report, dict) else None
        keyset = report.get("workload_keyset") if isinstance(report, dict) else None
        if not (isinstance(evidence, dict) and isinstance(keyset, dict)
                and evidence.get("quote")):
            return None, "its report was in no shape this app reads"
        try:
            raw = bytes.fromhex(str(evidence["quote"]))
            quote = dcap.parse_quote(raw)
        except (ValueError, dcap.DcapError) as e:
            raise PrivacyError(f"{self.model}'s gateway's Intel TDX quote could not be "
                               f"read ({e})")
        # From the quote's own bytes, never nano-gpt's copy (`report_data`).
        if quote.report_data != _statement(keyset, nonce):
            raise PrivacyError(f"{self.model}'s gateway did not bind its keys to our "
                               f"nonce")
        self._refuse_expired(keyset)
        check = _Checks("the gateway's Intel TDX quote binds its keys to our nonce")
        check.fall_short(GATEWAY_SHORTFALL)
        self._check_quote(raw, check)
        signers = frozenset(filter(None, (_address_of_public(entry.get("public_key"))
                                          for entry in keyset.get("e2ee_public_keys") or []
                                          if isinstance(entry, dict))))
        return _Gateway(keyset=keyset, signers=signers, check=check), ""

    def _refuse_expired(self, keyset):
        """Keys RedPill attests only until a time now past are refused: what was
        attested is keys no longer in force. A keyset that names no such time is not
        used for the receipts, and falls short (_take_keyset)."""
        expires = keyset.get("not_after") if isinstance(keyset, dict) else None
        if isinstance(expires, (int, float)) and not isinstance(expires, bool) \
                and expires < time.time():
            raise PrivacyError(f"{self.model}'s attested keys expired on "
                               f"{time.strftime('%Y-%m-%d', time.gmtime(expires))}")

    def _attest_gateway_alone(self, gateway, why):
        """The gateway, attested for our nonce, stands for a model RedPill answers whose
        own attestation cannot be had or checked (live, Nov 10 2026: TEE/deepseek-v3.2
        and TEE/qwen3.6-27b, relayed for a nonce not ours; TEE/qwen3.6-27b earlier that
        day, a 504 asked again). Its replies are signed by the gateway's bound keys and
        carry its receipts; the model's own enclave and GPUs are then the gateway's word
        in each receipt (`upstream.verified`), not checked here - so it falls short
        twice over: the gateway, and no GPU evidence. From SealedLore."""
        check = _Checks(f"{why}: the gateway that answers it is attested instead")
        check.merge(gateway.check)
        check.fall_short("no GPU evidence")
        check.summary_gpu.add("no GPU evidence")
        check.note("its own enclave is not attested here: RedPill's gateway passes each "
                   "request on to the model's host, and each reply's receipt carries the "
                   "gateway's word that its upstream was verified")
        self.signing_address, self.signers, self.instances = None, gateway.signers, False
        self._take_keyset(gateway.keyset, "the quote binds its receipt keys", check)
        return check.attestation(self.model, "gateway's keys bound to our nonce",
                                 signers=self.signers)

    def _attest_signing_key(self, body, nonce, gateway=None):
        """One enclave, its reply-signing key and our nonce in its quote.

        What nano-gpt relays for its TEE/ models in this shape is RedPill's gateway
        (aci/1, "serving": "aggregator"; live, Nov 2026, every one of them, one signing
        key for all), which passes each request on to the model's host. Its quote is the
        gateway's; the GPU evidence is the upstream node's, made with our nonce but not
        bound to that quote; that the model itself runs in an enclave is not in it -
        RedPill says that per reply, in a signed receipt, which check_reply reads where
        nano-gpt passes it on. So such an attestation is partial (GATEWAY_SHORTFALL),
        and Strict refuses it: a private/ model is sealed end to end, to an enclave that
        is attested itself. Keys RedPill attests that have expired are refused. From DA
        Vibe Manager (Nov 2026).

        The quote's report data binds the signing key in one of two ways: as nano-gpt
        serves it, the signing address, zero padding and our nonce; or as RedPill's own
        statement does, the gateway's keyset - the signing key among its keys - and our
        nonce (_statement). The first binds the key that signs each reply's record and
        not the receipt keys beside it, which are only published with it: it is
        partial for that too (RECEIPT_KEYS_LOOSE), keyset or none, so that leaving the
        keyset out never reads better than sending it loose. The keyset is kept for the
        replies' receipts either way it is tied (_take_keyset). From SealedLore (Nov
        2026).

        Unless the gateway's own report (`gateway`, _attest_gateway) binds a keyset the
        signing key is in: the receipt keys are then the hardware's too, and a reply's
        record is checked against every key that keyset holds (live, Nov 10 2026: every
        signing key was one of the gateway's two). A gateway whose keys do not hold the
        signing key counts for nothing here.
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
        # from nano-gpt's JSON copy of it.
        report_data = quote.report_data
        keyset = _keyset_of(body)
        holds = keyset is not None and _holds_key(keyset, address)
        signers = frozenset({address.lower()})
        through_gateway = False
        if report_data[:20] == bytes.fromhex(address[2:]) and not any(report_data[20:32]) \
                and report_data[32:] == bytes.fromhex(nonce):
            check = _Checks("the Intel TDX quote binds the signing key to our nonce")
            bound = "signing key bound to our nonce"
            if gateway is not None and address.lower() in gateway.signers:
                # The gateway's report binds the keyset this key is in: the receipt key
                # is the hardware's too, and its other keys sign records as this one does.
                check.merge(gateway.check)
                self._refuse_expired(keyset)        # the one published beside it, too
                keyset, signers, through_gateway = gateway.keyset, gateway.signers, True
                tie = ("the gateway's quote binds its receipt keys, the signing key among "
                       "them")
                loose, bound = False, "signing key and gateway's keys bound to our nonce"
            else:
                tie = ("its receipt keys are published beside the attested signing key"
                       if holds else "")
                loose = True
                if gateway is not None:
                    check.note("the gateway's attested keys do not include the signing "
                               "key")
        elif holds and report_data == _statement(keyset, nonce):
            check = _Checks("the Intel TDX quote binds the gateway's keys, its signing key "
                            "among them, to our nonce")
            tie = "the quote binds its receipt keys"
            loose, bound = False, "gateway's keys bound to our nonce"
        else:
            raise PrivacyError(f"{self.model}'s attestation did not bind its signing key "
                               f"to our nonce")
        self._refuse_expired(keyset)
        serving = body.get("service_capabilities")
        if through_gateway or body.get("api_version") == "aci/1" or (
                isinstance(serving, dict) and serving.get("serving") == "aggregator"):
            check.fall_short(GATEWAY_SHORTFALL)
            check.note("this is RedPill's gateway, which passes each request on to the "
                       "model's host: the gateway's quote and the GPUs' evidence are "
                       "checked, not that the model itself runs in an enclave")
        if loose:
            check.fall_short(RECEIPT_KEYS_LOOSE)
            check.note("the quote binds the key that signs each reply's record, not the "
                       "gateway's receipt keys")
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
        self.signing_address, self.signers, self.instances = address, signers, False
        self._take_keyset(keyset if tie else None, tie, check)
        return check.attestation(self.model, bound, signing_address=address,
                                 signers=signers)

    def _take_keyset(self, keyset, tie, check):
        """The gateway's receipt-signing keys (RedPill's aci/1 workload_keyset), kept
        for the replies' receipts (check_reply) only when tied to the quote: it commits
        to them (RedPill's statement, or the gateway's own report), or they are
        published with the signing key it binds (nano-gpt's other report, Nov 2026),
        the weaker tie. A keyset tied neither way, or past its time, is not used, and
        receipts go unread. From SealedLore (Nov 2026)."""
        self.keyset = self.keyset_digest = None
        if keyset is None:
            return
        not_after = keyset.get("not_after")
        if not isinstance(not_after, int) or not_after <= time.time():
            check.note("its receipt keys have expired, so receipts are not checked")
            check.fall_short("receipt keys expired")
            return
        self.keyset, self.keyset_digest = keyset, _digest(_canonical(keyset))
        check.note(tie)

    def _attest_instances(self, body, nonce, gateway=None, unheard=""):
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

        A model Chutes answers first falls back to RedPill now and then (nano-gpt, Nov
        10 2026: TEE/glm-5.1, TEE/glm-5.2, TEE/kimi-k2.6). With the gateway attested
        too (`gateway`), such a reply - it names a receipt - is checked against the
        gateway's bound keys, and the gateway's checks count with the instances'. Its
        shortfall among them: a request may be answered by RedPill's gateway, which
        does not show the model's host, so Strict refuses such a model as it does any
        other the gateway answers. SealedLore counts it whole; this app does not. And
        where nano-gpt gave no report of the gateway for a reason but that RedPill does
        not serve the model (`unheard`), whether it may answer is not known: that falls
        short too (GATEWAY_UNCHECKED), so that an outage never reads better than the
        gateway's report would.
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
        self.signing_address, self.instances = None, True
        if gateway is None:
            if unheard:
                check.fall_short(GATEWAY_UNCHECKED)
                check.note(f"nano-gpt gave no report of RedPill's gateway, which answers "
                           f"some models Chutes serves first now and then, just now "
                           f"({unheard}): a reply it answered could not be checked")
            self.signers = frozenset()
            self._take_keyset(None, "", check)
            attestation = check.attestation(self.model, summary, instances=len(instances))
            attestation.detail += ("; this provider signs no replies, so answers show as "
                                   "unsigned")
            return attestation
        check.merge(gateway.check)
        check.note("RedPill's gateway answers it instead now and then, and passes the "
                   "request on to a host of its own: such a reply is checked against the "
                   "gateway's attested keys, and its host is the gateway's word, not "
                   "attested here")
        self.signers = gateway.signers
        self._take_keyset(gateway.keyset, "a reply RedPill's gateway answers instead is "
                                          "checked against its attested keys", check)
        attestation = check.attestation(self.model, summary + ", and RedPill's gateway's "
                                        "keys", signers=self.signers,
                                        instances=len(instances))
        attestation.detail += ("; the instances sign no replies, so their answers show as "
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

    def check_reply(self, response_id, receipt_id=None, body_hash=None, route=None):
        """Whether this reply was the attested enclave's, as it arrived: VERIFIED,
        SIGNED, FAILED or UNCHECKED, with a sentence saying why.

        nano-gpt's record of a reply (Nov 2026: RedPill's signed receipts), looked up by
        the receipt id its headers named, else by the reply's own id:
          - `text`, "sha256(request):sha256(response)" as the gateway hashed them,
            signed (EIP-191) by the key the attestation bound;
          - the response's hash, that of the exact bytes the stream carried
            (`body_hash`, "sha256:...", hashed by ai.py as they were read) - but for a
            model whose stream nano-gpt rebuilds (REBUILT_STREAMS), and a stream that
            did not arrive whole (no `body_hash`), whose bytes are not compared;
          - RedPill's receipt, signed (Ed25519) by a key of the keyset kept at
            attestation (_take_keyset), for the same two hashes, saying its upstream was
            verified, as nano-gpt requires (_receipt_holds).

        "The key the attestation bound" is any of `signers`: the attested signing key,
        or the keys of the keyset the gateway's quote binds (Nov 10 2026), of which a
        model attested through the gateway alone has no other. A model attested by its
        instances (Chutes) signs no replies, and one that names no receipt is not looked
        up: only a reply RedPill's gateway answered instead names one, and is checked
        against the gateway's keys, where those were attested.

        VERIFIED: signed by the attested key, for these exact bytes, its receipt holding
        where it was read. SIGNED: signed by it, the bytes not compared. FAILED: signed
        by another key, or for other bytes, or a receipt that does not hold. UNCHECKED:
        no record could be read, or the gateway's keys changed since it was attested.
        Until Nov 2026 only the signer was checked, so "signed" said nothing of the text
        shown. From SealedLore (Nov 2026)."""
        lookup = receipt_id or response_id
        if not lookup:
            return UNCHECKED, "the reply carried no id to check"
        if not self.signers or (self.instances and receipt_id is None):
            # The per-instance providers (Chutes) sign no replies: nano-gpt answers
            # "TEE signature is not available for this model". Their instances were each
            # attested instead. Only a reply RedPill's gateway answered names a receipt
            # (nano-gpt, Nov 10 2026).
            return UNCHECKED, ("this model's provider signs no replies; every instance "
                               "that could answer was attested before sending")
        try:
            body = self._signature(lookup, route, wait=receipt_id is not None)
        except PrivacyError:
            # Live: some TEE/ models' providers keep no record of a reply.
            return UNCHECKED, "the provider kept no signature for this reply"
        text, signature = body.get("text"), body.get("signature")
        if not isinstance(text, str) or not isinstance(signature, str):
            return UNCHECKED, "the signature record held no text or signature"
        try:
            signer = recover_personal(text, signature)
        except ValueError:
            return FAILED, "the signature is not a valid signature"
        if signer.lower() not in self.signers:
            return FAILED, f"signed by {signer}, not {self._attested_keys()}"
        # Named as the attestation named it, where it named this one.
        key = (self.signing_address
               if (self.signing_address or "").lower() == signer.lower() else signer)
        request_hash, _colon, response_hash = text.partition(":")
        # However the model is written, as is_tee reads it.
        rebuilt = str(self.model).strip().lower() in {m.lower() for m in REBUILT_STREAMS}
        compared = bool(body_hash) and not rebuilt
        if compared and f"sha256:{response_hash.lower()}" != body_hash:
            return FAILED, (f"the attested key {key} signed a record of other bytes than "
                            f"the reply that came")
        receipt = body.get("receipt")
        read = isinstance(receipt, dict) and self.keyset is not None
        if read:
            try:
                holds = self._receipt_holds(receipt, request_hash, response_hash)
            except PrivacyError as e:
                return UNCHECKED, str(e)
            if not holds:
                return FAILED, "RedPill's receipt for it does not hold"
        held = "; RedPill's receipt for it holds" if read else ""
        if compared:
            return VERIFIED, (f"signed by the attested key {key}, for the exact bytes "
                              f"that came{held}")
        why = ("nano-gpt rebuilds this model's stream" if rebuilt
               else "the reply did not arrive whole")
        return SIGNED, (f"signed by the attested key {key}{held}; its text was not "
                        f"compared with the record: {why}")

    def _attested_keys(self):
        """The keys a reply's record may be signed by, as a sentence names them."""
        if self.signing_address and self.signers == {self.signing_address.lower()}:
            return f"the attested {self.signing_address}"
        return "any key the attestation bound (" + ", ".join(sorted(self.signers)) + ")"

    def _signature(self, lookup, route, wait):
        """The reply's record, or PrivacyError. For a reply that named its receipt, asked
        again while it is not ready (404, 502, 503), for RECEIPT_WAIT_SECONDS: a stream's
        receipt is complete just after it ends. One that named none is asked once:
        live, replies that named none had no record to wait for."""
        path = f"/tee/signature/{quote(lookup, safe='')}"
        deadline = time.monotonic() + (RECEIPT_WAIT_SECONDS if wait else 0)
        while True:
            try:
                return self._get(path, {"model": self.model}, route=route)
            except PrivacyError as e:
                ready_later = getattr(e, "status_code", None) in (404, 502, 503)
                if not ready_later or time.monotonic() >= deadline:
                    raise
            time.sleep(RECEIPT_RETRY_SECONDS)

    def _receipt_holds(self, receipt, request_hash, response_hash):
        """Whether the receipt was signed by a key of the attested keyset, under that
        keyset, for this exchange - the hashes the attested key signed - and says its
        upstream was verified, as nano-gpt requires. PrivacyError when the gateway's
        keys have changed since: not a reply that failed, one that cannot be judged."""
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        if receipt.get("workload_keyset_digest") != self.keyset_digest:
            # The gateway renews its keys now and then.
            raise PrivacyError("the gateway's keys changed since it was attested")
        served = receipt.get("served_at")
        if receipt.get("api_version") != "aci/1" or not isinstance(served, int):
            return False
        if served >= self.keyset.get("not_after", 0):
            return False
        named = receipt.get("key_id")
        entry = next((key for key in self.keyset.get("receipt_signing_keys") or []
                      if isinstance(key, dict) and key.get("key_id") == named), None)
        if entry is None or entry.get("algo") != "ed25519":
            return False
        unsigned = {key: value for key, value in receipt.items() if key != "signature"}
        try:
            Ed25519PublicKey.from_public_bytes(_hex(entry.get("public_key"))).verify(
                _hex(receipt.get("signature")), _canonical(unsigned))
        except (InvalidSignature, ValueError, TypeError):
            return False
        events = {}
        for event in receipt.get("event_log") or []:
            if isinstance(event, dict):
                events.setdefault(str(event.get("type")), event)
        hashes = (events.get("request.received", {}).get("body_hash"),
                  events.get("response.returned", {}).get("body_hash"))
        if hashes != (f"sha256:{request_hash.lower()}", f"sha256:{response_hash.lower()}"):
            return False
        upstream = events.get("upstream.verified", {})
        return upstream.get("result") == "verified" and upstream.get("required") is True



# ---- one operation ------------------------------------------------------------------

def _signs(attestation, receipt_id):
    """Whether a TEE reply is one the keys its model's attestation bound were to sign:
    it bound some (its signing key, or the gateway's keys), and - for a model attested
    by its instances, which sign none - the reply named a receipt, so RedPill's gateway
    answered it instead (Nov 10 2026)."""
    if attestation is None:
        return False
    signers = getattr(attestation, "signers", ()) or (
        (attestation.signing_address,) if attestation.signing_address else ())
    return bool(signers) and (receipt_id is not None
                              or not getattr(attestation, "instances", 0))


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
    (Attestation.fell_short), and attest a TEE model again after a reply that
    proved less than a verified one. Fixed for the operation, like its model.
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

    def check_reply(self, response_id, receipt_id=None, body_hash=None, route=None):
        """Record and return what can be said about one reply. `receipt_id`,
        `body_hash` and `route` are what a TEE reply's stream said of itself: the
        receipt id and route nano-gpt's headers named, and the hash of every byte
        it carried (TeeClient.check_reply).

        With strict attestation, a TEE reply that proved less than a verified
        one - a check that failed, or, one the attested keys were to sign
        (_signs), a record that could not be read or whose text could not be
        compared - has its model attested again before anything more is sent to
        it."""
        if self.kind == E2EE:
            receipt = Receipt(self.model, E2EE, SEALED,
                              "decrypted here; only the attested enclave could have "
                              "sealed it")
        elif self._tee is None:
            receipt = Receipt(self.model, TEE, UNCHECKED,
                              "the enclave had not been attested")
        else:
            state, detail = self._tee.check_reply(response_id, receipt_id=receipt_id,
                                                  body_hash=body_hash, route=route)
            receipt = Receipt(self.model, TEE, state, detail)
        with self._lock:
            self._receipts.append(receipt)
            signs = _signs(self.attestation, receipt_id)
            if self.strict and (receipt.state == FAILED
                                or (receipt.state in (UNCHECKED, SIGNED) and signs)):
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
                    "names, the tracklist. A reply is checked against the record the "
                    "attested key signed, for the exact bytes that came, where nano-gpt "
                    "passes its stream on as it was. Not checked by this app: what was "
                    "sent - the record hashes the request as nano-gpt passed it on, so "
                    "that it was this app's rests on nano-gpt; where nano-gpt has no "
                    "report of RedPill's gateway's own for a model, that the gateway's "
                    "receipt keys are in the quote; where the gateway answers, that the "
                    "model's own host is an enclave - each reply's receipt says so, in "
                    "the gateway's word; and which software the enclave runs - its quote "
                    "shows genuine, current hardware holding a live key bound to this "
                    "app's nonce, and no measurement in it is compared with a published "
                    "one.")
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
            keys = list(attestation.signers) or (
                [attestation.signing_address] if attestation.signing_address else [])
            if keys:
                lines.append(f"    signing key{'s' if len(keys) != 1 else ''} "
                             f"{', '.join(keys)}")
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
    """(text, tone) for one reply, from its receipt: what its check found, as DA
    Assistant's receipt_badge says it (Nov 2026). "Verified" only for a reply whose
    exact bytes the attested key's record names; one signed but not compared, or
    with no record to read, is amber, and one whose check failed is red."""
    if receipt is None:
        return "", OK
    if receipt.state == SEALED:
        return "✓ end-to-end encrypted", OK
    if receipt.state == VERIFIED:
        return "✓ TEE, verified", OK
    if receipt.state == SIGNED:
        return "TEE, signed, text not compared", WARN
    if receipt.state == FAILED:
        return "⚠ TEE, check failed", BAD
    return "⚠ TEE, not verified", WARN
