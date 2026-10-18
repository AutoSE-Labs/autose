"""DR-008: each stage may do exactly what its responsibility requires."""

from __future__ import annotations

import pytest

from logic.common.tool import clone_tools
from logic.common.tools import catalog_subset
from logic.workflows.stages import (
    EXECUTE,
    PLAN,
    STAGES,
    VALIDATE,
)


@pytest.mark.parametrize("stage", [PLAN])
def test_read_only_stages_have_no_write_or_edit_tool(stage):
    """Not discouraged from writing — structurally unable to."""
    names = set(STAGES[stage].tool_names)
    assert "write_file" not in names
    assert "edit_file" not in names
    assert not STAGES[stage].can_write


def test_the_execute_stage_has_the_tools_it_needs():
    assert STAGES[EXECUTE].can_write


def test_the_free_edition_has_no_design_stage():
    """The decision-record Design stage is Pro-only (DR-032)."""
    assert set(STAGES) == {PLAN, EXECUTE, VALIDATE}
    for spec in STAGES.values():
        assert "remember_fact" not in spec.tool_names


def test_the_validation_stage_can_still_run_a_command_to_check_things():
    assert "run_command" in STAGES[VALIDATE].tool_names


def test_every_stage_can_read():
    for spec in STAGES.values():
        assert "read_file" in spec.tool_names


def test_every_stage_owns_the_ledger():
    for stage in (PLAN, EXECUTE, VALIDATE):
        assert STAGES[stage].owns_ledger


def test_stage_status_vocabularies_do_not_overlap():
    """'Implemented' must never be readable as 'Verified'."""
    plan = STAGES[PLAN].allowed_statuses
    execute = STAGES[EXECUTE].allowed_statuses
    validate = STAGES[VALIDATE].allowed_statuses
    assert not (plan & execute) and not (execute & validate) and not (plan & validate)


# -- per-instance registries ------------------------------------------------------


def test_each_stage_clones_its_own_registry():
    catalog = catalog_subset("read_file")
    first = clone_tools(catalog, {"read_file": lambda **kw: "patched"})
    second = clone_tools(catalog)
    assert first["read_file"](path="x") == "patched"
    assert second["read_file"] is not first["read_file"]


def test_rewriting_a_check_script_to_grow_it_is_pointed_out(tmp_path):
    from logic.common.workspace import Workspace
    from logic.workflows.stages import (
        VALIDATE_SCRATCH_DIRECTORY,
        validate_scratch_guard,
    )

    workspace = Workspace.at(tmp_path)
    write = validate_scratch_guard(workspace)["write_file"]
    path = f"{VALIDATE_SCRATCH_DIRECTORY}/check.py"
    script = "".join(f"assert check_{index}()\n" for index in range(200))

    assert "append=true" not in write(path, script, workspace=workspace)
    grown = write(path, script + "assert check_new()\n", workspace=workspace)
    assert "append=true" in grown
    other = write(path, "print('different')\n" + script, workspace=workspace)
    assert "append=true" not in other


def test_the_validation_stage_writes_only_scratch_scripts(tmp_path):
    """DR-008 holds: validate can write a check script, never the work itself."""
    from logic.common.workspace import Workspace
    from logic.workflows.stages import (
        VALIDATE_SCRATCH_DIRECTORY,
        validate_scratch_guard,
    )

    assert "edit_file" not in STAGES[VALIDATE].tool_names
    workspace = Workspace.at(tmp_path)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    write = validate_scratch_guard(workspace)["write_file"]

    for path in ("app.py", "new.py", f"{VALIDATE_SCRATCH_DIRECTORY}/../app.py"):
        assert write(path, "x = 2\n", workspace=workspace).startswith("Error")
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (tmp_path / "new.py").exists()

    result = write(f"{VALIDATE_SCRATCH_DIRECTORY}/check.py", "print(1)\n", workspace=workspace)
    assert not result.startswith("Error")
    assert (tmp_path / VALIDATE_SCRATCH_DIRECTORY / "check.py").exists()
