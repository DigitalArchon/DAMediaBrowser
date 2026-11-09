# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Where Privacy Mode's end-to-end encryption client lives, run from source.

nano-gpt's `private/*` models are sealed to their enclave with Tinfoil's own
SDK (see sealed.py). No distribution packages it, and it isn't one of this
app's own dependencies: it pulls in sigstore, httpx and the OpenAI client,
which nothing else here needs. So it is installed, pinned by hash from
packaging/private-mode.lock, into a folder of its own:

  ~/.local/share/da-media-browser/private-mode

by tools/install-private-mode.sh, and put *ahead* of everything else on
sys.path, before anything imports cryptography: behind a virtualenv's or the
distribution's older copy, Tinfoil's pyOpenSSL would fail against it.
mediabrowser/__init__.py calls activate() for that reason, before any other
module of the app is imported.

The AppImage carries the client in a folder of the same name beside its
Python (sys.prefix/private-mode), installed from the same lock by
packaging/appimage/build.sh, and never reads the one under ~/.local:
nothing the user installed may shadow what was bundled.

From DA Assistant's vendored.py (Nov 2026).
"""

import os
import sys

DIRNAME = "private-mode"

_active = None


def user_dir() -> str:
    """The folder tools/install-private-mode.sh installs into."""
    from . import config
    return str(config.DATA_DIR / DIRNAME)


def bundled_dir() -> str:
    """Where the AppImage carries the client: beside its own Python."""
    return os.path.join(sys.prefix, DIRNAME)


def candidates() -> list[str]:
    """The folder to look in: the AppImage's own if this Python has one -
    and then never the user's - or else the one the install script fills."""
    if os.path.isdir(os.path.join(bundled_dir(), "tinfoil")):
        return [bundled_dir()]
    return [user_dir()]


def activate():
    """Put the private-mode folder at the front of sys.path, if it is there.
    Idempotent; returns the folder, or None."""
    global _active
    if _active is not None:
        return _active or None
    _active = ""
    for path in candidates():
        if os.path.isdir(os.path.join(path, "tinfoil")):
            if path not in sys.path:
                sys.path.insert(0, path)
            _active = path
            break
    return _active or None


def location():
    """The folder in use, or None."""
    return _active or None
