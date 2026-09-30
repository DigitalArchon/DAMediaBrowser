# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Decoding a Blu-ray menu straight from its transport stream."""

import shutil

import pytest

from mediabrowser.core import bdmenu
from tests import bdmv_builder as bd

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

# Y, Cr, Cb, alpha: transparent, white, and a pure-ish red.
COLOURS = {0: (16, 128, 128, 0), 1: (235, 128, 128, 255), 2: (82, 240, 90, 255)}


def menu_stream(popup=False, split_at=None):
    text = [[1, 1, 1, 0], [1, 0, 1, 0]]  # a 4x2 "word"
    highlight = [[2] * 4, [2] * 4]
    pages = [bd.page(0, [[
        bd.B(1, 10, 20, normal=0, selected=1, commands=[bd.link_mark(3)]),
        bd.B(2, 10, 40, normal=0, selected=1, commands=[bd.link_mark(5)]),
    ]], background=[(2, 0, 0)])]
    ics = (bd.composition_split(pages, split_at, width=64, height=48, popup=popup)
           if split_at else [bd.composition(pages, width=64, height=48, popup=popup)])
    return bd.transport([
        bd.palette(0, COLOURS), bd.bitmap(0, text), bd.bitmap(1, highlight),
        bd.bitmap(2, [[1] * 3]), *ics, bd.END,
    ])


class TestRunLength:
    def test_it_round_trips(self):
        rows = [[0, 0, 5, 5, 5, 1, 0], [7] * 7, [0] * 7]
        data = bd.rle(rows)
        assert bdmenu.decode_rle(data, 7, 3) == bytes(sum(rows, []))

    def test_long_runs(self):
        rows = [[3] * 300 + [0] * 200]
        assert bdmenu.decode_rle(bd.rle(rows), 500, 1) == bytes(rows[0])

    def test_a_short_stream_is_padded_not_trusted(self):
        assert bdmenu.decode_rle(b"\x05", 3, 2) == b"\x05\x00\x00\x00\x00\x00"


class TestCommands:
    def test_the_fields_come_out_where_they_go_in(self):
        c = bdmenu.Command.parse(bd.play_pl_at_mark(1, 0x19, imm_mark=False))
        assert (c.group, c.sub_group, c.branch_opt) == (0, 2, 2)
        assert c.imm_dst and not c.imm_src
        assert (c.dst, c.src) == (1, 0x19)
        s = bdmenu.Command.parse(bd.add(5, 3))
        assert (s.group, s.sub_group, s.set_opt, s.imm_src) == (2, 0, 3, True)


class TestReadingAClip:
    def test_the_menu_comes_out_whole(self, tmp_path):
        clip = tmp_path / "00006.m2ts"
        clip.write_bytes(menu_stream())
        [menu] = bdmenu.read_menus(clip)
        assert (menu.width, menu.height, menu.popup) == (64, 48, False)
        [page] = menu.pages
        assert [b.id for b in page.buttons] == [1, 2]
        assert page.buttons[0].x == 10 and page.buttons[0].y == 20
        assert page.buttons[0].normal == (0, 0) and page.buttons[0].selected == (1, 1)
        assert page.background == [bdmenu.Placement(2, 0, 0)]
        assert sorted(menu.bitmaps) == [0, 1, 2]
        assert menu.bitmaps[0].pixels == bytes([1, 1, 1, 0, 1, 0, 1, 0])
        assert menu.palettes[0].colours[0][3] == 0
        assert menu.palettes[0].colours[1][:3] == (255, 255, 255)
        c = page.buttons[1].commands[0]
        assert (c.branch_opt, c.dst) == (5, 5)

    def test_a_pop_up_says_so(self, tmp_path):
        clip = tmp_path / "x.m2ts"
        clip.write_bytes(menu_stream(popup=True))
        assert bdmenu.read_menus(clip)[0].popup

    def test_a_composition_in_two_segments_is_joined(self, tmp_path):
        clip = tmp_path / "x.m2ts"
        clip.write_bytes(menu_stream(split_at=30))
        [page] = bdmenu.read_menus(clip)[0].pages
        assert [b.id for b in page.buttons] == [1, 2]

    def test_a_clip_without_a_menu_has_none(self, tmp_path):
        clip = tmp_path / "x.m2ts"
        clip.write_bytes(bd.transport([bd.palette(0, COLOURS)], pid=0x1100))
        assert bdmenu.read_menus(clip) == []


class TestDrawing:
    def menu(self, tmp_path):
        clip = tmp_path / "x.m2ts"
        clip.write_bytes(menu_stream())
        [menu] = bdmenu.read_menus(clip)
        return menu, menu.pages[0]

    @staticmethod
    def pixel(rgba, menu, x, y):
        at = 4 * (y * menu.width + x)
        return tuple(rgba[at:at + 4])

    def test_normal_buttons_and_the_background(self, tmp_path):
        menu, page = self.menu(tmp_path)
        rgba = bdmenu.render(menu, page)
        assert len(rgba) == 64 * 48 * 4
        assert self.pixel(rgba, menu, 1, 0) == (255, 255, 255, 255)  # background object
        assert self.pixel(rgba, menu, 10, 20)[3] == 255  # the button's text
        assert self.pixel(rgba, menu, 13, 20)[3] == 0  # its transparent pixel
        assert self.pixel(rgba, menu, 40, 40)[3] == 0  # nothing

    def test_the_selected_button_is_highlighted_and_only_it(self, tmp_path):
        menu, page = self.menu(tmp_path)
        rgba = bdmenu.render(menu, page, selected=page.buttons[1])
        red = self.pixel(rgba, menu, 13, 40)
        assert red[3] == 255 and red[0] > 200 and red[1] < 80
        assert self.pixel(rgba, menu, 13, 20)[3] == 0  # the other stays normal

    def test_a_group_of_overlapping_buttons_shows_one(self):
        a, b = bd.B(1, 0, 0), bd.B(2, 0, 0)
        group = [bdmenu.Button(1, 0, False, 0, 0, (0, 0), (0, 0), (0, 0), []),
                 bdmenu.Button(2, 0, False, 0, 0, (1, 1), (1, 1), (1, 1), [])]
        assert bdmenu._shown(group, None) == group[:1]
        assert bdmenu._shown(group, group[1]) == [group[1]]
        side_by_side = [group[0], bdmenu.Button(3, 0, False, 50, 0, (1, 1), (1, 1), (1, 1), [])]
        assert bdmenu._shown(side_by_side, None) == side_by_side
        del a, b

    def test_a_button_is_as_big_as_its_biggest_state(self, tmp_path):
        menu, page = self.menu(tmp_path)
        assert bdmenu.button_box(menu, page.buttons[0]) == (10, 20, 4, 2)

    @needs_ffmpeg
    def test_a_picture_is_a_jpeg(self, tmp_path):
        menu, page = self.menu(tmp_path)
        jpeg = bdmenu.picture(menu, bdmenu.render(menu, page), crop=(8, 18, 16, 8))
        assert jpeg and jpeg.startswith(b"\xff\xd8")
        over = bdmenu.picture(menu, bdmenu.render(menu, page), b"\x80" * (64 * 48 * 3))
        assert over and over.startswith(b"\xff\xd8")

    @needs_ffmpeg
    def test_a_background_frame_falls_back_to_the_start(self, tmp_path):
        import subprocess

        clip = tmp_path / "still.m2ts"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", "color=c=red:s=64x48:d=0.04", "-frames:v", "1",
                        "-f", "mpegts", str(clip)], check=True)
        frame = bdmenu.background_frame(clip, 64, 48, at_seconds=30.0)
        assert frame is not None and len(frame) == 64 * 48 * 3
        assert frame[0] > 200 and frame[1] < 60, "red"


class TestFindingTheMenus:
    def test_the_clip_information_says_which_clips(self, tmp_path):
        disc = bd.make_disc(
            tmp_path, clips={"00001": b"x" * 10, "00005": b"y", "00006": b"z"},
            clip_info={"00001": False, "00005": True, "00006": True},
            index=bd.index_bdmv([0], None, None), objects=bd.movie_objects([[]]),
        )
        assert [c.name for c in bdmenu.clips_with_menus(disc)] == ["00005.m2ts", "00006.m2ts"]

    def test_without_clip_information_the_small_clips_are_read(self, tmp_path, monkeypatch):
        disc = bd.make_disc(
            tmp_path, clips={"00001": b"x" * 100, "00002": b"y"}, clip_info={},
            index=bd.index_bdmv([0], None, None), objects=bd.movie_objects([[]]),
        )
        monkeypatch.setattr(bdmenu, "UNLISTED_CLIP_LIMIT", 50)
        assert [c.name for c in bdmenu.clips_with_menus(disc)] == ["00002.m2ts"]

    def test_playlists_pair_a_menu_with_its_picture(self, tmp_path, monkeypatch):
        disc = bd.make_disc(
            tmp_path, clips={"00004": b"still", "00006": b"menu"}, clip_info={},
            index=bd.index_bdmv([0], None, None), objects=bd.movie_objects([[]]),
            playlists={3: ["00004", "00006"], 1: ["00001"]},
        )
        assert bdmenu.bluray.playlist_clips(disc) == {1: ["00001"], 3: ["00004", "00006"]}
        monkeypatch.setattr(bdmenu, "has_video", lambda path: path.name == "00004.m2ts")
        stream = disc / "BDMV" / "STREAM"
        assert bdmenu.background_clip(disc, stream / "00006.m2ts") == stream / "00004.m2ts"
        assert bdmenu.background_clip(disc, stream / "00004.m2ts") == stream / "00004.m2ts"
