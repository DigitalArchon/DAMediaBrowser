# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Loudness over time, read from a file's audio with ffmpeg.

Two curves come back, both in dB at a fixed step: the full band, and the
bass below BASS_CUTOFF_HZ. The bass is the one that finds songs. Kick drum
and bass guitar stop between songs even when a crowd is cheering as loudly
as the band was playing, which makes full-band loudness useless for telling
applause from music. The full band is still what "the quietest moment" means
when lining a boundary up with a gap.

Everything is measured on a mono 8kHz copy made inside ffmpeg: nothing
analysed here needs more, and decoding is the only real cost.
"""

import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

STEP_SECONDS = 0.5
BASS_CUTOFF_HZ = 150
SAMPLE_RATE = 8000

# What digital silence (-inf) is recorded as, so arithmetic stays finite.
FLOOR_DB = -90.0

_RMS_KEY = "lavfi.astats.Overall.RMS_level"

# Paths that can go into a filtergraph option without escaping. Temporary
# directories practically always qualify; anything else is refused rather
# than escaped, since a mis-escaped path fails in confusing ways.
_SAFE_PATH = re.compile(r"^[A-Za-z0-9/_.\-]+$")


class LevelsError(Exception):
    pass


class Cancelled(LevelsError):
    pass


@dataclass(frozen=True)
class Levels:
    step: float
    full: list[float]
    bass: list[float]

    @property
    def duration(self) -> float:
        return len(self.full) * self.step


def _stats_chain(label_in: str, label_out: str, output_file: str, pre: str = "") -> str:
    samples = int(SAMPLE_RATE * STEP_SECONDS)
    # Only options every ffmpeg since 4.x understands: measure_overall and
    # friends would be faster, but the AppImage runs whatever ffmpeg the
    # host has.
    return (
        f"[{label_in}]{pre}asetnsamples=n={samples}:p=0,"
        "astats=metadata=1:reset=1,"
        f"ametadata=mode=print:key={_RMS_KEY}:file={output_file}[{label_out}]"
    )


def parse_metadata(text: str) -> list[float]:
    """The RMS levels from an ametadata print, in order."""
    levels = []
    for line in text.splitlines():
        if not line.startswith(_RMS_KEY + "="):
            continue
        value = line.split("=", 1)[1].strip()
        try:
            level = float(value)
        except ValueError:
            level = FLOOR_DB
        if level != level or level < FLOOR_DB:  # nan, or -inf
            level = FLOOR_DB
        levels.append(level)
    return levels


def read_levels(path, duration=None, progress_cb=None, cancel: threading.Event | None = None):
    """Measure a file's full-band and bass loudness every STEP_SECONDS.

    progress_cb, if given, is called with a whole percentage as the decode
    goes (only when `duration` is known). Setting `cancel` stops ffmpeg and
    raises Cancelled.
    """
    workdir = tempfile.mkdtemp(prefix="mcb-levels-")
    if not _SAFE_PATH.match(workdir):
        workdir = tempfile.mkdtemp(prefix="mcb-levels-", dir="/tmp")
    work = Path(workdir)
    full_file = work / "full.txt"
    bass_file = work / "bass.txt"

    graph = ";".join([
        f"[0:a:0]aformat=channel_layouts=mono,aresample={SAMPLE_RATE},asplit=2[x][y]",
        _stats_chain("x", "full", str(full_file)),
        _stats_chain("y", "bass", str(bass_file), pre=f"lowpass=f={BASS_CUTOFF_HZ},"),
    ])
    command = [
        "ffmpeg", "-nostdin", "-v", "error", "-nostats", "-progress", "pipe:1",
        "-i", str(path),
        "-filter_complex", graph,
        "-map", "[full]", "-f", "null", "-",
        "-map", "[bass]", "-f", "null", "-",
    ]

    try:
        try:
            proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
        except OSError as exc:
            raise LevelsError(f"could not run ffmpeg: {exc}") from exc

        # stderr is drained on its own thread: -v error keeps it short, but a
        # file with a damaged stream can still fill the pipe and stall ffmpeg.
        errors: list[str] = []
        drain = threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True)
        drain.start()

        last_percent = -1
        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.wait()
                raise Cancelled("audio analysis cancelled")
            if progress_cb and duration and line.startswith("out_time_us="):
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                percent = max(0, min(100, int(100 * done / duration)))
                if percent != last_percent:
                    last_percent = percent
                    progress_cb(percent)
        proc.wait()
        drain.join(timeout=5)

        if proc.returncode != 0:
            message = "".join(errors).strip() or f"ffmpeg exited with {proc.returncode}"
            raise LevelsError(message)

        try:
            full = parse_metadata(full_file.read_text(encoding="utf-8", errors="replace"))
            bass = parse_metadata(bass_file.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            raise LevelsError(f"ffmpeg wrote no levels: {exc}") from exc
    finally:
        for file in (full_file, bass_file):
            file.unlink(missing_ok=True)
        work.rmdir()

    if not full:
        raise LevelsError("this file has no audio that could be measured")
    n = min(len(full), len(bass))
    return Levels(step=STEP_SECONDS, full=full[:n], bass=bass[:n])


# Decoding a concert's audio takes long enough that doing it twice in one
# session - detect, then snap a tracklist - would be noticed. Keyed on the
# file's size and mtime, so an edited file is measured again.
_cache: dict[tuple, Levels] = {}
_cache_lock = threading.Lock()


def cached_levels(path, fingerprint, duration=None, progress_cb=None, cancel=None) -> Levels:
    key = (str(path), tuple(fingerprint) if fingerprint else None)
    with _cache_lock:
        levels = _cache.get(key)
    if levels is not None:
        return levels
    levels = read_levels(path, duration=duration, progress_cb=progress_cb, cancel=cancel)
    with _cache_lock:
        _cache[key] = levels
    return levels


def peek_cached(path, fingerprint) -> Levels | None:
    """Levels already measured this session, without measuring anything."""
    key = (str(path), tuple(fingerprint) if fingerprint else None)
    with _cache_lock:
        return _cache.get(key)
