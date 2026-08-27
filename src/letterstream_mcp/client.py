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
import json
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


ERROR_MESSAGE_TYPE = "error"


def _is_error_type(raw: Any) -> bool:
    """Normalised test for an error message type.

    Shared by the JSON and XML paths deliberately. They read the type from
    different places — ``@attributes.type`` in JSON, a promoted top-level
    ``type`` in the XML parser's output — but must agree on what counts as an
    error. When these were two separate comparisons they drifted: one
    normalised case and whitespace and the other did not, so ``type="Error"``
    passed as a success on one path and failed on the other.
    """
    return str(raw or "").strip().lower() == ERROR_MESSAGE_TYPE


def _raise_for_error_entries(errors: list[dict[str, Any]], *, context: str) -> None:
    """Raise :class:`ApiError` describing every error entry found."""
    if not errors:
        return

    def render(entry: dict[str, Any]) -> str:
        # `is None` rather than falsiness on both halves: 0 and False are values
        # LetterStream could legitimately send, and reporting either of them as
        # "no detail supplied" would be a false statement in an error message.
        code = entry.get("code")
        code_text = "?" if code is None else str(code)
        raw_details = entry.get("details")
        details = ("" if raw_details is None else str(raw_details)).strip()
        return f"code {code_text}: {details or 'no detail supplied'}"

    first = errors[0].get("code")
    raise ApiError(
        f"{context}: LetterStream returned an error. "
        + "; ".join(render(entry) for entry in errors),
        code=str(first) if first is not None else None,
    )


def raise_for_json_api_error(payload: Any, *, context: str) -> None:
    """Raise :class:`ApiError` unless a JSON response is a recognised success.

    LetterStream's JSON responses wrap a list under ``message``; an error is an
    entry whose ``@attributes.type`` is ``error``. That shape has no top-level
    ``code``, so :func:`raise_for_api_error` cannot read it — a successful
    lookup and a failed one are both a 200 with a JSON body, and the difference
    is only visible inside the list. A response may carry an ``info`` entry
    (typically ``AUTHOK``) alongside errors, so one success entry does not mean
    the request succeeded.

    This recognises exactly one shape, the one observed live. Unknown entry
    *types* pass (``info``, ``docstatus``, and any benign type LetterStream
    adds later); malformed entries and unreadable envelopes are refused: reporting success over a body we cannot
    read is the failure this function exists to prevent, and these lookups are
    read-only, so a false failure costs a retry while a false success misleads
    the caller.
    """
    if not isinstance(payload, dict):
        raise ApiError(
            f"{context}: LetterStream returned a JSON body that is not an "
            f"object: {str(payload)[:200]}"
        )
    messages = payload.get("message")
    if isinstance(messages, dict):
        messages = [messages]
    if not isinstance(messages, list) or not messages:
        raise ApiError(
            f"{context}: LetterStream returned a JSON object with no readable "
            f"'message' list; cannot tell success from failure. "
            f"Keys: {sorted(payload)[:10]}"
        )
    errors = []
    for entry in messages:
        if not isinstance(entry, dict):
            raise ApiError(
                f"{context}: LetterStream returned a message entry that is not "
                f"an object: {str(entry)[:200]}"
            )
        attrs = entry.get("@attributes")
        if attrs is not None and not isinstance(attrs, dict):
            raise ApiError(
                f"{context}: LetterStream returned a message entry whose "
                f"'@attributes' is not an object: {str(attrs)[:200]}"
            )
        raw_type = (attrs or {}).get("type")
        if raw_type is not None and not isinstance(raw_type, str):
            raise ApiError(
                f"{context}: LetterStream returned a message entry whose "
                f"'type' is not a string: {str(raw_type)[:200]}"
            )
        if _is_error_type(raw_type):
            errors.append(entry)
    _raise_for_error_entries(errors, context=context)


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
        parsed = parse_response(self.transport.query(fields))
        context = "Fetching account status"
        raise_for_api_error(parsed, context=context)
        # raise_for_api_error reads only the code promoted to the top level,
        # which a leading AUTHOK fills in. Scan every message too, so a trailing
        # error cannot hide behind it — the same masking shape the tracking
        # path guards against.
        _raise_for_error_entries(
            [
                e
                for e in parsed.get("messages", [])
                if isinstance(e, dict) and _is_error_type(e.get("type"))
            ],
            context=context,
        )
        return parsed

    def job_status(self, job_name: str) -> dict[str, Any]:
        """Fetch a job's status. Deliberately does NOT raise on an error code.

        :func:`~letterstream_mcp.gate.interpret_job_status` reads non-success
        codes to distinguish "no such job" from "job exists", and its caller
        turns any exception into a refusal — so raising here would convert a
        legitimate resubmission-after-failure into a blocked one. The caller
        interprets the response instead.
        """
        fields = self._base_fields()
        fields["jobstatus"] = job_name
        return parse_response(self.transport.query(fields))

    def tracking(self, doc_id: str) -> dict[str, Any]:
        fields = self._base_fields()
        fields["doc_id"] = str(doc_id)
        fields["getinfo"] = "trackx"
        fields["responseformat"] = "json"
        body = self.transport.query(fields)
        context = f"Fetching tracking for doc_id {doc_id}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001 - fall back to the XML/text parser
            parsed = parse_response(body)
            # SUCCESS_CODES holds the submit/release codes and this endpoint's
            # XML success codes have never been observed, so raise_for_api_error
            # would reject valid lookups — and it reads only the promoted
            # top-level code, which a leading AUTHOK would fill in while trailing
            # error messages went unread. That is the live failure shape. So the
            # test here is literally the JSON guard's predicate, shared via
            # _is_error_type so the two paths cannot drift apart.
            if "raw" in parsed and "messages" not in parsed:
                raise ApiError(
                    f"{context}: LetterStream returned an unrecognised body: "
                    f"{parsed['raw'][:400]}"
                )
            entries = parsed.get("messages")
            if not isinstance(entries, list) or not entries:
                raise ApiError(
                    f"{context}: LetterStream returned no readable messages; "
                    "cannot tell success from failure."
                )
            errors = [
                e for e in entries if isinstance(e, dict) and _is_error_type(e.get("type"))
            ]
            _raise_for_error_entries(errors, context=context)
            return parsed
        raise_for_json_api_error(payload, context=context)
        return payload

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
