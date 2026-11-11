# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Privacy Mode: the AI asked only of the private model, attested first,
and never anything else - whatever is switched in Settings meanwhile.

The TEE enclave is tests/test_attestation.py's fake nano-gpt; the sealed one
a fake client that keeps what it was asked to seal, unless Tinfoil's SDK is
installed (tools/install-private-mode.sh), when the real client runs against
a mocked transport too. The chat endpoint is netguard's transport, faked."""

import json
import os
import re
import stat
from contextlib import contextmanager

import pytest

from mediabrowser.core import ai, config, netguard, outbound, privacy, private_ai, sealed, store
from mediabrowser.core.private_ai import ModelRetired, PrivacyError, PrivacySession
from tests.test_attestation import FakeNanoGpt, GpusTied

BASE = "https://nano-gpt.com/api/v1"
TEE_MODEL = "TEE/glm-5.3-flash"
SEALED_MODEL = "private/kimi-k3"
HERE = os.path.dirname(os.path.abspath(__file__))


class Reply:
    """An answer as netguard's transport gives it: its body, read as asked -
    at most `step` bytes a read, so that a stream's lines fall across reads,
    as on the wire - its headers, its status, and where it came from."""

    def __init__(self, body, headers=None, status=200, url=None, step=None):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = headers or {}
        self.status = status
        self.url = url
        self.step = step
        self._at = 0

    def read(self, size=-1):
        left = len(self.body) - self._at
        size = left if size is None or size < 0 else min(size, left)
        if self.step:
            size = min(size, self.step)
        piece = self.body[self._at:self._at + size]
        self._at += len(piece)
        return piece

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def _sent(request):
    return {"url": request.full_url, "body": json.loads(request.data or b"{}"),
            "headers": dict(request.header_items())}


@pytest.fixture
def endpoint(monkeypatch):
    """The chat endpoint: what it was sent, and a reply with an id."""
    sent = []

    def transport(request, timeout):
        sent.append(_sent(request))
        return Reply({"id": "chatcmpl-42",
                      "choices": [{"message": {"content": '{"ok": true}'}}]})

    monkeypatch.setattr(netguard, "_transport", transport)
    return sent


@pytest.fixture
def tee_stream(monkeypatch):
    """The chat endpoint as nano-gpt streams a TEE/ model's reply:
    `tee_stream(server)` has it answer with that FakeNanoGpt's stream, seven
    bytes a read, the receipt and route the server names in its headers - the
    stream the server's records hash - and returns what it was sent."""
    sent, serving = [], {}

    def transport(request, timeout):
        sent.append(_sent(request))
        server = serving["server"]
        named = {"Content-Type": "text/event-stream", "X-Receipt-Id": server.receipt_id,
                 "X-TEE-Verification-Route": server.route}
        return Reply(server.stream(), {name: value for name, value in named.items() if value},
                     step=7)

    monkeypatch.setattr(netguard, "_transport", transport)

    def serve(server):
        serving["server"] = server
        return sent

    return serve


class FakeSealed:
    """sealed.PrivateModeClient as the session uses it: attested, accepted,
    asked to seal - and what it was asked to seal kept, for the test."""

    def __init__(self, reply='{"ok": true}', failure=None, retired=False, done=True):
        self.reply, self.failure, self.retired, self.done = reply, failure, retired, done
        self.sealed = []
        self.attested = 0
        self.accepted = None
        self.reattested = None

    def check_offered(self, model):
        if self.retired:
            raise ModelRetired(model, ["private/deepseek-v4-1-flash"])

    def attest(self, model):
        self.attested += 1
        return private_ai.Attestation(
            model=model, kind=private_ai.E2EE,
            detail="router-0.tinfoil.sh runs tinfoilsh/confidential-model-router v0.0.171",
            proof=object())

    def accept(self, attestation):
        self.accepted = attestation

    @contextmanager
    def request(self, model, payload):
        if self.failure is not None:
            raise self.failure
        self.sealed.append((model, payload))
        chunk = {"id": "c1", "choices": [{"delta": {"content": self.reply}}]}
        end = {"id": "c1", "choices": [{"delta": {}, "finish_reason": "stop"}]}
        lines = [f"data: {json.dumps(chunk)}", f"data: {json.dumps(end)}"]
        yield lines + (["data: [DONE]"] if self.done else [])

    def lines(self, response):
        yield from response

    def finish(self, finish_reason, done):
        if not (finish_reason and done):
            raise PrivacyError("the encrypted reply ended before it was complete")


def private_settings(model=TEE_MODEL, server=None, sealed_client=None, strict=False, **more):
    """Settings for an operation in Privacy Mode, its session's enclave a fake."""
    settings = ai.settings_from({privacy.AI: True, ai.SETTING_KEY: "sk-1",
                                 ai.SETTING_PRIVACY_MODE: True, ai.SETTING_STRICT: strict,
                                 ai.SETTING_PRIVATE_MODEL: model, **more})
    session = PrivacySession(BASE, "sk-1", model, strict=strict,
                             tee_factory=(server or FakeNanoGpt()).tee_client,
                             sealed_factory=lambda *a: sealed_client or FakeSealed())
    session.listener = ai._attestation_listener(session)
    settings[ai.SESSION] = session
    return settings


@pytest.fixture(autouse=True)
def client_installed(monkeypatch):
    """Tinfoil's client, as far as problems() asks: there, for the fakes here
    to stand in for. Asked for real, it's only known once looked for
    (needs_sdk, below), and those tests run only where it's installed."""
    if sealed.known() is None or not sealed.known()[0]:
        monkeypatch.setattr(sealed, "_known", (True, "tinfoil"))


def save(**values):
    settings = store.load_app_settings()
    settings.update(values)
    store.save_app_settings(settings)


class TestTheSessionIsFixedWhenItStarts:
    def test_off_means_no_session(self):
        settings = ai.settings_from({privacy.AI: True, ai.SETTING_KEY: "sk-1"})
        assert ai.session_of(settings) is None and not ai.in_privacy_mode(settings)

    def test_on_its_model_and_strictness_are_copied_then(self):
        app = {privacy.AI: True, ai.SETTING_KEY: "sk-1", ai.SETTING_PRIVACY_MODE: True,
               ai.SETTING_PRIVATE_MODEL: " private/kimi-k3 ", ai.SETTING_STRICT: True}
        settings = ai.settings_from(app)
        session = ai.session_of(settings)
        assert (session.model, session.strict) == ("private/kimi-k3", True)
        app[ai.SETTING_PRIVATE_MODEL] = "TEE/other"
        app[ai.SETTING_STRICT] = False
        assert (session.model, session.strict) == ("private/kimi-k3", True)
        assert ai.active_model(settings) == "private/kimi-k3"
        # Copies of the settings, as a run makes them, share it.
        assert ai.session_of(ai.with_search(settings, "linkup")) is session

    def test_the_default_private_model_can_see_and_is_sealed(self):
        settings = ai.settings_from({privacy.AI: True, ai.SETTING_PRIVACY_MODE: True})
        assert ai.active_model(settings) == ai.DEFAULT_PRIVATE_MODEL
        assert private_ai.kind_of(ai.DEFAULT_PRIVATE_MODEL) == private_ai.E2EE

    @pytest.mark.parametrize("value", [True, "true", "yes", 1, "on", "garbled", [1]])
    def test_a_value_that_isnt_plainly_off_reads_as_on(self, value):
        assert ai._on(value)

    @pytest.mark.parametrize("value", [False, None, "false", "0", "off", "", 0])
    def test_plainly_off_is_off(self, value):
        assert not ai._on(value)

    def test_nothing_is_ready_that_privacy_mode_cant_honour(self):
        ordinary = private_settings(model="anthropic/claude-sonnet-5")
        assert "runs in no attested enclave" in ai.not_ready(ordinary)
        assert not ai.is_configured(ordinary)
        elsewhere = private_settings(model=SEALED_MODEL,
                                     **{ai.SETTING_BASE_URL: "https://models.example/v1"})
        assert "not nano-gpt's" in ai.not_ready(elsewhere)
        assert ai.is_configured(private_settings(model=SEALED_MODEL))

    def test_without_tinfoils_client_a_sealed_model_says_how_to_get_it(self, monkeypatch):
        monkeypatch.setattr(sealed, "_known", (False, sealed.INSTALL_HINT))
        settings = private_settings(model=SEALED_MODEL)
        assert "install-private-mode.sh" in ai.not_ready(settings)
        assert "AppImage carries it" in ai.not_ready(settings)


class TestTee:
    def test_attested_first_then_streamed_then_checked_to_its_bytes(self, tee_stream):
        server = FakeNanoGpt(reply='{"ok": true}')
        sent = tee_stream(server)
        settings = private_settings(server=server)
        assert ai.chat_json([{"role": "user", "content": "name these"}], settings,
                            online=True) == {"ok": True}
        (request,) = sent
        assert request["url"] == f"{BASE}/chat/completions"
        assert request["body"]["model"] == TEE_MODEL, "no web search, ever"
        assert request["body"]["response_format"] == {"type": "json_object"}
        assert any("/tee/attestation" in g for g in server.gets)
        assert any("/tee/signature/chatcmpl-42" in g for g in server.gets)
        receipt = ai.session_of(settings).last_receipt()
        assert receipt.state == private_ai.VERIFIED, receipt.detail
        assert "for the exact bytes that came" in receipt.detail
        check, listed = outbound.entries()
        assert (check.protection, check.outcome) == (outbound.CHECK, outbound.SENT)
        assert "nothing about your videos" in check.how
        # What was attested leads, before the checks that held.
        assert "Attested: the enclave whose key signs this model's replies" in check.detail
        assert check.detail.index("Attested: the enclave") < check.detail.index(
            "the Intel TDX quote binds")
        assert listed.protection == outbound.TEE and "can read it" in listed.how
        assert "reply: ✓ TEE, verified" in listed.detail
        # Received by the attested TEE: its reply's record, signed by the attested key,
        # shows it. Not "to its attested enclave" before that.
        assert listed.how == (f"Sent it to {TEE_MODEL}, attested first, through "
                              f"Nano-GPT's gateway, which can read it: received by the "
                              f"attested TEE")

    def test_a_tee_request_is_streamed_and_exact_mode_is_not_asked_for(self, tee_stream):
        """nano-gpt rebuilds a whole (unstreamed) TEE reply, so its bytes never match
        the record's hash unless it is asked for in its exact mode
        (X-NanoGPT-TEE-Response: exact, Nov 10 2026), which is not known to take the
        JSON object nearly every request here asks for. Every TEE request streams, as
        in DA Assistant and SealedLore: the bytes hashed are the ones that came."""
        server = FakeNanoGpt(reply="OK")
        sent = tee_stream(server)
        settings = private_settings(server=server)
        assert ai.ping(settings) == "OK"
        (request,) = sent
        assert request["body"]["stream"] is True
        assert request["headers"].get("Accept") == "text/event-stream"
        assert not any(name.lower() == "x-nanogpt-tee-response"
                       for name in request["headers"])
        assert ai.session_of(settings).last_receipt().state == private_ai.VERIFIED

    def test_the_hash_is_of_every_byte_the_stream_carried_and_its_receipt_is_named(
            self, tee_stream, monkeypatch):
        """Read on after [DONE], as the record hashes it: what came after it too. The
        receipt id and route nano-gpt's headers name go with the lookup, and the
        receipt is checked. From DA Assistant's tests, from SealedLore's (Nov 2026)."""
        monkeypatch.setattr(private_ai, "RECEIPT_RETRY_SECONDS", 0)
        server = FakeNanoGpt(tied="e2ee", receipts=True, receipt_id="rcpt-7", route="r2",
                             tail=b": a comment after the end\n\n", reply="Glass Harbor")
        tee_stream(server)
        settings = private_settings(server=server)
        assert ai.chat([], settings, json_reply=False) == "Glass Harbor"
        receipt = ai.session_of(settings).last_receipt()
        assert receipt.state == private_ai.VERIFIED, receipt.detail
        assert "RedPill's receipt for it holds" in receipt.detail
        assert "/tee/signature/rcpt-7" in [g for g in server.gets if "/tee/signature/" in g][-1]
        assert server.lookups[-1].get("X-TEE-Verification-Route") == "r2"
        assert not any("X-TEE-Verification-Route" in r["headers"] for r in server.reports)

    def test_a_reply_whose_bytes_are_not_those_signed_fails(self, tee_stream):
        from tests.test_attestation import stream_bytes
        server = FakeNanoGpt(tail=b": a comment after the end\n\n")
        server.signed_bytes = stream_bytes(server.lines())     # all but what came after
        tee_stream(server)
        settings = private_settings(server=server)
        ai.chat([], settings, json_reply=False)
        receipt = ai.session_of(settings).last_receipt()
        assert receipt.state == private_ai.FAILED
        assert private_ai.receipt_label(receipt) == ("⚠ TEE, check failed", private_ai.BAD)
        assert "reply: ⚠ TEE, check failed" in outbound.entries()[-1].detail

    def test_a_stream_cut_short_is_checked_as_not_whole_and_is_no_answer(self, tee_stream):
        server = FakeNanoGpt(cut=True)
        tee_stream(server)
        settings = private_settings(server=server)
        with pytest.raises(ai.AIError, match="cut off before it was complete"):
            ai.chat([], settings, json_reply=False)
        receipt = ai.session_of(settings).last_receipt()
        assert receipt.state == private_ai.SIGNED
        assert "the reply did not arrive whole" in receipt.detail
        assert outbound.entries()[-1].outcome == outbound.FAILED

    @pytest.mark.parametrize(("body", "kind"), [
        (b"<html><body>Sign in to the Wi-Fi</body></html>", "text/html"),
        (b"", "text/event-stream"),
        (b'{"id": "x", "choices": []}', "application/json"),
    ])
    def test_an_answer_with_no_stream_in_it_is_said_never_an_empty_reply(
            self, monkeypatch, body, kind):
        """A web page where the API was expected, or anything else with not one
        "data:" line in it, is an error naming its status and content type, and where
        it came from when that was elsewhere - never "the model returned an empty
        reply". Nothing in it is looked up as a reply. From SealedLore (e947938)."""
        monkeypatch.setattr(netguard, "_transport", lambda request, timeout: Reply(
            body, {"Content-Type": f"{kind}; charset=utf-8"},
            url="https://nano-gpt.com/login"))
        server = FakeNanoGpt()
        settings = private_settings(server=server)
        with pytest.raises(ai.AIError) as caught:
            ai.chat([], settings, json_reply=False)
        said = str(caught.value)
        assert f"answered HTTP 200 ({kind}) with no reply in it" in said
        assert "sent on to https://nano-gpt.com/login" in said
        assert "Settings → AI" in said
        assert server.lookups == [] and ai.session_of(settings).last_receipt() is None
        assert outbound.entries()[-1].outcome == outbound.FAILED

    def test_a_stream_is_read_whatever_its_content_type_says(self, monkeypatch):
        """Real servers label their streams every way: a body with "data:" lines is the
        reply. From SealedLore (e947938)."""
        server = FakeNanoGpt(reply="fine")
        for kind in ("text/html", "application/json", "text/plain", None):
            headers = {"Content-Type": kind} if kind else {}
            monkeypatch.setattr(netguard, "_transport", lambda request, timeout, h=headers:
                                Reply(server.stream(), h))
            assert ai.chat([], private_settings(server=server), json_reply=False) == "fine"

    def test_an_enclave_that_isnt_attested_is_sent_nothing(self, endpoint):
        settings = private_settings(server=FakeNanoGpt(nonce_bound=False))
        with pytest.raises(ai.Refused, match="did not bind its signing key"):
            ai.chat([{"role": "user", "content": "Glass Harbor"}], settings)
        assert endpoint == []
        check, refused = outbound.entries()
        assert check.outcome == outbound.REFUSED and refused.outcome == outbound.REFUSED
        assert "Glass Harbor" in refused.content, "the list says what would have gone"

    def test_strict_refuses_an_attestation_that_proved_less_than_it_could(self, tee_stream):
        # On an enclave whose quote binds the gateway's keys itself, which proves all
        # else there is to prove; and as nano-gpt serves them, whose quote binds the
        # key that signs each reply's record but not the receipt keys beside it.
        sent = tee_stream(FakeNanoGpt())
        for lacking, lacked in ((FakeNanoGpt(tied="statement", evidence=False),
                                 "no GPU evidence"),
                                (FakeNanoGpt(), private_ai.RECEIPT_KEYS_LOOSE),
                                (FakeNanoGpt(tied="statement"), private_ai.GPUS_LOOSE)):
            with pytest.raises(ai.Refused, match=f"proved less than it could .{lacked}."):
                ai.chat([], private_settings(server=lacking, strict=True))
            assert sent == []
        settings = private_settings(server=FakeNanoGpt(tied="statement", evidence=False))
        ai.chat([], settings, json_reply=False)
        assert len(sent) == 1
        assert "less than it could prove: no GPU evidence" in outbound.entries()[-1].detail

    def test_the_record_is_looked_up_by_the_receipt_and_route_the_reply_names(
            self, tee_stream):
        """A reply RedPill's gateway answers names its receipt and, now and then, a
        route (Nov 2026): its record is found by the receipt, the route named with it.
        For a model Chutes answers first, only such a reply is looked up (Nov 10 2026)."""
        from tests.test_attestation import InstanceEvidence, ShapedNanoGpt
        server = FakeNanoGpt(aci="bound", receipt_id="rcpt-7", route="r2")
        tee_stream(server)
        settings = private_settings(server=server)
        ai.chat([], settings, json_reply=False)
        assert ai.session_of(settings).last_receipt().state == private_ai.VERIFIED
        assert "/tee/signature/rcpt-7" in [g for g in server.gets if "/tee/signature/" in g][-1]
        assert server.lookups[-1].get("X-TEE-Verification-Route") == "r2"
        assert not any("X-TEE-Verification-Route" in r["headers"] for r in server.reports)

        instances = InstanceEvidence()
        chutes = ShapedNanoGpt(lambda nonce: (200, instances.body(nonce)), aci="bound")
        tee_stream(chutes)
        settings = private_settings(model="TEE/glm-5.1", server=chutes)
        ai.chat([], settings, json_reply=False)
        assert ai.session_of(settings).last_receipt().state == private_ai.UNCHECKED
        assert chutes.lookups == [], "Chutes answered: nothing to look up"
        chutes.receipt_id = "rcpt-8"
        ai.chat([], settings, json_reply=False)
        assert ai.session_of(settings).last_receipt().state == private_ai.VERIFIED

    def test_what_was_sent_is_received_only_once_its_replys_record_shows_it(
            self, tee_stream, monkeypatch):
        """A reply with no signed record (Chutes keeps none) or one whose check failed
        leaves what was sent not shown to reach the attested enclave; a stream that
        ended short whose record the attested key signed was received, though no answer
        came of it; and one that never brought a reply says nothing of it."""
        from tests.test_attestation import InstanceEvidence, ShapedNanoGpt
        instances = InstanceEvidence()
        chutes = ShapedNanoGpt(lambda nonce: (200, instances.body(nonce)))
        tee_stream(chutes)
        ai.chat([], private_settings(model="TEE/kimi-k3", server=chutes), json_reply=False)
        assert outbound.entries()[-1].how.endswith(
            ": ⚠ not shown to reach the attested enclave")
        other = FakeNanoGpt(signed_bytes=b"data: [DONE]\n\n")
        tee_stream(other)
        ai.chat([], private_settings(server=other), json_reply=False)
        assert outbound.entries()[-1].how.endswith(
            ": ⚠ not shown to reach the attested enclave")
        cut = FakeNanoGpt(cut=True)
        tee_stream(cut)
        with pytest.raises(ai.AIError):
            ai.chat([], private_settings(server=cut), json_reply=False)
        failed = outbound.entries()[-1]
        assert failed.outcome == outbound.FAILED
        assert failed.how.endswith(": received by the attested TEE")
        monkeypatch.setattr(netguard, "_transport", lambda request, timeout: Reply(
            b"<html></html>", {"Content-Type": "text/html"}))
        with pytest.raises(ai.AIError):
            ai.chat([], private_settings(server=FakeNanoGpt()), json_reply=False)
        assert outbound.entries()[-1].how == (
            f"Sends it to {TEE_MODEL}, attested first, through Nano-GPT's gateway, which "
            f"can read it")

    def test_through_the_gateway_alone_a_reply_needs_a_receipt_that_holds(self, tee_stream):
        """A model attested through RedPill's gateway alone has its own enclave only as
        the gateway's word, in each reply's receipt: a reply with none is not verified
        on the gateway's signature alone, and says why. From SealedLore (dd39efc), by
        way of DA Assistant (9b9743b)."""
        from tests.test_attestation import RELAYED, ShapedNanoGpt
        bare = ShapedNanoGpt(lambda nonce: (200, RELAYED), aci="bound")
        tee_stream(bare)
        settings = private_settings(model="TEE/deepseek-v3.2", server=bare)
        assert ai.chat([], settings, json_reply=False) == "Hello there"
        receipt = ai.session_of(settings).last_receipt()
        assert receipt.state == private_ai.UNCHECKED
        assert "it came with no receipt" in receipt.detail
        assert private_ai.receipt_label(receipt)[0] == "⚠ TEE, not verified"
        held = ShapedNanoGpt(lambda nonce: (200, RELAYED), aci="bound", receipts=True,
                             receipt_id="rcpt-1")
        tee_stream(held)
        settings = private_settings(model="TEE/deepseek-v3.2", server=held)
        ai.chat([], settings, json_reply=False)
        receipt = ai.session_of(settings).last_receipt()
        assert receipt.state == private_ai.VERIFIED, receipt.detail
        assert "RedPill's receipt for it holds" in receipt.detail

    def test_strict_attests_again_after_a_reply_less_than_verified(self):
        """Signed by the attested key but not shown to be for the text that came, no
        record to read, or a record of other bytes: less than a verified reply, and
        Strict has the model attested again before anything more is sent to it. A
        verified one, or any reply without Strict, is not. From DA Assistant's tests
        (Nov 2026)."""
        from tests.test_attestation import stream_hash

        def replies(server, strict=True, **record):
            session = PrivacySession(BASE, "sk-1", TEE_MODEL, strict=strict,
                                     tee_factory=server.tee_client)
            session.ensure()
            state = session.check_reply("chatcmpl-1", **record).state
            session.ensure()
            return state, len(server.plain_reports())

        # On an enclave that proves everything (GpusTied), which Strict accepts.
        came = {"body_hash": stream_hash(FakeNanoGpt().stream())}
        assert replies(GpusTied(tied="statement")) == (private_ai.SIGNED, 2)
        assert replies(GpusTied(tied="statement", signature_status=500),
                       **came) == (private_ai.UNCHECKED, 2)
        assert replies(GpusTied(tied="statement", signed_bytes=b"data: [DONE]\n\n"),
                       **came) == (private_ai.FAILED, 2)
        assert replies(GpusTied(tied="statement"), **came) == (private_ai.VERIFIED, 1)
        assert replies(GpusTied(tied="statement"), strict=False) == (private_ai.SIGNED, 1)

    def test_strict_attests_again_after_a_signature_by_another_key(self, tee_stream):
        server = GpusTied(tied="statement", signer=0x1234)
        tee_stream(server)
        settings = private_settings(server=server, strict=True)
        ai.chat([], settings, json_reply=False)
        assert ai.session_of(settings).last_receipt().state == private_ai.FAILED
        ai.chat([], settings, json_reply=False)
        assert len(server.plain_reports()) == 2


class TestSealed:
    def test_the_body_is_sealed_and_shaped_and_nothing_else_is_sent(self, endpoint):
        fake = FakeSealed()
        settings = private_settings(model=SEALED_MODEL, sealed_client=fake)
        messages = [{"role": "user", "content": [ai.text_part("Glass Harbor"),
                                                 ai.image_part(b"\xff\xd8frame")]}]
        assert ai.chat_json(messages, settings, online=True) == {"ok": True}
        assert endpoint == [], "nothing went to the plain endpoint"
        ((model, payload),) = fake.sealed
        assert model == SEALED_MODEL and payload["model"] == SEALED_MODEL
        assert payload["reasoning"] == {"effort": "low"} and "reasoning_effort" not in payload
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["messages"] == messages
        assert fake.accepted is not None, "the proof is sealed to only once accepted"
        assert ai.session_of(settings).last_receipt().state == private_ai.SEALED
        entry = outbound.entries()[-1]
        assert entry.protection == outbound.SEALED and "only ciphertext" in entry.how
        assert entry.what.startswith("1 frame and ")
        assert "(a frame of the video)" in entry.content and "\\xff" not in entry.content

    def test_one_attestation_serves_the_operation(self, endpoint):
        fake = FakeSealed()
        settings = private_settings(model=SEALED_MODEL, sealed_client=fake)
        for _ in range(3):
            ai.chat([], settings)
        assert len(fake.sealed) == 3
        assert fake.attested == 3, "asked each time; sealed.Enclave keeps it five minutes"

    def test_a_reply_cut_short_is_refused(self, endpoint):
        settings = private_settings(model=SEALED_MODEL, sealed_client=FakeSealed(done=False))
        with pytest.raises(ai.AIError, match="ended before it was complete"):
            ai.chat([], settings)
        assert outbound.entries()[-1].outcome == outbound.FAILED

    def test_a_refusal_before_sending_says_nothing_went_in_the_clear(self, endpoint):
        refusing = FakeSealed(failure=PrivacyError("nano-gpt refused it; nothing was sent"))
        with pytest.raises(ai.Refused, match="nothing was sent in the clear"):
            ai.chat([], private_settings(model=SEALED_MODEL, sealed_client=refusing))
        assert endpoint == []
        assert outbound.entries()[-1].outcome == outbound.REFUSED

    def test_a_retired_model_is_sent_nothing_and_says_why(self, endpoint):
        retired = FakeSealed(retired=True)
        with pytest.raises(ai.Refused, match="no longer offered"):
            ai.chat([], private_settings(model=SEALED_MODEL, sealed_client=retired))
        assert retired.sealed == [] and endpoint == []


class TestNeverTheOrdinaryWayRound:
    def test_a_private_model_typed_to_search_the_web_is_refused(self, endpoint):
        """":online" on a model's name has nano-gpt search the web with what is
        sent, which it can do only by reading it."""
        settings = private_settings(model="TEE/glm-5.1:online")
        assert "never searches" in ai.not_ready(settings)
        with pytest.raises(ai.NotConfigured, match="never searches"):
            ai.chat([], settings)
        # Held to at the moment of sending too, however the session was made.
        session = PrivacySession(BASE, "sk-1", "TEE/glm-5.1:Online/kagi",
                                 tee_factory=FakeNanoGpt().tee_client)
        with pytest.raises(ai.Refused, match="never does"):
            ai._private_chat({"model": "x"}, [], session)
        assert endpoint == []
        assert outbound.entries()[-1].outcome == outbound.REFUSED

    def test_a_private_model_is_never_sent_unsealed(self, endpoint):
        settings = ai.settings_from({privacy.AI: True, ai.SETTING_KEY: "sk-1",
                                     ai.SETTING_MODEL: " Private/kimi-k3"})
        assert "only ever sent sealed" in ai.not_ready(settings)
        with pytest.raises(ai.NotConfigured, match="only ever sent sealed"):
            ai.chat([], settings)
        assert endpoint == []

    def test_however_it_is_written_a_model_counts_as_its_kind_everywhere(self, endpoint):
        """Told by its exact prefix, "Private/glm-5-3" was no private model at all to
        Settings and Privacy Mode, which refused it, while the plain path refused it as
        end-to-end encrypted; and "tee/glm-5.3" ran in no enclave. Every place asks one
        question now, the id stripped and in lower case, as DA Assistant's does
        (fdf58c2, from DA Vibe Manager and DA Private Coder, Nov 2026)."""
        for model in ("Private/glm-5-3", " private/glm-5-3", "PRIVATE/glm-5-3 "):
            assert private_ai.is_private_mode(model) and private_ai.sealed_only(model), model
            assert private_ai.kind_of(model) == private_ai.E2EE, model
            assert private_ai.describe(model)[0] == "end-to-end encrypted"
        for model in ("tee/glm-5.3", " TEE/glm-5.3"):
            assert private_ai.kind_of(model) == private_ai.TEE, model
        for model in ("privately/x", "openai/private/x", "glm-5.3", "", None):
            assert private_ai.kind_of(model) is None, model
        assert private_ai.problems(BASE, "Private/glm-5-3", wait=False) == []
        assert private_ai.problems(BASE, "tee/glm-5.3", wait=False) == []
        assert ai.not_ready(private_settings(model="Private/glm-5-3")) == ""
        # Attested before anything is sent, however it is written; and never plain.
        settings = private_settings(model="tee/glm-5.3", server=FakeNanoGpt(nonce_bound=False))
        with pytest.raises(ai.Refused, match="did not bind its signing key"):
            ai.chat([], settings)
        assert endpoint == []

    def test_nor_listed_among_the_ordinary_models(self, monkeypatch):
        monkeypatch.setattr(netguard, "_transport", lambda r, t: Reply({"data": [
            {"id": "private/kimi-k3", "capabilities": {"vision": True}},
            {"id": "TEE/kimi-k3", "capabilities": {"vision": True}},
            {"id": "anthropic/claude-sonnet-5", "capabilities": {"vision": True}}]}))
        models = ai.list_models(ai.settings_from({privacy.AI: True}))
        assert [m["id"] for m in models] == ["anthropic/claude-sonnet-5", "TEE/kimi-k3"]
        assert models[1]["note"] == ai.TEE_NOTE

    def test_privacy_mode_never_searches_the_web(self):
        settings = private_settings(**{privacy.WEB_SEARCH: True})
        assert not ai.may_search(settings)
        assert ai.model_name(settings, online=True) == TEE_MODEL


class TestItOnlyEverTightens:
    """An operation's settings are loaded when it starts; Settings can't
    loosen them after, and whatever it tightens is held to at the next
    request."""

    def test_privacy_mode_switched_on_since_stops_the_ordinary_model(self, endpoint):
        settings = ai.settings_from({privacy.AI: True, ai.SETTING_KEY: "sk-1"})
        ai.chat([], settings)
        save(**{ai.SETTING_PRIVACY_MODE: True})
        with pytest.raises(ai.NotConfigured, match="Privacy Mode was switched on"):
            ai.chat([], settings)
        assert len(endpoint) == 1

    def test_privacy_mode_switched_off_since_leaves_a_private_run_private(self, endpoint):
        fake = FakeSealed()
        settings = private_settings(model=SEALED_MODEL, sealed_client=fake)
        save(**{ai.SETTING_PRIVACY_MODE: False})
        ai.chat([], settings)
        assert len(fake.sealed) == 1 and endpoint == []

    def test_the_ai_switched_off_since_stops_either(self, endpoint):
        ordinary = ai.settings_from({privacy.AI: True, ai.SETTING_KEY: "sk-1"})
        private = private_settings(model=SEALED_MODEL)
        save(**{privacy.AI: False})
        for settings in (ordinary, private):
            with pytest.raises(ai.NotConfigured, match="Settings → Privacy"):
                ai.chat([], settings)
        assert endpoint == []

    def test_the_web_search_switched_off_since_isnt_made(self, endpoint):
        settings = ai.settings_from({privacy.AI: True, privacy.WEB_SEARCH: True,
                                     ai.SETTING_KEY: "sk-1"})
        save(**{privacy.WEB_SEARCH: False})
        ai.chat([], settings, online=True)
        assert endpoint[0]["body"]["model"] == ai.DEFAULT_MODEL

    def test_settings_own_form_goes_by_what_it_shows(self, endpoint):
        save(**{ai.SETTING_PRIVACY_MODE: True})
        form = ai.settings_from({privacy.AI: True, ai.SETTING_KEY: "sk-1"}, form=True)
        ai.ping(form)
        assert len(endpoint) == 1


class TestWhereItGoesInWords:
    def test_each_way_out_says_who_can_read_it(self):
        def where(settings, online=False):
            return ai.destination(settings, online)

        ordinary = ai.settings_from({privacy.AI: True, privacy.WEB_SEARCH: True})
        text, protection = where(ordinary, online=True)
        assert protection == outbound.CLEAR
        assert "claude-sonnet-5 through Nano-GPT" in text and "both can read it" in text
        assert "Kagi is sent a search about the show" in text
        assert "search" not in where(ordinary)[0]
        tee_outside = ai.settings_from({ai.SETTING_MODEL: "TEE/kimi-k3"})
        assert "isn't attested first" in where(tee_outside)[0]
        local = ai.settings_from({ai.SETTING_BASE_URL: "http://192.168.1.20:11434/v1"})
        assert "this computer's own network" in where(local)[0]
        other = ai.settings_from({ai.SETTING_BASE_URL: "https://models.example/v1"})
        assert "at models.example, which can read it" in where(other)[0]
        text, protection = where(private_settings(model=SEALED_MODEL))
        assert protection == outbound.SEALED and "only ciphertext" in text
        text, protection = where(private_settings(model=TEE_MODEL))
        assert protection == outbound.TEE and "gateway can read" in text
        # What is attested for most TEE/ models is RedPill's gateway, not "its enclave".
        assert "attested first - most TEE/ models as RedPill's TEE gateway" in text
        assert "its enclave" not in text and "runs in a trusted enclave" not in text
        assert ai.protection_label(private_settings(model=SEALED_MODEL)) == (
            "Privacy Mode: end-to-end encrypted")
        tee = private_settings(model=TEE_MODEL, server=FakeNanoGpt(aci="bound"))
        assert ai.protection_label(tee) == "Privacy Mode: TEE, attested first"
        ai.session_of(tee).ensure()
        assert ai.protection_label(tee) == "Privacy Mode: TEE gateway attested"


class TestTheModelsOnOffer:
    def test_private_ones_first_priced_from_their_twins_and_able_to_see(self, monkeypatch):
        asked = []

        def transport(request, timeout):
            asked.append(request)
            if request.full_url.endswith("/private/tinfoil/models"):
                return Reply({"data": [{"id": "private/gpt-oss-120b"},
                                       {"id": "private/deepseek-v4-1-flash"},
                                       {"id": "private/kimi-k3"}]})
            return Reply({"data": [
                {"id": "TEE/kimi-k3", "name": "Kimi K3", "capabilities": {"vision": True},
                 "pricing": {"prompt": 2, "completion": 10}},
                {"id": "TEE/deepseek-v4.1-flash", "capabilities": {"vision": True}},
                {"id": "TEE/gpt-oss-120b", "capabilities": {"vision": False}},
                {"id": "anthropic/claude-sonnet-5", "capabilities": {"vision": True}}]})

        monkeypatch.setattr(netguard, "_transport", transport)
        models = ai.list_private_models(ai.settings_from({privacy.AI: True,
                                                          ai.SETTING_KEY: "sk-secret"}))
        assert [m["id"] for m in models] == [
            "private/kimi-k3", "private/deepseek-v4-1-flash",
            "TEE/deepseek-v4.1-flash", "TEE/kimi-k3"]
        assert models[0]["name"] == "Kimi K3 (end-to-end encrypted)"
        assert models[0]["price"] == "$2.1 in / $10.5 out per million tokens"
        assert models[2]["kind"] == private_ai.TEE and "gateway can read" in models[2]["note"]
        assert all(r.get_header("Authorization") is None for r in asked), "no key is sent"

    def test_check_attestation_attests_and_sends_nothing_to_read(self, monkeypatch):
        fake = FakeSealed()
        monkeypatch.setattr(sealed, "PrivateModeClient", lambda *args, **kwargs: fake)
        settings = ai.settings_from({privacy.AI: True, ai.SETTING_PRIVACY_MODE: True},
                                    form=True)
        session = ai.check_attestation(settings)
        assert session.attestation.headline == "End-to-end encrypted"
        assert fake.sealed == []
        (entry,) = outbound.entries()
        assert entry.protection == outbound.CHECK


class TestNothingGoesUnlisted:
    def test_a_malformed_answer_is_an_ai_error_and_listed(self, monkeypatch):
        import http.client

        def broken(request, timeout):
            raise http.client.BadStatusLine("HTTP/9 nonsense")

        monkeypatch.setattr(netguard, "_transport", broken)
        with pytest.raises(ai.AIError):
            ai.chat([], ai.settings_from({privacy.AI: True, ai.SETTING_KEY: "sk-1"}))
        (entry,) = outbound.entries()
        assert entry.outcome == outbound.FAILED

    def test_nothing_is_attested_while_the_ai_isnt_allowed(self, monkeypatch):
        monkeypatch.setattr(sealed, "PrivateModeClient",
                            lambda *a, **k: pytest.fail("attested"))
        settings = ai.settings_from({privacy.AI: False, ai.SETTING_PRIVACY_MODE: True},
                                    form=True)
        with pytest.raises(ai.NotConfigured, match="Settings → Privacy"):
            ai.check_attestation(settings)
        assert outbound.entries() == []


class TestTheAppImage:
    ROOT = os.path.dirname(HERE)

    def test_its_python_runs_isolated(self):
        """Without -I, the folder it was started from came ahead of the
        standard library: a secrets.py there stood in for the nonces' source."""
        with open(os.path.join(self.ROOT, "packaging", "appimage", "AppRun")) as f:
            launcher = f.read()
        assert 'exec "$PYTHON" -I -m mediabrowser.gui.app "$@"' in launcher
        assert "REQUESTS_CA_BUNDLE" in launcher

    def test_privacy_modes_client_is_compiled_and_checked_with_the_rest(self):
        with open(os.path.join(self.ROOT, "packaging", "appimage", "build.sh")) as f:
            build = f.read()
        assert '-s "$APPDIR" -p / "$PRIVATE_MODE"' in build
        assert "--require-hashes" in build and "$PRIVATE_MODE_LOCK" in build

    def test_inside_an_appimage_the_users_own_client_is_never_read(self, monkeypatch,
                                                                   tmp_path):
        from mediabrowser.core import vendored

        monkeypatch.setattr(vendored.sys, "prefix", str(tmp_path / "app" / "opt" / "py"))
        monkeypatch.setenv("APPDIR", str(tmp_path / "app"))
        assert vendored.in_appimage() and vendored.candidates() == []
        (tmp_path / "app" / "opt" / "py" / "private-mode" / "tinfoil").mkdir(parents=True)
        assert vendored.candidates() == [vendored.bundled_dir()]
        monkeypatch.delenv("APPDIR")
        monkeypatch.setattr(vendored.sys, "prefix", str(tmp_path / "venv"))
        assert vendored.candidates() == [vendored.user_dir()]


class TestHousekeeping:
    def test_the_data_folder_is_kept_to_its_owner(self, tmp_path):
        folder = tmp_path / "data"
        folder.mkdir(mode=0o755)
        os.chmod(folder, 0o755)
        config.keep_to_self(folder)
        assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
        config.keep_to_self(tmp_path / "not-there")       # nothing to do, no error

    def test_what_was_sent_is_its_words_and_a_count_of_its_frames(self):
        text, pictures = outbound.text_of([
            {"role": "system", "content": "You name chapters."},
            {"role": "user", "content": [ai.text_part("Paper Lanterns?"),
                                         ai.image_part(b"\xff\xd8a"),
                                         ai.image_part(b"\xff\xd8b")]}])
        assert pictures == 2
        assert "You name chapters." in text and "Paper Lanterns?" in text
        assert text.count("(a frame of the video)") == 2

    def test_the_list_says_what_each_request_was_for(self):
        with outbound.purpose("Detect Chapters: Glass Harbor - Live at Copperfield Hall"):
            outbound.record("MusicBrainz", "https://musicbrainz.org/ws/2/release", "a search",
                            outbound.CLEAR, "Over https")
        outbound.record("MusicBrainz", "https://musicbrainz.org/", "another", outbound.CLEAR,
                        "Over https")
        first, second = outbound.entries()
        assert first.purpose.startswith("Detect Chapters") and second.purpose == ""

    def test_the_relays_words_are_one_short_line_with_nothing_hidden(self):
        said = private_ai.unsealed_words("Model says:\nok‮​" + "x" * 900, 200)
        assert "\n" not in said and "‮" not in said and "​" not in said
        assert said.startswith("Model says: ok") and len(said) <= 200


# ---- with Tinfoil's own client ---------------------------------------------------


def _sdk():
    try:
        return sealed.available()[0]
    except Exception:  # noqa: BLE001
        return False


needs_sdk = pytest.mark.skipif(not _sdk(), reason="Tinfoil's client is not installed "
                                                  "(tools/install-private-mode.sh)")


@needs_sdk
class TestTheRealSealedClient:
    """sealed.PrivateModeClient itself, its enclave and transport mocked: from
    DA Assistant's tests of the same client."""

    MODEL = "private/glm-5-3"

    def verified(self, key="ab" * 32, **changes):
        import dataclasses
        proof = sealed.Verified(hpke_public_key=key, enclave="router-0.tinfoil.sh",
                                hardware="AMD SEV-SNP", measurement="m",
                                release_digest="d" * 64, release_tag="v0.0.171",
                                latest_release="", latest_checked=True)
        return dataclasses.replace(proof, **changes)

    def make(self, handler, verifier=None, routes=None):
        import httpx
        calls = {"verify": 0}

        def verify(relay, secret):
            calls["verify"] += 1
            return (verifier or self.verified)()

        def preflight(request):
            return httpx.Response(200, json={"cacheScope": "c" * 64,
                                             "upstreamModel": "glm-5.3"})

        def transport(key, route):
            if routes is not None:
                routes.append(route)
            return httpx.MockTransport(handler)

        client = sealed.PrivateModeClient(
            BASE, "sk-1", enclave=sealed.Enclave(sealed.relay_url(BASE), verifier=verify),
            transport_factory=transport,
            preflight_client=httpx.Client(transport=httpx.MockTransport(preflight)))
        return client, calls

    def stream_body(self, finished=True):
        body = b'data: {"id":"c1","choices":[{"delta":{"content":"{\\"ok\\": true}"}}]}\n\n'
        if finished:
            body += (b'data: {"id":"c1","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                     b"data: [DONE]\n\n")
        return body

    def settings_with(self, client, strict=False):
        settings = private_settings(model=self.MODEL, strict=strict)
        session = PrivacySession(BASE, "sk-1", self.MODEL, strict=strict,
                                 sealed_factory=lambda *a: client)
        settings[ai.SESSION] = session
        return settings

    def test_a_request_is_attested_shaped_sealed_and_opened(self, endpoint):
        import httpx
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            seen["headers"] = request.headers
            return httpx.Response(200, headers={"x-nanogpt-private-mode": "tinfoil"},
                                  content=self.stream_body())

        client, calls = self.make(handler)
        settings = self.settings_with(client)
        assert ai.chat_json([{"role": "user", "content": "hi"}], settings) == {"ok": True}
        assert calls["verify"] == 1 and endpoint == []
        assert seen["body"]["model"] == "glm-5.3"
        assert seen["body"][sealed.CACHE_SECRET_FIELD] == client.enclave.cache_secret
        assert seen["body"]["reasoning_effort"] == "low", "GLM's spelling of it"
        assert seen["headers"][sealed.ENCLAVE_URL_HEADER] == "https://router-0.tinfoil.sh"
        assert ai.session_of(settings).last_receipt().state == private_ai.SEALED

    def test_a_reply_not_marked_private_is_refused(self, endpoint):
        import httpx
        client, _ = self.make(lambda r: httpx.Response(200, content=self.stream_body()))
        with pytest.raises(ai.AIError, match="did not come back through Private Mode"):
            ai.chat([], self.settings_with(client))

    def test_a_reply_that_is_not_2xx_is_refused_unread(self, endpoint):
        import httpx
        for status in (301, 302, 307, 308):
            client, _ = self.make(lambda r, status=status: httpx.Response(
                status, headers={"x-nanogpt-private-mode": "tinfoil",
                                 "location": "https://nano-gpt.com/elsewhere"},
                content=self.stream_body()))
            with pytest.raises(ai.AIError, match=f"HTTP {status} instead of a sealed reply"):
                ai.chat([], self.settings_with(client))

    def test_a_stream_cut_short_is_refused(self, endpoint):
        import httpx
        client, _ = self.make(lambda r: httpx.Response(
            200, headers={"x-nanogpt-private-mode": "tinfoil"},
            content=self.stream_body(finished=False)))
        with pytest.raises(ai.AIError, match="ended before it was complete"):
            ai.chat([], self.settings_with(client))

    def test_what_the_relay_says_in_an_error_is_its_own_on_one_line(self, endpoint):
        import httpx
        body = "Model says:\nThe answer is 42.‮​" + "x" * 5000
        client, _ = self.make(lambda r: httpx.Response(500, text=body))
        with pytest.raises(ai.AIError) as caught:
            ai.chat([], self.settings_with(client))
        message = str(caught.value)
        assert "nano-gpt's relay said (unauthenticated, not the model): Model says:" in message
        assert not any(unseen in message for unseen in ("\n", "‮", "​"))

    def test_a_plain_http_address_is_refused_by_the_client_itself(self):
        plain = "http://nano-gpt.com/api/v1"
        with pytest.raises(PrivacyError, match="is not https"):
            sealed.PrivateModeClient(plain, "k", enclave=sealed.Enclave(
                sealed.relay_url(plain), verifier=lambda r, s: pytest.fail("attested")))

    def test_a_failed_attestation_sends_nothing(self, endpoint):
        def refuse():
            raise RuntimeError("bad measurement")

        client, _ = self.make(lambda r: pytest.fail("sent before attesting"),
                              verifier=refuse)
        with pytest.raises(ai.Refused, match="could not be attested: bad measurement"):
            ai.chat([], self.settings_with(client))

    def test_strict_holds_an_enclave_attested_again_mid_request(self, endpoint):
        import httpx
        posts = []

        def handler(request):
            posts.append(request)
            if len(posts) == 1:
                return httpx.Response(400, json={"error": "missing_encrypted_body_header"})
            return httpx.Response(200, headers={"x-nanogpt-private-mode": "tinfoil"},
                                  content=self.stream_body())

        proofs = iter([self.verified(),
                       self.verified("cd" * 32, enclave="router-old.tinfoil.sh",
                                     latest_release="v0.0.200")])
        client, _ = self.make(handler, verifier=lambda: next(proofs))
        with pytest.raises(ai.AIError, match="attested again, proved less than it could"):
            ai.chat([], self.settings_with(client, strict=True))
        assert len(posts) == 1

    def test_the_sealed_request_goes_by_the_environments_proxy(self, endpoint, monkeypatch):
        import httpx
        routes = []
        monkeypatch.setenv("https_proxy", "http://proxy.example:3128")
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        client, _ = self.make(lambda r: httpx.Response(
            200, headers={"x-nanogpt-private-mode": "tinfoil"}, content=self.stream_body()),
            routes=routes)
        ai.chat([], self.settings_with(client))
        assert routes == ["http://proxy.example:3128"]

    def test_a_private_model_written_another_way_goes_sealed_and_only_sealed(self, endpoint):
        """Privacy Mode refused "Private/glm-5-3" as no private model, and sealed.py as
        no Private Mode model of its own: now it is one, sealed like the rest, and
        nothing of it goes by the plain path. From DA Assistant's tests (fdf58c2)."""
        import httpx
        seen = []

        def handler(request):
            seen.append(request.headers)
            return httpx.Response(200, headers={"x-nanogpt-private-mode": "tinfoil"},
                                  content=self.stream_body())

        client, _calls = self.make(handler)
        model = "Private/glm-5-3"
        settings = private_settings(model=model)
        session = PrivacySession(BASE, "sk-1", model, sealed_factory=lambda *a: client)
        settings[ai.SESSION] = session
        assert ai.chat_json([{"role": "user", "content": "hi"}], settings) == {"ok": True}
        assert len(seen) == 1
        assert session.last_receipt().state == private_ai.SEALED
        assert endpoint == [], "something went by the plain path"

    def test_nothing_ehbp_did_not_seal_is_sent_to_the_enclave(self, monkeypatch):
        """EHBP passes a request it finds no body in on as it came, unsealed, and the
        transport under it sent that too. It refuses anything without EHBP's
        encapsulated key now, as DA Assistant's and DA Vibe Manager's do; what EHBP
        sealed goes. From DA Assistant's tests (8139d72, Nov 2026)."""
        import httpx
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        key = X25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        sent = []

        def network(_transport, request):
            sent.append((request.headers, request.read()))
            return httpx.Response(500, text="stop here")

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", network)
        url = sealed.relay_url(BASE) + "/v1/chat/completions"
        with httpx.Client(transport=sealed._sealing_transport(key)) as client:
            client.post(url, json={"messages": [{"role": "user", "content": "my secret"}]})
            with pytest.raises(PrivacyError, match="was not sealed, so it was refused"):
                client.post(url, content=b"")
        assert len(sent) == 1, "the unsealed request was sent"
        headers, body = sent[0]
        assert "Ehbp-Encapsulated-Key" in headers
        assert b"my secret" not in body

    def test_bodies_are_shaped_per_family(self):
        body = sealed.shape_body({"model": "x", "messages": [], "temperature": 0,
                                  "reasoning": {"effort": "low"}, "extra": 1},
                                 "private/kimi-k3", "kimi-k3")
        assert "temperature" not in body and "extra" not in body
        assert body["reasoning_effort"] == "low"
        flash = sealed.shape_body({"messages": [], "reasoning": {"effort": "low"}},
                                  "private/deepseek-v4-1-flash", "deepseek-v4.1-flash")
        assert flash["chat_template_kwargs"] == {"thinking": False}
        assert "reasoning_effort" not in flash, "a model that thinks only when asked isn't"


class TestTheRelaysContract:
    """The sealed client against the contract of nano-gpt's own Private Mode
    client, @nanogpt/private-mode on npm, as last read
    (tests/data/nanogpt_private_mode_proxy.json): every field it may seal is
    one the proxy lets through, every x-nanogpt header it names is one the
    proxy sends or reads, and the relay's paths are the proxy's."""

    @pytest.fixture
    def contract(self):
        with open(os.path.join(HERE, "data", "nanogpt_private_mode_proxy.json")) as f:
            return json.load(f)

    def test_every_field_we_may_seal_is_one_the_proxy_lets_through(self, contract):
        assert sealed.ALLOWED_FIELDS <= set(contract["body_fields"])
        assert sealed.CACHE_SECRET_FIELD not in contract["body_fields"]

    def test_every_header_we_name_is_one_the_proxy_sends_or_reads(self, contract):
        import inspect
        source = inspect.getsource(sealed)
        headers = set(re.findall(r'"(x-nanogpt-[a-z-]+)"', source))
        assert headers
        assert headers <= set(contract["request_headers"]) | set(contract["reply_headers"])
        assert f'"{contract["reply_header_private_mode_value"]}"' in source

    def test_the_relays_paths_are_the_proxys(self, contract):
        assert contract["relay_paths"]["preflight"] == sealed.RELAY_PATH + "/preflight"
        assert contract["relay_paths"]["chat"] == sealed.RELAY_PATH + "/v1/chat/completions"
        assert contract["relay_paths"]["models"] == sealed.RELAY_MODELS_PATH


# ---- what Tinfoil's SDK fetches ---------------------------------------------------


def _requests():
    try:
        import requests  # noqa: F401
    except ImportError:
        return False
    return True


FAKE_SDK = {
    "__init__.py": "from .client import SecureClient\n",
    "client.py": (
        "from .github import fetch_latest_release\n\n"
        "class SecureClient:\n"
        "    def __init__(self, **kwargs):\n"
        "        self.enclave = 'enclave.example'\n\n"
        "    def verify(self):\n"
        "        return fetch_latest_release('owner/repo')\n"),
    "github.py": (
        "import requests\n\n"
        "def fetch_latest_release(repo):\n"
        "    response = requests.get(f'https://github-proxy.example/repos/{repo}', timeout=5)\n"
        "    response.raise_for_status()\n"
        "    return response.text\n"),
    "later/__init__.py": "",
    "later/collateral.py": (
        "from requests import get\n\n"
        "def fetch(url):\n"
        "    return get(url, timeout=5).text\n"),
}


@pytest.mark.skipif(not _requests(), reason="requests comes only with Tinfoil's client")
class TestTheSdkFetchesThroughNetguard:
    """Tinfoil's SDK fetches with bare requests.get - its release, Sigstore's
    bundle, AMD's and Intel's certificates - and requests follows a redirect
    anywhere, https to plain http included. sealed.guard_sdk_requests hands
    each of its modules netguard's rules instead. The SDK here is a fake one,
    in the shapes the real one takes requests in; nothing reaches the network.
    From DA Assistant's tests/test_sdk_redirects.py."""

    URL = "https://github-proxy.example/repos/owner/repo"

    @pytest.fixture
    def sdk(self, tmp_path, monkeypatch):
        import importlib
        import sys

        import requests
        for name, text in FAKE_SDK.items():
            path = tmp_path / "tinfoil" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        for name in [n for n in sys.modules if n == "tinfoil" or n.startswith("tinfoil.")]:
            monkeypatch.delitem(sys.modules, name)
        monkeypatch.syspath_prepend(str(tmp_path))
        importlib.invalidate_caches()
        importlib.import_module("tinfoil")
        state = {"sent": [], "answers": {}}

        def adapter_send(adapter, request, **kwargs):
            state["sent"].append(request.url)
            response = requests.Response()
            response.url, response.request = request.url, request
            target = state["answers"].get(request.url)
            response.status_code = 302 if target else 200
            if target:
                response.headers["Location"] = target
            response._content = b"" if target else b"fine"
            return response

        monkeypatch.setattr("requests.adapters.HTTPAdapter.send", adapter_send)
        yield state
        for name in [n for n in sys.modules if n == "tinfoil" or n.startswith("tinfoil.")]:
            sys.modules.pop(name, None)

    def test_guarded_a_redirect_into_the_clear_is_refused(self, sdk):
        import importlib
        sealed.guard_sdk_requests()
        sdk["answers"][self.URL] = "http://evil.example/release"
        import requests
        with pytest.raises(requests.RequestException, match="not followed"):
            importlib.import_module("tinfoil.github").fetch_latest_release("owner/repo")
        assert sdk["sent"] == [self.URL]

    def test_a_redirect_that_stays_on_https_is_followed(self, sdk):
        import importlib
        sealed.guard_sdk_requests()
        sdk["answers"][self.URL] = "https://objects.example/release"
        assert importlib.import_module("tinfoil.github").fetch_latest_release("owner/repo") \
            == "fine"
        assert sdk["sent"] == [self.URL, "https://objects.example/release"]

    def test_a_function_taken_by_name_in_a_module_loaded_later_is_guarded(self, sdk):
        import importlib

        import requests
        given = sealed.guard_sdk_requests()
        assert "tinfoil.later.collateral" in given
        sdk["answers"]["https://kds.example/vcek"] = "http://kds.example/vcek"
        with pytest.raises(requests.RequestException):
            importlib.import_module("tinfoil.later.collateral").fetch(
                "https://kds.example/vcek")
        assert sdk["sent"] == ["https://kds.example/vcek"]
