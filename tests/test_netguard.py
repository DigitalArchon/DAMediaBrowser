# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTPS, or nothing: which addresses may be used, where a redirect may be
followed, and that a key never goes where it wasn't sent.

The redirects and the proxy run against real servers on this machine, with
netguard's own transport (conftest routes it through urllib.request.urlopen,
which the rest of the suite fakes)."""

import http.server
import json
import threading
import urllib.error
import urllib.request

import pytest

from mediabrowser.core import ai, artwork, musicbrainz, netguard, outbound, privacy, setlistfm


@pytest.fixture
def real_transport(monkeypatch):
    """netguard's own transport, redirects and proxy rules and all."""
    monkeypatch.setattr(netguard, "_transport",
                        lambda request, timeout: netguard._opener().open(request,
                                                                         timeout=timeout))


class Server:
    """An HTTP server on 127.0.0.1 answering each path as `routes` says: a
    (status, headers, body) tuple. Records what it was asked, headers and
    all."""

    def __init__(self, routes, host="127.0.0.1"):
        self.routes = routes
        self.seen = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's name
                outer.seen.append({"path": self.path, "headers": dict(self.headers)})
                status, headers, body = outer.routes.get(self.path, (404, {}, b""))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass        # a client that read no more than it would

            do_POST = do_GET  # noqa: N815

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer((host, 0), Handler)
        self.url = f"http://{host}:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    made = []

    def make(routes, host="127.0.0.1"):
        made.append(Server(routes, host))
        return made[-1]

    yield make
    for each in made:
        each.close()


class TestWhichAddresses:
    @pytest.mark.parametrize("url", [
        "https://nano-gpt.com/api/v1",
        "https://musicbrainz.org/ws/2/release?query=x",
        "http://localhost:11434/v1",
        "http://127.0.0.1:8080",
        "http://10.0.0.5/v1",
        "http://172.16.4.4/v1",
        "http://192.168.1.20:11434",
        "http://169.254.1.1/",
        "http://[::1]:8080/",
        "http://[fe80::1%25eth0]:8080/",
        "http://[fd00::5]/",
        "http://nas.local/v1",
        "http://models.localhost/v1",
        "http://exampleonionaddress.onion/v1",
    ])
    def test_these_may_be_used(self, url):
        assert netguard.problem(url) is None

    @pytest.mark.parametrize("url", [
        "http://nano-gpt.com/api/v1",
        "http://172.32.0.1/",               # just outside 172.16/12
        "http://8.8.8.8/",
        "http://localhost.example.com/",
        "http://127.0.0.1%2eexample.com/",  # an escape a parser would decode
        "http://evil.com\\@localhost/",     # parsers disagree on its host
        "http://local host/",
        "https://nano-gpt.com/\x00",
        "ftp://nano-gpt.com/",
        "file:///etc/passwd",
        "nano-gpt.com/api/v1",
        "http://[fd00::5",
        "https://host:99999/",
        "",
    ])
    def test_these_may_not(self, url):
        assert netguard.problem(url)

    def test_plain_http_is_refused_in_words_that_say_why_and_how(self):
        found = netguard.problem("http://models.example/v1")
        assert "not https" in found and "https://" in found and "local network" in found

    def test_a_password_in_an_address_is_never_repeated(self):
        found = netguard.problem("http://alice:secret@models.example/v1")
        assert "secret" not in found and "alice" not in found
        assert netguard.shown("https://alice:secret@host/v1") == "https://…@host/v1"
        assert "secret" not in netguard.shown("alice:secret@host")
        assert netguard.shown("https://host/v1") == "https://host/v1"

    @pytest.mark.parametrize("url", [
        "https://alice:secret@nano-gpt.com/api/v1",
        "http://alice:secret@192.168.1.5:8080/v1",
        "https://alice@nano-gpt.com/api/v1",
        "https://:secret@nano-gpt.com/api/v1",
        "http://alice:secret@localhost:11434/v1",
    ])
    def test_a_user_name_or_password_in_an_address_is_refused_unquoted(self, url):
        """The app sends neither: urllib can't, and Tinfoil's client would send them as
        Basic auth in place of the key. Said without repeating them, with where the key
        goes instead."""
        found = netguard.problem(url)
        assert found and "user name or password written into it" in found
        assert "secret" not in found and "alice" not in found
        assert "Settings' AI tab" in found and "keyring" in found

    def test_what_a_file_keeps_of_an_address_has_no_password(self):
        assert netguard.without_password("https://alice:secret@host/v1") == \
            "https://alice@host/v1"
        assert netguard.without_password("https://host/v1") == "https://host/v1"
        assert netguard.without_password("https://:secret@host/v1") == "https://host/v1"
        for unreadable in ("http://alice:secret@[fd00::5", "alice:secret@host"):
            assert "secret" not in netguard.without_password(unreadable)

    def test_a_key_that_cant_be_sent_is_said_without_quoting_it(self):
        assert netguard.key_problem("sk-ok_123") is None
        assert netguard.key_problem("") is None
        for pasted in ("sk-1 2", "sk-1\n", "sk-​1", "sk-é"):
            found = netguard.key_problem(pasted, "Nano-GPT")
            assert found and pasted.strip() not in found and "Nano-GPT" in found

    def test_nothing_is_sent_to_an_address_that_may_not_be_used(self, monkeypatch):
        sent = []
        monkeypatch.setattr(netguard, "_transport", lambda r, t: sent.append(r))
        with pytest.raises(netguard.InsecureURL, match="not https"):
            netguard.urlopen("http://models.example/v1", 5)
        request = urllib.request.Request("https://api.setlist.fm/x",
                                         headers={"x-api-key": "a b"})
        with pytest.raises(netguard.InsecureURL, match="API key"):
            netguard.urlopen(request, 5)
        assert sent == []


class TestRedirects:
    def test_the_rules(self):
        problem = netguard.redirect_problem
        assert problem("https://a.example/", "https://b.example/x") is None
        assert "never carried on over plain http" in problem(
            "https://a.example/", "http://192.168.1.5/x")
        assert "not https" in problem("http://localhost/", "http://elsewhere.example/")
        assert "own network" in problem("https://a.example/", "https://192.168.1.5/admin")
        assert problem("http://localhost:1/", "http://localhost:2/") is None
        assert "Not a web address" in problem("https://a.example/", "ftp://a.example/")

    def test_a_redirect_into_the_clear_is_refused_before_anything_goes_there(
            self, server, real_transport):
        target = server({"/landed": (200, {}, b"reached")}, host="127.0.0.2")
        start = server({"/go": (302, {"Location": "http://downloads.example/x"}, b"")})
        with pytest.raises(netguard.RedirectRefused, match="downloads.example.*not https"):
            netguard.urlopen(start.url + "/go", 5)
        assert target.seen == []

    def test_what_carries_a_key_a_body_or_what_was_sent_follows_no_redirect(
            self, server, real_transport):
        """A redirect would send the request on to wherever the server pointed, the key
        and what was sent with it - urllib turns a 301, 302 or 303 on a POST into a GET
        to the new address. So none is followed: an error says where the server pointed,
        without a password, and nothing goes there (SealedLore's e947938)."""
        other = server({"/landed": (200, {}, b"reached")}, host="127.0.0.2")
        landed = other.url + "/landed"
        start = server({
            "/key": (302, {"Location": landed}, b""),
            "/same-server": (301, {"Location": "/landed"}, b""),
            "/post": (303, {"Location": landed}, b""),
            "/post-308": (308, {"Location": landed}, b""),
            "/said": (307, {"Location": landed}, b""),
            "/password": (302, {"Location": other.url.replace("//", "//alice:secret@")
                                + "/landed"}, b""),
            "/public": (302, {"Location": landed}, b""),
            "/landed": (200, {}, b"the same server"),
        })
        request = urllib.request.Request
        for sent, follow, code, target in [
                (request(start.url + "/key", headers={"Authorization": "Bearer sk-secret"}),
                 True, 302, landed),
                (request(start.url + "/same-server", headers={"X-api-key": "setlist"}),
                 True, 301, start.url + "/landed"),
                (request(start.url + "/post", data=b'{"prompt": "Glass Harbor"}'),
                 True, 303, landed),
                (request(start.url + "/post-308", data=b"{}"), True, 308, landed),
                (request(start.url + "/said"), False, 307, landed)]:
            with pytest.raises(netguard.Redirected) as caught:
                netguard.urlopen(sent, 5, follow_redirects=follow)
            assert caught.value.reason == (f"{sent.full_url} redirected (HTTP {code}) to "
                                           f"{target}. Nothing was sent there.")
            assert not isinstance(caught.value, netguard.REFUSALS), "it was sent, there"
        with pytest.raises(netguard.Redirected) as caught:
            netguard.urlopen(request(start.url + "/password",
                                     headers={"Authorization": "Bearer sk-1"}), 5)
        assert "secret" not in caught.value.reason and "to http://…@127.0.0.2:" in \
            caught.value.reason
        assert other.seen == [], "nothing went where it pointed"
        assert "/landed" not in [seen["path"] for seen in start.seen]
        # A fetch with none of them - a cover, say - still follows one where netguard's
        # rule allows it.
        with netguard.urlopen(start.url + "/public", 5) as reply:
            assert reply.read() == b"reached"

    def test_a_server_on_this_network_is_reached_directly_whatever_the_proxy(
            self, server, real_transport, monkeypatch):
        """A proxy elsewhere would carry plain http to it across the network."""
        local = server({"/v1/models": (200, {}, b"{}")})
        for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
            monkeypatch.setenv(name, "http://proxy.invalid:3128")
        for name in ("no_proxy", "NO_PROXY"):
            monkeypatch.delenv(name, raising=False)
        with netguard.urlopen(local.url + "/v1/models", 5) as reply:
            assert reply.read() == b"{}"
        assert netguard.env_proxy_for(local.url) is None
        monkeypatch.setenv("https_proxy", "http://proxy.invalid:3128")
        assert netguard.env_proxy_for("https://nano-gpt.com/x") == "http://proxy.invalid:3128"


class TestSession:
    def test_it_answers_as_requests_would(self, server, real_transport):
        backend = server({
            "/json?a=1&b=two": (200, {"X-Thing": "yes"}, b'{"ok": true}'),
            "/missing": (404, {}, b'{"error": "none"}'),
        })
        with netguard.Session() as session:
            found = session.get(backend.url + "/json", params={"a": 1, "b": "two"})
            assert found.status_code == 200 and found.json() == {"ok": True}
            assert found.headers.get("X-Thing") == "yes"
            missing = session.get(backend.url + "/missing")
            assert missing.status_code == 404 and missing.json() == {"error": "none"}
            posted = session.post(backend.url + "/json?a=1&b=two", json={"x": 1})
            assert posted.status_code == 200
            with pytest.raises(ValueError):
                netguard.Response(200, {}, b"<html>", "u").json()

    def test_what_it_may_not_do_is_a_request_error(self, server, real_transport):
        with pytest.raises(netguard.RequestError, match="not https"):
            netguard.Session().get("http://intel.example/")
        start = server({"/go": (302, {"Location": "http://pcs.example/"}, b"")})
        with pytest.raises(netguard.RequestError, match="not followed"):
            netguard.Session().get(start.url + "/go")
        with pytest.raises(netguard.RequestError):
            netguard.Session().get("http://127.0.0.1:1/")   # nothing listens there


    def test_a_request_with_the_key_or_a_body_follows_no_redirect(self, server,
                                                                  real_transport):
        other = server({"/landed": (200, {}, b"{}")}, host="127.0.0.2")
        start = server({"/attest": (307, {"Location": other.url + "/landed"}, b""),
                        "/nras": (302, {"Location": other.url + "/landed"}, b"")})
        with pytest.raises(netguard.RequestError) as caught:
            netguard.Session().get(start.url + "/attest",
                                   headers={"Authorization": "Bearer sk-1"})
        assert str(caught.value) == (f"{start.url}/attest redirected (HTTP 307) to "
                                     f"{other.url}/landed. Nothing was sent there.")
        with pytest.raises(netguard.RequestError, match=r"redirected \(HTTP 302\)"):
            netguard.Session().post(start.url + "/nras", json={"evidence": "a GPU's"})
        assert other.seen == []


class Everywhere(dict):
    """Routes for a Server that answer every path as `answer(path)` says."""

    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def get(self, path, default=None):
        return self.answer(path)


class TestNothingThatCarriesWhatWasSentFollowsARedirect:
    """The AI's requests, Privacy Mode's TEE checks, a MusicBrainz search and setlist.fm:
    a redirect is an error saying where the server pointed, and nothing is sent there.
    Where the address is the user's to set - the AI endpoint - it says where a new one
    goes (SealedLore's e947938)."""

    MOVED = ("Nothing was sent there: if the endpoint has moved, put its new address in "
             "Settings → AI.")

    @pytest.fixture
    def moved(self, server):
        """(the server that redirects every request, the one it points to)."""
        def make(status):
            other = server({}, host="127.0.0.2")
            start = server(Everywhere(lambda path: (status, {"Location": other.url + path},
                                                    b"")))
            return start, other
        return make

    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    def test_the_ais_request_and_its_model_list(self, moved, real_transport, status):
        start, other = moved(status)
        settings = ai.settings_from({privacy.AI: True, ai.SETTING_KEY: "sk-1",
                                     ai.SETTING_BASE_URL: start.url + "/v1"})
        with pytest.raises(ai.AIError) as caught:
            ai.chat([{"role": "user", "content": "Glass Harbor"}], settings)
        assert str(caught.value) == (
            f"{start.url}/v1/chat/completions redirected (HTTP {status}) to "
            f"{other.url}/v1/chat/completions. {self.MOVED}")
        with pytest.raises(ai.AIError, match=f"redirected \\(HTTP {status}\\)"):
            ai.list_models(settings)
        assert other.seen == []

    @pytest.mark.parametrize("status", [301, 308])
    def test_a_tee_models_streamed_request(self, moved, real_transport, status):
        start, other = moved(status)
        with pytest.raises(ai.AIError) as caught:
            ai._tee_stream(start.url + "/v1/chat/completions", "sk-1",
                           {"model": "TEE/glm-5.3", "messages": []})
        assert str(caught.value) == (
            f"{start.url}/v1/chat/completions redirected (HTTP {status}) to "
            f"{other.url}/v1/chat/completions. {self.MOVED}")
        assert other.seen == []

    def test_privacy_modes_tee_checks_which_carry_the_key(self, moved, real_transport):
        from mediabrowser.core import private_ai
        start, other = moved(308)
        client = ai._tee_client(start.url + "/v1", "sk-1", "TEE/glm-5.3")
        with pytest.raises(private_ai.PrivacyError) as caught:
            client._get("/tee/attestation", {"model": "TEE/glm-5.3"})
        assert f"redirected (HTTP 308) to {other.url}/" in str(caught.value)
        assert self.MOVED in str(caught.value)
        assert other.seen == [] and all(
            seen["headers"].get("Authorization") == "Bearer sk-1" for seen in start.seen)

    def test_a_musicbrainz_search_and_setlist_fm(self, moved, real_transport, monkeypatch):
        start, other = moved(301)
        monkeypatch.setattr(musicbrainz, "_MIN_INTERVAL", 0.0)
        monkeypatch.setattr(setlistfm, "_MIN_INTERVAL", 0.0)
        monkeypatch.setattr(musicbrainz.config, "MUSICBRAINZ_BASE_URL", start.url + "/ws/2")
        monkeypatch.setattr(setlistfm, "BASE_URL", start.url + "/rest/1.0")
        monkeypatch.setenv(setlistfm.API_KEY_ENV, "setlist-key")
        with pytest.raises(musicbrainz.MusicBrainzError,
                           match=rf"redirected \(HTTP 301\) to {other.url}/ws/2/release"):
            musicbrainz.search_releases("Glass Harbor", fmt="json")
        with pytest.raises(setlistfm.SetlistError,
                           match=rf"redirected \(HTTP 301\) to {other.url}/rest/1\.0/"):
            setlistfm.search("Glass Harbor", year=2026)
        assert other.seen == []
        assert [entry.outcome for entry in outbound.entries()] == [outbound.FAILED] * 2
        assert all("Nothing was sent there." in entry.detail
                   for entry in outbound.entries())


class TestTheProviderLookup:
    """RedPill's session records (Settings → Privacy, "Name the provider behind each TEE
    reply"): fetched by netguard's Session as every attestation fetch is - https only,
    no redirect netguard would not follow, the environment's proxy or nothing - read a
    piece at a time, and with no key (private_ai._session_record)."""

    RECORD = {"api_version": "aci/1", "upstream_name": "phala-244",
              "verifier_id": "private-ai-verifier/phala/v1",
              "claims": {"extra": {"evidence_scope": "model_instance"}},
              "established_at": 1, "expires_at": 2}

    @pytest.fixture(autouse=True)
    def fresh(self, monkeypatch):
        from mediabrowser.core import private_ai
        monkeypatch.setattr(private_ai, "_records", {})
        for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
            monkeypatch.delenv(name.upper(), raising=False)

    def session_id(self):
        import hashlib

        from mediabrowser.core import private_ai
        return hashlib.sha256(private_ai._canonical(self.RECORD)).hexdigest()

    def test_it_is_read_through_netguard_with_no_key(self, server, real_transport,
                                                     monkeypatch):
        from mediabrowser.core import private_ai
        records = server({f"/v1/aci/sessions/{self.session_id()}":
                          (200, {"Content-Type": "application/json"},
                           json.dumps(self.RECORD).encode())})
        monkeypatch.setattr(private_ai, "SESSION_RECORDS", records.url + "/v1/aci/sessions/")
        found = private_ai._session_record(netguard.Session(), self.session_id())
        assert (found.name, found.scope, found.expires) == ("phala-244", "model_instance", 2)
        assert "Authorization" not in records.seen[0]["headers"]
        assert private_ai.SESSION_RECORDS.startswith(records.url)

    def test_a_redirect_netguard_would_not_follow_names_nobody(self, server, real_transport,
                                                               monkeypatch):
        from mediabrowser.core import private_ai
        moved = server({f"/v1/aci/sessions/{self.session_id()}":
                        (302, {"Location": "http://api.redpill.ai/v1/aci/sessions/x"}, b"")})
        monkeypatch.setattr(private_ai, "SESSION_RECORDS", moved.url + "/v1/aci/sessions/")
        with pytest.raises(private_ai.PrivacyError, match=r"api\.redpill\.ai could not be "
                                                          r"reached \(RequestError\)"):
            private_ai._session_record(netguard.Session(), self.session_id())
        assert len(moved.seen) == 1, "nothing was sent where it pointed"
        assert private_ai._records == {}

    def test_a_proxy_it_cannot_go_through_sends_nothing(self, monkeypatch, real_transport):
        from mediabrowser.core import private_ai
        sent = []
        monkeypatch.setattr(netguard, "_opener", lambda: sent.append(1))
        monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:9050")
        assert private_ai.SESSION_RECORDS.startswith("https://api.redpill.ai/")
        with pytest.raises(private_ai.PrivacyError, match="could not be reached"):
            private_ai._session_record(netguard.Session(), self.session_id())
        assert sent == []

    def test_a_record_larger_than_is_read_is_left_unread(self, server, real_transport,
                                                         monkeypatch):
        from mediabrowser.core import private_ai
        big = server({f"/v1/aci/sessions/{self.session_id()}":
                      (200, {}, b" " * (private_ai.SESSION_RECORD_MAX_BYTES + 1))})
        monkeypatch.setattr(private_ai, "SESSION_RECORDS", big.url + "/v1/aci/sessions/")
        with pytest.raises(private_ai.PrivacyError, match="KB, more than is read"):
            private_ai._session_record(netguard.Session(), self.session_id())


class TestEveryClientGoesThroughIt:
    """MusicBrainz, cover art, setlist.fm and the AI: each one's request is
    held to netguard's rules, and each goes on the list of what was sent."""

    @pytest.fixture
    def answered(self, monkeypatch):
        sent = []

        class Reply:
            def __init__(self, body):
                self.body = body

            def read(self, size=-1):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

        def transport(request, timeout):
            sent.append(request)
            url = request.full_url
            if "musicbrainz" in url:
                return Reply(json.dumps({"releases": []}).encode())
            if "setlist.fm" in url:
                return Reply(json.dumps({"setlist": []}).encode())
            if "coverartarchive" in url:
                return Reply(b"\xff\xd8not really a jpeg")
            return Reply(json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode())

        monkeypatch.setattr(netguard, "_transport", transport)
        monkeypatch.setattr(musicbrainz, "_MIN_INTERVAL", 0.0)
        monkeypatch.setattr(setlistfm, "_MIN_INTERVAL", 0.0)
        return sent

    def test_each_is_listed_with_what_it_carried(self, answered, tmp_path, monkeypatch):
        monkeypatch.setenv(setlistfm.API_KEY_ENV, "setlist-key")
        musicbrainz.search_releases("Glass Harbor Copperfield Hall", fmt="json")
        setlistfm.search("Glass Harbor", year=2026)
        artwork._from_cover_art_archive("rel-1", tmp_path / "cover.jpg")
        ai.ping(ai.settings_from({ai.SETTING_KEY: "sk-1"}))
        assert len(answered) == 4
        services = [e.service for e in outbound.entries()]
        assert services == ["MusicBrainz", "setlist.fm", "Cover Art Archive", "Nano-GPT"]
        mb, sl, cover, model = outbound.entries()
        assert "Glass Harbor Copperfield Hall" in mb.what and mb.host == "musicbrainz.org"
        assert "Glass Harbor" in sl.what and "setlist-key" not in sl.what
        assert "rel-1" in cover.what
        assert model.protection == outbound.CLEAR and "both can read it" in model.how
        assert "Reply with the single word OK." in model.content
        assert all(e.outcome == outbound.SENT for e in outbound.entries())

    def test_a_refused_one_is_listed_as_refused(self, answered, monkeypatch):
        monkeypatch.setattr(musicbrainz.config, "MUSICBRAINZ_BASE_URL",
                            "http://musicbrainz.example/ws/2")
        with pytest.raises(musicbrainz.MusicBrainzError, match="not https"):
            musicbrainz.search_releases("Glass Harbor", fmt="json")
        assert answered == []
        (entry,) = outbound.entries()
        assert entry.outcome == outbound.REFUSED and "not https" in entry.detail

    def test_nothing_is_listed_while_a_service_isnt_allowed(self, answered, shipped_privacy):
        with pytest.raises(musicbrainz.MusicBrainzError):
            musicbrainz.search_releases("Glass Harbor", fmt="json")
        assert outbound.entries() == [] and answered == []
        assert not privacy.allowed(privacy.MUSICBRAINZ)


class TestTheEnvironmentsProxy:
    """Every request goes the way httpx and requests would send it, or not at
    all: ALL_PROXY counts, a proxy urllib can't use refuses rather than goes
    around, and plain http to an onion goes only through a proxy here."""

    @pytest.fixture(autouse=True)
    def clean(self, monkeypatch):
        for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
            monkeypatch.delenv(name.upper(), raising=False)

    def test_all_proxy_counts_as_httpx_and_requests_count_it(self, monkeypatch):
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:8118")
        assert netguard.env_proxies()["https"] == "http://127.0.0.1:8118"
        assert netguard.env_proxy_for("https://musicbrainz.org/") == "http://127.0.0.1:8118"
        assert netguard.env_proxy_for("http://192.168.1.20/") is None, "local goes direct"

    def test_a_proxy_urllib_cant_use_sends_nothing_rather_than_go_around(
            self, monkeypatch, real_transport):
        sent = []
        monkeypatch.setattr(netguard, "_opener", lambda: sent.append(1))
        monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:9050")
        with pytest.raises(netguard.ProxyRefused, match="socks5h.*nothing was sent"):
            netguard.urlopen("https://musicbrainz.org/ws/2/release?query=x", 5)
        with pytest.raises(netguard.RequestError, match="nothing was sent"):
            netguard.Session().get("https://api.trustedservices.intel.com/x")
        assert sent == []
        assert isinstance(netguard.ProxyRefused("x"), netguard.REFUSALS)

    def test_an_onion_over_plain_http_goes_only_through_a_proxy_here(
            self, monkeypatch, server, real_transport):
        monkeypatch.setenv("http_proxy", "http://proxy.example:3128")
        with pytest.raises(netguard.ProxyRefused, match="unencrypted before reaching Tor"):
            netguard.urlopen("http://glassharbor.onion/v1/models", 5)
        tor = server({"http://glassharbor.onion/v1/models": (200, {}, b"through tor")})
        monkeypatch.delenv("http_proxy")
        monkeypatch.setenv("ALL_PROXY", tor.url)
        with netguard.urlopen("http://glassharbor.onion/v1/models", 5) as reply:
            assert reply.read() == b"through tor"
        assert tor.seen[0]["path"] == "http://glassharbor.onion/v1/models"
