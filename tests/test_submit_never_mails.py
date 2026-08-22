"""Property: submit creates a held job and never mails.

The assertion is on the transport, not on a return value: the fake records
``release`` calls separately from ``submit_preauth`` calls, and ``release`` is
the only method that sends LetterStream the field that puts a job into
production.
"""

from __future__ import annotations

import inspect

import pytest

from letterstream_mcp import gate as gate_module
from letterstream_mcp.transport import PREAUTH_FIELD, PREAUTH_VALUE, RELEASE_FIELD


def test_submit_never_calls_release(live_gate, transport, request_factory):
    result = live_gate.submit(request_factory())

    assert transport.submit_calls, "submit should have reached the transport"
    assert transport.release_calls == [], "submit must never call the mailing method"
    assert result["mailed"] is False
    assert result["proof_id"]


def test_submit_sends_no_release_field(live_gate, transport, request_factory):
    live_gate.submit(request_factory())
    fields = transport.submit_calls[0]["fields"]
    assert RELEASE_FIELD not in fields


def test_submit_response_does_not_contain_the_authcode(live_gate, transport, request_factory):
    """The authcode is the credential that releases mail; it stays in the ledger.

    If a submit result carried it, an agent holding that result could build a
    release request without ever passing back through the hash check.
    """
    result = live_gate.submit(request_factory())
    blob = repr(result)
    stored = live_gate.store.get_proof(result["proof_id"])

    assert stored is not None and stored.authcode
    assert stored.authcode not in blob
    assert "authcode" not in result["proof"]


def test_submit_uploads_the_exact_document_bytes(live_gate, transport, request_factory, pdf_path):
    live_gate.submit(request_factory())
    assert transport.submit_calls[0]["document_bytes"] == pdf_path.read_bytes()


def test_gate_submit_source_never_references_release():
    """Structural check: no part of the submit path calls a release method.

    Deleting this assertion would let a future edit reintroduce a direct
    release inside submit without any behavioural test noticing until the fake
    transport happened to be inspected.

    Every function the submit path runs is listed, not just the entry point.
    ``submit`` itself is now a short wrapper that takes the idempotency key's
    lock and delegates to ``_submit_locked``; reading only the wrapper would
    make this check vacuous.
    """
    for name in ("submit", "_submit_locked", "_refuse_or_clear_orphan"):
        source = inspect.getsource(getattr(gate_module.MailGate, name))
        assert ".release(" not in source, name
        assert RELEASE_FIELD not in source, name

    entry = inspect.getsource(gate_module.MailGate.submit)
    assert "_submit_locked" in entry, (
        "submit no longer delegates to _submit_locked; this test is checking a "
        "function that is not on the submit path any more"
    )


def test_transport_forces_preauth_even_when_caller_says_otherwise(monkeypatch):
    """A caller cannot downgrade a submission into an immediate mailing.

    HttpTransport sets preauth after merging caller fields. This test drives the
    real HttpTransport against a fake ``requests`` module, so it checks the form
    payload that would actually go out.
    """
    from fakes import FakeRequests

    from letterstream_mcp.transport import HttpTransport

    fake_requests = FakeRequests()
    transport = HttpTransport("https://fake.invalid/apis/index.php")
    monkeypatch.setattr(transport, "_session", lambda: fake_requests)

    transport.submit_preauth(
        {"job": "TESTJOB0001", PREAUTH_FIELD: "0"},
        document_name="x.pdf",
        document_bytes=b"%PDF-1.4\n",
    )

    sent = dict(fake_requests.posts[0]["data"])
    assert sent[PREAUTH_FIELD] == PREAUTH_VALUE


def test_transport_refuses_a_release_field_on_submit(monkeypatch):
    """The refusal must come from the guard, not from the request failing.

    Without a fake session and without ``match``, this test passed with the
    guard deleted: the call fell through to a real POST to a ``.invalid`` host,
    that failed, and the failure was re-wrapped as ``TransportError``. Injecting
    the session removes the fallback failure, ``match`` pins which
    ``TransportError`` this is, and the final assertion proves nothing was sent.
    """
    from fakes import FakeRequests

    from letterstream_mcp.errors import TransportError
    from letterstream_mcp.transport import HttpTransport

    fake_requests = FakeRequests()
    transport = HttpTransport("https://fake.invalid/apis/index.php")
    monkeypatch.setattr(transport, "_session", lambda: fake_requests)

    with pytest.raises(TransportError, match=r"doauth"):
        transport.submit_preauth(
            {RELEASE_FIELD: "abc"}, document_name="x.pdf", document_bytes=b"%PDF"
        )
    assert fake_requests.posts == [], "the guard must raise before anything is sent"


def test_transport_query_refuses_a_release_field(monkeypatch):
    """Same shape as above: the guard raises, and no request is built."""
    from fakes import FakeRequests

    from letterstream_mcp.errors import TransportError
    from letterstream_mcp.transport import HttpTransport

    fake_requests = FakeRequests()
    transport = HttpTransport("https://fake.invalid/apis/index.php")
    monkeypatch.setattr(transport, "_session", lambda: fake_requests)

    with pytest.raises(TransportError, match=r"doauth"):
        transport.query({RELEASE_FIELD: "abc"})
    assert fake_requests.posts == [], "the guard must raise before anything is sent"


def test_transport_release_refuses_a_request_with_no_release_field(monkeypatch):
    """The mailing endpoint refuses a request it cannot recognise as a release.

    The complement of the two above, and vacuous in exactly the same way if the
    session is not injected.
    """
    from fakes import FakeRequests

    from letterstream_mcp.errors import TransportError
    from letterstream_mcp.transport import HttpTransport

    fake_requests = FakeRequests()
    transport = HttpTransport("https://fake.invalid/apis/index.php")
    monkeypatch.setattr(transport, "_session", lambda: fake_requests)

    with pytest.raises(TransportError, match=r"requires a 'doauth' field"):
        transport.release({"a": "an-id"})
    assert fake_requests.posts == []
