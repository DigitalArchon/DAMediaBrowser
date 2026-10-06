# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Network shares, with the desktop's tools replaced by a fake runner."""

import pytest

from mediabrowser.core import network

SHARE = "smb://nas.local/media"
FUSE = "/run/user/1000/gvfs/smb-share:server=nas.local,share=media"

GIO_MOUNTS = """Drive(0): QEMU HARDDISK
  Type: GProxyDrive (GProxyVolumeMonitorUDisks2)
Mount(0): media on nas.local -> smb://nas.local/media/
  Type: GDaemonMount
Mount(1): thinclient_drives -> file:///home/user/thinclient_drives
"""


class FakeDesktop:
    """Answers gio and dbus-send the way a desktop would."""

    def __init__(self, connected=True, fuse=True, mount_error=None, kio_path=None):
        self.connected = connected
        self.fuse = fuse
        self.mount_error = mount_error
        self.kio_path = kio_path
        self.calls = []

    def __call__(self, args, timeout=None):
        self.calls.append(args)
        if args[:3] == ["gio", "mount", "-l"]:
            return 0, GIO_MOUNTS, ""
        if args[:2] == ["gio", "info"]:
            if not self.connected:
                return 1, "", "not mounted"
            lines = f"uri: {args[2]}\n"
            if self.fuse:
                rest = args[2][len(SHARE):]
                lines += f"local path: {FUSE}{rest}\n"
            return 0, lines, ""
        if args[:2] == ["gio", "mount"]:
            if self.mount_error:
                return 2, self.mount_error, ""
            self.connected = True
            return 0, "", ""
        if args[0] == "dbus-send" and "org.freedesktop.DBus.ListNames" in args[-1]:
            names = '"org.kde.KIOFuse"' if self.kio_path else ""
            return 0, f"array [ {names} ]", ""
        if args[0] == "dbus-send" and "ListActivatableNames" in args[-1]:
            return 0, "array [ ]", ""
        if args[0] == "dbus-send" and "mountUrl" in args[-2]:
            return 0, f'method return\n   string "{self.kio_path}"\n', ""
        return 1, "", "unexpected command"


@pytest.fixture
def gio_installed(monkeypatch):
    monkeypatch.setattr(network, "gvfs_present", lambda: True)
    monkeypatch.setattr(network, "gvfs_fuse_folder", lambda: "/run/user/1000/gvfs")


def always_a_folder(path):
    return True


class TestAddresses:
    def test_network_schemes_are_recognised(self):
        assert network.is_network_uri("smb://nas/media")
        assert network.is_network_uri("sftp://host/home")
        assert not network.is_network_uri("/home/user/Videos")
        assert not network.is_network_uri("file:///home/user")

    def test_a_trailing_slash_is_one_spelling(self):
        assert network.normalise("smb://nas/media/") == "smb://nas/media"

    def test_a_password_in_the_address_is_not_kept(self):
        # It would go into the library's file, every backup and every export.
        assert network.without_password("smb://jo:s3cret@nas/media") == "smb://jo@nas/media"
        assert network.normalise("smb://jo:s3cret@nas.local:445/media/") == (
            "smb://jo@nas.local:445/media"
        )
        assert network.normalise("smb://:s3cret@nas/media") == "smb://nas/media"
        assert network.without_password("smb://jo@nas/media") == "smb://jo@nas/media"
        assert network.display_name("smb://jo:s3cret@nas/media") == "media on nas"

    def test_display_names_read_naturally(self):
        name = network.display_name("smb://nas.local/media/Live%20Shows")
        assert name == "Live Shows on nas.local"
        assert network.display_name("smb://nas.local/") == "nas.local"

    def test_a_chosen_subfolder_gets_its_own_address(self):
        uri = network.subfolder_uri(SHARE, FUSE, f"{FUSE}/Live Shows/2024")
        assert uri == "smb://nas.local/media/Live%20Shows/2024"

    def test_choosing_the_share_itself_keeps_its_address(self):
        assert network.subfolder_uri(SHARE + "/", FUSE, FUSE) == SHARE

    def test_a_folder_outside_the_share_is_refused(self):
        with pytest.raises(ValueError):
            network.subfolder_uri(SHARE, FUSE, "/home/user/Videos")


class TestFindingShares:
    def test_connected_shares_come_from_gio(self, gio_installed):
        found = network.connected_locations(FakeDesktop())
        assert [(loc.name, loc.uri, loc.connected) for loc in found] == [
            ("media on nas.local", SHARE, True)
        ]

    def test_bookmarks_from_gtk_and_kde(self, tmp_path):
        gtk = tmp_path / ".config" / "gtk-3.0"
        gtk.mkdir(parents=True)
        (gtk / "bookmarks").write_text(
            "file:///home/user/Music\nsmb://nas.local/media/ NAS media\n", encoding="utf-8"
        )
        kde = tmp_path / ".local" / "share"
        kde.mkdir(parents=True)
        (kde / "user-places.xbel").write_text(
            '<?xml version="1.0"?><xbel>'
            '<bookmark href="file:///home/user"><title>Home</title></bookmark>'
            '<bookmark href="sftp://seedbox/downloads"><title>Seedbox</title></bookmark>'
            "</xbel>",
            encoding="utf-8",
        )
        found = network.bookmarked_locations(tmp_path)
        assert [(loc.name, loc.uri) for loc in found] == [
            ("NAS media", SHARE), ("Seedbox", "sftp://seedbox/downloads")
        ]

    def test_a_connected_bookmark_is_listed_once(self, gio_installed, tmp_path):
        gtk = tmp_path / ".config" / "gtk-3.0"
        gtk.mkdir(parents=True)
        (gtk / "bookmarks").write_text("smb://nas.local/media NAS\n", encoding="utf-8")
        found = network.known_locations(FakeDesktop(), home=tmp_path)
        assert len(found) == 1 and found[0].connected


class TestResolving:
    def test_a_connected_share_resolves_to_its_fuse_folder(self, gio_installed):
        assert network.resolve(SHARE, FakeDesktop(), always_a_folder) == FUSE

    def test_a_subfolder_resolves_inside_it(self, gio_installed):
        path = network.resolve(SHARE + "/Live", FakeDesktop(), always_a_folder)
        assert path == FUSE + "/Live"

    def test_a_share_not_yet_connected_is_connected_first(self, gio_installed):
        desktop = FakeDesktop(connected=False)
        assert network.resolve(SHARE, desktop, always_a_folder) == FUSE
        assert ["gio", "mount", SHARE] in desktop.calls

    def test_a_password_prompt_is_explained(self, gio_installed):
        desktop = FakeDesktop(
            connected=False, mount_error="Authentication Required\nUser [user]: "
        )
        with pytest.raises(network.NetworkError, match="user name and password"):
            network.resolve(SHARE, desktop, always_a_folder)

    def test_without_a_bridge_it_says_what_to_install(self, monkeypatch):
        monkeypatch.setattr(network, "gvfs_present", lambda: True)
        monkeypatch.setattr(network, "gvfs_fuse_folder", lambda: None)
        with pytest.raises(network.NetworkError, match="gvfs-fuse") as error:
            network.resolve(SHARE, FakeDesktop(fuse=False), always_a_folder)
        assert "kio-fuse" in str(error.value)

    def test_kde_is_asked_through_kio_fuse(self, monkeypatch):
        monkeypatch.setattr(network, "gvfs_present", lambda: False)
        kio = "/run/user/1000/kio-fuse-AbCdEf/smb/nas.local/media"
        assert network.resolve(SHARE, FakeDesktop(kio_path=kio), always_a_folder) == kio

    def test_an_ordinary_path_is_not_an_address(self):
        with pytest.raises(network.NetworkError):
            network.resolve("/home/user/Videos", FakeDesktop(), always_a_folder)
