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
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from mediabrowser.core import depcheck, hostenv, store
from mediabrowser.gui import plain

APP_ID = "mediabrowser"
STYLESHEET = Path(__file__).with_name("style.qss")
# The style sheet's accent (see its palette), for links.
LINK_COLOUR = "#7aa2f7"

# Preference order; the first family actually installed wins. Nothing is
# vendored, so this falls back to whatever the system provides.
FONT_CANDIDATES = ("Noto Sans", "DejaVu Sans", "Liberation Sans", "Cantarell")
# prctl(2)'s option for whether the process may be dumped (linux/prctl.h).
PR_SET_DUMPABLE = 4


def no_core_dumps() -> None:
    """A native crash must not write the app's memory to disk: What Was Sent - the words
    and frames sent to the AI - and what each enclave proved are kept in memory alone,
    and a key read from the keyring is there too. So, first thing, and in this order:

    - /proc/self/coredump_filter 0: a dump, should one be made, holds none of the
      app's memory. Written first: once the process is undumpable its /proc entries
      are root's, and the write is refused (measured on Mint 22).
    - not dumpable (PR_SET_DUMPABLE 0): the system's crash collector (systemd-coredump,
      apport) keeps nothing of a process that isn't, and no debugger or other program of
      yours can attach to it or read its memory through /proc.
    - a core size limit of 1, not 0: with a pipe in the kernel's core_pattern, as
      systemd-coredump's is, the kernel ignores a limit of 0 and hands the crash to the
      collector, which kept the dump and wrote the command line, the environment and the
      open files' paths into the journal (measured on MintDev, Nov 14 2026); at exactly
      1 it gives the dump up altogether ("RLIMIT_CORE is set to 1, aborting core"). The
      hard limit is left as it is; where it is 0 already, the soft one stays 0.

    mpv and ffmpeg, started from here, inherit the filter and the limit (an exec makes a
    program dumpable again). Whatever fails is left as it was: nothing else depends on
    it. Linux alone. The standard every DA app keeps (cross-app security, Nov 2026)."""
    if not sys.platform.startswith("linux"):
        return
    try:
        with open("/proc/self/coredump_filter", "w", encoding="ascii") as f:
            f.write("0")
    except OSError:
        pass
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    except (OSError, AttributeError):
        pass
    try:
        import resource

        _soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        if hard == resource.RLIM_INFINITY or hard >= 1:
            resource.setrlimit(resource.RLIMIT_CORE, (1, hard))
        else:
            resource.setrlimit(resource.RLIMIT_CORE, (0, hard))
    except (ImportError, ValueError, OSError):
        pass


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

    For this process alone (hostenv.set_own): a browser or file manager the app
    starts - a Qt program, maybe - gets the value it had before.
    """
    chosen = environ.get("QT_QPA_PLATFORM", "")
    allows_x11 = not chosen or (";" in chosen and "xcb" in chosen.split(";"))
    if allows_x11 and environ.get("WAYLAND_DISPLAY") and environ.get("DISPLAY"):
        if environ is os.environ:
            hostenv.set_own("QT_QPA_PLATFORM", "xcb")
        else:
            environ["QT_QPA_PLATFORM"] = "xcb"


def build_app(argv: Sequence[str]) -> QApplication:
    QCoreApplication.setApplicationName("DA Media Browser")
    QCoreApplication.setOrganizationName("DA Media Browser")
    # Drives the Wayland app_id and the X11 WM_CLASS.
    QApplication.setDesktopFileName(APP_ID)
    # Manual Edit's video surface is a native window. Without this, Qt makes
    # every widget beside it native too, and a menu or dialog opened from
    # one of those is parented to a child window - which Qt warns about
    # ("... must be a top level window"), and Wayland handles badly.
    QApplication.setAttribute(Qt.AA_DontCreateNativeWidgetSiblings)

    app = QApplication(list(argv))
    app.setStyleSheet(load_stylesheet())
    # Links in rich text take their colour from the palette, not the style
    # sheet: Qt's own dark blue is all but unreadable on the dark theme.
    palette = app.palette()
    for role in (QPalette.Link, QPalette.LinkVisited):
        palette.setColor(role, QColor(LINK_COLOUR))
    app.setPalette(palette)
    pick_font(app)
    return app


def main(argv: Sequence[str] | None = None) -> int:
    # Before anything is read or asked: the app's whole life is kept from a core dump.
    no_core_dumps()
    arguments = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog="mediabrowser")
    parser.add_argument("--library", default=None, help="open this library folder on start")
    options, qt_arguments = parser.parse_known_args(arguments[1:])

    store.migrate_legacy_library()
    # Libraries stored apart before overlapping ones shared their videos.
    store.merge_overlapping()
    missing_required, bluray_missing = depcheck.check()
    prefer_x11()

    app = build_app([arguments[0], *qt_arguments])

    if missing_required:
        plain.critical(
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
        plain.warning(
            window,
            "Blu-ray support unavailable",
            "libbluray isn't installed, so Blu-ray disc folders won't be found "
            "or playable. Regular video files are unaffected.\n\n"
            f"To add Blu-ray support, install it with:\n  {depcheck.install_hint()}",
        )

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
