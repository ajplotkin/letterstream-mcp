"""Read-only calls must not report success when LetterStream reported failure.

These fixtures are the real shapes observed live on 2026-08-27, including the
one that exposed the gap: a deleted job's tracking lookup returned HTTP 200 with
a JSON body whose ``message`` list held an ``AUTHOK`` info entry *followed by*
two error entries. Every previous test asserted on the happy path, so a call
that returned ``ok: true`` over an error payload passed 104 tests.

The AUTHOK-then-error ordering is deliberate: an implementation that inspects
only the first message, or that treats "an info entry is present" as success,
passes the success case and fails here.
"""

from __future__ import annotations

import json

import pytest

from letterstream_mcp.client import LetterStreamClient
from letterstream_mcp.errors import ApiError
from tests.fakes import ACCOUNT_XML, JOB_ABSENT_XML, FakeTransport

# Observed live: doc_id that no longer exists (job deleted server-side).
TRACKING_ERROR_JSON = json.dumps(
    {
        "apiid": "fake-account",
        "message": [
            {"@attributes": {"type": "info"}, "code": "-199", "details": "AUTHOK "},
            {
                "@attributes": {"type": "error"},
                "code": "-924",
                "details": "Failed: invalid doc id (doc_id(DOC0001)) ",
            },
            {
                "@attributes": {"type": "error"},
                "code": "-999",
                "details": "Error: (We could not retrieve that info.) ",
            },
        ],
    }
).encode()

# Observed live: a held job, before it was deleted.
TRACKING_OK_JSON = json.dumps(
    {
        "apiid": "fake-account",
        "message": [
            {"@attributes": {"type": "info"}, "code": "-199", "details": "AUTHOK "},
            {
                "@attributes": {"type": "docstatus"},
                "id": "DOC0001",
                "job": "TESTJOB0001",
                "code": "-1",
                "status": "Needs Attention",
                "history": "PreAuth - 08/27/2026 7:30 am",
            },
        ],
    }
).encode()

ACCOUNT_ERROR_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<messages id="fake-account">
  <message type="error"><code>-901</code><details>Authentication failed</details></message>
</messages>"""


def _client(live_config, body):
    return LetterStreamClient(live_config, FakeTransport(query_body=body))


def test_tracking_raises_when_payload_carries_errors(live_config):
    client = _client(live_config, TRACKING_ERROR_JSON)
    with pytest.raises(ApiError) as excinfo:
        client.tracking("DOC0001")
    message = str(excinfo.value)
    # Both errors surface, not just the first.
    assert "-924" in message
    assert "-999" in message
    assert "invalid doc id" in message


def test_tracking_returns_payload_when_only_info_and_status_present(live_config):
    """The success case must still pass — the guard keys on error entries only."""
    client = _client(live_config, TRACKING_OK_JSON)
    payload = client.tracking("DOC0001")
    entries = payload["message"]
    assert any(e.get("@attributes", {}).get("type") == "docstatus" for e in entries)


def test_tracking_error_is_not_masked_by_the_leading_authok(live_config):
    """Guards the exact mutation: 'first message is info, therefore success'."""
    payload = json.loads(TRACKING_ERROR_JSON)
    assert payload["message"][0]["@attributes"]["type"] == "info"
    assert payload["message"][0]["code"] == "-199"
    client = _client(live_config, TRACKING_ERROR_JSON)
    with pytest.raises(ApiError):
        client.tracking("DOC0001")


def test_account_status_raises_on_error_code(live_config):
    client = _client(live_config, ACCOUNT_ERROR_XML)
    with pytest.raises(ApiError):
        client.account_status()


def test_account_status_accepts_authok(live_config):
    client = _client(live_config, ACCOUNT_XML)
    assert client.account_status()["code"] == "-199"


def test_job_status_does_not_raise_on_error_code(live_config):
    """Regression guard.

    ``interpret_job_status`` reads non-success codes to reach ``JOB_ABSENT``.
    Making ``job_status`` raise would make that branch unreachable and block a
    legitimate resubmission after a mid-flight failure. This asserts the
    asymmetry is intentional rather than an oversight someone should "fix".
    """
    client = _client(live_config, JOB_ABSENT_XML)
    parsed = client.job_status("TESTJOB0001")
    assert parsed["code"] == "-300"


def test_toolset_tracking_surfaces_the_error_as_not_ok(state_dir, pdf_path):
    """End to end through the MCP surface: ok must be False, not True.

    Uses a real submit to create the proof, so the tracking call resolves a
    genuine doc_id rather than a hand-forged ledger entry.
    """
    from conftest import make_config
    from fixtures.synthetic_recipients import RECIPIENT_DICTS, SENDER_DICT
    from letterstream_mcp.store import Store
    from letterstream_mcp.toolset import ToolSet

    transport = FakeTransport()
    toolset = ToolSet(
        config=make_config(state_dir, live=True), transport=transport, store=Store(state_dir)
    )
    submitted = toolset.letterstream_submit(
        job_name="TESTJOB0001",
        document_path=str(pdf_path),
        pages=1,
        sender=SENDER_DICT,
        recipients=RECIPIENT_DICTS,
        mail_type="certified",
    )
    assert submitted["ok"] is True

    # The job is now gone server-side; the lookup fails while HTTP still says 200.
    transport.query_body = TRACKING_ERROR_JSON
    result = toolset.letterstream_tracking(proof_id=submitted["proof_id"])

    assert result["ok"] is False, "a failed lookup must not report ok: true"
    assert "-924" in result["error"]
    assert transport.release_calls == []


@pytest.mark.parametrize(
    "body",
    [
        b"-999",
        b"null",
        b'["Error: bad"]',
        b'"AUTHOK"',
        b'{"tracking": "number"}',
        b'{"message": "oops"}',
        b'{"message": []}',
    ],
    ids=[
        "bare-number",
        "null",
        "json-array",
        "json-string",
        "object-without-message",
        "message-not-a-list",
        "message-empty-list",
    ],
)
def test_unreadable_json_bodies_are_refused_not_passed_through(live_config, body):
    """A body we cannot read must never come back as a successful lookup.

    Each of these is valid JSON. Against the code as committed before this
    change, all six returned ok: true; against the intermediate guard written
    during review, four of them raised a bare AttributeError that escaped the
    toolset's LetterStreamError handler as a traceback. Both are wrong.
    """
    client = _client(live_config, body)
    with pytest.raises(ApiError):
        client.tracking("DOC0001")


def test_malformed_message_entries_are_refused(live_config):
    """A malformed entry must be refused, not skipped.

    An earlier version of this test asserted the opposite — that an entry whose
    ``@attributes`` is the string ``"error"`` returns successfully. That is not
    "does not crash", it is "a malformed error entry is ignored", which is the
    ok-over-error path this module exists to close.
    """
    for entry in (
        None,
        "error",
        {"@attributes": "error", "code": "-924"},
        # An unreadable `type` slot: an error could be hiding in it, so it is
        # refused for the same reason a non-dict `@attributes` is.
        {"@attributes": {"type": ["error"]}, "code": "-924"},
        {"@attributes": {"type": {"k": "error"}}, "code": "-924"},
    ):
        body = json.dumps({"message": [entry]}).encode()
        with pytest.raises(ApiError):
            _client(live_config, body).tracking("DOC0001")


def test_unknown_entry_types_still_pass(live_config):
    """The refusal must not be so wide that benign entry types break lookups."""
    body = json.dumps(
        {"message": [{"@attributes": {"type": "somethingnew"}, "code": "-1"}]}
    ).encode()
    assert _client(live_config, body).tracking("DOC0001")["message"][0]["code"] == "-1"


def test_a_single_message_object_is_accepted(live_config):
    """`message` may be one object rather than a list; covers the dict-wrap branch."""
    body = json.dumps(
        {"message": {"@attributes": {"type": "error"}, "code": "-924", "details": "bad"}}
    ).encode()
    client = _client(live_config, body)
    with pytest.raises(ApiError) as excinfo:
        client.tracking("DOC0001")
    assert "-924" in str(excinfo.value)


XML_ERROR_BODY = (
    b'<?xml version="1.0"?><messages id="fake-account">'
    b"<message type=\"info\"><code>-199</code><details>AUTHOK</details></message>"
    b"<message type=\"error\"><code>-924</code>"
    b"<details>Failed: invalid doc id</details></message></messages>"
)

XML_SUCCESS_BODY = (
    b'<?xml version="1.0"?><messages id="fake-account">'
    b"<message type=\"info\"><code>-199</code><details>AUTHOK</details></message>"
    b"<message type=\"docstatus\"><code>-1</code>"
    b"<details>Needs Attention</details></message></messages>"
)


def test_xml_fallback_refuses_an_error_body(live_config):
    """If LetterStream answers in XML, an error there must fail the call too.

    The JSON guard cannot see this shape. Applying ``raise_for_api_error`` here
    would not catch it either: that reads the promoted top-level code, which the
    leading AUTHOK fills in while the trailing error goes unread — precisely the
    live failure shape.
    """
    with pytest.raises(ApiError) as excinfo:
        _client(live_config, XML_ERROR_BODY).tracking("DOC0001")
    assert "-924" in str(excinfo.value)


def test_xml_fallback_accepts_a_success_body(live_config):
    """The XML refusal must not reject a valid lookup whose codes are unobserved."""
    parsed = _client(live_config, XML_SUCCESS_BODY).tracking("DOC0001")
    assert parsed["messages"]


def test_xml_fallback_refuses_an_empty_message_list(live_config):
    body = b'<?xml version="1.0"?><messages id="fake-account"></messages>'
    with pytest.raises(ApiError):
        _client(live_config, body).tracking("DOC0001")


@pytest.mark.parametrize("spelling", ["Error", " ERROR ", "eRrOr"])
def test_error_type_matching_is_case_and_whitespace_insensitive(live_config, spelling):
    """Both transports must agree on what counts as an error type.

    These were two separate comparisons once: the JSON guard normalised case and
    whitespace, the XML fallback did not, so `type="Error"` raised on one path
    and returned successfully on the other. They now share `_is_error_type`.
    """
    body = json.dumps(
        {"message": [{"@attributes": {"type": spelling}, "code": "-924"}]}
    ).encode()
    with pytest.raises(ApiError):
        _client(live_config, body).tracking("DOC0001")

    xml = (
        b'<?xml version="1.0"?><messages id="fake-account"><message type="'
        + spelling.encode()
        + b'"><code>-924</code><details>bad</details></message></messages>'
    )
    with pytest.raises(ApiError):
        _client(live_config, xml).tracking("DOC0001")


@pytest.mark.parametrize(
    "entry",
    [{"@attributes": {}, "code": "-1"}, {"code": "-1"}],
    ids=["empty-attributes", "absent-attributes"],
)
def test_a_readable_absence_of_type_is_not_an_error(live_config, entry):
    """The carve-out: a missing type is readable, so it is benign.

    Distinct from a non-dict `@attributes`, which is an unreadable type slot and
    is refused — an error could be hiding in it. This pins the asymmetry.
    """
    body = json.dumps({"message": [entry]}).encode()
    assert _client(live_config, body).tracking("DOC0001")["message"][0]["code"] == "-1"


def test_null_code_and_details_render_readably(live_config):
    """A JSON null must not surface to the operator as the string "None"."""
    body = json.dumps(
        {"message": [{"@attributes": {"type": "error"}, "code": None, "details": None}]}
    ).encode()
    with pytest.raises(ApiError) as excinfo:
        _client(live_config, body).tracking("DOC0001")
    rendered = str(excinfo.value)
    assert "None" not in rendered
    assert "no detail supplied" in rendered


@pytest.mark.parametrize(
    "details,expected",
    [
        (None, "no detail supplied"),
        ("", "no detail supplied"),
        ("   ", "no detail supplied"),
        (0, "0"),
        (False, "False"),
        ("real detail", "real detail"),
    ],
    ids=["null", "empty", "whitespace", "zero", "false", "text"],
)
def test_falsy_details_are_not_reported_as_absent(live_config, details, expected):
    """0 and False are values; only a missing one is "no detail supplied".

    An earlier rendering used `or ''`, so a legitimate `details: 0` was reported
    to the operator as "no detail supplied" — a false statement inside an error
    message. A later attempt to fix it inverted the null case instead.
    """
    body = json.dumps(
        {"message": [{"@attributes": {"type": "error"}, "code": "-1", "details": details}]}
    ).encode()
    with pytest.raises(ApiError) as excinfo:
        _client(live_config, body).tracking("DOC0001")
    assert f"code -1: {expected}" in str(excinfo.value)


def test_account_status_error_behind_a_leading_authok_is_not_masked(live_config):
    """AUTHOK first, error second — the shape the live incident had.

    `raise_for_api_error` reads only the code promoted to the top level, which
    the leading AUTHOK fills in, so on its own it would report success here.
    """
    xml = (
        b'<?xml version="1.0"?><messages id="fake-account">'
        b"<message type=\"info\"><code>-199</code><details>AUTHOK</details></message>"
        b"<message type=\"error\"><code>-901</code>"
        b"<details>Account suspended</details></message></messages>"
    )
    with pytest.raises(ApiError) as excinfo:
        _client(live_config, xml).account_status()
    assert "-901" in str(excinfo.value)


def test_account_status_raises_on_a_non_success_code_with_no_error_entry(live_config):
    """Pins the code-based guard specifically, not the message scan.

    `account_status` has two guards: `raise_for_api_error` on the promoted
    top-level code, and a scan for error-typed messages. Most fixtures trip
    both, which makes either one look redundant. This body trips only the
    first — a non-success code carried on an `info` message — so removing it
    is detectable.
    """
    xml = (
        b'<?xml version="1.0"?><messages id="fake-account">'
        b"<message type=\"info\"><code>-300</code>"
        b"<details>No record found</details></message></messages>"
    )
    with pytest.raises(ApiError) as excinfo:
        _client(live_config, xml).account_status()
    assert "-300" in str(excinfo.value)


def test_the_observed_live_account_status_shape_passes(live_config):
    """The two-message shape the live service actually returned, verbatim.

    `ACCOUNT_XML` in fakes.py is a single AUTHOK message; the real response
    carried a second, empty `accountstatus` message. Both guards must pass it —
    a guard that rejected this would break the only response shape anyone has
    ever seen from this endpoint.
    """
    xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<messages id="fake-account">'
        b"<message type=\"info\"><code>-199</code>"
        b"<details>AUTHOK (082726 07:28:11)</details></message>"
        b"<message type=\"accountstatus\"></message></messages>"
    )
    parsed = _client(live_config, xml).account_status()
    assert parsed["code"] == "-199"
    assert len(parsed["messages"]) == 2
