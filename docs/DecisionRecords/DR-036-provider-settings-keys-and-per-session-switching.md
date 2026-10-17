# DR-036: Providers Are Chosen per Session, Keys Live in an Owner-Only Credentials File, and Ollama Cloud Is a Provider

- **Status:** Accepted
- **Date:** 2026-10-17
- **Decision owners:** AutoSE maintainers
- **Scope:** new `src/logic/credentials.py`, `src/logic/user_config.py`, `src/logic/connections.py` and `src/logic/inference/ollama_cloud.py`; `Provider.requires_api_key` and `Provider.api_key_env` in `src/logic/inference/registry.py`; key and base-URL resolution in `src/logic/config.py`; `Session.select_model(…, provider=, base_url=)`, `Session.use_provider` and `setup_for_provider` in `src/logic/session.py`; the dashboard's `GET/POST /api/providers` and the extended `POST /api/model`. **Refines DR-006, DR-015 and DR-035; inherits DR-029's local-trust rules.** Supersedes none.

## Context

DR-006 makes AutoSE work with any endpoint and DR-015 makes the local one the
default. Between them sat a gap the dashboard made obvious:

- **One provider per process.** The provider came from configuration or
  discovery at start-up. The model picker could change the model, never the
  runtime, so trying a hosted model next to a local one meant editing a file
  and restarting, and every open session moved together.
- **Nowhere good for a key.** The only places a key could go were
  `inference.api_key` in `config.yaml` and `AUTOSE_API_KEY`. `config.yaml` is
  the file people paste into issues, and its repository twin,
  `.autose/config.yaml`, is meant to be committed (DR-021). A secret there is
  one `git add` from being published.
- **No hosted Ollama.** Ollama Cloud serves large models through the same
  native API as a local server (`/api/tags`, `/api/chat`) at
  `https://ollama.com`, authenticated with `Authorization: Bearer <key>`
  (keys from ollama.com/settings/keys). It is the shortest path from a laptop
  that cannot run a 120B model to one that can, and it needs no local Ollama.

## Decision

### Keys live in their own file, owner-only, and are never sent back

`logic.credentials.CredentialStore` keeps one key per provider name in
`credentials.yaml` in the user config directory: `$XDG_CONFIG_HOME/autose`
(default `~/.config/autose`), or `%APPDATA%/AutoSE` on Windows, located the
same way as `config.yaml` (`logic.user_config.user_config_dir`).

- The file is written atomically: `tempfile.mkstemp` in the same directory
  (which opens it 0600 from the first byte), `fsync`, `chmod 0600`,
  `os.replace`. A crash leaves the previous file; a rewrite re-tightens a file
  someone loosened. A directory AutoSE creates is 0700; an existing one is not
  touched.
- A key must be non-empty, at most 4,096 characters, and contain no
  whitespace or control character: anything else is a paste error or an
  attempt to inject a header.
- What leaves the store for display is a `KeyView`: `has_key`, the last four
  characters behind `••••` (nothing at all for a key shorter than 12), and the
  source (`stored`, `environment`, `config`). The dashboard's payloads are
  built only from that, so no response, state snapshot or event carries a key.
  Keys travel browser to server only, in the body of a token- and
  origin-checked POST (DR-029).

Where a provider's key comes from, in order: `AUTOSE_API_KEY` (for the
configured provider, at start-up, as before); the credentials file; the
provider's own conventional variable, declared as `Provider.api_key_env`
(`OLLAMA_API_KEY` for Ollama Cloud); a legacy `inference.api_key`. No generic
variable such as `OPENAI_API_KEY` is assumed for an arbitrary endpoint, since
that would send one service's key to another.

### Ollama Cloud is a registered provider

`ollama-cloud` reuses `OllamaChatModel` (DR-035) unchanged: its base URL is
`https://ollama.com`, which `native_root` leaves alone (`…/v1` works too), and
it already sends the bearer header. `OllamaCloudCatalog` lists `/api/tags` with
the key, and asks `/api/show` once per model, in parallel and cached for the
process, for the published maximum window (`<arch>.context_length`) and tool
support.

There is no `/api/ps` in the cloud, so the served window cannot be observed and
GPU residency does not apply. The budget is the published maximum, marked as
known; with none published, 32,768 (`COMFORTABLE_CONTEXT_TOKENS`), marked as an
assumption so the session never refuses work over it (DR-022).
`serves_requested_window` is **False**: `num_ctx` is still sent with every
request, so if the service honours it the window served is the one budgeted,
and if it ignores it the service serves at least the model's maximum, which is
at least the budget. Either way the budget is safe, but AutoSE cannot verify
which, so it does not offer window choices it cannot confirm take effect.

`Provider.requires_api_key` is `True` for `ollama-cloud` only. A switch to it,
a default set to it, or a start-up configured for it, with no key, is refused
up front with a `ConfigurationError` that names where to add one, rather than
failing a turn with a 401. `openai-compatible` stays `False`: many such
endpoints are self-hosted and keyless, and a stored key is still used.

### Every provider is listed, probed in parallel

`logic.connections.list_connections` returns one `Connection` per registered
provider, local first: description, `is_local`, `requires_api_key`, the
masked key, the base URL (configured, in use by a session, or conventional),
whether it answered, why not, and its models. Probes run concurrently with the
discovery timeout; a provider that fails, or raises, is a row with an error,
and a provider that needs a key and has none is not contacted at all.

### Switching is per session

`Session.select_model(name, provider=None, base_url=None)` keeps its old
meaning without a provider, and with one delegates to
`Session.use_provider(provider, model, *, base_url=None)`. That rebuilds the
inference settings (address: given, else the session's own for the same
provider, else conventional; key: stored or environment, else the session's
own for the same provider), lists the new runtime's models to find the model's
facts, decides the window afresh with `adopt_context_window` (a window the user
picked carries over only to a runtime that honours requested windows, DR-035),
rebuilds the chat model and the classifier over it, and emits `model_selected`
with `provider`, `base_url` and `previous_provider`. A refusal changes nothing.

Other open sessions are untouched. The dashboard refuses a switch while that
session is running, as it refuses a model change today.

### The default for new sessions is persisted, without the key

`set_default` resolves the provider and model like a configured start-up
(`setup_for_provider`), then `logic.user_config.save_default_inference` writes
`inference.provider`, `inference.base_url` and `inference.model` into the
user's `config.yaml`, keeping every other key. It never writes a key; one the
user put there by hand is left as it was. The round trip is
`yaml.safe_load`/`safe_dump`, so **comments in that file are lost**. It writes
only the user file, never a repository's committed `.autose/config.yaml`.

Configuration naming a provider without a base URL now gets that provider's
conventional address, so `provider: ollama-cloud` alone reaches ollama.com
rather than inheriting the local Ollama default.

### The HTTP surface

- `GET /api/providers`: `{providers: [...], default: {provider, base_url, model}, credentials_file}`.
- `POST /api/providers` with `action` `save_key` (`provider`, `api_key`),
  `delete_key` (`provider`) or `set_default` (`provider`, `model`, optional
  `base_url`, which must be http or https): the same listing back,
  `set_default` adding `saved_to`. Bad input is a 400, deleting a key that is
  not stored a 404.
- `POST /api/model` also accepts `provider` and `base_url`.
- `/api/state` gains `default`; `provider` and `base_url` describe the focused
  session, and `models` lists its current provider's models.

## Consequences

### Positive

- A hosted model can be tried beside a local one, in one session, with no
  restart and no file edited; other sessions carry on where they were.
- Keys are out of every file meant to be shared, readable by their owner only,
  and absent from everything the browser receives.
- Ollama Cloud works with no local Ollama at all: a dashboard whose default
  needs a key opens, says so, and starts a session the moment one is saved.
- Adding the next hosted provider is one `register_provider` call with
  `requires_api_key` and `api_key_env`; nothing that lists, switches or stores
  keys changes (DR-002).

### Negative

- Keys are stored in plain text on disk, protected by file permissions only.
  On Windows those bits are mostly ignored; the file inherits the user
  profile's ACLs.
- Saving the default rewrites the user's `config.yaml` and drops its comments.
- The settings listing probes every provider, so opening it costs up to one
  discovery timeout, and the first listing of Ollama Cloud makes one
  `/api/show` call per model.
- A key deleted or replaced while a session is using it stays in that session
  until it switches provider again.
- Ollama Cloud's window is the published maximum or an assumption, not an
  observation; the dashboard cannot offer to change it.
- `AUTOSE_API_KEY` wins at start-up, but a session that switches away and back
  uses the stored key, if there is one.

### Mitigations

- The file header says what it is and not to share it; the mask is computed in
  one place and tested against every payload that could carry a key.
- Only the user config is rewritten, only three keys in it change, and the
  example config points people to the settings page instead of `api_key`.
- Probes run in parallel outside the bridge lock, so a running session's frames
  never wait on them; `/api/show` results are cached for the process.

## Alternatives considered

### The OS keychain, through the `keyring` package

Rejected. It is the stronger store, but it is a new dependency (AutoSE has two,
and DR-029 keeps the dashboard to the standard library), its Linux backends
need a running secret service that headless and SSH sessions often lack, and
its failure modes differ per platform. A file with tight permissions is what
`~/.ollama`, `~/.netrc` and most CLIs use. A keychain backend could later sit
behind `CredentialStore` with no change to its callers.

### Keep keys in `config.yaml`

Rejected. That file is the one people share, and `.autose/config.yaml` is meant
to be committed (DR-021). Moving keys out also means the default-model writer
can rewrite `config.yaml` without ever handling a secret.

### Keep keys in the browser's `localStorage` and send them with each request

Rejected. Every page script could read them, a key would cross the socket on
every request instead of once, the terminal could not use a key saved in the
browser, and clearing site data would silently lose it.

### Reach Ollama Cloud through a local Ollama signed in to ollama.com

Rejected as the only route. It works, but it requires a local server, which is
exactly what someone on a machine that cannot run the model may not have. The
direct provider has no such requirement; a local Ollama's cloud models still
appear under `ollama` as before.

### Claim `serves_requested_window` for Ollama Cloud

Rejected. Nothing observable confirms that `num_ctx` is honoured there. Offering
a window choice that might be ignored would make the dashboard state a window
it cannot vouch for, which is what DR-022 rules out.

### Switch the provider for the whole dashboard rather than per session

Rejected. DR-031 makes sessions independent; one running a long local `forge`
job should not be moved to a hosted model because another session wanted to
try one.
