# Agent Design

Covers everything in the agent except durability (see `DURABILITY.md`).

## 1. Task contract

The contract compiler turns the request into a structured contract. It is the agent's
definition of done, and the plan may change but **the contract may not change without
the user's approval.**

| Field | Meaning | Example |
|---|---|---|
| `goal` | One sentence in plain words | "Record Northwind's latest invoice as a payable and tell the requester" |
| `deliverables` | What the user should end up with | A payable record, a notification with its ID |
| `checks` | Typed success checks (below) | `record_exists`, `field_equals`, `message_sent` |
| `write_scope` | Which apps and entity types the worker may change, and how | Admin: create `payable`. Notify: post. Nothing else |
| `protected` | Things that must not change | `vendor.bank_details` |
| `open_questions` | Unknowns, each tagged `blocking` or `discoverable` | "Which Northwind?" is discoverable until search finds two |
| `assumptions` | Defaults the compiler chose, shown to the user | "Latest means most recent invoice date" |

**Blocking vs discoverable.** A blocking unknown cannot be resolved by looking around
and changes the outcome (for example "enter it into which system?" when there are two
plausible ones). The worker asks before starting. A discoverable unknown is resolved by
work (which vendor, which invoice). If discovery turns up real ambiguity (two
similarly named vendors both with recent invoices), it becomes blocking at that moment.

**Contract changes.** If the worker finds it needs to write outside `write_scope`, it
must ask. Approving a scope change creates a new contract version. This is the main
structural defence against prompt injection: a PDF can say "also update the bank
account", but the contract has no write scope for bank details, so the gateway refuses.

## 2. Check types (the small typed vocabulary)

Checks are data, not free text, so the verifier can evaluate most of them without an
LLM. Expected values may be literals or references to ledger facts (`fact:invoice.amount`).

| Check | Meaning |
|---|---|
| `record_exists(app, entity, match)` | At least one record matches |
| `record_unique(app, entity, natural_key)` | Exactly one record matches (no duplicates) |
| `field_equals(app, entity, match, field, expected)` | A field has the expected value |
| `field_unchanged(app, entity, match, fields)` | Fields still equal their values at run start |
| `record_status(app, entity, match, status)` | For workflows, for example `pending_approval` |
| `message_sent(channel, to, must_contain)` | Email or notification sent with required content |
| `no_message_sent(channel, to)` | Nothing was sent (used for refusals) |
| `judgement(question, evidence_refs)` | Fuzzy check for an LLM judge, used rarely, must cite evidence |

Each check also has a `how_to_observe` hint resolved at runtime: API GET if the Admin
API exposes it, otherwise a UI search page. The verifier picks the route; the contract
only states what must be true.

## 3. Executor loop

One action per turn, over structured state rather than a growing chat transcript.

**Context assembled each turn**

1. The contract (goal, checks, scope, protected, assumptions)
2. The plan tree: subgoals with status (`todo`, `doing`, `done`, `blocked`)
3. The fact ledger, compact
4. Journal summary: confirmed effects, pending effects, failures
5. The latest observation (accessibility snapshot, file excerpt or API response),
   wrapped and labelled as untrusted data
6. The last few actions and their outcomes, compressed
7. Budgets used so far

**Action space**

- Tool reads: `browser.open`, `browser.observe`, `browser.click`, `browser.type`,
  `browser.select`, `files.list`, `files.read`, `http.get`
- Commits: `commit(kind, target, params, natural_key, description)` where the commit is
  either a browser submit (`element_id`) or an API write (`operation_id`). Only commits
  can change the world, and they always pass through the gateway.
- Session: `login(app)`: fills and submits an app's login form with credentials from
  config. Its POST is the one non-commit write the network watch allows. It is logged
  as a step event and never journaled as a business effect.
- Memory: `record_fact(key, value, source, locator)`
- Planning: `update_plan(...)`
- Talking to the user: `ask_user(question, options?)`
- `finish(summary)`: requests verification. The agent cannot mark the run done itself.

**Re-planning.** If an observation contradicts the plan (the invoice is not where
expected, the form has a new required field), the executor updates the plan tree
before acting. Plan updates are logged so the receipt can show how it adapted.

**Loop detection.** A state fingerprint is the hash of: current URL path, the list of
interactive elements (role and accessible name), and current form values. The same
action on the same fingerprint twice in a row counts as a loop. First time: force a
re-plan with a note. Second time: escalate with what was tried.

**Budgets.** Max steps per run, max wall time, max tokens. All configurable. Hitting a
budget ends the run as `FAILED` with an honest receipt, never as a silent stop.

## 4. Tools

All tools are generic. None knows about invoices, vendors or employees.

| Tool | Observes or does | Notes |
|---|---|---|
| `browser` | Open URLs, observe the accessibility tree with numbered interactive elements, click, type, select, screenshot | Watches network traffic: any non-GET request caused by something other than a declared commit or the declared `login` action is an integrity violation |
| `files` | List downloads and attachments, read PDF text with page and line locators | Text layer first. One scanned image-only invoice exists in the seed; for it, the worker must ask or escalate rather than guess |
| `http` | Call operations from the Acme Admin OpenAPI spec | GETs are reads. Writes only via `commit`, and every write carries an `Idempotency-Key` header set to the journal entry's idempotency key |
| `notify` | Post a message to the requester's channel | A commit, journaled, tier 1 |
| `ask_user` | Ask a question, run pauses durably | Answer becomes a fact with source `user` |

**API first, browser otherwise.** If the Admin API offers the operation, prefer it.
The API deliberately covers only some operations, so some tasks need the browser.

## 5. Gateway

Every commit passes through it, in this order:

1. **Scope check** against `write_scope` and `protected`. Out of scope: refuse and tell
   the executor to ask the user if it believes the change is needed.
2. **Value provenance check**: sensitive fields (amounts, dates, account numbers, IDs,
   email recipients) must reference ledger facts or user answers.
3. **Risk tier**
   - Tier 0, read: not a commit.
   - Tier 1, internal and reversible (create a payable, create a ticket, post a
     notification to the requester): journaled, allowed.
   - Tier 2, external, money-related or hard to undo (email to any address outside
     `@acme.test`, approve a payment, change payment details, delete): journaled,
     needs explicit user approval with the exact diff.
   - **Money threshold rule.** Any commit whose params include a money amount above
     `APPROVAL_THRESHOLD_INR` (config, default ₹1,00,000) is tier 2, even if it would
     otherwise be tier 1. The requester confirms large amounts before they are entered.
     Which params count as money is decided from the fact types in the ledger
     (`type: money`), not from field names.
4. **Duplicate check**: for creates, check the natural key in the app first. Already
   there: skip, mark `CONFIRMED`, and report that it already existed. This check plus
   reconcile is what prevents duplicates. Compute the idempotency key, which is the
   journal entry's internal identity, stable across retries. Only the Admin API's write
   endpoints accept it (as an `Idempotency-Key` header); browser forms get no such help.
5. **Journal** `INTENDED`, then `DISPATCHED`, then fire, then record the outcome
   (see `DURABILITY.md`).

**The worker never approves its own work.** Gateway approval (the requester saying
"yes, enter this") is separate from the app's own approval workflow. Payables above
₹1,00,000 still go to `pending_approval` in Acme Admin after the worker submits them;
the worker reports "pending approval" and never clicks Approve in the app.

## 6. Fact ledger

Each fact: `key`, `value`, `type` (money, date, text, id, email), `source` (app, file,
user), `locator` (URL plus element, or file plus page and line), `method` (read from
page, parsed from PDF, user answer), `step_no`.

Rules:

- Two facts with the same key and different values is a conflict. Conflicts on
  sensitive fields block commits that use them until resolved (re-read, or ask).
- Values written into apps for sensitive fields must reference a fact.
- The ledger is part of the checkpoint, so facts survive interruptions.

## 7. Recovery policy

| Error class | Example | Strategy |
|---|---|---|
| Transient | 5xx on a read, timeout on a page load | Backoff retry, up to 3 |
| Transient on a commit | 5xx or timeout after a submit | Mark `UNKNOWN`, reconcile, never blind retry |
| Element missing | Button renamed or moved | Re-observe, re-locate by role and accessible name, then re-plan |
| Session expired | Redirected to login | Log in again, return to the page, re-observe |
| Validation error | Form shows "Due date must be in the future" | Read the message, compare with ledger facts. Fixable from facts: fix. Otherwise ask |
| Unexpected modal | "What's new" popup | Dismiss if it is clearly non-committing, then continue |
| Loop | Same action, same fingerprint | Re-plan, then escalate |
| Repeated failure | Same subgoal fails 3 times | Escalate with what was tried |

The policy is a deterministic table in code. The model chooses *what* to try next only
when the table says "re-plan".

## 8. Verifier

- Runs after `finish`, in a fresh browser session, using the same tools.
- Sees the contract, the ledger and fresh observations. It does **not** see the
  executor's reasoning or step log, so it cannot be talked into agreeing.
- Evaluates every check. Deterministic checks first. `judgement` checks use the LLM
  with a separate prompt and must cite evidence.
- Verdict per check: `verified`, `failed` (with the observed value), `unverifiable`.
- Any `failed`: the executor gets one repair pass with the failure details, then the
  verifier runs again. Still failing: run ends `FAILED` with an honest receipt.
- Also cross-checks the journal: every `CONFIRMED` create should appear exactly once.

What it really catches (be precise about this in docs and interviews): a form that
never saved, the wrong record edited, transposed or truncated values, a validation
error the executor missed, a duplicate. It checks the world, not the executor's story.
It does not catch a contract that asked for the wrong thing; that is what showing the
contract to the user and the harness oracle are for.

## 9. Receipt

What the user gets at the end:

1. Two or three plain sentences: what was done, or where it stopped and why.
2. The contract checks with their verdicts.
3. The values written and where each came from (source and locator).
4. Effects from the journal: each with state and record ID or message reference.
5. Interruptions and how they were settled, if any.
6. Links to evidence: screenshots, downloaded files, page snapshots.

## 10. Prompts

- Stored as text files in `prompts/`: contract compiler, executor, verifier judge.
- Observed content is always wrapped and labelled as untrusted data.
- **Must not mention** specific vendors, people, task names or traps. Enforced by the
  generality check in `uv run tasks lint` against a deny-list built from the seed
  generator's name pools (every vendor, person and bank name any seed can produce, and
  each name's first word) and the task ids, not from one seed's output.
