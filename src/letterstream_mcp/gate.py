"""The safety gate: the reason this project exists.

Mailing a certified letter is irreversible, costs money, and can have legal
effect. So this package does not expose a ``send()``. It exposes two calls that
cannot be collapsed into one:

``submit``
    Uploads the document and creates a job that LetterStream holds. Returns a
    *proof*: an identifier, the cost LetterStream quoted, a per-recipient
    breakdown, and the SHA-256 of the exact bytes uploaded. It does not return
    the authcode.

``authorize``
    Takes a proof id and the caller's assertion of the document hash, re-reads
    the document from disk, and releases the held job only if everything still
    matches.

What "no path from submit to mail" means concretely, and where it stops:

* :meth:`MailGate.submit` calls ``transport.submit_preauth`` and, on a retry,
  ``transport.query``. It never calls ``transport.release``. ``release`` is the
  only transport method that sends the field LetterStream uses to put a job
  into production.
* ``submit_preauth`` sets ``preauth=1`` itself, overwriting anything the caller
  passed, so a submission through this package is always a held job.
* The authcode never leaves the ledger. :meth:`Proof.public_view` strips it and
  no tool returns it, so a caller holding a submit result cannot assemble a
  release request without going back through :meth:`MailGate.authorize`.

Both of those calls are also *serialised per key*, which matters because this
package ships inside an MCP server: FastMCP dispatches synchronous tools on a
worker-thread pool, so two overlapping tool calls run this code concurrently.
"Authorising twice mails once" has to survive two callers arriving at the same
instant, not just one caller calling twice — a check-then-set with no lock
between the check and the set lets both callers decide the job has not been
released and both release it.

So :meth:`MailGate.authorize` holds :meth:`Store.exclusive` on the proof id for
the whole claim-release-record sequence, and :meth:`MailGate.submit` holds it on
the idempotency key. A caller that cannot obtain the lock in time raises
:class:`~letterstream_mcp.errors.LockTimeout` and mails nothing; it never falls
through and never steals the lock. See :mod:`letterstream_mcp.store` for what
the lock is and what happens to it when a process dies mid-release.

This holds for code that goes through :class:`MailGate`. It is not a sandbox:
anything that imports :class:`~letterstream_mcp.client.LetterStreamClient` (or
reads the ledger file) can call ``release`` directly. The guarantee is about
the tool surface this package exposes, not about the process it runs in.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .client import SUCCESS_CODES, LetterStreamClient, response_cost
from .config import Config
from .errors import (
    AmbiguousRelease,
    CostCeilingExceeded,
    DryRunRefusal,
    OrphanedJob,
    ProofExpired,
    ProofMismatch,
    TransportError,
    UnknownProof,
)
from .models import Proof, SubmitRequest, sha256_file
from .store import STATE_IN_FLIGHT, Store
from .transport import Transport

#: Added to the HTTP timeout to get how long a caller waits for a contended
#: lock. The holder may be mid-request, so the wait has to outlast one request;
#: the margin covers the ledger writes on either side of it.
LOCK_TIMEOUT_MARGIN_SECONDS = 60.0


def proof_lock_name(proof_id: str) -> str:
    """Name of the lock that serialises releases of one proof.

    A function rather than an inline f-string so a test can take the same lock
    the gate takes, instead of a name that merely looks like it.
    """
    return f"proof::{proof_id}"


def submit_lock_name(idempotency_key: str) -> str:
    """Name of the lock that serialises submissions for one idempotency key."""
    return f"submit::{idempotency_key}"

#: Outcomes of asking LetterStream whether a job already exists.
JOB_EXISTS = "exists"
JOB_ABSENT = "absent"
JOB_UNKNOWN = "unknown"

_ABSENT_PHRASES = ("not found", "no record", "no such job", "unknown job", "no results")


def interpret_job_status(parsed: dict[str, Any], job_name: str) -> str:
    """Decide whether ``job_name`` already exists at LetterStream.

    Returns :data:`JOB_EXISTS`, :data:`JOB_ABSENT`, or :data:`JOB_UNKNOWN`.

    Only :data:`JOB_ABSENT` permits a resubmission. Both "exists" and "unknown"
    block it, so an unreadable or unexpected status response can never lead to
    a second job being created.

    Unverified: this mapping is derived from the documented response shape and
    is exercised against the fake transport in the test suite. It has not been
    checked against a live LetterStream account by this project.
    """
    blob = json.dumps(parsed, default=str).lower()
    if job_name.lower() in blob:
        return JOB_EXISTS
    details = str(parsed.get("details") or "").lower()
    code = parsed.get("code")
    if code is not None and code not in SUCCESS_CODES:
        if any(phrase in details for phrase in _ABSENT_PHRASES):
            return JOB_ABSENT
    if code in SUCCESS_CODES and not parsed.get("docs") and details:
        if any(phrase in details for phrase in _ABSENT_PHRASES):
            return JOB_ABSENT
    return JOB_UNKNOWN


class MailGate:
    """Policy layer over :class:`~letterstream_mcp.client.LetterStreamClient`."""

    def __init__(
        self,
        config: Config,
        transport: Transport | None = None,
        store: Store | None = None,
    ) -> None:
        self.config = config
        self.store = store or Store(config.state_dir)
        if transport is None:
            from .transport import HttpTransport

            transport = HttpTransport(config.base_url, config.timeout_seconds)
        self.transport = transport
        self.client = LetterStreamClient(config, transport)
        #: How long submit/authorize wait for this key's lock before refusing.
        self.lock_timeout_seconds = config.timeout_seconds + LOCK_TIMEOUT_MARGIN_SECONDS

    # ---- submit -------------------------------------------------------

    def submit(self, request: SubmitRequest) -> dict[str, Any]:
        """Validate, then create a held job. Never mails.

        Returns a dict with ``dry_run``, ``preview``, and — in live mode — a
        ``proof`` (see :meth:`Proof.public_view`) plus ``cost_usd``. The
        authcode is not included.

        Raises:
            ValidationError: the request is unmailable; nothing was sent.
            OrphanedJob: a previous attempt with this idempotency key may have
                been accepted and cannot be reconciled. Nothing was resubmitted.
            LockTimeout: another caller holds this idempotency key's lock and
                did not finish in time. Nothing was submitted.
        """
        request.validate()
        document_hash = request.document_sha256()
        key = request.effective_idempotency_key()
        preview = request.preview()

        if not self.config.live:
            # Dry run stops here. No transport method of any kind is called,
            # and no proof is written, so a later switch to live mode cannot
            # find a proof that was never actually held at LetterStream.
            return {
                "dry_run": True,
                "live": False,
                "mailed": False,
                "proof": None,
                "proof_id": None,
                "idempotency_key": key,
                "document_sha256": document_hash,
                "cost_usd": None,
                "cost_note": (
                    "No cost is available in dry run. LetterStream quotes the "
                    "price when a job is held, and no request was made."
                ),
                "preview": preview,
                "note": (
                    "Dry run: nothing was sent to LetterStream. Set "
                    "[safety] live = true in config.toml, or LETTERSTREAM_LIVE=true, "
                    "to create a held job."
                ),
            }

        # Everything below is a check-then-act on state shared with every other
        # caller: "has this key already been submitted?" and then "create a job
        # for it". Two callers interleaving between those two steps is exactly
        # how one letter becomes two held jobs, so the sequence is serialised on
        # the idempotency key rather than merely written atomically.
        with self.store.exclusive(
            submit_lock_name(key), timeout=self.lock_timeout_seconds
        ):
            return self._submit_locked(request, key, document_hash, preview)

    def _submit_locked(
        self,
        request: SubmitRequest,
        key: str,
        document_hash: str,
        preview: dict[str, Any],
    ) -> dict[str, Any]:
        """The body of :meth:`submit`, run with this key's lock held.

        Callers must be holding ``store.exclusive(submit_lock_name(key))``. Nothing in
        here re-checks the key's state without that lock, and the transport call
        happens inside it, so a second caller for the same key waits and then
        finds the proof the first one created rather than creating its own.
        """
        existing_proof = self.store.find_proof_by_idempotency_key(key)
        if existing_proof is not None:
            return {
                "dry_run": False,
                "live": True,
                "mailed": False,
                "reused_existing_submission": True,
                "proof": existing_proof.public_view(),
                "proof_id": existing_proof.proof_id,
                "idempotency_key": key,
                "document_sha256": existing_proof.document_sha256,
                "cost_usd": existing_proof.cost_usd,
                "preview": existing_proof.preview or preview,
                "note": (
                    "This idempotency key was already submitted. Returning the "
                    "existing held job; no second job was created."
                ),
            }

        prior = self.store.get_inflight(key)
        if prior is not None and prior.state == STATE_IN_FLIGHT and prior.attempts > 0:
            self._refuse_or_clear_orphan(prior, request)

        record = self.store.begin_attempt(
            idempotency_key=key,
            job_name=request.job_name,
            document_sha256=document_hash,
        )
        try:
            parsed = self.client.submit_preauth(request)
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            self.store.fail_attempt(record, error=f"{type(exc).__name__}: {exc}")
            raise

        authcode = parsed.get("authcode")
        if not authcode:
            self.store.fail_attempt(
                record, error="LetterStream returned no authcode for a preauth submission"
            )
            raise TransportError(
                f"Job {request.job_name} was submitted but LetterStream returned no "
                "authcode. The job may exist and be held. Check your LetterStream "
                "dashboard before submitting again."
            )

        cost = response_cost(parsed)
        proof = Proof(
            proof_id=f"prf_{uuid.uuid4().hex[:16]}",
            job_name=request.job_name,
            authcode=str(authcode),
            document_path=str(Path(request.document_path).resolve()),
            document_sha256=document_hash,
            pages=request.pages,
            recipient_count=len(request.recipients),
            cost_usd=cost,
            cost_currency="USD",
            per_doc=list(parsed.get("docs") or []),
            created_at=time.time(),
            idempotency_key=key,
            preview=preview,
            batch_id=parsed.get("batch"),
        )
        self.store.put_proof(proof)
        self.store.complete_attempt(record, proof_id=proof.proof_id)

        return {
            "dry_run": False,
            "live": True,
            "mailed": False,
            "reused_existing_submission": False,
            "proof": proof.public_view(),
            "proof_id": proof.proof_id,
            "idempotency_key": key,
            "document_sha256": document_hash,
            "cost_usd": cost,
            "cost_note": (
                "Quoted by LetterStream for the held job. This is what "
                "authorize will charge."
            ),
            "preview": preview,
            "response_code": parsed.get("code"),
            "note": (
                "Job is held at LetterStream and has not been mailed. Review the "
                "proof, then call authorize with this proof_id and "
                "document_sha256 to release it."
            ),
        }

    def _refuse_or_clear_orphan(self, prior, request: SubmitRequest) -> None:
        """Reconcile a previous attempt that never reported success.

        Asks LetterStream whether the job exists. Resubmits only on an
        unambiguous "absent"; refuses on "exists" and on "unknown".
        """
        try:
            status = self.client.job_status(request.job_name)
        except Exception as exc:  # noqa: BLE001
            raise OrphanedJob(
                f"A previous submission of job {request.job_name!r} did not complete "
                f"(last error: {prior.last_error}). Checking its status also failed "
                f"({exc}). Refusing to resubmit, because that is the only way this "
                "could produce two jobs for one letter. Check your LetterStream "
                "dashboard for this job name and resolve it there."
            ) from exc

        verdict = interpret_job_status(status, request.job_name)
        if verdict == JOB_ABSENT:
            prior.notes.append(
                f"reconciled: LetterStream reports job {request.job_name} absent; "
                "resubmitting"
            )
            self.store.put_inflight(prior)
            return

        reason = (
            "LetterStream reports this job already exists"
            if verdict == JOB_EXISTS
            else "LetterStream's status response could not be interpreted"
        )
        raise OrphanedJob(
            f"A previous submission of job {request.job_name!r} did not report back "
            f"(last error: {prior.last_error}). {reason}, so it may already be held "
            "at LetterStream with an authcode this tool never received.\n"
            "Refusing to resubmit; a second submission is the only way one letter "
            "could become two.\n"
            "To resolve: find this job in your LetterStream dashboard. If it is "
            "held, authorise or cancel it there. If it is genuinely absent, submit "
            "again under a different job_name."
        )

    # ---- authorize ----------------------------------------------------

    def authorize(
        self,
        proof_id: str,
        *,
        document_sha256: str,
        acknowledge_cost_usd: float | None = None,
    ) -> dict[str, Any]:
        """Release a held job. This is the only method that causes mail.

        ``document_sha256`` must be the hash the caller saw at submit time. The
        document is then re-read from disk and hashed again; both must match the
        proof. Any mismatch refuses without calling the transport.

        Raises:
            DryRunRefusal: live mode is off.
            UnknownProof: no such proof in the ledger.
            ProofExpired: the proof is older than the configured TTL.
            ProofMismatch: the caller's hash, or the file on disk, disagrees
                with what the proof approved.
            CostCeilingExceeded: the quoted cost exceeds the configured ceiling.
            AmbiguousRelease: a release for this proof was started and never
                confirmed.
            LockTimeout: another caller holds this proof's lock and did not
                finish in time. Nothing was released.
        """
        if not self.config.live:
            raise DryRunRefusal(
                "Live mode is off, so nothing can be authorised or mailed. "
                "Set [safety] live = true in config.toml, or LETTERSTREAM_LIVE=true. "
                "Note that turning live mode on does not mail anything by itself; "
                "you would still need to submit and then authorize."
            )

        # Cheap reject for an id that was never in the ledger, so a bogus id
        # does not create a lock file. The authoritative read is the one below,
        # taken with the lock held.
        if self.store.get_proof(proof_id) is None:
            raise self._unknown_proof(proof_id)

        # From here to the recorded outcome is one indivisible sequence. Reading
        # "not released yet" and acting on it are only safe together: a second
        # caller that reads between them reads a stale answer and mails again.
        with self.store.exclusive(
            proof_lock_name(proof_id), timeout=self.lock_timeout_seconds
        ):
            return self._authorize_locked(
                proof_id,
                document_sha256=document_sha256,
                acknowledge_cost_usd=acknowledge_cost_usd,
            )

    def _unknown_proof(self, proof_id: str) -> UnknownProof:
        return UnknownProof(
            f"No proof {proof_id!r} in {self.store.proofs_path}. Proofs are "
            "created by submit. If you submitted from a different working "
            "directory, point [storage] state_dir at the same place."
        )

    def _authorize_locked(
        self,
        proof_id: str,
        *,
        document_sha256: str,
        acknowledge_cost_usd: float | None = None,
    ) -> dict[str, Any]:
        """The body of :meth:`authorize`, run with this proof's lock held.

        Callers must be holding ``store.exclusive(proof_lock_name(proof_id))``. The
        proof is re-read here rather than passed in: a caller that waited for
        the lock must see what the previous holder wrote, not what it read
        before queueing. That re-read is what turns "authorising twice mails
        once" into a property that survives two simultaneous callers — the
        second one finds ``authorized_at`` already set and returns the recorded
        result without touching the transport.
        """
        proof = self.store.get_proof(proof_id)
        if proof is None:
            raise self._unknown_proof(proof_id)

        if proof.authorized:
            return {
                "mailed": False,
                "already_authorized": True,
                "proof_id": proof.proof_id,
                "job_name": proof.job_name,
                "authorized_at": proof.authorized_at,
                "quoted_cost_usd": proof.cost_usd,
                "charged_cost_usd": proof.charged_cost_usd,
                "response": proof.authorize_response,
                "note": (
                    "This proof was already authorised. No second release request "
                    "was sent, so this call mailed nothing."
                ),
            }

        if proof.release_attempted_at is not None:
            raise AmbiguousRelease(
                f"A release for proof {proof.proof_id} (job {proof.job_name}) was "
                "started but never confirmed, so it is unknown whether the job was "
                "released. Retrying could mail it twice. Check job "
                f"{proof.job_name!r} in your LetterStream dashboard and record the "
                "outcome there."
            )

        age = time.time() - proof.created_at
        if age > self.config.proof_ttl_seconds:
            raise ProofExpired(
                f"Proof {proof.proof_id} is {int(age)}s old; the limit is "
                f"{self.config.proof_ttl_seconds}s. Prices, addresses and documents "
                "drift. Submit again under a new job name and re-review before "
                "authorising."
            )

        supplied = (document_sha256 or "").strip().lower()
        if supplied != proof.document_sha256.lower():
            raise ProofMismatch(
                f"The document hash supplied to authorize does not match proof "
                f"{proof.proof_id}. Expected the hash you were given at submit time. "
                "Nothing was released."
            )

        document = Path(proof.document_path)
        if not document.is_file():
            raise ProofMismatch(
                f"The document this proof approved is no longer readable at "
                f"{document}. Nothing was released. Submit again once the document "
                "is back in place."
            )
        on_disk = sha256_file(document)
        if on_disk.lower() != proof.document_sha256.lower():
            raise ProofMismatch(
                f"{document} has changed since it was submitted.\n"
                f"  approved: {proof.document_sha256}\n"
                f"  on disk : {on_disk}\n"
                "The proof approved the earlier bytes, so it does not authorise "
                "these. Nothing was released. Submit the current document again and "
                "review the new proof."
            )

        ceiling = self.config.max_authorize_cost_usd
        if ceiling is not None and proof.cost_usd is not None and proof.cost_usd > ceiling:
            raise CostCeilingExceeded(
                f"Proof {proof.proof_id} is quoted at ${proof.cost_usd:.2f}, above the "
                f"configured ceiling of ${ceiling:.2f}. Nothing was released. Raise "
                "[safety] max_authorize_cost_usd if this is intended."
            )

        if acknowledge_cost_usd is not None and proof.cost_usd is not None:
            if abs(acknowledge_cost_usd - proof.cost_usd) > 0.005:
                raise CostCeilingExceeded(
                    f"You acknowledged ${acknowledge_cost_usd:.2f} but LetterStream "
                    f"quoted ${proof.cost_usd:.2f} for proof {proof.proof_id}. Nothing "
                    "was released."
                )

        # Claimed, durably, before the request goes out and while the lock is
        # held. Two things depend on that ordering. Within a process, the lock
        # is what stops a second caller reaching here at all. Across a crash,
        # the lock is gone — the kernel drops it when the process dies — and
        # this stamp is what remains: the next authorize re-reads it, sees a
        # release that was started and never confirmed, and refuses. Recovery
        # is deliberately a ledger fact rather than a lock that expires, since
        # an expiring lock would hand the proof to a second caller and mail it
        # twice.
        proof.release_attempted_at = time.time()
        self.store.put_proof(proof)

        parsed = self.client.release(proof.authcode)

        proof.authorized_at = time.time()
        proof.authorize_response = parsed
        proof.charged_cost_usd = response_cost(parsed)
        self.store.put_proof(proof)

        charged = proof.charged_cost_usd
        return {
            "mailed": True,
            "already_authorized": False,
            "proof_id": proof.proof_id,
            "job_name": proof.job_name,
            "recipient_count": proof.recipient_count,
            "quoted_cost_usd": proof.cost_usd,
            "charged_cost_usd": charged,
            "cost_matches_quote": (
                None
                if charged is None or proof.cost_usd is None
                else abs(charged - proof.cost_usd) <= 0.005
            ),
            "response_code": parsed.get("code"),
            "response": parsed,
            "note": "Released into production at LetterStream. This cannot be undone here.",
        }

    # ---- read-only ----------------------------------------------------

    def list_proofs(self) -> list[dict[str, Any]]:
        proofs = sorted(
            self.store.all_proofs().values(), key=lambda p: p.created_at, reverse=True
        )
        return [p.public_view() for p in proofs]

    def get_proof(self, proof_id: str) -> dict[str, Any]:
        proof = self.store.get_proof(proof_id)
        if proof is None:
            raise UnknownProof(f"No proof {proof_id!r} in {self.store.proofs_path}.")
        return proof.public_view()

    def _require_live(self, what: str) -> None:
        if not self.config.live:
            raise DryRunRefusal(
                f"Live mode is off, so {what} cannot contact LetterStream. "
                "Set [safety] live = true in config.toml, or LETTERSTREAM_LIVE=true."
            )

    def account_status(self) -> dict[str, Any]:
        self._require_live("account status")
        return self.client.account_status()

    def download_proof_pdfs(self, proof_id: str, out_dir: Path | str) -> list[str]:
        """Download LetterStream's print proofs for a held job. Read-only.

        Uses ``transport.query`` only. Nothing here releases mail.
        """
        self._require_live("proof download")
        proof = self.store.get_proof(proof_id)
        if proof is None:
            raise UnknownProof(f"No proof {proof_id!r} in {self.store.proofs_path}.")
        destination = Path(out_dir).expanduser()
        destination.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for doc in proof.per_doc:
            doc_id = doc.get("id")
            if not doc_id:
                continue
            pdf = self.client.document_proof_pdf(str(doc_id))
            target = destination / f"proof_{proof.proof_id}_{doc_id}.pdf"
            target.write_bytes(pdf)
            written.append(str(target))
        return written

    def tracking(self, proof_id: str) -> list[dict[str, Any]]:
        """Fetch tracking for each recipient copy. Read-only."""
        self._require_live("tracking")
        proof = self.store.get_proof(proof_id)
        if proof is None:
            raise UnknownProof(f"No proof {proof_id!r} in {self.store.proofs_path}.")
        results: list[dict[str, Any]] = []
        for doc in proof.per_doc:
            doc_id = doc.get("id")
            if not doc_id:
                continue
            results.append({"doc_id": doc_id, "tracking": self.client.tracking(str(doc_id))})
        return results
