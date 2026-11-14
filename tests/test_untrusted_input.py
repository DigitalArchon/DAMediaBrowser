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


# --- compressed and large answers ---------------------------------------------


def gzipped(data, members=1):
    """`data` gzipped, as `members` gzip members one after another."""
    import gzip
    size = -(-len(data) // members)
    return b"".join(gzip.compress(data[i:i + size]) for i in range(0, len(data), size))


def deflated(data, raw=False):
    import zlib
    packer = zlib.compressobj(wbits=-zlib.MAX_WBITS if raw else zlib.MAX_WBITS)
    return packer.compress(data) + packer.flush()


@pytest.fixture
def real_transport(monkeypatch):
    monkeypatch.setattr(netguard, "_transport",
                        lambda request, timeout: netguard._opener().open(request,
                                                                         timeout=timeout))


@pytest.fixture
def server():
    from tests.test_netguard import Server
    made = []

    def make(routes):
        made.append(Server(routes))
        return made[-1]

    yield make
    for each in made:
        each.close()


class TestCompressedAndLargeAnswers:
    def test_a_request_asks_for_gzip_and_deflate_alone(self, server, real_transport):
        backend = server({"/x": (200, {}, b"{}")})
        with netguard.urlopen(backend.url + "/x", 5) as reply:
            reply.read()
        netguard.Session().get(backend.url + "/x")
        ai._request(backend.url + "/x", "sk-1")
        assert [seen["headers"].get("Accept-Encoding") for seen in backend.seen] == \
            ["gzip, deflate"] * 3
        try:
            session = netguard.requests_session()
        except ImportError:
            return                              # requests comes with Tinfoil's client
        assert session.headers["Accept-Encoding"] == "gzip, deflate"

    def test_gzip_and_deflate_are_read_as_what_they_stand_for(self, server, real_transport):
        body = json.dumps({"words": ["Glass Harbor"] * 5000}).encode()
        backend = server({
            "/gzip": (200, {"Content-Encoding": "gzip"}, gzipped(body)),
            "/members": (200, {"Content-Encoding": "gzip"}, gzipped(body, members=3)),
            "/deflate": (200, {"Content-Encoding": "deflate"}, deflated(body)),
            "/raw": (200, {"Content-Encoding": "deflate"}, deflated(body, raw=True)),
            "/plain": (200, {"Content-Encoding": "identity"}, body),
        })
        for path in ("/gzip", "/members", "/deflate", "/raw", "/plain"):
            with netguard.urlopen(backend.url + path, 5) as reply:
                assert netguard.read_body(reply) == body, path
            assert netguard.Session().get(backend.url + path).json()["words"][0] == \
                "Glass Harbor", path
            assert ai._request(backend.url + path, None)["words"][-1] == "Glass Harbor"

    def test_an_answer_is_held_to_its_size_unpacked(self, server, real_transport):
        """A few KB of gzip can unpack to gigabytes: every limit counts the bytes
        unpacked, and no more is unpacked than one past the limit."""
        bomb = gzipped(b"0" * (netguard.MAX_BODY_BYTES + 1024))
        assert len(bomb) < 200_000
        backend = server({"/bomb": (200, {"Content-Encoding": "gzip"}, bomb)})
        with pytest.raises(netguard.RequestError, match="larger than 32 MB"):
            netguard.Session().get(backend.url + "/bomb")
        with pytest.raises(ai.AIError, match="larger than 32 MB"):
            ai._request(backend.url + "/bomb", None)
        with netguard.urlopen(backend.url + "/bomb", 5) as reply:
            assert len(reply.read(1000)) == 1000         # a piece is no more than asked

    def test_a_whole_answer_is_32_mb_at_most(self, server, real_transport):
        backend = server({"/big": (200, {}, b" " * (netguard.MAX_BODY_BYTES + 10))})
        with pytest.raises(ai.AIError, match="larger than 32 MB"):
            ai._request(backend.url + "/big", None)

    def test_compression_not_asked_for_or_damaged_is_refused(self, server, real_transport):
        body = gzipped(b'{"ok": true}' * 1000)
        backend = server({
            "/br": (200, {"Content-Encoding": "br"}, b"\x0b\x02\x80{}\x03"),
            "/twice": (200, {"Content-Encoding": "gzip, gzip"}, body),
            "/cut": (200, {"Content-Encoding": "gzip"}, body[:len(body) // 2]),
            "/garbled": (200, {"Content-Encoding": "deflate"}, b"not deflate at all"),
        })
        for path in ("/br", "/twice"):
            with pytest.raises(netguard.Undecodable, match="wasn't asked for"):
                netguard.urlopen(backend.url + path, 5)
        for path in ("/cut", "/garbled"):
            with pytest.raises(ai.AIError, match="compression was"):
                ai._request(backend.url + path, None)
        with pytest.raises(netguard.RequestError, match="wasn't asked for"):
            netguard.Session().get(backend.url + "/br")

    def test_an_errors_answer_is_read_decoded_and_64_kb_at_most(self, server,
                                                                real_transport):
        said = json.dumps({"error": {"message": "the model is resting"}}).encode()
        long = b"x" * (netguard.MAX_ERROR_BYTES * 4)
        backend = server({
            "/said": (400, {"Content-Encoding": "gzip"}, gzipped(said)),
            "/long": (500, {}, long),
            "/bomb": (500, {"Content-Encoding": "gzip"},
                      gzipped(b"y" * netguard.MAX_BODY_BYTES)),
        })
        with pytest.raises(ai.AIError, match="the model is resting"):
            ai._request(backend.url + "/said", None)
        assert len(netguard.Session().get(backend.url + "/long").content) == \
            netguard.MAX_ERROR_BYTES
        assert len(netguard.Session().get(backend.url + "/bomb").content) == \
            netguard.MAX_ERROR_BYTES

    def test_a_redirect_followed_is_not_read(self, real_transport):
        """urllib read a redirect's whole body, with no end to how much, before it
        went on."""
        import http.server
        import threading
        finished = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's name
                if self.path == "/landed":
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")
                    return
                size = 256 * 1024 * 1024
                self.send_response(302)
                self.send_header("Location", "/landed")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                try:
                    piece = b"z" * (1024 * 1024)
                    for _ in range(size // len(piece)):
                        self.wfile.write(piece)
                    finished.append(True)
                except (BrokenPipeError, ConnectionResetError):
                    finished.append(False)

            def log_message(self, *args):
                pass

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/go"
            with netguard.urlopen(url, 10) as reply:
                assert reply.read() == b"ok"
            for _ in range(200):
                if finished:
                    break
                import time
                time.sleep(0.05)
            assert finished == [False], "the redirect's body was read whole"
        finally:
            httpd.shutdown()
            httpd.server_close()
