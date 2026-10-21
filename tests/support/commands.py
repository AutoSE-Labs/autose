"""Shell commands the background-command tests run, in whichever shell is here.

``run_command`` and the process manager run a command through the system's
shell: ``sh`` on POSIX, ``cmd`` on Windows. These are the few commands the
tests need, written for both (DR-062).
"""

from __future__ import annotations

import os
from pathlib import Path

WINDOWS = os.name == "nt"

#: The interpreter by name, not path: run_command refuses an absolute path,
#: and every machine that runs these tests has one on PATH under this name.
PYTHON = "python" if WINDOWS else "python3"

#: Runs for far longer than any test waits, printing nothing.
SLEEP = f'{PYTHON} -c "import time; time.sleep(30)"'

#: Prints a numbered line every 50 ms for far longer than any test waits.
LOOP = (
    f'{PYTHON} -u -c "import time; '
    "[(print('tick', i), time.sleep(0.05)) for i in range(10000)]\""
)


def left_running(command: str, then: str = "echo started") -> str:
    """A line that starts *command* in the background, runs *then* and exits."""
    if WINDOWS:
        return f'start "" /b {command} & {then}'
    return f"{command} & {then}"


def start_script(root: Path, command: str) -> str:
    """Write a script that leaves *command* running and exits; the line that runs it.

    What ``./start.sh`` that runs ``node server.js &`` looks like here.
    """
    if WINDOWS:
        script = root / "start.bat"
        script.write_text(f"@echo off\r\n{left_running(command)}\r\n", encoding="utf-8")
        # By path: cmd may be told not to look in the current folder.
        return ".\\start.bat"
    (root / "start.sh").write_text(f"{left_running(command)}\n", encoding="utf-8")
    return "sh start.sh"
