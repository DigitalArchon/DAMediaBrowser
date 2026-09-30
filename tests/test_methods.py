# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

from mediabrowser.core import library, methods


def video(n_chapters=1, duration=600.0, origin=None):
    step = duration / n_chapters
    v = {
        "type": "file", "duration": duration,
        "chapters": [{"start": i * step, "end": (i + 1) * step, "title": None}
                     for i in range(n_chapters)],
    }
    if origin:
        v["chapter_origin"] = origin
    return v


WHOLE = [{"title": "One", "length": 300.0}, {"title": "Two", "length": 300.0}]
PART = [{"title": "One", "length": 100.0}, {"title": "Two", "length": 100.0}]
TITLES = [{"title": "One", "length": None}, {"title": "Two", "length": None}]


class TestChoosing:
    def test_lengths_that_are_the_whole_video_place_its_chapters(self):
        assert methods.choose(video(), WHOLE, True) == methods.LENGTHS

    def test_they_replace_an_estimate_too(self):
        assert methods.choose(video(5, origin=library.ORIGIN_ESTIMATED), WHOLE, True) == (
            methods.LENGTHS
        )

    def test_a_discs_own_chapters_are_named_rather_than_replaced(self):
        assert methods.choose(video(4), WHOLE, True) == methods.NAME

    def test_titles_alone_tell_detection_what_to_find(self):
        assert methods.choose(video(), TITLES, True) == methods.DETECT

    def test_titles_name_an_estimate_someone_may_have_fixed(self):
        assert methods.choose(video(5, origin=library.ORIGIN_ESTIMATED), TITLES, True) == (
            methods.NAME
        )

    def test_nothing_to_go_on_means_detecting(self):
        assert methods.choose(video(), [], True) == methods.DETECT

    def test_a_discs_chapters_with_no_tracklist_have_nothing_to_do(self):
        assert methods.choose(video(4), [], True) is None

    def test_a_blu_ray_title_cannot_be_detected(self):
        assert methods.choose(video(), TITLES, False) is None
        assert methods.choose(video(), PART, False) == methods.LENGTHS


class TestAvailable:
    def test_each_method_is_offered_only_where_it_can_work(self):
        assert methods.available(video(), TITLES, True) == [methods.DETECT]
        assert methods.available(video(4), WHOLE, True) == [
            methods.NAME, methods.LENGTHS, methods.DETECT
        ]
        assert methods.available(video(4), TITLES, False) == [methods.NAME]
