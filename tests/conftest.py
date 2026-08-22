"""Shared fixtures.

Two things every test here relies on:

* ``Config`` objects are built directly with obviously fake credential strings,
  so no test reads the developer's real config or environment.
* ``tmp_path`` backs the state directory, so ledgers never escape the test run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fakes import FakeTransport  # noqa: E402
from fixtures.synthetic_recipients import (  # noqa: E402
    SYNTHETIC_RECIPIENTS,
    SYNTHETIC_SENDER,
)
from letterstream_mcp.config import Config  # noqa: E402
from letterstream_mcp.gate import MailGate  # noqa: E402
from letterstream_mcp.models import SubmitRequest  # noqa: E402
from letterstream_mcp.store import Store  # noqa: E402

MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Make an in-process network call impossible for the duration of a test.

    Scope, stated exactly: this patches the socket entry points used by
    ``requests`` inside the pytest process. It does not constrain the child
    process spawned by the CLI end-to-end test, and it is not a sandbox. It is
    here so that "the suite never calls LetterStream" is enforced rather than
    assumed.
    """
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError(
            "A test attempted to open a network connection. This suite runs "
            "entirely against fakes and must never contact LetterStream."
        )

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def pdf_path(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic_letter.pdf"
    path.write_bytes(MINIMAL_PDF)
    return path


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


def make_config(state_dir: Path, *, live: bool, **overrides) -> Config:
    """Build a Config without reading any file or environment variable."""
    params = {
        "api_id": "fake-api-id-for-tests",
        "api_key": "fake-api-key-for-tests",
        "base_url": "https://fake.invalid/apis/index.php",
        "live": live,
        "state_dir": state_dir,
        "credential_source": "test fixture",
    }
    params.update(overrides)
    return Config(**params)


@pytest.fixture
def live_config(state_dir: Path) -> Config:
    return make_config(state_dir, live=True)


@pytest.fixture
def dry_config(state_dir: Path) -> Config:
    return make_config(state_dir, live=False)


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def live_gate(live_config: Config, transport: FakeTransport) -> MailGate:
    return MailGate(live_config, transport=transport, store=Store(live_config.state_dir))


@pytest.fixture
def dry_gate(dry_config: Config, transport: FakeTransport) -> MailGate:
    return MailGate(dry_config, transport=transport, store=Store(dry_config.state_dir))


@pytest.fixture
def request_factory(pdf_path: Path):
    def build(**overrides) -> SubmitRequest:
        params = {
            "job_name": "TESTJOB0001",
            "document_path": pdf_path,
            "pages": 1,
            "sender": SYNTHETIC_SENDER,
            "recipients": SYNTHETIC_RECIPIENTS,
            "mail_type": "certified",
        }
        params.update(overrides)
        return SubmitRequest(**params)

    return build
