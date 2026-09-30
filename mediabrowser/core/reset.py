# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Resetting a title, or a whole library, to how it was first scanned - and
undoing that.

A reset clears everything the app has added: chapter names from any
source, chapters it made or edited, a name given by hand, the MusicBrainz
release, and hidden flags; for a library, its hidden folders and the last
Identify run's history too, and - only when asked - its playlists. The
chapters are read from the files and discs again.

Kept, always: whether a video is locked or private (a reset mustn't send a
private video's name anywhere, nor make a locked one changeable), and any
locked video whole. A video whose file can't be read right now is kept as
it was too - there's nothing to reset it to.

The last reset in a library can be undone, even after a restart: what it
cleared is kept in the library's settings until the next reset replaces it
or the undo uses it.
"""

from __future__ import annotations

import copy
import time

from . import artwork, library, naming, playlists, protection, store

UNDO_KEY = "reset_undo"
# What a library reset clears from the library's settings.
LIBRARY_SETTINGS = ("hidden_folders", "identify_run")
# What a title reset clears from the video.
VIDEO_KEYS = ("custom_name", "musicbrainz_release_id", "hidden", "chapter_origin")


class ResetError(Exception):
    """Why it couldn't be reset, in words for the person."""


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def describe_video(video: dict) -> list[str]:
    """What a title reset would clear from this video, in words."""
    lost = []
    # Names the file or disc carries itself come back when it's read again.
    named = sum(1 for chapter in video["chapters"]
                if naming.is_named(chapter) and chapter.get("source") in library.KEPT_SOURCES)
    if named:
        lost.append(f"{named} chapter name(s) given here")
    if video.get("chapter_origin"):
        lost.append("the chapters made or edited here")
    if video.get("custom_name"):
        lost.append(f"its name “{video['custom_name']}”")
    if video.get("musicbrainz_release_id"):
        lost.append("the MusicBrainz release it was named from")
    if video.get("hidden"):
        lost.append("its being hidden")
    return lost


def reset_video(data: dict, video_id: str, chapters: list[dict]) -> dict:
    """Put a video back as scanned, with `chapters` its own read afresh
    (library.original_chapters). The undo keeps what it was."""
    video = data["videos"].get(video_id)
    if video is None:
        raise ResetError("that video isn't in the library any more")
    if protection.is_locked(data, video):
        raise ResetError(f"{video['display_name']} is locked")
    data["settings"][UNDO_KEY] = {
        "kind": "video", "when": _stamp(), "video_id": video_id,
        "name": video["display_name"], "video": copy.deepcopy(video),
    }
    for key in VIDEO_KEYS:
        video.pop(key, None)
    video["display_name"] = video.get("auto_name", video["display_name"])
    video["chapters"] = chapters
    library.name_single_chapters({"settings": data["settings"], "videos": {video_id: video}})
    if snapshot_release(data):
        artwork.forget(video_id)  # its cover was that release's sleeve
    return video


def snapshot_release(data: dict) -> bool:
    undo = last(data) or {}
    return bool((undo.get("video") or {}).get("musicbrainz_release_id"))


def reset_library(root, keep_playlists: bool = False, progress_cb=None, cancel=None) -> dict:
    """Rescan a library as if for the first time, keeping what it was for
    undo. Returns the library as reset (and saved)."""
    before = store.load_library_for_root(root)
    if protection.library_flag(before, protection.LOCKED):
        raise ResetError("the library is locked")
    snapshot = {
        "kind": "library", "when": _stamp(),
        "videos": copy.deepcopy(before["videos"]),
        "settings": {key: copy.deepcopy(before["settings"][key])
                     for key in LIBRARY_SETTINGS if key in before["settings"]},
        playlists.KEY: copy.deepcopy(before.get(playlists.KEY) or []),
    }
    data = library.rescan(root, progress_cb=progress_cb, cancel=cancel, fresh=True)
    for key in LIBRARY_SETTINGS:
        data["settings"].pop(key, None)
    if not keep_playlists:
        data.pop(playlists.KEY, None)
    data["settings"][UNDO_KEY] = snapshot
    _forget_sleeves(before["videos"])
    store.save_library(data)
    return data


def last(data: dict) -> dict | None:
    """The reset that can be undone in this library, if there is one."""
    return data.get("settings", {}).get(UNDO_KEY)


def describe(undo: dict) -> str:
    if undo["kind"] == "video":
        return f"the reset of {undo['name']} ({undo['when']})"
    return f"the reset of the whole library ({undo['when']})"


def undo(data: dict) -> str | None:
    """Put back what the last reset cleared; returns what was undone, in
    words, or None if there was nothing to undo."""
    snapshot = data["settings"].pop(UNDO_KEY, None)
    if snapshot is None:
        return None
    if snapshot["kind"] == "video":
        video_id = snapshot["video_id"]
        data["videos"][video_id] = snapshot["video"]
        _forget_sleeves({video_id: snapshot["video"]})
    else:
        data["videos"] = snapshot["videos"]
        for key in LIBRARY_SETTINGS:
            data["settings"].pop(key, None)
        data["settings"].update(snapshot["settings"])
        if snapshot.get(playlists.KEY):
            data[playlists.KEY] = snapshot[playlists.KEY]
        _forget_sleeves(data["videos"])
    return describe(snapshot)


def _forget_sleeves(videos: dict) -> None:
    """Covers taken from a MusicBrainz release: found again for the video as
    it now is. A frame grab stays right either way."""
    for video_id, video in videos.items():
        if video.get("musicbrainz_release_id"):
            artwork.forget(video_id)
