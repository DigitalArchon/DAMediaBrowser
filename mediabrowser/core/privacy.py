# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""What the app may send out, and to whom.

Everything that leaves this computer goes to one of four places, and each
is the person's to allow in Settings → Privacy:

- MusicBrainz, searched for tracklists: the search is usually the video's
  file or folder name.
- The Cover Art Archive, asked for the sleeve of a release a video was
  matched to - which tells it which release.
- The AI service: chapter times, file and folder names, tracklists and
  frames from the video.
- A web search the AI service runs for the model, about the show.

None is allowed until it's ticked. Each place that goes out checks here
itself, right where it would, so a path through the app that forgot to
ask still sends nothing. A video or library marked private (see
protection) goes nowhere whatever is allowed here.
"""

from __future__ import annotations

from . import store

MUSICBRAINZ = "allow_musicbrainz"
COVER_ART = "allow_cover_art"
AI = "allow_ai"
WEB_SEARCH = "allow_ai_web_search"
CHOICES = (MUSICBRAINZ, COVER_ART, AI, WEB_SEARCH)

# Nothing goes out until it's allowed.
DEFAULTS = dict.fromkeys(CHOICES, False)

# (label, what it sends) for Settings.
TEXT = {
    MUSICBRAINZ: (
        "Look up tracklists on MusicBrainz",
        "Sends a search - usually the video's file or folder name - and the ids of "
        "the releases it finds to musicbrainz.org, to name chapters.",
    ),
    COVER_ART: (
        "Fetch cover art from the Cover Art Archive",
        "Asks coverartarchive.org for the cover of the release a video was matched "
        "to on MusicBrainz, which tells it which release. Without it, covers come "
        "from the video's folder, the file itself, or a frame of it.",
    ),
    AI: (
        "Send videos to the AI",
        "Sends chapter times, file and folder names, tracklists and frames from the "
        "video to the AI service on the AI tab (Nano-GPT unless it's changed), which "
        "passes them on to the model's maker.",
    ),
    WEB_SEARCH: (
        "Let the AI search the web",
        "The AI service runs a web search about the show for the model - the "
        "artist, tour and venue - with the search provider chosen on the AI tab.",
    ),
}

# Where each is switched back on, for saying why something is off.
OFF = {
    MUSICBRAINZ: "Looking things up on MusicBrainz is switched off in Settings → Privacy.",
    COVER_ART: "Fetching cover art is switched off in Settings → Privacy.",
    AI: "Sending anything to the AI is switched off in Settings → Privacy.",
    WEB_SEARCH: "The AI's web search is switched off in Settings → Privacy.",
}


def allowed(choice: str, app_settings: dict | None = None) -> bool:
    """Whether this may go out: as settings.json has it, unless the app's
    settings are given (already loaded, or a form's)."""
    settings = store.load_app_settings() if app_settings is None else app_settings
    return bool(settings.get(choice, DEFAULTS[choice]))


def set_allowed(app_settings: dict, choice: str, on: bool) -> None:
    app_settings[choice] = bool(on)
