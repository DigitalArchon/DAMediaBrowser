# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Measuring a video's audio from a dialog without freezing it.

Decoding a concert's soundtrack takes anywhere from a few seconds to a
minute, so it runs on a worker thread and reports a percentage. The result
is cached for the session, so a second dialog on the same video gets it at
once.
"""

from __future__ import annotations

import threading

from mediabrowser.core import audio_levels, chaptergen, library, stage_light
from mediabrowser.gui.worker import run_job


def can_analyse(video) -> bool:
    """Only files: ffmpeg reads them directly, where a Blu-ray or DVD folder
    would need reading through the disc - and discs have chapters anyway.
    """
    return video.get("type") == "file"


def cached(video):
    return audio_levels.peek_cached(video["path"], library.file_fingerprint(video["path"]))


def cached_light(video, levels):
    """Stage lighting already sampled this session for these levels' gaps."""
    times = chaptergen.light_sample_times(levels, video["duration"])
    return stage_light.peek_cached(
        video["path"], library.file_fingerprint(video["path"]), times
    )


def start_light(parent, video, levels, cancel: threading.Event, *,
                on_progress, on_done, on_failed):
    """Sample the stage lighting around each quiet stretch the audio found,
    on a worker thread. The result is {seconds: brightness}."""
    path = video["path"]
    fingerprint = library.file_fingerprint(path)
    times = chaptergen.light_sample_times(levels, video["duration"])

    def work(progress_cb):
        return stage_light.cached_sample(
            path, fingerprint, times,
            progress_cb=lambda percent: progress_cb(str(percent)),
            cancel=cancel,
        )

    return run_job(
        parent,
        work,
        wants_progress=True,
        on_progress=lambda text: on_progress(int(text)),
        on_done=on_done,
        on_failed=on_failed,
    )


def start(parent, video, cancel: threading.Event, *, on_progress, on_done, on_failed):
    """Measure `video`'s audio on a worker thread. Returns the job, which
    the caller must keep referenced until it finishes.
    """
    path = video["path"]
    fingerprint = library.file_fingerprint(path)
    duration = video.get("duration")

    def work(progress_cb):
        return audio_levels.cached_levels(
            path, fingerprint, duration=duration,
            progress_cb=lambda percent: progress_cb(str(percent)),
            cancel=cancel,
        )

    return run_job(
        parent,
        work,
        wants_progress=True,
        on_progress=lambda text: on_progress(int(text)),
        on_done=on_done,
        on_failed=on_failed,
    )
