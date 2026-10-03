# Demo

## Demo video (about 5 minutes)

Record with the UI on one side and a terminal on the other. Speed up long stretches
and say so on screen. Never edit out failures that happened; re-record instead.

| # | Time | Scene | What the viewer should take away |
|---|---|---|---|
| 1 | 0:00 | One-sentence framing: real work gets interrupted. Show the brief's example task | The problem we chose to solve |
| 2 | 0:20 | Family 1 happy path in the UI: contract appears, step log streams, receipt with sources | It genuinely operates real apps and finishes |
| 3 | 1:10 | Family 3 batch backfill. Kill the worker abruptly with `breadcrumb kill <run_id>` after 3 of 7 invoices, at the `after_dispatch` point | The hard case: the last action may or may not have happened |
| 4 | 1:40 | `breadcrumb resume`: reconcile finds the ambiguous invoice already saved, marks it confirmed, enters the remaining 4. Oracle shows 7 records, no duplicates | It picks up where it left off and never doubles work |
| 5 | 2:20 | Same scene with the journal switched off: a duplicate appears | Why the journal matters, shown not told |
| 6 | 2:50 | Invoice above the approval threshold: the gateway shows an approval card with the exact diff before anything is entered. Kill the worker with `breadcrumb kill` while it waits, restart. Approve. It enters the payable once, which lands as `pending_approval` in Admin, and reports that | Approvals survive restarts and release exactly one action; it never approves its own payable in the app |
| 7 | 3:30 | Bank-detail change email: worker refuses to change payment details, flags it, asks | Judgment, and defences that are structural |
| 8 | 4:00 | Family 2 onboarding on the same code: different module, API plus browser | Generalization |
| 9 | 4:30 | Scorecard: held-out results and both ablation tables | Evidence, honestly reported |

## Live demo safety plan

Live runs involve many model calls and a real browser, so plan for them to be slow or
to take a wrong turn.

- Keep a pre-recorded copy of the video ready.
- Have a dev-cache mode for the scripted tasks, clearly labelled as cached when used.
- For an interviewer's own task: run it uncached, narrate the step log while it runs,
  and treat a failure as a debugging exercise (open the receipt and journal, find the
  first wrong step). That is a strength if handled calmly.
- Pre-seed the sandbox and pre-start all apps before the call. Check the API key and
  quota the same morning.
- Keep budgets tight for live runs so nothing spins for minutes.

## Commands to have ready

Kept in the README once they exist: start sandbox with a fault profile, start UI, run a
task with a crash point, resume a run, show oracle state for a natural key, open the
latest scorecard.
