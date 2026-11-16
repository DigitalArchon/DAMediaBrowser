# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The services an address belongs to (assistant/services.py) and the three privacy
levels with the rule a conversation keeps when its model changes (assistant/levels.py).
Every way an address can be written is recognised alike - when in doubt, sealed - and a
conversation moves only to a model at its level or higher. Ported from SealedLore's
tests/test_privacy.py, test_privacy_held.py and test_model_privacy.py."""

import unittest

from mediabrowser.core import levels, services  # noqa: E402
from mediabrowser.core.levels import (  # noqa: E402
    CHUTES,
    HIGHEST,
    NANOGPT_PRIVATE,
    NANOGPT_TEE,
    NOT_PRIVATE,
    PARTIAL,
    TINFOIL,
    Privacy,
    holds,
    move_refusal,
    plain_refusal,
    privacy_at,
)

NANOGPT = "https://nano-gpt.com/api/v1/chat/completions"
CHUTES_URL = "https://llm.chutes.ai/v1"
TINFOIL_URL = "https://inference.tinfoil.sh/v1"


class AddressesAreRecognisedHoweverTheyGetIn(unittest.TestCase):
    def test_tinfoils_hosts_are_sealed_whatever_the_protocol_says(self):
        for url in ("https://inference.tinfoil.sh/v1", "https://INFERENCE.TINFOIL.SH",
                    "https://inference.tinfoil.sh./v1/chat/completions",
                    "https://router-0.tinfoil.sh/v1", "https://user:pw@inference.tinfoil.sh",
                    "http://inference.tinfoil.sh/v1", "https://inference.tinfoil.sh:443/v1"):
            for protocol in ("auto", "openai", "anthropic"):
                with self.subTest(url=url, protocol=protocol):
                    self.assertEqual(services.sealed_service(url, protocol), "tinfoil")
                    self.assertEqual(services.chat_protocol(url, protocol), "tinfoil")
                    self.assertEqual(services.service_of(url), services.TINFOIL)
        for url in ("https://tinfoil.sh.evil.example/v1", "https://notinfoil.sh/v1",
                    "https://nano-gpt.com/api/v1"):
            with self.subTest(url=url):
                self.assertEqual(services.sealed_service(url), "")
        self.assertEqual(services.sealed_service("https://example.com", "tinfoil"), "tinfoil")

    def test_chutes_hosts_and_its_tee_models(self):
        for url in (CHUTES_URL, "https://claude.chutes.ai", "https://api.chutes.ai",
                    "https://LLM.Chutes.ai./v1", "https://x.chutes.dev/v1"):
            with self.subTest(url=url):
                self.assertTrue(services.on_chutes(url))
                self.assertEqual(services.service_of(url), services.CHUTES)
        for model in ("Qwen/Qwen3.8-27B-TEE", "qwen/qwen3.8-27b-tee", " a/b-TEE ",
                      "a/b-TEE:THINKING", "Chutes::a/b-TEE", "a/b-TEE: x"):
            with self.subTest(model=model):
                self.assertTrue(services.chutes_tee(model, CHUTES_URL))
                self.assertFalse(services.chutes_tee(model, NANOGPT))
        for model in ("zai-org/GLM-5.3", "a/TEE-b", "a/b-TEEN"):
            self.assertFalse(services.chutes_tee(model, CHUTES_URL))
        self.assertFalse(services.on_chutes("https://chutes.ai.evil.example"))

    def test_anthropics_own_and_chutes_proxy_speak_its_messages_api(self):
        self.assertEqual(services.chat_protocol("https://api.anthropic.com"), "anthropic")
        self.assertEqual(services.chat_protocol("https://claude.chutes.ai"), "anthropic")
        self.assertEqual(services.chat_protocol(CHUTES_URL), "openai")
        self.assertEqual(services.chat_protocol(NANOGPT), "openai")
        self.assertEqual(services.chat_protocol(NANOGPT, "anthropic"), "anthropic")
        self.assertEqual(services.service_of("https://api.anthropic.com/v1"),
                         services.ANTHROPIC)

    def test_a_key_is_bound_to_its_origins(self):
        self.assertEqual(services.origin("https://User:pw@LLM.chutes.ai./v1/x"),
                         "https://llm.chutes.ai:443")
        for url in (CHUTES_URL, "https://claude.chutes.ai", "https://api.chutes.ai/x"):
            for saved in sorted(services.CHUTES_KEY_ORIGINS):
                self.assertIsNone(services.chutes_key_refusal(url, saved))
        self.assertTrue(services.chutes_key_refusal("https://chutes.example/v1"))
        self.assertTrue(services.chutes_key_refusal("http://llm.chutes.ai/v1"))
        self.assertTrue(services.chutes_key_refusal("https://llm.chutes.ai:8443/v1"))
        self.assertTrue(services.chutes_key_refusal(CHUTES_URL, "https://nano-gpt.com:443"))
        self.assertIsNone(services.tinfoil_key_refusal(TINFOIL_URL))
        self.assertTrue(services.tinfoil_key_refusal("https://router-0.tinfoil.sh/v1"))
        self.assertTrue(services.tinfoil_key_refusal(TINFOIL_URL, "https://llm.chutes.ai:443"))

    def test_a_password_in_an_address_is_never_named(self):
        self.assertNotIn("hunter2", services.url_shown("https://bob:hunter2@x.example/v1"))
        self.assertNotIn("hunter2", services.chutes_key_refusal(
            "https://bob:hunter2@x.example/v1"))


class ThreeLevels(unittest.TestCase):
    def test_each_model_is_at_its_level(self):
        for model, url, expected, level in (
                ("private/glm-5-3", NANOGPT, Privacy("sealed", NANOGPT_PRIVATE), HIGHEST),
                (" Private/glm-5-3", NANOGPT, Privacy("sealed", NANOGPT_PRIVATE), HIGHEST),
                ("glm-5-3", TINFOIL_URL, Privacy("sealed", TINFOIL), HIGHEST),
                ("anything", "https://router-0.tinfoil.sh/v1", Privacy("sealed", TINFOIL),
                 HIGHEST),
                ("TEE/glm-5.3", NANOGPT, Privacy("tee", NANOGPT_TEE), PARTIAL),
                ("a/b-TEE", CHUTES_URL, Privacy("sealed", CHUTES), PARTIAL),
                ("a/b-TEE", "https://claude.chutes.ai", Privacy("sealed", CHUTES), PARTIAL),
                ("zai-org/GLM-5.3", CHUTES_URL, Privacy(plain_on=CHUTES), NOT_PRIVATE),
                ("gpt-x", NANOGPT, Privacy(), NOT_PRIVATE),
                ("private/x", "", Privacy("sealed", NANOGPT_PRIVATE), HIGHEST),
                ("a/b-TEE", "", Privacy(), NOT_PRIVATE)):
            with self.subTest(model=model, url=url):
                found = privacy_at(model, url)
                self.assertEqual(found, expected)
                self.assertEqual(found.level, level)

    def test_a_sealed_address_is_sealed_whatever_its_protocol_setting_says(self):
        for protocol in ("auto", "openai", "anthropic"):
            self.assertTrue(privacy_at("m", TINFOIL_URL, protocol).sealed)
        self.assertTrue(privacy_at("m", "https://example.com", "tinfoil").sealed)

    def test_every_level_says_what_it_is_and_chutes_limits_plainly(self):
        self.assertTrue(privacy_at("glm", TINFOIL_URL).label()[1].startswith(
            "Highest privacy"))
        label, note = privacy_at("a/b-TEE", CHUTES_URL).label()
        self.assertIn("partial", label)
        for words in ("Partial privacy", "chutes-aegis.so", "closed binary",
                      "Chutes' signature", "not measured", "Chutes' own"):
            self.assertIn(words, note)
        self.assertTrue(privacy_at("TEE/x", NANOGPT).label()[1].startswith("Partial"))
        self.assertTrue(privacy_at("x", CHUTES_URL).label()[1].startswith("Not private"))
        self.assertTrue(privacy_at("x", NANOGPT).label()[1].startswith("Not private"))
        self.assertIn("three levels", levels.PRIVACY_LEVELS)


class TheMoveRule(unittest.TestCase):
    def judge(self, model):
        url = {"tinfoil": TINFOIL_URL, "chutes": CHUTES_URL}.get(model.split("|")[0], NANOGPT)
        return privacy_at(model.split("|")[-1], url)

    def test_a_conversation_moves_only_to_its_level_or_higher(self):
        allowed = (("private/a", "tinfoil|glm"), ("tinfoil|glm", "private/a"),
                   ("TEE/a", "private/a"), ("TEE/a", "chutes|a/b-TEE"),
                   ("chutes|a/b-TEE", "TEE/a"), ("chutes|a/b-TEE", "tinfoil|glm"),
                   ("gpt", "TEE/a"), ("gpt", "chutes|plain"))
        for current, new in allowed:
            with self.subTest(current=current, new=new):
                self.assertIsNone(move_refusal(current, new, self.judge))
        refused = (("private/a", "chutes|a/b-TEE", "Chutes, but has partial privacy"),
                   ("private/a", "TEE/a", "a TEE model"),
                   ("tinfoil|glm", "gpt", "not a TEE or end-to-end encrypted"),
                   ("TEE/a", "chutes|plain", "no privacy"),
                   ("chutes|a/b-TEE", "gpt", "kept to partial privacy"))
        for current, new, why in refused:
            with self.subTest(current=current, new=new):
                said = move_refusal(current, new, self.judge)
                self.assertIn(why, said)
                self.assertIn("choose one of", said)

    def test_a_sealed_conversation_holds_to_its_own_sealing_service(self):
        nanogpt_private = Privacy("sealed", NANOGPT_PRIVATE)
        tinfoil = Privacy("sealed", TINFOIL)
        self.assertTrue(holds(nanogpt_private, nanogpt_private))
        self.assertFalse(holds(nanogpt_private, tinfoil), "a move records the new one first")
        self.assertFalse(holds(nanogpt_private, Privacy("tee", NANOGPT_TEE)))
        self.assertTrue(holds(Privacy("tee", NANOGPT_TEE), Privacy("sealed", CHUTES)))
        self.assertFalse(holds(Privacy("sealed", CHUTES), Privacy()))


class PlainClientsRefuse(unittest.TestCase):
    def test_a_plain_client_sends_nothing_that_must_go_sealed(self):
        self.assertIn("Tinfoil's end-to-end encrypted endpoint",
                      plain_refusal(TINFOIL_URL))
        self.assertIn("Tinfoil", plain_refusal("https://router-0.tinfoil.sh/v1/embeddings",
                                               "an embedding"))
        self.assertIn("one of Chutes' TEE models",
                      plain_refusal(CHUTES_URL, model="Chutes::a/b-TEE:THINKING"))
        self.assertIn("sealed", plain_refusal(NANOGPT, model="private/x"))
        self.assertIsNone(plain_refusal(CHUTES_URL, model="zai-org/GLM-5.3"))
        self.assertIsNone(plain_refusal(NANOGPT, model="TEE/x"))
        self.assertIsNone(plain_refusal("https://api.anthropic.com", model="claude-x"))


if __name__ == "__main__":
    unittest.main()
