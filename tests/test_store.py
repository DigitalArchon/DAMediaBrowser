# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Store tests. Every one redirects the data directory at a tmp_path, so a
test run never reads or writes the real ~/.local/share library.
"""

import pytest

from mediabrowser.core import config, library, store


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LIBRARIES_DIR", tmp_path / "libraries")
    monkeypatch.setattr(config, "LEGACY_LIBRARY_FILE", tmp_path / "library.json")
    monkeypatch.setattr(config, "APP_SETTINGS_FILE", tmp_path / "settings.json")
    return tmp_path


class TestVideoId:
    def test_it_is_stable_for_the_same_input(self):
        assert store.make_video_id("file", "/media/a.mkv") == store.make_video_id(
            "file", "/media/a.mkv"
        )

    def test_different_paths_differ(self):
        assert store.make_video_id("file", "/media/a.mkv") != store.make_video_id(
            "file", "/media/b.mkv"
        )

    def test_bluray_titles_of_one_disc_differ(self):
        assert store.make_video_id("bluray", "/media/DISC", 0) != store.make_video_id(
            "bluray", "/media/DISC", 1
        )

    def test_kind_is_part_of_the_identity(self):
        assert store.make_video_id("file", "/media/x") != store.make_video_id("bluray", "/media/x")


class TestLibraryRoundTrip:
    def test_a_saved_library_reads_back_the_same(self, tmp_path):
        root = str(tmp_path / "Videos")
        data = store.default_library(root)
        data["videos"]["abc"] = {
            "type": "file",
            "path": "/media/a.mkv",
            "display_name": "A",
            "duration": 100.0,
            "chapters": [{"title": "One", "start": 0.0, "end": 100.0, "source": "manual"}],
        }
        store.save_library(data)

        assert store.load_library_for_root(root) == data

    def test_an_unscanned_folder_loads_as_an_empty_library(self, tmp_path):
        data = store.load_library_for_root(str(tmp_path / "never-scanned"))
        assert data["videos"] == {}

    def test_each_folder_gets_its_own_file(self, tmp_path):
        for name in ("one", "two"):
            data = store.default_library(str(tmp_path / name))
            data["videos"][name] = {"display_name": name}
            store.save_library(data)

        assert set(store.load_library_for_root(str(tmp_path / "one"))["videos"]) == {"one"}
        assert set(store.load_library_for_root(str(tmp_path / "two"))["videos"]) == {"two"}

    def test_a_corrupt_file_loads_as_empty_rather_than_raising(self, tmp_path):
        root = str(tmp_path / "Videos")
        store.save_library(store.default_library(root))
        path = store._file_for_root(root)
        path.write_text("{not json", encoding="utf-8")

        assert store.load_library_for_root(root)["videos"] == {}

    def test_missing_settings_keys_are_filled_in_on_load(self, tmp_path):
        import json

        root = str(tmp_path / "Videos")
        config.ensure_data_dir()
        store._file_for_root(root).write_text(
            json.dumps({"videos": {}}), encoding="utf-8"
        )
        data = store.load_library_for_root(root)
        assert "library_root" in data["settings"]
        assert "min_bluray_title_seconds" in data["settings"]


class TestListAndDelete:
    def test_scanned_folders_are_listed_with_their_counts(self, tmp_path):
        data = store.default_library(str(tmp_path / "Videos"))
        data["videos"] = {"a": {}, "b": {}}
        store.save_library(data)

        listed = store.list_libraries()
        assert len(listed) == 1
        assert listed[0]["video_count"] == 2

    def test_deleting_removes_only_that_folders_data(self, tmp_path):
        for name in ("one", "two"):
            store.save_library(store.default_library(str(tmp_path / name)))

        store.delete_library(str(tmp_path / "one"))

        roots = {lib["root"] for lib in store.list_libraries()}
        assert roots == {str((tmp_path / "two").resolve())}

    def test_deleting_an_unknown_folder_is_harmless(self, tmp_path):
        store.delete_library(str(tmp_path / "never-scanned"))


class TestAppSettings:
    def test_defaults_apply_before_anything_is_saved(self):
        assert store.load_app_settings()["musicbrainz_format"] == "xml"

    def test_saved_settings_read_back(self):
        store.save_app_settings({"musicbrainz_format": "json"})
        assert store.load_app_settings()["musicbrainz_format"] == "json"

    def test_unknown_keys_survive_a_round_trip(self):
        store.save_app_settings({"musicbrainz_format": "json", "future_option": 3})
        assert store.load_app_settings()["future_option"] == 3

    def test_a_partial_file_still_gets_the_defaults(self):
        store.save_app_settings({"future_option": 3})
        assert store.load_app_settings()["musicbrainz_format"] == "xml"


class TestMergeChapters:
    """library._merge_chapters decides what survives a rescan."""

    def test_manual_titles_are_carried_forward(self):
        new = [{"title": None, "start": 0, "end": 10}]
        old = [{"title": "Kept", "source": "manual"}]
        assert library._merge_chapters(new, old)[0]["title"] == "Kept"

    def test_musicbrainz_titles_are_carried_forward(self):
        new = [{"title": None, "start": 0, "end": 10}]
        old = [{"title": "Kept", "source": "musicbrainz"}]
        assert library._merge_chapters(new, old)[0]["title"] == "Kept"

    def test_embedded_titles_are_re_read_rather_than_carried(self):
        # The file is the authority for its own embedded names.
        new = [{"title": "Fresh", "start": 0, "end": 10}]
        old = [{"title": "Stale", "source": "embedded"}]
        assert library._merge_chapters(new, old)[0]["title"] == "Fresh"

    def test_a_changed_chapter_count_drops_the_old_titles(self):
        # Index-based matching would put names on the wrong chapters.
        new = [{"title": None, "start": 0, "end": 10}, {"title": None, "start": 10, "end": 20}]
        old = [{"title": "Kept", "source": "manual"}]
        assert library._merge_chapters(new, old)[0]["title"] is None

    def test_untitled_chapters_come_back_auto_numbered(self):
        merged = library._merge_chapters([{"title": None, "start": 0, "end": 10}], None)
        assert merged[0]["source"] == "auto-numbered"
        assert merged[0]["index"] == 0

    def test_an_embedded_title_is_recorded_as_embedded(self):
        merged = library._merge_chapters([{"title": "Has One", "start": 0, "end": 10}], None)
        assert merged[0]["source"] == "embedded"
