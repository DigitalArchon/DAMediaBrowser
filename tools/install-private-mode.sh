#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Install the client that Privacy Mode's end-to-end encrypted models need, for running
# DA Media Browser from source. The AppImage already carries it.
#
# nano-gpt's private/* models are sealed on this machine to their attested enclave with
# Tinfoil's own SDK (mediabrowser/core/sealed.py). No distribution packages it, and what
# it pulls in - cryptography, requests, urllib3 - is newer than distributions ship, so it
# goes into a directory of its own that the app puts first on its path
# (mediabrowser/core/vendored.py):
#
#   ${XDG_DATA_HOME:-~/.local/share}/da-media-browser/private-mode
#
# Only what packaging/private-mode.lock names is installed, only as prebuilt wheels, and
# only if every file matches its pinned sha256. Nothing is installed system-wide, and
# nothing from nano-gpt is involved. TEE/ models need none of this.
#
# Usage:
#   tools/install-private-mode.sh            install, or reinstall from the current lock
#   tools/install-private-mode.sh --remove   delete it again
#
# Restart the app afterwards: the directory is put on the path at startup.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="$ROOT/packaging/private-mode.lock"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/da-media-browser/private-mode"
PYTHON="${PYTHON:-python3}"

if [ "${1-}" = "--remove" ]; then
  rm -rf "$DEST"
  echo "Removed $DEST"
  exit 0
fi
if [ $# -gt 0 ]; then
  sed -n '5,24p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
  exit 2
fi

[ -f "$LOCK" ] || { echo "Missing $LOCK" >&2; exit 1; }
if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
  echo "pip is needed to install from the lock: sudo apt install python3-pip" >&2
  exit 1
fi
version="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if [ "$version" != "3.12" ]; then
  echo "Note: the lock was made for Python 3.12 and this is $version; if a wheel is" >&2
  echo "missing for it, pip will say so and nothing will be installed." >&2
fi

# Into a fresh directory beside the old one, then swapped in: a failed or interrupted
# install leaves whatever was there before untouched.
staging="$DEST.new"
rm -rf "$staging"
mkdir -p "$(dirname "$DEST")"
trap 'rm -rf "$staging"' EXIT

echo "Installing $(grep -c '==' "$LOCK") pinned packages into $DEST"
"$PYTHON" -m pip install --quiet --disable-pip-version-check --no-input \
  --require-hashes --only-binary :all: --no-deps --no-compile \
  --target "$staging" -r "$LOCK"

rm -rf "$DEST"
mv "$staging" "$DEST"
trap - EXIT

# The same check the app makes (sealed.available), in a fresh interpreter. The checkout's
# folder is handed to it as an argument, not written into its code, so a folder named
# with a ' in it can't break the check or run what follows it as Python.
if "$PYTHON" -c '
import sys; sys.path.insert(0, sys.argv[1])
import mediabrowser
from mediabrowser.core import sealed, vendored
ok, message = sealed.available()
print(("ok: " if ok else "FAILED: ") + message + " from " + str(vendored.location()))
sys.exit(0 if ok else 1)' "$ROOT"; then
  echo "Restart DA Media Browser to use end-to-end encrypted models."
else
  exit 1
fi
