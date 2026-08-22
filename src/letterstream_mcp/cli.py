"""Command line interface. Flags override the environment, which overrides the file.

Subcommands mirror the MCP tools one for one, so anything an agent can do
through MCP can be reproduced and inspected by hand.

Deliberately absent: any flag that turns live mode on. ``--live`` does not
exist. Live sending is enabled in ``config.toml`` or via ``LETTERSTREAM_LIVE``,
where it is a decision the operator records rather than one they type by
accident. ``check-config`` prints which mode is active.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .errors import GateRefusal, LetterStreamError
from .toolset import ToolSet

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_CONFIG = 2
EXIT_ERROR = 3


def _address_from_json(raw: str, label: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} must be valid JSON: {exc}")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="letterstream-mcp",
        description=(
            "LetterStream certified-mail client with a submit/authorize gate. "
            "Nothing mails on a single command."
        ),
    )
    parser.add_argument("--config", help="Path to config.toml.")
    parser.add_argument("--api-id", help="Override the configured API identifier.")
    parser.add_argument(
        "--api-key",
        help=(
            "Override the configured API key. Prefer LETTERSTREAM_API_KEY; a key "
            "typed here lands in your shell history."
        ),
    )
    parser.add_argument("--base-url", help="Override the API base URL.")
    parser.add_argument("--state-dir", help="Override the proof/idempotency ledger directory.")
    parser.add_argument("--timeout", type=float, help="Override the HTTP timeout, in seconds.")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-config", help="Show resolved configuration. Never prints the key.")
    sub.add_parser("account", help="Show LetterStream account status and balance.")

    submit = sub.add_parser(
        "submit", help="Create a held job. Never mails; produces a proof to review."
    )
    submit.add_argument("--job-name", required=True)
    submit.add_argument("--document", required=True, help="Path to the PDF to mail.")
    submit.add_argument("--pages", type=int, required=True)
    submit.add_argument("--sender", required=True, help="JSON object of sender address fields.")
    submit.add_argument(
        "--recipient",
        action="append",
        required=True,
        metavar="JSON",
        help="JSON object with doc_id plus address fields. Repeat for multiple copies.",
    )
    submit.add_argument("--mail-type")
    submit.add_argument("--coversheet")
    submit.add_argument("--duplex")
    submit.add_argument("--ink")
    submit.add_argument("--return-envelope")
    submit.add_argument("--idempotency-key")

    sub.add_parser("list-proofs", help="List held and authorised jobs from the ledger.")

    show = sub.add_parser("show-proof", help="Show one ledger record.")
    show.add_argument("proof_id")

    proofs = sub.add_parser("download-proofs", help="Download LetterStream print proofs.")
    proofs.add_argument("proof_id")
    proofs.add_argument("--out-dir", default="proofs")

    authorize = sub.add_parser(
        "authorize", help="Release a held job. This mails and cannot be undone."
    )
    authorize.add_argument("proof_id")
    authorize.add_argument(
        "--document-sha256",
        required=True,
        help="The hash reported by submit. Re-checked against the file on disk.",
    )
    authorize.add_argument(
        "--acknowledge-cost",
        type=float,
        help="The dollar amount you expect to be charged. Refused if it disagrees.",
    )

    tracking = sub.add_parser("tracking", help="Fetch USPS tracking for a submitted job.")
    tracking.add_argument("proof_id")

    return parser


def _dispatch(toolset: ToolSet, args: argparse.Namespace) -> dict[str, Any]:
    command = args.command
    if command == "check-config":
        return toolset.letterstream_check_config()
    if command == "account":
        return toolset.letterstream_account_status()
    if command == "submit":
        return toolset.letterstream_submit(
            job_name=args.job_name,
            document_path=args.document,
            pages=args.pages,
            sender=_address_from_json(args.sender, "--sender"),
            recipients=[_address_from_json(r, "--recipient") for r in args.recipient],
            mail_type=args.mail_type,
            coversheet=args.coversheet,
            duplex=args.duplex,
            ink=args.ink,
            return_envelope=args.return_envelope,
            idempotency_key=args.idempotency_key,
        )
    if command == "list-proofs":
        return toolset.letterstream_list_proofs()
    if command == "show-proof":
        return toolset.letterstream_get_proof(proof_id=args.proof_id)
    if command == "download-proofs":
        return toolset.letterstream_download_proof_pdfs(
            proof_id=args.proof_id, out_dir=args.out_dir
        )
    if command == "authorize":
        return toolset.letterstream_authorize(
            proof_id=args.proof_id,
            document_sha256=args.document_sha256,
            acknowledge_cost_usd=args.acknowledge_cost,
        )
    if command == "tracking":
        return toolset.letterstream_tracking(proof_id=args.proof_id)
    raise SystemExit(f"Unknown command: {command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(
            config_path=args.config,
            api_id=args.api_id,
            api_key=args.api_key,
            base_url=args.base_url,
            timeout_seconds=args.timeout,
            state_dir=Path(args.state_dir) if args.state_dir else None,
        )
    except LetterStreamError as exc:
        # A clear sentence, not a traceback.
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG

    toolset = ToolSet(config=config)
    result = _dispatch(toolset, args)
    print(json.dumps(result, indent=2, default=str))

    if result.get("ok") is False:
        return EXIT_REFUSED if _is_refusal(result) else EXIT_ERROR
    return EXIT_OK


def _is_refusal(result: dict[str, Any]) -> bool:
    name = result.get("error_type", "")
    refusals = {cls.__name__ for cls in _all_subclasses(GateRefusal)} | {GateRefusal.__name__}
    return name in refusals


def _all_subclasses(cls: type) -> set[type]:
    found = set(cls.__subclasses__())
    for sub in list(found):
        found |= _all_subclasses(sub)
    return found


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
