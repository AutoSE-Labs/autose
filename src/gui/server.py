"""The HTTP surface of the dashboard: routes, streaming, and local trust.

Standard library only, on purpose. AutoSE's dependency list is two packages,
and a local-first tool that pulls a web framework (and a CDN's fonts, and a
markdown library) to draw one page has quietly become a networked application.
Nothing here reaches the network at all: every asset is served from
``gui/static``, and the page it serves fetches nothing from anywhere else.

Because the dashboard drives an agent that writes files and — with approval —
runs shell commands, the trust boundary is treated as real rather than assumed
away by "it's only localhost":

* it binds loopback unless told otherwise, and the browser is handed a
  single-use-per-run **token** it must present on every API call;
* the ``Host`` header must name a host we are actually serving, which is what
  stops DNS rebinding from turning a page on the open internet into a client
  of this server;
* a cross-origin ``Origin`` is refused outright, so no other page in the
  user's browser can post a prompt into their workspace.

Live updates are Server-Sent Events: one long GET per browser, numbered frames,
and a ``Last-Event-ID`` replay so a reload mid-turn resumes rather than
resynchronising from nothing. Every frame names the session it belongs to,
because several can be generating at once (DR-031) — and so does every request
that acts on one, as ``session`` in the body or the query string, defaulting to
whichever the browser is focused on.
"""

from __future__ import annotations

import ipaddress
import json
import queue
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import compare_digest
from typing import Any
from urllib.parse import parse_qs, urlparse

from .bridge import BridgeError, SessionBridge

__all__ = ["Dashboard", "create_dashboard", "STATIC_ROOT"]

STATIC_ROOT = Path(__file__).resolve().parent / "static"

#: The page and its two assets, named explicitly. An allowlist rather than a
#: directory walk: there is no path to traverse if no path is ever built from
#: what the client sent.
_ASSETS: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/inspector.js": ("inspector.js", "text/javascript; charset=utf-8"),
    "/settings.js": ("settings.js", "text/javascript; charset=utf-8"),
    "/notify.js": ("notify.js", "text/javascript; charset=utf-8"),
    "/route.js": ("route.js", "text/javascript; charset=utf-8"),
    "/life.js": ("life.js", "text/javascript; charset=utf-8"),
    "/diff.js": ("diff.js", "text/javascript; charset=utf-8"),
    "/files.js": ("files.js", "text/javascript; charset=utf-8"),
    "/palette.js": ("palette.js", "text/javascript; charset=utf-8"),
    "/automation.js": ("automation.js", "text/javascript; charset=utf-8"),
    "/steps.js": ("steps.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    # Bundled so the page needs nothing from the network (DR-029).
    "/fonts/space-grotesk-latin-wght-normal.woff2": (
        "fonts/space-grotesk-latin-wght-normal.woff2", "font/woff2"),
    "/fonts/ibm-plex-mono-latin-400-normal.woff2": (
        "fonts/ibm-plex-mono-latin-400-normal.woff2", "font/woff2"),
    "/fonts/allerta-stencil-latin-400-normal.woff2": (
        "fonts/allerta-stencil-latin-400-normal.woff2", "font/woff2"),
    "/fonts/atkinson-hyperlegible-latin-400-normal.woff2": (
        "fonts/atkinson-hyperlegible-latin-400-normal.woff2", "font/woff2"),
    "/fonts/atkinson-hyperlegible-latin-700-normal.woff2": (
        "fonts/atkinson-hyperlegible-latin-700-normal.woff2", "font/woff2"),
    "/fonts/architects-daughter-latin-400-normal.woff2": (
        "fonts/architects-daughter-latin-400-normal.woff2", "font/woff2"),
}

#: A prompt is prose and an approval is a sentence; nothing this API accepts is
#: large. Reading an unbounded body from a socket, however, is how a local
#: service becomes a memory-exhaustion bug.
_MAX_BODY_BYTES = 1 << 20

#: How long a quiet event stream waits before sending a keep-alive comment.
#: Short enough that a proxy or a sleeping laptop does not silently kill the
#: connection without either side noticing.
_STREAM_PING_SECONDS = 15.0

_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


class Dashboard(ThreadingHTTPServer):
    """The serving side: a bridge, a token, and the hosts it answers for."""

    daemon_threads = True
    # Never join handler threads at shutdown: one of them is parked on an event
    # stream that only ends when the browser goes away.
    block_on_close = False
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        bridge: SessionBridge,
        *,
        token: str,
        verbose: bool = False,
    ) -> None:
        self.bridge = bridge
        self.token = token
        self.verbose = verbose
        self.stopping = False
        self.host = address[0]
        super().__init__(address, DashboardHandler)

    @property
    def url(self) -> str:
        """The address to open, token included — the page needs it to talk back."""
        bound = self.server_address
        host, port = str(bound[0]), int(bound[1])
        shown = "localhost" if host in ("127.0.0.1", "::1") else host
        if ":" in shown:
            # A bare IPv6 literal has to be bracketed to be a valid authority.
            shown = f"[{shown}]"
        return f"http://{shown}:{port}/?token={self.token}"

    def allowed_hosts(self) -> frozenset[str]:
        """Host names this server will answer for, or ``*`` for any.

        The set is what the ``Host`` check compares against: a request naming
        anything else is refused, which is what makes DNS rebinding fail
        rather than succeed quietly.
        """
        names = set(_LOOPBACK_NAMES)
        names.add(self.host)
        if _serves_every_interface(self.host):
            # Bound to every interface, so any name that resolves here really
            # is one we serve; the token is the control that remains.
            names.add("*")
        return frozenset(names)

    def stop(self) -> None:
        self.stopping = True
        self.bridge.shutdown()
        # `shutdown` waits for the serving loop, which may be this thread's own
        # caller; run it beside us and give it a moment, so a caller that then
        # closes the socket is not racing the loop that is still polling it.
        closing = threading.Thread(target=self.shutdown, daemon=True)
        closing.start()
        closing.join(timeout=2.0)


def create_dashboard(
    bridge: SessionBridge,
    *,
    host: str,
    port: int,
    token: str,
    verbose: bool = False,
) -> Dashboard:
    """Bind a dashboard. ``port=0`` asks the OS for a free one."""
    return Dashboard((host, port), bridge, token=token, verbose=verbose)


class DashboardHandler(BaseHTTPRequestHandler):
    """One request. Every route is a method on this class and nothing else."""

    server_version = "AutoSE-Dashboard"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- dispatch ----------------------------------------------------------

    def do_GET(self) -> None:  # BaseHTTPRequestHandler's naming contract
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path in _ASSETS and path not in ("/", "/index.html"):
            self._send_asset(path)
            return
        if not self._authorized(query):
            return
        if path in ("/", "/index.html"):
            self._send_asset(path)
            return
        if path == "/api/events":
            self._stream_events(query)
            return
        # Which session a read is about travels in the query string, and
        # defaults to the focused one. Every write takes the same thing in its
        # body (DR-031).
        target = _first(query.get("session")) or None
        readers = {
            "/api/state": self._bridge.snapshot,
            "/api/sessions": self._bridge.sessions,
            "/api/changes": lambda: self._bridge.changes(
                target, turn=_turn_of(query)
            ),
            # DR-055: a finished turn's steps, read when the inspector shows it.
            "/api/steps": lambda: self._bridge.steps(target, turn=_turn_of(query)),
            "/api/doctor": self._bridge.doctor,
            # DR-051: the Files view, read-only and bounded by the workspace.
            "/api/files": self._bridge.files,
            "/api/file": lambda: self._bridge.file_text(_first(query.get("path")) or ""),
            "/api/providers": self._bridge.providers,
            "/api/automation": self._bridge.automation,
            # DR-062: background commands, and one's output after a cursor.
            "/api/processes": lambda: self._bridge.processes(
                _first(query.get("id")), since=_int_or_zero(_first(query.get("since")))
            ),
        }
        reader = readers.get(path)
        if reader is None:
            self._send_json({"error": "no such endpoint"}, status=HTTPStatus.NOT_FOUND)
            return
        self._guarded(reader)

    def do_POST(self) -> None:  # BaseHTTPRequestHandler's naming contract
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if not self._authorized(query):
            return
        writers = {
            "/api/prompt": self._prompt,
            "/api/allowances": self._allowances,
            "/api/yolo": lambda body: self._bridge.set_yolo(bool(body.get("enabled"))),
            "/api/plan": self._plan,
            "/api/plan-review": self._plan_review,
            "/api/queue": self._queue,
            "/api/interrupt": self._interrupt,
            "/api/approval": self._approval,
            "/api/model": self._model,
            "/api/context": self._context,
            "/api/tier": self._tier,
            "/api/undo": self._undo,
            "/api/clear": self._clear,
            "/api/session": self._session,
            "/api/reload": self._reload,
            "/api/connect": lambda body: self._bridge.connect(
                str(body.get("base_url") or ""), str(body.get("model") or "")
            ),
            "/api/providers": self._providers,
            "/api/automation": self._automation,
            "/api/processes": self._processes,
        }
        writer = writers.get(path)
        if writer is None:
            self._send_json({"error": "no such endpoint"}, status=HTTPStatus.NOT_FOUND)
            return
        body = self._read_body()
        if body is None:
            return
        self._guarded(lambda: writer(body))

    # -- API handlers ------------------------------------------------------

    def _prompt(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._bridge.submit(
            str(body.get("prompt") or ""),
            tier=_optional_str(body.get("tier")),
            session_id=_session_of(body),
            queue=bool(body.get("queue")),
        )

    def _queue(self, body: dict[str, Any]) -> dict[str, Any]:
        to = body.get("to")
        if to is not None and not isinstance(to, int):
            raise BridgeError("to must be a whole number")
        return self._bridge.queue_action(
            str(body.get("action") or ""),
            session_id=_session_of(body),
            item_id=_optional_str(body.get("id")),
            to=to,
        )

    def _interrupt(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._bridge.interrupt(_session_of(body))

    def _approval(self, body: dict[str, Any]) -> dict[str, Any]:
        identifier = str(body.get("id") or "")
        if not identifier:
            raise BridgeError("which approval? an id is required")
        return self._bridge.resolve_approval(
            identifier,
            approved=bool(body.get("approved")),
            reason=str(body.get("reason") or ""),
            scope=str(body.get("scope") or "once"),
        )

    def _plan(self, body: dict[str, Any]) -> dict[str, Any]:
        identifier = str(body.get("id") or "")
        if not identifier:
            raise BridgeError("which plan? an id is required")
        return self._bridge.resolve_plan(
            identifier,
            action=str(body.get("action") or ""),
            feedback=str(body.get("feedback") or ""),
            plan=str(body.get("plan") or ""),
        )

    def _plan_review(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._bridge.set_plan_review(
            bool(body.get("enabled")), session_id=_session_of(body)
        )

    def _allowances(self, body: dict[str, Any]) -> dict[str, Any]:
        action = body.get("action")
        # DR-057: a rule is for this session (the default) or the workspace.
        scope = str(body.get("scope") or "session").strip()
        if action == "add":
            # DR-050: a rule written by hand, checked as one given from a card.
            return self._bridge.add_allowance(
                str(body.get("tool") or "").strip(),
                str(body.get("pattern") or ""),
                session_id=_session_of(body),
                scope=scope,
            )
        if action != "revoke":
            raise BridgeError("an allowance action is add or revoke")
        allowance = str(body.get("id") or "").strip()
        if not allowance:
            raise BridgeError("which allowance? an id is required")
        return self._bridge.revoke_allowance(
            allowance, session_id=_session_of(body), scope=scope
        )

    def _model(self, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("model") or "").strip()
        if not name:
            raise BridgeError("which model? a name is required")
        return self._bridge.select_model(
            name,
            session_id=_session_of(body),
            provider=_optional_str(body.get("provider")),
            base_url=_optional_url(body.get("base_url")),
        )

    def _providers(self, body: dict[str, Any]) -> dict[str, Any]:
        """Keys and the default provider (DR-036).

        A key travels in this POST body only — browser to server, over the
        same token- and origin-checked route as everything else — and never
        comes back: every answer carries the masked form.
        """
        action = str(body.get("action") or "").strip().lower()
        provider = str(body.get("provider") or "").strip()
        if action not in ("save_key", "delete_key", "set_default"):
            raise BridgeError(f"'{action}' is not save_key, delete_key, or set_default")
        if not provider:
            raise BridgeError("which provider? a name is required")
        if action == "save_key":
            key = body.get("api_key")
            if not isinstance(key, str) or not key.strip():
                raise BridgeError("save_key needs an api_key")
            return self._bridge.save_key(provider, key)
        if action == "delete_key":
            return self._bridge.delete_key(provider)
        model = str(body.get("model") or "").strip()
        if not model:
            raise BridgeError("which model? a name is required")
        return self._bridge.set_default(
            provider, model, base_url=_optional_url(body.get("base_url"))
        )

    def _automation(self, body: dict[str, Any]) -> dict[str, Any]:
        """Schedules and watchers (DR-047): one route, an ``action`` per change.

        A watcher's command runs without the per-call approval gate, because
        the person typed it here. That is why this route takes nothing a page
        did not send with the token, exactly like every other write.
        """
        action = str(body.get("action") or "").strip().lower()
        fields = {key: value for key, value in body.items() if key != "action"}
        return self._bridge.automation_action(action, fields)

    def _processes(self, body: dict[str, Any]) -> dict[str, Any]:
        """Stop or clear one background command (DR-062)."""
        identifier = str(body.get("id") or "").strip()
        if not identifier:
            raise BridgeError("which process? an id is required")
        return self._bridge.process_action(
            str(body.get("action") or "").strip().lower(), identifier
        )

    def _context(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            tokens = int(body.get("tokens") or 0)
        except (TypeError, ValueError):
            raise BridgeError("tokens must be a whole number") from None
        if tokens <= 0:
            raise BridgeError("how large a window? tokens is required")
        action = str(body.get("action") or "use").strip().lower()
        if action == "save_default":
            # DR-059: this session, and new ones in this workspace.
            return self._bridge.save_context_default(tokens, session_id=_session_of(body))
        if action != "use":
            raise BridgeError(f"unknown context action '{action}'")
        return self._bridge.set_context_window(tokens, session_id=_session_of(body))

    def _tier(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._bridge.request_tier(
            str(body.get("tier") or "").strip().lower(),
            session_id=_session_of(body),
        )

    def _undo(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._bridge.undo(_session_of(body))

    def _clear(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._bridge.clear_history(_session_of(body))

    def _reload(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._bridge.reload()

    def _session(self, body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action") or "").strip().lower()
        identifier = str(body.get("id") or "").strip()
        if action == "new":
            return self._bridge.new_session()
        if action == "rename":
            if not identifier:
                raise BridgeError("which session? an id is required")
            return self._bridge.rename_session(identifier, str(body.get("name") or ""))
        needs_id = {
            "resume": self._bridge.resume,
            "focus": self._bridge.focus,
            "close": self._bridge.close_session,
            "delete": self._bridge.delete_session,
        }
        handler = needs_id.get(action)
        if handler is None:
            raise BridgeError(
                f"'{action}' is not new, resume, focus, rename, close, or delete"
            )
        if not identifier:
            raise BridgeError("which session? an id is required")
        return handler(identifier)

    # -- event stream ------------------------------------------------------

    def _stream_events(self, query: dict[str, list[str]]) -> None:
        """Hold the connection open and write frames as they are published."""
        since = _int_or_zero(
            self.headers.get("Last-Event-ID") or _first(query.get("since"))
        )
        try:
            subscriber = self._bridge.subscribe(since=since)
        except BridgeError as exc:
            self._send_json({"error": str(exc)}, status=exc.status)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # No length and no chunking, so the close *is* the delimiter — which
        # HTTP/1.1 requires us to announce rather than leave to be discovered.
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        try:
            self._write_frame(0, {"kind": "open", "sequence": since})
            while not self.server.stopping:  # type: ignore[attr-defined]
                try:
                    sequence, frame = subscriber.frames.get(
                        timeout=_STREAM_PING_SECONDS
                    )
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if subscriber.lagged:
                    subscriber.lagged = False
                    # Say so rather than leave a gap: the browser refetches
                    # state instead of rendering a turn with holes in it.
                    self._write_frame(0, {"kind": "lagged"})
                self._write_frame(sequence, frame)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            # The tab was closed, or the network went away mid-write. Neither
            # is an error worth reporting; the subscriber is cleaned up below.
            pass
        finally:
            self._bridge.unsubscribe(subscriber)

    def _write_frame(self, sequence: int, frame: dict[str, Any]) -> None:
        payload = _dump(frame)
        prefix = f"id: {sequence}\n" if sequence else ""
        self.wfile.write(f"{prefix}data: {payload}\n\n".encode())
        self.wfile.flush()

    # -- helpers -----------------------------------------------------------

    @property
    def _bridge(self) -> SessionBridge:
        return self.server.bridge  # type: ignore[attr-defined]

    def _guarded(self, call) -> None:
        """Run one API call, mapping a refusal onto the status it deserves."""
        try:
            payload = call()
        except BridgeError as exc:
            self._send_json({"error": str(exc)}, status=exc.status)
            return
        except Exception as exc:  # noqa: BLE001 - see below
            # An unhandled failure in one request must not take down a server
            # that is mid-turn on another thread. It reaches the browser as an
            # error, and the traceback is the operator's to find in the log.
            self._send_json(
                {"error": f"{type(exc).__name__}: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._send_json(payload)

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        """Local-trust checks, in the order that fails cheapest."""
        if not self._host_allowed():
            self._send_json(
                {"error": "this dashboard does not answer for that host name"},
                status=HTTPStatus.MISDIRECTED_REQUEST,
            )
            return False
        if not self._origin_allowed():
            self._send_json(
                {"error": "cross-origin requests are refused"},
                status=HTTPStatus.FORBIDDEN,
            )
            return False
        presented = (
            self.headers.get("X-AutoSE-Token") or _first(query.get("token")) or ""
        )
        expected = self.server.token  # type: ignore[attr-defined]
        if not compare_digest(presented, expected):
            self._send_json(
                {
                    "error": (
                        "this dashboard needs the token it printed at startup. "
                        "Open the URL from the terminal that started it."
                    )
                },
                status=HTTPStatus.UNAUTHORIZED,
            )
            return False
        return True

    def _host_allowed(self) -> bool:
        allowed = self.server.allowed_hosts()  # type: ignore[attr-defined]
        if "*" in allowed:
            return True
        name = _hostname(self.headers.get("Host") or "")
        return name in allowed if name else False

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        allowed = self.server.allowed_hosts()  # type: ignore[attr-defined]
        if "*" in allowed:
            return True
        return _hostname(urlparse(origin).netloc) in allowed

    def _read_body(self) -> dict[str, Any] | None:
        length = _int_or_zero(self.headers.get("Content-Length"))
        if length > _MAX_BODY_BYTES:
            self._send_json(
                {"error": "that request is too large"},
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return None
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(
                {"error": "the request body is not JSON"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return None
        if not isinstance(body, dict):
            self._send_json(
                {"error": "the request body must be a JSON object"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return None
        return body

    def _send_asset(self, path: str) -> None:
        name, content_type = _ASSETS[path]
        try:
            payload = (STATIC_ROOT / name).read_bytes()
        except OSError:  # pragma: no cover - a broken install
            self._send_json(
                {"error": f"missing asset '{name}'"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._respond(payload, content_type=content_type)

    def _send_json(self, payload: Any, *, status: int = HTTPStatus.OK) -> None:
        self._respond(
            _dump(payload).encode("utf-8"),
            content_type="application/json; charset=utf-8",
            status=status,
        )

    def _respond(
        self,
        payload: bytes,
        *,
        content_type: str,
        status: int = HTTPStatus.OK,
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            # The page loads nothing from anywhere else, so say so: a strict
            # policy is free here and closes the injection routes a dashboard
            # rendering model output would otherwise leave open.
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; object-src 'none'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            # The browser hung up before we finished answering. Nothing to do.
            self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:  # stdlib signature
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)


def _dump(payload: Any) -> str:
    """JSON that cannot fail on a value a tool put in an event's data."""
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - `default=str` runs arbitrary __str__
        return json.dumps({"error": "a payload could not be serialised"})


def _serves_every_interface(host: str) -> bool:
    """Whether *host* names every interface rather than one address.

    Asked of the address itself rather than matched against a list of literals,
    so the IPv4 and IPv6 spellings of "anywhere" are both recognised.
    """
    if not host:
        return True
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def _hostname(netloc: str) -> str:
    """The host part of a ``Host``/``Origin`` value, without its port."""
    value = netloc.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end != -1 else value
    return value.split(":", 1)[0]


def _first(values: list[str] | None) -> str:
    return values[0] if values else ""


def _int_or_zero(value: str | None) -> int:
    try:
        return int(str(value or "0").strip() or 0)
    except ValueError:
        return 0


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def _optional_url(value: Any) -> str | None:
    """A base URL from a request body: absent, or an http(s) address."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise BridgeError("base_url must be an http:// or https:// address")
    return text


def _turn_of(query: dict[str, list[str]]) -> int | None:
    """The ``turn`` index a read asks for, or ``None`` for the latest (DR-040)."""
    raw = _first(query.get("turn"))
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise BridgeError("turn must be a whole number") from None


def _session_of(body: dict[str, Any]) -> str | None:
    """Which open session a write is about; ``None`` means the focused one."""
    text = str(body.get("session") or "").strip()
    return text or None
