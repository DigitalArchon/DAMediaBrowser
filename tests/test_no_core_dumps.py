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


def _after_no_core_dumps(hard="unchanged"):
    """In a process of its own (a test process made undumpable stays so): whether it
    was dumpable before and after, the core limits after, and its coredump_filter
    after. `hard`: the hard core limit it starts with."""
    found = subprocess.run(
        [sys.executable, "-c",
         "import ctypes, resource, sys\n"
         "hard = sys.argv[1]\n"
         "if hard != 'unchanged':\n"
         "    resource.setrlimit(resource.RLIMIT_CORE, (0, int(hard)))\n"
         "from mediabrowser.gui.app import no_core_dumps\n"
         "libc = ctypes.CDLL(None)\n"
         "before = libc.prctl(3, 0, 0, 0, 0)\n"            # PR_GET_DUMPABLE
         "no_core_dumps()\n"
         "soft, hard = resource.getrlimit(resource.RLIMIT_CORE)\n"
         "inf = resource.RLIM_INFINITY\n"
         "print(before, libc.prctl(3, 0, 0, 0, 0), soft, 'inf' if hard == inf else hard,\n"
         "      open('/proc/self/coredump_filter').read().strip())",
         hard],
        cwd=ROOT, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=120, check=True)
    return found.stdout.split()


def test_the_app_is_undumpable_with_a_core_limit_of_1_and_no_memory_in_a_dump():
    """The standard's three, every one of them in force: filter 0 (written before the
    process is undumpable, or the write is refused), not dumpable, and a soft core
    limit of exactly 1 - at which the kernel gives a dump up even with a pipe in
    core_pattern, where 0 is ignored - with the hard limit left as it was."""
    before, after, soft, hard, coredump_filter = _after_no_core_dumps()
    assert before == "1", "dumpable to begin with, so the test shows a change"
    assert (after, soft, coredump_filter) == ("0", "1", "00000000")
    import resource
    started = resource.getrlimit(resource.RLIMIT_CORE)[1]
    assert hard == ("inf" if started == resource.RLIM_INFINITY else str(started))


def test_a_hard_limit_of_0_is_kept_and_the_rest_still_holds():
    _before, after, soft, hard, coredump_filter = _after_no_core_dumps(hard="0")
    assert (after, soft, hard, coredump_filter) == ("0", "0", "0", "00000000")


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
