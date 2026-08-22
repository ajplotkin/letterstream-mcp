"""The MCP tool surface, exercised exactly as an MCP client would call it."""

from __future__ import annotations

from conftest import make_config
from fixtures.synthetic_recipients import RECIPIENT_DICTS, SENDER_DICT
from letterstream_mcp.store import Store
from letterstream_mcp.toolset import TOOL_NAMES, ToolSet


def _toolset(state_dir, transport, *, live: bool) -> ToolSet:
    config = make_config(state_dir, live=live)
    return ToolSet(config=config, transport=transport, store=Store(state_dir))


def _submit_args(pdf_path, **overrides):
    args = {
        "job_name": "TESTJOB0001",
        "document_path": str(pdf_path),
        "pages": 1,
        "sender": SENDER_DICT,
        "recipients": RECIPIENT_DICTS,
        "mail_type": "certified",
    }
    args.update(overrides)
    return args


def test_every_documented_tool_name_exists_on_the_toolset(state_dir, transport):
    toolset = _toolset(state_dir, transport, live=False)
    for name in TOOL_NAMES:
        assert callable(getattr(toolset, name)), name


def _tool_names_registered_in(source: str) -> set[str]:
    """Names of every function decorated with ``@<something>.tool(...)``.

    Read from the syntax tree rather than by substring, so a name that merely
    appears in a comment or a docstring is not mistaken for a registration.
    """
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                names.add(node.name)
    return names


def test_the_mcp_server_registers_exactly_the_documented_tools(state_dir, transport):
    """The MCP binding and the tested toolset must not drift apart, either way.

    Checked in both directions on purpose. Asserting only that every documented
    name is present would pass while an *undocumented* ninth tool sat on the
    server: untested, absent from the README table, and callable by any MCP
    client that connects. The tool surface is the entire attack surface of an
    MCP server, and drift that *adds* a capability is the direction that
    matters. So the registered set is compared for equality, not containment.
    """
    import inspect

    from letterstream_mcp import server

    registered = _tool_names_registered_in(inspect.getsource(server))
    assert registered == set(TOOL_NAMES), (
        "the MCP server registers a different set of tools than TOOL_NAMES "
        f"documents; extra={sorted(registered - set(TOOL_NAMES))} "
        f"missing={sorted(set(TOOL_NAMES) - registered)}"
    )


def test_the_registration_check_can_actually_see_an_undocumented_tool():
    """Mutation-check on the extraction the test above depends on.

    If ``_tool_names_registered_in`` returned nothing — a renamed decorator, a
    changed AST shape — the equality assertion above would still fail, so that
    much is safe. What this pins is the other half: that an extra registration
    is *seen*, and seen under its own name, rather than quietly skipped.
    """
    synthetic = (
        "def build_server(toolset):\n"
        "    @server.tool()\n"
        "    def letterstream_submit():\n"
        "        pass\n"
        "\n"
        "    @server.tool()\n"
        "    def letterstream_undocumented_backdoor():\n"
        "        pass\n"
    )
    found = _tool_names_registered_in(synthetic)
    assert found == {"letterstream_submit", "letterstream_undocumented_backdoor"}
    assert found - set(TOOL_NAMES) == {"letterstream_undocumented_backdoor"}


def test_check_config_reports_whether_cross_process_locking_is_in_force(
    state_dir, transport
):
    """The concurrency guarantee has a platform caveat, so it is reported.

    ``authorize`` serialises callers with a thread lock plus ``flock``. Without
    :mod:`fcntl` only the thread lock exists, and the exclusion stops at the
    process boundary. An operator running two servers against one state
    directory needs to be able to see which of those they have, rather than
    read a README and hope.
    """
    from letterstream_mcp.store import CROSS_PROCESS_LOCKING

    toolset = _toolset(state_dir, transport, live=False)
    result = toolset.letterstream_check_config()
    assert result["cross_process_locking"] is CROSS_PROCESS_LOCKING
    assert isinstance(result["cross_process_locking"], bool)


def test_submit_tool_in_dry_run_returns_a_preview_and_mails_nothing(
    state_dir, transport, pdf_path
):
    toolset = _toolset(state_dir, transport, live=False)
    result = toolset.letterstream_submit(**_submit_args(pdf_path))

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["mailed"] is False
    assert result["preview"]["recipient_count"] == 2
    assert transport.total_calls == 0


def test_the_full_tool_sequence_mails_once(state_dir, transport, pdf_path):
    toolset = _toolset(state_dir, transport, live=True)

    submitted = toolset.letterstream_submit(**_submit_args(pdf_path))
    assert submitted["mailed"] is False
    assert transport.release_calls == []

    listed = toolset.letterstream_list_proofs()
    assert [p["proof_id"] for p in listed["proofs"]] == [submitted["proof_id"]]

    fetched = toolset.letterstream_get_proof(proof_id=submitted["proof_id"])
    assert fetched["proof"]["document_sha256"] == submitted["document_sha256"]
    assert "authcode" not in fetched["proof"]

    released = toolset.letterstream_authorize(
        proof_id=submitted["proof_id"],
        document_sha256=submitted["document_sha256"],
        acknowledge_cost_usd=submitted["cost_usd"],
    )
    assert released["mailed"] is True
    assert len(transport.release_calls) == 1


def test_a_refusal_is_returned_as_an_error_dict_not_a_traceback(
    state_dir, transport, pdf_path
):
    toolset = _toolset(state_dir, transport, live=True)
    submitted = toolset.letterstream_submit(**_submit_args(pdf_path))
    pdf_path.write_bytes(pdf_path.read_bytes() + b"tampered")

    result = toolset.letterstream_authorize(
        proof_id=submitted["proof_id"], document_sha256=submitted["document_sha256"]
    )
    assert result["ok"] is False
    assert result["mailed"] is False
    assert result["error_type"] == "ProofMismatch"
    assert transport.release_calls == []


def test_the_submit_tool_has_no_parameter_that_could_mail(state_dir, transport):
    """No live/force/send parameter exists on the submit tool or the request type.

    Counted by inspection rather than asserted from memory.
    """
    import inspect

    from letterstream_mcp.models import SubmitRequest

    tool_params = set(inspect.signature(ToolSet.letterstream_submit).parameters)
    request_fields = set(SubmitRequest.__dataclass_fields__)
    forbidden = {"live", "send", "force", "preauth", "authorize", "now", "immediate"}
    assert forbidden & tool_params == set()
    assert forbidden & request_fields == set()


def test_a_bad_address_payload_is_refused_before_the_transport(
    state_dir, transport, pdf_path
):
    toolset = _toolset(state_dir, transport, live=True)
    broken = [dict(RECIPIENT_DICTS[0])]
    broken[0].pop("zip_code")
    result = toolset.letterstream_submit(**_submit_args(pdf_path, recipients=broken))

    assert result["ok"] is False
    assert result["error_type"] == "ValidationError"
    assert "zip_code" in result["error"]
    assert transport.total_calls == 0


def test_a_recipient_without_a_doc_id_is_refused(state_dir, transport, pdf_path):
    toolset = _toolset(state_dir, transport, live=True)
    broken = [{k: v for k, v in RECIPIENT_DICTS[0].items() if k != "doc_id"}]
    result = toolset.letterstream_submit(**_submit_args(pdf_path, recipients=broken))
    assert result["ok"] is False
    assert "doc_id" in result["error"]
    assert transport.total_calls == 0


def test_tracking_is_read_only(state_dir, transport, pdf_path):
    toolset = _toolset(state_dir, transport, live=True)
    submitted = toolset.letterstream_submit(**_submit_args(pdf_path))
    transport.query_body = b'{"tracking": "fake-number"}'

    result = toolset.letterstream_tracking(proof_id=submitted["proof_id"])
    assert result["ok"] is True
    assert transport.release_calls == []
    assert transport.query_calls


def test_downloading_print_proofs_is_read_only(state_dir, transport, pdf_path, tmp_path):
    toolset = _toolset(state_dir, transport, live=True)
    submitted = toolset.letterstream_submit(**_submit_args(pdf_path))
    transport.query_body = b"%PDF-1.4\n% fake print proof\n%%EOF\n"

    result = toolset.letterstream_download_proof_pdfs(
        proof_id=submitted["proof_id"], out_dir=str(tmp_path / "proofs")
    )
    assert result["ok"] is True
    assert len(result["files"]) == 1
    assert transport.release_calls == []
