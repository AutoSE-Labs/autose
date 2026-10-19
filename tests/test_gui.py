"""The web-dashboard Adapter (DR-002, DR-013).

Two things are under test here, and the second matters as much as the first.

The dashboard itself: a turn started over HTTP reaches the same
``logic.session.Session`` the terminal drives, an approval-required call is put
to the browser and *denied* when nobody answers it (DR-012), and the local
trust boundary — token, ``Host``, ``Origin`` — actually refuses what it claims
to.

And the seam: this whole module skips cleanly when ``src/gui/`` is absent,
because DR-013 says a public-edition build deletes that folder and changes
nothing else. A test suite that went red on deletion would make that claim
false in the only way anyone would notice.
"""

from __future__ import annotations

import json
import queue
import threading
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from logic.common.approval import ApprovalRequest

gui = pytest.importorskip("gui", reason="the GUI adapter is a Pro-edition build")

from gui.bridge import MAX_OPEN_SESSIONS, BridgeError, SessionBridge  # noqa: E402
from gui.server import STATIC_ROOT, create_dashboard  # noqa: E402
from logic.config import AutoSEConfig  # noqa: E402
from logic.inference.catalog import ModelInfo  # noqa: E402
from logic.inference.registry import Provider, register_provider  # noqa: E402
from logic.inference.settings import InferenceSettings  # noqa: E402
from tests.support.fakes import ScriptedChatModel, answer, calls, tool_call  # noqa: E402

_MODEL = ModelInfo(
    name="gemma4:12b", context_length=32768, supports_tools=True, parameter_size="11.9B"
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scripted():
    """Register a provider whose chat model the test scripts."""
    holder: dict = {}

    class _Catalog:
        def __init__(self, settings):
            pass

        def list_models(self):
            return holder.get("models", (_MODEL,))

    register_provider(
        Provider(
            name="test-dashboard",
            # A `factory` is how a test gives *each* open session its own chat
            # model, which is what concurrent turns need: one ScriptedChatModel
            # shared between two live sessions would replay one script twice.
            chat_model=lambda settings: (
                holder["factory"](settings)
                if "factory" in holder
                else holder["model"]
            ),
            catalog=_Catalog,
            is_local=True,
        ),
        replace=True,
    )
    return holder


def _config(tmp_path) -> AutoSEConfig:
    return AutoSEConfig(
        inference=InferenceSettings(
            base_url="http://test/v1", model=_MODEL.name, provider="test-dashboard"
        ),
        workspace_root=tmp_path,
    )


def _bridge(scripted, tmp_path, script, **kwargs) -> SessionBridge:
    scripted["model"] = ScriptedChatModel(script)
    kwargs.setdefault("default_tier", "spark")
    return SessionBridge(_config(tmp_path), **kwargs)


def _drain(subscriber, kind: str, *, session: str | None = None, timeout: float = 5.0):
    """Wait for the next frame of *kind*, or fail saying what did arrive.

    *session* narrows it to one card's frames, which is the only way to assert
    on a dashboard where two turns are streaming at once (DR-031).
    """
    seen: list[str] = []
    deadline = timeout
    while deadline > 0:
        try:
            _, frame = subscriber.frames.get(timeout=0.25)
        except queue.Empty:
            deadline -= 0.25
            continue
        seen.append(frame.get("kind", "?"))
        if frame.get("kind") == kind and session in (None, frame.get("session")):
            return frame
    raise AssertionError(f"no '{kind}' frame arrived; saw {seen}")


class _Gated(ScriptedChatModel):
    """A chat model whose turn parks until the test lets it go.

    The stand-in for a real generation that takes minutes: it is the only way
    to assert what the dashboard permits *while* something is running, which is
    the whole subject of DR-031.
    """

    def __init__(self, script, *, gate: threading.Event | None = None) -> None:
        super().__init__(script)
        self.gate = gate
        #: Set once the turn is genuinely under way, so a test that acts next
        #: is not racing the worker thread into its first round.
        self.entered = threading.Event()

    def stream(self, messages, **kwargs):
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5.0)
        return super().stream(messages, **kwargs)


def _handing_out(*models):
    """A provider factory giving each session it is asked for the next model."""
    pending = list(models)

    def factory(settings=None):
        if pending:
            return pending.pop(0)
        return ScriptedChatModel([answer("nothing was asked of this one")])

    return factory


# -- the bridge ----------------------------------------------------------------


def test_a_dashboard_over_a_ready_runtime_reports_itself_ready(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [answer("hello")])
    state = bridge.snapshot()

    assert state["ready"] is True
    assert state["model"] == _MODEL.name
    assert state["workspace"] == str(tmp_path)
    assert [tier["name"] for tier in state["tiers"]] == ["spark", "craft"]
    # DR-012's gated set is read from logic, never re-declared here.
    assert set(state["gated_tools"]) == {"run_command", "web_search", "fetch_url"}


def test_a_turn_started_over_the_bridge_runs_and_lands_in_history(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [answer("the answer is 4")])
    subscriber = bridge.subscribe()

    bridge.submit("what is 2 + 2?")
    frame = _drain(subscriber, "turn")

    assert frame["turn"]["answer"] == "the answer is 4"
    assert frame["turn"]["succeeded"] is True
    assert frame["turn"]["tier"] == "spark"
    history = bridge.snapshot()["history"]
    assert [turn["prompt"] for turn in history] == ["what is 2 + 2?"]


def test_nothing_running_cannot_be_stopped(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [answer("hello")])
    with pytest.raises(BridgeError) as raised:
        bridge.interrupt()
    assert raised.value.status == 409


def test_stopping_a_running_turn_reaches_the_session_and_the_browser(
    scripted, tmp_path
):
    """DR-030: the dashboard's Stop, over a token rather than an exception.

    The turn is on a worker thread and this call is on the caller's, which is
    the whole reason the stop is a flag the loop reads.
    """
    holding = threading.Event()
    # `entered` is set once the turn is past the point where the session clears
    # its cancel token, so the stop below is not racing it.
    slow = _Gated([answer("done")], gate=holding)
    scripted["model"] = slow
    bridge = SessionBridge(_config(tmp_path), default_tier="spark")
    subscriber = bridge.subscribe()
    bridge.submit("something long")
    assert slow.entered.wait(5.0)
    try:
        assert bridge.interrupt()["stopping"] is True
        _drain(subscriber, "stopping")
        assert bridge.snapshot()["stopping"] is True
    finally:
        holding.set()
    _drain(subscriber, "turn")
    # And the flag is cleared, so the next turn is not born cancelled.
    assert bridge.snapshot()["stopping"] is False


def test_an_approval_reaches_the_browser_and_the_answer_reaches_the_tool(
    scripted, tmp_path
):
    (tmp_path / "note.txt").write_text("hello")
    bridge = _bridge(
        scripted,
        tmp_path,
        [calls(tool_call("run_command", command="ls -1")), answer("there is one file")],
    )
    subscriber = bridge.subscribe()

    bridge.submit("what is in this directory?")
    request = _drain(subscriber, "approval")["request"]
    assert request["tool"] == "run_command"
    assert request["summary"] == "ls -1"

    bridge.resolve_approval(request["id"], approved=False, reason="just read the file")
    closed = _drain(subscriber, "approval_closed")

    assert closed["approved"] is False
    turn = _drain(subscriber, "turn")["turn"]
    assert turn["succeeded"] is True


def test_an_approval_nobody_answers_is_denied(scripted, tmp_path):
    """DR-012: absent an answer, the posture is refusal — never a silent run."""
    bridge = _bridge(
        scripted,
        tmp_path,
        [calls(tool_call("run_command", command="rm -rf /")), answer("I did not")],
        approval_timeout=0.1,
    )
    subscriber = bridge.subscribe()

    bridge.submit("clean up")
    closed = _drain(subscriber, "approval_closed")

    assert closed["approved"] is False
    assert "nobody answered" in closed["reason"]


def test_a_reconnecting_browser_replays_the_frames_it_missed(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [answer("first answer")])
    bridge.submit("go")
    _drain(bridge.subscribe(), "turn")

    latecomer = bridge.subscribe(since=0)
    kinds = []
    while not latecomer.frames.empty():
        kinds.append(latecomer.frames.get_nowait()[1]["kind"])

    assert "turn" in kinds and "busy" in kinds


def test_a_dashboard_with_no_runtime_opens_and_explains_itself(tmp_path, monkeypatch):
    """A window that refused to open could not show the reason it refused."""
    monkeypatch.setattr(
        "logic.session.discover_local_runtimes", lambda **kwargs: ()
    )
    bridge = SessionBridge(AutoSEConfig(workspace_root=tmp_path))
    state = bridge.snapshot()

    assert state["ready"] is False
    assert state["problem"]
    with pytest.raises(BridgeError) as raised:
        bridge.submit("do something")
    assert raised.value.status == 503


def test_undo_reports_what_it_restored(scripted, tmp_path):
    target = tmp_path / "config.py"
    target.write_text("VALUE = 1\n")
    bridge = _bridge(
        scripted,
        tmp_path,
        [
            calls(
                tool_call(
                    "edit_file",
                    path="config.py",
                    edits=[{"old_str": "VALUE = 1", "new_str": "VALUE = 2"}],
                )
            ),
            answer("changed the value"),
        ],
    )
    subscriber = bridge.subscribe()
    bridge.submit("set VALUE to 2")
    _drain(subscriber, "turn")
    assert target.read_text() == "VALUE = 2\n"

    assert bridge.changes()["changes"][0]["path"] == "config.py"
    report = bridge.undo()

    assert report["restored"] == ["config.py"]
    assert target.read_text() == "VALUE = 1\n"


def test_nothing_to_undo_is_a_conflict_not_a_crash(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [answer("nothing written")])
    with pytest.raises(BridgeError) as raised:
        bridge.undo()
    assert raised.value.status == 409



# -- many sessions at once (DR-031) --------------------------------------------


def test_a_second_session_opens_and_runs_while_the_first_is_generating(
    scripted, tmp_path
):
    """DR-031, and the reason it exists: a long run must not lock the window.

    Everything asserted here used to be refused with a 409 for as long as any
    turn was running — opening a session, and running anything in it.
    """
    hold = threading.Event()
    slow = _Gated([answer("the long one")], gate=hold)
    quick = _Gated([answer("the quick one")])
    scripted["factory"] = _handing_out(slow, quick)
    bridge = SessionBridge(_config(tmp_path), default_tier="spark")
    subscriber = bridge.subscribe()

    long_running = bridge.submit("something that takes a while")["session"]
    assert slow.entered.wait(5.0)

    opened = bridge.new_session()
    fresh = opened["focus"]
    assert fresh != long_running
    assert [entry["session_id"] for entry in opened["open"]] == [long_running, fresh]

    bridge.submit("something quick", session_id=fresh)
    landed = _drain(subscriber, "turn", session=fresh)
    assert landed["turn"]["answer"] == "the quick one"

    # And the first turn was never touched by any of that.
    running = {
        entry["session_id"]: entry["running"] for entry in bridge.snapshot()["open"]
    }
    assert running == {long_running: True, fresh: False}
    hold.set()
    assert _drain(subscriber, "turn", session=long_running)["turn"]["answer"] == (
        "the long one"
    )


def test_a_second_prompt_into_the_same_session_is_still_refused(scripted, tmp_path):
    """One ``Session`` is not concurrent; a second session is the answer."""
    hold = threading.Event()
    slow = _Gated([answer("done")], gate=hold)
    scripted["factory"] = _handing_out(slow)
    bridge = SessionBridge(_config(tmp_path), default_tier="spark")
    target = bridge.submit("first")["session"]
    assert slow.entered.wait(5.0)
    try:
        with pytest.raises(BridgeError) as raised:
            bridge.submit("second", session_id=target)
        assert raised.value.status == 409
    finally:
        hold.set()


def test_switching_and_reading_sessions_is_never_refused_mid_run(scripted, tmp_path):
    hold = threading.Event()
    slow = _Gated([answer("done")], gate=hold)
    scripted["factory"] = _handing_out(slow)
    bridge = SessionBridge(_config(tmp_path), default_tier="spark")
    working = bridge.submit("hold on")["session"]
    assert slow.entered.wait(5.0)
    try:
        other = bridge.new_session()["focus"]
        assert bridge.focus(working)["focus"] == working
        assert bridge.focus(other)["focus"] == other
        # Reading a session that is mid-turn is a read, so it is allowed.
        assert bridge.changes(working)["changes"] == []
        assert set(bridge.sessions()["open"]) == {working, other}
    finally:
        hold.set()


def test_every_frame_names_the_session_it_came_from(scripted, tmp_path):
    """Without this the browser cannot tell two streaming cards apart."""
    bridge = _bridge(scripted, tmp_path, [answer("hi")])
    subscriber = bridge.subscribe()
    target = bridge.submit("go")["session"]

    frame = _drain(subscriber, "turn")
    assert frame["session"] == target
    # A frame about the dashboard rather than one session says so explicitly,
    # rather than leaving the field absent for the browser to guess at.
    assert _drain(subscriber, "state")["session"] == ""


def test_stopping_one_session_leaves_the_other_working(scripted, tmp_path):
    """DR-030's stop is per session, because the token is per session."""
    first_hold = threading.Event()
    second_hold = threading.Event()
    first = _Gated([answer("first")], gate=first_hold)
    second = _Gated([answer("second")], gate=second_hold)
    scripted["factory"] = _handing_out(first, second)
    bridge = SessionBridge(_config(tmp_path), default_tier="spark")

    one = bridge.submit("one")["session"]
    assert first.entered.wait(5.0)
    two = bridge.new_session()["focus"]
    bridge.submit("two", session_id=two)
    assert second.entered.wait(5.0)
    try:
        assert bridge.interrupt(one)["session"] == one
        stopping = {
            entry["session_id"]: entry["stopping"]
            for entry in bridge.snapshot()["open"]
        }
        assert stopping == {one: True, two: False}
    finally:
        first_hold.set()
        second_hold.set()


def test_undo_is_refused_while_another_session_is_working(scripted, tmp_path):
    """DR-024 and DR-031: one workspace, so an undo cannot race a writer.

    Putting one session's files back while another run is reading and editing
    them would destroy work nobody asked to lose, and the two journals cannot
    see each other. So this is the one thing a busy desk genuinely blocks.
    """
    target = tmp_path / "config.py"
    target.write_text("VALUE = 1\n")
    hold = threading.Event()
    writer = ScriptedChatModel(
        [
            calls(
                tool_call(
                    "edit_file",
                    path="config.py",
                    edits=[{"old_str": "VALUE = 1", "new_str": "VALUE = 2"}],
                )
            ),
            answer("changed the value"),
        ]
    )
    other = _Gated([answer("still going")], gate=hold)
    scripted["factory"] = _handing_out(writer, other)
    bridge = SessionBridge(_config(tmp_path), default_tier="spark")
    subscriber = bridge.subscribe()

    wrote = bridge.submit("set VALUE to 2")["session"]
    _drain(subscriber, "turn", session=wrote)
    assert target.read_text() == "VALUE = 2\n"

    second = bridge.new_session()["focus"]
    bridge.submit("something else", session_id=second)
    assert other.entered.wait(5.0)
    try:
        with pytest.raises(BridgeError) as raised:
            bridge.undo(wrote)
        assert raised.value.status == 409
        assert "working" in str(raised.value)
        assert target.read_text() == "VALUE = 2\n"
    finally:
        hold.set()
    _drain(subscriber, "turn", session=second)
    # Once nothing else is running it goes through.
    assert bridge.undo(wrote)["restored"] == ["config.py"]


def test_a_working_session_cannot_be_closed_out_from_under_its_thread(
    scripted, tmp_path
):
    hold = threading.Event()
    slow = _Gated([answer("done")], gate=hold)
    scripted["factory"] = _handing_out(slow)
    bridge = SessionBridge(_config(tmp_path), default_tier="spark")
    working = bridge.submit("hold on")["session"]
    assert slow.entered.wait(5.0)
    try:
        with pytest.raises(BridgeError) as raised:
            bridge.close_session(working)
        assert raised.value.status == 409
        assert "stop" in str(raised.value)
    finally:
        hold.set()


def test_closing_the_last_open_session_leaves_a_fresh_one(scripted, tmp_path):
    """A dashboard with nothing to type into would be a dead window."""
    bridge = _bridge(scripted, tmp_path, [answer("hi")])
    only = bridge.snapshot()["focus"]

    state = bridge.close_session(only)

    assert len(state["open"]) == 1
    assert state["focus"] != only
    assert state["ready"] is True


def test_an_open_session_cannot_be_deleted_from_under_itself(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [answer("hi")])
    subscriber = bridge.subscribe()
    target = bridge.submit("go")["session"]
    _drain(subscriber, "turn")

    with pytest.raises(BridgeError) as raised:
        bridge.delete_session(target)
    assert raised.value.status == 409


def test_the_desk_is_capped_rather_than_unbounded(scripted, tmp_path):
    """Every open session is a thread and a model connection when it runs."""
    bridge = _bridge(scripted, tmp_path, [answer("hi")])
    for _ in range(MAX_OPEN_SESSIONS - 1):
        bridge.new_session()

    with pytest.raises(BridgeError) as raised:
        bridge.new_session()
    assert raised.value.status == 409
    assert len(bridge.snapshot()["open"]) == MAX_OPEN_SESSIONS


def test_resuming_an_open_session_focuses_it_rather_than_opening_it_twice(
    scripted, tmp_path
):
    """Two ``Session`` objects over one stored id would overwrite each other."""
    bridge = _bridge(scripted, tmp_path, [answer("hi")])
    subscriber = bridge.subscribe()
    first = bridge.submit("go")["session"]
    _drain(subscriber, "turn")
    second = bridge.new_session()["focus"]

    state = bridge.resume(first)

    assert state["focus"] == first
    assert [entry["session_id"] for entry in state["open"]] == [first, second]


def test_an_approval_parked_in_one_session_does_not_hold_up_another(
    scripted, tmp_path
):
    """DR-012 meets DR-031: a call waiting on a human blocks only its own turn.

    Under the old single-session bridge this was the worst version of the
    freeze — a shell command nobody had noticed held the entire dashboard for
    up to fifteen minutes.
    """
    asking = ScriptedChatModel(
        [calls(tool_call("run_command", command="ls -1")), answer("listed them")]
    )
    other = ScriptedChatModel([answer("unblocked")])
    scripted["factory"] = _handing_out(asking, other)
    bridge = SessionBridge(
        _config(tmp_path), default_tier="spark", approval_timeout=5.0
    )
    subscriber = bridge.subscribe()

    waiting = bridge.submit("what is in this directory?")["session"]
    request = _drain(subscriber, "approval", session=waiting)["request"]

    second = bridge.new_session()["focus"]
    bridge.submit("something else", session_id=second)
    assert _drain(subscriber, "turn", session=second)["turn"]["answer"] == "unblocked"
    parked = {
        entry["session_id"]: entry["waiting"] for entry in bridge.snapshot()["open"]
    }
    assert parked == {waiting: 1, second: 0}

    # And it is answered by id alone: the browser answers the call it was shown
    # without having to say which card it came from.
    bridge.resolve_approval(request["id"], approved=True)
    assert _drain(subscriber, "turn", session=waiting)["turn"]["succeeded"] is True


def test_a_second_run_in_one_workspace_is_announced_not_hidden(scripted, tmp_path):
    """The cost of the concurrency: two journals that cannot see each other."""
    hold = threading.Event()
    slow = _Gated([answer("one")], gate=hold)
    quick = _Gated([answer("two")])
    scripted["factory"] = _handing_out(slow, quick)
    bridge = SessionBridge(_config(tmp_path), default_tier="spark")
    subscriber = bridge.subscribe()

    first = bridge.submit("one")["session"]
    assert slow.entered.wait(5.0)
    second = bridge.new_session()["focus"]
    bridge.submit("two", session_id=second)
    try:
        frame = _drain(subscriber, "concurrent")
        assert set(frame["sessions"]) == {first, second}
        assert frame["workspace"] == str(tmp_path)
    finally:
        hold.set()


# -- the HTTP surface ----------------------------------------------------------


@pytest.fixture
def served(scripted, tmp_path):
    """A dashboard on a real loopback socket, torn down after the test."""
    scripted["model"] = ScriptedChatModel([answer("hello from the dashboard")])
    bridge = SessionBridge(_config(tmp_path), default_tier="spark")
    dashboard = create_dashboard(
        bridge, host="127.0.0.1", port=0, token="test-token-1234"
    )
    thread = threading.Thread(target=dashboard.serve_forever, daemon=True)
    thread.start()
    try:
        yield dashboard
    finally:
        dashboard.stop()
        dashboard.server_close()


def _request(dashboard, path, *, token="test-token-1234", body=None, headers=None):
    port = dashboard.server_address[1]
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
    )
    if token:
        request.add_header("X-AutoSE-Token", token)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def test_the_page_and_its_state_are_served(served):
    status, payload = _request(served, "/api/state")
    assert status == 200
    assert payload["ready"] is True

    port = served.server_address[1]
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/?token=test-token-1234", timeout=5
    ) as response:
        page = response.read().decode()
    assert "<title>AutoSE</title>" in page
    assert response.headers["Content-Security-Policy"]


def test_without_the_token_nothing_is_reachable(served):
    status, payload = _request(served, "/api/state", token="")
    assert status == 401
    assert "token" in payload["error"]

    status, _ = _request(served, "/api/state", token="not-the-token")
    assert status == 401


def test_a_foreign_host_header_is_refused(served):
    """The check that stops DNS rebinding turning a web page into a client."""
    status, _ = _request(
        served, "/api/state", headers={"Host": "autose.attacker.example"}
    )
    assert status == 421


def test_a_cross_origin_request_is_refused(served):
    status, _ = _request(
        served, "/api/state", headers={"Origin": "https://attacker.example"}
    )
    assert status == 403


def test_an_unknown_endpoint_is_a_clean_404(served):
    status, payload = _request(served, "/api/whatever")
    assert status == 404
    assert payload["error"]


def test_a_prompt_posted_over_http_runs_the_turn(served):
    status, payload = _request(served, "/api/prompt", body={"prompt": "say hello"})
    assert status == 200
    assert payload["started"] is True

    for _ in range(80):
        _, state = _request(served, "/api/state")
        if state["history"]:
            break
        socket_sleep()
    assert state["history"][0]["answer"] == "hello from the dashboard"


def test_an_empty_prompt_is_refused_with_a_reason(served):
    status, payload = _request(served, "/api/prompt", body={"prompt": "   "})
    assert status == 400
    assert "nothing to run" in payload["error"]


def test_the_event_stream_opens_and_announces_itself(served):
    port = served.server_address[1]
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/events?token=test-token-1234"
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.headers["Content-Type"].startswith("text/event-stream")
        first = response.readline() + response.readline()
    assert b'"kind": "open"' in first or b'"kind":"open"' in first


def socket_sleep(seconds: float = 0.05) -> None:
    """A short wait that does not need `time` imported for one call."""
    ready = threading.Event()
    ready.wait(seconds)


# -- the seam DR-013 depends on ------------------------------------------------


def test_the_gui_is_not_referenced_by_the_packaging_entry_points():
    """Deleting src/gui must not leave a console script pointing into it."""
    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    scripts = document.get("project", {}).get("scripts", {})
    assert not [
        name for name, target in scripts.items() if target.startswith("gui")
    ], "a script entry point would break a build that drops src/gui/"


def test_the_gui_never_imports_the_terminal_adapter():
    """Two Adapters over one facade, not one Adapter wrapping another (DR-002)."""
    for path in sorted(Path(gui.__file__).parent.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "import tui" not in source and "from tui" not in source, path


def test_every_element_the_page_script_reaches_for_exists():
    """A typo in one id is a blank page, and only the browser would say so.

    The script wires itself up at load, so a missing element is a TypeError
    before anything renders — cheap to assert here, expensive to discover.
    """
    import re

    script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    page = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    wanted = set(re.findall(r'\bel\("([^"]+)"\)', script))
    present = set(re.findall(r'\bid="([^"]+)"', page))

    assert wanted <= present, f"app.js reaches for missing element(s) {sorted(wanted - present)}"


def test_the_live_card_is_updated_in_place_rather_than_rebuilt():
    """Why the streaming answer used to flicker on its last line.

    `renderRun` runs on every animation frame while a stage streams. Assigning
    to the card's own `innerHTML` there destroys and re-creates the answer
    block a few times a second: it collapses to nothing and re-expands, the
    scroll position goes with it, and the CSS animation on the status dot
    restarts. The card is built once and its parts written individually
    instead, so this asserts the shape of the fix rather than trusting it.
    """
    script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    body = script.split("function renderRun()", 1)[1].split("\nfunction ", 1)[0]

    assert "innerHTML" not in body, "renderRun must not rebuild the card wholesale"
    assert "setHtml(" in body and "setText(" in body
    # And the pinned-to-bottom check is read before the card grows, or it
    # answers "they scrolled away" on every delta.
    assert body.index("isPinnedToBottom()") < body.index("setHtml(")


def test_the_page_offers_two_ways_to_stop_a_run():
    """DR-030: on the card the user is watching, and in the composer.

    One is contextual and one is always in reach — a long run's card scrolls
    away, and the composer does not.
    """
    script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    page = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'id="stop-button"' in page, "the composer needs a stop control"
    assert 'data-stop="1"' in script, "the live card needs a stop control"
    # Both reach the one endpoint, and Stop never stands beside a dead,
    # disabled send button: while a run works, that button queues the next
    # request instead (DR-042).
    assert script.count("requestStop()") >= 2
    assert "/api/interrupt" in script
    assert 'state.running ? "Queue" : "Start"' in script
    assert "dom.send.disabled = !state.ready;" in script


def test_the_page_learns_a_turn_is_running_from_the_busy_frame():
    """Why the Stop button was invisible until it did.

    The bridge broadcasts a full state frame only when a turn *ends*; while one
    runs, the browser's only news is the `busy` frame. Everything that renders
    off `state.running` — the composer, the tier and model pickers, the presence
    label, Stop — therefore has to be told by that frame.
    """
    script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    busy = script.split('case "busy":', 1)[1].split("break;", 1)[0]

    assert "state.running = Boolean(frame.running)" in busy


def test_the_dashboard_fetches_nothing_from_the_network():
    """Local-first is a property of the page too, not only of inference."""
    for name in ("index.html", "app.css", "app.js", "inspector.js", "settings.js", "notify.js", "automation.js", "route.js", "life.js", "diff.js", "files.js"):
        # The SVG namespace inside an inline data: image is a name the SVG
        # parser requires, never a request, so it is the one URL allowed.
        source = (
            (STATIC_ROOT / name)
            .read_text(encoding="utf-8")
            .replace("xmlns='http://www.w3.org/2000/svg'", "")
        )
        for marker in ("http://", "https://", "//cdn", "fonts.googleapis"):
            assert marker not in source, f"{name} reaches out to {marker}"


def test_the_rail_is_not_gated_on_a_running_turn():
    """The dimming and the swallowed clicks were the freeze the user felt.

    Three things used to say "you can do nothing until this finishes": the rail
    click handler returned early while `state.running`, the New session button
    was disabled by it, and CSS greyed every row out. DR-031 removes all three,
    so this pins them out rather than trusting the diff.
    """
    script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    css = (STATIC_ROOT / "app.css").read_text(encoding="utf-8")
    handler = script.split('dom.sessionList.addEventListener("click"', 1)[1].split(
        "\n});", 1
    )[0]

    assert "state.running" not in handler, "the rail must stay usable mid-run"
    assert "dom.newSession.disabled = state.running" not in script
    assert ".is-running .session-row { opacity" not in css


def test_the_page_keeps_one_live_card_per_session():
    """A frame that did not say which session it came from would land wrongly.

    With two turns streaming at once, an untagged `answer` frame would append
    one session's tokens to the other's card. The page therefore reads
    `frame.session` and keeps its live state per id.
    """
    script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "const id = frame.session" in script
    assert "const desks = new Map()" in script
    # And only the focused session is painted; the rest accumulate in memory.
    assert "isFocused(run.session)" in script


# -- DR-033: a page opened mid-turn sees what the turn has already done --------


class _PausesBeforeRound(ScriptedChatModel):
    """Runs its first round, then parks the next one until the test says go."""

    def __init__(self, script, *, pause_at: int) -> None:
        super().__init__(script)
        self.pause_at = pause_at
        self.calls_seen = 0
        self.paused = threading.Event()
        self.go = threading.Event()

    def stream(self, messages, **kwargs):
        self.calls_seen += 1
        if self.calls_seen == self.pause_at:
            self.paused.set()
            self.go.wait(5.0)
        return super().stream(messages, **kwargs)


def test_a_snapshot_taken_mid_turn_carries_the_turn_so_far(scripted, tmp_path):
    """DR-033: a reloaded page used to show a bare "Thinking" card.

    The stream had already delivered the tool calls; a page that connected
    afterwards streams only from its snapshot onwards, so unless the snapshot
    carries the turn so far, everything before the reload is gone.
    """
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    model = _PausesBeforeRound(
        [calls(tool_call("read_file", path="notes.txt")), answer("read it")],
        pause_at=2,
    )
    scripted["model"] = model
    bridge = SessionBridge(_config(tmp_path), default_tier="spark")
    subscriber = bridge.subscribe()
    bridge.submit("read the notes")
    assert model.paused.wait(5.0)

    current = bridge.snapshot()["current"]
    kinds = [frame["event"] for frame in current["timeline"]]
    assert "tool_called" in kinds
    assert "round_usage" in kinds
    # Deltas are summarised by their tail, not replayed one by one.
    assert "agent_reasoning_delta" not in kinds
    assert "stage_output_delta" not in kinds

    model.go.set()
    _drain(subscriber, "turn")
    # A new turn starts from an empty timeline.
    assert bridge.snapshot()["current"] is None


def test_the_snapshot_and_the_stream_never_both_carry_one_event(scripted, tmp_path):
    """DR-033: replay the timeline, then stream from the snapshot's sequence.

    Every event the timeline holds must be at or before that sequence, or the
    page would draw it twice.
    """
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    model = _PausesBeforeRound(
        [calls(tool_call("read_file", path="notes.txt")), answer("read it")],
        pause_at=2,
    )
    scripted["model"] = model
    bridge = SessionBridge(_config(tmp_path), default_tier="spark")
    bridge.submit("read the notes")
    assert model.paused.wait(5.0)

    state = bridge.snapshot()
    after = bridge.subscribe(since=state["sequence"])
    model.go.set()
    streamed = []
    while True:
        _, frame = after.frames.get(timeout=5.0)
        if frame.get("kind") == "turn":
            break
        if frame.get("kind") == "event":
            streamed.append((frame["event"], frame["timestamp"]))
    replayed = {(f["event"], f["timestamp"]) for f in state["current"]["timeline"]}
    assert not replayed & set(streamed)


def test_the_snapshot_explains_the_served_context_window(scripted, tmp_path):
    """DR-034: the page shows a verdict, not a raw pair of numbers."""
    bridge = _bridge(scripted, tmp_path, [answer("x")])
    advice = bridge.snapshot()["context_advice"]
    assert advice["level"] in {"ok", "info", "warning", "blocking"}
    assert advice["served"] == 32768


# -- DR-035: choosing the context window from the dashboard ---------------------


@pytest.fixture
def adjustable(tmp_path):
    """A runtime that, like Ollama, serves the window each request asks for."""
    holder: dict = {}
    model = ModelInfo(
        name="qwen3.5:9b",
        context_length=32768,
        context_length_measured=True,
        max_context_length=131072,
        supports_tools=True,
    )

    class _Catalog:
        def __init__(self, settings):
            pass

        def list_models(self):
            return (model,)

    register_provider(
        Provider(
            name="test-window-dashboard",
            chat_model=lambda settings: holder.setdefault(
                "model", ScriptedChatModel([answer("ok")])
            ),
            catalog=_Catalog,
            is_local=True,
            serves_requested_window=True,
        ),
        replace=True,
    )
    config = AutoSEConfig(
        inference=InferenceSettings(
            base_url="http://test/v1",
            model=model.name,
            provider="test-window-dashboard",
        ),
        workspace_root=tmp_path,
    )
    return SessionBridge(config, default_tier="spark")


def test_the_dashboard_offers_windows_where_autose_chooses_them(adjustable):
    advice = adjustable.snapshot()["context_advice"]
    assert advice["adjustable"]
    tokens = [choice["tokens"] for choice in advice["choices"]]
    assert tokens == [8192, 16384, 32768, 65536, 131072]
    assert next(choice for choice in advice["choices"] if choice["current"])["tokens"] == 32768


def test_choosing_a_window_changes_what_the_session_asks_for(adjustable):
    state = adjustable.set_context_window(65536)
    assert state["context_limit"] == 65536
    assert state["context_advice"]["served"] == 65536


def test_a_window_the_model_cannot_serve_is_refused(adjustable):
    with pytest.raises(BridgeError):
        adjustable.set_context_window(262144)


def test_a_runtime_with_a_fixed_window_offers_no_choices(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [answer("x")])
    advice = bridge.snapshot()["context_advice"]
    assert advice["adjustable"] is False
    assert advice["choices"] == []
    with pytest.raises(BridgeError):
        bridge.set_context_window(65536)


def test_a_window_that_is_not_a_number_is_refused_over_http(served):
    status, payload = _request(served, "/api/context", body={"tokens": "lots"})
    assert status == 400
    assert "whole number" in json.dumps(payload)


# -- DR-037: the inspector ----------------------------------------------------


def test_the_inspector_script_is_served_with_the_page(served):
    port = served.server_address[1]
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/inspector.js", timeout=5
    ) as response:
        assert response.status == 200
        assert "activityPanelMarkup" in response.read().decode("utf-8")
    page = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    # Before app.js, which calls into it from its first paint.
    assert page.index("/inspector.js") < page.index("/app.js")


def test_the_inspector_has_the_elements_it_is_drawn_into():
    page = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    for element in ('id="inspector"', 'id="inspector-tabs"', 'id="inspector-body"', 'id="toggle-inspector"'):
        assert element in page


# -- DR-036: the settings page ---------------------------------------------------


def test_the_settings_script_is_served_and_loaded_before_the_page_script(served):
    port = served.server_address[1]
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/settings.js", timeout=5
    ) as response:
        assert response.status == 200
        assert "providerCardMarkup" in response.read().decode("utf-8")
    page = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    assert page.index("/settings.js") < page.index("/app.js")


def test_a_key_is_only_ever_typed_into_a_password_field():
    """DR-036: what is drawn on the settings page is the masked tail, never the key."""
    script = (STATIC_ROOT / "settings.js").read_text(encoding="utf-8")
    import re

    assert 'type="password"' in script
    assert "masked_key" in script
    # The renderer never reads a raw key field off the payload.
    assert not re.search(r"\.api_key\b|\[\s*[\"']api_key[\"']\s*\]", script)



# -- DR-040: any turn's diffs, on demand -----------------------------------------


def test_an_earlier_turn_s_diffs_can_be_read_by_its_index(scripted, tmp_path):
    (tmp_path / "notes.txt").write_text("draft\n")
    bridge = _bridge(
        scripted,
        tmp_path,
        [
            calls(
                tool_call(
                    "edit_file",
                    path="notes.txt",
                    edits=[{"old_str": "draft", "new_str": "first"}],
                )
            ),
            answer("wrote it"),
            answer("nothing to write this time"),
        ],
    )
    subscriber = bridge.subscribe()
    bridge.submit("write the notes")
    _drain(subscriber, "turn")
    bridge.submit("say something")
    _drain(subscriber, "turn")

    first = bridge.changes(turn=0)
    assert first["turn"] == 0
    (change,) = first["changes"]
    assert change["path"] == "notes.txt"
    assert "+first" in change["diff"]
    # The latest turn wrote nothing; asking for it by index says so.
    assert bridge.changes(turn=1)["changes"] == []


def test_a_turn_that_does_not_exist_is_a_404(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [answer("x")])
    with pytest.raises(BridgeError) as caught:
        bridge.changes(turn=7)
    assert caught.value.status == 404


def test_a_turn_index_that_is_not_a_number_is_refused_over_http(served):
    status, _payload = _request(served, "/api/changes?turn=latest")
    assert status == 400


def test_no_page_script_writes_inline_styles():
    """The CSP is default-src 'self' (DR-029), which drops inline style
    attributes without a word: a bar drawn with style="height:…" renders as
    nothing. Sizes go in SVG attributes or classes instead (DR-041)."""
    for name in ("app.js", "inspector.js", "settings.js", "notify.js", "automation.js", "route.js", "life.js", "diff.js", "files.js"):
        source = (STATIC_ROOT / name).read_text(encoding="utf-8")
        assert 'style="' not in source, f"{name} writes an inline style attribute"


# -- DR-042: a queue per session ---------------------------------------------------


def test_a_queued_prompt_starts_when_the_running_turn_ends(scripted, tmp_path):
    hold = threading.Event()
    model = _Gated([answer("the first"), answer("the second")], gate=hold)
    scripted["model"] = model
    bridge = SessionBridge(_config(tmp_path), default_tier="spark")
    subscriber = bridge.subscribe()

    session = bridge.submit("first request")["session"]
    assert model.entered.wait(5.0)
    queued = bridge.submit("second request", queue=True)
    assert queued["queued"] is True
    assert [item["prompt"] for item in bridge.snapshot()["queue"]] == ["second request"]

    hold.set()
    assert _drain(subscriber, "turn", session=session)["turn"]["answer"] == "the first"
    assert _drain(subscriber, "turn", session=session)["turn"]["answer"] == "the second"
    assert bridge.snapshot()["queue"] == []


def test_a_turn_that_fails_holds_the_queue_until_resumed(scripted, tmp_path):
    """DR-042: after a failure — or a stop — the next prompt waits for the user."""
    from logic.errors import InferenceError

    hold = threading.Event()
    model = _Gated([InferenceError("the runtime went away"), answer("the second")], gate=hold)
    scripted["model"] = model
    bridge = SessionBridge(_config(tmp_path), default_tier="spark")
    subscriber = bridge.subscribe()

    session = bridge.submit("first request")["session"]
    assert model.entered.wait(5.0)
    bridge.submit("second request", queue=True)
    hold.set()
    _drain(subscriber, "failed", session=session)
    _drain(subscriber, "busy", session=session)

    state = bridge.snapshot()
    assert state["queue_held"] is True
    assert [item["prompt"] for item in state["queue"]] == ["second request"]
    assert not state["running"]

    bridge.queue_action("resume", session_id=session)
    assert _drain(subscriber, "turn", session=session)["turn"]["answer"] == "the second"


def test_queued_prompts_can_be_reordered_and_removed(scripted, tmp_path):
    hold = threading.Event()
    model = _Gated([answer("a"), answer("b"), answer("c")], gate=hold)
    scripted["model"] = model
    bridge = SessionBridge(_config(tmp_path), default_tier="spark")
    bridge.submit("running")
    assert model.entered.wait(5.0)
    first = bridge.submit("one", queue=True)["id"]
    second = bridge.submit("two", queue=True)["id"]

    moved = bridge.queue_action("move", item_id=second, to=0)
    assert [item["prompt"] for item in moved["queue"]] == ["two", "one"]
    removed = bridge.queue_action("remove", item_id=first)
    assert [item["prompt"] for item in removed["queue"]] == ["two"]
    with pytest.raises(BridgeError) as caught:
        bridge.queue_action("remove", item_id="nope")
    assert caught.value.status == 404
    bridge.queue_action("clear")
    hold.set()


# -- DR-043: desktop notifications ---------------------------------------------


def test_notifications_are_opt_in_and_never_leave_the_machine(served):
    """DR-043: raised only after the person turns them on and the browser
    grants permission, through the browser itself — no server endpoint."""
    port = served.server_address[1]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/notify.js", timeout=5) as response:
        script = response.read().decode("utf-8")
    assert 'Notification.permission === "granted"' in script
    assert "notifyWanted()" in script
    page = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    assert page.index("/notify.js") < page.index("/app.js")
    assert 'id="toggle-notify"' in page


def test_an_approval_is_always_announced_when_the_window_is_elsewhere():
    script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    block = script[script.index('case "approval":') : script.index('case "approval_closed":')]
    assert "notifyAbout(" in block and "urgent: true" in block



# -- DR-044: allow a tool for the rest of the session ---------------------------


def test_allowing_a_tool_for_the_session_stops_asking_about_it(scripted, tmp_path):
    bridge = _bridge(
        scripted,
        tmp_path,
        [
            calls(tool_call("run_command", command="echo one")),
            calls(tool_call("run_command", command="echo two")),
            answer("ran both"),
        ],
    )
    subscriber = bridge.subscribe()
    bridge.submit("run two commands")
    first = _drain(subscriber, "approval")["request"]
    assert bridge.resolve_approval(first["id"], approved=True, scope="session")["scope"] == "session"
    turn = _drain(subscriber, "turn")["turn"]
    assert turn["answer"] == "ran both"
    # Only the first call was put to the person; the second ran on the allowance.
    asked = [
        frame
        for _, frame in bridge.subscribe(since=0).frames.queue
        if frame.get("kind") == "approval"
    ]
    assert len(asked) == 1
    assert [item["label"] for item in bridge.snapshot()["allowances"]] == ["echo … commands"]
    assert first["allow"]["label"] == "echo … commands"


def test_an_allowance_can_be_taken_back(scripted, tmp_path):
    bridge = _bridge(
        scripted,
        tmp_path,
        [calls(tool_call("run_command", command="echo one")), answer("done"), answer("x")],
    )
    subscriber = bridge.subscribe()
    bridge.submit("run it")
    request = _drain(subscriber, "approval")["request"]
    bridge.resolve_approval(request["id"], approved=True, scope="session")
    _drain(subscriber, "turn")
    assert bridge.revoke_allowance("run_command:echo")["allowances"] == []
    with pytest.raises(BridgeError) as caught:
        bridge.revoke_allowance("run_command:echo")
    assert caught.value.status == 404


def test_a_rule_added_by_hand_answers_the_calls_it_covers(scripted, tmp_path):
    """DR-050: a rule added in the dashboard works as one given from a card."""
    bridge = _bridge(
        scripted,
        tmp_path,
        [calls(tool_call("run_command", command="echo one 2>&1 | tail -1")), answer("done")],
    )
    subscriber = bridge.subscribe()
    bridge.submit("run it")
    _drain(subscriber, "approval")
    added = bridge.add_allowance("run_command", "echo")
    assert added["added"]["label"] == "echo … commands"
    assert _drain(subscriber, "turn")["turn"]["answer"] == "done"
    with pytest.raises(BridgeError):
        bridge.add_allowance("run_command", "rm")


def test_an_allowance_for_one_command_still_asks_about_another(scripted, tmp_path):
    """DR-048: allowing `echo` is not allowing every shell command."""
    bridge = _bridge(
        scripted,
        tmp_path,
        [
            calls(tool_call("run_command", command="echo one")),
            calls(tool_call("run_command", command="ls")),
            answer("done"),
        ],
    )
    subscriber = bridge.subscribe()
    bridge.submit("run two different commands")
    first = _drain(subscriber, "approval")["request"]
    bridge.resolve_approval(first["id"], approved=True, scope="session")
    second = _drain(subscriber, "approval")["request"]
    assert second["arguments"]["command"] == "ls"
    bridge.resolve_approval(second["id"], approved=False)
    _drain(subscriber, "turn")


def test_a_chain_that_writes_a_file_cannot_be_allowed_for_the_session(scripted, tmp_path):
    bridge = _bridge(
        scripted,
        tmp_path,
        [calls(tool_call("run_command", command="echo hi > notes.txt && echo there")), answer("done")],
    )
    subscriber = bridge.subscribe()
    bridge.submit("run it")
    request = _drain(subscriber, "approval")["request"]
    assert request["allow"] is None
    assert "writes to a file" in request["allow_note"]
    with pytest.raises(BridgeError) as caught:
        bridge.resolve_approval(request["id"], approved=True, scope="session")
    assert "only be allowed once" in str(caught.value)
    # Still waiting: refusing the wider yes did not answer the call.
    bridge.resolve_approval(request["id"], approved=True)
    assert _drain(subscriber, "turn")["turn"]["answer"] == "done"


def test_allowing_a_chain_allows_each_of_its_parts(scripted, tmp_path):
    """DR-052: `cd app && echo …` is a yes for echo; a later echo runs unasked."""
    bridge = _bridge(
        scripted,
        tmp_path,
        [
            calls(tool_call("run_command", command="cd . && echo one 2>&1 | tail -1")),
            calls(tool_call("run_command", command="echo two")),
            answer("ran both"),
        ],
    )
    subscriber = bridge.subscribe()
    bridge.submit("run two commands")
    first = _drain(subscriber, "approval")["request"]
    assert first["allow"]["patterns"] == ["echo"]
    bridge.resolve_approval(first["id"], approved=True, scope="session")
    assert _drain(subscriber, "turn")["turn"]["answer"] == "ran both"
    asked = [
        frame
        for _, frame in bridge.subscribe(since=0).frames.queue
        if frame.get("kind") == "approval"
    ]
    assert len(asked) == 1


def test_allowing_for_the_session_answers_matching_calls_already_waiting(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [answer("x")])
    live = bridge._target(None)
    from gui.bridge import _Pending

    first = _Pending(ApprovalRequest("run_command", {"command": "pytest -q"}))
    second = _Pending(ApprovalRequest("run_command", {"command": "pytest tests/x.py"}))
    other = _Pending(ApprovalRequest("run_command", {"command": "ls"}))
    for item in (first, second, other):
        live.pending[item.id] = item
    bridge.resolve_approval(first.id, approved=True, scope="session")
    assert second.answered.is_set() and second.decision.approved
    assert not other.answered.is_set()


def test_an_unknown_approval_scope_is_refused(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [answer("x")])
    with pytest.raises(BridgeError):
        bridge.resolve_approval("nope", approved=True, scope="forever")


# -- DR-045: plan review in the dashboard ----------------------------------------


def _ledger(kind: str, *ids: str) -> str:
    status = {"plan": "Not Started", "execute": "Implemented", "validate": "Verified"}[kind]
    lines = "\n".join(f"- {rid} [{status}] — Requirement {rid} — Evidence: x" for rid in ids)
    tail = "\n\nVERDICT: PASS" if kind == "validate" else ""
    return f"Text.\n\n## REQUIREMENTS STATUS\n{lines}{tail}"


def test_a_plan_waits_for_the_person_when_review_is_on(scripted, tmp_path):
    bridge = _bridge(
        scripted,
        tmp_path,
        [answer(_ledger("plan", "R1")), answer(_ledger("execute", "R1")), answer(_ledger("validate", "R1"))],
    )
    bridge.set_plan_review(True)
    subscriber = bridge.subscribe()
    bridge.submit("add a helper", tier="craft")

    review = _drain(subscriber, "plan_review")["review"]
    assert "REQUIREMENTS STATUS" in review["plan"]
    assert bridge.snapshot()["pending_plan"]["id"] == review["id"]

    bridge.resolve_plan(review["id"], action="approve")
    assert _drain(subscriber, "turn")["turn"]["succeeded"] is True
    assert bridge.snapshot()["pending_plan"] is None


def test_a_revise_without_saying_what_is_refused(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [answer("x")])
    with pytest.raises(BridgeError):
        bridge.resolve_plan("any", action="revise", feedback="  ")
    with pytest.raises(BridgeError):
        bridge.resolve_plan("any", action="approve-ish")


# -- the rail's strips: stage, counts, context and a state timeline ---------------


def test_a_strip_records_when_the_session_worked_and_waited(scripted, tmp_path):
    bridge = _bridge(
        scripted,
        tmp_path,
        [calls(tool_call("run_command", command="echo one")), answer("done")],
    )
    subscriber = bridge.subscribe()
    bridge.submit("run it")
    request = _drain(subscriber, "approval")["request"]
    bridge.resolve_approval(request["id"], approved=True)
    _drain(subscriber, "turn")
    _drain(subscriber, "busy")
    strip = bridge.snapshot()["open"][0]["strip"]
    states = [status for _, status in strip["lane"]]
    assert states == ["idle", "working", "waiting", "working", "idle"]
    times = [at for at, _ in strip["lane"]]
    assert times == sorted(times)


def test_a_strip_lane_keeps_only_the_last_half_hour():
    from gui.bridge import LANE_SECONDS, _Live

    live = _Live.__new__(_Live)
    live.lane = [(0.0, "idle")]
    live.mark("working", at=10.0)
    live.mark("idle", at=20.0)
    live.mark("working", at=20.0 + LANE_SECONDS + 5)
    # The state in force when the window opens is kept; older ones are not.
    assert [status for _, status in live.lane] == ["idle", "working"]


# -- renaming a session ------------------------------------------------------------


def test_a_session_can_be_renamed_open_or_stored(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [answer("hello")])
    subscriber = bridge.subscribe()
    first = bridge.snapshot()["session_id"]
    bridge.submit("say hello")
    _drain(subscriber, "turn")
    bridge.rename_session(first, "  Greeting   test ")
    open_row = next(item for item in bridge.snapshot()["open"] if item["session_id"] == first)
    assert open_row["title"] == "Greeting test" and open_row["named"]
    stored = next(item for item in bridge.sessions()["sessions"] if item["id"] == first)
    assert stored["name"] == "Greeting test"
    # An empty name goes back to the first request.
    bridge.rename_session(first, "")
    open_row = next(item for item in bridge.snapshot()["open"] if item["session_id"] == first)
    assert open_row["title"] == "say hello" and not open_row["named"]
    with pytest.raises(BridgeError):
        bridge.rename_session("no-such-session", "x")


# -- DR-053: YOLO mode -------------------------------------------------------------


def test_yolo_mode_answers_what_waits_and_asks_nothing_after(scripted, tmp_path):
    bridge = _bridge(
        scripted,
        tmp_path,
        [
            calls(tool_call("run_command", command="echo one")),
            calls(tool_call("run_command", command="rm -rf build")),
            answer("ran both"),
        ],
    )
    subscriber = bridge.subscribe()
    assert bridge.snapshot()["yolo"] is False
    bridge.submit("run two commands")
    _drain(subscriber, "approval")
    assert bridge.set_yolo(True)["yolo"] is True
    assert _drain(subscriber, "turn")["turn"]["answer"] == "ran both"
    asked = [
        frame
        for _, frame in bridge.subscribe(since=0).frames.queue
        if frame.get("kind") == "approval"
    ]
    assert len(asked) == 1  # only the call that was already waiting


def test_turning_yolo_off_asks_again(scripted, tmp_path):
    bridge = _bridge(
        scripted,
        tmp_path,
        [calls(tool_call("run_command", command="echo one")), answer("done")],
    )
    subscriber = bridge.subscribe()
    bridge.set_yolo(True)
    bridge.set_yolo(False)
    bridge.submit("run it")
    request = _drain(subscriber, "approval")["request"]
    bridge.resolve_approval(request["id"], approved=True)
    assert _drain(subscriber, "turn")["turn"]["answer"] == "done"
