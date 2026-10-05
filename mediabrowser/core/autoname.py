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

# Each step as the report names it.
STEP_NAMES = {
    MUSICBRAINZ: "MusicBrainz", AUDIO: "The audio", MENU: "The disc's menu",
    AI_LOOK: "The AI looks at the video", TRANSLATE: "Titles in English",
}

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
class Hints:
    """What the person watching knows about one video, each part optional:
    what it's known as, how many songs it has, and its setlist in order.

    What it's known as is searched on MusicBrainz first. A release must
    have that many songs, and most of the setlist's; a video in one piece
    is split into that many chapters; the AI is told all of it; and a
    setlist with one song per chapter names them when nothing else does.
    """

    known_as: str = ""
    songs: int = 0
    setlist: list[dict] = field(default_factory=list)  # [{"title", "length"}], in order

    def __bool__(self) -> bool:
        return bool(self.known_as.strip() or self.songs or self.setlist)

    def song_count(self) -> int:
        return self.songs or len(self.setlist)

    def titles(self) -> list[str]:
        return [track["title"] for track in self.setlist if track.get("title")]

    def describe(self) -> str:
        """What the person said, for the AI."""
        said = []
        if self.known_as.strip():
            said.append(f"it is known as “{self.known_as.strip()}”")
        if self.song_count():
            said.append(f"it has {self.song_count()} songs (an opening, a video interlude, "
                        "a solo or the broadcaster's own segments aren't songs, and may "
                        "have chapters of their own)")
        if self.setlist:
            said.append("its songs in order are: " + "; ".join(self.titles()))
        if not said:
            return ""
        return ("The person watching it says " + ", and ".join(said)
                + ". Trust that over the file's name.")


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
    # The log at length: each step, what it looked at and why it decided
    # as it did, for the person to read afterwards.
    report: list[str] = field(default_factory=list)

    def report_text(self) -> str:
        return "\n".join(self.report or self.log)


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


def artist_matches(release: dict, video: dict, library_root=None, extra: str = "") -> bool:
    """Whether the release is by an artist the video's name or folders (or
    `extra`, what the person said it's known as) mention. Without it, a
    length that happens to match lets in anyone's album - an indie "Red
    Fox" for BABYMETAL's Red Fox Festival."""
    path = PurePath(video["path"])
    parts = [video.get("display_name", "")] + list(path.parts[-4:])
    if library_root:
        try:
            parts = [video.get("display_name", "")] + list(
                path.relative_to(PurePath(library_root)).parts
            )
        except ValueError:
            pass
    context = _plain(" ".join([*parts, extra]))
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


def _context(video: dict, library_root=None, extra: str = "") -> str:
    """The video's name and the folders it's in, below the library root,
    and anything the person said it's known as."""
    path = PurePath(video["path"])
    try:
        parts = path.relative_to(PurePath(library_root)).parts if library_root else path.parts
    except ValueError:
        parts = path.parts[-4:]
    return " ".join([video.get("display_name", ""), *parts, extra])


def title_matches(release: dict, video: dict, library_root=None, extra: str = "") -> bool:
    """Whether the release's title, less its artist's name, shares a word
    with the video's name or folders (or `extra`, what the person said it's
    known as). The artist's self-titled studio album has the same songs as
    a concert and can have its length too, but the songs in another order:
    "BABYMETAL" has nothing left to share with "Apocrypha The Black Mass",
    where "LIVE AT WEMBLEY" shares "wembley"."""
    title = _words(release.get("title")) - _words(release.get("artist"))
    return bool(title & _words(_context(video, library_root, extra)))


# A year, alone or the start of an eight-digit date ("20260419"). Not a
# resolution: 1920x1080 has no year in it.
_YEAR_RE = re.compile(
    r"(?<!\d)((?:19[5-9]|20\d)\d)(?:(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))?(?!\d)"
)


def _years(text: str | None) -> list[int]:
    return [int(y) for y in _YEAR_RE.findall(unicodedata.normalize("NFKC", text or ""))]


def predates(release: dict, video: dict, library_root=None, extra: str = "") -> bool:
    """Whether the release came out before every year the video's name and
    folders mention, so can't be a recording of it. A show is named for
    its tour's years or the day it was played or broadcast, and a release
    of it comes after: BABYMETAL's LEGEND - MM (2024) isn't the broadcast
    "20260419 ... WORLD TOUR 2025-2026 ... LEGEND - METAL FORTH", though the
    lengths of its first two discs add up to it within 20 seconds and both
    are LEGENDs. A file named for when its disc came out mentions that
    year, so it never predates its own release."""
    came_out = _years((release.get("date") or "")[:4]) or _years(release.get("title"))
    mentioned = _years(_context(video, library_root, extra))
    return bool(came_out and mentioned) and max(came_out) < min(mentioned)


def agrees_with_names(tracks: list[dict], chapters: list[dict]) -> bool:
    """Whether a tracklist has the songs the video's chapters are already
    named - most of them - so it's the same show. A video with fewer than
    two real names has nothing to disagree with."""
    return agrees_with(tracks, [c["title"] for c in chapters if naming.is_named(c)])


def agrees_with(tracks: list[dict], names: list[str]) -> bool:
    """Whether a tracklist has most of these songs. Fewer than two have
    nothing to disagree with."""
    known = [k for k in (_plain(name) for name in names) if k]
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


# A track that isn't one of a show's songs, by its title.
_NOT_A_SONG = re.compile(
    r"^\W*(?:intro|outro|opening|overture|prologue|epilogue|interlude|ending|encore"
    r"|mc|se|end ?roll|credits)\b",
    re.IGNORECASE,
)


def has_songs(tracks: list[dict], count: int) -> bool:
    """Whether a tracklist has `count` songs: that many tracks, or that many
    once its openings, interludes and endings are left out."""
    songs = [t for t in tracks if not _NOT_A_SONG.match(t.get("title") or "")]
    return count in (len(tracks), len(songs))


def _setlist_position(title: str | None, setlist: list[str]) -> int | None:
    """Where on the setlist a chapter's title is, if it's one of its songs -
    "Headbangeeeeerrrrr!!!!! (cont.)" being Headbangeeeeerrrrr!!!!! still."""
    plain = _plain(title)
    if not plain:
        return None
    for i, song in enumerate(setlist):
        name = _plain(song)
        if name and (plain == name or (len(name) > 3 and plain.startswith(name))):
            return i
    return None


def out_of_order(chapters: list[dict], setlist: list[str]) -> list[int]:
    """The chapters whose songs break the setlist's order, or repeat one:
    all but the longest run that follows it. A setlist the person gave is
    the order the songs were played in."""
    placed = [(i, pos) for i, c in enumerate(chapters)
              if (pos := _setlist_position(c.get("title"), setlist)) is not None]
    # Longest strictly increasing run of setlist positions.
    best: list[list[tuple[int, int]]] = []
    for item in placed:
        longest = max((run for run in best if run[-1][1] < item[1]), key=len, default=[])
        best.append(longest + [item])
    keep = {i for i, _pos in max(best, key=len, default=[])}
    return [i for i, _pos in placed if i not in keep]


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
                 log: Callable[[str], None], hints: Hints | None = None,
                 note: Callable[[str], None] | None = None) -> None:
        self.work, self.options, self.settings = work, options, settings
        self.services, self.budget, self.root = services, budget, library_root
        self.cancel, self.log = cancel, log
        # For the report only: the detail behind what's logged.
        self.note = note or (lambda _line: None)
        self.hints = hints or Hints()
        self.ai_used = 0

    @property
    def video(self):
        return self.work.video

    def _ai_video(self) -> dict:
        """The video as the AI is shown it: its chapters as they are now,
        and what the person said about it."""
        video = dict(self.video, chapters=self.work.chapters)
        if self.hints:
            video["hints"] = self.hints.describe()
        return video

    def _why_recognised(self, release: dict) -> str:
        """Whether the names recognise the release, and on what, in words."""
        video, said = self.video, self.hints.known_as
        artist = artist_matches(release, video, self.root, said)
        title = title_matches(release, video, self.root, said)
        early = predates(release, video, self.root, said)
        if artist and title and not early:
            return "recognised (its artist and title are in the file's name or folders)"
        why = []
        if not artist:
            why.append("its artist isn't in the file's name or folders")
        if not title:
            why.append("no word of its title is")
        if early:
            why.append("it came out before the years the file's name mentions")
        return "not recognised - " + ", and ".join(why)

    def _recognises(self, release: dict) -> bool:
        """Whether the video's name and folders, and what the person said it's
        known as, name the release's artist and title - and don't date the
        video after the release came out."""
        video, said = self.video, self.hints.known_as
        return (artist_matches(release, video, self.root, said)
                and title_matches(release, video, self.root, said)
                and not predates(release, video, self.root, said))

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
        self.note(f"  (AI request {self.ai_used}; {self.budget.remaining} left for this run)")
        return True

    def _note_ai_result(self, result) -> None:
        """What the model said, chapter by chapter, for the report."""
        if result.show:
            self.note(f"  The AI says the video is: {result.show}")
        if result.setlist:
            self.note("  The setlist it worked from: " + "; ".join(result.setlist))
        if result.frames_sent:
            self.note(f"  Frames it was shown: {result.frames_sent} ({result.model})")
        for row in result.rows:
            line = (f"    {row.chapter:>2}. {utils.format_seconds(row.start):>8}  "
                    f"{row.title or '(no name)'}  [{row.confidence}]")
            if row.moved:
                line += " (start moved)"
            self.note(line)
            if row.seen:
                self.note(f"          seen: {row.seen}")
            if row.note:
                self.note(f"          note: {row.note}")
        if result.notes:
            self.note(f"  The AI's notes: {result.notes}")

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
        self.note(f"  Searched MusicBrainz for “{query}”: {len(releases)} release(s) looked at")
        judged = self._ai_helps_search()
        fits = []
        # The ones the names recognise first: when one of those fits, the
        # rest aren't worth a request each (two seconds, at MusicBrainz).
        ranked = sorted(releases, key=lambda r: not self._recognises(r))
        count, setlist = self.hints.song_count(), self.hints.titles()
        for release in ranked:
            self._check()
            if release["id"] in seen:
                continue
            seen[release["id"]] = release
            recognised = self._recognises(release)
            self.note(f"  · {ai_musicbrainz.describe_release(release)}: "
                      + self._why_recognised(release))
            if not recognised and (not judged or any(f.recognised for f in fits)):
                self.note("      not fetched: the file's name doesn't recognise it"
                          + (", and one it does already fits" if judged else
                             ", and there's no AI to judge it"))
                continue
            try:
                media = self.services.release_media(release["id"])
            except musicbrainz.MusicBrainzError:
                continue
            picked = chaptergen.pick_media(media, duration)
            tracks = [t for i in picked for t in media[i]["tracks"]]
            lengths = [t["length"] for t in tracks]
            if not tracks or not adds_up(lengths, duration):
                total = sum(length or 0 for length in lengths)
                self.note(f"      doesn't add up: its closest discs come to "
                          f"{utils.format_seconds(total)} against the video's "
                          f"{utils.format_seconds(duration)}"
                          + (" (some tracks have no length)" if not all(lengths) else ""))
                continue
            self.note(f"      adds up: {len(tracks)} tracks on {len(picked)} of {len(media)} "
                      f"disc(s) come to {utils.format_seconds(sum(lengths))}, "
                      f"{abs(sum(lengths) - duration):.0f}s off the video")
            claimed = getattr(self.services, "claimed", {})
            owner = claimed.get((release["id"], tuple(picked)))
            if owner is not None and owner != video["display_name"]:
                turned_down.append(f"“{release['title']}” already named “{owner}”")
                self.note(f"      turned down: {turned_down[-1]}")
                continue
            if count and not has_songs(tracks, count):
                turned_down.append(f"“{release['title']}” adds up but has {len(tracks)} "
                                   f"tracks, not the {count} songs this video has")
                self.note(f"      turned down: {turned_down[-1]}")
                continue
            if not (agrees_with_names(tracks, self.work.chapters)
                    and agrees_with(tracks, setlist)):
                turned_down.append(f"“{release['title']}” adds up but its songs aren't "
                                   "this video's")
                self.note(f"      turned down: {turned_down[-1]} - its tracks: "
                          + "; ".join(t.get("title") or "?" for t in tracks))
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
                self._ai_video(),
                ai_chapters.situation_context(self.video, self.root),
                candidates, self.settings,
            )
        except ai.AIError as exc:
            self.log(f"MusicBrainz: the AI couldn't choose ({exc})")
            return None
        names = ", ".join(f"“{f.release['title']}”" for f in fits)
        self.note(f"  The AI was asked which of {names} is this video: "
                  f"{choice.confidence} confidence")
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
                self._ai_video(),
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

    def _queries(self) -> list[str]:
        """The searches to start from: what the person said it's known as,
        then the file's name and folders."""
        said = utils.search_words(self.hints.known_as)
        own = utils.suggest_search_query(self.video, self.root)
        return [query for query in dict.fromkeys([said, own]) if query]

    def _release(self) -> tuple[_Fit | None, str, list[str]]:
        """The release that is this video, the searches made for it, and
        why any that added up were turned down. With the AI, it puts the
        search right when the file's name finds nothing, and chooses which
        release is this show; a release must add up to the video either way."""
        tried, seen, turned_down, fits = [], {}, [], []
        for query in self._queries():
            tried.append(query)
            fits = self._fits(query, seen, turned_down)
            if fits:
                break
        query = "”, “".join(tried)
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
            why = f"; {turned_down[0]}" if turned_down else ""
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
        # Every clear gap the audio finds: a concert has more pieces than
        # songs (an opening film, interludes, a broadcaster's segments), so
        # making it exactly that many chapters puts their starts in the wrong
        # places. Only when it finds too few are the best that many taken.
        count = self.hints.song_count()
        starts = chaptergen.estimate_starts(work.levels, self.video["duration"])
        if count and len(starts) < count:
            starts = chaptergen.estimate_starts(work.levels, self.video["duration"],
                                                song_count=count)
        if len(starts) < 2:
            self.log("Audio: no gaps between songs found")
            return
        work.replace(chaptergen.chapters_from_starts(starts, self.video["duration"],
                                                     estimated=True),
                     library.ORIGIN_ESTIMATED)
        self.log(f"Audio: split into {len(starts)} chapters where the music stops"
                 + (f", for the {count} songs it has" if count else ""))
        found = chaptergen.boundary_candidates(work.levels, self.video["duration"])
        self.note(f"  {len(found)} quiet moment(s) measured; chapters start at "
                  + ", ".join(utils.format_seconds(start) for start in starts))

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
                self._ai_video(), disc, self.settings,
                ai_chapters.situation_context(self.video, self.root), self.options.translate,
            )
        except ai.AIError as exc:
            self.log(f"Disc menu: the AI couldn't read it ({exc})")
            return
        self._note_ai_result(result)
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
        video = self._ai_video()
        tracks = list(work.tracks) or [dict(t) for t in self.hints.setlist]
        situation = ai_chapters.Situation(
            video=video, chapters=[dict(c) for c in work.chapters],
            mode=ai_chapters.PLACE if estimated else ai_chapters.NAME,
            tracks=tracks,
            tracks_source=("musicbrainz" if work.tracks else "hint" if tracks else ""),
            candidates=(chaptergen.boundary_candidates(work.levels, video["duration"])
                        if estimated and work.levels is not None else []),
            context=ai_chapters.situation_context(video, self.root),
            translate=self.options.translate,
            frames_per_chapter=self.settings.get(ai.SETTING_FRAMES, ai.DEFAULT_FRAMES_PER_CHAPTER),
            online=True,
        )
        task = "place and name" if situation.mode == ai_chapters.PLACE else "name"
        self.note(f"  Asked to {task} {len(situation.chapters)} chapter(s)"
                  + (f", with the tracklist from {'MusicBrainz' if work.tracks else 'the hints'}"
                     f" ({len(tracks)} songs)" if tracks else ", with no tracklist")
                  + (f" and {len(situation.candidates)} quiet moments to choose starts from"
                     if situation.candidates else ""))
        try:
            result = self.services.look(situation, self.settings, self.cancel)
        except audio_levels.Cancelled:  # what the frame grabber raises
            raise Cancelled() from None
        except ai.AIError as exc:
            self.log(f"AI: {exc}")
            return
        self._note_ai_result(result)
        sure = {row.chapter - 1 for row in result.rows
                if not self.options.only_sure or row.confidence in SURE}
        wrong = out_of_order(result.chapters, self.hints.titles()) if self.hints.setlist else []
        if wrong:
            sure -= set(wrong)
            self.note("  Left unnamed, as out of the setlist's order or a repeat: "
                      + ", ".join(f"{i + 1}. {result.chapters[i]['title']}" for i in wrong))
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
            certain = [entry for entry in result.mapping()
                       if confidence.get(entry[0]) == "high" and entry[0] not in wrong]
            names = [entry for entry in result.mapping()
                     if entry[0] in sure and confidence.get(entry[0]) != "high"]
            filled = work.fill(certain, authoritative=True) + work.fill(names)
            skipped = sum(1 for entry in result.mapping() if entry[0] not in sure)
            self.log(f"AI: named {filled} chapter(s)"
                     + (f", left {skipped} it was guessing at" if skipped else ""))

    def setlist(self) -> None:
        """Name the chapters from the person's setlist, in its order, when
        nothing else named any and there's one chapter per song."""
        work, titles = self.work, self.hints.titles()
        if not titles or work.status().named or len(work.chapters) != len(titles):
            return
        filled = work.fill([(i, title, None, "manual") for i, title in enumerate(titles)])
        self.log(f"Setlist: named {filled} chapter(s) in the order given")

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
                self.note(f"  {current} → {title}")
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
             cancel: threading.Event | None = None, private: bool = False,
             hints: Hints | None = None) -> Outcome:
    """Try each chosen method in turn until the video is identified, and
    say what would change. Nothing is applied here. A `private` video gets
    only the methods that send nothing out. `hints` are what the person
    watching knows about it."""
    before = naming.status(video)
    work = _Work(video, copy.deepcopy(video["chapters"]), video.get("chapter_origin"))
    lines: list[str] = []
    report: list[str] = []

    def log(line: str) -> None:
        lines.append(line)
        report.append(f"  ⇒ {line}")  # what the step concluded

    step = _Step(work, options, settings, services, budget, library_root, cancel,
                 log, hints, report.append)
    methods = plan(options)
    report.append(f"{video['display_name']}")
    report.append(f"{utils.format_seconds(video['duration'])} long, "
                  f"{'a Blu-ray title' if video.get('type') == 'bluray' else 'a video file'}; "
                  f"{before.describe()} ({len(video['chapters'])} chapter(s))")
    if hints:
        report.append(hints.describe())
    if private:
        methods = [m for m in methods if m in LOCAL_METHODS]
        log("Private: MusicBrainz and the AI aren't used for it.")
    report.append("Steps, in order: " + ", ".join(STEP_NAMES[m] for m in methods)
                  + f". AI requests allowed: {budget.remaining}.")
    for method in methods:
        step._check()
        report.append("")
        report.append(f"— {STEP_NAMES[method]}")
        if method != TRANSLATE and work.status().state == naming.NAMED:
            report.append("  Skipped: every chapter is named already.")
            continue
        if method == TRANSLATE and not options.translate:
            report.append("  Skipped: not asked for.")
            continue
        before_step = len(report)
        getattr(step, method)()
        if len(report) == before_step:
            report.append("  Nothing to do for this video.")
    # Last: the person's own titles need no translating.
    step.setlist()
    after = work.status()
    change = Change(work.chapters, work.origin, work.release_id) if work.changed else None
    report.append("")
    report.append(f"Result: {before.describe()} → {after.describe()}"
                  + ("" if change else "; nothing changed")
                  + f". AI requests made: {step.ai_used}.")
    return Outcome(video_id, video["display_name"], before, after, change, lines,
                   step.ai_used, report=report)


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
