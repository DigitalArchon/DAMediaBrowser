# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The AppImage's packaging: the X libraries it carries (packaging/appimage/build.sh's
DEBIAN_LIBS) cover what Qt's xcb plugins need beyond what a desktop with no X11 session
has (packaging/appimage/check-x11-libs.sh, which build.sh runs). 0.23.0's first build
carried xkbcommon-x11 but not the libxcb-xkb it and the xcb plugin link, and a stock
Ubuntu 26.04 GNOME, which has no libxcb-xkb1, opened no window at all (lab, Nov 12
2026). Checked here against the PySide6 this suite runs, the version the AppImage
carries; the carried libraries stand in as empty files named as build.sh names them."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "packaging" / "appimage" / "build.sh"
CHECK = ROOT / "packaging" / "appimage" / "check-x11-libs.sh"

needs_objdump = pytest.mark.skipif(shutil.which("objdump") is None,
                                   reason="needs objdump (binutils)")


def carried():
    """The sonames of the Debian packages build.sh carries in usr/lib/x11:
    libxcb-xkb1 gives libxcb-xkb.so.1, libxkbcommon-x11-0 libxkbcommon-x11.so.0."""
    text = BUILD.read_text()
    block = text[text.index("DEBIAN_LIBS=("):]
    block = block[:block.index("\n)")]
    names = []
    for line in block.splitlines():
        found = re.match(r'\s*"\S*?/(lib[^/_]+)_\S+\.deb\s+[0-9a-f]{64}"', line)
        if found:
            stem, number = re.fullmatch(r"(lib.+?)-?(\d+)", found.group(1)).groups()
            names.append(f"{stem}.so.{number}")
    return names


def appdir(root: Path, libraries) -> Path:
    """An AppDir as build.sh lays one out: PySide6's Qt in the Python's site-packages
    (this suite's own, linked), and the X libraries it carries."""
    import PySide6
    site = root / "opt" / "python3.12" / "lib" / "python3.12" / "site-packages" / "PySide6"
    site.mkdir(parents=True)
    (site / "Qt").symlink_to(Path(PySide6.__file__).parent / "Qt")
    x11 = root / "usr" / "lib" / "x11"
    x11.mkdir(parents=True)
    for name in libraries:
        (x11 / name).write_bytes(b"")
    return root


def check(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(CHECK), str(root)], capture_output=True, text=True,
                          timeout=120)


def test_build_sh_names_each_package_it_carries():
    assert carried() == [
        "libxcb-cursor.so.0", "libxcb-icccm.so.4", "libxcb-image.so.0",
        "libxcb-keysyms.so.1", "libxcb-render-util.so.0", "libxcb-util.so.1",
        "libxkbcommon.so.0", "libxkbcommon-x11.so.0", "libxcb-xkb.so.1"]


@needs_objdump
def test_what_it_carries_covers_what_qts_xcb_plugins_need(tmp_path):
    done = check(appdir(tmp_path, carried()))
    assert done.returncode == 0, done.stderr
    assert "every X library Qt's xcb plugins need is carried" in done.stdout


@needs_objdump
def test_the_build_stops_on_one_it_does_not_carry(tmp_path):
    done = check(appdir(tmp_path, [name for name in carried()
                                   if name != "libxcb-xkb.so.1"]))
    assert done.returncode == 1
    assert "Qt's xcb plugins need libxcb-xkb.so.1, which the AppImage doesn't carry" \
        in done.stderr
