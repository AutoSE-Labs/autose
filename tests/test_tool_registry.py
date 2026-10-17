"""DR-008: per-agent cloned tool registries and least-privilege subsets."""

from __future__ import annotations

import pytest

from logic.common.tool import Tool, clone_tools, tools_schema
from logic.common.tools import (
    BUILTIN_TOOLS,
    MUTATING_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
    catalog_subset,
)


def _probe(name: str = "probe") -> Tool:
    return Tool(
        name=name,
        description="d",
        parameters={"x": {"type": "string", "description": "x"}},
        required=["x"],
        fn=lambda **kwargs: "original",
    )


def test_clone_produces_independent_schema_containers():
    catalog = {"probe": _probe()}
    cloned = clone_tools(catalog)

    cloned["probe"].parameters["x"]["description"] = "mutated"
    cloned["probe"].required.append("y")

    assert catalog["probe"].parameters["x"]["description"] == "x"
    assert catalog["probe"].required == ["x"]


def test_two_clones_do_not_share_state():
    catalog = {"probe": _probe()}
    first = clone_tools(catalog, {"probe": lambda **kwargs: "first"})
    second = clone_tools(catalog)

    assert first["probe"](x="v") == "first"
    assert second["probe"](x="v") == "original"
    assert catalog["probe"](x="v") == "original"


def test_override_for_an_absent_tool_does_not_add_it():
    cloned = clone_tools({"probe": _probe()}, {"edit_file": lambda **kwargs: "nope"})
    assert set(cloned) == {"probe"}


def test_catalog_subset_preserves_order_and_rejects_typos():
    subset = catalog_subset("search_files", "read_file")
    assert list(subset) == ["search_files", "read_file"]
    with pytest.raises(KeyError):
        catalog_subset("read_file", "reed_file")


def test_read_only_subset_contains_no_mutating_tool():
    read_only = catalog_subset(*READ_ONLY_TOOL_NAMES)
    assert not set(read_only) & set(MUTATING_TOOL_NAMES)


def test_schema_matches_openai_function_calling_shape():
    schema = tools_schema(catalog_subset("read_file"))
    assert schema[0]["type"] == "function"
    function = schema[0]["function"]
    assert function["name"] == "read_file"
    assert function["parameters"]["type"] == "object"
    assert function["parameters"]["required"] == ["path"]


def test_schema_is_a_copy_not_a_view_onto_the_catalog():
    schema = tools_schema(BUILTIN_TOOLS)
    schema[0]["function"]["parameters"]["properties"].clear()
    assert BUILTIN_TOOLS["read_file"].parameters
