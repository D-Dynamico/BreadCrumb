# Durability: Journal, Checkpoints, Resume

This is Breadcrumb's headline feature. Read it fully before touching the executor,
gateway, journal or run store.

## The promise

1. A run can be interrupted at any moment (process killed, machine restarted, browser
   crashed, session expired, user away for a day) and later resumed.
2. After resuming, the worker continues from where it was, not from the start.
3. **No side effect ever happens twice** because of an interruption or a retry.
4. If the worker genuinely cannot tell whether something happened, it says so and
   asks, instead of guessing.

## Three layers of state

| Layer | What it holds | Written when |
|---|---|---|
| **Run record** | Status, lease, heartbeat, worker process ID (used by `breadcrumb kill`), task text, created and updated times | On every status change, heartbeat every few seconds |
| **Checkpoint** | Contract, plan tree with subgoal statuses, fact ledger, step counter, recent action summaries, budgets used | After every completed step |
| **Journal** | One entry per side-effecting action, with its state history | Before and after every side effect (write-ahead) |

Reads (page loads, PDF parsing, API GETs) are not journaled as side effects. They are
logged as step events for the UI and evidence, and they are simply repeated on resume.
LLM calls are not replayed; see "Why checkpoints, not replay" below.

## Run statuses

| Status | Meaning |
|---|---|
| `COMPILING` | Contract compiler running |
| `AWAITING_CLARIFICATION` | Blocked on a user answer. Durable: survives restarts |
| `RUNNING` | Executor loop active, lease held |
| `AWAITING_APPROVAL` | A tier 2 action waits for the user. Durable: survives restarts |
| `INTERRUPTED` | Found `RUNNING` with an expired lease on startup, or killed. Resumable |
| `RECONCILING` | Resume in progress, settling ambiguous journal entries |
| `VERIFYING` | Verifier evaluating contract checks |
| `DONE` | All checks passed |
| `FAILED` | Could not complete. Receipt explains where and why |
| `ESCALATED` | Needs a human decision the worker cannot make (for example a reconcile conflict) |

## Journal entry

Each entry records:

- `entry_id`, `run_id`, `step_no`, `attempt_no`
- `action_kind`: for example `create_record`, `update_record`, `submit_form`,
  `send_email`, `post_notification`
- `tool` and `target`: which app, which entity type
- `params`: the resolved values, with fact references for sensitive fields
- `natural_key`: the business identity of the effect, for example
  `vendor=Northwind Traders, invoice_no=INV-2291`, or `employee_email=priya@acme.test`
- `idempotency_key`: derived from run, contract and natural key, stable across retries.
  It is the entry's internal identity. It reaches an app only on Admin API writes, as
  an `Idempotency-Key` header (see below)
- `risk_tier` and `approval_id` if one was needed
- `state` plus a timestamp for each state reached
- `evidence`: references to the observation before and after (snapshot, screenshot,
  response body)
- `reconcile_result` when resume had to settle it

## Action state machine

```
            approval needed?
PROPOSED ───────────────────► AWAITING_APPROVAL ──rejected──► REJECTED
    │                               │ approved
    ▼                               ▼
 INTENDED ◄─────────────────────────┘
    │  (written and fsynced before anything touches the world)
    ▼
 DISPATCHED
    │  (written immediately before the tool fires the effect)
    ├──── clear success observed ──────► CONFIRMED
    ├──── clear failure observed ──────► FAILED   (safe to retry as a new attempt)
    └──── no clear outcome / crash ───► UNKNOWN  ("maybe committed")
                                            │ reconcile
                    ┌───────────────────────┼─────────────────────────┐
                    ▼                       ▼                         ▼
                CONFIRMED              NOT_APPLIED                CONFLICT
           (found, values match)   (absent, retry allowed)  (found, values differ,
                                                              escalate to user)
```

Rules:

- **Nothing reaches the world without an `INTENDED` entry already on disk.**
- An entry still in `DISPATCHED` when a run is resumed is treated as `UNKNOWN`.
- A retry is a new attempt on the same entry with the same idempotency key. Before any
  retry of a create, the gateway re-checks the natural key. If it already exists, the
  retry is skipped and the entry becomes `CONFIRMED`.

**What actually prevents duplicates.** The apps do not stop duplicates for us: Acme
Admin, like many real systems, accepts the same invoice number twice. Protection comes
from two things the worker does: the natural-key check before every create, and
reconcile of every ambiguous entry. Messages carry a visible reference token so they
can be found the same way. The one place the world helps is the Admin REST API, which
accepts an `Idempotency-Key` header on its write endpoints, as many payment and billing
APIs do: a repeated request with the same key returns the original result instead of
creating a second record. Browser forms get no such help, which is the point. The
demo and evaluation show both.
- `FAILED` means the worker observed a clear failure (validation error shown, HTTP 4xx
  with an error body). `UNKNOWN` means it did not (timeout, crash, session expired
  on the response page, 5xx after the request was sent).

## How reconcile works

For every `UNKNOWN` entry, the worker answers one question: **did this effect happen?**
It does so by looking, never by assuming.

| Effect type | How to check |
|---|---|
| Create a record | Search the app (UI search or API GET) by natural key. Exactly one match with matching values: `CONFIRMED`. None: `NOT_APPLIED`. Match with different values or more than one: `CONFLICT` |
| Update a record | Read the record. Fields equal the intended values: `CONFIRMED`. Equal the before-values: `NOT_APPLIED`. Anything else: `CONFLICT` |
| Send an email or notification | Look in the Sent folder or channel for a message carrying the run's reference token in its body footer (for example `Ref: BC-7F3K`). Visible to humans on purpose, like a real ticket reference |
| Submit for approval | Read the record's status |

These checks reuse the verifier's check types (see `AGENT_DESIGN.md`), so there is one
way of asking "is this true in the apps", not two.

`CONFLICT` and any reconcile that cannot be answered move the run to `ESCALATED`
with a clear question for the user. The worker never guesses on money or messages.

## Resume procedure

1. **Acquire the lease.** Refuse if another live worker holds it.
2. **Load the latest checkpoint**: contract, plan tree, ledger, counters.
3. **Start a fresh browser session.** Old sessions are gone. Log in again as needed.
4. **Reconcile.** Status `RECONCILING`. Settle every `DISPATCHED` or `UNKNOWN` entry.
   Mark plan subgoals as done or not done based on the results.
5. **Re-observe.** Take a fresh observation of where the worker needs to be next.
6. **Continue the loop.** The executor receives a short "you were interrupted, here is
   what is confirmed, here is what is not" summary in its context, built from the
   journal, not from memory.
7. Record the interruption and the reconcile results as events, so the receipt can
   show them.

Resume can be triggered by `breadcrumb resume <run_id>`, by a Resume button in the UI,
or automatically on startup for `INTERRUPTED` runs if `AUTO_RESUME=true`.

## Lease and heartbeat

Single worker by design. A running run holds a lease with a heartbeat every few
seconds. On startup, any `RUNNING` run whose heartbeat is older than the lease timeout
is marked `INTERRUPTED`. This prevents two workers acting on one run, and it is how a
killed process is detected without any special shutdown hook.

## Durable waits

Approvals and clarifications are stored in `runs.db`, not held in memory. A run in
`AWAITING_APPROVAL` can survive a restart and continue the next day when the user
approves. The approval card records the exact action and its diff; approving it
releases exactly that journal entry, nothing broader. If the world changed while
waiting (for example the record was created by someone else), the gateway re-checks
the natural key after approval and before dispatch.

## Crash points (used by the harness and the demo)

Crash injection is a harness and demo facility, off by default, enabled only by an
explicit environment variable. It kills the worker process abruptly (no cleanup) with
`os._exit`, which behaves the same on Windows, macOS and Linux. To kill a worker from
outside (the demo, or the harness simulating a machine dying), use
`breadcrumb kill <run_id>`, which terminates the worker's process with a
cross-platform hard terminate (no signal handlers, no cleanup), never `kill -9`.

| Crash point | When it fires | What it proves |
|---|---|---|
| `before_intended` | Before the journal write | Nothing happened, nothing recorded, resume redoes the step |
| `after_intended` | After `INTENDED`, before dispatch | Recorded but not sent: reconcile finds `NOT_APPLIED`, sends once |
| `after_dispatch` | After the effect fires, before the outcome is recorded | **The hard case.** Reconcile must find `CONFIRMED` and not resend |
| `mid_batch:<k>` | After k of n items in a batch task | Resume finishes the remaining items, no duplicates |
| `during_approval` | While waiting for approval (family 1 large amount, family 2 external welcome email) | Approval survives restart and releases the right action |
| `session_expiry_after_submit` | Sandbox fault: session dies on the response page | Same as `after_dispatch`, but caused by the app, not the worker |

## Why checkpoints, not replay

Durable-execution systems like Temporal replay deterministic workflow code from an event
history. That does not fit an LLM agent: model calls are not deterministic, so replaying
them would produce a different path. Breadcrumb instead checkpoints the agent's
*decided state* (contract, plan, ledger) and journals the *effects*. On resume it does
not ask "what would I have done", it asks the apps "what actually happened". The
journal is the source of truth for effects; the apps are the source of truth for the
world. This also keeps the mechanism small enough to explain line by line.

## What this does not handle (state honestly)

- Effects in systems that offer no way to look them up (no search, no sent folder).
  Breadcrumb would have to escalate every `UNKNOWN` there.
- Two humans or two workers editing the same record at the same time.
- Disk loss of `runs.db` itself.
- Changes made by someone else to a record between dispatch and reconcile can look like
  a `CONFLICT`. That is the correct, cautious outcome, but it costs a question.
