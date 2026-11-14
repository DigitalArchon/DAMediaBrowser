# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""What comes from outside - a server's answer, a file someone opens - is read
so that its shape can't crash the app: JSON nested too deep is refused in the
words a caller already has for JSON that doesn't parse."""

import io
import json

import pytest

from mediabrowser.core import ai, catalog, config, musicbrainz, netguard, safejson, setlistfm, store

# Past MAX_NESTING, but shallow enough that json.loads reads it: taken in whole
# before, to recurse too far later, in a copy or a save.
DEEP = "[" * 300 + "]" * 300
DEEP_OBJECT = '{"a": ' * 300 + "1" + "}" * 300
# Too deep for json.loads at all: RecursionError, which nothing caught.
BOTTOMLESS = "[" * 100_000


class Reply(io.BytesIO):
    status = 200

    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def geturl(self):
        return "https://example.test/"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def answers(monkeypatch):
    """Every request answered with `state["body"]`."""
    state = {"body": b"{}"}
    monkeypatch.setattr(netguard, "_transport",
                        lambda request, timeout: Reply(state["body"]))
    return state


class TestJsonNestedTooDeep:
    @pytest.mark.parametrize("text", [DEEP, DEEP_OBJECT, BOTTOMLESS])
    def test_it_is_refused_as_json_that_doesnt_parse(self, text):
        with pytest.raises(json.JSONDecodeError, match="nested more than 200 levels"):
            safejson.loads(text)
        with pytest.raises(json.JSONDecodeError):
            safejson.loads(text.encode())

    def test_what_anything_here_holds_is_read_as_ever(self):
        shallow = "[" * 199 + "]" * 199
        assert safejson.loads(shallow) is not None
        assert safejson.loads('{"ok": [1, {"two": 2}]}') == {"ok": [1, {"two": 2}]}
        assert not safejson.nests_deeper(json.loads(shallow))

    @pytest.mark.parametrize("text", [DEEP, BOTTOMLESS])
    def test_an_attestation_services_answer(self, text):
        with pytest.raises(ValueError, match="nested"):
            netguard.Response(200, {}, text.encode(), "https://example.test/").json()

    @pytest.mark.parametrize("text", [DEEP, BOTTOMLESS])
    def test_the_ais_answer_and_its_reply(self, answers, text):
        answers["body"] = text.encode()
        with pytest.raises(ai.AIError, match="nested"):
            ai._request("https://nano-gpt.com/api/v1/models", None)
        with pytest.raises(ai.AIError):
            ai.parse_json_reply('{"chapters": ' + text + "}")

    @pytest.mark.parametrize("text", [DEEP, BOTTOMLESS])
    def test_setlist_fm_and_musicbrainz(self, answers, text, monkeypatch):
        monkeypatch.setattr(setlistfm, "_MIN_INTERVAL", 0.0)
        monkeypatch.setattr(musicbrainz, "_MIN_INTERVAL", 0.0)
        answers["body"] = text.encode()
        with pytest.raises(setlistfm.SetlistError, match="nested"):
            setlistfm._get("search/setlists", {"artistName": "Glass Harbor"}, key="k")
        with pytest.raises(musicbrainz.MusicBrainzError, match="nested"):
            musicbrainz._get("release", {"query": "Glass Harbor"}, "json")

    @pytest.mark.parametrize("text", [DEEP, BOTTOMLESS])
    def test_a_catalog_someone_opens(self, tmp_path, text):
        path = tmp_path / "catalog.json"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(catalog.CatalogError, match="nested"):
            catalog.read(path)


class TestTheAppsOwnFilesDamaged:
    @pytest.fixture(autouse=True)
    def data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "LIBRARIES_DIR", tmp_path / "libraries")
        monkeypatch.setattr(config, "LEGACY_LIBRARY_FILE", tmp_path / "library.json")
        monkeypatch.setattr(config, "APP_SETTINGS_FILE", tmp_path / "settings.json")
        return tmp_path

    @pytest.mark.parametrize("text", [DEEP_OBJECT, BOTTOMLESS, "[1, 2]", '"text"'])
    def test_settings_edited_into_another_shape_read_as_the_defaults(self, text):
        config.APP_SETTINGS_FILE.write_text(text, encoding="utf-8")
        assert store.load_app_settings() == store.DEFAULT_APP_SETTINGS

    @pytest.mark.parametrize("text", [DEEP, BOTTOMLESS])
    def test_a_library_file_too_deep_reads_as_one_that_couldnt_be_read(self, text,
                                                                      tmp_path):
        path = tmp_path / "library.json"
        path.write_text(text, encoding="utf-8")
        assert store._read_json(path) is None
