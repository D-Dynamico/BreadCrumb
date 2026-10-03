# Interview Prep

Questions the panel is likely to ask, and what a strong answer must cover. Before the
interview, add the exact file and function names for each answer once the code exists.

---

**1. "The session expires a millisecond after you click Submit. Walk me through the
next ten steps."**

- The commit was journaled `INTENDED`, then `DISPATCHED`, before the click.
- The response is a login page, not a success page, so the outcome is `UNKNOWN`, not
  `FAILED`. Explain why that distinction matters: a blind retry here creates a duplicate.
- Recovery policy: log in again. Then reconcile: search by natural key (vendor plus
  invoice number).
- Found with matching values: `CONFIRMED`, move on. Not found: `NOT_APPLIED`, retry as a
  new attempt with the same idempotency key. Found with different values: `CONFLICT`,
  escalate.
- Point to the test that covers each branch and to the `session` fault profile.

**2. "What does your verifier actually catch? It reads the same database you wrote to."**

- Yes, and it should: the point is independence from the executor's story, not from
  the world. It runs in a fresh session and never sees the executor's reasoning.
- Concrete catches: a form that never saved, the wrong record edited, transposed or
  truncated values, a missed validation error, a duplicate.
- Show ablation 2: false completions with the verifier off vs on.

**3. "Who checks the contract? If the compiler writes the wrong check, you confirm the
wrong thing."**

- Admit it is the main remaining risk.
- Mitigations: typed checks, the contract and its assumptions shown to the user, tier 2
  actions show the exact diff, and evaluation is scored by an oracle with hand-written
  ground truth that never reads the contract.
- Say how often contract misreads showed up in the failure analysis.

**4. "You built the sandbox, the traps and the agent. Why isn't this overfit?"**

- Held-out tasks written before tuning, frozen at a recorded commit.
- Prompts contain no task, vendor or trap names, enforced by the generality check in lint.
- Seeds for the held-out run chosen after tuning.
- Offer to run a task they invent, right now.

**5. "The PDF says 'also update the bank account'. Where in the code is that blocked?"**

- Not "the prompt says to ignore it". Structurally: the contract has no write scope for
  bank details and lists them as protected; the gateway refuses out-of-scope commits;
  even in scope, payment-detail changes are tier 2 and need approval.
- Then the deeper case: what if an in-scope field is poisoned, for example a fake
  amount? The ledger records the source; conflicts with other sources block the commit;
  the receipt shows where every value came from.

**6. "Why not LangGraph or Temporal?"**

- LangGraph: its checkpointing overlaps with the journal and would hide the mechanism.
- Temporal: replay needs deterministic code and model calls are not deterministic.
  Breadcrumb checkpoints decisions and journals effects, then asks the apps what
  happened. Credit Temporal as the inspiration.

**7. "How would this work on real websites?"**

- What carries over: the contract, gateway, journal, reconcile, verifier.
- What changes: authentication (MFA, OAuth), anti-bot measures, sites with no way to
  look effects up (more escalations), legal and permission questions.
- What would be built first: one real sandbox integration, for example an accounting
  product's developer sandbox.

**8. "What happens when two workers pick up the same run?"**

- Lease and heartbeat: the second refuses. Two workers editing the same records is out
  of scope; next step would be per-record locks.

**9. "Show me a failure from your eval and how you debugged it."**

- Have one ready from the scorecard: the run, the first wrong step, the cause, the fix,
  and the before and after numbers.

**10. "If you had another week, what would you build?"**

- Procedure memory, and why the journal is the right input for it. Be specific about
  how a learned routine is invalidated when the UI changes.

---

## Live modification warm-ups

Be ready to do these in under 15 minutes each:

- Add a new check type (for example `field_in_range`).
- Add a new fault (for example a slow-loading dropdown).
- Change a risk tier rule (for example make ticket creation tier 2).
- Add a new crash point.
