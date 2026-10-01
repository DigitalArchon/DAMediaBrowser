# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Identifying a library unattended: which methods run, in what order,
what they may change, and undoing a run. Every outside service is a
stand-in that records what it was asked."""

import threading

import pytest

from mediabrowser.core import (
    ai,
    ai_chapters,
    ai_musicbrainz,
    autoname,
    library,
    menu_chapters,
    naming,
    store,
)
from tests.test_chaptergen import concert


def file_video(starts=(0.0,), duration=1212.0, titles=None, origin=None, kind="file"):
    ends = list(starts[1:]) + [duration]
    titles = titles or [None] * len(starts)
    video = {
        "type": kind, "path": "/lib/Artist/Show/Concert.mkv", "display_name": "Concert",
        "duration": duration,
        "chapters": [
            {"index": i, "start": s, "end": e, "title": t,
             "source": "embedded" if t else "auto-numbered"}
            for i, (s, e, t) in enumerate(zip(starts, ends, titles, strict=True))
        ],
    }
    if kind == "bluray":
        video.update(playlist=1, title_idx=0)
    if origin:
        video["chapter_origin"] = origin
    return video


class FakeServices:
    """Answers from what a test sets; records every call."""

    def __init__(self):
        self.calls = []
        self.releases = []  # search results
        self.searches = {}  # query -> search results, where they differ
        self.recordings = []  # recording search results
        self.media = {}  # release id -> media
        self.levels_value = None
        self.menu = None  # a DiscMenu, or an exception to raise
        self.menu_answer = None  # a Result
        self.look_answer = None  # a function of the situation -> Result
        self.translations = {}
        self.queries = []  # the AI's better searches
        self.choice = None  # an ai_musicbrainz.Choice, or a function of the candidates

    def search_releases(self, query):
        self.calls.append(("search", query))
        return self.searches.get(query, self.releases)

    def search_recordings(self, query):
        self.calls.append(("recordings", query))
        return self.recordings

    def release_media(self, release_id):
        self.calls.append(("media", release_id))
        return self.media[release_id]

    def better_queries(self, video, context, tried, found, settings):
        self.calls.append(("better_queries", tuple(tried)))
        return "the show", list(self.queries)

    def choose_release(self, video, context, candidates, settings):
        self.calls.append(("choose", tuple(c.release["id"] for c in candidates)))
        if callable(self.choice):
            return self.choice(candidates)
        return self.choice or ai_musicbrainz.Choice(None, "high", "none of them")

    def levels(self, video, cancel):
        self.calls.append(("levels", video["path"]))
        return self.levels_value

    def read_menu(self, video, cancel):
        self.calls.append(("read_menu",))
        if isinstance(self.menu, Exception):
            raise self.menu
        return self.menu

    def ask_menu(self, video, disc, settings, context, translate):
        self.calls.append(("ask_menu",))
        return self.menu_answer

    def look(self, situation, settings, cancel):
        self.calls.append(("look", situation.mode))
        return self.look_answer(situation)

    def translate(self, titles, settings):
        self.calls.append(("translate", tuple(titles)))
        return [(self.translations.get(t, t), t if t in self.translations else None)
                for t in titles]


def kinds(services):
    return [call[0] for call in services.calls]


@pytest.fixture
def services():
    return FakeServices()


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
    return ai.settings_from({ai.SETTING_KEY: "k"})


def options(*methods, **kw):
    return autoname.Options(methods=set(methods), **kw)


def release(tracks, rid="r1"):
    return rid, [{"title": "", "format": "CD", "tracks": [
        {"title": title, "length": length} for title, length in tracks
    ]}]


def result(chapters, rows, mode=ai_chapters.NAME):
    return ai_chapters.Result(mode, chapters, rows, "", "", 0, "m")


def named(chapters, names, source="ai"):
    out = [dict(c) for c in chapters]
    for i, title in names.items():
        out[i].update(title=title, source=source)
    return out


class TestMusicBrainz:
    def test_a_release_that_is_the_whole_video_places_its_chapters(self, services, settings):
        rid, media = release([("Megitsune", 300.0), ("Karate", 312.0), ("The One", 600.0)])
        services.releases = [{"id": rid, "title": "Show at Budokan", "date": "2014-01-01",
                              "artist": "Artist"}]
        services.media[rid] = media
        outcome = autoname.identify("v", file_video(), options(autoname.MUSICBRAINZ), settings,
                                    services, autoname.Budget(0), "/lib")
        assert outcome.before.state == naming.UNSPLIT and outcome.after.state == naming.NAMED
        assert [c["title"] for c in outcome.change.chapters] == ["Megitsune", "Karate", "The One"]
        assert [c["start"] for c in outcome.change.chapters] == [0.0, 300.0, 612.0]
        assert outcome.change.origin == library.ORIGIN_TRACKLIST
        assert outcome.change.release_id == "r1"
        assert outcome.log == [
            "MusicBrainz: placed 3 chapters by the lengths of “Show at Budokan” (2014)"
        ]
        assert services.calls[0] == ("search", "Artist Show Concert")

    def test_a_release_that_doesnt_add_up_is_not_used(self, services, settings):
        rid, media = release([("Megitsune", 300.0)])
        services.releases = [{"id": rid, "title": "Show", "artist": "Artist"}]
        services.media[rid] = media
        outcome = autoname.identify("v", file_video(), options(autoname.MUSICBRAINZ), settings,
                                    services, autoname.Budget(0))
        assert outcome.change is None
        assert outcome.log[0].startswith("MusicBrainz: no release of this show whose tracks")

    def test_existing_chapters_are_named_but_real_names_are_kept(self, services, settings):
        rid, media = release([("Intro", 100.0), ("Megitsune", 500.0), ("Karate", 612.0)])
        services.releases = [{"id": rid, "title": "The Show", "artist": "Artist"}]
        services.media[rid] = media
        video = file_video(starts=(0.0, 100.0, 600.0), titles=["Chapter 01", "My Name", None])
        outcome = autoname.identify("v", video, options(autoname.MUSICBRAINZ), settings,
                                    services, autoname.Budget(0))
        assert [c["title"] for c in outcome.change.chapters] == ["Intro", "My Name", "Karate"]
        assert outcome.change.origin is None, "a file's own chapters stay its own"
        assert video["chapters"][0]["title"] == "Chapter 01", "the video itself is untouched"


    def test_someone_elses_album_that_happens_to_fit_is_turned_down(self, services, settings):
        # "Red Fox" by Dopedemand is 54:40; BABYMETAL's Red Fox Festival 53:49.
        rid, media = release([("Everybody's Got to Be a Drummer", 3280.0)])
        services.releases = [{"id": rid, "title": "Red Fox", "artist": "Dopedemand"}]
        services.media[rid] = media
        video = file_video(duration=3229.0)
        video["path"] = "/lib/Babymetal - The Fox Festivals In Japan 2017/2. RED FOX.mkv"
        outcome = autoname.identify("v", video, options(autoname.MUSICBRAINZ), settings,
                                    services, autoname.Budget(0), "/lib")
        assert outcome.change is None
        assert ("media", rid) not in services.calls, "not even fetched"

    def test_a_release_whose_songs_arent_the_named_ones_is_turned_down(self, services,
                                                                      settings):
        rid, media = release([("Intro", 100.0), ("Other", 500.0), ("Songs", 612.0)])
        services.releases = [{"id": rid, "title": "Another Show", "artist": "Artist"}]
        services.media[rid] = media
        video = file_video(starts=(0.0, 100.0, 600.0), titles=["Megitsune", "Karate", None])
        outcome = autoname.identify("v", video, options(autoname.MUSICBRAINZ), settings,
                                    services, autoname.Budget(0))
        assert outcome.change is None
        assert "“Another Show” adds up but its songs aren't this video's" in outcome.log[0]


    def test_close_is_not_close_enough_unattended(self, services, settings):
        # 34s out on 87 minutes: inside the 3% a person may check, not here.
        rid, media = release([("A", 2600.0), ("B", 2664.0)])
        services.releases = [{"id": rid, "title": "Show", "artist": "Artist"}]
        services.media[rid] = media
        outcome = autoname.identify("v", file_video(duration=5230.0), options(autoname.MUSICBRAINZ),
                                    settings, services, autoname.Budget(0))
        assert outcome.change is None

    def test_one_release_names_one_video_a_run(self, settings):
        services = FakeServices()
        services.claimed = {}
        rid, media = release([("Megitsune", 300.0), ("Karate", 912.0)])
        services.releases = [{"id": rid, "title": "Show", "artist": "Artist"}]
        services.media[rid] = media
        first = file_video()
        second = dict(file_video(), display_name="Another night")
        a = autoname.identify("a", first, options(autoname.MUSICBRAINZ), settings, services,
                              autoname.Budget(0))
        b = autoname.identify("b", second, options(autoname.MUSICBRAINZ), settings, services,
                              autoname.Budget(0))
        assert a.change is not None and b.change is None
        assert "already named “Concert”" in b.log[0]


class TestMusicBrainzWithTheAI:
    """With the AI to look at the video too, it puts a MusicBrainz search
    right and says which release is the show - but a release must still
    add up to the video."""

    def video(self):
        # Romanised folders: MusicBrainz has the artist in Japanese.
        video = file_video(duration=1212.0)
        video["path"] = "/lib/Perfume/Budokan 2008/Concert.mkv"
        return video

    def chose(self, rid, confidence="high"):
        return ai_musicbrainz.Choice(rid, confidence, "the Budokan show")

    def test_it_chooses_a_release_the_names_dont_mention(self, services, settings):
        rid, media = release([("Polyrhythm", 600.0), ("Chocolate Disco", 612.0)])
        services.releases = [{"id": rid, "title": "BUDOUKANん", "artist": "パフューム"}]
        services.media[rid] = media
        services.choice = self.chose(rid)
        outcome = autoname.identify("v", self.video(),
                                    options(autoname.MUSICBRAINZ, autoname.AI_LOOK), settings,
                                    services, autoname.Budget(6), "/lib")
        assert [c["title"] for c in outcome.change.chapters] == ["Polyrhythm", "Chocolate Disco"]
        assert outcome.change.release_id == rid
        assert ("choose", (rid,)) in services.calls
        assert outcome.ai_used == 1
        assert "MusicBrainz: the AI chose “BUDOUKANん” - the Budokan show" in outcome.log

    def test_without_it_such_a_release_isnt_even_fetched(self, services, settings):
        rid, media = release([("Polyrhythm", 600.0), ("Chocolate Disco", 612.0)])
        services.releases = [{"id": rid, "title": "BUDOUKANん", "artist": "パフューム"}]
        services.media[rid] = media
        outcome = autoname.identify("v", self.video(), options(autoname.MUSICBRAINZ), settings,
                                    services, autoname.Budget(6), "/lib")
        assert outcome.change is None
        assert kinds(services) == ["search"]

    def test_it_searches_again_when_nothing_fits(self, services, settings):
        rid, media = release([("Polyrhythm", 600.0), ("Chocolate Disco", 612.0)])
        services.releases = []
        better = 'release:"Budokan" AND artist:"Perfume"'
        services.searches[better] = [{"id": rid, "title": "Live at Budokan",
                                      "artist": "Perfume"}]
        services.media[rid] = media
        services.queries = [better]
        outcome = autoname.identify("v", self.video(),
                                    options(autoname.MUSICBRAINZ, autoname.AI_LOOK), settings,
                                    services, autoname.Budget(6), "/lib")
        assert outcome.change.release_id == rid
        assert kinds(services) == ["search", "better_queries", "search", "media"]
        assert ("search", better) in services.calls
        assert outcome.ai_used == 1, "the rules recognise what the new search found"

    def test_it_chooses_between_releases_that_all_add_up(self, services, settings):
        night1, media1 = release([("A", 600.0), ("B", 612.0)], rid="night1")
        night2, media2 = release([("A", 601.0), ("B", 609.0)], rid="night2")
        services.releases = [{"id": night1, "title": "Budokan Day 1", "artist": "Perfume"},
                             {"id": night2, "title": "Budokan Day 2", "artist": "Perfume"}]
        services.media.update({night1: media1, night2: media2})
        services.choice = self.chose(night2)
        outcome = autoname.identify("v", self.video(),
                                    options(autoname.MUSICBRAINZ, autoname.AI_LOOK), settings,
                                    services, autoname.Budget(6), "/lib")
        assert outcome.change.release_id == "night2", "not the closest; the one it is"

    def test_when_it_says_none_fits_nothing_is_used(self, services, settings):
        rid, media = release([("Polyrhythm", 600.0), ("Chocolate Disco", 612.0)])
        services.releases = [{"id": rid, "title": "Some Album", "artist": "Someone"}]
        services.media[rid] = media
        outcome = autoname.identify("v", self.video(),
                                    options(autoname.MUSICBRAINZ, autoname.AI_LOOK), settings,
                                    services, autoname.Budget(6), "/lib")
        assert outcome.change is None or outcome.change.release_id is None
        assert kinds(services)[:4] == ["search", "media", "choose", "better_queries"]

    def test_a_guess_is_left_out_unattended(self, services, settings):
        rid, media = release([("Polyrhythm", 600.0), ("Chocolate Disco", 612.0)])
        services.releases = [{"id": rid, "title": "BUDOUKANん", "artist": "パフューム"}]
        services.media[rid] = media
        services.choice = self.chose(rid, "low")
        outcome = autoname.identify(
            "v", self.video(), options(autoname.MUSICBRAINZ, autoname.AI_LOOK, only_sure=True),
            settings, services, autoname.Budget(6), "/lib",
        )
        assert outcome.change is None or outcome.change.release_id is None
        guessed = autoname.identify(
            "v", self.video(), options(autoname.MUSICBRAINZ, autoname.AI_LOOK, only_sure=False),
            settings, services, autoname.Budget(6), "/lib",
        )
        assert guessed.change.release_id == rid

    def test_a_release_that_doesnt_add_up_isnt_offered(self, services, settings):
        rid, media = release([("Polyrhythm", 600.0)])
        services.releases = [{"id": rid, "title": "BUDOUKANん", "artist": "パフューム"}]
        services.media[rid] = media
        services.choice = self.chose(rid)
        outcome = autoname.identify("v", self.video(),
                                    options(autoname.MUSICBRAINZ, autoname.AI_LOOK), settings,
                                    services, autoname.Budget(6), "/lib")
        assert outcome.change is None or outcome.change.release_id is None
        assert "choose" not in kinds(services)

    def test_no_budget_left_means_the_rules_alone(self, services, settings):
        rid, media = release([("Polyrhythm", 600.0), ("Chocolate Disco", 612.0)])
        services.releases = [{"id": rid, "title": "BUDOUKANん", "artist": "パフューム"}]
        services.media[rid] = media
        autoname.identify("v", self.video(), options(autoname.MUSICBRAINZ, autoname.AI_LOOK),
                          settings, services, autoname.Budget(0), "/lib")
        assert kinds(services) == ["search"]


class TestTheRightReleaseFirst:
    def test_with_one_the_names_recognise_the_rest_arent_fetched(self, services, settings):
        right, media = release([("Arkadia", 600.0), ("Megitsune", 612.0)], rid="right")
        services.releases = [
            {"id": "album", "title": "BABYMETAL", "artist": "BABYMETAL"},
            {"id": right, "title": "Show", "artist": "Artist"},
        ]
        services.media[right] = media
        outcome = autoname.identify("v", file_video(),
                                    options(autoname.MUSICBRAINZ, autoname.AI_LOOK),
                                    settings, services, autoname.Budget(6), "/lib")
        assert outcome.change.release_id == "right"
        assert ("media", "album") not in services.calls

    def test_the_ai_sees_the_tracklist_and_may_put_this_runs_names_right(self, services,
                                                                       settings):
        # An opening film inside the CD's first track: the lengths give the
        # song's name to the film, and the AI, seeing it, puts it right.
        rid, media = release([("Arkadia", 645.0), ("Megitsune", 567.0)])
        services.releases = [{"id": rid, "title": "Show", "artist": "Artist"}]
        services.media[rid] = media
        video = file_video(starts=(0.0, 363.0, 645.0))
        seen = {}

        def look(situation):
            seen["tracks"] = [t["title"] for t in situation.tracks]
            chapters = named(situation.chapters, {0: "Intro to Arkadia", 1: "Arkadia"})
            return result(chapters, [
                ai_chapters.Row(1, 0.0, "Intro to Arkadia", None, "high", "", False),
                ai_chapters.Row(2, 0.0, "Arkadia", None, "high", "", False)])

        services.look_answer = look
        outcome = autoname.identify("v", video, options(autoname.MUSICBRAINZ, autoname.AI_LOOK),
                                    settings, services, autoname.Budget(6), "/lib")
        assert seen["tracks"] == ["Arkadia", "Megitsune"]
        assert [c["title"] for c in outcome.change.chapters] == [
            "Intro to Arkadia", "Arkadia", "Megitsune"]

    def test_but_not_the_names_the_video_already_had(self, services, settings):
        video = file_video(starts=(0.0, 363.0), titles=["Mine", None])

        def look(situation):
            chapters = named(situation.chapters, {0: "Theirs", 1: "Arkadia"})
            return result(chapters, [ai_chapters.Row(1, 0.0, "Theirs", None, "high", "", False),
                                     ai_chapters.Row(2, 0.0, "Arkadia", None, "high", "", False)])

        services.look_answer = look
        outcome = autoname.identify("v", video, options(autoname.AI_LOOK), settings,
                                    services, autoname.Budget(6), "/lib")
        assert [c["title"] for c in outcome.change.chapters] == ["Mine", "Arkadia"]


class TestMatching:
    def test_the_artist_can_be_in_the_folders_or_the_name(self):
        video = {"path": "/lib/Babymetal - Live/x.mkv", "display_name": "x"}
        assert autoname.artist_matches({"artist": "BABYMETAL"}, video, "/lib")
        assert not autoname.artist_matches({"artist": "Dopedemand"}, video, "/lib")
        both = {"artist": "BABYMETAL & Electric Callboy"}
        assert autoname.artist_matches(both, {"path": "/lib/Electric Callboy/y.mkv",
                                              "display_name": "y"}, "/lib")

    def test_a_self_titled_album_isnt_a_concert(self):
        video = {"path": "/lib/BABYMETAL/[2015.04.23] BABYMETAL Apocrypha The Black Mass.mkv",
                 "display_name": "[2015.04.23] BABYMETAL Apocrypha The Black Mass"}
        album = {"artist": "BABYMETAL", "title": "BABYMETAL"}
        assert not autoname.title_matches(album, video, "/lib")
        wembley = {"path": "/lib/BABYMETAL/[2016.11.23] LIVE AT WEMBLEY", "display_name": "x"}
        assert autoname.title_matches({"artist": "BABYMETAL", "title": "LIVE AT WEMBLEY"},
                                      wembley, "/lib")
        budokan = {"path": "/lib/BABYMETAL/10-BABYMETAL-BUDOKAN_THE-ONE-EDITION/2. D.mkv",
                   "display_name": "2. Doomsday III, IV"}
        assert autoname.title_matches({"artist": "BABYMETAL", "title": "10 BABYMETAL BUDOKAN"},
                                      budokan, "/lib")

    def test_names_agree_loosely(self):
        tracks = [{"title": "Gimme Chocolate!!"}, {"title": "Doki Doki☆Morning"},
                  {"title": "Road of Resistance"}]
        chapters = [{"title": "gimme chocolate"}, {"title": "Doki Doki Morning"},
                    {"title": "Chapter 03"}]
        assert autoname.agrees_with_names(tracks, chapters)
        assert not autoname.agrees_with_names(
            [{"title": "Everybody's Got to Be a Drummer"}, {"title": "Leaving Europa"}],
            chapters,
        )
        assert autoname.agrees_with_names([], [{"title": "Only one"}])


class TestASingleSong:
    def clip(self, title="BABYMETAL - Headbanger!! (Live at Metrock)", duration=268.0):
        video = file_video(duration=duration, titles=[title])
        video["chapters"][0]["source"] = naming.FILENAME_SOURCE
        video["path"] = "/lib/BABYMETAL/clip.mp4"
        return video

    def test_a_recording_confirms_and_tidies_the_name(self, services, settings):
        services.recordings = [
            {"id": "a", "title": "Headbanger!!", "artist": "BABYMETAL", "length": 250.0},
            {"id": "b", "title": "Head", "artist": "BABYMETAL", "length": 260.0},
            {"id": "c", "title": "Headbanger!!", "artist": "Someone Else", "length": 268.0},
        ]
        outcome = autoname.identify("v", self.clip(), options(autoname.MUSICBRAINZ), settings,
                                    services, autoname.Budget(0), "/lib")
        assert outcome.before.state == naming.UNVERIFIED
        assert outcome.after.state == naming.SINGLE
        assert outcome.change.chapters[0]["title"] == "Headbanger!!"
        assert outcome.change.chapters[0]["source"] == "musicbrainz"
        assert outcome.log == ["MusicBrainz: it's “Headbanger!!” by BABYMETAL"]
        assert ("recordings", "BABYMETAL - Headbanger!! (Live at Metrock)") in services.calls

    def test_a_recording_of_another_length_or_title_isnt_it(self, services, settings):
        services.recordings = [
            {"id": "a", "title": "Headbanger!!", "artist": "BABYMETAL", "length": 900.0},
            {"id": "b", "title": "Megitsune", "artist": "BABYMETAL", "length": 268.0},
        ]
        outcome = autoname.identify("v", self.clip(), options(autoname.MUSICBRAINZ), settings,
                                    services, autoname.Budget(0), "/lib")
        assert outcome.change is None and outcome.after.state == naming.UNVERIFIED

    def test_the_ai_can_name_it_over_the_files_name(self, services, settings):
        services.look_answer = lambda s: result(
            named(s.chapters, {0: "Headbanger!!"}),
            [ai_chapters.Row(1, 0.0, "Headbanger!!", None, "high", "", False)],
        )
        outcome = autoname.identify("v", self.clip(), options(autoname.AI_LOOK), settings,
                                    services, autoname.Budget(3), "/lib")
        assert outcome.change.chapters[0]["title"] == "Headbanger!!"
        assert outcome.after.state == naming.SINGLE

    def test_they_can_be_left_out_of_a_run(self):
        videos = [("clip", self.clip())]
        assert autoname.candidates(videos, autoname.Options(include_unverified=False)) == []


class TestAudio:
    def test_a_video_in_one_piece_is_split_where_the_music_stops(self, services, settings):
        levels, starts = concert([300, 300, 300, 300], gap=12.0)
        services.levels_value = levels
        video = file_video(duration=levels.duration)
        outcome = autoname.identify("v", video, options(autoname.AUDIO), settings, services,
                                    autoname.Budget(0))
        assert len(outcome.change.chapters) == 4
        assert outcome.change.origin == library.ORIGIN_ESTIMATED
        assert outcome.after.state == naming.UNNAMED

    def test_an_estimate_is_not_estimated_again(self, services, settings):
        video = file_video(starts=(0.0, 600.0), origin=library.ORIGIN_ESTIMATED)
        outcome = autoname.identify("v", video, options(autoname.AUDIO), settings, services,
                                    autoname.Budget(0))
        assert outcome.change is None and services.calls == []

    def test_but_musicbrainz_lengths_may_replace_an_unnamed_estimate(self, services, settings):
        rid, media = release([("Megitsune", 300.0), ("Karate", 912.0)])
        services.releases = [{"id": rid, "title": "The Show", "artist": "Artist"}]
        services.media[rid] = media
        video = file_video(starts=(0.0, 580.0), origin=library.ORIGIN_ESTIMATED)
        outcome = autoname.identify("v", video, options(autoname.MUSICBRAINZ), settings,
                                    services, autoname.Budget(0))
        assert [c["start"] for c in outcome.change.chapters] == [0.0, 300.0]
        assert outcome.change.origin == library.ORIGIN_TRACKLIST

    def test_a_named_video_is_never_re_split(self, services, settings):
        video = file_video(starts=(0.0, 600.0), titles=["A", None])
        outcome = autoname.identify("v", video, options(autoname.AUDIO), settings, services,
                                    autoname.Budget(0))
        assert outcome.change is None and services.calls == []


class TestTheAI:
    def disc(self):
        return menu_chapters.DiscMenu(buttons=[menu_chapters.ChapterButton(1, 0, None)])

    def menu_answer(self, video):
        chapters = named(video["chapters"], {0: "Opening", 1: "Megitsune"})
        chapters[1]["source"] = "menu"
        return result(chapters, [
            ai_chapters.Row(1, 0.0, "Opening", None, "low", "not on the menu", False),
            ai_chapters.Row(2, 300.0, "Megitsune", None, "high", menu_chapters.MENU_NOTE, False),
        ])

    def test_a_blu_ray_is_named_from_its_menu_but_guesses_are_left_out(self, services,
                                                                         settings):
        video = file_video(starts=(0.0, 300.0), kind="bluray")
        services.menu = self.disc()
        services.menu_answer = self.menu_answer(video)
        budget = autoname.Budget(5)
        outcome = autoname.identify("v", video, options(autoname.MENU), settings, services, budget)
        assert [c["title"] for c in outcome.change.chapters] == [None, "Megitsune"]
        assert outcome.change.chapters[1]["source"] == "menu"
        assert (budget.used, outcome.ai_used) == (1, 1)

    def test_with_guesses_allowed_they_go_in_too(self, services, settings):
        video = file_video(starts=(0.0, 300.0), kind="bluray")
        services.menu = self.disc()
        services.menu_answer = self.menu_answer(video)
        outcome = autoname.identify("v", video, options(autoname.MENU, only_sure=False),
                                    settings, services, autoname.Budget(5))
        assert [c["title"] for c in outcome.change.chapters] == ["Opening", "Megitsune"]

    def test_no_menu_costs_nothing(self, services, settings):
        services.menu = menu_chapters.MenuError("This disc's menus are written in Java")
        budget = autoname.Budget(5)
        outcome = autoname.identify("v", file_video(starts=(0.0, 300.0), kind="bluray"),
                                    options(autoname.MENU), settings, services, budget)
        assert budget.used == 0
        assert outcome.log == ["Disc menu: This disc's menus are written in Java"]

    def test_the_budget_stops_the_ai(self, services, settings):
        services.menu = self.disc()
        outcome = autoname.identify("v", file_video(starts=(0.0, 300.0), kind="bluray"),
                                    options(autoname.MENU), settings, services, autoname.Budget(0))
        assert "ask_menu" not in kinds(services)
        assert outcome.log == ["AI: this run's requests are used up"]

    def test_without_a_key_no_ai_is_asked(self, services, monkeypatch):
        monkeypatch.delenv(ai.API_KEY_ENV, raising=False)
        services.menu = self.disc()
        outcome = autoname.identify("v", file_video(starts=(0.0, 300.0), kind="bluray"),
                                    options(autoname.MENU), ai.settings_from({}), services,
                                    autoname.Budget(5))
        assert outcome.log == ["AI: no Nano-GPT key is set"]

    def test_looking_names_chapters_it_is_sure_of(self, services, settings):
        video = file_video(starts=(0.0, 300.0, 600.0))
        services.look_answer = lambda s: result(
            named(s.chapters, {0: "Opening", 1: "Megitsune", 2: "Karate"}),
            [ai_chapters.Row(1, 0.0, "Opening", None, "high", "", False),
             ai_chapters.Row(2, 300.0, "Megitsune", None, "medium", "", False),
             ai_chapters.Row(3, 600.0, "Karate", None, "low", "", False)],
        )
        outcome = autoname.identify("v", video, options(autoname.AI_LOOK), settings, services,
                                    autoname.Budget(5))
        assert [c["title"] for c in outcome.change.chapters] == ["Opening", "Megitsune", None]
        assert outcome.log == ["AI: named 2 chapter(s), left 1 it was guessing at"]

    def test_estimated_chapters_are_placed_with_the_audio(self, services, settings):
        levels, _ = concert([300, 300, 300], gap=12.0)
        services.levels_value = levels
        video = file_video(starts=(0.0, 300.0), duration=levels.duration,
                           origin=library.ORIGIN_ESTIMATED)
        seen = {}

        def look(situation):
            seen["candidates"] = situation.candidates
            chapters = named([dict(c) for c in situation.chapters], {0: "A", 1: "B"})
            return result(chapters, [
                ai_chapters.Row(1, 0.0, "A", None, "high", "", False),
                ai_chapters.Row(2, 300.0, "B", None, "high", "", True),
            ], mode=situation.mode)

        services.look_answer = look
        outcome = autoname.identify("v", video, options(autoname.AI_LOOK), settings, services,
                                    autoname.Budget(5))
        assert ("look", ai_chapters.PLACE) in services.calls
        assert seen["candidates"], "the audio's gaps go with it"
        assert outcome.change.origin == library.ORIGIN_ESTIMATED

    def test_a_video_in_one_piece_isnt_looked_at_unsplit(self, services, settings):
        outcome = autoname.identify("v", file_video(), options(autoname.AI_LOOK), settings,
                                    services, autoname.Budget(5))
        assert services.calls == [] and "needs splitting first" in outcome.log[0]

    def test_foreign_titles_are_translated_keeping_the_original(self, services, settings):
        video = file_video(starts=(0.0, 300.0), titles=["メギツネ", "Doki Doki в"])
        services.translations = {"メギツネ": "Megitsune", "Doki Doki в": "Doki Doki☆Morning"}
        outcome = autoname.identify("v", video, options(autoname.TRANSLATE), settings, services,
                                    autoname.Budget(5))
        assert [(c["title"], c.get("original_title")) for c in outcome.change.chapters] == [
            ("Megitsune", "メギツネ"), ("Doki Doki☆Morning", "Doki Doki в"),
        ]


class TestTheMenuPutsThisRunRight:
    def test_the_menu_replaces_this_runs_guesses_but_never_what_was_there(self, services,
                                                                          settings):
        # MusicBrainz first (last resort): its first song lands on the intro.
        rid, media = release([("BABYMETAL DEATH", 450.0), ("Gimmie Chocolate", 450.0),
                              ("Karate", 312.0)])
        services.releases = [{"id": rid, "title": "Show", "artist": "Artist"}]
        services.media[rid] = media
        video = file_video(starts=(0.0, 126.0, 450.0, 900.0), duration=1212.0,
                           titles=[None, None, None, "Kept"], kind="bluray")
        services.menu = menu_chapters.DiscMenu()
        chapters = named(video["chapters"],
                         {1: "BABYMETAL DEATH", 2: "Gimme Chocolate!!", 3: "KARATE"}, "menu")
        chapters[0].update(title="Opening", source="ai")
        services.menu_answer = result(chapters, [
            ai_chapters.Row(1, 0.0, "Opening", None, "high", "not on the menu", False)])
        outcome = autoname.identify("v", video, options(autoname.MUSICBRAINZ, autoname.MENU),
                                    settings, services, autoname.Budget(5))
        assert [c["title"] for c in outcome.change.chapters] == [
            "Opening", "BABYMETAL DEATH", "Gimme Chocolate!!", "Kept",
        ]
        assert [c["source"] for c in outcome.change.chapters][:3] == ["ai", "menu", "menu"]


class TestOrder:
    def setup_blu_ray(self, services):
        rid, media = release([("One", 300.0), ("Two", 312.0 + 600.0)])
        services.releases = [{"id": rid, "title": "Show", "artist": "Artist"}]
        services.media[rid] = media
        services.menu = menu_chapters.DiscMenu()
        video = file_video(starts=(0.0, 300.0), kind="bluray")
        services.menu_answer = result(named(video["chapters"], {0: "M1", 1: "M2"}, "menu"), [])
        return video

    def test_free_first_ai_only_if_still_needed(self, services, settings):
        video = self.setup_blu_ray(services)
        outcome = autoname.identify("v", video, options(autoname.MUSICBRAINZ, autoname.MENU),
                                    settings, services, autoname.Budget(5))
        assert "read_menu" not in kinds(services)
        assert [c["title"] for c in outcome.change.chapters] == ["One", "Two"]

    def test_most_accurate_first_reads_the_menu_before_musicbrainz(self, services, settings):
        video = self.setup_blu_ray(services)
        outcome = autoname.identify(
            "v", video, options(autoname.MUSICBRAINZ, autoname.MENU,
                                ai_policy=autoname.ACCURATE_FIRST),
            settings, services, autoname.Budget(5),
        )
        assert "search" not in kinds(services)
        assert [c["title"] for c in outcome.change.chapters] == ["M1", "M2"]


class TestALibrary:
    def test_only_videos_needing_it_are_worked_on(self):
        videos = [("done", file_video(starts=(0.0, 1.0), titles=["A", "B"])),
                  ("part", file_video(starts=(0.0, 1.0), titles=["A", None])),
                  ("none", file_video(starts=(0.0, 1.0))),
                  ("short", file_video(duration=200.0)),
                  ("hidden", file_video())]
        chosen = autoname.candidates(videos, autoname.Options(), skip={"hidden"})
        assert [v for v, _ in chosen] == ["part", "none", "short"], "short: unverified"
        chosen = autoname.candidates(videos, autoname.Options(include_partly=False))
        assert [v for v, _ in chosen] == ["none", "short", "hidden"]

    def test_a_run_reports_each_video_and_survives_a_bad_one(self, services, settings):
        class Broken(FakeServices):
            def search_releases(self, query):
                raise RuntimeError("boom")

        started, outcomes = [], []
        autoname.run([("a", file_video()), ("b", file_video())], options(autoname.MUSICBRAINZ),
                     settings, Broken(), on_started=lambda *a: started.append(a[:2]),
                     on_outcome=outcomes.append)
        assert started == [(0, 2), (1, 2)]
        assert [o.error for o in outcomes] == ["boom", "boom"]

    def test_it_stops_when_asked(self, services, settings):
        cancel = threading.Event()
        outcomes = []

        def started(n, total, video_id, name):
            cancel.set()

        autoname.run([("a", file_video()), ("b", file_video())], options(autoname.AUDIO),
                     settings, services, cancel=cancel, on_started=started,
                     on_outcome=outcomes.append)
        assert outcomes == []

    def test_free_methods_alone_have_no_ai_budget(self, services, settings):
        budget = autoname.run([], options(autoname.MUSICBRAINZ, ai_budget=10), settings, services)
        assert budget.remaining == 0


class TestUndo:
    def test_a_run_can_be_undone(self, tmp_path):
        data = store.default_library(str(tmp_path))
        before = file_video(starts=(0.0, 300.0))
        data["videos"] = {"v": before, "w": file_video()}
        autoname.start_journal(data)
        change = autoname.Change(named(before["chapters"], {0: "A"}), library.ORIGIN_TRACKLIST,
                                 "rel")
        autoname.apply(data, "v", change)
        autoname.apply(data, "v", autoname.Change(named(before["chapters"], {1: "B"}), None))
        assert data["videos"]["v"]["chapters"][1]["title"] == "B"
        assert autoname.last_run(data)["before"].keys() == {"v"}
        assert autoname.undo(data) == ["v"]
        video = data["videos"]["v"]
        assert [c["title"] for c in video["chapters"]] == [None, None]
        assert "chapter_origin" not in video and "musicbrainz_release_id" not in video
        assert autoname.last_run(data) is None

    def test_nothing_to_undo(self, tmp_path):
        assert autoname.undo(store.default_library(str(tmp_path))) == []
