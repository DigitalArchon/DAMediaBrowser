# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""DVD folders for tests, made without an authoring tool.

ffmpeg's DVD target writes a VOB with an empty navigation pack at the start
of each VOBU. This fills each one in with how far into its cell it is (from
the pack's clock), cuts the VOB into cells at the chapters asked for, and
writes VIDEO_TS.IFO and VTS_01_0.IFO around it, laid out as the DVD-Video
spec has them: the title table, the chapters, the program chains and their
cells, and the VOBU map - the parts core.dvd reads.

Titles are given as the chapters each plays: [[0, 4, 8]] is one title of
three chapters starting at 0, 4 and 8 seconds of the video. A title may
take only some of the cells (a song on its own), which is how a concert
DVD's per-song titles look.
"""

from __future__ import annotations

import struct
import subprocess
from pathlib import Path

SECTOR = 2048
FPS = 25
_MPG_CACHE: dict[tuple, bytes] = {}


def _bcd(value: int) -> int:
    return ((value // 10) << 4) | (value % 10)


def dvd_time(seconds: float) -> bytes:
    """A DVD playback time at 25 frames a second."""
    frames_total = int(round(seconds * FPS))
    whole, frames = divmod(frames_total, FPS)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return bytes([_bcd(hours), _bcd(minutes), _bcd(secs), 0x40 | _bcd(frames)])


def _video_pts(data: bytes, sector: int) -> float | None:
    """The presentation time of the first video packet from `sector` on:
    a VOBU's first picture, which is when it starts."""
    for number in range(sector + 1, min(sector + 64, len(data) // SECTOR)):
        pack = data[number * SECTOR:(number + 1) * SECTOR]
        at = pack.find(b"\x00\x00\x01\xe0")
        if at < 0 or not pack[at + 7] & 0x80:
            continue
        b = pack[at + 9:at + 14]
        return ((((b[0] >> 1) & 7) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15)
                | (b[3] << 7) | (b[4] >> 1)) / 90000.0
    return None


def make_mpg(seconds: float, brightness_by_frame: bool = True, channels: int = 2) -> bytes:
    """A PAL DVD program stream. With `brightness_by_frame`, each frame's
    brightness says its number (mod 50), so a frame grab can be checked."""
    key = (seconds, brightness_by_frame, channels)
    if key not in _MPG_CACHE:
        video = ("nullsrc=size=720x576:rate=25:duration={d},geq=lum='16+mod(N\\,50)*4':"
                 "cb=128:cr=128" if brightness_by_frame
                 else "testsrc=size=720x576:rate=25:duration={d}").format(d=seconds)
        _MPG_CACHE[key] = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", video,
             "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
             "-target", "pal-dvd", "-b:v", "800k", "-ac", str(channels),
             "-f", "dvd", "-"],
            check=True, capture_output=True,
        ).stdout
    return _MPG_CACHE[key]


def _vobus(data: bytes) -> list[tuple[int, float]]:
    """(sector, time from the start) of each VOBU's navigation pack."""
    found, origin = [], None
    for number in range(len(data) // SECTOR):
        sector = data[number * SECTOR:(number + 1) * SECTOR]
        if sector[0x26:0x2A] == b"\x00\x00\x01\xbf" and sector[0x2C] == 0:
            pts = _video_pts(data, number)
            if pts is None:
                continue
            origin = pts if origin is None else origin
            found.append((number, pts - origin))
    return found


def build(root, titles=((0.0, 4.0, 8.0),), seconds: float = 12.0, *, channels: int = 2,
          lowercase: bool = False, brightness_by_frame: bool = True) -> Path:
    """A DVD folder at `root` (made), returning it."""
    data = bytearray(make_mpg(seconds, brightness_by_frame, channels))
    vobus = _vobus(bytes(data))
    total = len(data) // SECTOR

    # Cells: cut at every chapter start of every title.
    cuts = sorted({0.0, *(t for title in titles for t in title)})
    starts = []
    for cut in cuts:
        sector = next((s for s, t in vobus if t >= cut - 0.001), vobus[-1][0])
        starts.append((sector, next(t for s, t in vobus if s == sector)))
    starts = sorted(set(starts))
    cells = []
    for i, (first, start_time) in enumerate(starts):
        last = starts[i + 1][0] - 1 if i + 1 < len(starts) else total - 1
        end_time = starts[i + 1][1] if i + 1 < len(starts) else seconds
        cells.append((first, last, start_time, end_time - start_time))
    # Each VOBU's elapsed time within its cell, written into its NAV pack.
    for sector, t in vobus:
        cell = max((c for c in cells if c[0] <= sector), key=lambda c: c[0])
        at = sector * SECTOR + 0x2D + 0x18
        data[at:at + 4] = dvd_time(t - cell[2])

    folder = Path(root) / ("video_ts" if lowercase else "VIDEO_TS")
    folder.mkdir(parents=True, exist_ok=True)
    name = (lambda n: n.lower()) if lowercase else (lambda n: n)
    (folder / name("VTS_01_1.VOB")).write_bytes(bytes(data))

    # Which cells each title plays, and where each of its chapters starts:
    # the first title all of them; any other from its first chapter to the
    # cut after its last.
    plans = []
    for k, title in enumerate(titles):
        begin = 0.0 if k == 0 else title[0]
        end = float("inf") if k == 0 else _next_cut(cuts, max(title))
        cell_numbers = [i + 1 for i, c in enumerate(cells)
                        if begin - 0.001 <= c[2] and c[2] < end - 0.001]
        programs = [next(i + 1 for i, c in enumerate(cells) if c[2] >= t - 0.001)
                    for t in title]
        plans.append((cell_numbers, programs))

    (folder / name("VIDEO_TS.IFO")).write_bytes(_vmg(titles))
    (folder / name("VTS_01_0.IFO")).write_bytes(_vts(plans, cells, [s for s, _t in vobus],
                                                     channels))
    return Path(root)


def _next_cut(cuts, t: float) -> float:
    later = [c for c in cuts if c > t + 0.001]
    return later[0] if later else float("inf")


def _pad(data: bytes, sectors: int | None = None) -> bytes:
    size = sectors * SECTOR if sectors else -(-len(data) // SECTOR) * SECTOR
    return data.ljust(size, b"\x00")


def _vmg(titles) -> bytes:
    header = bytearray(SECTOR)
    header[0:12] = b"DVDVIDEO-VMG"
    struct.pack_into(">I", header, 0xC4, 1)  # TT_SRPT in sector 1
    table = bytearray(struct.pack(">HHI", len(titles), 0, 8 + 12 * len(titles) - 1))
    for number, chapters in enumerate(titles, start=1):
        table += struct.pack(">BBHHBBI", 0x3C, 1, len(chapters), 0, 1, number, 0)
    return bytes(header) + _pad(bytes(table))


def _vts(plans, cells, vobu_sectors, channels: int) -> bytes:
    header = bytearray(SECTOR)
    header[0:12] = b"DVDVIDEO-VTS"
    struct.pack_into(">H", header, 0x202, 1)  # one audio stream
    header[0x204] = 0x00  # AC-3
    header[0x205] = channels - 1
    header[0x206:0x208] = b"en"

    # Chapters: one program chain per title, each chapter a program.
    ptt = bytearray(struct.pack(">HHI", len(plans), 0, 0))
    offsets_at = len(ptt)
    ptt += bytes(4 * len(plans))
    for number, (_cells, programs) in enumerate(plans, start=1):
        struct.pack_into(">I", ptt, offsets_at + 4 * (number - 1), len(ptt))
        for program in range(1, len(programs) + 1):
            ptt += struct.pack(">HH", number, program)
    struct.pack_into(">I", ptt, 4, len(ptt) - 1)

    pgcit = bytearray(struct.pack(">HHI", len(plans), 0, 0))
    entries_at = len(pgcit)
    pgcit += bytes(8 * len(plans))
    for number, (cell_numbers, programs) in enumerate(plans, start=1):
        struct.pack_into(">II", pgcit, entries_at + 8 * (number - 1), 0x81000000 | number << 24,
                         len(pgcit))
        pgc = bytearray(0xEC)
        pgc[2], pgc[3] = len(programs), len(cell_numbers)
        pgc[4:8] = dvd_time(sum(cells[n - 1][3] for n in cell_numbers))
        struct.pack_into(">H", pgc, 0x0C, 0x8000)  # audio stream 1 is physical stream 0
        struct.pack_into(">H", pgc, 0xE6, len(pgc))
        first_cell = cell_numbers[0]
        pgc += bytes(p - first_cell + 1 for p in programs)
        pgc += bytes(-len(pgc) % 2)
        struct.pack_into(">H", pgc, 0xE8, len(pgc))
        for n in cell_numbers:
            first, last, _start, length = cells[n - 1]
            pgc += bytes([0, 0, 0, 0]) + dvd_time(length) + struct.pack(">IIII", first, 0, last,
                                                                      last)
        pgcit += pgc
    struct.pack_into(">I", pgcit, 4, len(pgcit) - 1)

    admap = struct.pack(">I", 4 * len(vobu_sectors) + 3) + b"".join(
        struct.pack(">I", s) for s in vobu_sectors)

    parts = [_pad(bytes(ptt)), _pad(bytes(pgcit)), _pad(admap)]
    sector = 1
    for pointer, part in zip((0xC8, 0xCC, 0xE4), parts, strict=True):
        struct.pack_into(">I", header, pointer, sector)
        sector += len(part) // SECTOR
    return bytes(header) + b"".join(parts)
