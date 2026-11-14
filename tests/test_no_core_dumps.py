# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""No core dump of the app: What Was Sent, what each enclave proved and the key read
from the keyring are in memory alone, and a crash must not write them to disk."""

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux's prctl")


def test_the_app_is_neither_dumpable_nor_given_room_for_a_core_file():
    # In a process of its own: a test process made undumpable stays so.
    found = subprocess.run(
        [sys.executable, "-c",
         "import ctypes, resource\n"
         "from mediabrowser.gui.app import no_core_dumps\n"
         "libc = ctypes.CDLL(None)\n"
         "before = libc.prctl(3, 0, 0, 0, 0)\n"            # PR_GET_DUMPABLE
         "no_core_dumps()\n"
         "print(before, libc.prctl(3, 0, 0, 0, 0), *resource.getrlimit(resource.RLIMIT_CORE))"],
        cwd=ROOT, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=120, check=True)
    before, after, soft, hard = found.stdout.split()
    assert before == "1", "dumpable to begin with, so the test shows a change"
    assert (after, soft, hard) == ("0", "0", "0")


def test_main_turns_core_dumps_off_before_anything_else(monkeypatch):
    from mediabrowser.core import store
    from mediabrowser.gui import app

    class Stop(Exception):
        pass

    done = []

    def no_core_dumps():
        done.append("no core dumps")
        raise Stop

    monkeypatch.setattr(app, "no_core_dumps", no_core_dumps)
    monkeypatch.setattr(store, "migrate_legacy_library", lambda: done.append("settings"))
    with pytest.raises(Stop):
        app.main(["mediabrowser"])
    assert done == ["no core dumps"]


def test_every_way_the_app_starts_goes_through_main():
    # The AppImage, the installed command and main.py: each starts mediabrowser.gui.app's
    # main, which turns core dumps off first.
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts == {"mediabrowser": "mediabrowser.gui.app:main"}
    assert '-m mediabrowser.gui.app "$@"' in (ROOT / "packaging/appimage/AppRun").read_text()
    assert "from mediabrowser.gui.app import main" in (ROOT / "main.py").read_text()
    source = (ROOT / "mediabrowser/gui/app.py").read_text()
    assert source.rstrip().endswith('if __name__ == "__main__":\n    raise SystemExit(main())')
