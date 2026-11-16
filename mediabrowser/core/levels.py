# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""How private a model is, as a property of the address it is reached through, at one of
three levels; and the rule a private conversation keeps when its model is changed.

Pure: no network, nothing read or written. Shared with DA Assistant line for line (this
copy differs only in this docstring, its imports and the hooks tests/test_shared_code.py
lists: privacy.py is this app's private_ai.py). SealedLore's providers/privacy.py, in
this app's shape.

A model is of one kind (`Privacy.kind`), the mechanism:

- "sealed": every request is sealed on this computer to an attested enclave's key; the
  service in between relays ciphertext. NanoGPT's `private/` models (its Private Mode,
  sealed.py), every model on Tinfoil's own API (tinfoil_direct.py: by the address,
  whatever its protocol setting says, services.sealed_service), and Chutes' -TEE models
  on any of Chutes' hosts (services.chutes_tee, chutes.py).
- "tee": the model runs in an attested enclave and its replies are signed, but the text
  passes the service in the clear: NanoGPT's `TEE/` models (privacy.py).
- "": anything else, Chutes' other models included.

And at one of three levels (`Privacy.level`; SealedLore's author, Nov 15 2026), what it
is worth:

- "highest": NanoGPT's `private/` models, and every model on Tinfoil's own API;
- "partial": NanoGPT's `TEE/` models, and Chutes' -TEE models, always sealed end to end
  (`CHUTES_NOTE` says what that rests on);
- "": not private, every other model.

A conversation moves only to a model at its own level or higher, on any service
(`move_refusal`): a `private/` or Tinfoil conversation never moves to Chutes, nor to a
`TEE/` model.

When in doubt, sealed: a sealed host is sealed whatever the model and whatever its
protocol setting says, a Chutes id that may end "-TEE" is one, and a `private/` model on
any other address is still NanoGPT's Private Mode, whose client refuses to send it
anywhere but NanoGPT, as it always has. A model whose address is not known is judged by
its id alone.
"""

from dataclasses import dataclass

from . import services
from .private_ai import is_private_mode, is_tee

HIGHEST, PARTIAL, NOT_PRIVATE = "highest", "partial", ""

NANOGPT_PRIVATE = "nanogpt-private"
NANOGPT_TEE = "nanogpt-tee"
TINFOIL = "tinfoil"
CHUTES = "chutes"
SERVICE_NAMES = {
    NANOGPT_PRIVATE: "NanoGPT's Private Mode",
    NANOGPT_TEE: "NanoGPT's TEE models",
    TINFOIL: "Tinfoil",
    CHUTES: "Chutes",
}
# The service behind each sealed protocol an address may speak.
_SEALED_SERVICES = {"tinfoil": TINFOIL}
# Each sealed service's level: one not named here is taken as the highest, so that a
# conversation recorded on it moves to nothing less.
_SEALED_LEVELS = {NANOGPT_PRIVATE: HIGHEST, TINFOIL: HIGHEST, CHUTES: PARTIAL}
_RANK = {HIGHEST: 2, PARTIAL: 1, NOT_PRIVATE: 0}
LEVEL_WORDS = {HIGHEST: "Highest privacy", PARTIAL: "Partial privacy",
               NOT_PRIVATE: "Not private"}
# The three levels in one passage: in the notes, the attestation check and the manual.
PRIVACY_LEVELS = (
    "There are three levels of privacy. Highest: end-to-end encrypted to an attested "
    "enclave whose software is measured (NanoGPT's private/ models, and Tinfoil's). "
    "Partial: attested, but with a gap in what is proved (NanoGPT's TEE/ models, whose "
    "text passes NanoGPT's gateway in the clear, and Chutes' -TEE models, encrypted all "
    "the way to a key that rests on software Chutes signs). Not private: every other "
    "model, whose provider can read your text.")

E2EE_NOTE = (
    "Highest privacy. End-to-end encrypted: every message is sealed on this machine to a "
    "key only the model's attested enclave holds. nano-gpt relays ciphertext; it sees your "
    "account, the model, timing, sizes and cost, never the text.")
TINFOIL_NOTE = (
    "Highest privacy. End-to-end encrypted, straight to Tinfoil (preliminary): every "
    "message is sealed on this machine to a key only Tinfoil's attested router enclave "
    "holds, over a connection pinned to the enclave's attested TLS key. Tinfoil sees your "
    "account, the model, timing, sizes and cost, never the text. That the router hands "
    "your request only to attested model enclaves is the router's published code, not "
    "something this app checks.")
CHUTES_NOTE = (
    "Partial privacy. End-to-end encrypted to one Chutes instance (preliminary): each "
    "message is sealed on this machine to the key of an instance whose Intel TDX quote, "
    "for our nonce, binds that key, whose GPUs NVIDIA vouches for, and whose VM image is "
    "one Chutes publishes. Why partial: that only the model can read your text rests on "
    "software Chutes signs. The program that makes and holds the key (Chutes' aegis "
    "library, chutes-aegis.so, a closed binary) is admitted to the machine by Chutes' "
    "signature, not measured, and the list of measured images is Chutes' own. Beside "
    "NanoGPT's TEE models: your text is encrypted all the way here, where theirs passes "
    "NanoGPT's gateway in the clear; but what they attest is the enclave as measured, "
    "where here the program holding the key is outside the measurement. Replies stream, "
    "each piece held to its number, which Chutes' closed library writes: a piece "
    "dropped, repeated or out of its place is refused. Chutes sees your account, the "
    "model, timing, sizes and cost; the instance's operator, a third-party miner, "
    "ciphertext.")
TEE_NOTE = (
    "Partial privacy. TEE: the model runs in an attested enclave and its replies are "
    "signed, but the text passes nano-gpt's gateway in the clear.")
CHUTES_PLAIN_NOTE = (
    "Not private. Chutes reads this model's text, as any provider of an ordinary model "
    "does. Chutes' -TEE models are the ones sent end-to-end encrypted (partial privacy).")
PLAIN_NOTE = "Not private: the model's provider can read your text."


@dataclass(frozen=True)
class Privacy:
    kind: str = ""
    service: str = ""
    # A model that is not private on a service that also has private ones (CHUTES, for
    # its ordinary models): for the notes alone, never deciding anything.
    plain_on: str = ""

    @property
    def sealed(self):
        """End-to-end encrypted: never sent in the clear."""
        return self.kind == "sealed"

    @property
    def private(self):
        """A TEE or end-to-end encrypted model: attested, its calls kept to it."""
        return self.kind != ""

    @property
    def level(self):
        if self.kind == "sealed":
            return _SEALED_LEVELS.get(self.service, HIGHEST)
        if self.kind == "tee":
            return PARTIAL
        return NOT_PRIVATE

    @property
    def words(self):
        """The level's own words: "Highest privacy", "Partial privacy", "Not private"."""
        return LEVEL_WORDS[self.level]

    def label(self):
        """(short label, what it protects, its level's words first), for where models
        are chosen."""
        if self.service == NANOGPT_PRIVATE:
            return "🔐 End-to-end encrypted", E2EE_NOTE
        if self.service == TINFOIL:
            return "🔐 End-to-end encrypted (Tinfoil)", TINFOIL_NOTE
        if self.service == CHUTES:
            return "🔐 End-to-end encrypted (Chutes, partial)", CHUTES_NOTE
        if self.sealed:
            return "🔐 End-to-end encrypted", (
                f"{self.words}. End-to-end encrypted through "
                f"{SERVICE_NAMES.get(self.service, self.service)}.")
        if self.kind == "tee":
            return "TEE (partial)", TEE_NOTE
        if self.plain_on == CHUTES:
            return "Not private", CHUTES_PLAIN_NOTE
        return "Not private", PLAIN_NOTE


NONE = Privacy()


def privacy_at(model, url="", protocol="auto"):
    """`model`'s privacy at the address `url`, spoken in `protocol`: a sealed address's
    service for every model on it, Chutes' for a -TEE model on one of its hosts whatever
    it speaks, else NanoGPT's kinds by the id (capitals and stray spaces never make it
    less private). With no address, by the id alone. The address decides, never a
    provider's name."""
    sealed = services.sealed_service(url, protocol)
    if sealed:
        return Privacy("sealed", _SEALED_SERVICES[sealed])
    if services.chutes_tee(model, url):
        return Privacy("sealed", CHUTES)
    if is_private_mode(model):
        return Privacy("sealed", NANOGPT_PRIVATE)
    if is_tee(model):
        return Privacy("tee", NANOGPT_TEE)
    if services.on_chutes(url):
        return Privacy(plain_on=CHUTES)
    return NONE


def rank(privacy):
    """How private, by level: 2 the highest, 1 partial, 0 not private."""
    return _RANK.get(privacy.level, 0)


def more_private(one, other):
    """The more private of two, by level; `one` when they are as private."""
    return one if rank(one) >= rank(other) else other


def level_phrase(privacy):
    """Its level as a sentence says it."""
    return {HIGHEST: "the highest privacy", PARTIAL: "partial privacy"}.get(
        privacy.level, "no privacy")


def level_line(privacy):
    """One sentence on what `privacy`'s level rests on, for an attestation's details."""
    if privacy.level == HIGHEST:
        return "Highest privacy: end-to-end encrypted to an attested enclave."
    if privacy.service == CHUTES:
        return ("Partial privacy: end-to-end encrypted, but to a key that rests on "
                "software Chutes signs, not on what is measured.")
    if privacy.level == PARTIAL:
        return ("Partial privacy: an attested enclave, but your text passes NanoGPT's "
                "gateway in the clear.")
    return PLAIN_NOTE


# What a conversation at each level may move to, as the refusals say it.
LEVEL_CHOICES = {
    HIGHEST: "NanoGPT's private/ models or Tinfoil's",
    PARTIAL: "NanoGPT's TEE/ or private/ models, Chutes' -TEE models or Tinfoil's",
}


def holds(kept, found):
    """Whether `found`, where a request would go now, keeps what `kept` promised: at
    its level or higher, and an end-to-end encrypted one sealed by the same service,
    whose own client alone seals to it. An explicit move (`move_refusal`) records the
    new one first."""
    if rank(found) < rank(kept):
        return False
    return not (kept.sealed and found.service != kept.service)


def move_refusal(current, new, privacy):
    """Why a conversation on `current` may not move to `new`, or None, each judged by
    `privacy(model)`: only to a model at its own level or higher, on any service. A
    conversation with the highest privacy goes to NanoGPT's private/ models or
    Tinfoil's; one with partial privacy to those, NanoGPT's TEE/ models or Chutes' -TEE
    ones."""
    now, then = privacy(current), privacy(new)
    if rank(then) >= rank(now):
        return None
    if then.sealed:
        name = SERVICE_NAMES.get(then.service, then.service)
        what = f"{new} is end-to-end encrypted through {name}, but has {level_phrase(then)}"
    elif then.private:
        what = (f"{new} is not end-to-end encrypted (a TEE model), so it has "
                f"{level_phrase(then)}")
    else:
        what = (f"{new} is not a TEE or end-to-end encrypted model, so it has "
                f"{level_phrase(then)}")
    return (f"{what}, and this conversation is kept to {level_phrase(now)}: choose one "
            f"of {LEVEL_CHOICES[now.level]}, or start a new conversation.")


def plain_refusal(url, what="this request", protocol="auto", model=""):
    """Why a client that sends in the clear must not send `what` to `url`, or None:
    `url`'s host takes only end-to-end encrypted requests, or `protocol` names a sealed
    one; or `model`, the request's own, is one of Chutes' TEE models on one of Chutes'
    hosts, sent only end-to-end encrypted. Asked by every plain client before anything
    is sent."""
    sealed = services.sealed_service(url, protocol)
    host = services.host_of(url) or "the address"
    if sealed:
        name = SERVICE_NAMES[_SEALED_SERVICES[sealed]]
        return (f"{host} is {name}'s end-to-end encrypted endpoint, and {what} would go "
                f"to it in the clear, so nothing was sent.")
    if model and services.chutes_tee(model, url):
        return (f"{services.bare_model(model)} is one of Chutes' TEE models, which are "
                f"sent only end-to-end encrypted (partial privacy): {what} would go to "
                f"{host} in the clear, so nothing was sent.")
    if model and is_private_mode(model):
        return (f"{str(model).strip()} is end-to-end encrypted, and is only ever sent "
                f"sealed: {what} would go to {host} in the clear, so nothing was sent.")
    return None
