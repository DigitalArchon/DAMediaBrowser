# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""How brightly lit the stage is at chosen moments of a concert video.

A second opinion for estimating chapters. The audio finds the quiet
stretches; the picture helps say which of them are between songs. Only a
few hundred single frames are looked at - the moments in and around each
quiet stretch - because decoding a whole concert's video roughly doubles
the time the audio already takes, where seeking to one keyframe costs a
fraction of a second, several at once.

Brightness is the frame's average luma, 0 (black) to 255 (white), measured
on a 64-pixel-wide thumbnail by ffmpeg's signalstats filter.
"""

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

from .audio_levels import Cancelled

# Seeks run this many at a time: each is mostly waiting on the disk or the
# network, not the CPU.
WORKERS = 6
TIMEOUT_SECONDS = 20

_KEY = "lavfi.signalstats.YAVG"


def brightness_at(path, seconds: float) -> float | None:
    """The brightness of the keyframe at or just after `seconds`, or None
    if there is no picture there (an audio-only file, or past the end)."""
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-nostdin", "-skip_frame", "nokey",
                "-ss", f"{max(0.0, seconds):.2f}", "-i", str(path),
                "-frames:v", "1", "-an",
                "-vf", f"scale=64:-2,signalstats,metadata=mode=print:key={_KEY}:file=-",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in proc.stdout.splitlines():
        if line.startswith(_KEY + "="):
            try:
                return float(line.split("=", 1)[1])
            except ValueError:
                return None
    return None


def sample(path, times, progress_cb=None, cancel: threading.Event | None = None):
    """{time: brightness} for each of `times` that has a picture.

    progress_cb, if given, is called with a whole percentage. Setting
    `cancel` stops it with audio_levels.Cancelled.
    """
    times = list(times)
    result: dict[float, float] = {}
    if not times:
        return result
    done = 0
    last_percent = -1
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(brightness_at, path, t): t for t in times}
        for future in futures:
            if cancel is not None and cancel.is_set():
                for pending in futures:
                    pending.cancel()
                raise Cancelled("stage lighting check cancelled")
            value = future.result()
            if value is not None:
                result[futures[future]] = value
            done += 1
            percent = int(100 * done / len(times))
            if progress_cb and percent != last_percent:
                last_percent = percent
                progress_cb(percent)
    return result


_cache: dict[tuple, dict[float, float]] = {}
_cache_lock = threading.Lock()


def _key(path, fingerprint, times):
    return (str(path), tuple(fingerprint) if fingerprint else None, tuple(times))


def peek_cached(path, fingerprint, times):
    """What cached_sample() already has, without sampling anything."""
    with _cache_lock:
        return _cache.get(_key(path, fingerprint, times))


def cached_sample(path, fingerprint, times, progress_cb=None, cancel=None):
    """sample(), remembered for the session for this file and these times."""
    key = _key(path, fingerprint, times)
    with _cache_lock:
        found = _cache.get(key)
    if found is not None:
        return found
    found = sample(path, times, progress_cb=progress_cb, cancel=cancel)
    with _cache_lock:
        _cache[key] = found
    return found
