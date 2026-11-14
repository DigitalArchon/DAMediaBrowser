# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""A file written whole, or not at all.

Every file that matters - settings.json, a library's record and its backups,
an exported catalog or chapter file, a song exported, a cover in the cache - is
written to a temp file of its own beside it, flushed to the disk, and swapped
in with os.replace, and then the folder is flushed too. A rename alone isn't
enough: on a file system that allocates late (ext4, XFS) a power cut soon after
can leave the renamed file empty, its bytes never written; and a temp file of a
fixed name (settings.json.tmp) is one two writers share, each truncating the
other's. The temp file's name is unique, made with O_CREAT|O_EXCL, and readable
by its owner alone (0600) while it is written.

The app's own files stay 0600. A file written for someone to take elsewhere -
an export - ends with the permissions any new file of theirs would have (the
umask's), as it had before. As SealedLore's storage/atomic.py.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# What every temp file here ends with, beside what the file's own name ends with
# (a cover's temp file must still end in .jpg for ffmpeg to know what to write).
PART = ".part"
# Readable by its owner alone: the app's own files.
OWN = 0o600
# The permissions a new file would have: an export, for someone to take elsewhere.
AS_A_NEW_FILE = None


def umask() -> int:
    """This process's umask, as the kernel reports it, without setting it (os.umask
    can only be read by changing it, which another thread would see); 0o022 where
    it can't be read."""
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    return 0o022


def sync_folder(folder) -> None:
    """A folder's entries - a rename into it - flushed to the disk; failures
    ignored (a file system that can't)."""
    try:
        fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def temp_beside(path) -> Path:
    """A new, empty file of a name of its own beside `path` (0600), ending as
    `path` does: for a program that writes the file itself (ffmpeg), to be put in
    place with replace()."""
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=PART + path.suffix,
                                dir=path.parent)
    os.close(fd)
    return Path(name)


def replace(temp, path, mode=OWN) -> None:
    """`temp`, written whole, flushed to the disk and put in `path`'s place, and the
    folder flushed; `mode` its permissions (AS_A_NEW_FILE: the umask's). `temp` is
    removed if any of it fails."""
    temp, path = Path(temp), Path(path)
    try:
        with open(temp, "rb+") as handle:
            os.fchmod(handle.fileno(), (0o666 & ~umask()) if mode is None else mode)
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    sync_folder(path.parent)


def write_bytes(path, data: bytes, mode=OWN) -> None:
    """`data` swapped in at `path` whole (see the module)."""
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=PART, dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), (0o666 & ~umask()) if mode is None else mode)
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    sync_folder(path.parent)


def write_text(path, text: str, mode=OWN) -> None:
    """`text`, as UTF-8 and with its line ends as they are, swapped in whole."""
    write_bytes(path, text.encode("utf-8"), mode)


def write_json(path, data, mode=OWN) -> None:
    """`data` as JSON, as the app writes its files (indented, keys sorted),
    swapped in whole."""
    write_text(path, json.dumps(data, indent=2, sort_keys=True), mode)
