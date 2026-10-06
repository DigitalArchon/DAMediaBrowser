# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Looking a show up on setlist.fm, by artist and date.

MusicBrainz knows released albums; setlist.fm knows what was played on a
night - bootlegs, broadcasts and festival sets that were never released,
which is exactly what MusicBrainz can't name. Its API is free for
non-commercial use with a key of one's own (setlist.fm → API), kept in
the keyring like the AI's; SETLISTFM_API_KEY in the environment wins over
it. A setlist has no song lengths, so it names chapters rather than
placing them, and tells the audio how many songs to find.

Nothing is sent unless Settings → Privacy allows setlist.fm, and nothing
from it is kept but what the person applies: its terms ask that its data
isn't stored, beyond a short cache, and that it's credited with a link to
the setlist wherever it's shown.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import config, creds, privacy

BASE_URL = "https://api.setlist.fm/rest/1.0"
SITE_URL = "https://www.setlist.fm"
KEY_PAGE = "https://www.setlist.fm/settings/api"
API_KEY_ENV = "SETLISTFM_API_KEY"
KEYRING_NAME = "setlistfm-api-key"
USER_AGENT = f"DAMediaBrowser/{config.VERSION} ( {config.PROJECT_URL} )"

# setlist.fm allows a standard key two requests a second; one a second
# stays clear of it whatever else is asking.
_MIN_INTERVAL = 1.0
_turn = threading.Lock()
_last_request_time = 0.0
TIMEOUT_SECONDS = 15
# One page of a search is twenty setlists: plenty for an artist and a
# date, or an artist and a year.
PAGE_SIZE = 20

# The source a chapter's name has when it came from a setlist.
SOURCE = "setlistfm"
CREDIT = "Setlist from setlist.fm"


class SetlistError(Exception):
    pass


class NotConfigured(SetlistError):
    """No API key: nothing can be looked up until one is set."""


# --- the key ----------------------------------------------------------------------


def stored_key() -> str:
    try:
        return creds.get_secret(KEYRING_NAME) or ""
    except Exception:  # noqa: BLE001 - keyring locked or unavailable
        return ""


def store_key(key: str) -> None:
    """Keep the key in the keyring, or forget it when empty. Raises when
    the keyring can't be written."""
    if key:
        creds.set_secret(KEYRING_NAME, key)
    else:
        creds.delete_secret(KEYRING_NAME)


def api_key() -> str:
    """The key in force: the environment's, else the keyring's."""
    return os.environ.get(API_KEY_ENV, "").strip() or stored_key()


def not_ready(app_settings: dict | None = None, key: str | None = None) -> str:
    """Why setlist.fm can't be asked, for saying so; empty when it can."""
    if not privacy.allowed(privacy.SETLISTFM, app_settings):
        return privacy.OFF[privacy.SETLISTFM]
    if not (api_key() if key is None else key):
        return "Add a setlist.fm API key in Settings → setlist.fm (it's free)."
    return ""


def is_ready(app_settings: dict | None = None) -> bool:
    return not not_ready(app_settings)


# --- what comes back --------------------------------------------------------------


@dataclass(frozen=True)
class Song:
    title: str
    tape: bool = False  # played from tape - an intro, an outro - not live
    info: str = ""
    cover_of: str = ""
    encore: int = 0  # which encore it was in, 0 for the main set
    set_name: str = ""


@dataclass(frozen=True)
class Setlist:
    id: str
    artist: str
    date: datetime.date | None
    venue: str = ""
    city: str = ""
    country: str = ""
    tour: str = ""
    url: str = ""
    songs: list[Song] = field(default_factory=list)
    artist_mbid: str = ""

    def where(self) -> str:
        return ", ".join(part for part in (self.venue, self.city, self.country) if part)

    def describe(self) -> str:
        """"19 Apr 2026 · Copperfield Hall, Leeds, UK · Glass Harbor · 18 songs"."""
        played = sum(1 for song in self.songs if not song.tape)
        parts = [self.date.strftime("%d %b %Y") if self.date else "no date",
                 self.where() or "somewhere", self.artist]
        if self.tour:
            parts.append(self.tour)
        parts.append(f"{played} song{'s' if played != 1 else ''}")
        return " · ".join(parts)

    def tracks(self, include_tape: bool = False) -> list[dict]:
        """As a tracklist: titles in order, no lengths."""
        return [{"title": song.title, "length": None} for song in self.songs
                if include_tape or not song.tape]


def _parse_date(text: str | None) -> datetime.date | None:
    try:
        return datetime.datetime.strptime(text or "", "%d-%m-%Y").date()
    except ValueError:
        return None


def parse_setlist(data: dict) -> Setlist:
    artist = data.get("artist") or {}
    venue = data.get("venue") or {}
    city = venue.get("city") or {}
    songs = []
    for part in ((data.get("sets") or {}).get("set") or []):
        for song in part.get("song") or []:
            title = (song.get("name") or "").strip()
            if not title:
                continue  # an unknown song: setlist.fm lists it with no name
            songs.append(Song(
                title=title, tape=bool(song.get("tape")), info=song.get("info") or "",
                cover_of=((song.get("cover") or {}).get("name") or ""),
                encore=int(part.get("encore") or 0), set_name=part.get("name") or "",
            ))
    return Setlist(
        id=data.get("id") or "", artist=artist.get("name") or "",
        date=_parse_date(data.get("eventDate")), venue=venue.get("name") or "",
        city=city.get("name") or "", country=((city.get("country") or {}).get("name") or ""),
        tour=((data.get("tour") or {}).get("name") or ""), url=data.get("url") or SITE_URL,
        songs=songs, artist_mbid=artist.get("mbid") or "",
    )


# --- asking -----------------------------------------------------------------------


def _get(path: str, params: dict, key: str | None = None,
         app_settings: dict | None = None) -> dict | None:
    """One request; None for setlist.fm's "nothing found" (a 404)."""
    global _last_request_time
    if not privacy.allowed(privacy.SETLISTFM, app_settings):
        raise SetlistError(privacy.OFF[privacy.SETLISTFM])
    key = api_key() if key is None else key
    if not key:
        raise NotConfigured("Add a setlist.fm API key in Settings → setlist.fm.")
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    request = urllib.request.Request(
        f"{BASE_URL}/{path}?{query}",
        headers={"x-api-key": key, "Accept": "application/json",
                 "Accept-Language": "en", "User-Agent": USER_AGENT},
    )
    with _turn:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise SetlistError(_http_error(exc)) from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise SetlistError(str(reason) or repr(exc)) from exc
        finally:
            _last_request_time = time.monotonic()
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SetlistError(f"the reply wasn't JSON: {exc}") from exc


def _http_error(exc: urllib.error.HTTPError) -> str:
    hints = {
        400: "setlist.fm didn't understand the search",
        401: "the setlist.fm API key was refused",
        403: "the setlist.fm API key was refused",
        429: "setlist.fm says too many requests - try again in a moment",
    }
    return hints.get(exc.code, f"setlist.fm answered HTTP {exc.code}")


def search(artist: str, date: datetime.date | None = None, year: int | None = None,
           place: str = "", page: int = 1, key: str | None = None) -> list[Setlist]:
    """The setlists of `artist` on `date`, or in `year`; `place`, given, is
    tried as the venue and then as the city. Most recent first, as
    setlist.fm gives them; only those with songs."""
    params = {
        "artistName": artist.strip(),
        "date": date.strftime("%d-%m-%Y") if date else None,
        "year": None if date else year,
        "p": page,
    }
    attempts = [dict(params, venueName=place), dict(params, cityName=place)] if place.strip() \
        else [params]
    for attempt in attempts:
        data = _get("search/setlists", attempt, key)
        found = [parse_setlist(item) for item in (data or {}).get("setlist") or []]
        found = [setlist for setlist in found if setlist.songs]
        if found:
            return found
    return []


def check_key(key: str, app_settings: dict | None = None) -> str:
    """Ask setlist.fm one small thing with this key; raises if it's
    refused. Returns what came back, for saying it worked."""
    data = _get("search/artists", {"artistName": "a", "p": 1}, key, app_settings)
    return f"{(data or {}).get('total', 0)} artists"


# --- when and who, from a file's name ---------------------------------------------

_FULL_DATE_PATTERNS = (
    # 2026-04-19, 2026.04.19, 2026_04_19, 2026 04 19
    re.compile(r"(?<!\d)((?:19|20)\d{2})[-._ ](\d{1,2})[-._ ](\d{1,2})(?!\d)"),
    # 20260419, as a TV recorder names a recording
    re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)"),
)
_DAY_FIRST = re.compile(r"(?<!\d)(\d{1,2})[-.](\d{1,2})[-.]((?:19|20)\d{2})(?!\d)")
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def guess_date(*texts: str) -> tuple[datetime.date | None, int | None]:
    """(a full date, else a year) that names a show, from the first text
    that has one: a file or folder name, usually. A day-first date
    (19.04.2026) is only read when it can't be month-first as well."""
    for text in texts:
        for pattern in _FULL_DATE_PATTERNS:
            for match in pattern.finditer(text or ""):
                try:
                    return datetime.date(*(int(g) for g in match.groups())), None
                except ValueError:
                    continue
        for match in _DAY_FIRST.finditer(text or ""):
            day, month, year = (int(g) for g in match.groups())
            if day > 12 or day == month:
                try:
                    return datetime.date(year, month, day), None
                except ValueError:
                    continue
    for text in texts:
        years = [int(y) for y in _YEAR.findall(text or "")
                 if int(y) <= datetime.date.today().year]
        if years:
            return None, years[0]
    return None, None


def parse_when(text: str) -> tuple[datetime.date | None, int | None]:
    """What a person typed as the date: "2026-04-19", "19/04/2026" (day
    first, as most of the world writes it), or a year."""
    text = (text or "").strip()
    if not text:
        return None, None
    match = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-]((?:19|20)\d{2})", text)
    if match:
        day, month, year = (int(g) for g in match.groups())
        try:
            return datetime.date(year, month, day), None
        except ValueError:
            pass
    date, year = guess_date(text)
    if date or (year and re.fullmatch(r"\d{4}", text)):
        return date, year
    raise ValueError(f"“{text}” isn't a date - try 2026-04-19, or a year")


def video_texts(video, library_root=None) -> list[str]:
    """Where a video's date might be written: its name, then the folders
    it's in below the library, nearest first."""
    from pathlib import PurePath

    texts = [video.get("display_name") or "", PurePath(video["path"]).name]
    folder = PurePath(video["path"]).parent
    root = PurePath(library_root) if library_root else None
    for _ in range(3):
        if not folder.name or (root is not None and (folder == root
                                                     or not folder.is_relative_to(root))):
            break
        texts.append(folder.name)
        folder = folder.parent
    return texts


def _plain(text: str) -> str:
    return re.sub(r"[^\w]+", " ", (text or "").casefold()).strip()


def same_artist(a: str, b: str) -> bool:
    """Whether two ways of writing an artist are the same one: case,
    punctuation and a leading "The" aside."""
    def bare(text):
        text = _plain(text)
        return text[4:] if text.startswith("the ") else text
    return bool(bare(a)) and bare(a) == bare(b)
