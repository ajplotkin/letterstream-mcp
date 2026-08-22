"""Properties around authorize: it mails exactly once, and only on a good proof."""

from __future__ import annotations

import time

import pytest

from letterstream_mcp.errors import (
    AmbiguousRelease,
    CostCeilingExceeded,
    ProofExpired,
    UnknownProof,
)
from letterstream_mcp.transport import RELEASE_FIELD


def _submit(gate, request_factory, **overrides):
    return gate.submit(request_factory(**overrides))


def test_authorize_on_a_valid_proof_mails_exactly_once(live_gate, transport, request_factory):
    submitted = _submit(live_gate, request_factory)
    assert transport.release_calls == []

    result = live_gate.authorize(
        submitted["proof_id"], document_sha256=submitted["document_sha256"]
    )

    assert result["mailed"] is True
    assert len(transport.release_calls) == 1
    assert RELEASE_FIELD in transport.release_calls[0]


def test_authorize_twice_does_not_double_mail(live_gate, transport, request_factory):
    submitted = _submit(live_gate, request_factory)
    first = live_gate.authorize(
        submitted["proof_id"], document_sha256=submitted["document_sha256"]
    )
    second = live_gate.authorize(
        submitted["proof_id"], document_sha256=submitted["document_sha256"]
    )

    assert first["mailed"] is True
    assert second["mailed"] is False
    assert second["already_authorized"] is True
    assert len(transport.release_calls) == 1, "the second authorize must not reach the transport"


def test_authorize_uses_the_stored_authcode(live_gate, transport, request_factory):
    submitted = _submit(live_gate, request_factory)
    proof = live_gate.store.get_proof(submitted["proof_id"])
    live_gate.authorize(submitted["proof_id"], document_sha256=submitted["document_sha256"])
    assert transport.release_calls[0][RELEASE_FIELD] == proof.authcode


def test_authorize_on_an_unknown_proof_refuses_and_does_not_mail(live_gate, transport):
    with pytest.raises(UnknownProof, match=r"No proof 'prf_does_not_exist'"):
        live_gate.authorize("prf_does_not_exist", document_sha256="0" * 64)
    assert transport.release_calls == []


def test_stale_proof_is_refused(live_gate, transport, request_factory):
    submitted = _submit(live_gate, request_factory)
    proof = live_gate.store.get_proof(submitted["proof_id"])
    proof.created_at = time.time() - (live_gate.config.proof_ttl_seconds + 60)
    live_gate.store.put_proof(proof)

    with pytest.raises(ProofExpired, match=r"the limit is"):
        live_gate.authorize(
            submitted["proof_id"], document_sha256=submitted["document_sha256"]
        )
    assert transport.release_calls == []


def test_a_release_that_never_reported_back_is_not_retried(live_gate, transport, request_factory):
    """Crash between "release sent" and "release recorded" must not double-mail.

    The gate stamps ``release_attempted_at`` before the request goes out. A
    later authorize seeing that stamp without a completion refuses rather than
    sending a second release.
    """
    submitted = _submit(live_gate, request_factory)
    transport.fail_next_release = RuntimeError("socket closed mid-release")

    with pytest.raises(RuntimeError, match=r"socket closed mid-release"):
        live_gate.authorize(
            submitted["proof_id"], document_sha256=submitted["document_sha256"]
        )
    assert len(transport.release_calls) == 1

    with pytest.raises(AmbiguousRelease, match=r"started but never confirmed"):
        live_gate.authorize(
            submitted["proof_id"], document_sha256=submitted["document_sha256"]
        )
    assert len(transport.release_calls) == 1, "no second release request may be sent"


def test_cost_ceiling_blocks_authorization(state_dir, transport, request_factory):
    from conftest import make_config
    from letterstream_mcp.gate import MailGate
    from letterstream_mcp.store import Store

    config = make_config(state_dir, live=True, max_authorize_cost_usd=5.0)
    gate = MailGate(config, transport=transport, store=Store(state_dir))
    submitted = gate.submit(request_factory())
    assert submitted["cost_usd"] == 10.89

    with pytest.raises(CostCeilingExceeded, match=r"above the configured ceiling"):
        gate.authorize(submitted["proof_id"], document_sha256=submitted["document_sha256"])
    assert transport.release_calls == []


def test_acknowledged_cost_must_match_the_quote(live_gate, transport, request_factory):
    submitted = _submit(live_gate, request_factory)
    # Not the ceiling path: live_gate has no ceiling configured, so this can
    # only be the acknowledgement mismatch. The pattern says which.
    with pytest.raises(CostCeilingExceeded, match=r"You acknowledged"):
        live_gate.authorize(
            submitted["proof_id"],
            document_sha256=submitted["document_sha256"],
            acknowledge_cost_usd=1.00,
        )
    assert transport.release_calls == []
