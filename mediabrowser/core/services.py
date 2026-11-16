# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The services an address may belong to, read from the address itself.

Pure: no network, nothing read or written, nothing of the app's imported. Shared with DA
Assistant line for line (this copy differs only in this docstring, its imports and the
hooks tests/test_shared_code.py lists).

Which service an address belongs to decides what may be sent there and how, and it is
decided here however the address got into the settings - a preset, typed by hand, an
older config, a restore: whatever way Tinfoil's or Chutes' address comes in, the right
protocol, sealing, key origins and privacy marks follow from it. "When in doubt, sealed"
(SealedLore's rule, models/config.py): a host that takes only end-to-end encrypted
requests is sealed whatever any protocol setting says.

- **Tinfoil** (`TINFOIL_BASE_URL`): its own API, every model end-to-end encrypted to its
  attested router (tinfoil_direct.py). Any host under tinfoil.sh is sealed
  (`sealed_service`); its key goes to `TINFOIL_KEY_ORIGIN` only.
- **Chutes**: its OpenAI-compatible API at llm.chutes.ai, its own open-source proxy of
  Anthropic's Messages API at claude.chutes.ai, and api.chutes.ai, where its end-to-end
  encryption goes (chutes.py). A model whose id ends "-TEE" on any of Chutes' hosts
  (`chutes_tee`) is sent only end-to-end encrypted, whatever the endpoint speaks; its
  other models are ordinary. A Chutes key goes to Chutes' three fixed origins
  (`CHUTES_KEY_ORIGINS`) and nowhere else: the one stated exception to "a key goes only
  to the origin it was entered for".
- **Anthropic**: its own Messages API at api.anthropic.com (anthropic.py), and Chutes'
  proxy of it.
"""

from urllib.parse import urlparse

# Tinfoil's own API (tinfoil_direct.py), and the one origin its key goes to.
TINFOIL_BASE_URL = "https://inference.tinfoil.sh/v1"
TINFOIL_KEY_ORIGIN = "https://inference.tinfoil.sh:443"
# Chutes: its OpenAI-compatible API, its own open-source proxy of Anthropic's Messages
# API (github.com/chutesai/claude-proxy, which hands each request on to the
# OpenAI-compatible one), and the API its end-to-end encryption goes through.
CHUTES_BASE_URL = "https://llm.chutes.ai/v1"
CHUTES_ANTHROPIC_URL = "https://claude.chutes.ai"
CHUTES_E2EE_ORIGIN = "https://api.chutes.ai"
# Chutes' own origins, a fixed list: a Chutes key saved for one of them goes to the
# endpoint's own address for Chutes' ordinary models and to CHUTES_E2EE_ORIGIN for its
# -TEE ones, over https, and nowhere else.
CHUTES_KEY_ORIGINS = frozenset({"https://llm.chutes.ai:443", "https://api.chutes.ai:443",
                                "https://claude.chutes.ai:443"})
# Chutes' domains: a model on a host under one is judged by Chutes' rule (chutes_tee).
CHUTES_DOMAINS = ("chutes.ai", "chutes.dev")
# Anthropic's own API.
ANTHROPIC_URL = "https://api.anthropic.com"
ANTHROPIC_HOST = "api.anthropic.com"

# The hosts whose chat endpoints take only end-to-end encrypted requests, by domain (any
# host under it), and the protocol each speaks.
SEALED_HOSTS = {"tinfoil.sh": "tinfoil"}
# The protocols an endpoint may be set to speak; "auto" reads the address.
PROTOCOLS = ("auto", "openai", "anthropic", "tinfoil")
SEALED_PROTOCOLS = frozenset({"tinfoil"})
# Hosts that speak Anthropic's Messages API when nothing else is said.
ANTHROPIC_HOSTS = (ANTHROPIC_HOST, "claude.chutes.ai")

# The services an address may be recognised as (`service_of`).
TINFOIL, CHUTES, ANTHROPIC = "tinfoil", "chutes", "anthropic"


def host_of(url):
    """An address's host as a request would reach it: lowercase, a trailing dot gone
    ("inference.tinfoil.sh." is the same machine). "" for one that cannot be read."""
    try:
        return (urlparse(str(url or "").strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def origin(url):
    """`url`'s origin, "scheme://host:port", the port written even where it is the
    scheme's own, the host lowercase and without a trailing dot, never a user name or
    password: what a key is kept for. "" for an address that is not a web address."""
    try:
        parsed = urlparse(str(url or "").strip())
        port = parsed.port
    except ValueError:
        return ""
    scheme = parsed.scheme.lower()
    host = host_of(url)
    if not host or scheme not in ("http", "https"):
        return ""
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}:{port or (443 if scheme == 'https' else 80)}"


def under(host, domain):
    """Whether `host` is `domain` or any host under it."""
    return host == domain or host.endswith("." + domain)


def sealed_service(url, protocol="auto"):
    """The end-to-end encrypted protocol an endpoint must be spoken in ("tinfoil"), or
    "": the one `protocol` names, else the one its host is known for, whatever
    `protocol` says. When in doubt, sealed: an endpoint set to "openai" on Tinfoil's
    host is still Tinfoil's."""
    if protocol in SEALED_PROTOCOLS:
        return protocol
    host = host_of(url)
    for domain, name in SEALED_HOSTS.items():
        if under(host, domain):
            return name
    return ""


def on_chutes(url):
    """Whether `url` is one of Chutes' hosts (chutes.ai, chutes.dev and any host under
    them)."""
    host = host_of(url)
    return any(under(host, domain) for domain in CHUTES_DOMAINS)


def bare_model(model):
    """A model id without the endpoint name a reference may carry ("Chutes::x" -> "x"),
    stripped."""
    text = str(model or "")
    return text.split("::", 1)[1].strip() if "::" in text else text.strip()


def is_tee_id(model):
    """Whether a model id names one of Chutes' TEE models: it ends "-TEE", in any case,
    whatever stray spaces, less a ":variant" after it ("Qwen/Qwen3-32B-TEE:THINKING").
    Read generously, every ":" taken as where a variant might begin: a TEE model judged
    ordinary would be sent in the clear."""
    bare = bare_model(model)
    pieces = [bare] + [bare[:at] for at, char in enumerate(bare) if char == ":"]
    return any(piece.strip().upper().endswith("-TEE") for piece in pieces)


def chutes_tee(model, url):
    """Whether `model` on the endpoint at `url` is one of Chutes' TEE models
    (`is_tee_id` on one of Chutes' hosts): always sent end-to-end encrypted to an
    attested Chutes instance (chutes.py), never in the clear, whatever the endpoint
    speaks. Chutes lists exactly these as confidential compute (11 of its 19 models, Nov
    15 2026)."""
    return on_chutes(url) and is_tee_id(model)


def chat_protocol(url, protocol="auto"):
    """The protocol a chat endpoint speaks: a sealed one for a sealed host whatever
    `protocol` says (`sealed_service`), else `protocol` unless "auto", else Anthropic's
    for its own host and Chutes' proxy of it, and OpenAI's for anything else."""
    sealed = sealed_service(url, protocol)
    if sealed:
        return sealed
    if protocol in PROTOCOLS and protocol != "auto":
        return protocol
    host = host_of(url)
    if any(under(host, known) for known in ANTHROPIC_HOSTS):
        return "anthropic"
    return "openai"


def service_of(url):
    """Which of the services this module knows `url` belongs to - TINFOIL, CHUTES or
    ANTHROPIC - or "": however the address got into the settings."""
    if sealed_service(url) == "tinfoil":
        return TINFOIL
    if on_chutes(url):
        return CHUTES
    if under(host_of(url), ANTHROPIC_HOST):
        return ANTHROPIC
    return ""


def chutes_key_refusal(url, key_saved_for=""):
    """Why a Chutes key may not go from the endpoint at `url`, the key saved for
    `key_saved_for` (an origin), to Chutes' end-to-end encryption at
    CHUTES_E2EE_ORIGIN, or None: the endpoint must be at one of Chutes' own origins,
    over https, and the key saved for one of them."""
    if origin(url) not in CHUTES_KEY_ORIGINS:
        return (f"{url_shown(url)} is not one of Chutes' own addresses: its end-to-end "
                f"encrypted models are sent only through {CHUTES_E2EE_ORIGIN}, from an "
                f"endpoint at {CHUTES_BASE_URL} or {CHUTES_ANTHROPIC_URL}, so nothing was "
                f"sent.")
    if key_saved_for and key_saved_for not in CHUTES_KEY_ORIGINS:
        return (f"The key was saved for {key_saved_for}, which is not one of Chutes' own "
                f"addresses, so it is not sent to {CHUTES_E2EE_ORIGIN}; nothing was sent. "
                f"Enter your Chutes key again for this provider.")
    return None


def tinfoil_key_refusal(url, key_saved_for=""):
    """Why a Tinfoil key may not go to the endpoint at `url`, the key saved for
    `key_saved_for`, or None: it goes to TINFOIL_KEY_ORIGIN, over https, and nowhere
    else."""
    if origin(url) != TINFOIL_KEY_ORIGIN:
        return (f"{url_shown(url)} is not Tinfoil's API: a Tinfoil key goes only to "
                f"{TINFOIL_BASE_URL}, so nothing was sent. Set the address to that.")
    if key_saved_for and key_saved_for != TINFOIL_KEY_ORIGIN:
        return (f"The key was saved for {key_saved_for}, not Tinfoil's API, so it is not "
                f"sent there; nothing was sent. Enter your Tinfoil key again.")
    return None


def url_shown(url):
    """An address as an error may name it: its scheme, host, port and path, never a
    user name or password written into it, nor its query."""
    try:
        parsed = urlparse(str(url or "").strip())
        port = parsed.port
    except ValueError:
        return "the address"
    host = host_of(url)
    if not host:
        return "the address"
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}{f':{port}' if port else ''}{parsed.path}"
