"""Schedules and watchers, without a thread or a real clock (DR-047).

Every moment here is built from a naive ``datetime``, which Python reads as
local time — the same time cron expressions are matched against.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path

import pytest

from logic.automation import (
    AUTOMATION_FILE,
    OUTPUT_LIMIT,
    AutomationStore,
    CommandResult,
    CronCadence,
    IntervalCadence,
    InvalidAutomationError,
    RememberedTrust,
    TrustFile,
    UnknownAutomationError,
    parse_cadence,
    render_prompt,
    run_watch_command,
)


def at(*parts: int) -> float:
    return datetime(*parts).timestamp()  # type: ignore[arg-type]


def local(stamp: float) -> datetime:
    return datetime.fromtimestamp(stamp)


# -- the cron parser ---------------------------------------------------------------


def test_a_cron_field_takes_numbers_stars_ranges_steps_and_lists():
    cron = CronCadence.parse("*/15 9-17 1,15 * 1-5")
    assert cron.minutes == {0, 15, 30, 45}
    assert cron.hours == set(range(9, 18))
    assert cron.days == {1, 15}
    assert cron.months == set(range(1, 13))
    assert cron.weekdays == {1, 2, 3, 4, 5}


def test_a_stepped_range_and_a_stepped_start_count_from_their_first_value():
    assert CronCadence.parse("10-30/10 * * * *").minutes == {10, 20, 30}
    assert CronCadence.parse("5/20 * * * *").minutes == {5, 25, 45}


def test_seven_is_sunday_like_zero():
    assert CronCadence.parse("0 0 * * 7").weekdays == {0}


@pytest.mark.parametrize(
    "expression",
    [
        "* * * *",  # four fields
        "* * * * * *",  # six
        "60 * * * *",  # minute out of range
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "*/0 * * * *",  # zero step
        "5-1 * * * *",  # backwards range
        "1,,2 * * * *",  # empty list item
        "a * * * *",  # not a number
        "-1 * * * *",
        "* * 31 2 *",  # well formed, never happens
    ],
)
def test_a_cron_expression_that_cannot_work_is_refused_with_a_reason(expression):
    with pytest.raises(InvalidAutomationError) as raised:
        CronCadence.parse(expression)
    assert str(raised.value)


# -- next run ----------------------------------------------------------------------


def test_a_weekday_morning_schedule_skips_the_weekend():
    cron = CronCadence.parse("0 9 * * 1-5")
    # Friday 2026-10-16 at 10:00 -> Monday 2026-10-19 at 09:00.
    assert local(cron.next_after(at(2026, 10, 16, 10, 0))) == datetime(2026, 10, 19, 9, 0)


def test_the_next_cron_run_is_strictly_after_the_moment_given():
    cron = CronCadence.parse("30 * * * *")
    assert local(cron.next_after(at(2026, 10, 18, 8, 30))) == datetime(2026, 10, 18, 9, 30)
    assert local(cron.next_after(at(2026, 10, 18, 8, 29, 59))) == datetime(
        2026, 10, 18, 8, 30
    )


def test_restricting_both_day_fields_matches_either_as_cron_does():
    cron = CronCadence.parse("0 0 13 * 5")  # the 13th, or any Friday
    # From Sunday 2026-10-18: next Friday is the 23rd, the 13th of November later.
    assert local(cron.next_after(at(2026, 10, 18))) == datetime(2026, 10, 23, 0, 0)


def test_a_leap_day_schedule_finds_the_next_leap_year():
    cron = CronCadence.parse("0 12 29 2 *")
    assert local(cron.next_after(at(2026, 10, 18))) == datetime(2028, 2, 29, 12, 0)


def test_an_interval_counts_from_the_moment_given():
    assert IntervalCadence(15).next_after(at(2026, 10, 18, 8, 0)) == at(2026, 10, 18, 8, 15)


@pytest.mark.parametrize("every", [15, "15", 15.0])
def test_whole_minutes_in_any_spelling_are_an_interval(every):
    assert parse_cadence(every) == IntervalCadence(15)


@pytest.mark.parametrize("every", [0, -5, "0", True, 1.5, "", "soon", None])
def test_an_every_that_is_neither_minutes_nor_cron_is_refused(every):
    with pytest.raises(InvalidAutomationError):
        parse_cadence(every)


# -- the store ---------------------------------------------------------------------


def _schedule(**overrides):
    return {"name": "Nightly", "prompt": "summarise yesterday's commits", "every": 60, **overrides}


def test_a_schedule_survives_a_round_trip_through_the_file(tmp_path):
    store = AutomationStore(tmp_path)
    added = store.add_schedule(_schedule(every="0 9 * * 1-5", tier="craft"), now=at(2026, 10, 16, 10))

    reopened = AutomationStore(tmp_path)
    [loaded] = reopened.schedules()
    assert loaded == added
    assert local(loaded.next_run_at) == datetime(2026, 10, 19, 9, 0)
    assert (tmp_path / AUTOMATION_FILE).is_file()


def test_the_file_is_replaced_whole_never_written_in_place(tmp_path):
    store = AutomationStore(tmp_path)
    store.add_schedule(_schedule(), now=at(2026, 10, 18))
    store.add_schedule(_schedule(name="Second"), now=at(2026, 10, 18))

    directory = tmp_path / ".autose"
    # Only the file itself: no temporary sibling is left behind.
    assert sorted(path.name for path in directory.iterdir()) == ["automation.json"]
    document = json.loads((directory / "automation.json").read_text())
    assert [item["name"] for item in document["schedules"]] == ["Nightly", "Second"]


def test_an_unreadable_file_is_reported_and_kept_rather_than_overwritten(tmp_path):
    path = tmp_path / AUTOMATION_FILE
    path.parent.mkdir()
    path.write_text("{ not json")

    store = AutomationStore(tmp_path)
    assert store.schedules() == []
    assert store.problems and "could not be read" in store.problems[0]

    store.add_schedule(_schedule(), now=at(2026, 10, 18))
    kept = path.with_name("automation.json.unreadable")
    assert kept.read_text() == "{ not json"
    assert len(AutomationStore(tmp_path).schedules()) == 1


def test_a_schedule_that_does_not_validate_names_the_problem(tmp_path):
    store = AutomationStore(tmp_path)
    with pytest.raises(InvalidAutomationError, match="prompt"):
        store.add_schedule({"name": "x", "every": 5}, now=0)
    with pytest.raises(InvalidAutomationError, match="tier"):
        store.add_schedule(_schedule(tier="huge"), now=0)
    with pytest.raises(InvalidAutomationError, match="cron"):
        store.add_schedule(_schedule(every="every tuesday"), now=0)


def test_an_unknown_id_is_its_own_error(tmp_path):
    store = AutomationStore(tmp_path)
    with pytest.raises(UnknownAutomationError):
        store.update_schedule("nope", {"enabled": False}, now=0)
    with pytest.raises(UnknownAutomationError):
        store.delete_watcher("nope")


def test_due_returns_a_schedule_once_its_time_has_come(tmp_path):
    store = AutomationStore(tmp_path)
    schedule = store.add_schedule(_schedule(every=30), now=at(2026, 10, 18, 8, 0))

    assert not store.due(at(2026, 10, 18, 8, 29))
    assert [item.id for item in store.due(at(2026, 10, 18, 8, 30)).schedules] == [schedule.id]


def test_a_run_moves_the_next_one_on_and_missed_runs_collapse_into_one(tmp_path):
    store = AutomationStore(tmp_path)
    schedule = store.add_schedule(_schedule(every=60), now=at(2026, 10, 18, 0, 0))

    # The machine slept all night: eight hourly runs were missed.
    woke = at(2026, 10, 18, 8, 5)
    assert len(store.due(woke).schedules) == 1
    store.record_schedule_run(schedule.id, now=woke, session="abc")

    assert not store.due(woke)
    after = store.schedule(schedule.id)
    assert after.last_session == "abc"
    assert after.next_run_at == at(2026, 10, 18, 9, 5)


def test_a_skipped_run_stays_due(tmp_path):
    store = AutomationStore(tmp_path)
    schedule = store.add_schedule(_schedule(every=1), now=at(2026, 10, 18, 8, 0))
    store.record_schedule_skipped(schedule.id, reason="desk full")

    assert store.due(at(2026, 10, 18, 8, 1)).schedules
    assert store.schedule(schedule.id).last_outcome == "skipped: desk full"


def test_a_disabled_schedule_is_never_due_and_counts_afresh_when_enabled(tmp_path):
    store = AutomationStore(tmp_path)
    schedule = store.add_schedule(_schedule(every=10), now=at(2026, 10, 18, 8, 0))
    store.update_schedule(schedule.id, {"enabled": False}, now=at(2026, 10, 18, 8, 1))
    assert not store.due(at(2026, 10, 19))

    enabled = store.update_schedule(schedule.id, {"enabled": True}, now=at(2026, 10, 19, 12, 0))
    assert enabled.next_run_at == at(2026, 10, 19, 12, 10)


def test_the_summary_names_the_soonest_run(tmp_path):
    store = AutomationStore(tmp_path)
    store.add_schedule(_schedule(every=60), now=at(2026, 10, 18, 8, 0))
    store.add_schedule(_schedule(every=15), now=at(2026, 10, 18, 8, 0))
    summary = store.summary()
    assert summary["schedules"] == 2
    assert summary["watchers"] == 0
    assert summary["next_run_at"] == at(2026, 10, 18, 8, 15)


# -- watchers ----------------------------------------------------------------------


class FakeCommand:
    """A command runner that replays results and records what it ran."""

    def __init__(self, *results: CommandResult) -> None:
        self.results = list(results)
        self.ran: list[tuple[str, Path]] = []

    def __call__(self, command, *, root, timeout):
        self.ran.append((command, root))
        return self.results.pop(0)


def _watcher(**overrides):
    return {
        "name": "CI",
        "command": "gh run list --limit 1 --json status,conclusion",
        "every_minutes": 5,
        "trigger": "change",
        "prompt_template": "CI changed (exit {exit_code}):\n{output}",
        **overrides,
    }


def test_the_first_check_only_records_a_baseline(tmp_path):
    store = AutomationStore(tmp_path)
    watcher = store.add_watcher(_watcher(), now=at(2026, 10, 18, 8, 0))
    runner = FakeCommand(CommandResult(0, "in_progress"))

    outcome = store.check_watcher(watcher.id, now=at(2026, 10, 18, 8, 0), runner=runner)

    assert outcome.baseline is True
    assert outcome.triggered is False
    assert runner.ran == [(watcher.command, tmp_path)]
    assert store.watcher(watcher.id).last_outcome == "baseline recorded"


def test_a_change_watcher_fires_when_the_output_changes(tmp_path):
    store = AutomationStore(tmp_path)
    watcher = store.add_watcher(_watcher(), now=at(2026, 10, 18, 8, 0))
    runner = FakeCommand(
        CommandResult(0, "in_progress"),
        CommandResult(0, "in_progress"),
        CommandResult(0, '{"conclusion": "failure"}'),
    )
    store.check_watcher(watcher.id, now=at(2026, 10, 18, 8, 0), runner=runner)
    same = store.check_watcher(watcher.id, now=at(2026, 10, 18, 8, 5), runner=runner)
    changed = store.check_watcher(watcher.id, now=at(2026, 10, 18, 8, 10), runner=runner)

    assert same.triggered is False
    assert changed.triggered is True
    assert changed.prompt == 'CI changed (exit 0):\n{"conclusion": "failure"}'
    # Held until a session takes it, so a full desk does not lose it.
    assert store.due(at(2026, 10, 18, 8, 11)).pending


def test_a_failure_watcher_fires_on_a_new_failure_not_on_every_check(tmp_path):
    store = AutomationStore(tmp_path)
    watcher = store.add_watcher(
        _watcher(command="make test", trigger="failure"), now=at(2026, 10, 18, 8, 0)
    )
    runner = FakeCommand(
        CommandResult(0, "ok"),
        CommandResult(2, "1 failed"),
        CommandResult(2, "1 failed"),
        CommandResult(0, "ok"),
    )
    results = []
    for minute in (0, 5, 10, 15):
        results.append(
            store.check_watcher(watcher.id, now=at(2026, 10, 18, 8, minute), runner=runner)
        )
        if results[-1].triggered:
            store.record_watcher_started(watcher.id, now=0, session="s")

    assert [outcome.triggered for outcome in results] == [False, True, False, False]


def test_a_watcher_is_due_again_after_its_interval(tmp_path):
    store = AutomationStore(tmp_path)
    watcher = store.add_watcher(_watcher(every_minutes=5), now=at(2026, 10, 18, 8, 0))
    assert store.due(at(2026, 10, 18, 8, 0)).checks  # checked straight away
    store.check_watcher(
        watcher.id, now=at(2026, 10, 18, 8, 0), runner=FakeCommand(CommandResult(0, "x"))
    )
    assert not store.due(at(2026, 10, 18, 8, 4))
    assert store.due(at(2026, 10, 18, 8, 5)).checks


def test_a_watcher_command_that_reaches_outside_the_workspace_is_refused(tmp_path):
    store = AutomationStore(tmp_path)
    with pytest.raises(InvalidAutomationError, match="absolute path"):
        store.add_watcher(_watcher(command="cat /etc/passwd"), now=0)
    with pytest.raises(InvalidAutomationError, match="parent path"):
        store.add_watcher(_watcher(command="ls ../other"), now=0)


def test_a_watcher_command_that_arrived_in_the_file_does_not_run_until_saved_here(tmp_path):
    """The command runs unapproved only because a person on this machine wrote it."""
    trust_path = tmp_path / "trust.json"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    authored = AutomationStore(workspace, trust=RememberedTrust())
    watcher = authored.add_watcher(_watcher(command="make lint"), now=0)

    # Same file, read by a dashboard that never saw the command typed.
    cloned = AutomationStore(workspace, trust=TrustFile(trust_path, workspace))
    runner = FakeCommand(CommandResult(0, "fine"))
    outcome = cloned.check_watcher(watcher.id, now=1, runner=runner)

    assert outcome.refused
    assert runner.ran == []
    assert cloned.payload()["watchers"][0]["trusted"] is False

    # Switching it on or renaming it is not looking at the command.
    cloned.update_watcher(watcher.id, {"enabled": True, "name": "lint"}, now=2)
    assert cloned.check_watcher(watcher.id, now=3, runner=runner).refused
    assert runner.ran == []

    cloned.update_watcher(watcher.id, {"command": "make lint"}, now=4)
    assert cloned.check_watcher(watcher.id, now=5, runner=runner).baseline is True
    assert TrustFile(trust_path, workspace).allows("make lint")


def test_the_prompt_template_gets_the_bounded_output_and_the_exit_code():
    long_output = "x" * (OUTPUT_LIMIT + 500) + "END"
    prompt = render_prompt("exit={exit_code} out={output}", output=long_output, exit_code=3)
    assert prompt.startswith("exit=3 out=[… 503 earlier characters cut]")
    assert prompt.endswith("END")
    # Braces in output or template are plain text, not format fields.
    assert render_prompt("{output} {other}", output="{x}", exit_code=0) == "{x} {other}"


@pytest.mark.skipif(os.name == "nt", reason="the command is written for a POSIX shell")
def test_the_real_runner_runs_in_the_workspace_and_reports_the_exit_code(tmp_path):
    (tmp_path / "marker.txt").write_text("here")
    result = run_watch_command("ls; exit 3", root=tmp_path, timeout=10)
    assert result.exit_code == 3
    assert "marker.txt" in result.output


def test_the_real_runner_applies_the_shell_tool_s_confinement(tmp_path):
    with pytest.raises(InvalidAutomationError):
        run_watch_command("cat ~/.ssh/id_rsa", root=tmp_path, timeout=10)


def test_the_real_runner_turns_a_hung_command_into_a_failure(tmp_path):
    result = run_watch_command("sleep 5", root=tmp_path, timeout=0.2)
    assert result.exit_code == -1
    assert "timed out" in result.output


def test_the_trust_file_is_private(tmp_path):
    path = tmp_path / "config" / "trust.json"
    TrustFile(path, tmp_path).trust("make test")
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_change_to_the_file_made_elsewhere_is_read_not_overwritten(tmp_path):
    """A `git pull` or a hand edit while the dashboard runs is kept."""
    store = AutomationStore(tmp_path, trust=RememberedTrust())
    store.add_schedule(_schedule(name="first"), now=0)

    # Someone else adds a schedule to the same file.
    other = AutomationStore(tmp_path, trust=RememberedTrust())
    other.add_schedule(_schedule(name="from the pull"), now=1)

    assert sorted(item.name for item in store.schedules()) == ["first", "from the pull"]
    store.add_schedule(_schedule(name="third"), now=2)
    reread = AutomationStore(tmp_path, trust=RememberedTrust())
    assert sorted(item.name for item in reread.schedules()) == ["first", "from the pull", "third"]
