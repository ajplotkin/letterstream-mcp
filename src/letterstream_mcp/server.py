"""MCP stdio server binding.

This module contains no policy. Each tool below forwards to the identically
named method on :class:`~letterstream_mcp.toolset.ToolSet`, which is what the
test suite exercises.

Run it with::

    python -m letterstream_mcp.server

or point an MCP client at that command. Configuration is read at startup; if
credentials are missing the process exits with a one-line message rather than a
traceback, and no tools are registered.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .config import load_config
from .errors import LetterStreamError
from .toolset import ToolSet


def build_server(toolset: ToolSet):  # pragma: no cover - requires the mcp SDK
    """Wrap a :class:`ToolSet` in a FastMCP server."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "The 'mcp' package is required to run the MCP server.\n"
            "Install it with: pip install 'letterstream-mcp[mcp]'  (or: pip install mcp)"
        ) from exc

    server = FastMCP("letterstream")

    @server.tool()
    def letterstream_check_config() -> dict[str, Any]:
        """Report how this server is configured. Never returns the API key."""
        return toolset.letterstream_check_config()

    @server.tool()
    def letterstream_account_status() -> dict[str, Any]:
        """Authenticated account status check.

        Observed live to return an authentication confirmation only; no balance,
        quota, or funding data was present in the response. Do not build a
        pre-flight funding check on it.
        """
        return toolset.letterstream_account_status()

    @server.tool()
    def letterstream_submit(
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

        Returns a proof_id, the cost LetterStream quoted, and the SHA-256 of
        the uploaded document. Releasing the job requires a separate
        letterstream_authorize call carrying that proof_id and hash.
        """
        return toolset.letterstream_submit(
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

    @server.tool()
    def letterstream_list_proofs() -> dict[str, Any]:
        """List held and authorised jobs recorded in the local ledger."""
        return toolset.letterstream_list_proofs()

    @server.tool()
    def letterstream_get_proof(proof_id: str) -> dict[str, Any]:
        """Fetch one ledger record, including the document hash it binds."""
        return toolset.letterstream_get_proof(proof_id=proof_id)

    @server.tool()
    def letterstream_download_proof_pdfs(proof_id: str, out_dir: str) -> dict[str, Any]:
        """Download LetterStream's print proof PDFs for a held job."""
        return toolset.letterstream_download_proof_pdfs(proof_id=proof_id, out_dir=out_dir)

    @server.tool()
    def letterstream_authorize(
        proof_id: str,
        document_sha256: str,
        acknowledge_cost_usd: float | None = None,
    ) -> dict[str, Any]:
        """Release a held job into production. This mails, and cannot be undone.

        document_sha256 must be the hash returned by letterstream_submit. The
        document is re-hashed from disk and both must match the proof.
        """
        return toolset.letterstream_authorize(
            proof_id=proof_id,
            document_sha256=document_sha256,
            acknowledge_cost_usd=acknowledge_cost_usd,
        )

    @server.tool()
    def letterstream_tracking(proof_id: str) -> dict[str, Any]:
        """Fetch LetterStream's tracking record for each recipient copy.

        Returns LetterStream's per-document status and history. Observed live
        against a held job, where it reports document status rather than USPS
        scan events; whether the history carries USPS scans once a piece is
        released and delivered has not been verified.
        """
        return toolset.letterstream_tracking(proof_id=proof_id)

    return server


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - process entry
    argv = sys.argv[1:] if argv is None else argv
    config_path = None
    if "--config" in argv:
        config_path = argv[argv.index("--config") + 1]
    try:
        config = load_config(config_path=config_path)
    except LetterStreamError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    toolset = ToolSet(config=config)
    banner = {
        "server": "letterstream-mcp",
        "mode": "live" if config.live else "dry-run",
        "state_dir": str(toolset.gate.store.state_dir),
    }
    print(json.dumps(banner), file=sys.stderr)
    build_server(toolset).run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
