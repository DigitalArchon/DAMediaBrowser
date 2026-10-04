# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from . import config, privacy

_last_request_time = 0.0
# Held from one request's turn to the next's: an Identify run and a search in
# Detect Chapters, say, on their own threads, still take turns.
_turn = threading.Lock()
# MusicBrainz's policy is 1 request/second, but throttling at exactly that
# rate sits right on their tolerance and still gets rate-limited often in
# practice - 2 seconds is noticeably more reliable.
_MIN_INTERVAL = 2.0

# How long to wait before asking again when MusicBrainz says it's busy.
BUSY_RETRY_SECONDS = 3.0

_MB_NS = "{http://musicbrainz.org/ns/mmd-2.0#}"


class MusicBrainzError(Exception):
    pass


def _get(path: str, params: dict, fmt: str = "xml") -> dict:
    """Fetches from MusicBrainz and returns a plain dict shaped like their
    JSON responses, regardless of which wire format was actually used.

    fmt picks which representation to request ("json" or "xml") - offered
    as a user-facing toggle because in practice one or the other has been
    observed to fail far more often depending on network path (VPNs in
    particular seem to have worse luck with the JSON endpoint).

    Nothing is sent unless Settings → Privacy allows MusicBrainz.
    """
    if not privacy.allowed(privacy.MUSICBRAINZ):
        raise MusicBrainzError(privacy.OFF[privacy.MUSICBRAINZ])
    with _turn:
        return _get_in_turn(path, params, fmt)


def _get_in_turn(path: str, params: dict, fmt: str) -> dict:
    global _last_request_time

    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)

    query = urllib.parse.urlencode({**params, "fmt": fmt})
    url = f"{config.MUSICBRAINZ_BASE_URL}/{path}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": config.MUSICBRAINZ_USER_AGENT})

    try:
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            # 503 is MusicBrainz saying it's busy, and a moment later it
            # usually isn't: once more, after a pause, before giving up.
            if exc.code != 503:
                raise
            time.sleep(BUSY_RETRY_SECONDS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
    except (urllib.error.URLError, OSError) as exc:
        raise MusicBrainzError(str(exc) or repr(exc)) from exc
    finally:
        _last_request_time = time.monotonic()

    try:
        if fmt == "xml":
            return _xml_to_dict(ET.fromstring(body))
        return json.loads(body.decode("utf-8"))
    except (ET.ParseError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MusicBrainzError(str(exc) or repr(exc)) from exc


def _text(el, tag):
    child = el.find(f"{_MB_NS}{tag}")
    return child.text if child is not None else None


def _xml_to_dict(root) -> dict:
    """Normalizes a MusicBrainz XML <metadata> response into the same
    shape search_releases()/get_release_tracks() already expect from the
    JSON API - so neither of those needs to know which format was used.
    """
    release_list = root.find(f"{_MB_NS}release-list")
    if release_list is not None:
        releases = [_xml_release_summary(el) for el in release_list.findall(f"{_MB_NS}release")]
        return {"releases": releases}

    release_el = root.find(f"{_MB_NS}release")
    if release_el is not None:
        return {"media": _xml_media_tracks(release_el)}

    recording_list = root.find(f"{_MB_NS}recording-list")
    if recording_list is not None:
        return {"recordings": [
            _xml_recording(el) for el in recording_list.findall(f"{_MB_NS}recording")
        ]}

    return {}


def _xml_recording(recording_el):
    names = []
    credit_el = recording_el.find(f"{_MB_NS}artist-credit")
    if credit_el is not None:
        for name_credit in credit_el.findall(f"{_MB_NS}name-credit"):
            artist_el = name_credit.find(f"{_MB_NS}artist")
            name = _text(name_credit, "name") or (
                _text(artist_el, "name") if artist_el is not None else None
            )
            names.append({"name": name or ""})
    length = _text(recording_el, "length")
    return {
        "id": recording_el.get("id"),
        "title": _text(recording_el, "title") or "",
        "length": int(length) if length else None,
        "artist-credit": names,
    }


def _xml_release_summary(release_el):
    artist_credit = []
    credit_el = release_el.find(f"{_MB_NS}artist-credit")
    if credit_el is not None:
        for name_credit in credit_el.findall(f"{_MB_NS}name-credit"):
            artist_el = name_credit.find(f"{_MB_NS}artist")
            name = _text(artist_el, "name") if artist_el is not None else None
            artist_credit.append({"artist": {"name": name or ""}})

    media = []
    medium_list = release_el.find(f"{_MB_NS}medium-list")
    if medium_list is not None:
        for medium_el in medium_list.findall(f"{_MB_NS}medium"):
            track_list = medium_el.find(f"{_MB_NS}track-list")
            count = int(track_list.get("count", 0)) if track_list is not None else 0
            media.append({"track-count": count})

    return {
        "id": release_el.get("id"),
        "title": _text(release_el, "title") or "(untitled)",
        "date": _text(release_el, "date") or "",
        "artist-credit": artist_credit,
        "media": media,
    }


def _xml_media_tracks(release_el):
    media = []
    medium_list = release_el.find(f"{_MB_NS}medium-list")
    if medium_list is None:
        return media

    for medium_el in medium_list.findall(f"{_MB_NS}medium"):
        tracks = []
        track_list = medium_el.find(f"{_MB_NS}track-list")
        if track_list is not None:
            for track_el in track_list.findall(f"{_MB_NS}track"):
                recording_el = track_el.find(f"{_MB_NS}recording")
                recording = {}
                if recording_el is not None:
                    length_text = _text(recording_el, "length")
                    recording = {
                        "title": _text(recording_el, "title"),
                        "length": int(length_text) if length_text else None,
                    }
                length_text = _text(track_el, "length")
                tracks.append({
                    "title": _text(track_el, "title"),
                    "length": int(length_text) if length_text else None,
                    "recording": recording,
                })
        media.append({
            "title": _text(medium_el, "title") or "",
            "format": _text(medium_el, "format") or "",
            "tracks": tracks,
        })
    return media


def search_releases(query: str, limit: int = 10, fmt: str = "xml"):
    """Search MusicBrainz releases matching a free-text query.

    Returns a list of {"id", "title", "artist", "date", "track_count"}.
    """
    data = _get("release", {"query": query, "limit": limit}, fmt=fmt)
    results = []
    for release in data.get("releases", []):
        artist = " & ".join(
            c.get("artist", {}).get("name", "") for c in release.get("artist-credit", [])
        ) or release.get("artist-credit-phrase", "")

        track_count = None
        media = release.get("media") or []
        if media:
            track_count = sum(m.get("track-count", 0) for m in media)

        results.append({
            "id": release.get("id"),
            "title": release.get("title", "(untitled)"),
            "artist": artist,
            "date": release.get("date", ""),
            "track_count": track_count,
        })
    return results


def search_recordings(query: str, limit: int = 10, fmt: str = "xml"):
    """Search MusicBrainz recordings - songs, as recorded - matching a
    free-text query. Returns [{"id", "title", "artist", "length"}], length
    in seconds or None."""
    data = _get("recording", {"query": query, "limit": limit}, fmt=fmt)
    results = []
    for recording in data.get("recordings", []):
        artist = " & ".join(
            credit.get("name") or (credit.get("artist") or {}).get("name", "")
            for credit in recording.get("artist-credit", [])
        )
        length = recording.get("length")
        results.append({
            "id": recording.get("id"),
            "title": recording.get("title") or "",
            "artist": artist,
            "length": length / 1000.0 if length else None,
        })
    return results


def media_from_release(data) -> list[dict]:
    """A release lookup's media, each {"title", "format", "tracks"} with
    tracks as {"title", "length"} - length in seconds (float), or None if
    MusicBrainz doesn't have it.
    """
    media = []
    for medium in data.get("media", []):
        tracks = []
        for track in medium.get("tracks", []):
            recording = track.get("recording") or {}
            title = track.get("title") or recording.get("title") or "(untitled track)"
            length_ms = track.get("length") or recording.get("length")
            length = length_ms / 1000.0 if length_ms is not None else None
            tracks.append({"title": title, "length": length})
        media.append({
            "title": medium.get("title") or "",
            "format": medium.get("format") or "",
            "tracks": tracks,
        })
    return media


def get_release_media(release_id: str, fmt: str = "xml"):
    """Fetch a release's media (discs), each with its tracks in order.

    Kept apart rather than flattened because a box set lists every show's
    CDs and videos as separate media, and building chapters from track
    lengths needs to use only the ones that make up this video.
    """
    data = _get(f"release/{release_id}", {"inc": "recordings"}, fmt=fmt)
    return media_from_release(data)


def flatten(media) -> list[dict]:
    """Every track of every medium, in order."""
    return [track for medium in media for track in medium["tracks"]]


def get_release_tracks(release_id: str, fmt: str = "xml"):
    """Fetch the flat, in-order list of tracks for a release.

    Returns a list of {"title", "length"} where length is the track
    duration in seconds (float), or None if MusicBrainz doesn't have it.
    """
    return flatten(get_release_media(release_id, fmt=fmt))
