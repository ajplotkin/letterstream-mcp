"""Durable ledgers for proofs and for in-flight submissions, plus the lock that
makes them safe to touch from more than one caller at a time.

Two files live under the configured state directory:

``proofs.json``
    One record per held job, keyed by proof id. Holds the authcode and the
    document hash it is bound to, plus the authorisation outcome once released.

``inflight.json``
    One record per idempotency key. A key is written here **before** the
    submission request goes out and updated after it returns. That ordering is
    the whole point: if the process dies or the network drops between those two
    moments, the key survives marked ``in_flight``, and the next attempt knows
    it must ask LetterStream what happened rather than submit again.

Writes are atomic (write to a uniquely named temporary file in the same
directory, fsync, rename) so a crash mid-write cannot leave a half-parsed
ledger behind.

Atomicity of a *write* is not the same as exclusion between two *callers*,
and the gate needs both. ``authorize`` has to read a proof, decide it has not
been released, and stamp it as claimed without any other caller slipping
between the decision and the stamp — otherwise two callers both decide "not
released yet" and both mail. :meth:`Store.exclusive` provides that exclusion;
:mod:`letterstream_mcp.gate` holds it across the whole claim-release-record
sequence.

The lock is ``flock`` on a file under ``state_dir/locks/``, paired with an
in-process :class:`threading.RLock` for the same name:

* the ``threading.RLock`` is what serialises threads, which is the case that
  actually ships — FastMCP dispatches synchronous tools on a worker pool;
* ``flock`` extends the same exclusion to separate processes sharing a state
  directory, where the platform provides :mod:`fcntl` (see
  :data:`CROSS_PROCESS_LOCKING`);
* ``flock`` ownership is released by the kernel when the holding process exits,
  so a crash cannot leave a lock that has to be cleaned up, and there is no
  timeout-based expiry that could hand the lock to a second caller while the
  first is still mailing.

The lock *files* are never deleted. That is deliberate: an empty file is inert,
whereas deleting it opens a window where two callers lock two different inodes
under the same name. Recovery from a crash mid-release is not the lock's job —
it is ``release_attempted_at`` in ``proofs.json``, which survives the crash and
makes the next caller refuse.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import LockTimeout
from .models import Proof

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover - Windows and friends
    fcntl = None  # type: ignore[assignment]

#: True when this platform can extend the lock across processes. Without
#: :mod:`fcntl` the in-process lock still holds and the gate still serialises
#: threads, but two *processes* sharing one state directory are not excluded.
#: Stated as a value rather than as prose so a caller can check it.
CROSS_PROCESS_LOCKING = fcntl is not None

STATE_IN_FLIGHT = "in_flight"
STATE_SUBMITTED = "submitted"
STATE_FAILED = "failed"

#: How long a caller waits for a contended lock before failing closed.
DEFAULT_LOCK_TIMEOUT_SECONDS = 120.0

_POLL_SECONDS = 0.005


@dataclass
class InFlightRecord:
    """What we knew about a submission attempt at the moment it went out."""

    idempotency_key: str
    job_name: str
    document_sha256: str
    state: str
    created_at: float
    updated_at: float
    proof_id: str | None = None
    attempts: int = 0
    last_error: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "job_name": self.job_name,
            "document_sha256": self.document_sha256,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "proof_id": self.proof_id,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InFlightRecord":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write ``payload`` to ``path`` atomically.

    The temporary file name is unique per write. A shared ``.tmp`` name is not
    merely untidy: two writers racing on it clobber each other's partial
    content, and the second ``os.replace`` raises ``FileNotFoundError`` because
    the first already renamed the file out from under it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


class _NamedLock:
    """One exclusive lock: a thread lock, and an ``flock`` behind it.

    Re-entrant for the thread that holds it, so a locked public method may call
    another locked public method without deadlocking. Re-entry does not take a
    second ``flock``; the outermost hold owns the descriptor and closing it on
    the way out is what releases the OS lock.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._rlock = threading.RLock()
        self._local = threading.local()

    @contextmanager
    def hold(self, timeout: float, *, label: str) -> Iterator[None]:
        deadline = time.monotonic() + timeout
        if not self._rlock.acquire(timeout=max(timeout, 0.0)):
            raise LockTimeout(_timeout_message(label, timeout))
        try:
            depth = getattr(self._local, "depth", 0)
            if depth == 0:
                self._local.fd = self._flock(deadline, label, timeout)
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth -= 1
                if self._local.depth == 0:
                    descriptor, self._local.fd = self._local.fd, None
                    if descriptor is not None:
                        os.close(descriptor)  # releases the flock
        finally:
            self._rlock.release()

    def _flock(self, deadline: float, label: str, timeout: float) -> int | None:
        if fcntl is None:  # pragma: no cover - platform dependent
            return None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return descriptor
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                        raise
                    if time.monotonic() >= deadline:
                        raise LockTimeout(_timeout_message(label, timeout)) from exc
                    time.sleep(_POLL_SECONDS)
        except BaseException:
            os.close(descriptor)
            raise


def _timeout_message(label: str, timeout: float) -> str:
    return (
        f"Another caller has held the lock for {label} for more than "
        f"{timeout:g}s, so this call refused rather than acting on state that "
        "the other caller may be part-way through changing. Nothing was "
        "submitted and nothing was mailed. Do not retry blindly: check whether "
        "the other call completed first (letterstream_get_proof, or your "
        "LetterStream dashboard)."
    )


_LOCK_REGISTRY: dict[str, _NamedLock] = {}
_LOCK_REGISTRY_GUARD = threading.Lock()


def _lock_for(path: Path) -> _NamedLock:
    """One :class:`_NamedLock` per lock-file path, per process.

    Keyed on the canonical path rather than on the :class:`Store` instance, so
    two ``Store`` objects pointing at the same state directory — which is what
    two MCP tool calls constructing their own toolsets would produce — share the
    same in-process lock instead of each getting a private one.

    Entries are held strongly and never evicted, so the registry grows by one
    small object per distinct proof id and idempotency key a process handles.
    That is deliberate: dropping an entry whose lock might still be wanted is a
    correctness question, and the memory is not worth the argument.
    """
    key = str(path)
    with _LOCK_REGISTRY_GUARD:
        existing = _LOCK_REGISTRY.get(key)
        if existing is None:
            existing = _NamedLock(path)
            _LOCK_REGISTRY[key] = existing
        return existing


class Store:
    """File-backed proof and in-flight ledgers.

    Safe against a single process crashing at any point, and safe against
    concurrent callers *that go through the same public methods* — reads and
    writes of each ledger file are serialised by :meth:`exclusive`, and
    :mod:`letterstream_mcp.gate` holds a longer-lived lock across the
    check-then-act sequences that would otherwise race.

    Two scopes, stated separately because they are enforced differently:
    threads within one process are excluded by a :class:`threading.RLock`;
    separate processes are excluded by ``flock``, which needs :mod:`fcntl` (see
    :data:`CROSS_PROCESS_LOCKING`). Nothing here defends against a writer that
    edits ``proofs.json`` directly instead of calling these methods.
    """

    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.proofs_path = self.state_dir / "proofs.json"
        self.inflight_path = self.state_dir / "inflight.json"
        self.locks_dir = self.state_dir / "locks"

    # ---- locking ------------------------------------------------------

    def lock_path(self, name: str) -> Path:
        """Path of the lock file for ``name``, in canonical form.

        The name is hashed rather than used directly: idempotency keys are
        operator-supplied and may contain anything, including separators that
        would escape the locks directory.

        The directory is created and resolved here, not stored as given. The
        registry that pairs a lock file with its in-process lock is keyed on
        this path, so two ``Store`` objects naming one state directory
        differently — a relative path, a symlink, an alias — have to produce the
        same key. If they did not, each would get a private lock and the pair
        would exclude nobody.
        """
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
        return self.locks_dir.resolve() / f"{digest}.lock"

    @contextmanager
    def exclusive(
        self, name: str, *, timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS
    ) -> Iterator[None]:
        """Hold the exclusive lock named ``name`` for the body of the block.

        Raises :class:`~letterstream_mcp.errors.LockTimeout` — a
        ``GateRefusal`` — if the lock is not obtained within ``timeout``. It
        does not fall through and it does not steal the lock, because the
        holder may be mid-release.
        """
        with _lock_for(self.lock_path(name)).hold(timeout, label=name):
            yield

    def _ledger(self, path: Path):
        """Short lock held only across one read-modify-write of a ledger file."""
        return _lock_for(self.lock_path(f"ledger::{path.name}")).hold(
            DEFAULT_LOCK_TIMEOUT_SECONDS, label=f"the {path.name} ledger"
        )

    def _read_ledger(self, path: Path) -> dict[str, Any]:
        with self._ledger(path):
            return _read_json(path)

    def _update_ledger(self, path: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
        with self._ledger(path):
            data = _read_json(path)
            mutate(data)
            _atomic_write_json(path, data)

    # ---- proofs -------------------------------------------------------

    def all_proofs(self) -> dict[str, Proof]:
        return _proofs_from(self._read_ledger(self.proofs_path))

    def get_proof(self, proof_id: str) -> Proof | None:
        return self.all_proofs().get(proof_id)

    def put_proof(self, proof: Proof) -> None:
        self._update_ledger(
            self.proofs_path,
            lambda data: data.__setitem__(proof.proof_id, proof.to_dict()),
        )

    def find_proof_by_idempotency_key(self, key: str) -> Proof | None:
        for proof in self.all_proofs().values():
            if proof.idempotency_key == key:
                return proof
        return None

    # ---- in-flight ledger ---------------------------------------------

    def all_inflight(self) -> dict[str, InFlightRecord]:
        return {
            key: InFlightRecord.from_dict(value)
            for key, value in self._read_ledger(self.inflight_path).items()
            if isinstance(value, dict)
        }

    def get_inflight(self, key: str) -> InFlightRecord | None:
        return self.all_inflight().get(key)

    def put_inflight(self, record: InFlightRecord) -> None:
        record.updated_at = time.time()
        self._update_ledger(
            self.inflight_path,
            lambda data: data.__setitem__(record.idempotency_key, record.to_dict()),
        )

    def begin_attempt(
        self, *, idempotency_key: str, job_name: str, document_sha256: str
    ) -> InFlightRecord:
        """Record an attempt before it is made, and return the record.

        Called immediately before the transport call. If the caller never gets
        to :meth:`complete_attempt` or :meth:`fail_attempt`, the record stays
        ``in_flight`` on disk and the next run reconciles it.
        """
        now = time.time()
        existing = self.get_inflight(idempotency_key)
        if existing is None:
            existing = InFlightRecord(
                idempotency_key=idempotency_key,
                job_name=job_name,
                document_sha256=document_sha256,
                state=STATE_IN_FLIGHT,
                created_at=now,
                updated_at=now,
            )
        existing.state = STATE_IN_FLIGHT
        existing.attempts += 1
        self.put_inflight(existing)
        return existing

    def complete_attempt(self, record: InFlightRecord, *, proof_id: str) -> None:
        record.state = STATE_SUBMITTED
        record.proof_id = proof_id
        record.last_error = None
        self.put_inflight(record)

    def fail_attempt(self, record: InFlightRecord, *, error: str) -> None:
        """Mark an attempt as failed *without* clearing the in-flight warning.

        The state stays ``in_flight`` on purpose. A transport error means we do
        not know whether LetterStream accepted the job, so the next attempt must
        reconcile rather than assume nothing happened. ``last_error`` records
        what went wrong; ``state`` records what we still do not know.
        """
        record.last_error = error
        record.state = STATE_IN_FLIGHT
        record.notes.append(f"attempt {record.attempts} failed: {error}"[:500])
        self.put_inflight(record)

    def mark_abandoned(self, record: InFlightRecord, *, reason: str) -> None:
        record.state = STATE_FAILED
        record.notes.append(reason[:500])
        self.put_inflight(record)


def _proofs_from(data: dict[str, Any]) -> dict[str, Proof]:
    return {
        key: Proof.from_dict(value)
        for key, value in data.items()
        if isinstance(value, dict)
    }
