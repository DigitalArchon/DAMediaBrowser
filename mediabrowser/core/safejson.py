# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""JSON from outside the app, read without letting its shape crash it.

What arrives from a server - the AI's replies, MusicBrainz, setlist.fm, the
attestation services - and every file someone opens (an imported catalog, a
settings file edited by hand) is parsed here. json.loads recurses into every
list and object: nested a thousand levels it raises RecursionError, which
nothing expects and which, raised on a worker, stopped it with a traceback in
place of the error the window says. Nested less deep than that, but past what
anything the app keeps holds, it was taken in, and a deep copy or a save of it
recursed too far later, far from where it came in.

So loads() refuses anything nested past MAX_NESTING levels - and anything
json.loads could not read for its depth - as a NestedTooDeep, a
json.JSONDecodeError, which every caller already takes for "that wasn't JSON".
As SealedLore's storage/atomic.nests_deeper and DA Assistant's (Nov 2026).
"""

from __future__ import annotations

import json

# Deeper than anything the app keeps, or reads from a service, nests: a
# library file is four or five levels, an attestation a dozen.
MAX_NESTING = 200


class NestedTooDeep(json.JSONDecodeError):
    """JSON nested past MAX_NESTING levels: refused, as JSON that couldn't be read."""

    def __init__(self):
        super().__init__(f"it is nested more than {MAX_NESTING} levels deep, deeper "
                         f"than anything this app reads", "", 0)

    def __str__(self):
        return self.msg         # no "line 1 column 1": the place says nothing here


def nests_deeper(data, limit: int = MAX_NESTING) -> bool:
    """Whether `data` (as json.loads made it) nests past `limit` levels.
    Walked, never recursed into."""
    stack = [(data, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            return True
        if isinstance(item, dict):
            children = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        stack.extend((child, depth + 1) for child in children
                     if isinstance(child, (dict, list)))
    return False


def loads(text):
    """json.loads(`text`) - str, bytes or bytearray - refusing JSON nested past
    MAX_NESTING levels, or too deep for json.loads to read at all, with
    NestedTooDeep (a json.JSONDecodeError)."""
    try:
        data = json.loads(text)
    except RecursionError:
        raise NestedTooDeep() from None
    if nests_deeper(data):
        raise NestedTooDeep()
    return data


def load(handle):
    """loads() of what an open file holds."""
    return loads(handle.read())
