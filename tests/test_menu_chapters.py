# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Naming a title's chapters from its disc's menu: a synthetic disc, read
end to end, and the model's reading of it."""

import json

import pytest

from mediabrowser.core import ai, ai_chapters, bluray, menu_chapters
from tests import bdmv_builder as bd

COLOURS = {0: (16, 128, 128, 0), 1: (235, 128, 128, 255), 2: (82, 240, 90, 255)}
# Marks of the feature's playlist: every chapter start, one per 100s.
MARKS = [(bluray.MARK_ENTRY, 100.0 * i) for i in range(6)]


def title_video(root, chapters=6):
    starts = [100.0 * i for i in range(chapters)]
    return {
        "type": "bluray", "path": str(root), "title_idx": 0, "playlist": 1,
        "display_name": "LIVE AT SOMEWHERE", "duration": 100.0 * chapters,
        "chapters": [
            {"index": i, "start": s, "end": s + 100.0, "title": None, "source": "auto-numbered"}
            for i, s in enumerate(starts)
        ],
    }


def scene_menu(buttons, popup=False):
    """A one-page menu whose buttons press through title 1: GPR5 = the
    button's id + its offset, then the title plays playlist 1 at mark GPR5."""
    glyph = [[1] * 8, [1] * 8]
    shown = [bd.B(button_id, 10, 10 + 6 * n, normal=0, selected=1,
                  commands=[bd.move(5, bd.PSR(10), imm=False), bd.add(5, offset),
                            bd.jump_title(1)])
             for n, (button_id, offset) in enumerate(buttons)]
    back = bd.B(99, 10, 40, normal=0, selected=1, commands=[bd.set_button_page(page=1)])
    # The menu's plumbing: invisible, and pressed directly it plays mark 0.
    relay = bd.B(98, 0, 0, commands=[bd.jump_title(1)], auto=True)
    return bd.transport([
        bd.palette(0, COLOURS), bd.bitmap(0, glyph), bd.bitmap(1, [[2] * 8, [2] * 8]),
        bd.composition([bd.page(0, [shown + [back, relay]])], width=64, height=48,
                       popup=popup),
        bd.END,
    ])


@pytest.fixture
def disc(tmp_path, monkeypatch):
    """A disc whose scene menu has four songs; chapters 1 and 4 (0-based 0
    and 3) are films no button plays."""
    root = bd.make_disc(
        tmp_path / "DISC",
        clips={"00001": b"", "00006": scene_menu([(1, 0), (2, 0), (3, 1), (4, 1)])},
        clip_info={"00001": False, "00006": True},
        index=bd.index_bdmv([0], None, None),
        objects=bd.movie_objects([[bd.play_pl_at_mark(1, 5, imm_mark=False)]]),
    )
    monkeypatch.setattr(bluray, "playlist_marks", lambda root, playlist: MARKS)
    pictures = []

    def picture(menu, rgba, background=None, crop=None, width=None):
        pictures.append(crop)
        return b"\xff\xd8" + repr(crop).encode()

    monkeypatch.setattr(menu_chapters.bdmenu, "picture", picture)
    return root, pictures


class TestReading:
    def test_each_button_is_found_on_the_chapter_it_plays(self, disc):
        root, pictures = disc
        menu = menu_chapters.read(title_video(root))
        assert [b.chapter for b in menu.buttons] == [1, 2, 4, 5]
        assert menu.chapters() == [1, 2, 4, 5], "the invisible relay's mark 0 isn't one"
        assert [p.clip for p in menu.pages] == ["00006.m2ts"]
        assert menu.pages[0].picture and all(b.picture for b in menu.buttons)
        # One whole page and one crop per button.
        assert pictures.count(None) == 1 and len(pictures) == 5

    def test_a_file_has_no_menu(self, tmp_path):
        video = title_video(tmp_path)
        video["type"] = "file"
        with pytest.raises(menu_chapters.MenuError, match="single file"):
            menu_chapters.read(video)

    def test_a_java_disc_says_so(self, tmp_path, monkeypatch):
        root = bd.make_disc(tmp_path / "BDJ", clips={}, clip_info={},
                            index=bd.index_bdmv([None], None, None),
                            objects=bd.movie_objects([]), jar=True)
        monkeypatch.setattr(bluray, "playlist_marks", lambda root, playlist: MARKS)
        with pytest.raises(menu_chapters.MenuError, match="Java"):
            menu_chapters.read(title_video(root))

    def test_a_menu_for_another_title_is_no_use(self, disc):
        root, _ = disc
        video = title_video(root)
        video["playlist"] = 7
        with pytest.raises(menu_chapters.MenuError, match="No menu"):
            menu_chapters.read(video)

    def test_it_can_be_cancelled(self, disc):
        import threading

        cancel = threading.Event()
        cancel.set()
        with pytest.raises(menu_chapters.Cancelled):
            menu_chapters.read(title_video(disc[0]), cancel=cancel)


class TestChoosingPages:
    def candidate(self, chapters, popup=False):
        menu = menu_chapters.bdmenu.Menu(64, 48, popup, [], {}, {})
        return menu_chapters._Candidate(None, menu, None, {c: None for c in chapters})

    def test_a_menu_over_two_pages_takes_both(self):
        first, second = self.candidate([1, 2, 3]), self.candidate([4, 5])
        assert menu_chapters._choose([second, first]) == [first, second]

    def test_a_pop_up_repeating_the_scene_menu_is_left_out(self):
        screen, popup = self.candidate([1, 2, 3]), self.candidate([1, 2, 3], popup=True)
        assert menu_chapters._choose([popup, screen]) == [screen]

    def test_a_mark_between_chapters_lands_in_the_one_it_plays_in(self):
        chapters = title_video("/x")["chapters"]
        assert menu_chapters._chapter_at(chapters, 300.4) == 3
        assert menu_chapters._chapter_at(chapters, 350.0) == 3
        assert menu_chapters._chapter_at(chapters, 9999.0) is None


class TestTheModelsReading:
    @pytest.fixture
    def menu(self, disc):
        return menu_chapters.read(title_video(disc[0]))

    def test_the_request_shows_every_button_with_its_chapter(self, disc, menu):
        video = title_video(disc[0])
        messages = menu_chapters.build_messages(video, menu, context="Artist / Show")
        parts = messages[1]["content"]
        text = "\n".join(p["text"] for p in parts if p["type"] == "text")
        assert "Folders: Artist / Show" in text
        assert "Button 3 plays chapter 5 (starts 6:40):" in text
        assert "No button plays chapter(s) 1, 4." in text
        assert "\"Iine!\", not \"line!\"" in text
        assert sum(p["type"] == "image_url" for p in parts) == 5

    def test_buttons_name_their_chapters_and_the_rest_are_judged(self, disc, menu):
        video = title_video(disc[0])
        reply = json.dumps({
            "show": "x",
            "buttons": [
                {"button": 1, "title": "BABYMETAL DEATH", "is_title": True},
                {"button": 2, "title": "Megitsune", "original_title": "メギツネ"},
                {"button": 3, "title": "Song 4"},
                {"button": 4, "title": "Play All", "is_title": False},
                {"button": 9, "title": "nowhere"},
            ],
            "others": [
                {"chapter": 1, "title": "Opening", "confidence": "high", "note": "57s"},
                {"chapter": 2, "title": "Not allowed: a button names this"},
                {"chapter": 4, "title": "Intro to Song 4", "confidence": "medium"},
                {"chapter": 6, "title": "Encore", "confidence": "low"},
            ],
        })
        result = menu_chapters.parse_reply(reply, video, menu, model="m")
        assert [c["title"] for c in result.chapters] == [
            "Opening", "BABYMETAL DEATH", "Megitsune", "Intro to Song 4", "Song 4", "Encore",
        ]
        assert [c["source"] for c in result.chapters] == ["ai", "menu", "menu", "ai", "menu", "ai"]
        assert result.chapters[2]["original_title"] == "メギツネ"
        rows = {r.chapter: r for r in result.rows}
        assert rows[2].confidence == "high" and rows[2].note == menu_chapters.MENU_NOTE
        assert rows[1].note == "not on the menu: 57s"
        assert rows[6].confidence == "low", "Play All's chapter is judged, not read"
        assert result.mapping()[1] == (1, "BABYMETAL DEATH", None, "menu")

    def test_an_unreadable_button_leaves_its_chapter_alone(self, disc, menu):
        video = title_video(disc[0])
        result = menu_chapters.parse_reply('{"buttons": [], "others": []}', video, menu)
        assert all(c["title"] is None for c in result.chapters)
        assert {r.note for r in result.rows} == {"on the menu, but unreadable", "not on the menu"}
        assert result.mapping() == []

    def test_asking_uses_no_web_search(self, disc, menu):
        seen = {}

        def chat(messages, settings, **kwargs):
            seen.update(kwargs)
            return json.dumps({"buttons": [{"button": 1, "title": "A"}]})

        settings = ai.settings_from({ai.SETTING_KEY: "k"})
        result = menu_chapters.ask(title_video(disc[0]), menu, settings, chat=chat)
        assert "online" not in seen or not seen["online"]
        assert result.model == ai.DEFAULT_MODEL
        assert result.mode == ai_chapters.NAME
