# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Enough of a Blu-ray's HDMV navigation to know what a menu button plays.

A scene-selection button rarely says "play chapter 7" outright. Typically
it copies the selected button's number into a register, adds an offset
(skipping the chapters that are story films between songs), and jumps to
a title whose movie object plays the playlist at the mark that register
holds. Following that by hand is error-prone; running it is not.

So this is a small, sandboxed interpreter of the HDMV command set: the
registers, arithmetic, comparisons, gotos, jumps between titles and movie
objects, and a pressed button selecting another (auto-action) one. It runs
until something would start playback and reports what: a playlist and a
mark or play item. Nothing is played, and every run is bounded.

Movie objects come from BDMV/MovieObject.bdmv, and titles' movie objects
from BDMV/index.bdmv; both formats are read here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .bdmenu import Button, Command, Menu, Page

# Command groups and sub-groups.
BRANCH, COMPARE, SET = 0, 1, 2
GOTO, JUMP, PLAY = 0, 1, 2
SET_REG, SET_SYSTEM = 0, 1

# A button whose commands run longer than this is looping: give up.
MAX_STEPS = 5000
# Every jump or call to another object nests a level; objects that jump
# to one another in a ring would otherwise recurse until Python gave up,
# long before MAX_STEPS. No real disc's menu is nested this deep.
MAX_DEPTH = 64

# What a player's status registers hold before anything has run, as
# libbluray initialises them. Menus test some of these (player profile,
# region) before deciding what to play.
PSR_DEFAULTS = {
    0: 1, 1: 0xFF, 2: 0x0FFF0FFF, 3: 1, 4: 0xFF, 5: 0xFFFF, 6: 0, 7: 0, 8: 0, 9: 0,
    10: 0xFFFF, 11: 0, 12: 0xFF, 13: 0xFF, 14: 0xFFFF, 15: 0xFFFF,
    16: 0xFFFFFF, 17: 0xFFFFFF, 18: 0xFFFFFF, 19: 0xFFFF, 20: 0x07,
    29: 0x03, 30: 0x1FFFF, 31: 0x080200,
}


@dataclass(frozen=True)
class Target:
    """Where a button sends playback."""

    playlist: int
    mark: int | None = None  # index into the playlist's marks, link marks included
    play_item: int | None = None


@dataclass
class Navigation:
    """A disc's titles and movie objects."""

    titles: list[int | None]  # title number - 1 -> movie object id, None if BD-J
    top_menu: int | None
    objects: list[list[Command]]
    first_play: int | None = None


# --- the files --------------------------------------------------------------------


def _u16(data: bytes, at: int) -> int:
    return (data[at] << 8) | data[at + 1]


def _u32(data: bytes, at: int) -> int:
    return int.from_bytes(data[at:at + 4], "big")


def _hdmv_object(data: bytes, at: int) -> int | None:
    """A 12-byte index entry: the movie object it names, or None for BD-J."""
    object_type = data[at] >> 6
    if object_type != 1:
        return None
    return _u16(data, at + 6)


def parse_index(data: bytes) -> tuple[list[int | None], int | None, int | None]:
    """index.bdmv: (each title's movie object, the top menu's, first play's)."""
    if data[:4] != b"INDX":
        raise ValueError("not an index.bdmv")
    at = _u32(data, 8) + 4  # past the section's own length
    first_play = _hdmv_object(data, at)
    at += 12
    top_menu = _hdmv_object(data, at)
    at += 12
    count = _u16(data, at)
    at += 2
    titles = []
    for _ in range(count):
        titles.append(_hdmv_object(data, at))
        at += 12
    return titles, top_menu, first_play


def parse_movie_objects(data: bytes) -> list[list[Command]]:
    """MovieObject.bdmv: each object's commands."""
    if data[:4] != b"MOBJ":
        raise ValueError("not a MovieObject.bdmv")
    at = 40 + 8  # header, then the section's length and a reserved word
    count = _u16(data, at)
    at += 2
    objects = []
    for _ in range(count):
        at += 2  # flags
        commands = _u16(data, at)
        at += 2
        objects.append([Command.parse(data[at + 12 * i: at + 12 * i + 12])
                        for i in range(commands)])
        at += 12 * commands
    return objects


def read_navigation(disc_root) -> Navigation | None:
    bdmv = Path(disc_root) / "BDMV"
    try:
        titles, top_menu, first_play = parse_index((bdmv / "index.bdmv").read_bytes())
        objects = parse_movie_objects((bdmv / "MovieObject.bdmv").read_bytes())
    except (OSError, ValueError, IndexError):
        return None
    return Navigation(titles, top_menu, objects, first_play)


# --- running it -------------------------------------------------------------------


class _Stop(Exception):
    pass


class Machine:
    """One button press, from a clean player state."""

    def __init__(self, navigation: Navigation | None, menu: Menu | None = None,
                 page: Page | None = None, playlist: int | None = None) -> None:
        self.nav = navigation
        self.menu = menu
        self.page = page
        self.gpr: dict[int, int] = {}
        self.psr: dict[int, int] = dict(PSR_DEFAULTS)
        if playlist is not None:
            # A pop-up menu runs over a playing playlist; LinkMK means that one.
            self.psr[6] = playlist
        self.steps = 0
        self.depth = 0
        self.target: Target | None = None

    # --- registers

    def _read(self, reg: int) -> int:
        if reg & 0x80000000:
            return self.psr.get(reg & 0x7F, 0)
        return self.gpr.get(reg & 0xFFF, 0)

    def _write(self, reg: int, value: int) -> None:
        value &= 0xFFFFFFFF
        if reg & 0x80000000:
            self.psr[reg & 0x7F] = value
        else:
            self.gpr[reg & 0xFFF] = value

    def _operand(self, immediate: bool, value: int, button_page: bool = False) -> int:
        if immediate:
            return value
        if button_page:
            # Flags stay; the button and page numbers come from registers.
            flags = value & 0xC000C000
            low = self._read(value & 0xFFF) & 0x3FFF
            high = self._read((value >> 16) & 0xFFF) & 0x3FFF
            return flags | low | (high << 16)
        return self._read(value)

    # --- running

    def warm_up(self) -> None:
        """Run First Play and the Top Menu as far as they go before playing
        something, as a player has by the time a menu is on screen.

        Menus' movie objects commonly test a "set up yet?" register and, if
        not, reset everything - including the one a scene button has just
        put the chapter in. A cold machine takes that branch; a player
        never does.
        """
        if self.nav is None:
            return
        page = self.page
        for object_id in (self.nav.first_play, self.nav.top_menu):
            if object_id is None:
                continue
            try:
                self._run_object(object_id)
            except _Stop:
                pass
        self.page = page
        self.target = None
        self.steps = 0

    def press(self, button: Button) -> Target | None:
        self.psr[10] = button.id
        try:
            self._run_button(button)
        except _Stop:
            pass
        return self.target

    def _run_button(self, button: Button) -> None:
        selected = self._run(button.commands)
        # A button that selects another, auto-action button hands on to it
        # once its own commands are done.
        if selected is not None and self.page is not None:
            nxt = next((b for b in self.page.buttons if b.id == selected), None)
            if nxt is not None and nxt.auto_action and nxt is not button:
                self.psr[10] = nxt.id
                self._run_button(nxt)

    def _run_object(self, object_id: int) -> None:
        if self.nav is None or not 0 <= object_id < len(self.nav.objects):
            raise _Stop()
        # Leaving the menu: nothing selected on it matters any more.
        self.page = None
        self._run(self.nav.objects[object_id])
        raise _Stop()

    def _run_title(self, number: int) -> None:
        if self.nav is None or not 1 <= number <= len(self.nav.titles):
            raise _Stop()
        object_id = self.nav.titles[number - 1]
        if object_id is None:
            raise _Stop()  # a BD-J title
        self.psr[4] = number
        self._run_object(object_id)

    def _play(self, target: Target) -> None:
        self.target = target
        raise _Stop()

    def _run(self, commands: list[Command]) -> int | None:
        """Run a command list; returns a button it selected, if any."""
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise _Stop()
        try:
            return self._run_commands(commands)
        finally:
            self.depth -= 1

    def _run_commands(self, commands: list[Command]) -> int | None:
        selected = None
        pc = 0
        while 0 <= pc < len(commands):
            self.steps += 1
            if self.steps > MAX_STEPS:
                raise _Stop()
            c = commands[pc]
            pc += 1
            if c.group == BRANCH:
                if c.sub_group == GOTO:
                    if c.branch_opt == 1:  # Goto
                        pc = self._operand(c.imm_dst, c.dst)
                    elif c.branch_opt == 2:  # Break
                        break
                elif c.sub_group == JUMP:
                    dst = self._operand(c.imm_dst, c.dst)
                    if c.branch_opt in (0, 2):  # JumpObject, CallObject
                        self._run_object(dst)
                    elif c.branch_opt in (1, 3):  # JumpTitle, CallTitle
                        self._run_title(dst)
                    else:  # Resume
                        raise _Stop()
                elif c.sub_group == PLAY:
                    self._play_command(c)
            elif c.group == COMPARE:
                if not self._compare(c):
                    pc += 1
            elif c.group == SET:
                if c.sub_group == SET_REG:
                    self._set(c)
                elif c.sub_group == SET_SYSTEM and c.set_opt == 3:  # SetButtonPage
                    dst = self._operand(c.imm_dst, c.dst, button_page=True)
                    if dst & 0x80000000:
                        selected = dst & 0xFFFF
        return selected

    def _play_command(self, c: Command) -> None:
        dst = self._operand(c.imm_dst, c.dst)
        src = self._operand(c.imm_src, c.src)
        playing = self.psr.get(6, 0)
        if c.branch_opt == 0:  # PlayPL
            self._play(Target(dst))
        elif c.branch_opt == 1:  # PlayPLatPI
            self._play(Target(dst, play_item=src))
        elif c.branch_opt == 2:  # PlayPLatMK
            self._play(Target(dst, mark=src))
        elif c.branch_opt == 4:  # LinkPI
            self._play(Target(playing, play_item=dst))
        elif c.branch_opt == 5:  # LinkMK
            self._play(Target(playing, mark=dst))
        else:  # TerminatePL
            raise _Stop()

    def _compare(self, c: Command) -> bool:
        a = self._operand(c.imm_dst, c.dst)
        b = self._operand(c.imm_src, c.src)
        return {
            1: (a & b) == b,  # BC: every bit of b set in a
            2: a == b, 3: a != b, 4: a >= b, 5: a > b, 6: a <= b, 7: a < b,
        }.get(c.cmp_opt, False)

    def _set(self, c: Command) -> None:
        if c.imm_dst:
            return  # a constant can't be written to
        a = self._read(c.dst)
        b = self._operand(c.imm_src, c.src)
        op = c.set_opt
        if op == 1:
            value = b
        elif op == 2:  # Swap
            if not c.imm_src:
                self._write(c.src, a)
            value = b
        elif op == 3:
            value = a + b
        elif op == 4:
            value = max(0, a - b)
        elif op == 5:
            value = a * b
        elif op == 6:
            value = a // b if b else 0xFFFFFFFF
        elif op == 7:
            value = a % b if b else 0xFFFFFFFF
        elif op == 8:  # Rnd: 1..b; a deterministic pick is all a sandbox can do
            value = 1
        elif op == 9:
            value = a & b
        elif op == 10:
            value = a | b
        elif op == 11:
            value = a ^ b
        elif op == 12:
            value = a | (1 << (b & 31))
        elif op == 13:
            value = a & ~(1 << (b & 31))
        elif op == 14:
            value = a << (b & 31)
        elif op == 15:
            value = a >> (b & 31)
        else:
            return
        self._write(c.dst, value)


def press(button: Button, navigation: Navigation | None, menu: Menu | None = None,
          page: Page | None = None, playlist: int | None = None) -> Target | None:
    """What pressing `button` plays, from the state a player is in when the
    menu is showing, or None if it plays nothing (opens another page, sets
    a language, loops)."""
    machine = Machine(navigation, menu, page, playlist)
    try:
        machine.warm_up()
        if playlist is not None:
            machine.psr[6] = playlist
        return machine.press(button)
    except RecursionError:
        return None  # a ring the depth limit somehow didn't catch
