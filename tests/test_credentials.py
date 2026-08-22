"""Property: credentials come from the operator, and their absence is a sentence."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from letterstream_mcp.config import (
    ENV_API_ID,
    ENV_API_KEY,
    ENV_LIVE,
    ConfigError,
    load_config,
)
from letterstream_mcp.errors import CredentialsError

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_missing_credentials_raise_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CredentialsError) as excinfo:
        load_config(env={})
    message = str(excinfo.value)
    assert "credentials are not configured" in message
    assert "config.example.toml" in message
    assert ENV_API_ID in message and ENV_API_KEY in message


def test_partial_credentials_are_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CredentialsError, match=r"credentials are not configured"):
        load_config(env={ENV_API_ID: "only-an-id"})
    with pytest.raises(CredentialsError, match=r"credentials are not configured"):
        load_config(env={ENV_API_KEY: "only-a-key"})


def test_blank_values_in_the_example_config_count_as_absent(tmp_path, monkeypatch):
    """config.example.toml ships every value blank; copying it must not "work"."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "config.toml"
    target.write_text(
        (REPO_ROOT / "config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(CredentialsError, match=r"credentials are not configured"):
        load_config(config_path=target, env={})


def test_precedence_cli_over_env_over_file(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[credentials]\napi_id = "from-file"\napi_key = "key-from-file"\n', encoding="utf-8"
    )

    from_file = load_config(config_path=config_file, env={})
    assert from_file.api_id == "from-file"

    from_env = load_config(
        config_path=config_file, env={ENV_API_ID: "from-env", ENV_API_KEY: "key-from-env"}
    )
    assert from_env.api_id == "from-env"
    assert from_env.api_key == "key-from-env"

    from_cli = load_config(
        config_path=config_file,
        api_id="from-cli",
        api_key="key-from-cli",
        env={ENV_API_ID: "from-env", ENV_API_KEY: "key-from-env"},
    )
    assert from_cli.api_id == "from-cli"
    assert from_cli.api_key == "key-from-cli"


def test_a_named_config_file_that_does_not_exist_is_an_error(tmp_path):
    """``match`` is load-bearing here, because ``ConfigError`` is too broad.

    ``CredentialsError`` subclasses ``ConfigError``. Without the pattern, this
    test passed with the missing-file check deleted: the loader fell through,
    found no credentials, and raised ``CredentialsError`` — which satisfies a
    bare ``pytest.raises(ConfigError)``. The pattern is what makes the test
    about the file rather than about the credentials.
    """
    with pytest.raises(ConfigError, match=r"Config file not found"):
        load_config(config_path=tmp_path / "absent.toml", env={})


def test_redacted_view_never_contains_the_key(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[credentials]\napi_id = "an-id"\napi_key = "a-very-secret-key"\n', encoding="utf-8"
    )
    config = load_config(config_path=config_file, env={})
    blob = repr(config.redacted())
    assert "a-very-secret-key" not in blob
    assert config.redacted()["api_key_present"] is True


def test_check_config_tool_never_returns_the_key(state_dir, transport):
    from conftest import make_config

    from letterstream_mcp.toolset import ToolSet

    config = make_config(state_dir, live=False, api_key="another-secret-value")
    toolset = ToolSet(config=config, transport=transport)
    assert "another-secret-value" not in repr(toolset.letterstream_check_config())


def test_no_transport_call_happens_when_credentials_are_missing(tmp_path, monkeypatch):
    """Credential failure occurs during config load, before a gate exists."""
    monkeypatch.chdir(tmp_path)
    from letterstream_mcp.toolset import ToolSet

    with pytest.raises(CredentialsError, match=r"credentials are not configured"):
        ToolSet(env={})


def test_cli_reports_missing_credentials_without_a_traceback(tmp_path):
    """End to end: the process prints a sentence and exits 2."""
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "HOME": str(tmp_path),
    }
    completed = subprocess.run(
        [sys.executable, "-m", "letterstream_mcp.cli", "check-config"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "Traceback" not in completed.stderr
    assert "credentials are not configured" in completed.stderr


def test_live_mode_cannot_be_turned_on_by_a_cli_flag():
    """There is no --live flag, and load_config takes no live argument.

    Deleting this test would let someone add the convenience flag the design
    deliberately omits without anything noticing.
    """
    import inspect

    from letterstream_mcp.cli import build_parser

    options: set[str] = set()

    def collect(parser) -> None:
        for action in parser._actions:
            options.update(action.option_strings)
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):  # a subparsers action
                for subparser in choices.values():
                    collect(subparser)

    collect(build_parser())
    # Sanity: the walk really does reach subcommand flags, so a --live added to
    # any subcommand would be caught rather than silently missed.
    assert "--document-sha256" in options
    assert "--live" not in options
    assert "--send" not in options
    assert "--force" not in options
    assert "live" not in inspect.signature(load_config).parameters


def test_live_mode_can_be_turned_on_by_config_or_environment(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[credentials]\napi_id = "x"\napi_key = "y"\n\n[safety]\nlive = true\n',
        encoding="utf-8",
    )
    assert load_config(config_path=config_file, env={}).live is True

    plain = tmp_path / "plain.toml"
    plain.write_text('[credentials]\napi_id = "x"\napi_key = "y"\n', encoding="utf-8")
    assert load_config(config_path=plain, env={ENV_LIVE: "true"}).live is True
    assert load_config(config_path=plain, env={ENV_LIVE: "false"}).live is False


def test_an_unparseable_live_value_is_an_error_not_a_silent_true(tmp_path):
    plain = tmp_path / "plain.toml"
    plain.write_text('[credentials]\napi_id = "x"\napi_key = "y"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"LETTERSTREAM_LIVE"):
        load_config(config_path=plain, env={ENV_LIVE: "maybe"})
