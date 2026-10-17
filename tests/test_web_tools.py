"""Network tools: the address boundary approval cannot express (DR-012/DR-023).

These use real loopback sockets rather than a mocked opener on purpose. The
bug DR-023 fixes lived in urllib's redirect handling, not in AutoSE's own call
site, so a fake that stands in for urllib would have passed against the broken
code too.
"""

from __future__ import annotations

import http.server
import socketserver
import threading

import pytest

from logic.common.tools.web import (
    BlockedRedirectError,
    _guarded_opener,
    _refuse_non_public_url,
    fetch_url,
)


def _serve(handler) -> tuple[str, socketserver.TCPServer]:
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server


@pytest.fixture
def internal_target():
    """A stand-in for the thing an SSRF is trying to reach."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"SECRET-INTERNAL-METADATA"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    url, server = _serve(Handler)
    yield url
    server.shutdown()


@pytest.fixture
def redirector(internal_target):
    """A server that answers 302 pointing at the internal target."""
    target = internal_target

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"{target}/latest/meta-data/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    url, server = _serve(Handler)
    yield url
    server.shutdown()


# -- the direct check -------------------------------------------------------


def test_a_loopback_url_is_refused_outright():
    refusal = _refuse_non_public_url("http://127.0.0.1:8080/")
    assert refusal and "non-public address" in refusal


def test_a_link_local_metadata_address_is_refused():
    refusal = _refuse_non_public_url("http://169.254.169.254/latest/meta-data/")
    assert refusal and "non-public address" in refusal


def test_a_non_http_scheme_is_refused():
    assert _refuse_non_public_url("file:///etc/passwd")
    assert _refuse_non_public_url("gopher://example.com/")


# -- DR-023: the check has to survive a redirect ----------------------------


def test_a_redirect_to_a_private_address_is_refused(redirector):
    """The hop nobody approved is still a request AutoSE is making."""
    with pytest.raises(BlockedRedirectError) as caught:
        _guarded_opener().open(f"{redirector}/start", timeout=5)
    assert "non-public address" in caught.value.blocked_reason


def test_fetch_url_reports_a_blocked_redirect_instead_of_returning_content(
    redirector, monkeypatch
):
    """The internal body must never reach the model, by any path.

    The initial URL is loopback too, so the entry check is stubbed out to
    isolate the redirect hop — otherwise this would pass without DR-023's fix
    simply because the first check refused it.
    """
    seen: list[str] = []

    def only_guard_redirects(url: str):
        seen.append(url)
        # Let the first (loopback test-server) URL through; guard the rest.
        return None if len(seen) == 1 else _real_guard(url)

    from logic.common.tools import web as web_module

    _real_guard = web_module._refuse_non_public_url
    monkeypatch.setattr(web_module, "_refuse_non_public_url", only_guard_redirects)

    result = fetch_url(f"{redirector}/start")

    assert "SECRET-INTERNAL-METADATA" not in result
    assert "redirected to a non-public address" in result
