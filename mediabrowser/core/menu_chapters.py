# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Naming a Blu-ray title's chapters from the disc's own menu.

The scene-selection menu is the one list of a disc's songs its makers
wrote, and each of its buttons plays exactly one chapter. Both halves of
that are read here, and neither is a guess:

- which chapter a button plays, by pressing it in a sandbox (hdmv) and
  seeing which of the title's playlist marks it starts from;
- what the button says, by rendering the menu (bdmenu) exactly as a player
  would with that button highlighted, and having a model transcribe it.

So the model reads text off a picture and nothing more; it doesn't match
anything to anything. It is also shown the chapters no button reaches -
the story films between songs, an encore break - and asked what they are,
which is a judgement and marked as one.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import ai, ai_chapters, bdmenu, bluray, hdmv, utils

SOURCE = "menu"
# What a chapter named off the menu says, in the preview's notes column.
MENU_NOTE = "from the disc's menu"

# How close (seconds) a mark must be to a chapter's start to be that chapter.
SAME_START_SECONDS = 1.0
# How the whole page and each button are shown to the model.
PAGE_WIDTH = 1280
CROP_MAX_WIDTH = 900
CROP_MARGIN = 10
# A button narrower than this is probably a cursor beside its text, so its
# picture reaches this far to the right to take the text in too.
CROP_MIN_WIDTH = 560
# Pictures composed at once: each is an ffmpeg run, mostly waiting on the disc.
WORKERS = 4


class MenuError(Exception):
    """Why there's no menu to read, in words for the person."""


class Cancelled(MenuError):
    pass


@dataclass
class ChapterButton:
    chapter: int  # index into the title's chapters
    page: int  # index into DiscMenu.pages
    button: bdmenu.Button
    picture: bytes | None = None  # the button highlighted, cropped


@dataclass
class MenuPage:
    clip: str
    popup: bool
    page_id: int
    picture: bytes | None  # the whole page, nothing highlighted


@dataclass
class DiscMenu:
    pages: list[MenuPage] = field(default_factory=list)
    buttons: list[ChapterButton] = field(default_factory=list)

    def chapters(self) -> list[int]:
        return sorted({b.chapter for b in self.buttons})


# --- finding the buttons ------------------------------------------------------------


def _chapter_at(chapters, seconds: float) -> int | None:
    for i, chapter in enumerate(chapters):
        if abs(chapter["start"] - seconds) <= SAME_START_SECONDS:
            return i
    for i, chapter in enumerate(chapters):
        if chapter["start"] <= seconds < chapter["end"]:
            return i
    return None


def _crop_box(menu: bdmenu.Menu, button: bdmenu.Button) -> tuple[int, int, int, int] | None:
    box = bdmenu.button_box(menu, button)
    if box is None:
        return None
    x, y, w, h = box
    x0, y0 = max(0, x - CROP_MARGIN), max(0, y - CROP_MARGIN)
    x1 = min(menu.width, x + max(w, CROP_MIN_WIDTH) + CROP_MARGIN)
    y1 = min(menu.height, y + h + CROP_MARGIN)
    # Even sizes: the JPEG encoder wants them.
    return x0, y0, (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2


@dataclass
class _Candidate:
    clip: Path
    menu: bdmenu.Menu
    page: bdmenu.Page
    hits: dict[int, bdmenu.Button]  # chapter -> the button that plays it


def _candidates(root: str, video: dict, progress_cb=None, cancel=None) -> list[_Candidate]:
    navigation = hdmv.read_navigation(root)
    marks = bluray.playlist_marks(root, video["playlist"])
    chapters = video["chapters"]
    clips = bdmenu.clips_with_menus(root)
    found = []
    for n, clip in enumerate(clips):
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelled")
        try:
            menus = bdmenu.read_menus(clip)
        except OSError:
            menus = []
        for menu in menus:
            for page in menu.pages:
                hits: dict[int, bdmenu.Button] = {}
                for button in page.buttons:
                    # Only what's on screen is a menu item. An invisible or
                    # auto-action button is the menu's plumbing - the relay a
                    # scene button hands on to - and pressed directly it
                    # plays whatever its registers happen to hold.
                    if (not button.commands or button.auto_action
                            or bdmenu.button_box(menu, button) is None):
                        continue
                    target = hdmv.press(
                        button, navigation, menu, page,
                        playlist=video["playlist"] if menu.popup else None,
                    )
                    if (target is None or target.playlist != video["playlist"]
                            or target.mark is None or not 0 <= target.mark < len(marks)):
                        continue
                    chapter = _chapter_at(chapters, marks[target.mark][1])
                    if chapter is not None:
                        hits.setdefault(chapter, button)
                if len(hits) >= 2:
                    found.append(_Candidate(clip, menu, page, hits))
        if progress_cb:
            progress_cb(int(30 * (n + 1) / max(1, len(clips))))
    return found


def _choose(candidates: list[_Candidate]) -> list[_Candidate]:
    """The fewest pages that between them reach every chapter any menu
    reaches: a scene menu over two pages takes both, and the pop-up menu
    that repeats a full-screen one is left out. A full-screen page is
    preferred to a pop-up - it has its picture behind it."""
    ranked = sorted(candidates, key=lambda c: (-len(c.hits), c.menu.popup))
    chosen, covered = [], set()
    for candidate in ranked:
        new = set(candidate.hits) - covered
        if new:
            chosen.append(candidate)
            covered |= new
    return chosen


def read(video: dict, progress_cb=None, cancel: threading.Event | None = None) -> DiscMenu:
    """The title's scene-selection menu: its pages and each chapter's button,
    pictured. Raises MenuError saying why when there isn't one to read."""
    if video.get("type") != "bluray":
        raise MenuError(
            "Only a Blu-ray folder has its menus - a rip to a single file leaves them behind."
        )
    root = video["path"]
    jar = Path(root) / "BDMV" / "JAR"
    java = jar.is_dir() and any(jar.iterdir())
    try:
        candidates = _candidates(root, video, progress_cb, cancel)
    except bluray.BlurayError as exc:
        raise MenuError(f"The disc couldn't be read: {exc}") from exc
    chosen = _choose(candidates)
    if not chosen:
        if java:
            raise MenuError(
                "This disc's menus are written in Java (BD-J), which can't be read yet."
            )
        raise MenuError("No menu on this disc has buttons that play this title's chapters.")

    disc = DiscMenu()
    # Each menu clip's background, decoded once for all its pictures.
    backgrounds: dict[Path, bytes | None] = {}
    jobs = []
    for page_index, candidate in enumerate(chosen):
        if candidate.menu.popup:
            background = None
        else:
            if candidate.clip not in backgrounds:
                clip = bdmenu.background_clip(root, candidate.clip)
                backgrounds[candidate.clip] = bdmenu.background_frame(
                    clip, candidate.menu.width, candidate.menu.height,
                    bdmenu.background_time(clip),
                ) if clip is not None else None
            background = backgrounds[candidate.clip]
        disc.pages.append(MenuPage(candidate.clip.name, candidate.menu.popup,
                                   candidate.page.id, None))
        jobs.append((disc.pages[-1], candidate, None, background))
        for chapter, button in sorted(candidate.hits.items()):
            if any(b.chapter == chapter for b in disc.buttons):
                continue
            entry = ChapterButton(chapter, page_index, button)
            disc.buttons.append(entry)
            jobs.append((entry, candidate, button, background))
    disc.buttons.sort(key=lambda b: b.chapter)

    done = [0]

    def compose(job):
        target, candidate, button, background = job
        if cancel is not None and cancel.is_set():
            return
        rgba = bdmenu.render(candidate.menu, candidate.page, selected=button)
        if button is None:
            target.picture = bdmenu.picture(candidate.menu, rgba, background,
                                            width=PAGE_WIDTH)
        else:
            box = _crop_box(candidate.menu, button)
            width = min(box[2], CROP_MAX_WIDTH) if box else None
            target.picture = bdmenu.picture(candidate.menu, rgba, background, crop=box,
                                            width=width)
        done[0] += 1
        if progress_cb:
            progress_cb(30 + int(50 * done[0] / len(jobs)))

    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(compose, jobs))
    if cancel is not None and cancel.is_set():
        raise Cancelled("cancelled")
    return disc


# --- asking the model to read it -------------------------------------------------

SYSTEM_PROMPT = (
    "You read Blu-ray menus. You are shown a concert disc's own scene-selection "
    "menu and, one by one, each of its buttons highlighted. Which chapter each "
    "button plays has already been worked out exactly from the disc's navigation "
    "commands, so your job for those is only to transcribe the song title the "
    "button shows. Reply with a single JSON object and nothing else."
)


def _describe(video: dict, context: str) -> str:
    lines = [f"Title: {video['display_name']}"]
    if context:
        lines.append(f"Folders: {context}")
    if video.get("hints"):
        lines.append(video["hints"])
    lines.append(f"Length: {utils.format_seconds(video['duration'])}")
    lines.append("Its chapters:")
    for i, chapter in enumerate(video["chapters"]):
        lines.append(
            f"  {i + 1}. starts {utils.format_seconds(chapter['start'])}, "
            f"length {utils.format_seconds(chapter['end'] - chapter['start'])}"
        )
    return "\n".join(lines)


def _instructions(disc: DiscMenu, video: dict, translate: bool) -> str:
    unreached = [i + 1 for i in range(len(video["chapters"])) if i not in disc.chapters()]
    rules = [
        "For every button give \"title\": the item's text exactly as the menu writes "
        "it, without its list number (\"4. BxMxC\" is \"BxMxC\"), keeping any "
        "credit such as \"(feat. F.HERO)\". Where the typeface makes letters "
        "ambiguous - a capital I drawn like a lowercase l, 0 and O - write the "
        "song's official spelling (\"Iine!\", not \"line!\"). If the button isn't a "
        "song at all - \"Play All\", \"Back\" - set \"is_title\" false, and name "
        "its chapter in \"others\" as below.",
    ]
    if translate:
        rules.append(
            "Where the menu's text isn't in Latin script, \"title\" is "
            f"{ai_chapters.ENGLISH_TITLE}, and "
            "\"original_title\" is the text as the menu writes it. Otherwise "
            "original_title is null."
        )
    else:
        rules.append("Leave original_title null.")
    rules.append(
        (f"No button plays chapter(s) {', '.join(map(str, unreached))}. " if unreached
         else "")
        + "For each chapter no song button plays, say in \"others\" what it is from "
        "its place among the songs and its length - \"Opening\", \"Intro to <next "
        "song>\" for a film before a song, \"MC\", \"Encore break\", \"End "
        "credits\" - with \"confidence\" high, medium or low, and why in \"note\". "
        "This part is a judgement; be honest about it."
    )
    example = (
        '{"show": "...", "buttons": [{"button": 1, "title": "...", '
        '"original_title": null, "is_title": true}], '
        '"others": [{"chapter": 1, "title": "...", "confidence": "medium", '
        '"note": ""}], "notes": ""}'
    )
    return "\n".join(rules) + f"\n\nReply with exactly this shape:\n{example}"


def build_messages(video: dict, disc: DiscMenu, context: str = "",
                   translate: bool = True) -> list[dict]:
    parts = [ai.text_part(_describe(video, context))]
    pages = [p for p in disc.pages if p.picture]
    if pages:
        parts.append(ai.text_part(f"The disc's scene-selection menu ({len(pages)} page(s)):"))
        for page in pages:
            parts.append(ai.image_part(page.picture))
    parts.append(ai.text_part(
        "Each button follows, highlighted as a player shows it when selected, with "
        "the chapter the disc's navigation says it plays:"
    ))
    for n, entry in enumerate(disc.buttons, start=1):
        chapter = video["chapters"][entry.chapter]
        parts.append(ai.text_part(
            f"Button {n} plays chapter {entry.chapter + 1} "
            f"(starts {utils.format_seconds(chapter['start'])}):"
        ))
        if entry.picture:
            parts.append(ai.image_part(entry.picture))
        else:
            parts.append(ai.text_part("(no picture could be made of this button)"))
    parts.append(ai.text_part(_instructions(disc, video, translate)))
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": parts},
    ]


def parse_reply(text, video: dict, disc: DiscMenu, model: str = "") -> ai_chapters.Result:
    """The model's reading, as a naming result over the title's chapters:
    each button's text on the chapter it plays (from the menu, sure), and
    its view of the chapters no button plays (a judgement, marked so).
    `text` is the reply, or the JSON object already parsed from it."""
    reply = text if isinstance(text, dict) else ai.parse_json_reply(text)
    chapters = [dict(c) for c in video["chapters"]]
    rows: dict[int, ai_chapters.Row] = {}

    def name(index, title, original, source):
        chapter = chapters[index]
        chapter["title"] = title
        chapter["source"] = source
        if original and original != title:
            chapter["original_title"] = original
        else:
            chapter.pop("original_title", None)

    for entry in reply.get("buttons") or []:
        if not isinstance(entry, dict):
            continue
        number = ai_chapters._seconds(entry.get("button"))
        if number is None or not 1 <= int(number) <= len(disc.buttons):
            continue
        index = disc.buttons[int(number) - 1].chapter
        title, original = ai_chapters._titles(entry)
        if not title or entry.get("is_title") is False or index in rows:
            continue
        name(index, title, original, SOURCE)
        rows[index] = ai_chapters.Row(
            index + 1, chapters[index]["start"], title, chapters[index].get("original_title"),
            "high", MENU_NOTE, False,
        )

    reached = set(disc.chapters())
    for entry in reply.get("others") or []:
        if not isinstance(entry, dict):
            continue
        number = ai_chapters._seconds(entry.get("chapter"))
        if number is None:
            continue
        index = int(number) - 1
        # A chapter a song button plays is the menu's to name (the buttons
        # were read first), not the model's to judge.
        if not 0 <= index < len(chapters) or index in rows:
            continue
        title, original = ai_chapters._titles(entry)
        if not title:
            continue
        name(index, title, original, ai_chapters.SOURCE)
        note = ai_chapters._clean(entry.get("note")) or ""
        rows[index] = ai_chapters.Row(
            index + 1, chapters[index]["start"], title, chapters[index].get("original_title"),
            ai_chapters._confidence(entry.get("confidence")),
            ("not on the menu" + (f": {note}" if note else "")), False,
        )

    for index, chapter in enumerate(chapters):
        if index not in rows:
            rows[index] = ai_chapters.Row(
                index + 1, chapter["start"], chapter.get("title"),
                chapter.get("original_title"), "low",
                "on the menu, but unreadable" if index in reached else "not on the menu",
                False,
            )
    return ai_chapters.Result(
        mode=ai_chapters.NAME, chapters=chapters,
        rows=[rows[i] for i in sorted(rows)],
        show=ai_chapters._clean(reply.get("show")) or "",
        notes=ai_chapters._clean(reply.get("notes")) or "",
        frames_sent=sum(1 for p in disc.pages if p.picture)
        + sum(1 for b in disc.buttons if b.picture),
        model=model,
    )


def ask(video: dict, disc: DiscMenu, settings: dict, context: str = "",
        translate: bool = True, chat=ai.chat) -> ai_chapters.Result:
    """Have the model read the menu. No web search: everything it needs is
    in the pictures."""
    messages = build_messages(video, disc, context, translate)
    if chat is ai.chat:
        reply = ai.chat_json(messages, settings)
    else:
        reply = chat(messages, settings)
    return parse_reply(reply, video, disc, model=ai.model_name(settings))
