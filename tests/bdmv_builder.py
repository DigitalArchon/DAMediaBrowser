# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Builds the parts of a Blu-ray the menu reader reads, byte for byte:
Interactive Graphics segments in 192-byte transport packets, index.bdmv,
MovieObject.bdmv, CLPI and MPLS files. Only what the tests need, but in
the real formats, so the tests read what a disc holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# --- commands -----------------------------------------------------------------------


def command(group, sub_group, *, branch=0, cmp=0, set_op=0, dst=0, src=0,
            imm_dst=False, imm_src=False, op_cnt=2) -> bytes:
    insn = (
        (op_cnt << 29) | (group << 27) | (sub_group << 24)
        | (int(imm_dst) << 23) | (int(imm_src) << 22)
        | (branch << 16) | (cmp << 8) | set_op
    )
    return insn.to_bytes(4, "big") + dst.to_bytes(4, "big") + src.to_bytes(4, "big")


def PSR(n):  # noqa: N802 - reads like the register it names
    return 0x80000000 | n


def move(reg, value, imm=True):
    return command(2, 0, set_op=1, dst=reg, src=value, imm_src=imm)


def add(reg, value, imm=True):
    return command(2, 0, set_op=3, dst=reg, src=value, imm_src=imm)


def sub(reg, value, imm=True):
    return command(2, 0, set_op=4, dst=reg, src=value, imm_src=imm)


def if_eq(reg, value, imm=True):
    return command(1, 0, cmp=2, dst=reg, src=value, imm_src=imm)


def goto(line):
    return command(0, 0, branch=1, dst=line, imm_dst=True)


def jump_title(n):
    return command(0, 1, branch=1, dst=n, imm_dst=True)


def jump_object(n):
    return command(0, 1, branch=0, dst=n, imm_dst=True)


def play_pl(playlist):
    return command(0, 2, branch=0, dst=playlist, imm_dst=True)


def play_pl_at_mark(playlist, mark, imm_mark=True):
    return command(0, 2, branch=2, dst=playlist, src=mark, imm_dst=True, imm_src=imm_mark)


def link_mark(mark, imm=True):
    return command(0, 2, branch=5, dst=mark, imm_dst=imm)


def set_button_page(button=None, page=None):
    dst = 0x80000000 | button if button is not None else 0
    src = 0x80000000 | page if page is not None else 0
    return command(2, 1, set_op=3, dst=dst, src=src, imm_dst=True, imm_src=True)


# --- graphics -----------------------------------------------------------------------


def rle(pixels: list[list[int]]) -> bytes:
    """Blu-ray run-length coding of rows of palette indices."""
    out = bytearray()
    for row in pixels:
        i = 0
        while i < len(row):
            colour = row[i]
            run = 1
            while i + run < len(row) and row[i + run] == colour and run < 16383:
                run += 1
            if colour and run < 3:
                out += bytes([colour]) * run
            elif colour == 0:
                out += bytes([0, run]) if run < 64 else bytes([0, 0x40 | run >> 8, run & 0xFF])
            else:
                out += (bytes([0, 0x80 | run, colour]) if run < 64
                        else bytes([0, 0xC0 | run >> 8, run & 0xFF, colour]))
            i += run
        out += b"\x00\x00"
    return bytes(out)


def segment(kind: int, payload: bytes) -> bytes:
    return bytes([kind]) + len(payload).to_bytes(2, "big") + payload


def palette(palette_id: int, colours: dict[int, tuple[int, int, int, int]]) -> bytes:
    """A PDS; colours are index -> (Y, Cr, Cb, alpha)."""
    body = bytes([palette_id, 0])
    for index, (y, cr, cb, alpha) in colours.items():
        body += bytes([index, y, cr, cb, alpha])
    return segment(0x14, body)


def bitmap(object_id: int, pixels: list[list[int]]) -> bytes:
    """An ODS holding a whole object."""
    height, width = len(pixels), len(pixels[0])
    data = rle(pixels)
    body = (object_id.to_bytes(2, "big") + b"\x00\xc0"
            + (len(data) + 4).to_bytes(3, "big")
            + width.to_bytes(2, "big") + height.to_bytes(2, "big") + data)
    return segment(0x15, body)


@dataclass
class B:
    """A button for a page."""

    id: int
    x: int
    y: int
    normal: int = 0xFFFF
    selected: int = 0xFFFF
    commands: list[bytes] = field(default_factory=list)
    auto: bool = False
    numeric: int = 0xFFFF


def _button(b: B) -> bytes:
    out = (b.id.to_bytes(2, "big") + b.numeric.to_bytes(2, "big")
           + bytes([0x80 if b.auto else 0]) + b.x.to_bytes(2, "big") + b.y.to_bytes(2, "big")
           + b"\xff\xff" * 4)
    out += b.normal.to_bytes(2, "big") * 2 + b"\x00"
    out += b"\xff" + b.selected.to_bytes(2, "big") * 2 + b"\x00"
    out += b"\xff" + b"\xff\xff" * 2
    out += len(b.commands).to_bytes(2, "big") + b"".join(b.commands)
    return out


def page(page_id: int, groups: list[list[B]], palette_id: int = 0,
         background: list[tuple[int, int, int]] = ()) -> bytes:
    """A page: `background` is (object_id, x, y) drawn by its in-effect."""
    out = bytes([page_id, 0]) + b"\x00" * 8
    if background:
        effect = b"\x00\x00\x00" + bytes([palette_id, len(background)])
        for object_id, x, y in background:
            effect += (object_id.to_bytes(2, "big") + b"\x00\x00"
                       + x.to_bytes(2, "big") + y.to_bytes(2, "big"))
        out += b"\x00" + b"\x01" + effect  # no windows, one effect
    else:
        out += b"\x00\x00"
    out += b"\x00\x00"  # out effects
    out += b"\x00" + b"\x00\x00" + b"\xff\xff" + bytes([palette_id, len(groups)])
    for group in groups:
        out += b"\xff\xff" + bytes([len(group)]) + b"".join(_button(b) for b in group)
    return out


def composition(pages: list[bytes], width=1920, height=1080, popup=False) -> bytes:
    """An ICS in one segment."""
    body = bytes([0x80 | (0x40 if popup else 0)]) + b"\x00\x00\x00" + bytes([len(pages)])
    body += b"".join(pages)
    head = (width.to_bytes(2, "big") + height.to_bytes(2, "big") + b"\x10"
            + b"\x00\x00\x80" + b"\xc0" + len(body).to_bytes(3, "big"))
    return segment(0x18, head + body)


def composition_split(pages: list[bytes], at: int, **kw) -> list[bytes]:
    """The same ICS, split into two segments the way long ones are."""
    whole = composition(pages, **kw)[3:]
    first, rest = whole[:at], whole[at:]
    first = first[:8] + b"\x80" + first[9:]
    return [segment(0x18, first), segment(0x18, whole[:8] + b"\x40" + rest)]


END = segment(0x80, b"")


# --- transport stream ---------------------------------------------------------------


def transport(segments: list[bytes], pid: int = 0x1400, junk_pid: int = 0x1011) -> bytes:
    """Each segment as its own PES in 192-byte packets, with a packet of
    another stream between, as in a real clip."""
    out = bytearray()
    for seg in segments:
        pes = b"\x00\x00\x01\xbd" + (len(seg) + 3).to_bytes(2, "big") + b"\x80\x00\x00" + seg
        first = True
        while pes:
            chunk, pes = pes[:184], pes[184:]
            header = bytes([0x47, (0x40 if first else 0) | pid >> 8, pid & 0xFF])
            if len(chunk) < 184:
                stuffing = 184 - len(chunk) - 1
                adaptation = bytes([stuffing]) + (b"\x00" + b"\xff" * (stuffing - 1)
                                                  if stuffing else b"")
                packet = header + b"\x30" + adaptation + chunk
            else:
                packet = header + b"\x10" + chunk
            out += b"\x00\x00\x00\x00" + packet
            first = False
        other = bytes([0x47, junk_pid >> 8, junk_pid & 0xFF, 0x10]) + b"\xaa" * 184
        out += b"\x00\x00\x00\x00" + other
    return bytes(out)


# --- the disc's files ----------------------------------------------------------------


def _hdmv(object_id: int | None) -> bytes:
    if object_id is None:  # a BD-J title
        return b"\x80\x00\x00\x00" + b"\x00" * 8
    return b"\x40\x00\x00\x00" + b"\x00\x00" + object_id.to_bytes(2, "big") + b"\x00" * 4


def index_bdmv(titles: list[int | None], top_menu: int | None, first_play: int | None) -> bytes:
    body = _hdmv(first_play) + _hdmv(top_menu) + len(titles).to_bytes(2, "big")
    body += b"".join(_hdmv(t) for t in titles)
    start = 40
    return (b"INDX0200" + start.to_bytes(4, "big") + b"\x00" * 4 + b"\x00" * 24
            + len(body).to_bytes(4, "big") + body)


def movie_objects(objects: list[list[bytes]]) -> bytes:
    body = len(objects).to_bytes(2, "big")
    for commands in objects:
        body += b"\x00\x00" + len(commands).to_bytes(2, "big") + b"".join(commands)
    return (b"MOBJ0200" + b"\x00" * 4 + b"\x00" * 28
            + len(body).to_bytes(4, "big") + b"\x00" * 4 + body)


def clpi(ig: bool) -> bytes:
    """Enough of a clip's information to say what streams it has."""
    streams = b"\x10\x11\x05\x1b" if not ig else b"\x14\x00\x05\x91"
    return b"HDMV0200" + b"\x00" * 40 + streams + b"\x00" * 8


def mpls(clips: list[str]) -> bytes:
    return b"MPLS0200" + b"\x00" * 30 + b"".join(f"{c}M2TS".encode() for c in clips)


def make_disc(root: Path, *, clips: dict[str, bytes], clip_info: dict[str, bool],
              index: bytes, objects: bytes, playlists: dict[int, list[str]] = None,
              jar: bool = False) -> Path:
    bdmv = root / "BDMV"
    for folder in ("STREAM", "CLIPINF", "PLAYLIST"):
        (bdmv / folder).mkdir(parents=True, exist_ok=True)
    for name, data in clips.items():
        (bdmv / "STREAM" / f"{name}.m2ts").write_bytes(data)
    for name, ig in clip_info.items():
        (bdmv / "CLIPINF" / f"{name}.clpi").write_bytes(clpi(ig))
    for number, names in (playlists or {}).items():
        (bdmv / "PLAYLIST" / f"{number:05d}.mpls").write_bytes(mpls(names))
    (bdmv / "index.bdmv").write_bytes(index)
    (bdmv / "MovieObject.bdmv").write_bytes(objects)
    if jar:
        (bdmv / "JAR").mkdir()
        (bdmv / "JAR" / "00000.jar").write_bytes(b"PK")
    return root
