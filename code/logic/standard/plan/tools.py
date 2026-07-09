from common.tool import Tool
from common.tools import (
    _find_files,
    _list_files,
    _read_file,
    _run_command,
    _search_files,
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            name="read_file",
            description="Read the contents of a file. Optionally restrict to a line range.",
            parameters={
                "path": {"type": "string", "description": "Path to the file."},
                "start_line": {
                    "type": "integer",
                    "description": "First line to include (default 1).",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line to include; -1 means EOF (default -1).",
                },
            },
            required=["path"],
            fn=_read_file,
        ),
        Tool(
            name="list_files",
            description="List files and subdirectories inside a directory.",
            parameters={
                "directory": {
                    "type": "string",
                    "description": "Directory to list (default '.').",
                },
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to filter entries (default '*').",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Recurse into subdirectories (default false).",
                },
            },
            required=[],
            fn=_list_files,
        ),
        Tool(
            name="search_files",
            description="Search for a regex pattern across files. Returns matching lines with file paths and line numbers.",
            parameters={
                "text": {
                    "type": "string",
                    "description": "Regex pattern to search for.",
                },
                "directory": {
                    "type": "string",
                    "description": "Root directory to search (default '.').",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Glob to restrict which files are searched (default '*').",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum matching lines to return (default 50).",
                },
            },
            required=["text"],
            fn=_search_files,
        ),
        Tool(
            name="find_files",
            description="Find files by name pattern anywhere under a directory.",
            parameters={
                "name_pattern": {
                    "type": "string",
                    "description": "Glob pattern matched against filename (e.g. '*.py').",
                },
                "directory": {
                    "type": "string",
                    "description": "Root directory to search (default '.').",
                },
            },
            required=["name_pattern"],
            fn=_find_files,
        ),
        Tool(
            name="run_command",
            description=(
                "Run a shell command to gather context that cannot be obtained via file tools "
                "(e.g. checking installed packages, compiler version, git log, linter output). "
                "Prefer file tools for reading files. Requires user approval before execution."
            ),
            parameters={
                "command": {
                    "type": "string",
                    "description": "Shell command to execute.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory (default '.').",
                },
            },
            required=["command"],
            fn=_run_command,
        ),
    ]
}

TOOLS_SCHEMA: list[dict] = [tool.to_openai_schema() for tool in TOOLS.values()]
