"""Thin client for the LetterStream integration API.

This layer knows how to authenticate, how to shape a request, and how to read
the XML that comes back. It knows nothing about proofs, idempotency, or dry
runs; that policy lives in :mod:`letterstream_mcp.gate`.

Authentication follows the scheme LetterStream documents for their integration
API: a per-request unique id (a millisecond timestamp), the account's API
identifier, and an MD5 digest over a base64 encoding of the API key wrapped in
slices of that unique id. MD5 here is LetterStream's choice of construction and
is reproduced because their server verifies it; it is not a security decision
made by this project. The API key itself is never transmitted.
"""

from __future__ import annotations

import base64
import hashlib
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any

from .config import Config
from .errors import ApiError, TransportError
from .models import SubmitRequest
from .transport import RELEASE_FIELD, Transport

#: Response codes LetterStream documents as successful outcomes.
CODE_SUBMIT_SUCCESS = "-100"
CODE_PREAUTH = "-200"
CODE_AUTH_OK = "-199"
CODE_INSUFFICIENT_FUNDS = "-911"

SUCCESS_CODES = frozenset({CODE_SUBMIT_SUCCESS, CODE_PREAUTH, CODE_AUTH_OK})


def build_auth_fields(api_id: str, api_key: str, *, unique_id: str | None = None) -> dict[str, str]:
    """Return the ``a``/``h``/``t`` fields every request carries.

    ``unique_id`` is injectable so tests can pin it. In production it is the
    current time in milliseconds; LetterStream accepts each value once.
    """
    uid = unique_id or str(int(time.time() * 1000))
    material = f"{uid[-6:]}{api_key}{uid[:6]}".encode("utf-8")
    digest = hashlib.md5(base64.b64encode(material)).hexdigest()  # noqa: S324
    return {"a": api_id, "h": digest, "t": uid}


def parse_response(text: str | bytes) -> dict[str, Any]:
    """Parse a LetterStream XML response into a plain dict.

    Returns keys: ``code``, ``details``, ``batch``, ``quantity``, ``cost``,
    ``authcode``, ``docs``, ``messages``. Missing elements are simply absent.
    A body that is not XML comes back as ``{"raw": ...}`` so the caller can
    surface it rather than crash on it.
    """
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            return {"raw": "<binary response>"}

    stripped = text.strip()
    if not stripped.startswith("<"):
        return {"raw": stripped[:2000]}

    try:
        root = ElementTree.fromstring(stripped)
    except ElementTree.ParseError:
        return {"raw": stripped[:2000]}

    result: dict[str, Any] = {"messages": []}
    docs: list[dict[str, Any]] = []

    for message in root.iter("message"):
        entry: dict[str, Any] = {"type": message.get("type")}
        for tag in ("code", "details", "batch", "quantity", "cost", "authcode"):
            element = message.find(tag)
            if element is not None and element.text:
                entry[tag] = element.text.strip()
        message_docs = []
        for doc in message.findall("doc"):
            record: dict[str, Any] = {}
            for tag in ("id", "job", "cost", "tracking"):
                element = doc.find(tag)
                if element is not None and element.text:
                    record[tag] = element.text.strip()
            if record:
                message_docs.append(record)
        if message_docs:
            entry["docs"] = message_docs
            docs.extend(message_docs)
        result["messages"].append(entry)

    for entry in result["messages"]:
        for key in ("code", "details", "batch", "quantity", "authcode"):
            if key in entry and key not in result:
                result[key] = entry[key]
        # -199 messages echo an authorisation acknowledgement rather than a
        # batch price, so their cost is not the batch cost.
        if "cost" in entry and entry.get("code") != CODE_AUTH_OK and "cost" not in result:
            result["cost"] = entry["cost"]

    if docs:
        result["docs"] = docs
    return result


def response_cost(parsed: dict[str, Any]) -> float | None:
    raw = parsed.get("cost")
    if raw is None:
        return None
    try:
        return float(str(raw).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def raise_for_api_error(parsed: dict[str, Any], *, context: str) -> None:
    """Raise :class:`ApiError` unless the response reports a documented success."""
    if "raw" in parsed and "messages" not in parsed:
        raise ApiError(
            f"{context}: LetterStream returned an unrecognised body: {parsed['raw'][:400]}"
        )
    code = parsed.get("code")
    if code is None:
        raise ApiError(f"{context}: LetterStream returned no response code.")
    if code in SUCCESS_CODES:
        return
    details = parsed.get("details", "no detail supplied")
    if code == CODE_INSUFFICIENT_FUNDS:
        raise ApiError(
            f"{context}: LetterStream reports insufficient account funding. {details}",
            code=code,
        )
    raise ApiError(f"{context}: LetterStream returned code {code}. {details}", code=code)


class LetterStreamClient:
    """Stateless wrapper over a :class:`~letterstream_mcp.transport.Transport`."""

    def __init__(self, config: Config, transport: Transport) -> None:
        self.config = config
        self.transport = transport

    def _auth(self) -> dict[str, str]:
        return build_auth_fields(self.config.api_id, self.config.api_key)

    def _base_fields(self) -> dict[str, Any]:
        fields = self._auth()
        fields["debug"] = "3"
        return fields

    def submit_preauth(self, request: SubmitRequest) -> dict[str, Any]:
        """Create a held job. Returns the parsed response.

        This method does not release mail and has no parameter that could. The
        ``preauth`` flag is applied by the transport, not by this method, so it
        cannot be omitted here either.
        """
        fields = self._base_fields()
        fields.update(
            {
                "job": request.job_name,
                "from": request.sender.as_sender_string(),
                "to[]": [r.as_wire_string() for r in request.recipients],
                "pages": str(request.pages),
                "mailtype": request.mail_type,
                "coversheet": request.coversheet,
                "duplex": request.duplex,
                "ink": request.ink,
                "paper": request.paper,
                "returnenv": request.return_envelope,
            }
        )
        document = Path(request.document_path)
        body = document.read_bytes()
        raw = self.transport.submit_preauth(
            fields, document_name=document.name, document_bytes=body
        )
        parsed = parse_response(raw)
        raise_for_api_error(parsed, context=f"Submitting job {request.job_name}")
        return parsed

    def release(self, authcode: str) -> dict[str, Any]:
        """Release a held job into production. This is the mailing call."""
        fields = self._base_fields()
        fields[RELEASE_FIELD] = authcode
        parsed = parse_response(self.transport.release(fields))
        raise_for_api_error(parsed, context="Authorising a held job")
        return parsed

    def account_status(self) -> dict[str, Any]:
        fields = self._base_fields()
        fields["accountstatus"] = "1"
        return parse_response(self.transport.query(fields))

    def job_status(self, job_name: str) -> dict[str, Any]:
        fields = self._base_fields()
        fields["jobstatus"] = job_name
        return parse_response(self.transport.query(fields))

    def tracking(self, doc_id: str) -> dict[str, Any]:
        fields = self._base_fields()
        fields["doc_id"] = str(doc_id)
        fields["getinfo"] = "trackx"
        fields["responseformat"] = "json"
        body = self.transport.query(fields)
        try:
            import json

            return json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001 - fall back to the XML/text parser
            return parse_response(body)

    def document_proof_pdf(self, doc_id: str) -> bytes:
        """Fetch the print proof PDF for one recipient copy.

        LetterStream may return the PDF directly or base64-encoded; both are
        handled. Raises :class:`TransportError` if neither yields a PDF.
        """
        fields = self._base_fields()
        fields["doc_id"] = str(doc_id)
        fields["getinfo"] = "proof"
        body = self.transport.query(fields)
        if body[:4] == b"%PDF":
            return body
        try:
            decoded = base64.b64decode(body, validate=False)
        except Exception:  # noqa: BLE001
            decoded = b""
        if decoded[:4] == b"%PDF":
            return decoded
        raise TransportError(
            f"Proof request for doc_id {doc_id} did not return a PDF "
            f"({len(body)} bytes received)."
        )
