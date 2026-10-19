"""The dashboard as a gateway: the runner, the API, the service mode (DR-047).

The runner is driven by calling ``tick()`` directly with a fake clock, so no
test waits thirty seconds or races a background thread.
"""

from __future__ import annotations

import os
import stat
import subprocess
import threading

import pytest

gui = pytest.importorskip("gui", reason="the GUI adapter is a Pro-edition build")

from gui.bridge import MAX_OPEN_SESSIONS, SessionBridge  # noqa: E402
from gui.server import create_dashboard  # noqa: E402
from gui.service import install_service, stable_token, unit_path  # noqa: E402
from logic.automation import CommandResult  # noqa: E402
from logic.inference.registry import Provider, register_provider  # noqa: E402
from tests.support.fakes import ScriptedChatModel, answer, calls, tool_call  # noqa: E402
from tests.test_gui import _MODEL, _config, _drain, _request  # noqa: E402


@pytest.fixture
def scripted():
    """The provider ``test_gui`` registers, giving each session its own model."""
    holder: dict = {}

    class _Catalog:
        def __init__(self, settings):
            pass

        def list_models(self):
            return (_MODEL,)

    register_provider(
        Provider(
            name="test-dashboard",
            chat_model=lambda settings: (
                holder["factory"](settings) if "factory" in holder else holder["model"]
            ),
            catalog=_Catalog,
            is_local=True,
        ),
        replace=True,
    )
    return holder


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += minutes * 60


class FakeCommand:
    def __init__(self, *outputs: tuple[int, str]) -> None:
        self.outputs = list(outputs)
        self.ran: list[str] = []

    def __call__(self, command, *, root, timeout):
        self.ran.append(command)
        code, text = self.outputs.pop(0)
        return CommandResult(code, text)


def _gateway(scripted, tmp_path, *, script=None, command=None, config=None):
    clock = Clock()
    scripted["factory"] = lambda settings: ScriptedChatModel(
        list(script or [answer("scheduled work done")])
    )
    bridge = SessionBridge(
        config or _config(tmp_path),
        default_tier="spark",
        clock=clock,
        command_runner=command or FakeCommand(),
    )
    return bridge, clock


def _collect(subscriber, kinds, timeout=5.0):
    """The first frame of each of *kinds*, in whatever order they arrive."""
    import queue

    found: dict = {}
    remaining = timeout
    while remaining > 0 and set(found) != set(kinds):
        try:
            _, frame = subscriber.frames.get(timeout=0.25)
        except queue.Empty:
            remaining -= 0.25
            continue
        if frame.get("kind") in kinds:
            found.setdefault(frame["kind"], frame)
    assert set(found) == set(kinds), f"only saw {sorted(found)}"
    return found


def _wait_idle(bridge, session_id, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if not bridge.is_running(session_id):
            return
        threading.Event().wait(0.02)
    raise AssertionError("the unattended session never finished")


# -- the runner ------------------------------------------------------------------


def test_a_due_schedule_opens_its_own_session_and_runs_the_prompt(scripted, tmp_path):
    bridge, clock = _gateway(scripted, tmp_path)
    focused = bridge.snapshot()["focus"]
    subscriber = bridge.subscribe()
    schedule = bridge.automation_action(
        "add_schedule", {"name": "Morning digest", "prompt": "summarise the log", "every": 30}
    )["schedule"]

    assert bridge._runner.tick()["ran"] == []  # not due yet
    clock.advance(30)
    report = bridge._runner.tick()

    [ran] = report["ran"]
    assert ran["type"] == "schedule" and ran["id"] == schedule["id"]
    # The turn runs on its own thread, so its frames and the runner's may
    # arrive in either order.
    frames = _collect(subscriber, {"automation", "turn"})
    assert frames["automation"]["ran"][0]["session"] == ran["session"]
    assert frames["turn"]["session"] == ran["session"]
    assert frames["turn"]["turn"]["prompt"] == "summarise the log"

    state = bridge.snapshot()
    # The person's view is not yanked to the scheduled run.
    assert state["focus"] == focused
    card = next(one for one in state["open"] if one["session_id"] == ran["session"])
    assert card["title"] == "Morning digest"
    assert card["automation"]["id"] == schedule["id"]
    stored = bridge.automation()["schedules"][0]
    assert stored["last_session"] == ran["session"]
    assert stored["last_run_at"] == clock.now
    assert stored["next_run_at"] == clock.now + 30 * 60


def test_a_full_desk_skips_the_run_and_retries_it_next_tick(scripted, tmp_path):
    bridge, clock = _gateway(scripted, tmp_path)
    while len(bridge.snapshot()["open"]) < MAX_OPEN_SESSIONS:
        bridge.new_session()
    bridge.automation_action("add_schedule", {"name": "n", "prompt": "p", "every": 1})
    clock.advance(1)

    report = bridge._runner.tick()

    assert report["ran"] == []
    assert "desk full" in report["skipped"][0]["reason"]
    assert bridge.automation()["schedules"][0]["last_outcome"].startswith("skipped: desk full")
    # Still due: room on the desk lets the next tick run it.
    bridge.close_session(bridge.snapshot()["open"][0]["session_id"])
    assert len(bridge._runner.tick()["ran"]) == 1


def test_an_unattended_run_s_approval_is_refused_at_once(scripted, tmp_path):
    """Nobody is there to answer it, so it does not wait fifteen minutes."""
    bridge, clock = _gateway(scripted, tmp_path)
    model = ScriptedChatModel(
        [calls(tool_call("run_command", command="make deploy")), answer("could not")]
    )
    scripted["factory"] = lambda settings: model
    subscriber = bridge.subscribe()
    bridge.automation_action("add_schedule", {"name": "n", "prompt": "deploy", "every": 1})
    clock.advance(1)
    session = bridge._runner.tick()["ran"][0]["session"]

    # The default approval timeout is fifteen minutes; this lands in seconds.
    _drain(subscriber, "turn", session=session, timeout=3.0)
    tool_results = [
        message["content"]
        for message in model.requests[-1]["messages"]
        if message.get("role") == "tool"
    ]
    assert any("unattended run: run_command needs a person" in text for text in tool_results)
    assert not any(
        one["waiting"] for one in bridge.snapshot()["open"] if one["session_id"] == session
    )
    assert bridge.automation()["unattended_approvals"] == "deny"


def test_the_denial_reason_tells_the_person_how_to_approve_it(scripted, tmp_path):
    from gui.bridge import _Live
    from logic.common.approval import ApprovalRequest

    bridge, _ = _gateway(scripted, tmp_path)
    live = next(iter(bridge._open.values()))
    assert isinstance(live, _Live)
    live.unattended = True
    decision = bridge._ask(live, ApprovalRequest("run_command", {"command": "ls"}))
    assert decision.approved is False
    assert decision.reason == (
        "unattended run: run_command needs a person; run it from the dashboard to approve it"
    )


def test_a_person_s_prompt_into_a_scheduled_session_asks_them_as_usual(scripted, tmp_path):
    """The denial says "run it from the dashboard" — and doing so must work."""
    bridge, clock = _gateway(
        scripted,
        tmp_path,
        script=[
            answer("checked, nothing to do"),
            calls(tool_call("run_command", command="make deploy")),
            answer("deployed"),
        ],
    )
    bridge.automation_action("add_schedule", {"name": "n", "prompt": "check", "every": 1})
    clock.advance(1)
    session = bridge._runner.tick()["ran"][0]["session"]
    _wait_idle(bridge, session)
    subscriber = bridge.subscribe()

    bridge.submit("now deploy it", session_id=session)
    request = _drain(subscriber, "approval", session=session)["request"]
    bridge.resolve_approval(request["id"], approved=True)

    assert _drain(subscriber, "approval_closed", session=session)["approved"] is True


def test_with_yes_unattended_approvals_are_reported_as_automatic(scripted, tmp_path):
    from dataclasses import replace

    config = replace(
        _config(tmp_path), auto_approve=True, auto_approve_authorized_by="me (dashboard --yes)"
    )
    bridge, _ = _gateway(scripted, tmp_path, config=config)
    payload = bridge.automation()
    assert payload["unattended_approvals"] == "auto"
    assert payload["authorized_by"] == "me (dashboard --yes)"


def test_a_watcher_records_a_baseline_then_runs_its_prompt_on_change(scripted, tmp_path):
    command = FakeCommand((0, "in_progress"), (0, "in_progress"), (0, "completed failure"))
    bridge, clock = _gateway(scripted, tmp_path, command=command)
    watcher = bridge.automation_action(
        "add_watcher",
        {
            "name": "CI",
            "command": "gh run list --limit 1 --json status,conclusion",
            "every_minutes": 5,
            "trigger": "change",
            "prompt_template": "CI now says: {output}",
        },
    )["watcher"]

    first = bridge._runner.tick()
    assert first["checked"][0]["outcome"] == "baseline recorded"
    assert first["ran"] == []
    clock.advance(5)
    assert bridge._runner.tick()["checked"][0]["outcome"] == "no change"
    clock.advance(5)
    third = bridge._runner.tick()

    assert third["ran"][0]["id"] == watcher["id"]
    session = third["ran"][0]["session"]
    _wait_idle(bridge, session)
    opened = bridge.snapshot()
    card = next(one for one in opened["open"] if one["session_id"] == session)
    assert card["title"] == "CI"
    stored = bridge.automation()["watchers"][0]
    assert stored["last_session"] == session
    assert stored["pending"] is False
    assert command.ran == [watcher["command"]] * 3


def test_a_schedule_whose_last_run_is_still_working_is_not_started_twice(scripted, tmp_path):
    bridge, clock = _gateway(scripted, tmp_path)
    schedule = bridge.automation_action(
        "add_schedule", {"name": "n", "prompt": "p", "every": 1}
    )["schedule"]
    bridge._automation.record_schedule_run(schedule["id"], now=clock.now, session="busy")
    bridge.is_running = lambda session_id: session_id == "busy"  # type: ignore[method-assign]
    clock.advance(1)

    report = bridge._runner.tick()
    assert report["ran"] == []
    assert report["skipped"][0]["reason"] == "its previous run is still working"


def test_the_runner_thread_starts_and_stops(scripted, tmp_path):
    bridge, _ = _gateway(scripted, tmp_path)
    assert bridge.automation()["running"] is False
    bridge.start_automation()
    try:
        assert bridge.automation()["running"] is True
        assert bridge.snapshot()["automation"]["running"] is True
    finally:
        bridge.shutdown()
    assert bridge.automation()["running"] is False


# -- the HTTP API ---------------------------------------------------------------


@pytest.fixture
def gateway(scripted, tmp_path):
    bridge, _ = _gateway(scripted, tmp_path, command=FakeCommand((0, "ok")))
    dashboard = create_dashboard(bridge, host="127.0.0.1", port=0, token="test-token-1234")
    thread = threading.Thread(target=dashboard.serve_forever, daemon=True)
    thread.start()
    try:
        yield dashboard
    finally:
        dashboard.stop()
        dashboard.server_close()


def test_the_automation_api_lists_adds_updates_and_deletes(gateway):
    status, empty = _request(gateway, "/api/automation")
    assert status == 200
    assert empty["schedules"] == [] and empty["watchers"] == []
    assert empty["unattended_approvals"] == "deny"
    assert empty["running"] is False

    status, added = _request(
        gateway,
        "/api/automation",
        body={"action": "add_schedule", "name": "Digest", "prompt": "p", "every": "0 9 * * 1-5"},
    )
    assert status == 200
    schedule = added["schedule"]
    assert schedule["every_kind"] == "cron"
    assert added["automation"]["schedules"][0]["id"] == schedule["id"]

    status, updated = _request(
        gateway,
        "/api/automation",
        body={"action": "update_schedule", "id": schedule["id"], "enabled": False},
    )
    assert status == 200
    assert updated["schedule"]["enabled"] is False
    assert updated["schedule"]["next_run_at"] is None

    _, state = _request(gateway, "/api/state")
    assert state["automation"]["schedules"] == 1
    assert state["automation"]["next_run_at"] is None

    status, deleted = _request(
        gateway, "/api/automation", body={"action": "delete_schedule", "id": schedule["id"]}
    )
    assert status == 200
    assert deleted["deleted"] == schedule["id"]


def test_an_invalid_definition_is_a_400_with_the_reason(gateway):
    status, payload = _request(
        gateway,
        "/api/automation",
        body={"action": "add_schedule", "name": "x", "prompt": "p", "every": "61 * * * *"},
    )
    assert status == 400
    assert "minute" in payload["error"]

    status, payload = _request(
        gateway,
        "/api/automation",
        body={
            "action": "add_watcher",
            "name": "x",
            "command": "cat /etc/hosts",
            "every_minutes": 5,
            "prompt_template": "p",
        },
    )
    assert status == 400
    assert "absolute path" in payload["error"]

    status, payload = _request(gateway, "/api/automation", body={"action": "explode"})
    assert status == 400


def test_an_unknown_id_is_a_404(gateway):
    for action in ("update_schedule", "delete_schedule", "run_now", "check_now", "delete_watcher"):
        status, payload = _request(
            gateway, "/api/automation", body={"action": action, "id": "missing"}
        )
        assert status == 404, action
        assert "missing" in payload["error"]


def test_run_now_and_check_now_act_immediately(gateway):
    _, added = _request(
        gateway,
        "/api/automation",
        body={"action": "add_schedule", "name": "Now", "prompt": "p", "every": 600},
    )
    status, ran = _request(
        gateway, "/api/automation", body={"action": "run_now", "id": added["schedule"]["id"]}
    )
    assert status == 200
    assert ran["ran"]["session"]
    assert ran["schedule"]["last_session"] == ran["ran"]["session"]

    _, watcher = _request(
        gateway,
        "/api/automation",
        body={
            "action": "add_watcher",
            "name": "W",
            "command": "make check",
            "every_minutes": 5,
            "trigger": "failure",
            "prompt_template": "fix it: {output}",
        },
    )
    status, checked = _request(
        gateway, "/api/automation", body={"action": "check_now", "id": watcher["watcher"]["id"]}
    )
    assert status == 200
    assert checked["check"]["baseline"] is True
    assert checked["ran"] is None
    assert checked["watcher"]["last_exit_code"] == 0


def test_the_automation_api_needs_the_token(gateway):
    status, _ = _request(gateway, "/api/automation", token="")
    assert status == 401
    status, _ = _request(
        gateway, "/api/automation", token="", body={"action": "add_schedule"}
    )
    assert status == 401


# -- service mode ---------------------------------------------------------------


def test_the_service_token_is_created_once_private_and_reused(tmp_path):
    first = stable_token(tmp_path)
    second = stable_token(tmp_path)
    assert first == second
    path = tmp_path / "dashboard.token"
    assert path.read_text().strip() == first
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_loose_token_file_is_tightened_and_a_short_one_replaced(tmp_path):
    path = tmp_path / "dashboard.token"
    path.write_text("x" * 40)
    path.chmod(0o644)
    assert stable_token(tmp_path) == "x" * 40
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("short")
    assert stable_token(tmp_path) != "short"


def test_the_service_token_lives_in_the_user_config_directory(isolated_user_config):
    token = stable_token()
    assert (isolated_user_config / "autose" / "dashboard.token").read_text().strip() == token


def test_install_service_writes_the_unit_and_never_runs_systemctl(
    tmp_path, monkeypatch, isolated_user_config
):
    def refuse(*args, **kwargs):
        raise AssertionError(f"a process was started: {args}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    lines: list[str] = []
    workspace = tmp_path / "my project"
    workspace.mkdir()

    code = install_service(
        workspace=workspace,
        port=8899,
        host="127.0.0.1",
        platform="linux",
        python="/usr/bin/python3",
        out=lines.append,
    )

    assert code == 0
    unit = unit_path()
    assert unit == isolated_user_config / "systemd" / "user" / "autose-dashboard.service"
    text = unit.read_text()
    assert (
        f'ExecStart=/usr/bin/python3 -m gui --service --workspace "{workspace.resolve()}" '
        "--host 127.0.0.1 --port 8899"
    ) in text
    assert "WantedBy=default.target" in text
    assert any("systemctl --user enable --now autose-dashboard" in line for line in lines)
    assert any("Nothing has been started" in line for line in lines)


def test_install_service_explains_itself_off_linux(tmp_path):
    lines: list[str] = []
    code = install_service(
        workspace=tmp_path, port=8765, host="127.0.0.1", platform="darwin", out=lines.append
    )
    assert code == 2
    assert "not supported here" in lines[0]
    assert not unit_path().exists()


def test_the_terminal_cli_hands_service_flags_to_the_dashboard(monkeypatch):
    from tui import cli

    seen: list[list[str]] = []
    import gui.cli as gui_cli

    monkeypatch.setattr(gui_cli, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["--install-service", "--port", "9000"]) == 0
    assert cli.main(["--service"]) == 0
    assert "--install-service" in seen[0] and "9000" in seen[0]
    assert "--service" in seen[1]


def test_service_mode_never_opens_a_browser_and_uses_the_stable_token(
    scripted, tmp_path, monkeypatch
):
    import webbrowser

    import gui.cli as gui_cli
    from gui import server

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    captured = {}

    real = server.create_dashboard

    def capture(bridge, **kwargs):
        dashboard = real(bridge, **{**kwargs, "port": 0})
        captured["dashboard"] = dashboard
        # Stop as soon as it starts serving.
        threading.Timer(0.2, dashboard.shutdown).start()
        return dashboard

    monkeypatch.setattr(server, "create_dashboard", capture)
    scripted["model"] = ScriptedChatModel([answer("hi")])
    code = gui_cli.serve(_config(tmp_path), port=0, service=True, open_browser=True)

    assert code == 0
    assert opened == []
    assert captured["dashboard"].token == stable_token()
