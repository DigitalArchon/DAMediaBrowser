# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

import re
import unicodedata
from pathlib import PurePath

# The app's text is English wherever it runs; strftime("%b") would name
# the month in the machine's own language ("ápr" under a Hungarian LC_TIME).
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_date(year: int, month: int, day: int, pad_day: bool = False) -> str:
    """"19 Apr 2026", whatever the locale."""
    return f"{day:02d}" + f" {MONTHS[month - 1]} {year}" if pad_day else (
        f"{day} {MONTHS[month - 1]} {year}"
    )


def format_seconds(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def guess_search_query(display_name: str) -> str:
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", display_name)
    text = re.sub(r"[._]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# "1. ", "02 - ", "3) " at the start of a file name: a position in a
# series, not part of anything MusicBrainz would know it by.
_LEADING_NUMBER_RE = re.compile(r"^\s*\d{1,3}\s*[.\-)]\s+")
# When a TV recorder made it: "20260419 2100 [初]BABYMETAL ..." is a
# broadcast that started at 9pm on 19 April 2026. No release is known by it.
_RECORDED_AT_RE = re.compile(r"^\s*(?:19|20)\d{6}(?:[\s_-]*\d{4}(?!\d))?\s*")
# The suffix a multi-title Blu-ray's titles get: "Disc - Title 8".
_TITLE_SUFFIX_RE = re.compile(r"\s+-\s+Title\s+\d+$")
# What MusicBrainz's search reads as syntax rather than words: in
# "AWAKENS -THE SUN ALSO RISES-" the hyphen means "without", and the
# release it names is the one search that can't find it.
_SEARCH_SYNTAX_RE = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]+')


def search_words(text: str) -> str:
    """Text made safe to search MusicBrainz with as plain words."""
    return re.sub(r"\s+", " ", _SEARCH_SYNTAX_RE.sub(" ", text)).strip()


def _clean_folder_name(name: str) -> str:
    # A folder named with hyphens for spaces (10-BABYMETAL-BUDOKAN) reads as
    # words once they are spaces; one that already has spaces keeps its
    # hyphens, which there usually separate artist from title.
    if " " not in name:
        name = name.replace("-", " ")
    return guess_search_query(name)


def suggest_search_query(video, library_root=None, max_folders: int = 2) -> str:
    """A MusicBrainz search to start from, for the person to edit.

    A file's own name is often only a position in a set - "1. Doomsday I,
    II" - with the artist and album in the folders above it, so up to
    `max_folders` of those (below the library root) are put in front. A
    folder whose name is already in the text adds nothing and is skipped.
    """
    name = _TITLE_SUFFIX_RE.sub("", video["display_name"])
    bare = _RECORDED_AT_RE.sub("", _LEADING_NUMBER_RE.sub("", name))
    name = guess_search_query(bare) or guess_search_query(name)

    path = PurePath(video["path"])
    # A disc's own folder is where its name came from; start above it.
    folder = path.parent
    root = PurePath(library_root) if library_root else None
    parts = [name]
    for _ in range(max_folders):
        if not folder.name or (root is not None and (
            folder == root or not folder.is_relative_to(root)
        )):
            break
        cleaned = _clean_folder_name(folder.name)
        if cleaned and cleaned.lower() not in " ".join(parts).lower():
            parts.insert(0, cleaned)
        folder = folder.parent
    return search_words(" ".join(p for p in parts if p))


def guess_artist(video, library_root=None) -> str:
    """Who a video is probably by, for a person to check: what its name
    has before " - " ("Glass Harbor - Live at Copperfield Hall"), else the
    first folder below the library's own (Concerts/Glass Harbor/...)."""
    name = _TITLE_SUFFIX_RE.sub("", video.get("display_name") or "")
    name = _RECORDED_AT_RE.sub("", _LEADING_NUMBER_RE.sub("", name))
    head, dash, _rest = name.partition(" - ")
    head = guess_search_query(head)
    if dash and head and not re.fullmatch(r"[\d\s.\-]+", head):
        return head
    path = PurePath(video["path"])
    root = PurePath(library_root) if library_root else None
    if root is not None and path.is_relative_to(root):
        parts = path.relative_to(root).parts
        # A file's own name, or a disc's own folder, isn't a folder above it.
        if len(parts) > 1:
            return _clean_folder_name(parts[0])
    return ""


_DURATION_RE = re.compile(r"(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?)(?!\d)")
_LEADING_TRACK_NUMBER_RE = re.compile(r"^\s*\d{1,3}[.\-):]\s*")


def _parse_duration_token(token: str) -> float:
    parts = [int(p) for p in token.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return float(minutes * 60 + seconds)
    hours, minutes, seconds = parts
    return float(hours * 3600 + minutes * 60 + seconds)


def parse_pasted_tracklist(text: str):
    """Parses one track per non-blank line into [{"title", "length"}, ...],
    length in seconds or None if that line had no duration.

    Tolerates an optional leading track number ("1.", "2)") and an optional
    duration token anywhere in the line ("8:23 Title", "Title - 8:23",
    "Title (8:23)") - whatever's left after stripping those is the title.
    Lines with nothing left after stripping are skipped, so junk lines
    (e.g. a "Play Video" link copied along with a tracklist) can just be
    deleted by the user, or left as blank/whitespace-only lines.
    """
    tracks = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        length = None
        match = _DURATION_RE.search(line)
        if match:
            length = _parse_duration_token(match.group(1))
            # What held the duration goes with it: "Title (8:23)", "Title - 8:23".
            line = (line[: match.start()] + line[match.end() :]).strip()
            line = re.sub(r"\(\s*\)|\[\s*\]", "", line).strip(" \t-–—")

        line = _LEADING_TRACK_NUMBER_RE.sub("", line)
        # A bullet before it, but nothing a title ends with: "White Flame
        # -Byakuen-", "Elevator Girl (medley)".
        title = line.strip().lstrip("-–—•* \t").strip()
        if not title:
            continue

        tracks.append({"title": title, "length": length})
    return tracks


def export_named_chapters(chapters) -> str:
    """Formats a video's already-named chapters back into "duration title"
    lines - the same shape parse_pasted_tracklist reads - so titles can be
    round-tripped through an external translator (copy out, translate,
    paste back in) without retyping durations or losing the match.

    Untitled chapters are omitted; nothing to translate there.
    """
    lines = []
    for ch in chapters:
        if ch["title"]:
            duration = ch["end"] - ch["start"]
            lines.append(f"{format_seconds(duration)} {ch['title']}")
    return "\n".join(lines)


_BRACKETED_RE = re.compile(r"\[[^\]]*\]")


def name_from_filename(display_name: str) -> str:
    """What a single-chapter video is probably called, from its file's
    name: without a leading track number or [bracketed tags] like
    "[4K 50fps]". Parentheses stay - "(feat. Poppy)" is part of a name."""
    name = _BRACKETED_RE.sub(" ", display_name or "")
    name = _LEADING_NUMBER_RE.sub("", name)
    name = re.sub(r"[._]+(?=\S)", " ", name) if " " not in name else name
    name = re.sub(r"\s+", " ", name).strip(" -–—")
    return name or (display_name or "").strip()


def title_or_number(index: int, title) -> str:
    """The name to show for a chapter with this title: the title, or the
    chapter's 1-based number when it has none.

    Chapters often have no embedded title, and a blank row reads as a bug
    rather than as "this one was never named".
    """
    return title or f"Chapter {index + 1}"


def chapter_label(index: int, chapter) -> str:
    """title_or_number for a whole chapter dict."""
    return title_or_number(index, chapter["title"])


def set_chapter_title(chapter, title, source: str = "manual", original_title=None) -> None:
    """Names a chapter and records where that name came from.

    The source matters beyond bookkeeping: library._merge_chapters carries
    the app's own titles ('manual', 'musicbrainz', 'ai') across a rescan
    and lets embedded ones be re-read. Clearing a title returns the chapter
    to its number, so the source goes back to 'auto-numbered' with it - a
    chapter with no title but a 'manual' source would survive rescans as a
    permanent blank.

    `original_title` is the song's own title where `title` is a translation
    of it (BABYMETAL's "Megitsune" for "メギツネ"), kept so a search in
    either finds the song. Given, it's stored; not given, it survives only
    while the title it belongs to does.
    """
    previous = chapter.get("title")
    title = (title or "").strip() or None
    chapter["title"] = title
    chapter["source"] = source if title else "auto-numbered"
    original_title = (original_title or "").strip() or None
    if original_title and title and original_title != title:
        chapter["original_title"] = original_title
    elif original_title is not None or title != previous:
        chapter.pop("original_title", None)


def chapter_search_text(index: int, chapter) -> str:
    """Everything a chapter can be found by: its label and, where its
    title is a translation, the original."""
    text = chapter_label(index, chapter)
    original = chapter.get("original_title")
    return f"{text}\n{original}" if original else text


def search_fold(text: str) -> str:
    """Text as search compares it: without case, and without the accents
    on Latin letters, so "cafe lumiere" finds "Café Lumière" and "zoe"
    "Zoë". Full-width letters are matched as ordinary ones. A mark
    that changes a letter elsewhere - the dakuten that makes か into が -
    stays: there it's a different letter, not an accent."""
    if text.isascii():
        return text.casefold()
    kept = []
    for char in unicodedata.normalize("NFKD", text):
        if unicodedata.combining(char) and kept and kept[-1].isascii():
            continue
        kept.append(char)
    return unicodedata.normalize("NFC", "".join(kept)).casefold()


def matches_search(video, chapters, text: str) -> bool:
    """Whether a video should be listed for this search text - by its own
    name, or by any of its chapter titles.
    """
    if not text:
        return True
    text = search_fold(text)
    if text in search_fold(video["display_name"]):
        return True
    return any(text in search_fold(chapter_search_text(i, ch)) for i, ch in enumerate(chapters))


def matching_chapters(chapters, text: str):
    """The (index, chapter) pairs worth showing under a video for this search
    text. An empty search shows them all.
    """
    if not text:
        return list(enumerate(chapters))
    text = search_fold(text)
    return [
        (i, ch) for i, ch in enumerate(chapters)
        if text in search_fold(chapter_search_text(i, ch))
    ]
