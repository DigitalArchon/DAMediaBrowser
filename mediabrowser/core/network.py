# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Network shares as ordinary folders.

Everything this app hands a path to - ffprobe, ffmpeg, mpv, libbluray -
needs a real file path, so a share only a file manager can see (smb://,
sftp://...) has to be presented as a folder first. The desktop does that
with a FUSE bridge:

- GVFS (Cinnamon, GNOME, MATE, Xfce) with gvfs-fuse puts every connected
  share under $XDG_RUNTIME_DIR/gvfs, and `gio` says where.
- KIO (KDE) with kio-fuse mounts a share on request, over D-Bus.

This module asks whichever is there, connecting the share first if it
isn't connected, and explains what is missing when neither can help. The
address is kept with the library, so a rescan after the share drops can
reconnect it rather than only report it gone.

Only the desktop's own command-line tools are used (gio, dbus-send), and
the runner is swappable so all of this can be tested without a network.
"""

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

NETWORK_SCHEMES = {
    "smb", "sftp", "ftp", "ftps", "dav", "davs", "webdav", "webdavs",
    "nfs", "afp", "fish",
}

# Connecting can mean a password prompt being answered from the keyring and
# a server spinning up disks; give it time, but not forever.
MOUNT_TIMEOUT_SECONDS = 45
QUERY_TIMEOUT_SECONDS = 15

INSTALL_HINT = (
    "Install your desktop's bridge and log out and back in:\n"
    "  • Cinnamon, GNOME, MATE, Xfce: gvfs-fuse "
    "(Mint/Ubuntu/Debian: sudo apt install gvfs-fuse; Fedora: sudo dnf install "
    "gvfs-fuse; on Arch it is part of gvfs)\n"
    "  • KDE: kio-fuse (Debian/Ubuntu: sudo apt install kio-fuse; Fedora: sudo dnf "
    "install kio-fuse; Arch: sudo pacman -S kio-fuse)\n"
    "Or mount the share yourself (in /etc/fstab, say) and use Add Folder."
)


class NetworkError(Exception):
    pass


@dataclass(frozen=True)
class Location:
    name: str
    uri: str
    connected: bool  # connected now, as opposed to only bookmarked


def run(args, timeout=QUERY_TIMEOUT_SECONDS):
    """Run a command, returning (returncode, stdout, stderr). A missing
    command or a timeout comes back as a failure rather than an exception.
    """
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def is_network_uri(text: str) -> bool:
    return urlsplit(text.strip()).scheme.lower() in NETWORK_SCHEMES


def normalise(uri: str) -> str:
    """One spelling per location: no trailing slash (except a bare host)."""
    uri = uri.strip()
    parts = urlsplit(uri)
    if parts.path not in ("", "/"):
        uri = uri.rstrip("/")
    return uri


def display_name(uri: str) -> str:
    """"Concerts on mediaserver.local" for smb://mediaserver.local/media/Concerts."""
    parts = urlsplit(uri)
    segments = [unquote(s) for s in parts.path.split("/") if s]
    host = parts.hostname or parts.netloc
    if not segments:
        return host
    return f"{segments[-1]} on {host}"


def subfolder_uri(base_uri: str, base_path, chosen_path) -> str:
    """The address of a folder chosen inside a share's local folder."""
    relative = PurePosixPath(chosen_path).relative_to(PurePosixPath(base_path))
    uri = normalise(base_uri)
    if relative.parts:
        if not uri.endswith("/"):
            uri += "/"
        uri += "/".join(quote(part) for part in relative.parts)
    return uri


# --- what can be reached --------------------------------------------------

def gvfs_present() -> bool:
    return shutil.which("gio") is not None


def gvfs_fuse_folder() -> Path | None:
    """Where gvfs-fuse puts shares, if it is running."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    folder = Path(runtime) / "gvfs"
    return folder if os.path.ismount(folder) else None


_MOUNT_LINE = re.compile(r"^Mount\(\d+\):\s*(.+?)\s*->\s*(\S+)\s*$")


def connected_locations(runner=run) -> list[Location]:
    """Shares GVFS is connected to right now (what Nemo or Nautilus show)."""
    if not gvfs_present():
        return []
    code, out, _err = runner(["gio", "mount", "-l"])
    if code != 0:
        return []
    found = []
    for line in out.splitlines():
        match = _MOUNT_LINE.match(line.strip())
        if match and is_network_uri(match.group(2)):
            found.append(Location(match.group(1), normalise(match.group(2)), True))
    return found


def bookmarked_locations(home=None) -> list[Location]:
    """Network places bookmarked in the file manager - GTK's bookmarks
    (Nemo, Nautilus, Caja, Thunar) and KDE's places (Dolphin).
    """
    home = Path(home) if home else Path.home()
    found = []

    gtk = home / ".config" / "gtk-3.0" / "bookmarks"
    try:
        lines = gtk.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        uri, _, name = line.strip().partition(" ")
        if is_network_uri(uri):
            found.append(Location(name.strip() or display_name(uri), normalise(uri), False))

    kde = home / ".local" / "share" / "user-places.xbel"
    try:
        tree = ET.parse(kde)
    except (OSError, ET.ParseError):
        tree = None
    if tree is not None:
        for bookmark in tree.getroot().iter("bookmark"):
            uri = bookmark.get("href") or ""
            if is_network_uri(uri):
                title = bookmark.findtext("title") or display_name(uri)
                found.append(Location(title, normalise(uri), False))
    return found


def known_locations(runner=run, home=None) -> list[Location]:
    """Connected shares first, then bookmarks not already listed."""
    seen = set()
    result = []
    for location in connected_locations(runner) + bookmarked_locations(home):
        if location.uri not in seen:
            seen.add(location.uri)
            result.append(location)
    return result


# --- turning an address into a folder -------------------------------------

def gvfs_local_path(uri: str, runner=run) -> str | None:
    code, out, _err = runner(["gio", "info", uri])
    if code != 0:
        return None
    for line in out.splitlines():
        if line.startswith("local path:"):
            return line.split(":", 1)[1].strip() or None
    return None


def gvfs_mount(uri: str, runner=run) -> str | None:
    """Connect GVFS to `uri`. Returns an error message, or None if it is
    connected (including when it already was).
    """
    code, out, err = runner(["gio", "mount", uri], timeout=MOUNT_TIMEOUT_SECONDS)
    if code == 0 or "already mounted" in err.lower():
        return None
    # gio asks for credentials on stdout and gives up when there's nobody to
    # answer - which, for SMB, also happens for a server that isn't there.
    if "authentication required" in out.lower() or "password" in out.lower():
        return (
            "it wants a user name and password, or the server couldn't be found. "
            "Check the address, then connect to it once in your file manager and "
            "choose to remember the password."
        )
    return err.strip() or out.strip() or "gio mount failed"


def kio_fuse_present(runner=run) -> bool:
    for method in ("ListNames", "ListActivatableNames"):
        code, out, _err = runner([
            "dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.DBus",
            "/org/freedesktop/DBus", f"org.freedesktop.DBus.{method}",
        ])
        if code == 0 and '"org.kde.KIOFuse"' in out:
            return True
    return False


def kio_fuse_mount(uri: str, runner=run) -> str:
    code, out, err = runner([
        "dbus-send", "--session", "--print-reply", "--dest=org.kde.KIOFuse",
        "/org/kde/KIOFuse", "org.kde.KIOFuse.VFS.mountUrl", f"string:{uri}",
    ], timeout=MOUNT_TIMEOUT_SECONDS)
    match = re.search(r'string\s+"(.*)"', out)
    if code != 0 or not match:
        raise NetworkError(
            f"KDE couldn't connect to {uri}: {err.strip() or 'no answer from kio-fuse'}"
        )
    return match.group(1)


def resolve(uri: str, runner=run, is_dir=os.path.isdir) -> str:
    """The local folder for a network address, connecting it if needed.

    Raises NetworkError saying what to do when it can't be reached.
    """
    uri = normalise(uri)
    if not is_network_uri(uri):
        raise NetworkError(f"{uri} isn't a network address (smb://, sftp://...).")

    problems = []
    if gvfs_present():
        path = gvfs_local_path(uri, runner)
        if path is None:
            error = gvfs_mount(uri, runner)
            if error:
                problems.append(f"Couldn't connect to {uri}: {error}")
            else:
                path = gvfs_local_path(uri, runner)
                if path is None:
                    problems.append(
                        "Connected, but this desktop isn't presenting network shares "
                        "as folders (gvfs-fuse isn't running)."
                    )
        if path is not None and is_dir(path):
            return path

    if kio_fuse_present(runner):
        path = kio_fuse_mount(uri, runner)
        if is_dir(path):
            return path
        problems.append(f"kio-fuse gave {path}, which isn't a folder.")

    bridge = gvfs_fuse_folder() is not None or kio_fuse_present(runner)
    if not bridge:
        problems.append(
            "Nothing on this desktop is presenting network shares as folders.\n\n"
            + INSTALL_HINT
        )
    raise NetworkError("\n\n".join(problems))
