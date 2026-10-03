# Submission

## Checklist (from the brief)

| Requirement | Where it lives | Done |
|---|---|---|
| GitHub repository | Public repo, clean history | ☐ |
| README with setup and run instructions | `README.md` | ☐ |
| Short explanation of the architecture | README section, links to `docs/ARCHITECTURE.md` | ☐ |
| Important technical and design decisions | README section, links to `docs/DECISIONS.md` | ☐ |
| Demo video or live demo | Link at the top of the README | ☐ |
| Known limitations | README section | ☐ |
| What you would build next | README section | ☐ |
| Assumptions | README section, links to `docs/PROBLEM.md` | ☐ |
| Models, APIs, frameworks, services, pre-built components | README section | ☐ |
| No real credentials or third-party systems | Sandbox only, `.env.example` | ☐ |

## README outline

1. **Breadcrumb**: one line, then the tagline. Link to the demo video.
2. **What it does**: the brief's example task, what happens, and the promise (finishes
   correctly or tells you exactly where it stopped, and never does a side effect twice).
3. **Results**: held-out success, duplicate side effects, false completion. Both
   ablation tables. One sentence on how numbers were measured (oracle, uncached,
   held-out frozen before tuning).
4. **Quick start**: prerequisites (Python 3.12, `uv`), `.env`, `uv run tasks setup`,
   `uv run tasks seed`, `uv run tasks sandbox`, `uv run tasks ui`, first task. Works on
   Windows, macOS and Linux. Then "try interrupting it" in three commands.
5. **How it works**: the diagram, then one short paragraph each on contract, executor,
   gateway and journal, resume, verifier, sandbox.
6. **Design decisions**: the five most important, one or two lines each, link to the rest.
7. **Evaluation**: suites, metrics, anti-overfitting rules, how to reproduce.
8. **Limitations**
9. **What's next**
10. **Assumptions**
11. **Built with**
12. **Prior work and inspiration**: durable execution systems such as Temporal, research
    on agent reliability and fault injection (cite what was actually read).

## Built with (fill in exact versions at the end)

- Google Gemini API, Gemini 3.1 Flash Lite (`gemini-3.1-flash-lite`, from config)
- Python 3.12, uv
- FastAPI, Jinja2, HTMX
- Playwright (Chromium)
- Pydantic
- SQLite
- A PDF text extraction library and a PDF generation library (record which)
- AI coding tools used during development: Claude Code

## Known limitations (seed list, refine at the end)

- The world is a sandbox of three apps. Real sites add CAPTCHAs, MFA and anti-bot
  measures that are out of scope.
- Reconcile depends on being able to look effects up. Systems without search or a sent
  log would force escalation on every ambiguous action.
- One worker at a time; no concurrent edits by others are modelled.
- English only. Text-layer PDFs only; image-only documents are escalated, not read.
- The contract compiler can still misread a request. Showing the contract to the user
  and the oracle-based evaluation reduce but do not remove this.
- Results come from a modest number of runs; rates have wide uncertainty.

## What's next (seed list)

1. **Procedure memory.** Turn a successful run into a reusable routine (the steps,
   which tools, where the fields are), so the tenth invoice is fast and cheap. When the
   UI changes and a step fails, fall back to reasoning and repair the routine. The
   journal already records exactly what worked, so it is the natural input.
2. Real sandboxes over OAuth (a test Gmail account, an accounting product's sandbox).
3. A team approval queue with roles, and notifications by email or chat.
4. Multiple workers with proper leasing and per-record locks.
5. A vision fallback for image-only documents, with mandatory human confirmation of
   extracted amounts.
6. Scheduled and recurring tasks ("every Monday, chase overdue invoices").
