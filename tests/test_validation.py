"""Local validation: bad submissions fail before anything is sent."""

from __future__ import annotations

import pytest

from fixtures.synthetic_recipients import SYNTHETIC_RECIPIENT_A, SYNTHETIC_SENDER
from letterstream_mcp.errors import ValidationError
from letterstream_mcp.models import Address, Recipient


def test_a_delimiter_in_an_address_field_is_rejected():
    """A colon in a company name would silently shift every later field.

    Without this check the letter still mails, just to a mangled address, which
    is the failure mode that is hardest to notice after the fact.
    """
    address = Address(
        name_1="Placeholder Bank: NA",
        address_1="2 Nowhere Road",
        city="Faketown",
        state="AZ",
        zip_code="99999",
    )
    with pytest.raises(ValidationError, match=r"may not contain ':'"):
        address.as_sender_string()


def test_a_pipe_in_an_address_field_is_rejected():
    address = Address(
        name_1="Placeholder|Bank",
        address_1="2 Nowhere Road",
        city="Faketown",
        state="AZ",
        zip_code="99999",
    )
    with pytest.raises(ValidationError, match=r"may not contain '\|'"):
        address.as_sender_string()


def test_recipient_string_has_eight_fields_and_sender_has_seven():
    assert len(SYNTHETIC_RECIPIENT_A.as_wire_string().split(":")) == 8
    assert len(SYNTHETIC_SENDER.as_sender_string().split(":")) == 7


def test_the_document_id_leads_the_recipient_string():
    assert SYNTHETIC_RECIPIENT_A.as_wire_string().startswith("SYNDOC0001:")


@pytest.mark.parametrize("job_name", ["short", "x" * 21, "has spaces here", ""])
def test_bad_job_names_are_rejected(request_factory, job_name, transport):
    with pytest.raises(ValidationError, match=r"job_name must be 8-20 characters"):
        request_factory(job_name=job_name).validate()


def test_duplicate_doc_ids_are_rejected(request_factory):
    duplicate = Recipient(doc_id="SYNDOC0001", address=SYNTHETIC_RECIPIENT_A.address)
    with pytest.raises(ValidationError, match=r"appears twice"):
        request_factory(recipients=(SYNTHETIC_RECIPIENT_A, duplicate)).validate()


def test_a_non_pdf_document_is_rejected(request_factory, tmp_path):
    not_a_pdf = tmp_path / "letter.pdf"
    not_a_pdf.write_bytes(b"Dear Sir, this is not a PDF at all.\n")
    with pytest.raises(ValidationError) as excinfo:
        request_factory(document_path=not_a_pdf).validate()
    assert "%PDF" in str(excinfo.value)


def test_a_missing_document_is_rejected(request_factory, tmp_path):
    with pytest.raises(ValidationError, match=r"Document not found"):
        request_factory(document_path=tmp_path / "absent.pdf").validate()


def test_zero_recipients_is_rejected(request_factory):
    with pytest.raises(ValidationError, match=r"At least one recipient is required"):
        request_factory(recipients=()).validate()


def test_validation_failure_makes_no_transport_call(live_gate, transport, request_factory):
    with pytest.raises(ValidationError, match=r"job_name must be 8-20 characters"):
        live_gate.submit(request_factory(job_name="bad"))
    assert transport.total_calls == 0
