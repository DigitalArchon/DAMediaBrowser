# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Privacy Mode's verifiers: TEE attestation, reply signatures, Intel's and
NVIDIA's checks, and the sealed enclave's clock.

Ported from DA Assistant's tests/test_privacy.py (Nov 2026), whose verifiers
these are line for line; its session and chat tests are its own, and this
app's are in tests/test_private_ai.py.

The TEE checks run against a fake nano-gpt that attests, signs and streams
like the real one (the report data layout, NRAS's JWT list and EIP-191
signatures as measured live, Sept 2026). Intel's and NVIDIA's verification
is checked against a real attestation captured with its collateral
(tests/data/tee_attestation.json), at the moment it was captured, and
against forgeries of it; the certificate rules Intel's own chain keeps,
against synthetic chains under a root of their own.
"""

import base64
import hashlib
import json
import os
import struct
import time
import unittest
from datetime import UTC, datetime
from unittest import mock

from mediabrowser.core import dcap, netguard, nras, sealed
from mediabrowser.core import private_ai as privacy
from mediabrowser.core.ethsig import (
    address_of,
    keccak256,
    personal_hash,
    recover,
    recover_personal,
    sign,
)
from mediabrowser.core.private_ai import PrivacyError, PrivacySession, TeeClient

KEY = 0xB25C7DB31FEED9122727BF0939DC769A96564B2DE4C4726D035B36ECF1E5B364
ADDRESS = "0x5ce9454909639D2D17A3F753ce7d93fa0b9aB12E"
API_URL = "https://nano-gpt.com/api/v1/chat/completions"
TEXT = "TEE/glm-5.3-flash"
VISION = "TEE/qwen3.8-27b"


def jwt(claims):
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return f"{part({'alg': 'ES384'})}.{part(claims)}.c2ln"


def fake_quote(report_data):
    """A version 4 TDX quote with every field in place and nothing signed: enough for
    parse_quote, and for tests that stand in for Intel's verification."""
    header = (struct.pack("<HHI", 4, dcap.ECDSA_P256, dcap.TDX_TEE_TYPE) + bytes(4)
              + dcap.INTEL_QE_VENDOR_ID + bytes(20))
    body = bytes(520) + report_data
    inner = struct.pack("<HI", dcap.PCK_CHAIN, 0)
    cert = bytes(dcap.QE_REPORT_LEN) + bytes(64) + struct.pack("<H", 0) + inner
    signature_data = bytes(128) + struct.pack("<HI", dcap.QE_REPORT_DATA, len(cert)) + cert
    return (header + body + struct.pack("<I", len(signature_data)) + signature_data).hex()


class FakeResponse:
    def __init__(self, status=200, body=None, lines=(), content=None, headers=None):
        self.status_code = status
        self._body = body
        self._lines = list(lines)
        self.text = json.dumps(body) if body is not None else ""
        self.content = content if content is not None else self.text.encode()
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body

    def iter_lines(self):
        yield from self._lines

    def close(self):
        self.closed = True


def sse_lines(reply, response_id="chatcmpl-42"):
    """A streamed reply as requests' iter_lines yields it: a chunk per word, then DONE."""
    words = reply.split(" ")
    pieces = [words[0]] + [" " + word for word in words[1:]]
    return [b"data: " + json.dumps({"id": response_id,
                                    "choices": [{"delta": {"content": piece}}]}).encode()
            for piece in pieces] + [b"data: [DONE]"]


UP_TO_DATE = dcap.Result(status="UpToDate", platform="UpToDate", qe="UpToDate",
                         notes=["Intel's certificate chain verifies"])


def e2ee_public_key(private_key):
    """`private_key`'s uncompressed secp256k1 public key, as RedPill's keyset lists the
    e2ee key whose address is the attested signing address."""
    from mediabrowser.core import ethsig
    point = ethsig._mul(private_key, ethsig._G)
    return "04" + point[0].to_bytes(32, "big").hex() + point[1].to_bytes(32, "big").hex()


def canonical(value):
    """JSON as RedPill digests its keysets (RFC 8785, which for these is sorted keys and
    no spaces)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


class FakeNanoGpt:
    """A requests.Session standing in for nano-gpt: TEE attestation and signatures, NRAS,
    and the chat endpoint. Records every request so a test can say none was made.

    Intel's and NVIDIA's verification are stood in for by `intel` (a dcap.Result to
    return, or an exception to raise) and `nvidia` (a GPU count, or an exception); the
    real ones are tested against real evidence in RealEvidence below.

    `aci` is what `report=aci` answers (Nov 10 2026, gateway_report): RedPill's
    gateway's own report binding its keyset - `gateway_keys` its e2ee keys, held until
    `not_after` - to our nonce ("bound") or to another ("unbound"), a 400 as for a model
    only Chutes or Tinfoil serves ("400"), nano-gpt's rate limit ("429"), or, None, the
    plain report, as before nano-gpt offered the gateway's. Every attestation asked for
    is in `reports` (its params, headers and timeout), and the headers of every lookup
    of a reply's record in `lookups`."""

    def __init__(self, nonce_bound=True, gpu=True, evidence=True, signer=KEY,
                 attestation_status=200, signature_status=200, reply="Hello there",
                 intel=UP_TO_DATE, nvidia=8, nras_status=200, attestation_extra=None,
                 aci=None, gateway_keys=(KEY,), not_after=4_000_000_000):
        self.aci, self.gateway_keys, self.not_after = aci, gateway_keys, not_after
        self.reports = []
        self.lookups = []
        self.nonce_bound = nonce_bound
        # More of the attestation's body: what RedPill's gateway adds, say.
        self.attestation_extra = attestation_extra or {}
        self.gpu = gpu
        self.evidence = evidence
        self.signer = signer
        self.attestation_status = attestation_status
        self.signature_status = signature_status
        self.reply = reply
        self.intel = intel
        self.nvidia = nvidia if gpu else nras.NrasError("NVIDIA rejected the GPU evidence")
        self.nras_status = nras_status
        self.gets = []
        self.posts = []

    def intel_verify(self, raw, session):
        if isinstance(self.intel, BaseException):
            raise self.intel
        return self.intel

    def nvidia_verify(self, answer, nonce, session):
        if isinstance(self.nvidia, BaseException):
            raise self.nvidia
        return self.nvidia

    def tee_client(self, url, key, model):
        return TeeClient(url, key, model, session=self, intel=self.intel_verify,
                         nvidia=self.nvidia_verify)

    @property
    def keyset(self):
        """RedPill's workload keyset, as the gateway's report binds it."""
        return {"subject": None, "not_after": self.not_after,
                "receipt_signing_keys": [{"key_id": "dstack-kms-receipt-ed25519-v1",
                                          "algo": "ed25519", "public_key": "00" * 32}],
                "e2ee_public_keys": [{"key_id": f"dstack-kms-e2ee-v{i + 1}",
                                      "algo": "secp256k1-aes-256-gcm-hkdf-sha256",
                                      "public_key": e2ee_public_key(key)}
                                     for i, key in enumerate(self.gateway_keys)],
                "tls_public_keys": []}

    def gateway_report(self, nonce):
        """What `report=aci` answers, as `aci` has it (nano-gpt, Nov 10 2026)."""
        if self.aci == "400":
            return FakeResponse(400, {"error": "report=aci is only available for models "
                                               "served through RedPill"})
        if self.aci == "429":
            return FakeResponse(429, {"error": "Rate limit exceeded"})
        keyset = self.keyset
        digest = "sha256:" + hashlib.sha256(canonical(keyset)).hexdigest()
        bound_to = nonce if self.aci == "bound" else "ab" * 32
        statement = (f'{{"keyset_digest":"{digest}","nonce":"{bound_to}",'
                     f'"purpose":"aci.report_data.v1"}}')
        report = hashlib.sha256(statement.encode()).digest() + bytes(32)
        return FakeResponse(200, {
            "api_version": "aci/1", "workload_keyset_digest": digest,
            "attestation": {"tee_type": "tdx", "workload_keyset": keyset,
                            "report_data": report[:32].hex(),
                            "evidence": {"quote": fake_quote(report)}}})

    def asked(self, url, params, headers, timeout):
        """Note an attestation asked for."""
        self.gets.append(url)
        self.reports.append({"params": dict(params or {}), "headers": dict(headers or {}),
                             "timeout": timeout})

    def plain_reports(self):
        """The model's own attestations asked for, not the gateway's."""
        return [r for r in self.reports if r["params"].get("report") != "aci"]

    def get(self, url, params=None, headers=None, timeout=None):
        if "/tee/attestation" in url:
            self.asked(url, params, headers, timeout)
            if params.get("report") == "aci" and self.aci is not None:
                return self.gateway_report(params["nonce"])
            if self.attestation_status != 200:
                return FakeResponse(self.attestation_status, {"error": "no"})
            nonce = params["nonce"]
            address = address_of(KEY).lower()
            report = bytes.fromhex(address[2:]) + bytes(12) + (
                bytes.fromhex(nonce) if self.nonce_bound else bytes(32))
            return FakeResponse(200, {
                "signing_address": address,
                "attestation": {"report_data": report.hex()},
                "intel_quote": fake_quote(report),
                "nvidia_payload": json.dumps(
                    {"nonce": nonce,
                     "evidence_list": [{"evidence": "e"}] if self.evidence else []}),
                **self.attestation_extra,
            })
        self.gets.append(url)
        if "/tee/signature/" in url:
            self.lookups.append(dict(headers or {}))
            if self.signature_status != 200:
                return FakeResponse(self.signature_status, {"error": "none kept"})
            text = "aa:bb"
            signature = sign(personal_hash(text.encode()), self.signer, 12345).hex()
            return FakeResponse(200, {"text": text, "signature": signature})
        return FakeResponse(404, {})

    def post(self, url, json=None, headers=None, data=None, stream=False, timeout=None):
        self.posts.append({"url": url, "data": data, "json": json})
        if url.startswith(privacy.NRAS_URL):
            # As NVIDIA's tokens are: for the nonce it was sent, and current.
            now = int(time.time())
            return FakeResponse(self.nras_status,
                                [["JWT", jwt({"x-nvidia-overall-att-result": self.gpu,
                                              "eat_nonce": (json or {}).get("nonce"),
                                              "nbf": now - 60, "exp": now + 3600})],
                                 {}])
        if url == API_URL:
            return FakeResponse(200, None, sse_lines(self.reply))
        return FakeResponse(404, {})

    @property
    def chat_posts(self):
        return [p for p in self.posts if p["url"] == API_URL]


def session_for(server, model=TEXT):
    return PrivacySession(API_URL, "k", model, tee_factory=server.tee_client)


# ---- signatures ----------------------------------------------------------------------

class Signatures(unittest.TestCase):
    def test_keccak_and_addresses_match_known_vectors(self):
        self.assertEqual(keccak256(b"").hex(),
                         "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(keccak256(b"abc").hex(),
                         "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")
        self.assertEqual(address_of(1), "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf")
        self.assertEqual(address_of(KEY), ADDRESS)

    def test_a_signature_recovers_its_signer_and_only_its_signer(self):
        digest = personal_hash(b"req:resp")
        signature = sign(digest, KEY, 987654321)
        self.assertEqual(recover(digest, signature), ADDRESS)
        self.assertEqual(recover_personal("req:resp", signature.hex()), ADDRESS)
        self.assertNotEqual(recover(personal_hash(b"tampered"), signature), ADDRESS)


# ---- TEE attestation -----------------------------------------------------------------

class TeeAttestation(unittest.TestCase):
    def client(self, **server):
        return FakeNanoGpt(**server).tee_client(API_URL, "k", TEXT)

    def test_a_bound_quote_intel_and_nvidia_vouch_for_is_fully_attested(self):
        attestation = self.client().attest()
        self.assertEqual(attestation.level, "full")
        self.assertTrue(attestation.gpu_ok)
        self.assertEqual(attestation.gpus, 8)
        self.assertEqual(attestation.tcb_status, "UpToDate")
        self.assertEqual(attestation.signing_address, address_of(KEY).lower())
        self.assertIn("binds the signing key to our nonce", attestation.detail)
        self.assertIn("vouches for 8 GPUs", attestation.detail)
        self.assertEqual(attestation.headline, "TEE attested")

    def test_no_gpu_evidence_is_partial_and_says_so(self):
        attestation = self.client(evidence=False).attest()
        self.assertEqual(attestation.level, "partial")
        self.assertIsNone(attestation.gpu_ok)
        self.assertIn("no GPU evidence", attestation.headline)

    def test_an_unbound_nonce_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "did not bind"):
            self.client(nonce_bound=False).attest()

    def test_a_quote_intel_does_not_vouch_for_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "failed verification: .*revoked"):
            self.client(intel=dcap.DcapError("the PCK certificate has been revoked")).attest()

    def test_a_tcb_that_is_not_current_is_partial_and_named(self):
        stale = dcap.Result(status="OutOfDate", platform="OutOfDate", qe="UpToDate")
        attestation = self.client(intel=stale).attest()
        self.assertEqual(attestation.level, "partial")
        self.assertIn("Intel TCB OutOfDate", attestation.headline)

    def test_intel_out_of_reach_is_partial_not_a_pass(self):
        attestation = self.client(intel=dcap.Result(collateral=False)).attest()
        self.assertEqual(attestation.level, "partial")
        self.assertIn("Intel TCB unchecked", attestation.headline)

    def test_without_cryptography_nothing_is_attested_and_it_says_what_to_install(self):
        """It used to pass with "signatures unchecked": the quote's bytes, unchecked,
        named the key, and every reply that key signed then read "signed by attested
        enclave"."""
        for intel, nvidia in ((ImportError("cryptography"), ImportError("cryptography")),
                              (UP_TO_DATE, ImportError("cryptography"))):
            with self.assertRaisesRegex(PrivacyError, "cannot be checked without Python's "
                                                      "cryptography package "
                                                      r"\(python3-cryptography\)"):
                self.client(intel=intel, nvidia=nvidia).attest()

    def test_a_verdict_nvidia_did_not_sign_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "GPU attestation failed: .*nonce"):
            self.client(nvidia=nras.NrasError("not made for this request's nonce")).attest()
        with self.assertRaisesRegex(PrivacyError, "NVIDIA rejected"):
            self.client(gpu=False).attest()
        with self.assertRaisesRegex(PrivacyError, "NVIDIA rejected"):
            self.client(nras_status=400).attest()

    def test_nvidia_out_of_reach_is_partial(self):
        self.assertIn("GPUs unverified", self.client(nras_status=503).attest().headline)
        keys = self.client(nvidia=nras.KeysUnavailable("keys could not be fetched"))
        self.assertIn("NVIDIA signature unchecked", keys.attest().headline)

    def test_nvidia_is_asked_about_the_gpu_evidence_with_our_nonce(self):
        """nano-gpt's copy of the GPU evidence names a nonce of its own, and NVIDIA was
        sent that one: evidence replayed with the nonce it was made for came back
        vouched for, for that nonce. Now NVIDIA checks it against ours."""
        address = address_of(KEY).lower()
        ours = []

        def respond(nonce):
            ours.append(nonce)
            report = bytes.fromhex(address[2:]) + bytes(12) + bytes.fromhex(nonce)
            return 200, {"signing_address": address, "intel_quote": fake_quote(report),
                         "nvidia_payload": json.dumps({
                             "nonce": "0d" * 32, "arch": "HOPPER",
                             "evidence_list": [{"evidence": "e"}]})}

        server = ShapedNanoGpt(respond)
        server.tee_client(API_URL, "k", TEXT).attest()
        self.assertEqual(server.nras_nonces, ours)
        sent = next(p["json"] for p in server.posts if p["url"] == privacy.NRAS_URL)
        self.assertEqual((sent["arch"], sent["evidence_list"]), ("HOPPER",
                                                                 [{"evidence": "e"}]))

    def test_a_model_offering_no_attestation_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "offers no attestation"):
            self.client(attestation_status=400).attest()

    def test_reply_signatures(self):
        client = self.client()
        client.attest()
        self.assertEqual(client.check_reply("chatcmpl-1")[0], privacy.SIGNED)

        other = self.client(signer=1)
        other.attest()
        self.assertEqual(other.check_reply("chatcmpl-1")[0], privacy.MISMATCH)

        none_kept = self.client(signature_status=500)
        none_kept.attest()
        self.assertEqual(none_kept.check_reply("chatcmpl-1")[0], privacy.UNSIGNED)
        self.assertEqual(client.check_reply(None)[0], privacy.UNSIGNED)


# ---- real evidence -------------------------------------------------------------------

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                       "tee_attestation.json")


def _has_cryptography():
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False
    return True


def key_usage(cert_sign):
    """keyUsage as Intel's certificates have it: a CA's key signs certificates and
    CRLs, any other's signs data."""
    from cryptography import x509
    return x509.KeyUsage(digital_signature=not cert_sign, content_commitment=not cert_sign,
                         key_encipherment=False, data_encipherment=False,
                         key_agreement=False, key_cert_sign=cert_sign, crl_sign=cert_sign,
                         encipher_only=False, decipher_only=False)


def synthetic_cert(name, key, issuer=None, ca=None, path_length=None, cert_sign=None):
    """A certificate named `name` for `key`, signed by `issuer` - (its certificate, its
    key) - or by itself; with basicConstraints if `ca` is given, and keyUsage if
    `cert_sign` is."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, name)])
    issuer_cert, issuer_key = issuer or (None, key)
    builder = (x509.CertificateBuilder().subject_name(subject)
               .issuer_name(issuer_cert.subject if issuer_cert else subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(datetime(2018, 1, 1, tzinfo=UTC))
               .not_valid_after(datetime(2049, 1, 1, tzinfo=UTC)))
    if ca is not None:
        builder = builder.add_extension(x509.BasicConstraints(ca=ca, path_length=path_length),
                                        critical=True)
    if cert_sign is not None:
        builder = builder.add_extension(key_usage(cert_sign), critical=True)
    return builder.sign(issuer_key, hashes.SHA256())


class FixtureSession:
    """Serves one real attestation, captured live from TEE/glm-5.3-flash with Intel's
    collateral and NVIDIA's answer and key, exactly as they came (tests/data)."""

    def __init__(self, fixture, intel_status=200, jwks=None):
        self.fixture = fixture
        self.intel_status = intel_status
        self.jwks = fixture["jwks"] if jwks is None else jwks

    def get(self, url, params=None, headers=None, timeout=None):
        if "/tee/attestation" in url:
            return FakeResponse(200, {
                "signing_address": self.fixture["signing_address"],
                "intel_quote": self.fixture["intel_quote"],
                "nvidia_payload": json.dumps({"nonce": self.fixture["nonce"],
                                              "evidence_list": [{"evidence": "e"}]})})
        if url == nras.JWKS_URL:
            return FakeResponse(200, self.jwks)
        for item in self.fixture["collateral"]:
            if item["url"] == url and (item["params"] or None) == (params or None):
                if self.intel_status != 200:
                    return FakeResponse(self.intel_status, {})
                return FakeResponse(item["status"], None,
                                    content=base64.b64decode(item["body"]),
                                    headers=item["headers"])
        return FakeResponse(404, {})

    def post(self, url, json=None, headers=None, timeout=None, **_kw):
        if url == privacy.NRAS_URL:
            return FakeResponse(200, self.fixture["nras_answer"])
        return FakeResponse(404, {})


class KeysOutOfReach(FakeNanoGpt):
    """nano-gpt as FakeNanoGpt has it, a quote binding our nonce and all, but NVIDIA's
    service answering with the verdict captured in tests/data - made for the nonce of
    that request - and its signing keys out of reach."""

    def __init__(self, fixture, **kwargs):
        super().__init__(**kwargs)
        self.fixture = fixture

    def get(self, url, params=None, headers=None, timeout=None):
        if url == nras.JWKS_URL:
            self.gets.append(url)
            return FakeResponse(503, {})
        return super().get(url, params, headers, timeout)

    def post(self, url, json=None, **kwargs):
        if url == privacy.NRAS_URL:
            self.posts.append({"url": url, "json": json})
            return FakeResponse(200, self.fixture["nras_answer"])
        return super().post(url, json=json, **kwargs)


@unittest.skipUnless(_has_cryptography(), "the cryptography package is not installed")
class RealEvidence(unittest.TestCase):
    """Intel's and NVIDIA's real signatures, checked at the moment they were captured."""

    def setUp(self):
        with open(FIXTURE) as f:
            self.fixture = json.load(f)
        self.now = self.fixture["now"]
        self.when = datetime.fromtimestamp(self.now, UTC)
        self.quote = bytes.fromhex(self.fixture["intel_quote"])
        self.session = FixtureSession(self.fixture)
        dcap._cache.clear()
        nras._keys.update(at=0.0, keys={})

    def tearDown(self):
        dcap._cache.clear()
        nras._keys.update(at=0.0, keys={})

    def intel(self, raw=None, session=None):
        return dcap.verify(raw or self.quote, session or self.session, now=self.when)

    def nvidia(self, answer=None, nonce=None, session=None, now=None):
        return nras.verify(answer or self.fixture["nras_answer"],
                           nonce or self.fixture["nonce"], session or self.session,
                           now=now or self.now)

    # Intel

    def test_the_quote_verifies_up_to_intels_root_and_is_current(self):
        result = self.intel()
        self.assertTrue(result.collateral)
        self.assertEqual(result.status, "UpToDate")
        self.assertEqual((result.platform, result.module, result.qe),
                         ("UpToDate", "UpToDate", "UpToDate"))
        self.assertEqual(result.fmspc, "20a06f000000")

    def test_a_revoked_tcb_is_refused_whatever_else_is_named(self):
        """Only the worst status was compared with Revoked, and one this app does not
        list ranks above it: a platform Intel had revoked passed as partial beside it."""
        for platform, module, qe in (("Revoked", "TDRelaunchAdvised", "UpToDate"),
                                     ("UpToDate", "Revoked", "SomethingNew"),
                                     ("SomethingNew", "UpToDate", "Revoked")):
            dcap._cache.clear()
            with mock.patch.object(dcap, "platform_level",
                                   return_value={"tcbStatus": platform}), \
                    mock.patch.object(dcap, "module_status", return_value=module), \
                    mock.patch.object(dcap, "qe_status", return_value=qe), \
                    self.assertRaisesRegex(dcap.DcapError, "revoked this platform"):
                self.intel()
        dcap._cache.clear()
        with mock.patch.object(dcap, "module_status", return_value="TDRelaunchAdvised"):
            result = self.intel()
        self.assertEqual(result.status, "TDRelaunchAdvised")    # partial, and named

    def test_a_changed_report_breaks_the_quote_signature(self):
        tampered = bytearray(self.quote)
        tampered[dcap.HEADER_LEN + 520] ^= 1            # the report data: our nonce
        with self.assertRaisesRegex(dcap.DcapError, "quote's signature"):
            self.intel(bytes(tampered))

    def test_a_debug_td_is_refused(self):
        debug = bytearray(self.quote)
        debug[dcap.HEADER_LEN + 120] |= 0x01            # TDATTRIBUTES.DEBUG
        with self.assertRaisesRegex(dcap.DcapError, "debug TD"):
            self.intel(bytes(debug))

    def test_a_chain_to_any_other_root_is_refused(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "Intel SGX Root CA"),
                          x509.NameAttribute(x509.NameOID.ORGANIZATION_NAME,
                                             "Intel Corporation"),
                          x509.NameAttribute(x509.NameOID.LOCALITY_NAME, "Santa Clara"),
                          x509.NameAttribute(x509.NameOID.STATE_OR_PROVINCE_NAME, "CA"),
                          x509.NameAttribute(x509.NameOID.COUNTRY_NAME, "US")])
        # Intel's root in every respect but its key, extensions included.
        impostor = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                    .public_key(key.public_key()).serial_number(1)
                    .not_valid_before(datetime(2018, 1, 1, tzinfo=UTC))
                    .not_valid_after(datetime(2049, 1, 1, tzinfo=UTC))
                    .add_extension(x509.BasicConstraints(ca=True, path_length=1),
                                   critical=True)
                    .add_extension(key_usage(cert_sign=True), critical=True)
                    .sign(key, hashes.SHA256()))
        original = dcap.intel_root
        dcap.intel_root = lambda: impostor
        try:
            with self.assertRaisesRegex(dcap.DcapError, "signature does not verify"):
                self.intel()
        finally:
            dcap.intel_root = original

    def test_a_revocation_list_intel_did_not_sign_is_refused(self):
        forged = json.loads(json.dumps(self.fixture))
        crl = bytearray(base64.b64decode(forged["collateral"][0]["body"]))
        crl[-5] ^= 1                                    # inside the CRL's signature
        forged["collateral"][0]["body"] = base64.b64encode(bytes(crl)).decode()
        with self.assertRaisesRegex(dcap.DcapError, "CRL is not signed"):
            self.intel(session=FixtureSession(forged))

    def test_tcb_information_intel_did_not_sign_is_refused(self):
        forged = json.loads(json.dumps(self.fixture))
        body = base64.b64decode(forged["collateral"][2]["body"]).decode()
        body = body.replace('"tcbEvaluationDataNumber":', '"tcbEvaluationDataNumber":1', 1)
        forged["collateral"][2]["body"] = base64.b64encode(body.encode()).decode()
        with self.assertRaisesRegex(dcap.DcapError, "signature on its tcbInfo"):
            self.intel(session=FixtureSession(forged))

    def test_tcb_information_signed_with_a_pck_chain_is_refused(self):
        """A PCK certificate chains to Intel's root just as the TCB signing one does,
        and its key lives in the very platform whose TCB is being judged."""
        from urllib.parse import quote as url_quote
        pck_chain = dcap.parse_quote(self.quote).pck_chain_pem.decode()
        for index, header, key in ((2, "TCB-Info-Issuer-Chain", "tcbInfo"),
                                   (3, "SGX-Enclave-Identity-Issuer-Chain",
                                    "enclaveIdentity")):
            dcap._cache.clear()
            forged = json.loads(json.dumps(self.fixture))
            forged["collateral"][index]["headers"][header] = url_quote(pck_chain)
            with self.assertRaisesRegex(dcap.DcapError, f"Intel's {key} is signed by "
                                                        f"Intel SGX PCK Certificate, not "
                                                        f"by its TCB signing certificate"):
                self.intel(session=FixtureSession(forged))

    def test_collateral_out_of_reach_is_reported_not_passed(self):
        result = self.intel(session=FixtureSession(self.fixture, intel_status=503))
        self.assertFalse(result.collateral)
        self.assertEqual(result.status, "")
        self.assertIn("could not be fetched", result.notes[-1])

    def test_the_quoting_enclave_is_intels_whether_or_not_its_collateral_can_be_had(self):
        """It is what makes the attestation key the QE's, not any enclave's the platform
        certified. It was checked only against Intel's QE identity, fetched with the
        collateral: out of reach, or made so, a quote such an enclave signed passed as
        partial."""
        quote = dcap.parse_quote(self.quote)
        dcap.qe_is_intels(dcap.INTEL_TD_QE, quote.qe_report)       # the real one is
        other = bytearray(quote.qe_report)
        other[128:160] = b"\x11" * 32                               # another's MRSIGNER
        with self.assertRaisesRegex(dcap.DcapError, "not signed by Intel"):
            dcap.qe_is_intels(dcap.INTEL_TD_QE, bytes(other))
        # The real quote, held to an identity its QE does not have, with Intel's
        # collateral out of reach: refused, not partial.
        impostor = dict(dcap.INTEL_TD_QE, mrsigner="11" * 32)
        with mock.patch.object(dcap, "INTEL_TD_QE", impostor), \
                self.assertRaisesRegex(dcap.DcapError, "not signed by Intel"):
            self.intel(session=FixtureSession(self.fixture, intel_status=503))

    def test_the_td_qe_identity_pinned_is_the_one_intel_signed(self):
        item = next(c for c in self.fixture["collateral"]
                    if c["url"] == dcap.QE_IDENTITY_URL)
        signed = json.loads(base64.b64decode(item["body"]))["enclaveIdentity"]
        self.assertEqual(dcap.INTEL_TD_QE, {key: signed[key] for key in dcap.INTEL_TD_QE})

    # NVIDIA

    def test_nvidias_verdict_verifies_for_every_gpu(self):
        self.assertEqual(self.nvidia(), 8)

    def test_a_verdict_for_another_nonce_is_refused(self):
        with self.assertRaisesRegex(nras.NrasError, "nonce"):
            self.nvidia(nonce="00" * 32)

    def test_an_old_verdict_is_refused(self):
        with self.assertRaisesRegex(nras.NrasError, "validity window"):
            self.nvidia(now=self.now + 86400)

    def test_edited_claims_break_nvidias_signature(self):
        answer = json.loads(json.dumps(self.fixture["nras_answer"]))
        header, claims, signature = answer[0][1].split(".")
        decoded = json.loads(base64.urlsafe_b64decode(claims + "=="))
        decoded["jti"] = "replayed"
        claims = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")
        answer[0][1] = f"{header}.{claims}.{signature}"
        with self.assertRaisesRegex(nras.NrasError, "signature does not verify"):
            self.nvidia(answer)

    def test_a_gpu_token_swapped_for_another_is_refused(self):
        answer = json.loads(json.dumps(self.fixture["nras_answer"]))
        gpus = sorted(answer[1])
        answer[1][gpus[0]] = answer[1][gpus[1]]
        with self.assertRaisesRegex(nras.NrasError, "not the one"):
            self.nvidia(answer)

    def test_a_key_nvidia_does_not_publish_is_refused(self):
        session = FixtureSession(self.fixture, jwks={"keys": []})
        with self.assertRaisesRegex(nras.NrasError, "does not publish"):
            self.nvidia(session=session)

    def attest_with_nvidias_keys_out_of_reach(self, nonce, now):
        """The attestation, made with `nonce`, at `now`, of a TEE model whose GPU
        evidence NVIDIA answered for with the captured verdict."""
        server = KeysOutOfReach(self.fixture)
        client = TeeClient(API_URL, "k", TEXT, session=server, intel=server.intel_verify)
        try:
            with mock.patch.object(privacy.secrets, "token_hex", return_value=nonce), \
                    mock.patch("time.time", return_value=now):
                return client.attest()
        finally:
            self.assertIn(nras.JWKS_URL, server.gets, "NVIDIA's keys were not looked for")

    def test_with_nvidias_keys_out_of_reach_a_verdict_for_another_nonce_is_refused(self):
        """Without its keys NVIDIA's signatures cannot be checked, and only the overall
        verdict used to be read: one made for an earlier request passed as "unchecked".
        The answer comes over TLS from NVIDIA itself, so its nonce and its time are
        held to even then."""
        with self.assertRaisesRegex(PrivacyError, "GPU attestation failed: NVIDIA's "
                                                  "verdict was not made for this "
                                                  "request's nonce"):
            self.attest_with_nvidias_keys_out_of_reach("0d" * 32, self.now)

    def test_with_nvidias_keys_out_of_reach_an_old_verdict_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "GPU attestation failed: a token is "
                                                  "outside its validity window"):
            self.attest_with_nvidias_keys_out_of_reach(self.fixture["nonce"],
                                                       self.now + 86400)

    def test_without_nvidias_keys_each_gpus_verdict_is_still_checked(self):
        """Only the overall verdict, its nonce and its time were read without NVIDIA's
        keys: a GPU whose token said its measurements failed, or a GPU token the
        overall one does not list, passed as "unchecked"."""
        no_keys = FixtureSession(self.fixture, jwks={"no": "keys"})
        with self.assertRaises(nras.KeysUnavailable):
            self.nvidia(session=no_keys)                    # as it came: unchecked
        label, overall = self.fixture["nras_answer"][0]
        tokens = dict(self.fixture["nras_answer"][1])
        gpu = sorted(tokens)[0]
        header, claims, signature = tokens[gpu].split(".")
        failing = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
        failing["measres"] = "fail"
        part = base64.urlsafe_b64encode(json.dumps(failing).encode()).decode().rstrip("=")
        tokens[gpu] = f"{header}.{part}.{signature}"
        # Listed by the overall verdict, digest and all: only the signatures, which go
        # unchecked here, would have told it from NVIDIA's.
        o_header, o_claims, o_signature = overall.split(".")
        verdict = json.loads(base64.urlsafe_b64decode(o_claims + "=" * (-len(o_claims) % 4)))
        verdict["submods"][gpu][1][1] = hashlib.sha256(tokens[gpu].encode()).hexdigest()
        o_part = base64.urlsafe_b64encode(json.dumps(verdict).encode()).decode().rstrip("=")
        forged = [[label, f"{o_header}.{o_part}.{o_signature}"], tokens]
        with self.assertRaisesRegex(nras.NrasError, f"{gpu} failing: measurements"):
            self.nvidia(answer=forged, session=no_keys)
        unlisted = [[label, overall], dict(tokens)]
        with self.assertRaisesRegex(nras.NrasError, "not the one NVIDIA's verdict lists"):
            self.nvidia(answer=unlisted, session=no_keys)

    def test_keys_fetched_before_are_used_when_fresh_ones_cannot_be_had(self):
        """Older than an hour, but NVIDIA's, fetched over TLS from its own service: a
        signature checked against them beats one not checked at all."""
        self.assertEqual(self.nvidia(), 8)                  # fetched now, and kept
        nras._keys["at"] = dcap.since_boot() - nras.KEYS_TTL - 1
        no_keys = FixtureSession(self.fixture, jwks={"no": "keys"})
        self.assertEqual(self.nvidia(session=no_keys), 8)
        label, overall = self.fixture["nras_answer"][0]
        forged = overall[:-4] + ("AAAA" if not overall.endswith("AAAA") else "BBBB")
        with self.assertRaisesRegex(nras.NrasError, "signature does not verify"):
            self.nvidia(answer=[[label, forged], self.fixture["nras_answer"][1]],
                        session=no_keys)

    def test_with_nvidias_keys_out_of_reach_a_current_verdict_is_partial(self):
        attestation = self.attest_with_nvidias_keys_out_of_reach(self.fixture["nonce"],
                                                                 self.now)
        self.assertEqual(attestation.level, "partial")
        self.assertIn("NVIDIA signature unchecked", attestation.headline)
        self.assertIn("NVIDIA's verdict unchecked", attestation.summary)

    # where their requests go

    def test_without_a_session_given_each_uses_netguards(self):
        """This app has no requests: what they fetch goes through netguard's
        Session, which follows a redirect only where the request could have
        been sent in the first place, never from https to plain http."""
        import inspect
        for module in (privacy, dcap, nras):
            self.assertNotIn("import requests", inspect.getsource(module))
        with mock.patch.object(netguard, "Session", return_value=self.session) as made:
            self.assertEqual(dcap.verify(self.quote, now=self.when).status, "UpToDate")
            self.assertEqual(nras.verify(self.fixture["nras_answer"], self.fixture["nonce"],
                                         now=self.now), 8)
            self.assertIs(TeeClient(API_URL, "k", TEXT).session, self.session)
        self.assertEqual(made.call_count, 3)

    # together

    def test_the_whole_attestation_is_full(self):
        client = TeeClient(API_URL, "k", self.fixture["model"], session=self.session,
                           intel=lambda raw, s: dcap.verify(raw, s, now=self.when),
                           nvidia=lambda a, n, s: nras.verify(a, n, s, now=self.now))
        original = privacy.secrets.token_hex
        privacy.secrets.token_hex = lambda _n: self.fixture["nonce"]
        try:
            attestation = client.attest()
        finally:
            privacy.secrets.token_hex = original
        self.assertEqual(attestation.headline, "TEE attested")
        self.assertEqual(attestation.level, "full")
        self.assertEqual((attestation.gpus, attestation.tcb_status), (8, "UpToDate"))
        self.assertEqual(attestation.signing_address, self.fixture["signing_address"])


@unittest.skipUnless(_has_cryptography(), "the cryptography package is not installed")
class IntelsChainRules(unittest.TestCase):
    """verify_chain held a certificate to its issuer's name, signature and dates, and
    no more: any certificate Intel's root vouched for - the TCB signing one, a PCK
    certificate whose key a compromised platform gave up - could issue certificates of
    its own that passed as Intel's. And Intel's TCB information and QE identity were
    believed signed by any certificate chaining to the root. Synthetic chains, under a
    root of their own; the real one is RealEvidence's."""

    NOW = datetime(2026, 9, 26, tzinfo=UTC)

    @classmethod
    def setUpClass(cls):
        from cryptography.hazmat.primitives.asymmetric import ec
        cls.keys = {name: ec.generate_private_key(ec.SECP256R1())
                    for name in ("root", "ca", "leaf")}

    def root(self, path_length=1):
        return synthetic_cert("Test Root CA", self.keys["root"], ca=True,
                              path_length=path_length, cert_sign=True)

    def under(self, issuer, issuer_key, name, key, **extensions):
        return synthetic_cert(name, self.keys[key], (issuer, self.keys[issuer_key]),
                              **extensions)

    def test_a_chain_of_authorities_allowed_to_sign_verifies(self):
        root = self.root()
        ca = self.under(root, "root", "Test CA", "ca", ca=True, path_length=0,
                        cert_sign=True)
        leaf = self.under(ca, "ca", "Test leaf", "leaf", ca=False, cert_sign=False)
        dcap.verify_chain([leaf, ca], root, self.NOW)

    def test_a_chain_through_a_certificate_that_is_no_ca_is_refused(self):
        root = self.root()
        signing = self.under(root, "root", "Intel SGX TCB Signing", "ca", ca=False,
                             cert_sign=False)
        leaf = self.under(signing, "ca", "Intel SGX PCK Certificate", "leaf", ca=False,
                          cert_sign=False)
        with self.assertRaisesRegex(dcap.DcapError, "CN=Intel SGX TCB Signing signed a "
                                                    "certificate, and is not a "
                                                    "certificate authority"):
            dcap.verify_chain([leaf, signing], root, self.NOW)
        # Nor one that does not say what it is.
        bare = self.under(root, "root", "Bare", "ca")
        leaf = self.under(bare, "ca", "Test leaf", "leaf", ca=False)
        with self.assertRaisesRegex(dcap.DcapError, "Bare signed a certificate, and is "
                                                    "not a certificate authority"):
            dcap.verify_chain([leaf, bare], root, self.NOW)

    def test_an_authority_beyond_its_path_length_is_refused(self):
        root = self.root(path_length=0)
        ca = self.under(root, "root", "Test CA", "ca", ca=True, path_length=0,
                        cert_sign=True)
        leaf = self.under(ca, "ca", "Test leaf", "leaf", ca=False, cert_sign=False)
        with self.assertRaisesRegex(dcap.DcapError, "Test Root CA may have 0 certificate "
                                                    "authorities below it, and the chain "
                                                    "puts 1 there"):
            dcap.verify_chain([leaf, ca], root, self.NOW)

    def test_an_authority_whose_key_is_not_for_certificates_is_refused(self):
        root = self.root()
        ca = self.under(root, "root", "Test CA", "ca", ca=True, path_length=0,
                        cert_sign=False)
        leaf = self.under(ca, "ca", "Test leaf", "leaf", ca=False, cert_sign=False)
        with self.assertRaisesRegex(dcap.DcapError, "Test CA signed a certificate with "
                                                    "a key not meant for that"):
            dcap.verify_chain([leaf, ca], root, self.NOW)

    def signed_tcb_info(self, signer_name):
        """Intel's TCB info as its service sends it, signed by a certificate named
        `signer_name` that the root issued."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        from cryptography.hazmat.primitives.serialization import Encoding
        root = self.root()
        signer = self.under(root, "root", signer_name, "leaf", ca=False, cert_sign=False)
        raw = json.dumps({"id": "TDX", "fmspc": "20a06f000000"}, separators=(",", ":"))
        r, s = decode_dss_signature(self.keys["leaf"].sign(raw.encode(),
                                                           ec.ECDSA(hashes.SHA256())))
        signature = (r.to_bytes(32, "big") + s.to_bytes(32, "big")).hex()
        content = f'{{"tcbInfo":{raw},"signature":"{signature}"}}'.encode()
        chain = (signer.public_bytes(Encoding.PEM) + root.public_bytes(Encoding.PEM))
        return dcap._signed_json(content, "tcbInfo", chain.decode(), root, self.NOW)

    def test_tcb_info_signed_by_any_but_the_tcb_signing_certificate_is_refused(self):
        info, signer = self.signed_tcb_info("Intel SGX TCB Signing")
        self.assertEqual(info["fmspc"], "20a06f000000")
        for name in ("Intel SGX PCK Certificate", "Intel SGX TCB Signing 2"):
            with self.assertRaisesRegex(dcap.DcapError, f"Intel's tcbInfo is signed by "
                                                        f"{name}, not by its TCB signing "
                                                        f"certificate"):
                self.signed_tcb_info(name)


@unittest.skipUnless(hasattr(time, "CLOCK_BOOTTIME"), "no CLOCK_BOOTTIME here")
class ClocksThatCountSleep(unittest.TestCase):
    """The end-to-end encrypted enclave's five-minute attestation, Intel's collateral
    and NVIDIA's keys were aged by time.monotonic, which stops while the machine is
    suspended: a laptop that slept for hours took them all for as fresh as when it
    went to sleep. Here the boot clock moves on while the monotonic one stands still,
    as across a suspend."""

    def setUp(self):
        self.boot = 1000.0
        real = time.clock_gettime

        def clock_gettime(clock):
            return self.boot if clock == time.CLOCK_BOOTTIME else real(clock)

        for patch in (mock.patch.object(time, "clock_gettime", clock_gettime),
                      mock.patch.object(time, "monotonic", return_value=50.0)):
            patch.start()
            self.addCleanup(patch.stop)

    def sleep(self, hours):
        self.boot += hours * 3600

    def test_the_boot_clock_is_read_and_the_monotonic_one_only_without_it(self):
        self.assertEqual(dcap.since_boot(), 1000.0)
        with mock.patch.object(dcap, "_BOOTTIME", None):
            self.assertEqual(dcap.since_boot(), 50.0)
        with mock.patch.object(time, "clock_gettime", side_effect=OSError):
            self.assertEqual(dcap.since_boot(), 50.0)

    def test_the_sealed_enclave_is_attested_again_after_a_sleep(self):
        verified = []
        enclave = sealed.Enclave(sealed.relay_url(API_URL),
                                 verifier=lambda relay, secret: verified.append(1) or 1)
        enclave.current()
        enclave.current()
        self.assertEqual(len(verified), 1)
        self.sleep(2)
        enclave.current()
        self.assertEqual(len(verified), 2)

    def test_intels_collateral_is_fetched_again_after_a_sleep(self):
        dcap._cache.clear()
        self.addCleanup(dcap._cache.clear)
        session = mock.Mock()
        session.get.return_value = FakeResponse(200, None, content=b"collateral")
        for _ in range(2):
            self.assertEqual(dcap._fetch(session, dcap.ROOT_CRL_URL)[0], b"collateral")
        self.assertEqual(session.get.call_count, 1)
        self.sleep(2)
        dcap._fetch(session, dcap.ROOT_CRL_URL)
        self.assertEqual(session.get.call_count, 2)

    def test_a_tee_attestation_is_made_again_after_a_sleep(self):
        server = FakeNanoGpt()
        session = session_for(server)
        session.ensure()
        session.ensure()
        self.sleep(1)
        session.ensure()
        self.assertEqual(len(server.plain_reports()), 2)

    def test_nvidias_keys_are_fetched_again_after_a_sleep(self):
        nras._keys.update(at=0.0, keys={})
        self.addCleanup(nras._keys.update, at=0.0, keys={})
        session = mock.Mock()
        session.get.return_value = FakeResponse(200, {"keys": [{"kid": "k1"}]})
        for _ in range(2):
            self.assertIn("k1", nras._jwks(session))
        self.assertEqual(session.get.call_count, 1)
        self.sleep(2)
        nras._jwks(session)
        self.assertEqual(session.get.call_count, 2)


# ---- the session ---------------------------------------------------------------------

class InstanceEvidence:
    """Chutes' per-instance attestation, built with a real key: each instance's
    certificate key is bound into its quote's report data, and signs a body carrying the
    nonce, the quote and the GPU evidence - the shape TEE/kimi-k3 answers with. The
    signed quote is under "quote", as live in Nov 2026; `quote_fields` names others
    (Sept 2026's "tdx_quote"), or none."""

    def __init__(self, count=2):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        self.keys, self.certs = [], []
        for i in range(count):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME,
                                                 "attestation-service")])
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                    .public_key(key.public_key()).serial_number(i + 1)
                    .not_valid_before(datetime(2026, 1, 1, tzinfo=UTC))
                    .not_valid_after(datetime(2027, 1, 1, tzinfo=UTC))
                    .sign(key, hashes.SHA256()))
            self.keys.append(key)
            self.certs.append(cert)
        self.gpu_nonce = "ab" * 32

    def body(self, nonce, sign_with=None, bind=True, signed_nonce=None, other_quote=False,
             failed=(), quote_fields=("quote",)):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        evidence = []
        for i, (key, cert) in enumerate(zip(self.keys, self.certs, strict=True)):
            spki = cert.public_key().public_bytes(Encoding.DER,
                                                  PublicFormat.SubjectPublicKeyInfo)
            bound = hashlib.sha256(spki).digest() if bind else bytes(32)
            quote = bytes.fromhex(fake_quote(bytes.fromhex(self.gpu_nonce) + bound))
            signed_quote = bytes.fromhex(fake_quote(bytes(64))) if other_quote else quote
            gpu = [{"arch": "HOPPER", "evidence": "e", "certificate": "c"}]
            signed_evidence = {field: base64.b64encode(signed_quote).decode()
                               for field in quote_fields}
            signed_evidence["nvtrust_evidence"] = json.dumps(gpu)
            attested = json.dumps({"nonce": signed_nonce or nonce,
                                   "evidence": signed_evidence}).encode()
            signer = sign_with or key
            signature = signer.sign(attested, padding.PKCS1v15(), hashes.SHA256())
            evidence.append({
                "instance_id": f"inst-{i}", "tee_type": "tdx",
                "quote": base64.b64encode(quote).decode(),
                "gpu_evidence": gpu,
                "certificate": base64.b64encode(cert.public_bytes(Encoding.DER)).decode(),
                "signature": base64.b64encode(signature).decode(),
                "attested_body": base64.b64encode(attested).decode()})
        return {"evidence": evidence, "failed_instance_ids": list(failed)}


class ShapedNanoGpt(FakeNanoGpt):
    """nano-gpt answering /tee/attestation with a given body, or an error; and
    `report=aci` as `aci` says (FakeNanoGpt), with a 400 when it says nothing, as for a
    model only Chutes or Tinfoil serves, or ("ignored") with that body too, as before
    nano-gpt offered the gateway's report."""

    def __init__(self, respond, **kwargs):
        super().__init__(**kwargs)
        self.respond = respond
        self.nras_nonces = []

    def get(self, url, params=None, headers=None, timeout=None):
        if "/tee/attestation" in url and (params.get("report") != "aci"
                                          or self.aci in (None, "ignored")):
            self.asked(url, params, headers, timeout)
            if params.get("report") == "aci" and self.aci is None:
                return FakeResponse(400, {"error": "report=aci is only available for "
                                                   "models served through RedPill"})
            status, body = self.respond(params["nonce"])
            return FakeResponse(status, body)
        return super().get(url, params, headers, timeout)

    def post(self, url, json=None, **kwargs):
        if url.startswith(privacy.NRAS_URL):
            self.nras_nonces.append(json.get("nonce"))
        return super().post(url, json=json, **kwargs)

    def nvidia_verify(self, answer, nonce, session):
        self.verified_nonces = getattr(self, "verified_nonces", []) + [nonce]
        return super().nvidia_verify(answer, nonce, session)


@unittest.skipUnless(_has_cryptography(), "the cryptography package is not installed")
class PerInstanceAttestation(unittest.TestCase):
    """TEE/kimi-k3 and the other Chutes models (live, Sept and Nov 2026)."""

    @classmethod
    def setUpClass(cls):
        cls.instances = InstanceEvidence()

    def attest(self, **shape):
        server = ShapedNanoGpt(lambda nonce: (200, self.instances.body(nonce, **shape)))
        client = server.tee_client(API_URL, "k", "TEE/kimi-k3")
        return client, server, client.attest()

    def test_every_instance_bound_and_signed_is_fully_attested(self):
        client, server, attestation = self.attest()
        self.assertEqual(attestation.level, "full")
        self.assertEqual(attestation.gpus, 16)                  # 8 per instance, faked
        self.assertIn("2 instances, each key bound to our nonce", attestation.summary)
        self.assertIn("signs no replies", attestation.detail)
        # NVIDIA's verdict is sought for the nonce the quote binds, as measured live.
        self.assertEqual(server.nras_nonces, [self.instances.gpu_nonce] * 2)
        self.assertEqual(server.verified_nonces, [self.instances.gpu_nonce] * 2)
        state, detail = client.check_reply("chatcmpl-1")
        self.assertEqual(state, privacy.UNSIGNED)
        self.assertIn("signs no replies", detail)

    def test_a_body_for_another_nonce_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "did not sign our nonce"):
            self.attest(signed_nonce="00" * 32)

    def test_a_signature_by_another_key_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "did not sign its evidence"):
            self.attest(sign_with=InstanceEvidence(1).keys[0])

    def test_a_key_the_quote_does_not_bind_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "quote does not bind"):
            self.attest(bind=False)

    def test_a_signed_body_for_another_quote_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "signed a different quote"):
            self.attest(other_quote=True)

    def test_the_quote_signed_under_its_earlier_name_still_counts(self):
        _client, _server, attestation = self.attest(quote_fields=("tdx_quote",))
        self.assertEqual(attestation.level, "full")

    def test_a_signed_body_with_no_quote_in_it_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "signed a different quote"):
            self.attest(quote_fields=())

    def test_any_instance_that_failed_to_attest_refuses_them_all(self):
        with self.assertRaisesRegex(PrivacyError, "1 of 3 .* could not be attested"):
            self.attest(failed=["inst-9"])


class AttestationShapesAndOutages(unittest.TestCase):
    def attest(self, status, body, model="TEE/x"):
        server = ShapedNanoGpt(lambda nonce: (status, body(nonce) if callable(body)
                                              else body))
        return server.tee_client(API_URL, "k", model).attest()

    def test_an_attestation_relayed_for_the_relays_own_nonce_is_refused(self):
        body = {"attestation_type": "chutes", "nonce": "f9" * 32,
                "all_attestations": [{"intel_quote": "", "gpu_evidence": []}]}
        with self.assertRaisesRegex(PrivacyError, "relayed by its provider \\(chutes\\), "
                                                  "was made for a nonce the relay chose"):
            self.attest(200, body)

    def test_nano_gpts_reason_is_given_and_tinfoil_models_point_to_their_twin(self):
        reason = ("Fresh nonce-bound attestation is not available for Tinfoil via this "
                  "endpoint.")
        with self.assertRaises(PrivacyError) as caught:
            self.attest(400, {"error": reason}, model="TEE/gemma4-31b")
        self.assertIn(reason, str(caught.exception))
        self.assertIn("private/ version", str(caught.exception))

    def test_a_provider_outage_says_so(self):
        with self.assertRaisesRegex(PrivacyError, "Unable to reach RedPill.*provider's "
                                                  "side.*tried again"):
            self.attest(502, {"error": "Unable to reach RedPill attestation service"})

    def test_a_provider_too_busy_to_attest_says_so(self):
        with self.assertRaisesRegex(PrivacyError, "HTTP 429: Unable to reach Chutes.*"
                                                  "provider's side.*tried again"):
            self.attest(429, {"error": "Unable to reach Chutes TEE evidence service"})

    def test_an_unknown_shape_names_what_it_held(self):
        with self.assertRaisesRegex(PrivacyError, "does not recognise .*holds novel"):
            self.attest(200, {"novel": 1})



# ---- the gateway's own report (report=aci, nano-gpt, Nov 10 2026) --------------------

# What a model's own attestation is when nano-gpt relays Chutes' for the relay's own
# nonce (live, Nov 10 2026: TEE/deepseek-v3.2).
RELAYED = {"attestation_type": "chutes", "nonce": "f9" * 32,
           "all_attestations": [{"intel_quote": "", "gpu_evidence": []}]}
TIMED_OUT = {"error": "RedPill attestation service took too long to respond. Please retry.",
             "code": "tee_attestation_upstream_timeout"}


@unittest.skipUnless(_has_cryptography(), "the cryptography package is not installed")
class GatewaysOwnReport(unittest.TestCase):
    """report=aci (nano-gpt, Nov 10 2026): RedPill's gateway's own report, whose quote
    binds the gateway's whole keyset - the keys that sign each reply's record among
    them - to a nonce of ours. Asked for beside the model's own report, each with its
    own nonce. Ported from DA Assistant's tests (Nov 10 2026), from SealedLore's."""

    @classmethod
    def setUpClass(cls):
        cls.instances = InstanceEvidence()

    def test_the_gateways_report_binds_the_signing_key_with_its_others(self):
        server = FakeNanoGpt(aci="bound", gateway_keys=(KEY, 9))
        client = server.tee_client(API_URL, "k", TEXT)
        attestation = client.attest()
        self.assertEqual(attestation.shortfalls, [privacy.GATEWAY_SHORTFALL])
        self.assertIn("the gateway's quote binds its keys, the signing key among them",
                      attestation.detail)
        self.assertIn("not that the model itself runs in an enclave", attestation.detail)
        self.assertEqual((attestation.gpus, attestation.tcb_status), (8, "UpToDate"),
                         "the model's own report is checked as ever")
        self.assertEqual(attestation.signing_address, address_of(KEY).lower())
        self.assertEqual(len(attestation.signers), 2)
        self.assertEqual(client.check_reply("chatcmpl-42")[0], privacy.SIGNED)
        server.signer = 9           # the gateway's other key
        state, detail = client.check_reply("chatcmpl-42")
        self.assertEqual(state, privacy.SIGNED, detail)
        self.assertIn(address_of(9), detail)
        server.signer = 1
        state, detail = client.check_reply("chatcmpl-42")
        self.assertEqual(state, privacy.MISMATCH)
        self.assertIn("not any key the attestation bound", detail)
        nonces = [r["params"]["nonce"] for r in server.reports]
        self.assertEqual(len(set(nonces)), 2, "a fresh nonce for each")
        self.assertTrue(all(len(n) == 64 and n == n.lower() for n in nonces))
        self.assertEqual(len([r for r in server.reports
                              if r["params"].get("report") == "aci"]), 1)
        self.assertTrue(all(r["timeout"] == privacy.ATTESTATION_TIMEOUT_SECONDS == 90
                            for r in server.reports), "Chutes' evidence can take a minute")

    def test_a_gateway_report_for_another_nonce_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "gateway did not bind its keys to our "
                                                  "nonce"):
            FakeNanoGpt(aci="unbound").tee_client(API_URL, "k", TEXT).attest()

    def test_its_keys_past_their_time_are_refused(self):
        with self.assertRaisesRegex(PrivacyError, "attested keys expired on 2023-11-14"):
            FakeNanoGpt(aci="bound", not_after=1_700_000_000).tee_client(
                API_URL, "k", TEXT).attest()

    def test_without_the_gateways_report_things_are_as_they_were(self):
        for server in (FakeNanoGpt(aci="400"), FakeNanoGpt(aci="429"), FakeNanoGpt()):
            attestation = server.tee_client(API_URL, "k", TEXT).attest()
            self.assertEqual((attestation.level, attestation.signers),
                             ("full", (address_of(KEY).lower(),)))

    def test_a_gateway_whose_keys_do_not_hold_the_signing_key_counts_for_nothing(self):
        attestation = FakeNanoGpt(aci="bound", gateway_keys=(7,)).tee_client(
            API_URL, "k", TEXT).attest()
        self.assertEqual(attestation.shortfalls, [])
        self.assertIn("the gateway's attested keys do not include the signing key",
                      attestation.detail)
        self.assertEqual(attestation.signers, (address_of(KEY).lower(),))

    def test_the_gateway_stands_in_for_a_model_whose_own_evidence_was_relayed(self):
        """Live, TEE/deepseek-v3.2: its own report is Chutes' for the relay's nonce,
        its replies RedPill's. Refused before."""
        server = ShapedNanoGpt(lambda nonce: (200, RELAYED), aci="bound")
        client = server.tee_client(API_URL, "k", "TEE/deepseek-v3.2")
        attestation = client.attest()
        self.assertEqual(attestation.shortfalls,
                         [privacy.GATEWAY_SHORTFALL, "no GPU evidence"])
        self.assertIn("relayed for a nonce not ours: the gateway that answers it is "
                      "attested instead", attestation.detail)
        self.assertEqual(attestation.signing_address, "")
        self.assertEqual(attestation.signers, (address_of(KEY).lower(),))
        self.assertEqual(client.check_reply("chatcmpl-42", receipt_id="rcpt-1")[0],
                         privacy.SIGNED)
        with self.assertRaisesRegex(PrivacyError, "nonce the relay chose"):
            ShapedNanoGpt(lambda nonce: (200, RELAYED), aci="400").tee_client(
                API_URL, "k", "TEE/deepseek-v3.2").attest()

    def test_a_slow_attestation_is_asked_again_with_a_new_nonce_then_the_gateway_stands_in(
            self):
        asked = []

        def once(nonce):
            asked.append(nonce)
            return (504, TIMED_OUT) if len(asked) == 1 else (200, RELAYED)

        ShapedNanoGpt(once, aci="bound").tee_client(API_URL, "k", "TEE/x").attest()
        self.assertEqual(len(asked), 2)
        self.assertNotEqual(asked[0], asked[1])
        always = ShapedNanoGpt(lambda nonce: (504, TIMED_OUT), aci="bound")
        attestation = always.tee_client(API_URL, "k", "TEE/qwen3.6-27b").attest()
        self.assertEqual(attestation.shortfalls,
                         [privacy.GATEWAY_SHORTFALL, "no GPU evidence"])
        self.assertIn("its own attestation timed out: the gateway that answers it is "
                      "attested instead", attestation.detail)
        self.assertEqual(len(always.plain_reports()), 2, "asked once more, no more")
        with self.assertRaisesRegex(PrivacyError, "HTTP 504.*provider's side"):
            ShapedNanoGpt(lambda nonce: (504, TIMED_OUT)).tee_client(
                API_URL, "k", "TEE/qwen3.6-27b").attest()

    def test_a_rate_limited_report_refuses_however_good_the_gateways(self):
        """Live (Nov 10 2026), nano-gpt rate-limited TEE/glm-5.2's own report, and
        SealedLore's rule attested it through the gateway: Chutes answers it first, and
        none of its instances would have been attested. Refused, as before, as a 429 is:
        on the provider's side."""
        server = ShapedNanoGpt(lambda nonce: (429, {"error": "Rate limit exceeded"}),
                               aci="bound")
        with self.assertRaisesRegex(PrivacyError, "HTTP 429: Rate limit exceeded.*"
                                                  "provider's side.*tried again"):
            server.tee_client(API_URL, "k", "TEE/glm-5.2").attest()
        self.assertEqual(len([r for r in server.reports
                              if r["params"].get("report") == "aci"]), 1,
                         "the gateway's report was had, and good")

    def test_the_gateway_stands_in_for_nothing_else(self):
        """Only a relayed report or a 504 asked again: a 400, a 502 or 503, a body
        holding nothing to check refuse as they did."""
        for status, body in ((400, {"error": "no attestation for this model"}),
                             (502, {"error": "Bad gateway"}), (503, {"error": "down"}),
                             (200, {"novel": 1}), (200, {"error": "unavailable"})):
            server = ShapedNanoGpt(lambda nonce, s=status, b=body: (s, b), aci="bound")
            with self.assertRaises(PrivacyError, msg=f"{status} {body}"):
                server.tee_client(API_URL, "k", "TEE/glm-5.2").attest()

    def test_a_chutes_model_with_the_gateway_checks_the_replies_redpill_answers(self):
        server = ShapedNanoGpt(lambda nonce: (200, self.instances.body(nonce)), aci="bound")
        client = server.tee_client(API_URL, "k", "TEE/glm-5.1")
        attestation = client.attest()
        self.assertEqual((attestation.instances, attestation.gpus), (2, 16))
        self.assertEqual(attestation.shortfalls, [privacy.GATEWAY_SHORTFALL],
                         "a request may be answered by RedPill's gateway")
        self.assertIn("the instances sign no replies", attestation.detail)
        self.assertEqual(client.check_reply("chatcmpl-42", receipt_id="rcpt-1")[0],
                         privacy.SIGNED)
        looked_up = len(server.lookups)
        state, detail = client.check_reply("chatcmpl-42")
        self.assertEqual(state, privacy.UNSIGNED)
        self.assertIn("signs no replies", detail)
        self.assertEqual(len(server.lookups), looked_up, "Chutes' reply is not looked up")
        alone = ShapedNanoGpt(lambda nonce: (200, self.instances.body(nonce)))
        client = alone.tee_client(API_URL, "k", "TEE/kimi-k3")
        self.assertEqual(client.attest().level, "full")
        self.assertEqual(client.check_reply("chatcmpl-42", receipt_id="rcpt-1")[0],
                         privacy.UNSIGNED)
        self.assertEqual(alone.lookups, [])

    def test_a_chutes_model_whose_gateway_could_not_be_asked_falls_short(self):
        for aci in ("429", "ignored"):
            server = ShapedNanoGpt(lambda nonce: (200, self.instances.body(nonce)), aci=aci)
            attestation = server.tee_client(API_URL, "k", "TEE/glm-5.1").attest()
            self.assertEqual(attestation.shortfalls, [privacy.GATEWAY_UNCHECKED], aci)

    def test_the_route_goes_with_the_lookup_and_not_with_the_attestation(self):
        server = FakeNanoGpt(aci="bound")
        client = server.tee_client(API_URL, "k", TEXT)
        client.attest()
        self.assertEqual(client.check_reply("chatcmpl-42", receipt_id="rcpt-7",
                                            route="r2")[0], privacy.SIGNED)
        client.attest()
        self.assertIn("/tee/signature/rcpt-7", [g for g in server.gets
                                                if "/tee/signature/" in g][-1])
        self.assertEqual(server.lookups[-1].get("X-TEE-Verification-Route"), "r2")
        self.assertFalse(any("X-TEE-Verification-Route" in r["headers"]
                             for r in server.reports))

    def test_strict_attests_again_only_after_a_reply_the_bound_keys_were_to_sign(self):
        chutes = privacy.Attestation(model="TEE/glm-5.1", kind=privacy.TEE,
                                     signers=("0xab",), instances=2)
        self.assertFalse(privacy._signs(chutes, None), "Chutes answered: nothing signed")
        self.assertTrue(privacy._signs(chutes, "rcpt-1"), "RedPill answered instead")
        gateway = privacy.Attestation(model="TEE/x", kind=privacy.TEE, signers=("0xab",))
        self.assertTrue(privacy._signs(gateway, None))
        self.assertFalse(privacy._signs(privacy.Attestation(model="TEE/kimi-k3",
                                                            kind=privacy.TEE,
                                                            instances=2), "rcpt-1"))

    def test_strict_refuses_a_model_attested_through_the_gateway(self):
        server = ShapedNanoGpt(lambda nonce: (200, RELAYED), aci="bound")
        strict = PrivacySession(API_URL, "k", "TEE/deepseek-v3.2",
                                tee_factory=server.tee_client, strict=True)
        with self.assertRaisesRegex(PrivacyError, "RedPill's gateway, not the model's host, "
                                                  "no GPU evidence.*choose a private/ "
                                                  "model.*nothing was sent"):
            strict.ensure()
        relaxed = PrivacySession(API_URL, "k", "TEE/deepseek-v3.2",
                                 tee_factory=server.tee_client)
        relaxed.ensure()
        self.assertIn(f"signing key {address_of(KEY).lower()}", relaxed.details())
