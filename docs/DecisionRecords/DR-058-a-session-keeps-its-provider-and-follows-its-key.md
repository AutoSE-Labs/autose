# DR-058: A Reopened Session Goes Back to Its Provider, and Open Sessions Follow a Rotated Key

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** `Session.return_to_recorded_model`, `Session.refresh_api_key`, the key check at the start of `Session.stream` and the optional `listed` argument of `Session.use_provider` in `src/logic/session.py`; `CredentialStore.stamp` and `ProviderKey` in `src/logic/credentials.py`; `SessionBridge._return_to_record` and `SessionBridge._pick_up_keys` and their callers in `src/gui/bridge.py`; the session's `notice` in the dashboard state and the toast `src/gui/static/app.js` shows for it. **Refines DR-026 and DR-036.** Supersedes none.

## Context

DR-036 made the provider a per-session choice and put keys in their own file.
Two gaps were left, both listed among its negatives or visible in use:

- **A reopened session forgot what it ran on.** A stored session
  (`SessionRecord`, DR-026) already records `provider`, `base_url` and
  `model`, but reopening it from the rail built it from the dashboard's
  default. A conversation held with a hosted 120B model carried on with an 8B
  local one, and nothing said so.
- **A rotated key did not reach open sessions.** A key saved or removed in
  Settings was used only by sessions created afterwards; an open one kept the
  old key until it switched provider or was reopened, and an edit made to
  `credentials.yaml` by hand was never seen at all. Rotating a leaked key meant
  closing every session that used it.

## Decision

### A reopened session goes back to the provider and model it used

When the dashboard opens a stored session (from the rail, or `--resume` at
start), it builds it on the default as before and then calls
`Session.return_to_recorded_model(record)`:

- **Nothing to do** when the record names no provider or model (older
  records), or names what the session is already on.
- **Same provider as the default:** the default's address is kept, not the
  record's. A session saved while the model ran on another port (an SSH tunnel
  on :11435, say) must not be sent back to an address that may be dead; this
  is the rule `select_model` already follows when switching back to the
  default provider.
- **Another provider:** the record's address, else the provider's
  conventional one. Session files live in the workspace, so a repository can
  plant one: the record's address is followed only when it is http(s), and
  for a provider that needs a key only when it is that provider's usual
  address. Otherwise the usual address is used, so a stored key never goes
  to an address that only a session file named.
- The switch happens only when the provider is **usable now**: it is
  registered, it has a key if it needs one, and one listing of its models —
  bounded by the discovery timeout, the same listing a switch from the picker
  makes — answers and includes the recorded model. A provider that needs a
  key and has none is never contacted.
- Otherwise the session stays on the default and emits
  `provider_not_restored`, once, with the sentence the dashboard shows:
  "This session used *provider · model*; it is on *default provider · model*
  now because *reason*." The reasons are plain: the provider is not available
  here; no API key is saved for it; nothing answered at its address; it does
  not offer that model now. The dashboard keeps the sentence as the open
  session's `notice` in its state until the session's next turn, and the page
  shows it once for each opening: a session reopened by `--resume` at start is
  reopened before any page is connected to hear the event.

Going back changes only that session. The default for new sessions is not
touched: reopening an old session is not a choice of what the next one starts
on. On another provider, the listing made for the check is reused for the
switch and for the model picker, so the reopen asks that provider once.

The terminal's `--resume` is unchanged: there the model comes from the command
line and configuration the user just gave (DR-015), and that explicit choice
wins.

### Open sessions follow a rotated key

`ProviderKey` holds the key one holder uses for one provider and keeps it in
step with the credentials file. Every `Session` has one for its current
provider (replaced when it switches), and the dashboard has one for its
default.

- `CredentialStore.stamp()` is the file's inode, size and modification time:
  a `stat`, not a read. Every save replaces the file with a new one, so even
  two saves within one timestamp tick differ; an edit by hand changes the size
  or the time.
- `ProviderKey.check()` does nothing more than that `stat` while the stamp is
  unchanged. When it changes, the key resolved for this provider (stored, then
  the provider's variable) is compared with the one seen last. Only if that
  changed does the holder move: to the new key, or, when the key was removed,
  to a key that came from elsewhere (`AUTOSE_API_KEY`, a legacy
  `inference.api_key`), else to none. A change to another provider's key
  never displaces a key given on the command line.
- `Session.refresh_api_key()` applies a change: the settings get the new key
  and the chat model and classifier are rebuilt over it. It emits
  `api_key_changed` with the provider and the masked tail only.
- **When:** at the start of every turn (`Session.stream`, before
  classification), so an outside edit is noticed at the next turn in the
  dashboard and the terminal alike; and, in the dashboard, at once for every
  idle session after a key is saved or removed in Settings, and whenever a
  turn starts, a session opens or one is reopened. A running session keeps its
  key until the turn ends: no request is sent with a key that changed half
  way through a run.
- A session on a provider that needs a key, whose key was removed, refuses
  its next turn with the same "needs an API key" message a switch gives,
  rather than sending a request that fails with a 401.
- The dashboard's default follows the same way, so a new session never starts
  on a key that was replaced since the dashboard started.

No key value appears in any event, payload or log line; only `mask_key`'s
tail, as DR-036 requires.

## Consequences

### Positive

- A conversation carries on with the model it was held with, on the same
  address the dashboard uses today, or says plainly why it cannot.
- Rotating or revoking a key takes effect across the dashboard from the next
  request, without closing anything, whether it was changed in Settings, in
  another AutoSE process, or in a text editor.
- The check is cheap enough to run before every turn: one `stat` per open
  session, and no read of the file unless it changed.

### Negative

- Reopening a session on another provider makes one listing of that
  provider's models while the dashboard waits, bounded by the discovery
  timeout (2 s by default). An address that drops packets rather than
  refusing costs that much once.
- A session that cannot go back says so as a passing notice, again on each
  page load until its next turn. A person who missed it sees the model in the
  header and the session's history, but not the reason.
- A key set only in a provider's environment variable is not followed: a
  change there is not a file change, and is seen when a session next switches
  provider.
- The terminal's `--resume` still starts on the configured model, so the two
  interfaces differ on purpose here.
