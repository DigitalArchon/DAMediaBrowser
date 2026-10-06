# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Single frames of a video, as small JPEGs, for showing to a model.

A concert Blu-ray usually puts the song's title on screen a few seconds
after it starts, and the stage, the projection and the band's positions
say plenty about which song it is even when it doesn't. A frame 512 pixels
wide keeps a caption legible and costs a model a couple of hundred tokens,
so a whole concert's chapter starts go in one request.

Files are read directly; a Blu-ray folder goes through ffmpeg's bluray:
protocol with the title's playlist, and a DVD title is read from its VOBs
(core.dvd) into ffmpeg's input. Like stage_light, each grab is one ffmpeg
seek, and several run at once because each is mostly waiting.
"""

from __future__ import annotations

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

from .audio_levels import Cancelled

FRAME_WIDTH = 512
# JPEG quality for ffmpeg's mjpeg encoder: 2 is best, 31 worst. 5 keeps
# small caption text readable without the file growing past ~40KB.
JPEG_QUALITY = 5
WORKERS = 4
TIMEOUT_SECONDS = 40


def ffmpeg_input(video: dict, seconds: float, position=None) -> list[str]:
    """The ffmpeg arguments that open a library video at `seconds`.

    A file is seeked by time. A Blu-ray title goes through ffmpeg's bluray:
    protocol, which can't be seeked by time (its timestamps start hours in
    and its duration reads as seconds), so it is seeked by byte to
    `position` - (offset, seconds_before) from bluray.byte_positions - and
    decoded the last stretch.
    """
    if video.get("type") == "bluray":
        args = []
        if position is not None:
            args += ["-skip_initial_bytes", str(position[0])]
        if video.get("playlist") is not None:
            args += ["-playlist", str(video["playlist"])]
        args += ["-i", f"bluray:{video['path']}"]
        if position is not None and position[1] > 0.05:
            args += ["-ss", f"{position[1]:.2f}"]
        return args
    return ["-ss", f"{max(0.0, seconds):.2f}", "-i", str(video["path"])]


def positions_for(video: dict, times) -> dict:
    """Disc positions for `times` - a Blu-ray's bytes, a DVD's sectors - or
    {} for a file (which needs none) or a disc that couldn't be opened."""
    if video.get("type") == "dvd":
        from . import dvd

        found = {}
        for t in times:
            try:
                found[t] = dvd.position(video, t)
            except Exception:
                continue
        return found
    if video.get("type") != "bluray":
        return {}
    from . import bluray

    try:
        return bluray.byte_positions(video["path"], video.get("title_idx", 0), times)
    except Exception:
        return {}


def grab(video: dict, seconds: float, width: int = FRAME_WIDTH, position=None) -> bytes | None:
    """The frame at `seconds`, as JPEG bytes, or None if there is no
    picture there. `position` is the Blu-ray byte position (see
    ffmpeg_input); looked up here if not given."""
    if video.get("type") in ("bluray", "dvd") and position is None:
        position = positions_for(video, [seconds]).get(seconds)
        if position is None:
            return None
    feed = None
    if video.get("type") == "dvd":
        from . import dvd

        source, feed = dvd.ffmpeg_source(video, seconds, position)
    else:
        source = ffmpeg_input(video, seconds, position)
    returncode, stdout = run_ffmpeg(
        [
            "ffmpeg", "-v", "error", "-nostdin",
            *source,
            "-frames:v", "1", "-an", "-sn",
            "-vf", f"scale={width}:-2",
            "-q:v", str(JPEG_QUALITY),
            "-f", "image2pipe", "-c:v", "mjpeg", "-",
        ],
        feed,
    )
    if returncode != 0 or not stdout.startswith(b"\xff\xd8"):
        return None
    return stdout


def run_ffmpeg(args, feed=None, timeout: float = TIMEOUT_SECONDS) -> tuple[int | None, bytes]:
    """Run ffmpeg for what it writes to its output, and (returncode,
    output); None for a returncode when it couldn't run or took too long.
    `feed`, given, writes its input - a DVD's sectors - on a thread of its
    own, and stops when ffmpeg has had all it wants."""
    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE if feed else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None, b""
    stop = threading.Event()
    killed = threading.Event()
    timer = threading.Timer(timeout, lambda: (killed.set(), stop.set(), proc.kill()))
    timer.daemon = True
    timer.start()

    def write():
        try:
            feed(proc.stdin, stop)
        except (OSError, ValueError):
            pass  # ffmpeg has stopped reading: it has its frame
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    writer = threading.Thread(target=write, daemon=True) if feed else None
    if writer is not None:
        writer.start()
    try:
        stdout = proc.stdout.read()
        proc.wait()
    finally:
        stop.set()
        timer.cancel()
        if writer is not None:
            writer.join(timeout=5)
    if killed.is_set():
        return None, b""
    return proc.returncode, stdout


def grab_many(video: dict, times, progress_cb=None, cancel: threading.Event | None = None,
              width: int = FRAME_WIDTH) -> dict[float, bytes]:
    """{time: jpeg} for each of `times` that has a picture. progress_cb gets
    a whole percentage; `cancel` stops it with audio_levels.Cancelled."""
    times = list(times)
    result: dict[float, bytes] = {}
    if not times:
        return result
    # One trip through libbluray for every position, not one per frame.
    positions = positions_for(video, times)
    if video.get("type") in ("bluray", "dvd"):
        times = [t for t in times if t in positions]
        if not times:
            return result
    done = 0
    last_percent = -1
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {
            pool.submit(grab, video, t, width, positions.get(t)): t for t in times
        }
        for future in futures:
            if cancel is not None and cancel.is_set():
                for pending in futures:
                    pending.cancel()
                raise Cancelled("frame grab cancelled")
            frame = future.result()
            if frame is not None:
                result[futures[future]] = frame
            done += 1
            percent = int(100 * done / len(times))
            if progress_cb and percent != last_percent:
                last_percent = percent
                progress_cb(percent)
    return result


# Frames already grabbed this session, so asking the model again about the
# same video (with a different tracklist, say) doesn't decode them again.
_cache: dict[tuple, bytes | None] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    """Forget everything measured: all library data is being deleted."""
    with _cache_lock:
        _cache.clear()


def _key(video: dict, seconds: float, width: int):
    title = video.get("playlist") if video.get("type") == "bluray" else video.get("title_idx")
    return (video["path"], title, round(seconds, 2), width)


def cached_grab_many(video: dict, times, progress_cb=None, cancel=None,
                     width: int = FRAME_WIDTH) -> dict[float, bytes]:
    """grab_many, remembering every frame for the session."""
    times = list(times)
    with _cache_lock:
        wanted = [t for t in times if _key(video, t, width) not in _cache]
    fresh = (
        grab_many(video, wanted, progress_cb=progress_cb, cancel=cancel, width=width)
        if wanted else {}
    )
    with _cache_lock:
        for t in wanted:
            _cache[_key(video, t, width)] = fresh.get(t)
        return {
            t: _cache[_key(video, t, width)]
            for t in times if _cache.get(_key(video, t, width)) is not None
        }
