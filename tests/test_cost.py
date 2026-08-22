"""Property: the price is visible before the money is spent, and matches after."""

from __future__ import annotations

from fakes import RELEASE_XML, preauth_body


def test_submit_surfaces_the_quoted_cost(live_gate, request_factory):
    result = live_gate.submit(request_factory())
    assert result["cost_usd"] == 10.89
    assert result["proof"]["cost_usd"] == 10.89
    assert result["cost_note"]


def test_per_recipient_costs_are_surfaced(state_dir, request_factory):
    """A two-recipient job shows both copies and their individual prices."""
    from conftest import make_config
    from fakes import FakeTransport

    from letterstream_mcp.gate import MailGate
    from letterstream_mcp.store import Store

    transport = FakeTransport(
        submit_body=preauth_body(
            cost="21.78", doc_ids=("SYNDOC0001", "SYNDOC0002"), per_doc_cost="10.89"
        )
    )
    gate = MailGate(make_config(state_dir, live=True), transport=transport, store=Store(state_dir))
    result = gate.submit(request_factory())

    assert result["cost_usd"] == 21.78
    assert [doc["cost"] for doc in result["proof"]["per_doc"]] == ["10.89", "10.89"]


def test_cost_is_known_before_any_release_call(live_gate, transport, request_factory):
    result = live_gate.submit(request_factory())
    assert result["cost_usd"] is not None
    assert transport.release_calls == [], "the price is quoted while nothing has been mailed"


def test_charged_cost_matches_the_quote(live_gate, request_factory):
    submitted = live_gate.submit(request_factory())
    released = live_gate.authorize(
        submitted["proof_id"], document_sha256=submitted["document_sha256"]
    )
    assert released["quoted_cost_usd"] == 10.89
    assert released["charged_cost_usd"] == 10.89
    assert released["cost_matches_quote"] is True


def test_a_charge_that_differs_from_the_quote_is_reported_not_hidden(
    state_dir, request_factory
):
    """If LetterStream charges something else, the result says so rather than
    echoing the quote back."""
    from conftest import make_config
    from fakes import FakeTransport

    from letterstream_mcp.gate import MailGate
    from letterstream_mcp.store import Store

    transport = FakeTransport(
        release_body=RELEASE_XML.format(batch="fakebatch1", quantity=1, cost="19.99")
    )
    gate = MailGate(make_config(state_dir, live=True), transport=transport, store=Store(state_dir))
    submitted = gate.submit(request_factory())
    released = gate.authorize(
        submitted["proof_id"], document_sha256=submitted["document_sha256"]
    )

    assert released["quoted_cost_usd"] == 10.89
    assert released["charged_cost_usd"] == 19.99
    assert released["cost_matches_quote"] is False


def test_dry_run_reports_that_no_cost_is_available(dry_gate, request_factory):
    """Dry run makes no request, so it has no price. It says that plainly rather
    than inventing an estimate."""
    result = dry_gate.submit(request_factory())
    assert result["cost_usd"] is None
    assert "No cost is available in dry run" in result["cost_note"]
