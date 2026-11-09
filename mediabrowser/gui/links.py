# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Links that open in the web browser: where each goes is said before it's
clicked, and one that isn't secure isn't opened.

A label's link would otherwise open at a click with nothing to say where:
its words need not be its address. Here, hovering one says "Opens
https://… in your web browser". A link netguard wouldn't let the app use
itself - plain http to anywhere but this computer, its own network or an
onion service - isn't opened at all, and a message says why.

Opening a link is the browser's request, not the app's, so it isn't on
What Was Sent: that's why the address is said first.
"""

from __future__ import annotations

from urllib.parse import urlparse

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QLabel, QMessageBox, QWidget

from mediabrowser.core import netguard


def tip(url: str) -> str:
    return f"Opens {url} in your web browser"


def guard(label: QLabel) -> None:
    """The label's links say where they go on hover, and open only if
    they're secure."""
    label.setOpenExternalLinks(False)
    label.setTextInteractionFlags(label.textInteractionFlags() | Qt.LinksAccessibleByMouse)
    resting = label.toolTip()
    label.linkHovered.connect(lambda url: label.setToolTip(tip(url) if url else resting))
    label.linkActivated.connect(lambda url: open_link(label, url))


def problem(url: str) -> str | None:
    """Why the link isn't opened, or None when it may be."""
    found = netguard.problem(url)
    if found and urlparse(str(url).strip()).scheme.lower() == "http":
        return ("This link uses plain http, so the page wouldn't be encrypted: anyone "
                "between you and the site could read it, or change it on its way. Only "
                "https links are opened from here.")
    return found


def open_link(parent: QWidget | None, url: str) -> bool:
    """Open `url` in the browser, unless it isn't secure: then say so."""
    why = problem(url)
    if why:
        QMessageBox.warning(parent, "Link Not Opened",
                            f"{why}\n\nThe link wasn't opened: {url}")
        return False
    return QDesktopServices.openUrl(QUrl(url))
