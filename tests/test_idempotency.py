"""Property: a retry after a failure cannot turn one letter into two.

The scenario these tests are built around is the one that actually bites: the
request reaches LetterStream, LetterStream accepts it, and the response never
arrives. The client has no authcode and no proof, but a job exists.

The fake transport records the submit call *before* raising, which is exactly
that shape.
"""

from __future__ import annotations

import pytest

from fakes import job_exists_body, network_drop
from letterstream_mcp.errors import OrphanedJob, TransportError
from letterstream_mcp.gate import (
    JOB_ABSENT,
    JOB_EXISTS,
    JOB_UNKNOWN,
    interpret_job_status,
)
from letterstream_mcp.store import STATE_IN_FLIGHT


def test_retry_after_network_failure_does_not_create_a_second_job(
    live_gate, transport, request_factory
):
    request = request_factory()

    transport.fail_next_submit = network_drop()
    with pytest.raises(TransportError, match=r"connection reset"):
        live_gate.submit(request)
    assert len(transport.submit_calls) == 1, "LetterStream saw the first request"

    # LetterStream now reports the job as present: the accepted-but-unreported case.
    transport.query_queue = [job_exists_body(request.job_name)]

    with pytest.raises(OrphanedJob, match=r"already exists") as excinfo:
        live_gate.submit(request)

    assert len(transport.submit_calls) == 1, "the retry must not submit a second time"
    assert transport.release_calls == []
    assert request.job_name in str(excinfo.value)


def test_failed_attempt_is_recorded_as_still_in_flight(live_gate, transport, request_factory):
    """A failed attempt is not "nothing happened"; it is "outcome unknown"."""
    request = request_factory()
    transport.fail_next_submit = network_drop()
    with pytest.raises(TransportError, match=r"connection reset"):
        live_gate.submit(request)

    record = live_gate.store.get_inflight(request.effective_idempotency_key())
    assert record is not None
    assert record.state == STATE_IN_FLIGHT
    assert record.attempts == 1
    assert "connection reset" in (record.last_error or "")


def test_retry_resubmits_only_when_the_job_is_provably_absent(
    live_gate, transport, request_factory
):
    from fakes import JOB_ABSENT_XML

    request = request_factory()
    transport.fail_next_submit = network_drop()
    with pytest.raises(TransportError, match=r"connection reset"):
        live_gate.submit(request)

    transport.query_queue = [JOB_ABSENT_XML]
    result = live_gate.submit(request)

    assert len(transport.submit_calls) == 2
    assert result["proof_id"]
    assert transport.release_calls == []


def test_an_uninterpretable_status_blocks_the_retry(live_gate, transport, request_factory):
    """Ambiguity must never resolve towards submitting again."""
    request = request_factory()
    transport.fail_next_submit = network_drop()
    with pytest.raises(TransportError, match=r"connection reset"):
        live_gate.submit(request)

    transport.query_queue = [b"<html>maintenance</html>"]
    with pytest.raises(OrphanedJob, match=r"could not be interpreted"):
        live_gate.submit(request)
    assert len(transport.submit_calls) == 1


def test_a_failing_status_check_also_blocks_the_retry(live_gate, transport, request_factory):
    request = request_factory()
    transport.fail_next_submit = network_drop()
    with pytest.raises(TransportError, match=r"connection reset"):
        live_gate.submit(request)

    def explode(fields):
        raise TransportError("status endpoint unreachable")

    transport.query = explode  # type: ignore[assignment]
    with pytest.raises(OrphanedJob, match=r"Checking its status also failed"):
        live_gate.submit(request)
    assert len(transport.submit_calls) == 1


def test_identical_resubmit_reuses_the_existing_proof(live_gate, transport, request_factory):
    """A successful submit repeated verbatim returns the same proof."""
    first = live_gate.submit(request_factory())
    second = live_gate.submit(request_factory())

    assert len(transport.submit_calls) == 1
    assert second["proof_id"] == first["proof_id"]
    assert second["reused_existing_submission"] is True
    assert transport.release_calls == []


def test_an_explicit_idempotency_key_collides_across_differing_requests(
    live_gate, transport, request_factory
):
    """An operator-supplied key wins over the derived fingerprint."""
    first = live_gate.submit(request_factory(idempotency_key="operator-key-1"))
    second = live_gate.submit(
        request_factory(job_name="TESTJOB0002", idempotency_key="operator-key-1")
    )
    assert len(transport.submit_calls) == 1
    assert second["proof_id"] == first["proof_id"]


def test_a_genuinely_different_letter_is_not_deduplicated(
    live_gate, transport, request_factory, tmp_path
):
    """The key must distinguish real differences, or it would suppress real mail.

    A naive constant key would pass every other idempotency test here and fail
    this one.
    """
    other = tmp_path / "second_letter.pdf"
    other.write_bytes(b"%PDF-1.4\n% a different synthetic letter\n%%EOF\n")

    live_gate.submit(request_factory())
    live_gate.submit(request_factory(job_name="TESTJOB0002", document_path=other))

    assert len(transport.submit_calls) == 2


def test_the_derived_key_changes_when_the_document_changes(request_factory, pdf_path):
    before = request_factory().effective_idempotency_key()
    pdf_path.write_bytes(pdf_path.read_bytes() + b"\n% edited\n")
    after = request_factory().effective_idempotency_key()
    assert before != after


def test_the_derived_key_changes_when_a_recipient_changes(request_factory):
    from fixtures.synthetic_recipients import SYNTHETIC_RECIPIENT_A

    before = request_factory().effective_idempotency_key()
    after = request_factory(recipients=(SYNTHETIC_RECIPIENT_A,)).effective_idempotency_key()
    assert before != after


@pytest.mark.parametrize(
    ("parsed", "expected"),
    [
        ({"code": "-100", "docs": [{"job": "TESTJOB0001"}]}, JOB_EXISTS),
        ({"code": "-300", "details": "No record found"}, JOB_ABSENT),
        ({"raw": "<html>maintenance</html>"}, JOB_UNKNOWN),
        ({"code": "-100", "details": "Success"}, JOB_UNKNOWN),
    ],
)
def test_job_status_interpretation(parsed, expected):
    assert interpret_job_status(parsed, "TESTJOB0001") == expected
