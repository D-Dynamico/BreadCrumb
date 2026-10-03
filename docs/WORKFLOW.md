# Workflow

How to work in this repo. Applies to humans and to Claude Code.

## Session notes

- One note per working session: `docs/sessions/<YYYY-MM-DD>-<topic>.md`.
- Create it on the first behavior-changing task of the session; update it as you go.
- Sections: **Scope**, **Changes** (what and why), **Files touched**, **Verification**
  (exact commands run and their result), **Decisions** (link to `DECISIONS.md`
  entries), **Open items**.
- Record the decision, not just the diff. "Switched reconcile for emails to a reference
  token because the Sent folder has no other stable key" is useful; "updated reconcile"
  is not.

## Keeping docs true

- If you change behavior that a doc describes, update the doc in the same change.
- If you make a design choice, add a numbered entry to `DECISIONS.md`.
- If code and docs disagree and you are not sure which is right, stop and ask.
- Keep `CLAUDE.md` under 200 lines. Details belong in `docs/`.

## Phase discipline

- Work on the current phase in `PHASES.md`. Don't start later phases early unless the
  current one is blocked.
- A phase is done only when its exit criteria are met and the session note shows how
  they were verified.
- If something outside the plan seems worth building, write it under Open items and ask.

## Verification habits

- After agent changes: run the `smoke` suite.
- After journal, gateway or run-store changes: run the crash-point tests for family 1.
- After prompt changes: run `smoke` plus the generality check. Never look at held-out
  results while tuning.
- Unit-test the deterministic parts: action state machine, reconcile decisions, risk
  tiers, scope check, loop fingerprint, check evaluation, fault profiles.
- Do not mark anything done based on the agent's own receipt. Use the oracle.

## Debugging an agent run

1. Open the run's step log and receipt (UI or `runs/artifacts/<run_id>/`).
2. Find the first step where the observation and the plan disagreed.
3. Check the journal for that run: which effects are `CONFIRMED`, `UNKNOWN`, `FAILED`.
4. Reproduce with the same seed, faults and dev cache, then change one thing at a time.
5. Write down the cause in the session note, even if the fix was small.

## Commits

- Plain language a non-engineer could follow: `resume a run after a crash without
  duplicating the invoice`, `add the vendor portal login page`.
- No conventional-commit prefixes, no jargon.
- One logical change per commit. Docs updated in the same commit as the behavior.
- Never commit `.env`, `runs/` or real credentials.

## Writing style (docs, notes, README, receipts)

- Concise, natural prose. Short sentences.
- No em dashes. Use commas, colons or parentheses instead.
- Relatable words over jargon: "it picks up where it left off" before
  "idempotent resumption".
- Don't describe the project using commit counts or line counts as a measure of effort.

## Safety rules while building

- Sandbox credentials only. Never point the agent at a real site or account.
- Crash injection and fault profiles are off unless explicitly enabled.
- Model API keys stay in `.env`.
