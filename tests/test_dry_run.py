"""Property: in dry run, no transport method is called under any sequence."""

from __future__ import annotations

import pytest

from letterstream_mcp.errors import DryRunRefusal


def test_dry_run_submit_never_touches_the_transport(dry_gate, transport, request_factory):
    result = dry_gate.submit(request_factory())

    assert transport.total_calls == 0
    assert transport.submit_calls == []
    assert transport.release_calls == []
    assert transport.query_calls == []
    assert result["dry_run"] is True
    assert result["mailed"] is False
    assert result["proof_id"] is None


def test_dry_run_submit_writes_no_proof(dry_gate, request_factory):
    """A dry run must not leave behind something authorize could later act on."""
    dry_gate.submit(request_factory())
    assert dry_gate.store.all_proofs() == {}


def test_dry_run_authorize_refuses_and_never_releases(dry_gate, transport, request_factory):
    dry_gate.submit(request_factory())
    with pytest.raises(DryRunRefusal, match=r"nothing can be authorised or mailed"):
        dry_gate.authorize("prf_anything", document_sha256="0" * 64)
    assert transport.total_calls == 0


def test_dry_run_read_only_calls_also_refuse(dry_gate, transport):
    for call in (
        lambda: dry_gate.account_status(),
        lambda: dry_gate.tracking("prf_anything"),
        lambda: dry_gate.download_proof_pdfs("prf_anything", "/tmp/nope"),
    ):
        with pytest.raises(DryRunRefusal, match=r"cannot contact LetterStream"):
            call()
    assert transport.total_calls == 0


def test_dry_run_survives_a_long_call_sequence(dry_gate, transport, request_factory):
    """Repeated submits and attempted authorizes still reach nothing."""
    for index in range(5):
        dry_gate.submit(request_factory(job_name=f"TESTJOB{index:04d}"))
        with pytest.raises(DryRunRefusal, match=r"nothing can be authorised or mailed"):
            dry_gate.authorize(f"prf_{index}", document_sha256="0" * 64)
        with pytest.raises(DryRunRefusal, match=r"cannot contact LetterStream"):
            dry_gate.account_status()
    assert transport.total_calls == 0


def test_dry_run_is_the_default_when_config_is_silent(tmp_path, monkeypatch):
    """A config file that says nothing about live mode is a dry run."""
    from letterstream_mcp.config import load_config

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[credentials]\napi_id = "x"\napi_key = "y"\n', encoding="utf-8"
    )
    config = load_config(config_path=config_file, env={})
    assert config.live is False


def test_dry_run_still_validates(dry_gate, request_factory, transport):
    """Validation failures happen locally, before any transport call."""
    from letterstream_mcp.errors import ValidationError

    with pytest.raises(ValidationError, match=r"job_name must be 8-20 characters"):
        dry_gate.submit(request_factory(job_name="short"))
    assert transport.total_calls == 0
