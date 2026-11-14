# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The programs the app starts get the environment they would have had anyway.

mpv, ffmpeg and ffprobe, gio, and the browser or file manager a link or a
folder opens in are the computer's own programs, not the app's. A few
variables are set for the app alone:

- by the AppImage's AppRun: LD_LIBRARY_PATH with the X11 libraries it carries,
  built for an old glibc and meant for Qt alone; SSL_CERT_FILE, SSL_CERT_DIR and
  REQUESTS_CA_BUNDLE, pointing its own Python at a trust store; PYTHONHOME and
  PYTHONPATH taken away. AppRun keeps each one's value from before as
  APPIMAGE_HOST_<NAME>, and names them in APPIMAGE_HOST_ENV_KEYS: one named with
  no APPIMAGE_HOST_ value beside it was unset, and stays unset;
- by the app itself (set_own): QT_QPA_PLATFORM, which gui/app.prefer_x11 sets to
  xcb so that mpv can draw into the app's window - a Qt program the browser
  is, or a file manager, would otherwise open on XWayland too;
- and by the AppImage's runtime, naming the image and its mount (APPIMAGE,
  APPDIR, OWD, ARGV0): an AppImage-packaged browser that inherited them would
  take them for its own.

host_env() undoes all of it, for every program the app starts. As DA
Assistant's assistant/hostenv.py, which is the reference for every DA app.
"""

from __future__ import annotations

import os
import shutil
import subprocess

PREFIX = "APPIMAGE_HOST_"
KEYS = "APPIMAGE_HOST_ENV_KEYS"
RUNTIME = ("APPIMAGE", "APPDIR", "OWD", "ARGV0")

# What the app has set for its own process (set_own), each with the value it
# had before (None: unset), which the programs it starts get back.
_own: dict[str, str | None] = {}


def set_own(name: str, value: str | None) -> None:
    """Set `name` for this process alone (None: unset it): host_env() hands
    every program it starts the value it had before."""
    if name not in _own:
        _own[name] = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def original(name: str) -> str | None:
    """What `name` was before the app or AppRun set it, or is now if neither did."""
    if name in _own:
        return _own[name]
    if name in os.environ.get(KEYS, "").split():
        return os.environ.get(PREFIX + name)
    return os.environ.get(name)


def host_env(environ=None) -> dict:
    """The environment for a program of the computer's own: `environ` (this
    process's, by default) as it would have been without the AppImage and the
    app (see the module)."""
    env = dict(os.environ if environ is None else environ)
    for name, before in _own.items() if environ is None else ():
        if before is None:
            env.pop(name, None)
        else:
            env[name] = before
    keys = env.pop(KEYS, "").split()
    for name in keys:
        env.pop(name, None)
        # Only what AppRun found set was kept: a name with nothing kept was unset.
        saved = env.pop(PREFIX + name, None)
        if saved is not None:
            env[name] = saved
    if keys:
        for name in RUNTIME:
            env.pop(name, None)
    return env


def open_with_desktop(target: str) -> bool | None:
    """Open `target` - a web address, or a folder's file:// address - with the
    desktop's own handler for it, started with the host's environment: xdg-open,
    as Qt's own QDesktopServices would start it, but not with the app's
    environment. None when there is no xdg-open to start it with (the caller
    falls back to QDesktopServices); whether it started otherwise."""
    program = shutil.which("xdg-open", path=host_env().get("PATH"))
    if program is None:
        return None
    try:
        _start([program, target], host_env())
    except OSError:
        return False
    return True


def _start(argv, env) -> None:
    """Start a desktop program and leave it running, apart from the app (its own
    session: closing the app doesn't close the browser). Tests stand in here."""
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env=env, cwd=os.path.expanduser("~"),
                     start_new_session=True)
