"""Repository hygiene, asserted rather than promised.

These tests scan the tracked source for the things this project claims are not
in it. They are cheap and they fail loudly, which is the point: the README says
no credential appears as a literal here, and this is what makes that a checked
statement rather than a hopeful one.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "state", "proofs", ".venv", "venv"}
TEXT_SUFFIXES = {".py", ".toml", ".md", ".txt", ".cfg", ".gitignore", ""}


def _tracked_text_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name == ".gitignore":
            files.append(path)
    return files


def test_no_absolute_home_paths_are_committed():
    offenders = []
    for path in _tracked_text_files():
        if path.name == "test_repo_hygiene.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "/Users/" in text or "/home/" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"absolute home paths found in: {offenders}"


def test_no_assignment_of_a_credential_literal():
    """Catches ``api_key = "something"`` outside the blank example and fixtures.

    Scope, stated exactly: this matches assignment of a non-empty string to a
    name containing key/secret/token/password/api_id. It does not detect a
    credential embedded in prose, in a URL, or in a non-obvious variable name.
    """
    pattern = re.compile(
        r"""(?ix)
        \b(api[_-]?key|api[_-]?id|secret|token|password)\b
        \s*[:=]\s*
        ["']([^"']{4,})["']
        """
    )
    allowed_values = {
        "fake-api-id-for-tests",
        "fake-api-key-for-tests",
        "from-file",
        "key-from-file",
        "from-env",
        "key-from-env",
        "from-cli",
        "key-from-cli",
        "an-id",
        "a-very-secret-key",
        "another-secret-value",
        "only-an-id",
        "only-a-key",
        "operator-key-1",
        "fakeauthcode0001",
        "test fixture",
        "unknown",
    }
    offenders = []
    for path in _tracked_text_files():
        if path.name == "test_repo_hygiene.py":
            continue
        for match in pattern.finditer(path.read_text(encoding="utf-8", errors="ignore")):
            value = match.group(2)
            if value in allowed_values:
                continue
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)[:80]}")
    assert offenders == [], f"credential-shaped literals found: {offenders}"


def test_the_example_config_has_no_filled_in_values():
    text = (REPO_ROOT / "config.example.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        assert "=" in stripped, stripped
        _, _, value = stripped.partition("=")
        assert value.strip() in {'""', "false"}, f"non-blank value in example config: {stripped}"


def test_gitignore_excludes_the_real_config_and_the_ledger():
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    entries = {line.strip() for line in text}
    assert "config.toml" in entries
    assert "state/" in entries
    assert "proofs/" in entries


def test_no_config_toml_is_present_in_the_repository():
    assert not (REPO_ROOT / "config.toml").exists(), (
        "config.toml exists in the working tree; it is gitignored, but it must "
        "not be committed"
    )


def test_the_license_is_apache_2_0():
    text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in text
    assert "Version 2.0, January 2004" in text
    assert "APPENDIX: How to apply the Apache License to your work." in text


def test_the_network_block_is_actually_in_force():
    """Mutation-check on the guard above: prove the block bites.

    If ``no_network`` stopped working, this test would pass silently in a suite
    that had quietly started making real connections. Asserting the refusal
    keeps the "no live API calls" claim honest.
    """
    import socket

    import pytest as _pytest

    with _pytest.raises(AssertionError, match="network connection"):
        socket.create_connection(("127.0.0.1", 9))

    with _pytest.raises(AssertionError, match="network connection"):
        socket.socket().connect(("127.0.0.1", 9))
