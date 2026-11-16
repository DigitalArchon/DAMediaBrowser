# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tinfoil's own API, end-to-end encrypted (assistant/tinfoil_direct.py). Offline.

The attestation runs Tinfoil's real SDK over a capture of Tinfoil's public bundle
(tests/data/tinfoil/bundle.json: GET https://atc.tinfoil.sh/attestation, Nov 14 2026,
keyless, from SealedLore): the SEV-SNP report verified against AMD's chain with the
bundle's VCEK and the enclave certificate's binding, at the capture's moment; only
Sigstore's check of the release (it needs Sigstore's TUF root, online) is stood in for.
tests/data/tinfoil/models.json is GET https://inference.tinfoil.sh/v1/models the same
day. A request goes through a mocked transport where EHBP's would be (the sealing itself
is the SDK's, tested in test_privacy's Sealed); the TLS pin is checked against a real TLS
server on this computer. Ported from SealedLore's tests/test_tinfoil_direct.py.
"""

import base64
import datetime as dt
import json
import os
import socket
import ssl
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from mediabrowser.core import private_ai as privacy  # noqa: E402
from mediabrowser.core import safejson, sealed, services, tinfoil_direct
from mediabrowser.core.netguard import Headers as CaseInsensitiveDict  # noqa: E402
from mediabrowser.core.private_ai import ModelRetired, PrivacyError  # noqa: E402
from mediabrowser.core.tinfoil_direct import (  # noqa: E402
    ATC_URL,
    DIRECT_FIELDS,
    Listing,
    TinfoilDirectClient,
    TlsPin,
    TlsPinMismatch,
    chosen_effort,
    direct_verify,
    listing_entries,
    spki_sha256,
    within,
)

DATA = Path(__file__).resolve().parent / "data"
BUNDLE = json.loads((DATA / "tinfoil" / "bundle.json").read_text(encoding="utf-8"))
MODELS = json.loads((DATA / "tinfoil" / "models.json").read_text(encoding="utf-8"))
RELEASE = json.loads((DATA / "tinfoil" / "router_release.json").read_text(encoding="utf-8"))
ATTESTED_TLS_KEY = "e5db636af31d94d74871ab63d5e62ac2d34beab02032629dac2685149b7d99d7"
HOST = "inference.tinfoil.sh"
SDK = sealed.available()[0]


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self.headers = CaseInsensitiveDict()
        self.content = json.dumps(body).encode()

    def json(self):
        return safejson.loads(self.content)

    def close(self):
        pass


class ListingSession:
    """Tinfoil's public listing, as a requests-like Session, every request recorded."""

    def __init__(self, data=MODELS, status=200):
        self.data, self.status, self.asked = data, status, []

    def get(self, url, headers=None, timeout=None, allow_redirects=True, **_kwargs):
        self.asked.append((url, dict(headers or {}), allow_redirects))
        return Response(self.status, self.data)

    def close(self):
        pass


def verified(tls=ATTESTED_TLS_KEY, enclave=HOST):
    return sealed.Verified(hpke_public_key="ab" * 32, enclave=enclave, hardware="AMD SEV-SNP",
                           measurement="07c3b78a", release_digest="cb88bff7" * 8,
                           release_tag="v0.0.176", latest_release="", latest_checked=True,
                           tls_public_key=tls)


def stream_body(text="Hello there."):
    return (b'data: {"id":"c1","choices":[{"delta":{"content":"' + text.encode()
            + b'"}}]}\n\n'
            b'data: {"id":"c1","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            b'data: [DONE]\n\n')


def direct(handler, verifier=None, listing=None, url=services.TINFOIL_BASE_URL,
           saved_for=services.TINFOIL_KEY_ORIGIN):
    """A client whose router attests as `verifier` says, its requests answered by
    `handler` where EHBP's transport would be, and no pin (pin_for stood in for: the mock
    transport makes no TLS; the pin's own tests are below)."""
    import httpx
    calls = {"verify": 0}

    def verify(bundle, secret):
        calls["verify"] += 1
        calls["bundle"] = bundle
        return (verifier or verified)()

    client = TinfoilDirectClient(
        url, "tk-test", key_saved_for=saved_for,
        enclave=sealed.Enclave(ATC_URL, verifier=verify),
        transport_factory=lambda key, route: httpx.MockTransport(handler),
        listing=listing or Listing(services.TINFOIL_BASE_URL, session=ListingSession()))
    client.pin_for = lambda host, proof: (lambda name, info: None)
    return client, calls


PAYLOAD = {"model": "glm-5-3", "stream": True, "temperature": 0.6, "max_tokens": 300,
           "messages": [{"role": "system", "content": "Be brief."},
                        {"role": "user", "content": "The secret words."}],
           "reasoning": {"effort": "low"}, "promptCaching": {"enabled": True}}


def read(client, payload=PAYLOAD, model="glm-5-3"):
    from tests.shared_core import LLMClient
    with client.request(model, payload) as response:
        result = LLMClient._consume(client.lines(response), None, None, [])
    client.finish(result["finish_reason"], result["done"])
    return result


# ---- the attestation --------------------------------------------------------------------

def verify_report():
    from tinfoil.attestation.attestation import Document
    from tinfoil.attestation.types import PredicateType

    report = BUNDLE["enclaveAttestationReport"]
    document = Document(format=PredicateType(report["format"]), body=report["body"])
    return document, document.verify(vcek_der=base64.b64decode(BUNDLE["vcek"]))


@unittest.skipUnless(SDK, "Tinfoil's client is not installed (tools/install-private-mode.sh)")
class TheAttestation(unittest.TestCase):
    def setUp(self):
        import socket as sockets

        import tinfoil.attestation.bundle
        import tinfoil.client
        import tinfoil.github

        _document, verification = verify_report()
        self.fetched = []
        self.bundle = dict(BUNDLE)
        test = self

        class Reply:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return test.bundle

        def get(url, **_kwargs):
            self.fetched.append(url)
            return Reply()

        def post(url, json=None, **_kwargs):
            self.fetched.append(("POST", url, json))
            return Reply()

        def nowhere(*_args, **_kwargs):
            raise AssertionError("a router lookup, or plain urllib: never reached")

        class Release:
            tag, digest = RELEASE["tag_name"], BUNDLE["digest"]

        for target, name, value in (
                (tinfoil.attestation.bundle, "requests", types.SimpleNamespace(get=get,
                                                                               post=post)),
                (sockets.socket, "connect", nowhere), (sockets, "create_connection", nowhere),
                (tinfoil.client, "get_router_address", nowhere),
                (tinfoil.client, "verify_attestation",
                 lambda *a, **k: verification.measurement),
                (tinfoil.github, "fetch_latest_release", lambda repo: Release()),
                # This app has no proxy setting of its own to refuse around.
                (sealed, "guard_sdk_requests", lambda *a, **k: [])):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("urllib.request.urlopen", nowhere)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_captured_bundle_binds_the_tls_key_the_pin_checks(self):
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding
        from tinfoil.attestation.bundle import verify_certificate

        document, verification = verify_report()
        self.assertEqual(verification.public_key_fp, ATTESTED_TLS_KEY)
        self.assertEqual(verify_certificate(BUNDLE["enclaveCert"], HOST, document,
                                            verification.hpke_public_key),
                         verification.hpke_public_key)
        certificate = x509.load_pem_x509_certificate(BUNDLE["enclaveCert"].encode())
        self.assertEqual(spki_sha256(certificate.public_bytes(Encoding.DER)),
                         ATTESTED_TLS_KEY)

    def test_direct_attestation_takes_atcs_bundle_and_never_looks_a_router_up(self):
        proof = direct_verify(HOST, ATC_URL, "s" * 64)
        self.assertEqual(self.fetched, [("POST", ATC_URL + "/attestation",
                                         {"enclaveUrl": f"https://{HOST}"})])
        self.assertEqual((proof.enclave, proof.tls_public_key), (HOST, ATTESTED_TLS_KEY))
        attestation = sealed.attestation_for("glm-5-3", proof, service="tinfoil")
        self.assertEqual((attestation.kind, attestation.service, attestation.release,
                          attestation.hardware),
                         (privacy.E2EE, "tinfoil", "v0.0.176", "AMD SEV-SNP"))
        self.assertIn("Tinfoil's router enclave at inference.tinfoil.sh",
                      privacy.what_was_attested(attestation))

    def test_a_bundle_for_another_host_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "attestation is for inference.tinfoil.sh, "
                                                  "not router-0"):
            direct_verify("router-0.tinfoil.sh", ATC_URL, "s" * 64)
        self.bundle["domain"] = "evil.example"
        with self.assertRaisesRegex(ValueError, "not valid for domain"):
            direct_verify("evil.example", ATC_URL, "s" * 64)


class AttestingHoldsItsWords(unittest.TestCase):
    def test_an_attestation_naming_no_tls_key_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "no TLS key"):
            direct_verify(HOST, ATC_URL, "s", verify=lambda *a, **k: verified(tls=""))
        with self.assertRaisesRegex(PrivacyError, "not inference.tinfoil.sh"):
            direct_verify(HOST, ATC_URL, "s",
                          verify=lambda *a, **k: verified(enclave="router-0.tinfoil.sh"))

    def test_the_whole_attempt_has_its_time(self):
        release = threading.Event()

        def slow():
            release.wait(5)
            raise AssertionError("never used")

        with self.assertRaisesRegex(PrivacyError, "not attested within"):
            within(0.05, slow)
        release.set()
        self.assertEqual(within(1, lambda: "done"), "done")
        with self.assertRaises(KeyError):
            within(1, lambda: {}["x"])


# ---- one request ------------------------------------------------------------------------

@unittest.skipUnless(SDK, "Tinfoil's client is not installed (tools/install-private-mode.sh)")
class OneRequest(unittest.TestCase):
    def test_the_prompt_goes_to_tinfoils_own_api_with_nothing_of_nanogpts(self):
        import httpx
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, content=stream_body())

        client, calls = direct(handler)
        result = read(client)
        self.assertEqual(result["text"], "Hello there.")
        self.assertEqual(calls["bundle"], ATC_URL)
        request = seen[0]
        self.assertEqual(str(request.url), "https://inference.tinfoil.sh/v1/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer tk-test")
        self.assertFalse([name for name in request.headers
                          if name.lower().startswith(("x-nanogpt", "x-tinfoil", "x-query"))])
        body = json.loads(request.content)
        self.assertEqual((body["model"], body["temperature"], body["stream"]),
                         ("glm-5-3", 0.6, True))
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertEqual(body[sealed.CACHE_SECRET_FIELD], client.enclave.cache_secret)
        self.assertLessEqual(set(body) - {sealed.CACHE_SECRET_FIELD}, DIRECT_FIELDS)
        self.assertNotIn("chat_template_kwargs", body)
        self.assertNotIn("promptCaching", body)
        self.assertEqual(body["reasoning_effort"], "low")

    def test_a_key_mismatch_is_attested_again_and_sent_once_more(self):
        import httpx
        from ehbp import KeyConfigMismatchError
        answers = [KeyConfigMismatchError("rotated"), httpx.Response(200, content=stream_body())]

        def handler(request):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        client, calls = direct(handler)
        self.assertEqual(read(client)["text"], "Hello there.")
        self.assertEqual(calls["verify"], 2)

    def test_a_reply_the_enclave_did_not_seal_is_refused(self):
        import httpx
        for status, why in ((302, "redirected"), (500, "HTTP 500"),
                            (401, "Tinfoil said \\(unauthenticated, not the model\\)")):
            with self.subTest(status=status):
                client, _calls = direct(lambda request: httpx.Response(
                    status, headers={"location": "https://elsewhere.example/"},
                    content=b'{"error": "no"}'))
                with self.assertRaisesRegex(PrivacyError, why):
                    read(client)

    def test_a_stream_without_its_end_is_refused(self):
        import httpx
        client, _calls = direct(lambda request: httpx.Response(
            200, content=b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'))
        with self.assertRaisesRegex(PrivacyError, "ended before it was complete"):
            read(client)

    def test_a_model_gone_from_the_list_is_retired_with_what_is_offered(self):
        client, _calls = direct(lambda request: self.fail("sent a retired model"))
        with self.assertRaises(ModelRetired) as caught:
            read(client, model="gone-model")
        self.assertIn("Tinfoil", str(caught.exception))
        self.assertIn("glm-5-3", caught.exception.offered)

    def test_the_key_goes_to_tinfoils_api_alone(self):
        for url, saved, why in (("https://router-0.tinfoil.sh/v1", "", "not Tinfoil's API"),
                                ("https://inference.tinfoil.sh.evil.example/v1", "",
                                 "not Tinfoil's API"),
                                ("http://inference.tinfoil.sh/v1", "", "https"),
                                (services.TINFOIL_BASE_URL, "https://nano-gpt.com:443",
                                 "saved for https://nano-gpt.com:443")):
            with self.subTest(url=url, saved=saved), \
                    self.assertRaisesRegex(PrivacyError, why):
                TinfoilDirectClient(url, "tk-test", key_saved_for=saved,
                                    enclave=sealed.Enclave(ATC_URL, verifier=verified))

    def test_an_address_typed_any_way_is_tinfoils_own_api(self):
        for typed in ("https://inference.tinfoil.sh", "https://inference.tinfoil.sh/v1/",
                      "https://INFERENCE.tinfoil.sh/v1/chat/completions",
                      "https://inference.tinfoil.sh./v1"):
            with self.subTest(typed=typed):
                client = TinfoilDirectClient(typed, "tk-test",
                                             enclave=sealed.Enclave(ATC_URL,
                                                                    verifier=verified))
                self.assertEqual(client.endpoint,
                                 "https://" + client.host + "/v1/chat/completions")
                self.assertEqual(client.host, HOST)

    def test_a_request_with_no_pin_sends_nothing(self):
        import httpx
        sent = []
        client, _calls = direct(lambda request: sent.append(request))
        del client.pin_for      # the real pin: the mock transport makes no handshake

        class NoTls(httpx.MockTransport):
            def handle_request(self, request):
                request.extensions["trace"]("http11.send_request_headers.started",
                                            {"request": request})
                sent.append(request)
                return httpx.Response(200, content=stream_body())

        client._transport_factory = lambda key, route: NoTls(lambda r: None)
        with self.assertRaisesRegex(PrivacyError, "not checked against the attested TLS"):
            read(client)
        self.assertEqual(sent, [])


class TheListing(unittest.TestCase):
    def test_the_listing_is_fetched_without_the_key_and_read_as_the_apps_shape(self):
        session = ListingSession()
        entries = Listing(services.TINFOIL_BASE_URL, session=session).fetch()
        url, headers, followed = session.asked[0]
        self.assertEqual(url, "https://inference.tinfoil.sh/v1/models")
        self.assertNotIn("Authorization", headers)
        self.assertFalse(followed)
        self.assertEqual(entries, listing_entries(MODELS))
        self.assertTrue(all(model["owned_by"] == "Tinfoil" for model in entries.values()))
        glm = entries["glm-5-3"]
        self.assertIn("pricing", glm)
        self.assertEqual(glm["pricing"]["unit"], "per_million_tokens")
        self.assertTrue(glm["reasoning_efforts"])

    def test_a_listing_that_cannot_be_read_says_so(self):
        with self.assertRaisesRegex(PrivacyError, "HTTP 503"):
            Listing(services.TINFOIL_BASE_URL, session=ListingSession(status=503)).fetch()
        self.assertIsNone(Listing(services.TINFOIL_BASE_URL,
                                  session=ListingSession(status=503)).known())

    def test_only_an_effort_the_model_lists_is_sent(self):
        self.assertEqual(chosen_effort("medium", ["low", "high", "max"]), "high")
        self.assertEqual(chosen_effort("low", ["low", "high"]), "low")
        self.assertIsNone(chosen_effort("none", ["low", "high"]))
        self.assertEqual(chosen_effort("none", ["none", "low"]), "none")
        self.assertIsNone(chosen_effort("high", []))
        self.assertIsNone(chosen_effort("high", None))
        self.assertIsNone(chosen_effort("bogus", ["low"]))
        body = tinfoil_direct.direct_body({**PAYLOAD, "reasoning": {"effort": "xhigh"}},
                                          ["low", "medium", "high"])
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertNotIn("reasoning_effort",
                         tinfoil_direct.direct_body(PAYLOAD, []))


# ---- the TLS pin, against a real TLS server ---------------------------------------------

def self_signed(name):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, name)])
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(key.public_key()).serial_number(1)
                   .not_valid_before(dt.datetime(2020, 1, 1, tzinfo=dt.UTC))
                   .not_valid_after(dt.datetime(2040, 1, 1, tzinfo=dt.UTC))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), False)
                   .sign(key, hashes.SHA256()))
    pem = certificate.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return certificate, pem, key_pem


class TlsServer:
    """A TLS server on this computer answering every request with a stream, and
    recording every byte it is sent after the handshake."""

    def __init__(self):
        self.certificate, pem, key_pem = self_signed("localhost")
        folder = tempfile.mkdtemp(prefix="tls-pin-")
        self.ca = os.path.join(folder, "cert.pem")
        key_file = os.path.join(folder, "key.pem")
        with open(self.ca, "wb") as f:
            f.write(pem)
        with open(key_file, "wb") as f:
            f.write(key_pem)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(self.ca, key_file)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.received = bytearray()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                raw, _ = self.listener.accept()
            except OSError:
                return
            try:
                with self.context.wrap_socket(raw, server_side=True) as conn:
                    conn.settimeout(2)
                    data = b""
                    while b"\r\n\r\n" not in data:
                        piece = conn.recv(65536)
                        if not piece:
                            break
                        data += piece
                    self.received += data
                    if data:
                        body = stream_body()
                        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                                     b"Content-Length: " + str(len(body)).encode()
                                     + b"\r\n\r\n" + body)
            except (OSError, ssl.SSLError):
                pass

    def close(self):
        self.listener.close()

    @property
    def key(self):
        from cryptography.hazmat.primitives.serialization import Encoding
        return spki_sha256(self.certificate.public_bytes(Encoding.DER))


@unittest.skipUnless(SDK, "httpx comes with Tinfoil's client (tools/install-private-mode.sh)")
class TheTlsPin(unittest.TestCase):
    def setUp(self):
        self.server = TlsServer()
        self.addCleanup(self.server.close)

    def send(self, expected):
        import httpx
        context = ssl.create_default_context(cafile=self.server.ca)
        with httpx.Client(transport=httpx.HTTPTransport(verify=context)) as client:
            request = client.build_request(
                "POST", f"https://localhost:{self.server.port}/v1/chat/completions",
                content=b"the secret words",
                extensions={"trace": TlsPin("localhost", expected)})
            return client.send(request)

    def test_a_tls_key_that_is_not_the_attested_one_gets_not_one_byte(self):
        with self.assertRaises(TlsPinMismatch):
            self.send("00" * 32)
        self.assertEqual(bytes(self.server.received), b"")

    def test_the_attested_tls_key_lets_the_request_go(self):
        response = self.send(self.server.key)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"POST /v1/chat/completions", bytes(self.server.received))

    def test_no_request_goes_on_a_connection_whose_key_was_not_checked(self):
        pin = TlsPin("inference.tinfoil.sh", ATTESTED_TLS_KEY)
        request = types.SimpleNamespace(method=b"POST")
        with self.assertRaisesRegex(TlsPinMismatch, "not checked"):
            pin("http11.send_request_headers.started", {"request": request})
        # A CONNECT to a proxy carries nothing of ours, and is let through.
        pin("http11.send_request_headers.started",
            {"request": types.SimpleNamespace(method=b"CONNECT")})
        # A proxy's own TLS is told apart by the name it was made for.
        pin("connection.start_tls.started", {"server_hostname": "proxy.example"})
        pin("connection.start_tls.complete", {"return_value": None})
        self.assertFalse(pin.checked)


if __name__ == "__main__":
    unittest.main()
