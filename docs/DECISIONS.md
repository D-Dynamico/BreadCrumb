# Decisions

Each entry: the decision, why, and what we rejected. Add new entries at the bottom with
the next number and a date. If a decision is reversed, don't delete it; add a new entry
that supersedes it.

---

### D1. Headline is durability, not verification (2026-10-04)

**Decision.** The standout feature is crash-safe, resumable execution with no duplicate
side effects. Verification is a lean, built-in part of the system, not the pitch.

**Why.** Three options were weighed: verification-first, crash-safe resume, and
procedure memory (learning reusable routines). Crash-safe resume scored best on the
brief's criteria (especially reliability, engineering quality and how well it can be
explained live), had the lowest schedule risk, and is rarer among take-home submissions.
Verification-first overlapped heavily with the author's earlier projects. Procedure
memory was the riskiest to finish and is kept as the "what's next" story.

**Rejected.** Verification as the headline; procedure memory in scope.

### D2. Build our own sandbox company (2026-10-04)

**Why.** The brief forbids real credentials and third-party systems. Pure mocks hide
failure. Owning the environment lets us inject faults and crashes on purpose and score
runs against ground truth.

**Rejected.** Public demo sites (cannot inject faults, may change, terms of use);
mocked tool responses (would not count as execution).

### D3. Python, FastAPI, Jinja and HTMX for the sandbox (2026-10-04)

**Why.** One language across the system. Real server-rendered forms, cookies, sessions
and redirects, which is what the browser agent should face. No frontend build step.

**Rejected.** A React sandbox (more time, nothing gained for the agent's realism).

### D4. Hand-written agent loop, no LangChain or LangGraph (2026-10-04)

**Why.** The loop, journal and gateway are the parts the panel will probe. A few hundred
explicit lines can be explained, debugged and modified live. Frameworks would hide
exactly those parts and add their own state model that conflicts with ours.

**Rejected.** LangGraph (its checkpointing would overlap with and obscure our journal).

### D5. Checkpoint decided state and journal effects, instead of replaying (2026-10-04)

**Why.** Replay-based durable execution (Temporal style) needs deterministic code; LLM
calls are not deterministic. We persist the contract, plan and ledger after each step
and journal every effect, then on resume we ask the apps what actually happened.

**Rejected.** Temporal or another workflow engine (heavy, and it would hide the very
mechanism we want to show). Full event sourcing of model calls.

### D6. Declared commit actions plus a network watch (2026-10-04)

**Why.** The gateway must know which actions change the world. Guessing from button
labels is unreliable, so the executor must declare commits explicitly. As a backstop,
the browser tool watches network traffic: any write request not caused by a declared
commit is flagged as an integrity violation.

**Rejected.** Classifying clicks by label alone; marking sandbox buttons with special
attributes (that would rig the sandbox).

### D7. Typed check vocabulary for contracts (2026-10-04)

**Why.** Free-text success criteria make "deterministic verification" a vague claim.
A small set of typed checks (`record_exists`, `field_equals`, `record_unique`,
`message_sent`, ...) can be evaluated without an LLM and reused by reconcile.

**Rejected.** LLM-judged success criteria everywhere.

### D8. The harness oracle is separate from the agent's verifier (2026-10-04)

**Why.** If the agent's own verifier decided success in evaluation, a wrong contract
would score as a pass. Ground truth is written by hand per task and read by an oracle
the agent cannot reach.

### D9. SQLite in WAL mode for the run store (2026-10-04)

**Why.** Durable writes with real fsync semantics, zero setup for reviewers, easy to
inspect while debugging. Journal writes must complete before effects are dispatched.

**Rejected.** Postgres (setup cost for reviewers), JSON files (no atomic multi-row writes).

### D10. One worker, with a lease and heartbeat (2026-10-04)

**Why.** Concurrency is out of scope. A lease prevents two processes acting on the same
run and makes crash detection simple: an expired heartbeat means interrupted.

### D11. Approvals and clarifications are durable (2026-10-04)

**Why.** In real companies, approvals take hours. Storing them in the run store lets a
run wait across restarts. An approval releases exactly one journal entry.

### D12. Accessibility tree as the main observation (2026-10-04)

**Why.** Cheaper and more stable than pixels, gives deterministic element IDs and
accessible names that survive layout changes. Screenshots are kept as evidence.

**Rejected.** Screenshot-and-coordinates as the primary mode.

### D13. API first, browser otherwise (2026-10-04)

**Why.** APIs are more reliable when available. The Admin API deliberately covers only
some operations so the agent must still choose and use the browser.

### D14. Sensitive values must come from the ledger (2026-10-04)

**Why.** Prevents the model from writing a hallucinated or misremembered amount, date
or account number. Every written value can be traced to a source.

### D15. Dev cache for model calls, uncached for reported results (2026-10-04)

**Why.** Caching keeps development cheap and the demo reproducible. Reported eval
numbers are uncached so reruns are independent.

### D16. No vision model for PDF reading in v1 (2026-10-04)

**Why.** Seeded PDFs have text layers; a vision cross-check would add cost without
catching anything real. The one image-only invoice tests that the worker admits it
cannot read something instead of guessing.

### D17. Acme Admin does not block duplicate invoice numbers (2026-10-04)

**Decision.** Drop the "invoice number unique per vendor" validation. Duplicates are
counted across every effect type (payables, employees, tickets, notifications, emails).

**Why.** Many real systems only warn about duplicate invoice numbers, or don't check at
all, which is exactly why duplicate entries are a real problem. With the rule in place,
the "journal off" ablation would hit a validation error instead of creating a
duplicate, and the headline comparison would measure the app, not the worker. The
duplicate trap still works: it tests whether the worker notices the existing record
through its natural-key check.

**Rejected.** Keeping the rule and basing the ablation on messages only.

### D18. Large money commits need requester approval in the gateway (2026-10-04)

**Decision.** Any commit with a money amount above `APPROVAL_THRESHOLD_INR` (default
₹1,00,000) is tier 2. In family 2, the welcome email goes to the new hire's personal
address, which is outside `@acme.test`, so it is tier 2 as an external email. The app's
own `pending_approval` workflow stays, and the worker still never approves in the app.

**Why.** Confirming large amounts with the requester before entering them is sensible
product behavior. It also means approvals and the `during_approval` crash point exist
in families 1 and 2, so they no longer depend on family 4, which stays cuttable.

**Rejected.** Making family 4 uncuttable; treating the app's own approval workflow as
the approval story (the worker would never be asked anything).

### D19. Duplicates are prevented by the natural-key check and reconcile (2026-10-04)

**Decision.** Say plainly that duplicate protection comes from the natural-key check
before every create, plus reconcile. The idempotency key is the journal entry's internal
identity. The Acme Admin REST API accepts an `Idempotency-Key` header on its write
endpoints, and the http tool sends it. Browser forms get no such help.

**Why.** The earlier wording implied the apps honored the key, which only an API can do,
and adding it to forms would rig the sandbox. An API that helps next to forms that
don't is realistic, since many payment and billing APIs offer this header, and it lets
us show both worlds.

### D20. A declared `login` action (2026-10-04)

**Decision.** Logging in is a declared action. Its POST is allowed by the network watch,
logged as a step event, and not journaled as a business effect. All sandbox reads,
including search and HTMX partials, use GET. Any other undeclared non-GET request is
still an integrity violation.

**Why.** Logins are POSTs, so without this the watch would flag every login. Declaring
it keeps the rule strict ("every write is declared") instead of adding a loose
exception such as "ignore POSTs to /login".

### D21. Cross-platform tooling: uv and a Python task runner, no make (2026-10-04)

**Decision.** `uv` manages Python and dependencies. A small Python task runner replaces
the Makefile (`uv run tasks <name>`). Crash points exit with `os._exit` inside the
worker. External kills use a cross-platform hard terminate exposed as
`breadcrumb kill <run_id>`, which finds the worker through the process ID in the run
record.

**Why.** The author works on Windows, where `make` and `kill -9` are not available by
default, and reviewers may use any OS. One Python entry point behaves the same
everywhere and needs no extra install beyond `uv`.

**Rejected.** Requiring GNU make on Windows; shell scripts (two versions to keep in sync).

### D22. Audit table, real Phase 0 test, deny-list from name pools (2026-10-04)

**Decision.** (a) Every sandbox write appends to an append-only `audit_log` table; the
oracle reads field histories and `field_unchanged` ground truth from it. (b) Phase 0
ships at least one real test, so the test command passes on merit (pytest fails when
no tests exist). (c) The generality deny-list is built from the seed generator's whole
name pools, not from one seed's output.

**Why.** (a) The oracle needs history to judge "this field never changed", and a final
snapshot cannot show a change that was made and reverted. (c) A deny-list from one
seed would miss names that only appear under other seeds, which are exactly the ones
used in held-out runs.
