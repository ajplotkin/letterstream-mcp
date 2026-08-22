# letterstream-mcp

An MCP server for [LetterStream](https://www.letterstream.com/)'s certified-mail
API, built around a question that matters more than the API client: **how do you
let an agent take an irreversible real-world action safely?**

Sending certified mail is a good test case because it is unusually
unforgiving. It spends money. It puts a physical object in the postal system.
The document can start a statutory clock. There is no undo, no `DELETE`
endpoint, no support ticket that unsends a letter. If an agent gets a normal
`send()` tool, then a hallucinated address, a stale draft, a retried call after
a timeout, or a misread instruction all resolve the same way: something gets
mailed.

So this server has no `send()`.

## The gate

Mailing is split into two calls that cannot be collapsed into one.

```
submit  ──▶  LetterStream holds the job, quotes a price, returns a PROOF
             (proof id + cost + SHA-256 of the exact bytes uploaded)
             ── nothing has been mailed ──

             [ a human reads the proof PDF and the price ]

authorize ──▶ takes the proof id and the hash the caller approved,
              re-reads the document from disk, re-hashes it,
              and releases the job only if everything still matches
```

Five things make that a gate rather than a two-step convenience:

**1. `submit` calls a different transport method than `authorize` does.**
The transport exposes `submit_preauth`, `release`, and `query` as three named
methods rather than one generic `post()`. `release` is the only one that sends
LetterStream the field that puts a job into production. `MailGate.submit` calls
`submit_preauth` and — on a retry — `query`. It never calls `release`. A test
asserts on the fake transport's `release_calls` list, and a second test reads
`MailGate.submit`'s own source and asserts it contains no `.release(` call.

**2. A submission is always a held job.** `HttpTransport.submit_preauth` sets
the pre-authorisation flag *after* merging the caller's fields, so a caller
cannot overwrite it. A test drives the real `HttpTransport` against a fake
`requests` module, passes a contradicting value, and checks the form payload
that would actually go out.

**3. The release credential never leaves the ledger.** LetterStream returns an
authcode when it holds a job; that authcode is what releases the mail. It is
stored locally and stripped from every return value. An agent holding a `submit`
result cannot assemble a release request from it — the only route is back
through `authorize`, which re-checks the hash.

**4. The proof is bound to content, not to intent.** `authorize` requires the
caller to restate the document hash *and* re-hashes the file on disk. Editing
the PDF between submit and authorize invalidates the proof. Restoring the exact
bytes makes it valid again, which is the check that the binding is on content
rather than on some proxy like mtime.

**5. Two callers cannot both release one proof.** A gate that races is not a
gate. `authorize` is a check-then-act — it reads that a proof has not been
released, then releases it — and this server ships inside FastMCP, which
dispatches synchronous tools on a worker-thread pool, so two overlapping tool
calls genuinely run it at the same time. That used to be enough to double-mail:
six threads authorising one valid proof produced six `release` calls. Now
`authorize` holds an exclusive lock on the proof id across the whole
read-claim-release-record sequence, and `submit` holds one on the idempotency
key. The lock is an in-process `threading.RLock` paired with `flock` on a file
under `state_dir/locks/`, so it holds between threads and between processes.
A caller that cannot obtain it within `timeout_seconds + 60` raises
`LockTimeout` and mails nothing rather than proceeding. Each thread race runs
sixty independent times per test run, and the same property is checked again
across four separate OS processes. That "cannot" has a scope, and it is narrower
than "safe under concurrency" — spelled out under *Where the gate stops*, below.

### Where the gate stops

This is a guarantee about the tool surface, not about the process. Anything that
imports `LetterStreamClient` directly, or reads the proof ledger file and calls
`release` with the authcode it finds there, bypasses all of the above. The
design assumes the agent is constrained to the MCP tools; it is not a sandbox
and does not try to be one. Stated plainly so nobody mistakes it for one.

The serialisation in point 5 has its own boundary. Two scopes hold, and they are
enforced by different mechanisms: threads inside one process are excluded by a
`threading.RLock`, and separate processes sharing a state directory are excluded
by `flock`, which needs `fcntl`. `letterstream_check_config` reports
`cross_process_locking`, so you can see which of the two you have rather than
infer it — on a platform
without `fcntl` it is `false` and only the in-process half applies. What is
**not** established here: whether `flock` is honoured for a state directory on
a network filesystem. Some are, some are not; every test in this repository
runs against a local temporary directory, so that case is untested and
unclaimed. And, as above, nothing serialises a program that writes
`proofs.json` directly instead of calling these tools.

### The other safety properties

- **Dry run is the default.** With live mode off, no transport method is called
  at all — not `query`, not `submit_preauth`. `submit` returns a local preview
  and writes no proof, so flipping live mode on later cannot find an approval
  for a job LetterStream never held. Turning live mode on requires editing
  `config.toml` or setting `LETTERSTREAM_LIVE`; there is deliberately no
  `--live` flag and no MCP tool parameter for it, and a test walks the whole
  argument parser to confirm that.
- **Cost is surfaced before authorisation.** `submit` returns LetterStream's
  quote and a per-recipient breakdown while nothing has been mailed.
  `authorize` optionally takes `acknowledge_cost_usd` and refuses if it
  disagrees with the quote, and reports the charged amount alongside the quote
  rather than echoing the quote back.
- **Idempotency designed for the failure that actually happens.** The dangerous
  case is not a duplicate click; it is the network dropping *after* LetterStream
  accepted the job. An idempotency key is written to disk before the request
  goes out and is left marked in-flight if the response never arrives. A retry
  does not resubmit: it queries LetterStream for the job, and resubmits only on
  an unambiguous "absent". "Exists" blocks it, and so does "cannot tell" — an
  unreadable status response can never resolve towards creating a second job.
  The same key also serialises simultaneous submissions, so two identical
  submits that arrive together produce one held job rather than two, on the same
  terms as the release lock above.

  One consequence is worth stating, because it can surprise: an explicit
  `idempotency_key` means "this is the same mailing". Reusing one across two
  *different* documents returns the first document's proof, and authorising it
  mails the first document. That is the deduplication working, not a failure —
  nothing is ever double-mailed, and nothing is ever mailed that a proof did not
  approve, because the proof still binds to the bytes it was issued for. But if
  you want a second mailing, give it a different key, or omit the key and let it
  be derived from the document and recipients.
- **An unconfirmed release is not retried, by a later caller or a queued one.**
  A timestamp is written to the ledger before the release request goes out, and
  while the proof's lock is held. A later `authorize` that sees it without a
  completion refuses and hands the decision to a human rather than risking a
  second release; so does a caller that was waiting behind the one that died.
  That recovery deliberately rests on the ledger record rather than on the lock.
  A dead process's `flock` is dropped by the kernel immediately, so there is no
  stale lock to clean up and nothing to brick — and equally, no expiry that
  could hand the proof to a second caller and mail it twice.
- **Authorising twice mails once, sequentially or simultaneously.** The second
  call returns the recorded result and does not reach the transport. With six
  threads racing on a single proof, exactly one mails and the other five are
  told the proof was already authorised; the assertion is on the fake
  transport's `release_calls`, over sixty independent races per run. Within one
  process that holds unconditionally. Across processes it holds wherever `flock`
  does — see the boundary above, which is not a formality.
- **Proofs go stale.** Past a configurable TTL (default 24h), `authorize`
  refuses and you must submit and re-review.
- **Credentials come from you.** There is no key in this repository, no default
  account, and no fallback. Missing credentials produce a paragraph explaining
  where the code looked, and exit code 2 — not a traceback.

## What it does not do

- **It has never been run against the live LetterStream API by this project.**
  Every test runs against a fake transport, and the suite blocks in-process
  socket connections so that stays true. The request shapes are built from
  LetterStream's published integration documentation; whether the live service
  responds exactly as modelled is **unverified here**. In particular, the
  function that decides whether a job already exists (`interpret_job_status`)
  is exercised only against fixtures — its docstring says so too.
- It does not batch-upload ZIP archives (LetterStream's other submission
  method). One PDF, one job, one or more recipients.
- It does not do USPS notification subscriptions, address validation, or
  pre-flight envelope checking.
- It does not cancel or recall a released job. Nothing here can.
- It does not manage LetterStream account funding or test mode. Those are web
  UI settings on your account, and they are an additional layer of protection
  this code does not control and cannot see.
- The "held, not mailed" half of the gate ultimately depends on LetterStream
  honouring the pre-authorisation flag on their side. This project guarantees
  the flag is always sent and never overridable by a caller; it cannot verify
  what the service does with it, and has not tried.
- The proof ledger on disk is a local trust root. Anything able to write to
  `state/` can forge a proof, and anything able to read it holds the authcodes.
  Put it somewhere only you can write.

## Install

```bash
git clone https://github.com/YOUR-USERNAME/letterstream-mcp.git
cd letterstream-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e '.[mcp,dev]'
```

Python 3.11 or newer. The only runtime dependency is `requests`; `mcp` is needed
only to run the MCP server, and `pytest` only to run the tests.

## Configuration

You need your own LetterStream API credentials. LetterStream issues these after
you request and are granted API access on your account; this project ships none
and cannot obtain them for you.

Copy the example and fill in the blanks:

```bash
cp config.example.toml config.toml
```

`config.toml` is in `.gitignore`. Every credential, path and tuning value in
`config.example.toml` is blank and documented; the one value that is filled in is
`live = false`. Copying the example unedited fails with the missing-credentials
message rather than half-working — there is a test for that.

Resolution order, highest priority first:

1. CLI flags (`--api-id`, `--api-key`, `--base-url`, `--state-dir`, `--timeout`)
2. Environment (`LETTERSTREAM_API_ID`, `LETTERSTREAM_API_KEY`, `LETTERSTREAM_LIVE`,
   `LETTERSTREAM_BASE_URL`, `LETTERSTREAM_STATE_DIR`, `LETTERSTREAM_TIMEOUT`,
   `LETTERSTREAM_MCP_CONFIG`)
3. The config file — `--config PATH`, then `$LETTERSTREAM_MCP_CONFIG`, then
   `./config.toml`, then `~/.config/letterstream-mcp/config.toml`

`live` is the exception: it is settable only in the config file or via
`LETTERSTREAM_LIVE`. On a shared machine, prefer the environment for the key so
it never lands in a file or in shell history.

With nothing configured:

```
$ letterstream-mcp check-config
LetterStream API credentials are not configured.
Looked in: the --api-id/--api-key command line flags; the LETTERSTREAM_API_ID and
LETTERSTREAM_API_KEY environment variables; a config.toml (searched: config.toml,
~/.config/letterstream-mcp/config.toml).
To fix this: copy config.example.toml to config.toml and fill in
api_id and api_key from your own LetterStream account (My Account ->
API Information), or export LETTERSTREAM_API_ID and LETTERSTREAM_API_KEY.
This project ships no credentials and has no default account.
$ echo $?
2
```

### Running as an MCP server

```jsonc
{
  "mcpServers": {
    "letterstream": {
      "command": "letterstream-mcp-server",
      "args": ["--config", "/absolute/path/to/config.toml"]
    }
  }
}
```

The server prints its mode (`live` or `dry-run`) to stderr on startup. If
credentials are missing it exits 2 with the message above and registers no
tools.

## Worked example

Recipients below are synthetic. Figures shown in the responses are illustrative
— this project has not called the live LetterStream API, so the prices here come
from test fixtures, not from a real quote. Start in dry run, which is where you
already are, because live mode is off by default.

```bash
letterstream-mcp submit \
  --job-name DEMOJOB0001 \
  --document ./letter.pdf \
  --pages 1 \
  --mail-type certified \
  --sender '{"name_1":"Testcorp Holdings","address_1":"1 Example Plaza","city":"Faketown","state":"AZ","zip_code":"99999"}' \
  --recipient '{"doc_id":"DEMODOC0001","name_1":"Placeholder Bank NA","address_1":"2 Nowhere Road","city":"Faketown","state":"AZ","zip_code":"99999"}'
```

```jsonc
// abridged; the full response also carries "ok", "live" and "idempotency_key"
{
  "dry_run": true,
  "mailed": false,
  "proof_id": null,
  "document_sha256": "cfa3181c1ee36e8bce5e39f84959f4558ea7ba32c0e4539a8ab3c8ce8c716ec6",
  "cost_usd": null,
  "cost_note": "No cost is available in dry run. LetterStream quotes the price when a job is held, and no request was made.",
  "preview": {
    "sender": "Testcorp Holdings / 1 Example Plaza / Faketown, AZ 99999",
    "recipients": [
      { "doc_id": "DEMODOC0001", "address": "Placeholder Bank NA / 2 Nowhere Road / Faketown, AZ 99999" }
    ],
    "recipient_count": 1
  },
  "note": "Dry run: nothing was sent to LetterStream. Set [safety] live = true in config.toml, or LETTERSTREAM_LIVE=true, to create a held job."
}
```

Read the preview. Then turn live mode on in `config.toml` and run the same
command. This time LetterStream holds the job and quotes a price:

```jsonc
{
  "dry_run": false,
  "mailed": false,
  "proof_id": "prf_...",
  "document_sha256": "cfa3181c...",
  "cost_usd": 10.89,
  "cost_note": "Quoted by LetterStream for the held job. This is what authorize will charge.",
  "note": "Job is held at LetterStream and has not been mailed. Review the proof, then call authorize with this proof_id and document_sha256 to release it."
}
```

Download and read what will actually be printed:

```bash
letterstream-mcp download-proofs prf_... --out-dir ./proofs
```

Then, and only then:

```bash
letterstream-mcp authorize prf_... \
  --document-sha256 cfa3181c... \
  --acknowledge-cost 10.89
```

If you edited the PDF in between, that last command refuses:

```jsonc
{
  "ok": false,
  "mailed": false,
  "error_type": "ProofMismatch",
  "error": "/path/to/letter.pdf has changed since it was submitted.\n  approved: cfa3181c...\n  on disk : 9d21ab40...\nThe proof approved the earlier bytes, so it does not authorise these. Nothing was released. Submit the current document again and review the new proof."
}
```

Exit codes: `0` success, `1` the gate refused, `2` configuration problem,
`3` other error.

## MCP tool surface

Eight tools. Exactly one of them mails.

| Tool | Parameters | Returns |
|---|---|---|
| `letterstream_check_config` | none | `{ok, configured, live, mode, config, state_dir, cross_process_locking, tools}` — `config` reports `api_key_present` and `api_key_length`, never the key; `cross_process_locking` says whether the release lock extends past this process |
| `letterstream_account_status` | none | `{ok, account_status}` — LetterStream's parsed account/balance response. Read-only. Refuses in dry run |
| `letterstream_submit` | `job_name`, `document_path`, `pages`, `sender`, `recipients`, and optional `mail_type`, `coversheet`, `duplex`, `ink`, `return_envelope`, `idempotency_key` | `{ok, dry_run, live, mailed: false, proof_id, proof, document_sha256, cost_usd, cost_note, preview, note}`. **Never mails.** In dry run `proof_id` is `null` and no proof is written |
| `letterstream_list_proofs` | none | `{ok, proofs: [...]}` — ledger records, authcode stripped. Local only |
| `letterstream_get_proof` | `proof_id` | `{ok, proof}` — one record including `document_sha256`, authcode stripped. Local only |
| `letterstream_download_proof_pdfs` | `proof_id`, `out_dir` | `{ok, files: [paths]}` — LetterStream's print proofs. Read-only. Refuses in dry run |
| `letterstream_authorize` | `proof_id`, `document_sha256`, optional `acknowledge_cost_usd` | `{ok, mailed, already_authorized, proof_id, job_name, recipient_count, quoted_cost_usd, charged_cost_usd, cost_matches_quote, response_code, response, note}`. **This mails.** Refuses in dry run |
| `letterstream_tracking` | `proof_id` | `{ok, tracking: [{doc_id, tracking}]}` — read-only. Refuses in dry run |

`sender` is an object with `name_1`, `address_1`, `city`, `state`, `zip_code`
required and `name_2`, `address_2` optional. Each entry in `recipients` is the
same shape plus a required `doc_id`. Address fields may not contain `:` or `|`,
because those are LetterStream's address-string delimiters and a stray one would
silently shift every later field — the letter would still mail, just to a
mangled address.

Refusals come back as `{"ok": false, "mailed": false, "error_type": ..., "error": ...}`.
Only errors this package raises on purpose are converted that way; a genuine
defect still raises, rather than being disguised as a polite refusal.

## Tests

```bash
pytest -v
```

104 tests, no network. An autouse fixture patches the socket entry points
`requests` reaches for — `socket.create_connection`, `socket.socket.connect` and
`socket.socket.connect_ex` — and raises if a test tries to connect; one test
deliberately trips it, so the block is verified rather than assumed. Every
transport in the suite is a fake, and the configs backing them point at a
`.invalid` base URL, a reserved TLD that does not resolve. Two tests spawn child
processes (the CLI end-to-end check, and the cross-process release race); an
autouse fixture cannot reach those, so each child installs the same socket block
itself and drives the same fake transport.

The interesting tests are the safety ones:

| File | Property |
|---|---|
| `test_submit_never_mails.py` | `submit` reaches the transport but never `release`; the authcode does not appear in any return value; `preauth` cannot be overridden by a caller |
| `test_authorize.py` | a valid proof mails exactly once; a second `authorize` does not reach the transport; stale proofs, unknown proofs, cost ceilings and unconfirmed releases all refuse |
| `test_tampering.py` | a document edited after approval is rejected even when the caller replays the correct original hash; restoring the exact bytes makes the proof valid again |
| `test_dry_run.py` | no transport method is called under any call sequence, including repeated submits interleaved with attempted authorizes |
| `test_idempotency.py` | a retry after a mid-flight failure does not create a second job; ambiguity blocks the retry; a genuinely different letter is *not* deduplicated |
| `test_cost.py` | the quote is available before anything is released, and a charge differing from the quote is reported rather than hidden |
| `test_credentials.py` | missing credentials give a message and exit 2, with no traceback and no transport call; there is no `--live` flag anywhere in the parser |
| `test_concurrency.py` | six threads authorising one proof release it exactly once, and the other five are told it was already authorised; identical simultaneous submits create one held job; a crash between claiming a release and recording it refuses every caller queued behind it; a caller that cannot get the lock refuses rather than mailing; six independently built gates over one state directory (half of them via a symlink) release it once; four separate processes releasing one proof release it once |
| `test_repo_hygiene.py` | no absolute home paths, no credential-shaped literals, blank example config, correct `.gitignore` |

### Mutation testing

A passing suite proves nothing about a guard unless removing the guard makes it
fail. `tools/mutation_test.py` breaks each safety property on purpose — in a
temporary copy of the repository, never in the working tree — and checks that a
*named* test fails.

```bash
python tools/mutation_test.py          # run all
python tools/mutation_test.py --list   # list them
python tools/mutation_test.py -k dry_run
```

25 mutations, all currently caught. In full: make `submit` call `release`; make
`authorize` skip the on-disk re-hash; make `authorize` trust the caller's hash;
let dry run fall through to the transport; let dry run authorise; let a second
`authorize` release again; remove the retry reconciliation; let an
uninterpretable job status count as "absent"; forget prior submissions so an
identical resubmit creates a second job; make the idempotency key a constant;
make proofs never expire; retry an unconfirmed release; drop the lock around
`authorize`; drop the lock around `submit`; give every caller a private lock so
the lock excludes nobody; fail open instead of closed when the lock is
contended; stop canonicalising the lock path so one state directory maps to two
locks; do the ledger's read-modify-write unlocked; share one temporary filename
between atomic writers; let a caller override `preauth`; fall back to a
default credential; leak the authcode; hide the cost; ignore the cost ceiling;
stop validating address delimiters. The script exits non-zero if any mutation
survives, and refuses to run if the unmutated copy does not pass first.

## LetterStream's terms

Reported as findings, not as legal advice, and based only on publicly readable
pages — no API call was made to establish any of this.

LetterStream's published Terms of Service does not contain the word "API"
anywhere in the agreement body. It has no developer terms, no API licence grant,
no rate-limit clause, no anti-automation clause, and no clause about third-party
clients or competing tools. On the specific question of whether an independent
open-source API client is permitted, the published terms are **silent** — there
is no clause permitting it and none prohibiting it.

The one adjacent clause is a proprietary-rights paragraph restricting
modification, distribution and derivative works of "the Software", which it
defines as the website and software used in connection with it. Whether that
definition reaches the HTTP API is ambiguous on the face of the document and is
**not resolved here**.

LetterStream's own integration documentation is only released after account
approval. Whether separate API terms accompany it is **unverified** — that
material is behind an account wall and was not accessed for this project. That
is also the most likely place a redistribution restriction would live, so if you
deploy this, read the terms attached to your own API grant.

No LetterStream documentation text, sample code, or proprietary material is
reproduced in this repository. The request shapes here were written from scratch
against a description of the protocol.

## How this was built

Written with Claude. The safety properties are not asserted on that basis — they
are the ones `tools/mutation_test.py` breaks on purpose, each tied to a named
test that fails when it does. Run it; that is what the claim rests on.

## Licence

Apache License 2.0. See [LICENSE](LICENSE).
