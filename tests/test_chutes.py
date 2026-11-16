# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Chutes (assistant/chutes.py): its end-to-end encrypted models, the whole flow offline -
a fake api.chutes.ai and llm.chutes.ai answering as Chutes' code does, its instances keys
of our own (an ML-KEM-768 key, a TLS key bound into a quote with the registers of a
published set), Intel's and NVIDIA's checks stood in for: discovery, attestation and its
cache, the nonces, the sealed request and its streamed reply, and every refusal. And its
ordinary models' list. Ported from SealedLore's tests/test_chutes_provider.py."""

import base64
import gzip
import hashlib
import json
import secrets
import struct
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from mediabrowser.core import chutes, chutes_e2ee, dcap, nras, safejson  # noqa: E402
from mediabrowser.core import private_ai as privacy
from mediabrowser.core.chutes import (  # noqa: E402
    EVIDENCE_LIMIT,
    ChutesClient,
    ChutesState,
    NotOffered,
)
from mediabrowser.core.chutes_e2ee import (  # noqa: E402
    INFO_REQUEST,
    INFO_STREAM,
    derive_key,
    stream_nonce,
)
from mediabrowser.core.netguard import Headers as CaseInsensitiveDict  # noqa: E402
from mediabrowser.core.private_ai import (  # noqa: E402
    MEASUREMENTS_LISTED,
    NRAS_URL,
    WORKLOAD_UNMEASURED,
    PrivacyError,
    shortfall_tips,
    what_was_attested,
)
from tests.shared_core import LLMClient  # noqa: E402

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import mlkem, padding, rsa
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    HAVE_MLKEM = chutes_e2ee.mlkem_available()
except ImportError:
    HAVE_MLKEM = False

CHUTE = "9ecb73cc-909b-5334-9cd8-74985945cff5"
MODEL = "test/Model-TEE"
KEY = "cpk_test"
MRTD = bytes(range(48))
RTMRS = tuple(bytes([i]) * 48 for i in (1, 2, 3, 4))
UP_TO_DATE = dcap.Result(status="UpToDate", platform="UpToDate", qe="UpToDate",
                         notes=["ok"])
REPLY = {"id": "chatcmpl-7",
         "choices": [{"message": {"role": "assistant", "content": "The door opens."},
                      "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 40, "completion_tokens": 5}}


def tdx_quote(report_data, mrtd=MRTD, rtmrs=RTMRS):
    """A version 4 TDX quote with its registers and report data in place and nothing
    signed (Intel's check is stood in for)."""
    header = (struct.pack("<HHI", 4, dcap.ECDSA_P256, dcap.TDX_TEE_TYPE) + bytes(4)
              + dcap.INTEL_QE_VENDOR_ID + bytes(20))
    body = bytearray(584)
    body[136:184] = mrtd
    for offset, value in zip((328, 376, 424, 472), rtmrs):
        body[offset:offset + 48] = value
    body[520:584] = report_data
    inner = struct.pack("<HI", dcap.PCK_CHAIN, 0)
    cert = bytes(dcap.QE_REPORT_LEN) + bytes(64) + struct.pack("<H", 0) + inner
    signature = bytes(128) + struct.pack("<HI", dcap.QE_REPORT_DATA, len(cert)) + cert
    return header + bytes(body) + struct.pack("<I", len(signature)) + signature


class FakeInstance:
    """An instance: its ML-KEM key, its TLS key and certificate, its registers."""

    tls = None

    def __init__(self, mrtd=MRTD, binds=None, gpus=True):
        if FakeInstance.tls is None:
            FakeInstance.tls = rsa.generate_private_key(public_exponent=65537,
                                                        key_size=2048)
        self.id = str(uuid.uuid4())
        self.key = mlkem.MLKEM768PrivateKey.generate()
        self.pubkey = base64.b64encode(self.key.public_key().public_bytes_raw()).decode()
        self.mrtd = mrtd
        self.binds = binds
        self.gpus = gpus
        self.signer = None
        name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "sek8s")])
        self.cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                     .public_key(self.tls.public_key()).serial_number(1)
                     .not_valid_before(datetime(2026, 1, 1, tzinfo=UTC))
                     .not_valid_after(datetime(2027, 1, 1, tzinfo=UTC))
                     .sign(self.tls, hashes.SHA256()))

    def evidence(self, nonce):
        spki = self.cert.public_key().public_bytes(Encoding.DER,
                                                   PublicFormat.SubjectPublicKeyInfo)
        bound = hashlib.sha256((nonce + (self.binds or self.pubkey)).encode()).digest()
        quote = tdx_quote(bound + hashlib.sha256(spki).digest(), self.mrtd)
        gpu = [{"arch": "HOPPER", "evidence": "e", "certificate": "c"}] if self.gpus else []
        attested = json.dumps({"nonce": nonce, "evidence": {
            "quote": base64.b64encode(quote).decode(),
            "nvtrust_evidence": json.dumps(gpu)}}).encode()
        signature = (self.signer or self.tls).sign(attested, padding.PKCS1v15(),
                                                   hashes.SHA256())
        return {"tee_type": "tdx", "instance_id": self.id,
                "quote": base64.b64encode(quote).decode(),
                "certificate": base64.b64encode(self.cert.public_bytes(Encoding.DER)).decode(),
                "signature": base64.b64encode(signature).decode(),
                "attested_body": base64.b64encode(attested).decode()}

    def open(self, blob):
        ciphertext, nonce, sealed = blob[:1088], blob[1088:1100], blob[1100:]
        key = derive_key(self.key.decapsulate(ciphertext), ciphertext, INFO_REQUEST)
        return json.loads(gzip.decompress(ChaCha20Poly1305(key).decrypt(nonce, sealed,
                                                                        None)))


def stream_pieces(reply):
    """`reply` as the pieces of the model server's stream, each one SSE event of its own,
    the [DONE] and then the blank line after it sealed on its own (as live)."""
    rid = reply.get("id", "chatcmpl-x")
    choice = reply["choices"][0]
    words = choice["message"]["content"].split(" ")
    chunks = [{"delta": {"content": word + (" " if i < len(words) - 1 else "")}}
              for i, word in enumerate(words) if word]
    chunks.append({"delta": {}, "finish_reason": choice["finish_reason"]})
    events = [{"id": rid, "choices": [{"index": 0, **c}]} for c in chunks]
    events.append({"id": rid, "choices": [], "usage": reply["usage"]})
    return ["data: " + json.dumps(event) + "\n\n" for event in events] + \
        ["data: [DONE]\n", "\n"]


def sealed_stream(pieces, response_pk, usage=None):
    client = mlkem.MLKEM768PublicKey.from_public_bytes(base64.b64decode(response_pk))
    secret, ciphertext = client.encapsulate()
    key = derive_key(secret, ciphertext, INFO_STREAM)
    lines = ["data: " + json.dumps({"e2e_init": base64.b64encode(ciphertext).decode()}), ""]
    for number, piece in enumerate(pieces):
        nonce = stream_nonce(number)
        sealed = nonce + ChaCha20Poly1305(key).encrypt(nonce, piece.encode(), None)
        lines += ["data: " + json.dumps({"e2e": base64.b64encode(sealed).decode()}), ""]
    if usage:
        lines += ["data: " + json.dumps({"usage": usage}), ""]
    return [*lines, "data: [DONE]", ""]


class Response:
    """What a requests Session gives back, as much as the client reads."""

    def __init__(self, status, body=b"", headers=None):
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers or {})
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.closed = False

    def json(self):
        return safejson.loads(self.content)

    def iter_content(self, chunk_size=512):
        for at in range(0, len(self.content), 7):       # a few bytes at a time
            yield self.content[at:at + 7]

    def close(self):
        self.closed = True


class Asked:
    def __init__(self, method, url, params, headers, data):
        self.method, self.url, self.headers, self.data = method, url, \
            CaseInsensitiveDict(headers or {}), data
        parsed = urlparse(url)
        self.host, self.path = parsed.hostname, parsed.path
        self.params = dict(params or {})
        self.params.update({k: v[0] for k, v in parse_qs(parsed.query).items()})


class FakeChutes:
    """api.chutes.ai and llm.chutes.ai as Chutes' code answers, as a requests-like
    Session, every request recorded. Also NVIDIA's NRAS, for the checks' session."""

    def __init__(self, count=3):
        self.instances = [FakeInstance() for _ in range(count)]
        self.requests = []
        self.issued = {}
        self.used = []
        self.opened = []
        self.reply = REPLY
        self.evidence_status = []
        self.invoke_status = []
        self.invoke_body = None
        self.spoil = None
        self.redirect = None
        self.measured = [MRTD]
        self.nonce_life = 60

    # the Session's face
    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True,
            stream=False):
        return self.request("GET", url, params=params, headers=headers)

    def post(self, url, json=None, data=None, headers=None, timeout=None, stream=False,
             allow_redirects=True):
        return self.request("POST", url, data=data if json is None
                            else __import__("json").dumps(json).encode(), headers=headers)

    def request(self, method, url, params=None, data=None, headers=None, timeout=None,
                stream=False, allow_redirects=True):
        asked = Asked(method, url, params, headers, data)
        self.requests.append(asked)
        return self.answer(asked)

    def close(self):
        pass

    def answer(self, asked):
        if asked.url.startswith(NRAS_URL):
            return Response(200, [["JWT", "a.b.c"], {}])
        if asked.host == "llm.chutes.ai" and asked.path == "/v1/models":
            return Response(200, self.models())
        if asked.host != "api.chutes.ai":
            return Response(404)
        if self.redirect and asked.path == self.redirect:
            return Response(307, headers={"location": "https://elsewhere.example/steal"})
        if asked.path == "/servers/tdx/measurements":
            return Response(200, [{"version": "1.5.0", "name": f"set {i}",
                                   "mrtd": m.hex().upper(),
                                   **{f"rtmr{j}": r.hex().upper()
                                      for j, r in enumerate(RTMRS)}}
                                  for i, m in enumerate(self.measured)])
        if asked.path == f"/chutes/{CHUTE}/evidence":
            if self.evidence_status:
                return Response(self.evidence_status.pop(0), {"detail": "no"},
                                {"retry-after": "1"})
            nonce = asked.params["nonce"]
            return Response(200, {"evidence": [i.evidence(nonce) for i in self.instances],
                                  "failed_instance_ids": [], "excluded_instance_ids": []})
        if asked.path.startswith("/instances/") and asked.path.endswith("/evidence"):
            if asked.headers.get("authorization") != f"Bearer {KEY}":
                return Response(401, {"detail": "no key"})
            instance = next(i for i in self.instances if i.id == asked.path.split("/")[2])
            return Response(200, instance.evidence(asked.params["nonce"]))
        if asked.path == f"/e2e/instances/{CHUTE}":
            if asked.headers.get("authorization") != f"Bearer {KEY}":
                return Response(401, {"detail": "Can't find a user with that api key"})
            listed = []
            for instance in self.instances[:5]:
                tokens = [secrets.token_urlsafe(24) for _ in range(10)]
                self.issued.update({t: instance.id for t in tokens})
                listed.append({"instance_id": instance.id, "e2e_pubkey": instance.pubkey,
                               "nonces": tokens})
            return Response(200, {"instances": listed, "nonce_expires_in": self.nonce_life})
        if asked.path == "/e2e/invoke" and asked.method == "POST":
            return self.invoke(asked)
        return Response(404)

    def invoke(self, asked):
        headers = asked.headers
        token, instance_id = headers["x-e2e-nonce"], headers["x-instance-id"]
        if self.issued.get(token) != instance_id or token in self.used:
            return Response(403, {"detail": "Invalid, expired, or already-used nonce"})
        self.used.append(token)
        if self.invoke_status:
            return Response(self.invoke_status.pop(0), {"detail": "Instance\u202e busy\nnow"})
        assert headers["authorization"] == f"Bearer {KEY}"
        assert (headers["x-chute-id"], headers["x-e2e-stream"]) == (CHUTE, "true")
        assert headers["x-e2e-path"] == "/v1/chat/completions"
        if self.invoke_body is not None:
            return Response(200, self.invoke_body)
        instance = next(i for i in self.instances if i.id == instance_id)
        body = instance.open(asked.data)
        self.opened.append((instance_id, body))
        lines = sealed_stream(stream_pieces(self.reply), body["e2e_response_pk"],
                              usage=self.reply.get("usage"))
        if self.spoil is not None:
            lines = self.spoil(lines)
        return Response(200, "\n".join(lines).encode())

    @staticmethod
    def models():
        return {"data": [{"id": MODEL, "chute_id": CHUTE, "confidential_compute": True,
                          "pricing": {"prompt": 0.2, "completion": 1.0},
                          "context_length": 131072},
                         {"id": "plain/Model", "chute_id": str(uuid.uuid4()),
                          "confidential_compute": False}]}

    def paths(self, host="api.chutes.ai"):
        return [r.path for r in self.requests if r.host == host]

    def count(self, path):
        return sum(1 for r in self.requests if r.path == path)


def client(fake, clock=None, url="https://llm.chutes.ai/v1", key=KEY,
           saved_for="https://llm.chutes.ai:443"):
    return ChutesClient(url, key, key_saved_for=saved_for, session=fake,
                        checks_session=fake, intel=lambda raw, session: UP_TO_DATE,
                        nvidia=lambda answer, nonce, session: 1,
                        state=ChutesState(clock) if clock is not None else None)


PAYLOAD = {"model": MODEL, "stream": True, "max_tokens": 300,
           "messages": [{"role": "system", "content": "Be brief."},
                        {"role": "user", "content": "Open the door."}],
           "reasoning": {"effort": "high"}}


def ask(chutes_client, payload=PAYLOAD):
    """One request through the client, read as the app reads any stream."""
    with chutes_client.request(MODEL, payload) as stream:
        result = LLMClient._consume(chutes_client.lines(stream), None, None, [])
        chutes_client.finish(result["finish_reason"], result["done"])
        return result, stream


@unittest.skipUnless(HAVE_MLKEM, "this cryptography has no ML-KEM-768")
class SealedToAnAttestedInstance(unittest.TestCase):
    def setUp(self):
        self.fake = FakeChutes()

    def test_a_request_is_sealed_to_an_attested_instance_and_its_reply_opened(self):
        chutes_client = client(self.fake)
        result, stream = ask(chutes_client)
        self.assertEqual(result["text"], "The door opens.")
        self.assertEqual((result["finish_reason"], result["done"]), ("stop", True))
        self.assertEqual(stream.usage, REPLY["usage"])
        instance_id, body = self.fake.opened[0]
        self.assertEqual(instance_id, chutes_client.last_instance)
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["messages"], PAYLOAD["messages"])
        self.assertTrue(body["stream"])
        self.assertNotIn("reasoning", body)
        self.assertNotIn("chat_template_kwargs", body)

    def test_the_lowest_reasoning_switches_thinking_off_through_the_chat_template(self):
        for effort in ("none", "minimal", "low"):
            body = ChutesClient.body_for(MODEL, {**PAYLOAD, "reasoning": {"effort": effort}})
            self.assertEqual(body["chat_template_kwargs"],
                             {"enable_thinking": False, "thinking": False})
        self.assertNotIn("chat_template_kwargs",
                         ChutesClient.body_for(MODEL, {**PAYLOAD,
                                                       "reasoning": {"effort": "medium"}}))

    def test_the_record_is_partial_always_and_says_what_was_attested(self):
        attestation = client(self.fake).attest(f"Chutes::{MODEL}")
        self.assertEqual((attestation.kind, attestation.service, attestation.level),
                         (privacy.E2EE, "chutes", "partial"))
        self.assertIn(WORKLOAD_UNMEASURED, attestation.shortfalls)
        self.assertIn(MEASUREMENTS_LISTED, attestation.shortfalls)
        self.assertEqual(attestation.instances, 3)
        self.assertTrue(attestation.headline.startswith("End-to-end encrypted ("))
        words = what_was_attested(attestation)
        self.assertIn("each of 3 Chutes instances", words)
        self.assertIn("chutes-aegis.so", words)
        tips = shortfall_tips(attestation)
        self.assertTrue(any("closed binary" in tip for tip in tips))
        self.assertTrue(any("rebuilt" in tip for tip in tips))

    def test_a_nonce_is_used_once_and_never_again(self):
        chutes_client = client(self.fake)
        for _ in range(3):
            ask(chutes_client)
        self.assertEqual(len(self.fake.used), 3)
        self.assertEqual(len(set(self.fake.used)), 3)
        self.assertEqual(self.fake.count(f"/e2e/instances/{CHUTE}"), 1)

    def test_an_instance_attested_fifteen_minutes_ago_is_attested_again(self):
        now = [1000.0]
        chutes_client = client(self.fake, clock=lambda: now[0])
        ask(chutes_client)
        ask(chutes_client)
        self.assertEqual(self.fake.count(f"/chutes/{CHUTE}/evidence"), 1)
        now[0] += chutes.REATTEST_SECONDS + 1
        ask(chutes_client)
        self.assertEqual(self.fake.count(f"/chutes/{CHUTE}/evidence"), 2)

    def test_an_instance_first_seen_since_the_chutes_evidence_is_attested_alone(self):
        chutes_client = client(self.fake)
        ask(chutes_client)
        newcomer = FakeInstance()
        self.fake.instances.append(newcomer)
        chutes_client.state.pools.clear()       # discovery's nonces ran out
        ask(chutes_client)
        self.assertEqual(self.fake.count(f"/chutes/{CHUTE}/evidence"), 1)
        self.assertEqual(self.fake.count(f"/instances/{newcomer.id}/evidence"), 1)
        alone = next(r for r in self.fake.requests
                     if r.path == f"/instances/{newcomer.id}/evidence")
        self.assertEqual(alone.headers["authorization"], f"Bearer {KEY}")

    def test_an_instance_that_fails_is_left_out_said_and_never_sealed_to(self):
        for spoil, why in ((lambda i: setattr(i, "mrtd", bytes(48)), "measurements"),
                           (lambda i: setattr(i, "binds", FakeInstance().pubkey),
                            "does not bind the key"),
                           (lambda i: setattr(i, "gpus", False), "no GPU evidence"),
                           (lambda i: setattr(i, "signer", rsa.generate_private_key(
                               public_exponent=65537, key_size=2048)), "did not sign")):
            with self.subTest(why=why):
                fake = FakeChutes()
                bad = fake.instances[0]
                spoil(bad)
                chutes_client = client(fake)
                attestation = chutes_client.attest(MODEL)
                self.assertEqual(attestation.instances, 2)
                self.assertIn(why, attestation.detail)
                self.assertIn(f"left out, never sealed to: {bad.id[:8]}", attestation.detail)
                for _ in range(6):
                    ask(chutes_client)
                self.assertNotIn(bad.id, {instance for instance, _body in fake.opened})

    def test_with_no_instance_left_the_model_is_refused_and_nothing_sent(self):
        self.fake.measured = [bytes(48)]
        with self.assertRaisesRegex(PrivacyError, "none of the instances"):
            ask(client(self.fake))
        self.assertEqual(self.fake.count("/e2e/invoke"), 0)

    def test_chutes_evidence_limit_is_waited_out_within_the_attempt(self):
        self.fake.evidence_status = [429]
        result, _stream = ask(client(self.fake))
        self.assertEqual(result["text"], "The door opens.")
        self.assertEqual(self.fake.count(f"/chutes/{CHUTE}/evidence"), 2)

    def test_chutes_evidence_limit_past_the_attempt_refuses_in_words(self):
        self.fake.evidence_status = [429] * 5
        with mock.patch.object(chutes, "ATTEMPT_SECONDS", 1.5), \
                self.assertRaises(PrivacyError) as caught:
            ask(client(self.fake))
        self.assertIn(EVIDENCE_LIMIT, str(caught.exception))
        self.assertEqual(self.fake.count("/e2e/invoke"), 0)

    def test_a_reply_whose_seal_does_not_hold_is_refused_and_doubts_the_attestation(self):
        def tamper(lines):
            first = next(i for i, line in enumerate(lines) if '"e2e"' in line)
            raw = bytearray(base64.b64decode(json.loads(lines[first][6:])["e2e"]))
            raw[-1] ^= 1
            lines[first] = "data: " + json.dumps({"e2e": base64.b64encode(raw).decode()})
            return lines

        self.fake.spoil = tamper
        chutes_client = client(self.fake)
        with self.assertRaisesRegex(PrivacyError, "attested again"):
            ask(chutes_client)
        self.assertEqual(chutes_client.state.instances, {})
        self.fake.spoil = None
        ask(chutes_client)
        self.assertEqual(self.fake.count(f"/chutes/{CHUTE}/evidence"), 2)

    def test_an_unsealed_reply_is_never_taken_for_the_models(self):
        for body in (b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n\n'
                     b'data: [DONE]\n\n',
                     json.dumps(REPLY).encode()):
            with self.subTest(body=body[:20]):
                self.fake.invoke_body = body
                shown = []
                with self.assertRaises(PrivacyError):
                    with client(self.fake).request(MODEL, PAYLOAD) as stream:
                        LLMClient._consume(client(self.fake).lines(stream), None,
                                           lambda piece, text: shown.append(piece), [])
                self.assertEqual(shown, [])

    def test_a_piece_out_of_its_place_or_a_stream_cut_short_is_refused_in_words(self):
        def swapped(lines):
            sealed = [i for i, line in enumerate(lines) if '"e2e"' in line]
            lines[sealed[1]], lines[sealed[2]] = lines[sealed[2]], lines[sealed[1]]
            return lines

        def cut(lines):
            sealed = [i for i, line in enumerate(lines) if '"e2e"' in line]
            return lines[:sealed[2]]

        for spoil, why in ((swapped, "not the one that comes next"),
                           (cut, "ended before the model said it was done")):
            with self.subTest(why=why):
                fake = FakeChutes()
                fake.spoil = spoil
                shown = []
                chutes_client = client(fake)
                with self.assertRaisesRegex(PrivacyError, why):
                    with chutes_client.request(MODEL, PAYLOAD) as stream:
                        LLMClient._consume(chutes_client.lines(stream), None,
                                           lambda piece, text: shown.append(piece), [])
                self.assertNotIn("opens.", "".join(shown))

    def test_a_refusal_is_said_as_chutes_unauthenticated_words(self):
        self.fake.invoke_status = [500]
        with self.assertRaises(PrivacyError) as caught:
            ask(client(self.fake))
        self.assertIn("Chutes said (unauthenticated): Instance busy now", str(caught.exception))
        self.assertEqual(self.fake.count("/e2e/invoke"), 1)

    def test_a_nonce_chutes_refuses_sends_the_next_request_to_discovery(self):
        chutes_client = client(self.fake)
        ask(chutes_client)
        self.fake.used.extend(self.fake.issued)       # every nonce issued, spent
        with self.assertRaisesRegex(PrivacyError, "HTTP 403"):
            ask(chutes_client)
        ask(chutes_client)
        self.assertEqual(self.fake.count(f"/e2e/instances/{CHUTE}"), 2)

    def test_the_key_goes_to_api_chutes_ai_alone(self):
        ask(client(self.fake))
        for asked in self.fake.requests:
            keyed = "authorization" in asked.headers
            self.assertEqual(keyed, asked.host == "api.chutes.ai"
                             and (asked.path.startswith("/e2e/")
                                  or asked.path.startswith("/instances/")), asked.url)

    def test_no_redirect_is_followed(self):
        for where in (f"/e2e/instances/{CHUTE}", f"/chutes/{CHUTE}/evidence",
                      "/e2e/invoke", "/servers/tdx/measurements"):
            with self.subTest(where=where):
                fake = FakeChutes()
                fake.redirect = where
                with self.assertRaisesRegex(PrivacyError, "redirected|not followed"):
                    ask(client(fake))
                self.assertFalse(any(r.host == "elsewhere.example" for r in fake.requests))

    def test_nothing_is_fetched_or_sent_without_a_key_that_may_go_to_chutes(self):
        for settings, why in (({"key": ""}, "need a Chutes API key"),
                              ({"url": "https://chutes.example/v1"},
                               "not one of Chutes' own addresses"),
                              ({"url": "http://llm.chutes.ai/v1"},
                               "not one of Chutes' own addresses"),
                              ({"saved_for": "https://nano-gpt.com:443"},
                               "saved for https://nano-gpt.com:443")):
            with self.subTest(why=why):
                fake = FakeChutes()
                with self.assertRaisesRegex(PrivacyError, why):
                    client(fake, **settings).attest(MODEL)
                self.assertEqual(fake.paths(), [])

    def test_a_key_saved_for_any_of_chutes_three_origins_goes(self):
        for url in ("https://llm.chutes.ai/v1", "https://claude.chutes.ai",
                    "https://api.chutes.ai"):
            for saved in services_origins():
                with self.subTest(url=url, saved=saved):
                    client(FakeChutes(), url=url, saved_for=saved).attest(MODEL)

    def test_a_model_chutes_does_not_list_as_confidential_is_never_sent(self):
        for model in ("plain/Model", f"{MODEL}:THINKING", "nobody/Else-TEE"):
            with self.subTest(model=model):
                fake = FakeChutes()
                with self.assertRaises(NotOffered):
                    client(fake).request(model, PAYLOAD).__enter__()
                self.assertEqual(fake.count("/e2e/invoke"), 0)

    def test_without_ml_kem_nothing_is_sent(self):
        with mock.patch.object(chutes, "mlkem_refusal", lambda: "no ML-KEM here"):
            with self.assertRaisesRegex(PrivacyError, "no ML-KEM here"):
                ask(client(self.fake))
            with self.assertRaisesRegex(PrivacyError, "no ML-KEM here"):
                client(self.fake).attest(MODEL)
        self.assertEqual(self.fake.requests, [])

    def test_the_models_own_error_inside_the_seal_is_said(self):
        # The model server's error, sealed, is said as its own.
        original = self.fake.invoke

        def invoke(asked):
            instance = next(i for i in self.fake.instances
                            if i.id == asked.headers["x-instance-id"])
            self.fake.used.append(asked.headers["x-e2e-nonce"])
            body = instance.open(asked.data)
            return Response(200, "\n".join(sealed_stream(
                ['data: {"error": {"message": "too long"}}\n\n'],
                body["e2e_response_pk"])).encode())

        self.fake.invoke = invoke
        with self.assertRaisesRegex(PrivacyError, "model's server reported an error: too "
                                                  "long"):
            ask(client(self.fake))
        self.fake.invoke = original

    def test_a_discovered_key_that_is_not_one_is_never_sealed_to(self):
        broken = self.fake.instances[0]
        broken.pubkey = base64.b64encode(b"\0" * 10).decode()
        chutes_client = client(self.fake)
        attestation = chutes_client.attest(MODEL)
        self.assertEqual(attestation.instances, 2)
        for _ in range(4):
            ask(chutes_client)
        self.assertNotIn(broken.id, {instance for instance, _body in self.fake.opened})

    def test_the_keyless_check_says_the_key_binding_is_unchecked(self):
        fake = FakeChutes()
        attestation = client(fake, key="").attest_public(MODEL)
        self.assertIn(privacy.KEY_BINDING_UNCHECKED, attestation.shortfalls)
        self.assertFalse(any("authorization" in r.headers for r in fake.requests))
        self.assertEqual(fake.count("/e2e/invoke"), 0)


def services_origins():
    from mediabrowser.core import services
    return sorted(services.CHUTES_KEY_ORIGINS)


class TheModelLists(unittest.TestCase):
    def test_an_ordinary_chutes_endpoint_lists_every_model_keyless(self):
        fake = FakeChutes.__new__(FakeChutes)
        fake.requests = []
        fake.redirect = None
        found = chutes.fetch_models(fake)
        self.assertEqual(set(found), {MODEL, "plain/Model"})
        self.assertTrue(found[MODEL]["confidential_compute"])
        self.assertFalse(found["plain/Model"]["confidential_compute"])
        self.assertFalse(any("authorization" in r.headers for r in fake.requests))

    def test_the_end_to_end_encrypted_list_is_the_confidential_models_alone(self):
        fake = FakeChutes.__new__(FakeChutes)
        fake.requests = []
        fake.redirect = None
        self.assertEqual(set(ChutesClient("https://llm.chutes.ai/v1", KEY,
                                          session=fake).models()), {MODEL})


REAL = Path(__file__).parent / "data" / "chutes_evidence.json"


class RealChecks:
    """Intel's collateral, NVIDIA's answers (by the nonce each was asked for) and keys,
    exactly as they came at the capture of tests/data/chutes_evidence.json, as a
    requests-like Session."""

    def __init__(self, fixture, chutes_answers=None):
        self.fixture = fixture
        self.answers = {item["nonce"]: item["answer"] for item in fixture["nras"]}
        self.chutes_answers = chutes_answers
        self.requests = []

    def close(self):
        pass

    def get(self, url, params=None, headers=None, timeout=None, **_kwargs):
        return self.request("GET", url, params=params, headers=headers)

    def post(self, url, json=None, headers=None, timeout=None, **_kwargs):
        if url == NRAS_URL:
            nonce = json["nonce"]
            return Response(200, self.answers[nonce]) if nonce in self.answers else \
                Response(400, {"error": "not this nonce"})
        return Response(404)

    def request(self, method, url, params=None, data=None, headers=None, timeout=None,
                **_kwargs):
        asked = Asked(method, url, params, headers, data)
        self.requests.append(asked)
        if self.chutes_answers is not None and asked.host in ("api.chutes.ai",
                                                               "llm.chutes.ai"):
            return self.chutes_answers(asked)
        if url == nras.JWKS_URL:
            return Response(200, self.fixture["jwks"])
        for item in self.fixture["collateral"]:
            if item["url"] == url and (item["params"] or None) == (params or None):
                return Response(item["status"], base64.b64decode(item["body"]),
                                item["headers"])
        return Response(404)


@unittest.skipUnless(HAVE_MLKEM, "this cryptography has no ML-KEM-768")
class RealChutesEvidence(unittest.TestCase):
    """Real Chutes evidence (three instances of Qwen/Qwen3.8-27B-TEE, keyless, for our
    nonce), verified offline at the moment it was captured through TeeClient's real
    Intel and NVIDIA checks. From SealedLore's tests/test_chutes_evidence.py."""

    def setUp(self):
        self.real = json.loads(REAL.read_text(encoding="utf-8"))
        self.moment = datetime.fromtimestamp(self.real["now"], UTC)
        dcap._cache.clear()
        nras._keys.update(at=0.0, keys={})

    def tearDown(self):
        dcap._cache.clear()
        nras._keys.update(at=0.0, keys={})

    def checker(self, session=None):
        session = session or RealChecks(self.real)
        return privacy.TeeClient(
            "https://api.chutes.ai", "", self.real["model"], session=session,
            intel=lambda raw, s: dcap.verify(raw, s, now=self.moment),
            nvidia=lambda answer, nonce, s: nras.verify(answer, nonce, s,
                                                        now=self.real["now"]))

    def test_each_real_instance_holds_up_to_intel_and_nvidia_and_hands_back_its_quote(self):
        sets = chutes_e2ee.published_measurements(self.real["measurements"])
        for item in self.real["evidence"]["evidence"]:
            with self.subTest(instance=item["instance_id"]):
                check = self.checker().check_instance(item, self.real["nonce"])
                self.assertEqual((check.status, check.gpus, check.shortfalls),
                                 ("UpToDate", 1, []))
                facts = check.quote
                self.assertIsInstance(facts, privacy.QuoteFacts)
                self.assertNotEqual(facts.report_data[:32],
                                    bytes.fromhex(self.real["nonce"]))
                self.assertIsNotNone(chutes_e2ee.measured(facts.mrtd, facts.rtmrs, sets))
                self.assertFalse(chutes_e2ee.key_bound(facts.report_data,
                                                       self.real["nonce"], "A" * 1580 + "=="))

    def test_the_registers_are_read_where_intel_puts_them(self):
        raw = base64.b64decode(self.real["evidence"]["evidence"][0]["quote"])
        facts = privacy.QuoteFacts.of(dcap.parse_quote(raw))
        report = raw[48:632]
        self.assertEqual((facts.mrtd, facts.report_data), (report[136:184], report[520:584]))
        self.assertEqual(facts.rtmrs, tuple(report[o:o + 48] for o in (328, 376, 424, 472)))

    def test_real_evidence_for_another_nonce_or_changed_is_refused(self):
        items = self.real["evidence"]["evidence"]
        with self.assertRaisesRegex(PrivacyError, "did not sign our nonce"):
            self.checker().check_instance(items[0], hashlib.sha256(b"x").hexdigest())
        item = dict(items[1])
        quote = bytearray(base64.b64decode(item["quote"]))
        quote[48 + 520] ^= 1
        item["quote"] = base64.b64encode(bytes(quote)).decode()
        with self.assertRaisesRegex(PrivacyError, "signed a different quote"):
            self.checker().check_instance(item, self.real["nonce"])
        other = dict(items[1], signature=items[0]["signature"])
        with self.assertRaisesRegex(PrivacyError, "did not sign its evidence"):
            self.checker().check_instance(other, self.real["nonce"])

    def test_evidence_in_no_shape_is_refused_by_what_it_raised(self):
        with self.assertRaisesRegex(PrivacyError, r"shape it should be \(AttributeError\)"):
            self.checker().check_instance(["not", "an instance"], self.real["nonce"])

    def test_an_attempts_time_holds_the_checks_it_wraps(self):
        import time
        checker = self.checker()
        with checker.attempt(time.monotonic() - 1):
            with self.assertRaisesRegex(PrivacyError, "not done within 3 minutes"):
                checker.check_instance(self.real["evidence"]["evidence"][0],
                                       self.real["nonce"])
        self.assertIsNone(checker._deadline)

    def test_the_capture_holds_no_key(self):
        text = REAL.read_text(encoding="utf-8").lower()
        self.assertIn("keyless", self.real["source"])
        for word in ("authorization", "bearer", "cpk_"):
            self.assertNotIn(word, text)

    def test_the_client_attests_real_instances_up_to_the_key_it_cannot_know(self):
        real = self.real
        items = real["evidence"]["evidence"]

        def answers(asked):
            if asked.host == "llm.chutes.ai":
                return Response(200, {"data": [{"id": real["model"],
                                                "chute_id": real["chute_id"],
                                                "confidential_compute": True}]})
            if asked.path == f"/e2e/instances/{real['chute_id']}":
                made_up = [{"instance_id": item["instance_id"],
                            "e2e_pubkey": base64.b64encode(
                                mlkem.MLKEM768PrivateKey.generate().public_key()
                                .public_bytes_raw()).decode(),
                            "nonces": [secrets.token_urlsafe(24) for _ in range(10)]}
                           for item in items]
                return Response(200, {"instances": made_up, "nonce_expires_in": 60})
            if asked.path == f"/chutes/{real['chute_id']}/evidence":
                return Response(200, real["evidence"])
            if asked.path == "/servers/tdx/measurements":
                return Response(200, real["measurements"])
            return Response(404)

        def made():
            session = RealChecks(real, answers)
            return ChutesClient(
                "https://llm.chutes.ai/v1", KEY, session=session, checks_session=session,
                intel=lambda raw, s: dcap.verify(raw, s, now=self.moment),
                nvidia=lambda answer, nonce, s: nras.verify(answer, nonce, s,
                                                            now=real["now"]))

        with mock.patch.object(chutes.secrets, "token_hex", lambda _n: real["nonce"]):
            with self.assertRaisesRegex(PrivacyError, "does not bind the key Chutes lists"):
                made().attest(real["model"])
            with mock.patch.object(chutes, "key_bound", lambda *_args: True):
                attestation = made().attest(real["model"])
        self.assertEqual((attestation.instances, attestation.level), (3, "partial"))
        self.assertEqual((attestation.tcb_status, attestation.gpus), ("UpToDate", 3))
        self.assertIn("(1.5.0)", attestation.release)


if __name__ == "__main__":
    unittest.main()
