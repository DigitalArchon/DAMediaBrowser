# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import subprocess


class ProbeError(Exception):
    pass


TIMEOUT_SECONDS = 60
# How often a running ffprobe checks whether the scan was cancelled.
CANCEL_POLL_SECONDS = 0.2


class Cancelled(ProbeError):
    pass


def _run_ffprobe(args, cancel=None):
    """Run ffprobe, giving up after TIMEOUT_SECONDS or as soon as `cancel`
    is set - a read over a stalled network share can otherwise sit there
    for the whole timeout after someone has asked to stop.
    """
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
    except OSError as exc:
        raise ProbeError(str(exc)) from exc
    waited = 0.0
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=CANCEL_POLL_SECONDS)
            return proc.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            waited += CANCEL_POLL_SECONDS
            cancelled = cancel is not None and cancel.is_set()
            if cancelled or waited >= TIMEOUT_SECONDS:
                proc.kill()
                proc.communicate()
                if cancelled:
                    raise Cancelled("cancelled") from None
                raise ProbeError(f"ffprobe took longer than {TIMEOUT_SECONDS}s") from None


def read_regular_file_info(path, cancel=None) -> dict:
    """Read duration + chapters from a regular media file via ffprobe.

    Returns {"duration": float_seconds, "chapters": [{"start", "end", "title"}]}
    If the file has no embedded chapters, a single synthetic chapter spanning
    the whole file is returned so playback/browsing still works uniformly.
    """
    returncode, stdout, stderr = _run_ffprobe(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_chapters",
            "-show_format",
            str(path),
        ],
        cancel,
    )

    if returncode != 0:
        raise ProbeError(stderr.strip() or "ffprobe failed")

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"could not parse ffprobe output: {exc}") from exc

    duration = float(data.get("format", {}).get("duration", 0.0) or 0.0)

    chapters = []
    for ch in data.get("chapters", []):
        start = float(ch.get("start_time", 0.0))
        end = float(ch.get("end_time", duration))
        title = (ch.get("tags") or {}).get("title") or None
        chapters.append({"start": start, "end": end, "title": title})

    if not chapters:
        chapters = [{"start": 0.0, "end": duration, "title": None}]

    return {"duration": duration, "chapters": chapters}
