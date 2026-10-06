# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The sandboxed HDMV interpreter: what pressing a button plays. The
programs here are the shapes found on real concert discs."""

from mediabrowser.core import bdmenu, hdmv
from tests import bdmv_builder as bd


def button(button_id, commands, auto=False):
    return bdmenu.Button(button_id, 0xFFFF, auto, 0, 0, (0, 0), (1, 1), (1, 1),
                         [bdmenu.Command.parse(c) for c in commands])


def navigation(titles, objects, top_menu=None, first_play=None):
    return hdmv.Navigation(
        titles, top_menu, [[bdmenu.Command.parse(c) for c in o] for o in objects], first_play
    )


class TestTheFiles:
    def test_index_and_movie_objects_round_trip(self):
        titles, top, first = hdmv.parse_index(bd.index_bdmv([2, None, 0], 1, 3))
        assert (titles, top, first) == ([2, None, 0], 1, 3)
        objects = hdmv.parse_movie_objects(
            bd.movie_objects([[bd.play_pl(1)], [], [bd.goto(0)] * 3])
        )
        assert [len(o) for o in objects] == [1, 0, 3]
        assert objects[0][0].branch_opt == 0 and objects[0][0].dst == 1

    def test_a_disc_folder(self, tmp_path):
        disc = bd.make_disc(
            tmp_path, clips={}, clip_info={},
            index=bd.index_bdmv([0], None, None), objects=bd.movie_objects([[bd.play_pl(7)]]),
        )
        nav = hdmv.read_navigation(disc)
        assert nav.titles == [0] and nav.objects[0][0].dst == 7
        assert hdmv.read_navigation(tmp_path / "nowhere") is None


class TestPressing:
    def test_a_direct_link_to_a_mark(self):
        assert hdmv.press(button(1, [bd.link_mark(4)]), None, playlist=2) == hdmv.Target(2, mark=4)

    def test_a_pop_up_computes_the_mark_from_the_button(self):
        # Wembley's pop-up: GPR25 = (button - 1) / 2 + 1, then LinkMK GPR25.
        commands = [bd.move(25, bd.PSR(10), imm=False), bd.sub(25, 1),
                    bd.command(2, 0, set_op=6, dst=25, src=2, imm_src=True), bd.add(25, 1),
                    bd.link_mark(25, imm=False)]
        assert hdmv.press(button(7, commands), None, playlist=1) == hdmv.Target(1, mark=4)

    def test_through_a_title_to_its_movie_object(self):
        # Wembley's scene menu: GPR5 = button + 2; JumpTitle 1; the title's
        # object plays playlist 1 at mark GPR5.
        nav = navigation(
            [0], [[bd.move(25, 5, imm=False), bd.play_pl_at_mark(1, 25, imm_mark=False)]]
        )
        pressed = button(10, [bd.move(5, bd.PSR(10), imm=False), bd.add(5, 2), bd.jump_title(1)])
        assert hdmv.press(pressed, nav) == hdmv.Target(1, mark=12)

    def test_a_guard_that_resets_on_a_cold_start_is_passed_by_warming_up(self):
        # The title's object: "if not set up, reset GPR5" - set up is done by
        # the top menu, as a player has before any menu shows.
        title = [bd.if_eq(21, 1), bd.goto(3), bd.move(5, 0),
                 bd.play_pl_at_mark(1, 5, imm_mark=False)]
        top = [bd.if_eq(21, 1), bd.goto(3), bd.move(21, 1), bd.play_pl(3)]
        nav = navigation([0], [title, top], top_menu=1)
        pressed = button(6, [bd.move(5, bd.PSR(10), imm=False), bd.jump_title(1)])
        assert hdmv.press(pressed, nav) == hdmv.Target(1, mark=6)
        cold = hdmv.Machine(nav)
        assert cold.press(pressed) == hdmv.Target(1, mark=0), "why warming up matters"

    def test_selecting_an_auto_action_button_hands_on_to_it(self):
        # O2: GPR5 = button + 2; select button 16, which is auto-action and
        # does GPR6 = GPR5 - 1; JumpTitle 1.
        nav = navigation([0], [[bd.play_pl_at_mark(0, 6, imm_mark=False)]])
        relay = button(16, [bd.move(6, 5, imm=False), bd.sub(6, 1), bd.jump_title(1)], auto=True)
        pressed = button(3, [bd.move(5, bd.PSR(10), imm=False), bd.add(5, 2),
                             bd.set_button_page(button=16)])
        page = bdmenu.Page(1, 0, 0, [], [[pressed, relay]])
        assert hdmv.press(pressed, nav, page=page) == hdmv.Target(0, mark=4)

    def test_a_button_that_opens_another_page_plays_nothing(self):
        assert hdmv.press(button(1, [bd.set_button_page(button=1, page=2)]), None) is None

    def test_a_loop_is_given_up_on(self):
        assert hdmv.press(button(1, [bd.goto(0)]), None) is None

    def test_a_bd_j_title_plays_nothing_it_can_see(self):
        nav = navigation([None], [])
        assert hdmv.press(button(1, [bd.jump_title(1)]), nav) is None

    def test_comparisons_skip_the_next_command(self):
        commands = [bd.if_eq(1, 5), bd.link_mark(9), bd.link_mark(2)]
        assert hdmv.press(button(1, commands), None, playlist=0) == hdmv.Target(0, mark=2)
        commands = [bd.move(1, 5), *commands]
        assert hdmv.press(button(1, commands), None, playlist=0) == hdmv.Target(0, mark=9)

    def test_objects_that_jump_to_one_another_are_given_up_on(self):
        # Each jump nests a level: a ring of two would otherwise recurse
        # until Python gave up, long before MAX_STEPS.
        nav = navigation([], [[bd.jump_object(1)], [bd.jump_object(0)]])
        assert hdmv.press(button(1, [bd.jump_object(0)]), nav) is None
