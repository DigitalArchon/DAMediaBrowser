# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Chutes' end-to-end encryption, the parts that are pure: no network, no clock, nothing
read or written but what each function is given. Preliminary, as SealedLore's is.

SealedLore's providers/chutes_e2ee.py, in this app's words (its errors are
private_ai.PrivacyError, what Chutes writes is said through private_ai.unsealed_words,
JSON is read through safejson). Shared with DA Assistant line for line (this copy
differs only in this docstring, its imports and the hooks tests/test_shared_code.py
lists).

Chutes (api.chutes.ai) relays a request it cannot read to one of a model's instances, a
TDX machine a third-party miner runs, which alone holds the private half of the
instance's ML-KEM-768 key (made inside it at start-up). The protocol, as read from
Chutes' own code (Nov 14 2026, MIT: chutes-api `api/e2e/router.py` and
`scripts/test_e2e_client.py`, chutes `chutes/entrypoint/run.py` and `verify.py`, sek8s
`providers/base.py`):

- **A request** (`seal_request`): encapsulate to the instance's `e2e_pubkey` (base64,
  1184 bytes), giving a ciphertext `ct` (1088 bytes) and a shared secret; key =
  HKDF-SHA256(secret, salt = ct[:16], info = b"e2e-req-v1"), 32 bytes. The body is the
  OpenAI JSON with `e2e_response_pk` (a fresh ML-KEM-768 key of ours, base64) added,
  gzipped, sealed with ChaCha20-Poly1305 (a random 12-byte nonce, no associated data):
  the blob `ct || nonce || ciphertext || tag`, POSTed to `/e2e/invoke`.
- **A reply sent whole** (`open_reply`, which this app never asks for: every request
  streams): the same layout, encapsulated to our response key, info b"e2e-resp-v1".
- **A streamed reply** (`StreamOpener`): SSE events. `{"e2e_init": b64(ct)}` first, the
  stream key from it with info b"e2e-stream-v1"; then `{"e2e": b64(nonce || ciphertext
  || tag)}` per piece of the model server's own SSE bytes (its `data: [DONE]` included);
  beside them, unsealed, `{"usage": ...}` (Chutes' copy, for its billing), and
  `{"e2e_error": ...}` and a `data: [DONE]`.
- **The key binding** (`key_bound`): asked for evidence with our nonce, an instance's
  workload asks for a TDX quote whose report data begins with sha256(nonce_hex +
  e2e_pubkey_b64), the two strings as written, UTF-8. So the hardware vouches for the key
  a request is sealed to, for our nonce.
- **Measurements** (`measured`): the quote's MRTD and RTMR0-3 against the sets Chutes
  publishes (`/servers/tdx/measurements`).
- **Models** (`listed_models`, `confidential_models`): `llm.chutes.ai/v1/models`,
  keyless; its end-to-end encryption goes only to those listed `confidential_compute:
  true` (exactly its -TEE ids, Nov 15 2026).

ML-KEM-768 comes from the cryptography package already pinned (50.0.2, which both of
this app's locks ship, built against an OpenSSL with ML-KEM): nothing new is added, and
where the cryptography in use has no ML-KEM, Chutes' end-to-end encryption is refused in
words (`mlkem_refusal`). Chutes' own Python client (`chutes-e2ee` 0.1.0) is not used: it
attests nothing, sends a body it cannot read as JSON in the clear, and takes any
unsealed event naming `usage` for the model's (see `StreamOpener`).
"""

import base64
import binascii
import codecs
import gzip
import hashlib
import hmac
import json
import os
import re
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field

from . import safejson
from .private_ai import PrivacyError, unsealed_words

PUBLIC_KEY_BYTES = 1184         # ML-KEM-768 encapsulation key
CIPHERTEXT_BYTES = 1088         # ML-KEM-768 ciphertext
SHARED_SECRET_BYTES = 32
SALT_BYTES = 16
NONCE_BYTES = 12
TAG_BYTES = 16
KEY_BYTES = 32
INFO_REQUEST = b"e2e-req-v1"
INFO_RESPONSE = b"e2e-resp-v1"
INFO_STREAM = b"e2e-stream-v1"
RESPONSE_KEY_FIELD = "e2e_response_pk"
# What a sealed reply may open to, decoded, at most (the DA apps' standard for a reply
# read whole): its gzip is inflated a piece at a time and refused past this.
MAX_DECODED_BYTES = 32 * 1024 * 1024
# A line of a sealed stream's own text, at most.
MAX_LINE_CHARS = 32 * 1024 * 1024
_INFLATE_PIECE = 256 * 1024
_HEX96 = re.compile(r"[0-9a-fA-F]{96}")

MLKEM_MISSING = (
    "This computer's cryptography library has no ML-KEM-768 (it needs cryptography 50 "
    "built with OpenSSL 3.5 or later, as the AppImage carries and "
    "tools/install-private-mode.sh installs from source), so Chutes' end-to-end "
    "encryption cannot be used here; nothing was sent.")


class SealError(PrivacyError):
    """A request could not be sealed, or a reply opened: nothing of it is used."""


class SealBroken(SealError):
    """The reply's seal does not hold (its length, its key or its tag): the relay changed
    it, or it was sealed to another key. Its attestation is doubted (chutes.py)."""


# ---- ML-KEM-768 from the cryptography already pinned ----------------------------------

_available = None


def mlkem_available():
    """Whether this cryptography can make and use ML-KEM-768 keys: an older one, or one
    built against an OpenSSL older than 3.5 (a distribution's), cannot. Asked once a
    process."""
    global _available
    if _available is None:
        try:
            from cryptography.exceptions import UnsupportedAlgorithm
        except ImportError:
            _available = False
            return False
        try:
            from cryptography.hazmat.primitives.asymmetric import mlkem

            key = mlkem.MLKEM768PrivateKey.generate()
            secret, ciphertext = key.public_key().encapsulate()
            _available = key.decapsulate(ciphertext) == secret
        except (ImportError, AttributeError, UnsupportedAlgorithm):
            _available = False
    return _available


def mlkem_refusal():
    """Why Chutes' end-to-end encryption cannot be used on this computer, or None."""
    return None if mlkem_available() else MLKEM_MISSING


def _refuse_unless_available():
    if not mlkem_available():
        raise SealError(MLKEM_MISSING)


def _b64(text, what):
    """Base64 (the standard alphabet, padded), strictly: whatever else it is is not
    read."""
    if not isinstance(text, str) or not text.isascii():
        raise SealError(f"{what} is not base64")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as e:
        raise SealError(f"{what} is not base64") from e


def public_key(e2e_pubkey_b64):
    """An instance's ML-KEM-768 key from the base64 Chutes lists, exactly 1184 bytes and
    one FIPS 203 accepts, or SealError."""
    _refuse_unless_available()
    from cryptography.hazmat.primitives.asymmetric import mlkem

    raw = _b64(e2e_pubkey_b64, "the instance's key")
    if len(raw) != PUBLIC_KEY_BYTES:
        raise SealError(f"the instance's key is {len(raw)} bytes, not an ML-KEM-768 "
                        f"key's {PUBLIC_KEY_BYTES}")
    try:
        return mlkem.MLKEM768PublicKey.from_public_bytes(raw)
    except ValueError as e:
        raise SealError("the instance's key is not a valid ML-KEM-768 key") from e


def derive_key(shared_secret, ciphertext, info):
    """HKDF-SHA256 of the shared secret, the ML-KEM ciphertext's first 16 bytes as its
    salt, 32 bytes."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    if len(shared_secret) != SHARED_SECRET_BYTES or len(ciphertext) != CIPHERTEXT_BYTES:
        raise SealError("an ML-KEM secret or ciphertext of the wrong length")
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=ciphertext[:SALT_BYTES],
                info=info).derive(shared_secret)


# ---- a request ------------------------------------------------------------------------

@dataclass
class SealedRequest:
    """A request sealed to one instance, and the key of ours its reply comes back sealed
    to: made for this request alone, never kept past it."""

    blob: bytes
    response_key: object = field(repr=False)


def seal_request(e2e_pubkey_b64, body):
    """`body` (the OpenAI JSON) sealed to the instance whose key Chutes lists as
    `e2e_pubkey_b64`, with a fresh response key of ours in it. Refuses (SealError) a key
    that is not one, and a body that is not a JSON object."""
    _refuse_unless_available()
    from cryptography.hazmat.primitives.asymmetric import mlkem
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    if not isinstance(body, Mapping) or not body:
        raise SealError("the request is not a JSON object; nothing was sealed")
    instance = public_key(e2e_pubkey_b64)
    response_key = mlkem.MLKEM768PrivateKey.generate()
    plain = dict(body)
    plain[RESPONSE_KEY_FIELD] = base64.b64encode(
        response_key.public_key().public_bytes_raw()).decode()
    try:
        text = json.dumps(plain, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as e:
        raise SealError("the request cannot be written as JSON; nothing was sealed") from e
    secret, ciphertext = instance.encapsulate()
    if len(ciphertext) != CIPHERTEXT_BYTES:
        raise SealError("ML-KEM-768 gave a ciphertext of the wrong length")
    key = derive_key(secret, ciphertext, INFO_REQUEST)
    nonce = os.urandom(NONCE_BYTES)
    sealed = ChaCha20Poly1305(key).encrypt(nonce, gzip.compress(text, mtime=0), None)
    return SealedRequest(blob=ciphertext + nonce + sealed, response_key=response_key)


# ---- a reply sent whole (the protocol's; this app streams every request) ---------------

def _open(key, nonce, sealed):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    try:
        return ChaCha20Poly1305(key).decrypt(nonce, sealed, None)
    except InvalidTag as e:
        raise SealBroken("the reply's seal does not hold: it was changed, or sealed to "
                         "another key") from e


def _decapsulate(response_key, ciphertext):
    try:
        return response_key.decapsulate(ciphertext)
    except ValueError as e:
        raise SealBroken("the reply's ML-KEM ciphertext is not one") from e


def gunzip(data, most=MAX_DECODED_BYTES):
    """One gzip member, inflated a piece at a time and refused past `most` bytes; one
    cut short, or with anything after it, is not whole."""
    inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    out = bytearray()
    pending = data
    try:
        while not inflater.eof:
            piece = inflater.decompress(pending, _INFLATE_PIECE)
            out += piece
            if len(out) > most:
                raise SealError(f"the sealed reply opens to more than "
                                f"{most // (1024 * 1024)} MB; not read")
            left = inflater.unconsumed_tail
            if not piece and (not left or left == pending):
                break           # the input ran out before the gzip ended
            pending = left
    except zlib.error as e:
        raise SealError("the sealed reply's compressed text could not be read") from e
    if not inflater.eof or inflater.unused_data:
        raise SealError("the sealed reply's compressed text was not whole")
    return bytes(out)


def open_reply(blob, response_key):
    """A reply sent whole, opened with our response key: the model server's JSON, or
    SealBroken when the seal does not hold and SealError when what it holds cannot be
    read."""
    _refuse_unless_available()
    if len(blob) < CIPHERTEXT_BYTES + NONCE_BYTES + TAG_BYTES:
        raise SealBroken(f"the sealed reply is {len(blob)} bytes, too short to be one")
    ciphertext = blob[:CIPHERTEXT_BYTES]
    nonce = blob[CIPHERTEXT_BYTES:CIPHERTEXT_BYTES + NONCE_BYTES]
    secret = _decapsulate(response_key, ciphertext)
    text = gunzip(_open(derive_key(secret, ciphertext, INFO_RESPONSE), nonce,
                        blob[CIPHERTEXT_BYTES + NONCE_BYTES:]))
    try:
        reply = safejson.loads(text)
    except safejson.NestedTooDeep as e:
        raise SealError("the sealed reply's JSON is nested too deeply to read") from e
    except ValueError as e:
        raise SealError("the sealed reply is not JSON") from e
    if not isinstance(reply, dict):
        raise SealError("the sealed reply is not a JSON object")
    return reply


# ---- a streamed reply (how every request is answered) ----------------------------------
#
# Each piece of a stream is sealed on its own, with no associated data; but its nonce is
# a counter (live, Nov 15 2026, two models on two instances, 52 and 54 pieces: four zero
# bytes, then the piece's number from 0, eight bytes little-endian), and a piece's nonce
# is part of what opens it. So holding each piece to its number (`stream_nonce`) makes a
# piece dropped, repeated or put out of its place a refusal: without it, all three
# opened and read as the model's text. The end is held by the decrypted `[DONE]` and a
# finish reason. The counter is what Chutes' closed library does, not a documented
# promise: should it change, every stream is refused, never read wrongly.

def stream_nonce(index):
    """The nonce Chutes' stream seals its piece number `index` (from 0) under."""
    return b"\x00" * 4 + index.to_bytes(8, "little")


class StreamOpener:
    """A Chutes stream's SSE lines (text, one each), opened: `feed` returns the model
    server's own chunks (OpenAI's stream JSON) from the sealed pieces, in order. Only
    sealed text counts:

    - an unsealed `{"usage": ...}` and nothing else is Chutes' word, kept apart
      (`plain_usage`) for a cost estimate when the sealed usage is missing;
    - an unsealed `{"e2e_error": ...}` is said as Chutes' relay's, unauthenticated, and
      ends the stream;
    - an unsealed `[DONE]` is ignored: only the sealed one ends it;
    - any other unsealed event is refused. Chutes' own client passed any event naming
      `usage` on as it came, so `{"usage": {}, "choices": [...]}` from the relay read as
      the model's text.

    `finish` then requires the sealed `[DONE]` and a finish reason."""

    def __init__(self, response_key):
        _refuse_unless_available()
        self._response_key = response_key
        self._key = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._line = ""
        self.done = False
        self.finish_reason = None
        # The sealed usage (the model server's own), and Chutes' unsealed copy.
        self.usage = {}
        self.plain_usage = {}
        self.pieces = 0

    def feed(self, line):
        """One SSE line from Chutes: the model server's chunks it opens to."""
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        if not line.startswith("data:"):
            return []           # a blank line between events, a comment, a keep-alive
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            return []           # Chutes' own end of stream: not the sealed one
        try:
            event = safejson.loads(data)
        except ValueError as e:
            raise SealError("Chutes' stream carried an event that is not JSON; "
                            "refused") from e
        if not isinstance(event, dict) or len(event) != 1:
            raise SealError("Chutes' stream carried an unsealed event that is not its "
                            "own; refused")
        name, value = next(iter(event.items()))
        if name == "e2e_init":
            return self._init(value)
        if name == "e2e":
            return self._piece(value)
        if name == "usage":
            if not isinstance(value, dict):
                raise SealError("Chutes' unsealed usage is not an object; refused")
            self.plain_usage = value
            return []
        if name == "e2e_error":
            said = unsealed_words(value if isinstance(value, str) else json.dumps(value))
            raise PrivacyError("Chutes' relay said (unauthenticated): "
                               + (said or "an error, with no words"))
        raise SealError(f"Chutes' stream carried an unsealed {unsealed_words(name, 40)!r} "
                        f"event; refused")

    def _init(self, value):
        if self._key is not None or self.pieces:
            raise SealError("Chutes' stream began its encryption twice; refused")
        ciphertext = _b64(value, "the stream's key exchange")
        if len(ciphertext) != CIPHERTEXT_BYTES:
            raise SealBroken("the stream's key exchange is not an ML-KEM-768 ciphertext")
        self._key = derive_key(_decapsulate(self._response_key, ciphertext), ciphertext,
                               INFO_STREAM)
        return []

    def _piece(self, value):
        if self._key is None:
            raise SealError("Chutes' stream sent a sealed piece before its key exchange; "
                            "refused")
        raw = _b64(value, "a sealed piece of the stream")
        if len(raw) < NONCE_BYTES + TAG_BYTES:
            raise SealBroken("a sealed piece of the stream is too short to be one")
        if raw[:NONCE_BYTES] != stream_nonce(self.pieces):
            raise SealBroken("a piece of the stream is not the one that comes next: "
                             "dropped, repeated or put out of its place on the way")
        text = self._decoder.decode(_open(self._key, raw[:NONCE_BYTES], raw[NONCE_BYTES:]))
        self.pieces += 1
        if self.done:
            # The model server's SSE ends its [DONE] with a blank line, sealed as a
            # piece of its own (live): nothing else may follow.
            if text.strip():
                raise SealError("Chutes' stream went on after the model's end; refused")
            return []
        return self._lines(text)

    def _lines(self, text):
        """The model server's own SSE text, split into its lines across pieces."""
        chunks = []
        *ended, self._line = (self._line + text).replace("\r\n", "\n").replace(
            "\r", "\n").split("\n")
        if len(self._line) > MAX_LINE_CHARS:
            raise SealError("the sealed stream sent a line too long to read")
        for line in ended:
            chunk = self._sealed_line(line)
            if chunk is not None:
                chunks.append(chunk)
        return chunks

    def _sealed_line(self, line):
        if not line.startswith("data:"):
            return None
        data = line[len("data:"):].strip()
        if self.done:
            if data:
                raise SealError("the sealed stream went on after its end; refused")
            return None
        if data == "[DONE]":
            self.done = True
            return None
        try:
            chunk = safejson.loads(data)
        except ValueError as e:
            raise SealError("the sealed stream carried a chunk that is not JSON") from e
        if not isinstance(chunk, dict):
            raise SealError("the sealed stream carried a chunk that is not an object")
        if chunk.get("error"):
            error = chunk["error"]
            message = error.get("message") if isinstance(error, dict) else error
            raise PrivacyError(f"the model's server reported an error: "
                               f"{unsealed_words(str(message))}")
        if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if isinstance(choice, dict) and isinstance(choice.get("finish_reason"), str):
                self.finish_reason = choice["finish_reason"]
        return chunk

    def finish(self):
        """The stream has ended: refused unless the sealed text said so."""
        if self._line.strip():
            self._lines("\n")
        if not (self.done and self.finish_reason):
            raise SealError("the encrypted reply ended before the model said it was done")


# ---- the key binding --------------------------------------------------------------------

def binding(nonce_hex, e2e_pubkey_b64):
    """What an instance's quote holds in its report data's first half for our nonce and
    its key: sha256 of the two strings joined, UTF-8, as written. The nonce is lowercase
    hex; the key is the base64 exactly as Chutes lists it, never decoded and written
    again."""
    return hashlib.sha256((nonce_hex + e2e_pubkey_b64).encode()).digest()


def key_bound(report_data, nonce_hex, e2e_pubkey_b64):
    """Whether a quote's report data binds this key to this nonce."""
    if len(report_data) != 64 or not isinstance(e2e_pubkey_b64, str):
        return False
    return hmac.compare_digest(report_data[:32], binding(nonce_hex, e2e_pubkey_b64))


# ---- measurements -------------------------------------------------------------------------

@dataclass(frozen=True)
class MeasurementSet:
    """One VM image and hardware Chutes publishes: its MRTD and RTMR0-3."""

    name: str
    version: str
    mrtd: bytes
    rtmrs: tuple


def _register(value):
    return bytes.fromhex(value) if isinstance(value, str) and _HEX96.fullmatch(value) \
        else None


def published_measurements(listing):
    """The sets in Chutes' list, in either shape it serves: flat (`mrtd`, `rtmr0`-`rtmr3`:
    `/servers/tdx/measurements`) or the runtime registers apart (`runtime_rtmrs`). A set
    any part of which is not a register of 48 bytes in hex is left out. SealError when
    nothing in it can be read."""
    if not isinstance(listing, list):
        raise SealError("Chutes' published measurements are not a list")
    sets = []
    for entry in listing:
        if not isinstance(entry, dict):
            continue
        runtime = entry.get("runtime_rtmrs")
        if isinstance(runtime, dict):
            values = [runtime.get(f"RTMR{i}") for i in range(4)]
        else:
            values = [entry.get(f"rtmr{i}") for i in range(4)]
        mrtd = _register(entry.get("mrtd"))
        rtmrs = [_register(value) for value in values]
        if mrtd is None or any(value is None for value in rtmrs):
            continue
        sets.append(MeasurementSet(name=unsealed_words(str(entry.get("name") or ""), 80),
                                   version=unsealed_words(str(entry.get("version") or ""),
                                                          20),
                                   mrtd=mrtd, rtmrs=tuple(rtmrs)))
    if not sets:
        raise SealError("Chutes' published measurements hold no set that can be read")
    return sets


def measured(mrtd, rtmrs, sets):
    """The published set a quote's MRTD and RTMR0-3 are, all five, or None."""
    return next((s for s in sets if s.mrtd == mrtd and s.rtmrs == tuple(rtmrs)), None)


# ---- the models ---------------------------------------------------------------------------

_CHUTE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def is_chute_id(value):
    """A chute's or an instance's id as Chutes writes them (a UUID, lowercase): nothing
    else goes into a path or a header."""
    return isinstance(value, str) and _CHUTE_ID.fullmatch(value) is not None


def _price(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


def confidential_models(listing):
    """The models of Chutes' listing that run in a TEE, `confidential_compute: true` and
    nothing less: the only ones its end-to-end encryption is sent to."""
    return {model_id: entry for model_id, entry in listed_models(listing).items()
            if entry["confidential_compute"] is True}


def listed_models(listing):
    """Every model of Chutes' listing (`llm.chutes.ai/v1/models`), keyed by its id, with
    its `chute_id` and whether Chutes lists it as confidential compute (true, and nothing
    else, counts): its price per million tokens (`unit: per_million_tokens`), context,
    output limit, what it reads and whether it reasons. A model without a chute id is
    not listed. How private each is is not the listing's to say: its id decides
    (services.chutes_tee)."""
    items = listing.get("data") if isinstance(listing, dict) else None
    entries = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        model_id, chute_id = item.get("id"), item.get("chute_id")
        if not (isinstance(model_id, str) and model_id.strip() and is_chute_id(chute_id)):
            continue
        entry = {"id": model_id, "name": model_id, "chute_id": chute_id,
                 "confidential_compute": item.get("confidential_compute") is True}
        if isinstance(item.get("owned_by"), str):
            entry["owned_by"] = item["owned_by"]
        for field_name, source in (("context_length", "context_length"),
                                   ("max_output_tokens", "max_output_length")):
            value = item.get(source)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                entry[field_name] = value
        inputs, outputs = item.get("input_modalities"), item.get("output_modalities")
        if isinstance(inputs, list) or isinstance(outputs, list):
            entry["architecture"] = {
                "input_modalities": [m for m in inputs or [] if isinstance(m, str)],
                "output_modalities": [m for m in outputs or [] if isinstance(m, str)]}
        features = item.get("supported_features")
        if isinstance(features, list):
            entry["capabilities"] = {"reasoning": "reasoning" in features}
        pricing = item.get("pricing")
        if isinstance(pricing, dict):
            prompt, completion = _price(pricing.get("prompt")), _price(
                pricing.get("completion"))
            if prompt is not None and completion is not None:
                entry["pricing"] = {"prompt": prompt, "completion": completion,
                                    "unit": "per_million_tokens"}
                cached = _price(pricing.get("input_cache_read"))
                if cached is not None:
                    entry["pricing"]["input_cache_read"] = cached
        entries[model_id] = entry
    return entries
