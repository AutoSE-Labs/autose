"""Storing a session and picking it up again (DR-026)."""

from __future__ import annotations

import json
import time

import pytest

from logic.common.changes import FileChange
from logic.common.events import RecordingEventSink
from logic.common.workspace import Workspace
from logic.inference.catalog import ModelInfo
from logic.inference.registry import Provider, register_provider
from logic.inference.settings import InferenceSettings
from logic.persistence import (
    SESSION_FORMAT_VERSION,
    FileSessionStore,
    NullSessionStore,
    SessionRecord,
    SessionStoreError,
    TurnRecord,
    new_session_id,
)
from logic.session import Session
from logic.tiers import CRAFT, SPARK
from tests.support.fakes import ScriptedChatModel, answer, calls, tool_call

_MODEL = ModelInfo(name="gemma4:12b", context_length=262144, supports_tools=True)


@pytest.fixture
def store(tmp_path) -> FileSessionStore:
    return FileSessionStore(tmp_path / ".autose" / "sessions")


def _record(identifier: str = "s1", **kwargs) -> SessionRecord:
    defaults = {
        "workspace": "/work",
        "model": "gemma4:12b",
        "tier": SPARK,
        "turns": (TurnRecord(prompt="do a thing", answer="did it", tier=SPARK),),
    }
    defaults.update(kwargs)
    return SessionRecord(id=identifier, **defaults)


# -- identifiers --------------------------------------------------------------


def test_ids_sort_chronologically():
    first = new_session_id()
    time.sleep(0.01)
    assert first < new_session_id() or first[:15] == new_session_id()[:15]


def test_two_ids_in_the_same_second_do_not_collide():
    assert len({new_session_id() for _ in range(50)}) == 50


# -- the round trip -----------------------------------------------------------


def test_a_session_survives_a_round_trip(store):
    store.save(_record("s1"))

    loaded = store.load("s1")

    assert loaded is not None
    assert loaded.id == "s1"
    assert loaded.turns[0].prompt == "do a thing"
    assert loaded.tier == SPARK


def test_change_content_survives_including_bytes_that_are_not_text(store):
    change = FileChange(path="blob.bin", before=b"\xff\xfe\x00", after=b"\x00\x01")
    store.save(
        _record("s1", turns=(TurnRecord(prompt="p", answer="a", tier=SPARK, changes=(change,)),))
    )

    restored = store.load("s1").turns[0].changes[0]

    assert restored.before == b"\xff\xfe\x00"
    assert restored.after == b"\x00\x01"


def test_saving_twice_replaces_rather_than_appends(store):
    store.save(_record("s1"))
    store.save(_record("s1", turns=(TurnRecord(prompt="second", answer="b", tier=SPARK),)))

    assert store.load("s1").turns[0].prompt == "second"
    assert len(store.list()) == 1


def test_a_missing_session_is_none_not_an_error(store):
    assert store.load("never-existed") is None


def test_stored_sessions_are_kept_out_of_git(store):
    store.save(_record("s1"))
    assert (store.root / ".gitignore").read_text(encoding="utf-8").strip() == "*"


# -- listing ------------------------------------------------------------------


def test_sessions_are_listed_newest_first(store):
    store.save(_record("old", updated_at=1000.0))
    store.save(_record("new", updated_at=2000.0))

    assert [record.id for record in store.list()] == ["new", "old"]


def test_latest_is_the_most_recently_updated(store):
    store.save(_record("old", updated_at=1000.0))
    store.save(_record("new", updated_at=2000.0))

    assert store.latest().id == "new"


def test_latest_with_nothing_stored_is_none(store):
    assert store.latest() is None


def test_a_session_resolves_by_unambiguous_prefix(store):
    store.save(_record("20260830-120000-abcdef"))

    assert store.resolve("20260830-12").id == "20260830-120000-abcdef"


def test_an_ambiguous_prefix_resolves_to_nothing(store):
    store.save(_record("aaa-1"))
    store.save(_record("aaa-2"))

    assert store.resolve("aaa") is None


def test_the_title_is_the_first_request(store):
    record = _record("s1", turns=(TurnRecord(prompt="  fix   the parser ", answer="", tier=SPARK),))
    assert record.title == "fix the parser"


def test_a_session_with_no_turns_still_has_a_title():
    assert "no requests" in SessionRecord(id="s1").title


# -- robustness ---------------------------------------------------------------


def test_a_corrupt_file_is_skipped_rather_than_crashing_the_listing(store):
    store.save(_record("good"))
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "broken.json").write_text("{not json", encoding="utf-8")

    listing = store.list()

    assert [record.id for record in listing] == ["good"]
    assert any("could not be read" in problem for problem in store.problems)


def test_a_session_from_a_newer_autose_is_refused_with_an_explanation(store):
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "future.json").write_text(
        json.dumps({"version": SESSION_FORMAT_VERSION + 5, "id": "future"}),
        encoding="utf-8",
    )

    assert store.list() == ()
    assert any("newer AutoSE" in problem for problem in store.problems)


def test_a_record_without_an_id_is_refused():
    with pytest.raises(SessionStoreError):
        SessionRecord.from_dict({"version": 1})


def test_an_unwritable_store_records_the_problem_instead_of_raising(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    store = FileSessionStore(blocker / "sessions")

    store.save(_record("s1"))

    assert store.problems and "could not save" in store.problems[0]


def test_an_id_cannot_escape_the_sessions_directory(store):
    store.save(_record("../../escaped"))

    assert not (store.root.parent.parent / "escaped.json").exists()
    assert list(store.root.glob("*.json"))


def test_old_sessions_are_pruned_past_the_cap(tmp_path):
    store = FileSessionStore(tmp_path / "sessions", max_sessions=3)
    for index in range(6):
        store.save(_record(f"s{index}"))
        time.sleep(0.01)

    assert len(store.list()) == 3


# -- bounds -------------------------------------------------------------------


def test_an_oversized_change_is_stored_as_unrestorable_rather_than_dropped(store):
    big = FileChange(path="big.py", before=b"x" * 5000, after=b"y" * 5000)
    record = _record(
        "s1", turns=(TurnRecord(prompt="p", answer="a", tier=SPARK, changes=(big,)),)
    ).bounded(max_change_bytes=100)

    (change,) = record.turns[0].changes
    assert change.path == "big.py"
    assert change.restorable is False
    assert change.before is None
    assert "too large" in change.reason


def test_the_session_budget_keeps_the_most_recent_turns_intact(store):
    def turn(name: str) -> TurnRecord:
        return TurnRecord(
            prompt=name,
            answer="",
            tier=SPARK,
            changes=(FileChange(path=f"{name}.py", before=b"a" * 400, after=b"b" * 400),),
        )

    record = _record("s1", turns=(turn("first"), turn("second"))).bounded(
        max_change_bytes=10_000, max_session_bytes=900
    )

    assert record.turns[1].changes[0].restorable is True, "newest turn lost its content"
    assert record.turns[0].changes[0].restorable is False


# -- session integration --------------------------------------------------------


@pytest.fixture
def make_session(tmp_path):
    holder: dict = {}

    class _Catalog:
        def __init__(self, settings):
            pass

        def list_models(self):
            return (_MODEL,)

    register_provider(
        Provider(
            name="test-persist",
            chat_model=lambda settings: holder["model"],
            catalog=_Catalog,
            is_local=True,
        ),
        replace=True,
    )

    def build(script, **kwargs) -> Session:
        holder["model"] = ScriptedChatModel(script)
        kwargs.setdefault("auto_classify", False)
        return Session(
            inference=InferenceSettings(
                base_url="http://test/v1", model=_MODEL.name, provider="test-persist"
            ),
            workspace=Workspace.at(tmp_path),
            model_info=_MODEL,
            **kwargs,
        )

    return build


def test_a_session_saves_itself_after_every_turn(make_session, store):
    session = make_session([answer("first"), answer("second")], store=store)

    session.run("one")
    assert len(store.load(session.session_id).turns) == 1

    session.run("two")
    assert len(store.load(session.session_id).turns) == 2


def test_with_no_store_nothing_is_written(make_session, tmp_path):
    session = make_session([answer("hi")])
    session.run("one")

    assert isinstance(session.store, NullSessionStore)
    assert not (tmp_path / ".autose" / "sessions").exists()


def test_a_resumed_session_carries_its_history_and_tally(make_session, store):
    first = make_session([answer("the answer", prompt_tokens=10, completion_tokens=5)], store=store)
    first.run("a question")
    identifier = first.session_id

    second = make_session([answer("later")], store=store)
    second.restore(store.load(identifier))

    assert second.session_id == identifier
    assert [turn.prompt for turn in second.history] == ["a question"]
    assert second.usage.total_tokens == 15


def test_a_resumed_session_replays_history_into_the_next_prompt(make_session, store):
    first = make_session([answer("it is at line 4")], store=store)
    first.run("where is the value set?")

    second = make_session([answer("still line 4")], store=store)
    second.restore(store.load(first.session_id))
    second.run("are you sure?")

    sent = second._model.requests[-1]["messages"]
    assert any("where is the value set?" in str(message) for message in sent)


def test_the_tier_ratchets_on_resume_rather_than_dropping(make_session, store):
    """DR-003: resuming a craft session must not silently fall back to spark."""
    session = make_session([answer("hi")], store=store, default_tier=SPARK)
    session.restore(_record("s1", tier=CRAFT))

    assert session.tier == CRAFT


def test_resuming_is_announced_as_an_event(make_session, store):
    events = RecordingEventSink()
    session = make_session([answer("hi")], store=store, events=events)
    session.restore(_record("s1"))

    (event,) = events.of_type("session_resumed")
    assert event.data["session_id"] == "s1"


def test_undo_still_works_on_a_resumed_session(make_session, store, tmp_path):
    """The point of storing both halves of a change (DR-024)."""
    target = tmp_path / "m.py"
    target.write_text("value = 1\n", encoding="utf-8")
    first = make_session(
        [
            calls(
                tool_call(
                    "edit_file",
                    path="m.py",
                    edits=[{"old_str": "value = 1", "new_str": "value = 2"}],
                )
            ),
            answer("Done."),
        ],
        store=store,
    )
    first.run("bump it")
    assert target.read_text(encoding="utf-8") == "value = 2\n"

    second = make_session([answer("hi")], store=store)
    second.restore(store.load(first.session_id))
    report = second.undo_last_change()

    assert report is not None and report.restored == ("m.py",)
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_a_failing_store_never_breaks_a_turn(make_session):
    class _Broken:
        def save(self, record):
            raise RuntimeError("disk on fire")

        def load(self, session_id):
            return None

        def list(self, limit=0):
            return ()

        def latest(self):
            return None

        def delete(self, session_id):
            return False

    events = RecordingEventSink()
    session = make_session([answer("fine")], store=_Broken(), events=events)

    turn = session.run("do it")

    assert turn.answer == "fine"
    assert events.of_type("session_save_failed")


def test_the_snapshot_holds_only_what_the_session_promoted(make_session, store):
    """DR-004's boundary applies to the durable copy too."""
    session = make_session([answer("the answer")], store=store)
    session.run("the question")

    document = json.loads((store.root / f"{session.session_id}.json").read_text())

    assert set(document["turns"][0]) == {
        "prompt",
        "answer",
        "tier",
        "stop_reason",
        "prompt_tokens",
        "completion_tokens",
        "files_changed",
        "changes",
        # DR-038: the final ledger is a canonical outcome, like files_changed —
        # not the intermediate loop state DR-004 keeps out of the durable copy.
        "requirements",
        # The run's shape: stages and which requirements sent it back.
        "route",
        # Changes an undo reverted: a canonical outcome, kept for the record.
        "undone",
    }


def test_restoring_re_bounds_a_history_longer_than_this_session_allows(make_session, store):
    """A record written under a larger cap cannot smuggle a longer history in."""
    session = make_session([answer("hi")], store=store)
    session.restore(
        _record("s1", verbatim=tuple((f"p{i}", f"a{i}") for i in range(20)))
    )

    assert len(session.memory.exchanges) <= 4


# -- DR-038: the final requirements ledger survives a restart ----------------


def test_a_turn_s_requirements_ledger_round_trips():
    from logic.persistence import TurnRecord

    record = TurnRecord(
        prompt="fix it",
        answer="done",
        tier="craft",
        requirements=(
            {"id": "R1", "status": "Verified", "description": "mul multiplies", "evidence": "test_ops.py"},
        ),
    )
    assert TurnRecord.from_dict(record.to_dict()).requirements == record.requirements


def test_a_turn_stored_before_ledgers_were_kept_still_loads():
    from logic.persistence import TurnRecord

    old = {"prompt": "p", "answer": "a", "tier": "craft"}
    assert TurnRecord.from_dict(old).requirements == ()



def test_a_turn_s_route_round_trips_and_is_optional_on_read():
    from logic.persistence import TurnRecord

    route = (
        {"kind": "stage", "stage": "plan", "title": "Plan"},
        {"kind": "loopback", "stage": "plan", "detail": "wrong approach", "unmet": "R2"},
    )
    record = TurnRecord(prompt="p", answer="a", tier="craft", route=route)
    stored = TurnRecord.from_dict(record.to_dict()).route
    assert stored[0] == route[0]
    # The validator's words stay off disk (DR-004); the shape and the IDs stay.
    assert stored[1] == {"kind": "loopback", "stage": "plan", "unmet": "R2"}
    older = record.to_dict()
    del older["route"]
    assert TurnRecord.from_dict(older).route == ()


# -- DR-056: requests waiting in a session's queue are kept with it ----------


def test_a_queue_round_trips_with_the_session(store):
    from logic.persistence import QueuedRequest

    queue = (
        QueuedRequest(id="q1", prompt="add tests", tier="craft", queued_at=10.0),
        QueuedRequest(id="q2", prompt="then tidy up"),
    )
    store.save(_record("s1", queue=queue))

    assert store.load("s1").queue == queue


def test_a_session_stored_before_queues_were_kept_still_loads(store):
    older = _record("s1").to_dict()
    del older["queue"]
    (store.root).mkdir(parents=True, exist_ok=True)
    (store.root / "s1.json").write_text(json.dumps(older), encoding="utf-8")

    loaded = store.load("s1")

    assert loaded is not None
    assert loaded.queue == ()
    assert loaded.turns[0].prompt == "do a thing"


def test_a_queued_item_with_no_request_is_dropped_on_read():
    document = _record("s1").to_dict()
    document["queue"] = [{"id": "q1", "prompt": "  "}, "junk", {"id": "q2", "prompt": "real"}]

    assert [item.prompt for item in SessionRecord.from_dict(document).queue] == ["real"]


def test_a_kept_queue_is_saved_at_once_and_survives_the_next_turn(make_session, store):
    from logic.persistence import QueuedRequest

    session = make_session([answer("done")], store=store)
    session.keep_queue([QueuedRequest(id="q1", prompt="next thing")])

    assert [item.prompt for item in store.load(session.session_id).queue] == ["next thing"]

    session.run("this thing")
    stored = store.load(session.session_id)
    assert [turn.prompt for turn in stored.turns] == ["this thing"]
    assert [item.prompt for item in stored.queue] == ["next thing"]


def test_a_resumed_session_carries_its_queue(make_session, store):
    from logic.persistence import QueuedRequest

    first = make_session([answer("hi")], store=store)
    first.keep_queue([QueuedRequest(id="q1", prompt="one"), QueuedRequest(id="q2", prompt="two")])

    second = make_session([answer("hi")], store=store)
    second.restore(store.load(first.session_id))

    assert [item.prompt for item in second.queued] == ["one", "two"]


def test_a_queue_saved_beside_a_turn_never_overlaps_its_save(make_session):
    """Two writes of one session file at once can leave neither on disk."""
    import threading

    from logic.persistence import QueuedRequest

    class _Slow(NullSessionStore):
        def __init__(self) -> None:
            self.active = 0
            self.most = 0
            self.guard = threading.Lock()

        def save(self, record):
            with self.guard:
                self.active += 1
                self.most = max(self.most, self.active)
            time.sleep(0.02)
            with self.guard:
                self.active -= 1

    slow = _Slow()
    session = make_session([answer("hi")], store=slow)

    def turn_saves():
        for _ in range(5):
            session.persist()

    worker = threading.Thread(target=turn_saves)
    worker.start()
    for index in range(5):
        session.keep_queue([QueuedRequest(id=f"q{index}", prompt="later")])
    worker.join(5.0)

    assert slow.most == 1


def test_an_unchanged_queue_is_not_written_again(make_session):
    from logic.persistence import QueuedRequest

    class _Counting(NullSessionStore):
        def __init__(self) -> None:
            self.saves = 0

        def save(self, record):
            self.saves += 1

    counting = _Counting()
    session = make_session([answer("hi")], store=counting)
    session.keep_queue([])
    assert counting.saves == 0
    session.keep_queue([QueuedRequest(id="q1", prompt="one")])
    session.keep_queue([QueuedRequest(id="q1", prompt="one")])
    assert counting.saves == 1
