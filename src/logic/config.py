"""Runtime configuration: where to infer, what to work on, what to auto-approve.

Deliberately narrow. DR-004 keeps organisational standards content out of this
file entirely — different owner, different lifetime, different access concerns
— so this covers only the runtime knobs: the inference endpoint, the workspace
root, and the approval posture.

DR-015 sets the shape of the defaults: a complete, usable configuration with
nothing on disk and no environment set, pointing at a local runtime with no
API key. Loading configuration is therefore never a precondition for running —
it is a way to override what would otherwise be discovered.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .credentials import CredentialStore
from .errors import ConfigurationError
from .inference.registry import available_providers
from .inference.settings import (
    DEFAULT_DISCOVERY_TIMEOUT,
    DEFAULT_PROVIDER,
    DEFAULT_REQUEST_TIMEOUT,
    InferenceSettings,
)
from .prompt_overrides import KNOWN_PROMPT_NAMES, PromptOverrides
from .tuning import Tuning
from .user_config import user_config_dir

__all__ = [
    "AutoSEConfig",
    "config_search_paths",
    "find_config_file",
    "load_config",
    "write_example_config",
    "CONFIG_FILENAME",
    "WORKSPACE_CONFIG_PATH",
]

CONFIG_FILENAME = "config.yaml"

#: Environment overrides, applied last and winning over any file. Named so a
#: CI job or a one-off shell can redirect AutoSE without editing anything.
_ENV_BASE_URL = "AUTOSE_BASE_URL"
_ENV_API_KEY = "AUTOSE_API_KEY"
_ENV_MODEL = "AUTOSE_MODEL"
_ENV_PROVIDER = "AUTOSE_PROVIDER"
_ENV_CONTEXT_LIMIT = "AUTOSE_CONTEXT_LIMIT"
_ENV_WORKSPACE = "AUTOSE_WORKSPACE"
_ENV_CONFIG = "AUTOSE_CONFIG"


@dataclass(frozen=True)
class AutoSEConfig:
    """Everything AutoSE reads before it starts working."""

    inference: InferenceSettings = field(default_factory=InferenceSettings)
    #: Directory AutoSE may inspect and modify. ``None`` means the directory
    #: the command was run from.
    workspace_root: Path | None = None
    #: DR-012: auto-approval is opt-in per run and never inferred. Persisting
    #: it in a file is possible but must be paired with an identity, so an
    #: auto-approved call remains attributable in the audit trail.
    auto_approve: bool = False
    auto_approve_authorized_by: str = ""
    #: DR-024: file edits are recorded and revertible rather than gated. A
    #: workspace that would rather be asked before each write turns this on,
    #: which adds `write_file`/`edit_file` to the approval set.
    require_edit_approval: bool = False
    #: Every tunable knob (DR-021): a repo's `.autose/config.yaml` `tuning:`
    #: section, merged over defaults that reproduce AutoSE's previously
    #: hardcoded behaviour exactly.
    tuning: Tuning = field(default_factory=Tuning)
    #: Named prompt replacements (DR-021), from the same file's `prompts:`
    #: section.
    prompt_overrides: PromptOverrides = field(default_factory=dict)
    #: Where this configuration came from, for diagnostics. Empty means
    #: defaults only — the expected state for a first run (DR-015).
    source: Path | None = None
    #: Non-fatal problems found while loading, e.g. a `prompts:` key that
    #: doesn't match any known prompt name — most likely a typo that would
    #: otherwise silently do nothing.
    warnings: tuple[str, ...] = ()

    def resolve_workspace_root(self) -> Path:
        # Coerced rather than asserted: a string here is the single easiest
        # mistake for an Adapter to make, and it is harmless to accept.
        root = self.workspace_root or Path.cwd()
        return Path(root).expanduser().resolve()

    def with_inference(self, inference: InferenceSettings) -> AutoSEConfig:
        return replace(self, inference=inference)

    def describe_source(self) -> str:
        return str(self.source) if self.source else "built-in defaults"


def config_search_paths() -> tuple[Path, ...]:
    """Where a config file is looked for, in precedence order."""
    paths: list[Path] = []
    override = os.environ.get(_ENV_CONFIG)
    if override:
        paths.append(Path(override).expanduser())
    if os.name != "nt" or os.environ.get("APPDATA"):
        paths.append(user_config_dir() / CONFIG_FILENAME)
    paths.append(Path.cwd() / WORKSPACE_CONFIG_PATH)
    return tuple(paths)


def find_config_file() -> Path | None:
    """Return the first existing config file, or ``None`` for defaults-only."""
    return next((path for path in config_search_paths() if path.is_file()), None)


#: The repo-local file `tuning:`/`prompts:` are meant to live in (DR-021):
#: the last entry in `config_search_paths()`, named directly so the overlay
#: below can find it regardless of which file (if any) precedence already
#: picked.
WORKSPACE_CONFIG_PATH = Path(".autose") / CONFIG_FILENAME


def load_config(
    path: str | Path | None = None, *, workspace_root: str | Path | None = None
) -> AutoSEConfig:
    """Load configuration: defaults, then a file if present, then environment.

    Missing files are not an error — DR-015 requires a complete, usable
    configuration with nothing on disk.

    *workspace_root*'s own `.autose/config.yaml` (DR-021) is always
    additionally layered on top of whatever the search above found — a repo's
    own tuning/prompt customisation applies to work done in it regardless of
    whether a separate machine-level config also exists, rather than only when
    no other config is found. It defaults to the invoking directory, but a
    caller told to operate on a *different* workspace (e.g. `--workspace`)
    must pass that path here, or the wrong repo's file would apply.
    """
    resolved = Path(path).expanduser() if path else find_config_file()
    if resolved is not None and not resolved.is_file():
        raise ConfigurationError(f"config file not found: {resolved}")

    document: dict[str, Any] = {}
    if resolved is not None:
        document = _read_yaml(resolved)

    root = Path(workspace_root).expanduser() if workspace_root else Path.cwd()
    document = _overlay_workspace_config(document, root=root, already_loaded=resolved)

    config = _from_document(document, source=resolved)
    return _with_stored_key(_apply_environment(config))


def _with_stored_key(config: AutoSEConfig) -> AutoSEConfig:
    """Give the configured provider its key from the credentials file (DR-036).

    ``AUTOSE_API_KEY`` still wins — it is the explicit, per-shell override — and
    a legacy ``inference.api_key`` is used only when nothing else supplies one.
    """
    if _ENV_API_KEY in os.environ:
        return config
    key = CredentialStore().resolve(config.inference.provider)
    if not key:
        return config
    return config.with_inference(replace(config.inference, api_key=key))


def _overlay_workspace_config(
    document: dict[str, Any], *, root: Path, already_loaded: Path | None
) -> dict[str, Any]:
    workspace_config = root / WORKSPACE_CONFIG_PATH
    if not workspace_config.is_file():
        return document
    if (
        already_loaded is not None
        and workspace_config.resolve() == already_loaded.resolve()
    ):
        return document
    overlay = _read_yaml(workspace_config)
    return _deep_merge(document, overlay)


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Merge *overlay* over *base*, recursing into shared mapping sections.

    A repo's `.autose/config.yaml` only needs to name the keys it wants to
    change; anything it does not mention keeps whatever the base config (or
    the built-in default) already had.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# -- parsing ---------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency is declared
        raise ConfigurationError(
            "PyYAML is required to read a config file; install autose's dependencies."
        ) from exc
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{path} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigurationError(f"could not read {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigurationError(f"{path} must contain a YAML mapping at the top level")
    return loaded


def _from_document(document: Mapping[str, Any], *, source: Path | None) -> AutoSEConfig:
    inference_section = _section(document, "inference")
    workspace_section = _section(document, "workspace")
    approval_section = _section(document, "approval")

    defaults = InferenceSettings()
    provider = str(inference_section.get("provider") or DEFAULT_PROVIDER)
    inference = InferenceSettings(
        base_url=str(
            inference_section.get("base_url")
            or _conventional_base_url(provider)
            or defaults.base_url
        ),
        model=str(inference_section.get("model") or ""),
        api_key=str(inference_section.get("api_key") or ""),
        provider=provider,
        context_limit=_optional_int(
            inference_section.get("context_limit"), "inference.context_limit"
        ),
        temperature=_float(
            inference_section.get("temperature"),
            defaults.temperature,
            "inference.temperature",
        ),
        timeout=_optional_float(
            inference_section.get("timeout"),
            DEFAULT_REQUEST_TIMEOUT,
            "inference.timeout",
        ),
        discovery_timeout=_float(
            inference_section.get("discovery_timeout"),
            DEFAULT_DISCOVERY_TIMEOUT,
            "inference.discovery_timeout",
        ),
        stream=_bool(inference_section.get("stream"), defaults.stream, "inference.stream"),
        request_extras=_mapping(
            inference_section.get("request_extras"), "inference.request_extras"
        ),
    )

    configured_root = workspace_section.get("root")
    workspace_root = (
        Path(str(configured_root)).expanduser() if configured_root else None
    )

    auto_approve = bool(approval_section.get("auto_approve") or False)
    require_edit_approval = bool(
        approval_section.get("require_edit_approval") or False
    )
    authorized_by = str(approval_section.get("authorized_by") or "")
    if auto_approve and not authorized_by.strip():
        raise ConfigurationError(
            "approval.auto_approve requires approval.authorized_by naming who "
            "opted into unattended approval."
        )

    tuning = Tuning.from_mapping(document.get("tuning"))
    prompt_overrides = _prompt_overrides(document.get("prompts"))
    warnings = _prompt_override_warnings(prompt_overrides)

    return AutoSEConfig(
        inference=inference,
        workspace_root=workspace_root,
        auto_approve=auto_approve,
        auto_approve_authorized_by=authorized_by.strip(),
        require_edit_approval=require_edit_approval,
        tuning=tuning,
        prompt_overrides=prompt_overrides,
        source=source,
        warnings=warnings,
    )


def _prompt_overrides(section: Any) -> PromptOverrides:
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise ConfigurationError(
            "config section 'prompts' must be a mapping of name to text"
        )
    return {str(name): str(text) for name, text in section.items()}


def _prompt_override_warnings(overrides: PromptOverrides) -> tuple[str, ...]:
    unknown = sorted(set(overrides) - set(KNOWN_PROMPT_NAMES))
    if not unknown:
        return ()
    return tuple(
        f"config key 'prompts.{name}' is not a known prompt name and will be "
        "ignored — check for a typo"
        for name in unknown
    )


def _apply_environment(config: AutoSEConfig) -> AutoSEConfig:
    inference = config.inference
    environment = os.environ

    if environment.get(_ENV_BASE_URL):
        inference = replace(inference, base_url=environment[_ENV_BASE_URL])
    if environment.get(_ENV_MODEL):
        inference = replace(inference, model=environment[_ENV_MODEL])
    if _ENV_API_KEY in environment:
        inference = replace(inference, api_key=environment[_ENV_API_KEY])
    if environment.get(_ENV_PROVIDER):
        inference = replace(inference, provider=environment[_ENV_PROVIDER])
    if environment.get(_ENV_CONTEXT_LIMIT):
        inference = replace(
            inference,
            context_limit=_optional_int(
                environment[_ENV_CONTEXT_LIMIT], _ENV_CONTEXT_LIMIT
            ),
        )

    workspace_root = config.workspace_root
    if environment.get(_ENV_WORKSPACE):
        workspace_root = Path(environment[_ENV_WORKSPACE]).expanduser()

    return replace(config, inference=inference, workspace_root=workspace_root)


def _conventional_base_url(provider: str) -> str:
    """Where *provider* usually listens, so naming it is enough (DR-036).

    Without this, ``provider: ollama-cloud`` alone would inherit the local
    Ollama address and fail to connect.
    """
    registered = available_providers().get(provider.strip().lower())
    return registered.default_base_url if registered is not None else ""


def _section(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = document.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"config section '{name}' must be a mapping")
    return value


def _optional_int(value: Any, label: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label} must be an integer, got {value!r}") from exc
    return number if number > 0 else None


def _float(value: Any, fallback: float, label: str) -> float:
    if value is None or value == "":
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label} must be a number, got {value!r}") from exc


def _bool(value: Any, fallback: bool, label: str) -> bool:
    if value is None or value == "":
        return fallback
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    raise ConfigurationError(f"{label} must be true or false, got {value!r}")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{label} must be a mapping, got {value!r}")
    return dict(value)


def _optional_float(value: Any, fallback: float | None, label: str) -> float | None:
    if value is None:
        return fallback
    if value == "" or value is False:
        return None
    return _float(value, fallback or 0.0, label)


# -- scaffolding -------------------------------------------------------------

EXAMPLE_CONFIG = """\
# AutoSE runtime configuration.
#
# Every value here is optional. With no config file at all, AutoSE looks for a
# local runtime (Ollama on its usual port) and picks a model from what it finds
# Set values here only to override that.

inference:
  # Registered provider: ollama | ollama-cloud | lm-studio | vllm | llama-cpp |
  # openai-compatible
  provider: ollama
  # OpenAI-compatible endpoint. Ollama's is at /v1 on port 11434.
  base_url: http://localhost:11434/v1
  # Leave empty to choose from the models the runtime reports.
  model: ""
  # Not needed for a local runtime. Prefer storing a hosted provider's key
  # from the dashboard's settings, which keeps it in an owner-only
  # credentials.yaml beside this file rather than here.
  api_key: ""
  # Leave empty to use the model's real context length where the runtime
  # publishes one, as Ollama does.
  context_limit: ""
  temperature: 0.2
  # Seconds to wait for one generation. Generous by default: a local model on
  # modest hardware legitimately takes minutes.
  timeout: 900

workspace:
  # Directory AutoSE may read and modify. Empty means the directory you ran it
  # from.
  root: ""

approval:
  # Shell and network calls are shown to you and wait for your approval.
  # Turning this off is for unattended runs only, and requires naming who did.
  auto_approve: false
  authorized_by: ""
  # File edits are journalled and revertible with /undo rather than
  # gated, because a local model makes many small edits and a prompt per edit
  # is a prompt people stop reading. Turn this on to be asked first as well.
  require_edit_approval: false

# Every tunable knob AutoSE has, all optional, all commented out below
# at the value that already applies if you leave them out. A repo's own
# .autose/config.yaml (this file, at the workspace root) is always applied on
# top of any machine-level config you also have, so these are the settings to
# commit and share with a team working on this repo.
#
# -1 means "no limit" for every cap here, and the caps that decide how
# long AutoSE keeps working — tool rounds, validation loopbacks, format
# retries, the wall clock — are -1 already. A small local model works in many
# more, smaller steps than a hosted one, so a number that looks generous for
# GPT-scale models cuts it off mid-task. What ends a long run instead is you:
# ctrl-c in the terminal (again to abort), or Stop in the dashboard, either of
# which keeps the work done so far. Put real numbers back below for an
# unattended run — CI, a cron job — where nobody is watching to stop it.
# tuning:
#   context:
#     safety_factor: 1.10
#     reserved_output_fraction: 0.25
#     reserved_output_min: 512
#     reserved_output_max: 8192
#     max_history_rounds: 20      # -1 keeps every round, at the cost of a fuller window
#     min_usable_context_tokens: 8192  # -1 runs in a window too small to work in
#   tool_output:
#     max_chars: 8000             # ceiling; the window narrows it. -1 sends all
#     max_artifacts: 20
#   shell:
#     command_timeout_seconds: 120  # -1 waits forever; nothing can stop it
#   editing:
#     max_edits_per_call: 20      # -1 accepts a batch of any size
#   classifier:
#     max_output_tokens: 200      # -1 sends no output cap at all
#     reasoning_effort: none      # empty disables the field entirely
#     max_history_messages: 4
#   session:
#     max_history_exchanges: 4
#     max_summary_chars: 2000
#   spark:
#     max_tool_rounds: -1
#   craft:
#     max_tool_rounds: -1
#     max_validation_iterations: -1
#     time_budget_seconds: -1
#     max_format_retries: -1

# Replace any built-in prompt by name; see logic.prompt_overrides for the full
# list. Everything not named here keeps its built-in default.
# prompts:
#   classifier_system: |
#     ...
#   spark_system: |
#     ...
#   plan: |
#     ...
#   execute: |
#     ...
#   validate: |
#     ...
"""


def write_example_config(path: str | Path) -> Path:
    """Write a commented example config, refusing to overwrite an existing one."""
    target = Path(path).expanduser()
    if target.exists():
        raise ConfigurationError(f"refusing to overwrite existing config: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    return target
