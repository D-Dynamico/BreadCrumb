# Build Phases

About 7 days. Each phase has exit criteria; a phase is done only when they are met and
recorded in a session note. Day numbers are a guide, not a promise.

**Golden rule:** a narrow thing that genuinely works beats a broad thing that is mocked.
If Phase 2 slips, cut scope later in the plan, never fake behavior.

---

## Phase 0: Scaffold (day 1, morning)

- Repo layout as in `ARCHITECTURE.md`, `uv` project, `.env.example`, a small Python task
  runner (`uv run tasks <name>`) with the commands listed in `CLAUDE.md` (stubs are fine
  where the feature comes later; stubs exit with an error, never pretend to succeed)
- Lint, type-check, test runner, and the generality check wired into `uv run tasks lint`
- At least one real test, so the test command has something genuine to pass
- `runs/` and artifacts gitignored

**Exit:** `uv run tasks setup`, `uv run tasks lint`, `uv run tasks test` run clean on
Windows, on an otherwise empty project.

## Phase 1: Sandbox (day 1 to day 2 morning)

- Mailbox, Vendor Portal, Acme Admin (payables, people, tickets, notify page)
- Partial Admin API with OpenAPI spec, `Idempotency-Key` header on its write endpoints
- Append-only audit table recorded on every write; all reads are GET
- Seed generation including invoice PDFs and the five trap situations
- Oracle service
- **Write the held-out tasks now** and freeze them (record the commit hash)
- Fault middleware can wait until Phase 4, but leave the hook in place

**Exit:** a human can do family 1 and family 2 tasks end to end in a browser. The oracle
returns correct ground truth for those end states. Held-out tasks committed.

## Phase 2: Bare agent, happy path (day 2)

- LLM client with config-driven model ID and dev cache
- Tools: browser (accessibility observation, click, type, select), files (PDF text),
  http (OpenAPI GETs), notify
- Executor loop with plan tree and a simple `finish`
- Commits can still call tools directly here; the gateway comes next

**Exit:** family 1 happy path completes 3 out of 3 runs against the real sandbox, with
a readable step log. **This is the milestone everything else depends on.**

## Phase 3: Durability (day 3)

- Run store: runs, checkpoints after every step, lease and heartbeat
- Journal and action state machine
- Gateway skeleton: declared commits, natural-key check, idempotency key (sent as a
  header on API writes), journaling
- Declared `login` action allowed by the network watch, not journaled
- `breadcrumb kill <run_id>` for cross-platform external kills
- `breadcrumb resume`, reconcile procedure, crash injection hooks
- Unit tests for the state machine and reconcile decisions

**Exit:** for family 1, inject each crash point (`before_intended`, `after_intended`,
`after_dispatch`), resume, and finish with exactly one payable and one notification.

## Phase 4: Contract, verifier, safety, faults (day 4)

- Contract compiler with typed checks, write scope, protected fields, open questions
- Fact ledger with provenance and conflict detection
- Gateway: scope check, value provenance check, risk tiers including the money
  threshold rule, durable approvals
- Clarifications as durable waits
- Recovery policy table
- Verifier with fresh session, verdicts, one repair pass
- Receipt
- Fault profiles: `flaky`, `session`, `drift`, `chaos`

**Exit:** family 1 passes under every fault profile. A family 1 task with an amount
above the approval threshold waits for approval, survives a restart (`during_approval`
crash point), and releases exactly the right action once approved. A failed check produces an honest `FAILED` receipt.

## Phase 5: Generalize and the traps (day 5)

- Families 2 and 3 working with **no task-specific code** (generality check passes)
- Family 2's external welcome email goes through a tier 2 approval
- All five traps behave correctly
- `mid_batch` crash on family 3 resumes with no duplicates
- Minimal UI: task box, run list with statuses, live step log, approval and
  clarification cards, receipt view, Resume button

**Exit:** every dev task passes at least once. UI can drive a full run including an
approval.

## Phase 6: Evaluation (day 6)

- Harness runner, crash and fault injection, oracle scoring, metrics, reports
- Both ablations
- Held-out run with seeds chosen now, uncached, 3 repeats
- Fix the biggest failure group, rerun, and stop. Don't chase the last percent

**Exit:** `scorecard.md` committed with real numbers for dev, held-out and both ablations.

## Phase 7: Ship (day 7)

- README (outline in `SUBMISSION.md`), architecture summary, decisions summary,
  limitations, what's next, assumptions, models and frameworks used
- Demo video (script in `DEMO.md`)
- Final pass: fresh clone, follow the README exactly, everything runs

**Exit:** every item in the `SUBMISSION.md` checklist is ticked.

---

## Cut line (apply in this order if behind)

1. Family 4 (overdue reminders) is a stretch goal. Skip it first.
2. UI polish. Keep the step log, approval cards and receipt; drop styling work.
3. `drift` fault profile.
4. Reduce held-out repeats from 3 to 2.
5. Family 3 last, because it carries the best durability demo (`mid_batch`). Cut it only
   if Phase 3 itself is in trouble.

**Never cut:** the journal and resume, the oracle, the generality rule, honest numbers.
