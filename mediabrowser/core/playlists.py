# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Playlists: a queue saved under a name, to be played again later.

They're kept in the library's own data, beside the chapters they point
into, so a catalog taken to another device takes its playlists with it.

An entry is a chapter of a video, by the video's id. A playlist keeps
whether its queue played as audio or as video, which playing it can
override; where video plays, in the app or in mpv's own window, is chosen
each time it's played, not saved with it. Each entry also keeps the
names it had and where its chapter started: an entry whose video has gone
still reads as something, and one whose video's chapters have since been
split or merged finds the same music again by its start rather than by a
number that now means another song.
"""

from __future__ import annotations

import time

from . import utils
from .playback import QueueEntry

KEY = "playlists"

# How far a chapter may have moved and still be the one an entry saved.
SAME_START_SECONDS = 1.5


def all_playlists(data: dict) -> list[dict]:
    """Every playlist, by name."""
    return sorted(data.get(KEY) or [], key=lambda p: p["name"].lower())


def get(data: dict, name: str) -> dict | None:
    wanted = name.strip().lower()
    return next((p for p in data.get(KEY) or [] if p["name"].lower() == wanted), None)


def save(data: dict, name: str, entries, videos=None, audio_only: bool | None = None) -> dict:
    """Save queue entries as the playlist `name`, replacing one of that name,
    to play as audio or as video (`audio_only`; else as the first entry
    does). `videos` (the library's) gives each entry its chapter's start."""
    name = name.strip()
    if not name:
        raise ValueError("a playlist needs a name")
    videos = videos if videos is not None else data.get("videos", {})
    saved = []
    for entry in entries:
        item = {
            "video_id": entry.video_id,
            "chapter_index": entry.chapter_index,
            "title": entry.title,
            "video_name": entry.video_name,
            "duration": entry.duration,
        }
        video = videos.get(entry.video_id)
        if video and 0 <= entry.chapter_index < len(video["chapters"]):
            item["start"] = video["chapters"][entry.chapter_index]["start"]
        saved.append(item)
    if audio_only is None:
        audio_only = entries[0].audio_only if entries else True
    playlist = {"name": name, "entries": saved, "audio_only": bool(audio_only),
                "saved": time.strftime("%Y-%m-%d %H:%M")}
    existing = get(data, name)
    playlists = data.setdefault(KEY, [])
    if existing is not None:
        playlists[playlists.index(existing)] = playlist
    else:
        playlists.append(playlist)
    return playlist


def delete(data: dict, name: str) -> bool:
    playlist = get(data, name)
    if playlist is None:
        return False
    data[KEY].remove(playlist)
    return True


def rename(data: dict, old: str, new: str) -> None:
    playlist = get(data, old)
    new = new.strip()
    if playlist is None or not new:
        return
    other = get(data, new)
    if other is not None and other is not playlist:
        raise ValueError(f"there is already a playlist called {other['name']}")
    playlist["name"] = new


def _chapter_index(video: dict, item: dict) -> int | None:
    chapters = video["chapters"]
    start = item.get("start")
    if start is not None:
        nearest = min(range(len(chapters)), key=lambda i: abs(chapters[i]["start"] - start),
                      default=None)
        if nearest is not None and abs(chapters[nearest]["start"] - start) <= SAME_START_SECONDS:
            return nearest
    index = item.get("chapter_index")
    return index if isinstance(index, int) and 0 <= index < len(chapters) else None


def queue_entries(data: dict, playlist: dict, audio_only: bool | None = None,
                  default_audio_only: bool = True) -> tuple[list[QueueEntry], int]:
    """A playlist as queue entries, named as the library names them now;
    and how many of its entries couldn't be found. They play as `audio_only`
    says, else as the playlist was saved - or, one saved before playlists
    kept that, as `default_audio_only`."""
    if audio_only is None:
        audio_only = plays_audio_only(playlist, default_audio_only)
    videos = data.get("videos", {})
    entries, missing = [], 0
    for item in playlist.get("entries", []):
        video = videos.get(item.get("video_id"))
        index = _chapter_index(video, item) if video else None
        if index is None:
            missing += 1
            continue
        chapter = video["chapters"][index]
        entries.append(QueueEntry(
            video_id=item["video_id"],
            chapter_index=index,
            audio_only=audio_only,
            title=utils.chapter_label(index, chapter),
            video_name=video["display_name"],
            duration=chapter["end"] - chapter["start"],
        ))
    return entries, missing


def plays_audio_only(playlist: dict, default: bool = True) -> bool:
    """Whether a playlist was saved playing as audio; `default` for one
    saved before that was kept."""
    saved = playlist.get("audio_only")
    return saved if isinstance(saved, bool) else default


def length(playlist: dict) -> float:
    return sum(item.get("duration") or 0.0 for item in playlist.get("entries", []))


def remap_ids(data: dict, id_map: dict) -> None:
    """Point every playlist at its videos' new ids, after the library moved."""
    for playlist in data.get(KEY) or []:
        for item in playlist.get("entries", []):
            item["video_id"] = id_map.get(item["video_id"], item["video_id"])


def merge(current: dict, incoming: dict) -> int:
    """Bring in an imported catalog's playlists that this one hasn't a
    playlist of the same name for. Returns how many."""
    added = 0
    for playlist in incoming.get(KEY) or []:
        if get(current, playlist["name"]) is None:
            current.setdefault(KEY, []).append(playlist)
            added += 1
    return added
