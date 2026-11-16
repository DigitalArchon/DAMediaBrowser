# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Chutes' end-to-end encryption, the pure parts (assistant/chutes_e2ee.py): ML-KEM-768
against NIST's vectors, the request and reply framing as round trips with a key of our
own standing in for an instance, every hostile relay the design names, the key binding,
the measurements and the model list. Ported from SealedLore's tests/test_chutes_e2ee.py,
with its captures (tests/data/chutes_*.json, mlkem768_acvp.json)."""

import base64
import gzip
import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from mediabrowser.core import chutes_e2ee  # noqa: E402
from mediabrowser.core.chutes_e2ee import (  # noqa: E402
    CIPHERTEXT_BYTES,
    INFO_REQUEST,
    INFO_RESPONSE,
    INFO_STREAM,
    MLKEM_MISSING,
    NONCE_BYTES,
    RESPONSE_KEY_FIELD,
    SealBroken,
    SealError,
    StreamOpener,
    binding,
    confidential_models,
    derive_key,
    gunzip,
    key_bound,
    measured,
    open_reply,
    published_measurements,
    seal_request,
    stream_nonce,
)
from mediabrowser.core.private_ai import PrivacyError  # noqa: E402

try:
    from cryptography.hazmat.primitives.asymmetric import mlkem
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    HAVE_MLKEM = chutes_e2ee.mlkem_available()
except ImportError:
    HAVE_MLKEM = False

DATA = Path(__file__).parent / "data"
ACVP = json.loads((DATA / "mlkem768_acvp.json").read_text(encoding="utf-8"))
EVIDENCE = json.loads((DATA / "chutes_evidence.json").read_text(encoding="utf-8"))


def b64(data):
    return base64.b64encode(data).decode()


class Instance:
    """Our own ML-KEM key standing in for an instance's, doing what its workload does
    with a sealed request and its reply (chutes/entrypoint/run.py)."""

    def __init__(self):
        self.key = mlkem.MLKEM768PrivateKey.generate()
        self.pubkey = b64(self.key.public_key().public_bytes_raw())

    def open_request(self, blob):
        ciphertext, nonce, sealed = blob[:1088], blob[1088:1100], blob[1100:]
        key = derive_key(self.key.decapsulate(ciphertext), ciphertext, INFO_REQUEST)
        return json.loads(gzip.decompress(ChaCha20Poly1305(key).decrypt(nonce, sealed,
                                                                        None)))

    @staticmethod
    def seal_reply(body, response_pk, raw=None):
        client = mlkem.MLKEM768PublicKey.from_public_bytes(base64.b64decode(response_pk))
        secret, ciphertext = client.encapsulate()
        key = derive_key(secret, ciphertext, INFO_RESPONSE)
        nonce = os.urandom(12)
        text = raw if raw is not None else gzip.compress(json.dumps(body).encode())
        return ciphertext + nonce + ChaCha20Poly1305(key).encrypt(nonce, text, None)


class Stream:
    """A stream sealed as Chutes' workload seals one: the key exchange, then each piece
    of the model server's own SSE bytes sealed alone, its nonce the piece's number."""

    def __init__(self, response_pk):
        client = mlkem.MLKEM768PublicKey.from_public_bytes(base64.b64decode(response_pk))
        secret, self.ciphertext = client.encapsulate()
        self.key = derive_key(secret, self.ciphertext, INFO_STREAM)
        self.sent = 0

    def init(self):
        return "data: " + json.dumps({"e2e_init": b64(self.ciphertext)})

    def piece(self, text, key=None, number=None):
        if number is None:
            number, self.sent = self.sent, self.sent + 1
        nonce = stream_nonce(number)
        sealed = ChaCha20Poly1305(key or self.key).encrypt(nonce, text.encode(), None)
        return "data: " + json.dumps({"e2e": b64(nonce + sealed)})


def chunk(content="", finish=None, usage=None):
    body = {"choices": [{"delta": {"content": content}, "finish_reason": finish}]}
    if usage:
        body["usage"] = usage
    return f"data: {json.dumps(body)}\n\n"


def streamed():
    sealed = seal_request(Instance().pubkey, {"model": "m", "messages": [], "stream": True})
    pk = b64(sealed.response_key.public_key().public_bytes_raw())
    return StreamOpener(sealed.response_key), Stream(pk)


def opened(opener, lines):
    text = ""
    for line in lines:
        for item in opener.feed(line):
            for choice in item.get("choices") or []:
                text += choice.get("delta", {}).get("content") or ""
    return text


def live_stream():
    """A streamed reply as it came from Chutes, with the response key it was sealed to."""
    capture = json.loads((DATA / "chutes_stream.json").read_text())
    key = mlkem.MLKEM768PrivateKey.from_seed_bytes(
        bytes.fromhex(capture["response_key_seed"]))
    return capture["lines"], StreamOpener(key)


REPLY = {"id": "chatcmpl-1",
         "choices": [{"message": {"role": "assistant", "content": "Hello."},
                      "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 12, "completion_tokens": 3}}


@unittest.skipUnless(HAVE_MLKEM, "this cryptography has no ML-KEM-768")
class MlKemIsNists(unittest.TestCase):
    def test_a_key_made_from_nists_seed_is_nists_key(self):
        for case in ACVP["keyGen"]:
            with self.subTest(tc=case["tcId"]):
                key = mlkem.MLKEM768PrivateKey.from_seed_bytes(
                    bytes.fromhex(case["d"] + case["z"]))
                self.assertEqual(key.public_key().public_bytes_raw(),
                                 bytes.fromhex(case["ek"]))

    def test_an_instance_key_nist_rejects_is_refused(self):
        for case in ACVP["encapsulationKeyCheck"]:
            with self.subTest(tc=case["tcId"]):
                encoded = b64(bytes.fromhex(case["ek"]))
                if case["testPassed"]:
                    self.assertIsNotNone(chutes_e2ee.public_key(encoded))
                else:
                    with self.assertRaisesRegex(SealError, "not a valid ML-KEM-768 key"):
                        chutes_e2ee.public_key(encoded)

    def test_decapsulation_rejects_a_ciphertext_implicitly_as_fips_203_says(self):
        for case in ACVP["keyGen"][:3]:
            with self.subTest(tc=case["tcId"]):
                key = mlkem.MLKEM768PrivateKey.from_seed_bytes(
                    bytes.fromhex(case["d"] + case["z"]))
                forged = os.urandom(CIPHERTEXT_BYTES)
                rejected = hashlib.shake_256(bytes.fromhex(case["z"]) + forged).digest(32)
                self.assertEqual(key.decapsulate(forged), rejected)

    def test_the_nist_file_names_where_it_came_from(self):
        self.assertIn("usnistgov/ACVP-Server", ACVP["source"])
        self.assertEqual(len(ACVP["commit"]), 40)


@unittest.skipUnless(HAVE_MLKEM, "this cryptography has no ML-KEM-768")
class RequestsAndRepliesSealed(unittest.TestCase):
    def test_a_request_and_its_reply_go_round_sealed_as_chutes_lays_them_out(self):
        instance = Instance()
        body = {"model": "Qwen/Qwen3.8-27B-TEE",
                "messages": [{"role": "user", "content": "Hi"}]}
        sealed = seal_request(instance.pubkey, body)
        self.assertGreater(len(sealed.blob), CIPHERTEXT_BYTES + NONCE_BYTES + 16)
        got = instance.open_request(sealed.blob)
        response_pk = got.pop(RESPONSE_KEY_FIELD)
        self.assertEqual(got, body)
        self.assertEqual(len(base64.b64decode(response_pk)), 1184)
        self.assertEqual(open_reply(Instance.seal_reply(REPLY, response_pk),
                                    sealed.response_key), REPLY)
        again = seal_request(instance.pubkey, body)
        self.assertNotEqual(again.blob[:1100], sealed.blob[:1100])
        self.assertNotEqual(instance.open_request(again.blob)[RESPONSE_KEY_FIELD],
                            response_pk)

    def test_a_reply_changed_anywhere_is_refused(self):
        instance = Instance()
        sealed = seal_request(instance.pubkey, {"model": "m", "messages": []})
        pk = instance.open_request(sealed.blob)[RESPONSE_KEY_FIELD]
        reply = Instance.seal_reply(REPLY, pk)
        for where in (0, 1087, 1088, 1099, 1100, -17, -1):
            with self.subTest(where=where):
                blob = bytearray(reply)
                blob[where] ^= 0x01
                with self.assertRaises(SealBroken):
                    open_reply(bytes(blob), sealed.response_key)

    def test_a_reply_sealed_to_another_key_or_cut_short_is_refused(self):
        instance = Instance()
        sealed = seal_request(instance.pubkey, {"model": "m", "messages": []})
        other = seal_request(instance.pubkey, {"model": "m", "messages": []})
        pk = instance.open_request(other.blob)[RESPONSE_KEY_FIELD]
        with self.assertRaises(SealBroken):
            open_reply(Instance.seal_reply(REPLY, pk), sealed.response_key)
        mine = Instance.seal_reply(REPLY,
                                   instance.open_request(sealed.blob)[RESPONSE_KEY_FIELD])
        for cut in (0, 1088, 1115, len(mine) - 1):
            with self.assertRaises(SealBroken):
                open_reply(mine[:cut], sealed.response_key)

    def test_an_instance_key_that_is_not_one_is_refused_before_anything_is_sealed(self):
        good = Instance().pubkey
        raw = base64.b64decode(good)
        for wrong, why in ((b64(raw[:-1]), "1183 bytes"), (b64(raw + b"\0"), "1185 bytes"),
                           (good[:-4] + "!!!=", "not base64"), (" " + good, "not base64"),
                           (None, "not base64"), ("é" * 8, "not base64")):
            with self.subTest(why=why), self.assertRaisesRegex(SealError, why):
                seal_request(wrong, {"model": "m"})
        with self.assertRaisesRegex(SealError, "not a JSON object"):
            seal_request(good, {})
        with self.assertRaisesRegex(SealError, "cannot be written as JSON"):
            seal_request(good, {"model": float("nan")})

    def test_a_reply_that_opens_to_too_much_is_refused_before_it_is_all_inflated(self):
        instance = Instance()
        sealed = seal_request(instance.pubkey, {"model": "m", "messages": []})
        pk = instance.open_request(sealed.blob)[RESPONSE_KEY_FIELD]
        bomb = gzip.compress(b" " * (chutes_e2ee.MAX_DECODED_BYTES + 1), mtime=0)
        self.assertLess(len(bomb), 64 * 1024)
        with self.assertRaisesRegex(SealError, "more than 32 MB"):
            open_reply(Instance.seal_reply({}, pk, raw=bomb), sealed.response_key)
        self.assertEqual(gunzip(gzip.compress(b"x" * 100), most=100), b"x" * 100)
        with self.assertRaisesRegex(SealError, "more than"):
            gunzip(gzip.compress(b"x" * 101), most=100)

    def test_what_a_sealed_reply_holds_must_be_one_whole_json_object(self):
        instance = Instance()
        sealed = seal_request(instance.pubkey, {"model": "m", "messages": []})
        pk = instance.open_request(sealed.blob)[RESPONSE_KEY_FIELD]
        for raw, why in ((gzip.compress(b'{"a": 1}')[:-3], "was not whole"),
                         (gzip.compress(b'{"a": 1}') + b"more", "was not whole"),
                         (gzip.compress(b'{"a": 1}') + gzip.compress(b"{}"), "was not whole"),
                         (b"not gzip at all", "could not be read"),
                         (gzip.compress(b"[1, 2]"), "not a JSON object"),
                         (gzip.compress(b"hello"), "is not JSON"),
                         (gzip.compress(b"[" * 300 + b"]" * 300), "nested too deeply")):
            with self.subTest(why=why):
                with self.assertRaisesRegex(SealError, why) as caught:
                    open_reply(Instance.seal_reply({}, pk, raw=raw), sealed.response_key)
                self.assertNotIsInstance(caught.exception, SealBroken)

    def test_without_ml_kem_chutes_is_refused_in_words(self):
        from cryptography.exceptions import UnsupportedAlgorithm

        def unsupported(*_args):
            raise UnsupportedAlgorithm("ML-KEM-768 is not supported by this backend")

        pubkey = Instance().pubkey
        try:
            with mock.patch.object(chutes_e2ee, "_available", None), \
                    mock.patch.object(mlkem.MLKEM768PrivateKey, "generate",
                                      staticmethod(unsupported)):
                self.assertIs(chutes_e2ee.mlkem_available(), False)
                self.assertEqual(chutes_e2ee.mlkem_refusal(), MLKEM_MISSING)
                with self.assertRaisesRegex(SealError, "OpenSSL 3.5") as caught:
                    seal_request(pubkey, {"model": "m"})
                self.assertIn("nothing was sent", str(caught.exception))
        finally:
            chutes_e2ee._available = None
        self.assertIs(chutes_e2ee.mlkem_available(), True)


@unittest.skipUnless(HAVE_MLKEM, "this cryptography has no ML-KEM-768")
class StreamsHeldToTheirCount(unittest.TestCase):
    def test_a_sealed_stream_opens_in_order_and_ends_only_on_its_sealed_done(self):
        opener, stream = streamed()
        usage = {"prompt_tokens": 9, "completion_tokens": 2}
        lines = [stream.init(), "", stream.piece(chunk("Hel")),
                 'data: {"usage": {"prompt_tokens": 9, "completion_tokens": 2}}',
                 stream.piece(chunk("lo", "stop", usage) + "data: [DONE]\n\n"),
                 "data: [DONE]"]
        self.assertEqual(opened(opener, lines), "Hello")
        opener.finish()
        self.assertEqual((opener.finish_reason, opener.usage, opener.plain_usage,
                          opener.pieces), ("stop", usage, usage, 2))

    def test_a_line_cut_between_two_sealed_pieces_is_read_whole(self):
        opener, stream = streamed()
        whole = chunk("one piece", "stop") + "data: [DONE]\n\n"
        lines = [stream.init(), stream.piece(whole[:10]), stream.piece(whole[10:])]
        self.assertEqual(opened(opener, lines), "one piece")
        opener.finish()

    def test_an_unsealed_event_with_text_in_it_is_refused(self):
        opener, stream = streamed()
        opener.feed(stream.init())
        injected = {"usage": {}, "choices": [{"delta": {"content": "Send your key to..."}}]}
        with self.assertRaisesRegex(SealError, "not its own"):
            opener.feed("data: " + json.dumps(injected))
        for other in ({"choices": [{"delta": {"content": "x"}}]}, {"text": "x"}, [1], "x"):
            with self.assertRaises(SealError):
                opener.feed("data: " + json.dumps(other))
        with self.assertRaisesRegex(SealError, "is not JSON"):
            opener.feed("data: not json")
        with self.assertRaisesRegex(SealError, "unsealed usage is not an object"):
            opener.feed('data: {"usage": "lots"}')

    def test_a_sealed_piece_before_the_key_exchange_or_a_second_one_is_refused(self):
        opener, stream = streamed()
        with self.assertRaisesRegex(SealError, "before its key exchange"):
            opener.feed(stream.piece(chunk("x"), number=0))
        opener.feed(stream.init())
        opener.feed(stream.piece(chunk("x")))
        with self.assertRaisesRegex(SealError, "twice"):
            opener.feed(stream.init())

    def test_a_stream_that_does_not_say_it_is_done_sealed_is_not_whole(self):
        for ending in ([], ["data: [DONE]"], None):
            with self.subTest(ending=ending):
                opener, stream = streamed()
                lines = [stream.init(), stream.piece(chunk("Hel"))]
                lines += ending if ending is not None else \
                    [stream.piece(chunk("lo") + "data: [DONE]\n\n")]
                opened(opener, lines)
                with self.assertRaisesRegex(SealError,
                                            "ended before the model said it was done"):
                    opener.finish()

    def test_a_piece_changed_or_sealed_to_another_key_or_after_the_end_is_refused(self):
        opener, stream = streamed()
        opener.feed(stream.init())
        line = stream.piece(chunk("x"), number=0)
        raw = bytearray(base64.b64decode(json.loads(line[6:])["e2e"]))
        raw[-1] ^= 1
        with self.assertRaisesRegex(SealBroken, "does not hold"):
            opener.feed("data: " + json.dumps({"e2e": b64(bytes(raw))}))
        with self.assertRaisesRegex(SealBroken, "does not hold"):
            opener.feed(stream.piece(chunk("x"), key=os.urandom(32), number=0))
        with self.assertRaisesRegex(SealBroken, "too short"):
            opener.feed("data: " + json.dumps({"e2e": b64(b"\0" * 27)}))
        opener.feed(stream.piece(chunk("x", "stop") + "data: [DONE]\n\n"))
        opener.feed(stream.piece("\n"))
        with self.assertRaisesRegex(SealError, "after the model's end"):
            opener.feed(stream.piece(chunk("more")))

    def test_a_live_stream_opens_piece_by_piece_in_its_counted_order(self):
        lines, opener = live_stream()
        text = opened(opener, lines)
        opener.finish()
        self.assertEqual(text.strip(), "one, two, three, four, five, six, seven, eight, "
                                       "nine, ten, eleven, twelve")
        self.assertEqual((opener.pieces, opener.finish_reason), (54, "stop"))

    def test_a_piece_out_of_its_place_is_refused(self):
        for change in ("swapped", "dropped", "repeated"):
            with self.subTest(change=change):
                lines, opener = live_stream()
                sealed = [i for i, line in enumerate(lines) if '"e2e"' in line]
                if change == "swapped":
                    a, b = sealed[2], sealed[4]
                    lines[a], lines[b] = lines[b], lines[a]
                elif change == "dropped":
                    del lines[sealed[4]]
                else:
                    lines.insert(sealed[5], lines[sealed[4]])
                with self.assertRaisesRegex(SealBroken, "not the one that comes next"):
                    opened(opener, lines)

    def test_chutes_relays_error_is_said_as_its_unauthenticated_words(self):
        opener, stream = streamed()
        opener.feed(stream.init())
        with self.assertRaises(PrivacyError) as caught:
            opener.feed('data: {"e2e_error": "Instance\\u202e gone\\nnow"}')
        self.assertEqual(str(caught.exception),
                         "Chutes' relay said (unauthenticated): Instance gone now")
        self.assertNotIsInstance(caught.exception, SealError)

    def test_the_model_servers_own_error_inside_the_seal_is_said(self):
        opener, stream = streamed()
        opener.feed(stream.init())
        with self.assertRaisesRegex(PrivacyError,
                                    "model's server reported an error: too long"):
            opener.feed(stream.piece('data: {"error": {"message": "too long"}}\n\n'))


def registers(item):
    body = base64.b64decode(item["quote"])[48:632]
    return body[136:184], tuple(body[o:o + 48] for o in (328, 376, 424, 472))


class BindingMeasurementsAndModels(unittest.TestCase):
    @unittest.skipUnless(HAVE_MLKEM, "this cryptography has no ML-KEM-768")
    def test_the_binding_is_sha256_of_the_nonce_and_the_key_as_chutes_writes_them(self):
        nonce, pubkey = "ab" * 32, Instance().pubkey
        expected = bytes.fromhex(hashlib.sha256((nonce + pubkey).encode()).hexdigest())
        self.assertEqual(binding(nonce, pubkey), expected)
        report = expected + os.urandom(32)
        self.assertTrue(key_bound(report, nonce, pubkey))
        self.assertFalse(key_bound(report, "cd" * 32, pubkey))
        self.assertFalse(key_bound(report, nonce, Instance().pubkey))
        self.assertFalse(key_bound(report, nonce.upper(), pubkey))
        self.assertFalse(key_bound(bytes(32) + expected, nonce, pubkey))
        self.assertFalse(key_bound(report[:63], nonce, pubkey))

    def test_each_real_instances_registers_are_one_of_chutes_published_sets(self):
        for shape in ("measurements", "measurements_runtime"):
            sets = published_measurements(EVIDENCE[shape])
            for item in EVIDENCE["evidence"]["evidence"]:
                with self.subTest(shape=shape, instance=item["instance_id"]):
                    mrtd, rtmrs = registers(item)
                    found = measured(mrtd, rtmrs, sets)
                    self.assertIn(found.version, ("1.4.1", "1.5.0"))
                    self.assertIsNone(measured(mrtd, (rtmrs[0], rtmrs[1], rtmrs[2],
                                                      bytes(48)), sets))
                    self.assertIsNone(measured(bytes(48), rtmrs, sets))

    def test_a_published_list_that_cannot_be_read_is_refused_and_bad_sets_left_out(self):
        good = EVIDENCE["measurements"][0]
        broken = [{**good, "rtmr2": "zz" * 48}, {**good, "mrtd": "AB"}, "x", {**good}]
        self.assertEqual(len(published_measurements(broken)), 1)
        with self.assertRaisesRegex(SealError, "are not a list"):
            published_measurements({"sets": []})
        with self.assertRaisesRegex(SealError, "hold no set"):
            published_measurements([{"mrtd": "AB"}])

    def test_only_confidential_models_are_offered_priced_per_million(self):
        listing = json.loads((DATA / "chutes_models.json").read_text(encoding="utf-8"))
        found = confidential_models(listing)
        self.assertEqual(len(found), 11)
        self.assertNotIn("zai-org/GLM-5.3", found)
        self.assertTrue(all(model_id.upper().endswith("-TEE") for model_id in found))
        qwen = found["Qwen/Qwen3.8-27B-TEE"]
        self.assertEqual(qwen["chute_id"], "9ecb73cc-909b-5334-9cd8-74985945cff5")
        self.assertEqual((qwen["pricing"]["prompt"], qwen["pricing"]["completion"]),
                         (0.039, 1.25))
        self.assertEqual((qwen["context_length"], qwen["max_output_tokens"]),
                         (262144, 65536))

    def test_a_model_is_offered_only_when_chutes_says_so_plainly(self):
        base = {"id": "a/b-TEE", "chute_id": "9ecb73cc-909b-5334-9cd8-74985945cff5"}
        for listed in ({**base, "confidential_compute": "true"},
                       {**base, "confidential_compute": 1},
                       {**base, "confidential_compute": True, "chute_id": "../e2e/invoke"},
                       {**base, "confidential_compute": True, "chute_id": None},
                       {**base, "confidential_compute": True, "id": ""}):
            self.assertEqual(confidential_models({"data": [listed]}), {})
        priced = confidential_models({"data": [{**base, "confidential_compute": True,
                                                "pricing": {"prompt": -1,
                                                            "completion": 2}}]})
        self.assertNotIn("pricing", priced["a/b-TEE"])
        self.assertEqual(confidential_models({"data": "nothing"}), {})
        self.assertEqual(confidential_models([]), {})


if __name__ == "__main__":
    unittest.main()
