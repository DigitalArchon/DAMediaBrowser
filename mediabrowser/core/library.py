# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

import copy
import os
import time
from pathlib import Path

from . import artwork, bluray, config, ffprobe_chapters, playlists, protection, scanner, store
from .scanner import ScanCancelled

# How often, at most, a scan reports which folder it has reached. A walk
# through a big tree of empty folders enters thousands a second, and each
# report crosses to the GUI thread.
FOLDER_PROGRESS_SECONDS = 0.1

# Where a library on a network share remembers the share's address (an
# smb://, sftp://... URI of the library folder itself), so a rescan can
# reconnect it rather than only report it gone.
NETWORK_URI_SETTING = "network_uri"


# Title sources the app gave, which a rescan must carry over: the file has
# nothing to read them back from. "embedded" is re-read instead.
KEPT_SOURCES = ("manual", "musicbrainz", "ai", "menu")


def _merge_chapters(new_chapters, old_chapters):
    """Carry forward the chapter titles the app gave (by hand, from
    MusicBrainz, from the AI or read off the disc's menu) from a previous
    scan when the chapter count is unchanged, matching by index.
    """
    if old_chapters and len(old_chapters) == len(new_chapters):
        for new_ch, old_ch in zip(new_chapters, old_chapters, strict=True):
            if old_ch.get("source") in KEPT_SOURCES:
                new_ch["title"] = old_ch["title"]
                new_ch["source"] = old_ch["source"]
                if old_ch.get("original_title"):
                    new_ch["original_title"] = old_ch["original_title"]
    for i, ch in enumerate(new_chapters):
        ch.setdefault("index", i)
        ch.setdefault("source", "embedded" if ch.get("title") else "auto-numbered")
    return new_chapters


# Where a video's chapters came from, when the app made or changed them
# rather than reading them from the file or disc. Stored on the video as
# "chapter_origin"; absent means the chapters are the file's own.
ORIGIN_TRACKLIST = "tracklist"  # placed by a tracklist's lengths
ORIGIN_ESTIMATED = "estimated"  # estimated from the audio
ORIGIN_EDITED = "edited"  # the file's own, split, merged or moved by hand
ORIGIN_MARKED = "marked"  # marked by hand while the video played

# Things the app learned about a video that can't be read back from the file
# or disc, so a rescan that re-reads it must carry them over. Without this,
# re-probing a changed file - and every Blu-ray title, which is re-probed on
# every rescan - forgot which MusicBrainz release it was named from (and
# cover art fell back to a frame grab), or that it had been hidden.
CARRIED_OVER_KEYS = (
    "musicbrainz_release_id", "hidden", "custom_name", protection.LOCKED, protection.PRIVATE,
    "marked_named",
)

# What a video is underneath, whatever chapters it has been given since: a
# Blu-ray title, a file that came with chapters of its own, or one that came
# without. Recorded when the file is read, since the chapters stored later
# may be the app's rather than the file's.
MEDIA_TYPE = "media_type"
MEDIA_BLURAY = "bluray"
MEDIA_CHAPTERED = "chaptered"
MEDIA_PLAIN = "plain"
MEDIA_LABELS = {
    MEDIA_BLURAY: "Blu-ray",
    MEDIA_CHAPTERED: "File with chapters",
    MEDIA_PLAIN: "File, no chapters",
}
MEDIA_TIPS = {
    MEDIA_BLURAY: "A title of a Blu-ray disc folder, with the disc's own chapter marks.",
    MEDIA_CHAPTERED: "A video file that came with chapters of its own.",
    MEDIA_PLAIN: "A video file that came without chapters - any it has were added here.",
}


def media_type_of_file(chapters) -> str:
    """For a file as read: ffprobe reports one whole-file chapter for a file
    with none."""
    return MEDIA_CHAPTERED if len(chapters) > 1 else MEDIA_PLAIN


def media_type(video) -> str | None:
    """The video's media type; None for a file whose chapters the app has
    since made or changed, from a library older than the record of it - the
    next rescan reads the file to find out."""
    stored = video.get(MEDIA_TYPE)
    if stored:
        return stored
    if video.get("type") == "bluray":
        return MEDIA_BLURAY
    if not video.get("chapter_origin"):
        return media_type_of_file(video.get("chapters") or [])
    return None


def media_label(video) -> str:
    kind = media_type(video)
    return MEDIA_LABELS[kind] if kind else "File"

# How much a video's length may change before chapters the app made for it
# are thrown away: they were placed against the old length, and against a
# different cut of the video they would be in the wrong places.
KEEP_CHAPTERS_TOLERANCE_SECONDS = 1.0


def _keep_own_chapters(old_video, duration):
    """The chapters the app made for this video, if they still fit it.

    Those chapters exist only in the library - there is nothing in the file
    to read them back from - so a rescan must not replace them with the
    single whole-file chapter ffprobe reports for a file without any.
    """
    if not old_video or not old_video.get("chapter_origin"):
        return None
    old_duration = old_video.get("duration") or 0.0
    if abs(old_duration - duration) > KEEP_CHAPTERS_TOLERANCE_SECONDS:
        return None
    return old_video["chapters"]


MOVED_HINT = (
    "If the folder was renamed or moved, or the NAS has a new address or is a new "
    "one, right-click the library and choose Locate Moved Folder…: its titles and "
    "chapters go with it. Nothing in this library has been changed."
)


class LibraryUnavailable(Exception):
    """The library's folder isn't there: an unmounted network share or a
    disconnected drive, most likely. Nothing is changed.
    """


def _carry_over(new_video, old_video):
    for key in CARRIED_OVER_KEYS:
        if old_video and key in old_video:
            new_video[key] = old_video[key]
    # The name the scan gave is kept apart from a name given by hand, so
    # clearing the one brings back the other.
    new_video["auto_name"] = new_video["display_name"]
    if new_video.get("custom_name"):
        new_video["display_name"] = new_video["custom_name"]


def name_single_chapters(data) -> int:
    """Give each single-chapter video file's chapter its file's name, marked as
    only that (naming.FILENAME_SOURCE) - and keep it in step with the
    video's name as that changes. Returns how many were set."""
    from . import naming, utils

    changed = 0
    for video in data["videos"].values():
        if protection.is_locked(data, video):
            continue
        chapters = video.get("chapters") or []
        # A file's name is usually what's in it; a Blu-ray title's ("Disc -
        # Title 3") never is.
        if len(chapters) != 1 or video.get("type") != "file":
            continue
        chapter = chapters[0]
        provisional = chapter.get("source") == naming.FILENAME_SOURCE
        if not provisional and not naming.is_placeholder(chapter.get("title")):
            continue  # it has a real name
        name = utils.name_from_filename(video["display_name"])
        if chapter.get("title") != name or not provisional:
            chapter["title"] = name
            chapter["source"] = naming.FILENAME_SOURCE
            changed += 1
    return changed


def set_custom_name(video, name) -> None:
    """Name a video by hand - a disc's "Title 5" as "Behind the Scenes" -
    or, with no name, go back to the one the scan gave it."""
    name = (name or "").strip()
    if name:
        video["custom_name"] = name
        video.setdefault("auto_name", video["display_name"])
        video["display_name"] = name
    else:
        video.pop("custom_name", None)
        video["display_name"] = video.get("auto_name", video["display_name"])


def file_fingerprint(path):
    """Size and modification time, as a cheap "has this changed?" check.

    Not a hash: hashing every file in a library would cost more than the
    ffprobe run it is trying to avoid. Size plus mtime catches every
    realistic edit, and a rescan is always available if one slips through.
    """
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return [stat.st_size, int(stat.st_mtime)]


def _unchanged(path, old_video):
    """Whether a previously scanned file can be reused as-is."""
    if not old_video or old_video.get("type") != "file":
        return False
    stored = old_video.get("fingerprint")
    if not stored:
        return False
    current = file_fingerprint(path)
    return current is not None and list(stored) == list(current)


def rescan(root_path, progress_cb=None, force=False, cancel=None, network_uri=None,
           fresh=False):
    """Scan root_path for media, returning a fresh library dict for that
    folder. Preserves manual/musicbrainz chapter names and settings from
    that folder's own previously stored data - other folders' stored
    libraries are untouched.

    Files whose size and mtime are unchanged since the last scan are carried
    over without re-running ffprobe, which is almost all of them on a rescan
    and is the difference between a moment and several minutes on a large
    library. Pass force=True to re-probe everything regardless.

    `fresh` scans as if for the first time (reset.reset_library): nothing
    the app stored about a video is kept but its locked and private
    settings. A locked video is kept whole all the same, and so is one
    whose file can't be read now - there's nothing to reset it to.

    Setting `cancel` (a threading.Event) stops the scan with ScanCancelled
    and leaves the stored library exactly as it was: nothing is saved until
    the end. `network_uri`, for a library on a network share, is remembered
    with it.
    """
    root_path = str(Path(root_path).resolve())
    # A network share that has dropped, or a drive that is unplugged, looks
    # exactly like a folder with nothing in it. Scanning it anyway would
    # "find" that every video is gone.
    if not Path(root_path).is_dir():
        raise LibraryUnavailable(
            f"{root_path} isn't available right now. If it's on a network share "
            f"or a removable drive, reconnect it and rescan.\n\n{MOVED_HINT}"
        )
    data = store.load_library_for_root(root_path)
    stored = data["videos"]

    def locked(old_video):
        return old_video is not None and protection.is_locked(data, old_video)

    if fresh:
        force = True
        old_videos = {
            video_id: video if locked(video) else {
                key: video[key] for key in (protection.LOCKED, protection.PRIVATE)
                if key in video
            }
            for video_id, video in stored.items()
        }
    else:
        old_videos = stored

    min_bluray_seconds = data["settings"]["min_bluray_title_seconds"]
    if network_uri:
        data["settings"][NETWORK_URI_SETTING] = network_uri

    new_videos = {}
    last_report = [0.0]

    def on_folder(folder):
        now = time.monotonic()
        if progress_cb and now - last_report[0] >= FOLDER_PROGRESS_SECONDS:
            last_report[0] = now
            progress_cb(str(folder))

    for kind, path in scanner.find_media_units(root_path, cancel=cancel, on_folder=on_folder):
        if cancel is not None and cancel.is_set():
            raise ScanCancelled()
        if progress_cb:
            progress_cb(str(path))

        if kind == "file":
            video_id = store.make_video_id("file", path)
            old_video = old_videos.get(video_id)

            if locked(old_video):
                new_videos[video_id] = old_video
                continue
            if not force and _unchanged(path, old_video):
                if media_type(old_video) is None:
                    _learn_media_type(old_video, cancel)
                new_videos[video_id] = old_video
                continue

            try:
                info = ffprobe_chapters.read_regular_file_info(path, cancel=cancel)
            except ffprobe_chapters.Cancelled:
                raise ScanCancelled() from None
            except Exception:
                # Couldn't be read this time (a flaky network read, a file
                # still being copied): keep what was known rather than
                # forgetting it.
                if video_id in stored:
                    new_videos[video_id] = stored[video_id]
                continue

            kept = _keep_own_chapters(old_video, info["duration"])
            chapters = kept or _merge_chapters(
                info["chapters"], (old_video or {}).get("chapters")
            )
            new_videos[video_id] = {
                "type": "file",
                "path": str(path),
                "display_name": Path(path).stem,
                "duration": info["duration"],
                "chapters": chapters,
                "fingerprint": file_fingerprint(path),
                MEDIA_TYPE: media_type_of_file(info["chapters"]),
            }
            if kept:
                new_videos[video_id]["chapter_origin"] = old_video["chapter_origin"]
            _carry_over(new_videos[video_id], old_video)

        elif kind == "bluray":
            try:
                titles = bluray.probe_bluray_disc(path, min_bluray_seconds)
            except Exception:
                for video_id, old_video in stored.items():
                    if old_video["type"] == "bluray" and old_video["path"] == str(path):
                        new_videos[video_id] = old_video
                continue

            disc_name = Path(path).name
            multiple = len(titles) > 1
            for title in titles:
                video_id = store.make_video_id("bluray", path, title["title_idx"])
                display_name = (
                    f"{disc_name} - Title {title['title_idx'] + 1}"
                    if multiple else disc_name
                )
                old_video = old_videos.get(video_id)
                if locked(old_video):
                    new_videos[video_id] = old_video
                    continue
                kept = _keep_own_chapters(old_video, title["duration"])
                chapters = kept or _merge_chapters(
                    title["chapters"], (old_video or {}).get("chapters")
                )
                new_videos[video_id] = {
                    "type": "bluray",
                    "path": str(path),
                    "title_idx": title["title_idx"],
                    "playlist": title["playlist"],
                    "display_name": display_name,
                    "duration": title["duration"],
                    "chapters": chapters,
                    MEDIA_TYPE: MEDIA_BLURAY,
                }
                if kept:
                    new_videos[video_id]["chapter_origin"] = old_video["chapter_origin"]
                _carry_over(new_videos[video_id], old_video)

    walked = set(new_videos)
    # Videos that weren't found because their files aren't there are kept,
    # names and all, and show as missing: a share that is only partly
    # mounted, or a file moved out and back, must not cost its chapters.
    # Removing them is always a deliberate step (remove_videos). A locked
    # video is kept whatever became of it.
    for video_id, old_video in stored.items():
        if video_id not in new_videos and (
            locked(old_video) or not Path(old_video["path"]).exists()
        ):
            new_videos[video_id] = old_video

    moved = {} if fresh else _moved_within(
        {vid: stored[vid] for vid in stored
         if vid not in walked and not locked(stored[vid])
         and not Path(stored[vid]["path"]).exists()},
        {vid: new_videos[vid] for vid in walked - set(stored)},
    )
    for old_id, new_id in moved.items():
        _take_over(new_videos[new_id], stored[old_id])
        _move_cover(old_id, new_id)
        new_videos.pop(old_id, None)
    if moved:
        playlists.remap_ids(data, moved)
    data[MOVED_KEY] = len(moved)

    data["videos"] = new_videos
    data["settings"]["library_root"] = str(root_path)
    name_single_chapters(data)
    store.save_library(data)
    return data


# On a library as rescanned, never stored: how many videos it found moved
# or renamed within it and kept the names of.
MOVED_KEY = "_moved"


def _unique_pairs(gone: dict, found: dict, key) -> dict:
    """{gone id: found id} for each key exactly one of each has."""
    def by_key(videos):
        groups: dict = {}
        for video_id, video in videos.items():
            k = key(video)
            if k is not None:
                groups.setdefault(k, []).append(video_id)
        return {k: ids[0] for k, ids in groups.items() if len(ids) == 1}

    theirs = by_key(found)
    return {old: theirs[k] for k, old in by_key(gone).items() if k in theirs}


def _moved_within(gone: dict, found: dict) -> dict:
    """Which videos that went missing turned up under another path in the
    same scan - a folder renamed, files reorganised - as {old id: new id}.

    A file is the same file when its size and modification time are (a
    rename or move keeps both) or, failing that, its name and size are (a
    copy may not keep the time). A disc is the same disc when it has the
    same titles: their playlists and lengths. Only a match that is the one
    possible counts; anything less stays missing, names and all.
    """
    pairs: dict = {}
    files = {k: v for k, v in gone.items() if v.get("type") == "file"}
    new_files = {k: v for k, v in found.items() if v.get("type") == "file"}
    for key in (
        lambda v: tuple(v["fingerprint"]) if v.get("fingerprint") else None,
        lambda v: (Path(v["path"]).name, v["fingerprint"][0]) if v.get("fingerprint") else None,
    ):
        matched = _unique_pairs({k: v for k, v in files.items() if k not in pairs},
                                {k: v for k, v in new_files.items()
                                 if k not in pairs.values()}, key)
        pairs.update(matched)

    def discs(videos):
        by_path: dict = {}
        for video_id, video in videos.items():
            if video.get("type") == "bluray":
                by_path.setdefault(video["path"], {})[video_id] = video
        return by_path

    def disc_key(titles):
        return frozenset((v.get("title_idx"), v.get("playlist"), round(v.get("duration") or 0))
                         for v in titles.values())

    old_discs, new_discs = discs(gone), discs(found)
    disc_pairs = _unique_pairs({p: {"titles": t} for p, t in old_discs.items()},
                               {p: {"titles": t} for p, t in new_discs.items()},
                               lambda d: disc_key(d["titles"]))
    for old_path, new_path in disc_pairs.items():
        by_title = {v.get("title_idx"): k for k, v in new_discs[new_path].items()}
        for old_id, video in old_discs[old_path].items():
            if video.get("title_idx") in by_title:
                pairs[old_id] = by_title[video.get("title_idx")]
    return pairs


def _take_over(new_video: dict, old_video: dict) -> None:
    """A video found under a new path takes on all that was known of it
    under its old one, as a rescan would have kept it there."""
    kept = _keep_own_chapters(old_video, new_video["duration"])
    if kept:
        new_video["chapters"] = copy.deepcopy(kept)
        new_video["chapter_origin"] = old_video["chapter_origin"]
    else:
        _merge_chapters(new_video["chapters"], old_video.get("chapters"))
    _carry_over(new_video, old_video)


def _learn_media_type(video, cancel=None) -> None:
    """Read a file once to find whether it came with chapters: for a video
    stored before that was recorded, whose chapters are now the app's."""
    try:
        info = ffprobe_chapters.read_regular_file_info(video["path"], cancel=cancel)
    except ffprobe_chapters.Cancelled:
        raise ScanCancelled() from None
    except Exception:
        return
    video[MEDIA_TYPE] = media_type_of_file(info["chapters"])


def remove_videos(data, video_ids) -> int:
    """Forget stored videos - for ones that are missing and aren't coming
    back, since a rescan keeps them. Returns how many were removed.
    """
    removed = 0
    for video_id in list(video_ids):
        if data["videos"].pop(video_id, None) is not None:
            removed += 1
    return removed


def missing_videos(data):
    """The ids of stored videos whose files are no longer where they were.

    Libraries store absolute paths, so moving or unmounting media leaves
    entries that look fine until something tries to play them. Naming them
    is better than a playback failure with no explanation.
    """
    # Each folder is listed once rather than each video looked for: over a
    # network share every look is a round trip, and a library's videos sit
    # a few dozen to a folder.
    listings: dict[str, set | None] = {}

    def present(path: str) -> bool:
        folder, name = os.path.split(path)
        if folder not in listings:
            try:
                listings[folder] = set(os.listdir(folder))
            except OSError:
                listings[folder] = None
        names = listings[folder]
        return names is not None and name in names

    return [video_id for video_id, video in data["videos"].items()
            if not present(str(video["path"]))]


def original_chapters(video, min_bluray_seconds):
    """Read a video's own chapters from its file or disc again, unnamed.

    What "reset" goes back to after chapters were generated or edited.
    """
    if video["type"] == "file":
        chapters = ffprobe_chapters.read_regular_file_info(video["path"])["chapters"]
    else:
        titles = bluray.probe_bluray_disc(video["path"], min_bluray_seconds)
        matching = [t for t in titles if t["title_idx"] == video.get("title_idx")]
        if not matching:
            raise LookupError("that title is no longer on the disc")
        chapters = matching[0]["chapters"]
    return _merge_chapters(chapters, None)


def moved_path(path: str, old_root: str, new_root: str) -> str:
    """Where `path`, under `old_root`, is under `new_root` instead."""
    old, new = Path(old_root), Path(new_root)
    candidate = Path(path)
    if candidate == old or candidate.is_relative_to(old):
        return str(new / candidate.relative_to(old))
    return path


def _move_cover(old_id: str, new_id: str) -> None:
    if new_id == old_id:
        return
    for name in (f"{old_id}.jpg", f"{old_id}{artwork.MISS_SUFFIX}"):
        source = config.own(config.ARTWORK_DIR / name)
        if source.exists():
            source.replace(config.own(config.ARTWORK_DIR / name.replace(old_id, new_id, 1)))


def _move_videos(videos: dict, old_root, new_root, move_artwork: bool) -> tuple[dict, dict]:
    """Videos moved from under `old_root` to under `new_root`, by their new
    ids, and {old id: new id}."""
    moved, new_ids = {}, {}
    for old_id, video in videos.items():
        video = dict(video, path=moved_path(video["path"], old_root, new_root))
        new_id = store.make_video_id(video["type"], video["path"], video.get("title_idx"))
        moved[new_id] = video
        new_ids[old_id] = new_id
        if move_artwork:
            _move_cover(old_id, new_id)
    return moved, new_ids


def _move_meta(meta: dict, old_root, new_root, new_ids: dict) -> dict:
    """A library's settings and playlists moved with its videos."""
    settings = dict(meta["settings"],
                    library_root=moved_path(meta["settings"]["library_root"], old_root, new_root))
    if "hidden_folders" in settings:
        settings["hidden_folders"] = [
            moved_path(folder, old_root, new_root) for folder in settings["hidden_folders"]
        ]
    moved = dict(meta, settings=settings)
    if meta.get(playlists.KEY):
        moved[playlists.KEY] = copy.deepcopy(meta[playlists.KEY])
        playlists.remap_ids(moved, new_ids)
    return moved


def relocate_data(data: dict, new_root, move_artwork: bool = True) -> dict:
    """A library's data moved onto `new_root`: each video's path and id, the
    hidden folders, the playlists' entries, and - with `move_artwork` - the
    covers cached under the old ids. The data given is left as it was."""
    old_root = data["settings"]["library_root"]
    new_root = str(Path(new_root).resolve())
    videos, new_ids = _move_videos(data["videos"], old_root, new_root, move_artwork)
    moved = _move_meta({k: v for k, v in data.items() if k != "videos"},
                       old_root, new_root, new_ids)
    moved["videos"] = videos
    # Not the library as loaded any more: every video is written.
    moved.pop(store.LOADED_KEY, None)
    return moved


# How many of a library's videos are looked for to tell whether a folder is
# where they went, and what share must be there. A few, not all: over a
# network share each look is a round trip.
SAMPLE = 40
FOUND_FRACTION = 0.6


def _sample(videos: list[dict]) -> list[dict]:
    """Up to SAMPLE videos, spread across all of them."""
    videos = sorted(videos, key=lambda v: v["path"])
    if len(videos) <= SAMPLE:
        return videos
    step = len(videos) / SAMPLE
    return [videos[int(i * step)] for i in range(SAMPLE)]


def located(videos, old_root, new_root) -> tuple[int, int]:
    """(found, looked): how many of a sample of these videos, under
    `old_root`, are at the same place under `new_root`."""
    sample = _sample(list(videos))
    found = sum(1 for video in sample
                if Path(moved_path(video["path"], old_root, new_root)).exists())
    return found, len(sample)


def mostly_there(videos, old_root, new_root) -> bool:
    found, looked = located(videos, old_root, new_root)
    return looked > 0 and found / looked >= FOUND_FRACTION


def relocation(old_root, new_root) -> tuple[str, str]:
    """What moving a library to `new_root` moves: (from, to).

    Usually just the library (and any inside it). But a library inside
    another that turns up at the same place within a new parent - the NAS
    has a new address, or is mounted somewhere new - means the whole share
    moved: the outermost library moves too, when its videos are there."""
    old_root, new_root = str(Path(old_root).resolve()), str(Path(new_root).resolve())
    outer = store.outermost_library(old_root)
    if not outer or outer == old_root:
        return old_root, new_root
    tail = Path(old_root).relative_to(outer)
    if Path(new_root).parts[-len(tail.parts):] != tail.parts:
        return old_root, new_root
    new_outer = str(Path(*Path(new_root).parts[:-len(tail.parts)]))
    if new_outer == outer:
        return old_root, new_root
    # Only the videos outside the library being located tell: its own are
    # at the new place whether the share moved or just it did.
    outer_videos = [video for video in store.load_library_for_root(outer)["videos"].values()
                    if not Path(video["path"]).is_relative_to(old_root)]
    if not mostly_there(outer_videos, outer, new_outer):
        return old_root, new_root
    return outer, new_outer


def relocate(old_root, new_root) -> dict:
    """Move a stored library to a new root folder: the same videos, found
    somewhere else. Returns the library as stored at `new_root`.

    kio-fuse presents a share under a folder whose name changes from one
    session to the next, so a KDE library on a network share turns up at a
    new path after every login; a NAS with a new address, a renamed
    folder, or a move to another NAS does the same. Everything keyed by
    path moves with it - each video's path and id, its cached cover, the
    hidden folders, the playlists - and any library inside it moves too
    (see relocation for when the library around it does).
    """
    new_root = str(Path(new_root).resolve())
    source, target = relocation(old_root, new_root)

    def move(bundle):
        videos, new_ids = _move_videos(bundle["videos"], source, target, move_artwork=True)
        libraries = {}
        for meta in bundle["libraries"].values():
            meta = _move_meta(meta, source, target, new_ids)
            libraries[meta["settings"]["library_root"]] = meta
        return {"root": moved_path(bundle["root"], source, target),
                "libraries": libraries, "videos": videos}

    if source != target:
        store.move_libraries(source, move)
    return store.load_library_for_root(new_root)


def delete_library_data(root) -> list[str]:
    """Delete everything recorded about one library, for good (see
    store.erase_library) - with the covers cached for its videos and what
    has been measured of them. Returns the ids of the videos it held. The
    folder and its videos are never touched; adding it again starts it from
    scratch."""
    from . import audio_levels, frames, stage_light

    erased = store.erase_library(root)
    for video_id in erased:
        artwork.forget(video_id)
    for module in (audio_levels, frames, stage_light):
        module.clear_cache()
    return erased


def delete_all_data() -> None:
    """Delete everything stored about every library, for good: each
    library's file with its titles, chapters, names, hidden folders and
    playlists, every backup of them, the cached covers, and what has been
    measured of the videos. Nothing can be restored; adding a folder again
    starts it from scratch. App settings (privacy, the AI) are kept.
    """
    from . import audio_levels, frames, stage_light

    store.delete_everything()
    artwork.forget_all()
    for module in (audio_levels, frames, stage_light):
        module.clear_cache()


def network_uri_for(root) -> str | None:
    """The network address a stored library was added from, if any."""
    return store.load_library_for_root(root)["settings"].get(NETWORK_URI_SETTING)


def reconnect(root, resolver) -> str:
    """Make sure a library's folder is there, reconnecting its network share
    if it was added from one. Returns the folder to scan, which for KDE can
    be somewhere new (the library is moved there first).

    `resolver` turns a network address into a local folder, raising if it
    can't (network.resolve). A library not on a network share, or whose
    folder is already there, is returned untouched.
    """
    root = str(Path(root).resolve())
    if Path(root).is_dir():
        return root
    uri = network_uri_for(root)
    if not uri:
        return root
    try:
        new_root = str(Path(resolver(uri)).resolve())
    except Exception as exc:  # noqa: BLE001 - whatever the bridge says, said plainly
        raise LibraryUnavailable(
            f"Couldn't reconnect to {uri}: {exc}\n\n{MOVED_HINT}"
        ) from exc
    if new_root != root:
        relocate(root, new_root)
    return new_root
