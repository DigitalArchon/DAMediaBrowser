# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Locked and private: how far the app may go with a video.

- Locked: nothing changes it. No renaming, no chapters edited or named by
  hand, nothing identified, and a rescan keeps it exactly as it's stored.
  It still plays and queues.
- Private: the person may change it as they like, and the app may work on
  it locally - measure its audio, read its own chapters - but nothing about
  it goes out: no MusicBrainz search, no AI.

Either is set on a single video, or on a whole library (in its settings),
which then holds for every video in it whatever the video's own says - and
wherever the video is seen from: a library nested in another shares its
videos with it, so BABYMETAL made private keeps its videos private when
they're on the shelf as MusicVids' too.
"""

from __future__ import annotations

from pathlib import Path

LOCKED = "locked"
PRIVATE = "private"
FLAGS = (LOCKED, PRIVATE)

LOCKED_TIP = "Locked: nothing changes it - by hand, by a scan or by Identify."
PRIVATE_TIP = (
    "Private: yours to change, and local detection works, but MusicBrainz and the AI "
    "are never sent anything about it."
)


def _settings(data: dict) -> dict:
    return data.get("settings") or {}


def library_flag(data: dict, flag: str) -> bool:
    return bool(_settings(data).get(flag))


def set_library_flag(data: dict, flag: str, on: bool) -> None:
    if on:
        data["settings"][flag] = True
    else:
        data["settings"].pop(flag, None)


def set_flag(video: dict, flag: str, on: bool) -> None:
    if on:
        video[flag] = True
    else:
        video.pop(flag, None)


def covering(video: dict, flag: str) -> str | None:
    """The stored library, set locked or private as a whole, that holds this
    video - whichever library it's seen from."""
    from . import store

    path = video.get("path")
    if not path:
        return None
    for root, settings in store.flagged_roots():
        if settings.get(flag) and store.is_under(path, root):
            return root
    return None


def is_locked(data: dict, video: dict) -> bool:
    return (library_flag(data, LOCKED) or bool(video.get(LOCKED))
            or covering(video, LOCKED) is not None)


def is_private(data: dict, video: dict) -> bool:
    return (library_flag(data, PRIVATE) or bool(video.get(PRIVATE))
            or covering(video, PRIVATE) is not None)


def may_change(data: dict, video: dict) -> bool:
    return not is_locked(data, video)


def may_go_online(data: dict, video: dict) -> bool:
    """Whether MusicBrainz or the AI may be told about it."""
    return not is_locked(data, video) and not is_private(data, video)


def label(data: dict, video: dict) -> str:
    """What the shelf says: "Locked", "Private", or nothing - with
    "(library)" when it's the whole library's."""
    for flag, text in ((LOCKED, "Locked"), (PRIVATE, "Private")):
        if library_flag(data, flag):
            return f"{text} (library)"
        if video.get(flag):
            return text
        root = covering(video, flag)
        if root is not None:
            return f"{text} ({Path(root).name or root})"
    return ""


def tip(data: dict, video: dict) -> str:
    if is_locked(data, video):
        text = LOCKED_TIP
        flag = LOCKED
    elif is_private(data, video):
        text = PRIVATE_TIP
        flag = PRIVATE
    else:
        return ""
    if library_flag(data, flag):
        text += " Set for the whole library."
    elif not video.get(flag):
        root = covering(video, flag)
        if root is not None:
            text += f" Set for the whole {Path(root).name or root} library, which holds it."
    return text
