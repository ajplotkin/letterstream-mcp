"""Property: the gate is a gate even when two callers arrive at once.

Every other test in this suite calls the gate from one thread. That is not the
configuration this package ships in: FastMCP dispatches synchronous tools on a
worker-thread pool, so two tool calls that overlap in time run the gate's code
concurrently in one process.

Before the lock in :mod:`letterstream_mcp.store` existed, ``authorize`` was a
non-atomic check-then-set — it read ``proof.authorized`` and
``proof.release_attempted_at`` at the top and wrote ``release_attempted_at``
only much later, after re-hashing the document. Threads that both passed the
check before either wrote the stamp both reached ``client.release``, and one
proof mailed twice. ``submit`` had the same shape between
``find_proof_by_idempotency_key`` and ``begin_attempt``.

These tests are written to lose that race reliably rather than occasionally:

* a :class:`threading.Barrier` releases every thread into ``authorize`` within
  microseconds of the others;
* the document is large enough that the SHA-256 re-read inside the unguarded
  window takes long enough to interleave (that window is where the old code
  lost);
* :func:`sys.setswitchinterval` is lowered so CPython preempts more often;
* and each scenario runs many independent iterations, so a lucky ordering in
  one iteration cannot make the whole test pass.

Measured, not assumed: against a copy of this repository with the two
``store.exclusive`` blocks removed from ``gate.py`` and nothing else changed,
seven of the eleven tests here fail on every run of five, and the authorize
race fails on its *first* iteration with all six threads reaching
``client.release``. Against the guarded gate, twenty consecutive runs of this
file and fifteen of the whole suite passed.

The assertions that matter are on ``transport.release_calls`` — the list that
counts real mailings — rather than on a return value.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import make_config
from fakes import FakeTransport
from fixtures.synthetic_recipients import SYNTHETIC_RECIPIENTS, SYNTHETIC_SENDER
from letterstream_mcp.errors import AmbiguousRelease, LockTimeout
from letterstream_mcp.gate import MailGate, proof_lock_name
from letterstream_mcp.models import SubmitRequest
from letterstream_mcp.store import CROSS_PROCESS_LOCKING, Store

#: How many independent races each scenario runs. A concurrency test that runs
#: once proves nothing; a single favourable interleaving is not evidence.
ITERATIONS = 60

#: Threads per race. The audit that found the original bug used 4-5.
THREADS = 6

#: Big enough that hashing it inside authorize takes long enough for other
#: threads to reach the same point. Real certified-mail PDFs are this size.
BULK_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    + b"% synthetic filler to widen the hashing window\n" * 12000
    + b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


@pytest.fixture(autouse=True)
def eager_preemption():
    """Make CPython switch threads more often, for the duration of these tests.

    This only changes how often the interpreter preempts; it does not change
    any behaviour under test. It is here so an interleaving that is possible is
    also *likely*, which is what makes these tests fail against unguarded code
    instead of passing by luck.
    """
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)
    try:
        yield
    finally:
        sys.setswitchinterval(previous)


def _bulk_pdf(directory: Path) -> Path:
    path = directory / "concurrent_letter.pdf"
    path.write_bytes(BULK_PDF)
    return path


def _gate(root: Path, transport: FakeTransport) -> MailGate:
    config = make_config(root / "state", live=True)
    return MailGate(config, transport=transport, store=Store(config.state_dir))


def _request(pdf: Path, **overrides) -> SubmitRequest:
    params = {
        "job_name": "RACEJOB0001",
        "document_path": pdf,
        "pages": 1,
        "sender": SYNTHETIC_SENDER,
        "recipients": SYNTHETIC_RECIPIENTS,
        "mail_type": "certified",
    }
    params.update(overrides)
    return SubmitRequest(**params)


def _race(worker, threads: int = THREADS) -> list:
    """Run ``worker(index)`` on ``threads`` threads released simultaneously.

    Returns one entry per thread: the return value, or the exception raised.
    """
    barrier = threading.Barrier(threads)
    results: list = [None] * threads

    def run(index: int) -> None:
        barrier.wait(timeout=30)
        try:
            results[index] = worker(index)
        except BaseException as exc:  # noqa: BLE001 - the outcome *is* the data
            results[index] = exc

    workers = [threading.Thread(target=run, args=(i,)) for i in range(threads)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=60)
    assert not any(t.is_alive() for t in workers), "a racing thread did not finish"
    return results


# ---- authorize ---------------------------------------------------------


def test_concurrent_authorize_on_one_proof_releases_exactly_once(tmp_path):
    """The headline property, under contention: one proof, one mailing.

    This is the test the original TOCTOU failed. With the locks removed it
    reported six release calls — one per racing thread — for a single proof, on
    the first iteration.
    """
    for iteration in range(ITERATIONS):
        root = tmp_path / f"iter{iteration}"
        root.mkdir()
        pdf = _bulk_pdf(root)
        transport = FakeTransport()
        gate = _gate(root, transport)
        submitted = gate.submit(_request(pdf))

        _race(
            lambda _i: gate.authorize(
                submitted["proof_id"], document_sha256=submitted["document_sha256"]
            )
        )

        assert len(transport.release_calls) == 1, (
            f"iteration {iteration}: {len(transport.release_calls)} release calls "
            f"for one proof; concurrent authorize double-mailed"
        )


def test_the_losers_of_an_authorize_race_get_the_already_authorized_answer(tmp_path):
    """Exactly one caller mails; every other caller is told so, and mails nothing.

    The losers must not merely fail to mail — they must come back with the
    truthful, actionable answer rather than a lock error or a crash.
    """
    for iteration in range(ITERATIONS):
        root = tmp_path / f"iter{iteration}"
        root.mkdir()
        pdf = _bulk_pdf(root)
        transport = FakeTransport()
        gate = _gate(root, transport)
        submitted = gate.submit(_request(pdf))

        outcomes = _race(
            lambda _i: gate.authorize(
                submitted["proof_id"], document_sha256=submitted["document_sha256"]
            )
        )

        raised = [o for o in outcomes if isinstance(o, BaseException)]
        assert raised == [], f"iteration {iteration}: unexpected {raised!r}"
        mailed = [o for o in outcomes if o["mailed"] is True]
        told_already = [
            o for o in outcomes if o["mailed"] is False and o["already_authorized"]
        ]
        assert len(mailed) == 1, f"iteration {iteration}: {len(mailed)} callers mailed"
        assert len(told_already) == THREADS - 1
        assert len(transport.release_calls) == 1


def test_a_concurrent_authorize_whose_release_fails_is_not_retried_by_the_losers(
    tmp_path,
):
    """A release of unknown outcome stays unknown, even with callers queued behind it.

    The winner's release request fails after the transport saw it. Every caller
    behind it must get :class:`AmbiguousRelease` rather than a second attempt —
    the concurrent form of "an unconfirmed release is never retried".
    """
    for iteration in range(ITERATIONS):
        root = tmp_path / f"iter{iteration}"
        root.mkdir()
        pdf = _bulk_pdf(root)
        transport = FakeTransport()
        transport.fail_next_release = RuntimeError("socket closed mid-release")
        gate = _gate(root, transport)
        submitted = gate.submit(_request(pdf))

        outcomes = _race(
            lambda _i: gate.authorize(
                submitted["proof_id"], document_sha256=submitted["document_sha256"]
            )
        )

        assert len(transport.release_calls) == 1, (
            f"iteration {iteration}: {len(transport.release_calls)} release calls; "
            "an unconfirmed release was retried by a racing caller"
        )
        ambiguous = [o for o in outcomes if isinstance(o, AmbiguousRelease)]
        crashed = [o for o in outcomes if isinstance(o, RuntimeError)]
        assert len(crashed) == 1, f"iteration {iteration}: {crashed!r}"
        assert len(ambiguous) == THREADS - 1


def test_a_crash_between_claiming_and_recording_blocks_every_later_caller(tmp_path):
    """Recovery path: the durable stamp, not the lock, is what refuses.

    A process that dies between claiming a release and recording its outcome
    leaves ``release_attempted_at`` set and ``authorized_at`` unset. The OS
    drops its lock immediately — that is the point of using ``flock`` rather
    than a lock file that has to be cleaned up — so the next callers acquire
    the lock without waiting. What stops them is the ledger record, and it stops
    all of them, however many arrive at once.
    """
    for iteration in range(ITERATIONS):
        root = tmp_path / f"iter{iteration}"
        root.mkdir()
        pdf = _bulk_pdf(root)
        transport = FakeTransport()
        gate = _gate(root, transport)
        submitted = gate.submit(_request(pdf))

        # Exactly the on-disk state a crash mid-release leaves behind.
        crashed = gate.store.get_proof(submitted["proof_id"])
        crashed.release_attempted_at = time.time()
        crashed.authorized_at = None
        gate.store.put_proof(crashed)

        outcomes = _race(
            lambda _i: gate.authorize(
                submitted["proof_id"], document_sha256=submitted["document_sha256"]
            )
        )

        assert transport.release_calls == [], (
            f"iteration {iteration}: a release was sent for a proof whose previous "
            "release never reported back"
        )
        assert all(isinstance(o, AmbiguousRelease) for o in outcomes), outcomes


def test_a_lock_file_left_behind_by_a_dead_process_does_not_brick_the_gate(tmp_path):
    """The lock file persists on disk by design; holding it is what matters.

    ``flock`` ownership dies with the process that held it, so a lock file left
    over from a crash is inert. This asserts the recovery direction the previous
    test does not: a stale *file* must not permanently refuse a proof that was
    never actually released.
    """
    pdf = _bulk_pdf(tmp_path)
    transport = FakeTransport()
    gate = _gate(tmp_path, transport)
    submitted = gate.submit(_request(pdf))

    lock_dir = gate.store.locks_dir
    assert lock_dir.is_dir(), "submit should have created the lock directory"
    stale = list(lock_dir.iterdir())
    assert stale, "submit should have left its lock file on disk"
    for path in stale:
        assert path.is_file()

    # No process holds these; authorize must proceed normally.
    result = gate.authorize(
        submitted["proof_id"], document_sha256=submitted["document_sha256"]
    )
    assert result["mailed"] is True
    assert len(transport.release_calls) == 1


def test_a_caller_that_cannot_get_the_lock_refuses_rather_than_mailing(tmp_path):
    """Fail closed on lock timeout: refuse, do not proceed, do not retry.

    Simulated by holding the proof's lock from another thread for longer than
    the caller is willing to wait. The refusal is a :class:`GateRefusal`, so the
    MCP layer renders it as an error dict rather than a traceback.
    """
    from letterstream_mcp.errors import GateRefusal

    pdf = _bulk_pdf(tmp_path)
    transport = FakeTransport()
    gate = _gate(tmp_path, transport)
    submitted = gate.submit(_request(pdf))
    gate.lock_timeout_seconds = 0.25

    holding = threading.Event()
    release_it = threading.Event()

    def squat() -> None:
        with gate.store.exclusive(proof_lock_name(submitted["proof_id"]), timeout=10):
            holding.set()
            release_it.wait(timeout=30)

    squatter = threading.Thread(target=squat)
    squatter.start()
    try:
        assert holding.wait(timeout=10)
        with pytest.raises(LockTimeout, match=r"held the lock for") as excinfo:
            gate.authorize(
                submitted["proof_id"], document_sha256=submitted["document_sha256"]
            )
    finally:
        release_it.set()
        squatter.join(timeout=10)

    assert isinstance(excinfo.value, GateRefusal)
    assert submitted["proof_id"] in str(excinfo.value)
    assert transport.release_calls == [], "a caller that timed out on the lock mailed"


# ---- submit ------------------------------------------------------------


def test_concurrent_identical_submits_create_exactly_one_held_job(tmp_path):
    """"An identical resubmit reuses the existing proof", under contention.

    Held jobs do not mail, so this is less dangerous than the authorize race —
    but two held jobs for one letter is still two letters waiting to be mailed,
    and the README states this property without qualification.
    """
    for iteration in range(ITERATIONS):
        root = tmp_path / f"iter{iteration}"
        root.mkdir()
        pdf = _bulk_pdf(root)
        transport = FakeTransport()
        gate = _gate(root, transport)

        outcomes = _race(lambda _i: gate.submit(_request(pdf)))

        raised = [o for o in outcomes if isinstance(o, BaseException)]
        assert raised == [], f"iteration {iteration}: {raised!r}"
        assert len(transport.submit_calls) == 1, (
            f"iteration {iteration}: {len(transport.submit_calls)} held jobs created "
            "for one identical submission"
        )
        proof_ids = {o["proof_id"] for o in outcomes}
        assert len(proof_ids) == 1, f"iteration {iteration}: {proof_ids}"
        assert len(gate.store.all_proofs()) == 1
        assert transport.release_calls == []


def test_concurrent_submits_of_different_letters_all_survive_in_the_ledger(tmp_path):
    """Distinct letters must not overwrite each other in a shared ledger file.

    ``put_proof`` is a read-modify-write of one JSON document. Without a lock
    around it, two threads writing different proofs lose one of them — and the
    shared ``.tmp`` filename the writes used to share made the racing
    ``os.replace`` raise ``FileNotFoundError`` outright.
    """
    for iteration in range(ITERATIONS):
        root = tmp_path / f"iter{iteration}"
        root.mkdir()
        transport = FakeTransport()
        gate = _gate(root, transport)
        pdfs = []
        for index in range(THREADS):
            path = root / f"letter{index}.pdf"
            path.write_bytes(BULK_PDF + f"% letter {index}\n".encode())
            pdfs.append(path)

        outcomes = _race(
            lambda i: gate.submit(
                _request(pdfs[i], job_name=f"RACEJOB{i:04d}", idempotency_key=f"key-{i}")
            )
        )

        raised = [o for o in outcomes if isinstance(o, BaseException)]
        assert raised == [], f"iteration {iteration}: {raised!r}"
        assert len(transport.submit_calls) == THREADS
        assert len(gate.store.all_proofs()) == THREADS, (
            f"iteration {iteration}: {len(gate.store.all_proofs())} of {THREADS} "
            "proofs survived concurrent ledger writes"
        )
        assert len(gate.store.all_inflight()) == THREADS


def test_separate_gate_objects_over_one_state_directory_share_the_lock(tmp_path):
    """Two callers that each build their own Store must still exclude each other.

    Every other race here shares one :class:`MailGate`. An MCP server that
    builds a ``ToolSet`` per call, or a CLI run twice, does not — it produces
    several ``Store`` objects over one state directory, half of them reaching it
    through a symlink here.

    Two separate assertions, because they cover different halves of the lock and
    only one of them is a race. The first is direct: both spellings must hash to
    the same lock-file path, which is what puts them on the same *in-process*
    lock. Without that canonicalisation they would each register a private
    ``threading.Lock`` and fall back to ``flock`` alone — which does still
    exclude them here, so the race below would pass either way and is not
    evidence for the first assertion. On a platform with no :mod:`fcntl` there
    would be nothing left to fall back to.
    """
    state = tmp_path / "state"
    state.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(state, target_is_directory=True)
    pdf = _bulk_pdf(tmp_path)
    transport = FakeTransport()

    assert Store(state).lock_path("proof::x") == Store(alias).lock_path("proof::x"), (
        "two spellings of one state directory produced two lock files; each "
        "caller would get a private in-process lock"
    )

    seed = MailGate(
        make_config(state, live=True), transport=transport, store=Store(state)
    )
    submitted = seed.submit(_request(pdf))

    spellings = [state, alias, state, alias, state, alias][:THREADS]
    gates = [
        MailGate(make_config(s, live=True), transport=transport, store=Store(s))
        for s in spellings
    ]

    outcomes = _race(
        lambda i: gates[i].authorize(
            submitted["proof_id"], document_sha256=submitted["document_sha256"]
        )
    )

    assert [o for o in outcomes if isinstance(o, BaseException)] == [], outcomes
    assert len(transport.release_calls) == 1, (
        f"{len(transport.release_calls)} release calls from {THREADS} independent "
        "gates over one state directory"
    )
    assert sum(1 for o in outcomes if o["mailed"] is True) == 1


def test_two_writers_of_one_ledger_file_do_not_collide_on_a_temp_name(tmp_path):
    """``_atomic_write_json`` is safe on its own, not only because callers lock.

    The ledger lock already serialises the two public methods that write
    ``proofs.json``, so this is defence in depth — but the function is the
    primitive everything else is built on, and it should not depend on its
    caller for correctness. With the shared ``<name>.tmp`` filename it used to
    build, two writers clobbered each other's partial content and the second
    ``os.replace`` raised ``FileNotFoundError``, because the first had already
    renamed the file out from under it.

    Called directly here, deliberately bypassing :meth:`Store._ledger`, because
    that is the only way to test the primitive rather than the lock in front of
    it.
    """
    from letterstream_mcp.store import _atomic_write_json

    target = tmp_path / "proofs.json"

    def write(index: int) -> int:
        for round_number in range(40):
            _atomic_write_json(target, {"writer": index, "round": round_number})
        return index

    outcomes = _race(write)
    raised = [o for o in outcomes if isinstance(o, BaseException)]
    assert raised == [], f"concurrent atomic writes collided: {raised!r}"
    assert json.loads(target.read_text(encoding="utf-8"))["round"] == 39
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"temporary files left behind: {leftovers}"


# ---- across processes --------------------------------------------------


CHILD_SCRIPT = '''
import json, os, socket, sys, time

# The suite's autouse no_network fixture does not reach a child process, so the
# child installs the same block itself. Nothing here may contact LetterStream.
def _blocked(*a, **k):
    raise AssertionError("child process attempted a network connection")


socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked

src, tests, state_dir, proof_id, doc_hash, log, ready_dir, go_path = sys.argv[1:]
sys.path.insert(0, tests)
sys.path.insert(0, src)
from fakes import FakeTransport
from letterstream_mcp.config import Config
from letterstream_mcp.gate import MailGate
from letterstream_mcp.store import Store


class LoggingTransport(FakeTransport):
    """Appends one line per release to a file every child shares.

    Small O_APPEND writes are atomic on POSIX, so the line count is the number
    of real release calls across all the processes, not an approximation.
    """

    def release(self, fields):
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, b"release ")
        finally:
            os.close(fd)
        return super().release(fields)


config = Config(
    api_id="fake-api-id-for-tests",
    api_key="fake-api-key-for-tests",
    base_url="https://fake.invalid/apis/index.php",
    live=True,
    state_dir=state_dir,
    credential_source="test fixture",
)
gate = MailGate(config, transport=LoggingTransport(), store=Store(state_dir))

# Two-phase start. The child announces that it is fully loaded, then waits on a
# file the parent creates once every child has announced. Waiting on the parent
# rather than on each other means no child is burning CPU while the last one is
# still importing, so all of them become runnable at the same instant and
# actually contend. Guessing a wall-clock start instead makes this test pass by
# accident on a loaded machine.
open(os.path.join(ready_dir, str(os.getpid())), "wb").close()
deadline = time.time() + 60
while not os.path.exists(go_path) and time.time() < deadline:
    time.sleep(0.001)

try:
    result = gate.authorize(proof_id, document_sha256=doc_hash)
    print(json.dumps({"outcome": "ok", "mailed": result["mailed"]}))
except Exception as exc:
    print(json.dumps({"outcome": type(exc).__name__}))
'''

#: Processes per round of the cross-process race.
CHILDREN = 4


@pytest.mark.skipif(
    not CROSS_PROCESS_LOCKING,
    reason="cross-process exclusion needs fcntl.flock, which this platform lacks",
)
def test_two_processes_authorizing_one_proof_release_it_once(tmp_path):
    """The exclusion is an OS lock, so it also holds between separate processes.

    Stated as a test rather than as prose because a ``threading.Lock`` would
    pass every other test in this file and fail this one. Both children run
    against :class:`FakeTransport`; nothing here contacts LetterStream.
    """
    pdf = _bulk_pdf(tmp_path)
    transport = FakeTransport()
    gate = _gate(tmp_path, transport)
    submitted = gate.submit(_request(pdf))

    repo = Path(__file__).resolve().parent.parent
    script = tmp_path / "child_authorize.py"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")
    log = tmp_path / "releases.log"

    for iteration in range(10):
        # A fresh, unauthorised copy of the proof for each round.
        proof = gate.store.get_proof(submitted["proof_id"])
        proof.release_attempted_at = None
        proof.authorized_at = None
        proof.authorize_response = None
        gate.store.put_proof(proof)
        log.write_bytes(b"")
        ready = tmp_path / f"ready{iteration}"
        ready.mkdir()
        go = tmp_path / f"go{iteration}"

        children = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(script),
                    str(repo / "src"),
                    str(repo / "tests"),
                    str(gate.store.state_dir),
                    submitted["proof_id"],
                    submitted["document_sha256"],
                    str(log),
                    str(ready),
                    str(go),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(CHILDREN)
        ]
        # Release them together, once every child is loaded and waiting.
        waited = time.monotonic() + 60
        while len(list(ready.iterdir())) < CHILDREN and time.monotonic() < waited:
            time.sleep(0.005)
        assert len(list(ready.iterdir())) == CHILDREN, "a child never reported ready"
        go.write_bytes(b"")

        reports = []
        for child in children:
            out, err = child.communicate(timeout=120)
            assert child.returncode == 0, err
            reports.append(json.loads(out.strip().splitlines()[-1]))

        releases = log.read_text().count("release")
        assert releases == 1, (
            f"round {iteration}: {releases} processes released one proof"
        )
        assert sum(1 for r in reports if r.get("mailed") is True) == 1, reports
