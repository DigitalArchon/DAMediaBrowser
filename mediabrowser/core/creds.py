# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Secrets in the OS keyring: the Secret Service on Linux (GNOME Keyring,
KWallet), reached over D-Bus. There is no fallback to a file: without a
keyring, a secret can only come from the environment.
"""

from __future__ import annotations

import keyring
from keyring.errors import PasswordDeleteError

from . import config

SERVICE = config.APP_NAME


def backend_error() -> str | None:
    """What's wrong, in words, when there's no keyring to keep secrets in."""
    try:
        backend = keyring.get_keyring()
    except Exception as e:  # noqa: BLE001 - any backend failure is reportable
        return f"The keyring can't be reached: {e}"
    if getattr(backend, "priority", 0) < 1:
        return (
            "There's no keyring to keep it in. Install and unlock a Secret "
            "Service provider such as GNOME Keyring or KWallet."
        )
    return None


def get_secret(name: str) -> str | None:
    return keyring.get_password(SERVICE, name)


def set_secret(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def delete_secret(name: str) -> None:
    try:
        keyring.delete_password(SERVICE, name)
    except PasswordDeleteError:
        pass
