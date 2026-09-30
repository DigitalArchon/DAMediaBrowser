# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""A Blu-ray's own menus, decoded.

A disc's scene-selection menu is the one place its chapters are named by
the people who made it, and every button on it says exactly which chapter
it plays. On an HDMV disc (most concert discs; the rest are BD-J, which is
Java) the menu isn't a picture to screenshot: it is an Interactive
Graphics stream - stream type 0x91, PID 0x1400 onward, which ffprobe
mislabels as mp3 - inside one of the disc's clips, holding:

- palettes (PDS) and bitmaps (ODS, run-length encoded, 8-bit indexed);
- an interactive composition (ICS): pages, each drawing some bitmaps as
  its background and a set of buttons, each button a bitmap per state plus
  the navigation commands it runs when pressed.

This module reads all of that straight from the .m2ts, renders pages and
buttons to RGBA, and parses the commands. Working out which chapter a
button plays - commands often set a register and jump to a movie object
that does the playing - is hdmv_vm's business.

Decoding needs nothing but the files; pictures need ffmpeg.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import bluray

TS_PACKET = 192  # a Blu-ray transport packet: 4-byte arrival stamp + 188
IG_PIDS = range(0x1400, 0x1420)

SEG_PDS = 0x14
SEG_ODS = 0x15
SEG_ICS = 0x18
SEG_END = 0x80

# How much of a clip to read looking for its menu. A menu clip is a few MB;
# a pop-up menu multiplexed into the main feature starts at its beginning.
MAX_SCAN_BYTES = 48 * 1024 * 1024


# --- the transport stream ------------------------------------------------------


def ig_segments(path, max_bytes: int = MAX_SCAN_BYTES) -> dict[int, list[tuple[int, bytes]]]:
    """Every Interactive Graphics segment in the first `max_bytes` of a
    clip: {pid: [(segment_type, payload), ...]} in stream order."""
    pes: dict[int, bytearray] = {}
    segments: dict[int, list[tuple[int, bytes]]] = {}

    def flush(pid):
        data = pes.pop(pid, None)
        if not data or len(data) < 9 or data[:3] != b"\x00\x00\x01":
            return
        start = 9 + data[8]
        _split_segments(bytes(data[start:]), segments.setdefault(pid, []))

    read = 0
    with open(path, "rb") as f:
        while read < max_bytes:
            chunk = f.read(TS_PACKET * 4096)
            if not chunk:
                break
            read += len(chunk)
            for off in range(0, len(chunk) - TS_PACKET + 1, TS_PACKET):
                ts = off + 4
                if chunk[ts] != 0x47:
                    continue
                pid = ((chunk[ts + 1] & 0x1F) << 8) | chunk[ts + 2]
                if pid not in IG_PIDS:
                    continue
                afc = (chunk[ts + 3] >> 4) & 3
                if not afc & 1:
                    continue
                start = ts + 4
                if afc & 2:
                    start += 1 + chunk[ts + 4]
                end = off + TS_PACKET
                if chunk[ts + 1] & 0x40:  # payload unit start: a new PES
                    flush(pid)
                    pes[pid] = bytearray()
                if pid in pes and start < end:
                    pes[pid] += chunk[start:end]
    for pid in list(pes):
        flush(pid)
    return segments


def _split_segments(data: bytes, out: list) -> None:
    i = 0
    while i + 3 <= len(data):
        kind = data[i]
        length = (data[i + 1] << 8) | data[i + 2]
        if i + 3 + length > len(data):
            break
        out.append((kind, data[i + 3:i + 3 + length]))
        i += 3 + length


# --- the pieces ------------------------------------------------------------------


class _Bits:
    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos * 8

    def read(self, n: int) -> int:
        value = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3] if (self.pos >> 3) < len(self.data) else 0
            value = (value << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return value

    def u8(self) -> int:
        return self.read(8)

    def u16(self) -> int:
        return self.read(16)

    def skip(self, n: int) -> None:
        self.pos += n

    def bytes(self, n: int) -> bytes:
        start = self.pos >> 3
        self.pos += 8 * n
        return self.data[start:start + n]


@dataclass
class Palette:
    id: int
    # index -> (r, g, b, a)
    colours: dict[int, tuple[int, int, int, int]] = field(default_factory=dict)


@dataclass
class Bitmap:
    id: int
    width: int
    height: int
    pixels: bytes  # palette indices, row by row


@dataclass(frozen=True)
class Command:
    """One HDMV navigation command, as its 12 bytes say."""

    op_cnt: int
    group: int  # 0 branch, 1 compare, 2 set
    sub_group: int
    imm_dst: bool
    imm_src: bool
    branch_opt: int
    cmp_opt: int
    set_opt: int
    dst: int
    src: int

    @classmethod
    def parse(cls, raw: bytes) -> Command:
        b = _Bits(raw)
        op_cnt, group, sub_group = b.read(3), b.read(2), b.read(3)
        imm_dst, imm_src = bool(b.read(1)), bool(b.read(1))
        b.skip(2)
        branch_opt = b.read(4)
        b.skip(4)
        cmp_opt = b.read(4)
        b.skip(3)
        set_opt = b.read(5)
        return cls(op_cnt, group, sub_group, imm_dst, imm_src, branch_opt, cmp_opt, set_opt,
                   b.read(32), b.read(32))


@dataclass(frozen=True)
class Placement:
    """A bitmap drawn somewhere on a page."""

    object_id: int
    x: int
    y: int
    crop: tuple[int, int, int, int] | None = None  # x, y, w, h within the bitmap


@dataclass
class Button:
    id: int
    numeric: int
    auto_action: bool
    x: int
    y: int
    normal: tuple[int, int]  # first and last object id of the state's animation
    selected: tuple[int, int]
    activated: tuple[int, int]
    commands: list[Command]


@dataclass
class Page:
    id: int
    palette_id: int
    default_button: int
    background: list[Placement]  # what the page's in-effects draw
    groups: list[list[Button]]  # button overlap groups; one of each shows

    @property
    def buttons(self) -> list[Button]:
        return [button for group in self.groups for button in group]


@dataclass
class Menu:
    """One Interactive Graphics stream's menu: what a page needs to be drawn,
    and its pages."""

    width: int
    height: int
    popup: bool  # a pop-up menu over the feature, rather than a menu screen
    pages: list[Page]
    palettes: dict[int, Palette]
    bitmaps: dict[int, Bitmap]


# --- decoding --------------------------------------------------------------------


def _ycc_to_rgba(y: int, cr: int, cb: int, alpha: int) -> tuple[int, int, int, int]:
    # BT.709, limited range: how HD Blu-ray graphics are coded.
    y = 1.164 * (y - 16)
    r = y + 1.793 * (cr - 128)
    g = y - 0.213 * (cb - 128) - 0.533 * (cr - 128)
    b = y + 2.112 * (cb - 128)
    return (max(0, min(255, round(r))), max(0, min(255, round(g))),
            max(0, min(255, round(b))), alpha)


def decode_palette(data: bytes) -> Palette:
    palette = Palette(id=data[0])
    for i in range(2, len(data) - 4, 5):
        index, y, cr, cb, alpha = data[i:i + 5]
        palette.colours[index] = _ycc_to_rgba(y, cr, cb, alpha)
    return palette


def decode_rle(data: bytes, width: int, height: int) -> bytes:
    """Blu-ray graphics run-length coding (as PGS subtitles), to one palette
    index per pixel. A short or damaged stream is padded, not trusted."""
    out = bytearray()
    row = bytearray()
    i, n = 0, len(data)
    while i < n and len(out) < width * height:
        byte = data[i]
        i += 1
        if byte:
            row.append(byte)
            continue
        if i >= n:
            break
        flag = data[i]
        i += 1
        if flag == 0:  # end of line
            out += row[:width].ljust(width, b"\x00")
            row = bytearray()
            continue
        count = flag & 0x3F
        if flag & 0x40:
            if i >= n:
                break
            count = (count << 8) | data[i]
            i += 1
        colour = 0
        if flag & 0x80:
            if i >= n:
                break
            colour = data[i]
            i += 1
        row += bytes([colour]) * count
    if row:
        out += row[:width].ljust(width, b"\x00")
    return bytes(out[:width * height].ljust(width * height, b"\x00"))


def _placement(b: _Bits) -> Placement:
    object_id = b.u16()
    b.u8()  # window
    cropped = b.read(1)
    b.skip(7)
    x, y = b.u16(), b.u16()
    crop = (b.u16(), b.u16(), b.u16(), b.u16()) if cropped else None
    return Placement(object_id, x, y, crop)


def _effects(b: _Bits) -> list[Placement]:
    """An effect sequence, as the placements of its first effect: what the
    page looks like once it has appeared."""
    for _ in range(b.u8()):  # windows
        b.skip(8 + 16 * 4)
    placements: list[Placement] = []
    for index in range(b.u8()):
        b.skip(24)  # duration
        b.u8()  # palette
        drawn = [_placement(b) for _ in range(b.u8())]
        if index == 0:
            placements = drawn
    return placements


def _button(b: _Bits) -> Button:
    button_id, numeric = b.u16(), b.u16()
    auto_action = bool(b.read(1))
    b.skip(7)
    x, y = b.u16(), b.u16()
    b.skip(16 * 4)  # neighbours
    normal = (b.u16(), b.u16())
    b.skip(8)
    b.u8()  # sound
    selected = (b.u16(), b.u16())
    b.skip(8)
    b.u8()  # sound
    activated = (b.u16(), b.u16())
    commands = [Command.parse(b.bytes(12)) for _ in range(b.u16())]
    return Button(button_id, numeric, auto_action, x, y, normal, selected, activated, commands)


def _page(b: _Bits) -> Page:
    page_id = b.u8()
    b.u8()  # version
    b.skip(64)  # UO mask
    background = _effects(b)
    _effects(b)  # out effects
    b.u8()  # animation frame rate
    default_button = b.u16()
    b.u16()  # default activated
    palette_id = b.u8()
    groups = []
    for _ in range(b.u8()):
        b.u16()  # default valid button
        groups.append([_button(b) for _ in range(b.u8())])
    return Page(page_id, palette_id, default_button, background, groups)


def decode_composition(data: bytes) -> tuple[int, int, bool, list[Page]]:
    """An ICS's composition, joined from its fragments: (width, height,
    popup, pages)."""
    b = _Bits(data)
    width, height = b.u16(), b.u16()
    b.u8()  # frame rate
    b.skip(24)  # composition number and state
    b.u8()  # sequence flags
    b.skip(24)  # data length
    stream_model = b.read(1)
    ui_model = b.read(1)
    b.skip(6)
    if stream_model == 0:
        b.skip(80)  # composition and selection time-outs
    b.skip(24)  # user time-out
    pages = [_page(b) for _ in range(b.u8())]
    return width, height, bool(ui_model), pages


def decode_menu(segments: list[tuple[int, bytes]]) -> Menu | None:
    """The last complete menu in a stream's segments, or None."""
    palettes: dict[int, Palette] = {}
    bitmaps: dict[int, Bitmap] = {}
    pending: dict[int, tuple[int, int, bytearray]] = {}
    composition = bytearray()
    menu = None
    for kind, data in segments:
        if kind == SEG_PDS and data:
            palette = decode_palette(data)
            palettes[palette.id] = palette
        elif kind == SEG_ODS and len(data) >= 4:
            object_id, flags = (data[0] << 8) | data[1], data[3]
            if flags & 0x80 and len(data) >= 11:
                width = (data[7] << 8) | data[8]
                height = (data[9] << 8) | data[10]
                pending[object_id] = (width, height, bytearray(data[11:]))
            elif object_id in pending:
                pending[object_id][2].extend(data[4:])
            if flags & 0x40 and object_id in pending:
                width, height, rle = pending.pop(object_id)
                bitmaps[object_id] = Bitmap(object_id, width, height,
                                            decode_rle(bytes(rle), width, height))
        elif kind == SEG_ICS and len(data) >= 9:
            if data[8] & 0x80:  # first in sequence
                composition = bytearray(data)
            else:
                composition.extend(data[9:])
            if data[8] & 0x40:  # last
                try:
                    width, height, popup, pages = decode_composition(bytes(composition))
                except IndexError:
                    continue
                menu = Menu(width, height, popup, pages, palettes, bitmaps)
    if menu is not None:
        # Bitmaps and palettes can follow the composition in its display set.
        menu.palettes, menu.bitmaps = palettes, bitmaps
    return menu


def read_menus(path, max_bytes: int = MAX_SCAN_BYTES) -> list[Menu]:
    """Every menu in a clip, one per Interactive Graphics stream."""
    menus = []
    for _pid, segments in sorted(ig_segments(path, max_bytes).items()):
        menu = decode_menu(segments)
        if menu is not None and menu.pages:
            menus.append(menu)
    return menus


# --- drawing -------------------------------------------------------------------


_OPAQUE_RUN = re.compile(rb"[^\x00]+")


def _draw(canvas: bytearray, width: int, height: int, bitmap: Bitmap, x: int, y: int,
          opaque: bytes, crop=None) -> None:
    """Draw `bitmap`'s indices onto `canvas` at x, y. `opaque` maps each
    index to 1 if its colour shows and 0 if it's transparent, which leaves
    what's underneath: rows are copied a run of opaque pixels at a time."""
    cx, cy, cw, ch = crop or (0, 0, bitmap.width, bitmap.height)
    for row in range(ch):
        ty = y + row
        sy = cy + row
        if not 0 <= ty < height or sy >= bitmap.height:
            continue
        line = bitmap.pixels[sy * bitmap.width + cx: sy * bitmap.width + cx + cw]
        tx = x
        if tx < 0:
            line, tx = line[-tx:], 0
        line = line[:max(0, width - tx)]
        base = ty * width + tx
        for run in _OPAQUE_RUN.finditer(line.translate(opaque)):
            canvas[base + run.start():base + run.end()] = line[run.start():run.end()]


def render(menu: Menu, page: Page, selected: Button | None = None) -> bytes:
    """A page as RGBA, `menu.width` x `menu.height`, as a player shows it:
    its background, one button of each group in its normal state, and
    `selected` (if given) highlighted instead."""
    palette = menu.palettes.get(page.palette_id) or next(iter(menu.palettes.values()), None)
    colours = palette.colours if palette else {}
    opaque = bytes(1 if colours.get(i, (0, 0, 0, 0))[3] else 0 for i in range(256))
    clear = opaque.find(b"\x00")
    canvas = bytearray([max(0, clear)]) * (menu.width * menu.height)
    for placed in page.background:
        bitmap = menu.bitmaps.get(placed.object_id)
        if bitmap:
            _draw(canvas, menu.width, menu.height, bitmap, placed.x, placed.y,
                  opaque, placed.crop)
    for group in page.groups:
        for button in _shown(group, selected):
            state = button.selected if button is selected else button.normal
            bitmap = menu.bitmaps.get(state[0])
            if bitmap:
                _draw(canvas, menu.width, menu.height, bitmap, button.x, button.y, opaque)
    rgba = bytearray(len(canvas) * 4)
    for channel in range(4):
        table = bytes(colours.get(i, (0, 0, 0, 0))[channel] for i in range(256))
        rgba[channel::4] = canvas.translate(table)
    return bytes(rgba)


def _shown(group: list[Button], selected: Button | None) -> list[Button]:
    """The buttons of a group a player draws. Buttons in a group overlap
    (the same place, different looks); one shows at a time. A group of
    buttons that don't overlap is drawn whole."""
    if selected in group:
        return [selected]
    positions = {(b.x, b.y) for b in group}
    if len(positions) == len(group):
        return group
    return group[:1]


def button_box(menu: Menu, button: Button) -> tuple[int, int, int, int] | None:
    """Where a button is drawn: x, y, width, height of its largest state."""
    sizes = [
        menu.bitmaps[object_id] for object_id in
        (button.normal[0], button.selected[0], button.activated[0])
        if object_id in menu.bitmaps
    ]
    if not sizes:
        return None
    return (button.x, button.y, max(b.width for b in sizes), max(b.height for b in sizes))


# --- pictures ------------------------------------------------------------------

# How long ffmpeg may take to compose one picture of a menu.
PICTURE_TIMEOUT_SECONDS = 60


def clip_seconds(path) -> float | None:
    """How long a clip is, by ffprobe."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=PICTURE_TIMEOUT_SECONDS,
        )
        return float(proc.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def background_time(path) -> float:
    """When to take a menu clip's background: halfway, since a menu often
    fades in and some clips are only a second long."""
    seconds = clip_seconds(path)
    return max(0.0, seconds / 2) if seconds else 0.0


def background_frame(clip, width: int, height: int, at_seconds: float = 0.0) -> bytes | None:
    """A frame of a menu's background clip as raw RGB, `width` x `height`:
    from `at_seconds` in, or from the start if that finds nothing - a still
    menu is a single frame any seek misses, and a seek into a moving one
    can land where no frame decodes."""
    seeks = ([at_seconds] if at_seconds >= 0.25 else []) + [None]
    for seek in seeks:
        try:
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-nostdin",
                 *(["-ss", f"{seek:.2f}"] if seek is not None else []), "-i", str(clip),
                 "-map", "0:v:0", "-frames:v", "1", "-vf", f"scale={width}:{height},setsar=1",
                 "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                capture_output=True, timeout=PICTURE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if len(proc.stdout) == width * height * 3:
            return proc.stdout
    return None


def picture(menu: Menu, rgba: bytes, background: bytes | None = None, crop=None,
            width=None) -> bytes | None:
    """A JPEG of a rendered page over its background - raw RGB from
    background_frame for a menu screen (the text is often in that video,
    not the graphics), or plain dark grey for a pop-up. `crop` is (x, y, w,
    h) in the menu's coordinates; `width` scales the result."""
    size = f"{menu.width}x{menu.height}"
    chain = ["[0:v][1:v]overlay=format=auto"]
    if crop is not None:
        x, y, w, h = crop
        chain[-1] += f",crop={w}:{h}:{x}:{y}"
    if width:
        chain[-1] += f",scale={width}:-2"
    handle = None
    try:
        if background is not None and len(background) == menu.width * menu.height * 3:
            # Two raw inputs and one stdin: the background goes by file.
            handle = tempfile.NamedTemporaryFile(suffix=".rgb", delete=False)
            handle.write(background)
            handle.close()
            first = ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", size, "-i", handle.name]
        else:
            first = ["-f", "lavfi", "-i", f"color=c=0x202020:s={size}:d=1"]
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", *first,
             "-f", "rawvideo", "-pix_fmt", "rgba", "-s", size, "-i", "pipe:0",
             "-filter_complex", ";".join(chain), "-frames:v", "1",
             "-q:v", "3", "-f", "image2pipe", "-c:v", "mjpeg", "-"],
            input=rgba, capture_output=True, timeout=PICTURE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        if handle is not None:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
    if proc.returncode != 0 or not proc.stdout.startswith(b"\xff\xd8"):
        return None
    return proc.stdout


def has_video(path) -> bool:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=PICTURE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(proc.stdout.strip())


def background_clip(disc_root, clip: Path) -> Path | None:
    """The clip whose picture a menu screen is drawn over: the menu's own
    clip if it has video, or else the one it shares a playlist with (a
    still image in a clip of its own, as many discs do)."""
    if has_video(clip):
        return clip
    stem = clip.stem
    for names in bluray.playlist_clips(disc_root).values():
        if stem not in names:
            continue
        for name in names:
            candidate = clip.with_name(f"{name}{clip.suffix}")
            if name != stem and candidate.exists() and has_video(candidate):
                return candidate
    return None


# In a clip's CLPI, a stream entry: its PID, then its coding info's length
# and coding type. 0x91 is Interactive Graphics.
_IG_STREAM = re.compile(rb"\x14[\x00-\x1f].\x91", re.S)

# Without clip information to go by, clips bigger than this aren't read:
# a whole disc's features over a network share would take minutes.
UNLISTED_CLIP_LIMIT = 256 * 1024 * 1024


def clips_with_menus(disc_root) -> list[Path]:
    """The disc's clips that carry a menu, by what their clip information
    (BDMV/CLIPINF) declares - which costs a few hundred bytes a clip,
    where finding out from the clips themselves could mean reading
    gigabytes. A disc whose clip information says nothing has its small
    clips read instead."""
    bdmv = Path(disc_root) / "BDMV"
    try:
        clips = sorted(p for p in (bdmv / "STREAM").iterdir() if p.suffix.lower() == ".m2ts")
    except OSError:
        return []
    listed = []
    for clip in clips:
        info = bdmv / "CLIPINF" / f"{clip.stem}.clpi"
        try:
            if _IG_STREAM.search(info.read_bytes()):
                listed.append(clip)
        except OSError:
            continue
    if listed:
        return listed
    small = []
    for clip in clips:
        try:
            if clip.stat().st_size <= UNLISTED_CLIP_LIMIT:
                small.append(clip)
        except OSError:
            continue
    return small
