# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The AI's help with a MusicBrainz search, where the file's name isn't
enough.

Two questions, each one request:

- better_queries: the search made from a file's name and folders found
  nothing that fits. The model works out what the video is - searching
  the web if it needs to - and says what to search MusicBrainz for, by
  the names MusicBrainz knows the artist and the release by.
- choose_release: of the releases whose tracks add up to the video, which
  is this show. The rules ask that the release's artist and title appear
  in the file's name or folders, which a romanised folder name or a tour
  named apart from its album defeats; the model can tell.

Neither answer is taken on trust: a release is only ever used when its
tracks add up to the video, which the caller checks whatever the model
says. Nothing here talks to the network itself; the chat function is
passed in.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ai, naming, utils

CONFIDENCES = ("high", "medium", "low")
# Searches the model may suggest; each is a MusicBrainz request, and
# their best few releases a request each.
MAX_QUERIES = 2
# Track titles shown per release: enough to tell one show from another.
TRACKS_SHOWN = 40

SYSTEM_PROMPT = (
    "You identify concert videos and music videos, and find their releases on "
    "MusicBrainz. Reply with a single JSON object and nothing else."
)


@dataclass
class Candidate:
    """A release whose tracks add up to the video, as the model is shown it."""

    release: dict  # a musicbrainz.search_releases() entry
    tracks: list[dict]  # the tracks of the discs that add up
    discs: str  # which of the release's discs those are, in words
    recognised: bool  # its artist and title are in the file's name or folders


@dataclass
class Choice:
    release_id: str | None
    confidence: str
    reason: str


def _time(seconds: float) -> str:
    return utils.format_seconds(seconds)


def _describe_video(video: dict, context: str) -> str:
    lines = [
        f"File: {video['display_name']}",
        f"Kind: {'Blu-ray title' if video.get('type') == 'bluray' else 'video file'}",
        f"Length: {_time(video['duration'])}",
    ]
    if context:
        lines.append(f"Folders: {context}")
    chapters = video.get("chapters") or []
    names = [c["title"] for c in chapters if naming.is_named(c)]
    if len(chapters) > 1:
        lines.append(f"Chapters: {len(chapters)}"
                     + (f", some named: {'; '.join(names)}" if names else ", none named"))
    if video.get("hints"):
        lines.append(video["hints"])
    return "\n".join(lines)


def describe_release(release: dict) -> str:
    year = f" ({release['date'][:4]})" if release.get("date") else ""
    count = f", {release['track_count']} tracks" if release.get("track_count") else ""
    return f"“{release.get('title')}” by {release.get('artist') or 'unknown'}{year}{count}"


def _ask(chat, messages, settings, **kwargs) -> dict:
    """The real chat is asked for JSON with one retry (ai.chat_json); a
    stand-in's text is parsed as it is. A web search this Nano-GPT account
    doesn't allow is made with LinkUp, which it does."""
    def once(settings):
        if chat is ai.chat:
            return ai.chat_json(messages, settings, **kwargs)
        reply = chat(messages, settings, **kwargs)
        return reply if isinstance(reply, dict) else ai.parse_json_reply(reply)

    try:
        return once(settings)
    except ai.SearchUnavailable:
        return once(ai.with_search(settings, "linkup"))


QUERIES_PROMPT = (
    "Work out what this video is - the artist, and the show, album or programme it "
    "comes from - searching the web if it helps. Then give up to {most} MusicBrainz "
    "release searches likely to find the release whose tracklist is this video's "
    "songs in order (a live album or video release of the same show; for a single "
    "song, the single or album it's on), best first.\n"
    "- Use the names MusicBrainz has: the artist's official name, which for a "
    "Japanese artist is often in Japanese script, and the release's own title, not "
    "a tour's or a file's.\n"
    "- MusicBrainz's search syntax works and is the most precise: "
    "release:\"Live at Wembley\" AND artist:\"BABYMETAL\".\n"
    "- Don't repeat a search that was already made.\n"
    "Reply with exactly: "
    '{{"what": "one line: what the video is", "queries": ["...", "..."]}}'
)


def better_queries(video: dict, context: str, tried: list[str], found: list[dict],
                   settings: dict, chat=ai.chat) -> tuple[str, list[str]]:
    """What the video is, in a line, and up to MAX_QUERIES searches to
    make for it - none of them one already `tried`."""
    lines = [_describe_video(video, context), ""]
    for query in tried:
        lines.append(f"Searched MusicBrainz for: {query}")
    if found:
        lines.append("It found these, none of them this video's release:")
        lines += [f"- {describe_release(r)}" for r in found]
    else:
        lines.append("It found nothing.")
    lines += ["", QUERIES_PROMPT.format(most=MAX_QUERIES)]
    reply = _ask(chat, [{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": "\n".join(lines)}],
                 settings, online=True, max_tokens=2048)
    seen = {q.strip().casefold() for q in tried}
    queries = []
    for query in reply.get("queries") or []:
        if not isinstance(query, str) or not query.strip():
            continue
        if query.strip().casefold() in seen:
            continue
        seen.add(query.strip().casefold())
        queries.append(query.strip())
    what = reply.get("what")
    return (what.strip() if isinstance(what, str) else ""), queries[:MAX_QUERIES]


CHOOSE_PROMPT = (
    "Each release above has tracks that add up to this video's length. Which one, "
    "if any, is this video: the same show or programme, not just the same songs? "
    "A studio album with the same songs isn't a concert; another night of the same "
    "tour isn't this one. The file's and folders' names are the best clue to which "
    "show it is.\n"
    "confidence is \"high\" when you're sure, \"medium\" when it's likely, \"low\" "
    "when you're guessing. Reply with exactly: "
    '{"n": 1, "confidence": "high", "reason": "one short line"} - n is null if none '
    "of them is this video."
)


def _describe_candidate(n: int, candidate: Candidate, duration: float) -> str:
    tracks = candidate.tracks
    total = sum(t.get("length") or 0 for t in tracks)
    titles = "; ".join(t.get("title") or "?" for t in tracks[:TRACKS_SHOWN])
    if len(tracks) > TRACKS_SHOWN:
        titles += f"; and {len(tracks) - TRACKS_SHOWN} more"
    lines = [
        f"{n}. {describe_release(candidate.release)}",
        f"   {candidate.discs}: {len(tracks)} tracks, {_time(total)} "
        f"(the video is {_time(duration)})",
        f"   Tracks: {titles}",
    ]
    if candidate.recognised:
        lines.append("   Its artist and title are both in the file's name or folders.")
    return "\n".join(lines)


def choose_release(video: dict, context: str, candidates: list[Candidate], settings: dict,
                   chat=ai.chat) -> Choice:
    """Which of `candidates` is this video, if any."""
    if not candidates:
        return Choice(None, "low", "")
    lines = [_describe_video(video, context), "", "Releases:"]
    lines += [_describe_candidate(n, c, video["duration"])
              for n, c in enumerate(candidates, start=1)]
    lines += ["", CHOOSE_PROMPT]
    reply = _ask(chat, [{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": "\n".join(lines)}],
                 settings, max_tokens=1024)
    reason = reply.get("reason") if isinstance(reply.get("reason"), str) else ""
    confidence = reply.get("confidence")
    confidence = confidence if confidence in CONFIDENCES else "low"
    n = reply.get("n")
    try:
        n = int(n) if n is not None and not isinstance(n, bool) else None
    except (TypeError, ValueError):
        n = None
    if n is None or not 1 <= n <= len(candidates):
        return Choice(None, confidence, reason.strip())
    return Choice(candidates[n - 1].release["id"], confidence, reason.strip())
