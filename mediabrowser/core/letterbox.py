# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Black bars baked into a video's picture, so playback can crop them off.

A Blu-ray's picture is always 1920x1080. A film wider than 16:9 - Legend S
is 2.4:1 - is stored with black bars above and below, and mpv, fitting the
whole 16:9 frame into a window wider than that, adds bars at the sides too:
the picture never grows past the window's height, boxed in on every side.
Cropped to what's really picture, it fills the width.

ffmpeg's cropdetect finds the bars, over a second's worth of frames at
places all through the video. What any of them shows is picture, so the
crop keeps all of it: each edge comes in only as far as every sample was
black there. Captions can sit in a bar - Legend S puts its narration in
the bottom one - and a bar that ever carries something is kept, as much of
it as was used. Once measured, a video's crop is remembered across runs.
"""

from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

from . import config, frames

SAMPLES = 16
FRAMES_PER_SAMPLE = 30
WORKERS = 4
TIMEOUT_SECONDS = 40
# Fewer samples with a picture than this (the rest black, or unreadable)
# says too little to crop on.
MIN_PICTURES = 4
# A bar thinner than this share of the picture isn't worth cropping.
MIN_BAR = 0.015
# Black left on at each cropped edge, for whatever reached a row or two
# further than the samples saw - a caption's lower edge.
MARGIN = 4

_FRAME = re.compile(r"Stream #.*Video:.*?(\d{2,5})x(\d{2,5})")
_EXTENT = re.compile(r"x1:(-?\d+) x2:(-?\d+) y1:(-?\d+) y2:(-?\d+)")

# Crop = (width, height, x, y) in the picture's own pixels.
Crop = tuple[int, int, int, int]


def sample_times(duration: float) -> list[float]:
    return [duration * (i + 0.5) / SAMPLES for i in range(SAMPLES)]


def extent_at(video: dict, seconds: float, position=None):
    """((width, height), (x1, x2, y1, y2)) of the picture at `seconds`: the
    frame's size, and the first and last rows and columns that aren't black
    across a second of it. None if there's no picture there."""
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostdin",
                *frames.ffmpeg_input(video, seconds, position),
                "-an", "-sn", "-frames:v", str(FRAMES_PER_SAMPLE),
                "-vf", "cropdetect=limit=24:round=2:reset=0",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, errors="replace", timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    size = _FRAME.search(proc.stderr)
    # reset=0: each line is everything seen so far, so the last is the lot.
    extents = _EXTENT.findall(proc.stderr)
    if size is None or not extents:
        return None
    x1, x2, y1, y2 = (int(n) for n in extents[-1])
    if x2 <= x1 or y2 <= y1:  # all black
        return None
    return (int(size.group(1)), int(size.group(2))), (x1, x2, y1, y2)


def _bar(black: int, length: int) -> int:
    """How much of `black` pixels at one edge to crop: an even number, a
    little short of all of them, and nothing for a sliver."""
    bar = max(0, black - MARGIN) // 2 * 2
    return bar if bar >= MIN_BAR * length else 0


def crop_from(extents) -> Crop | None:
    """The crop that keeps all the picture any of `extents` (extent_at's
    answers) saw, or None if it would take nothing off."""
    extents = [e for e in extents if e is not None]
    if len(extents) < MIN_PICTURES:
        return None
    sizes = {size for size, _ in extents}
    if len(sizes) != 1:  # the picture changes shape: nothing to go by
        return None
    width, height = sizes.pop()
    x1 = min(e[0] for _, e in extents)
    x2 = max(e[1] for _, e in extents)
    y1 = min(e[2] for _, e in extents)
    y2 = max(e[3] for _, e in extents)
    left, right = _bar(x1, width), _bar(width - 1 - x2, width)
    top, bottom = _bar(y1, height), _bar(height - 1 - y2, height)
    if not (left or right or top or bottom):
        return None
    return width - left - right, height - top - bottom, left, top


def detect(video: dict) -> Crop | None:
    """Measure `video`'s black bars: its crop, or None for none. Slow - a
    few seconds of ffmpeg - so off the GUI thread."""
    duration = float(video.get("duration") or 0.0)
    if duration <= 0:
        return None
    times = sample_times(duration)
    positions = frames.positions_for(video, times)
    if video.get("type") == "bluray":
        times = [t for t in times if t in positions]
    with ThreadPoolExecutor(WORKERS) as pool:
        extents = list(pool.map(lambda t: extent_at(video, t, positions.get(t)), times))
    return crop_from(extents)


# --- remembered ----------------------------------------------------------------

_tables: dict[str, dict] = {}


def _file():
    return config.DATA_DIR / "letterbox.json"


def _table() -> dict:
    path = _file()
    key = str(path)
    if key not in _tables:
        try:
            with open(path, encoding="utf-8") as f:
                table = json.load(f)
        except (OSError, json.JSONDecodeError):
            table = {}
        _tables[key] = table if isinstance(table, dict) else {}
    return _tables[key]


def measured(video_id: str) -> bool:
    return video_id in _table()


def crop_for(video_id: str) -> Crop | None:
    """The crop measured for `video_id`, or None: no bars, or not measured."""
    crop = _table().get(video_id)
    if isinstance(crop, list) and len(crop) == 4 and all(isinstance(n, int) for n in crop):
        return tuple(crop)
    return None


def remember(video_id: str, crop: Crop | None) -> None:
    table = _table()
    table[video_id] = list(crop) if crop else None
    try:
        _file().parent.mkdir(parents=True, exist_ok=True)
        with open(_file(), "w", encoding="utf-8") as f:
            json.dump(table, f)
    except OSError:
        pass  # measured again next run
