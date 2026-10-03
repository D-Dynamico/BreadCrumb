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

### D23. Gemini 3.1 Flash Lite on the free tier as the model (2026-10-04)

**Decision.** Use the Google Gemini API with `gemini-3.1-flash-lite`, set through
`LLM_PROVIDER` and `LLM_MODEL` in `.env`. The model client stays behind one small
interface, so the provider can be swapped without touching the agent.

**Why.** The author wanted a free provider, and reviewers can get a free key too.
Checked in Google's model docs on 2026-10-04: the ID is `gemini-3.1-flash-lite`, a
stable release, with function calling, structured output and a 1,048,576-token input
window. A lite model keeps the agent honest about its design: the contract, gateway
and verifier have to carry reliability, not a large model.

**Risk.** Free-tier limits are per project and only shown in AI Studio, not in the
docs. As shown in AI Studio on 2026-10-04: 15 requests per minute, 250K tokens per
minute, 500 requests per day. A run is about 30 to 45 calls, so about 12 runs a day.
That is plenty for development with the dev cache (D15), but the planned evaluation
(about 75 runs) needs several days of quota. Consequences: the model client must pace
itself under 15 requests per minute and back off on rate-limit errors, and step
prompts should stay under about 15K tokens. How to fit the evaluation (spread over
days, a paid day, or fewer repeats) is open.

**Rejected.** Anthropic API (paid). Groq free tier (very fast, but tokens-per-minute
caps clash with large page snapshots, and open-weight models are weaker at long
tool-calling chains). Gemini 3.8 Flash (stronger, but the free tier showed only 20
requests per day).

### D24. Task files are templates over the seeded scenario (2026-10-04)

**Decision.** The seed generator writes `scenario.json`, naming every role a task refers
to (target vendor, look-alike pair, traps, new hires, backfill batches). Task files are
Jinja templates over it, rendered strictly, then parsed as YAML. Held-out tasks have
`seed: null`; the harness picks seeds at eval time. Only the harness reads the
scenario, through the oracle's `/scenario` endpoint.

**Why.** Names, amounts and dates must vary by seed so nothing can be memorized, but a
task like "enter the latest invoice from Northwind" has to name a real vendor. Roles
keep the request and its ground truth in step under any seed. Strict rendering means a
typo in a task fails loudly instead of producing an empty check.

**Rejected.** Fixed names in task files (would tie tasks to one seed and invite
overfitting); generating tasks in code (ground truth would no longer be hand-written).

### D25. reportlab to make PDFs, pdfplumber to read them (2026-10-04)

**Decision.** reportlab generates invoice and offer-letter PDFs with a real text layer.
The scanned-style invoice is drawn with Pillow and embedded as an image only. The
agent's files tool will use pdfplumber.

**Why.** reportlab is the standard pure-Python PDF writer. pdfplumber returns text with
positions, which gives the fact ledger page-and-line locators. Both work on Windows
without system packages.

**Rejected.** pypdf for reading (fewer layout details); OCR for the scanned invoice
(D16: the worker should say it cannot read it, not guess).

### D26. Realistic web app details in the sandbox (2026-10-04)

**Decision.** Server-side sessions with one cookie name per app, CSRF tokens on forms,
GET for every read (opening an email does not mark it read), POST for writes, plain
validation messages beside fields with an alert summary, and a saved-record
confirmation showing the new reference.

**Why.** These are ordinary traits of internal web tools, so the agent faces what it
would face at a real company, without anything added for its benefit (ground rule 7).
GET-only reads are also what make the browser's network watch meaningful (D20).

### D27. Stateless model calls through the Interactions API (2026-10-04)

**Decision.** Each turn makes one call to Gemini's Interactions API with `store=False`,
a fixed system prompt, one freshly assembled user prompt, the action schemas and
`tool_choice: any`, so the model must answer with exactly one action. Thinking level
`low` by default (`LLM_THINKING_LEVEL`).

**Why.** Google now recommends the Interactions API. Rebuilding the context every turn
(AGENT_DESIGN section 3) means no conversation history lives with the provider: a
resumed run needs nothing but our own checkpoint, which is the durability story. It
also keeps each prompt bounded (about 5 to 7K tokens), well under the free tier's
tokens-per-minute cap.

**Rejected.** Server-side history via `previous_interaction_id` (state we do not own and
that the free tier deletes after a day); a growing chat transcript (unbounded tokens).

### D28. Facts and plan ride along on every action (2026-10-04)

**Decision.** `remember` and `plan` are optional fields on every action instead of
separate `record_fact` and `update_plan` actions.

**Why.** On the first run the model spent four of nine turns updating its plan. With
500 requests a day, bookkeeping must not cost turns. The plan and facts are still
recorded with their step and source, exactly as before.

### D29. Elements are found by role and name, never by tagging the page (2026-10-04)

**Decision.** The observation is Playwright's ARIA snapshot with interactive elements
numbered by the worker. An action resolves its number to (role, accessible name,
position among same-named elements) and uses `get_by_role`. Repeated text (row names,
cell wrappers, link URLs, echoed labels) is dropped before numbering.

**Why.** Adding ids or attributes to the page would change the apps the worker is
operating, which is close to rigging (ground rule 7). Role and name are what a person
using a screen reader relies on, and they survive layout changes (useful for `drift`).

### D30. The worker is told what each system is for (2026-10-04)

**Decision.** Config gives each system a one-line purpose, for example that the inbox
is where documents from outside arrive and the system of record holds work already
entered. The executor prompt gains two generic rules: find the item at its source
before checking the system of record, and look everywhere it could be before
concluding there is nothing to do.

**Why.** On the second dev run the worker never opened the inbox: it took an older
invoice already in Admin as "the latest" and declared the job done. A new employee
would be told on day one where work arrives; that is company knowledge, not a task
hint. Nothing names a task, vendor or trap, and the generality check still passes.
This failure is also the case the Phase 4 verifier exists for.

### D31. A commit says how to find its record again (2026-10-04)

**Decision.** `browser_submit` and `http_write` declare their effect: `effect` (create,
update, other), `key_json` (the fields that identify the record, named as an API read
returns them), `values_json` and `lookup_operation` (an API read that lists such
records). A create or update without a key and a lookup is refused before anything is
journaled. The gateway runs the lookup before dispatch (the natural-key check) and
again on resume to settle an in-flight entry. The natural key and the lookup are
stored in the journal entry, so reconcile needs nothing from the worker's memory.
Lookup filters are only a hint: if a filtered lookup finds nothing, the whole list is
checked before concluding the record is absent. Messages need no declaration: their
key is the channel plus the run's reference token, so one notify per channel per run.

**Why.** Reconcile has to answer "did this happen?" deterministically, and the agent
may not contain task-specific code (no "search payables by invoice number" anywhere).
The worker already knows the API's operations; asking it to name the lookup before it
acts turns "check it doesn't exist first" from prompt advice into structure, and the
answer is fixed at the moment of intent, not reconstructed after a crash. Phase 4's
contract can later supply or cross-check the key.

**Rejected.** Lookups hard-coded per entity type (task-specific code); letting the
model judge after resume whether the effect happened (not deterministic, and exactly
the memory a crash erases); waiting for the Phase 4 contract (Phase 3's exit needs
reconcile now). **Limit.** Effects with no API read to find them can still be made
(`effect: other`), but if one is ever in doubt the run escalates.

### D32. Outcomes come from HTTP status; an INTENDED entry was never sent (2026-10-04)

**Decision.** A dispatch is classified from the HTTP status of the writes it sent:
2xx or 3xx is `CONFIRMED`, 4xx is `FAILED`, 5xx or no answer is `UNKNOWN` (then
settled at once by looking), nothing sent is `FAILED`. The browser tool records the
response status of each declared write. On resume, an entry still `INTENDED` becomes
`NOT_APPLIED` without looking, because `DISPATCHED` is committed to disk before the
tool fires; `DISPATCHED` becomes `UNKNOWN` and is settled by the lookup.

**Why.** Standard HTTP meaning is generic across apps, and the sandbox forms already
answer 303 on save and 422 on a validation error, like most real web apps. The
write-ahead order is what makes the `INTENDED` rule safe; it is the reason for writing
`DISPATCHED` separately at all.

### D33. Crash points fire once, in the process that started the run (2026-10-04)

**Decision.** `CRASH_POINT=after_dispatch` fires on the first commit;
`after_dispatch:2` on the second. A resumed run ignores `CRASH_POINT`, so one injected
crash is one interruption even if the variable is still set. Checkpoints keep one row
per run (the latest), without the page observation, since a resumed run starts with a
fresh browser.

**Why.** The harness and demo need a predictable single crash per run. Keeping only
the latest checkpoint is enough for resume; the step log keeps the full history.

### D34. Loop detection moves into Phase 3, as a repeat window (2026-10-04)

**Decision.** Build the first slice of the loop detection designed in `AGENT_DESIGN.md`
section 3 now. A repeat is the same action and arguments on the same state fingerprint
as an action within the last 6 steps. The fingerprint depends on what is observed: a
page's URL path and interactive elements with their values, or a file's or API
answer's source and content hash. First repeat: run it with a note pointing to
`remember`. Second: refuse it, unexecuted, and ask for an updated plan. Third: end the
run `ESCALATED`. The count resets on progress (a `CONFIRMED` commit or a plan step
newly done) and at the start of every session of a run. Every run also records
`ended_by`, so the receipt says whether a budget ran out or repeats escalated. Exit
runs for Phase 3 use `--max-steps 40`.

**Why.** The first crash-and-resume run looped for 27 steps (read the PDF, reopen the
form, which wipes it, read again) until the token budget ran out, costing about 60 of
the day's 500 free-tier requests. With several crash-point runs to do, a loop has to
end in a few steps, not a whole budget. The design's "twice in a row" rule would not
have caught it, because the loop was a cycle of 2 to 4 different actions; a window
does. Resetting on progress keeps scattered, legitimate repeats in a long run from
adding up; resetting per session keeps re-observing after a resume from counting.

**Rejected.** Consecutive-only detection (misses cycles); rerunning without it and
accepting the variance (one loop costs a whole run's requests); a 30-step cap for the
exit runs (a legitimate crash plus resume needs 30 to 32 steps, since a resume must
log in again and refill the form).
