"""Error types for letterstream-mcp.

Every error raised deliberately by this package derives from
:class:`LetterStreamError` and carries a message written for a human operator
rather than a stack trace. The CLI and the MCP tool layer both catch this base
class and render ``str(exc)``; they do not let it propagate as a traceback.

Errors that indicate a refusal to mail (rather than a failure of some other
kind) additionally derive from :class:`GateRefusal`, so a caller can tell "we
declined to release this" apart from "the network broke".
"""

from __future__ import annotations


class LetterStreamError(Exception):
    """Base class for every error this package raises on purpose."""


class ConfigError(LetterStreamError):
    """Configuration is missing, unreadable, or internally inconsistent."""


class CredentialsError(ConfigError):
    """API credentials were not supplied, or were supplied in an unusable form.

    Raised before any network call is attempted. No transport method is
    reached, so nothing can be submitted or mailed when this is raised.
    """


class TransportError(LetterStreamError):
    """A request to LetterStream could not be completed.

    This covers connection failures, timeouts, and non-2xx responses. It says
    nothing about whether LetterStream accepted the job: a request that fails
    after the server accepted it raises this too, which is exactly the case the
    idempotency ledger exists to handle.
    """


class ApiError(LetterStreamError):
    """LetterStream returned a well-formed response reporting an error code."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class GateRefusal(LetterStreamError):
    """The safety gate declined to act. Nothing was submitted and nothing mailed."""


class DryRunRefusal(GateRefusal):
    """Live mode is off. Raised by any operation that would contact LetterStream."""


class UnknownProof(GateRefusal):
    """No proof with that identifier exists in the local ledger."""


class ProofExpired(GateRefusal):
    """The proof is older than the configured TTL and may no longer be authorised."""


class ProofMismatch(GateRefusal):
    """The document no longer hashes to what the proof approved.

    Raised when the bytes on disk at authorize time differ from the bytes
    hashed at submit time, or when the caller's asserted hash does not match
    the proof. Either way the release call is not made.
    """


class CostCeilingExceeded(GateRefusal):
    """The pre-authorised cost exceeds the configured ceiling."""


class ValidationError(LetterStreamError):
    """A submission was rejected locally before any request was built."""


class OrphanedJob(GateRefusal):
    """A previous submission may have been accepted, but its authcode was lost.

    Raised when an idempotency key is retried after a failed attempt and the
    gate cannot establish that the job is absent from LetterStream. Refusing is
    the fail-safe choice: resubmitting under the same intent is the only way
    this package could produce two jobs for one request.
    """


class LockTimeout(GateRefusal):
    """Another caller holds this proof's, or this idempotency key's, lock.

    Raised when an operation waited for the exclusive lock that serialises
    releases (or submissions) for one key and did not get it in time. Refusing
    is the fail-closed choice: the holder may be mid-release, so proceeding
    would be exactly the double-mailing the lock exists to prevent. Nothing was
    submitted and nothing was mailed by the caller that saw this, and it must
    not be retried blindly.
    """


class AmbiguousRelease(GateRefusal):
    """A release was attempted for this proof but never recorded as complete.

    The job may or may not have been released. Retrying the release could
    double-mail, so the gate refuses and hands the decision to a human.
    """
