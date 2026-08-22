"""Repository hygiene, asserted rather than promised.

These tests scan the tracked source for the things this project claims are not
in it. They are cheap and they fail loudly, which is the point: the README says
no credential appears as a literal here, and this is what makes that a checked
statement rather than a hopeful one.

"Tracked" means tracked: the scan asks git which files are actually committed
rather than walking the working tree. The distinction matters because the README
tells an operator to create a real ``config.toml`` in this directory, and that
file is full of credentials by design. It is gitignored, it is not committed,
and a hygiene check that reads it would fail on a correctly configured machine —
which would train people to ignore the check that is supposed to catch a genuine
leak.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "state", "proofs", ".venv", "venv"}
#: Local-only files named in .gitignore. Only consulted by the no-git fallback.
SKIP_FILES = {"config.toml", ".env"}
TEXT_SUFFIXES = {".py", ".toml", ".md", ".txt", ".cfg", ".gitignore", ""}


def _git_tracked_paths() -> list[Path] | None:
    """Every path git has committed, or ``None`` if git cannot answer.

    ``None`` means "no git here" — an unpacked source archive, or git not
    installed — not "nothing is tracked".
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    names = [name for name in completed.stdout.decode("utf-8").split("\0") if name]
    if not names:
        return None
    return [REPO_ROOT / name for name in names]


def _tracked_text_files() -> list[Path]:
    """Text files git tracks, read from the working tree.

    Known limit, stated rather than hidden: this reads the working-tree bytes of
    tracked paths, not the blob content at HEAD. A credential committed in an
    earlier commit and then removed from the working tree without committing the
    removal would pass this scan while still being published by a push. Scanning
    history is the orchestrator's job and is done separately with `git log -p`
    before every push; this suite guards what a developer is about to add.
    """
    candidates = _git_tracked_paths()
    if candidates is None:
        # Fallback for a source export with no git metadata. Weaker: it cannot
        # tell tracked from untracked, so it excludes the gitignored local files
        # by name instead.
        candidates = [
            path
            for path in REPO_ROOT.rglob("*")
            if not any(part in SKIP_DIRS for part in path.parts)
            and path.name not in SKIP_FILES
        ]
    files: list[Path] = []
    for path in candidates:
        if not path.is_file():
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


def test_no_config_toml_is_committed():
    """A real ``config.toml`` may exist locally; it must never be committed.

    Asserted against git's index rather than the filesystem. An operator who
    followed the README has one of these sitting right here, and that is the
    intended state — the failure this guards against is it reaching a commit.
    """
    tracked = _git_tracked_paths()
    if tracked is None:
        import pytest as _pytest

        _pytest.skip("no git metadata here; cannot tell tracked from untracked")
    committed = {str(path.relative_to(REPO_ROOT)) for path in tracked}
    assert "config.toml" not in committed, (
        "config.toml is tracked by git; it is listed in .gitignore and must "
        "never be committed"
    )
    assert not any(name.startswith("state/") for name in committed), (
        "the state/ directory is tracked by git; it holds proof authcodes and "
        "must never be committed"
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
