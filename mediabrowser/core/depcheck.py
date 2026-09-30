# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Startup checks for the external binaries/libraries this app depends on,
so a missing one produces a clear message instead of a confusing failure
partway through scanning or playback.
"""

import shutil
from pathlib import Path

from . import bluray

# The window is Qt now, so python3-tk/tk are no longer among these; PySide6
# is a Python package and is installed with the app rather than by the
# distribution, so it isn't here either - main.py reports that one.
INSTALL_COMMANDS = {
    "arch": "sudo pacman -S ffmpeg mpv libbluray",
    "debian": "sudo apt install ffmpeg mpv libbluray-bin",
    "unknown": "install ffmpeg, mpv, and libbluray using your distribution's package manager",
}


def _distro_family():
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        return "unknown"

    fields = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key] = value.strip('"')

    identifiers = f"{fields.get('ID', '')} {fields.get('ID_LIKE', '')}".lower()
    if "arch" in identifiers:
        return "arch"
    if "debian" in identifiers or "ubuntu" in identifiers:
        return "debian"
    return "unknown"


def install_hint():
    return INSTALL_COMMANDS.get(_distro_family(), INSTALL_COMMANDS["unknown"])


def check():
    """Returns (missing_required, bluray_missing).

    missing_required: names of hard-required binaries not found on PATH -
        the app can't do anything useful without these.
    bluray_missing: True if libbluray isn't usable - a soft dependency,
        only Blu-ray disc folders are affected.
    """
    missing_required = []
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        missing_required.append("ffmpeg")
    if shutil.which("mpv") is None:
        missing_required.append("mpv")

    return missing_required, not bluray.is_available()
