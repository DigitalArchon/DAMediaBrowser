# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading release lookups, both wire formats, without the network."""

import xml.etree.ElementTree as ET

from mediabrowser.core import musicbrainz

XML = """<?xml version="1.0" encoding="UTF-8"?>
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <release id="r1">
    <title>Live</title>
    <medium-list count="2">
      <medium>
        <title>Night One</title>
        <format>CD</format>
        <track-list count="2">
          <track><length>606000</length><recording><title>In The Name Of</title></recording></track>
          <track><title>Distortion</title><recording><title>x</title><length>253000</length></recording></track>
        </track-list>
      </medium>
      <medium>
        <format>Blu-ray</format>
        <track-list count="1">
          <track><recording><title>In The Name Of</title></recording></track>
        </track-list>
      </medium>
    </medium-list>
  </release>
</metadata>"""


def test_xml_media_keep_their_discs_apart():
    data = musicbrainz._xml_to_dict(ET.fromstring(XML))
    media = musicbrainz.media_from_release(data)
    assert [(m["format"], m["title"], len(m["tracks"])) for m in media] == [
        ("CD", "Night One", 2), ("Blu-ray", "", 1)
    ]
    assert media[0]["tracks"] == [
        {"title": "In The Name Of", "length": 606.0},
        {"title": "Distortion", "length": 253.0},
    ]
    assert media[1]["tracks"][0]["length"] is None


def test_json_media_read_the_same_way():
    data = {"media": [{
        "title": "Night One", "format": "CD",
        "tracks": [{"title": "Megitsune", "length": 401000, "recording": {}}],
    }]}
    assert musicbrainz.media_from_release(data) == [{
        "title": "Night One", "format": "CD",
        "tracks": [{"title": "Megitsune", "length": 401.0}],
    }]


def test_flatten_keeps_every_track_in_order():
    media = [{"tracks": [{"title": "A"}, {"title": "B"}]}, {"tracks": [{"title": "C"}]}]
    assert [t["title"] for t in musicbrainz.flatten(media)] == ["A", "B", "C"]



class TestBusy:
    def test_a_busy_answer_is_asked_again_once(self, monkeypatch):
        import io
        import urllib.error

        from mediabrowser.core import musicbrainz

        answers = [urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO(b"")),
                   b'{"recordings": [{"id": "x", "title": "Karate", "length": 250000,'
                   b' "artist-credit": [{"name": "BABYMETAL"}]}]}']

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return Response(answer)

        monkeypatch.setattr(musicbrainz.urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(musicbrainz.time, "sleep", lambda s: None)
        monkeypatch.setattr(musicbrainz, "_last_request_time", 0.0)
        assert musicbrainz.search_recordings("karate", fmt="json") == [
            {"id": "x", "title": "Karate", "artist": "BABYMETAL", "length": 250.0}
        ]


class TestTheirTerms:
    """MusicBrainz asks for no more than a request a second, from an
    application that says who it is."""

    def test_it_says_which_version_it_is_and_how_to_reach_us(self):
        from mediabrowser.core import config

        assert config.MUSICBRAINZ_USER_AGENT.startswith(f"DAMediaBrowser/{config.VERSION} (")
        assert config.VERSION[0].isdigit()
        assert config.MUSICBRAINZ_USER_AGENT.endswith(f"( {config.PROJECT_URL} )")

    def test_requests_from_several_threads_still_take_turns(self, monkeypatch):
        import io
        import threading

        sent = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            sent.append(musicbrainz.time.monotonic())
            return Response(b'{"recordings": []}')

        monkeypatch.setattr(musicbrainz.urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(musicbrainz, "_MIN_INTERVAL", 0.2)
        monkeypatch.setattr(musicbrainz, "_last_request_time", 0.0)
        threads = [threading.Thread(target=musicbrainz.search_recordings, args=("x",),
                                    kwargs={"fmt": "json"}) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        gaps = [b - a for a, b in zip(sent, sent[1:], strict=False)]
        assert len(sent) == 3 and min(gaps) >= 0.19, gaps
