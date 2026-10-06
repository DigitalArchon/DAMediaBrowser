# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""A video's chapters as a file other tools read, for putting them into
the video with those tools.

The app never writes to media, so chapters found here stay in its own
library. Someone who wants them inside their files exports them in the
shape their tool takes and runs it themselves:

- Matroska chapters XML, which mkvmerge and mkvpropedit read
  (`mkvpropedit video.mkv --chapters chapters.xml`).
- FFmpeg's metadata file
  (`ffmpeg -i video.mkv -i chapters.txt -map 0 -map_chapters 1 -c copy out.mkv`).
- A CUE sheet, which audio players and splitters read.

Each is saved where the person picks, but never inside a library and
never over a file that isn't the same kind of chapter file already.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path, PurePath

from . import config, utils

MKVMERGE = "mkvmerge"
FFMETADATA = "ffmetadata"
CUE = "cue"
FORMATS = (MKVMERGE, FFMETADATA, CUE)

SUFFIXES = {MKVMERGE: ".xml", FFMETADATA: ".txt", CUE: ".cue"}
LABELS = {
    MKVMERGE: "Matroska chapters for mkvmerge",
    FFMETADATA: "FFmpeg metadata",
    CUE: "CUE sheet",
}
# What a file of each kind starts with, or has near its start, so one can
# be told from anything else before it's replaced.
_SIGNATURES = {
    MKVMERGE: ("<Chapters",),
    FFMETADATA: (";FFMETADATA1",),
    CUE: ("TRACK ", "FILE "),
}

# A CUE sheet counts in frames of a CD: 75 to the second.
CUE_FRAMES_PER_SECOND = 75


class ExportError(Exception):
    """Why chapters can't be saved there, in words for the person."""


def chapter_names(video) -> list[str]:
    """Each chapter's name, or its number when it has none."""
    return [utils.chapter_label(i, ch) for i, ch in enumerate(video["chapters"])]


def _spans(video) -> list[tuple[float, float]]:
    """(start, end) of each chapter, measured from the video's start. A
    disc title's chapters already count from its own start, which is where
    a rip of it starts too."""
    duration = video.get("duration") or 0.0
    spans = []
    for chapter in video["chapters"]:
        start = max(0.0, float(chapter["start"]))
        end = float(chapter.get("end") or duration)
        spans.append((start, max(start, end)))
    return spans


def _clock(seconds: float) -> str:
    """HH:MM:SS.nnnnnnnnn, as Matroska's chapter XML writes a time."""
    nanos = int(round(max(0.0, seconds) * 1_000_000_000))
    whole, nanos = divmod(nanos, 1_000_000_000)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{nanos:09d}"


def _uid(video_id: str, index: int) -> int:
    """A chapter UID that is the same each time this chapter is exported:
    non-zero and within 64 bits, as Matroska requires."""
    import hashlib

    digest = hashlib.sha256(f"{video_id}:{index}".encode()).digest()
    return int.from_bytes(digest[:8], "big") or 1


def mkvmerge_xml(video, video_id: str = "") -> str:
    root = ET.Element("Chapters")
    edition = ET.SubElement(root, "EditionEntry")
    for index, ((start, end), name) in enumerate(
        zip(_spans(video), chapter_names(video), strict=True)
    ):
        atom = ET.SubElement(edition, "ChapterAtom")
        ET.SubElement(atom, "ChapterUID").text = str(_uid(video_id, index))
        ET.SubElement(atom, "ChapterTimeStart").text = _clock(start)
        ET.SubElement(atom, "ChapterTimeEnd").text = _clock(end)
        display = ET.SubElement(atom, "ChapterDisplay")
        ET.SubElement(display, "ChapterString").text = name
        ET.SubElement(display, "ChapterLanguage").text = "und"
    ET.indent(root, space="  ")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE Chapters SYSTEM "matroskachapters.dtd">\n'
        + ET.tostring(root, encoding="unicode")
        + "\n"
    )


def _ffmeta_escape(text: str) -> str:
    """FFmpeg's metadata file takes '=', ';', '#', '\\' and a line break
    in a value only with a backslash before them."""
    out = []
    for char in text:
        if char in "=;#\\\n":
            out.append("\\")
        out.append(char)
    return "".join(out)


def ffmetadata(video) -> str:
    lines = [
        ";FFMETADATA1",
        "; Chapters for: ffmpeg -i VIDEO -i THIS_FILE -map 0 -map_chapters 1 -c copy OUTPUT",
    ]
    for (start, end), name in zip(_spans(video), chapter_names(video), strict=True):
        lines += [
            "",
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={int(round(start * 1000))}",
            f"END={int(round(end * 1000))}",
            f"title={_ffmeta_escape(name)}",
        ]
    return "\n".join(lines) + "\n"


def _cue_text(text: str) -> str:
    """A CUE sheet's strings are in double quotes with no way to escape
    one, and a line break would end the command."""
    return " ".join(text.replace('"', "'").split())


def _cue_time(seconds: float) -> str:
    frames = int(round(max(0.0, seconds) * CUE_FRAMES_PER_SECOND))
    whole, frames = divmod(frames, CUE_FRAMES_PER_SECOND)
    minutes, secs = divmod(whole, 60)
    return f"{minutes:02d}:{secs:02d}:{frames:02d}"


def media_file_name(video) -> str:
    """The file a CUE sheet's FILE line names: a file's own name, and for a
    disc's title the name a rip of it would likely be given."""
    if video.get("type") == "file":
        return PurePath(video["path"]).name
    return f"{video['display_name']}.mkv"


def cue_sheet(video) -> str:
    lines = [f'TITLE "{_cue_text(video["display_name"])}"']
    if video.get("type") != "file":
        lines.insert(0, "REM The disc title's chapters, timed from the title's start")
    lines.append(f'FILE "{_cue_text(media_file_name(video))}" WAVE')
    for index, ((start, _end), name) in enumerate(
        zip(_spans(video), chapter_names(video), strict=True), start=1
    ):
        lines += [
            f"  TRACK {index:02d} AUDIO",
            f'    TITLE "{_cue_text(name)}"',
            f"    INDEX 01 {_cue_time(start)}",
        ]
    return "\n".join(lines) + "\n"


def render(video, fmt: str, video_id: str = "") -> str:
    if fmt == MKVMERGE:
        return mkvmerge_xml(video, video_id)
    if fmt == FFMETADATA:
        return ffmetadata(video)
    if fmt == CUE:
        return cue_sheet(video)
    raise ValueError(f"unknown chapter format {fmt!r}")


def format_for(path) -> str | None:
    """The format a file name's extension says, if any."""
    suffix = PurePath(path).suffix.lower()
    for fmt, known in SUFFIXES.items():
        if suffix == known:
            return fmt
    if suffix == ".ffmeta":
        return FFMETADATA
    return None


def default_name(video, fmt: str) -> str:
    return f"{safe_file_name(video['display_name'])} chapters{SUFFIXES[fmt]}"


# Characters a phone's or a Windows share's file system won't take in a
# name, and the ones that would make it a path.
_UNSAFE = set('<>:"/\\|?*') | {chr(c) for c in range(32)}


def safe_file_name(name: str, fallback: str = "untitled") -> str:
    """`name` as a file name any file system takes: no path separators, no
    characters FAT or NTFS refuse, not ending in a dot or space, and not
    so long that a file system refuses it."""
    cleaned = "".join("_" if char in _UNSAFE else char for char in name)
    cleaned = " ".join(cleaned.split()).strip(" .")
    while len(cleaned.encode("utf-8")) > 180:
        cleaned = cleaned[:-1]
    cleaned = cleaned.rstrip(" .")
    return cleaned or fallback


def inside_library(path, library_roots) -> str | None:
    """The library `path` is in, if it's in one."""
    target = Path(path).expanduser().resolve()
    for root in library_roots:
        root_path = Path(root).resolve()
        if target == root_path or target.is_relative_to(root_path):
            return str(root_path)
    return None


def refused_destination(path, fmt: str, library_roots) -> str | None:
    """Why chapters mustn't be written to `path`, or None if they may: never
    into a library - the app puts nothing among the videos - and never over
    anything but a chapter file of the same kind."""
    target = Path(path).expanduser().resolve()
    root = inside_library(target, library_roots)
    if root is not None:
        return f"{target.parent} is inside the library {root}, which the app never writes to"
    if target.suffix.lower() in config.MEDIA_EXTENSIONS:
        return f"{target.name} is a video's name"
    if format_for(target) != fmt:
        return f"a {LABELS[fmt]} file is saved as {SUFFIXES[fmt]}"
    if target.exists():
        if not target.is_file():
            return f"{target.name} is there already and isn't a file"
        try:
            with open(target, encoding="utf-8-sig", errors="replace") as handle:
                head = handle.read(4096)
        except OSError as exc:
            return f"{target.name} is there already and can't be read: {exc}"
        if not any(sign in head for sign in _SIGNATURES[fmt]):
            return f"{target.name} is there already and isn't a {LABELS[fmt]} file"
    return None


def export(video, fmt: str, path, library_roots=(), video_id: str = "") -> Path:
    """Write the video's chapters to `path` in `fmt`. Raises ExportError
    when it mustn't be written there, OSError when it can't."""
    why = refused_destination(path, fmt, library_roots)
    if why:
        raise ExportError(f"Chapters can't be saved there: {why}.")
    target = Path(path).expanduser().resolve()
    text = render(video, fmt, video_id)
    # Written beside it and then put in its place, so a failure part way
    # leaves whatever was there before.
    partial = target.with_name(f".{target.name}.part")
    try:
        with open(partial, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(partial, target)
    except OSError:
        try:
            os.unlink(partial)
        except OSError:
            pass
        raise
    return target
