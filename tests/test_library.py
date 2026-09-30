# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Rescan behaviour, with ffprobe and the scanner stubbed out so the tests
describe the merge and reuse rules rather than exercising ffmpeg.
"""

import copy

import pytest

from mediabrowser.core import config, ffprobe_chapters, library, scanner, store


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LIBRARIES_DIR", tmp_path / "data" / "libraries")
    monkeypatch.setattr(config, "LEGACY_LIBRARY_FILE", tmp_path / "data" / "library.json")
    monkeypatch.setattr(config, "ARTWORK_DIR", tmp_path / "data" / "artwork")


@pytest.fixture
def media(tmp_path):
    root = tmp_path / "Videos"
    root.mkdir()
    path = root / "Concert.mkv"
    path.write_bytes(b"x" * 100)
    return root, path


@pytest.fixture
def probes(monkeypatch, media):
    """Counts how often ffprobe would actually have been run."""
    _root, path = media
    calls = []

    def fake_probe(target, **kw):
        calls.append(str(target))
        return {
            "duration": 300.0,
            "chapters": [
                {"title": None, "start": 0.0, "end": 150.0},
                {"title": None, "start": 150.0, "end": 300.0},
            ],
        }

    monkeypatch.setattr(ffprobe_chapters, "read_regular_file_info", fake_probe)
    monkeypatch.setattr(scanner, "find_media_units", lambda root, **kw: [("file", path)])
    return calls


class TestIncrementalRescan:
    def test_a_first_scan_probes_the_file(self, media, probes):
        root, _ = media
        data = library.rescan(root)
        assert len(probes) == 1
        assert len(data["videos"]) == 1

    def test_an_unchanged_file_is_not_probed_again(self, media, probes):
        root, _ = media
        library.rescan(root)
        library.rescan(root)
        assert len(probes) == 1, "a rescan re-probed a file that had not changed"

    def test_a_changed_file_is_probed_again(self, media, probes):
        root, path = media
        library.rescan(root)
        path.write_bytes(b"y" * 250)  # different size
        library.rescan(root)
        assert len(probes) == 2

    def test_force_probes_everything_regardless(self, media, probes):
        root, _ = media
        library.rescan(root)
        library.rescan(root, force=True)
        assert len(probes) == 2

    def test_names_survive_an_incremental_rescan(self, media, probes):
        root, _ = media
        data = library.rescan(root)
        video_id = next(iter(data["videos"]))
        data["videos"][video_id]["chapters"][0]["title"] = "Megitsune"
        data["videos"][video_id]["chapters"][0]["source"] = "manual"
        store.save_library(data)

        again = library.rescan(root)
        assert again["videos"][video_id]["chapters"][0]["title"] == "Megitsune"

    def test_names_survive_a_full_reprobe_too(self, media, probes):
        root, path = media
        data = library.rescan(root)
        video_id = next(iter(data["videos"]))
        data["videos"][video_id]["chapters"][0]["title"] = "Megitsune"
        data["videos"][video_id]["chapters"][0]["source"] = "manual"
        store.save_library(data)

        path.write_bytes(b"y" * 250)
        again = library.rescan(root)
        assert len(probes) == 2
        assert again["videos"][video_id]["chapters"][0]["title"] == "Megitsune"


class TestFingerprint:
    def test_it_changes_with_the_contents(self, media):
        _root, path = media
        before = library.file_fingerprint(path)
        path.write_bytes(b"y" * 500)
        assert library.file_fingerprint(path) != before

    def test_a_file_that_is_not_there_has_none(self, tmp_path):
        assert library.file_fingerprint(tmp_path / "nope.mkv") is None


class TestMissingVideos:
    def test_a_file_still_present_is_not_reported(self, media, probes):
        root, _ = media
        assert library.missing_videos(library.rescan(root)) == []

    def test_a_file_that_has_gone_is_reported(self, media, probes):
        root, path = media
        data = library.rescan(root)
        path.unlink()
        assert library.missing_videos(data) == list(data["videos"])

    def test_an_empty_library_reports_nothing(self):
        assert library.missing_videos(store.default_library()) == []


class TestChaptersTheAppMade:
    """A file without chapter marks has its chapters only in the library, so
    a rescan must not swap them back for ffprobe's single whole-file one.
    """

    @pytest.fixture
    def chapterless(self, monkeypatch, media):
        _root, path = media
        info = {"duration": 300.0, "chapters": [{"title": None, "start": 0.0, "end": 300.0}]}
        monkeypatch.setattr(ffprobe_chapters, "read_regular_file_info",
                            lambda target, **kw: copy.deepcopy(info))
        monkeypatch.setattr(scanner, "find_media_units", lambda root, **kw: [("file", path)])
        return info

    def _give_three_chapters(self, root):
        data = library.rescan(root)
        video_id = next(iter(data["videos"]))
        video = data["videos"][video_id]
        video["chapters"] = [
            {"index": i, "start": s, "end": e, "title": t, "source": "musicbrainz"}
            for i, (s, e, t) in enumerate([(0, 100, "A"), (100, 200, "B"), (200, 300, "C")])
        ]
        video["chapter_origin"] = library.ORIGIN_TRACKLIST
        store.save_library(data)
        return video_id

    def test_they_survive_a_forced_rescan(self, media, chapterless):
        root, _ = media
        video_id = self._give_three_chapters(root)
        again = library.rescan(root, force=True)["videos"][video_id]
        assert [c["title"] for c in again["chapters"]] == ["A", "B", "C"]
        assert again["chapter_origin"] == library.ORIGIN_TRACKLIST

    def test_they_survive_the_file_being_touched(self, media, chapterless):
        root, path = media
        video_id = self._give_three_chapters(root)
        path.write_bytes(b"y" * 250)
        again = library.rescan(root)["videos"][video_id]
        assert len(again["chapters"]) == 3

    def test_they_are_dropped_when_the_video_is_a_different_length(self, media, chapterless):
        # Placed against the old length, they'd be in the wrong places now.
        root, _ = media
        video_id = self._give_three_chapters(root)
        chapterless["duration"] = 250.0
        chapterless["chapters"][0]["end"] = 250.0
        again = library.rescan(root, force=True)["videos"][video_id]
        assert len(again["chapters"]) == 1
        assert "chapter_origin" not in again

    def test_resetting_reads_the_files_own_chapters(self, media, chapterless):
        root, _ = media
        video_id = self._give_three_chapters(root)
        video = store.load_library_for_root(root)["videos"][video_id]
        chapters = library.original_chapters(video, 120)
        assert len(chapters) == 1
        assert chapters[0]["source"] == "auto-numbered"


class TestCarriedOver:
    def test_the_musicbrainz_release_survives_a_reprobe(self, media, probes):
        # Cover art is fetched by it; losing it meant a frame grab instead.
        root, path = media
        data = library.rescan(root)
        video_id = next(iter(data["videos"]))
        data["videos"][video_id]["musicbrainz_release_id"] = "mbid-123"
        store.save_library(data)

        path.write_bytes(b"y" * 250)
        again = library.rescan(root)
        assert len(probes) == 2
        assert again["videos"][video_id]["musicbrainz_release_id"] == "mbid-123"

    def test_and_a_forced_one(self, media, probes):
        root, _ = media
        data = library.rescan(root)
        video_id = next(iter(data["videos"]))
        data["videos"][video_id]["musicbrainz_release_id"] = "mbid-123"
        store.save_library(data)
        again = library.rescan(root, force=True)
        assert again["videos"][video_id]["musicbrainz_release_id"] == "mbid-123"

    def test_hidden_folders_survive_a_rescan(self, media, probes):
        root, _ = media
        data = library.rescan(root)
        data["settings"]["hidden_folders"] = [str(root / "Extras")]
        store.save_library(data)
        assert library.rescan(root)["settings"]["hidden_folders"] == [str(root / "Extras")]


class TestNothingIsLostWhileMediaIsAway:
    """A dropped network share or unplugged drive must not cost any names."""

    def _named(self, root):
        data = library.rescan(root)
        video_id = next(iter(data["videos"]))
        data["videos"][video_id]["chapters"][0]["title"] = "Megitsune"
        data["videos"][video_id]["chapters"][0]["source"] = "manual"
        store.save_library(data)
        return video_id

    def test_a_missing_library_folder_changes_nothing(self, tmp_path, media, probes):
        root, _ = media
        video_id = self._named(root)
        root.rename(tmp_path / "unmounted")
        with pytest.raises(library.LibraryUnavailable):
            library.rescan(root)
        (tmp_path / "unmounted").rename(root)
        stored = store.load_library_for_root(root)
        assert stored["videos"][video_id]["chapters"][0]["title"] == "Megitsune"

    def test_a_missing_file_is_kept_with_its_names(self, media, probes, monkeypatch):
        root, path = media
        video_id = self._named(root)
        path.unlink()
        monkeypatch.setattr(scanner, "find_media_units", lambda root, **kw: [])
        data = library.rescan(root)
        assert data["videos"][video_id]["chapters"][0]["title"] == "Megitsune"
        assert library.missing_videos(data) == [video_id]

    def test_it_comes_back_named_when_the_file_does(self, media, probes, monkeypatch):
        root, path = media
        video_id = self._named(root)
        contents = path.read_bytes()
        path.unlink()
        monkeypatch.setattr(scanner, "find_media_units", lambda root, **kw: [])
        library.rescan(root)
        path.write_bytes(contents)
        monkeypatch.setattr(scanner, "find_media_units", lambda root, **kw: [("file", path)])
        data = library.rescan(root)
        assert data["videos"][video_id]["chapters"][0]["title"] == "Megitsune"
        assert library.missing_videos(data) == []

    def test_a_file_that_cannot_be_read_this_time_is_kept(self, media, probes, monkeypatch):
        root, path = media
        video_id = self._named(root)

        def failing(target, **kw):
            raise ffprobe_chapters.ProbeError("network read timed out")

        monkeypatch.setattr(ffprobe_chapters, "read_regular_file_info", failing)
        data = library.rescan(root, force=True)
        assert data["videos"][video_id]["chapters"][0]["title"] == "Megitsune"

    def test_a_file_that_is_there_but_no_longer_scanned_is_dropped(
        self, media, probes, monkeypatch
    ):
        # Present on disk but not a media unit any more (say, renamed to a
        # non-video extension in place): nothing is away, so nothing to keep.
        root, _ = media
        self._named(root)
        monkeypatch.setattr(scanner, "find_media_units", lambda root, **kw: [])
        assert library.rescan(root)["videos"] == {}

    def test_missing_videos_can_be_removed_deliberately(self, media, probes):
        root, path = media
        video_id = self._named(root)
        path.unlink()
        data = library.rescan(root)
        assert library.remove_videos(data, [video_id]) == 1
        assert data["videos"] == {}


class TestCancelling:
    def test_a_cancelled_scan_changes_nothing(self, media, probes):
        import threading

        root, _ = media
        video_id = next(iter(library.rescan(root)["videos"]))
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(library.ScanCancelled):
            library.rescan(root, force=True, cancel=cancel)
        assert video_id in store.load_library_for_root(root)["videos"]

    def test_a_first_scan_cancelled_leaves_no_library(self, media, probes):
        import threading

        root, _ = media
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(library.ScanCancelled):
            library.rescan(root, cancel=cancel)
        assert store.list_libraries() == []

    def test_the_walk_itself_stops_between_folders(self, tmp_path):
        # The slow part of a wrong choice is thousands of empty folders.
        import threading

        for i in range(50):
            (tmp_path / f"empty{i}").mkdir()
        cancel = threading.Event()
        seen = []

        def on_folder(folder):
            seen.append(folder)
            if len(seen) == 3:
                cancel.set()

        with pytest.raises(scanner.ScanCancelled):
            list(scanner.find_media_units(tmp_path, cancel=cancel, on_folder=on_folder))
        assert len(seen) == 3

    def test_a_slow_ffprobe_is_stopped(self, monkeypatch):
        import threading
        import time

        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        with pytest.raises(ffprobe_chapters.Cancelled):
            ffprobe_chapters._run_ffprobe(["sleep", "10"], cancel)
        assert time.monotonic() - started < 3


class TestNetworkLibraries:
    def test_the_address_is_remembered(self, media, probes):
        root, _ = media
        library.rescan(root, network_uri="smb://nas/media")
        assert library.network_uri_for(root) == "smb://nas/media"
        assert store.list_libraries()[0]["network_uri"] == "smb://nas/media"

    def test_a_folder_that_is_there_is_not_reconnected(self, media, probes):
        root, _ = media
        library.rescan(root, network_uri="smb://nas/media")

        def resolver(uri):
            raise AssertionError("should not reconnect")

        assert library.reconnect(root, resolver) == str(root.resolve())

    def test_a_dropped_share_is_reconnected_somewhere_new(self, tmp_path, media, probes):
        # kio-fuse puts a share under a new folder each session.
        from mediabrowser.core import artwork

        root, path = media
        data = library.rescan(root, network_uri="smb://nas/media")
        old_id = next(iter(data["videos"]))
        data["videos"][old_id]["chapters"][0]["title"] = "Megitsune"
        data["settings"]["hidden_folders"] = [str(root / "Extras")]
        store.save_library(data)
        config.ARTWORK_DIR.mkdir(parents=True, exist_ok=True)
        (config.ARTWORK_DIR / f"{old_id}.jpg").write_bytes(b"cover")

        new_root = tmp_path / "kio-fuse-XyZ" / "media"
        new_root.parent.mkdir()
        root.rename(new_root)

        result = library.reconnect(root, lambda uri: str(new_root))
        assert result == str(new_root.resolve())
        moved = store.load_library_for_root(new_root)
        (new_id, video), = moved["videos"].items()
        assert video["path"] == str(new_root.resolve() / path.name)
        assert video["chapters"][0]["title"] == "Megitsune"
        assert moved["settings"]["hidden_folders"] == [str(new_root.resolve() / "Extras")]
        assert artwork.lookup(new_id) is not None
        assert [lib["root"] for lib in store.list_libraries()] == [str(new_root.resolve())]

    def test_a_share_that_cannot_be_reached_says_so(self, media, probes, tmp_path):
        root, _ = media
        library.rescan(root, network_uri="smb://nas/media")
        root.rename(tmp_path / "gone")

        def resolver(uri):
            raise RuntimeError("no route to host")

        with pytest.raises(RuntimeError):
            library.reconnect(root, resolver)


class TestHiddenTitlesSurviveRescans:
    def test_a_hidden_blu_ray_title_stays_hidden(self, media, monkeypatch):
        # Every Blu-ray title is re-read on every rescan, so the flag has to
        # be carried over rather than just left in place.
        root, _ = media
        disc = root / "Worlds Collide"
        disc.mkdir()
        titles = [
            {"title_idx": idx, "playlist": idx, "duration": 600.0,
             "chapters": [{"title": None, "start": 0.0, "end": 600.0}]}
            for idx in (0, 7)
        ]
        monkeypatch.setattr(scanner, "find_media_units", lambda root, **kw: [("bluray", disc)])
        monkeypatch.setattr(library.bluray, "probe_bluray_disc", lambda path, minimum: [
            dict(t, chapters=[dict(c) for c in t["chapters"]]) for t in titles
        ])
        data = library.rescan(root)
        extra = store.make_video_id("bluray", disc, 0)
        concert = store.make_video_id("bluray", disc, 7)
        data["videos"][extra]["hidden"] = True
        store.save_library(data)

        again = library.rescan(root)
        assert again["videos"][extra].get("hidden") is True
        assert not again["videos"][concert].get("hidden")


class TestBluRayTitles:
    """Which of a disc's titles become videos, and what they're called."""

    @pytest.fixture
    def disc(self, media, monkeypatch):
        root, _ = media
        disc = root / "Omega Alive"
        disc.mkdir()
        monkeypatch.setattr(scanner, "find_media_units", lambda root, **kw: [("bluray", disc)])
        seen = {}

        def probe(path, minimum):
            seen["minimum"] = minimum
            return [
                {"title_idx": idx, "playlist": idx, "duration": length,
                 "chapters": [{"title": None, "start": 0.0, "end": length}]}
                for idx, length in ((0, 6332.0), (1, 77.0))
            ]

        monkeypatch.setattr(library.bluray, "probe_bluray_disc", probe)
        return root, disc, seen

    def test_extras_are_listed_now(self, disc):
        root, _, seen = disc
        data = library.rescan(root)
        assert seen["minimum"] == config.DEFAULT_MIN_BLURAY_TITLE_SECONDS == 20
        assert len(data["videos"]) == 2

    def test_a_library_on_the_old_cut_off_moves_to_the_new_one(self, disc):
        root, _, seen = disc
        data = store.default_library(root)
        data["settings"]["min_bluray_title_seconds"] = 120
        store.save_library(data)
        library.rescan(root)
        assert seen["minimum"] == 20

    def test_a_name_given_by_hand_survives_a_rescan_and_can_be_undone(self, disc):
        root, path, _ = disc
        data = library.rescan(root)
        extra = data["videos"][store.make_video_id("bluray", path, 1)]
        assert extra["display_name"] == "Omega Alive - Title 2"
        library.set_custom_name(extra, "  Behind the Scenes ")
        store.save_library(data)
        again = library.rescan(root)["videos"][store.make_video_id("bluray", path, 1)]
        assert again["display_name"] == "Behind the Scenes"
        library.set_custom_name(again, "")
        assert again["display_name"] == "Omega Alive - Title 2"
        assert "custom_name" not in again


class TestDuplicateTitles:
    def title(self, idx, playlist, length, chapters):
        return {"title_idx": idx, "playlist": playlist, "duration": length,
                "chapters": [{}] * chapters}

    def test_the_copy_with_chapters_is_kept(self):
        titles = [self.title(0, 3, 7900.4, 1), self.title(1, 1003, 7901.0, 22),
                  self.title(2, 2, 80.0, 1), self.title(3, 1002, 80.0, 1)]
        clips = {3: ["00003"], 1003: ["00003"], 2: ["00005"], 1002: ["00005"]}
        kept = library.bluray.drop_duplicates(titles, clips)
        assert [t["title_idx"] for t in kept] == [1, 2]

    def test_a_copy_with_a_stub_clip_added_is_the_same_video(self):
        # Nightwish's Decades: the chaptered copy adds a 0.4s clip.
        titles = [self.title(0, 1003, 7900.9, 22), self.title(1, 3, 7900.5, 1),
                  self.title(4, 1, 44.9, 1), self.title(8, 1001, 45.3, 2)]
        clips = {1003: ["00003", "00004"], 3: ["00003"], 1: ["00001"], 1001: ["00001", "00004"]}
        kept = library.bluray.drop_duplicates(titles, clips)
        assert [t["title_idx"] for t in kept] == [0, 8]

    def test_different_clips_or_lengths_are_different_videos(self):
        titles = [self.title(0, 1, 600.0, 1), self.title(1, 2, 600.0, 1),
                  self.title(2, 3, 900.0, 1)]
        clips = {1: ["00001"], 2: ["00002"], 3: ["00001"]}
        assert len(library.bluray.drop_duplicates(titles, clips)) == 3

    def test_without_playlists_to_go_by_nothing_is_dropped(self):
        titles = [self.title(0, 1, 600.0, 1), self.title(1, 2, 600.0, 1)]
        assert len(library.bluray.drop_duplicates(titles, {})) == 2
