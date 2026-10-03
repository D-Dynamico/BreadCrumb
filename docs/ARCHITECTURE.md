# Architecture

## One-paragraph summary

A user gives Breadcrumb a task. The **contract compiler** turns it into a task contract:
the goal, typed success checks, what the worker may change, and any blocking
questions. The **executor** then loops: observe, decide one action, act. Reads go
straight to the tools. Anything that changes the world goes through the **gateway**,
which checks scope and risk, asks for approval when needed, and writes it to the
**journal** before and after it happens. Facts the worker discovers go into the
**fact ledger** with their source. The whole run is **checkpointed** after every step,
so it can be resumed after any interruption. When the executor thinks it is finished,
the **verifier** evaluates every contract check against fresh observations and the
worker produces a **receipt**. A separate **harness** scores runs with an **oracle**
that reads the sandbox's ground truth and that the agent can never reach.

## Component diagram

```
                 ┌────────────── Breadcrumb UI / CLI ───────────────┐
                 │  task box · run list · step log · approval cards │
                 └───────────────┬──────────────────────▲───────────┘
                                 │ task                 │ events (SSE)
                 ┌───────────────▼───────────────┐      │
                 │       Contract compiler       │      │
                 └───────────────┬───────────────┘      │
                                 │ contract             │
   ┌─────────────┐   ┌───────────▼───────────────┐   ┌──┴──────────┐
   │ Fact ledger │◄─►│         Executor          │──►│  Run store  │
   └─────────────┘   │ observe → decide → act    │   │ checkpoints │
                     └─────┬───────────────┬─────┘   │ + journal   │
                    reads  │               │ commits └──▲──────────┘
                           │       ┌───────▼────────┐   │
                           │       │    Gateway     │───┘
                           │       │ scope · risk · │
                           │       │ approval · idem│
                           │       └───────┬────────┘
                     ┌─────▼───────────────▼─────┐
                     │ Tools: browser · files ·  │
                     │ http · notify · ask_user  │
                     └─────────────┬─────────────┘
                                   │
          ┌────────────────────────▼─────────────────────────┐
          │  Sandbox "Acme Co.": Mailbox · Vendor Portal ·   │
          │  Acme Admin (payables, people, tickets) · faults │
          └────────────────────────▲─────────────────────────┘
                                   │ ground truth (harness only)
                     ┌─────────────┴───────────┐   ┌──────────┐
                     │ Oracle                  │◄──│ Harness  │
                     └─────────────────────────┘   └──────────┘
       Verifier: reads through the same tools as the executor, fresh session,
       evaluates contract checks, never sees the executor's reasoning.
```

## Components and responsibilities

| Component | Owns | Does not do |
|---|---|---|
| Contract compiler | Turning a request into a contract with typed checks, write scope, open questions | Acting on the world |
| Executor | The loop, the plan tree, choosing the next action | Writing to apps directly |
| Tools | Observing and acting on browser, files, HTTP APIs, notify, ask_user | Deciding anything |
| Gateway | Scope check, risk tier, approvals, idempotency key, budgets, journaling of commits | Planning |
| Journal | Durable record of every side effect and its state | Interpretation |
| Run store | Checkpoints of run state, run status, lease and heartbeat | Business logic |
| Fact ledger | Typed facts with provenance, conflict detection | Fetching data |
| Recovery policy | Mapping an error class to a strategy | Executing the strategy |
| Verifier | Evaluating contract checks against fresh observations, verdicts | Fixing things |
| Receipt builder | Summary and evidence pack for the user | Judging success |
| Sandbox | The fake company apps, seed data, fault injection | Knowing the agent exists |
| Oracle | Ground-truth read of sandbox state for scoring | Being reachable by the agent |
| Harness | Running task suites, crash injection, scoring, reports | Changing agent behavior |

Detailed design: `AGENT_DESIGN.md` (contract, loop, tools, gateway, ledger, recovery,
verifier), `DURABILITY.md` (journal, checkpoints, resume), `SANDBOX.md`, `EVALUATION.md`.

## Processes and ports

| Process | Port | Notes |
|---|---|---|
| Mailbox app | 8101 | Acme's webmail. Login required |
| Vendor Portal | 8102 | "External" site with its own login and look |
| Acme Admin | 8103 | Internal system: payables, people, tickets. Web UI plus a partial REST API with an OpenAPI spec |
| Oracle | 8109 | Bound to localhost, used only by the harness. Not in any agent tool config |
| Breadcrumb UI | 8000 | Task box, run list, live step log, approvals, receipts |

The three sandbox apps can be started by one launcher but are separate FastAPI apps on
separate ports, so they behave like separate systems (separate cookies and sessions).

## Data stores

| Store | Contents | Owner |
|---|---|---|
| `sandbox/data/sandbox.db` | All Acme Co. app data | Sandbox (rebuilt by `uv run tasks seed`) |
| `runs/runs.db` | Runs, checkpoints, journal entries, approvals, events | Breadcrumb run store (WAL mode) |
| `runs/artifacts/<run_id>/` | Screenshots, downloaded PDFs, page snapshots, receipt | Breadcrumb |
| `harness/reports/` | Eval results as JSON and a Markdown or HTML scorecard | Harness |

## Planned folder layout

```
breadcrumb/                      repo root
├── CLAUDE.md
├── README.md                    written in the final phase
├── docs/                        all design docs and session notes
├── sandbox/
│   ├── apps/mailbox/            webmail app
│   ├── apps/vendor_portal/      external vendor site
│   ├── apps/admin/              Acme Admin: payables, people, tickets, partial API
│   ├── common/                  config, database, sessions and CSRF, audit, money
│   ├── static/                  htmx, served locally (no internet needed)
│   ├── faults/                  fault profiles and middleware
│   ├── seed/                    name pools, scenario builder, PDF generation, writer
│   ├── oracle/                  ground-truth read service (harness only)
│   └── launcher.py              starts all apps with a fault profile
├── breadcrumb/                  the agent package
│   ├── contract/                compiler, check types
│   ├── executor/                loop, plan tree, context assembly, loop detection
│   ├── tools/                   browser, files, http, notify, ask_user
│   ├── gateway/                 scope, risk tiers, approvals, idempotency, budgets
│   ├── journal/                 action state machine, reconcile
│   ├── ledger/                  facts and provenance
│   ├── recovery/                error taxonomy and strategies
│   ├── verifier/                check evaluation, verdicts
│   ├── receipt/                 summary and evidence pack
│   ├── runs/                    run store, checkpoints, lease, resume entry point
│   ├── llm/                     model client, prompt loading, dev cache
│   ├── ui/                      web UI and SSE events
│   └── cli                      run, resume, runs commands
├── prompts/                     prompt text files, trap-agnostic
├── harness/
│   ├── tasks/dev/               task YAML files used during development
│   ├── tasks/heldout/           frozen tasks, never used for tuning
│   ├── taskfile.py              task file format: template, rendering, ground truth
│   ├── runner                   runs suites, injects crashes and faults
│   ├── scoring                  oracle-based scoring and metrics
│   └── reports/                 output
├── tests/                       unit tests for deterministic parts
└── runs/                        run store and artifacts (gitignored)
```

## Dependency rules

- `breadcrumb/` never imports from `sandbox/` or `harness/`. It only knows URLs and
  credentials from config, like it would for a real company.
- `sandbox/` knows nothing about the agent.
- `harness/` may import `breadcrumb/` (to start runs) and talk to the oracle.
- Only the harness config contains the oracle URL.
- Inside `breadcrumb/`: tools never call the gateway; the executor calls the gateway,
  the gateway calls tools for commit actions. The journal and run store are the only
  modules that write to `runs.db`.

## Request lifecycle (happy path)

1. User submits a task. A run is created with status `COMPILING`.
2. Contract compiler produces the contract. Blocking questions move the run to
   `AWAITING_CLARIFICATION`; otherwise `RUNNING`.
3. Executor loop: assemble context (contract, plan, ledger, journal summary, latest
   observation), ask the model for one action, execute it, checkpoint.
4. Reads run directly. Commits go through the gateway: scope check, risk tier,
   approval if needed (run becomes `AWAITING_APPROVAL`), journal `INTENDED`, then
   `DISPATCHED`, then the tool fires, then `CONFIRMED` or `FAILED` or `UNKNOWN`.
5. The executor calls `finish`. The run becomes `VERIFYING`. The verifier evaluates
   every check in a fresh browser session.
6. All checks pass: `DONE`, receipt built, user notified. A check fails: one repair
   pass, then `DONE` or `FAILED` with an honest receipt.

## Stack choices (short form, full reasoning in DECISIONS.md)

- Python 3.12 for the agent and sandbox, so the whole system is one language.
- FastAPI + Jinja + HTMX for the sandbox: real server-rendered forms and sessions,
  fast to build, no frontend build step.
- Playwright for the browser, accessibility tree as the main observation.
- SQLite in WAL mode for the run store: durable, zero setup, honest fsync semantics.
- Google Gemini API (`gemini-3.1-flash-lite`, free tier) with provider and model ID
  from config (D23). Model access sits behind one small
  client interface so it can be swapped.
