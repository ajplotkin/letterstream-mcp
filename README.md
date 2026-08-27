# letterstream-mcp

> **Unofficial and unaffiliated third-party client.** This is not an official
> LetterStream SDK. It is not produced, endorsed, supported, or reviewed by
> LetterStream, and it uses no LetterStream logo or brand asset. LetterStream
> confirmed by email on 27 August 2026 that customers may publish open-source
> clients for their API; the exchange and its conditions are recorded under
> [LetterStream's terms](#letterstreams-terms).

An MCP server for [LetterStream](https://www.letterstream.com/)'s mailing API —
certified mail, first class, and the other mail types they support. Mailing
takes two separate calls rather than one, because a letter cannot be recalled
once it has been released.

The reason for the split is specific. A mailing spends money, puts a physical
object in the postal system, and can start a statutory clock, and there is no
endpoint that undoes any of that. A single `send()` gives a caller no point at
which a wrong address or a retry after a timeout can be caught, so this server
does not have one: `submit` creates a job LetterStream holds, and a separate
`authorize` releases it.

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

One more boundary, because *What has been verified against the live service*
further down reports the live testing and this is not part of it: the races in point
5 are run in process against a fake transport. No concurrent call has been made
to LetterStream. The serialisation claim rests on the suite and the mutation
harness, not on live evidence.

### A failure the gate cannot catch: the envelope window

Worth reading even if you skip the rest of this page, because it has actually
happened and nothing in this repository would have prevented it.

On a real mailing, an entire batch of certified pieces was submitted with
`coversheet` set to `"N"`, on the reasoning that the PDFs already carried the
recipient's address laid out on the page. The whole batch was delivered to the
wrong address — the same wrong address for every piece, in a different state
from most of the intended recipients. LetterStream support confirmed the cause:
an address on the PDF fell in the window area of their windowed envelopes, and
USPS read what showed through the window rather than the address supplied
through the API.

The fix in that case was `coversheet = "Y"`, which has LetterStream generate
their own addressed coversheet so the window shows the address the API was
given. `"Y"` is this package's default. It addresses this specific failure mode;
it is not a general guarantee that a piece arrives where you intended.

Now note what the gate did and did not do. The job was submitted, held,
reviewed and authorised correctly. The addresses carried through the API were
right. The cost was right. The tracking numbers were right. The properties this
repository argues for held — and the mail still went to the wrong state. The
gate's promise is that nothing is mailed that a human did not approve. It says
nothing about whether the approved thing is correct once it leaves. A proof PDF
shows you the page; it does not show you which part of the page will be visible
through an envelope.

So: if you set `coversheet` to `"N"`, you are taking responsibility for the
window area of every page yourself, and no check in this codebase is watching.

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
  does — see the boundary above, which is not a formality. The sequential half
  of this has also been observed against the live service; the simultaneous half
  has not. Both are separated out in the next section.
- **Proofs go stale.** Past a configurable TTL (default 24h), `authorize`
  refuses and you must submit and re-review.
- **Credentials come from you.** There is no key in this repository, no default
  account, and no fallback. Missing credentials produce a paragraph explaining
  where the code looked, and exit code 2 — not a traceback.

## What has been verified against the live service

On 22 August 2026 the CLI was run in live mode against a real LetterStream
account with real credentials. Two jobs were submitted and released: one page
and one recipient each, both to the same real postal address, one **certified**
(quoted $11.01) and one **first class** (quoted $1.19). Two letters, one address,
one session; every item below is bounded by that. A second session on 27 August
verified the read-only tools and recorded one confirmed delivery, in its own
subsection below.

- **Authentication works.** The auth digest in this repository was written from
  LetterStream's published documentation rather than copied from an existing
  client, and the service accepted it (`AUTHOK`).
- **`submit` creates a held job that does not mail.** This is the one that
  previously could not be checked from inside the code. After `submit` returned,
  a human opened LetterStream's web UI and confirmed the job was sitting there
  unreleased. LetterStream honoured the pre-authorisation flag.
- **A tracking number exists before authorisation.** The certified job carried a
  tracking number while it was still held and unmailed. LetterStream issues it
  as a USPS tracking number; no USPS lookup has been run against one, so that
  it is USPS-recognised is LetterStream's representation, not a verified fact
  here. So the number
  a USPS notification subscription needs is in hand during the review window,
  before anything is committed to the mail stream. Subscribing is still not
  something this project does — see *What it does not do* — but nothing has to
  be mailed first to obtain the number.
- **The quote is the charge.** On both jobs the amount `authorize` reported as
  charged equalled the amount `submit` quoted, and `cost_matches_quote` came
  back true.
- **`authorize` releases the job.** `mailed: true`, and LetterStream returned a
  success code.
- **Authorising twice does not mail twice or charge twice.** A second
  `authorize` on the same proof returned `mailed: false`,
  `already_authorized: true`, and the originally recorded response carrying its
  original timestamp. No second release and no second charge. This had been
  demonstrated only against the fake transport; the sequential case now holds
  against the live service too.
- **`mail_type` is genuinely configurable.** Certified and first class both went
  through, and priced differently.
- **A different document is not deduplicated.** The second submission reported
  `reused_existing_submission: false` rather than handing back the first job's
  proof.

### What that session did not establish

- **Concurrency was not exercised live.** The claims on this page about two
  callers racing — the release lock, the submit lock, six threads, four
  processes — rest on the test suite and the mutation harness, in process,
  against a fake transport. No concurrent call was made to LetterStream.
- **`interpret_job_status` is still fixtures-only.** It has one call site, on
  the reconciliation path: a resubmission after a previous attempt failed to
  report back. No such failure was provoked, so that path was never entered.
  Its docstring says the same thing.
- **The document-hash check was not verified live.** An attempt was made and did
  not test what it appeared to test: it ran against a proof that had already
  been authorised, so the `already_authorized` branch answered first and the
  hash was never compared — the right answer for the wrong reason. A live run
  would add nothing here in any case. That check is purely local: `authorize`
  re-hashes the file on disk and compares before any transport call is made, so
  no service behaviour is involved. That is what distinguishes it from the
  pre-authorisation hold, where the thing in question was LetterStream's
  behaviour rather than this code's. It is covered by `test_tampering.py` and by
  two of the mutations.
- **Nothing else changed.** The session added no capability. Everything under
  *What it does not do* is still not done.

### A second live session — 27 August 2026

The first session left every read-only tool that calls the live service
unverified, because nothing had exercised them. All three were run on 27 August 2026 against a job that was
submitted and deliberately never released. That session mailed nothing, and no charge was
recorded: the ledger showed `authorized_at: None` and `charged_cost_usd: None`
throughout, and `authorize` was never called. The ledger records what
`authorize` reports, and `authorize` never ran — so that is the absence of a
recorded charge, not an account-side confirmation that none occurred.

- **The certified letter was delivered.** The certified piece released on
  22 August arrived at the address it was addressed to. Delivery confirmed by
  physical receipt, for one piece, to an address the sender controls. Nothing
  here establishes anything about the first-class piece, which carries no
  tracking, or about delivery times in general.
- **`letterstream_account_status` authenticates.** It returned `AUTHOK`. Note
  what it did *not* return: no balance, no quota, no funding state, no account
  detail of any kind. The tool works; the name promises more than the response
  carries. Do not build a pre-flight funding check on it.
- **`letterstream_tracking` answers for a job that is still held.** Run against
  an unreleased proof it returned a document status for the held doc —
  `status: "Needs Attention"`, `history: "PreAuth - ..."`. So the endpoint does
  not require a released job, and this held job surfaced in LetterStream as
  *Needs Attention*. **But read what came back:** that is LetterStream's own
  document status, not USPS scan events. Whether the `history` field carries
  USPS scans once a piece is released and delivered is **still unverified** —
  the delivered letter's proof had already been deleted from the ledger, so it
  could not be queried. The tool descriptions used to say "USPS tracking";
  they have been narrowed to LetterStream's tracking record.
- **`letterstream_download_proof_pdfs` returns a print-proof PDF.** For the
  held job it fetched a 43 KB PDF with three page objects, whose extracted text
  contained the submitted document's text and matched /certif/i. What the two
  extra pages are was not established, and the page count is a marker count
  rather than a rendered count. The proof document can be read while the job is
  still held, and only then authorized. Whether it matches what is physically
  printed is untested: no downloaded proof has been compared against a delivered
  piece.

- **A read-only lookup reported success over a failure — now fixed.** After the
  held job was deleted at LetterStream, `letterstream_tracking` was run against
  the same proof. It returned `ok: true` while the payload underneath carried
  `-924 invalid doc id` and `-999 could not retrieve that info`. The cause was
  that `raise_for_api_error` was wired into `submit_preauth` and `release` only;
  the read path never checked. Nothing could mail or charge through this — but a
  caller reading `ok` would have concluded the lookup succeeded. `tracking` and
  `account_status` now raise on an error response, covered by the read-only
  error suite and the nine mutations against `client.py`. `job_status`
  deliberately still does not raise:
  `interpret_job_status` reads non-success codes to decide a job is absent, and
  raising there would make a legitimate resubmission unreachable.

Also confirmed incidentally: a tracking number is issued at submit time on a
second, independent certified job — so that finding is no longer a single
observation.

## What it does not do

- **It has been run against the live LetterStream API in two sessions only** —
  the two letters of 22 August and the single held job of 27 August, both
  described above. The test suite itself never touches the
  network: every test runs against a fake transport, and the suite blocks
  in-process socket connections so that stays true. The request shapes are built
  from LetterStream's published integration documentation, and those sessions
  confirm the submission, release, and read-only query shapes and nothing
  wider. In particular
  the function that decides whether a job already exists
  (`interpret_job_status`) is exercised only against fixtures — its docstring
  says so too.
- It does not batch-upload ZIP archives (LetterStream's other submission
  method). One PDF, one job, one or more recipients.
- It does not do USPS notification subscriptions, address validation, or
  pre-flight envelope checking. For certified mail it does surface the tracking
  number a subscription would need, at submit time — see the `per_doc` note
  under *MCP tool surface* — but subscribing is left to you.
- It does not cancel or recall a released job. Nothing here can.
- It does not manage LetterStream account funding or test mode. Those are web
  UI settings on your account, and they are an additional layer of protection
  this code does not control and cannot see.
- The "held, not mailed" half of the gate depends on LetterStream honouring the
  pre-authorisation flag on their side. This project guarantees the flag is
  always sent and never overridable by a caller, and one live submission was
  confirmed held and unmailed in LetterStream's web UI — so the dependency has
  been checked once rather than assumed. What the code still cannot do is see
  that for itself: it reads LetterStream's response, not their production queue,
  so it could not detect a job that was mailed despite the flag.
- It does not inspect the layout of your PDF, and in particular does not check
  whether an address falls in the window area of LetterStream's envelopes. That
  has caused a whole batch to be delivered to one wrong address on a real
  mailing, with the gate raising no objection at any point — see *A failure the
  gate cannot catch: the envelope window*.
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

### Setup, start to finish

You need your own LetterStream API credentials. This project ships none, has no
default account, and cannot obtain them for you.

1. **Create a LetterStream account, or sign in to one.** Their public site is
   [letterstream.com](https://www.letterstream.com/).

2. **Ask LetterStream to enable API access.** It is not on by default. Their
   published help material describes a review-and-approval step before API
   access is switched on for an account, and says the API documentation and
   sample code only become available inside the account after that. Expect a
   round trip, not an instant self-service key. Their [API
   page](https://www.letterstream.com/api/) is where that request starts.

3. **Find your API ID and API key under *My Account → API Information*.**
   LetterStream's publicly readable help pages do not document this path, so if
   the interface has moved, the interface is right and this line is out of date.

4. **Copy the example config and fill in the two credentials.**

   ```bash
   cp config.example.toml config.toml
   ```

   Set `api_id` and `api_key` in the `[credentials]` section. On a shared
   machine, export `LETTERSTREAM_API_ID` and `LETTERSTREAM_API_KEY` instead, so
   the key never lands in a file or in shell history.

5. **Leave `live = false` for now.** That is how the example ships. In this
   state no request of any kind reaches LetterStream: `submit` returns a local
   preview and `authorize` refuses outright.

6. **Check the configuration.**

   ```bash
   letterstream-mcp check-config
   ```

   It reports whether credentials were found, which mode you are in, and
   whether cross-process locking is available. It never prints the key.

7. **Do a dry run first.** Run a real `submit` with `live = false` and read the
   preview — the sender and recipient block, the document hash, the recipient
   count. This costs nothing and touches nothing.

8. **Only then set `live = true`.** Edit `[safety] live` in `config.toml`, or
   set `LETTERSTREAM_LIVE=true`. This is deliberately a separate, manual step:
   there is no `--live` flag and no MCP tool parameter that can flip it, so
   neither an agent nor a mistyped command can turn dry run into live mode.
   Turning it on still does not mail anything by itself — `submit` creates a
   held job, and `authorize` is what releases it.

`config.toml` is in `.gitignore`. Every credential, path and tuning value in
`config.example.toml` is blank and documented; the one value that is filled in is
`live = false`. Copying the example unedited fails with the missing-credentials
message rather than half-working — there is a test for that.

### Where settings come from

Resolution order, highest priority first:

1. CLI flags (`--api-id`, `--api-key`, `--base-url`, `--state-dir`, `--timeout`)
2. Environment (`LETTERSTREAM_API_ID`, `LETTERSTREAM_API_KEY`, `LETTERSTREAM_LIVE`,
   `LETTERSTREAM_BASE_URL`, `LETTERSTREAM_STATE_DIR`, `LETTERSTREAM_TIMEOUT`,
   `LETTERSTREAM_MCP_CONFIG`)
3. The config file — `--config PATH`, then `$LETTERSTREAM_MCP_CONFIG`, then
   `./config.toml`, then `~/.config/letterstream-mcp/config.toml`

`live` is the exception: it is settable only in the config file or via
`LETTERSTREAM_LIVE`, as in step 8 above.

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

Recipients below are synthetic. The costs in the responses come from the test
fixtures and the document hashes are invented placeholders; neither comes from
either live session described above. Start in dry run, which is where you already are, because live mode is
off by default.

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

Once you have read the proof and are satisfied with it, authorize:

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

Eight tools. `letterstream_authorize` is the only one that mails.

| Tool | Parameters | Returns |
|---|---|---|
| `letterstream_check_config` | none | `{ok, configured, live, mode, config, state_dir, cross_process_locking, tools}` — `config` reports `api_key_present` and `api_key_length`, never the key; `cross_process_locking` says whether the release lock extends past this process |
| `letterstream_account_status` | none | `{ok, account_status}` — LetterStream's parsed account-status response. Observed live to carry an auth confirmation only, with no balance or funding data. Read-only. Refuses in dry run |
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

`coversheet` defaults to `"Y"`. Before you set it to `"N"`, read *A failure the
gate cannot catch: the envelope window* above — that setting has a documented
route to delivering mail to the wrong address, and nothing here validates it.

The `proof` object returned by `letterstream_submit` — and by
`letterstream_get_proof` and `letterstream_list_proofs` — carries `per_doc`: one
entry per recipient copy, parsed out of LetterStream's response. An entry has
`id`, the per-document identifier that `letterstream_download_proof_pdfs` and
`letterstream_tracking` are keyed on, and may also carry `job`, `cost` and
`tracking`. **`tracking` is not always there.** In the first live session the
certified job's entry carried a tracking number and the first-class job's entry
had no `tracking` key at all, so a consumer must read it with `.get()` and
handle its absence rather than indexing into it. Where it is present it is
present at submit time, while the job is still held, so something outside this
project can attempt a USPS notification subscription with it before `authorize` is ever
called.

Refusals come back as `{"ok": false, "mailed": false, "error_type": ..., "error": ...}`.
Only errors this package raises on purpose are converted that way; a genuine
defect still raises, rather than being disguised as a polite refusal.

## Tests

```bash
pytest -v
```

139 tests, no network. An autouse fixture patches the socket entry points
`requests` reaches for — `socket.create_connection`, `socket.socket.connect` and
`socket.socket.connect_ex` — and raises if a test tries to connect; one test
deliberately trips it, so the block is verified rather than assumed. Every
transport in the suite is a fake, and the configs backing them point at a
`.invalid` base URL, a reserved TLD that does not resolve. Two tests spawn child
processes, and an autouse fixture cannot reach those. The release race's children
install the same socket block themselves and drive the same fake transport; the
CLI check runs with no credentials and exits before any transport is constructed,
so there is nothing for it to connect to.

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
| `test_repo_hygiene.py` | no absolute home paths, no credential-shaped literals, blank example config, correct `.gitignore`, and neither `config.toml` nor `state/` committed |

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

34 mutations, all currently caught. In full: make `submit` call `release`; make
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
stop validating address delimiters; ignore error messages in an XML lookup
response; skip a malformed message entry; mask an account-status error behind a leading AUTHOK; accept a message entry
whose `type` slot is
unreadable; accept an unreadable lookup body; accept a
lookup body with no readable message list; let a read-only lookup report success over
an error payload; have that check read only the first message so an error
behind a leading `AUTHOK` slips through; skip the error check on account
status. The script exits non-zero if any mutation
survives, and refuses to run if the unmutated copy does not pass first.

## LetterStream's terms

Reported as findings, not as legal advice.

**LetterStream was asked directly, and answered.** On 22 August 2026 the author
emailed LetterStream support three questions: whether customers may publish
open-source clients for the API, whether API-specific terms exist beyond the
website Terms of Service, and whether any attribution or naming requirements
apply. The reply, received 27 August 2026, was:

1. Yes — they are happy for customers to publish open-source clients.
2. There are currently no API-specific terms beyond the standard ToS.
3. Two conditions: do not use their logo or imply official endorsement, and
   include a clear "unofficial, unaffiliated third-party client" statement in
   the README. Naming in the style of `unofficial-letterstream-client` is fine;
   names implying an official SDK are not.

This repository meets those conditions. The disclaimer is the first thing in
this file; no LetterStream logo, brand asset, or image of any kind appears
anywhere in the repository; and nothing here claims official status. The name
`letterstream-mcp` follows the ordinary `<service>-mcp` convention for MCP
servers and asserts no official standing, which is the distinction condition 3
draws.

Two honest qualifications. That reply is a support-desk email, not a signed
licence grant, and it is recorded here as what was said rather than as a
permission that binds. And "currently no API-specific terms" is a statement
about the present; if you deploy this, read whatever terms accompany your own
API grant.

The published Terms of Service remains silent on APIs. It contains no developer
terms, no API licence grant, no rate-limit clause, no anti-automation clause,
and no clause about third-party clients, as published. The one adjacent clause
is a
proprietary-rights paragraph restricting modification, distribution, and
derivative works of "the Software", which it defines as the website and the
software used in connection with it. Whether that definition reaches the HTTP
API is ambiguous on the face of the document; the answer to question 2 is what
resolves it in practice, not a reading of that paragraph.

No LetterStream documentation text, sample code, or proprietary material is
reproduced in this repository. The request shapes here were written from scratch
against a description of the protocol.

## How this was built

Written with Claude. The safety properties are not asserted on that basis — they
are the ones `tools/mutation_test.py` breaks on purpose, each tied to a named
test that fails when it does. Run it; that is what the claim rests on.

## Licence

Apache License 2.0. See [LICENSE](LICENSE).
