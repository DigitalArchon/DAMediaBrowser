# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""What the app has sent, where, and how, since it started.

Settings → Privacy says what each service may be sent; this says what it
was sent. Every request that carries anything about the library - a
MusicBrainz search, a cover asked for, a setlist looked up, a question for
the AI - goes on this list as it is made: which service, which address, what
it carried in words, how it travelled (in the clear over https, to an
attested enclave, sealed end to end), and whether it went. An attestation
is listed too, though it carries nothing about the videos: it is where
Privacy Mode's checks went.

Help → What Was Sent shows it. It is kept in memory only, never written to
disk, and is gone when the app closes: it names the videos, as the requests
did. What the AI was sent is kept as its text, the frames left out.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlparse

# Enough for a long Identify Library run; the oldest go first.
MAX_ENTRIES = 1000
# What of a request's text is kept for the window: the AI's prompts are a
# few thousand characters, the rest far less.
MAX_CONTENT = 20_000

# How a request travelled.
CLEAR = "clear"         # https: the service, and whoever it passes it to, can read it
TEE = "tee"             # to an attested enclave, through a gateway that can read it
SEALED = "sealed"       # sealed end to end: only the attested enclave can read it
CHECK = "check"         # an attestation: nothing about the videos

# What became of it.
SENT = "sent"
REFUSED = "refused"     # nothing was sent: an address, a check or a switch said no
FAILED = "failed"       # sent, or begun, and the service or the network failed


@dataclass(frozen=True)
class Entry:
    when: float
    service: str        # "MusicBrainz", "Nano-GPT", ...
    host: str
    what: str           # what it carried, in words
    protection: str     # CLEAR, TEE, SEALED or CHECK
    how: str            # how it travelled, in words
    outcome: str        # SENT, REFUSED or FAILED
    detail: str = ""    # why it was refused or failed; what an enclave proved
    purpose: str = ""   # what it was for, as the window doing it said
    content: str = ""   # the text sent, for the AI


_lock = threading.Lock()
_entries: deque = deque(maxlen=MAX_ENTRIES)
_listeners: list = []
_local = threading.local()


@contextmanager
def purpose(text: str):
    """Requests made on this thread meanwhile were for `text` ("Detect
    Chapters: Glass Harbor - Live at Copperfield Hall")."""
    stack = getattr(_local, "purposes", None)
    if stack is None:
        stack = _local.purposes = []
    stack.append(text)
    try:
        yield
    finally:
        stack.pop()


def current_purpose() -> str:
    stack = getattr(_local, "purposes", None)
    return stack[-1] if stack else ""


def host_of(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def record(service: str, url: str, what: str, protection: str, how: str,
           outcome: str = SENT, detail: str = "", content: str = "") -> Entry:
    """Put one request on the list, and tell whoever is listening."""
    entry = Entry(
        when=time.time(), service=service, host=host_of(url), what=what,
        protection=protection, how=how, outcome=outcome, detail=detail,
        purpose=current_purpose(), content=content[:MAX_CONTENT],
    )
    with _lock:
        _entries.append(entry)
        listeners = list(_listeners)
    for listener in listeners:
        try:
            listener(entry)
        except Exception:  # noqa: BLE001 - a window gone must not stop a request
            pass
    return entry


def entries() -> list[Entry]:
    with _lock:
        return list(_entries)


def clear() -> None:
    with _lock:
        _entries.clear()


def add_listener(listener) -> None:
    """`listener(entry)` is called from whichever thread made the request."""
    with _lock:
        _listeners.append(listener)


def remove_listener(listener) -> None:
    with _lock:
        if listener in _listeners:
            _listeners.remove(listener)


def text_of(messages) -> tuple[str, int]:
    """(the text of chat messages, how many pictures were among them), for the
    list: the words as they were sent, each picture as a placeholder."""
    parts, pictures = [], 0
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        role = message.get("role", "") if isinstance(message, dict) else ""
        if isinstance(content, str):
            parts.append(f"[{role}]\n{content}")
            continue
        words = []
        for part in content or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                pictures += 1
                words.append("(a frame of the video)")
            elif part.get("type") == "text":
                words.append(str(part.get("text") or ""))
        parts.append(f"[{role}]\n" + "\n".join(words))
    return "\n\n".join(parts), pictures
