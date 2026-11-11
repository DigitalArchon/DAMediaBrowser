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
        if content is None:
            content = stream_bytes(self._lines) if self._lines else self.text.encode()
        self.content = content
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body

    def iter_lines(self):
        yield from self._lines

    def iter_content(self, chunk_size=1, decode_unicode=False):
        """The bytes, seven at a time: a line falls across pieces, as on the wire."""
        for at in range(0, len(self.content), 7):
            yield self.content[at:at + 7]

    def close(self):
        self.closed = True


def sse_lines(reply, response_id="chatcmpl-42"):
    """A streamed reply as requests' iter_lines yields it: a chunk per word, then DONE."""
    words = reply.split(" ")
    pieces = [words[0]] + [" " + word for word in words[1:]]
    return [b"data: " + json.dumps({"id": response_id,
                                    "choices": [{"delta": {"content": piece}}]}).encode()
            for piece in pieces] + [b"data: [DONE]"]


def stream_bytes(lines):
    """The bytes a stream of `lines` carried: each an event, and a blank line after."""
    return b"".join(line + b"\n\n" for line in lines)


def stream_hash(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def e2ee_public_key(private_key):
    """`private_key`'s uncompressed secp256k1 public key, as RedPill's keyset lists the
    e2ee key whose address is the attested signing address."""
    from mediabrowser.core import ethsig
    point = ethsig._mul(private_key, ethsig._G)
    return "04" + point[0].to_bytes(32, "big").hex() + point[1].to_bytes(32, "big").hex()


UP_TO_DATE = dcap.Result(status="UpToDate", platform="UpToDate", qe="UpToDate",
                         notes=["Intel's certificate chain verifies"])


def canonical(value):
    """JSON as RedPill signs and digests its keysets and receipts (RFC 8785, which for
    these is sorted keys and no spaces)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


class FakeNanoGpt:
    """A requests.Session standing in for nano-gpt: TEE attestation and the records of
    replies, NRAS, and the chat endpoint. Records every request so a test can say none
    was made.

    Intel's and NVIDIA's verification are stood in for by `intel` (a dcap.Result to
    return, or an exception to raise) and `nvidia` (a GPU count, or an exception); the
    real ones are tested against real evidence in RealEvidence below.

    As nano-gpt is live (Nov 2026): the quote binds the signing address and our nonce,
    and the record of a reply, signed by `signer`, hashes the exact bytes its stream
    carried - or `signed_bytes`. `tied` adds RedPill's keyset to the attestation:
    listing the signing key as its e2ee key ("e2ee", as nano-gpt relays it), bound by
    the quote itself in RedPill's statement ("statement"), or neither ("none"). With it,
    `receipts` puts the gateway's receipt in each record: its upstream `upstream`,
    signed by the keyset's key or `receipt_key`, under the keyset's digest or `digest`.
    `receipt_id` and `route` are the headers a reply names them in, and the first
    `not_ready` lookups of a record find none yet. The stream carries `tail` after its
    [DONE], or, `cut`, ends before it.

    `aci` is what `report=aci` answers (Nov 10 2026, gateway_report): RedPill's
    gateway's own report binding the keyset `tied` makes ("e2ee" when not given) to our
    nonce ("bound") or to another ("unbound"), a 400 as for a model only Chutes or
    Tinfoil serves ("400"), nano-gpt's rate limit ("429"), or, None, the plain report, as
    before nano-gpt offered the gateway's. Every attestation asked for is in `reports`:
    its params, headers and timeout.

    `record`: RedPill's record of the gateway's session, which each receipt names by its
    sha256 and api.redpill.ai serves - as `record_body`, or with `record_status` or a
    `record_length` header, when given (Nov 11 2026). Every request for one is in
    `records_asked`.

    From DA Assistant's tests (Nov 2026). This app's chat endpoint is netguard's
    transport, which tests/test_private_ai.py fakes with `stream()` and the headers
    `receipt_id` and `route` name (its tee_stream fixture)."""

    def __init__(self, nonce_bound=True, gpu=True, evidence=True, signer=KEY,
                 attestation_status=200, signature_status=200, reply="Hello there",
                 intel=UP_TO_DATE, nvidia=8, nras_status=200, attestation_extra=None,
                 signed_bytes=None, tied=None, receipts=False, upstream="verified",
                 receipt_key=None, digest=None, not_after=4_000_000_000, receipt_id=None,
                 route=None, not_ready=0, tail=b"", cut=False, aci=None, record=None,
                 record_body=None, record_status=200, record_length=None):
        if aci is not None and tied is None:
            tied = "e2ee"
        self.aci = aci
        self.reports = []
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
        self.signed_bytes = signed_bytes
        self.tied, self.receipts, self.upstream = tied, receipts, upstream
        self.receipt_key, self.digest, self.not_after = receipt_key, digest, not_after
        self.receipt_id, self.route, self.not_ready = receipt_id, route, not_ready
        self.tail, self.cut = tail, cut
        self.record, self.record_body = record, record_body
        self.record_status, self.record_length = record_status, record_length
        self.records_asked = []
        self._keyset = None
        self.gets = []
        self.posts = []
        self.lookups = []           # the headers of each lookup of a reply's record

    def lines(self):
        lines = sse_lines(self.reply)
        return lines[:-1] if self.cut else lines

    def stream(self):
        """The bytes the chat endpoint's stream carries."""
        return stream_bytes(self.lines()) + self.tail

    @property
    def keyset(self):
        """RedPill's workload keyset, as `tied` has it - made once, its Ed25519 key with
        it - or None."""
        if self.tied is None:
            return None
        if self._keyset is None:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            self.gateway = Ed25519PrivateKey.generate()
            public = self.gateway.public_key().public_bytes_raw().hex()
            self._keyset = {
                "subject": None, "not_after": self.not_after,
                "receipt_signing_keys": [{"key_id": "dstack-kms-receipt-ed25519-v1",
                                          "algo": "ed25519", "public_key": public}],
                "e2ee_public_keys": [{"key_id": "dstack-kms-e2ee-v1",
                                      "algo": "secp256k1-aes-256-gcm-hkdf-sha256",
                                      "public_key": e2ee_public_key(
                                          KEY if self.tied != "none" else 7)}],
                "tls_public_keys": []}
        return self._keyset

    def report(self, nonce):
        """The quote's report data: the signing address, zero padding and `nonce`, or
        RedPill's statement of the keyset and `nonce`, as its verifier computes it."""
        if self.tied == "statement":
            digest = "sha256:" + hashlib.sha256(canonical(self.keyset)).hexdigest()
            statement = (f'{{"keyset_digest":"{digest}","nonce":"{nonce}",'
                         f'"purpose":"aci.report_data.v1"}}')
            return hashlib.sha256(statement.encode()).digest() + bytes(32)
        return bytes.fromhex(address_of(KEY).lower()[2:]) + bytes(12) + bytes.fromhex(nonce)

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

    def receipt(self, request_hash, response_hash):
        """RedPill's receipt for one exchange, as nano-gpt's lookup carries it."""
        keyset = self.keyset
        receipt = {
            "api_version": "aci/1", "receipt_id": "rcpt-1",
            "key_id": "dstack-kms-receipt-ed25519-v1", "served_at": 1_800_000_000,
            "workload_keyset_digest": (self.digest or "sha256:"
                                       + hashlib.sha256(canonical(keyset)).hexdigest()),
            "event_log": [
                {"type": "request.received", "body_hash": f"sha256:{request_hash}"},
                {"type": "upstream.verified", "result": self.upstream, "required": True,
                 "session_id": self.session_id, "model_id": "m"},
                {"type": "response.returned", "body_hash": f"sha256:{response_hash}"}]}
        receipt["signature"] = (self.receipt_key or self.gateway).sign(
            canonical(receipt)).hex()
        return receipt

    @property
    def session_id(self):
        """The session a receipt names: the sha256 of RedPill's record of it, as JCS."""
        return hashlib.sha256(canonical(self.record)).hexdigest() if self.record else "s"

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

    def get(self, url, params=None, headers=None, timeout=None, stream=False):
        if url.startswith(privacy.SESSION_RECORDS):
            self.records_asked.append({"url": url, "headers": dict(headers or {}),
                                       "stream": stream, "timeout": timeout})
            body = (self.record_body if self.record_body is not None
                    else json.dumps(self.record).encode())
            length = {"content-length": self.record_length} if self.record_length else {}
            return FakeResponse(self.record_status, None, content=body, headers=length)
        if "/tee/attestation" in url:
            self.asked(url, params, headers, timeout)
            if params.get("report") == "aci" and self.aci is not None:
                return self.gateway_report(params["nonce"])
            if self.attestation_status != 200:
                return FakeResponse(self.attestation_status, {"error": "no"})
            nonce = params["nonce"]
            report = self.report(nonce if self.nonce_bound else "00" * 32)
            attestation = {"report_data": report.hex()}
            if self.keyset is not None:
                attestation["workload_keyset"] = self.keyset
            return FakeResponse(200, {
                "signing_address": address_of(KEY).lower(),
                "attestation": attestation,
                "intel_quote": fake_quote(report),
                "nvidia_payload": json.dumps(
                    {"nonce": nonce,
                     "evidence_list": [{"evidence": "e"}] if self.evidence else []}),
                **self.attestation_extra,
            })
        self.gets.append(url)
        if "/tee/signature/" in url:
            self.lookups.append(dict(headers or {}))
            if self.not_ready:
                self.not_ready -= 1
                return FakeResponse(404, {"error": "not yet"})
            if self.signature_status != 200:
                return FakeResponse(self.signature_status, {"error": "none kept"})
            request_hash = hashlib.sha256(b"the request as nano-gpt passed it on").hexdigest()
            response_hash = hashlib.sha256(self.stream() if self.signed_bytes is None
                                           else self.signed_bytes).hexdigest()
            text = f"{request_hash}:{response_hash}"
            record = {"text": text,
                      "signature": sign(personal_hash(text.encode()), self.signer,
                                        12345).hex()}
            if self.receipts:
                record["receipt"] = self.receipt(request_hash, response_hash)
            return FakeResponse(200, record)
        return FakeResponse(404, {})

    def post(self, url, json=None, headers=None, data=None, stream=False, timeout=None):
        self.posts.append({"url": url, "data": data, "json": json, "headers": headers})
        if url.startswith(privacy.NRAS_URL):
            # As NVIDIA's tokens are: for the nonce it was sent, and current.
            now = int(time.time())
            return FakeResponse(self.nras_status,
                                [["JWT", jwt({"x-nvidia-overall-att-result": self.gpu,
                                              "eat_nonce": (json or {}).get("nonce"),
                                              "nbf": now - 60, "exp": now + 3600})],
                                 {}])
        if url == API_URL:
            named = {"X-Receipt-Id": self.receipt_id, "X-TEE-Verification-Route": self.route}
            return FakeResponse(200, None, self.lines(), content=self.stream(),
                                headers={key: value for key, value in named.items() if value})
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

    def test_a_bound_quote_intel_and_nvidia_vouch_for_is_attested_its_loose_keys_named(self):
        """The quote binds the key that signs each reply's record, as nano-gpt serves
        it (Nov 2026), and not the gateway's receipt keys: said, keyset or none. Nor
        its GPUs: NVIDIA vouches for them, for our nonce, but the quote's report data is
        the signing key and the nonce, nothing of the GPUs' (SealedLore, Nov 11 2026)."""
        attestation = self.client().attest()
        self.assertEqual(attestation.level, "partial")
        self.assertEqual(attestation.shortfalls,
                         [privacy.RECEIPT_KEYS_LOOSE, privacy.GPUS_LOOSE])
        self.assertTrue(attestation.gpu_ok)
        self.assertEqual(attestation.gpus, 8)
        self.assertEqual(attestation.tcb_status, "UpToDate")
        self.assertEqual(attestation.signing_address, address_of(KEY).lower())
        self.assertIn("binds the signing key to our nonce", attestation.detail)
        self.assertIn("vouches for 8 GPUs", attestation.detail)
        self.assertIn("the quote does not commit to the GPU evidence, so the GPUs may be "
                      "another machine's", attestation.detail)
        self.assertEqual(attestation.headline, "TEE attested (receipt keys not in the "
                                               "quote, GPUs not tied to the quote)")
        # No gateway's report and no mark of RedPill's: the machine whose key signs, not
        # known to be a gateway.
        self.assertEqual(attestation.attested, "enclave")
        self.assertIn("Whether that machine runs the model itself or passes requests on "
                      "is not shown", privacy.what_was_attested(attestation))

    def test_one_whose_quote_binds_the_gateways_keys_itself_falls_short_of_its_gpus(self):
        """RedPill's own statement in the report data: the keyset, its signing key
        among them, and our nonce - the receipt keys bound, the GPUs still not."""
        client = self.client(tied="statement")
        attestation = client.attest()
        self.assertEqual(attestation.level, "partial")
        self.assertEqual(attestation.shortfalls, [privacy.GPUS_LOOSE])
        # The keyset is the gateway's: what was attested is RedPill's gateway.
        self.assertEqual(attestation.attested, "gateway")
        self.assertEqual(attestation.headline,
                         "TEE gateway attested (GPUs not tied to the quote)")
        self.assertIn("binds the gateway's keys, its signing key among them, to our "
                      "nonce", attestation.detail)
        self.assertIsNotNone(client.keyset)
        self.assertEqual(attestation.signing_address, address_of(KEY).lower())

    def test_no_gpu_evidence_is_named_once_and_not_as_gpus_not_tied(self):
        """With no GPU evidence there is nothing for the quote to leave untied."""
        attestation = self.client(tied="statement", evidence=False).attest()
        self.assertEqual(attestation.shortfalls, ["no GPU evidence"])

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

    def test_reply_records(self):
        """Only the signer was checked, so "signed" said nothing of the text shown: the
        record's response hash must be the hash of the exact bytes that came now. From
        SealedLore (Nov 2026)."""
        server = FakeNanoGpt()
        client = server.tee_client(API_URL, "k", TEXT)
        client.attest()
        came = stream_hash(server.stream())
        self.assertEqual(client.check_reply("chatcmpl-1", body_hash=came)[0],
                         privacy.VERIFIED)
        self.assertEqual(client.check_reply("chatcmpl-1")[0], privacy.SIGNED,
                         "no bytes to compare")
        state, detail = client.check_reply("chatcmpl-1",
                                           body_hash=stream_hash(b"data: [DONE]\n\n"))
        self.assertEqual(state, privacy.FAILED)
        self.assertIn("other bytes than the reply that came", detail)

        other = self.client(signer=1)
        other.attest()
        self.assertEqual(other.check_reply("chatcmpl-1", body_hash=came)[0], privacy.FAILED)

        none_kept = self.client(signature_status=500)
        none_kept.attest()
        self.assertEqual(none_kept.check_reply("chatcmpl-1", body_hash=came)[0],
                         privacy.UNCHECKED)
        self.assertEqual(client.check_reply(None)[0], privacy.UNCHECKED)


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
        self.closed = False

    def close(self):
        self.closed = True

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
            with self.assertRaisesRegex(dcap.DcapError, f"Intel's {key} isn't signed by "
                                                        f"Intel's TCB signing certificate"):
                self.intel(session=FixtureSession(forged))

    def test_collateral_out_of_reach_is_reported_not_passed(self):
        result = self.intel(session=FixtureSession(self.fixture, intel_status=503))
        self.assertFalse(result.collateral)
        self.assertEqual(result.status, "")
        self.assertIn("could not be fetched", result.notes[-1])

    def test_collateral_that_arrived_but_cannot_be_read_is_refused(self):
        """Out of reach is "unchecked", a partial attestation. What did arrive and
        cannot be read was taken for the same - a CRL that is no CRL, an issuer chain or
        a signed document left out, JSON that does not parse - so that an answer shaped
        to fail parsing, by whatever answered at Intel's address, bought a pass as
        "unchecked" for a revoked platform. It is refused now, as a bad signature is.
        From DA Vibe Manager's copy of this module (Nov 2026)."""
        def forged(index, body=None, drop_header=None):
            copy = json.loads(json.dumps(self.fixture))
            if body is not None:
                copy["collateral"][index]["body"] = base64.b64encode(body).decode()
            if drop_header:
                del copy["collateral"][index]["headers"][drop_header]
            return FixtureSession(copy)

        for session, refusal in (
                (forged(0, b"<html>not a CRL</html>"), "a CRL from Intel could not be read"),
                (forged(1, b"<html>not a CRL</html>"), "a CRL from Intel could not be read"),
                (forged(2, drop_header="TCB-Info-Issuer-Chain"),
                 "Intel's response had no TCB-Info-Issuer-Chain"),
                (forged(3, drop_header="SGX-Enclave-Identity-Issuer-Chain"),
                 "Intel's response had no SGX-Enclave-Identity-Issuer-Chain"),
                (forged(2, b'{"signature": "00"}'), "Intel's tcbInfo was missing"),
                (forged(3, b'{"enclaveIdentity": {"id": "TD_QE"'),
                 "Intel's collateral could not be read")):
            dcap._cache.clear()
            with self.subTest(refusal), self.assertRaisesRegex(dcap.DcapError, refusal):
                self.intel(session=session)

    def test_an_attestation_whose_collateral_cannot_be_read_is_refused_not_partial(self):
        copy = json.loads(json.dumps(self.fixture))
        copy["collateral"][0]["body"] = base64.b64encode(b"<html>maintenance</html>").decode()
        client = TeeClient(API_URL, "k", self.fixture["model"], session=FixtureSession(copy),
                           intel=lambda raw, s: dcap.verify(raw, s, now=self.when),
                           nvidia=lambda a, n, s: nras.verify(a, n, s, now=self.now))
        with mock.patch.object(privacy.secrets, "token_hex",
                               return_value=self.fixture["nonce"]), \
                self.assertRaisesRegex(PrivacyError, "failed verification: a CRL from "
                                                     "Intel could not be read"):
            client.attest()

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
            self.assertTrue(self.session.closed, "dcap.verify left its own session open")
            self.assertEqual(nras.verify(self.fixture["nras_answer"], self.fixture["nonce"],
                                         now=self.now), 8)
            self.assertIs(TeeClient(API_URL, "k", TEXT).session, self.session)
        self.assertEqual(made.call_count, 3)

    # together

    def test_the_whole_attestation_holds_with_only_its_receipt_keys_and_gpus_short(self):
        """Sept 2026's capture has no keyset: no receipts, and nothing binds them
        either; and its quote binds the signing key and our nonce, not its GPUs.
        Everything else is proved."""
        client = TeeClient(API_URL, "k", self.fixture["model"], session=self.session,
                           intel=lambda raw, s: dcap.verify(raw, s, now=self.when),
                           nvidia=lambda a, n, s: nras.verify(a, n, s, now=self.now))
        original = privacy.secrets.token_hex
        privacy.secrets.token_hex = lambda _n: self.fixture["nonce"]
        try:
            attestation = client.attest()
        finally:
            privacy.secrets.token_hex = original
        self.assertEqual(attestation.headline, "TEE attested (receipt keys not in the "
                                               "quote, GPUs not tied to the quote)")
        self.assertEqual(attestation.shortfalls,
                         [privacy.RECEIPT_KEYS_LOOSE, privacy.GPUS_LOOSE])
        self.assertEqual((attestation.gpus, attestation.tcb_status), (8, "UpToDate"))
        self.assertEqual(attestation.signing_address, self.fixture["signing_address"])
        self.assertIsNone(client.keyset)


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

    def signed_tcb_info(self, signer_name, through_ca=False):
        """Intel's TCB info as its service sends it, signed by a certificate named
        `signer_name` that the root issued - or, `through_ca`, that a CA under the root
        issued."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        from cryptography.hazmat.primitives.serialization import Encoding
        root = self.root()
        issuers = [root]
        if through_ca:
            issuers.insert(0, self.under(root, "root", "Intel SGX PCK Platform CA", "ca",
                                         ca=True, path_length=0, cert_sign=True))
        signer = self.under(issuers[0], "ca" if through_ca else "root", signer_name,
                            "leaf", ca=False, cert_sign=False)
        raw = json.dumps({"id": "TDX", "fmspc": "20a06f000000"}, separators=(",", ":"))
        r, s = decode_dss_signature(self.keys["leaf"].sign(raw.encode(),
                                                           ec.ECDSA(hashes.SHA256())))
        signature = (r.to_bytes(32, "big") + s.to_bytes(32, "big")).hex()
        content = f'{{"tcbInfo":{raw},"signature":"{signature}"}}'.encode()
        chain = b"".join(cert.public_bytes(Encoding.PEM) for cert in [signer] + issuers)
        return dcap._signed_json(content, "tcbInfo", chain.decode(), root, self.NOW)

    def test_tcb_info_signed_by_any_but_the_tcb_signing_certificate_is_refused(self):
        info, signer = self.signed_tcb_info("Intel SGX TCB Signing")
        self.assertEqual(info["fmspc"], "20a06f000000")
        for name in ("Intel SGX PCK Certificate", "Intel SGX TCB Signing 2"):
            with self.assertRaisesRegex(dcap.DcapError, "Intel's tcbInfo isn't signed by "
                                                        "Intel's TCB signing certificate"):
                self.signed_tcb_info(name)

    def test_the_tcb_signing_certificate_must_be_the_roots_own(self):
        """Named as Intel's TCB signing certificate, but issued by a CA under the root:
        its chain verifies, a CA allowed to issue and all, and its name was all that was
        asked of it. Now it must be the only certificate between the signature and the
        root, as in the other apps' copies of this module (Nov 2026)."""
        root = self.root()
        ca = self.under(root, "root", "Intel SGX PCK Platform CA", "ca", ca=True,
                        path_length=0, cert_sign=True)
        signer = self.under(ca, "ca", "Intel SGX TCB Signing", "leaf", ca=False,
                            cert_sign=False)
        dcap.verify_chain([signer, ca], root, self.NOW)             # a chain that holds
        with self.assertRaisesRegex(dcap.DcapError, "Intel's tcbInfo isn't signed by "
                                                    "Intel's TCB signing certificate"):
            self.signed_tcb_info("Intel SGX TCB Signing", through_ca=True)


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

    def get(self, url, params=None, headers=None, timeout=None, stream=False):
        if "/tee/attestation" in url and (params.get("report") != "aci"
                                          or self.aci in (None, "ignored")):
            self.asked(url, params, headers, timeout)
            if params.get("report") == "aci" and self.aci is None:
                return FakeResponse(400, {"error": "report=aci is only available for "
                                                   "models served through RedPill"})
            status, body = self.respond(params["nonce"])
            return FakeResponse(status, body)
        return super().get(url, params, headers, timeout, stream)

    def post(self, url, json=None, **kwargs):
        if url.startswith(privacy.NRAS_URL):
            self.nras_nonces.append(json.get("nonce"))
        return super().post(url, json=json, **kwargs)

    def nvidia_verify(self, answer, nonce, session):
        self.verified_nonces = getattr(self, "verified_nonces", []) + [nonce]
        return super().nvidia_verify(answer, nonce, session)


class GpusTied(FakeNanoGpt):
    """FakeNanoGpt, standing for an enclave whose GPU evidence its quote commits to: one
    that proves everything this app can check. None of nano-gpt's TEE/ models does (their
    quotes bind the signing key and our nonce, nothing of the GPUs: GPUS_LOOSE), so this
    is what Strict's handling of an enclave it has accepted is tested with - its replies
    doubted, its attestation made again - and what the other shortfalls are tested alone
    against."""

    def tee_client(self, url, key, model):
        client = super().tee_client(url, key, model)
        attest = client.attest

        def tied():
            attestation = attest()
            attestation.shortfalls = [what for what in attestation.shortfalls
                                      if what != privacy.GPUS_LOOSE]
            attestation.level = "partial" if attestation.shortfalls else "full"
            return attestation

        client.attest = tied
        return client


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
        self.assertEqual(state, privacy.UNCHECKED)
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



# ---- RedPill's gateway, as nano-gpt relays it -----------------------------------------

GATEWAY = {"api_version": "aci/1", "service_capabilities": {"serving": "aggregator"},
           "attestation": {"workload_keyset": {"not_after": 4_000_000_000}}}


class GatewayAttestation(unittest.TestCase):
    """What nano-gpt relays for its TEE/ models is RedPill's gateway, which passes each
    request on to the model's host: its quote is the gateway's, and the model's own
    enclave is not shown. Ported from DA Assistant's tests (Nov 2026), from DA Vibe
    Manager's."""

    def attest(self, extra=GATEWAY):
        return FakeNanoGpt(attestation_extra=extra).tee_client(API_URL, "k", TEXT).attest()

    def test_the_gateways_attestation_is_partial_and_says_why(self):
        attestation = self.attest()
        self.assertEqual(attestation.level, "partial")
        self.assertIn(privacy.GATEWAY_SHORTFALL, attestation.headline)
        self.assertEqual(attestation.fell_short,
                         [privacy.GATEWAY_SHORTFALL, privacy.RECEIPT_KEYS_LOOSE,
                          privacy.GPUS_LOOSE])
        self.assertIn("not that the model itself runs in an enclave", attestation.detail)
        self.assertEqual((attestation.gpus, attestation.tcb_status), (8, "UpToDate"),
                         "every other check stands")

    def test_either_of_its_marks_is_the_gateway(self):
        for extra in ({"api_version": "aci/1"},
                      {"service_capabilities": {"serving": "aggregator"}}):
            self.assertEqual(self.attest(extra).fell_short,
                             [privacy.GATEWAY_SHORTFALL, privacy.RECEIPT_KEYS_LOOSE,
                              privacy.GPUS_LOOSE])
        self.assertEqual(self.attest({}).fell_short,
                         [privacy.RECEIPT_KEYS_LOOSE, privacy.GPUS_LOOSE])

    @unittest.skipUnless(_has_cryptography(), "the cryptography package is not installed")
    def test_bound_by_its_own_statement_it_is_still_the_gateway(self):
        """RedPill's statement ties its receipt keys to the quote; the quote is still
        the gateway's, not the model's host's."""
        server = FakeNanoGpt(tied="statement", attestation_extra={
            key: GATEWAY[key] for key in ("api_version", "service_capabilities")})
        attestation = server.tee_client(API_URL, "k", TEXT).attest()
        self.assertEqual(attestation.fell_short,
                         [privacy.GATEWAY_SHORTFALL, privacy.GPUS_LOOSE])

    def test_keys_it_attests_that_have_expired_are_refused(self):
        expired = dict(GATEWAY, attestation={"workload_keyset": {"not_after": 1700000000}})
        with self.assertRaisesRegex(PrivacyError, "attested keys expired on 2023-11-14"):
            self.attest(expired)

    def test_strict_refuses_it_and_says_where_to_turn(self):
        server = FakeNanoGpt(attestation_extra=GATEWAY)
        strict = PrivacySession(API_URL, "k", TEXT, tee_factory=server.tee_client,
                                strict=True)
        with self.assertRaisesRegex(PrivacyError, "RedPill's gateway, not the model's "
                                                  "host.*strict attestation is on.*choose "
                                                  "a private/ model.*nothing was sent"):
            strict.ensure()
        self.assertEqual(server.chat_posts, [])
        relaxed = PrivacySession(API_URL, "k", TEXT, tee_factory=FakeNanoGpt(
            attestation_extra=GATEWAY).tee_client)
        self.assertEqual(relaxed.ensure().level, "partial")


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
    binds the gateway's whole keyset - the receipt key and the keys that sign each
    reply's record - to a nonce of ours. Asked for beside the model's own report, each
    with its own nonce. Ported from DA Assistant's tests (Nov 2026), from SealedLore's;
    the gateway stays partial here, as it is wherever it answers (GATEWAY_SHORTFALL)."""

    @classmethod
    def setUpClass(cls):
        cls.instances = InstanceEvidence()

    def check(self, client, server, receipt_id="rcpt-1", **record):
        record.setdefault("body_hash", stream_hash(server.stream()))
        return client.check_reply("chatcmpl-42", receipt_id=receipt_id, **record)

    def test_the_gateways_report_binds_the_receipt_keys(self):
        server = FakeNanoGpt(aci="bound", receipts=True)
        client = server.tee_client(API_URL, "k", TEXT)
        attestation = client.attest()
        self.assertEqual(attestation.shortfalls,
                         [privacy.GATEWAY_SHORTFALL, privacy.GPUS_LOOSE],
                         "whole but for the gateway, and GPUs the quote does not name")
        self.assertIn("the gateway's quote binds its receipt keys, the signing key among "
                      "them", attestation.detail)
        self.assertIn("not that the model itself runs in an enclave", attestation.detail)
        self.assertEqual((attestation.gpus, attestation.tcb_status), (8, "UpToDate"),
                         "the model's own report is checked as ever")
        self.assertEqual(attestation.signing_address, address_of(KEY).lower())
        self.assertEqual(attestation.signers, (address_of(KEY).lower(),))
        self.assertIsNotNone(client.keyset)
        state, detail = self.check(client, server)
        self.assertEqual(state, privacy.VERIFIED, detail)
        self.assertIn("RedPill's receipt for it holds", detail)
        nonces = [r["params"]["nonce"] for r in server.reports]
        self.assertEqual(len(nonces), 2)
        self.assertEqual(len(set(nonces)), 2, "a fresh nonce for each")
        self.assertTrue(all(len(n) == 64 and n == n.lower() and int(n, 16) >= 0
                            for n in nonces))
        aci = [r for r in server.reports if r["params"].get("report") == "aci"]
        self.assertEqual(len(aci), 1)
        self.assertTrue(all(r["timeout"] == privacy.ATTESTATION_TIMEOUT_SECONDS == 90
                            for r in server.reports), "Chutes' evidence can take a minute")

    def test_a_record_signed_by_another_of_the_gateways_bound_keys_holds(self):
        """Live, every model's signing key was one of the gateway's two e2ee keys."""
        server = FakeNanoGpt(aci="bound", receipts=True)
        server.keyset["e2ee_public_keys"].append(
            {"key_id": "dstack-kms-e2ee-v2", "algo": "secp256k1-aes-256-gcm-hkdf-sha256",
             "public_key": e2ee_public_key(9)})
        client = server.tee_client(API_URL, "k", TEXT)
        self.assertEqual(len(client.attest().signers), 2)
        server.signer = 9
        state, detail = self.check(client, server)
        self.assertEqual(state, privacy.VERIFIED, detail)
        self.assertIn(address_of(9), detail)
        server.signer = 1
        state, detail = self.check(client, server)
        self.assertEqual(state, privacy.FAILED)
        self.assertIn("not any key the attestation bound", detail)

    def test_a_gateway_report_for_another_nonce_is_refused(self):
        server = FakeNanoGpt(aci="unbound")
        with self.assertRaisesRegex(PrivacyError, "gateway did not bind its keys to our "
                                                  "nonce"):
            server.tee_client(API_URL, "k", TEXT).attest()
        session = session_for(FakeNanoGpt(aci="unbound"))
        with self.assertRaisesRegex(PrivacyError, "nothing was sent"):
            session.ensure()

    def test_its_keys_past_their_time_are_refused(self):
        server = FakeNanoGpt(aci="bound", not_after=1_700_000_000)
        with self.assertRaisesRegex(PrivacyError, "attested keys expired on 2023-11-14"):
            server.tee_client(API_URL, "k", TEXT).attest()

    def test_without_the_gateways_report_the_receipt_keys_are_named_short(self):
        # A 400 (Chutes or Tinfoil alone), nano-gpt's rate limit, and the plain report
        # given for report=aci, as before nano-gpt offered the gateway's.
        for server in (FakeNanoGpt(aci="400", tied="e2ee"), FakeNanoGpt(aci="429"),
                       FakeNanoGpt(tied="e2ee"),
                       FakeNanoGpt(tied="e2ee", attestation_extra=GATEWAY)):
            attestation = server.tee_client(API_URL, "k", TEXT).attest()
            self.assertIn(privacy.RECEIPT_KEYS_LOOSE, attestation.shortfalls)
            self.assertEqual(attestation.signers, (address_of(KEY).lower(),))

    def test_a_gateway_whose_keys_do_not_hold_the_signing_key_counts_for_nothing(self):
        server = FakeNanoGpt(aci="bound", tied="none")
        client = server.tee_client(API_URL, "k", TEXT)
        attestation = client.attest()
        self.assertEqual(attestation.shortfalls,
                         [privacy.RECEIPT_KEYS_LOOSE, privacy.GPUS_LOOSE])
        self.assertIn("the gateway's attested keys do not include the signing key",
                      attestation.detail)
        self.assertIsNone(client.keyset)
        self.assertEqual(attestation.signers, (address_of(KEY).lower(),))

    def test_the_gateway_stands_in_for_a_model_whose_own_evidence_was_relayed(self):
        """Live, TEE/deepseek-v3.2: its own report is Chutes' for the relay's nonce,
        its replies RedPill's, with receipts. Refused before."""
        server = ShapedNanoGpt(lambda nonce: (200, RELAYED), aci="bound", receipts=True)
        client = server.tee_client(API_URL, "k", "TEE/deepseek-v3.2")
        attestation = client.attest()
        self.assertEqual(attestation.level, "partial")
        self.assertEqual(attestation.shortfalls,
                         [privacy.GATEWAY_SHORTFALL, "no GPU evidence"])
        self.assertIn("relayed for a nonce not ours: the gateway that answers it is "
                      "attested instead", attestation.detail)
        self.assertIn("no GPU evidence", attestation.summary)
        self.assertEqual(attestation.signing_address, "")
        self.assertEqual(attestation.signers, (address_of(KEY).lower(),))
        state, detail = self.check(client, server)
        self.assertEqual(state, privacy.VERIFIED, detail)
        self.assertIn("RedPill's receipt for it holds", detail)
        # Without the gateway, refused as before.
        alone = ShapedNanoGpt(lambda nonce: (200, RELAYED), aci="400")
        with self.assertRaisesRegex(PrivacyError, "nonce the relay chose"):
            alone.tee_client(API_URL, "k", "TEE/deepseek-v3.2").attest()

    def test_gpus_not_tied_to_the_quote_change_nothing_strict_refuses(self):
        """GPUS_LOOSE turns nothing amber that was green among the shapes nano-gpt's TEE/
        models attest in (live, Nov 2026): every one RedPill serves fell short for the
        gateway already, so Strict refused it already; a Chutes instance's quote binds
        its GPU evidence's nonce, so its GPUs are its own, and a model Chutes alone
        serves stays whole. From DA Assistant's tests (b931944)."""
        marks = {key: GATEWAY[key] for key in ("api_version", "service_capabilities")}
        shapes = {
            "RedPill's, with the gateway's report": FakeNanoGpt(
                aci="bound", attestation_extra=marks),
            "RedPill's, without the gateway's report": FakeNanoGpt(
                aci="400", tied="e2ee", attestation_extra=marks),
            "through the gateway alone": ShapedNanoGpt(lambda nonce: (200, RELAYED),
                                                       aci="bound"),
            "Chutes first, with the gateway": ShapedNanoGpt(
                lambda nonce: (200, self.instances.body(nonce)), aci="bound"),
        }
        for what, server in shapes.items():
            lacked = server.tee_client(API_URL, "k", "TEE/x").attest().fell_short
            self.assertIn(privacy.GATEWAY_SHORTFALL, lacked, what)
            self.assertTrue([w for w in lacked if w != privacy.GPUS_LOOSE], what)
        chutes = ShapedNanoGpt(lambda nonce: (200, self.instances.body(nonce)))
        attestation = chutes.tee_client(API_URL, "k", "TEE/kimi-k3").attest()
        self.assertEqual((attestation.level, attestation.fell_short), ("full", []))

    def test_through_the_gateway_alone_a_reply_without_a_receipt_is_not_verified(self):
        """The gateway's signature on a reply's record says only that the reply passed
        through it; the receipt is its word that the model's own enclave answered. A
        record with no receipt was "verified" all the same, as for a model attested in
        its own right. From SealedLore (dd39efc, Nov 11 2026)."""
        bare = ShapedNanoGpt(lambda nonce: (200, RELAYED), aci="bound")
        client = bare.tee_client(API_URL, "k", "TEE/deepseek-v3.2")
        client.attest()
        self.assertTrue(client.receipt_required)
        state, detail = self.check(client, bare)
        self.assertEqual(state, privacy.UNCHECKED, detail)
        self.assertIn("it came with no receipt, and through RedPill's gateway alone only "
                      "a receipt says the model's own enclave answered it", detail)
        # Without the bytes to compare either: still not on the signature alone.
        self.assertEqual(self.check(client, bare, body_hash=None)[0], privacy.UNCHECKED)
        # A receipt the attested keys cannot be held to is none either.
        undated = ShapedNanoGpt(lambda nonce: (200, RELAYED), aci="bound", receipts=True,
                                not_after=None)
        client = undated.tee_client(API_URL, "k", "TEE/deepseek-v3.2")
        client.attest()
        self.assertIsNone(client.keyset)
        state, detail = self.check(client, undated)
        self.assertEqual(state, privacy.UNCHECKED, detail)
        self.assertIn("its receipt could not be read under the attested keys", detail)
        # A model attested itself needs none: its own key signed the record.
        itself = FakeNanoGpt(aci="bound")
        client = itself.tee_client(API_URL, "k", TEXT)
        client.attest()
        self.assertFalse(client.receipt_required)
        self.assertEqual(self.check(client, itself, receipt_id=None)[0], privacy.VERIFIED)

    def test_the_receipt_rule_goes_once_the_model_attests_in_its_own_right(self):
        """Set by the gateway standing in, and cleared at the start of every
        attestation: the model's own report answering again is the model attested."""
        shape = {"body": RELAYED}

        def respond(nonce):
            if shape["body"] is RELAYED:
                return 200, RELAYED
            report = bytes.fromhex(address_of(KEY).lower()[2:]) + bytes(12) + \
                bytes.fromhex(nonce)
            return 200, {"signing_address": address_of(KEY).lower(),
                         "intel_quote": fake_quote(report),
                         "nvidia_payload": json.dumps({"nonce": nonce,
                                                       "evidence_list": []})}

        server = ShapedNanoGpt(respond, aci="bound")
        client = server.tee_client(API_URL, "k", "TEE/qwen3.6-27b")
        client.attest()
        self.assertTrue(client.receipt_required)
        shape["body"] = None
        client.attest()
        self.assertFalse(client.receipt_required)
        self.assertEqual(self.check(client, server, receipt_id=None)[0], privacy.VERIFIED)

    def test_strict_refuses_a_model_attested_through_the_gateway(self):
        server = ShapedNanoGpt(lambda nonce: (200, RELAYED), aci="bound", receipts=True)
        strict = PrivacySession(API_URL, "k", "TEE/deepseek-v3.2",
                                tee_factory=server.tee_client, strict=True)
        with self.assertRaisesRegex(PrivacyError, "RedPill's gateway, not the model's host, "
                                                  "no GPU evidence.*choose a private/ "
                                                  "model.*nothing was sent"):
            strict.ensure()

    def test_a_slow_attestation_is_asked_again_with_a_new_nonce_then_the_gateway_stands_in(
            self):
        asked = []

        def once(nonce):
            asked.append(nonce)
            return (504, TIMED_OUT) if len(asked) == 1 else (200, RELAYED)

        ShapedNanoGpt(once, aci="bound").tee_client(API_URL, "k", "TEE/x").attest()
        self.assertEqual(len(asked), 2)
        self.assertNotEqual(asked[0], asked[1])
        # Live, TEE/qwen3.6-27b: its own report always times out.
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
        """Chutes answers first and signs nothing; a reply RedPill answers instead names
        its receipt, checked against the gateway's bound keys."""
        server = ShapedNanoGpt(lambda nonce: (200, self.instances.body(nonce)),
                               aci="bound", receipts=True)
        client = server.tee_client(API_URL, "k", "TEE/glm-5.1")
        attestation = client.attest()
        self.assertEqual(attestation.instances, 2)
        self.assertEqual(attestation.gpus, 16)
        self.assertEqual(attestation.shortfalls, [privacy.GATEWAY_SHORTFALL],
                         "a request may be answered by RedPill's gateway")
        self.assertIn("a reply RedPill's gateway answers instead is checked against its "
                      "attested keys", attestation.detail)
        self.assertIn("the instances sign no replies", attestation.detail)
        state, detail = self.check(client, server)
        self.assertEqual(state, privacy.VERIFIED, detail)
        looked_up = len(server.lookups)
        state, detail = self.check(client, server, receipt_id=None)
        self.assertEqual(state, privacy.UNCHECKED)
        self.assertIn("signs no replies", detail)
        self.assertEqual(len(server.lookups), looked_up, "Chutes' reply is not looked up")
        # Chutes alone (report=aci answers 400): whole, its replies unsigned as before.
        alone = ShapedNanoGpt(lambda nonce: (200, self.instances.body(nonce)))
        client = alone.tee_client(API_URL, "k", "TEE/kimi-k3")
        attestation = client.attest()
        self.assertEqual((attestation.level, attestation.signers), ("full", ()))
        self.assertEqual(self.check(client, alone)[0], privacy.UNCHECKED)
        self.assertEqual(alone.lookups, [])

    def test_a_chutes_model_whose_gateway_could_not_be_asked_falls_short(self):
        """Live (Nov 10 2026), report=aci was rate-limited for TEE/glm-5.1, which then
        read whole: better than with the gateway's report, which names the gateway that
        may answer it. Only a 400 - RedPill does not serve it - leaves it whole."""
        for aci in ("429", "ignored"):
            server = ShapedNanoGpt(lambda nonce: (200, self.instances.body(nonce)), aci=aci)
            attestation = server.tee_client(API_URL, "k", "TEE/glm-5.1").attest()
            self.assertEqual(attestation.shortfalls, [privacy.GATEWAY_UNCHECKED], aci)
            self.assertIn("nano-gpt gave no report of RedPill's gateway", attestation.detail)

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

    def test_neither_report_is_asked_with_a_replys_route(self):
        """The route a reply names goes with the lookup of its record, as SealedLore
        sends it; an attestation is made for whatever answers next, and a route that
        has expired makes nano-gpt answer it with a 400."""
        server = FakeNanoGpt(aci="bound", receipts=True)
        client = server.tee_client(API_URL, "k", TEXT)
        client.attest()
        self.assertEqual(self.check(client, server, receipt_id="rcpt-7", route="r2")[0],
                         privacy.VERIFIED)
        client.attest()
        self.assertIn("/tee/signature/rcpt-7", [g for g in server.gets
                                                if "/tee/signature/" in g][-1])
        self.assertEqual(server.lookups[-1].get("X-TEE-Verification-Route"), "r2")
        self.assertEqual(len(server.reports), 4)
        self.assertFalse(any("X-TEE-Verification-Route" in r["headers"]
                             for r in server.reports))

    def test_the_details_name_every_key_a_reply_may_be_signed_by(self):
        server = ShapedNanoGpt(lambda nonce: (200, RELAYED), aci="bound")
        session = PrivacySession(API_URL, "k", "TEE/deepseek-v3.2",
                                 tee_factory=server.tee_client)
        session.ensure()
        self.assertIn(f"signing key {address_of(KEY).lower()}", session.details())


@unittest.skipUnless(_has_cryptography(), "the cryptography package is not installed")
class WhatWasAttested(unittest.TestCase):
    """SealedLore's author, Nov 11 2026: "always make clear to the user what is actually
    happening and if it is properly attested or not". RedPill's gateway is not the
    machine that runs the model; Chutes' instances sign no replies. Every attestation
    says which it was, and what that leaves to someone's word (9af15c1). Ported from DA
    Assistant's tests (98e939a)."""

    MARKS = {key: GATEWAY[key] for key in ("api_version", "service_capabilities")}

    @classmethod
    def setUpClass(cls):
        cls.instances = InstanceEvidence()

    def attest(self, server, model="TEE/x"):
        return server.tee_client(API_URL, "k", model).attest()

    def test_redpills_gateway_is_named_as_what_was_attested(self):
        for what, server in (
                ("its own report", FakeNanoGpt(aci="bound")),
                ("its marks, no report of its own",
                 FakeNanoGpt(aci="400", tied="e2ee", attestation_extra=self.MARKS)),
                ("RedPill's own statement", FakeNanoGpt(tied="statement")),
                ("standing in alone",
                 ShapedNanoGpt(lambda nonce: (200, RELAYED), aci="bound"))):
            attestation = self.attest(server)
            self.assertEqual(attestation.attested, "gateway", what)
            self.assertTrue(attestation.headline.startswith("TEE gateway attested ("),
                            what)
            said = privacy.what_was_attested(attestation)
            self.assertTrue(said.startswith("Attested: RedPill's TEE gateway, which passes "
                                            "this model's requests on to the machine that "
                                            "runs it."), what)
            self.assertIn("That machine is not attested by this app: that it is a "
                          "verified TEE is the gateway's word, in each reply's signed "
                          "receipt.", said)
            self.assertIn("Your text passes nano-gpt's servers in the clear on the way",
                          said)
            self.assertNotIn("in the clear", privacy.what_was_attested(attestation,
                                                                       clear=False))

    def test_chutes_instances_are_named_and_that_they_sign_no_replies(self):
        alone = self.attest(ShapedNanoGpt(lambda nonce: (200, self.instances.body(nonce))),
                            "TEE/kimi-k3")
        self.assertEqual((alone.attested, alone.headline), ("instances", "TEE attested"))
        said = privacy.what_was_attested(alone)
        self.assertIn("Attested: each of the 2 instances this model's provider listed. "
                      "They sign no replies, so nothing shows which machine answered a "
                      "request", said)
        self.assertNotIn("RedPill's gateway", said)
        first = self.attest(ShapedNanoGpt(lambda nonce: (200, self.instances.body(nonce)),
                                          aci="bound"), "TEE/glm-5.1")
        self.assertEqual(first.attested, "instances")
        self.assertIn("RedPill's gateway, attested too, answers it now and then",
                      privacy.what_was_attested(first))

    def test_one_enclave_not_known_to_be_a_gateway_is_said_as_such(self):
        attestation = self.attest(FakeNanoGpt())
        self.assertEqual(attestation.attested, "enclave")
        self.assertEqual(attestation.headline, "TEE attested (receipt keys not in the "
                                               "quote, GPUs not tied to the quote)")
        self.assertTrue(privacy.what_was_attested(attestation).startswith(
            "Attested: the enclave whose key signs this model's replies."))

    def test_an_end_to_end_encrypted_enclave_or_an_unnamed_one_says_nothing_of_it(self):
        sealed_one = privacy.Attestation(model="private/glm-5-3", kind=privacy.E2EE)
        self.assertEqual(privacy.what_was_attested(sealed_one), "")
        self.assertEqual(sealed_one.headline, "End-to-end encrypted")
        unnamed = privacy.Attestation(model="TEE/x", kind=privacy.TEE)
        self.assertEqual((privacy.what_was_attested(unnamed), unnamed.headline),
                         ("", "TEE attested"))
        self.assertEqual(privacy.what_was_attested(None), "")

    def test_the_details_lead_with_what_was_attested(self):
        server = FakeNanoGpt(aci="bound")
        session = PrivacySession(API_URL, "k", TEXT, tee_factory=server.tee_client)
        session.ensure()
        details = session.details()
        headline, what = details.splitlines()[1:3]
        self.assertTrue(headline.strip().startswith("TEE gateway attested ("))
        self.assertTrue(what.strip().startswith("Attested: RedPill's TEE gateway"))
        self.assertIn("the gateway's quote binds its receipt keys", details)
        chutes = ShapedNanoGpt(lambda nonce: (200, self.instances.body(nonce)))
        session = PrivacySession(API_URL, "k", "TEE/kimi-k3", tee_factory=chutes.tee_client)
        session.ensure()
        self.assertIn("They sign no replies", session.details())

    def test_nothing_says_every_tee_reply_is_signed(self):
        """Chutes signs none: a TEE/ model's description and the caveats said each was
        signed, or checked against a record."""
        said = privacy.describe(TEXT)[1]
        self.assertNotIn("replies are signed", said)
        self.assertIn("where its provider signs one", said)
        tee = PrivacySession(API_URL, "k", TEXT).caveats()
        self.assertIn("Chutes signs none, so its replies read not verified", tee)
        self.assertIn("that the GPUs NVIDIA vouched for are the attested machine's", tee)


# ---- the provider behind a reply (Settings → Privacy, off by default) -----------------

# RedPill's record of a gateway session with Tinfoil's router, as api.redpill.ai serves
# one (SealedLore's tests, Nov 11 2026): the receipt's served_at, 1_800_000_000, is inside.
TINFOIL = {
    "api_version": "aci/1",
    "upstream_name": "tinfoil",
    "verifier_id": "private-ai-verifier/tinfoil/v1",
    "claims": {"extra": {"evidence_scope": "router"}, "gpu_attested": {"status": "unknown"}},
    "established_at": 1_799_999_000,
    "expires_at": 1_800_002_600,
}


@unittest.skipUnless(_has_cryptography(), "the cryptography package is not installed")
class ProviderBehindAReply(unittest.TestCase):
    """SealedLore 44e145d (Nov 11 2026): after a TEE/ reply whose receipt holds under the
    attested keys, RedPill's public record of the gateway's session names the provider
    that ran the model - looked up from api.redpill.ai with no key, believed only as the
    signed receipt names it, never for a model Chutes serves, and never changing the
    reply's state. Ported from DA Assistant's tests (aee8930)."""

    def setUp(self):
        patch = mock.patch.object(privacy, "_records", {})
        patch.start()
        self.addCleanup(patch.stop)

    def named(self, server, model=TEXT, receipt_id="rcpt-1"):
        client = server.tee_client(API_URL, "k", model)
        client.attest()
        return client, client.check("chatcmpl-42", receipt_id=receipt_id,
                                    body_hash=stream_hash(server.stream()),
                                    name_upstream=True)

    def test_a_reply_names_its_provider_by_redpills_record_read_once_a_session(self):
        server = FakeNanoGpt(aci="bound", receipts=True, record=TINFOIL)
        client, found = self.named(server)
        self.assertEqual(found.state, privacy.VERIFIED, found.detail)
        upstream = found.upstream
        self.assertEqual((upstream.name, upstream.scope, upstream.gpus, upstream.model),
                         ("tinfoil", "router", "unknown", "m"))
        self.assertEqual(privacy.upstream_label(upstream), "Tinfoil")
        words = privacy.upstream_words(upstream)
        self.assertIn("Run by Tinfoil (as m): RedPill's record of its gateway's session says "
                      "its verifier (private-ai-verifier/tinfoil/v1) checked its router",
                      words)
        self.assertIn("RedPill's record has no GPU attestation", words)
        self.assertIn("That is RedPill's word, fetched from api.redpill.ai and matched to "
                      "the receipt the attested gateway signed", words)
        asked = server.records_asked
        self.assertEqual([a["url"] for a in asked],
                         [privacy.SESSION_RECORDS + server.session_id])
        self.assertTrue(asked[0]["url"].startswith("https://api.redpill.ai/v1/aci/"
                                                   "sessions/"))
        self.assertEqual(asked[0]["headers"], {}, "no key goes to RedPill")
        self.assertTrue(asked[0]["stream"], "read a piece at a time")
        # The next reply in the same session reads no record again; one not asked to
        # name its provider asks nothing of RedPill at all.
        again = client.check("chatcmpl-42", receipt_id="rcpt-1",
                             body_hash=stream_hash(server.stream()), name_upstream=True)
        self.assertEqual(again.upstream.name, "tinfoil")
        self.assertEqual(client.check_reply("chatcmpl-42", receipt_id="rcpt-1",
                                            body_hash=stream_hash(server.stream()))[0],
                         privacy.VERIFIED)
        self.assertIsNone(client.check("chatcmpl-42", receipt_id="rcpt-1").upstream)
        self.assertEqual(len(server.records_asked), 1)

    def test_a_provider_label_is_redpills_name_without_its_number(self):
        self.assertEqual(privacy.upstream_label(privacy.TeeUpstream(name="phala-244")),
                         "Phala")
        self.assertEqual(privacy.upstream_label(privacy.TeeUpstream(name="near-ai")),
                         "NEAR AI")
        self.assertEqual(privacy.upstream_label(privacy.TeeUpstream(name="somebody-new")),
                         "somebody-new")
        words = privacy.upstream_words(privacy.TeeUpstream(
            name="phala-244", scope="model_instance", gpus="asserted"))
        self.assertIn("checked the machine that ran the model; RedPill's verifier checked "
                      "its GPUs", words)

    def test_a_record_that_cannot_be_read_or_believed_names_nobody(self):
        """Never a failed reply: the state is the gateway's check, and stands."""
        for server, note in (
                ({"record_body": b'{"upstream_name": "chutes"}'},
                 "is not the one the receipt names"),
                ({"record_length": "3424330"}, "3,424 KB, more than is read for one reply"),
                ({"record_body": b" " * (privacy.SESSION_RECORD_MAX_BYTES + 1)},
                 "more than is read for one reply"),
                ({"record_status": 503}, "api.redpill.ai answered HTTP 503"),
                ({"record_body": b"not json"}, "RedPill's record could not be read"),
                # Nested past what Python reads: still a note, not a failed check.
                ({"record_body": b"[" * 100_000 + b"]" * 100_000},
                 "RedPill's record could not be read")):
            with self.subTest(note=note):
                privacy._records.clear()
                fake = FakeNanoGpt(aci="bound", receipts=True, record=TINFOIL, **server)
                _client, found = self.named(fake)
                self.assertEqual(found.state, privacy.VERIFIED)
                self.assertEqual(found.upstream.name, "")
                self.assertIn(note, found.upstream.note)
                self.assertEqual(privacy._records, {}, "nothing kept of it")
                words = privacy.upstream_words(found.upstream)
                self.assertTrue(words.startswith("The provider that ran it was not named: "))
                self.assertIn(note, words)

    def test_a_reply_served_outside_the_session_its_receipt_names_names_nobody(self):
        stale = dict(TINFOIL, expires_at=1_799_999_500)
        _client, found = self.named(FakeNanoGpt(aci="bound", receipts=True, record=stale))
        self.assertEqual(found.state, privacy.VERIFIED)
        self.assertIn("served outside the session", found.upstream.note)

    def test_a_model_chutes_serves_is_never_looked_up(self):
        """A Chutes instance has a session of its own, 3.4 MB for TEE/deepseek-v3.2, and
        replies spread over the instances: megabytes nearly every message."""
        instances = InstanceEvidence()
        server = ShapedNanoGpt(lambda nonce: (200, instances.body(nonce)), aci="bound",
                               receipts=True, record=TINFOIL)
        _client, found = self.named(server, model="TEE/glm-5.1")
        self.assertEqual(found.state, privacy.VERIFIED, "RedPill answered this one")
        self.assertEqual(found.upstream.name, "")
        self.assertIn("not looked up for a model Chutes serves", found.upstream.note)
        self.assertEqual(server.records_asked, [])

    def test_a_session_named_by_a_receipt_not_read_is_never_looked_up(self):
        server = FakeNanoGpt(tied="none", receipts=True, record=TINFOIL)
        _client, found = self.named(server)
        self.assertEqual(found.state, privacy.VERIFIED)
        self.assertIn("its receipt was not read under the attested keys", found.upstream.note)
        self.assertEqual(server.records_asked, [])

    def test_a_reply_short_of_signed_names_nobody_and_asks_nothing(self):
        for server in (FakeNanoGpt(aci="bound", receipts=True, record=TINFOIL, signer=1),
                       FakeNanoGpt(aci="bound", receipts=True, record=TINFOIL,
                                   signature_status=500)):
            _client, found = self.named(server)
            self.assertIn(found.state, (privacy.FAILED, privacy.UNCHECKED))
            self.assertIsNone(found.upstream)
            self.assertEqual(server.records_asked, [])

    def test_at_most_sixty_four_records_are_kept(self):
        class Records:
            def __init__(self):
                self.asked = 0

            def get(self, url, timeout=None, stream=False):
                self.asked += 1
                return FakeResponse(200, None, content=json.dumps(by_id[url[-64:]]).encode())

        by_id = {}
        for n in range(privacy.SESSION_RECORDS_KEPT + 1):
            record = dict(TINFOIL, upstream_name=f"phala-{n}")
            by_id[hashlib.sha256(canonical(record)).hexdigest()] = record
        ids, session = list(by_id), Records()
        for session_id in ids:
            privacy._session_record(session, session_id)
        self.assertEqual(len(privacy._records), privacy.SESSION_RECORDS_KEPT)
        self.assertNotIn(ids[0], privacy._records, "the oldest goes first")
        privacy._session_record(session, ids[-1])
        self.assertEqual(session.asked, len(ids), "a record kept is not read again")


# ---- each reply's record and receipt (Nov 2026) ---------------------------------------

@unittest.skipUnless(_has_cryptography(), "the cryptography package is not installed")
class Receipts(unittest.TestCase):
    """A reply's exact bytes, and RedPill's signed receipt for it (Nov 2026): nano-gpt as
    live, the quote binding the signing key, the keyset beside it listing that key as its
    e2ee key, and each reply's record carrying the receipt the keyset's key signed.
    Ported from DA Assistant's tests (Nov 2026), from SealedLore's."""

    def client(self, model=TEXT, **server):
        server = FakeNanoGpt(**dict({"tied": "e2ee", "receipts": True}, **server))
        client = server.tee_client(API_URL, "k", model)
        client.attest()
        return client, server

    def check(self, client, server, **record):
        record.setdefault("body_hash", stream_hash(server.stream()))
        return client.check_reply("chatcmpl-42", **record)

    def test_a_streamed_reply_is_checked_to_its_exact_bytes_and_its_receipt(self):
        client, server = self.client()
        self.assertIsNotNone(client.keyset, "the keyset holds the attested key")
        state, detail = self.check(client, server, receipt_id="rcpt-1")
        self.assertEqual(state, privacy.VERIFIED, detail)
        self.assertIn("RedPill's receipt for it holds", detail)
        self.assertIn("/tee/signature/rcpt-1", server.gets[-1])

    def test_bytes_other_than_those_signed_fail(self):
        client, server = self.client()
        self.assertEqual(self.check(client, server,
                                    body_hash=stream_hash(b"data: [DONE]\n\n"))[0],
                         privacy.FAILED)

    def test_a_model_whose_streams_nano_gpt_rebuilds_is_checked_by_signature_alone(self):
        for model in ("TEE/gemma-4-31b-it", "tee/gemma-4-31b-it",      # however written
                      "TEE/Gemma-4-31B-it"):
            client, server = self.client(model=model)
            state, detail = self.check(client, server, body_hash=stream_hash(b"rebuilt"))
            self.assertEqual(state, privacy.SIGNED, model)
            self.assertIn("nano-gpt rebuilds this model's stream", detail)

    def test_a_receipt_whose_upstream_was_not_verified_fails(self):
        client, server = self.client(upstream="failed")
        self.assertEqual(self.check(client, server), (privacy.FAILED,
                                                      "RedPill's receipt for it does not "
                                                      "hold"))

    def test_a_receipt_signed_by_another_key_fails(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        client, server = self.client(receipt_key=Ed25519PrivateKey.generate())
        self.assertEqual(self.check(client, server)[0], privacy.FAILED)

    def test_a_receipt_under_keys_renewed_since_is_unchecked_not_failed(self):
        client, server = self.client(digest="sha256:" + "0" * 64)
        state, detail = self.check(client, server)
        self.assertEqual(state, privacy.UNCHECKED)
        self.assertIn("keys changed", detail)

    def test_a_keyset_the_quote_binds_itself_is_taken_and_one_tied_neither_way_is_not(self):
        bound, _server = self.client(tied="statement")
        self.assertIsNotNone(bound.keyset)
        self.assertEqual(bound.attest().shortfalls, [privacy.GPUS_LOOSE],
                         "RedPill's own form binds the receipt keys")
        loose, server = self.client(tied="none")
        self.assertIsNone(loose.keyset)
        # Still signed by the attested key, for these bytes: the receipt goes unread.
        state, detail = self.check(loose, server)
        self.assertEqual(state, privacy.VERIFIED)
        self.assertNotIn("receipt", detail)

    def test_the_receipt_keys_are_named_short_whether_a_keyset_comes_or_not(self):
        """Leaving the keyset out must never read better than sending it loose."""
        with_keys, _server = self.client()
        self.assertIsNotNone(with_keys.keyset)
        self.assertIn(privacy.RECEIPT_KEYS_LOOSE, with_keys.attest().shortfalls)
        without, _server = self.client(tied=None, receipts=False)
        self.assertIsNone(without.keyset)
        self.assertIn(privacy.RECEIPT_KEYS_LOOSE, without.attest().shortfalls)

    def test_receipt_keys_with_no_time_to_hold_them_to_are_not_used(self):
        client, _server = self.client(tied="statement", not_after=None)
        self.assertIsNone(client.keyset)
        self.assertEqual(client.attest().shortfalls,
                         [privacy.GPUS_LOOSE, "receipt keys expired"])

    def test_a_stream_just_ended_waits_for_its_receipt(self):
        with mock.patch.object(privacy, "RECEIPT_RETRY_SECONDS", 0):
            client, server = self.client(not_ready=2)
            state, detail = self.check(client, server, receipt_id="rcpt-1", route="r1")
        self.assertEqual(state, privacy.VERIFIED, detail)
        self.assertEqual(len(server.lookups), 3)
        self.assertTrue(all(headers.get("X-TEE-Verification-Route") == "r1"
                            for headers in server.lookups))

    def test_a_reply_that_named_no_receipt_is_looked_up_once(self):
        """Live, a reply that named none never had one to wait for."""
        with mock.patch.object(privacy, "RECEIPT_RETRY_SECONDS", 0):
            client, server = self.client(not_ready=1)
            state, _detail = self.check(client, server)
        self.assertEqual(state, privacy.UNCHECKED)
        self.assertEqual(len(server.lookups), 1)

    def test_only_a_record_not_ready_yet_is_waited_for(self):
        with mock.patch.object(privacy, "RECEIPT_RETRY_SECONDS", 0):
            client, server = self.client(signature_status=500)
            state, _detail = self.check(client, server, receipt_id="rcpt-1")
        self.assertEqual(state, privacy.UNCHECKED)
        self.assertEqual(len(server.lookups), 1)

    def test_the_wait_for_a_receipt_has_an_end(self):
        """A receipt that never comes is waited for RECEIPT_WAIT_SECONDS, no longer:
        here a clock that moves 5 s at each look at it."""
        client, server = self.client(not_ready=100)
        clock = iter(range(0, 1000, 5))
        with mock.patch.object(privacy, "RECEIPT_RETRY_SECONDS", 0), \
                mock.patch.object(privacy.time, "monotonic", lambda: next(clock)):
            state, _detail = self.check(client, server, receipt_id="rcpt-1")
        self.assertEqual(state, privacy.UNCHECKED)
        self.assertEqual(len(server.lookups), privacy.RECEIPT_WAIT_SECONDS // 5)


class ReplyMarks(unittest.TestCase):
    """What each reply's check is called where the app shows it: after Test in
    Settings, under Detect Chapters' answer, and in Help → What Was Sent."""

    def test_receipts_read_as_what_they_prove(self):
        def label(state):
            return privacy.receipt_label(privacy.Receipt(TEXT, privacy.TEE, state))
        self.assertEqual(label(privacy.VERIFIED), ("✓ TEE, verified", privacy.OK))
        self.assertEqual(label(privacy.SIGNED),
                         ("TEE, signed, text not compared", privacy.WARN))
        self.assertEqual(label(privacy.FAILED), ("⚠ TEE, check failed", privacy.BAD))
        self.assertEqual(label(privacy.UNCHECKED), ("⚠ TEE, not verified", privacy.WARN))
        self.assertEqual(privacy.receipt_label(privacy.Receipt(
            "private/x", privacy.E2EE, privacy.SEALED)), ("✓ end-to-end encrypted",
                                                         privacy.OK))
        self.assertEqual(privacy.receipt_label(None), ("", privacy.OK))

    def test_a_replys_mark_says_what_is_the_gateways_word_and_what_passed_in_the_clear(
            self):
        """As SealedLore's marks say it (TEE_MARK_TIPS, 9af15c1): for a model RedPill
        serves, the key that signed is its gateway's, and the machine that runs the
        model is the gateway's word; the text passed nano-gpt in the clear; a private/
        model is end-to-end encrypted instead. Ported from DA Assistant's (1a99042)."""
        def tip(state, detail="d", **kwargs):
            return privacy.receipt_tip(privacy.Receipt(TEXT, privacy.TEE, state, detail,
                                                       **kwargs))

        verified = tip(privacy.VERIFIED, "signed by the attested key 0xab, for the exact "
                                         "bytes that came")
        self.assertTrue(verified.startswith("TEE verified: signed by the attested key 0xab"))
        self.assertIn("the attested key is RedPill's gateway's, which passed the request on "
                      "to the machine that runs the model: that machine is the gateway's "
                      "word, in its signed receipt, not checked by this app", verified)
        self.assertIn("What was sent is not covered", verified)
        self.assertIn("The text passed nano-gpt's servers in the clear; a private/ model is "
                      "end-to-end encrypted instead.", verified)
        signed = tip(privacy.SIGNED)
        self.assertIn("A change on its way here would not show", signed)
        self.assertIn("the gateway's word", signed)
        unchecked = tip(privacy.UNCHECKED, "this model's provider signs no replies")
        self.assertTrue(unchecked.startswith("Not verified: this model's provider signs no "
                                             "replies."))
        self.assertIn("Nothing this app could check shows which machine answered it: "
                      "nano-gpt routes each request, and a model may be served elsewhere "
                      "when its provider is busy.", unchecked)
        self.assertIn("in the clear", unchecked)
        self.assertNotIn("Strict", unchecked)
        self.assertIn("Strict is on, so its model is attested again before anything more "
                      "is sent to it", tip(privacy.UNCHECKED, doubted=True))
        self.assertIn("This reply does not match its record", tip(privacy.FAILED))
        sealed_one = privacy.receipt_tip(privacy.Receipt(
            "private/x", privacy.E2EE, privacy.SEALED, "decrypted here"))
        self.assertEqual(sealed_one, "End-to-end encrypted: decrypted here. nano-gpt "
                                     "relayed ciphertext.")
        self.assertEqual(privacy.receipt_tip(None), "")

    def test_a_reply_strict_doubted_says_so_and_no_other_does(self):
        server = GpusTied(tied="statement", signer=1)
        strict = PrivacySession(API_URL, "k", TEXT, tee_factory=server.tee_client,
                                strict=True)
        strict.ensure()
        failed = strict.check_reply("chatcmpl-1", body_hash=stream_hash(server.stream()))
        self.assertEqual(failed.state, privacy.FAILED)
        self.assertTrue(failed.doubted)
        self.assertIn("Strict is on", privacy.receipt_tip(failed))
        relaxed = PrivacySession(API_URL, "k", TEXT, tee_factory=server.tee_client)
        relaxed.ensure()
        self.assertFalse(relaxed.check_reply("chatcmpl-1").doubted)

    def test_what_was_sent_is_received_only_as_far_as_its_replys_check_shows(self):
        """What Was Sent said a TEE request went "to the model's attested enclave"
        whatever its reply showed: nano-gpt routes each request, Chutes signs no
        replies, and a model may be served elsewhere. Only a record the attested key
        signed shows the attested enclave received it (SealedLore 9af15c1, by way of DA
        Assistant's 4f8bbce)."""
        def sent(state):
            return privacy.sent_label(privacy.Receipt(TEXT, privacy.TEE, state))
        for state in (privacy.VERIFIED, privacy.SIGNED):
            text, tone, tip = sent(state)
            self.assertEqual((text, tone), ("received by the attested TEE", privacy.OK))
            self.assertIn("RedPill's gateway, which passed it on", tip)
            self.assertIn("in the clear", tip)
        for state in (privacy.UNCHECKED, privacy.FAILED):
            text, tone, tip = sent(state)
            self.assertEqual((text, tone), ("⚠ not shown to reach the attested enclave",
                                            privacy.WARN))
            self.assertIn("no signed record of its reply shows the attested enclave "
                          "received it", tip)
        self.assertEqual(privacy.sent_label(None)[0], privacy.UNPROVEN)
        self.assertNotIn("igned", sent(privacy.SIGNED)[0], "signed is the reply's word")
