# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Identifying a whole library, unattended.

Every way the app has of naming chapters, run video by video with nobody
watching - so it holds itself to a stricter standard than any one of them
does when a person is there to check:

- It only fills in what's missing. A chapter that already has a real name
  keeps it; chapters are only placed afresh for a video in one piece, or
  one whose chapters were only estimated and have no names yet.
- A MusicBrainz release is only used when its tracks add up to the video,
  so each is placed where it plays rather than matched by guesswork. When
  the AI is to look at the video too, it also puts right a search the
  file's name got wrong, and says which of the releases that add up is
  this show.
- What the AI marks as a guess can be left out.
- The state of every video it changes is kept, so a whole run can be undone.

Which methods run, in what order, and how many AI requests it may make are
the person's to choose (Options). Free methods go first unless they ask
for the most accurate first - which for a Blu-ray means reading its menu
before trying MusicBrainz.

Nothing here touches a widget or the stored library: identify() works on a
copy and returns what it would change, and the caller applies it.
"""

from __future__ import annotations

import copy
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import PurePath

from . import (
    ai,
    ai_chapters,
    ai_musicbrainz,
    audio_levels,
    chaptergen,
    library,
    menu_chapters,
    musicbrainz,
    naming,
    proposal,
    utils,
)

MUSICBRAINZ = "musicbrainz"
AUDIO = "audio"
MENU = "menu"
AI_LOOK = "ai_look"
TRANSLATE = "translate"

METHODS = (MUSICBRAINZ, AUDIO, MENU, AI_LOOK, TRANSLATE)
AI_METHODS = (MENU, AI_LOOK, TRANSLATE)
FREE_METHODS = (MUSICBRAINZ, AUDIO)
# What may run for a private video: nothing that sends anything out.
LOCAL_METHODS = (AUDIO,)
# How many AI requests identifying one video may make (Just Figure It Out,
# or Identify from its right-click menu): the menu, a better MusicBrainz
# search and a choice or two between its releases, a look at the video and
# a translation. A few cents at most.
ONE_VIDEO_AI_BUDGET = 6
# The most AI requests each method may make for one video. The look at the
# video brings the AI's help with MusicBrainz along: a choice between
# releases, a better search, and a choice between what that finds.
AI_REQUESTS = {MENU: 1, AI_LOOK: 4, TRANSLATE: 1}

LAST_RESORT = "last_resort"
ACCURATE_FIRST = "accurate_first"

ORDER = {
    LAST_RESORT: (MUSICBRAINZ, AUDIO, MENU, AI_LOOK, TRANSLATE),
    ACCURATE_FIRST: (MENU, MUSICBRAINZ, AUDIO, AI_LOOK, TRANSLATE),
}

# MusicBrainz releases looked at per video: the search's best few. Each is
# a request, and MusicBrainz allows one every couple of seconds.
RELEASES_TRIED = 4
# The confidences of an AI answer that count as sure.
SURE = ("high", "medium")
# Of a video's chapters that already have real names, how many must be on
# a release's tracklist for it to be the same show - and how alike two
# titles must be to count as the same song.
AGREEMENT_FRACTION = 0.5
SAME_TITLE_RATIO = 0.8
# How near a recording's length must be to a single-song video's. Looser
# than a whole show's: a music video has an intro, a live clip applause.
RECORDING_SECONDS = 45.0
RECORDING_FRACTION = 0.25
# How close a release's tracks must add up to the video, unattended. A live
# album cut from the same master as its video lands within seconds; the 3%
# a person checking can allow let one 87-minute album of a ten-night box
# set pass for every night of it.
WHOLE_VIDEO_SECONDS = 20.0
WHOLE_VIDEO_FRACTION = 0.005


def adds_up(lengths, duration: float) -> bool:
    if not lengths or any(not length for length in lengths):
        return False
    tolerance = max(WHOLE_VIDEO_SECONDS, WHOLE_VIDEO_FRACTION * duration)
    return abs(sum(lengths) - duration) <= tolerance

# Where the journal of the last run lives in a library's settings.
JOURNAL = "identify_run"


class Cancelled(Exception):
    pass


@dataclass
class Options:
    methods: set[str] = field(default_factory=lambda: set(FREE_METHODS))
    ai_policy: str = LAST_RESORT
    ai_budget: int = 40  # AI requests this run may make
    only_sure: bool = True  # leave out what the AI marks as a guess
    translate: bool = True  # AI names romanised or official English, originals kept
    include_partly: bool = True  # videos some of whose chapters are named
    include_unverified: bool = True  # short videos named only from their file


@dataclass
class Budget:
    remaining: int
    used: int = 0

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        self.used += 1
        return True


@dataclass
class Change:
    """What a run does to one video."""

    chapters: list[dict]
    origin: str | None  # chapter_origin afterwards; None for the file's own
    release_id: str | None = None


@dataclass
class Outcome:
    video_id: str
    name: str
    before: naming.Status
    after: naming.Status
    change: Change | None
    log: list[str]
    ai_used: int = 0
    error: str = ""


# --- the outside world ------------------------------------------------------------


class Services:
    """Everything identify() reaches outside itself for; tests pass their
    own. MusicBrainz answers are remembered for the run, since videos in
    one folder usually search for, and pick, the same box set."""

    def __init__(self, mb_format: str = "xml") -> None:
        self._format = mb_format
        self._searches: dict[str, list] = {}
        self._media: dict[str, list] = {}
        # (release, discs) -> the video it named this run: one show is one
        # video, so a second that fits the same discs only fits by chance.
        self.claimed: dict[tuple, str] = {}

    def search_releases(self, query: str) -> list[dict]:
        if query not in self._searches:
            self._searches[query] = musicbrainz.search_releases(query, limit=5, fmt=self._format)
        return self._searches[query]

    def search_recordings(self, query: str) -> list[dict]:
        return musicbrainz.search_recordings(query, limit=10, fmt=self._format)

    def release_media(self, release_id: str) -> list[dict]:
        if release_id not in self._media:
            self._media[release_id] = musicbrainz.get_release_media(release_id, fmt=self._format)
        return self._media[release_id]

    def better_queries(self, video, context, tried, found, settings):
        return ai_musicbrainz.better_queries(video, context, tried, found, settings)

    def choose_release(self, video, context, candidates, settings):
        return ai_musicbrainz.choose_release(video, context, candidates, settings)

    def levels(self, video, cancel):
        return audio_levels.cached_levels(
            video["path"], library.file_fingerprint(video["path"]),
            duration=video.get("duration"), cancel=cancel,
        )

    def read_menu(self, video, cancel):
        return menu_chapters.read(video, cancel=cancel)

    def ask_menu(self, video, disc, settings, context, translate):
        return menu_chapters.ask(video, disc, settings, context, translate)

    def look(self, situation, settings, cancel):
        return ai_chapters.run(situation, settings, cancel=cancel)

    def translate(self, titles, settings):
        return ai_chapters.translate_titles(titles, settings)


# --- one video ----------------------------------------------------------------------


@dataclass
class _Work:
    video: dict
    chapters: list[dict]
    origin: str | None
    release_id: str | None = None
    levels: object = None
    # The tracklist of the release this run named it from, for the AI.
    tracks: list = field(default_factory=list)
    changed: bool = False
    # Chapters this run has named, and from where: provisional, so the
    # disc's own menu, read later in the same run, may put them right.
    given: dict = field(default_factory=dict)

    def status(self) -> naming.Status:
        return naming.status(dict(self.video, chapters=self.chapters), marked=False)

    def can_resplit(self) -> bool:
        """Whether its chapters may be placed afresh: one piece, or only an
        estimate nobody has named."""
        state = self.status()
        return state.state == naming.UNSPLIT or (
            self.origin == library.ORIGIN_ESTIMATED and state.named == 0
        )

    def replace(self, chapters: list[dict], origin: str) -> None:
        self.chapters, self.origin, self.changed = chapters, origin, True

    def fill(self, names, authoritative: bool = False) -> int:
        """Name the chapters that have no real name yet, from
        (index, title, original, source); returns how many.

        `authoritative` names - read off the disc's own menu - may also
        replace names this run gave (MusicBrainz's, the AI's), and a name
        this run gave that the menu puts on another chapter is taken back:
        it was on the wrong one. Names the video had before are kept always.
        """
        filled = 0
        for index, title, original, source in names:
            if not 0 <= index < len(self.chapters) or not title:
                continue
            chapter = self.chapters[index]
            replaceable = authoritative and index in self.given
            if naming.is_named(chapter) and not replaceable:
                continue
            if chapter.get("title") != title:
                filled += 1
            utils.set_chapter_title(chapter, title, source, original_title=original)
            self.given[index] = source
        if authoritative:
            placed = {_plain(t) for i, t, _o, _s in names if 0 <= i < len(self.chapters) and t}
            ours = {i for i, _t, _o, _s in names}
            for index in list(self.given):
                chapter = self.chapters[index]
                if index not in ours and _plain(chapter.get("title")) in placed:
                    utils.set_chapter_title(chapter, None)
                    del self.given[index]
        self.changed = self.changed or bool(filled)
        return filled


@dataclass
class _Fit:
    """A release whose tracks add up to the video."""

    release: dict
    picked: list[int]  # the discs that add up
    tracks: list[dict]
    media_count: int
    error: float  # seconds off the video's length
    recognised: bool  # artist and title in the file's name or folders

    def discs(self) -> str:
        if self.media_count == 1:
            return "The release"
        if len(self.picked) == 1:
            return f"Disc {self.picked[0] + 1} of {self.media_count}"
        return f"Discs {self.picked[0] + 1}-{self.picked[-1] + 1} of {self.media_count}"


def _plain(text: str | None) -> str:
    """Lower case, letters and digits only: "Gimme Chocolate!!" and "gimme
    chocolate" alike."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text or "").casefold())


def artist_matches(release: dict, video: dict, library_root=None) -> bool:
    """Whether the release is by an artist the video's name or folders
    mention. Without it, a length that happens to match lets in anyone's
    album - an indie "Red Fox" for BABYMETAL's Red Fox Festival."""
    path = PurePath(video["path"])
    parts = [video.get("display_name", "")] + list(path.parts[-4:])
    if library_root:
        try:
            parts = [video.get("display_name", "")] + list(
                path.relative_to(PurePath(library_root)).parts
            )
        except ValueError:
            pass
    context = _plain(" ".join(parts))
    names = re.split(r"\s*(?:&|,|/| feat\.? | x | and )\s*", release.get("artist") or "",
                     flags=re.IGNORECASE)
    return any(len(_plain(name)) >= 2 and _plain(name) in context for name in names)


# Words a release's title and a video's name share without saying they're
# the same show.
_GENERIC = {
    "live", "at", "in", "the", "of", "on", "and", "a", "an", "tour", "world", "concert",
    "blu", "ray", "bluray", "dvd", "bd", "bdrip", "bdmv", "edition", "disc", "full",
    "complete", "proshot", "remastered", "hq", "1080p", "2160p", "4k", "x264", "flac",
}


def _words(text: str | None) -> set[str]:
    return {
        w for w in re.findall(r"\w+", unicodedata.normalize("NFKC", text or "").casefold())
        if w not in _GENERIC and (len(w) >= 3 or w.isdigit())
    }


def title_matches(release: dict, video: dict, library_root=None) -> bool:
    """Whether the release's title, less its artist's name, shares a word
    with the video's name or folders. The artist's self-titled studio album
    has the same songs as a concert and can have its length too, but the
    songs in another order: "BABYMETAL" has nothing left to share with
    "Apocrypha The Black Mass", where "LIVE AT WEMBLEY" shares "wembley"."""
    title = _words(release.get("title")) - _words(release.get("artist"))
    path = PurePath(video["path"])
    try:
        parts = path.relative_to(PurePath(library_root)).parts if library_root else path.parts
    except ValueError:
        parts = path.parts[-4:]
    context = _words(" ".join([video.get("display_name", ""), *parts]))
    return bool(title & context)


def agrees_with_names(tracks: list[dict], chapters: list[dict]) -> bool:
    """Whether a tracklist has the songs the video's chapters are already
    named - most of them - so it's the same show. A video with fewer than
    two real names has nothing to disagree with."""
    known = [_plain(c["title"]) for c in chapters if naming.is_named(c)]
    known = [k for k in known if k]
    if len(known) < 2:
        return True
    titles = [_plain(t.get("title")) for t in tracks]

    def listed(name):
        return any(
            name == title or (len(name) > 3 and (name in title or title in name))
            or SequenceMatcher(None, name, title).ratio() >= SAME_TITLE_RATIO
            for title in titles if title
        )

    return sum(1 for name in known if listed(name)) >= AGREEMENT_FRACTION * len(known)


def _has_foreign_script(title: str | None) -> bool:
    """Letters in a script other than Latin - Japanese, or the Cyrillic that
    mis-decoded Japanese punctuation turns into ("Doki Doki в")."""
    for ch in title or "":
        if ch.isalpha() and not unicodedata.name(ch, "").startswith("LATIN"):
            return True
    return False


class _Step:
    """The steps, each a method on this, sharing one video's state."""

    def __init__(self, work: _Work, options: Options, settings: dict, services: Services,
                 budget: Budget, library_root, cancel: threading.Event | None,
                 log: Callable[[str], None]) -> None:
        self.work, self.options, self.settings = work, options, settings
        self.services, self.budget, self.root = services, budget, library_root
        self.cancel, self.log = cancel, log
        self.ai_used = 0

    @property
    def video(self):
        return self.work.video

    def _check(self):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()

    def _ai(self) -> bool:
        if not ai.is_configured(self.settings):
            self.log(f"AI: {ai.not_ready(self.settings)}")
            return False
        if not self.budget.take():
            self.log("AI: this run's requests are used up")
            return False
        self.ai_used += 1
        return True

    # --- free

    def _recording(self) -> None:
        """A single song, named only after its file: checked against the
        recordings MusicBrainz has - by the video's artist, with a title the
        file's name contains, near enough its length."""
        video, work = self.video, self.work
        chapter = work.chapters[0]
        guess = chapter.get("title") or utils.name_from_filename(video["display_name"])
        try:
            found = self.services.search_recordings(guess)
        except musicbrainz.MusicBrainzError as exc:
            self.log(f"MusicBrainz: {exc}")
            return
        plain_guess = _plain(guess)
        best = None
        for recording in found:
            title = _plain(recording.get("title"))
            if len(title) < 3 or title not in plain_guess:
                continue
            if not artist_matches(recording, video, self.root):
                continue
            length = recording.get("length")
            if length and abs(length - video["duration"]) > max(
                RECORDING_SECONDS, RECORDING_FRACTION * video["duration"]
            ):
                continue
            # The longest title the file's name contains is the song; a
            # shorter one inside it ("Karate" in "Karate (Live)") is a guess.
            key = (len(title), -abs((length or video["duration"]) - video["duration"]))
            if best is None or key > best[0]:
                best = (key, recording)
        if best is None:
            self.log(f"MusicBrainz: no recording by this artist matches “{guess}”")
            return
        recording = best[1]
        utils.set_chapter_title(chapter, recording["title"], "musicbrainz")
        work.given[0] = "musicbrainz"
        work.changed = True
        self.log(f"MusicBrainz: it's “{recording['title']}” by {recording['artist']}")

    def _ai_helps_search(self) -> bool:
        """Whether the AI may put a MusicBrainz search right and choose
        between its releases: when it's to look at the video too, so has
        been paid for, and there's a request left for it."""
        return (AI_LOOK in self.options.methods and ai.is_configured(self.settings)
                and self.budget.remaining > 0)

    def _fits(self, query: str, seen: dict, turned_down: list[str]) -> list[_Fit]:
        """The releases a search finds whose tracks add up to the video.
        Without the AI to judge, only those whose artist and title the
        file's name or folders mention are looked at: each is a request.
        `seen` (id -> release) has the releases already looked at, and
        gains these."""
        video, duration = self.video, self.video["duration"]
        try:
            releases = self.services.search_releases(query)[:RELEASES_TRIED]
        except musicbrainz.MusicBrainzError as exc:
            self.log(f"MusicBrainz: {exc}")
            return []
        judged = self._ai_helps_search()
        fits = []
        # The ones the names recognise first: when one of those fits, the
        # rest aren't worth a request each (two seconds, at MusicBrainz).
        ranked = sorted(releases, key=lambda r: not (
            artist_matches(r, video, self.root) and title_matches(r, video, self.root)
        ))
        for release in ranked:
            self._check()
            if release["id"] in seen:
                continue
            seen[release["id"]] = release
            recognised = (artist_matches(release, video, self.root)
                          and title_matches(release, video, self.root))
            if not recognised and (not judged or any(f.recognised for f in fits)):
                continue
            try:
                media = self.services.release_media(release["id"])
            except musicbrainz.MusicBrainzError:
                continue
            picked = chaptergen.pick_media(media, duration)
            tracks = [t for i in picked for t in media[i]["tracks"]]
            lengths = [t["length"] for t in tracks]
            if not tracks or not adds_up(lengths, duration):
                continue
            claimed = getattr(self.services, "claimed", {})
            owner = claimed.get((release["id"], tuple(picked)))
            if owner is not None and owner != video["display_name"]:
                turned_down.append(f"{release['title']}” already named “{owner}")
                continue
            if not agrees_with_names(tracks, self.work.chapters):
                turned_down.append(release["title"])
                continue
            fits.append(_Fit(release, picked, tracks, len(media),
                             abs(sum(lengths) - duration), recognised))
        return fits

    def _choose(self, fits: list[_Fit]) -> _Fit | None:
        """The AI's choice of the releases that add up, if it's sure
        enough of one."""
        if not fits or not self._ai():
            return None
        candidates = [ai_musicbrainz.Candidate(f.release, f.tracks, f.discs(), f.recognised)
                      for f in fits]
        try:
            choice = self.services.choose_release(
                dict(self.video, chapters=self.work.chapters),
                ai_chapters.situation_context(self.video, self.root),
                candidates, self.settings,
            )
        except ai.AIError as exc:
            self.log(f"MusicBrainz: the AI couldn't choose ({exc})")
            return None
        names = ", ".join(f"“{f.release['title']}”" for f in fits)
        if choice.release_id is None:
            self.log(f"MusicBrainz: the AI says none of {names} is this video"
                     + (f" ({choice.reason})" if choice.reason else ""))
            return None
        if self.options.only_sure and choice.confidence not in SURE:
            self.log(f"MusicBrainz: the AI was only guessing at {names}, so none is used")
            return None
        fit = next(f for f in fits if f.release["id"] == choice.release_id)
        guess = ", a guess" if choice.confidence not in SURE else ""
        self.log(f"MusicBrainz: the AI chose “{fit.release['title']}”{guess}"
                 + (f" - {choice.reason}" if choice.reason else ""))
        return fit

    def _better_queries(self, tried: list[str], seen: dict) -> list[str]:
        if not self._ai():
            return []
        try:
            what, queries = self.services.better_queries(
                dict(self.video, chapters=self.work.chapters),
                ai_chapters.situation_context(self.video, self.root),
                tried, list(seen.values()), self.settings,
            )
        except ai.AIError as exc:
            self.log(f"MusicBrainz: the AI couldn't suggest a search ({exc})")
            return []
        if queries:
            self.log("MusicBrainz: the AI"
                     + (f" thinks it's {what}, and" if what else "")
                     + " searched " + ", ".join(f"“{q}”" for q in queries))
        else:
            self.log("MusicBrainz: the AI had no better search")
        return queries

    def _release(self) -> tuple[_Fit | None, str, list[str]]:
        """The release that is this video, the searches made for it, and
        any that added up but were turned down. With the AI, it puts the
        search right when the file's name finds nothing, and chooses which
        release is this show; a release must add up to the video either way."""
        query = utils.suggest_search_query(self.video, self.root)
        tried, seen, turned_down = [query], {}, []
        fits = self._fits(query, seen, turned_down)
        recognised = [f for f in fits if f.recognised]
        # Free and certain: the one release the rules recognise.
        if len(recognised) == 1 and len(fits) == 1:
            return recognised[0], query, turned_down
        if self._ai_helps_search():
            chosen = self._choose(fits)
            if chosen is not None:
                return chosen, query, turned_down
            more = []
            for better in self._better_queries(tried, seen):
                tried.append(better)
                more += self._fits(better, seen, turned_down)
            if len(more) == 1 and more[0].recognised:
                chosen = more[0]
            else:
                chosen = self._choose(more)
            return chosen, "”, “".join(tried), turned_down
        best = min(recognised, key=lambda f: f.error, default=None)
        return best, query, turned_down

    def musicbrainz(self) -> None:
        video, work = self.video, self.work
        if work.status().state == naming.UNVERIFIED:
            self._recording()
            return
        duration = video["duration"]
        best, query, turned_down = self._release()
        if best is None:
            why = ""
            if turned_down:
                why = (f"; “{turned_down[0]}”" if "already named" in turned_down[0]
                       else f"; “{turned_down[0]}” adds up but its songs aren't this video's")
            self.log("MusicBrainz: no release of this show whose tracks add up to this video"
                     f" (searched “{query}”){why}")
            return
        release, tracks = best.release, best.tracks
        work.tracks = tracks
        claimed = getattr(self.services, "claimed", None)
        if claimed is not None:
            claimed[(release["id"], tuple(best.picked))] = video["display_name"]
        year = f" ({release['date'][:4]})" if release.get("date") else ""
        label = f"“{release['title']}”{year}"
        if work.can_resplit():
            chapters = chaptergen.chapters_from_tracks(tracks, duration, "musicbrainz")
            if video.get("type") == "file" and AUDIO in self.options.methods:
                work.levels = work.levels or self._levels()
                if work.levels is not None:
                    chapters = chaptergen.split_intros(chapters, work.levels)
            work.replace(chapters, library.ORIGIN_TRACKLIST)
            self.log(f"MusicBrainz: placed {len(chapters)} chapters by the lengths of {label}")
        else:
            built = proposal.build(work.chapters, tracks)
            names = [(row.chapter_index, row.title, None, "musicbrainz") for row in built.rows
                     if row.chapter_index is not None and row.title
                     and row.flag != proposal.FLAG_POSITION]
            filled = work.fill(names)
            self.log(f"MusicBrainz: named {filled} chapter(s) from {label}")
            if not filled:
                return
        work.release_id = release["id"]
        work.changed = True

    def _levels(self):
        try:
            return self.services.levels(self.video, self.cancel)
        except audio_levels.Cancelled:
            raise Cancelled() from None
        except Exception as exc:
            self.log(f"Audio: couldn't be measured ({exc})")
            return None

    def audio(self) -> None:
        work = self.work
        # Only a video in one piece: estimating an estimate again gains
        # nothing, and would throw away any boundary someone had moved.
        if self.video.get("type") != "file" or work.status().state != naming.UNSPLIT:
            return
        work.levels = work.levels or self._levels()
        if work.levels is None:
            return
        starts = chaptergen.estimate_starts(work.levels, self.video["duration"])
        if len(starts) < 2:
            self.log("Audio: no gaps between songs found")
            return
        work.replace(chaptergen.chapters_from_starts(starts, self.video["duration"],
                                                     estimated=True),
                     library.ORIGIN_ESTIMATED)
        self.log(f"Audio: split into {len(starts)} chapters where the music stops")

    # --- costs money

    def menu(self) -> None:
        work = self.work
        if self.video.get("type") != "bluray" or len(work.chapters) < 2:
            return
        try:
            disc = self.services.read_menu(dict(self.video, chapters=work.chapters), self.cancel)
        except menu_chapters.Cancelled:
            raise Cancelled() from None
        except menu_chapters.MenuError as exc:
            self.log(f"Disc menu: {exc}")
            return
        if not self._ai():
            return
        try:
            result = self.services.ask_menu(
                dict(self.video, chapters=work.chapters), disc, self.settings,
                ai_chapters.situation_context(self.video, self.root), self.options.translate,
            )
        except ai.AIError as exc:
            self.log(f"Disc menu: the AI couldn't read it ({exc})")
            return
        confidence = {row.chapter - 1: row.confidence for row in result.rows}
        read = [entry for entry in result.mapping() if entry[3] == menu_chapters.SOURCE]
        judged = [
            entry for entry in result.mapping()
            if entry[3] != menu_chapters.SOURCE
            and (not self.options.only_sure or confidence.get(entry[0]) in SURE)
        ]
        # The menu's own names first, and over this run's guesses; then
        # the AI's view of the chapters the menu doesn't list.
        filled = work.fill(read, authoritative=True) + work.fill(judged)
        self.log(f"Disc menu: named {filled} chapter(s), {len(read)} read off the menu")

    def ai_look(self) -> None:
        work = self.work
        if work.can_resplit() and work.status().state == naming.UNSPLIT:
            self.log("AI: a video in one piece needs splitting first (Detect chapters)")
            return
        estimated = work.origin == library.ORIGIN_ESTIMATED and work.can_resplit()
        if estimated and work.levels is None and self.video.get("type") == "file":
            work.levels = self._levels()
        if not self._ai():
            return
        video = dict(self.video, chapters=work.chapters)
        situation = ai_chapters.Situation(
            video=video, chapters=[dict(c) for c in work.chapters],
            mode=ai_chapters.PLACE if estimated else ai_chapters.NAME,
            tracks=list(work.tracks),
            tracks_source="musicbrainz" if work.tracks else "",
            candidates=(chaptergen.boundary_candidates(work.levels, video["duration"])
                        if estimated and work.levels is not None else []),
            context=ai_chapters.situation_context(video, self.root),
            translate=self.options.translate,
            frames_per_chapter=self.settings.get(ai.SETTING_FRAMES, ai.DEFAULT_FRAMES_PER_CHAPTER),
            online=True,
        )
        try:
            result = self.services.look(situation, self.settings, self.cancel)
        except audio_levels.Cancelled:  # what the frame grabber raises
            raise Cancelled() from None
        except ai.AIError as exc:
            self.log(f"AI: {exc}")
            return
        sure = {row.chapter - 1 for row in result.rows
                if not self.options.only_sure or row.confidence in SURE}
        if situation.mode == ai_chapters.PLACE:
            chapters = [dict(c) for c in result.chapters]
            for i, chapter in enumerate(chapters):
                if i not in sure:
                    utils.set_chapter_title(chapter, None)
            work.replace(chapters, library.ORIGIN_ESTIMATED)
            named = sum(1 for c in chapters if naming.is_named(c))
            self.log(f"AI: placed {len(chapters)} chapters and named {named}")
        else:
            confidence = {row.chapter - 1: row.confidence for row in result.rows}
            # What it's certain of may put right this run's own names - an
            # opening film MusicBrainz's lengths took for the first song;
            # names the video had before are kept whatever it says.
            certain = [entry for entry in result.mapping() if confidence.get(entry[0]) == "high"]
            names = [entry for entry in result.mapping()
                     if entry[0] in sure and confidence.get(entry[0]) != "high"]
            filled = work.fill(certain, authoritative=True) + work.fill(names)
            skipped = sum(1 for entry in result.mapping() if entry[0] not in sure)
            self.log(f"AI: named {filled} chapter(s)"
                     + (f", left {skipped} it was guessing at" if skipped else ""))

    def translate(self) -> None:
        work = self.work
        foreign = [(i, c["title"]) for i, c in enumerate(work.chapters)
                   if _has_foreign_script(c.get("title")) and not c.get("original_title")]
        if not foreign or not self._ai():
            return
        try:
            answers = self.services.translate([title for _, title in foreign], self.settings)
        except ai.AIError as exc:
            self.log(f"Romanise: {exc}")
            return
        changed = 0
        for (index, current), (title, original) in zip(foreign, answers, strict=False):
            if title and title != current:
                chapter = work.chapters[index]
                utils.set_chapter_title(chapter, title, chapter.get("source") or "ai",
                                        original_title=original or current)
                changed += 1
        work.changed = work.changed or bool(changed)
        self.log(f"Romanise: {changed} title(s) given as they're known in English")


def plan(options: Options) -> list[str]:
    return [m for m in ORDER.get(options.ai_policy, ORDER[LAST_RESORT]) if m in options.methods]


def identify(video_id: str, video: dict, options: Options, settings: dict,
             services: Services, budget: Budget, library_root=None,
             cancel: threading.Event | None = None, private: bool = False) -> Outcome:
    """Try each chosen method in turn until the video is identified, and
    say what would change. Nothing is applied here. A `private` video gets
    only the methods that send nothing out."""
    before = naming.status(video)
    work = _Work(video, copy.deepcopy(video["chapters"]), video.get("chapter_origin"))
    lines: list[str] = []
    step = _Step(work, options, settings, services, budget, library_root, cancel, lines.append)
    methods = plan(options)
    if private:
        methods = [m for m in methods if m in LOCAL_METHODS]
        lines.append("Private: MusicBrainz and the AI aren't used for it.")
    for method in methods:
        step._check()
        if method != TRANSLATE and work.status().state == naming.NAMED:
            continue
        if method == TRANSLATE and not options.translate:
            continue
        getattr(step, method)()
    after = work.status()
    change = Change(work.chapters, work.origin, work.release_id) if work.changed else None
    return Outcome(video_id, video["display_name"], before, after, change, lines, step.ai_used)


# --- a library ---------------------------------------------------------------------


def candidates(videos, options: Options, skip=()) -> list[tuple[str, dict]]:
    """The videos a run would work on, in shelf order: those still needing
    it, less any in `skip` (hidden, missing)."""
    chosen = []
    for video_id, video in videos:
        if video_id in skip:
            continue
        status = naming.status(video)
        if not status.needs_work:
            continue
        if status.state == naming.PARTLY and not options.include_partly:
            continue
        if status.state == naming.UNVERIFIED and not options.include_unverified:
            continue
        chosen.append((video_id, video))
    return chosen


def run(videos, options: Options, settings: dict, services: Services | None = None,
        library_root=None, cancel: threading.Event | None = None,
        on_started: Callable[[int, int, str, str], None] | None = None,
        on_outcome: Callable[[Outcome], None] | None = None,
        private=()) -> Budget:
    """Identify each of `videos` ((id, video) pairs) in turn, reporting as it
    goes; returns the AI budget as it was left. Setting `cancel` stops it
    between videos, or during a long step. Videos whose ids are in
    `private` get only local methods. `library_root` may be a function of
    a video's id, for a shelf of several libraries."""
    services = services or Services()
    budget = Budget(options.ai_budget if set(options.methods) & set(AI_METHODS) else 0)
    videos = list(videos)
    for n, (video_id, video) in enumerate(videos):
        if cancel is not None and cancel.is_set():
            break
        if on_started:
            on_started(n, len(videos), video_id, video["display_name"])
        try:
            root = library_root(video_id) if callable(library_root) else library_root
            outcome = identify(video_id, video, options, settings, services, budget,
                               root, cancel, private=video_id in private)
        except Cancelled:
            break
        except Exception as exc:  # one bad video mustn't end the run
            status = naming.status(video)
            outcome = Outcome(video_id, video["display_name"], status, status, None, [],
                              error=str(exc) or repr(exc))
        if on_outcome:
            on_outcome(outcome)
    return budget


# --- undoing a run -----------------------------------------------------------------

_KEPT = ("chapters", "chapter_origin", "musicbrainz_release_id")


def start_journal(data: dict) -> str:
    """Begin a new run's journal in the library, replacing the last one:
    only the latest run can be undone."""
    run_id = time.strftime("%Y-%m-%d %H:%M:%S")
    data["settings"][JOURNAL] = {"started": run_id, "before": {}}
    return run_id


def remember(data: dict, video_id: str, video: dict) -> None:
    """Keep a video's state from before the run changed it - once, so a
    video changed twice goes back to how it was before either."""
    journal = data["settings"].get(JOURNAL)
    if journal is None or video_id in journal["before"]:
        return
    journal["before"][video_id] = {
        key: copy.deepcopy(video[key]) for key in _KEPT if key in video
    }


def apply(data: dict, video_id: str, change: Change, journal: bool = True) -> dict | None:
    """Write a run's change into the stored library, journalled first
    (unless `journal` is off: a change the person looked at and applied
    isn't a run's to undo)."""
    video = data["videos"].get(video_id)
    if video is None:
        return None
    if journal:
        remember(data, video_id, video)
    video["chapters"] = change.chapters
    if change.origin:
        video["chapter_origin"] = change.origin
    else:
        video.pop("chapter_origin", None)
    if change.release_id:
        video["musicbrainz_release_id"] = change.release_id
    return video


def last_run(data: dict) -> dict | None:
    journal = data["settings"].get(JOURNAL)
    return journal if journal and journal.get("before") else None


def undo(data: dict) -> list[str]:
    """Put back every video the last run changed, as it was; returns their
    ids. A video that has gone since is skipped."""
    journal = data["settings"].pop(JOURNAL, None)
    restored = []
    for video_id, before in (journal or {}).get("before", {}).items():
        video = data["videos"].get(video_id)
        if video is None:
            continue
        for key in _KEPT:
            if key in before:
                video[key] = before[key]
            else:
                video.pop(key, None)
        restored.append(video_id)
    return restored
