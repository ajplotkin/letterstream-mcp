"""Property: the proof binds to the exact document bytes it approved."""

from __future__ import annotations

import pytest

from letterstream_mcp.errors import ProofMismatch


def test_authorize_rejects_a_tampered_document(live_gate, transport, request_factory, pdf_path):
    """The dangerous case: the caller replays the *correct* hash from submit.

    Only the on-disk re-hash can catch this, which is why an implementation
    that trusts the caller's hash alone fails here.
    """
    submitted = live_gate.submit(request_factory())
    original_hash = submitted["document_sha256"]

    pdf_path.write_bytes(pdf_path.read_bytes() + b"\n% tampered after approval\n")

    with pytest.raises(ProofMismatch, match=r"has changed since it was submitted") as excinfo:
        live_gate.authorize(submitted["proof_id"], document_sha256=original_hash)

    assert transport.release_calls == [], "a tampered document must not be released"
    assert "has changed since it was submitted" in str(excinfo.value)


def test_authorize_rejects_a_wrong_caller_supplied_hash(live_gate, transport, request_factory):
    """The complementary case: file untouched, caller asserts a different hash."""
    submitted = live_gate.submit(request_factory())

    # The pattern separates this from the two other ProofMismatch paths (a
    # changed file, a missing file), which a bare raises would conflate.
    with pytest.raises(ProofMismatch, match=r"hash supplied to authorize does not match"):
        live_gate.authorize(submitted["proof_id"], document_sha256="a" * 64)
    assert transport.release_calls == []


def test_authorize_rejects_a_missing_document(live_gate, transport, request_factory, pdf_path):
    submitted = live_gate.submit(request_factory())
    pdf_path.unlink()

    with pytest.raises(ProofMismatch, match=r"no longer readable at"):
        live_gate.authorize(
            submitted["proof_id"], document_sha256=submitted["document_sha256"]
        )
    assert transport.release_calls == []


def test_a_restored_document_authorizes_normally(live_gate, transport, request_factory, pdf_path):
    """The hash check is a binding, not a one-way latch.

    Tampering then restoring the exact bytes leaves the proof usable. If this
    test failed, the guard would be rejecting on file mtime or on some other
    proxy rather than on content.
    """
    original = pdf_path.read_bytes()
    submitted = live_gate.submit(request_factory())

    pdf_path.write_bytes(original + b"tamper")
    with pytest.raises(ProofMismatch, match=r"has changed since it was submitted"):
        live_gate.authorize(
            submitted["proof_id"], document_sha256=submitted["document_sha256"]
        )
    assert transport.release_calls == []

    pdf_path.write_bytes(original)
    result = live_gate.authorize(
        submitted["proof_id"], document_sha256=submitted["document_sha256"]
    )
    assert result["mailed"] is True
    assert len(transport.release_calls) == 1


def test_hash_case_does_not_matter(live_gate, request_factory):
    submitted = live_gate.submit(request_factory())
    result = live_gate.authorize(
        submitted["proof_id"], document_sha256=submitted["document_sha256"].upper()
    )
    assert result["mailed"] is True
