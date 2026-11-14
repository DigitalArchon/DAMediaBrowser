# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Intel's and NVIDIA's checks (core/dcap.py, core/nras.py, DA Toolkit's copies, shared by
every DA app) read their JSON to MAX_NESTING (200) levels at most, a deeper answer refused as
one that cannot be read, and their own fetches wait no longer than the attestation's time
leaves (its deadline, on time.monotonic: TeeClient's ATTEMPT_SECONDS), refused in words once
none is left (the DA apps' security standard, its final pass, Nov 14 2026). As DA Toolkit's
tests/test_attestation_limits.py. Nothing reaches the network."""

import base64
import time
from datetime import datetime, timezone

import pytest

from mediabrowser.core import dcap, nras
from mediabrowser.core.private_ai import TeeClient

DEEP = "[" * 300 + "]" * 300


class Answer:
    def __init__(self, body, status=200):
        self.content, self.status_code, self.headers = body, status, {}


class Recording:
    """A session that answers every GET with `body` and keeps each request's timeout."""

    def __init__(self, body=b"{}", status=200):
        self.body, self.status, self.timeouts = body, status, []

    def get(self, url, params=None, timeout=None, **_kw):
        self.timeouts.append(timeout)
        return Answer(self.body, self.status)


def test_intels_json_is_read_to_200_levels_and_a_deeper_answer_is_refused():
    assert dcap.MAX_NESTING == 200
    assert dcap._nesting('{"a": ' + "[" * 199 + "]" * 199 + "}") == 200
    assert dcap._nesting('{"text": "[[[[ in a string ]"}') == 1
    with pytest.raises(dcap.DcapError, match="nested deeper than 200 levels"):
        dcap._loads('{"a": ' + DEEP + "}")
    content = ('{"tcbInfo": {"id": "TDX", "deep": ' + DEEP + '}, "signature": "00"}').encode()
    with pytest.raises(dcap.DcapError, match="nested deeper"):
        dcap._signed_json(content, "tcbInfo", "", dcap.intel_root(), datetime.now(timezone.utc))


def test_nvidias_deeper_token_or_key_set_is_refused(monkeypatch):
    assert nras.MAX_NESTING == 200
    deep = base64.urlsafe_b64encode(
        ('{"alg": "ES384", "x": ' + DEEP + "}").encode()).decode().rstrip("=")
    claims = base64.urlsafe_b64encode(b'{"iss": "x"}').decode().rstrip("=")
    signature = base64.urlsafe_b64encode(bytes(96)).decode()
    with pytest.raises(nras.NrasError, match="nested deeper than 200 levels"):
        nras.verify_token(f"{deep}.{claims}.{signature}", Recording(), "nonce", time.time())
    monkeypatch.setattr(nras, "_keys", {"at": 0.0, "keys": {}})
    with pytest.raises(nras.NrasError, match="nested deeper"):
        nras._jwks(Recording(('{"keys": ' + DEEP + "}").encode()))


def test_intels_fetches_wait_only_for_what_is_left_of_the_attestations_time(monkeypatch):
    monkeypatch.setattr(dcap, "_cache", {})
    session = Recording(b"collateral")
    dcap._fetch(session, "https://intel.example/a")
    dcap._fetch(session, "https://intel.example/b", deadline=time.monotonic() + 5)
    assert session.timeouts[0] == dcap.TIMEOUT_SECONDS and 0 < session.timeouts[1] <= 5
    with pytest.raises(dcap.DcapError, match="time ran out before Intel's collateral"):
        dcap._fetch(session, "https://intel.example/c", deadline=time.monotonic() - 1)
    assert len(session.timeouts) == 2, "nothing fetched once the time is up"


def test_nvidias_key_fetch_waits_only_for_what_is_left(monkeypatch):
    monkeypatch.setattr(nras, "_keys", {"at": 0.0, "keys": {}})
    session = Recording(b'{"keys": [{"kid": "k1"}]}')
    assert set(nras._jwks(session, deadline=time.monotonic() + 5)) == {"k1"}
    assert 0 < session.timeouts[0] <= 5
    monkeypatch.setattr(nras, "_keys", {"at": 0.0, "keys": {}})
    with pytest.raises(nras.NrasError, match="time ran out"):
        nras._jwks(session, deadline=time.monotonic() - 1)


def test_the_tee_client_gives_intels_and_nvidias_checks_its_deadline(monkeypatch):
    seen = {}
    monkeypatch.setattr(dcap, "verify",
                        lambda raw, session, **kw: seen.update(intel=kw.get("deadline")))
    monkeypatch.setattr(nras, "verify", lambda answer, nonce, session, **kw: seen.update(
        nvidia=kw.get("deadline")))
    client = TeeClient("https://nano-gpt.com/api/v1", "k", "TEE/x")
    client._deadline = 1234.5
    client._intel(b"quote", client.session)
    client._nvidia([], "nonce", client.session)
    assert seen == {"intel": 1234.5, "nvidia": 1234.5}
