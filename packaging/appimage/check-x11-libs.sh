#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
# Check that the AppImage carries every X library Qt's xcb plugins need that a
# desktop without an X11 session may lack:
#
#   packaging/appimage/check-x11-libs.sh <AppDir>
#
# The app runs on X11 (XWayland on a Wayland desktop: app.prefer_x11), so Qt
# loads its xcb plugin, and if one library it links is missing it stops before
# any window, with a message naming another. A stock Ubuntu 26.04 GNOME, which
# has no X11 session, installs no libxcb-xkb1: 0.23.0's first build, carrying
# xkbcommon-x11 but not the libxcb-xkb it links, did not open there at all
# (lab, Nov 12 2026; SealedLore met the same in 3.1.0b4).
#
# What each of Qt's xcb plugins and libQt6XcbQpa needs, and what each X library
# the AppImage carries (usr/lib/x11) needs in turn, must be carried itself or be
# one of SYSTEM_X11: what that stock Ubuntu 26.04 GNOME has, the leanest
# desktop met so far. Run by build.sh; prints what is missing and fails.
set -euo pipefail

appdir="${1:?usage: check-x11-libs.sh <AppDir>}"
x11="$appdir/usr/lib/x11"
# X libraries a desktop with no X11 session still has (Ubuntu 26.04 GNOME, Nov
# 2026: libxcb1, libx11-6, libx11-xcb1 and the xcb extensions Mesa and GTK pull
# in), and the GL ones every desktop's Mesa brings.
SYSTEM_X11=(libxcb.so.1 libX11.so.6 libX11-xcb.so.1 libxcb-shm.so.0 libxcb-render.so.0
            libxcb-xfixes.so.0 libxcb-sync.so.1 libxcb-shape.so.0 libxcb-randr.so.0
            libxcb-glx.so.0 libxcb-present.so.0 libxcb-dri3.so.0 libGL.so.1 libEGL.so.1)

qt="$(find -L "$appdir" -type d -path '*/PySide6/Qt' -print -quit 2>/dev/null)"
if [ -z "$qt" ] || [ ! -d "$qt/plugins/platforms" ]; then
    echo "check-x11-libs: no PySide6/Qt with platform plugins in $appdir" >&2
    exit 1
fi
users=()
while IFS= read -r -d '' file; do
    users+=("$file")
done < <(find -L "$qt/plugins" "$qt/lib" -type f \( -name '*.so' -o -name 'libQt6XcbQpa.so*' \) \
             -print0 2>/dev/null)
xcb_users=()
for file in "${users[@]}"; do
    if grep -qa 'libxcb' "$file"; then
        xcb_users+=("$file")
    fi
done
if [ "${#xcb_users[@]}" -eq 0 ]; then
    echo "check-x11-libs: none of Qt's libraries links libxcb: is this an AppDir?" >&2
    exit 1
fi
if [ -d "$x11" ]; then
    while IFS= read -r -d '' file; do
        xcb_users+=("$file")
    done < <(find "$x11" -type f -name '*.so.*' -print0)
fi

# A file objdump can't read names nothing; one it can, and nothing X, leaves this empty.
needed="$(for file in "${xcb_users[@]}"; do
              objdump -p "$file" 2>/dev/null | awk '/NEEDED/ {print $2}' || true
          done | { grep -E '^lib(xcb|X11|xkbcommon|GL|EGL)' || true; } | sort -u)"
missing=()
for lib in $needed; do
    [ -e "$x11/$lib" ] && continue
    if printf '%s\n' "${SYSTEM_X11[@]}" | grep -qxF "$lib"; then
        continue
    fi
    missing+=("$lib")
done
if [ "${#missing[@]}" -gt 0 ]; then
    echo "Qt's xcb plugins need ${missing[*]}, which the AppImage doesn't carry and a" >&2
    echo "desktop with no X11 session may lack (Ubuntu 26.04 GNOME): it wouldn't open" >&2
    echo "there. Add its Debian 11 package to DEBIAN_LIBS in build.sh." >&2
    exit 1
fi
echo "  every X library Qt's xcb plugins need is carried or every desktop's ($(echo $needed | wc -w) in all)"
