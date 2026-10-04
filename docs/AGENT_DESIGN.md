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

### 1.1 The contract as built (Phase 4, D38)

The contract is centred on **deliverables**: the effects the user should end up with.
Scope, natural keys, lookups and most checks all come from them, so they are stated
once, before any work, instead of being re-declared by the worker at every commit.

```
Contract
  goal: str
  assumptions: [str]
  open_questions: [{question, blocking: bool}]
  facts: [{key, type: money|date|id|email|text, description}]
  deliverables: [{
      id: str                      e.g. "entered_record", "requester_note"
      kind: create | update | send
      description: str
      lookup_operation: str        an API read that lists such records, or "notify"
      key:    {field: "fact:<key>"}   the natural key, field names as the lookup returns them
      values: {field: "fact:<key>"}   other values the effect must carry
      must_contain: ["fact:<key>"]    send only: what the message must mention
  }]
  protected: [{lookup_operation, match: {field: value}, fields: [str]}]
  extra_checks: [field_unchanged | no_message_sent ...]    never judgement (D37)
  version: int
```

- **Facts are named by the contract.** The worker must `remember` values under these
  keys (it may remember other facts too). A deliverable refers to them as
  `fact:<key>`, so field names and fact names are fixed before work starts. This ends
  the key drift seen in Phase 3, where each retry named the key differently.
- **Write scope is the list of deliverables.** A commit must name the deliverable it
  produces (`deliverable: <id>`). A commit that names none, or one that does not
  exist, is refused with "outside the contract; ask the user". A PDF that says "also
  change the bank account" has no deliverable to name, so the gateway refuses it.
- **Checks are derived, not written by the model.** Each `create` gives
  `record_unique(key)` plus `field_equals` for every value; `update` gives
  `field_equals` for every value; `send` gives `message_sent(must_contain)`; each
  `protected` entry gives `field_unchanged`, compared with a snapshot taken when the
  contract is accepted. The compiler may add only `field_unchanged` and
  `no_message_sent` in `extra_checks`; anything else, `judgement` included, fails
  validation (D37).
- **Validation is code, not prompt.** Every `fact:` reference must name a declared
  fact; every `lookup_operation` must be a GET operation in the API spec (or
  `notify`); every key and value field must be a field that operation returns,
  checked against the spec's response schema; `send` needs `must_contain`. An invalid
  contract is sent back to the compiler with the errors up to twice, then the run fails
  honestly.
- **Blocking questions** stop the run as `AWAITING_CLARIFICATION` before any work; the
  answer is added to the task as a user statement and the contract is compiled again.
- **Limit (Phase 5).** Deliverables need an API read to be looked up and verified.
  Employees and sent mail have none in the Admin API, so families 2 and 4 will need a
  UI lookup route. Until then such a deliverable is accepted but marked as unverifiable
  by API, and the receipt says so.

### 1.2 Commits under the contract (Phase 4)

`browser_submit` and `http_write` take `deliverable` instead of `effect`, `key_json`,
`values_json` and `lookup_operation` (D38 supersedes that part of D31). The gateway
resolves the deliverable's key and values from the ledger; a fact that is missing
refuses the commit with "remember <key> first". `notify` is the `send` deliverable
whose lookup is `notify`; a run whose contract has no such deliverable cannot notify.

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
| `judgement(question, evidence_refs)` | Fuzzy check for an LLM judge, used rarely, must cite evidence. Phase 5; until then rejected by contract validation (D37) |

Each check also has a `how_to_observe` hint resolved at runtime: API GET if the Admin
API exposes it, otherwise a UI search page. The verifier picks the route; the contract
only states what must be true.

## 3. Executor loop

One action per turn, over structured state rather than a growing chat transcript.

**Context assembled each turn**

1. The contract (goal, checks, scope, protected, assumptions)
2. The plan tree: subgoals with status (`todo`, `doing`, `done`, `blocked`)
3. The fact ledger, compact
4. Journal summary: every effect with its state (built from `runs.db`), and after a
   resume, what reconcile settled
5. The latest observation (accessibility snapshot, file excerpt or API response),
   wrapped and labelled as untrusted data
6. The last few actions and their outcomes, compressed
7. Budgets used so far

Also in every prompt: the systems the worker may use, each with its URL and one line
on what it is for (from config, `breadcrumb/config.py`), and the API's operations. The
one-liners are company knowledge a new employee would get on day one, such as "the
system of record holds work already entered, not new incoming items". They name no
task, vendor or person (D30). Until Phase 4, the task text stands in for the contract.

**Action space**

As built in Phase 2 (`breadcrumb/executor/actions.py`):

- Reads: `browser_open(url)`, `browser_click(element)`, `browser_type(element, text)`,
  `browser_select(element, option)`, `files_list()`, `files_read(name)`,
  `http_get(operation_id, params_json)`. Every browser action returns a fresh
  observation, so there is no separate observe action.
- Commits (declared writes): `browser_submit(element, description)` for any button that
  saves, sends, approves or deletes; `http_write(operation_id, params_json, body_json,
  description)`; `notify(message)`. Only commits may change the world, and all of them
  go through the gateway and the journal. `browser_submit` and `http_write` also
  declare their effect: `effect` (create, update or other), `key_json` (the fields that
  identify the record, named as an API read returns them), `values_json` and
  `lookup_operation` with optional `lookup_params_json` (an API read that lists such
  records). A create or update without a key and a lookup is refused (D31). `notify`
  needs none: its key is the channel plus the run's reference token. Phase 4 adds the
  contract's scope and risk tiers on top.
- Session: `login()`: fills and submits the current app's login form with credentials
  from config, matched by URL. The model never sees a password. Its POST is the one
  non-commit write the network watch allows. Logged as a step event, never journaled.
- Bookkeeping fields on every action: `why` (required, one sentence for the step log),
  `remember` (facts read in the current observation, each with its source and
  locator) and `plan` (the whole updated plan, only when it changes). Facts and the
  plan ride along on whatever action the worker takes next, so keeping them costs no
  model call (D28). There are no separate bookkeeping actions.
- `finish(summary)`: requests verification. The agent cannot mark the run done itself.
  Until the Phase 4 verifier, a run ends as `FINISHED`, meaning "the worker says so";
  the oracle scores it.
- Planned: `ask_user(question, options?)` with durable waits (Phases 3 and 4).

**Re-planning.** If an observation contradicts the plan (the invoice is not where
expected, the form has a new required field), the executor updates the plan tree
before acting. Plan updates are logged so the receipt can show how it adapted.

**Loop detection** (`breadcrumb/executor/repeats.py`, D34). The state fingerprint
depends on what is observed: for a page, the URL path plus the interactive elements
with their values (typed text, selected options); for a file or an API answer, the
source plus a hash of the content. A repeat is the same action with the same
arguments on the same fingerprint as an action within the last 6 steps, not only the
previous one, because real loops cycle (read a file, reopen a form, read it again).

| Repeat | What happens |
|---|---|
| First | The action runs; its outcome carries a note naming the earlier step and asking the worker to put needed values in `remember` or change approach |
| Second | Refused, not executed; the outcome lists the steps involved and asks for an updated plan |
| Third | The run ends `ESCALATED`, saying what was repeated |

The count resets on progress (a commit that ends `CONFIRMED`, or a plan step newly
marked done), so scattered, legitimate repeats in a long run do not add up. Each
session of a run starts with a fresh window, so re-observing pages after a resume is
never a repeat.

**Budgets.** Max steps per run, max wall time, max tokens. All configurable. Hitting a
budget ends the run as `FAILED` with an honest receipt, never as a silent stop. Every
run records `ended_by` (`finish`, `step_budget`, `time_budget`, `token_budget`,
`repeats`, `reconcile`, `unclear_effect`, `model_error`), shown by the CLI and kept in
`summary.json`, so a receipt always says whether a budget ran out or repeats escalated.

## 4. Tools

All tools are generic. None knows about invoices, vendors or employees.

| Tool | Observes or does | Notes |
|---|---|---|
| `browser` | Open URLs, observe the accessibility tree with numbered interactive elements, click, type, select, screenshot | Watches network traffic: any non-GET request caused by something other than a declared commit or the declared `login` action is an integrity violation |
| `files` | List downloads and attachments, read PDF text with page and line locators | Text layer first. One scanned image-only invoice exists in the seed; for it, the worker must ask or escalate rather than guess |
| `http` | Call operations listed in the API's OpenAPI spec (fetched at run start and shown to the model as one line per operation) | GETs are reads. Writes only via `http_write`, and every write carries an `Idempotency-Key` header: the journal entry's idempotency key, derived from the run and the natural key, so a retry repeats the same key |
| `notify` | Post a message to the requester's channel (`NOTIFY_URL`, `NOTIFY_CHANNEL`) | A commit, tier 1. Every message ends with the run's reference token, `Ref: BC-XXXX` |
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

**How the Phase 4 build makes these checks concrete (D38).**

- *Scope*: the commit must name a contract deliverable (section 1.2).
- *Provenance, browser*: the browser tool keeps the values typed and chosen on the
  current page since it loaded. Before a submit, every typed value that reads as money,
  a date or an email address must equal a ledger fact of that type (numbers by value,
  dates in any common format, so "15 Nov 2026" equals `2026-11-15`). Otherwise the
  commit is refused, naming the value. Free text is not checked.
- *Provenance, API*: the same rule over every leaf value of the request body.
- *Tier 2*: any money value in the commit (typed, or resolved from the deliverable)
  above `APPROVAL_THRESHOLD_INR`, or an email recipient outside `@acme.test`.
- *Approval*: the entry goes `PROPOSED` to `AWAITING_APPROVAL` and an approval row is
  written with the exact diff (deliverable, key, values, typed fields). The run waits
  as `AWAITING_APPROVAL`, polling `runs.db` with its browser still open. Released by
  `breadcrumb approve` (D35). If the worker dies while waiting, the approval still
  stands: on resume the worker refills the form, and the same deliverable and key give
  the same journal entry, which may then proceed, but only if the values still equal
  the approved diff; otherwise approval is asked again. After approval, the natural
  key is checked again before dispatch.

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
| Loop | Same action on the same fingerprint within the last 6 steps | Note, then refuse and ask for a new plan, then escalate (section 3) |
| Repeated failure | Same subgoal fails 3 times | Escalate with what was tried |

The policy is a deterministic table in code. The model chooses *what* to try next only
when the table says "re-plan".

**As built (Phase 4, D40):** `breadcrumb/recovery/policy.py`. The executor applies
retries (navigation and API reads on 5xx, timeouts or dropped connections; clicks,
typing and choices only when the page itself answered 5xx, since a timeout there
usually means the element is not there) and automatic sign-in (any browser action
that lands on a sign-in page) without a model call. Gateway and verifier lookups
retry transient failures 3 times. "Repeated failure" is covered by repeat detection
for now; per-subgoal counting is not built.

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

**As built (Phase 4, D39):** `breadcrumb/verifier/verifier.py` decides every derived
check (section 1.1) from fresh API reads through the gateway's lookups, so it needs no
browser and no model. `record_unique` covers the journal cross-check. A key fact that
was never recorded fails its deliverable. `judgement` checks are not built (D37): one
that appears is `unverifiable` with that reason. Outcome: all verified is `DONE`; any
failed after the repair pass is `FAILED`; otherwise `FINISHED`, never `DONE`.

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

**As built (Phase 4):** `breadcrumb/receipt/receipt.py` writes `receipt.md` and
`receipt.json` in the run folder for every run, however it ended, from durable records
only (state, journal, waits, events), so a resumed run's receipt is complete. It
includes the status and `ended_by` in words, the contract goal and assumptions, check
verdicts, values written with their sources, journal effects with their state
history, approvals and questions with their answers, and interruptions. Screenshots
are not captured yet; downloaded files are in the run folder.

## 10. Prompts

- Stored as text files in `prompts/`: contract compiler, executor, verifier judge.
- Observed content is always wrapped and labelled as untrusted data.
- **Must not mention** specific vendors, people, task names or traps. Enforced by the
  generality check in `uv run tasks lint` against a deny-list built from the seed
  generator's name pools (every vendor, person and bank name any seed can produce, and
  each name's first word) and the task ids, not from one seed's output.
