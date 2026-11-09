#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Pin Privacy Mode's end-to-end encryption client, and everything it pulls in, by
# version and hash:  packaging/private-mode.in  ->  packaging/private-mode.lock
#
# Run after changing private-mode.in, and commit the lock with it. Both the AppImage
# build and tools/install-private-mode.sh install only what the lock names, and only if
# every hash matches.
#
#   packaging/lock-private-mode.sh             lock what private-mode.in adds or changes
#   UPGRADE=1 packaging/lock-private-mode.sh   re-resolve everything to today's newest
#
# The resolver is uv at a fixed release, checked against its published sha256 before
# it runs - the same one DA Assistant and StoryLoom lock the same SDK with.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="${BUILD_DIR:-$ROOT/build}/cache"

UV_VERSION="0.13.5"
UV_URL="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz"
UV_SHA256="f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f"

archive="$CACHE/uv-$UV_VERSION.tar.gz"
mkdir -p "$CACHE"
if [ ! -f "$archive" ]; then
  curl --proto '=https' --tlsv1.2 -fL --retry 3 -o "$archive.part" "$UV_URL"
  mv "$archive.part" "$archive"
fi
if ! echo "$UV_SHA256  $archive" | sha256sum --check --status; then
  rm -f "$archive"
  echo "uv $UV_VERSION does not match its checksum; not running it." >&2
  exit 1
fi
UV="$CACHE/uv-$UV_VERSION/uv"
if [ ! -x "$UV" ]; then
  mkdir -p "$CACHE/uv-$UV_VERSION"
  tar -xzf "$archive" -C "$CACHE/uv-$UV_VERSION" --strip-components=1
fi

# Nothing from this machine's uv or pip settings: no other index, no other cache.
# UV_NO_CONFIG skips only uv's config files; uv reads UV_INDEX_URL, UV_DEFAULT_INDEX,
# UV_FIND_LINKS, UV_INSECURE_HOST and the rest from the environment as well, so every UV_
# variable is cleared first. (pip's settings uv doesn't read at all.)
unset "${!UV_@}"
export UV_NO_CONFIG=1 UV_CACHE_DIR="$CACHE/uv" UV_PYTHON_DOWNLOADS=never

# uv keeps the versions a lock it writes over already pins, so a package added to
# private-mode.in is locked without moving everything else to whatever is newest today.
# UPGRADE=1 re-resolves the lot.
upgrade=""
if [ "${UPGRADE:-0}" = 1 ]; then
  upgrade="--upgrade"
fi

# Python 3.12 is what the AppImage bundles and what Ubuntu 24.04 runs from source;
# manylinux_2_28 keeps the compiled wheels (cryptography, pydantic-core) loadable on any
# host the rest of the bundle runs on.
# What the AppImage's own lock pins too is pinned to the same version here. The AppImage
# installs both into one site-packages, so a version of its own here would replace the
# one appimage/requirements.txt names (DA Assistant shipped cryptography 50.0.1 in place
# of 50.0.2 that way, in its 3.4.0). The AppImage build stops if the two ever disagree.
# (Named by its path relative to here, which is what the lock's "via" lines then say.)
cd "$ROOT/packaging"
"$UV" pip compile --quiet private-mode.in -o private-mode.lock \
  --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
  --only-binary :all: --generate-hashes --constraint appimage/requirements.txt \
  --custom-compile-command "packaging/lock-private-mode.sh" ${upgrade:+"$upgrade"}
echo "Locked $(grep -c '==' private-mode.lock) packages into packaging/private-mode.lock"
