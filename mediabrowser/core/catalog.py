# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Taking a catalog from one device to another.

A catalog - one library's stored chapters and names - is filed under the
path of its folder, and each video under its own path. The same NAS
mounted on another machine is somewhere else entirely:
/run/user/1000/gvfs/smb-share:server=nas,share=media/Concerts on one,
/mnt/nas/Concerts on the next. So a catalog copied across found nothing
of itself, and hours of naming stayed behind.

What doesn't change is where each video is *within* the folder. A catalog
matches a folder when most of its videos are there at the same place
relative to it, and is then moved onto it (library.relocate_data): every
path, id and hidden folder, and the covers cached under the old ids.

That happens by itself when a folder is added that has no catalog yet and
a stored catalog - its own folder absent on this device - matches it. And
a catalog can be exported to a file and imported on the other device,
merged with whatever that device has already scanned of the same folder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import library, network, playlists, protection, store

# How many of a catalog's videos are looked for, and what share of them
# must be found, for a folder to be its folder (see library.located).
SAMPLE = library.SAMPLE
MATCH_FRACTION = library.FOUND_FRACTION

FORMAT_KEY = "catalog_format"
FORMAT = 1


class CatalogError(Exception):
    """Why a catalog can't be used here, in words for the person."""


@dataclass(frozen=True)
class Match:
    old_root: str
    new_root: str
    found: int
    looked: int

    @property
    def fraction(self) -> float:
        return self.found / max(1, self.looked)


def match(data: dict, folder) -> Match | None:
    """Whether `folder` holds this catalog's videos where it had them,
    relative to its own folder; None if too few are there."""
    old_root = data["settings"].get("library_root")
    videos = list(data.get("videos", {}).values())
    if not old_root or not videos:
        return None
    new_root = str(Path(folder).resolve())
    found, looked = library.located(videos, old_root, new_root)
    result = Match(old_root, new_root, found, looked)
    return result if result.fraction >= MATCH_FRACTION else None


def _same_share(a: str | None, b: str | None) -> bool:
    return bool(a and b) and network.normalise(a).rstrip("/") == network.normalise(b).rstrip("/")


def find_moved(folder, network_uri: str | None = None) -> Match | None:
    """A stored catalog that is this folder's, from somewhere else: its own
    folder isn't on this device, and its videos are here. The best if
    several are; one added from the same network address wins a tie."""
    folder = str(Path(folder).resolve())
    best, best_key = None, None
    for known in store.list_libraries():
        root = known["root"]
        if root == folder or Path(root).is_dir():
            continue  # it's this folder already, or still in use where it is
        found = match(store.load_library_for_root(root), folder)
        if found is None:
            continue
        key = (found.fraction, _same_share(known.get("network_uri"), network_uri))
        if best_key is None or key > best_key:
            best, best_key = found, key
    return best


def adopt(found: Match) -> dict:
    """Move a stored catalog onto the folder it matched."""
    return library.relocate(found.old_root, found.new_root)


# --- files ------------------------------------------------------------------------


def refused_destination(path, library_roots) -> str | None:
    """Why a catalog mustn't be written to `path`, or None if it may. The
    app never writes into a library - a catalog saved there would be the
    first file it ever put among the videos - nor over anything that isn't
    a catalog already."""
    target = Path(path).expanduser().resolve()
    for root in library_roots:
        root = Path(root).resolve()
        if target == root or target.is_relative_to(root):
            return f"{target.parent} is inside the library {root}, which the app never writes to"
    if target.suffix.lower() != ".json":
        return "a catalog is saved as a .json file"
    if target.exists():
        try:
            read(target)
        except CatalogError:
            return f"{target.name} is there already and isn't a catalog"
    return None


def export(data: dict, path, library_roots=()) -> None:
    """Write a catalog to a file, for importing on another device - never
    into a library (its own or any in `library_roots`), nor over a file
    that isn't a catalog: CatalogError says why."""
    roots = [data["settings"].get("library_root"), *library_roots]
    refused = refused_destination(path, [root for root in roots if root])
    if refused:
        raise CatalogError(f"Not saved there: {refused}.")
    # A reset's undo and the last Identify run's journal belong to this
    # device's catalog, not a copy of it; a share's password to nobody's.
    settings = {key: value for key, value in data["settings"].items()
                if key not in ("reset_undo", "identify_run")}
    if settings.get(library.NETWORK_URI_SETTING):
        settings[library.NETWORK_URI_SETTING] = network.without_password(
            str(settings[library.NETWORK_URI_SETTING])
        )
    out = dict(data, settings=settings, **{FORMAT_KEY: FORMAT})
    tmp = Path(path).with_suffix(Path(path).suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=True)
    tmp.replace(path)


def read(path) -> dict:
    """A catalog from a file: one exported here, or a library file copied
    straight out of another device's data folder."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CatalogError(f"That file couldn't be read as a catalog ({exc}).") from exc
    if (not isinstance(data, dict) or not isinstance(data.get("videos"), dict)
            or not isinstance(data.get("settings"), dict)
            or not isinstance(data["settings"].get("library_root"), str)
            or not data["settings"]["library_root"]):
        raise CatalogError("That file isn't a DA Media Browser catalog.")
    for video_id, video in data["videos"].items():
        if (not isinstance(video_id, str) or not isinstance(video, dict)
                or not isinstance(video.get("path"), str)
                or not isinstance(video.get("chapters", []), list)):
            raise CatalogError(
                "That file isn't a DA Media Browser catalog: one of its videos isn't one."
            )
    data.pop(FORMAT_KEY, None)
    return data


_further_along = store.further_along


def merge(current: dict, incoming: dict) -> int:
    """Bring an imported catalog's work into this device's catalog of the
    same folder (both already rooted here); returns how many videos it
    changed. Each video keeps whichever copy is further along; what only
    the import knows - a name given by hand, a hidden flag, a MusicBrainz
    release - comes across either way."""
    changed = 0
    for video_id, theirs in incoming["videos"].items():
        mine = current["videos"].get(video_id)
        if mine is None:
            if Path(theirs["path"]).exists():
                current["videos"][video_id] = theirs
                changed += 1
            continue
        if protection.is_locked(current, mine):
            continue  # left exactly as this device has it
        if _further_along(theirs, mine):
            for key in ("chapters", "chapter_origin"):
                if key in theirs:
                    mine[key] = theirs[key]
                else:
                    mine.pop(key, None)
            changed += 1
        for key in ("custom_name", "hidden", "musicbrainz_release_id",
                    protection.LOCKED, protection.PRIVATE):
            if key in theirs and key not in mine:
                mine[key] = theirs[key]
                if key == "custom_name":
                    mine["display_name"] = theirs[key]
    hidden = current["settings"].setdefault("hidden_folders", [])
    for folder in incoming["settings"].get("hidden_folders", []):
        if folder not in hidden:
            hidden.append(folder)
    playlists.merge(current, incoming)
    return changed


def import_into(incoming: dict, folder) -> tuple[dict, int]:
    """Put an imported catalog onto `folder` of this device: moved onto it,
    and merged with this device's own catalog of it if there is one.
    Returns (the catalog as stored, videos brought in or updated)."""
    found = match(incoming, folder)
    if found is None:
        raise CatalogError(
            f"Too few of the catalog's videos are in {folder} - is it the same folder "
            "as the one the catalog was made from?"
        )
    moved = library.relocate_data(incoming, found.new_root, move_artwork=False)
    if store.has_library(found.new_root):
        current = store.load_library_for_root(found.new_root)
        changed = merge(current, moved)
    else:
        current, changed = moved, len(moved["videos"])
    store.save_library(current)
    return current, changed
