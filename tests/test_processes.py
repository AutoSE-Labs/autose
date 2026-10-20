"""DR-062: commands that keep running are started, watched and stopped.

Every process here is a tiny Python loop or a sleep, stopped by the test that
started it, so the file stays fast and leaves nothing behind.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from logic.common.allowances import allowance_for_rule, offer_for, standing_for
from logic.common.approval import ApprovalRequest, AutoApproveGate
from logic.common.events import RecordingEventSink
from logic.common.execution import ToolExecutor
from logic.common.processes import (
    EXITED,
    STOPPED,
    SUPPORTED,
    ProcessError,
    ProcessManager,
    UnknownProcessError,
    background_form,
    stop_managers,
)
from logic.common.tool import clone_tools
from logic.common.tools import background as background_tools
from logic.common.tools import catalog_subset
from logic.common.tools.background import process_output, stop_process
from logic.common.tools.shell import run_command
from logic.common.workspace import Workspace
from logic.inference.catalog import ModelInfo
from logic.inference.registry import Provider, register_provider
from logic.inference.settings import InferenceSettings
from logic.session import Session
from tests.support.fakes import ScriptedChatModel, answer, calls, tool_call

PY = f'"{sys.executable}" -u -c'

#: Process groups, and so background commands, are POSIX (DR-062); elsewhere
#: they are refused, which the last test here checks on every system.
posix_only = pytest.mark.skipif(not SUPPORTED, reason="process groups are POSIX")

#: Prints a numbered line every 50 ms for far longer than any test waits.
#: `python3` rather than the interpreter's path: run_command refuses an
#: absolute path, and every machine that runs these tests has one on PATH.
LOOP = (
    'python3 -u -c "import time\n'
    "for i in range(10000):\n"
    "    print('tick', i)\n"
    '    time.sleep(0.05)"'
)


@pytest.fixture
def manager():
    events = RecordingEventSink()
    made = ProcessManager(events=events, stop_grace=0.5)
    made.recorded = events  # type: ignore[attr-defined]
    yield made
    made.stop_all()


@pytest.fixture(autouse=True)
def quick_first_look(monkeypatch):
    """`run_command` looks at a background command for 2 s; tests need less."""
    monkeypatch.setattr(background_tools, "FIRST_LOOK_SECONDS", 0.3)


def _until(check, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()


def _gone(pid: int) -> bool:
    """Whether *pid* has ended — a zombie waiting to be reaped counts."""
    stat = Path(f"/proc/{pid}/stat")
    try:
        state = stat.read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return True
    return state in {"Z", "X"}


# -- what counts as a background command ---------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python server.py &", "python server.py"),
        ("npm run dev  &  ", "npm run dev"),
        ("nohup python server.py > out.log 2>&1 &", "python server.py > out.log 2>&1"),
        ("make build && python serve.py &", "make build && python serve.py"),
        ("pytest -q", None),
        ("make a && make b", None),
        ("python server.py 2>&1", None),
        ("echo a\\&", None),
        ("&", None),
    ],
)
def test_a_trailing_ampersand_asks_to_keep_running(command, expected):
    assert background_form(command) == expected


# -- the manager -----------------------------------------------------------------


@posix_only
def test_output_is_kept_and_the_exit_is_detected(manager, tmp_path):
    process = manager.start(f"{PY} \"print('a'); print('b'); raise SystemExit(3)\"", cwd=tmp_path)

    assert process.wait(2.0)
    assert process.status == EXITED and process.exit_code == 3
    assert process.output().lines == ("a", "b")
    later = process.output(since=1)
    assert later.lines == ("b",) and later.cursor == 2
    types = [event.type for event in manager.recorded.events]
    assert types == ["process_started", "process_exited"]
    exited = manager.recorded.of_type("process_exited")[0]
    assert exited.data["exit_code"] == 3 and exited.data["status"] == EXITED


@posix_only
def test_only_a_bounded_tail_is_kept_and_a_reader_is_told_what_it_missed(tmp_path):
    small = ProcessManager(max_lines=5)
    process = small.start(f"{PY} \"[print(i) for i in range(20)]\"", cwd=tmp_path)

    assert process.wait(2.0)
    shown = process.output(since=0)
    assert shown.lines == ("15", "16", "17", "18", "19")
    assert shown.start == 15 and shown.skipped == 15 and shown.cursor == 20


@posix_only
def test_a_line_still_being_written_is_shown_but_not_counted(manager, tmp_path):
    process = manager.start(
        f"{PY} \"import sys, time; print('x'); sys.stdout.write('half'); "
        f'sys.stdout.flush(); time.sleep(30)"',
        cwd=tmp_path,
    )
    assert _until(lambda: process.output().partial == "half")
    assert process.output().cursor == 1
    manager.stop(process.id)


@posix_only
def test_stop_ends_the_whole_group_not_just_the_shell(manager, tmp_path):
    process = manager.start(
        f"{PY} \"import subprocess, time; child = subprocess.Popen(['sleep', '30']); "
        f'print(child.pid); time.sleep(30)"',
        cwd=tmp_path,
    )
    assert _until(lambda: bool(process.output().lines))
    grandchild = int(process.output().lines[0])

    manager.stop(process.id)

    assert process.status == STOPPED
    assert _until(lambda: _gone(grandchild))
    assert manager.recorded.of_type("process_exited")[0].data["status"] == STOPPED


@posix_only
def test_a_process_that_ignores_sigterm_is_killed_after_the_grace(tmp_path):
    impatient = ProcessManager(stop_grace=0.3)
    process = impatient.start(
        f"{PY} \"import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"print('ready'); time.sleep(30)\"",
        cwd=tmp_path,
    )
    assert _until(lambda: process.output().lines == ("ready",))

    started = time.monotonic()
    impatient.stop(process.id)

    assert time.monotonic() - started < 1.5
    assert process.status == STOPPED and process.exit_code == -9


@posix_only
def test_a_running_process_cannot_be_cleared_and_a_finished_one_can(manager, tmp_path):
    process = manager.start("sleep 30", cwd=tmp_path)
    with pytest.raises(ProcessError):
        manager.clear(process.id)

    manager.stop(process.id)
    manager.clear(process.id)

    assert manager.processes() == ()
    assert manager.recorded.of_type("process_cleared")[0].data["id"] == process.id
    with pytest.raises(UnknownProcessError):
        manager.get(process.id)


@posix_only
def test_what_an_ended_command_left_running_can_be_stopped_and_is_not_cleared(
    manager, workspace, monkeypatch
):
    """`./start.sh` that runs a server with `&` and exits has "exited 0" while
    the server goes on; it must stay stoppable, and on the list so closing the
    session still reaches it."""
    # Past the half second an exit waits for the last of the output.
    monkeypatch.setattr(background_tools, "FIRST_LOOK_SECONDS", 1.0)
    (workspace.root / "start.sh").write_text(
        f"{LOOP} &\necho started\n", encoding="utf-8"
    )
    first_look = run_command(
        "sh start.sh", background=True, workspace=workspace, processes=manager
    )
    (process,) = manager.processes()
    assert _until(lambda: not process.running)
    assert process.alive and process.to_dict()["alive"] is True
    assert "what it started is still running" in first_look
    assert _until(lambda: process.line_count >= 3)  # the server is still printing
    with pytest.raises(ProcessError, match="still running"):
        manager.clear(process.id)

    stopped = stop_process(process.id, workspace=workspace, processes=manager)

    assert stopped.startswith(f"Stopped process {process.id}")
    assert _until(lambda: not process.alive)
    settled = process.line_count
    time.sleep(0.2)
    assert process.line_count == settled  # nothing in its group prints any more
    manager.clear(process.id)
    assert manager.processes() == ()


@posix_only
def test_closing_reaches_what_an_ended_command_left_running(manager, workspace):
    (workspace.root / "start.sh").write_text(
        f"{LOOP} &\necho started\n", encoding="utf-8"
    )
    process = manager.start("sh start.sh", cwd=workspace.root)
    assert _until(lambda: not process.running and process.line_count >= 3)

    manager.stop_all()

    assert _until(lambda: not process.alive)


@posix_only
def test_the_end_of_what_an_ended_command_left_running_is_told(manager, tmp_path):
    """Without it the dashboard, which fetches the list on process events,
    would count the command as running until something else happened."""
    process = manager.start(f"{LOOP.replace('10000', '12')} & echo started", cwd=tmp_path)
    assert _until(lambda: manager.recorded.of_type("process_exited"))
    assert manager.recorded.of_type("process_exited")[0].data["alive"] is True

    assert _until(lambda: not process.alive, timeout=3.0)
    assert _until(lambda: manager.recorded.of_type("process_ended"))
    time.sleep(0.1)
    types = [event.type for event in manager.recorded.events]
    assert types == ["process_started", "process_exited", "process_ended"]


@posix_only
def test_an_exit_with_nothing_left_running_owes_no_second_event(manager, tmp_path):
    process = manager.start("echo done", cwd=tmp_path)
    assert process.wait(2.0)
    time.sleep(0.1)

    assert manager.recorded.of_type("process_exited")[0].data["alive"] is False
    assert manager.recorded.of_type("process_ended") == []


@posix_only
def test_a_pipe_held_outside_the_group_is_not_offered_a_stop(manager, workspace):
    """A child moved to a session of its own (`setsid`) keeps the output
    open but is out of a stop's reach; saying it was stopped would be false,
    and refusing to clear it would keep it listed for ever."""
    process = manager.start(
        f"{PY} \"import subprocess; "
        f"print(subprocess.Popen(['sleep', '30'], start_new_session=True).pid)\"",
        cwd=workspace.root,
    )
    assert _until(lambda: bool(process.output().lines))
    escaped = int(process.output().lines[0])
    try:
        assert process.wait(2.0)
        assert not process.alive
        said = stop_process(process.id, workspace=workspace, processes=manager)
        assert "already ended" in said
        manager.clear(process.id)
        assert manager.processes() == ()
    finally:
        import os
        import signal

        os.kill(escaped, signal.SIGKILL)


@posix_only
def test_what_an_ended_command_left_running_counts_against_the_limit(workspace):
    capped = ProcessManager(max_running=1, stop_grace=0.5)
    (workspace.root / "start.sh").write_text(f"{LOOP} &\necho started\n", encoding="utf-8")
    process = capped.start("sh start.sh", cwd=workspace.root)
    try:
        assert _until(lambda: not process.running and process.alive)
        with pytest.raises(ProcessError, match="already running"):
            capped.start("sleep 30", cwd=workspace.root)
    finally:
        capped.stop_all()


@posix_only
def test_a_session_cannot_fill_the_machine_with_copies(tmp_path):
    capped = ProcessManager(max_running=1)
    capped.start("sleep 30", cwd=tmp_path)
    try:
        with pytest.raises(ProcessError, match="already running"):
            capped.start("sleep 30", cwd=tmp_path)
    finally:
        capped.stop_all()


@posix_only
def test_a_closed_manager_starts_nothing_more(manager, workspace):
    """A run still going when its session closes must not start a server
    that nothing would stop any more."""
    manager.stop_all()

    with pytest.raises(ProcessError, match="closed"):
        manager.start("sleep 30", cwd=workspace.root)
    result = run_command("sleep 30 &", workspace=workspace, processes=manager)
    assert result.startswith("Error:") and "closed" in result
    assert manager.processes() == ()


@posix_only
def test_stopping_managers_together_closes_them_too(tmp_path):
    first, second = ProcessManager(), ProcessManager()
    stop_managers([first, second])

    for closed in (first, second):
        with pytest.raises(ProcessError, match="closed"):
            closed.start("sleep 30", cwd=tmp_path)


@posix_only
def test_stopping_several_managers_shares_one_grace(tmp_path):
    first, second = ProcessManager(), ProcessManager()
    one = first.start("sleep 30", cwd=tmp_path)
    two = second.start("sleep 30", cwd=tmp_path)

    stop_managers([first, second])

    assert one.status == STOPPED and two.status == STOPPED


# -- run_command -----------------------------------------------------------------


@posix_only
def test_a_backgrounded_command_returns_at_once_instead_of_hanging(manager, workspace):
    """Before DR-062 this waited out the whole timeout: the loop held the pipe."""
    started = time.monotonic()
    result = run_command(f"{LOOP} &", workspace=workspace, timeout=10, processes=manager)

    assert time.monotonic() - started < 2.0
    assert result.startswith("Started in the background as process p")
    assert "Still running" in result and "tick 0" in result
    (process,) = manager.running()
    assert process.command == LOOP
    assert f'process_output(process_id="{process.id}")' in result


@posix_only
def test_background_true_does_the_same_without_an_ampersand(manager, workspace):
    result = run_command("sleep 30", background=True, workspace=workspace, processes=manager)

    assert "Started in the background" in result
    assert [process.command for process in manager.running()] == ["sleep 30"]


@posix_only
def test_a_background_command_that_ends_at_once_says_how(manager, workspace):
    result = run_command(
        "python3 -c \"print('boom'); raise SystemExit(2)\" &",
        workspace=workspace,
        processes=manager,
    )
    assert "It ended within" in result and "exited with code 2" in result
    assert "boom" in result


def test_without_a_manager_a_background_command_is_refused_not_hung(workspace):
    started = time.monotonic()
    result = run_command(f"{LOOP} &", workspace=workspace, timeout=10)

    assert time.monotonic() - started < 1.0
    assert result.startswith("Error:") and "background" in result


def test_a_plain_command_behaves_exactly_as_before(manager, workspace):
    result = run_command("echo hi", workspace=workspace, processes=manager)
    assert result == run_command("echo hi", workspace=workspace) == "hi\n\nExit code: 0"
    assert manager.processes() == ()


def test_the_workspace_checks_still_apply_to_a_background_command(manager, workspace):
    result = run_command("cat /etc/passwd &", workspace=workspace, processes=manager)
    assert "absolute path" in result
    assert manager.processes() == ()


@posix_only
def test_a_model_cannot_supply_its_own_manager(manager, workspace):
    """`processes` is injected, like `workspace`; a model's value is dropped."""
    from functools import partial

    registry = clone_tools(
        catalog_subset("run_command"),
        {"run_command": partial(run_command, processes=manager)},
    )
    executor = ToolExecutor(
        registry, workspace=workspace, approval_gate=AutoApproveGate(authorized_by="test")
    )
    result = executor.execute(tool_call("run_command", command="sleep 30 &", processes="mine"))

    assert "Started in the background" in result.content
    assert len(manager.running()) == 1


# -- reading and stopping, as the model does -----------------------------------------


@posix_only
def test_the_model_can_read_output_later_and_stop_the_process(manager, workspace):
    run_command(f"{LOOP} &", workspace=workspace, processes=manager)
    (process,) = manager.running()
    assert _until(lambda: process.line_count >= 3)

    listing = process_output(workspace=workspace, processes=manager)
    assert process.id in listing and "running" in listing
    shown = process_output(process.id, lines=2, workspace=workspace, processes=manager)
    assert "Status: running" in shown and shown.count("\ntick ") == 2

    stopped = stop_process(process.id, workspace=workspace, processes=manager)
    assert stopped.startswith(f"Stopped process {process.id}")
    assert process.status == STOPPED
    again = stop_process(process.id, workspace=workspace, processes=manager)
    assert "already ended" in again


@posix_only
def test_an_unknown_process_is_an_error_that_lists_the_real_ones(manager, workspace):
    manager.start("sleep 30", cwd=workspace.root)
    result = stop_process("p999999", workspace=workspace, processes=manager)
    assert result.startswith("Error:") and "sleep 30" in result


# -- allowances judge the command without the `&` --------------------------------


def _offer(command: str):
    return offer_for(ApprovalRequest("run_command", {"command": command}))


def test_a_trailing_ampersand_no_longer_makes_a_command_once_only():
    assert _offer("npm run dev &").allowance.pattern == "npm run dev"
    assert _offer("nohup python server.py &").allowance.pattern == "python server.py"


def test_backgrounding_does_not_widen_what_an_allowance_covers():
    allowed = _offer("python server.py").allowance
    assert standing_for(ApprovalRequest("run_command", {"command": "python server.py &"}), [allowed])
    assert not standing_for(
        ApprovalRequest("run_command", {"command": "python other.py &"}), [allowed]
    )
    for command in ("rm -rf build &", "sleep 1 & rm -rf build &", "python a.py > log &"):
        assert _offer(command).allowance is None, command


def test_a_rule_written_by_hand_names_the_command_without_the_ampersand():
    with pytest.raises(ValueError, match="npm run dev"):
        allowance_for_rule("run_command", "npm run dev &")


# -- the session ---------------------------------------------------------------------

_MODEL = ModelInfo(name="m:1b", context_length=32768, supports_tools=True)


@pytest.fixture
def scripted():
    holder: dict = {}

    class _Catalog:
        def __init__(self, settings):
            pass

        def list_models(self):
            return (_MODEL,)

    register_provider(
        Provider(
            name="test-processes",
            chat_model=lambda settings: holder["model"],
            catalog=_Catalog,
            is_local=True,
        ),
        replace=True,
    )
    return holder


@posix_only
def test_a_run_starts_a_server_and_closing_the_session_stops_it(scripted, tmp_path):
    scripted["model"] = ScriptedChatModel(
        [calls(tool_call("run_command", command=f"{LOOP} &")), answer("it is running")]
    )
    events = RecordingEventSink()
    session = Session(
        inference=InferenceSettings(
            base_url="http://test/v1", model=_MODEL.name, provider="test-processes"
        ),
        workspace=Workspace.at(tmp_path),
        model_info=_MODEL,
        events=events,
        approval_gate=AutoApproveGate(authorized_by="test"),
        auto_classify=False,
    )
    turn = session.run("start the dev server")

    offered = {tool["function"]["name"] for tool in scripted["model"].requests[0]["tools"]}
    assert {"process_output", "stop_process"} <= offered
    assert turn.succeeded
    (process,) = session.processes.running()
    assert events.of_type("process_started")[0].data["id"] == process.id

    session.close()

    assert process.status == STOPPED
    assert session.processes.running() == ()


# -- the app going away ------------------------------------------------------------


@posix_only
def test_sigterm_becomes_an_ordinary_exit_so_cleanup_runs():
    """A `kill` would otherwise skip every `finally` that stops processes."""
    import os
    import signal

    from logic.common.processes import exit_cleanly_on_termination

    if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
        pytest.skip("something else already handles SIGTERM here")
    cleaned = []
    with pytest.raises(SystemExit) as stopped:
        try:
            with exit_cleanly_on_termination():
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(1)
        finally:
            cleaned.append(True)

    assert stopped.value.code == 128 + signal.SIGTERM and cleaned
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def test_a_system_without_process_groups_refuses_rather_than_orphans(
    monkeypatch, workspace
):
    """Without a group of its own a command could not be stopped with what it
    started, so it is not started at all; nothing hangs and nothing is left."""
    from logic.common import processes as processes_module

    monkeypatch.setattr(processes_module, "SUPPORTED", False)
    manager = ProcessManager()

    result = run_command("sleep 30 &", workspace=workspace, processes=manager)

    assert result.startswith("Error:") and "trailing '&'" in result
    assert manager.processes() == ()
    manager.stop_all()  # nothing to stop, and no POSIX-only call is reached


# -- DR-064: who started it, and where it can be opened -----------------------


@posix_only
def test_a_command_says_who_started_it(manager, tmp_path):
    manager.started_by({"by": "run", "run": 3, "request": "start it"})
    by_run = manager.start("true", cwd=tmp_path)
    mine = manager.start("true", cwd=tmp_path, origin={"by": "person"})
    manager.started_by(None)
    nobody = manager.start("true", cwd=tmp_path)

    assert by_run.to_dict()["origin"] == {"by": "run", "run": 3, "request": "start it"}
    assert mine.to_dict()["origin"] == {"by": "person"}
    assert nobody.to_dict()["origin"] == {}
    started = manager.recorded.of_type("process_started")[0]
    assert started.data["origin"]["run"] == 3


@posix_only
def test_local_addresses_it_prints_are_kept_to_open(manager, tmp_path):
    printed = (
        "Serving HTTP on 0.0.0.0 port 8000 (http://0.0.0.0:8000/) ...;"
        "Local: http://localhost:5173/, also http://127.0.0.1:5173/app.;"
        "docs at https://example.com/x and http://[::]:9000;"
        "again http://localhost:5173/"
    )
    script = "; ".join(f"print({line!r})" for line in printed.split(";"))
    process = manager.start(f'{PY} "{script}"', cwd=tmp_path)
    assert process.wait(2.0)

    assert process.to_dict()["links"] == [
        "http://localhost:8000/",
        "http://localhost:5173/",
        "http://127.0.0.1:5173/app",
    ]


@posix_only
def test_python_output_arrives_while_it_runs_not_when_it_ends(manager, tmp_path):
    """No -u: the command as a person types it. Through a pipe Python would
    hold this line until its buffer filled."""
    process = manager.start(
        "python3 -c \"import time; print('listening'); time.sleep(30)\"", cwd=tmp_path
    )
    try:
        assert _until(lambda: process.output().lines == ("listening",), timeout=3.0)
    finally:
        manager.stop(process.id)

