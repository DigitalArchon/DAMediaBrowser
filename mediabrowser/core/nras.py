# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""NVIDIA's verdict on a TEE/ model's GPUs, with NVIDIA's signature checked.

A TEE/ model's attestation carries NVIDIA GPU evidence, made with our nonce. private_ai.py
sends it to NVIDIA's Remote Attestation Service (NRAS), which answers with signed
Entity Attestation Tokens (JWTs, ES384):

  [["JWT", <overall token>], {"GPU-0": <token>, "GPU-1": <token>, ...}]

The overall token carries `x-nvidia-overall-att-result`, `eat_nonce`, and in `submods` a
SHA-256 digest of each GPU's token. This module checks, before anything is believed:

  - every token's ES384 signature, against NVIDIA's published keys
    (nras.attestation.nvidia.com/.well-known/jwks.json, fetched over TLS from NVIDIA);
  - the issuer, and that each token is within its nbf/exp window;
  - that `eat_nonce` is our nonce, in every token - the evidence was made for this
    request, not replayed from another;
  - that the GPU tokens are exactly those the overall token lists, digest for digest;
  - the verdicts: the overall result, and for each GPU its measurements (`measres`),
    secure boot, debug disabled and the report's own nonce match.

Measured live (TEE/glm-5.3-flash, Sept 2026): eight H100 tokens, every digest matching,
keys named nv-eat-kid-prod-<date>-<uuid> and rotated daily. A key missing from the
cached key set is looked for once more before the token is refused.

When fresh keys cannot be fetched, the ones fetched before are used, however old: a
signature checked against NVIDIA's keys of yesterday beats one not checked. With none
at all (KeysUnavailable) the signatures go unchecked, but the answer still came over
TLS from NVIDIA's own service, so everything else above is checked all the same - the
nonce, the time, the digests, each GPU's verdict - and only then is the verdict shown
as unchecked. Evidence replayed with an old nonce, or a GPU failing, is refused either
way. From DA Vibe Manager's copy of this module (Nov 2026), by way of DA
Assistant's, with netguard's Session for requests; private_ai.py holds the time
and the nonce once more itself (check_claims).
"""

import base64
import hashlib
import json
import threading
import time

from . import netguard
from .dcap import since_boot

JWKS_URL = "https://nras.attestation.nvidia.com/.well-known/jwks.json"
ISSUER = "https://nras.attestation.nvidia.com"
TIMEOUT_SECONDS = 30
KEYS_TTL = 3600
LEEWAY_SECONDS = 300


class NrasError(Exception):
    """NVIDIA's answer is not a signed, current verdict for this request: refuse."""


class KeysUnavailable(Exception):
    """NVIDIA's signing keys could not be fetched: the verdict is unchecked."""


def _b64(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


_keys = {"at": 0.0, "keys": {}}
_keys_lock = threading.Lock()


def _jwks(session, refresh=False):
    with _keys_lock:
        if not refresh and _keys["keys"] and since_boot() - _keys["at"] < KEYS_TTL:
            return _keys["keys"]
    try:
        response = session.get(JWKS_URL, timeout=TIMEOUT_SECONDS)
        body = response.json() if response.status_code == 200 else None
    except (netguard.RequestError, ValueError) as e:
        body, error = None, f"NVIDIA's signing keys could not be fetched ({e})"
    else:
        error = "NVIDIA's signing keys could not be read"
    if not isinstance(body, dict) or not isinstance(body.get("keys"), list):
        with _keys_lock:
            if _keys["keys"] and not refresh:
                return _keys["keys"]    # older than KEYS_TTL, but NVIDIA's: better than none
        raise KeysUnavailable(error)
    keys = {k["kid"]: k for k in body["keys"] if isinstance(k, dict) and k.get("kid")}
    with _keys_lock:
        _keys.update(at=since_boot(), keys=keys)
    return keys


def _public_key(jwk):
    from cryptography.hazmat.primitives.asymmetric import ec
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-384":
        raise NrasError(f"NVIDIA's key {jwk.get('kid')} is not a P-384 key")
    return ec.EllipticCurvePublicNumbers(int.from_bytes(_b64(jwk["x"]), "big"),
                                         int.from_bytes(_b64(jwk["y"]), "big"),
                                         ec.SECP384R1()).public_key()


def check_claims(claims, nonce, now):
    """Raise NrasError unless a token's claims put it within its nbf/exp window, give or
    take LEEWAY_SECONDS, and say it was made for `nonce`. Its signature is not looked
    at: private_ai.py holds an answer to this much even when NVIDIA's keys are out of
    reach."""
    if not (claims.get("nbf", 0) - LEEWAY_SECONDS <= now
            <= claims.get("exp", 0) + LEEWAY_SECONDS):
        raise NrasError("a token is outside its validity window")
    if claims.get("eat_nonce") != nonce:
        raise NrasError("NVIDIA's verdict was not made for this request's nonce")


def verify_token(token, session, nonce, now, signed=True):
    """The claims of one NRAS token, once its signature, issuer, time and nonce hold
    (`signed` False, when NVIDIA's keys cannot be had: all but its signature)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    try:
        header_part, claims_part, signature_part = token.split(".")
        header = json.loads(_b64(header_part))
        claims = json.loads(_b64(claims_part))
        signature = _b64(signature_part)
    except (ValueError, AttributeError):
        raise NrasError("NVIDIA's answer held a malformed token")
    if header.get("alg") != "ES384" or len(signature) != 96:
        raise NrasError(f"a token is signed with {header.get('alg')}, not ES384")
    if not signed:
        return _claims_hold(claims, nonce, now)
    keys = _jwks(session)
    if header.get("kid") not in keys:
        keys = _jwks(session, refresh=True)
    jwk = keys.get(header.get("kid"))
    if jwk is None:
        raise NrasError(f"a token names a key NVIDIA does not publish ({header.get('kid')})")
    der = encode_dss_signature(int.from_bytes(signature[:48], "big"),
                               int.from_bytes(signature[48:], "big"))
    try:
        _public_key(jwk).verify(der, f"{header_part}.{claims_part}".encode("ascii"),
                                ec.ECDSA(hashes.SHA384()))
    except InvalidSignature:
        raise NrasError("a token's signature does not verify against NVIDIA's key")
    return _claims_hold(claims, nonce, now)


def _claims_hold(claims, nonce, now):
    if claims.get("iss") != ISSUER:
        raise NrasError(f"a token was issued by {claims.get('iss')}, not NVIDIA's service")
    check_claims(claims, nonce, now)
    return claims


def verify(answer, nonce, session=None, now=None):
    """Check NRAS's answer. Returns the number of GPUs vouched for, or raises NrasError
    (refuse), KeysUnavailable (unchecked), or ImportError (no cryptography).

    KeysUnavailable only once everything but the signatures has held: see the module's
    docstring."""
    import cryptography  # noqa: F401  - ImportError here, before anything is claimed
    session = session or netguard.Session()
    now = now if now is not None else time.time()
    try:
        return _verify(answer, nonce, session, now)
    except KeysUnavailable:
        _verify(answer, nonce, session, now, signed=False)     # NrasError, if it fails
        raise


def _verify(answer, nonce, session, now, signed=True):
    try:
        label, overall = answer[0]
        tokens = answer[1] if len(answer) > 1 else {}
    except (TypeError, ValueError, IndexError, KeyError):
        raise NrasError("NVIDIA's answer was not in the expected shape")
    if label != "JWT" or not isinstance(tokens, dict):
        raise NrasError("NVIDIA's answer was not in the expected shape")

    claims = verify_token(overall, session, nonce, now, signed)
    if claims.get("x-nvidia-overall-att-result") is not True:
        raise NrasError("NVIDIA rejected the GPU evidence")
    listed = claims.get("submods")
    if not isinstance(listed, dict) or set(listed) != set(tokens) or not tokens:
        raise NrasError("the GPU tokens are not the ones NVIDIA's verdict lists")
    for gpu, token in tokens.items():
        try:
            digest = listed[gpu][1][1]
        except (TypeError, IndexError, KeyError):
            raise NrasError(f"NVIDIA's verdict gives no digest for {gpu}")
        if hashlib.sha256(token.encode("ascii")).hexdigest() != digest:
            raise NrasError(f"{gpu}'s token is not the one NVIDIA's verdict lists")
        gpu_claims = verify_token(token, session, nonce, now, signed)
        failed = [name for name, ok in (
            ("measurements", gpu_claims.get("measres") == "success"),
            ("secure boot", gpu_claims.get("secboot") is True),
            ("debug disabled", gpu_claims.get("dbgstat") == "disabled"),
            ("report nonce",
             gpu_claims.get("x-nvidia-gpu-attestation-report-nonce-match") is True),
        ) if not ok]
        if failed:
            raise NrasError(f"NVIDIA found {gpu} failing: {', '.join(failed)}")
    return len(tokens)
