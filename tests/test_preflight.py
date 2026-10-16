"""Property: PreFlight uploads a document for a window check and nothing else.

It goes through ``transport.preflight`` only — never ``submit_preauth``, never
``release``, never the ledger — and it cannot be dressed up as a submission:
the transport refuses every field that describes a mailing, and the file goes
up under ``preflight_file``, never ``single_file``.
"""

from __future__ import annotations

import base64
import inspect

import pytest

from conftest import MINIMAL_PDF, make_config
from fakes import PREFLIGHT_PDF, FakeRequests, FakeResponse, FakeTransport
from letterstream_mcp import gate as gate_module
from letterstream_mcp.errors import ApiError, DryRunRefusal, TransportError, ValidationError
from letterstream_mcp.gate import MailGate
from letterstream_mcp.store import Store
from letterstream_mcp.toolset import ToolSet
from letterstream_mcp.transport import (
    PREFLIGHT_FILE_FIELD,
    RELEASE_FIELD,
    SUBMISSION_FIELDS,
    HttpTransport,
)


def test_preflight_uses_only_the_preflight_transport_method(live_gate, transport, pdf_path, tmp_path):
    out = tmp_path / "marked.pdf"
    result = live_gate.preflight(pdf_path, out)

    assert len(transport.preflight_calls) == 1
    assert transport.submit_calls == []
    assert transport.release_calls == []
    assert transport.query_calls == []
    assert result["mailed"] is False
    assert out.read_bytes() == PREFLIGHT_PDF
    assert result["out_path"] == str(out)


def test_preflight_writes_nothing_to_the_ledger(live_gate, pdf_path, tmp_path):
    live_gate.preflight(pdf_path, tmp_path / "marked.pdf")
    assert live_gate.store.all_proofs() == {}


def test_preflight_sends_the_documented_fields_and_no_submission_fields(
    live_gate, transport, pdf_path, tmp_path
):
    live_gate.preflight(pdf_path, tmp_path / "marked.pdf")
    call = transport.preflight_calls[0]

    assert call["fields"]["preflight"] == "visual"
    assert {"a", "h", "t"} <= set(call["fields"])
    assert SUBMISSION_FIELDS & set(call["fields"]) == set()
    assert call["document_bytes"] == pdf_path.read_bytes()
    assert call["document_name"] == pdf_path.name


def test_dry_run_preflight_refuses_before_the_transport(dry_gate, transport, pdf_path, tmp_path):
    out = tmp_path / "marked.pdf"
    with pytest.raises(DryRunRefusal, match=r"PreFlight cannot contact LetterStream"):
        dry_gate.preflight(pdf_path, out)
    assert transport.total_calls == 0
    assert not out.exists()


def test_preflight_never_overwrites_an_existing_file(live_gate, transport, pdf_path, tmp_path):
    existing = tmp_path / "marked.pdf"
    existing.write_bytes(b"keep me")
    with pytest.raises(ValidationError, match=r"already exists"):
        live_gate.preflight(pdf_path, existing)
    assert existing.read_bytes() == b"keep me"
    assert transport.total_calls == 0


def test_preflight_cannot_overwrite_the_document_it_checks(live_gate, transport, pdf_path):
    original = pdf_path.read_bytes()
    with pytest.raises(ValidationError, match=r"already exists"):
        live_gate.preflight(pdf_path, pdf_path)
    assert pdf_path.read_bytes() == original
    assert transport.total_calls == 0


def test_preflight_refuses_a_file_that_is_not_a_pdf(live_gate, transport, tmp_path):
    not_pdf = tmp_path / "letter.docx"
    not_pdf.write_bytes(b"PK\x03\x04 not a pdf")
    with pytest.raises(ValidationError, match=r"is not a PDF"):
        live_gate.preflight(not_pdf, tmp_path / "marked.pdf")
    assert transport.total_calls == 0


def test_preflight_refuses_a_missing_document(live_gate, transport, tmp_path):
    with pytest.raises(ValidationError, match=r"No PDF at"):
        live_gate.preflight(tmp_path / "absent.pdf", tmp_path / "marked.pdf")
    assert transport.total_calls == 0


def _gate_with(state_dir, transport: FakeTransport) -> MailGate:
    config = make_config(state_dir, live=True)
    return MailGate(config, transport=transport, store=Store(state_dir))


def test_a_base64_preflight_body_is_decoded(state_dir, pdf_path, tmp_path):
    transport = FakeTransport(preflight_body=base64.b64encode(PREFLIGHT_PDF))
    out = tmp_path / "marked.pdf"
    _gate_with(state_dir, transport).preflight(pdf_path, out)
    assert out.read_bytes() == PREFLIGHT_PDF


def test_an_xml_error_body_raises_and_writes_nothing(state_dir, pdf_path, tmp_path):
    transport = FakeTransport(
        preflight_body=(
            b'<?xml version="1.0"?><messages id="fake-account">'
            b'<message type="error"><code>-501</code><details>Bad file</details></message>'
            b"</messages>"
        )
    )
    out = tmp_path / "marked.pdf"
    with pytest.raises(ApiError, match=r"code -501: Bad file"):
        _gate_with(state_dir, transport).preflight(pdf_path, out)
    assert not out.exists()


def test_an_xml_body_with_no_error_entry_still_is_not_a_success(state_dir, pdf_path, tmp_path):
    """An AUTHOK-only reply carries no PDF, so it must not be reported as a result."""
    transport = FakeTransport(
        preflight_body=(
            b'<?xml version="1.0"?><messages id="fake-account">'
            b'<message type="info"><code>-199</code><details>AUTHOK</details></message>'
            b"</messages>"
        )
    )
    out = tmp_path / "marked.pdf"
    with pytest.raises(ApiError, match=r"instead of a PDF"):
        _gate_with(state_dir, transport).preflight(pdf_path, out)
    assert not out.exists()


def test_an_unreadable_body_raises_and_writes_nothing(state_dir, pdf_path, tmp_path):
    transport = FakeTransport(preflight_body=b"\x00\x01 not a pdf, not xml")
    out = tmp_path / "marked.pdf"
    with pytest.raises(TransportError, match=r"did not return a PDF"):
        _gate_with(state_dir, transport).preflight(pdf_path, out)
    assert not out.exists()


def test_gate_preflight_source_never_references_submit_or_release():
    """Structural check, mirroring the one on submit: no path from PreFlight to mail.

    Both layers are read: the gate method and the client method it calls. The
    patterns are call sites, not words, because the docstrings say "never
    releases" on purpose.
    """
    from letterstream_mcp import client as client_module

    for source in (
        inspect.getsource(gate_module.MailGate.preflight),
        inspect.getsource(client_module.LetterStreamClient.preflight_visual),
    ):
        assert ".release(" not in source
        assert ".submit_preauth(" not in source
        assert ".put_proof(" not in source
        assert RELEASE_FIELD not in source
    assert "self.client.preflight_visual(" in inspect.getsource(gate_module.MailGate.preflight)


# ---- the real transport, against a fake requests module ----------------------


def _http(monkeypatch, response: FakeResponse | None = None) -> tuple[HttpTransport, FakeRequests]:
    fake = FakeRequests(response or FakeResponse(content=PREFLIGHT_PDF))
    transport = HttpTransport("https://fake.invalid/apis/index.php")
    monkeypatch.setattr(transport, "_session", lambda: fake)
    return transport, fake


def test_http_preflight_uploads_under_preflight_file_not_single_file(monkeypatch):
    transport, fake = _http(monkeypatch)
    body = transport.preflight(
        {"a": "x", "h": "y", "t": "1", "preflight": "visual"},
        document_name="letter.pdf",
        document_bytes=MINIMAL_PDF,
    )
    assert body == PREFLIGHT_PDF
    assert len(fake.posts) == 1
    file_fields = [name for name, _ in fake.posts[0]["files"]]
    assert file_fields == [PREFLIGHT_FILE_FIELD]
    assert "single_file" not in file_fields
    assert ("preflight", "visual") in fake.posts[0]["data"]


@pytest.mark.parametrize("field", sorted(SUBMISSION_FIELDS))
def test_http_preflight_refuses_every_submission_field(monkeypatch, field):
    transport, fake = _http(monkeypatch)
    with pytest.raises(TransportError, match=r"submission field"):
        transport.preflight(
            {"preflight": "visual", field: "1"},
            document_name="letter.pdf",
            document_bytes=MINIMAL_PDF,
        )
    assert fake.posts == []


def test_http_preflight_refuses_the_release_field_specifically(monkeypatch):
    """Named on its own so the mutation harness can point at it."""
    transport, fake = _http(monkeypatch)
    with pytest.raises(TransportError, match=RELEASE_FIELD):
        transport.preflight(
            {"preflight": "visual", RELEASE_FIELD: "fakeauthcode0001"},
            document_name="letter.pdf",
            document_bytes=MINIMAL_PDF,
        )
    assert fake.posts == []


@pytest.mark.parametrize("fields", [{}, {"preflight": "auto"}, {"preflight": ""}])
def test_http_preflight_requires_preflight_visual(monkeypatch, fields):
    transport, fake = _http(monkeypatch)
    with pytest.raises(TransportError, match=r"requires preflight='visual'"):
        transport.preflight(fields, document_name="letter.pdf", document_bytes=MINIMAL_PDF)
    assert fake.posts == []


# ---- the tool surface ---------------------------------------------------------


def test_preflight_tool_is_read_only(state_dir, transport, pdf_path, tmp_path):
    toolset = ToolSet(
        config=make_config(state_dir, live=True), transport=transport, store=Store(state_dir)
    )
    result = toolset.letterstream_preflight(
        document_path=str(pdf_path), out_path=str(tmp_path / "marked.pdf")
    )
    assert result["ok"] is True
    assert result["mailed"] is False
    assert transport.submit_calls == []
    assert transport.release_calls == []


def test_preflight_tool_refuses_in_dry_run_as_a_sentence(state_dir, transport, pdf_path, tmp_path):
    toolset = ToolSet(
        config=make_config(state_dir, live=False), transport=transport, store=Store(state_dir)
    )
    result = toolset.letterstream_preflight(
        document_path=str(pdf_path), out_path=str(tmp_path / "marked.pdf")
    )
    assert result["ok"] is False
    assert result["error_type"] == "DryRunRefusal"
    assert transport.total_calls == 0


def test_cli_preflight_reaches_the_tool(monkeypatch, pdf_path, tmp_path, capsys):
    """The CLI subcommand is wired to letterstream_preflight with its arguments."""
    from letterstream_mcp import cli

    seen = {}

    def fake_preflight(self, *, document_path, out_path):
        seen.update(document_path=document_path, out_path=out_path)
        return {"ok": True, "mailed": False}

    monkeypatch.setattr(ToolSet, "letterstream_preflight", fake_preflight)
    monkeypatch.setattr(cli, "load_config", lambda **_: make_config(tmp_path / "s", live=True))
    out = str(tmp_path / "marked.pdf")
    assert cli.main(["preflight", str(pdf_path), "--out", out]) == cli.EXIT_OK
    assert seen == {"document_path": str(pdf_path), "out_path": out}
