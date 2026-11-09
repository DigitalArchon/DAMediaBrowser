# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTPS, or nothing.

Everything this app sends goes through here: MusicBrainz searches, cover
art, setlist.fm, the AI and the attestation services Privacy Mode checks.
Most of it names the videos in a library, the AI's carries frames from
them and the API key. So an address is used only if it is https - or if it
never leaves the local network, which is how a model server on this
machine or the one next to it is reached, and where there is usually no
certificate to be had:

    localhost, *.localhost, *.local
    127.0.0.0/8, ::1
    10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
    169.254.0.0/16, fe80::/10            (link-local)
    fc00::/7                             (unique local)

- or if it is a Tor onion service (*.onion), which Tor encrypts and
authenticates end to end whatever the scheme: Tor Browser counts an onion
as secure for the same reason.

Anything else over plain http is refused, with the reason, before a byte is
sent: not rewritten to https, which might be another server altogether, and
not allowed with a warning. Certificates are always verified; nothing here
or anywhere else switches that off.

A name is judged as written. It is not looked up to see where it points: an
address that resolves to this network today can resolve elsewhere tomorrow.
An address with a backslash, a space or a control character in it is
refused outright, because parsers do not agree on where its host ends -
`http://evil.com\\@localhost` is localhost to one and evil.com to another.

A redirect is held to the same rule: followed only to an address that could
have been used in the first place, never from https to plain http, and
never from a server out there to one on this machine's own network. One
that goes to another server is followed without the key.

A server on this machine or its own network is reached directly, whatever
the environment's proxy says: plain http to it through a proxy elsewhere
would carry the key and what was sent across the network in the clear.
Everything else takes the environment's proxy as httpx and requests read it,
ALL_PROXY included, so that every request of the app's goes the same way;
one urllib can't go through - SOCKS, Tor's usual - means nothing is sent,
rather than sent around it from this machine's own address. And an onion
service over plain http goes only through a proxy on this machine: through
one elsewhere, it would cross the network in the clear before Tor.

Ported from DA Assistant's netguard.py (Nov 2026), whose rules these are;
it speaks requests, and this app urllib, so the transport is this app's own.
"""

from __future__ import annotations

import http.client
import ipaddress
import json as _json
import re
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urljoin, urlparse

LOCAL_NAMES = ("localhost",)
LOCAL_SUFFIXES = (".localhost", ".local")
ONION_SUFFIX = ".onion"
LOCAL_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "fe80::/10", "fc00::/7"))
UNSAFE = re.compile(r"[\\\s\x00-\x1f\x7f]")
# Everything of a text up to its last "@" before a query or a fragment, the
# scheme and "//" aside: where an address that cannot be read may have a user
# name and password.
_UP_TO_AT = re.compile(r"^((?:[^:/?#]+:)?//)?[^?#]*@")
# Headers that carry a secret, and so never follow a redirect to another server.
SECRET_HEADERS = ("Authorization", "X-api-key", "Proxy-Authorization")
# The most of one answer that is read: Intel's revocation lists and an
# enclave's attestation are a few hundred KB, the AI's reply far less.
MAX_BODY_BYTES = 32 * 1024 * 1024


class InsecureURL(urllib.error.URLError):
    """An address that may not be used. A URLError, so that every caller
    already handles it, with the reason as `reason`."""


class RedirectRefused(urllib.error.URLError):
    """A redirect to somewhere the request could not have been sent in the
    first place. A URLError, like InsecureURL."""


class RequestError(Exception):
    """A request Session could not make: refused, or the network failed."""


class ProxyRefused(urllib.error.URLError):
    """Nothing was sent: the environment's proxy can't be used for it, and it
    would have gone around it."""


# Every way a request is refused before it is sent, for a caller to tell
# "nothing was sent" from "the network failed".
REFUSALS = (InsecureURL, RedirectRefused, ProxyRefused)


def _unescaped(host):
    """`host` with an IPv6 literal's zone taken off - or None if it has a "%"
    that is not a zone's. Only an IPv6 address has a zone ("fe80::1%eth0");
    anywhere else a "%" is an escape, which a parser may decode into another
    name: "127.0.0.1%2eevil.com" would be judged 127.0.0.1 and sent to
    127.0.0.1.evil.com."""
    host = host.strip("[]")
    if "%" not in host:
        return host
    address, _zone = host.split("%", 1)
    return address if ":" in address else None


def is_local(host) -> bool:
    """Whether `host`, as written, is on this machine or its own network."""
    host = _unescaped((host or "").strip().lower().rstrip("."))
    if not host:
        return False
    if host in LOCAL_NAMES or host.endswith(LOCAL_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return any(address.version == network.version and address in network
               for network in LOCAL_NETWORKS)


def is_onion(host) -> bool:
    """Whether `host` is a Tor onion service."""
    host = (host or "").strip().lower().rstrip(".")
    return "%" not in host and host.endswith(ONION_SUFFIX)


def problem(url) -> str | None:
    """Why `url` may not be used, or None when it may. The address is named
    as shown() has it, without a password written into it: these are shown
    in the window and copied into bug reports."""
    text = str(url or "").strip()
    if not text:
        return "No address given."
    if UNSAFE.search(text):
        return (f"Not a web address: {shown(text)!r}. An address has no spaces, "
                f"backslashes or control characters in it.")
    try:
        parsed = urlparse(text)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        _ = parsed.port         # raises on a port no address can have
    except ValueError:          # "http://[fd00::5", half typed
        return f"Not a web address: {shown(text)!r}."
    if scheme not in ("http", "https") or not host:
        return (f"Not a web address: {shown(text)!r}. It must start with https:// - or "
                f"http:// for this machine or its local network.")
    if scheme == "https":
        return None
    if _unescaped(host) is None:
        return f"Not a web address: {shown(text)!r}."
    if not (is_local(host) or is_onion(host)):
        return (f"{shown(text)} is not https, so the API key and everything sent with "
                f"it would cross the network unencrypted. Use an https:// address. "
                f"Plain http is accepted only for this machine and its local network "
                f"(localhost, 10.x, 172.16-31.x, 192.168.x, .local), and for Tor onion "
                f"services.")
    return None


def check(url) -> str:
    """Raise InsecureURL unless `url` may be used. Returns it stripped."""
    found = problem(url)
    if found:
        raise InsecureURL(found)
    return str(url).strip()


def redirect_problem(source, target) -> str | None:
    """Why a redirect from `source` to `target` may not be followed, or None."""
    found = problem(target)
    if found:
        return (f"The server sent the request on to {shown(target)}, which was not "
                f"followed: {found}")
    if urlparse(source).scheme.lower() == "https" and \
            urlparse(target).scheme.lower() != "https":
        return (f"The server sent the request on from https to {shown(target)}, which "
                f"was not followed: an https request is never carried on over plain "
                f"http.")
    if not is_local(urlparse(source).hostname) and is_local(urlparse(target).hostname):
        return (f"The server sent the request on to {shown(target)}, on this "
                f"computer's own network, which was not followed.")
    return None


def shown(url) -> str:
    """`url` as it may be shown, or copied into a bug report: without the user
    name and password an address may carry (https://alice:secret@host/v1).

    For any text at all, and never raising. One that cannot be read as an
    address says nothing certain of where its host begins, so all it has up
    to its last "@" goes."""
    text = str(url or "")
    try:
        parsed = urlparse(text)
        _ = parsed.port         # raises on a port that is not a number
    except ValueError:
        parsed = None
    if parsed is None or not parsed.netloc:
        return _UP_TO_AT.sub(r"\1…@", text, count=1)
    if "@" not in parsed.netloc:
        return text
    return parsed._replace(netloc="…@" + parsed.netloc.rsplit("@", 1)[1]).geturl()


def key_problem(key, name="the service") -> str | None:
    """Why `key` cannot be sent as an API key, or None. A key is plain ASCII
    with no spaces in it; one pasted with a line break, or a zero-width space
    from a web page, is refused by http.client in words that quote it whole,
    and those would be shown in the window."""
    if key and (not key.isascii() or UNSAFE.search(key) or " " in key):
        return (f"The API key for {name} has a space, a line break or another "
                f"character no API key has in it, picked up where it was copied "
                f"from, perhaps. Paste it again in Settings.")
    return None


# --- the transport ----------------------------------------------------------


def _origin(url) -> tuple:
    parts = urlparse(url)
    return (parts.scheme.lower(), (parts.hostname or "").lower(), parts.port)


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect followed only where the request could have gone in the
    first place (redirect_problem), and without the key when it goes to
    another server: urllib hands a redirect every header it was given."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = urljoin(req.full_url, newurl)
        found = redirect_problem(req.full_url, newurl)
        if found:
            raise RedirectRefused(found)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(newurl) != _origin(req.full_url):
            for name in SECRET_HEADERS:
                new.remove_header(name)
                new.remove_header(name.capitalize())
        return new


def env_proxies() -> dict:
    """The environment's proxies as httpx and requests take them: a scheme's
    own, or ALL_PROXY's for one that has none. urllib by itself reads no
    ALL_PROXY, and with Tor set that way its requests went direct while the
    sealed client's went through Tor."""
    proxies = urllib.request.getproxies()
    every = proxies.get("all")
    for scheme in ("http", "https"):
        if every and not proxies.get(scheme):
            proxies[scheme] = every
    return proxies


class _LocalDirect(urllib.request.ProxyHandler):
    """The environment's proxy (env_proxies), for everything but this machine
    and its own network, which are reached directly: see the module's
    docstring."""

    def __init__(self):
        super().__init__(env_proxies())

    def proxy_open(self, req, proxy, type):
        host = urlparse(req.full_url).hostname
        if is_local(host):
            return None
        return super().proxy_open(req, proxy, type)


def _opener() -> urllib.request.OpenerDirector:
    # Made per request: the environment's proxy is read as it is now.
    return urllib.request.build_opener(_LocalDirect(), _GuardedRedirects())


def proxy_problem(url) -> str | None:
    """Why the environment's proxy won't do for `url`, or None: one urllib
    can't go through (SOCKS), or plain http to an onion service through a
    proxy that isn't on this machine."""
    proxy = env_proxy_for(url)
    if not proxy:
        return None
    try:
        parsed = urlparse(proxy)
        scheme, host = parsed.scheme.lower(), parsed.hostname
    except ValueError:
        scheme, host = "", None
    if scheme not in ("http", "https") or not host:
        return (f"The environment's proxy, {shown(proxy)}, isn't one this app's "
                f"requests can go through (an http:// proxy is: Tor's HTTPTunnelPort, "
                f"for one), so nothing was sent rather than sent around it.")
    target = urlparse(url)
    if target.scheme.lower() == "http" and is_onion(target.hostname) and not is_local(host):
        return (f"{shown(url)} is plain http, and the environment's proxy, "
                f"{shown(proxy)}, isn't on this computer: it would cross the network "
                f"unencrypted before reaching Tor. Nothing was sent.")
    return None


def _transport(request, timeout):
    """The one place a request leaves the app; tests stand in for it here,
    after every check of this module has been made."""
    return _opener().open(request, timeout=timeout)


def urlopen(request, timeout):
    """urllib.request.urlopen, held to this module's rules. Raises
    InsecureURL (a URLError) before anything is sent to an address that may
    not be used, RedirectRefused for a redirect that may not be followed,
    and urllib's own errors for the rest."""
    if isinstance(request, str):
        request = urllib.request.Request(request)
    check(request.full_url)
    refused = proxy_problem(request.full_url)
    if refused:
        raise ProxyRefused(refused)
    for name in ("Authorization", "X-api-key"):
        value = request.get_header(name) or request.get_header(name.capitalize()) or ""
        secret = value[7:] if value.startswith("Bearer ") else value
        found = key_problem(secret, urlparse(request.full_url).hostname)
        if found:
            raise InsecureURL(found)
    return _transport(request, timeout)


def reason(exc) -> str:
    """What went wrong with a request, in words that never quote a key."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    found = getattr(exc, "reason", None) or exc
    return str(found) or type(exc).__name__


# --- a small Session, for the attestation checks ---------------------------


class Response:
    """Just enough of a requests Response for what the attestation checks
    read: the status, the headers, the body."""

    def __init__(self, status_code: int, headers, content: bytes, url: str):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.content = content
        self.url = url

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        """The body as JSON; ValueError when it isn't."""
        return _json.loads(self.content.decode("utf-8", errors="replace"))


def _read(handle) -> bytes:
    body = handle.read(MAX_BODY_BYTES + 1)
    if len(body) > MAX_BODY_BYTES:
        raise RequestError("the answer was far larger than any it should be")
    return body


class Session:
    """GET and POST as requests has them, over this module's urlopen: an
    answer with an error status comes back as a Response, as requests gives
    one; a refused address, a refused redirect or a network failure raises
    RequestError."""

    def get(self, url, params=None, headers=None, timeout=30) -> Response:
        return self.request("GET", url, params=params, headers=headers, timeout=timeout)

    def post(self, url, json=None, headers=None, timeout=30) -> Response:
        return self.request("POST", url, json=json, headers=headers, timeout=timeout)

    def request(self, method, url, params=None, json=None, headers=None,
                timeout=30) -> Response:
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None})
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        data = None
        headers = dict(headers or {})
        if json is not None:
            data = _json.dumps(json).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout) as handle:
                return Response(handle.status, handle.headers.items(), _read(handle),
                                handle.geturl())
        except urllib.error.HTTPError as exc:
            try:
                body = _read(exc)
            except Exception:  # noqa: BLE001 - the status is what matters
                body = b""
            return Response(exc.code, exc.headers.items() if exc.headers else (), body, url)
        except (urllib.error.URLError, OSError, ValueError,
                http.client.HTTPException) as exc:
            raise RequestError(f"{shown(url)}: {reason(exc)}") from exc

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# --- the environment's proxy, for the sealed client -------------------------


def env_proxy_for(url) -> str | None:
    """The proxy the environment gives `url`, as urllib would use it, or
    None: for this machine and its own network, always None. For the
    end-to-end encrypted client, whose transport takes a proxy only when it
    is told one: without it, that request alone of all the app's would have
    gone around the proxy the environment set."""
    parsed = urlparse(url)
    if is_local(parsed.hostname):
        return None
    if urllib.request.proxy_bypass(parsed.hostname or ""):
        return None
    return env_proxies().get(parsed.scheme.lower()) or None


# --- requests, as Tinfoil's SDK is given it ---------------------------------


def requests_session():
    """A requests Session held to this module's rules - for Tinfoil's SDK,
    which fetches with requests, and is the only part of the app that does
    (see sealed.guard_sdk_requests). requests follows a redirect anywhere,
    https to plain http included; this follows one only where the request
    could have gone in the first place, and reaches a server on this
    machine's own network directly. Raises ImportError without requests,
    which only the SDK brings."""
    import requests

    class InvalidRedirect(requests.exceptions.InvalidURL):
        pass

    class GuardedSession(requests.Session):
        def get_redirect_target(self, response):
            target = super().get_redirect_target(response)
            if target:
                found = redirect_problem(response.url, urljoin(response.url, target))
                if found:
                    raise InvalidRedirect(found, response=response)
            return target

        def send(self, request, **kwargs):
            found = problem(request.url)
            if found:
                raise requests.exceptions.InvalidURL(found, request=request)
            if is_local(urlparse(request.url).hostname):
                kwargs["proxies"] = {}
                request.headers.pop("Proxy-Authorization", None)
            return super().send(request, **kwargs)

    return GuardedSession()
