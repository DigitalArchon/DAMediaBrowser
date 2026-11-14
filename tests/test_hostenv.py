# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The programs the app starts - mpv, ffmpeg, gio, the browser, the file
manager - get the environment they would have had anyway: not the AppImage's
library path or trust store, not the platform the app chose for itself."""

import ast
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mediabrowser.core import hostenv, player
from mediabrowser.gui import app

ROOT = Path(__file__).resolve().parent.parent
# Before the conftest stands in for it, which every test does.
REAL_START = hostenv._start


@pytest.fixture(autouse=True)
def nothing_set_by_the_app(monkeypatch):
    monkeypatch.setattr(hostenv, "_own", {})


def apprun_environment(tmp_path, environ) -> dict:
    """The environment AppRun hands the app, run as it is from an AppDir whose
    Python only prints what it was given."""
    appdir = tmp_path / "AppDir"
    python = appdir / "opt" / "python3.12" / "bin" / "python3.12"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexec env -0\n")
    python.chmod(0o755)
    (appdir / "usr" / "lib" / "x11").mkdir(parents=True)
    shutil.copy(ROOT / "packaging" / "appimage" / "AppRun", appdir / "AppRun")
    out = subprocess.run(["/bin/sh", str(appdir / "AppRun")], env=environ,
                         capture_output=True, check=True, timeout=30).stdout
    return dict(item.split("=", 1) for item in out.decode().split("\0") if "=" in item)


class TestTheAppImage:
    def test_a_program_gets_back_what_apprun_set_for_the_app(self, tmp_path):
        host = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                "LD_LIBRARY_PATH": "/opt/host/lib", "PYTHONPATH": "/home/me/py",
                "APPIMAGE": "/x.AppImage", "APPDIR": "/tmp/.mount_x", "OWD": "/w",
                "ARGV0": "x"}
        inside = apprun_environment(tmp_path, host)
        assert inside["LD_LIBRARY_PATH"].endswith("/usr/lib/x11:/opt/host/lib")
        assert "PYTHONPATH" not in inside
        assert inside["SSL_CERT_FILE"] == inside["REQUESTS_CA_BUNDLE"]
        given = hostenv.host_env(inside)
        for name in ("LD_LIBRARY_PATH", "PYTHONPATH", "PATH", "HOME"):
            assert given[name] == host[name], name
        # Set by AppRun where they were unset: unset for the program again.
        for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "PYTHONHOME",
                     "APPIMAGE", "APPDIR", "OWD", "ARGV0", hostenv.KEYS):
            assert name not in given, name
        assert not any(name.startswith(hostenv.PREFIX) for name in given)

    def test_one_the_user_had_set_is_theirs_again(self, tmp_path):
        mine = str(tmp_path / "my-bundle.pem")
        inside = apprun_environment(tmp_path, {"PATH": "/usr/bin:/bin",
                                               "SSL_CERT_FILE": mine})
        assert inside["SSL_CERT_FILE"] == mine == hostenv.host_env(inside)["SSL_CERT_FILE"]
        assert "LD_LIBRARY_PATH" not in hostenv.host_env(inside)

    def test_outside_the_appimage_the_environment_is_as_it_is(self, monkeypatch):
        monkeypatch.delenv(hostenv.KEYS, raising=False)
        monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/mine")
        assert hostenv.host_env() == dict(os.environ)


class TestWhatTheAppSetsForItself:
    def test_the_platform_it_chose_is_its_own(self, monkeypatch):
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
        app.prefer_x11()
        try:
            assert os.environ["QT_QPA_PLATFORM"] == "xcb"
            assert "QT_QPA_PLATFORM" not in hostenv.host_env()
            assert hostenv.original("QT_QPA_PLATFORM") is None
        finally:
            os.environ.pop("QT_QPA_PLATFORM", None)

    def test_one_the_user_set_is_left_alone(self, monkeypatch):
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setenv("QT_QPA_PLATFORM", "wayland;xcb")
        app.prefer_x11()
        assert os.environ["QT_QPA_PLATFORM"] == "xcb"
        assert hostenv.host_env()["QT_QPA_PLATFORM"] == "wayland;xcb"

    def test_mpv_draws_on_x11_from_the_host_environment(self, monkeypatch):
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setenv(hostenv.KEYS, "LD_LIBRARY_PATH")
        monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/.mount_x/usr/lib/x11")
        monkeypatch.delenv(hostenv.PREFIX + "LD_LIBRARY_PATH", raising=False)
        env = player.x11_environment()
        assert "WAYLAND_DISPLAY" not in env and "LD_LIBRARY_PATH" not in env


class TestEveryProgramStarted:
    def test_every_subprocess_is_given_the_host_environment(self):
        """A program started with no env= inherits the app's: the AppImage's
        library path, its trust store, the platform it chose."""
        missing = []
        for path in sorted((ROOT / "mediabrowser").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "subprocess"
                        and node.func.attr in ("run", "Popen", "call", "check_call",
                                               "check_output")
                        and not any(k.arg == "env" for k in node.keywords)):
                    missing.append(f"{path.name}:{node.lineno}")
        assert not missing

    def test_links_and_folders_open_through_the_host_environment(self):
        for name in ("links.py", "reveal.py"):
            text = (ROOT / "mediabrowser" / "gui" / name).read_text(encoding="utf-8")
            assert "hostenv.open_with_desktop(" in text, name

    def test_a_link_opens_with_xdg_open_in_the_host_environment(self, monkeypatch,
                                                                tmp_path):
        started = []
        monkeypatch.setattr(hostenv, "_start", lambda argv, env: started.append((argv, env)))
        monkeypatch.setattr(hostenv.shutil, "which", lambda name, path=None: "/usr/bin/" + name)
        monkeypatch.setenv(hostenv.KEYS, "LD_LIBRARY_PATH")
        monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/.mount_x/usr/lib/x11")
        assert hostenv.open_with_desktop("https://www.setlist.fm/") is True
        ((argv, env),) = started
        assert argv == ["/usr/bin/xdg-open", "https://www.setlist.fm/"]
        assert env == hostenv.host_env() and "LD_LIBRARY_PATH" not in env

    def test_with_no_xdg_open_qt_opens_it(self, monkeypatch):
        monkeypatch.setattr(hostenv.shutil, "which", lambda name, path=None: None)
        assert hostenv.open_with_desktop("https://www.setlist.fm/") is None

    def test_the_desktop_program_runs_apart_from_the_app(self, monkeypatch):
        seen = {}

        class FakeSubprocess:
            DEVNULL = subprocess.DEVNULL

            @staticmethod
            def Popen(argv, **kwargs):  # noqa: N802 - subprocess's name
                seen.update(kwargs, argv=argv)

        monkeypatch.setattr(hostenv, "subprocess", FakeSubprocess)
        REAL_START(["xdg-open", "x"], {"PATH": "/usr/bin"})
        assert seen["argv"] == ["xdg-open", "x"] and seen["env"] == {"PATH": "/usr/bin"}
        assert seen["start_new_session"] is True and seen["stdin"] == subprocess.DEVNULL
