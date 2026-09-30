#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
# Check that a commit's AppImage is reproducible: build it twice, from two
# fresh clones at different paths, and compare the two byte for byte.
#
#   packaging/appimage/verify-reproducible.sh [commit]    (default: HEAD)
#
# Anyone can run this against a published release to check that the
# AppImage offered for download is exactly what its source builds: the
# checksum printed at the end is the one to compare. Downloads are shared
# between the two builds (MCB_BUILD_CACHE) - they are checked by hash, so
# sharing them changes nothing.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
COMMIT="$(git -C "$REPO" rev-parse "${1:-HEAD}")"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/mcb-repro.XXXXXX")"
trap 'chmod -R u+w "$WORK" 2>/dev/null; rm -rf "$WORK"' EXIT
export MCB_BUILD_CACHE="${MCB_BUILD_CACHE:-$REPO/build/appimage/cache}"

build() {  # build <folder>
    git clone --quiet --no-hardlinks "$REPO" "$1"
    git -C "$1" checkout --quiet "$COMMIT"
    echo "==> building $COMMIT in $1" >&2
    "$1/packaging/appimage/build.sh" > "$1.log" 2>&1 || {
        echo "the build in $1 failed:" >&2
        tail -20 "$1.log" >&2
        exit 1
    }
    find "$1/dist" -name '*.AppImage'
}

first="$(build "$WORK/first")"
second="$(build "$WORK/a/second/place")"
sum_first="$(sha256sum < "$first" | cut -d' ' -f1)"
sum_second="$(sha256sum < "$second" | cut -d' ' -f1)"
echo
echo "$sum_first  first"
echo "$sum_second  second"
if [ "$sum_first" != "$sum_second" ]; then
    echo "NOT reproducible: the two builds differ" >&2
    if command -v unsquashfs >/dev/null; then
        # The squashfs starts after the runtime; unsquashfs finds it by offset.
        for build in first second; do
            file="$([ $build = first ] && echo "$first" || echo "$second")"
            offset="$(APPIMAGE_EXTRACT_AND_RUN=1 "$file" --appimage-offset)"
            unsquashfs -q -o "$offset" -d "$WORK/$build-files" "$file" >/dev/null
        done
        diff -rq "$WORK/first-files" "$WORK/second-files" >&2 || true
    fi
    exit 1
fi
echo "Reproducible: $(basename "$first") $sum_first"
