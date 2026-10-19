# DR-059: A Window Nobody Chose Matches the Loaded Model, and a Chosen One Can Be Saved for the Workspace

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** `Session.match_loaded_window`, the `configured_window` a session is built with, and `Session.set_context_window` in `src/logic/session.py`; `LoadedWindow`/`LoadedWindowReader` in `src/logic/inference/catalog.py` and `OllamaCatalog.loaded_window`; `save_workspace_context_limit` in `src/logic/config.py`; `update_yaml_scalars` in `src/logic/user_config.py` and the line editor in `src/logic/yaml_edit.py`; `Bridge.save_context_default` and `POST /api/context {action: "save_default"}`; the context window panel in `src/gui/static/`. **Refines DR-035**; DR-021's layering and DR-036's user-file rule are unchanged.

## Context

DR-035 lets AutoSE choose the window Ollama serves, by sending
`options.num_ctx` on every request. It decides that window once, when a
session starts: the configured limit, else the served window when the model is
loaded at 32k or more, else 32k.

Ollama reloads a model whenever a request asks for a window other than the one
it is loaded with, which takes tens of seconds to minutes for a large model.
On a shared server that turned into a tug of war, seen on a cluster node: an
OpenAI-compatible client (which cannot send a window) loads the model at the
server default, 64k there; AutoSE's next request asks for the 32k it decided
earlier and forces a reload; the other client's next request reloads it back.
Nobody had asked for 32k. It was only the default.

Separately, keeping a window for a workspace meant copying a YAML snippet from
the panel into `.autose/config.yaml` by hand. And the one place AutoSE did
write YAML for the person, the default model in their own `config.yaml`
(DR-036), round-tripped the file through `safe_load`/`safe_dump`, which drops
every comment.

## Decision

### Match the loaded window when nobody chose one

As each turn starts, before its first request (classification is one), the
session reads what the runtime has loaded through an optional catalog seam,
`LoadedWindowReader.loaded_window(name)`. Ollama implements it with `/api/ps`
alone: one small request, no model call. If

- nobody chose the window: not in the picker this session, and not as
  `inference.context_limit` in configuration;
- the runtime serves the window a request asks for (DR-035);
- the model is loaded, at a different window that is at least AutoSE's minimum
  usable and at most the model's maximum,

the session adopts that window, rebuilds its chat model, keeps the GPU share
`/api/ps` measured at that window, and emits `context_window_changed` with
`reason: "matched the loaded model"`. Otherwise nothing changes: a model that
is not loaded, a server that cannot be reached, or a window too small to work
in keeps the window decided by DR-035.

An explicit choice always wins. A window configured for the session's provider
is passed to the session (`configured_window`) so it is never matched away, and
it now also survives a model switch inside the session. Choosing the window
already in use in the picker marks it as chosen without a reload or an event.

The change is quiet: the dashboard updates the header ruler without a toast,
the context window panel says the size is in use because the model was already
loaded at it, and the terminal prints one muted line.

### Save a window as the workspace's default

The context window panel has a "Make this the default for this workspace"
button, sent as `POST /api/context {tokens, action: "save_default"}`. The
session switches to the size first, which checks it against the model and the
minimum; then `inference.context_limit` is written to the workspace's own
`.autose/config.yaml`, which DR-021 layers over every other file. A missing
file and directory are created with ordinary permissions, since the file holds
no secret and may be committed. A `.autose` directory or file that is a link is
refused rather than written through: replacing it would copy whatever it points
at, perhaps a private file outside the workspace, into the repository. New sessions on the dashboard, and the next
start of AutoSE in that workspace, begin with it as an explicit choice. The
YAML it writes stays on show beneath the picker as reference. The action is
refused, like any window change, while the session is running a turn and for a
runtime that serves a fixed window; and, since a configured window belongs to
the configured provider, for a session switched to another provider, where the
button is not shown.

### Change settings in a YAML file without losing its comments

`update_yaml_scalars(path, {"inference.context_limit": 65536})` sets dotted
scalar keys in a file a person wrote. A small line editor changes a value on
its own line, keeping indentation, quoting and a trailing comment; adds a
missing key at the end of its parent's block; and adds a missing parent block.
It refuses what it cannot vouch for (flow style, anchors and aliases, merge
keys, tags, block scalars, several documents, tabs, quoted or duplicate keys),
and its result is kept only if parsing it gives exactly what a load, update and
dump would have written. Otherwise the file is rewritten with `safe_dump` as
before: still correct, without the comments. The workspace default and
DR-036's default model both use it. No YAML dependency beyond PyYAML is added.

## Consequences

### Positive

- AutoSE and an OpenAI-compatible client can share one Ollama model without
  reloading it back and forth, as long as nobody pinned AutoSE to another size.
- Keeping a window for a workspace is one click, and the file it lands in keeps
  its comments, as does the user's `config.yaml` when the default model is
  saved.

### Negative

- One extra small request to the model server at the start of every turn.
- The window can change between turns without the person asking, to a size
  they did not pick, possibly one where less of the model fits on the GPU. The
  panel shows the GPU share measured at that size, and picking a size stops it.
- Matching happens per turn, not per request: another client loading the model
  at a new size in the middle of a turn still costs a reload.
- A loaded window between AutoSE's minimum and its comfortable 32k is matched
  too: avoiding the reload is judged worth the tighter window, which DR-034's
  advice then reports.
- `AUTOSE_CONTEXT_LIMIT`, when set, still overrides the saved default for the
  shell it is set in.
- A file the line editor will not touch still loses its comments, as before.
