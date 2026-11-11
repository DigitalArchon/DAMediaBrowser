# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Where the app can send something right now, in words: for a window to say
before anything is sent, and for the status bar to say all the time.

privacy keeps the switches and says what each service may be sent. Whether
a request can actually go depends on more than its switch: setlist.fm
needs its key, the AI its key and an endpoint fit to send to, and in
Privacy Mode a private model; the AI's web search needs the AI, an endpoint
that searches, and no Privacy Mode. This puts all of that together once,
so that each window, the status bar and What Was Sent say the same thing,
in the same words, from the same facts.

A video or library marked private goes nowhere whatever is said here: the
windows that handle one say so themselves.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ai, privacy, setlistfm, store

# Who each is, in a sentence, and in the status bar where room is short.
NAMES = {
    privacy.MUSICBRAINZ: "MusicBrainz",
    privacy.COVER_ART: "the Cover Art Archive",
    privacy.SETLISTFM: "setlist.fm",
    privacy.AI: "the AI",
    privacy.WEB_SEARCH: "the AI's web search",
}
SHORT = {
    privacy.MUSICBRAINZ: "MusicBrainz",
    privacy.COVER_ART: "Covers",
    privacy.SETLISTFM: "setlist.fm",
    privacy.AI: "AI",
    privacy.WEB_SEARCH: "Web search",
}
# Where its requests go, as an address to name.
HOSTS = {
    privacy.MUSICBRAINZ: "musicbrainz.org",
    privacy.COVER_ART: "coverartarchive.org",
    privacy.SETLISTFM: "setlist.fm",
}

NOTHING = "Nothing leaves this computer."
LOCAL = "On this computer: nothing is sent."
PRIVATE_SKIPPED = "A video or library marked Private is never sent anywhere."


@dataclass(frozen=True)
class Service:
    choice: str         # privacy's: MUSICBRAINZ, COVER_ART, ...
    on: bool            # whether a request to it can go now
    where: str          # "to musicbrainz.org", for "Sends ... <where>"
    off: str            # why it can't, when it can't, as a sentence

    @property
    def name(self) -> str:
        return NAMES[self.choice]

    @property
    def short(self) -> str:
        return SHORT[self.choice]

    def line(self) -> str:
        """"MusicBrainz: to musicbrainz.org" or "setlist.fm: off - ...", for a
        list of them."""
        # "The AI", "The Cover Art Archive" - but setlist.fm is setlist.fm.
        name = "The" + self.name[3:] if self.name.startswith("the ") else self.name
        return f"{name}: {self.where}" if self.on else f"{name}: off. {self.off}"


def services(app_settings: dict | None = None,
             ai_settings: dict | None = None) -> dict[str, Service]:
    """Every place the app can send to, keyed by privacy's choice, as things
    stand: settings.json's switches unless the app's settings are given, and
    the AI's settings as loaded from them unless they're given too."""
    app_settings = store.load_app_settings() if app_settings is None else app_settings
    if ai_settings is None:
        ai_settings = ai.load_settings(app_settings)
    found = {}
    for choice in (privacy.MUSICBRAINZ, privacy.COVER_ART):
        on = privacy.allowed(choice, app_settings)
        found[choice] = Service(choice, on, f"to {HOSTS[choice]}",
                                "" if on else privacy.OFF[choice])
    setlist_off = setlistfm.not_ready(app_settings)
    found[privacy.SETLISTFM] = Service(
        privacy.SETLISTFM, not setlist_off, "to setlist.fm, with your API key", setlist_off)
    ai_off = ai.not_ready(ai_settings)
    where, _protection = ai.destination(ai_settings)
    found[privacy.AI] = Service(privacy.AI, not ai_off, where, ai_off)
    found[privacy.WEB_SEARCH] = _web_search(ai_settings, ai_off)
    return found


def search_provider(ai_settings: dict) -> str:
    return ai.SEARCH_PROVIDERS.get(ai_settings.get(ai.SETTING_SEARCH), "a search engine")


def _web_search(ai_settings: dict, ai_off: str) -> Service:
    provider = search_provider(ai_settings)
    if ai.in_privacy_mode(ai_settings):
        off = ("Never in Privacy Mode: Nano-GPT would have to read the request to "
               "search for it.")
    elif not ai.searches_the_web(ai_settings):
        off = "Web search is Nano-GPT's own, and the AI's endpoint is another."
    elif not ai.may_search(ai_settings):
        off = privacy.OFF[privacy.WEB_SEARCH]
    elif ai_off:
        off = "It's the AI's, and the AI can't be asked."
    else:
        off = ""
    return Service(privacy.WEB_SEARCH, not off,
                   f"to {provider}, through Nano-GPT: a search about the show", off)


def status_text(found: dict[str, Service]) -> str:
    """The status bar's few words: "Online: MusicBrainz · Covers · AI", or
    that nothing goes online at all."""
    on = [service for service in found.values() if service.on]
    if not on:
        return "Nothing goes online"
    names = []
    for service in on:
        if service.choice == privacy.AI and service.where.startswith("sealed"):
            names.append("AI, sealed end to end")
        elif service.choice == privacy.AI and "attested" in service.where:
            # Not "enclave": most TEE/ models are attested as RedPill's gateway.
            names.append("AI, TEE attested first")
        else:
            names.append(service.short)
    return "Online: " + " · ".join(names)


def overview(found: dict[str, Service]) -> str:
    """Each place, whether it can be sent to and where, a line each, for a
    tooltip or What Was Sent."""
    return "\n".join(f"{'✓' if service.on else '✗'} {service.line()}"
                     for service in found.values())


def names(choices, found: dict[str, Service]) -> str:
    """"musicbrainz.org and setlist.fm": the hosts of those of `choices` that
    are on, for a sentence; empty when none is."""
    hosts = [HOSTS.get(choice, NAMES[choice]) for choice in choices
             if choice in found and found[choice].on]
    return and_list(hosts)


def and_list(items) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]
