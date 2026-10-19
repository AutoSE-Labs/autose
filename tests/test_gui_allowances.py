"""Allow rules that last, in the dashboard (DR-057).

A session's own rules are saved with it and come back when it reopens; rules
for every session in a workspace live in the user's config directory and
answer calls in any session there, except unattended ones.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest

gui = pytest.importorskip("gui", reason="the GUI adapter is a Pro-edition build")

from gui.bridge import BridgeError, SessionBridge  # noqa: E402
from gui.server import create_dashboard  # noqa: E402
from logic.common.allowances import Allowance  # noqa: E402
from logic.common.approval import ApprovalRequest  # noqa: E402
from logic.inference.registry import Provider, register_provider  # noqa: E402
from logic.saved_allowances import ALLOWANCES_FILENAME, AllowanceFile  # noqa: E402
from logic.user_config import user_config_dir  # noqa: E402
from tests.support.fakes import ScriptedChatModel, answer, calls, tool_call  # noqa: E402
from tests.test_gui import _MODEL, _bridge, _config, _drain, _request  # noqa: E402

ECHO = calls(tool_call("run_command", command="echo one"))


@pytest.fixture
def scripted():
    """The provider ``test_gui`` registers, scripted by the test."""
    holder: dict = {}

    class _Catalog:
        def __init__(self, settings):
            pass

        def list_models(self):
            return (_MODEL,)

    register_provider(
        Provider(
            name="test-dashboard",
            chat_model=lambda settings: holder["model"],
            catalog=_Catalog,
            is_local=True,
        ),
        replace=True,
    )
    return holder


@pytest.fixture
def served(scripted, tmp_path):
    """A dashboard on a real loopback socket, torn down after the test."""
    scripted["model"] = ScriptedChatModel([answer("hello")])
    bridge = SessionBridge(_config(tmp_path), default_tier="spark")
    dashboard = create_dashboard(bridge, host="127.0.0.1", port=0, token="test-token-1234")
    thread = threading.Thread(target=dashboard.serve_forever, daemon=True)
    thread.start()
    try:
        yield dashboard
    finally:
        dashboard.stop()
        dashboard.server_close()


def _asked(bridge) -> int:
    """How many calls were put to the person since the bridge started."""
    return sum(
        1 for _, frame in bridge.subscribe(since=0).frames.queue if frame.get("kind") == "approval"
    )


# -- a session's own rules are saved with it ---------------------------------------


def test_a_session_s_rules_come_back_when_it_reopens(scripted, tmp_path):
    bridge = _bridge(
        scripted,
        tmp_path,
        [ECHO, answer("done"), calls(tool_call("run_command", command="echo two")), answer("again")],
    )
    subscriber = bridge.subscribe()
    bridge.submit("run it")
    request = _drain(subscriber, "approval")["request"]
    bridge.resolve_approval(request["id"], approved=True, scope="session")
    session = bridge.snapshot()["session_id"]
    _drain(subscriber, "turn")

    bridge.close_session(session)
    assert bridge.snapshot()["allowances"] == []
    reopened = bridge.resume(session)

    assert [item["label"] for item in reopened["allowances"]] == ["echo … commands"]
    assert reopened["dropped_allowances"] == []
    bridge.submit("run it again")
    assert _drain(subscriber, "turn")["turn"]["answer"] == "again"
    assert _asked(bridge) == 1


def test_a_saved_rule_today_s_rules_refuse_is_dropped_on_reopening(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [answer("done")])
    subscriber = bridge.subscribe()
    bridge.submit("hello")
    _drain(subscriber, "turn")
    session = bridge.snapshot()["session_id"]
    bridge.close_session(session)
    # Saved and sealed here when the rules were looser; `rm` is once-only now.
    rules = (Allowance("run_command", "echo"), Allowance("run_command", "rm"))
    record = bridge._store.load(session)
    bridge._store.save(
        replace(
            record,
            allowances=tuple((item.tool, item.pattern) for item in rules),
            allowances_seal=AllowanceFile.for_workspace(tmp_path).seal(session, rules),
        )
    )

    reopened = bridge.resume(session)

    assert [item["pattern"] for item in reopened["allowances"]] == ["echo"]
    assert reopened["dropped_allowances"] == [
        "rm … commands: rm can only be allowed one call at a time"
    ]
    frame = _drain(subscriber, "allowances", session=session)
    assert frame["dropped"] == reopened["dropped_allowances"]


def test_rules_in_a_session_file_nobody_here_saved_are_not_restored(scripted, tmp_path):
    """A session file can come in a clone; it must not grant permissions."""
    bridge = _bridge(scripted, tmp_path, [answer("done")])
    subscriber = bridge.subscribe()
    bridge.submit("hello")
    _drain(subscriber, "turn")
    session = bridge.snapshot()["session_id"]
    bridge.close_session(session)
    record = bridge._store.load(session)
    bridge._store.save(
        replace(record, allowances=(("run_command", "python evil.py"),), allowances_seal="ab" * 32)
    )

    reopened = bridge.resume(session)

    assert reopened["allowances"] == []
    assert "not restored" in reopened["dropped_allowances"][0]


# -- rules for every session in the workspace ----------------------------------------


def test_a_workspace_rule_applies_in_every_session(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [ECHO, answer("ran unasked")])
    added = bridge.add_allowance("run_command", "echo", scope="workspace")
    assert [item["label"] for item in added["workspace_allowances"]] == ["echo … commands"]
    # Not a rule of the asking session's.
    assert added["allowances"] == []

    bridge.new_session()
    subscriber = bridge.subscribe()
    bridge.submit("run it")

    assert _drain(subscriber, "turn")["turn"]["answer"] == "ran unasked"
    assert _asked(bridge) == 0
    state = bridge.snapshot()
    assert state["allowances"] == []
    assert [item["id"] for item in state["workspace_allowances"]] == ["run_command:echo"]


def test_a_workspace_rule_answers_a_call_waiting_in_another_session(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [ECHO, answer("done")])
    subscriber = bridge.subscribe()
    bridge.submit("run it")
    waiting = bridge.snapshot()["session_id"]
    _drain(subscriber, "approval")
    other = bridge.new_session()["focus"]

    bridge.add_allowance("run_command", "echo", session_id=other, scope="workspace")

    closed = _drain(subscriber, "approval_closed", session=waiting)
    assert closed["approved"] is True
    assert "every session in this workspace" in closed["reason"]
    assert _drain(subscriber, "turn", session=waiting)["turn"]["answer"] == "done"


def test_keeping_a_session_rule_for_the_workspace_moves_it(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [answer("x")])
    bridge.add_allowance("run_command", "echo")
    assert [item["id"] for item in bridge.snapshot()["allowances"]] == ["run_command:echo"]

    moved = bridge.add_allowance("run_command", "echo", scope="workspace")

    assert moved["allowances"] == []
    assert [item["id"] for item in moved["workspace_allowances"]] == ["run_command:echo"]


def test_a_workspace_rule_can_be_taken_back(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [answer("x")])
    bridge.add_allowance("fetch_url", "docs.python.org", scope="workspace")

    removed = bridge.revoke_allowance("fetch_url:docs.python.org", scope="workspace")

    assert removed["workspace_allowances"] == []
    assert bridge.snapshot()["workspace_allowances"] == []
    with pytest.raises(BridgeError) as caught:
        bridge.revoke_allowance("fetch_url:docs.python.org", scope="workspace")
    assert caught.value.status == 404


def test_a_workspace_rule_is_checked_like_any_other(scripted, tmp_path):
    bridge = _bridge(scripted, tmp_path, [answer("x")])
    with pytest.raises(BridgeError):
        bridge.add_allowance("run_command", "rm", scope="workspace")
    with pytest.raises(BridgeError):
        bridge.add_allowance("run_command", "echo", scope="everywhere")
    assert bridge.snapshot()["workspace_allowances"] == []


def test_workspace_rules_are_kept_outside_the_workspace(scripted, tmp_path):
    """A repository must not be able to grant itself permissions."""
    bridge = _bridge(scripted, tmp_path, [answer("x")])
    bridge.add_allowance("run_command", "echo", scope="workspace")

    saved = user_config_dir() / ALLOWANCES_FILENAME
    assert saved.is_file()
    assert not saved.resolve().is_relative_to(tmp_path.resolve())
    assert not list(tmp_path.rglob(ALLOWANCES_FILENAME))
    # And nothing written into the workspace adds a rule.
    (tmp_path / ALLOWANCES_FILENAME).write_text(
        json.dumps({"workspaces": {str(tmp_path): [{"tool": "run_command", "pattern": "make"}]}}),
        encoding="utf-8",
    )
    assert [item["pattern"] for item in bridge.snapshot()["workspace_allowances"]] == ["echo"]


def test_an_unattended_run_is_not_answered_by_a_workspace_rule(scripted, tmp_path):
    """DR-047 holds: nobody is there, so the call is refused unless --yes."""
    bridge = _bridge(scripted, tmp_path, [answer("x")])
    bridge.add_allowance("run_command", "echo", scope="workspace")
    live = next(iter(bridge._open.values()))
    request = ApprovalRequest("run_command", {"command": "echo hi"})

    assert bridge._ask(live, request).approved is True
    live.unattended = True
    decision = bridge._ask(live, request)

    assert decision.approved is False
    assert decision.reason.startswith("unattended run:")


# -- the HTTP surface ----------------------------------------------------------------


def test_the_allowances_endpoint_takes_a_scope(served):
    status, added = _request(
        served,
        "/api/allowances",
        body={"action": "add", "tool": "run_command", "pattern": "pytest", "scope": "workspace"},
    )
    assert status == 200
    assert added["scope"] == "workspace"
    _, state = _request(served, "/api/state")
    assert [item["id"] for item in state["workspace_allowances"]] == ["run_command:pytest"]
    assert state["allowances"] == []

    status, removed = _request(
        served,
        "/api/allowances",
        body={"action": "revoke", "id": "run_command:pytest", "scope": "workspace"},
    )
    assert status == 200
    assert removed["workspace_allowances"] == []

    status, _ = _request(
        served,
        "/api/allowances",
        body={"action": "add", "tool": "run_command", "pattern": "pytest", "scope": "galaxy"},
    )
    assert status == 400

