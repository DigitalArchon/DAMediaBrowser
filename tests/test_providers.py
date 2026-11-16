# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The one endpoint's providers - NanoGPT, Chutes, Tinfoil, Anthropic, another
OpenAI-compatible service - as the AI's wiring takes them (ai.py): the provider read
from the address however it got in, a key per provider bound to its origins, each
request routed by provider and model (Anthropic natively, Tinfoil's every model sealed,
Chutes' -TEE ones sealed and its others plain), Privacy Mode's choices, the frames, the
three privacy levels and the rule an operation keeps when its model changes. The network
is faked at urllib, as test_ai.py fakes it; the sealed clients are stood in for."""

import http.client
import io
import json
from contextlib import contextmanager

import pytest

from mediabrowser.core import (
    ai,
    levels,
    netguard,
    outbound,
    privacy,
    private_ai,
    sealed,
    services,
    store,
)

TINFOIL = services.TINFOIL_BASE_URL
CHUTES = services.CHUTES_BASE_URL
CLAUDE_CHUTES = services.CHUTES_ANTHROPIC_URL
ANTHROPIC = services.ANTHROPIC_URL
TEE_MODEL = "Qwen/Qwen3.6-27B-TEE"


class Answer(io.BytesIO):
    """An answer as urllib hands one to netguard: status, headers, where it landed."""

    def __init__(self, body, status=200, headers=None, url="https://x/"):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.status = self.code = status
        self.headers = http.client.HTTPMessage()
        for name, value in (headers or {}).items():
            self.headers[name] = value
        self.url = url

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def wire(monkeypatch):
    """Every request that reaches the network, answered by `answer(request)`."""
    state = {"calls": [], "answer": lambda request: Answer({})}

    def urlopen(request, timeout=None):
        state["calls"].append(request)
        return state["answer"](request)

    monkeypatch.setattr(ai.urllib.request, "urlopen", urlopen)
    monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
    return state


def form(**overrides):
    """Settings as Settings' form or settings.json has them, the AI allowed."""
    base = {privacy.AI: True, ai.SETTING_KEY: "k-1", ai.SETTING_MODEL: ai.DEFAULT_MODEL}
    base.update(overrides)
    return ai.settings_from(base)


def frame_message(text="Name the songs."):
    return [{"role": "user", "content": [ai.text_part(text), ai.image_part(b"\xff\xd8jpeg")]}]


# ---- the provider, however the address gets in ---------------------------------------

class TestTheProviderIsTheAddresss:
    @pytest.mark.parametrize("address, provider, protocol", [
        (ai.DEFAULT_BASE_URL, ai.NANOGPT, "openai"),
        ("https://NANO-GPT.com:443/api/v1/", ai.NANOGPT, "openai"),
        (TINFOIL, ai.TINFOIL, "tinfoil"),
        ("https://INFERENCE.tinfoil.sh./v1/chat/completions", ai.TINFOIL, "tinfoil"),
        ("https://inference.tinfoil.sh:443", ai.TINFOIL, "tinfoil"),
        ("https://router-2.tinfoil.sh/v1", ai.TINFOIL, "tinfoil"),
        (CHUTES, ai.CHUTES, "openai"),
        ("https://llm.CHUTES.ai/v1", ai.CHUTES, "openai"),
        (CLAUDE_CHUTES, ai.CHUTES, "anthropic"),
        (ANTHROPIC, ai.ANTHROPIC, "anthropic"),
        ("https://api.anthropic.com/v1/messages", ai.ANTHROPIC, "anthropic"),
        ("https://api.openai.com/v1", ai.OTHER, "openai"),
        ("http://192.168.1.5:8080/v1", ai.OTHER, "openai"),
        # A look-alike is nobody's.
        ("https://tinfoil.sh.example.com/v1", ai.OTHER, "openai"),
        ("https://chutes.ai.evil.example/v1", ai.OTHER, "openai"),
    ])
    def test_however_written(self, address, provider, protocol):
        assert ai.provider_of(address) == provider
        assert ai.protocol_of(address) == protocol

    def test_an_older_settings_file_naming_tinfoil_is_sealed_from_the_start(self, data_dir,
                                                                            wire):
        """No protocol is stored: an older settings.json, or one edited by hand, with
        Tinfoil's address and a model, sends that model sealed, attested first, and
        nothing in the clear."""
        store.save_app_settings({ai.SETTING_BASE_URL: "https://inference.tinfoil.sh/v1",
                                 ai.SETTING_MODEL: "llama3-3-70b", privacy.AI: True})
        settings = ai.load_settings()
        session = ai.session_of(settings)
        assert session is not None and session.kind == private_ai.E2EE
        assert session.service == levels.TINFOIL and session.model == "llama3-3-70b"
        assert ai.privacy_of(settings).level == levels.HIGHEST
        where, protection = ai.destination(settings)
        assert protection == outbound.SEALED and "Tinfoil's router" in where
        assert ai.protection_label(settings) == "Tinfoil: end-to-end encrypted"
        assert wire["calls"] == [], "loading sends nothing"

    def test_a_plain_client_never_sends_to_a_sealed_host_or_a_tee_model(self, wire):
        """When in doubt, sealed: settings that somehow come without their session still
        send nothing in the clear to Tinfoil's host, nor a Chutes -TEE model."""
        for address, model in ((TINFOIL, "gemma4-31b"), (CHUTES, TEE_MODEL),
                               (CLAUDE_CHUTES, TEE_MODEL)):
            settings = form(**{ai.SETTING_BASE_URL: address, ai.SETTING_MODEL: model})
            settings.pop(ai.SESSION)
            with pytest.raises(ai.Refused, match="nothing was sent"):
                ai.chat([{"role": "user", "content": "hi"}], settings)
        assert wire["calls"] == []

    def test_web_search_is_nano_gpts_alone(self):
        for address in (TINFOIL, CHUTES, ANTHROPIC, "https://api.openai.com/v1"):
            settings = form(**{ai.SETTING_BASE_URL: address, privacy.WEB_SEARCH: True})
            assert not ai.may_search(settings)
            assert ai.SEARCH_PROVIDERS["kagi"] not in ai.destination(settings, online=True)[0]
        assert ai.may_search(form(**{privacy.WEB_SEARCH: True}))


# ---- one key per provider ------------------------------------------------------------

class TestAKeyPerProvider:
    def test_each_provider_keeps_its_own_and_switching_back_loses_none(self, data_dir):
        ai.store_key("sk-nano", ai.DEFAULT_BASE_URL)
        ai.store_key("cpk-chutes", CHUTES)
        ai.store_key("tk-tinfoil", TINFOIL)
        ai.store_key("sk-ant", ANTHROPIC)
        for address, key in ((ai.DEFAULT_BASE_URL, "sk-nano"), (CHUTES, "cpk-chutes"),
                             (TINFOIL, "tk-tinfoil"), (ANTHROPIC, "sk-ant"),
                             (ai.DEFAULT_BASE_URL, "sk-nano")):
            store.save_app_settings({ai.SETTING_BASE_URL: address, privacy.AI: True})
            assert ai.load_settings()[ai.SETTING_KEY] == key, address

    def test_a_chutes_key_goes_to_chutes_three_origins_and_nowhere_else(self, data_dir):
        ai.store_key("cpk-chutes", CHUTES)
        store.save_app_settings({ai.SETTING_BASE_URL: CLAUDE_CHUTES, privacy.AI: True})
        settings = ai.load_settings()
        assert settings[ai.SETTING_KEY] == "cpk-chutes"
        assert ai.key_goes("https://llm.chutes.ai:443", "https://api.chutes.ai/x")
        assert not ai.key_goes("https://llm.chutes.ai:443", "https://chutes.ai.evil.example/")
        assert not ai.key_goes("https://llm.chutes.ai:443", "http://llm.chutes.ai/v1")

    def test_no_other_providers_key_is_offered_to_tinfoil_chutes_or_anthropic(self,
                                                                             data_dir):
        ai.store_key("sk-nano", ai.DEFAULT_BASE_URL)
        for address, name in ((TINFOIL, "Tinfoil"), (CHUTES, "Chutes"),
                              (ANTHROPIC, "Anthropic")):
            store.save_app_settings({ai.SETTING_BASE_URL: address, privacy.AI: True})
            settings = ai.load_settings()
            assert settings[ai.SETTING_KEY] == "", address
            assert f"{name} API key in Settings → AI" in ai.not_ready(settings)

    def test_a_key_saved_for_one_of_them_before_there_were_providers_keeps_working(
            self, data_dir, memory_keyring):
        """The one entry the app had: a key saved there for Anthropic's API (as Other,
        once) is Anthropic's still, where it was entered for."""
        memory_keyring.store[("da-media-browser", ai.KEYRING_NAME)] = "sk-ant-old"
        memory_keyring.store[("da-media-browser", ai.KEYRING_ORIGIN_NAME)] = \
            "https://api.anthropic.com:443"
        store.save_app_settings({ai.SETTING_BASE_URL: ANTHROPIC, privacy.AI: True})
        assert ai.load_settings()[ai.SETTING_KEY] == "sk-ant-old"

    def test_a_tinfoil_key_held_to_another_origin_goes_nowhere(self, data_dir):
        """Saved for Tinfoil's API, an address moved to another Tinfoil host has the key
        held back by the origin rule, and the client itself refuses it there too."""
        ai.store_key("tk-tinfoil", TINFOIL)
        store.save_app_settings({ai.SETTING_BASE_URL: "https://router-2.tinfoil.sh/v1",
                                 privacy.AI: True})
        settings = ai.load_settings()
        assert settings[ai.SETTING_KEY] == ""
        assert "entered for https://inference.tinfoil.sh" in ai.not_ready(settings)
        assert services.tinfoil_key_refusal("https://router-2.tinfoil.sh/v1",
                                            "https://inference.tinfoil.sh:443")


# ---- routing -------------------------------------------------------------------------

ANTHROPIC_STREAM = b"".join(
    b"data: " + json.dumps(event).encode() + b"\n\n" for event in (
        {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 9}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": '{"word": "OK"}'}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 4}},
        {"type": "message_stop"}))


class TestAnthropicNatively:
    @pytest.mark.parametrize("address, header, value", [
        (ANTHROPIC, "X-api-key", "sk-ant"),
        (CLAUDE_CHUTES, "Authorization", "Bearer sk-ant"),
    ])
    def test_its_messages_api_with_the_key_as_it_takes_it(self, wire, address, header, value):
        wire["answer"] = lambda request: Answer(
            ANTHROPIC_STREAM, headers={"Content-Type": "text/event-stream"})
        settings = form(**{ai.SETTING_BASE_URL: address, ai.SETTING_KEY: "sk-ant",
                           ai.SETTING_MODEL: "claude-sonnet-5"})
        assert ai.chat_json(frame_message(), settings) == {"word": "OK"}
        # On Chutes, its public list is read first (no key) for whether the model sees.
        request = wire["calls"][-1]
        assert all(r.get_header("Authorization") is None for r in wire["calls"][:-1])
        assert request.full_url == address + "/v1/messages"
        assert request.get_header(header) == value
        body = json.loads(request.data)
        assert body["model"] == "claude-sonnet-5" and body["stream"] is True
        assert "response_format" not in body and "temperature" not in body
        image = body["messages"][0]["content"][1]
        assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"
        entry = outbound.entries()[-1]
        assert entry.outcome == outbound.SENT and entry.protection == outbound.CLEAR
        assert "which can read it" in entry.how

    def test_a_refusal_is_said_and_listed(self, wire):
        wire["answer"] = lambda request: Answer(
            {"error": {"type": "authentication_error", "message": "invalid x-api-key"}},
            status=401)

        def refuse(request):
            raise ai.urllib.error.HTTPError(request.full_url, 401, "no", http.client.
                                            HTTPMessage(), io.BytesIO(
                                                b'{"error": {"message": "invalid x-api-key"}}'))

        wire["answer"] = refuse
        settings = form(**{ai.SETTING_BASE_URL: ANTHROPIC, ai.SETTING_MODEL: "claude-sonnet-5"})
        with pytest.raises(ai.AIError, match="HTTP 401: invalid x-api-key"):
            ai.chat([{"role": "user", "content": "hi"}], settings)
        assert outbound.entries()[-1].outcome == outbound.FAILED

    def test_ping_gives_a_thinking_model_room(self, wire):
        wire["answer"] = lambda request: Answer(ANTHROPIC_STREAM)
        settings = form(**{ai.SETTING_BASE_URL: ANTHROPIC, ai.SETTING_MODEL: "claude-sonnet-5"})
        ai.ping(settings)
        assert json.loads(wire["calls"][0].data)["max_tokens"] == 1024


class TestChutesOrdinaryModels:
    def test_go_plain_through_its_openai_api_and_say_so(self, wire):
        wire["answer"] = lambda request: Answer(
            {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})
        settings = form(**{ai.SETTING_BASE_URL: CHUTES, ai.SETTING_MODEL: "zai-org/GLM-5.3"})
        assert ai.session_of(settings) is None
        assert ai.privacy_of(settings).words == "Not private"
        assert "Chutes' -TEE models are the ones sent end-to-end" in \
            ai.destination(settings)[0]
        ai.chat([{"role": "user", "content": "hi"}], settings, json_reply=False)
        (request,) = wire["calls"]
        assert request.full_url == CHUTES + "/chat/completions"
        assert request.get_header("Authorization") == "Bearer k-1"


# ---- sealed: Tinfoil's every model, Chutes' -TEE ones -------------------------------

class FakeSealed:
    """A sealed client's shape (sealed.PrivateModeClient's: attest, accept, request,
    lines, finish), its stream the reply's lines, everything it was given kept."""

    made = []

    def __init__(self, api_url, api_key, key_saved_for="", request_timeout=180, **kwargs):
        self.api_url, self.api_key, self.key_saved_for = api_url, api_key, key_saved_for
        self.kwargs = kwargs
        self.sent = []
        self.accepted = None
        self.reattested = None
        FakeSealed.made.append(self)

    def check_offered(self, model):
        pass

    def attest(self, model):
        attestation = sealed.attestation_for(model, sealed.Verified(
            hpke_public_key="ab" * 32, enclave="inference.tinfoil.sh", hardware="AMD SEV-SNP",
            measurement="m", release_digest="d" * 64, release_tag="v0.0.176",
            latest_release="", latest_checked=True, tls_public_key="e5" * 32),
            service=self.SERVICE)
        if self.SERVICE == levels.CHUTES:
            attestation.shortfalls = [private_ai.WORKLOAD_UNMEASURED,
                                      private_ai.MEASUREMENTS_LISTED]
            attestation.level = "partial"
        return attestation

    def accept(self, attestation):
        self.accepted = attestation

    @contextmanager
    def request(self, model, payload):
        self.sent.append((model, payload))
        yield [b'data: {"id": "c1", "choices": [{"delta": {"content": "{\\"word\\": '
               b'\\"OK\\"}"}}]}',
               b'data: {"id": "c1", "choices": [{"delta": {}, "finish_reason": "stop"}]}',
               b"data: [DONE]"]

    @staticmethod
    def lines(stream):
        return iter(stream)

    @staticmethod
    def finish(finish_reason, saw_done):
        assert finish_reason and saw_done


class FakeTinfoil(FakeSealed):
    SERVICE = levels.TINFOIL


class FakeChutes(FakeSealed):
    SERVICE = levels.CHUTES


@pytest.fixture
def sealed_clients(monkeypatch):
    from mediabrowser.core import chutes, tinfoil_direct
    FakeSealed.made = []
    monkeypatch.setattr(tinfoil_direct, "TinfoilDirectClient", FakeTinfoil)
    monkeypatch.setattr(chutes, "ChutesClient", FakeChutes)
    monkeypatch.setattr(sealed, "known", lambda: (True, ""))
    monkeypatch.setattr(sealed, "available", lambda: (True, ""))
    # The public lists, as they say which can see.
    monkeypatch.setitem(ai._listings, (ai.TINFOIL, TINFOIL), (
        ai.time.monotonic(), {"gemma4-31b": {"capabilities": {"vision": True}},
                              "glm-5-3": {"capabilities": {"vision": False}}}))
    monkeypatch.setitem(ai._listings, (ai.CHUTES, CHUTES), (
        ai.time.monotonic(), {TEE_MODEL: {"architecture": {"input_modalities": ["text",
                                                                               "image"]}},
                              "zai-org/GLM-5.2-TEE": {"architecture": {
                                  "input_modalities": ["text"]}}}))
    return FakeSealed.made


class TestSealed:
    def test_every_tinfoil_model_goes_sealed_attested_first(self, wire, sealed_clients):
        settings = form(**{ai.SETTING_BASE_URL: TINFOIL, ai.SETTING_MODEL: "gemma4-31b"})
        assert ai.chat_json(frame_message(), settings) == {"word": "OK"}
        (client,) = sealed_clients
        assert client.api_url == TINFOIL and client.api_key == "k-1"
        assert client.key_saved_for == "https://inference.tinfoil.sh:443"
        assert client.accepted is not None and client.accepted.service == levels.TINFOIL
        model, payload = client.sent[0]
        assert model == "gemma4-31b" and payload["reasoning"] == {"effort": "low"}
        assert wire["calls"] == [], "nothing went any other way"
        sent = [e for e in outbound.entries() if e.what.startswith("1 frame")][-1]
        assert sent.protection == outbound.SEALED and sent.service == "Tinfoil"
        assert "Tinfoil's attested router" in sent.how and "highest privacy" in sent.how
        check = next(e for e in outbound.entries() if e.protection == outbound.CHECK)
        assert check.service == "Tinfoil" and "atc.tinfoil.sh" in check.how

    def test_a_chutes_tee_model_goes_sealed_with_partial_privacy(self, wire, sealed_clients):
        settings = form(**{ai.SETTING_BASE_URL: CHUTES, ai.SETTING_MODEL: TEE_MODEL})
        assert ai.privacy_of(settings).level == levels.PARTIAL
        assert ai.chat_json(frame_message(), settings) == {"word": "OK"}
        (client,) = sealed_clients
        assert client.key_saved_for == "https://llm.chutes.ai:443"
        assert client.kwargs["state"] is ai._chutes_state(), "attestations shared a while"
        assert wire["calls"] == []
        sent = [e for e in outbound.entries() if e.what.startswith("1 frame")][-1]
        assert sent.protection == outbound.SEALED and "partial privacy" in sent.how

    def test_frames_go_only_to_a_model_that_can_see_or_are_refused_in_words(
            self, wire, sealed_clients):
        for address, model in ((TINFOIL, "glm-5-3"), (CHUTES, "zai-org/GLM-5.2-TEE")):
            settings = form(**{ai.SETTING_BASE_URL: address, ai.SETTING_MODEL: model})
            with pytest.raises(ai.Refused, match="can't look at pictures") as caught:
                ai.chat(frame_message(), settings)
            assert "Nothing was sent" in str(caught.value)
            # Text alone goes.
            ai.chat([{"role": "user", "content": "hi"}], settings, json_reply=False)
        assert all(len(client.sent) == 1 for client in sealed_clients)
        assert wire["calls"] == []
        assert ai.model_vision(form(**{ai.SETTING_BASE_URL: ANTHROPIC}), "claude-opus-5-5")

    def test_privacy_mode_takes_tinfoil_or_a_chutes_tee_model(self, wire, sealed_clients):
        for address, model, service in ((TINFOIL, "gemma4-31b", levels.TINFOIL),
                                        (CHUTES, TEE_MODEL, levels.CHUTES)):
            settings = form(**{ai.SETTING_BASE_URL: address, ai.SETTING_PRIVACY_MODE: True,
                               ai.SETTING_PRIVATE_MODEL: model})
            assert ai.not_ready(settings) == ""
            assert ai.session_of(settings).service == service
            assert ai.protection_label(settings) == \
                f"Privacy Mode: end-to-end encrypted ({ai.PROVIDERS[service]})"
            ai.chat_json(frame_message(), settings)
        assert wire["calls"] == []

    def test_check_attestation_needs_no_key_for_tinfoil_or_chutes(self, monkeypatch,
                                                                  sealed_clients):
        """Without a key, Tinfoil's router is attested from its own bundle service and
        Chutes' instances from its public evidence: nothing is sealed to either."""
        from mediabrowser.core import chutes, tinfoil_direct

        seen = []

        def verify(host, bundle, secret, **kwargs):
            seen.append((host, bundle))
            return sealed.Verified(
                hpke_public_key="ab" * 32, enclave=host, hardware="AMD SEV-SNP",
                measurement="m", release_digest="d" * 64, release_tag="v0.0.176",
                latest_release="", latest_checked=True, tls_public_key="e5" * 32)

        monkeypatch.setattr(tinfoil_direct, "direct_verify", verify)
        monkeypatch.setattr(tinfoil_direct.Listing, "known", lambda self: None)
        settings = form(**{ai.SETTING_KEY: "", ai.SETTING_BASE_URL: TINFOIL,
                           ai.SETTING_PRIVACY_MODE: True,
                           ai.SETTING_PRIVATE_MODEL: "kimi-k3-1-pro"})
        session = ai.check_attestation(settings)
        assert seen == [("inference.tinfoil.sh", tinfoil_direct.ATC_URL)]
        assert session.attestation.service == levels.TINFOIL
        assert "Tinfoil's router enclave at inference.tinfoil.sh" in \
            private_ai.what_was_attested(session.attestation)
        assert sealed_clients == [], "the keyed client is never made without a key"

        public = []
        monkeypatch.setattr(chutes.ChutesClient, "attest_public",
                            lambda self, model: public.append((model, self.api_key))
                            or FakeChutes(CHUTES, "").attest(model), raising=False)
        monkeypatch.setattr(chutes.ChutesClient, "check_offered", lambda self, m: None,
                            raising=False)
        settings = form(**{ai.SETTING_KEY: "", ai.SETTING_BASE_URL: CHUTES,
                           ai.SETTING_PRIVACY_MODE: True, ai.SETTING_PRIVATE_MODEL: TEE_MODEL})
        session = ai.check_attestation(settings)
        assert public == [(TEE_MODEL, "")]


class TestPrivacyModesChoices:
    @pytest.mark.parametrize("address, model, said", [
        (CHUTES, "zai-org/GLM-5.3", "not one of Chutes' -TEE models"),
        (ANTHROPIC, "claude-sonnet-5", "anthropic has no attested or end-to-end"),
        ("https://api.openai.com/v1", "private/kimi-k3-1-pro", "not nano-gpt's"),
        (TINFOIL, "gemma4-31b:online", "web search"),
    ])
    def test_what_cannot_be_private_is_refused_in_words(self, wire, address, model, said):
        settings = form(**{ai.SETTING_BASE_URL: address, ai.SETTING_PRIVACY_MODE: True,
                           ai.SETTING_PRIVATE_MODEL: model})
        assert said in ai.not_ready(settings)
        with pytest.raises(ai.NotConfigured):
            ai.chat([{"role": "user", "content": "hi"}], settings)
        assert wire["calls"] == []

    def test_a_chutes_model_only_where_partial_privacy_is_allowed(self, wire):
        """Chutes' end-to-end encryption is always partial (its key-holding program is
        Chutes' signed one, not measured): never under Strict."""
        strict = form(**{ai.SETTING_BASE_URL: CHUTES, ai.SETTING_PRIVACY_MODE: True,
                         ai.SETTING_PRIVATE_MODEL: TEE_MODEL, ai.SETTING_STRICT: True})
        assert "always partial privacy" in ai.not_ready(strict)
        assert "Strict" in ai.not_ready(strict)
        loose = form(**{ai.SETTING_BASE_URL: CHUTES, ai.SETTING_PRIVACY_MODE: True,
                        ai.SETTING_PRIVATE_MODEL: TEE_MODEL})
        assert ai.not_ready(loose) == ""

    def test_without_ml_kem_chutes_is_refused_in_words(self, monkeypatch):
        from mediabrowser.core import chutes_e2ee
        monkeypatch.setattr(chutes_e2ee, "mlkem_refusal", lambda: chutes_e2ee.MLKEM_MISSING)
        settings = form(**{ai.SETTING_BASE_URL: CHUTES, ai.SETTING_MODEL: TEE_MODEL})
        assert ai.not_ready(settings) == chutes_e2ee.MLKEM_MISSING


# ---- the levels, and the rule an operation keeps -------------------------------------

class TestTheMoveRule:
    """What an operation has worked on - frames, titles, menu text - goes on only to a
    model at its level or higher, whichever feature sends it (ai.carry_on)."""

    def private(self):
        return form(**{ai.SETTING_PRIVACY_MODE: True,
                       ai.SETTING_PRIVATE_MODEL: "private/kimi-k3-1-pro"})

    @pytest.mark.parametrize("after, then", [
        ({ai.SETTING_BASE_URL: ANTHROPIC, ai.SETTING_MODEL: "claude-sonnet-5"},
         "no privacy"),
        ({ai.SETTING_MODEL: ai.DEFAULT_MODEL}, "no privacy"),
        ({ai.SETTING_BASE_URL: CHUTES, ai.SETTING_MODEL: TEE_MODEL}, "partial privacy"),
        ({ai.SETTING_PRIVACY_MODE: True, ai.SETTING_PRIVATE_MODEL: "TEE/glm-5.3-flash"},
         "partial privacy"),
    ])
    def test_the_highest_moves_to_nothing_less_whichever_feature_sends(self, wire, after,
                                                                       then):
        moved = ai.carry_on(self.private(), form(**after), "Detect Chapters")
        said = ai.not_ready(moved)
        assert said.startswith("Nothing was sent: Detect Chapters began with the highest "
                               "privacy")
        assert f"has {then}" in said and "Close the window and open it again" in said
        assert "NanoGPT's private/ models or Tinfoil's" in said
        # Every feature's requests go through chat: chapters, menus, titles, MusicBrainz
        # help, identifying - each is refused before anything is sent.
        from mediabrowser.core import ai_chapters, ai_musicbrainz  # noqa: F401
        with pytest.raises(ai.NotConfigured, match="began with the highest privacy"):
            ai.chat_json(frame_message(), moved)
        with pytest.raises(ai.NotConfigured):
            ai_chapters.translate_titles(["Glass Harbor"], moved)
        assert wire["calls"] == []

    def test_it_moves_to_as_private_or_more(self, sealed_clients):
        for before, after in (
                (self.private(), form(**{ai.SETTING_BASE_URL: TINFOIL,
                                         ai.SETTING_MODEL: "gemma4-31b"})),
                (form(**{ai.SETTING_PRIVACY_MODE: True,
                         ai.SETTING_PRIVATE_MODEL: "TEE/glm-5.3-flash"}),
                 form(**{ai.SETTING_BASE_URL: CHUTES, ai.SETTING_MODEL: TEE_MODEL})),
                (form(), form(**{ai.SETTING_BASE_URL: ANTHROPIC}))):
            assert ai.move_refusal(ai.carry_on(before, after, "Detect Chapters")) == ""

    def test_a_kept_level_stays_through_another_change(self):
        once = ai.carry_on(self.private(), form(**{ai.SETTING_BASE_URL: TINFOIL,
                                                   ai.SETTING_MODEL: "gemma4-31b"}), "X")
        twice = ai.carry_on(once, form(), "X")
        assert ai.kept_rank(twice) == 2 and ai.move_refusal(twice)


class TestTheLevelsWhereModelsAreChosen:
    @pytest.mark.parametrize("address, model, private, label", [
        (TINFOIL, "gemma4-31b", False, "🔐 End-to-end encrypted (Tinfoil) · Highest privacy"),
        (CHUTES, TEE_MODEL, False, "🔐 End-to-end encrypted (Chutes, partial) · Partial "
                                   "privacy"),
        (CHUTES, "zai-org/GLM-5.3", False, "Not private"),
        (ANTHROPIC, "claude-sonnet-5", False, "Not private"),
        (ai.DEFAULT_BASE_URL, "TEE/glm-5.3-flash", False, "Not private"),
        (ai.DEFAULT_BASE_URL, "TEE/glm-5.3-flash", True, "TEE (partial) · Partial privacy"),
        (ai.DEFAULT_BASE_URL, "private/kimi-k3-1-pro", True,
         "🔐 End-to-end encrypted · Highest privacy"),
        (ai.DEFAULT_BASE_URL, "private/kimi-k3-1-pro", False, "Only in Privacy Mode"),
    ])
    def test_each_says_its_level(self, address, model, private, label):
        assert ai.model_privacy({ai.SETTING_BASE_URL: address}, model, private)[0] == label

    def test_the_lists_say_it_too(self, monkeypatch):
        from mediabrowser.core import tinfoil_direct
        monkeypatch.setattr(tinfoil_direct.Listing, "fetch", lambda self: {
            "gemma4-31b": {"id": "gemma4-31b", "name": "Gemma 4 31B",
                           "capabilities": {"vision": True},
                           "pricing": {"prompt": 0.4, "completion": 1.0}},
            "glm-5-3": {"id": "glm-5-3", "capabilities": {"vision": False}}})
        settings = form(**{ai.SETTING_BASE_URL: TINFOIL})
        (model,) = ai.list_models(settings)
        assert model["id"] == "gemma4-31b" and model["price"] == \
            "$0.4 in / $1 out per million tokens"
        assert model["note"].endswith("Highest privacy")
        (private,) = ai.list_private_models(settings)
        assert private["kind"] == private_ai.E2EE


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
    return tmp_path


def test_settings_json_never_holds_a_key_or_a_protocol(data_dir):
    """Nothing about a provider is stored but its address: the protocol, the sealing and
    where its key may go are read from it each time."""
    store.save_app_settings({ai.SETTING_BASE_URL: TINFOIL})
    kept = store.load_app_settings()
    assert not [name for name in kept if "key" in name or "protocol" in name], kept
    assert netguard.problem(TINFOIL) is None
