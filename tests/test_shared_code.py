# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The code every DA app keeps alike, held to its canonical copy (the DA apps' security
standard, Nov 14 2026). Drift is a failing test here, not a finding in a review: a change
is made in the canonical copy first, then carried here.

- dcap.py, nras.py and ethsig.py are DA Toolkit's (src/datoolkit/llm/), line for line but
  their module docstring, their imports, and the HTTP call: netguard's Session where DA
  Toolkit has its httpx client (TOOLKIT_TRANSPORT, every word of it listed).
- sdk_guard.py is SealedLore's (providers/sdk_guard.py), but its docstring and imports.
- sealed.py and private_ai.py's TEE part are DA Assistant's (assistant/sealed.py and
  privacy.py), but their imports, the transport (netguard's Session for DA Assistant's
  requests session), and the app's own hooks: what this app has that DA Assistant has
  elsewhere or not at all - the relay's list of models, no proxy setting of its own, its
  file names and windows (SEALED_HOOKS, TEE_HOOKS, every difference listed).

Each is skipped where the canonical copy's repository is not beside this one.
"""

from __future__ import annotations

import ast
import difflib
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "mediabrowser" / "core"
DOCUMENTS = Path(__file__).resolve().parents[2]
TOOLKIT = DOCUMENTS / "DAToolkit" / "src" / "datoolkit" / "llm"
SEALEDLORE = DOCUMENTS / "SealedLore" / "src" / "sealedlore" / "providers"
ASSISTANT = DOCUMENTS / "DAAssistant" / "assistant"

# DA Toolkit's words for the HTTP call, and this app's.
TOOLKIT_TRANSPORT = (
    ("make_client(ROOT_CRL_URL, timeout=httpx.Timeout(TIMEOUT_SECONDS))",
     "netguard.Session(follow=True)"),
    ("make_client(JWKS_URL, timeout=httpx.Timeout(TIMEOUT_SECONDS))",
     "netguard.Session(follow=True)"),
    ("httpx.Client", "netguard.Session"),
    ("httpx.HTTPError", "netguard.RequestError"),
    ("ProviderError", "netguard.RequestError"),
)
# DA Assistant's words for its transport, and this app's.
ASSISTANT_TRANSPORT = (
    ("netguard.session(follow=True)", "netguard.Session(follow=True)"),
    ("requests.exceptions.RequestException", "netguard.RequestError"),
)
# What this app's sealed.py has in DA Assistant's place: the relay's list of models (DA
# Assistant's models.py), no proxy set in Settings (the environment's), the stream's lines
# split by netguard (DA Assistant's streams.py), and its own words for a run.
SEALED_HOOKS = (
    ('RELAY_PATH = "/api/v1/private/tinfoil"\n',
     'RELAY_PATH = "/api/v1/private/tinfoil"\n'
     "# Where the relay lists its models: public, no key is sent.\n"
     'RELAY_MODELS_PATH = RELAY_PATH + "/models"\n'),
    ('                "installed: run tools/install-private-mode.sh, then restart the app.")',
     '                "installed: the AppImage carries it, and from source "\n'
     '                "tools/install-private-mode.sh installs it; then restart the app.")'),
    ("    it, some 1,200 modules and most of a second. The main loop asks known() instead, and",
     "    it, some 1,200 modules and most of a second. The GUI thread asks known() instead, and"),
    ('    not in the middle of a conversation."""', '    not in the middle of a run."""'),
    ('    return f"{parsed.scheme}://{parsed.netloc}{RELAY_PATH}"\n',
     '    return f"{parsed.scheme}://{parsed.netloc}{RELAY_PATH}"\n'
     "\n\n"
     "def private_list_url(api_url):\n"
     '    """Where the Private Mode relay lists its models: public, no key is sent. Without\n'
     "    a user name and password written into the address, as relay_url has it.\"\"\"\n"
     "    parsed = urlparse(netguard.without_password(api_url))\n"
     '    return f"{parsed.scheme}://{parsed.netloc}{RELAY_MODELS_PATH}"\n'
     "\n\n"
     "def private_ids_in(body):\n"
     '    """The private/ model ids in the relay\'s list, as JSON."""\n'
     '    items = body.get("data") if isinstance(body, dict) else None\n'
     '    return [item["id"] for item in items or []\n'
     '            if isinstance(item, dict) and isinstance(item.get("id"), str)\n'
     '            and item["id"].startswith("private/")]\n'
     "\n\n"
     "def twin_key(model_id):\n"
     '    """What a TEE/ id and its private/ twin share: TEE/glm-5.3 <-> private/glm-5-3."""\n'
     '    return model_id.split("/", 1)[-1].lower().replace(".", "-")\n'
     "\n\n"
     "def offered(model_id, private_ids):\n"
     '    """Whether the relay\'s list still has `model_id`: nano-gpt retires Private Mode\n'
     "    models (private/glm-5-3-flash, Oct 2026). Its dotted alias is the same model, a\n"
     "    :thinking one is not. None when the list is empty - one that could not be read -\n"
     '    which says nothing either way. From DA Assistant\'s models.py."""\n'
     "    private_ids = list(private_ids)\n"
     "    if not private_ids:\n"
     "        return None\n"
     "    key = twin_key(model_id)\n"
     "    return any(twin_key(i) == key for i in private_ids)\n"),
    ("def _around_the_proxy():\n"
     '    """Raise PrivacyError if a proxy set in Settings cannot be used: httpx and TUF do\n'
     "    not go through netguard's session, which refuses the rest (proxy.refusal()).\"\"\"\n"
     "    refused = proxy.refusal()\n"
     "    if refused:\n"
     '        raise PrivacyError(f"the proxy set in Settings cannot be used, and nothing goes "\n'
     '                           f"around it: {refused}")\n'
     "\n\n", ""),
    ("    \"\"\"Attest Tinfoil's router through nano-gpt's relay (network; worker thread).\"\"\"\n"
     "    _around_the_proxy()\n",
     "    \"\"\"Attest Tinfoil's router through nano-gpt's relay (network; worker thread).\"\"\"\n"),
    ("    from tinfoil.github import fetch_latest_release\n    proxy.let_tuf_use_socks()\n",
     "    from tinfoil.github import fetch_latest_release\n"),
    ('    if "TUF" not in str(error) or not proxy.in_use():',
     '    if "TUF" not in str(error) or not netguard.env_proxy_for("https://sigstore.dev"):'),
    ('    """The attested enclave one conversation seals to. Thread-safe."""',
     '    """The attested enclave one operation seals to. Thread-safe."""'),
    ("        # Ours, not nano-gpt's: see the module docstring. One per conversation, never",
     "        # Ours, not nano-gpt's: see the module docstring. One per operation, never"),
    ("    own, so this one is told: without it the sealed request, alone of all the app's,\n"
     "    went direct - from this machine's own address, Tor or no Tor, beside a preflight\n"
     "    that had gone through the proxy.",
     "    own, so this one is told (netguard.env_proxy_for): without it the sealed request,\n"
     "    alone of all the app's, would go direct - from this machine's own address, Tor or no\n"
     "    Tor, beside a preflight that had gone through the proxy."),
    ("        another private/ model of the conversation - went out sealed to an enclave",
     "        another private/ model of the operation - went out sealed to an enclave"),
    ("        if proxy.refusal():\n            return []\n", ""),
    ("models.private_list_url(", "private_list_url("),
    ("models.private_ids_in(", "private_ids_in("),
    ("models.offered(", "offered("),
    ("        # The proxy is read at each request, as every other client of the app's reads\n"
     "        # it, so that one set in Settings meanwhile is the one this goes through too.\n"
     "        route = proxy.for_url(self.endpoint)",
     "        # The environment's proxy, read at each request as urllib reads it for every\n"
     "        # other request of the app's.\n"
     "        route = netguard.env_proxy_for(self.endpoint)"),
    ("            # Every path's backstop - a model retired mid-conversation, the connection",
     "            # Every path's backstop - a model retired part way through a run, the connection"),
    ('            raise PrivacyError(f"{model} is not a Private Mode model; nothing was sent")\n'
     "        _around_the_proxy()\n",
     '            raise PrivacyError(f"{model} is not a Private Mode model; nothing was sent")\n'),
    ('        """The decrypted lines of a sealed stream, as bytes (streams.read_lines: not\n'
     "        httpx's iter_lines, which holds a line without end, and splits at U+2028 and\n"
     "        U+2029 inside a reply's JSON too).\"\"\"",
     '        """The decrypted lines of a sealed stream, as bytes: split by netguard.lines_of,\n'
     "        in time linear in the stream's length and none longer than\n"
     "        netguard.MAX_LINE_BYTES (httpx's iter_lines holds a line however long it runs).\"\"\""),
    ("            yield from read_lines(response.iter_bytes())",
     "            yield from netguard.lines_of(response.iter_bytes())"),
    ("        except LineTooLong as e:", "        except netguard.LineTooLong as e:"),
    ('    cannot see (privacy.unsealed_words)."""', '    cannot see (private_ai.unsealed_words)."""'),
    ('    `payload["reasoning"]`, when llm.py sets it,', '    `payload["reasoning"]`, when ai.py sets it,'),
)
# What this app's private_ai.py says where DA Assistant's privacy.py names its windows.
TEE_HOOKS = (
    ("# _attest_gateway): it turns the banner amber, and Strict refuses it.",
     "# _attest_gateway): it is shown in amber, and Strict refuses it."),
    ('    summary: str = ""               # one line of `detail`, for the chat window\'s banner',
     '    summary: str = ""               # one line of `detail`, for the window'),
    ("        that turns the chat window's banner amber - or [] when it proved everything",
     "        that is shown in amber - or [] when it proved everything"),
    ("    said plainly: in the chat window's banner and its Details, and by Settings' Check\n"
     "    attestation. SealedLore's author",
     "    said plainly: by Settings' Check Attestation, in the details of a session\n"
     "    (PrivacySession.details) and on Help → What Was Sent, and after an answer in Detect\n"
     "    Chapters. SealedLore's author"),
    ("        # netguard's refusals among them: a redirect it would not follow, a proxy set in\n"
     "        # Settings that cannot be used.",
     "        # netguard's refusals among them: a redirect it would not follow, an\n"
     "        # environment's proxy that cannot be used."),
    ("            # netguard's refusals among them: a proxy set in Settings that cannot be used.",
     "            # netguard's refusals among them: an environment's proxy that cannot be used."),
    ("hashed by llm.py as they were read", "hashed by ai.py as they were read"),
)
# private_ai.py's TEE part: the constants, and what attests and checks (DA Assistant's
# privacy.py between the same markers). The rest - who may run, the session, the marks -
# is the app's own.
TEE_PARTS = (("NRAS_URL = ", "TEE_NOTE = ("),
             ("@dataclass\nclass Attestation:", "\n# ---- one "))


def comparable(text: str, replace=(), listed=()) -> list[str]:
    """`text`'s lines without its leading comments, its module docstring and its top-level
    imports, `replace`'s words put in (those it has) and `listed`'s (each of which it must
    have: a hook gone from the canonical copy is a list to be brought up to date), and a
    run of blank lines as one: what the copies must hold alike."""
    tree = ast.parse(text)
    dropped: set[int] = set()
    body = list(tree.body)
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        dropped.update(range(1, body.pop(0).end_lineno + 1))
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            dropped.update(range(node.lineno, node.end_lineno + 1))
    kept = "\n".join(line for number, line in enumerate(text.splitlines(), 1)
                     if number not in dropped)
    lines: list[str] = []
    for line in _replaced(kept, replace, listed).splitlines():
        if line.strip() or (lines and lines[-1].strip()):
            lines.append(line)
    return lines


def _replaced(text: str, replace=(), listed=()) -> str:
    for theirs, ours in replace:
        text = text.replace(theirs, ours)
    for theirs, ours in listed:
        if theirs not in text:
            raise AssertionError(f"a difference listed is no longer there: {theirs[:80]!r}")
        text = text.replace(theirs, ours)
    return text


def tee_part(text: str, replace=(), listed=()) -> list[str]:
    """private_ai.py's TEE part (TEE_PARTS) of `text`, its words put in first as
    comparable() puts them."""
    text = _replaced(text, replace, listed)
    lines: list[str] = []
    for start, end in TEE_PARTS:
        at = text.index(start)
        lines += text[at:text.index(end, at)].rstrip("\n").splitlines()
    return lines


def same(ours: list[str], canonical: list[str], name: str, whose: str) -> None:
    if ours != canonical:
        diff = "\n".join(difflib.unified_diff(canonical, ours, whose, "DA Media Browser",
                                              lineterm="", n=1))
        pytest.fail(f"{name} has drifted from {whose}'s copy: change it there first, then "
                    f"carry it here\n{diff[:4000]}")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.skipif(not TOOLKIT.is_dir(), reason="DA Toolkit's repository isn't beside this one")
@pytest.mark.parametrize("name", ("dcap.py", "nras.py", "ethsig.py"))
def test_the_verifiers_are_da_toolkits_copy(name):
    same(comparable(read(HERE / name)), comparable(read(TOOLKIT / name), TOOLKIT_TRANSPORT),
         name, "DA Toolkit")


@pytest.mark.skipif(not SEALEDLORE.is_dir(), reason="SealedLore's repository isn't beside this one")
def test_the_sdk_guard_is_sealedlores_copy():
    same(comparable(read(HERE / "sdk_guard.py")), comparable(read(SEALEDLORE / "sdk_guard.py")),
         "sdk_guard.py", "SealedLore")


@pytest.mark.skipif(not ASSISTANT.is_dir(), reason="DA Assistant's repository isn't beside this one")
def test_the_sealed_client_is_da_assistants_copy():
    same(comparable(read(HERE / "sealed.py")),
         comparable(read(ASSISTANT / "sealed.py"), listed=SEALED_HOOKS),
         "sealed.py", "DA Assistant")


@pytest.mark.skipif(not ASSISTANT.is_dir(), reason="DA Assistant's repository isn't beside this one")
def test_the_tee_part_is_da_assistants_copy():
    same(tee_part(read(HERE / "private_ai.py")),
         tee_part(read(ASSISTANT / "privacy.py"), ASSISTANT_TRANSPORT, TEE_HOOKS),
         "private_ai.py's TEE part", "DA Assistant")


def test_the_comparison_forgives_only_what_it_lists():
    theirs = ('"""Docstring naming llm/tee.py,\nover two lines."""\n\nimport httpx\n\n'
              "from .secure_http import ProviderError, make_client\n\n"
              "def get(session: httpx.Client):\n"
              "    try:\n        return session.get(URL)\n"
              "    except (httpx.HTTPError, ProviderError):\n        raise\n")
    ours = ("# SPDX-License-Identifier: GPL-3.0-or-later\n"
            '"""Docstring naming private_ai.py."""\n\nfrom . import netguard\n\n'
            "def get(session: netguard.Session):\n"
            "    try:\n        return session.get(URL)\n"
            "    except (netguard.RequestError, netguard.RequestError):\n        raise\n")
    assert comparable(ours) == comparable(theirs, TOOLKIT_TRANSPORT)
    for changed in (ours.replace("session.get(URL)", "session.get(URL, verify=False)"),
                    ours.replace("        raise\n", "        return None\n")):
        assert comparable(changed) != comparable(theirs, TOOLKIT_TRANSPORT)
    with pytest.raises(AssertionError, match="no longer there"):
        comparable(theirs, listed=(("not in it", "x"),))
