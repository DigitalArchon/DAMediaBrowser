# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""GUI entry point.

Sets the Wayland app ID via setDesktopFileName so window matching works under
Wayland as well as X11. Modelled on StoryLoom's gui/app.py.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from mediabrowser.core import depcheck, store

APP_ID = "mediabrowser"
STYLESHEET = Path(__file__).with_name("style.qss")

# Preference order; the first family actually installed wins. Nothing is
# vendored, so this falls back to whatever the system provides.
FONT_CANDIDATES = ("Noto Sans", "DejaVu Sans", "Liberation Sans", "Cantarell")


def load_stylesheet() -> str:
    try:
        return STYLESHEET.read_text(encoding="utf-8")
    except OSError:
        return ""


def pick_font(app: QApplication) -> None:
    from PySide6.QtGui import QFontDatabase

    families = set(QFontDatabase.families())
    for candidate in FONT_CANDIDATES:
        if candidate in families:
            font = app.font()
            font.setFamily(candidate)
            app.setFont(font)
            return


def prefer_x11(environ=os.environ) -> None:
    """On a Wayland desktop, run under XWayland unless told otherwise.

    Manual Edit draws the video into its own window by handing mpv that
    window's id, which only X11 has. XWayland is on every mainstream
    Wayland desktop, and the AppImage carries Qt's X11 support; someone
    who wants native Wayland can set QT_QPA_PLATFORM=wayland, and the
    editor then plays the video in mpv's own window instead.

    A list naming xcb as a fallback - "wayland;xcb", which many desktops
    export for every Qt app - says X11 will do, so it's taken.
    """
    chosen = environ.get("QT_QPA_PLATFORM", "")
    allows_x11 = not chosen or (";" in chosen and "xcb" in chosen.split(";"))
    if allows_x11 and environ.get("WAYLAND_DISPLAY") and environ.get("DISPLAY"):
        environ["QT_QPA_PLATFORM"] = "xcb"


def build_app(argv: Sequence[str]) -> QApplication:
    QCoreApplication.setApplicationName("Media Chapter Browser")
    QCoreApplication.setOrganizationName("Media Chapter Browser")
    # Drives the Wayland app_id and the X11 WM_CLASS.
    QApplication.setDesktopFileName(APP_ID)
    # Manual Edit's video surface is a native window. Without this, Qt makes
    # every widget beside it native too, and a menu or dialog opened from
    # one of those is parented to a child window - which Qt warns about
    # ("... must be a top level window"), and Wayland handles badly.
    QApplication.setAttribute(Qt.AA_DontCreateNativeWidgetSiblings)

    app = QApplication(list(argv))
    app.setStyleSheet(load_stylesheet())
    pick_font(app)
    return app


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog="mediabrowser")
    parser.add_argument("--library", default=None, help="open this library folder on start")
    options, qt_arguments = parser.parse_known_args(arguments[1:])

    store.migrate_legacy_library()
    missing_required, bluray_missing = depcheck.check()
    prefer_x11()

    app = build_app([arguments[0], *qt_arguments])

    if missing_required:
        QMessageBox.critical(
            None,
            "Missing required software",
            "This app needs the following installed to work:\n\n"
            + "\n".join(f"  - {name}" for name in missing_required)
            + f"\n\nInstall them with:\n  {depcheck.install_hint()}"
            + "\n\nThen restart the app.",
        )
        return 1

    # Imported here so the dependency check above can report a clear problem
    # before anything heavier is pulled in.
    from mediabrowser.gui.main_window import MainWindow

    window = MainWindow(bluray_available=not bluray_missing)
    if options.library:
        window.load_root(options.library)
    window.show()

    if bluray_missing:
        QMessageBox.warning(
            window,
            "Blu-ray support unavailable",
            "libbluray isn't installed, so Blu-ray disc folders won't be found "
            "or playable. Regular video files are unaffected.\n\n"
            f"To add Blu-ray support, install it with:\n  {depcheck.install_hint()}",
        )

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
