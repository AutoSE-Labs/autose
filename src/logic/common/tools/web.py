"""Network tool handlers: web search and URL fetch.

Both are approval-gated in the shared executor (DR-012). The handlers add the
boundary approval cannot express: a user approving "fetch this URL" is not
approving a request to a loopback, link-local, or private address. Because
config-driven inference backends commonly *are* on localhost, and cloud
metadata endpoints are one link-local hop away, every hostname is resolved and
refused if any of its addresses is non-global — closing the local SSRF path
even on an approved call.

That check is worth nothing if it only guards the URL the user saw. A redirect
is a second request to an address nobody approved, so DR-023 requires the same
check on **every hop**: the opener below re-validates each ``Location`` before
following it, and refuses the redirect otherwise. Without that, a public page
answering 302 with ``http://169.254.169.254/`` reaches cloud metadata through
a guard that already said yes.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Final

from ..workspace import Workspace

__all__ = ["fetch_url", "web_search", "BlockedRedirectError"]

_REQUEST_TIMEOUT: Final[int] = 15
_USER_AGENT: Final[str] = (
    "AutoSE-Agent/1.0 (+tool-call; https://github.com/AutoSE-Labs)"
)
_MAX_FETCH_BYTES: Final[int] = 2_000_000
_DEFAULT_FETCH_CHARS: Final[int] = 6_000
_MAX_FETCH_CHARS: Final[int] = 20_000
_MAX_SEARCH_RESULTS: Final[int] = 10

_URL_PREFIX = re.compile(r"^(https?)://([^/:?#]+)", re.IGNORECASE)
_CHARSET = re.compile(r"charset=([\w-]+)", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")

_DUCKDUCKGO_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'(?:<a[^>]+class="result__snippet"[^>]*>(.*?)</a>)?',
    re.DOTALL,
)


class _TextExtractor(HTMLParser):
    """Minimal stdlib HTML-to-text extraction — no third-party dependency."""

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})
    _BREAK_TAGS = frozenset({"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BREAK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self.chunks.append(data.strip())

    def text(self) -> str:
        joined = " ".join(self.chunks)
        return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*", "\n", joined)).strip()


def _refuse_non_public_url(url: str) -> str | None:
    """Return an error string unless *url* is an http(s) URL on a global address."""
    match = _URL_PREFIX.match(url.strip())
    if not match:
        return "Error: only absolute http:// or https:// URLs are supported."
    host = match.group(2)
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror as exc:
        return f"Error: could not resolve host '{host}': {exc}"
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not ip.is_global:
            return (
                f"Error: '{host}' resolves to a non-public address ({address}); "
                "fetching internal or private network locations is not allowed."
            )
    return None


class BlockedRedirectError(urllib.error.HTTPError):
    """A redirect whose destination failed the non-public address check."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(url, 403, reason, {}, None)  # type: ignore[arg-type]
        self.blocked_reason = reason


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-runs the address check on every hop before following it (DR-023).

    Subclassing rather than disabling redirects keeps urllib's own loop and
    depth limits, which a hand-rolled follow loop would have to reimplement.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # Protocol implementation.
        refusal = _refuse_non_public_url(newurl)
        if refusal:
            raise BlockedRedirectError(newurl, refusal)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _guarded_opener() -> urllib.request.OpenerDirector:
    """An opener that validates redirect targets and nothing else special."""
    return urllib.request.build_opener(_GuardedRedirectHandler)


def fetch_url(
    url: str,
    max_chars: int = _DEFAULT_FETCH_CHARS,
    *,
    workspace: Workspace | None = None,
) -> str:
    """Fetch a public web page and return its visible text, bounded."""
    refusal = _refuse_non_public_url(url)
    if refusal:
        return refusal
    try:
        limit = int(max_chars) if max_chars else _DEFAULT_FETCH_CHARS
    except (TypeError, ValueError):
        limit = _DEFAULT_FETCH_CHARS
    limit = max(1, min(limit, _MAX_FETCH_CHARS))

    # S310: `url` has already been validated as public http(s), and is
    # re-validated on every redirect hop (DR-023).
    request = urllib.request.Request(  # noqa: S310
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with _guarded_opener().open(request, timeout=_REQUEST_TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read(_MAX_FETCH_BYTES + 1)
    except BlockedRedirectError as exc:
        return (
            f"Error: {url} redirected to a non-public address, which was refused. "
            f"{exc.blocked_reason}"
        )
    except urllib.error.HTTPError as exc:
        return f"Error: HTTP {exc.code} fetching {url}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return f"Error: could not fetch {url}: {exc}"
    raw = raw[:_MAX_FETCH_BYTES]

    charset = _CHARSET.search(content_type)
    try:
        body = raw.decode(charset.group(1) if charset else "utf-8", errors="replace")
    except LookupError:
        body = raw.decode("utf-8", errors="replace")

    if "html" in content_type.lower() or "<html" in body[:1000].lower():
        extractor = _TextExtractor()
        extractor.feed(body)
        text = extractor.text()
    else:
        text = body.strip()

    suffix = (
        f"\n\n[Truncated to {limit} characters. Raise max_chars for more.]"
        if len(text) > limit
        else ""
    )
    return f"# {url}\n" + text[:limit] + suffix


def web_search(
    query: str,
    max_results: int = 5,
    *,
    workspace: Workspace | None = None,
) -> str:
    """Search the web and return the top results as title/URL/snippet lines."""
    query = (query or "").strip()
    if not query:
        return "Error: query must not be empty."
    try:
        count = int(max_results) if max_results else 5
    except (TypeError, ValueError):
        count = 5
    count = max(1, min(count, _MAX_SEARCH_RESULTS))

    search_url = "https://duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    refusal = _refuse_non_public_url(search_url)
    if refusal:
        return refusal

    request = urllib.request.Request(
        search_url, headers={"User-Agent": _USER_AGENT, "Accept": "text/html"}
    )
    try:
        with _guarded_opener().open(request, timeout=_REQUEST_TIMEOUT) as response:
            body = response.read(_MAX_FETCH_BYTES).decode("utf-8", errors="replace")
    except BlockedRedirectError as exc:
        return (
            "Error: the search backend redirected to a non-public address, "
            f"which was refused. {exc.blocked_reason}"
        )
    except urllib.error.HTTPError as exc:
        return f"Error: search backend returned HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return f"Error: could not reach search backend: {exc}"

    results = _parse_results(body, count)
    if not results:
        return f"No results for '{query}'."
    lines = [f"Search results for '{query}':"]
    for position, (title, href, snippet) in enumerate(results, start=1):
        lines.append(f"{position}. {title}\n   {href}\n   {snippet}".rstrip())
    return "\n".join(lines)


def _parse_results(html: str, max_results: int) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    for block in _DUCKDUCKGO_RESULT.finditer(html):
        href = block.group(1)
        title = _TAG.sub("", block.group(2)).strip()
        snippet = _TAG.sub("", block.group(3) or "").strip()
        if not title or not href:
            continue
        results.append((title, href, snippet))
        if len(results) >= max_results:
            break
    return results
