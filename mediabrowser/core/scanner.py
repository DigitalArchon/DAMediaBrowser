# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

import os
from pathlib import Path

from . import config, dvd


def is_bluray_root(path: Path) -> bool:
    bdmv = path / "BDMV"
    return (
        (bdmv / "index.bdmv").is_file()
        and (bdmv / "PLAYLIST").is_dir()
        and (bdmv / "STREAM").is_dir()
    )


class ScanCancelled(Exception):
    pass


def find_media_units(root: Path, cancel=None, on_folder=None):
    """Walk root recursively. Yields ("bluray", disc_root_path), ("dvd",
    disc_root_path) or ("file", file_path).

    Disc folders are not descended into further once detected: a DVD's VOBs
    are its titles' pieces, not videos of their own.

    `cancel` (a threading.Event) is checked in every folder, not just between
    videos: a tree the size of a whole drive can hold thousands of folders
    with nothing in them, and that walk is exactly what someone who picked
    the wrong folder wants to stop. `on_folder` is told each folder as it is
    entered, so progress shows something moving in the meantime.
    """
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        if cancel is not None and cancel.is_set():
            raise ScanCancelled()
        dirpath = Path(dirpath)
        if on_folder is not None:
            on_folder(dirpath)
        # A stable order makes a scan's progress (and its results) the same
        # from one run to the next.
        dirnames.sort()

        if is_bluray_root(dirpath):
            yield ("bluray", dirpath)
            dirnames[:] = []
            continue
        if dvd.is_dvd_root(dirpath):
            yield ("dvd", dirpath)
            dirnames[:] = []
            continue

        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext in config.MEDIA_EXTENSIONS:
                yield ("file", dirpath / fname)
