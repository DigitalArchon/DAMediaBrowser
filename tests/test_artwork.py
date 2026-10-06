# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Covers for Blu-ray discs: the disc's own artwork when it has some (the
thumbnails its maker put in BDMV/META/DL), and a frame from the title when
it hasn't - so no disc is left a blank square."""

import shutil
import subprocess

import pytest

from mediabrowser.core import artwork, config, frames

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "ARTWORK_DIR", tmp_path / "data" / "artwork")


def image(path, colour: str, size: str) -> bytes:
    data = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
         f"color=c={colour}:s={size}:d=1", "-frames:v", "1", "-f", "image2pipe",
         "-c:v", "mjpeg", "-"], check=True, capture_output=True).stdout
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return data


def centre_colour(path) -> tuple:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vf", "scale=1:1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    return tuple(out[:3])


def disc(tmp_path, name="Omega Alive"):
    folder = tmp_path / name
    (folder / "BDMV" / "STREAM").mkdir(parents=True)
    return folder, {"type": "bluray", "path": str(folder), "title_idx": 0,
                    "playlist": 800, "duration": 6000.0}


def test_a_disc_shows_its_own_artwork_the_largest_there_is(tmp_path, monkeypatch):
    folder, video = disc(tmp_path)
    image(folder / "BDMV" / "META" / "DL" / "small.jpg", "blue", "240x135")
    image(folder / "BDMV" / "META" / "DL" / "large.jpg", "red", "640x360")
    monkeypatch.setattr(frames, "grab", lambda *a, **k: pytest.fail("grabbed a frame"))

    found = artwork.find("disc", video)

    assert found is not None
    red, green, blue = centre_colour(found)
    assert red > 200 and green < 60 and blue < 60, "the larger artwork"


def test_a_cover_beside_the_disc_still_comes_first(tmp_path):
    folder, video = disc(tmp_path)
    image(folder / "BDMV" / "META" / "DL" / "large.jpg", "red", "640x360")
    image(folder / "cover.jpg", "lime", "300x300")

    red, green, _ = centre_colour(artwork.find("disc", video))

    assert green > 200 and red < 60


def test_a_disc_without_artwork_gets_a_frame_of_its_title(tmp_path, monkeypatch):
    _folder, video = disc(tmp_path)
    asked = []

    def grab(v, seconds, *a, **k):
        asked.append(seconds)
        return image(None, "white", "512x288")

    monkeypatch.setattr(frames, "grab", grab)
    found = artwork.find("disc", video)

    assert found is not None and asked == [3000.0], "from the middle of the title"
    assert min(centre_colour(found)) > 200


def test_a_disc_with_nothing_to_show_is_remembered_as_such(tmp_path, monkeypatch):
    _folder, video = disc(tmp_path)
    monkeypatch.setattr(frames, "grab", lambda *a, **k: None)

    assert artwork.find("disc", video) is None
    assert artwork.is_resolved("disc") and artwork.lookup("disc") is None


def test_a_downloaded_cover_is_scaled_into_the_cache(tmp_path, monkeypatch):
    """The Cover Art Archive's image went to the cache file and was scaled
    onto itself, which ffmpeg refuses: no sleeve was ever kept."""
    from mediabrowser.core import privacy

    monkeypatch.setattr(privacy, "allowed", lambda choice, settings=None: True)

    class Response:
        def __init__(self, data):
            self.data = data

        def read(self):
            return self.data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    sleeve = image(None, "yellow", "500x500")
    monkeypatch.setattr(artwork.urllib.request, "urlopen", lambda *a, **k: Response(sleeve))
    config.ensure_artwork_dir()
    target = artwork.cached_path("sleeve")

    assert artwork._from_cover_art_archive("release-1", target)
    red, green, blue = centre_colour(target)
    assert red > 200 and green > 200 and blue < 80
    assert [p.name for p in config.ARTWORK_DIR.iterdir()] == ["sleeve.jpg"], "nothing left over"


def test_an_id_that_cant_name_a_cache_file_is_left_alone(tmp_path, monkeypatch):
    """A library file or catalog edited by hand could carry any id; one
    that would reach outside the cache does nothing - and doesn't stop
    the covers of every video after it (config.own would have raised)."""
    monkeypatch.setattr(frames, "grab", lambda *a, **k: pytest.fail("grabbed a frame"))
    video = {"type": "file", "path": str(tmp_path / "x.mkv"), "duration": 10.0}
    for bad in ("../../etc/passwd", "a/b", "", "x" * 65, None):
        assert artwork.find(bad, video) is None
        assert artwork.is_resolved(bad), "nothing to do for it"
        assert artwork.lookup(bad) is None
        artwork.forget(bad)
    assert not (tmp_path / "etc").exists() and not config.ARTWORK_DIR.exists()
