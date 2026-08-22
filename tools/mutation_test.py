#!/usr/bin/env python3
"""Break each safety property on purpose; confirm a named test catches it.

A test suite that passes proves nothing about a guard unless removing the guard
makes it fail. This script copies the repository to a temporary directory,
applies one textual mutation, runs pytest there, and checks that the specific
test named for that property failed.

It never modifies the working tree: every mutation is applied to a copy under
the system temp directory, and the copy is deleted afterwards.

Usage::

    python tools/mutation_test.py            # run all mutations
    python tools/mutation_test.py --list     # show them without running
    python tools/mutation_test.py -k dry_run # run mutations whose id matches

Exit status is 0 only if every mutation was caught by its named test.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# `config.toml` and `.env` are excluded because this script copytree()s the whole
# repository once per mutation. On an operator's machine those files hold live API
# credentials, and copying them into TMPDIR 26 times per run puts real secrets
# outside the repository. A killed run would leave them there. The hygiene suite's
# SKIP_FILES is defence in depth for a genuine source export; it is not a reason to
# make the copy in the first place.
EXCLUDE = shutil.ignore_patterns(
    ".git", "__pycache__", "*.pyc", ".pytest_cache", "state", "proofs", ".venv", "venv",
    "config.toml", "*.config.toml", ".env",
)


@dataclass
class Mutation:
    """One deliberate break, and the tests that must notice it."""

    identifier: str
    property_broken: str
    relative_path: str
    find: str
    replace: str
    expect_failures: list[str] = field(default_factory=list)

    def apply(self, root: Path) -> None:
        target = root / self.relative_path
        text = target.read_text(encoding="utf-8")
        if self.find not in text:
            raise SystemExit(
                f"[{self.identifier}] anchor text not found in {self.relative_path}. "
                "The source moved; update tools/mutation_test.py."
            )
        target.write_text(text.replace(self.find, self.replace, 1), encoding="utf-8")


MUTATIONS: list[Mutation] = [
    Mutation(
        identifier="submit-mails-directly",
        property_broken="submit never mails",
        relative_path="src/letterstream_mcp/gate.py",
        find="        self.store.put_proof(proof)\n        self.store.complete_attempt(record, proof_id=proof.proof_id)\n",
        replace=(
            "        self.store.put_proof(proof)\n"
            "        self.store.complete_attempt(record, proof_id=proof.proof_id)\n"
            "        self.client.release(proof.authcode)  # MUTATION: mail on submit\n"
        ),
        expect_failures=[
            "tests/test_submit_never_mails.py::test_submit_never_calls_release",
            "tests/test_submit_never_mails.py::test_gate_submit_source_never_references_release",
        ],
    ),
    Mutation(
        identifier="authorize-ignores-document-hash",
        property_broken="a proof binds to the exact document bytes",
        relative_path="src/letterstream_mcp/gate.py",
        find="        on_disk = sha256_file(document)\n        if on_disk.lower() != proof.document_sha256.lower():",
        replace="        on_disk = proof.document_sha256  # MUTATION: skip the re-hash\n        if False:",
        expect_failures=[
            "tests/test_tampering.py::test_authorize_rejects_a_tampered_document",
            "tests/test_tampering.py::test_a_restored_document_authorizes_normally",
        ],
    ),
    Mutation(
        identifier="authorize-ignores-caller-hash",
        property_broken="the caller must assert the hash it approved",
        relative_path="src/letterstream_mcp/gate.py",
        find="        if supplied != proof.document_sha256.lower():",
        replace="        if False:  # MUTATION: trust whatever hash the caller sent",
        expect_failures=[
            "tests/test_tampering.py::test_authorize_rejects_a_wrong_caller_supplied_hash",
        ],
    ),
    Mutation(
        identifier="dry-run-falls-through-on-submit",
        property_broken="dry run never reaches the transport",
        relative_path="src/letterstream_mcp/gate.py",
        find="        if not self.config.live:\n            # Dry run stops here.",
        replace="        if False:  # MUTATION: dry run falls through\n            # Dry run stops here.",
        expect_failures=[
            "tests/test_dry_run.py::test_dry_run_submit_never_touches_the_transport",
            "tests/test_dry_run.py::test_dry_run_submit_writes_no_proof",
            "tests/test_dry_run.py::test_dry_run_survives_a_long_call_sequence",
        ],
    ),
    Mutation(
        identifier="dry-run-allows-authorize",
        property_broken="dry run refuses to authorise",
        relative_path="src/letterstream_mcp/gate.py",
        find='        if not self.config.live:\n            raise DryRunRefusal(\n                "Live mode is off, so nothing can be authorised or mailed. "',
        replace='        if False:  # MUTATION: dry run authorises\n            raise DryRunRefusal(\n                "Live mode is off, so nothing can be authorised or mailed. "',
        expect_failures=[
            "tests/test_dry_run.py::test_dry_run_authorize_refuses_and_never_releases",
            "tests/test_dry_run.py::test_dry_run_survives_a_long_call_sequence",
        ],
    ),
    Mutation(
        identifier="authorize-twice-double-mails",
        property_broken="authorize is idempotent per proof",
        relative_path="src/letterstream_mcp/gate.py",
        find="        if proof.authorized:\n            return {",
        replace="        if False:  # MUTATION: release again on a second authorize\n            return {",
        expect_failures=[
            "tests/test_authorize.py::test_authorize_twice_does_not_double_mail",
        ],
    ),
    Mutation(
        identifier="no-reconciliation-on-retry",
        property_broken="a retry after a failure cannot create a second job",
        relative_path="src/letterstream_mcp/gate.py",
        find="            self._refuse_or_clear_orphan(prior, request)",
        replace="            pass  # MUTATION: retry blindly",
        expect_failures=[
            "tests/test_idempotency.py::test_retry_after_network_failure_does_not_create_a_second_job",
            "tests/test_idempotency.py::test_an_uninterpretable_status_blocks_the_retry",
            "tests/test_idempotency.py::test_a_failing_status_check_also_blocks_the_retry",
        ],
    ),
    Mutation(
        identifier="ambiguous-status-permits-resubmit",
        property_broken="ambiguity never resolves towards submitting again",
        relative_path="src/letterstream_mcp/gate.py",
        find="    return JOB_UNKNOWN\n",
        replace="    return JOB_ABSENT  # MUTATION: treat the unknown as safe\n",
        expect_failures=[
            "tests/test_idempotency.py::test_an_uninterpretable_status_blocks_the_retry",
            "tests/test_idempotency.py::test_job_status_interpretation",
        ],
    ),
    Mutation(
        identifier="no-idempotency-dedup-on-success",
        property_broken="an identical resubmit reuses the existing job",
        relative_path="src/letterstream_mcp/gate.py",
        find="        existing_proof = self.store.find_proof_by_idempotency_key(key)\n        if existing_proof is not None:",
        replace="        existing_proof = None  # MUTATION: forget prior submissions\n        if False:",
        expect_failures=[
            "tests/test_idempotency.py::test_identical_resubmit_reuses_the_existing_proof",
            "tests/test_idempotency.py::test_an_explicit_idempotency_key_collides_across_differing_requests",
        ],
    ),
    Mutation(
        identifier="constant-idempotency-key",
        property_broken="the idempotency key distinguishes different letters",
        relative_path="src/letterstream_mcp/models.py",
        find="        return self.idempotency_key or self.fingerprint()",
        replace='        return self.idempotency_key or "constant"  # MUTATION',
        expect_failures=[
            "tests/test_idempotency.py::test_a_genuinely_different_letter_is_not_deduplicated",
            "tests/test_idempotency.py::test_the_derived_key_changes_when_the_document_changes",
            "tests/test_idempotency.py::test_the_derived_key_changes_when_a_recipient_changes",
        ],
    ),
    Mutation(
        identifier="stale-proof-accepted",
        property_broken="a stale proof cannot be authorised",
        relative_path="src/letterstream_mcp/gate.py",
        find="        if age > self.config.proof_ttl_seconds:",
        replace="        if False:  # MUTATION: proofs never expire",
        expect_failures=["tests/test_authorize.py::test_stale_proof_is_refused"],
    ),
    Mutation(
        identifier="unconfirmed-release-retried",
        property_broken="an unconfirmed release is never retried",
        relative_path="src/letterstream_mcp/gate.py",
        find="        if proof.release_attempted_at is not None:",
        replace="        if False:  # MUTATION: retry a release of unknown outcome",
        expect_failures=[
            "tests/test_authorize.py::test_a_release_that_never_reported_back_is_not_retried",
            "tests/test_concurrency.py::test_a_crash_between_claiming_and_recording_blocks_every_later_caller",
        ],
    ),
    Mutation(
        identifier="authorize-not-serialised",
        property_broken="concurrent authorize of one proof mails once",
        relative_path="src/letterstream_mcp/gate.py",
        find=(
            "        with self.store.exclusive(\n"
            "            proof_lock_name(proof_id), timeout=self.lock_timeout_seconds\n"
            "        ):\n"
            "            return self._authorize_locked(\n"
            "                proof_id,\n"
            "                document_sha256=document_sha256,\n"
            "                acknowledge_cost_usd=acknowledge_cost_usd,\n"
            "            )\n"
        ),
        replace=(
            "        return self._authorize_locked(  # MUTATION: no lock, check-then-set\n"
            "            proof_id,\n"
            "            document_sha256=document_sha256,\n"
            "            acknowledge_cost_usd=acknowledge_cost_usd,\n"
            "        )\n"
        ),
        expect_failures=[
            "tests/test_concurrency.py::test_concurrent_authorize_on_one_proof_releases_exactly_once",
            "tests/test_concurrency.py::test_the_losers_of_an_authorize_race_get_the_already_authorized_answer",
            "tests/test_concurrency.py::test_a_concurrent_authorize_whose_release_fails_is_not_retried_by_the_losers",
            "tests/test_concurrency.py::test_a_caller_that_cannot_get_the_lock_refuses_rather_than_mailing",
            "tests/test_concurrency.py::test_two_processes_authorizing_one_proof_release_it_once",
        ],
    ),
    Mutation(
        identifier="submit-not-serialised",
        property_broken="concurrent identical submits create one held job",
        relative_path="src/letterstream_mcp/gate.py",
        find=(
            "        with self.store.exclusive(\n"
            "            submit_lock_name(key), timeout=self.lock_timeout_seconds\n"
            "        ):\n"
            "            return self._submit_locked(request, key, document_hash, preview)\n"
        ),
        replace=(
            "        return self._submit_locked(  # MUTATION: no lock on the key\n"
            "            request, key, document_hash, preview\n"
            "        )\n"
        ),
        expect_failures=[
            "tests/test_concurrency.py::test_concurrent_identical_submits_create_exactly_one_held_job",
        ],
    ),
    Mutation(
        identifier="lock-excludes-nobody",
        property_broken="the per-key lock is actually shared between callers",
        relative_path="src/letterstream_mcp/store.py",
        find='        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]',
        replace="        digest = uuid.uuid4().hex  # MUTATION: a private lock nobody contends",
        expect_failures=[
            "tests/test_concurrency.py::test_concurrent_authorize_on_one_proof_releases_exactly_once",
            "tests/test_concurrency.py::test_the_losers_of_an_authorize_race_get_the_already_authorized_answer",
            "tests/test_concurrency.py::test_a_concurrent_authorize_whose_release_fails_is_not_retried_by_the_losers",
            "tests/test_concurrency.py::test_a_caller_that_cannot_get_the_lock_refuses_rather_than_mailing",
            "tests/test_concurrency.py::test_concurrent_identical_submits_create_exactly_one_held_job",
            "tests/test_concurrency.py::test_two_processes_authorizing_one_proof_release_it_once",
        ],
    ),
    Mutation(
        identifier="lock-timeout-fails-open",
        property_broken="a caller that cannot get the lock refuses instead of proceeding",
        relative_path="src/letterstream_mcp/store.py",
        find=(
            "        with _lock_for(self.lock_path(name)).hold(timeout, label=name):\n"
            "            yield\n"
        ),
        replace=(
            "        try:  # MUTATION: fail open on a contended lock\n"
            "            with _lock_for(self.lock_path(name)).hold(timeout, label=name):\n"
            "                yield\n"
            "        except LockTimeout:\n"
            "            yield\n"
        ),
        expect_failures=[
            "tests/test_concurrency.py::test_a_caller_that_cannot_get_the_lock_refuses_rather_than_mailing",
        ],
    ),
    Mutation(
        identifier="ledger-writes-not-serialised",
        property_broken="a ledger write cannot lose another caller's record",
        relative_path="src/letterstream_mcp/store.py",
        find=(
            "        with self._ledger(path):\n"
            "            data = _read_json(path)\n"
            "            mutate(data)\n"
            "            _atomic_write_json(path, data)\n"
        ),
        replace=(
            "        data = _read_json(path)  # MUTATION: unlocked read-modify-write\n"
            "        mutate(data)\n"
            "        _atomic_write_json(path, data)\n"
        ),
        expect_failures=[
            "tests/test_concurrency.py::test_concurrent_submits_of_different_letters_all_survive_in_the_ledger",
        ],
    ),
    Mutation(
        identifier="lock-path-not-canonical",
        property_broken="one state directory maps to one lock, however it is spelled",
        relative_path="src/letterstream_mcp/store.py",
        find='        return self.locks_dir.resolve() / f"{digest}.lock"',
        replace='        return self.locks_dir / f"{digest}.lock"  # MUTATION: not canonical',
        expect_failures=[
            "tests/test_concurrency.py::test_separate_gate_objects_over_one_state_directory_share_the_lock",
        ],
    ),
    Mutation(
        identifier="shared-temp-file-name",
        property_broken="two atomic writers of one file do not collide",
        relative_path="src/letterstream_mcp/store.py",
        find='    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")',
        replace='    temp = path.with_suffix(path.suffix + ".tmp")  # MUTATION: shared name',
        expect_failures=[
            "tests/test_concurrency.py::test_two_writers_of_one_ledger_file_do_not_collide_on_a_temp_name",
        ],
    ),
    Mutation(
        identifier="preauth-not-forced",
        property_broken="a submission is always a held job",
        relative_path="src/letterstream_mcp/transport.py",
        find="        outgoing[PREAUTH_FIELD] = PREAUTH_VALUE",
        replace="        outgoing.setdefault(PREAUTH_FIELD, PREAUTH_VALUE)  # MUTATION",
        expect_failures=[
            "tests/test_submit_never_mails.py::test_transport_forces_preauth_even_when_caller_says_otherwise",
        ],
    ),
    Mutation(
        identifier="credentials-fall-back-to-a-default",
        property_broken="missing credentials fail loudly, never silently",
        relative_path="src/letterstream_mcp/config.py",
        find="        raise CredentialsError(_missing_credentials_message(path))",
        replace='        resolved_id = resolved_id or "fallback-id"  # MUTATION\n        resolved_key = resolved_key or "fallback-key"',
        expect_failures=[
            "tests/test_credentials.py::test_missing_credentials_raise_a_clear_error",
            "tests/test_credentials.py::test_cli_reports_missing_credentials_without_a_traceback",
            "tests/test_credentials.py::test_blank_values_in_the_example_config_count_as_absent",
        ],
    ),
    Mutation(
        identifier="authcode-leaks-to-callers",
        property_broken="the authcode never leaves the ledger",
        relative_path="src/letterstream_mcp/models.py",
        find='        data.pop("authcode", None)',
        replace="        pass  # MUTATION: leak the authcode to callers",
        expect_failures=[
            "tests/test_submit_never_mails.py::test_submit_response_does_not_contain_the_authcode",
        ],
    ),
    Mutation(
        identifier="cost-not-surfaced",
        property_broken="cost is surfaced before authorisation",
        relative_path="src/letterstream_mcp/gate.py",
        find="        cost = response_cost(parsed)",
        replace="        cost = None  # MUTATION: hide the price",
        expect_failures=[
            "tests/test_cost.py::test_submit_surfaces_the_quoted_cost",
            "tests/test_cost.py::test_charged_cost_matches_the_quote",
            "tests/test_cost.py::test_cost_is_known_before_any_release_call",
        ],
    ),
    Mutation(
        identifier="cost-ceiling-ignored",
        property_broken="the configured cost ceiling blocks authorisation",
        relative_path="src/letterstream_mcp/gate.py",
        find="        if ceiling is not None and proof.cost_usd is not None and proof.cost_usd > ceiling:",
        replace="        if False:  # MUTATION: ignore the ceiling",
        expect_failures=["tests/test_authorize.py::test_cost_ceiling_blocks_authorization"],
    ),
    Mutation(
        identifier="address-delimiter-not-validated",
        property_broken="a delimiter in an address field is rejected",
        relative_path="src/letterstream_mcp/models.py",
        find="    for bad in _FORBIDDEN_IN_FIELD:\n        if bad in text:",
        replace="    for bad in _FORBIDDEN_IN_FIELD:\n        if False:  # MUTATION",
        expect_failures=[
            "tests/test_validation.py::test_a_delimiter_in_an_address_field_is_rejected",
            "tests/test_validation.py::test_a_pipe_in_an_address_field_is_rejected",
        ],
    ),
]

FAILURE_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)


def run_pytest(root: Path) -> tuple[int, set[str], str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    failed = set()
    for match in FAILURE_LINE.finditer(output):
        nodeid = match.group(1).split(" ")[0]
        # Drop the parametrisation suffix so "test_x[case1]" matches "test_x".
        failed.add(re.sub(r"\[.*\]$", "", nodeid))
    return completed.returncode, failed, output


def check_baseline(verbose: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="lsmut-base-") as temp:
        root = Path(temp) / "repo"
        shutil.copytree(REPO_ROOT, root, ignore=EXCLUDE)
        code, failed, output = run_pytest(root)
    if code != 0:
        if verbose:
            print(output)
        raise SystemExit(
            "Baseline suite does not pass on an unmutated copy; fix that before "
            f"running mutations. Failing: {sorted(failed)}"
        )
    print("baseline: unmutated copy passes\n")


def run_mutation(mutation: Mutation, verbose: bool) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="lsmut-") as temp:
        root = Path(temp) / "repo"
        shutil.copytree(REPO_ROOT, root, ignore=EXCLUDE)
        mutation.apply(root)
        code, failed, output = run_pytest(root)

    expected = set(mutation.expect_failures)
    caught = sorted(expected & failed)
    missed = sorted(expected - failed)
    collateral = sorted(failed - expected)

    result = {
        "id": mutation.identifier,
        "property": mutation.property_broken,
        # Detected only if EVERY test that names this property broke. `bool(caught)`
        # alone would let a mutation count as caught on one incidental failure while
        # the test that actually holds the property had quietly stopped holding it.
        "detected": bool(caught) and not missed,
        "expected_failures_seen": caught,
        "expected_failures_missing": missed,
        "other_tests_that_also_failed": collateral,
        "pytest_exit_code": code,
    }
    if verbose and not caught:
        print(output)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List mutations and exit.")
    parser.add_argument("-k", dest="pattern", help="Only run mutations whose id contains this.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable results.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print pytest output on miss.")
    args = parser.parse_args(argv)

    selected = [
        m for m in MUTATIONS if not args.pattern or args.pattern in m.identifier
    ]
    if args.list:
        for mutation in selected:
            print(f"{mutation.identifier:42s} {mutation.property_broken}")
        return 0
    if not selected:
        print(f"No mutation ids match {args.pattern!r}.")
        return 1

    check_baseline(args.verbose)

    results = []
    for mutation in selected:
        outcome = run_mutation(mutation, args.verbose)
        results.append(outcome)
        if not args.json:
            status = "CAUGHT " if outcome["detected"] else "MISSED!"
            print(f"{status} {mutation.identifier}")
            print(f"        property: {mutation.property_broken}")
            for name in outcome["expected_failures_seen"]:
                print(f"        failing test: {name}")
            for name in outcome["expected_failures_missing"]:
                print(f"        NOT failing (expected to): {name}")
            print()

    missed = [r for r in results if not r["detected"]]
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(f"{len(results) - len(missed)}/{len(results)} mutations caught by a named test.")
        if missed:
            print("Unprotected properties:")
            for record in missed:
                print(f"  - {record['property']} ({record['id']})")
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
