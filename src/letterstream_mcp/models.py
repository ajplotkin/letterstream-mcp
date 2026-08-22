"""Value types for addresses, submissions, and proofs.

The address wire format LetterStream expects is a delimited string. Building it
by string concatenation at the call site is how a stray colon in a company name
silently shifts every field one position to the left and mails a letter to the
wrong place, so it is built here, once, with validation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import ValidationError

#: LetterStream accepts ':' or '|' as the address field delimiter, but forbids
#: mixing them. We always emit ':' and reject either character in field values.
_FORBIDDEN_IN_FIELD = (":", "|")

#: Job names must be unique across a LetterStream account's active and mailed
#: jobs, and are restricted to a conservative alphanumeric-plus-separator set.
_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{8,20}$")

#: Document ids identify one recipient copy. LetterStream documents these as
#: alphanumeric, max 20 characters, unique across active and mailed jobs.
_DOC_ID_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")


def _clean(value: str | None, *, field_name: str, required: bool) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise ValidationError(f"{field_name} is required and was empty.")
    for bad in _FORBIDDEN_IN_FIELD:
        if bad in text:
            raise ValidationError(
                f"{field_name} may not contain {bad!r}; it is the address field "
                f"delimiter. Got: {text!r}"
            )
    return text


@dataclass(frozen=True)
class Address:
    """A postal address in the shape LetterStream's address string expects."""

    name_1: str
    address_1: str
    city: str
    state: str
    zip_code: str
    name_2: str = ""
    address_2: str = ""

    def _fields(self) -> list[str]:
        return [
            _clean(self.name_1, field_name="name_1", required=True),
            _clean(self.name_2, field_name="name_2", required=False),
            _clean(self.address_1, field_name="address_1", required=True),
            _clean(self.address_2, field_name="address_2", required=False),
            _clean(self.city, field_name="city", required=True),
            _clean(self.state, field_name="state", required=True),
            _clean(self.zip_code, field_name="zip_code", required=True),
        ]

    def as_sender_string(self) -> str:
        """Seven colon-delimited fields, no document id. Used for ``from``."""
        return ":".join(self._fields())

    def as_recipient_string(self, doc_id: str) -> str:
        """Eight colon-delimited fields, document id first. Used for ``to[]``."""
        if not _DOC_ID_RE.match(doc_id or ""):
            raise ValidationError(
                f"doc_id must be 1-20 alphanumeric characters (got {doc_id!r})."
            )
        return ":".join([doc_id, *self._fields()])

    def summary(self) -> str:
        """One-line human summary, for previews and proof records."""
        parts = [self.name_1]
        if self.name_2.strip():
            parts.append(self.name_2.strip())
        parts.append(self.address_1)
        if self.address_2.strip():
            parts.append(self.address_2.strip())
        parts.append(f"{self.city}, {self.state} {self.zip_code}")
        return " / ".join(p.strip() for p in parts if p and p.strip())


@dataclass(frozen=True)
class Recipient:
    """One addressed copy of the document."""

    doc_id: str
    address: Address

    def as_wire_string(self) -> str:
        return self.address.as_recipient_string(self.doc_id)


@dataclass(frozen=True)
class SubmitRequest:
    """Everything needed to create a held, pre-authorised job.

    There is deliberately no field on this type that could request immediate
    mailing. The gate has no parameter for it either; releasing mail is a
    separate call against a stored proof. See :mod:`letterstream_mcp.gate`.
    """

    job_name: str
    document_path: Path
    pages: int
    sender: Address
    recipients: tuple[Recipient, ...]
    mail_type: str = "firstclass"
    coversheet: str = "Y"
    duplex: str = "N"
    ink: str = "B"
    return_envelope: str = "N"
    paper: str = ""
    #: Caller-supplied idempotency key. When None, the gate derives a
    #: deterministic one from the request contents and document hash.
    idempotency_key: str | None = None

    def validate(self) -> None:
        """Raise :class:`ValidationError` if this request cannot be submitted.

        Checked entirely locally. Nothing here contacts LetterStream, so a
        request that fails validation has made no network call.
        """
        if not _JOB_NAME_RE.match(self.job_name or ""):
            raise ValidationError(
                "job_name must be 8-20 characters of letters, digits, '-' or '_' "
                f"(got {self.job_name!r}). LetterStream requires it to be unique "
                "across your active and mailed jobs."
            )
        if self.pages < 1:
            raise ValidationError(f"pages must be at least 1 (got {self.pages}).")
        if not self.recipients:
            raise ValidationError("At least one recipient is required.")

        seen: set[str] = set()
        for recipient in self.recipients:
            wire = recipient.as_wire_string()  # validates field contents
            if len(wire.split(":")) != 8:
                raise ValidationError(
                    f"Recipient {recipient.doc_id} did not produce 8 address "
                    "fields; this is a bug in address construction."
                )
            if recipient.doc_id in seen:
                raise ValidationError(
                    f"doc_id {recipient.doc_id!r} appears twice. LetterStream "
                    "requires a unique doc_id per recipient copy."
                )
            seen.add(recipient.doc_id)

        if len(self.sender.as_sender_string().split(":")) != 7:
            raise ValidationError(
                "Sender did not produce 7 address fields; this is a bug in "
                "address construction."
            )

        path = Path(self.document_path)
        if not path.is_file():
            raise ValidationError(f"Document not found: {path}")
        head = path.open("rb").read(5)
        if head[:4] != b"%PDF":
            raise ValidationError(
                f"{path} does not start with a %PDF header. LetterStream mails "
                "PDFs; sending anything else would print garbage."
            )

    def document_sha256(self) -> str:
        """SHA-256 of the exact bytes that would be uploaded."""
        return sha256_file(self.document_path)

    def fingerprint(self) -> str:
        """A stable hash of everything that determines what gets mailed.

        Two requests with the same fingerprint would produce the same physical
        mail. Used as the default idempotency key, so a naive retry of an
        identical request collides with the original rather than creating a
        second job.
        """
        payload = {
            "job_name": self.job_name,
            "pages": self.pages,
            "mail_type": self.mail_type,
            "coversheet": self.coversheet,
            "duplex": self.duplex,
            "ink": self.ink,
            "return_envelope": self.return_envelope,
            "paper": self.paper,
            "sender": self.sender.as_sender_string(),
            "recipients": [r.as_wire_string() for r in self.recipients],
            "document_sha256": self.document_sha256(),
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def effective_idempotency_key(self) -> str:
        return self.idempotency_key or self.fingerprint()

    def preview(self) -> dict[str, Any]:
        """A human-reviewable description. Contains no credentials."""
        return {
            "job_name": self.job_name,
            "document": str(self.document_path),
            "document_sha256": self.document_sha256(),
            "pages": self.pages,
            "mail_type": self.mail_type,
            "coversheet": self.coversheet,
            "duplex": self.duplex,
            "ink": self.ink,
            "return_envelope": self.return_envelope,
            "sender": self.sender.summary(),
            "recipients": [
                {"doc_id": r.doc_id, "address": r.address.summary()}
                for r in self.recipients
            ],
            "recipient_count": len(self.recipients),
        }


@dataclass
class Proof:
    """The record of a held job, and the only thing that can be authorised.

    A proof binds an authcode to ``document_sha256``. :meth:`Proof.verify` is
    what makes it a gate rather than a receipt: if the bytes on disk have
    changed since submission, the authcode is not usable through this package.
    """

    proof_id: str
    job_name: str
    authcode: str
    document_path: str
    document_sha256: str
    pages: int
    recipient_count: int
    cost_usd: float | None
    cost_currency: str
    per_doc: list[dict[str, Any]]
    created_at: float
    idempotency_key: str
    preview: dict[str, Any] = field(default_factory=dict)
    #: Set immediately before a release request goes out, and left set if
    #: the request never reports back. A proof with this set and
    #: ``authorized_at`` unset is in an unknown state; the gate refuses it.
    release_attempted_at: float | None = None
    authorized_at: float | None = None
    authorize_response: dict[str, Any] | None = None
    charged_cost_usd: float | None = None
    batch_id: str | None = None

    @property
    def authorized(self) -> bool:
        return self.authorized_at is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Proof":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def public_view(self) -> dict[str, Any]:
        """What callers see. Excludes the authcode.

        The authcode is the credential that releases mail. Withholding it from
        every return value means an agent holding a submit result cannot
        assemble a release request on its own; it must come back through
        :meth:`letterstream_mcp.gate.MailGate.authorize`, which re-checks the
        document hash.
        """
        data = self.to_dict()
        data.pop("authcode", None)
        data["authorized"] = self.authorized
        return data


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
