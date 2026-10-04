# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

import copy
import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

from . import config, naming

DEFAULT_APP_SETTINGS = {
    "musicbrainz_format": "xml",
    # The Nano-GPT model and endpoint: see core.ai. The key is in the keyring.
    "ai_model": "anthropic/claude-sonnet-5",
    "ai_base_url": "https://nano-gpt.com/api/v1",
    "ai_frames_per_chapter": 2,
    "ai_search": "kagi",
}


def load_app_settings() -> dict:
    config.ensure_data_dir()
    if not config.APP_SETTINGS_FILE.exists():
        return dict(DEFAULT_APP_SETTINGS)
    try:
        with open(config.APP_SETTINGS_FILE, encoding="utf-8") as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_APP_SETTINGS)
    merged = dict(DEFAULT_APP_SETTINGS)
    merged.update(settings)
    return merged


def save_app_settings(settings: dict) -> None:
    config.ensure_data_dir()
    tmp_path = config.own(config.APP_SETTINGS_FILE.with_suffix(".json.tmp"))
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, sort_keys=True)
    tmp_path.replace(config.own(config.APP_SETTINGS_FILE))


def make_video_id(kind: str, path, title_idx=None) -> str:
    h = hashlib.sha1()
    h.update(kind.encode("utf-8"))
    h.update(b"|")
    h.update(str(Path(path).resolve()).encode("utf-8"))
    if title_idx is not None:
        h.update(b"|")
        h.update(str(title_idx).encode("utf-8"))
    return h.hexdigest()[:16]


def default_library(root=None) -> dict:
    return {
        "settings": {
            "library_root": str(Path(root).resolve()) if root else None,
            "min_bluray_title_seconds": config.DEFAULT_MIN_BLURAY_TITLE_SECONDS,
        },
        "videos": {},
    }


def _normalize(data: dict) -> dict:
    data.setdefault("settings", {})
    data["settings"].setdefault("library_root", None)
    data["settings"].setdefault(
        "min_bluray_title_seconds", config.DEFAULT_MIN_BLURAY_TITLE_SECONDS
    )
    if data["settings"]["min_bluray_title_seconds"] == config.LEGACY_MIN_BLURAY_TITLE_SECONDS:
        data["settings"]["min_bluray_title_seconds"] = config.DEFAULT_MIN_BLURAY_TITLE_SECONDS
    data.setdefault("videos", {})
    return data


def _file_for_root(root) -> Path:
    digest = hashlib.sha1(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]
    return config.LIBRARIES_DIR / f"{digest}.json"


def _read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def migrate_legacy_library() -> None:
    """One-time move of the old single-file layout into the new per-folder
    layout, so upgrading the app doesn't lose anyone's existing data.
    """
    if not config.LEGACY_LIBRARY_FILE.exists():
        return
    data = _read_json(config.LEGACY_LIBRARY_FILE)
    root = (data or {}).get("settings", {}).get("library_root")
    if not root:
        config.own(config.LEGACY_LIBRARY_FILE).unlink(missing_ok=True)
        return

    config.ensure_data_dir()
    target = _file_for_root(root)
    if not target.exists():
        config.own(config.LEGACY_LIBRARY_FILE).replace(config.own(target))
    else:
        config.own(config.LEGACY_LIBRARY_FILE).unlink(missing_ok=True)


# --- libraries -----------------------------------------------------------------
#
# A video's titles and chapters belong to the video, not to whichever library
# it was named in. So libraries that overlap - MusicVids, and BABYMETAL inside
# it - share one file: the outermost folder's, holding every video under it,
# with each nested library's own settings (hidden folders, playlists, locked
# and private) kept beside its own in NESTED_KEY. A file is a "tree".
#
# A library is loaded as a view of its tree: its own settings, and the
# videos under its folder. Adding a folder that holds libraries already
# folds their trees into its own; adding one inside a library shows that
# library's videos from it, named already; forgetting a nested library
# leaves its videos to the library around it, so adding it again finds
# them all there.
#
# Two views of the same videos can be open at once (both libraries on the
# shelf). Saving writes only what the saved view changed since it was
# loaded, so an older copy in the other view can't undo it.

NESTED_KEY = "nested_libraries"
BACKUP_DIR = "backups"
# On a library as loaded, never stored: each video as it was loaded (the
# cache's own copy, which is never changed in place), so that saving writes
# only the videos this copy changed.
LOADED_KEY = "_loaded"

_lock = threading.RLock()
# Library file path -> ((mtime, size), tree): reread only when it changes.
_cache: dict[str, tuple[tuple, dict | None]] = {}
# Bumped whenever a tree is written or reread, for flagged_roots().
_generation = 0
_flagged: tuple = (None, -1, [])


def _norm(root) -> str:
    return str(Path(root).resolve())


def _under(path, root) -> bool:
    """Whether `path` is `root` or somewhere inside it. Both are absolute
    and resolved already, so it's a matter of the strings - called for
    every video of a library whenever libraries are listed or loaded."""
    if not path or not root:
        return False
    path, root = str(path), str(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


is_under = _under


def _inside(root, outer) -> bool:
    """Whether `root` is strictly inside `outer`."""
    return root != outer and _under(root, outer)


def further_along(a: dict, b: dict) -> bool:
    """Whether video `a` is more identified than `b`."""
    sa, sb = naming.status(a), naming.status(b)
    return (sa.named, sa.fraction) > (sb.named, sb.fraction)


def _meta(data: dict) -> dict:
    """A library as stored beside its videos: everything but them, and
    nothing that only belongs to it as loaded."""
    return {key: copy.deepcopy(value) for key, value in data.items()
            if key != "videos" and not key.startswith("_")}


def _tree_from_raw(raw, path) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("settings"), dict):
        return None
    root = raw["settings"].get("library_root")
    if not root:
        return None
    libraries = {root: {k: v for k, v in raw.items() if k not in ("videos", NESTED_KEY)}}
    for nested_root, meta in (raw.get(NESTED_KEY) or {}).items():
        if isinstance(meta, dict) and isinstance(meta.get("settings"), dict):
            meta["settings"]["library_root"] = nested_root
            libraries[nested_root] = meta
    videos = raw.get("videos") if isinstance(raw.get("videos"), dict) else {}
    return {"root": root, "libraries": libraries, "videos": videos, "path": path}


def _trees() -> list[dict]:
    """Every stored tree, from the cache when its file hasn't changed. The
    trees are the cache's own: callers under the lock change them only
    to write them."""
    global _generation
    config.ensure_data_dir()
    trees, seen = [], set()
    for path in sorted(config.LIBRARIES_DIR.glob("*.json")):
        key = str(path)
        seen.add(key)
        try:
            stat = path.stat()
        except OSError:
            continue
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = _cache.get(key)
        if cached is None or cached[0] != stamp:
            _cache[key] = (stamp, _tree_from_raw(_read_json(path), path))
            _generation += 1
        if _cache[key][1] is not None:
            trees.append(_cache[key][1])
    folder = str(config.LIBRARIES_DIR)
    for key in [k for k in _cache if k not in seen and str(Path(k).parent) == folder]:
        del _cache[key]
        _generation += 1
    return trees


def _holder(trees, root) -> dict | None:
    """The tree `root` is in: the one whose folder it is, or is inside."""
    for tree in trees:
        if _under(root, tree["root"]):
            return tree
    return None


def _trees_inside(trees, root) -> list[dict]:
    return [tree for tree in trees if _inside(tree["root"], root)]


def _videos_of(tree, root) -> dict:
    """A tree's videos that are in `root`'s library: all of them for the
    tree's own, those under its folder for a nested one."""
    if root == tree["root"]:
        return tree["videos"]
    return {video_id: video for video_id, video in tree["videos"].items()
            if _under(video.get("path"), root)}


def _write(tree: dict) -> None:
    global _generation
    raw = dict(tree["libraries"][tree["root"]])
    raw["videos"] = tree["videos"]
    nested = {root: meta for root, meta in tree["libraries"].items() if root != tree["root"]}
    if nested:
        raw[NESTED_KEY] = nested
    config.ensure_data_dir()
    path = config.own(_file_for_root(tree["root"]))
    tmp_path = config.own(path.with_suffix(".json.tmp"))
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2, sort_keys=True)
        tmp_path.replace(path)
    except BaseException:
        # The cached tree may have been changed for this write: reread it.
        if tree.get("path") is not None:
            _cache.pop(str(tree["path"]), None)
        raise
    tree["path"] = path
    stat = path.stat()
    _cache[str(path)] = ((stat.st_mtime_ns, stat.st_size), tree)
    _generation += 1


def backup_dir() -> Path:
    return config.LIBRARIES_DIR / BACKUP_DIR


# Why a backup was made, as its file name says it, and in words.
BACKUP_REASONS = {
    "forgotten": "before a library in it was forgotten",
    "merged": "before it was folded into a library around it",
    "moved": "before it was moved",
    "restored": "before a backup was restored into it",
}


def _backup(tree: dict, reason: str) -> None:
    """A copy of a tree's file as it is, before it is folded into another,
    moved, restored into or has a library forgotten: nothing is ever only
    deleted (but by delete_everything, which means it)."""
    path = tree.get("path")
    if path is None or not Path(path).exists():
        return
    backup_dir().mkdir(parents=True, exist_ok=True)
    now = time.time_ns()
    seconds, micro = divmod(now // 1000, 1_000_000)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(seconds)) + f"-{micro:06d}"
    shutil.copy2(path, config.own(backup_dir() / f"{Path(path).stem}-{stamp}-{reason}.json"))


def list_backups() -> list[dict]:
    """Every backup, newest first: {"path", "when" (seconds since the
    epoch), "reason" (in words), "root", "libraries" (roots), "videos"}."""
    with _lock:
        folder = backup_dir()
        found = []
        for path in folder.glob("*.json") if folder.is_dir() else []:
            tree = _tree_from_raw(_read_json(path), path)
            if tree is None:
                continue
            parts = path.stem.split("-")
            reason = parts[-1] if parts[-1] in BACKUP_REASONS else ""
            try:
                when = time.mktime(time.strptime(f"{parts[-4]}-{parts[-3]}", "%Y%m%d-%H%M%S"))
                when += int(parts[-2]) / 1_000_000
            except (IndexError, ValueError):
                when = path.stat().st_mtime
            found.append({
                "path": path, "when": when, "reason": BACKUP_REASONS.get(reason, ""),
                "root": tree["root"], "libraries": sorted(tree["libraries"]),
                "videos": tree["videos"],
            })
    found.sort(key=lambda backup: backup["when"], reverse=True)
    return found


def restore_backup(path) -> list[str]:
    """Bring back the libraries and videos in a backup; returns the
    libraries it held. Nothing here now is lost: a library still here keeps
    its settings, a video still here keeps whichever copy is further along,
    and the library file it goes into is backed up first."""
    with _lock:
        incoming = _tree_from_raw(_read_json(path), None)
        if incoming is None:
            raise ValueError(f"{Path(path).name} isn't a library backup")
        root = _norm(incoming["root"])
        trees = _trees()
        holder = _holder(trees, root)
        absorbed = []
        if holder is None:
            absorbed = _trees_inside(trees, root)
            holder = {"root": root, "libraries": {}, "videos": {}, "path": None}
            _absorb(holder, absorbed)
        else:
            _backup(holder, "restored")
        for library_root, meta in incoming["libraries"].items():
            holder["libraries"].setdefault(library_root, meta)
        _absorb(holder, [{"libraries": {}, "videos": incoming["videos"]}], backup=False)
        _write(holder)
        for old in absorbed:
            _remove(old)
        return sorted(incoming["libraries"])


def delete_backup(path) -> None:
    """Delete one backup, for good."""
    path = Path(path)
    if path.parent.resolve() != backup_dir().resolve() or path.suffix != ".json":
        raise ValueError(f"{path} isn't a library backup")
    config.own(path).unlink(missing_ok=True)


def erase_plan(root) -> dict:
    """What erasing a library deletes: {"videos": every video under its
    folder, "libraries": it and those inside it, "shared_with": the library
    around it, whose copies go too, "backups": how many backups hold some
    of it}."""
    with _lock:
        root = _norm(root)
        tree = _holder(_trees(), root)
        if tree is None:
            return {"videos": {}, "libraries": [], "shared_with": None, "backups": 0}
        around = [r for r in tree["libraries"] if _inside(root, r)]
        backups = sum(1 for backup in list_backups()
                      if any(_under(r, root) for r in backup["libraries"])
                      or any(_under(v.get("path"), root) for v in backup["videos"].values()))
        return {
            "videos": copy.deepcopy(_videos_of(tree, root)),
            "libraries": sorted(r for r in tree["libraries"] if _under(r, root)),
            "shared_with": max(around, key=len) if around else None,
            "backups": backups,
        }


def _erase_from(tree: dict, root: str) -> bool:
    """Take everything at or under `root` out of a tree; whether it held
    anything there."""
    videos = _videos_of(tree, root)
    gone = [r for r in tree["libraries"] if _under(r, root)]
    for r in gone:
        del tree["libraries"][r]
    for video_id in list(videos):
        tree["videos"].pop(video_id, None)
    return bool(gone or videos)


def erase_library(root) -> list[str]:
    """Delete everything recorded about a library, for good: it, the
    libraries inside it, and every video under its folder - from a library
    around it too, which shares them, and from every backup, so nothing can
    bring them back. Returns the ids of the videos erased.

    Only this app's records go. The library's folder and everything in it
    are never touched (config.own refuses anything outside the data folder).
    """
    with _lock:
        root = _norm(root)
        tree = _holder(_trees(), root)
        erased = list(_videos_of(tree, root)) if tree is not None else []
        if tree is not None:
            _erase_from(tree, root)
            if tree["root"] in tree["libraries"]:
                _write(tree)
            else:
                _remove(tree)
        folder = backup_dir()
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            backup = _tree_from_raw(_read_json(path), path)
            if backup is None or not _erase_from(backup, root):
                continue
            if backup["root"] not in backup["libraries"]:
                config.own(path).unlink(missing_ok=True)
                continue
            raw = dict(backup["libraries"][backup["root"]], videos=backup["videos"])
            nested = {r: m for r, m in backup["libraries"].items() if r != backup["root"]}
            if nested:
                raw[NESTED_KEY] = nested
            tmp_path = config.own(path.with_suffix(".json.tmp"))
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2, sort_keys=True)
            tmp_path.replace(config.own(path))
        return erased


def delete_everything() -> None:
    """Delete every library's stored data, and every backup of it, for
    good: nothing is kept to restore. App settings stay."""
    global _generation
    with _lock:
        config.ensure_data_dir()
        for path in config.LIBRARIES_DIR.glob("*.json*"):
            config.own(path).unlink(missing_ok=True)
        folder = backup_dir()
        if folder.is_dir():
            for path in folder.glob("*.json*"):
                config.own(path).unlink(missing_ok=True)
        config.own(config.LEGACY_LIBRARY_FILE).unlink(missing_ok=True)
        _cache.clear()
        _generation += 1


def _remove(tree: dict) -> None:
    global _generation
    path = tree.get("path")
    if path is None:
        return
    config.own(path).unlink(missing_ok=True)
    _cache.pop(str(path), None)
    _generation += 1


def _absorb(into: dict, trees, prefer_incoming: bool = False, backup: bool = True) -> None:
    """Fold trees into `into`, which holds their folders. A video both
    have keeps the copy further along."""
    for tree in trees:
        if backup:
            _backup(tree, "merged")
        for root, meta in tree["libraries"].items():
            into["libraries"].setdefault(root, meta)
        for video_id, video in tree["videos"].items():
            mine = into["videos"].get(video_id)
            if mine is None or further_along(video, mine) or (
                prefer_incoming and not further_along(mine, video)
            ):
                into["videos"][video_id] = video


def load_library_for_root(root) -> dict:
    """The library for this folder: its settings, and the videos under it
    with all that's known of them - from the library around it if it is
    nested in one, or from the libraries inside it it if holds some. A
    fresh empty one if the app has never seen the folder."""
    with _lock:
        root = _norm(root)
        trees = _trees()
        holder = _holder(trees, root)
        if holder is not None:
            meta = holder["libraries"].get(root)
            videos = _videos_of(holder, root)
        else:
            meta, videos = None, {}
            for tree in _trees_inside(trees, root):
                for video_id, video in tree["videos"].items():
                    videos.setdefault(video_id, video)
        data = copy.deepcopy({**(meta or default_library(root)), "videos": videos})
    data = _normalize(data)
    data["settings"]["library_root"] = root
    data[LOADED_KEY] = dict(videos)
    return data


def has_library(root) -> bool:
    """Whether this folder is a library of its own (not only inside one)."""
    with _lock:
        root = _norm(root)
        return any(root in tree["libraries"] for tree in _trees())


def save_library(data: dict) -> None:
    """Store a library: its settings, and each of its videos it changed
    since it was loaded - so another library's newer copy of a video the
    two share is never overwritten with this one's older copy."""
    with _lock:
        root = _norm(data["settings"]["library_root"])
        trees = _trees()
        tree = _holder(trees, root)
        absorbed = []
        if tree is None:
            # A new outermost folder: any libraries inside it join it.
            absorbed = _trees_inside(trees, root)
            tree = {"root": root, "libraries": {}, "videos": {}, "path": None}
            _absorb(tree, absorbed)
        tree["libraries"][root] = _meta(data)
        tree["libraries"][root]["settings"]["library_root"] = root
        loaded = data.get(LOADED_KEY) or {}
        videos = data["videos"]
        now = {}
        for video_id, video in videos.items():
            before = loaded.get(video_id)
            if before is None or before != video:
                tree["videos"][video_id] = now[video_id] = copy.deepcopy(video)
            else:
                now[video_id] = before
        for video_id in loaded:
            if video_id not in videos:
                tree["videos"].pop(video_id, None)
        _write(tree)
        for old in absorbed:
            _remove(old)
    data[LOADED_KEY] = now


def mark_saved(data: dict, video_id: str) -> None:
    """This library's copy of a video is the stored one (another library
    on the shelf saved it), so it isn't written again as a change."""
    if video_id in data["videos"]:
        data.setdefault(LOADED_KEY, {})[video_id] = copy.deepcopy(data["videos"][video_id])


def list_libraries():
    """Returns [{"root": str, "video_count": int, "network_uri": str | None,
    "locked": bool, "private": bool, "within": str | None}, ...] for every
    library, newest-scanned first. `within` is the library a nested one is
    inside, whose titles and chapters it shares.
    """
    with _lock:
        libraries = []
        for tree in _trees():
            try:
                mtime = Path(tree["path"]).stat().st_mtime
            except OSError:
                mtime = 0.0
            roots = list(tree["libraries"])
            for root, meta in tree["libraries"].items():
                settings = meta.get("settings") or {}
                around = [other for other in roots if _inside(root, other)]
                libraries.append({
                    "root": root,
                    "video_count": len(_videos_of(tree, root)),
                    "network_uri": settings.get("network_uri"),
                    "locked": bool(settings.get("locked")),
                    "private": bool(settings.get("private")),
                    "within": max(around, key=len) if around else None,
                    "mtime": mtime,
                })
    libraries.sort(key=lambda lib: (lib["mtime"], -len(lib["root"])), reverse=True)
    return libraries


def overlap(root) -> dict:
    """How a folder about to be added meets the libraries there are:
    {"is_library": bool, "within": the library it's inside or None,
    "contains": [the outermost libraries inside it]}."""
    with _lock:
        root = _norm(root)
        trees = _trees()
        holder = _holder(trees, root)
        within = None
        if holder is not None:
            around = [r for r in holder["libraries"] if _under(root, r) and r != root]
            within = max(around, key=len) if around else None
            contains = [r for r in holder["libraries"] if _inside(r, root)]
        else:
            contains = [r for tree in _trees_inside(trees, root) for r in tree["libraries"]]
        outermost = [r for r in contains if not any(_inside(r, o) for o in contains)]
        return {
            "is_library": holder is not None and root in holder["libraries"],
            "within": within,
            "contains": sorted(outermost),
        }


def forget_plan(root) -> dict:
    """What forgetting a library would do: {"videos": its videos,
    "lost": those no other library holds, which go with it, "kept_by":
    the libraries that keep the rest}."""
    with _lock:
        root = _norm(root)
        for tree in _trees():
            if root not in tree["libraries"]:
                continue
            videos = _videos_of(tree, root)
            others = [r for r in tree["libraries"] if r != root]
            if root != tree["root"]:
                around = [r for r in others if _inside(root, r)]
                return {"videos": copy.deepcopy(videos), "lost": {},
                        "kept_by": [max(around, key=len)] if around else others[:1]}
            outer = [r for r in others if not any(_inside(r, o) for o in others)]
            lost = {video_id: video for video_id, video in videos.items()
                    if not any(_under(video.get("path"), r) for r in outer)}
            return {"videos": copy.deepcopy(videos), "lost": copy.deepcopy(lost),
                    "kept_by": sorted(outer)}
        return {"videos": {}, "lost": {}, "kept_by": []}


def delete_library(root) -> None:
    """Forget a library: its settings, and the videos no other library
    holds. A nested one's videos stay with the library around it; the
    libraries inside a forgotten one stand on their own. Media files are
    never touched, and the file as it was is kept in backups/.
    """
    with _lock:
        root = _norm(root)
        for tree in _trees():
            if root not in tree["libraries"]:
                continue
            _backup(tree, "forgotten")
            del tree["libraries"][root]
            if root != tree["root"]:
                _write(tree)
                return
            remaining = tree["libraries"]
            outer = [r for r in remaining if not any(_inside(r, o) for o in remaining)]
            for new_root in outer:
                _write({
                    "root": new_root,
                    "libraries": {r: m for r, m in remaining.items() if _under(r, new_root)},
                    "videos": _videos_of({"root": None, "videos": tree["videos"]}, new_root),
                    "path": None,
                })
            _remove(tree)
            return


def move_libraries(old_root, transform, prefer_incoming: bool = True) -> str:
    """Move the library at `old_root` - with the libraries inside it and
    all their videos - to wherever `transform` puts them. `transform`
    takes and returns {"root", "libraries", "videos"} (a copy: it may
    change it as it likes). Returns the new root. The old file is kept in
    backups/.

    Where the new folder is already known - inside another library, or
    holding some - the moved libraries join it, a video both have keeping
    the moved copy unless the other is further along.
    """
    with _lock:
        old_root = _norm(old_root)
        trees = _trees()
        tree = _holder(trees, old_root)
        if tree is None or old_root not in tree["libraries"]:
            raise KeyError(old_root)
        _backup(tree, "moved")
        whole = old_root == tree["root"]
        bundle = copy.deepcopy({
            "root": old_root,
            "libraries": {r: m for r, m in tree["libraries"].items() if _under(r, old_root)},
            "videos": _videos_of(tree, old_root),
        })
        moved = transform(bundle)
        new_root = _norm(moved["root"])
        if whole:
            _remove(tree)
        else:
            for r in bundle["libraries"]:
                del tree["libraries"][r]
            for video_id in bundle["videos"]:
                tree["videos"].pop(video_id, None)
            _write(tree)

        trees = _trees()
        holder = _holder(trees, new_root)
        absorbed = []
        if holder is None:
            absorbed = _trees_inside(trees, new_root)
            holder = {"root": new_root, "libraries": {}, "videos": {}, "path": None}
            _absorb(holder, absorbed)
        for r, meta in moved["libraries"].items():
            mine = holder["libraries"].get(r)
            holder["libraries"][r] = meta if mine is None else {**mine, **meta}
        _absorb(holder, [{"libraries": {}, "videos": moved["videos"]}], prefer_incoming,
                backup=False)
        _write(holder)
        for old in absorbed:
            _remove(old)
        return new_root


def outermost_library(root) -> str | None:
    """The outermost library of the tree a library is in."""
    with _lock:
        tree = _holder(_trees(), _norm(root))
        return tree["root"] if tree is not None else None


def merge_overlapping() -> int:
    """Fold libraries stored apart that overlap - from before they were
    kept together - into the outermost one's file; returns how many were
    folded in. A video both had keeps the copy further along."""
    with _lock:
        trees = sorted(_trees(), key=lambda tree: len(tree["root"]))
        gone, folded = set(), 0
        for outer in trees:
            if id(outer) in gone:
                continue
            inner = [tree for tree in trees
                     if id(tree) not in gone and _inside(tree["root"], outer["root"])]
            if not inner:
                continue
            _absorb(outer, inner)
            _write(outer)
            for tree in inner:
                _remove(tree)
                gone.add(id(tree))
            folded += len(inner)
        return folded


def flagged_roots() -> list[tuple[str, dict]]:
    """(root, settings) for every library set locked or private as a whole.
    Such a library covers its videos wherever they're seen from - in a
    library around it too (see protection)."""
    global _flagged
    with _lock:
        folder = str(config.LIBRARIES_DIR)
        if _flagged[0] != folder or _flagged[1] != _generation:
            if _flagged[0] != folder:
                _trees()
            flagged = [
                (root, meta["settings"]) for tree in _trees()
                for root, meta in tree["libraries"].items()
                if (meta.get("settings") or {}).get("locked")
                or (meta.get("settings") or {}).get("private")
            ]
            _flagged = (folder, _generation, flagged)
        return _flagged[2]
