"""DR-059: a setting AutoSE saves keeps the comments in the file it lands in."""

from __future__ import annotations

import stat

import pytest
import yaml

from logic.errors import ConfigurationError
from logic.user_config import save_default_inference, update_yaml_scalars
from logic.yaml_edit import set_scalars

COMMENTED = """\
# My AutoSE settings. Keep the cluster model as the default.
inference:
  provider: ollama   # the cluster, through the tunnel
  base_url: http://localhost:11435/v1
  # the 35b model is the only one that tool-calls well
  model: qwen3.6:35b-a3b
  temperature: 0.2

tuning:
  context:
    safety_factor: 1.2  # measured, see notes
"""


# -- the line editor ---------------------------------------------------------------


def test_a_value_changes_on_its_own_line_and_its_comment_stays():
    edited = set_scalars(COMMENTED, {"inference.provider": "vllm"})
    assert edited == COMMENTED.replace(
        "provider: ollama   # the cluster", "provider: vllm   # the cluster"
    )


def test_a_missing_key_is_added_at_the_end_of_its_block():
    edited = set_scalars(COMMENTED, {"inference.context_limit": 65536})
    assert edited is not None
    assert "  temperature: 0.2\n  context_limit: 65536\n\ntuning:" in edited
    assert edited.count("#") == COMMENTED.count("#")


def test_a_missing_parent_is_created_at_the_end_of_the_file():
    text = "# only tuning here\ntuning:\n    session:\n        max_history_exchanges: 4\n"
    edited = set_scalars(text, {"inference.context_limit": 32768})
    # The file's own four-space step is copied for the new block.
    assert edited == text + "inference:\n    context_limit: 32768\n"


def test_an_empty_or_missing_file_gets_just_the_setting():
    assert set_scalars("", {"inference.context_limit": 32768}) == (
        "inference:\n  context_limit: 32768\n"
    )
    assert set_scalars("# nothing yet", {"inference.model": "m"}) == (
        "# nothing yet\ninference:\n  model: m\n"
    )


def test_an_empty_parent_gets_its_child_straight_underneath():
    text = "inference:  # filled in later\nother: 1\n"
    assert set_scalars(text, {"inference.model": "m"}) == (
        "inference:  # filled in later\n  model: m\nother: 1\n"
    )


def test_quoting_is_kept_and_values_are_quoted_where_yaml_needs_it():
    text = "inference:\n  model: \"old\"  # quoted\n  base_url: 'x'\n"
    edited = set_scalars(
        text, {"inference.model": "qwen3:8b", "inference.base_url": "http://h/v1"}
    )
    assert edited == "inference:\n  model: \"qwen3:8b\"  # quoted\n  base_url: 'http://h/v1'\n"
    # A value YAML would read as something else is quoted when it is new.
    edited = set_scalars("inference:\n  model: m\n", {"inference.model": "yes"})
    assert edited is not None and yaml.safe_load(edited)["inference"]["model"] == "yes"


def test_windows_line_endings_are_kept():
    text = "inference:\r\n  model: m\r\n"
    assert set_scalars(text, {"inference.context_limit": 8192}) == (
        "inference:\r\n  model: m\r\n  context_limit: 8192\r\n"
    )


def test_a_hash_inside_a_value_is_not_a_comment():
    text = "inference:\n  base_url: http://h/v1#frag  # real comment\n"
    assert set_scalars(text, {"inference.base_url": "http://g/v1"}) == (
        "inference:\n  base_url: http://g/v1  # real comment\n"
    )


@pytest.mark.parametrize(
    "text",
    [
        "inference: {model: m}\n",  # flow style
        "base: &b\n  model: m\ninference: *b\n",  # anchor and alias
        "inference:\n  <<: *b\n  model: m\n",  # merge key
        "inference:\n  model: !!str m\n",  # tag
        "inference:\n  model: |\n    m\n",  # block scalar
        "inference:\n  model: m\n---\ninference:\n  model: n\n",  # two documents
        "inference:\n\tmodel: m\n",  # tab indentation
        "inference:\n  model: m\n  model: n\n",  # duplicate key
        "inference:\n  model:\n    nested: 1\n",  # a block where a scalar is set
        "inference: null\n",  # a scalar where a block is needed
        "\"inference\":\n  model: m\n",  # quoted key
        "- inference\n",  # not a mapping at all
    ],
)
def test_shapes_it_cannot_vouch_for_are_refused(text):
    assert set_scalars(text, {"inference.model": "x"}) is None


# -- the file updater --------------------------------------------------------------


def test_a_commented_file_keeps_every_comment(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(COMMENTED)
    kept = update_yaml_scalars(
        path, {"inference.model": "qwen3:8b", "inference.context_limit": 65536}
    )
    assert kept
    text = path.read_text()
    for line in COMMENTED.splitlines():
        if "#" in line:
            assert line in text
    loaded = yaml.safe_load(text)
    assert loaded["inference"]["model"] == "qwen3:8b"
    assert loaded["inference"]["context_limit"] == 65536
    assert loaded["tuning"] == {"context": {"safety_factor": 1.2}}


def test_a_file_it_cannot_edit_is_rewritten_correctly_without_comments(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("# gone after this\ninference: {provider: ollama, model: m}\n")
    kept = update_yaml_scalars(path, {"inference.model": "n"})
    assert not kept
    assert yaml.safe_load(path.read_text()) == {
        "inference": {"provider": "ollama", "model": "n"}
    }


def test_a_new_file_is_private_unless_asked_and_an_old_one_keeps_its_mode(tmp_path):
    private = tmp_path / "user" / "config.yaml"
    update_yaml_scalars(private, {"inference.model": "m"})
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    shared = tmp_path / "shared.yaml"
    update_yaml_scalars(shared, {"inference.model": "m"}, new_file_mode=0o644)
    assert stat.S_IMODE(shared.stat().st_mode) == 0o644
    shared.chmod(0o640)
    update_yaml_scalars(shared, {"inference.model": "n"})
    assert stat.S_IMODE(shared.stat().st_mode) == 0o640


def test_a_parent_that_is_not_a_mapping_is_an_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("inference: 3\n")
    with pytest.raises(ConfigurationError):
        update_yaml_scalars(path, {"inference.model": "m"})
    assert path.read_text() == "inference: 3\n"


def test_saving_the_default_model_keeps_the_comments(tmp_path):
    """Before DR-059 this round-tripped through safe_dump and lost them all."""
    path = tmp_path / "config.yaml"
    path.write_text(COMMENTED)
    save_default_inference("ollama", "qwen3:8b", "http://localhost:11434/v1", path=path)
    text = path.read_text()
    assert "# My AutoSE settings." in text
    assert "provider: ollama   # the cluster, through the tunnel" in text
    assert "  # the 35b model is the only one that tool-calls well\n  model: qwen3:8b\n" in text
    assert "safety_factor: 1.2  # measured, see notes" in text
    assert yaml.safe_load(text)["inference"]["base_url"] == "http://localhost:11434/v1"
