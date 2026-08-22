"""Fakes used by the whole suite. No test in this repository touches the network.

:class:`FakeTransport` implements the three-method transport protocol and
records each call separately, so a test can assert "submit was called once and
release was never called" rather than inspecting URLs or form bodies.

:class:`FakeRequests` stands in for the ``requests`` module so that
:class:`~letterstream_mcp.transport.HttpTransport` itself can be tested — the
form payload it builds is checked without a socket being opened.
"""

from __future__ import annotations

from typing import Any

from letterstream_mcp.errors import TransportError

PREAUTH_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<messages id="fake-account">
  <message type="info">
    <code>-200</code>
    <details>Successful preauth: Please resubmit authcode to complete order</details>
    <authcode>{authcode}</authcode>
    <batch>{batch}</batch>
    <quantity>{quantity}</quantity>
    <cost>{cost}</cost>
    {docs}
  </message>
</messages>"""

DOC_XML = "<doc><id>{doc_id}</id><job>{job}</job><cost>{cost}</cost></doc>"

RELEASE_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<messages id="fake-account">
  <message type="info">
    <code>-200</code>
    <details>Success</details>
    <batch>{batch}</batch>
    <quantity>{quantity}</quantity>
    <cost>{cost}</cost>
  </message>
</messages>"""

ACCOUNT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<messages id="fake-account">
  <message type="info"><code>-199</code><details>AUTHOK balance 0.00</details></message>
</messages>"""

JOB_ABSENT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<messages id="fake-account">
  <message type="error"><code>-300</code><details>No record found</details></message>
</messages>"""


def preauth_body(
    *,
    authcode: str = "fakeauthcode0001",
    batch: str = "fakebatch1",
    cost: str = "10.89",
    doc_ids: tuple[str, ...] = ("doc0001",),
    job: str = "TESTJOB0001",
    per_doc_cost: str = "10.89",
) -> str:
    docs = "".join(
        DOC_XML.format(doc_id=d, job=job, cost=per_doc_cost) for d in doc_ids
    )
    return PREAUTH_XML.format(
        authcode=authcode,
        batch=batch,
        quantity=len(doc_ids),
        cost=cost,
        docs=docs,
    )


def job_exists_body(job: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<messages id=\"fake-account\"><message type=\"info\"><code>-100</code>"
        f"<details>Job found</details><doc><id>doc0001</id><job>{job}</job></doc>"
        "</message></messages>"
    )


class FakeTransport:
    """Records calls by consequence. Never performs I/O.

    ``release_calls`` is the list a safety test asserts is empty. A single
    generic ``post()`` would have made that assertion impossible to write.
    """

    def __init__(
        self,
        *,
        submit_body: str | None = None,
        release_body: str | None = None,
        query_body: bytes | str | None = None,
    ) -> None:
        self.submit_calls: list[dict[str, Any]] = []
        self.release_calls: list[dict[str, Any]] = []
        self.query_calls: list[dict[str, Any]] = []

        self.submit_body = submit_body if submit_body is not None else preauth_body()
        self.release_body = (
            release_body
            if release_body is not None
            else RELEASE_XML.format(batch="fakebatch1", quantity=1, cost="10.89")
        )
        self.query_body = query_body if query_body is not None else ACCOUNT_XML

        #: Set to an exception to make the next submit fail *after* recording,
        #: which is what a network drop on an accepted request looks like.
        self.fail_next_submit: Exception | None = None
        self.fail_next_release: Exception | None = None
        #: Queue of bodies returned by successive query() calls, if non-empty.
        self.query_queue: list[bytes | str] = []

    @property
    def total_calls(self) -> int:
        return len(self.submit_calls) + len(self.release_calls) + len(self.query_calls)

    def submit_preauth(
        self, fields: dict[str, Any], *, document_name: str, document_bytes: bytes
    ) -> str:
        # Recorded before the failure is raised: the server saw this request.
        self.submit_calls.append(
            {
                "fields": dict(fields),
                "document_name": document_name,
                "document_bytes": document_bytes,
            }
        )
        if self.fail_next_submit is not None:
            failure, self.fail_next_submit = self.fail_next_submit, None
            raise failure
        return self.submit_body

    def release(self, fields: dict[str, Any]) -> str:
        self.release_calls.append(dict(fields))
        if self.fail_next_release is not None:
            failure, self.fail_next_release = self.fail_next_release, None
            raise failure
        return self.release_body

    def query(self, fields: dict[str, Any]) -> bytes:
        self.query_calls.append(dict(fields))
        body = self.query_queue.pop(0) if self.query_queue else self.query_body
        return body.encode("utf-8") if isinstance(body, str) else body


class FakeResponse:
    def __init__(self, text: str = "", content: bytes | None = None, status: int = 200):
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeRequests:
    """Stand-in for the ``requests`` module, for testing HttpTransport itself."""

    def __init__(self, response: FakeResponse | None = None) -> None:
        self.posts: list[dict[str, Any]] = []
        self.response = response or FakeResponse(preauth_body())

    def post(self, url, data=None, files=None, timeout=None):  # noqa: ANN001
        self.posts.append({"url": url, "data": data, "files": files, "timeout": timeout})
        return self.response


def network_drop(message: str = "connection reset after request was sent") -> TransportError:
    return TransportError(message)
