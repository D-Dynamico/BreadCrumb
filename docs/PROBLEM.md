# The Problem

## The brief (as given)

**Intern Problem Statement: Autonomous AI Task Worker**

Companies perform many repetitive tasks that involve reading information, deciding
what to do next, using websites or applications, and checking whether the task was
completed correctly. Today, a person often has to manually move between different
tools to complete even a simple request.

Build a prototype of an AI worker that can take a natural language task and
autonomously attempt to complete it using a computer. For example, a user might say:

> "Find the latest invoice from Company X, extract the amount and due date, enter it
> into our internal system, and tell me once it is done."

The system should ideally be capable of:

- Understanding the user's end goal rather than requiring every step to be specified.
- Breaking the request into a sequence of actions.
- Using available tools such as a browser, files, APIs, or a simulated company application.
- Observing the result of each action.
- Deciding what to do next based on what happened.
- Remembering relevant information discovered during execution.
- Detecting when an action fails.
- Attempting a reasonable alternative or retry where appropriate.
- Verifying whether the requested outcome was actually achieved.
- Asking the user for clarification or approval when it cannot safely proceed.
- Returning a concise summary and useful evidence of completion.

**Scope.** Not production ready. Need not support every website, app or workflow.
May restrict to a small environment, simulated company application, browser
environment, or limited set of tools. *A narrow prototype that genuinely works is
better than a broad system where most functionality is mocked.* Architecture, model,
framework, interface and workflow are intentionally not prescribed.

**Evaluation criteria**

| Criterion | What they ask |
|---|---|
| Autonomy | Can it determine and execute meaningful next actions without being told every step? |
| Execution | Does it actually perform work rather than explain what should be done? |
| Reliability | How does it handle unexpected states, errors, retries and failures? |
| Verification | Does it determine whether the requested outcome was actually achieved? |
| Generalization | How much of the system can stay unchanged for a different task? |
| Engineering Quality | Architecture, implementation, code quality, debugging, technical judgment |
| Product Thinking | Does it focus on accomplishing the user's actual objective? |
| Technical Understanding | Can you clearly explain why you built it the way you did? |

Not evaluated on the number of features.

**Submission requirements:** repo link; README with setup and run instructions; short
architecture explanation; important technical and design decisions; demo video or
live demo; known limitations; what you'd build next; assumptions; models, APIs,
frameworks, external services and pre-built components used. AI coding tools are
allowed. Be ready to explain, debug or modify the implementation live. No real
company credentials, confidential information, or unauthorized access to third-party
systems; use sandbox or mock environments.

## Our interpretation

The hard part of a real AI worker is not clicking buttons. It is that real work is
**interrupted**: sessions expire, pages fail, processes crash, approvals take hours.
An agent that only works when everything goes right, and that might enter an invoice
twice when it retries, is not something a company can hand real work to.

So Breadcrumb is built around one promise: **give it a task, and it will either finish
it correctly or tell you exactly where it stopped and why, no matter what interrupts
it, and it will never do the same side effect twice.**

That promise touches almost every criterion:

| Criterion | How Breadcrumb answers it |
|---|---|
| Autonomy | Goal compiled into a contract; the agent plans and re-plans on its own, asks only when blocked |
| Execution | Real Playwright browser on real (sandbox) web apps, real PDFs, real API calls |
| Reliability | Journaled actions, crash-safe resume, reconcile of ambiguous actions, recovery taxonomy |
| Verification | Typed contract checks evaluated against fresh observations; separate harness oracle |
| Generalization | Generic tools only; three different task families run on unchanged code |
| Engineering Quality | Small explicit state machines, deterministic pieces tested, clear module boundaries |
| Product Thinking | Durable approvals, no duplicates, honest receipts, fraud-aware behavior |
| Technical Understanding | Hand-written loop and journal that can be explained line by line |

## What we are deliberately not doing

- Not supporting arbitrary real websites. The sandbox is the world.
- Not building a general desktop computer-use agent (pixels and OS apps).
- Not running many workers in parallel. One worker, one run at a time, with a lease.
- Not learning across runs. That is the "what's next" story (procedure memory).

## Assumptions

- Reviewers will run it locally with their own Anthropic API key.
- A sandbox the candidate builds is acceptable, as long as it is honest (not rigged)
  and the evaluation is scored by an oracle independent of the agent.
- English-language tasks, Indian rupee amounts, dates in ISO format inside the apps.
- The requester is a finance or operations employee at Acme Co., talking to the worker
  through the Breadcrumb UI or CLI.
