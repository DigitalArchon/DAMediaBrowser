# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cover art for the grid, found once and cached.

Four places are tried, cheapest and most trustworthy first:

  1. a cover file sitting beside the video - whoever put it there meant it;
  2. artwork embedded in the file itself, as a music-video rip usually has;
  3. a frame from the middle of the video - never as good as real artwork,
     but far better than a blank square, and it is at least of this video;
  4. the Cover Art Archive, when the chapters were named from a MusicBrainz
     release and so the release id is known.

Every step shells out or waits on the network, so nothing here may be called
from the GUI thread. Results are cached under the data directory, keyed by
video id, and a video that yields nothing is remembered as such so the
expensive steps are not retried on every launch.
"""

import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from . import config

FFMPEG_TIMEOUT_SECONDS = 20
FETCH_TIMEOUT_SECONDS = 15

# Written instead of an image when every source has been tried and failed,
# so the next launch shows the placeholder immediately rather than running
# ffmpeg over the whole library again.
MISS_SUFFIX = ".none"


def cached_path(video_id: str) -> Path:
    return config.ARTWORK_DIR / f"{video_id}.jpg"


def _miss_path(video_id: str) -> Path:
    return config.ARTWORK_DIR / f"{video_id}{MISS_SUFFIX}"


def lookup(video_id: str) -> Path | None:
    """The cached cover for this video, without going and finding one.

    Cheap enough to call while building the grid.
    """
    path = cached_path(video_id)
    return path if path.exists() else None


def is_resolved(video_id: str) -> bool:
    """Whether this video has been looked at before, found or not."""
    return cached_path(video_id).exists() or _miss_path(video_id).exists()


def forget(video_id: str) -> None:
    cached_path(video_id).unlink(missing_ok=True)
    _miss_path(video_id).unlink(missing_ok=True)


def find(video_id: str, video: dict, release_id: str | None = None) -> Path | None:
    """Find and cache a cover for this video. Slow; worker thread only."""
    config.ensure_artwork_dir()
    existing = lookup(video_id)
    if existing is not None:
        return existing

    target = cached_path(video_id)
    source = Path(video["path"])
    is_disc = video.get("type") == "bluray"
    folder = source if is_disc else source.parent

    for attempt in (
        lambda: _from_sibling_file(folder, target),
        lambda: None if is_disc else _from_embedded_art(source, target),
        lambda: _from_cover_art_archive(release_id, target),
        # A frame grab is last: it always produces something, so anything
        # better than it must be tried first.
        lambda: None if is_disc else _from_frame(source, video.get("duration"), target),
    ):
        try:
            if attempt() and target.exists() and target.stat().st_size > 0:
                _miss_path(video_id).unlink(missing_ok=True)
                return target
        except Exception:
            # One source failing is ordinary - a file with no embedded art,
            # a release with no scan - and must not stop the others.
            continue

    target.unlink(missing_ok=True)
    _miss_path(video_id).touch()
    return None


# --- sources -------------------------------------------------------------


def _from_sibling_file(folder: Path, target: Path) -> bool:
    for name in config.COVER_FILENAMES:
        candidate = folder / name
        if candidate.is_file():
            return _rescale(candidate, target)
    return False


def _from_embedded_art(source: Path, target: Path) -> bool:
    """Cover art carried inside the container, as an attached picture."""
    return _run_ffmpeg([
        "-i", str(source),
        "-map", "0:v:disp:attached_pic",
        "-frames:v", "1",
        "-vf", _SCALE,
        str(target),
    ])


def _from_frame(source: Path, duration, target: Path) -> bool:
    """A frame from the middle, which beats a blank square."""
    midpoint = max(1.0, (duration or 60.0) / 2)
    return _run_ffmpeg([
        # Before -i so ffmpeg seeks rather than decoding up to the point.
        "-ss", f"{midpoint:.3f}",
        "-i", str(source),
        "-frames:v", "1",
        "-vf", _SCALE,
        str(target),
    ])


def _from_cover_art_archive(release_id, target: Path) -> bool:
    if not release_id:
        return False
    url = config.COVER_ART_ARCHIVE_URL.format(mbid=release_id)
    request = urllib.request.Request(
        url, headers={"User-Agent": config.MUSICBRAINZ_USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            data = response.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return False
    if not data:
        return False
    target.write_bytes(data)
    # Whatever the archive served, normalise it to the cache's own shape.
    return _rescale(target, target)


# --- ffmpeg --------------------------------------------------------------

_SCALE = (
    f"scale={config.ARTWORK_SIZE}:{config.ARTWORK_SIZE}"
    ":force_original_aspect_ratio=decrease"
)


def _rescale(source: Path, target: Path) -> bool:
    return _run_ffmpeg(["-i", str(source), "-frames:v", "1", "-vf", _SCALE, str(target)])


def _run_ffmpeg(args) -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-nostdin", *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
