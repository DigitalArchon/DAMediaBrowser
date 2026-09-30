# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The library is read only: no media file is ever written to, renamed,
moved or deleted, and nothing is put among them. Everything the app keeps
is in its own data folder (config.DATA_DIR) or a temporary one.

Two guards. Every place in the source that can write, rename or delete a
file is listed here, each one looked at: a new one fails the test until it
has been. And a real library - files and folders made unwritable, as a
read-only share would be - is scanned, measured, grabbed from and exported
with the real ffmpeg, and must come out byte for byte, time for time, as
it went in.
"""

import ast
import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from mediabrowser.core import (
    artwork,
    audio_levels,
    catalog,
    config,
    frames,
    library,
    stage_light,
    store,
)

SOURCE = Path(__file__).resolve().parent.parent / "mediabrowser"

# --- every place that can write -----------------------------------------------

PATH_WRITES = {"write_text", "write_bytes", "unlink", "rename", "touch", "mkdir", "rmdir",
               "symlink_to", "hardlink_to", "chmod", "lchmod"}
MODULE_WRITES = (
    {("os", name) for name in ("remove", "unlink", "rename", "replace", "renames", "mkdir",
                               "makedirs", "rmdir", "removedirs", "chmod", "utime",
                               "truncate", "link", "symlink")}
    | {("shutil", name) for name in ("move", "copy", "copy2", "copyfile", "copytree",
                                     "rmtree", "copymode", "copystat")}
)
# Calls that share a name with a file operation but aren't one: the shelf of
# libraries, a playlist, the chapter editor's sheet.
NOT_FILES = {"shown", "playlists", "sheet", "painter"}

# Each module's writes, and where they go. Change a number only after
# checking that the new write can't reach a library.
REVIEWED = {
    "core/artwork.py": 6,       # the cover cache, config.ARTWORK_DIR
    "core/audio_levels.py": 2,  # its own temporary folder
    "core/bdmenu.py": 2,        # a temporary file for ffmpeg
    "core/catalog.py": 2,       # an export, refused inside any library
    "core/config.py": 2,        # the data folders
    "core/library.py": 1,       # cached covers renamed within ARTWORK_DIR
    "core/player.py": 2,        # mpv's socket, in the temporary folder
    "core/store.py": 8,         # settings and library files, config.DATA_DIR
}


def _writes(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            mode = node.args[1] if len(node.args) > 1 else next(
                (k.value for k in node.keywords if k.arg == "mode"), None)
            if mode is None:
                continue  # "r"
            if not isinstance(mode, ast.Constant) or set(mode.value) & set("wax+"):
                found.append(ast.unparse(node))
            continue
        if not isinstance(func, ast.Attribute):
            continue
        receiver = func.value.attr if isinstance(func.value, ast.Attribute) else (
            func.value.id if isinstance(func.value, ast.Name) else None)
        if receiver in NOT_FILES:
            continue
        if (receiver, func.attr) in MODULE_WRITES or func.attr in PATH_WRITES:
            found.append(ast.unparse(node))
        elif func.attr == "replace" and len(node.args) == 1 and not node.keywords:
            found.append(ast.unparse(node))  # Path.replace, not str.replace
        elif func.attr in ("write", "save") and node.args and receiver is not None:
            found.append(ast.unparse(node))
    return found


def test_every_write_in_the_source_has_been_reviewed():
    seen = {}
    for path in sorted(SOURCE.rglob("*.py")):
        writes = _writes(path)
        if writes:
            seen[path.relative_to(SOURCE).as_posix()] = writes
    counts = {module: len(writes) for module, writes in seen.items()}
    assert counts == REVIEWED, (
        "A file write was added or removed - check it can't touch a library, then "
        f"update REVIEWED: {seen}"
    )


def test_ffmpeg_is_only_ever_told_to_write_into_the_cover_cache():
    """artwork's ffmpeg is the one that writes a file (the rest send their
    output down a pipe); each of its calls ends with the cache target."""
    text = (SOURCE / "core" / "artwork.py").read_text(encoding="utf-8")
    calls = text.count("_run_ffmpeg([")
    assert calls == 3 and text.count("        str(target),\n    ])") == 2
    assert '["-i", str(source), "-frames:v", "1", "-vf", _SCALE, str(target)]' in text
    for name in ("audio_levels", "frames", "stage_light", "bdmenu"):
        module = (SOURCE / "core" / f"{name}.py").read_text(encoding="utf-8")
        assert '"-y"' not in module, f"{name} lets ffmpeg overwrite"


# --- a real, read-only library ------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg"
)


def _make_video(path: Path) -> None:
    chapters = path.with_suffix(".txt")
    chapters.write_text(
        ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=2000\ntitle=Megitsune\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=2000\nEND=4000\ntitle=Karate\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin",
         "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=4",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-i", str(chapters), "-map_metadata", "2", "-map_chapters", "2",
         "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True,
    )
    chapters.unlink()


def _snapshot(root: Path) -> dict:
    """Every file and folder under root: its bytes, size, times and mode."""
    seen = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            info = path.stat()
            digest = (hashlib.sha256(path.read_bytes()).hexdigest()
                      if stat.S_ISREG(info.st_mode) else None)
            seen[path.relative_to(root).as_posix()] = (
                digest, info.st_size, info.st_mtime_ns, info.st_mode
            )
    return seen


def _set_writable(root: Path, writable: bool) -> None:
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            os.chmod(Path(dirpath) / name, 0o644 if writable else 0o444)
        os.chmod(dirpath, 0o755 if writable else 0o555)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "LIBRARIES_DIR", data / "libraries")
    monkeypatch.setattr(config, "LEGACY_LIBRARY_FILE", data / "library.json")
    monkeypatch.setattr(config, "APP_SETTINGS_FILE", data / "settings.json")
    monkeypatch.setattr(config, "ARTWORK_DIR", data / "artwork")
    return data


@pytest.fixture
def read_only_library(tmp_path):
    root = tmp_path / "Concerts"
    (root / "Tokyo Dome").mkdir(parents=True)
    _make_video(root / "Tokyo Dome" / "Tokyo Dome.mkv")
    _make_video(root / "Wembley.mp4")
    (root / "Tokyo Dome" / "cover.jpg").write_bytes(
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi",
                        "-i", "color=c=red:s=64x64:d=1", "-frames:v", "1",
                        "-f", "image2pipe", "-c:v", "mjpeg", "-"],
                       check=True, capture_output=True).stdout
    )
    _set_writable(root, False)
    yield root
    _set_writable(root, True)


@needs_ffmpeg
def test_nothing_in_a_library_changes_whatever_is_done_with_it(
        read_only_library, data_dir, tmp_path):
    root = read_only_library
    before = _snapshot(root)

    data = library.rescan(root)
    assert len(data["videos"]) == 2
    store.save_library(data)
    data = library.rescan(root, fresh=True)
    for video_id, video in data["videos"].items():
        assert artwork.find(video_id, video) is not None  # cover.jpg, or a frame
        assert frames.grab(video, 1.0) is not None
        if video["path"].endswith(".mp4"):
            assert audio_levels.read_levels(video["path"], video["duration"]).full
            stage_light.brightness_at(video["path"], 1.0)
    catalog.export(data, tmp_path / "out.json")

    assert _snapshot(root) == before
    written = {p.parent.name for p in data_dir.rglob("*") if p.is_file()}
    assert written <= {"data", "libraries", "artwork"}


def test_a_catalog_is_never_saved_into_a_library(tmp_path, data_dir):
    root = tmp_path / "Concerts"
    root.mkdir()
    video = root / "Budokan.mkv"
    video.write_bytes(b"video")
    data = store.default_library(str(root))
    data["videos"] = {"a": {"path": str(video)}}
    other = tmp_path / "Festivals"
    for path, roots in ((root / "catalog.json", ()), (video, ()),
                        (other / "sub" / "c.json", [str(other)])):
        with pytest.raises(catalog.CatalogError):
            catalog.export(data, path, roots)
    assert video.read_bytes() == b"video" and sorted(os.listdir(root)) == ["Budokan.mkv"]
    # Nor over something that isn't a catalog, anywhere.
    elsewhere = tmp_path / "notes.json"
    elsewhere.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(catalog.CatalogError):
        catalog.export(data, elsewhere)
    assert elsewhere.read_text(encoding="utf-8") == "[1, 2]"
    catalog.export(data, tmp_path / "Concerts catalog.json")
    catalog.export(data, tmp_path / "Concerts catalog.json")  # over its own: fine


def test_mpv_never_runs_in_the_folder_the_app_was_started_from(monkeypatch, tmp_path):
    from mediabrowser.core import player

    launched = {}

    class Popen:
        def __init__(self, args, **kwargs):
            launched.update(kwargs)

        def poll(self):
            return None

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(player.subprocess, "Popen", Popen)
    player.Player()._launch(["/v/a.mkv"], None, None, False)
    assert launched["cwd"] == Path.home()
