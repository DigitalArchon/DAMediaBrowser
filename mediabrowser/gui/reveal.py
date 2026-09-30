# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Show a video in the desktop's file manager.

The freedesktop FileManager1 interface opens the folder with the video
itself highlighted, which in a folder of fifty concerts is the difference
between useful and not. Most Linux file managers provide it (Nautilus,
Dolphin, Nemo, Thunar, Caja, PCManFM-Qt); where none does, the folder is
simply opened.

Going over D-Bus also means the file manager is started by the session,
not by this process - so it does not inherit the AppImage's library path.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from mediabrowser.core import folders


def _show_items(item: Path) -> bool:
    try:
        from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage
    except ImportError:
        return False
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        return False
    interface = QDBusInterface(
        "org.freedesktop.FileManager1",
        "/org/freedesktop/FileManager1",
        "org.freedesktop.FileManager1",
        bus,
    )
    if not interface.isValid():
        return False
    reply = interface.call("ShowItems", [QUrl.fromLocalFile(str(item)).toString()], "")
    return reply.type() != QDBusMessage.ErrorMessage


def show_in_file_manager(video) -> bool:
    """Open the folder holding `video`, highlighting it where the file
    manager can. False if there was nothing to open.
    """
    folder, item = folders.reveal_target(video)
    folder, item = Path(folder), Path(item)
    if not folder.is_dir():
        return False
    if item.exists() and _show_items(item):
        return True
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
