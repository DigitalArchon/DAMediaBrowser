# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cancelling a scan and adding a network folder, through the real window
offscreen. No network is touched: resolving an address is faked.
"""

import threading
import time

import pytest

from mediabrowser.core import config, library, network, scanner, store

PySide6 = pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    from mediabrowser.gui.app import build_app

    existing = QApplication.instance()
    yield existing or build_app(["mediabrowser"])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    from mediabrowser.core import artwork
    from mediabrowser.gui.main_window import MainWindow

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LIBRARIES_DIR", tmp_path / "data" / "libraries")
    monkeypatch.setattr(config, "ARTWORK_DIR", tmp_path / "data" / "artwork")
    monkeypatch.setattr(config, "LEGACY_LIBRARY_FILE", tmp_path / "data" / "library.json")
    monkeypatch.setattr(config, "APP_SETTINGS_FILE", tmp_path / "data" / "settings.json")
    monkeypatch.setattr(artwork, "is_resolved", lambda video_id: True)
    w = MainWindow()
    yield w
    w.player.stop()
    w.close()


def pump(app, until, timeout=10.0):
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    return until()


@pytest.fixture
def endless_walk(monkeypatch):
    """A folder walk that goes on until cancelled, like a whole drive."""
    started = threading.Event()

    def walk(root, cancel=None, on_folder=None):
        started.set()
        while True:
            if cancel is not None and cancel.is_set():
                raise scanner.ScanCancelled()
            if on_folder:
                on_folder(root)
            time.sleep(0.01)
            yield from ()

    monkeypatch.setattr(scanner, "find_media_units", walk)
    return started


class TestCancellingAScan:
    def test_cancel_shows_only_while_scanning(self, app, window, tmp_path, endless_walk):
        assert window.cancel_scan_button.isHidden()
        window.rescan(str(tmp_path))
        assert not window.cancel_scan_button.isHidden()
        assert window.cancel_scan_action.isEnabled()
        window.cancel_scan()
        assert pump(app, lambda: not window._scanning)
        assert window.cancel_scan_button.isHidden()
        assert not window.cancel_scan_action.isEnabled()

    def test_a_cancelled_first_scan_adds_no_library(self, app, window, tmp_path, endless_walk):
        window.rescan(str(tmp_path))
        assert endless_walk.wait(5)
        window.cancel_scan()
        assert pump(app, lambda: not window._scanning)
        assert store.list_libraries() == []
        assert "cancelled" in window.status_label.text()

    def test_scanning_can_start_again_after(self, app, window, tmp_path, endless_walk):
        window.rescan(str(tmp_path))
        window.cancel_scan()
        assert pump(app, lambda: not window._scanning)
        assert window.rescan_action.isEnabled() and window.choose_action.isEnabled()


class TestAddingANetworkFolder:
    def test_a_chosen_share_folder_becomes_a_library(self, app, window, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QFileDialog

        from mediabrowser.gui.dialogs.network_dialog import NetworkFolderDialog

        share = tmp_path / "gvfs" / "smb-share:server=nas,share=media"
        (share / "Live Shows").mkdir(parents=True)
        monkeypatch.setattr(network, "known_locations", lambda: [
            network.Location("media on nas", "smb://nas/media", True)
        ])
        monkeypatch.setattr(network, "resolve", lambda uri: str(share))
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory", lambda *a, **k: str(share / "Live Shows")
        )

        dialog = NetworkFolderDialog(window)
        dialog.locations.setCurrentRow(0)
        assert dialog.address.text() == "smb://nas/media"
        dialog.connect_to()
        assert pump(app, lambda: dialog.result_folder() is not None)
        path, uri = dialog.result_folder()
        assert uri == "smb://nas/media/Live%20Shows"

        window.rescan(path, network_uri=uri)
        assert pump(app, lambda: not window._scanning)
        assert library.network_uri_for(path) == uri
        item = window.libraries.list.item(0)
        assert item.text().startswith("Live Shows on nas")

    def test_a_failure_is_shown_in_the_dialog(self, app, window, monkeypatch):
        from mediabrowser.gui.dialogs.network_dialog import NetworkFolderDialog

        monkeypatch.setattr(network, "known_locations", lambda: [])

        def fail(uri):
            raise network.NetworkError("Couldn't connect to smb://nas/media: no route")

        monkeypatch.setattr(network, "resolve", fail)
        dialog = NetworkFolderDialog(window)
        dialog.address.setText("smb://nas/media")
        dialog.connect_to()
        assert pump(app, lambda: "no route" in dialog.status.text())
        assert dialog.connect_button.isEnabled()

    def test_an_ordinary_path_is_turned_away(self, app, window, monkeypatch):
        from mediabrowser.gui.dialogs.network_dialog import NetworkFolderDialog

        monkeypatch.setattr(network, "known_locations", lambda: [])
        dialog = NetworkFolderDialog(window)
        dialog.address.setText("/home/user/Videos")
        dialog.connect_to()
        assert "isn't a network address" in dialog.status.text()

    def test_a_dropped_share_is_reconnected_on_rescan(self, app, window, tmp_path, monkeypatch):
        share = tmp_path / "share"
        share.mkdir()
        library.rescan(share, network_uri="smb://nas/media")
        share.rename(tmp_path / "away")
        calls = []

        def resolve(uri):
            calls.append(uri)
            (tmp_path / "away").rename(share)
            return str(share)

        monkeypatch.setattr(network, "resolve", resolve)
        window.rescan(str(share))
        assert pump(app, lambda: not window._scanning)
        assert calls == ["smb://nas/media"]
        assert window.data["settings"]["library_root"] == str(share.resolve())
