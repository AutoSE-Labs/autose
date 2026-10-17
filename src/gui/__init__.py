"""The web-dashboard Adapter (DR-002, DR-029).

A browser interface over the same ``logic.session.Session`` the terminal drives.
Everything it knows how to *do* — find a runtime, classify a prompt, gate a
tool call, journal a file change — it does by calling ``logic``; nothing here
decides behaviour, and no other package imports this one. Deleting
``src/gui/`` therefore leaves a working terminal build with nothing else to change:
that separability is asserted by ``tests/test_architecture.py``, and it is why
the dashboard brings its own entry point (``python -m gui``) instead of adding
a flag to the terminal CLI.

Importing this package stays cheap on purpose — the server, the session
bridge, and the payload serialisers are pulled in by :mod:`gui.cli` when a
dashboard is actually started.
"""

from __future__ import annotations

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT"]

#: Loopback by default: the dashboard drives an agent that can write files and
#: run approved shell commands in the workspace, so it is a local tool unless
#: its operator says otherwise in as many words (``--host``).
DEFAULT_HOST = "127.0.0.1"

#: An arbitrary high port, chosen to collide with nothing common. ``--port 0``
#: asks the OS for a free one.
DEFAULT_PORT = 8765
