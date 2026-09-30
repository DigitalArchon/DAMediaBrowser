# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""ctypes bindings to libbluray, for the small subset needed to enumerate
titles and their chapter marks.

The library is loaded lazily (on first real use) rather than at import
time, so that a system without libbluray installed can still run the app
for regular files - only actually opening a Blu-ray disc requires it.

libbluray's public struct ABI is not perfectly stable across releases:
checked against the real headers for 1.3.4 through 1.5.1, BLURAY_TITLE_INFO
gained a trailing field once (harmless - it's read as a single struct, not
indexed as an array, so an appended field doesn't shift anything we read),
but BLURAY_TITLE_CHAPTER gained a `chapter_name` field in 1.5.0 that changes
its size - and we DO index that one as an array (`info.chapters[c]`), where
using the wrong size would silently misalign every element after the
first. So the chapter struct layout is picked at runtime based on the
loaded library's own reported version, rather than assumed fixed. Fields
we never dereference (clip/mark arrays) are left as opaque pointers so
their internal layout can't affect us either way.
"""

import ctypes
import ctypes.util
import re
from pathlib import Path

TITLES_ALL = 0x00

# The soname isn't guaranteed to be libbluray.so.2 on every distro/version
# (e.g. CachyOS ships libbluray.so.4 for 1.5.1) - ctypes.util.find_library()
# asks the platform's own linker cache, which is the correct way to resolve
# it; the explicit names are only a fallback for systems where that lookup
# itself doesn't work (e.g. a stale cache).
_CANDIDATE_NAMES = [
    "libbluray.so.2", "libbluray.so.3", "libbluray.so.4", "libbluray.so.1", "libbluray.so",
]

TICKS_PER_SECOND = 90000.0

# libbluray < 1.5.0
class BLURAY_TITLE_CHAPTER_V1(ctypes.Structure):
    _fields_ = [
        ("idx", ctypes.c_uint32),
        ("start", ctypes.c_uint64),
        ("duration", ctypes.c_uint64),
        ("offset", ctypes.c_uint64),
        ("clip_ref", ctypes.c_uint),
    ]


# libbluray >= 1.5.0: adds a trailing chapter_name field
class BLURAY_TITLE_CHAPTER_V2(ctypes.Structure):
    _fields_ = [
        ("idx", ctypes.c_uint32),
        ("start", ctypes.c_uint64),
        ("duration", ctypes.c_uint64),
        ("offset", ctypes.c_uint64),
        ("clip_ref", ctypes.c_uint),
        ("chapter_name", ctypes.c_char_p),
    ]


# Only the leading fields we actually read are declared - clips/chapters/marks
# are opaque pointers here (cast to a typed pointer only where dereferenced),
# and any real trailing fields beyond `marks` in newer libbluray versions are
# simply never represented, which is safe since nothing after them is read.
class BLURAY_TITLE_INFO(ctypes.Structure):
    _fields_ = [
        ("idx", ctypes.c_uint32),
        ("playlist", ctypes.c_uint32),
        ("duration", ctypes.c_uint64),
        ("clip_count", ctypes.c_uint32),
        ("angle_count", ctypes.c_uint8),
        ("chapter_count", ctypes.c_uint32),
        ("mark_count", ctypes.c_uint32),
        ("clips", ctypes.c_void_p),
        ("chapters", ctypes.c_void_p),
        ("marks", ctypes.c_void_p),
    ]


# A playlist mark: an entry mark is a chapter; a link mark is only somewhere
# a menu button can jump to. HDMV's LinkMK and PlayPLatMK count both.
class BLURAY_TITLE_MARK(ctypes.Structure):
    _fields_ = [
        ("idx", ctypes.c_uint32),
        ("type", ctypes.c_int),
        ("start", ctypes.c_uint64),
        ("duration", ctypes.c_uint64),
        ("offset", ctypes.c_uint64),
        ("clip_ref", ctypes.c_uint),
    ]


MARK_ENTRY = 1
MARK_LINK = 2


class BlurayError(Exception):
    pass


_lib = None
_chapter_struct = None


def _get_lib():
    global _lib, _chapter_struct
    if _lib is not None:
        return _lib

    resolved = ctypes.util.find_library("bluray")
    names_to_try = ([resolved] if resolved else []) + _CANDIDATE_NAMES

    lib = None
    last_error = None
    for name in names_to_try:
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError as exc:
            last_error = exc

    if lib is None:
        raise BlurayError(
            "libbluray is not installed, or its shared library couldn't be "
            f"found under any of: {', '.join(names_to_try)}. Blu-ray disc "
            "folders can't be read without it - regular video files are "
            "unaffected."
        ) from last_error

    lib.bd_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.bd_open.restype = ctypes.c_void_p

    lib.bd_close.argtypes = [ctypes.c_void_p]
    lib.bd_close.restype = None

    lib.bd_get_titles.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint32]
    lib.bd_get_titles.restype = ctypes.c_uint32

    lib.bd_get_title_info.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint]
    lib.bd_get_title_info.restype = ctypes.POINTER(BLURAY_TITLE_INFO)

    lib.bd_free_title_info.argtypes = [ctypes.POINTER(BLURAY_TITLE_INFO)]
    lib.bd_free_title_info.restype = None

    lib.bd_get_version.argtypes = [
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
    ]
    lib.bd_get_version.restype = None

    lib.bd_get_playlist_info.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint]
    lib.bd_get_playlist_info.restype = ctypes.POINTER(BLURAY_TITLE_INFO)

    # Positioning within a title, for reading a frame at a given time.
    lib.bd_select_title.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.bd_select_title.restype = ctypes.c_uint32
    lib.bd_seek_time.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    lib.bd_seek_time.restype = ctypes.c_int64
    lib.bd_tell_time.argtypes = [ctypes.c_void_p]
    lib.bd_tell_time.restype = ctypes.c_uint64

    # libbluray writes its own diagnostics to stderr - "BD-J check: Failed
    # to load JVM library" on every disc opened on a machine without Java -
    # which the app has no use for.
    try:
        lib.bd_set_debug_mask.argtypes = [ctypes.c_uint32]
        lib.bd_set_debug_mask.restype = None
        lib.bd_set_debug_mask(0)
    except AttributeError:
        pass

    major, minor, _micro = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
    lib.bd_get_version(ctypes.byref(major), ctypes.byref(minor), ctypes.byref(_micro))
    _chapter_struct = (
        BLURAY_TITLE_CHAPTER_V2 if (major.value, minor.value) >= (1, 5)
        else BLURAY_TITLE_CHAPTER_V1
    )

    _lib = lib
    return lib


def is_available():
    """Whether libbluray could be loaded on this system."""
    try:
        _get_lib()
        return True
    except BlurayError:
        return False


def get_version():
    """Returns the loaded libbluray's (major, minor, micro) version, or
    None if it isn't available.
    """
    try:
        lib = _get_lib()
    except BlurayError:
        return None
    major, minor, micro = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
    lib.bd_get_version(ctypes.byref(major), ctypes.byref(minor), ctypes.byref(micro))
    return (major.value, minor.value, micro.value)


def playlist_marks(disc_root, playlist: int) -> list[tuple[int, float]]:
    """Every mark of a playlist, in order: [(type, start_seconds), ...].

    A menu button that plays "mark 7" means the seventh of these, counting
    link marks as well as the entry marks that are chapters.
    """
    lib = _get_lib()
    disc_root = str(Path(disc_root).resolve())
    bd = lib.bd_open(disc_root.encode("utf-8"), None)
    if not bd:
        raise BlurayError(f"bd_open failed for {disc_root}")
    try:
        info_ptr = lib.bd_get_playlist_info(bd, int(playlist), 0)
        if not info_ptr:
            raise BlurayError(f"playlist {playlist} couldn't be read")
        try:
            info = info_ptr.contents
            marks = ctypes.cast(info.marks, ctypes.POINTER(BLURAY_TITLE_MARK))
            return [
                (marks[i].type, marks[i].start / TICKS_PER_SECOND)
                for i in range(info.mark_count)
            ]
        finally:
            lib.bd_free_title_info(info_ptr)
    finally:
        lib.bd_close(bd)


def byte_positions(disc_root, title_idx: int, times) -> dict[float, tuple[int, float]]:
    """Where in a title's stream each of `times` (seconds) is: {time:
    (byte_offset, seconds_before)}.

    ffmpeg reads a disc through its bluray: protocol but can't seek it by
    time - the playlist's timestamps start hours in and its duration comes
    out as a few seconds - so a frame at a time is reached by byte instead.
    libbluray knows the mapping; it lands on the entry point at or before
    the time, and `seconds_before` is how far before, for the reader to
    decode past.
    """
    lib = _get_lib()
    disc_root = str(Path(disc_root).resolve())
    bd = lib.bd_open(disc_root.encode("utf-8"), None)
    if not bd:
        raise BlurayError(f"bd_open failed for {disc_root}")
    positions = {}
    try:
        lib.bd_get_titles(bd, TITLES_ALL, 0)
        if not lib.bd_select_title(bd, int(title_idx)):
            raise BlurayError(f"title {title_idx} couldn't be selected")
        for t in times:
            ticks = int(max(0.0, t) * TICKS_PER_SECOND)
            offset = lib.bd_seek_time(bd, ticks)
            if offset < 0:
                continue
            reached = lib.bd_tell_time(bd) / TICKS_PER_SECOND
            positions[t] = (int(offset), max(0.0, t - reached))
    finally:
        lib.bd_close(bd)
    return positions


_CLIP_NAME = re.compile(rb"(\d{5})M2TS")


def playlist_clips(disc_root) -> dict[int, list[str]]:
    """Each playlist's clips, as their five-digit names, in the order the
    playlist names them - its play items and sub-paths alike. Read straight
    from BDMV/PLAYLIST; no libbluray needed."""
    folder = Path(disc_root) / "BDMV" / "PLAYLIST"
    playlists = {}
    try:
        files = sorted(folder.iterdir())
    except OSError:
        return {}
    for path in files:
        if path.suffix.lower() != ".mpls" or not path.stem.isdigit():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        names = [m.group(1).decode("ascii") for m in _CLIP_NAME.finditer(data)]
        playlists[int(path.stem)] = list(dict.fromkeys(names))
    return playlists


# Two titles this close in length, over the same clips, are the same video.
SAME_LENGTH_SECONDS = 1.0


def _same_clips(a: set, b: set) -> bool:
    """The same clips, give or take one side's extra: the chaptered copy of
    a feature often adds a sub-second stub clip the other lacks, which the
    lengths being all but equal already allows for."""
    return bool(a and b) and (a <= b or b <= a)


def drop_duplicates(titles: list[dict], clips: dict[int, list[str]]) -> list[dict]:
    """Titles that play the same clips for the same length, but one: the one
    with the most chapters, then the first.

    Discs often carry the feature twice - once with chapter marks, once
    without, or once per audio setup - and the copies were each a video on
    the shelf, the chapterless one never worth playing.
    """
    kept: list[dict] = []
    for title in sorted(titles, key=lambda t: (-len(t["chapters"]), t["title_idx"])):
        mine = set(clips.get(title["playlist"], ()))
        if mine and any(
            _same_clips(mine, set(clips.get(other["playlist"], ())))
            and abs(other["duration"] - title["duration"]) <= SAME_LENGTH_SECONDS
            for other in kept
        ):
            continue
        kept.append(title)
    return sorted(kept, key=lambda t: t["title_idx"])


def probe_bluray_disc(disc_root, min_title_seconds: int = 20):
    """Open a Blu-ray disc folder and return chapter/duration info for each
    relevant (non-duplicate, long enough) title.

    Returns a list of dicts:
        {"title_idx": int, "playlist": int, "duration": float,
         "chapters": [{"start": float, "end": float, "title": str | None}, ...]}
    """
    lib = _get_lib()
    chapter_array_type = ctypes.POINTER(_chapter_struct)

    disc_root = str(Path(disc_root).resolve())
    bd = lib.bd_open(disc_root.encode("utf-8"), None)
    if not bd:
        raise BlurayError(f"bd_open failed for {disc_root}")

    results = []
    try:
        # mpv's bd://N addresses titles by their raw disc index (as if queried
        # with TITLES_ALL), not libbluray's deduplicated/filtered "relevant"
        # list. To keep the indices we store usable for playback, we must
        # enumerate with TITLES_ALL here and do our own duration filtering
        # below, rather than asking libbluray to filter (which renumbers).
        num_titles = lib.bd_get_titles(bd, TITLES_ALL, 0)
        for i in range(num_titles):
            info_ptr = lib.bd_get_title_info(bd, i, 0)
            if not info_ptr:
                continue
            try:
                info = info_ptr.contents
                duration_s = info.duration / TICKS_PER_SECOND
                if duration_s < min_title_seconds:
                    continue

                chapter_count = info.chapter_count
                chapter_array = ctypes.cast(info.chapters, chapter_array_type)

                chapters = []
                for c in range(chapter_count):
                    chapter = chapter_array[c]
                    start_s = chapter.start / TICKS_PER_SECOND
                    if c + 1 < chapter_count:
                        end_s = chapter_array[c + 1].start / TICKS_PER_SECOND
                    else:
                        end_s = duration_s
                    name = getattr(chapter, "chapter_name", None)
                    title = name.decode("utf-8", "replace") if name else None
                    chapters.append({"start": start_s, "end": end_s, "title": title})

                if not chapters:
                    chapters = [{"start": 0.0, "end": duration_s, "title": None}]

                results.append({
                    "title_idx": i,
                    "playlist": info.playlist,
                    "duration": duration_s,
                    "chapters": chapters,
                })
            finally:
                lib.bd_free_title_info(info_ptr)
    finally:
        lib.bd_close(bd)

    return drop_duplicates(results, playlist_clips(disc_root))
