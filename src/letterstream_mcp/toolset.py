"""The MCP tool surface, as plain functions returning plain dicts.

Keeping the tool bodies here — rather than inside MCP decorators — means the
test suite exercises the same code an MCP client reaches, without needing the
MCP SDK installed. :mod:`letterstream_mcp.server` is a thin binding over this
module and adds no logic of its own.

Every function returns a dict. Errors this package raises deliberately are
converted to ``{"error": ..., "error_type": ..., "mailed": False}`` rather than
propagating, so an MCP client sees a sentence instead of a traceback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .config import Config, load_config
from .errors import LetterStreamError, ValidationError
from .gate import MailGate
from .models import Address, Recipient, SubmitRequest
from .store import CROSS_PROCESS_LOCKING, Store

#: Names of the tools this package exposes, in the order documented in README.
TOOL_NAMES = (
    "letterstream_check_config",
    "letterstream_account_status",
    "letterstream_submit",
    "letterstream_list_proofs",
    "letterstream_get_proof",
    "letterstream_download_proof_pdfs",
    "letterstream_authorize",
    "letterstream_tracking",
)


def _error(exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "mailed": False,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def guard(func: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Convert deliberate errors into an error dict; let bugs still raise.

    Only :class:`~letterstream_mcp.errors.LetterStreamError` is caught. A
    genuine defect in this package still surfaces as an exception rather than
    being disguised as a polite refusal.
    """

    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            result = func(*args, **kwargs)
        except LetterStreamError as exc:
            return _error(exc)
        result.setdefault("ok", True)
        return result

    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


class ToolSet:
    """Bound set of tools sharing one configuration and one transport.

    ``config`` and ``transport`` are injected so tests can drive the exact tool
    functions an MCP client calls, against a fake transport.
    """

    def __init__(
        self,
        config: Config | None = None,
        transport: Any | None = None,
        store: Store | None = None,
        **config_kwargs: Any,
    ) -> None:
        self.config = config if config is not None else load_config(**config_kwargs)
        self.gate = MailGate(self.config, transport=transport, store=store)

    # ---- tools --------------------------------------------------------

    @guard
    def letterstream_check_config(self) -> dict[str, Any]:
        """Report how this server is configured. Never returns the API key."""
        redacted = self.config.redacted()
        return {
            "configured": True,
            "live": self.config.live,
            "mode": "LIVE (submissions reach LetterStream)"
            if self.config.live
            else "DRY RUN (no request is made to LetterStream)",
            "config": redacted,
            "state_dir": str(self.gate.store.state_dir),
            "cross_process_locking": CROSS_PROCESS_LOCKING,
            "tools": list(TOOL_NAMES),
        }

    @guard
    def letterstream_account_status(self) -> dict[str, Any]:
        """Fetch LetterStream account status, including prepaid balance."""
        return {"account_status": self.gate.account_status()}

    @guard
    def letterstream_submit(
        self,
        *,
        job_name: str,
        document_path: str,
        pages: int,
        sender: dict[str, Any],
        recipients: list[dict[str, Any]],
        mail_type: str | None = None,
        coversheet: str | None = None,
        duplex: str | None = None,
        ink: str | None = None,
        return_envelope: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a held job at LetterStream. Never mails anything.

        Returns a proof id, the quoted cost, and the SHA-256 of the uploaded
        document. Releasing the job requires a separate
        ``letterstream_authorize`` call carrying that proof id and hash.
        """
        request = self._build_request(
            job_name=job_name,
            document_path=document_path,
            pages=pages,
            sender=sender,
            recipients=recipients,
            mail_type=mail_type,
            coversheet=coversheet,
            duplex=duplex,
            ink=ink,
            return_envelope=return_envelope,
            idempotency_key=idempotency_key,
        )
        return self.gate.submit(request)

    @guard
    def letterstream_list_proofs(self) -> dict[str, Any]:
        """List held and authorised jobs recorded in the local ledger."""
        return {"proofs": self.gate.list_proofs()}

    @guard
    def letterstream_get_proof(self, *, proof_id: str) -> dict[str, Any]:
        """Fetch one ledger record, including the hash it is bound to."""
        return {"proof": self.gate.get_proof(proof_id)}

    @guard
    def letterstream_download_proof_pdfs(
        self, *, proof_id: str, out_dir: str
    ) -> dict[str, Any]:
        """Download LetterStream's print proof PDFs for a held job."""
        return {"files": self.gate.download_proof_pdfs(proof_id, Path(out_dir))}

    @guard
    def letterstream_authorize(
        self,
        *,
        proof_id: str,
        document_sha256: str,
        acknowledge_cost_usd: float | None = None,
    ) -> dict[str, Any]:
        """Release a held job into production. This mails, and cannot be undone.

        ``document_sha256`` must be the hash returned by ``letterstream_submit``.
        The document is re-hashed from disk and both must match the proof.
        """
        return self.gate.authorize(
            proof_id,
            document_sha256=document_sha256,
            acknowledge_cost_usd=acknowledge_cost_usd,
        )

    @guard
    def letterstream_tracking(self, *, proof_id: str) -> dict[str, Any]:
        """Fetch USPS tracking for each recipient copy of a submitted job."""
        return {"tracking": self.gate.tracking(proof_id)}

    # ---- helpers ------------------------------------------------------

    def _build_request(
        self,
        *,
        job_name: str,
        document_path: str,
        pages: int,
        sender: dict[str, Any],
        recipients: list[dict[str, Any]],
        mail_type: str | None,
        coversheet: str | None,
        duplex: str | None,
        ink: str | None,
        return_envelope: str | None,
        idempotency_key: str | None,
    ) -> SubmitRequest:
        defaults = self.config.defaults
        if not isinstance(sender, dict):
            raise ValidationError("sender must be an object of address fields.")
        if not isinstance(recipients, list) or not recipients:
            raise ValidationError("recipients must be a non-empty list of addresses.")

        parsed_recipients = []
        for index, entry in enumerate(recipients):
            if not isinstance(entry, dict):
                raise ValidationError(f"recipients[{index}] must be an object.")
            doc_id = entry.get("doc_id")
            if not doc_id:
                raise ValidationError(
                    f"recipients[{index}] needs a doc_id: a unique alphanumeric id "
                    "for this recipient's copy."
                )
            parsed_recipients.append(
                Recipient(doc_id=str(doc_id), address=_address_from(entry, f"recipients[{index}]"))
            )

        return SubmitRequest(
            job_name=job_name,
            document_path=Path(document_path).expanduser(),
            pages=int(pages),
            sender=_address_from(sender, "sender"),
            recipients=tuple(parsed_recipients),
            mail_type=mail_type or defaults.mail_type or "firstclass",
            coversheet=coversheet or defaults.coversheet or "Y",
            duplex=duplex or defaults.duplex or "N",
            ink=ink or defaults.ink or "B",
            return_envelope=return_envelope or defaults.return_envelope or "N",
            idempotency_key=idempotency_key,
        )


def _address_from(data: dict[str, Any], where: str) -> Address:
    required = ("name_1", "address_1", "city", "state", "zip_code")
    missing = [key for key in required if not str(data.get(key) or "").strip()]
    if missing:
        raise ValidationError(f"{where} is missing required field(s): {', '.join(missing)}")
    return Address(
        name_1=str(data["name_1"]),
        name_2=str(data.get("name_2") or ""),
        address_1=str(data["address_1"]),
        address_2=str(data.get("address_2") or ""),
        city=str(data["city"]),
        state=str(data["state"]),
        zip_code=str(data["zip_code"]),
    )
