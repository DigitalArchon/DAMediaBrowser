# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""DVD-Video folders: their titles and chapters, read from the IFO files,
and their pictures and sound, read from the VOBs and handed to ffmpeg.

A DVD folder - one with a VIDEO_TS folder in it, as a rip copies the disc
- keeps everything a player needs in a few small files. VIDEO_TS.IFO lists
the titles, and which title set (VTS_nn_0.IFO) each lives in. A title set
says each title's chapters as programs of a program chain, each program a
run of cells, and each cell its length and where its sectors are in the
title set's VOBs (VTS_nn_1.VOB onwards, one stream cut into files of a
gigabyte). A chapter starts where its first cell does: add up the lengths
of the cells before it. No library is needed, so DVDs work wherever the
app does; mpv plays them with its own dvdnav.

ffmpeg can't open a title by itself (only newer builds can, and only some),
so a title's sectors are read here, in order, and written into ffmpeg's
input as the MPEG program stream they are. Starting at a moment means
starting at the VOBU before it: the title set's VOBU map lists where
each starts, and each VOBU's navigation pack says how far into its cell it
is, to the frame - so ffmpeg decodes only the last fraction of a second.

Everything here only reads: the IFOs and VOBs are opened for reading.
Discs whose files are encrypted (CSS) - a disc copied as it is rather
than ripped - can't be read; a rip has had that removed.
"""

from __future__ import annotations

import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path

SECTOR = 2048
FOLDER = "VIDEO_TS"
VMG_IFO = "VIDEO_TS.IFO"
# How much is handed to ffmpeg at a time.
FEED_SECTORS = 256


class DvdError(Exception):
    pass


# --- where it is -------------------------------------------------------------------


def _named(folder: Path, name: str) -> Path | None:
    """`name` in `folder`, whatever its case: a rip made on Windows may be
    video_ts/video_ts.ifo."""
    exact = folder / name
    if exact.exists():
        return exact
    try:
        for child in folder.iterdir():
            if child.name.lower() == name.lower():
                return child
    except OSError:
        pass
    return None


def video_ts(disc_root) -> Path | None:
    """The VIDEO_TS folder of a DVD folder: the one in it, or the folder
    itself when that's what was copied."""
    root = Path(disc_root)
    if root.name.upper() == FOLDER and _named(root, VMG_IFO):
        return root
    inner = _named(root, FOLDER)
    if inner is not None and inner.is_dir() and _named(inner, VMG_IFO):
        return inner
    return None


def is_dvd_root(path) -> bool:
    return video_ts(path) is not None


# --- reading the IFOs --------------------------------------------------------------


def _u8(data: bytes, offset: int) -> int:
    return data[offset]


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def _bcd(byte: int) -> int:
    return (byte >> 4) * 10 + (byte & 0x0F)


def dvd_time(data: bytes, offset: int) -> float:
    """A DVD playback time - hours, minutes, seconds and frames in BCD, the
    frame byte's top bits saying the rate - in seconds."""
    hours, minutes, seconds, frames = data[offset:offset + 4]
    rate = {1: 25.0, 3: 30000 / 1001}.get(frames >> 6, 25.0)
    return (_bcd(hours) * 3600 + _bcd(minutes) * 60 + _bcd(seconds)
            + _bcd(frames & 0x3F) / rate)


@dataclass(frozen=True)
class Cell:
    seconds: float
    first: int  # sectors, from the start of the title set's VOBs
    last: int
    # The second or later angle of a multi-angle stretch: the same moment
    # as the first, so it takes no time of its own.
    other_angle: bool = False


@dataclass
class Pgc:
    cells: list[Cell]
    programs: list[int]  # each program's first cell, 1-based
    audio: list[int | None]  # each audio stream's physical number, or None if absent

    @property
    def seconds(self) -> float:
        return sum(c.seconds for c in self.cells if not c.other_angle)


@dataclass(frozen=True)
class Audio:
    coding: str  # "ac3", "mpeg", "lpcm", "dts" or "other"
    channels: int
    language: str = ""


@dataclass
class TitleSet:
    number: int
    pgcs: list[Pgc]
    ptts: list[list[tuple[int, int]]]  # per title of the set: (pgcn, pgn) per chapter
    audio: list[Audio]
    vobus: list[int] = field(default_factory=list)  # where each VOBU starts


_CODINGS = {0: "ac3", 2: "mpeg", 3: "mpeg", 4: "lpcm", 6: "dts"}


def _table(data: bytes, pointer_offset: int) -> int | None:
    """Where a table a sector pointer at `pointer_offset` points to starts,
    or None when it's absent or past the end."""
    sector = _u32(data, pointer_offset)
    start = sector * SECTOR
    return start if sector and start < len(data) else None


def parse_vmg(data: bytes) -> list[tuple[int, int, int]]:
    """VIDEO_TS.IFO's titles, in order: (title set, title within it,
    chapters)."""
    if not data.startswith(b"DVDVIDEO-VMG"):
        raise DvdError("VIDEO_TS.IFO isn't a DVD's")
    start = _table(data, 0xC4)
    if start is None:
        raise DvdError("VIDEO_TS.IFO lists no titles")
    titles = []
    for i in range(_u16(data, start)):
        entry = start + 8 + 12 * i
        if entry + 12 > len(data):
            break
        titles.append((_u8(data, entry + 6), _u8(data, entry + 7), _u16(data, entry + 2)))
    return titles


def _parse_pgc(data: bytes, start: int) -> Pgc:
    programs_count, cells_count = _u8(data, start + 2), _u8(data, start + 3)
    audio = []
    for i in range(8):
        control = _u16(data, start + 0x0C + 2 * i)
        audio.append((control >> 8) & 0x07 if control & 0x8000 else None)
    program_map = start + _u16(data, start + 0xE6)
    cell_table = start + _u16(data, start + 0xE8)
    programs = [_u8(data, program_map + i) for i in range(programs_count)]
    cells = []
    for i in range(cells_count):
        entry = cell_table + 24 * i
        category = _u8(data, entry)
        angle_block = (category >> 4) & 0x03 == 1
        block_mode = category >> 6
        cells.append(Cell(
            seconds=dvd_time(data, entry + 4),
            first=_u32(data, entry + 8),
            last=_u32(data, entry + 20),
            other_angle=angle_block and block_mode in (2, 3),
        ))
    return Pgc(cells, programs, audio)


def parse_vts(data: bytes, number: int) -> TitleSet:
    if not data.startswith(b"DVDVIDEO-VTS"):
        raise DvdError(f"VTS_{number:02d}_0.IFO isn't a DVD title set's")
    audio = []
    for i in range(min(8, _u16(data, 0x202))):
        entry = 0x204 + 8 * i
        language = data[entry + 2:entry + 4].decode("ascii", "ignore").strip("\x00 ")
        audio.append(Audio(_CODINGS.get(data[entry] >> 5, "other"),
                           (data[entry + 1] & 0x07) + 1, language))

    ptts: list[list[tuple[int, int]]] = []
    start = _table(data, 0xC8)
    if start is not None:
        count = _u16(data, start)
        end = start + _u32(data, start + 4) + 1
        offsets = [start + _u32(data, start + 8 + 4 * i) for i in range(count)] + [end]
        for i in range(count):
            ptts.append([(_u16(data, at), _u16(data, at + 2))
                         for at in range(offsets[i], min(offsets[i + 1], len(data)) - 3, 4)])

    pgcs = []
    start = _table(data, 0xCC)
    if start is not None:
        for i in range(_u16(data, start)):
            pgcs.append(_parse_pgc(data, start + _u32(data, start + 8 + 8 * i + 4)))

    vobus = []
    start = _table(data, 0xE4)
    if start is not None:
        end = min(len(data), start + _u32(data, start) + 1)
        vobus = [_u32(data, at) for at in range(start + 4, end - 3, 4)]
    return TitleSet(number, pgcs, ptts, audio, vobus)


# --- a disc's titles ---------------------------------------------------------------


@dataclass
class Title:
    """One title as played: its cells in order, each with where it starts
    in the title, and its chapters."""

    index: int  # 0-based, as mpv's dvd:// counts them
    vts: int
    duration: float
    cells: list[tuple[float, Cell]]  # (start in the title, cell), angle 1 only
    chapters: list[dict]
    audio: list[dict]  # [{"channels", "coding", "language", "map"}], in the title's order

    def cell_keys(self) -> set:
        return {(self.vts, cell.first, cell.last) for _start, cell in self.cells}


# ffmpeg's id for each kind of DVD audio, as its MPEG demuxer numbers them:
# private stream 1's substreams, and MPEG audio's own stream ids.
_STREAM_IDS = {"ac3": 0x80, "dts": 0x88, "lpcm": 0xA0, "mpeg": 0x1C0}


def _title(index: int, title_set: TitleSet, ttn: int) -> Title | None:
    if not 0 < ttn <= len(title_set.ptts) or not title_set.ptts[ttn - 1]:
        return None
    ptts = title_set.ptts[ttn - 1]
    order = list(dict.fromkeys(pgcn for pgcn, _pgn in ptts))
    cells: list[tuple[float, Cell]] = []
    cell_start: dict[tuple[int, int], float] = {}
    clock = 0.0
    for pgcn in order:
        if not 0 < pgcn <= len(title_set.pgcs):
            continue
        for number, cell in enumerate(title_set.pgcs[pgcn - 1].cells, start=1):
            if cell.other_angle:
                continue
            cell_start[(pgcn, number)] = clock
            cells.append((clock, cell))
            clock += cell.seconds
    duration = clock
    starts = []
    for pgcn, pgn in ptts:
        pgc = title_set.pgcs[pgcn - 1] if 0 < pgcn <= len(title_set.pgcs) else None
        if pgc is None or not 0 < pgn <= len(pgc.programs):
            continue
        first_cell = pgc.programs[pgn - 1]
        # An angle cell's later angles were left out: its first angle stands in.
        while first_cell > 1 and (pgcn, first_cell) not in cell_start:
            first_cell -= 1
        if (pgcn, first_cell) in cell_start:
            starts.append(cell_start[(pgcn, first_cell)])
    starts = sorted(set(starts)) or [0.0]
    if starts[0] > 0.5:
        starts.insert(0, 0.0)
    chapters = [{"start": s, "end": e, "title": None}
                for s, e in zip(starts, [*starts[1:], duration], strict=True)]

    first_pgc = title_set.pgcs[order[0] - 1] if order and order[0] <= len(title_set.pgcs) else None
    audio = []
    for number, attributes in enumerate(title_set.audio):
        physical = first_pgc.audio[number] if first_pgc is not None else number
        if physical is None:
            continue
        base = _STREAM_IDS.get(attributes.coding)
        audio.append({
            "channels": attributes.channels, "coding": attributes.coding,
            "language": attributes.language,
            "map": f"i:{base + physical}" if base is not None else None,
        })
    return Title(index, title_set.number, duration, cells, chapters, audio)


def _read(path: Path, limit: int = 4 * 1024 * 1024) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(limit)


def read_titles(disc_root) -> list[Title]:
    """Every title on the disc, as VIDEO_TS.IFO lists them."""
    folder = video_ts(disc_root)
    if folder is None:
        raise DvdError(f"{disc_root} isn't a DVD folder")
    try:
        entries = parse_vmg(_read(_named(folder, VMG_IFO)))
    except (OSError, struct.error, IndexError) as exc:
        raise DvdError(f"VIDEO_TS.IFO couldn't be read: {exc}") from exc
    sets: dict[int, TitleSet | None] = {}
    titles = []
    for index, (vts, ttn, _chapters) in enumerate(entries):
        if vts not in sets:
            ifo = _named(folder, f"VTS_{vts:02d}_0.IFO")
            try:
                sets[vts] = parse_vts(_read(ifo), vts) if ifo else None
            except (OSError, struct.error, IndexError, DvdError):
                sets[vts] = None
        if sets[vts] is None:
            continue
        try:
            title = _title(index, sets[vts], ttn)
        except (struct.error, IndexError):
            title = None
        if title is not None:
            titles.append(title)
    return titles


def drop_duplicates(titles: list[Title]) -> list[Title]:
    """Titles that play nothing another doesn't, but one: a concert DVD often
    has a title for each song as well as one playing them all, and the
    songs are that one's chapters already. The longest, then the one with
    the most chapters, then the first, is kept."""
    kept: list[Title] = []
    for title in sorted(titles, key=lambda t: (-t.duration, -len(t.chapters), t.index)):
        mine = title.cell_keys()
        if mine and any(mine <= other.cell_keys() for other in kept):
            continue
        kept.append(title)
    return sorted(kept, key=lambda t: t.index)


def probe_dvd_disc(disc_root, min_title_seconds: int = 20) -> list[dict]:
    """The disc's titles worth listing - long enough, and not a copy of
    another's - shaped as bluray.probe_bluray_disc's are:
    {"title_idx", "vts", "duration", "chapters": [{"start", "end", "title"}]}."""
    titles = [t for t in read_titles(disc_root) if t.duration >= min_title_seconds]
    return [{"title_idx": t.index, "vts": t.vts, "duration": t.duration,
             "chapters": [dict(c) for c in t.chapters]}
            for t in drop_duplicates(titles)]


# The titles of discs read this session, so each frame grab or song doesn't
# read the IFOs again: keyed on VIDEO_TS.IFO's size and time.
_cache: dict[tuple, list[Title]] = {}
_cache_lock = threading.Lock()


def title_of(video) -> Title:
    folder = video_ts(video["path"])
    if folder is None:
        raise DvdError(f"{video['path']} isn't there, or isn't a DVD folder")
    ifo = _named(folder, VMG_IFO)
    stat = ifo.stat()
    key = (str(folder), stat.st_size, stat.st_mtime_ns)
    with _cache_lock:
        titles = _cache.get(key)
    if titles is None:
        titles = read_titles(video["path"])
        with _cache_lock:
            _cache[key] = titles
    for title in titles:
        if title.index == video.get("title_idx", 0):
            return title
    raise DvdError("that title is no longer on the disc")


# --- reading the VOBs --------------------------------------------------------------


def _vobs(disc_root, vts: int) -> list[tuple[Path, int, int]]:
    """The title set's VOBs, as (file, first sector, sectors), in order."""
    folder = video_ts(disc_root)
    files, sector = [], 0
    for part in range(1, 10):
        path = _named(folder, f"VTS_{vts:02d}_{part}.VOB") if folder else None
        if path is None:
            break
        count = path.stat().st_size // SECTOR
        files.append((path, sector, count))
        sector += count
    if not files:
        raise DvdError(f"title set {vts}'s VOB files aren't there")
    return files


def read_sectors(disc_root, vts: int, first: int, last: int, stop=None):
    """The bytes of sectors `first` to `last` of a title set's VOBs, in
    pieces, across the files they're cut into."""
    files = _vobs(disc_root, vts)
    at = first
    while at <= last:
        if stop is not None and stop.is_set():
            return
        part = next(((p, s, n) for p, s, n in files if s <= at < s + n), None)
        if part is None:
            return  # past the end of the last file
        path, start, count = part
        upto = min(last, start + count - 1)
        with open(path, "rb") as handle:
            handle.seek((at - start) * SECTOR)
            while at <= upto:
                if stop is not None and stop.is_set():
                    return
                n = min(FEED_SECTORS, upto - at + 1)
                data = handle.read(n * SECTOR)
                if not data:
                    return
                yield data
                at += n


def _nav_elapsed(disc_root, vts: int, sector: int) -> float | None:
    """How far into its cell the VOBU starting at `sector` is, from its
    navigation pack (the PCI's cell elapsed time); None if it isn't one."""
    data = b"".join(read_sectors(disc_root, vts, sector, sector))
    # A pack header, a system header, then the PCI packet: private stream 2.
    if len(data) < 0x50 or data[0x26:0x2A] != b"\x00\x00\x01\xbf" or data[0x2C] != 0:
        return None
    return dvd_time(data, 0x2D + 0x18)


@dataclass(frozen=True)
class Position:
    """Where reading starts for a moment of a title: the sector ranges to
    read, in order, and how far before the moment the first one starts."""

    ranges: tuple[tuple[int, int], ...]
    seconds_before: float


def position(video, seconds: float) -> Position:
    """Where `seconds` into the title is: from the VOBU at or before it, to
    the end of the title."""
    title = title_of(video)
    if not title.cells:
        raise DvdError("this title has no cells")
    seconds = max(0.0, min(seconds, title.duration))
    index = 0
    for i, (start, _cell) in enumerate(title.cells):
        if start <= seconds:
            index = i
    cell_start, cell = title.cells[index]
    into = seconds - cell_start
    sector, before = cell.first, into
    folder = video["path"]
    if into > 0.05:
        title_set = parse_vts(_read(_named(video_ts(folder), f"VTS_{title.vts:02d}_0.IFO")),
                              title.vts)
        vobus = [s for s in title_set.vobus if cell.first <= s <= cell.last]
        # The last VOBU starting at or before the moment, by halving: each
        # look reads one sector.
        low, high, found = 0, len(vobus) - 1, None
        while low <= high:
            middle = (low + high) // 2
            elapsed = _nav_elapsed(folder, title.vts, vobus[middle])
            if elapsed is None or (elapsed == 0 and vobus[middle] != cell.first):
                # Not a navigation pack, or one nobody filled in: no telling.
                found = None
                break
            if elapsed <= into + 0.001:
                found, low = (vobus[middle], elapsed), middle + 1
            else:
                high = middle - 1
        if found is not None:
            sector, before = found[0], max(0.0, into - found[1])
        elif cell.seconds > 0:
            # No map to go by: as far through the cell's sectors as through
            # its time, less a second's worth for the VOBU to start in.
            share = max(0.0, (into - 1.0) / cell.seconds)
            sector = cell.first + int((cell.last - cell.first) * share)
            before = into - share * cell.seconds
    ranges = [(sector, cell.last)] + [(c.first, c.last) for _s, c in title.cells[index + 1:]]
    return Position(tuple(ranges), before)


def feeder(video, where: Position):
    """What writes the title, from `where` on, into ffmpeg's input."""
    disc, vts = video["path"], title_of(video).vts

    def feed(stream, stop) -> None:
        for first, last in where.ranges:
            for chunk in read_sectors(disc, vts, first, last, stop):
                stream.write(chunk)

    return feed


def ffmpeg_source(video, seconds: float, where: Position | None = None):
    """(ffmpeg's arguments, feed) for reading the title from `seconds`. The
    arguments end with how much to decode past, which is an option of the
    output: they go last among the inputs."""
    where = where or position(video, seconds)
    args = ["-f", "mpeg", "-i", "pipe:0"]
    if where.seconds_before > 0.02:
        args += ["-ss", f"{where.seconds_before:.3f}"]
    return args, feeder(video, where)


def audio_streams(video) -> list[dict]:
    """The title's audio tracks, as audio_export wants them."""
    try:
        return [dict(a, sample_fmt="") for a in title_of(video).audio if a["map"]]
    except (DvdError, OSError):
        return []


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
