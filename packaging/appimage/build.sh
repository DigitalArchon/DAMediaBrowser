#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
# Build Media Chapter Browser as a single-file AppImage:
#   dist/MediaChapterBrowser-<version>-x86_64.AppImage
#
# Runs on any x86_64 Linux with bash, curl, dpkg-deb and sha256sum. Downloads
# (HTTPS only, each checked where a checksum can be pinned) are cached in
# build/appimage/cache, so a rebuild after a code change is quick.
#
# What goes in:
#   - a relocatable CPython 3.12 built for glibc 2.28 (python-appimage). The
#     floor that matters is PySide6's: 6.10 and later are built for glibc
#     2.34, so the result runs on Ubuntu 22.04 / Debian 12 / Fedora 35 /
#     RHEL 9 and newer;
#   - the app and PySide6-Essentials, pinned to the version this repo is
#     tested with. Essentials rather than the full PySide6: the add-ons are
#     hundreds of megabytes this app never imports;
#   - the X11 helper libraries Qt's xcb plugin needs and many systems lack,
#     taken from Debian 11 (built for glibc <= 2.17) rather than from
#     whatever machine runs this script - a host's copy can need a newer
#     glibc than the systems the AppImage is for (Ubuntu 24.04's
#     libxcb-cursor needs 2.38).
#
# What does NOT go in: ffmpeg, ffprobe, mpv and libbluray. See AppRun.
#
# The build is reproducible: the same commit gives the same AppImage, byte
# for byte, whoever builds it and wherever the checkout is. Everything that
# goes in is pinned by checksum (the Python base, every wheel, the Debian
# libraries, appimagetool and the AppImage runtime); every file's time is the
# last commit's (SOURCE_DATE_EPOCH) and its owner root; nothing inside names
# the folder it was built in. verify-reproducible.sh builds a commit twice,
# in two different places, and compares.
set -euo pipefail
umask 022

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
BUILD="$REPO/build/appimage"
# Downloads only: sharing one between checkouts changes nothing in the result.
CACHE="${MCB_BUILD_CACHE:-$BUILD/cache}"
APPDIR="$BUILD/MediaChapterBrowser.AppDir"
DIST="$REPO/dist"

VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO/pyproject.toml" | head -1)"
OUTPUT="$DIST/MediaChapterBrowser-$VERSION-x86_64.AppImage"

PYTHON_APPIMAGE="python3.12.14-cp312-cp312-manylinux_2_28_x86_64.AppImage"
PYTHON_URL="https://github.com/niess/python-appimage/releases/download/python3.12/$PYTHON_APPIMAGE"
PYTHON_SHA256="cefdd1b6e08dfb6c977d4233a4177ed3ee55a526991a122c8d92263f5901544f"
# A tagged release of each, never "continuous": that moves under a build.
# appimagetool carries its own mksquashfs, so the host's doesn't matter.
APPIMAGETOOL="appimagetool-1.9.1-x86_64.AppImage"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
RUNTIME="runtime-20251108-x86_64"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64"
RUNTIME_SHA256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

# PySide6 and what builds the app's package, each by version and hash.
REQUIREMENTS="$HERE/requirements.txt"
BUILD_REQUIREMENTS="$HERE/build-requirements.txt"

DEBIAN_POOL="https://deb.debian.org/debian/pool/main"
DEBIAN_LIBS=(
    # path in the pool                                              sha256
    "x/xcb-util-cursor/libxcb-cursor0_0.1.1-4_amd64.deb             bab731cd0143303f77461dd0a03ad20807bd3d767d5d6af6a11ff99c3b7aeac3"
    "x/xcb-util-wm/libxcb-icccm4_0.4.1-1.1_amd64.deb                f323194cb04cd4e5ae064fafec39db6dcf8a431cbd65a0bc53fa6c359862d8ff"
    "x/xcb-util-image/libxcb-image0_0.4.0-1+b3_amd64.deb            36a381bb18c9f349a53457c66b3d1825631f3e50c4f6c12326cf05257302172d"
    "x/xcb-util-keysyms/libxcb-keysyms1_0.4.0-1+b2_amd64.deb        aed1436db9a3e63b10d00c4ed16248b5c82b5dd2963a83a761f406af65eb4b49"
    "x/xcb-util-renderutil/libxcb-render-util0_0.3.9-1+b1_amd64.deb be4b38a63e65c84e2f1322f044d05a9baa677e0f3dc68b742a0a109a3ff40ae9"
    "x/xcb-util/libxcb-util1_0.4.0-1+b1_amd64.deb                   4c48af51fb2ac1be0490067e7450aeda27bf6c6c395165de02199eee4835336f"
    # Qt calls only xkbcommon's 0.5-era API, all present in 1.0.3; bundling the
    # pair keeps xkbcommon-x11 matched to the xkbcommon it was built with.
    "libx/libxkbcommon/libxkbcommon0_1.0.3-2_amd64.deb              d74d0b9f0a6641b44c279644c7ac627fa7a9b92350b7c6ff37da94352885bcfc"
    "libx/libxkbcommon/libxkbcommon-x11-0_1.0.3-2_amd64.deb         c786f80d1a5405e96167ebbebdd7d100b356c0a3ae0f87fb6f65f05f2e723f72"
)

# The oldest glibc the AppImage runs on; anything inside needing more fails the build.
GLIBC_FLOOR="2.34"

log() { printf '\n==> %s\n' "$*"; }

# Every time inside is the last commit's; outside a git checkout (a source
# tarball) it must be given.
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
    SOURCE_DATE_EPOCH="$(git -C "$REPO" log -1 --format=%ct 2>/dev/null)" || {
        echo "not a git checkout: set SOURCE_DATE_EPOCH" >&2
        exit 1
    }
fi
export SOURCE_DATE_EPOCH
# Python's own sources of variation: set iteration order (which reaches
# marshalled code), and .pyc files written by whatever the build imports.
export PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1
# mksquashfs reads SOURCE_DATE_EPOCH itself, for the image's time and every
# file's. C.UTF-8 sorts as C does, and Qt wants UTF-8.
export TZ=UTC LC_ALL=C.UTF-8

fetch() {  # fetch <url> <file> [sha256]
    local url="$1" file="$2" sum="${3:-}"
    if [ ! -f "$file" ]; then
        curl --proto '=https' --tlsv1.2 -fL --retry 3 -o "$file.part" "$url"
        mv "$file.part" "$file"
    fi
    if [ -n "$sum" ] && ! echo "$sum  $file" | sha256sum -c --quiet -; then
        echo "checksum mismatch for $file (from $url)" >&2
        echo "the upstream file changed; check it and update the pinned sum" >&2
        rm -f "$file"
        exit 1
    fi
}

mkdir -p "$BUILD" "$CACHE" "$DIST"
rm -rf "$APPDIR"

log "Python base"
fetch "$PYTHON_URL" "$CACHE/$PYTHON_APPIMAGE" "$PYTHON_SHA256"
chmod +x "$CACHE/$PYTHON_APPIMAGE"
# --appimage-extract needs no FUSE, so this works in containers too.
(cd "$BUILD" && rm -rf squashfs-root && "$CACHE/$PYTHON_APPIMAGE" --appimage-extract >/dev/null)
mv "$BUILD/squashfs-root" "$APPDIR"
# Its own launcher, desktop entry and icon give way to this app's.
rm -f "$APPDIR"/AppRun "$APPDIR"/*.desktop "$APPDIR"/*.png "$APPDIR"/.DirIcon
rm -rf "$APPDIR/usr/share/applications" "$APPDIR/usr/share/metainfo" "$APPDIR/usr/share/icons"
PYTHON="$APPDIR/opt/python3.12/bin/python3.12"

log "The app and PySide6"
export PYTHONNOUSERSITE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
unset PYTHONHOME PYTHONPATH
# --no-compile: pip's .pyc files would record where they were built.
PIP=("$PYTHON" -s -m pip install --quiet --no-warn-script-location --no-compile
     --cache-dir "$CACHE/pip")
"${PIP[@]}" --require-hashes --only-binary=:all: --no-deps -r "$REQUIREMENTS"
"${PIP[@]}" --require-hashes --only-binary=:all: --no-deps -r "$BUILD_REQUIREMENTS"
# setuptools builds in the repo's own build/lib and *.egg-info and never
# clears them, so a module deleted from the source would otherwise ride
# along into every AppImage after it. They are only ever scratch.
rm -rf "$REPO/build/lib" "$REPO"/build/bdist.* "$REPO"/*.egg-info
# With the pinned setuptools above, not whatever an isolated build fetches.
"${PIP[@]}" --no-deps --no-build-isolation "$REPO"
rm -rf "$REPO/build/lib" "$REPO"/build/bdist.* "$REPO"/*.egg-info
"$PYTHON" -s -c "import mediabrowser.gui.app, PySide6.QtWidgets"
# The installed package must be exactly the source's modules, no more.
INSTALLED="$("$PYTHON" -s -c 'import mediabrowser, os; print(os.path.dirname(mediabrowser.__file__))')"
if ! diff <(cd "$REPO/mediabrowser" && find . -name '*.py' | sort) \
          <(cd "$INSTALLED" && find . -name '*.py' | sort) >/dev/null; then
    echo "the installed package doesn't match the source's modules:" >&2
    diff <(cd "$REPO/mediabrowser" && find . -name '*.py' | sort) \
         <(cd "$INSTALLED" && find . -name '*.py' | sort) >&2 || true
    exit 1
fi

log "Trimming what the app never loads"
SITE="$("$PYTHON" -s -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
QT="$SITE/PySide6"
# Qt's developer tools and headers: designer, linguist, the QML tooling.
rm -rf "$QT"/{include,typesystems,glue,scripts,examples,doc} \
       "$QT"/{assistant,designer,linguist,lrelease,lupdate,qmlformat,qmllint,qmlls,balsam,balsamui} \
       "$QT"/Qt/libexec "$QT"/Qt/plugins/designer "$QT"/Qt/plugins/qmltooling
# QML and Quick: a Qt Widgets app never loads any of it, and it is about a
# third of what PySide6-Essentials weighs. The smoke test below is what makes
# removing this safe to keep doing - it builds the real window, so anything
# that turns out to need these fails the build rather than the user.
rm -rf "$QT/Qt/qml" "$QT/Qt/plugins/scenegraph" "$QT/Qt/plugins/qmllint"
rm -f "$QT"/Qt/lib/libQt6Qml*.so.6* "$QT"/Qt/lib/libQt6Quick*.so.6* \
      "$QT"/Qt/lib/libQt6Labs*.so.6* \
      "$QT"/QtQml*.abi3.so "$QT"/QtQuick*.abi3.so
"$PYTHON" -s -m pip uninstall --quiet --yes pip setuptools wheel 2>/dev/null || true

log "Leaving out where it was built"
# pip's scripts name the build's own interpreter - on the #! line, or (for a
# path too long for one) on the line after a #!/bin/sh. pyside6-uic and the
# rest: the app never runs them, and they couldn't run from the AppImage
# anyway. Out they go, with their lines in each package's RECORD, and so
# does the record of the folder the app was installed from.
BIN="$APPDIR/opt/python3.12/bin"
for script in "$BIN"/*; do
    if [ -f "$script" ] && [ ! -L "$script" ] && head -c 2 "$script" | grep -q '#!' \
            && grep -qF "$APPDIR" "$script"; then
        rm -f "$script"
        name="$(basename "$script")"
        sed -i "\|/bin/$name,|d" "$SITE"/*.dist-info/RECORD
    fi
done
for record in "$SITE"/*.dist-info/direct_url.json; do
    [ -e "$record" ] || continue
    sed -i '\|direct_url.json,|d' "$(dirname "$record")/RECORD"
    rm -f "$record"
done
find "$APPDIR" -name "__pycache__" -type d -prune -exec rm -rf {} +
# Hash-checked rather than timestamped: a .pyc stays valid whatever time its
# source is given below, and names no build folder (-s/-p).
"$PYTHON" -s -m compileall -q --invalidation-mode unchecked-hash \
    -s "$APPDIR" -p / "$SITE/mediabrowser" >/dev/null

log "Smoke test"
# Builds the actual window offscreen, with its own data directory so the
# build neither reads nor writes the library of whoever is running it. An
# import check alone would miss a trimmed Qt library or a stylesheet left
# out of the package data - both of which look fine until the app is opened.
SMOKE_HOME="$BUILD/smoke-home"
rm -rf "$SMOKE_HOME" && mkdir -p "$SMOKE_HOME"
QT_QPA_PLATFORM=offscreen XDG_DATA_HOME="$SMOKE_HOME" \
LD_LIBRARY_PATH="$APPDIR/usr/lib/x11" "$PYTHON" -s -c "
from mediabrowser.gui.app import build_app, load_stylesheet
from mediabrowser.gui.main_window import MainWindow

assert len(load_stylesheet()) > 1000, 'style.qss missing from the build'
app = build_app(['mediabrowser'])
window = MainWindow()
window.show()
window.refresh_library()
window.set_view(1)
window.search.setText('x')
window.refresh_library()
window.player.stop()
print('  window builds, both views render, search runs')
"
rm -rf "$SMOKE_HOME"

log "X11 helper libraries (Debian 11)"
# Their own folder, the only one AppRun puts on LD_LIBRARY_PATH. usr/lib holds
# the Python base's libraries (an old liblzma, libtinfo, OpenSSL 1.1), which
# Python finds through its RUNPATH; exported, they would override the system's
# copies for everything Qt loads, libsystemd included.
X11LIB="$APPDIR/usr/lib/x11"
mkdir -p "$X11LIB" "$CACHE/debs"
for entry in "${DEBIAN_LIBS[@]}"; do
    read -r path sum <<<"$entry"
    deb="$CACHE/debs/$(basename "$path")"
    fetch "$DEBIAN_POOL/$path" "$deb" "$sum"
    rm -rf "$CACHE/debs/x" && dpkg-deb -x "$deb" "$CACHE/debs/x"
    cp -a "$CACHE"/debs/x/usr/lib/x86_64-linux-gnu/*.so.* "$X11LIB/"
done
rm -rf "$CACHE/debs/x"

log "Launcher, desktop entry, icon"
install -m 755 "$HERE/AppRun" "$APPDIR/AppRun"
cp "$REPO/packaging/mediabrowser.svg" "$APPDIR/mediabrowser.svg"
mkdir -p "$APPDIR/usr/share/doc/mediabrowser"
cp "$REPO/LICENSE" "$APPDIR/usr/share/doc/mediabrowser/LICENSE"
ln -sf mediabrowser.svg "$APPDIR/.DirIcon"
mkdir -p "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp "$REPO/packaging/mediabrowser.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/mediabrowser.svg"
# The repo's entry, minus its template comments, with the AppImage's own
# launcher and an icon. Integration tools rewrite Exec to the AppImage's path.
grep -v '^#' "$REPO/mediabrowser.desktop" \
    | sed 's|^Exec=.*|Exec=mediabrowser|' > "$APPDIR/mediabrowser.desktop"
echo "Icon=mediabrowser" >> "$APPDIR/mediabrowser.desktop"
# What AppImage managers (Gear Lever, AppImageLauncher) show as the version;
# the filename alone is lost as soon as they rename the file on integration.
echo "X-AppImage-Version=$VERSION" >> "$APPDIR/mediabrowser.desktop"

log "Checking the result"
# Every ELF inside must run on the glibc floor.
floor="$(find "$APPDIR" -type f \( -name '*.so*' -o -perm -u+x \) -exec sh -c \
    'file -b "$1" | grep -q ELF && objdump -T "$1" 2>/dev/null' _ {} \; \
    | grep -o 'GLIBC_[0-9][0-9.]*' | sed 's/GLIBC_//' | sort -uV | tail -1)"
echo "highest glibc symbol needed: $floor"
if [ "$(printf '%s\n%s\n' "$floor" "$GLIBC_FLOOR" | sort -V | tail -1)" != "$GLIBC_FLOOR" ]; then
    echo "something inside needs glibc $floor, above the $GLIBC_FLOOR floor" >&2
    exit 1
fi
# The xcb plugin's libraries must all resolve from the AppImage or the core X
# libraries every X11 desktop has.
missing="$(LD_LIBRARY_PATH="$X11LIB" ldd "$QT/Qt/plugins/platforms/libqxcb.so" | grep 'not found' || true)"
if [ -n "$missing" ]; then
    echo "the xcb plugin can't resolve: $missing" >&2
    exit 1
fi

# The bundled OpenSSL finds no CA certificates by itself (its compiled-in
# paths belong to the distribution it was built on), so AppRun points it at
# the host's trust store. Without that, MusicBrainz search and cover art both
# fail with "unable to get local issuer certificate" - and only once the app
# is running, which is how it got shipped that way the first time.
#
# This runs AppRun's own probe rather than a copy of it: the launcher is
# copied with its last line swapped for the check below, so the two cannot
# drift apart.
CA_PY="$BUILD/ca_check.py"
cat > "$CA_PY" <<'CACHECK'
import ssl
import sys

count = len(ssl.create_default_context().get_ca_certs())
print("  %d CA certificates visible to the bundled Python" % count)
sys.exit(0 if count else 1)
CACHECK
CA_CHECK="$APPDIR/.ca-check.sh"
sed "s|^exec .*|exec \\"\\$PYTHON\\" -s '$CA_PY'|" "$APPDIR/AppRun" > "$CA_CHECK"
chmod +x "$CA_CHECK"
if ! "$CA_CHECK"; then
    echo "the bundled Python trusts no certificate authorities" >&2
    echo "AppRun's CA probe found no readable trust store on this machine" >&2
    rm -f "$CA_CHECK" "$CA_PY"
    exit 1
fi
rm -f "$CA_CHECK" "$CA_PY"

log "Packing"
if leaked="$(grep -rlF "$REPO" "$APPDIR")"; then
    echo "these name the folder the build ran in:" >&2
    echo "$leaked" >&2
    exit 1
fi
# One time, and one set of permissions, for everything.
chmod -R u+rw,go-w,a+rX "$APPDIR"
find "$APPDIR" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
fetch "$APPIMAGETOOL_URL" "$CACHE/$APPIMAGETOOL" "$APPIMAGETOOL_SHA256"
fetch "$RUNTIME_URL" "$CACHE/$RUNTIME" "$RUNTIME_SHA256"
chmod +x "$CACHE/$APPIMAGETOOL"
rm -f "$OUTPUT"
ARCH=x86_64 VERSION="$VERSION" APPIMAGE_EXTRACT_AND_RUN=1 "$CACHE/$APPIMAGETOOL" \
    --no-appstream --runtime-file "$CACHE/$RUNTIME" \
    --mksquashfs-opt -all-root --mksquashfs-opt -no-xattrs \
    "$APPDIR" "$OUTPUT" >/dev/null
touch -d "@$SOURCE_DATE_EPOCH" "$OUTPUT"
(cd "$DIST" && sha256sum "$(basename "$OUTPUT")" > "$(basename "$OUTPUT").sha256")
echo
echo "Built $OUTPUT ($(du -h "$OUTPUT" | cut -f1))"
cat "$OUTPUT.sha256"
