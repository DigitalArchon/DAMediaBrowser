# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Asking a model to look at a concert and say what its chapters are.

Everything the app has already worked out goes into one request - the
file and folder names, the chapter or candidate times, the tracklist if
there is one, the quiet stretches the audio found - together with a frame
or two from just after each start, where a Blu-ray shows the song's title.
The model does what none of the matching here can: read that caption,
recall the tour's setlist, tell an MC from a song, and put the title in
English.

Two modes:

- NAME: the chapters stay where they are (a disc's own marks, or ones a
  person has fixed) and each gets its title.
- PLACE: the chapters are estimates. The model may keep, move, add or drop
  starts, but a moved start is snapped to a measured candidate boundary
  when one is near, so the picture and the audio agree on it.

Nothing here talks to the network or ffmpeg itself: the frame grabber and
the chat function are passed in, which is what makes the request and the
reply testable without either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import PurePath

from . import ai, frames, utils

# What a title is put into, where the model is asked to: not a translation
# of what it means, but what the song is called for English speakers -
# which for most Japanese songs is its romanised title.
ENGLISH_TITLE = (
    "the title the song is officially released under in English-language markets "
    "(Western releases, streaming services). Where that release gives it a title of "
    "its own, use it (ギミチョコ!! is released as \"Gimme Chocolate!!\"); otherwise, "
    "as for most Japanese songs, a Hepburn romanisation of how the title is read "
    "(Iine!, Megitsune, Ijime, Dame, Zettai) - where the title spells out its own "
    "reading, that reading alone (紅月-アカツキ- is \"Akatsuki\"). Digits and Latin "
    "letters stay as written (4の歌 is \"4 no Uta\"). Never translate "
    "what a title means - Iine! is not \"So Good\""
)

NAME = "name"
PLACE = "place"

# Seconds after a start to take frames: a song caption usually appears a
# few seconds in, sometimes a dozen; later frames only show the stage.
FRAME_OFFSETS = (3.0, 12.0, 25.0, 45.0)
# Candidate boundaries beyond the proposed chapters are worth a look too -
# a song the estimate missed - but there can be dozens, so only the
# strongest go, with a single frame each.
MAX_EXTRA_CANDIDATES = 30
# A candidate this close to a proposed start is the same boundary.
SAME_BOUNDARY_SECONDS = 5.0
# How far a start the model gives may be from a known moment (a proposed
# start or a candidate) and still be taken as that moment.
SNAP_SECONDS = 2.0
# Two starts closer than this can't both be songs.
MIN_SONG_SECONDS = 30.0

CONFIDENCES = ("high", "medium", "low")
SOURCE = "ai"
# The sources a result names chapters with: the model's judgement, or text
# it read off the disc's own menu (menu_chapters).
NAMED_SOURCES = (SOURCE, "menu")


@dataclass(frozen=True)
class Situation:
    """Everything the model is told."""

    video: dict
    chapters: list[dict]  # as proposed or as they are: start, end, title
    mode: str  # NAME or PLACE
    tracks: list[dict] = field(default_factory=list)  # [{"title", "length"}]
    tracks_source: str = ""  # where the tracklist came from, for the model
    candidates: list[tuple[float, float]] = field(default_factory=list)  # (seconds, score)
    context: str = ""  # the folders above the file, below the library root
    translate: bool = True
    frames_per_chapter: int = ai.DEFAULT_FRAMES_PER_CHAPTER
    # Let the model search the web for the show's setlist.
    online: bool = True


@dataclass(frozen=True)
class Row:
    """One chapter of the answer, for showing."""

    chapter: int  # 1-based position in the answer
    start: float
    title: str | None
    original_title: str | None
    confidence: str
    note: str
    moved: bool  # PLACE mode: not one of the proposed starts


@dataclass(frozen=True)
class Result:
    mode: str
    chapters: list[dict]  # ready to store
    rows: list[Row]
    show: str
    notes: str
    frames_sent: int
    model: str
    # The setlist the model says it knows for this show, in order.
    setlist: list[str] = field(default_factory=list)

    def mapping(self) -> list[tuple[int, str, str | None, str]]:
        """NAME mode: (chapter_index, title, original_title, source) for each
        chapter named here - source "ai", or "menu" where the name was read
        off the disc's own menu."""
        return [
            (i, ch["title"], ch.get("original_title"), ch["source"])
            for i, ch in enumerate(self.chapters)
            if ch.get("source") in NAMED_SOURCES and ch["title"]
        ]

    @property
    def moved_any(self) -> bool:
        return any(row.moved for row in self.rows)


# --- what to look at ------------------------------------------------------------


def situation_context(video: dict, library_root=None) -> str:
    """The folders a file sits in, below the library root: usually the
    artist and the show."""
    path = PurePath(video["path"])
    folder = path.parent
    root = PurePath(library_root) if library_root else None
    parts = []
    for _ in range(3):
        if not folder.name or (root is not None and (
            folder == root or not folder.is_relative_to(root)
        )):
            break
        parts.insert(0, folder.name)
        folder = folder.parent
    return " / ".join(parts)


def _extra_candidates(situation: Situation) -> list[float]:
    """Candidate boundaries that aren't already a proposed start, strongest
    first, capped."""
    starts = [ch["start"] for ch in situation.chapters]
    extra = [
        (position, score) for position, score in situation.candidates
        if all(abs(position - s) > SAME_BOUNDARY_SECONDS for s in starts)
    ]
    extra.sort(key=lambda c: -c[1])
    return sorted(position for position, _ in extra[:MAX_EXTRA_CANDIDATES])


def frame_plan(situation: Situation) -> list[tuple[float, str]]:
    """[(seconds, label), ...] of the frames to send, in time order.

    Each proposed chapter gets frames_per_chapter frames just after its
    start; in PLACE mode each strong candidate the proposal doesn't use
    gets one, so a missed song can be noticed.
    """
    duration = situation.video["duration"]
    # frames_per_chapter 0 sends none: the words alone, for a cheap ask.
    offsets = FRAME_OFFSETS[:max(0, situation.frames_per_chapter)]
    plan: dict[float, str] = {}
    if not offsets:
        return []
    for i, chapter in enumerate(situation.chapters):
        for offset in offsets:
            t = round(chapter["start"] + offset, 1)
            if t < min(chapter["end"], duration) - 0.5:
                plan.setdefault(t, f"chapter {i + 1}, {offset:g}s in")
    if situation.mode == PLACE:
        for position in _extra_candidates(situation):
            t = round(position + offsets[0], 1)
            if t < duration - 0.5:
                plan.setdefault(t, f"candidate at {utils.format_seconds(position)}, "
                                   f"{offsets[0]:g}s in")
    return sorted(plan.items())


# --- the request ----------------------------------------------------------------

SYSTEM_PROMPT = (
    "You identify the songs in concert videos and name their chapters. You are "
    "given the video's file and folder names, its length, its chapters or the "
    "estimated starts of its songs, sometimes a tracklist, sometimes the quiet "
    "stretches an audio analysis found between songs, and frames taken a few "
    "seconds after each start. Use every clue together: the song title shown on "
    "screen (concert Blu-rays usually caption each song a few seconds in), the "
    "setlist you know for this artist, tour and show (look it up on setlist.fm or "
    "Wikipedia when you can search), the song lengths, and the order. Be honest "
    "about confidence. Reply with a single JSON object and nothing else."
)


def _time(seconds: float) -> str:
    return f"{utils.format_seconds(seconds)} ({seconds:.0f}s)"


def _describe_video(situation: Situation) -> str:
    video = situation.video
    lines = [
        f"Video: {video['display_name']}",
        f"Kind: {'Blu-ray title' if video.get('type') == 'bluray' else 'video file'}",
        f"Length: {_time(video['duration'])}",
    ]
    if situation.context:
        lines.append(f"Folders: {situation.context}")
    return "\n".join(lines)


def _describe_chapters(situation: Situation) -> str:
    if situation.mode == NAME:
        head = (
            "The video's chapters. Keep them exactly where they are; your job is "
            "to name them.\n"
        )
    else:
        head = (
            "Estimated song starts. They may be wrong, and songs may be missing or "
            "split in two. You may keep, move, add or drop starts.\n"
        )
    lines = [head]
    for i, ch in enumerate(situation.chapters):
        length = utils.format_seconds(ch["end"] - ch["start"])
        current = ch.get("title")
        text = f"  {i + 1}. starts {_time(ch['start'])}, length {length}"
        if current:
            text += f", currently titled: {current!r}"
        lines.append(text)
    return "\n".join(lines)


def _describe_tracks(situation: Situation) -> str:
    if not situation.tracks:
        return ""
    where = {
        "musicbrainz": "from MusicBrainz",
        "manual": "pasted by the person",
        "pasted": "pasted by the person",
    }.get(situation.tracks_source, "")
    lines = [f"Tracklist {where}, in order. Use its titles for the songs it lists:".strip()]
    for i, track in enumerate(situation.tracks):
        length = f" ({utils.format_seconds(track['length'])})" if track.get("length") else ""
        lines.append(f"  {i + 1}. {track['title']}{length}")
    return "\n".join(lines)


def _describe_candidates(situation: Situation) -> str:
    if situation.mode != PLACE or not situation.candidates:
        return ""
    lines = [
        "Moments where the music stops, measured from the audio (the higher the "
        "score, the more it looks like a gap between songs). A song's start should "
        "be one of these where possible:"
    ]
    for position, score in situation.candidates:
        lines.append(f"  {_time(position)} score {score:.0f}")
    return "\n".join(lines)


def _instructions(situation: Situation) -> str:
    n = len(situation.chapters)
    rules = []
    if situation.mode == NAME:
        rules.append(
            f"Return one entry per chapter, numbered 1 to {n} in the \"chapter\" "
            "field, keeping the chapters where they are. Chapters that are not a song "
            "get a short descriptive title: \"Opening\", \"MC\", \"Encore break\", "
            "\"End credits\". A part that belongs to the song after it (a story film, "
            "an entrance) is titled \"Intro to <song>\"."
        )
    else:
        rules.append(
            "Return one entry per chapter you want, each with its \"start\" in "
            "seconds, in time order, starting with one at 0. One chapter per song; a "
            "long opening, MC or encore break may have its own. Prefer starts that are "
            "listed quiet moments; only put one elsewhere if the frames show it must "
            "be. A part that belongs to the song after it is \"Intro to <song>\"."
        )
    if situation.translate:
        rules.append(
            f"Each title is {ENGLISH_TITLE}. \"original_title\" "
            "is the song's official title in its own script, only when that script "
            "isn't Latin and you know it exactly (メギツネ for Megitsune, 紅月-アカツキ- "
            "for Akatsuki). A song whose official title is already in Latin script "
            "(KARATE, Gimme Chocolate!!, Road of Resistance) has none: never "
            "transliterate an English title into another script. Unsure: null."
        )
    else:
        rules.append(
            "Titles are as the song is officially written; leave original_title null."
        )
    rules.append(
        "Work it out in this order, and the JSON's fields are in this order too. "
        "First say in \"show\" what this video is (artist, tour or event, venue, "
        "date), or null. Then write in \"setlist\" the setlist you know for this exact "
        "show, in order - or, if you don't know this show, the setlist this tour "
        "usually played, and say so in \"notes\". Then match the chapters to it: by "
        "the order, by each chapter's length against the song's usual length, and by "
        "what the frames show - and use what the frames show to correct the setlist "
        "where they disagree."
    )
    rules.append(
        "For each chapter, \"seen\" says in a few words what its frames show, quoting "
        "any on-screen text exactly. \"confidence\" is \"high\" when a frame shows "
        "the title or you are sure, \"medium\" when inferred from the setlist, the "
        "order or the lengths, and \"low\" for a guess. Never invent a song: if you "
        "can't tell, give your best guess with low confidence and say why in "
        "\"note\". \"notes\" at the end is for anything the person should check: a "
        "boundary that looks wrong, a song that seems to be missing."
    )
    example = (
        '{"show": "...", "setlist": ["...", "..."], "chapters": [{"chapter": 1, '
        '"start": 0, "seen": "...", "title": "...", "original_title": null, '
        '"confidence": "high", "note": ""}], "notes": ""}'
    )
    return "\n".join(rules) + f"\n\nReply with exactly this shape:\n{example}"


def build_messages(situation: Situation, grabbed: dict[float, bytes]) -> list[dict]:
    """The chat messages for this situation, with whichever planned frames
    were grabbed."""
    sections = [
        _describe_video(situation),
        _describe_chapters(situation),
        _describe_tracks(situation),
        _describe_candidates(situation),
    ]
    parts = [ai.text_part("\n\n".join(s for s in sections if s))]
    plan = [(t, label) for t, label in frame_plan(situation) if t in grabbed]
    if plan:
        parts.append(ai.text_part(f"{len(plan)} frames from the video follow."))
        for t, label in plan:
            parts.append(ai.text_part(f"Frame at {_time(t)} ({label}):"))
            parts.append(ai.image_part(grabbed[t]))
    else:
        parts.append(ai.text_part("No frames could be taken from this video."))
    parts.append(ai.text_part(_instructions(situation)))
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": parts},
    ]


# --- the reply ------------------------------------------------------------------

_CLOCK_RE = re.compile(r"^\s*(\d+):(\d{1,2})(?::(\d{1,2}))?\s*$")


def _seconds(value) -> float | None:
    """A time the model gave, as seconds: a number, or "h:mm:ss"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        match = _CLOCK_RE.match(value)
        if match:
            a, b, c = match.groups()
            if c is None:
                return int(a) * 60 + int(b)
            return int(a) * 3600 + int(b) * 60 + int(c)
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _clean(value) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _confidence(value) -> str:
    text = str(value or "").strip().lower()
    return text if text in CONFIDENCES else "medium"


def _entries(reply: dict) -> list[dict]:
    entries = reply.get("chapters")
    if not isinstance(entries, list):
        raise ai.AIError("the model's reply had no chapters")
    return [e for e in entries if isinstance(e, dict)]


def _titles(entry: dict) -> tuple[str | None, str | None]:
    title = _clean(entry.get("title"))
    original = _clean(entry.get("original_title"))
    if original and title and original == title:
        original = None
    return title, original


def _named_chapters(situation: Situation, entries) -> tuple[list[dict], list[Row]]:
    """NAME mode: the existing chapters, titled where the model titled them."""
    chapters = [dict(ch) for ch in situation.chapters]
    rows: list[Row] = []
    seen = set()
    for entry in entries:
        number = _seconds(entry.get("chapter"))
        if number is None:
            continue
        index = int(number) - 1
        if not 0 <= index < len(chapters) or index in seen:
            continue
        seen.add(index)
        title, original = _titles(entry)
        if not title:
            continue
        chapter = chapters[index]
        chapter["title"] = title
        chapter["source"] = SOURCE
        if original:
            chapter["original_title"] = original
        else:
            chapter.pop("original_title", None)
        rows.append(Row(
            chapter=index + 1, start=chapter["start"], title=title,
            original_title=original, confidence=_confidence(entry.get("confidence")),
            note=_clean(entry.get("note")) or "", moved=False,
        ))
    for index, chapter in enumerate(chapters):
        if index not in seen:
            rows.append(Row(
                chapter=index + 1, start=chapter["start"], title=chapter.get("title"),
                original_title=chapter.get("original_title"), confidence="low",
                note="not named", moved=False,
            ))
    rows.sort(key=lambda row: row.chapter)
    return chapters, rows


def _placed_chapters(situation: Situation, entries) -> tuple[list[dict], list[Row]]:
    """PLACE mode: chapters at the starts the model chose, snapped to the
    moments it was told about."""
    duration = situation.video["duration"]
    proposed = {round(ch["start"], 1): ch for ch in situation.chapters}
    known = sorted(set(proposed) | {round(p, 1) for p, _ in situation.candidates})

    picked: list[tuple[float, bool, dict]] = []
    for entry in entries:
        start = _seconds(entry.get("start"))
        if start is None or not 0 <= start < duration - 1.0:
            continue
        nearest = min(known, key=lambda k: abs(k - start), default=None)
        if nearest is not None and abs(nearest - start) <= SNAP_SECONDS:
            start = nearest
        picked.append((start, start in proposed, entry))
    picked.sort(key=lambda p: p[0])

    # The first chapter always starts at 0; two starts that are too close
    # to both be songs keep the earlier.
    kept: list[tuple[float, bool, dict]] = []
    for start, was_proposed, entry in picked:
        if kept and start - kept[-1][0] < MIN_SONG_SECONDS:
            continue
        kept.append((start, was_proposed, entry))
    if not kept or kept[0][0] > 0.0:
        head = (0.0, 0.0 in proposed, {})
        if kept and kept[0][0] < MIN_SONG_SECONDS:
            kept[0] = (0.0, kept[0][1], kept[0][2])
        else:
            kept.insert(0, head)

    chapters: list[dict] = []
    rows: list[Row] = []
    for i, (start, was_proposed, entry) in enumerate(kept):
        end = kept[i + 1][0] if i + 1 < len(kept) else duration
        title, original = _titles(entry)
        chapter = {
            "index": i, "start": start, "end": end,
            "title": title, "source": SOURCE if title else "auto-numbered",
        }
        if original and title:
            chapter["original_title"] = original
        moved = not was_proposed
        if i > 0 and (moved or proposed[round(start, 1)].get("estimated")):
            chapter["estimated"] = True
        chapters.append(chapter)
        rows.append(Row(
            chapter=i + 1, start=start, title=title, original_title=original,
            confidence=_confidence(entry.get("confidence")) if entry else "low",
            note=_clean(entry.get("note")) or ("" if entry else "added: the video starts here"),
            moved=moved,
        ))
    return chapters, rows


def parse_reply(text, situation: Situation, frames_sent: int = 0,
                model: str = "") -> Result:
    """`text` is the model's reply, or the JSON object already parsed from it."""
    reply = text if isinstance(text, dict) else ai.parse_json_reply(text)
    entries = _entries(reply)
    if situation.mode == NAME:
        chapters, rows = _named_chapters(situation, entries)
    else:
        chapters, rows = _placed_chapters(situation, entries)
    return Result(
        mode=situation.mode, chapters=chapters, rows=rows,
        show=_clean(reply.get("show")) or "",
        notes=_clean(reply.get("notes")) or "",
        frames_sent=frames_sent, model=model,
        setlist=[
            str(item).strip() for item in (reply.get("setlist") or [])
            if isinstance(reply.get("setlist"), list) and str(item).strip()
        ],
    )


# --- running it -----------------------------------------------------------------

# How much of the progress bar the frames take; the request is the rest.
FRAMES_PROGRESS = 80


def _ask(chat, messages, settings, **kwargs):
    """The real chat is asked for JSON with one retry (ai.chat_json); a
    stand-in's text is passed on as it is."""
    if chat is ai.chat:
        return ai.chat_json(messages, settings, **kwargs)
    return chat(messages, settings, **kwargs)


def run(situation: Situation, settings: dict, progress_cb=None, cancel=None,
        grab=frames.cached_grab_many, chat=ai.chat) -> Result:
    """Grab the frames, ask, and read the answer. `grab` and `chat` are
    frames.cached_grab_many and ai.chat unless a test says otherwise."""
    plan = frame_plan(situation)
    grabbed = grab(
        situation.video, [t for t, _ in plan],
        progress_cb=(lambda p: progress_cb(p * FRAMES_PROGRESS // 100)) if progress_cb else None,
        cancel=cancel,
    ) if plan else {}
    if cancel is not None and cancel.is_set():
        raise frames.Cancelled("cancelled")
    if progress_cb:
        progress_cb(FRAMES_PROGRESS)
    messages = build_messages(situation, grabbed)
    fallback = ""
    try:
        text = _ask(chat, messages, settings, online=situation.online)
    except ai.SearchUnavailable:
        # Nano-GPT allows only LinkUp with Zero Data Retention on; the
        # search still happens, with it.
        wanted = ai.SEARCH_PROVIDERS.get(settings.get(ai.SETTING_SEARCH), "that search")
        settings = ai.with_search(settings, "linkup")
        text = _ask(chat, messages, settings, online=True)
        fallback = (
            f"{wanted} isn't allowed on this Nano-GPT account (Zero Data Retention is "
            "on), so LinkUp searched instead."
        )
    if progress_cb:
        progress_cb(100)
    result = parse_reply(
        text, situation, frames_sent=len(grabbed), model=ai.model_name(settings, situation.online)
    )
    if fallback:
        result = replace(result, notes=" ".join(p for p in (fallback, result.notes) if p))
    return result


# --- titles only ----------------------------------------------------------------

TRANSLATE_PROMPT = (
    "These are the chapter titles of a concert video, in order. Give each one as "
    f"{ENGLISH_TITLE}. A title already in Latin script stays "
    "exactly as it is. A title that looks garbled or cut short - characters mis-decoded "
    "into another script, like \"Doki Doki в\" or \"YAVAпјЃ\" - is the song's full "
    "official title (\"Doki Doki☆Morning\", \"YAVA!\"). Where the given title is in "
    "another script, put it in "
    "\"original_title\"; otherwise original_title is null. Reply with exactly: "
    '{"titles": [{"n": 1, "title": "...", "original_title": null}]}'
)


def translate_titles(titles: list[str], settings: dict,
                     chat=ai.chat) -> list[tuple[str, str | None]]:
    """(english, original_or_None) for each title, in order. A title the
    model didn't answer for comes back unchanged."""
    if not titles:
        return []
    listing = "\n".join(f"{i + 1}. {title}" for i, title in enumerate(titles))
    text = _ask(
        chat,
        [
            {"role": "system",
             "content": "You give song titles as they are officially known in "
                        "English-language releases. Reply with JSON only."},
            {"role": "user", "content": f"{TRANSLATE_PROMPT}\n\n{listing}"},
        ],
        settings, max_tokens=4096,
    )
    reply = text if isinstance(text, dict) else ai.parse_json_reply(text)
    answered: dict[int, tuple[str, str | None]] = {}
    for entry in reply.get("titles") or []:
        if not isinstance(entry, dict):
            continue
        number = _seconds(entry.get("n"))
        title, original = _titles(entry)
        if number is None or not title:
            continue
        answered[int(number) - 1] = (title, original)
    result = []
    for i, given in enumerate(titles):
        title, original = answered.get(i, (given, None))
        if title == given:
            original = None
        elif original is None and given != title:
            original = given
        result.append((title, original))
    return result
