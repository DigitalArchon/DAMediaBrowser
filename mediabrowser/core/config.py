# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

import os
from pathlib import Path

APP_NAME = "da-media-browser"


def _data_home() -> Path:
    """$XDG_DATA_HOME when it is set to an absolute path, else the default.

    The spec says a relative value is invalid and must be ignored, which is
    why this does not simply take whatever is in the variable.
    """
    value = os.environ.get("XDG_DATA_HOME", "")
    path = Path(value) if value else None
    if path is not None and path.is_absolute():
        return path
    return Path.home() / ".local" / "share"


DATA_DIR = _data_home() / APP_NAME
LIBRARIES_DIR = DATA_DIR / "libraries"
ARTWORK_DIR = DATA_DIR / "artwork"

# Pre-multi-library layout: a single library.json holding one folder's data.
# Still checked at startup so existing data gets migrated, not lost.
LEGACY_LIBRARY_FILE = DATA_DIR / "library.json"

APP_SETTINGS_FILE = DATA_DIR / "settings.json"

# Blu-ray titles shorter than this aren't listed: below it are the menu
# backgrounds, logos and warnings; above it the extras - a trailer, a
# behind-the-scenes film - which are worth having, named and hideable.
DEFAULT_MIN_BLURAY_TITLE_SECONDS = 20
# What that was before the extras were wanted. A library still on it (it
# was never a setting anyone could change) moves to the new one.
LEGACY_MIN_BLURAY_TITLE_SECONDS = 120

MEDIA_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".avi", ".mov", ".wmv",
    ".flv", ".webm", ".ts", ".mpg", ".mpeg", ".m2ts", ".vob",
}



def _version() -> str:
    """This app's version: pyproject.toml's, in a checkout (where installed
    metadata can be left behind by a bump), else the installed package's."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            if line.startswith("version = "):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    try:
        from importlib.metadata import version

        return version("mediabrowser")
    except Exception:
        return "unknown"


VERSION = _version()
COPYRIGHT = "2026 Digital Archon"

MUSICBRAINZ_BASE_URL = "https://musicbrainz.org/ws/2"
PROJECT_URL = "https://github.com/DigitalArchon/DAMediaBrowser"
# MusicBrainz takes a web page as the contact as readily as an address.
MUSICBRAINZ_CONTACT = PROJECT_URL
# MusicBrainz asks every application to say what it is, which version, and
# how its maintainers can be reached; it blocks what it can't identify.
MUSICBRAINZ_USER_AGENT = f"DAMediaBrowser/{VERSION} ( {MUSICBRAINZ_CONTACT} )"

MPV_BINARY = "mpv"


# Covers are cached at this size: enough for the grid on a high-DPI screen,
# small enough that a large library's cache stays trivial.
ARTWORK_SIZE = 512

COVER_FILENAMES = (
    "cover.jpg", "cover.jpeg", "cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "front.jpg", "front.png", "poster.jpg",
)

COVER_ART_ARCHIVE_URL = "https://coverartarchive.org/release/{mbid}/front-500"


class NotOwnFile(Exception):
    """A file the app was about to write, rename or delete isn't one of its
    own - so it's left alone, whatever asked."""


def own(path) -> Path:
    """`path`, once it's certain to be one of this app's own files: in its
    data folder (the libraries' records, their backups, the cover cache,
    settings). Everything that writes, renames or deletes a file there goes
    through this first, so no bug can reach a video, a disc or anything else
    in a library - it raises NotOwnFile instead.

    The folder a file is in is resolved, so a link can't lead out of the
    data folder; the file itself isn't, so deleting a link deletes the link.
    """
    target = Path(path)
    where = target.parent.resolve() / target.name
    for folder in (DATA_DIR, LIBRARIES_DIR, ARTWORK_DIR, APP_SETTINGS_FILE.parent):
        folder = Path(folder).resolve()
        if where == folder or where.is_relative_to(folder):
            return target
    raise NotOwnFile(f"Refused to change {path}: it isn't one of this app's own files.")


def ensure_data_dir() -> None:
    LIBRARIES_DIR.mkdir(parents=True, exist_ok=True)


def ensure_artwork_dir() -> None:
    ARTWORK_DIR.mkdir(parents=True, exist_ok=True)
