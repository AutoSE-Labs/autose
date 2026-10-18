"""Runtime configuration: usable with nothing on disk, overridable everywhere."""

from __future__ import annotations

import pytest

from logic.config import (
    CONFIG_FILENAME,
    AutoSEConfig,
    config_search_paths,
    load_config,
    write_example_config,
)
from logic.errors import ConfigurationError
from logic.inference import InferenceSettings


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch, tmp_path):
    """Isolate from the developer's own config file and environment."""
    for name in (
        "AUTOSE_CONFIG",
        "AUTOSE_BASE_URL",
        "AUTOSE_API_KEY",
        "AUTOSE_MODEL",
        "AUTOSE_PROVIDER",
        "AUTOSE_CONTEXT_LIMIT",
        "AUTOSE_WORKSPACE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)


def _write(tmp_path, body: str):
    path = tmp_path / CONFIG_FILENAME
    path.write_text(body, encoding="utf-8")
    return path


def test_defaults_are_complete_with_no_file_present():
    """DR-015: a first run needs nothing on disk."""
    config = load_config()
    assert config.source is None
    assert config.describe_source() == "built-in defaults"
    assert config.inference.provider == "ollama"
    assert config.inference.api_key == ""
    assert not config.auto_approve


def test_a_named_file_is_loaded(tmp_path):
    path = _write(
        tmp_path,
        "inference:\n  provider: vllm\n  base_url: http://gpu.box:8000/v1\n  model: qwen\n",
    )
    config = load_config(path)
    assert config.inference.provider == "vllm"
    assert config.inference.base_url == "http://gpu.box:8000/v1"
    assert config.inference.model == "qwen"
    assert config.source == path


def test_a_missing_named_file_is_an_error(tmp_path):
    with pytest.raises(ConfigurationError, match="not found"):
        load_config(tmp_path / "absent.yaml")


def test_an_empty_file_falls_back_to_defaults(tmp_path):
    config = load_config(_write(tmp_path, "\n"))
    assert config.inference.provider == "ollama"


def test_malformed_yaml_is_a_configuration_error(tmp_path):
    with pytest.raises(ConfigurationError, match="not valid YAML"):
        load_config(_write(tmp_path, "inference:\n  - [unclosed\n"))


def test_a_non_mapping_document_is_refused(tmp_path):
    with pytest.raises(ConfigurationError, match="mapping at the top level"):
        load_config(_write(tmp_path, "- just\n- a\n- list\n"))


def test_a_non_mapping_section_is_refused(tmp_path):
    with pytest.raises(ConfigurationError, match="must be a mapping"):
        load_config(_write(tmp_path, "inference: just-a-string\n"))


def test_a_non_numeric_context_limit_is_refused(tmp_path):
    with pytest.raises(ConfigurationError, match="must be an integer"):
        load_config(_write(tmp_path, "inference:\n  context_limit: enormous\n"))


def test_environment_overrides_the_file(tmp_path, monkeypatch):
    path = _write(tmp_path, "inference:\n  model: from-file\n  base_url: http://file/v1\n")
    monkeypatch.setenv("AUTOSE_MODEL", "from-env")
    config = load_config(path)
    assert config.inference.model == "from-env"
    assert config.inference.base_url == "http://file/v1"


def test_every_inference_field_has_an_environment_override(monkeypatch):
    monkeypatch.setenv("AUTOSE_BASE_URL", "http://env/v1")
    monkeypatch.setenv("AUTOSE_MODEL", "env-model")
    monkeypatch.setenv("AUTOSE_API_KEY", "env-key")
    monkeypatch.setenv("AUTOSE_PROVIDER", "openai-compatible")
    monkeypatch.setenv("AUTOSE_CONTEXT_LIMIT", "16384")
    inference = load_config().inference
    assert inference.base_url == "http://env/v1"
    assert inference.model == "env-model"
    assert inference.api_key == "env-key"
    assert inference.provider == "openai-compatible"
    assert inference.context_limit == 16384


def test_the_workspace_root_defaults_to_the_current_directory(tmp_path):
    assert load_config().resolve_workspace_root() == tmp_path.resolve()


def test_the_workspace_root_can_be_configured_or_overridden(tmp_path, monkeypatch):
    target = tmp_path / "project"
    target.mkdir()
    path = _write(tmp_path, f"workspace:\n  root: {target}\n")
    assert load_config(path).resolve_workspace_root() == target.resolve()

    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("AUTOSE_WORKSPACE", str(other))
    assert load_config(path).resolve_workspace_root() == other.resolve()


def test_auto_approval_must_name_who_opted_in(tmp_path):
    """DR-012: an auto-approved call stays attributable."""
    with pytest.raises(ConfigurationError, match="authorized_by"):
        load_config(_write(tmp_path, "approval:\n  auto_approve: true\n"))

    config = load_config(
        _write(tmp_path, "approval:\n  auto_approve: true\n  authorized_by: ci\n")
    )
    assert config.auto_approve
    assert config.auto_approve_authorized_by == "ci"


def test_the_config_env_var_takes_precedence_in_the_search_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOSE_CONFIG", str(tmp_path / "custom.yaml"))
    assert config_search_paths()[0] == tmp_path / "custom.yaml"


def test_the_example_config_is_valid_and_yields_the_defaults(tmp_path):
    path = write_example_config(tmp_path / "example.yaml")
    config = load_config(path)
    assert config.inference.provider == "ollama"
    assert config.inference.model == ""
    assert config.inference.context_limit is None
    assert not config.auto_approve
    assert config.warnings == ()


def test_uncommenting_the_examples_tuning_block_changes_nothing(tmp_path):
    """DR-021: every commented setting is written at the value already in force.

    Which is what makes the file self-documenting — editing one line changes
    behaviour and deleting it restores the default. DR-030 put ``-1`` in that
    block, so this is also the check that the sentinel in the example and the
    sentinel in the code have not drifted apart.
    """
    import re

    import yaml

    from logic.config import EXAMPLE_CONFIG
    from logic.tuning import Tuning

    lines = EXAMPLE_CONFIG.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "# tuning:")
    block: list[str] = []
    for line in lines[start:]:
        if not line.startswith("#"):
            break
        if line.startswith("# ") and not line.startswith("#   ") and line.strip() != "# tuning:":
            break  # a following comment paragraph, not part of the block
        block.append(re.sub(r"^# ?", "", line, count=1))

    document = yaml.safe_load("\n".join(block))
    assert Tuning.from_mapping(document["tuning"]) == Tuning()


def test_a_typod_prompt_key_is_warned_about_rather_than_silently_ignored(tmp_path):
    config = load_config(
        _write(tmp_path, "prompts:\n  clasifier_system: 'be nice'\n")
    )
    assert any("clasifier_system" in warning for warning in config.warnings)
    assert config.prompt_overrides == {"clasifier_system": "be nice"}


def test_a_known_prompt_key_produces_no_warning(tmp_path):
    config = load_config(
        _write(tmp_path, "prompts:\n  classifier_system: 'be nice'\n")
    )
    assert config.warnings == ()


def test_writing_an_example_refuses_to_clobber(tmp_path):
    path = write_example_config(tmp_path / "example.yaml")
    with pytest.raises(ConfigurationError, match="refusing to overwrite"):
        write_example_config(path)


# -- the repo-local .autose/config.yaml overlay (DR-021) ----------------------


def _write_workspace_config(root, body: str):
    autose_dir = root / ".autose"
    autose_dir.mkdir(parents=True, exist_ok=True)
    (autose_dir / CONFIG_FILENAME).write_text(body, encoding="utf-8")


def test_the_invoking_directorys_own_config_is_layered_on_top(tmp_path):
    """DR-021: a repo's own file applies to work done in it."""
    _write_workspace_config(tmp_path, "inference:\n  model: repo-local\n")
    config = load_config()
    assert config.inference.model == "repo-local"


def test_a_different_workspaces_config_applies_when_told_to_operate_there(tmp_path):
    """--workspace pointing elsewhere must use *that* repo's file, not the
    invoking directory's (a bug: config.py previously always read Path.cwd())."""
    other = tmp_path / "other-repo"
    other.mkdir()
    _write_workspace_config(other, "inference:\n  model: other-repo\n")
    _write_workspace_config(tmp_path, "inference:\n  model: invoking-dir\n")

    assert load_config().inference.model == "invoking-dir"
    assert load_config(workspace_root=other).inference.model == "other-repo"


def test_inference_settings_can_be_swapped_without_mutating_config():
    config = AutoSEConfig()
    updated = config.with_inference(config.inference.with_model("chosen"))
    assert updated.inference.model == "chosen"
    assert config.inference.model == ""


def test_stream_and_request_extras_are_read_from_the_inference_section(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "inference:\n"
        "  base_url: http://localhost:8000/v1\n"
        "  stream: false\n"
        "  request_extras:\n"
        "    top_p: 0.8\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.inference.stream is False
    assert config.inference.request_extras == {"top_p": 0.8}


def test_stream_defaults_to_true():
    assert InferenceSettings().stream is True
