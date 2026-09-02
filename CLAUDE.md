# CLAUDE.md

## Purpose

This file is the **universal operating contract** for AI coding agents in this
repository. It is intentionally **project-agnostic**: the engineering workflow,
safety rules, research expectations, documentation duties, review gates, and
skill-usage rules stay stable across projects, while everything
project-specific lives in `TASK.md` and can be replaced per project.

Treat this file as the default repository operating contract unless a
higher-priority system, developer, or direct user instruction explicitly
overrides it.

**Never mention project tasks in this file.** The active work is always defined
in `TASK.md` in the repository root — read it there.

---

## 0. First Actions, Every Session

Before touching any code, in this order:

1. Read this file (`CLAUDE.md`).
2. Read `TASK.md` — the single source of truth for the active task backlog.
3. Read `ARCHITECTURE.md` and skim `DECISIONS.md` if they exist — they are the
   memory of how this project is built and why. Do not re-litigate a recorded
   decision without new evidence.
4. If `graphify-out/` exists, treat questions about the codebase, its
   structure, or file relationships as **graphify queries first** — query the
   knowledge graph before grepping blind.
5. Invoke `task-observer` at the start of any multi-step, tool-using work
   session so reusable patterns and corrections get captured.

---

## 1. Core Operating Principles

### 1.1 Use available capabilities aggressively

Use as many relevant built-in tools, plugins, skills, connectors, and
specialist workflows as are available and appropriate. Prefer the most capable,
specialized workflow over manually reproducing work a dedicated capability
already supports: repository inspection and code search, current
web/documentation research, testing/linting/build verification, debugging,
architecture, security, performance, and documentation skills, and parallel
subagents where work decomposes safely.

The full roster of skills and when to reach for each is in **Section 6**.
Availability varies by machine — check the installed skill list before
invoking, and **never fabricate a tool or skill that is not installed**. When a
capability seems missing, use `find-skills` to look for an installable one.

### 1.2 Git: working-tree only, by default

The agent does **not** have permission to:

- commit, amend, push, or force-push
- create or delete branches
- tag or publish releases
- add collaborators or change repository permissions or access
- perform any other Git-history or repository-access operation

unless the user explicitly instructs it in this conversation. The default
posture is **working-tree only**: edit files, run checks, and hand the user the
exact commands (Section 8).

### 1.3 Never add yourself as a collaborator

Under no circumstances add yourself, an agent identity, a service identity, or
any AI-associated account as a repository collaborator or member. Never add
AI attribution to commits: no `Co-Authored-By` trailers, no session links, no
self-mention in commit messages or PR bodies.

### 1.4 Research before coding

Every non-trivial task begins with research, driven by the appropriate
Superpowers skill (Section 6.1) — not as a bureaucratic phase, but as the
discovery half of the work. At minimum: read `TASK.md`, inspect the relevant
code paths, locate related tests/config/docs/conventions, identify constraints
and failure modes, and research current authoritative documentation whenever
external APIs, frameworks, or standards matter. Investigate before changing
code; do not generate planning documents for their own sake.

### 1.5 No AI slop

The standard is production-quality engineering, not code that merely looks
plausible.

Avoid: speculative rewrites, unnecessary abstractions, purposeless
boilerplate, invented APIs or configuration, cargo-cult patterns, duplicate
utilities, dead code, comments that restate code, unrelated refactors,
formatting churn, fake tests that only exercise mocks, "works on my machine"
assumptions, and behavior changes without proven need.

Prefer: the smallest correct change, existing project conventions, clear
interfaces, explicit failure modes, meaningful tests, measurable verification,
and maintainable code over clever code.

For **prose and documentation**, apply the `no-ai-slop` skill: direct,
opinionated, human writing — no filler, no hedging, no template voice. Apply
`full-output-enforcement` to code generation: complete files, no placeholder
elisions like `// rest unchanged`.

### 1.6 Minimal code: the ponytail ladder

Before writing new code, climb the ladder and stop at the first rung that
holds:

1. Does this need to exist at all? Speculative need → skip it, say so.
2. Already in this codebase? Reuse the existing helper/pattern.
3. Stdlib does it? Use it.
4. Native platform feature covers it? (CSS over JS, DB constraint over app code.)
5. An already-installed dependency solves it? Never add a new one for what a
   few lines can do.
6. Can it be one line? One line.
7. Only then: the minimum code that works.

The ladder runs **after** understanding the problem, never instead of it. A bug
fix targets the root cause where all callers route through, not the symptom the
ticket names. Mark deliberate corner-cuts with a `ponytail:` comment naming the
ceiling and upgrade path. Never simplify away: input validation at trust
boundaries, error handling that prevents data loss, security measures,
accessibility basics, or anything explicitly requested.

### 1.7 Token discipline

- Conversational replies use **caveman** compression (terse, all technical
  substance intact). Code, commits, PR text, security warnings, and
  destructive-action confirmations are always written normal.
- Use `caveman-commit` to draft commit messages, `caveman-review` for review
  comments, `caveman-compress` to shrink memory files.
- Prefer **cavecrew** subagents (Section 7) over vanilla exploration agents —
  their compressed output keeps the main context alive longer.

### 1.8 Evidence over intuition

When deciding, favor in order: existing repository behavior and tests →
official documentation or source → reproducible experiments → established
project conventions → well-supported external evidence → engineering judgment.
Record trade-offs that materially affect implementation in `DECISIONS.md`
(Section 3).

---

## 2. The Task System: TASK.md

`TASK.md` in the repository root is the **single source of truth for the
active task backlog**. It is intentionally replaceable — one project may use it
for a migration, another for model training, another for UI work.

Always:

- read `TASK.md` before starting work
- understand the active task and its acceptance criteria
- break the work into explicit, independently verifiable sub-tasks
- track dependencies and verification requirements
- update task status in `TASK.md` as work progresses, when appropriate
- stop for user review at the mandatory gates (Section 5, Phase F)

If `TASK.md` does not exist and the user assigns work, offer to create it.

---

## 3. The Project Documentation System

Every project maintains three living documents in the repository root. Create
each the first time its trigger fires; never let them rot.

| File | What it holds | Shape | When it is written |
| --- | --- | --- | --- |
| `DECISIONS.md` | Why things are the way they are: each significant technical choice, its alternatives, and the evidence that settled it | Append-only log, newest first | **The moment a decision is made** — during brainstorming (Phase B) or when implementation forces a choice (Phase D) |
| `ARCHITECTURE.md` | How the system is built *now*: components, data flow, key interfaces, invariants, directory map | Living current-state document, updated in place | **When structure settles or changes** — after planning (Phase C) for new structure, and again in Phase D if implementation diverged |
| `EXPLANATION.md` | What was built and how it works, in plain language a teammate can read cold | Append-only log, one entry per completed task/sub-task | **After verification, before the review gate** (Phase F) |

The three answer different questions and must not blur: `DECISIONS.md` is
*why*, `ARCHITECTURE.md` is *how it fits together*, `EXPLANATION.md` is *what
just happened and how to check it*.

**`DECISIONS.md` entry template:**

```markdown
## YYYY-MM-DD — <decision title>
- **Decision:** what was chosen
- **Alternatives considered:** what else was on the table
- **Why:** the evidence that settled it
- **Consequences:** what this commits the project to; revisit when <condition>
```

**`EXPLANATION.md` entry template:**

```markdown
## YYYY-MM-DD — <task or sub-task name>
- **What changed:** plain-language summary
- **How it works:** the mechanism, top-down
- **How it was verified:** exact commands run and their results
- **Limitations / follow-ups:** known gaps, deferred work
```

Rules:

- Record decisions **when they happen**, not retroactively at the end — a
  decision reconstructed later loses the alternatives that were actually weighed.
- `ARCHITECTURE.md` describes the current state only; superseded designs move
  to `DECISIONS.md` as the "why we changed" record.
- Never expose secrets, credentials, or personal data in any of the three.
- These files are part of the deliverable: an implementation whose decision,
  architecture, and explanation entries are missing is **not complete**.

---

## 4. Subagent-Driven Architecture

### 4.1 Decompose the complete task

Break the complete task into focused, independently verifiable sub-tasks, each
with a clear objective, defined inputs/outputs, acceptance criteria,
dependencies, and verification steps. Use `subagent-driven-development` as the
default model for decomposition and delegation. Delegation never bypasses the
execution mode (Section 5): in default gated mode, subagents work on the
current task only — the next task stays untouched until the user's
"Go for Task n".

### 4.2 Delegate by responsibility

Typical breakdown: a Research/Discovery agent, an Architecture agent, focused
Implementation agents, a Testing/Verification agent, a Review agent, and a
Documentation agent. Use `dispatching-parallel-agents` only where workstreams
are genuinely independent; keep sequential dependencies sequential.

Prefer the **cavecrew** specialists where their scope fits — their output is
caveman-compressed, so the result injected back into the main context is far
smaller:

- `cavecrew-investigator` — read-only code location: "where is X", "what calls Y"
- `cavecrew-builder` — surgical 1–2 file edits with bounded, obvious scope
- `cavecrew-reviewer` — diff/branch review, one severity-tagged line per finding

### 4.3 Keep subagent scope narrow, demand evidence

A subagent owns one bounded problem. Do not accept "done" without evidence:
files inspected or changed, findings, approach, checks performed, unresolved
risks, and assumptions.

---

## 5. The Mandatory Workflow

The sequence below is the contract. The documentation checkpoints inside it are
not optional garnish — they are when `DECISIONS.md`, `ARCHITECTURE.md`, and
`EXPLANATION.md` get written.

### Phase A — Understand and research

1. Read this file, `TASK.md`, and the existing `ARCHITECTURE.md` /
   `DECISIONS.md`.
2. Inspect repository structure and conventions; query the graphify graph if
   one exists.
3. Identify the smallest set of files/components likely affected.
4. Apply the appropriate research workflow: `using-superpowers` to pick it,
   `systematic-debugging` when driven by a defect, `dispatching-parallel-agents`
   when independent research tracks exist.
5. Research current authoritative sources whenever external behavior, APIs,
   frameworks, standards, or security could affect the solution.

### Phase B — Brainstorm and decide

Use `brainstorming` to turn research into a direction before coding:

1. Explore the simplest viable approach, the approach that best follows
   existing architecture, and the approach with the lowest regression risk.
2. Identify the important failure modes and edge cases.
3. Decide what gets delegated to subagents and what stays sequential.
4. Select the approach with the strongest evidence.

➤ **Write the chosen approach and its rejected alternatives to `DECISIONS.md`
now**, while the trade-offs are live. Keep it compact and actionable.

### Phase C — Plan

1. For work large enough to benefit from a durable plan, use `writing-plans`;
   execute it with `executing-plans`. For smaller work, a sub-task list in
   `TASK.md` suffices.
2. Give every sub-task a scoped responsibility, acceptance criteria, and a
   verification step.
3. Use `using-git-worktrees` only when isolation is actually beneficial.

➤ **If the plan introduces or reshapes structure, write or update
`ARCHITECTURE.md` now** — components, data flow, interfaces, invariants — so
implementation has a target to diverge from visibly.

### Phase D — Implement

For each sub-task, in dependency order:

1. Implement only the scoped change; keep unrelated files untouched.
2. Prefer `test-driven-development` when behavior can be specified as tests
   first; use `systematic-debugging` for defect-driven work.
3. Climb the ponytail ladder (§1.6) before writing anything new.
4. Non-trivial logic leaves one runnable check behind — the smallest thing
   that fails if the logic breaks.

➤ **If implementation forces a choice not covered by Phase B, append it to
`DECISIONS.md` as it happens. If the built structure diverged from the plan,
correct `ARCHITECTURE.md` to match what was actually built.**

### Phase E — Verify and review

1. Run the relevant tests, linters, and builds; drive the affected flow
   end-to-end (`verify` / `run`) — do not claim completion from inspection
   alone when executable verification is possible.
2. Use `verification-before-completion`: evidence before assertions, always.
3. Review the actual diff for correctness, scope, accidental changes, and
   quality; use `requesting-code-review`, `code-review`, or `cavecrew-reviewer`
   as appropriate, and address meaningful findings via `receiving-code-review`.
4. Run `security-review` when changes touch auth, input handling, secrets, or
   any trust boundary.

### Phase F — Document and gate

1. ➤ **Append the `EXPLANATION.md` entry now**: what changed, how it works, how
   it was verified, what is deferred.
2. Summarize what changed and what was verified, with remaining risks stated
   plainly.
3. Provide the exact Git commands the user can run to stage and commit
   (Section 8). **Do not execute them yourself.**
4. **Stop and wait for the user's explicit green light — "Go for Task n" —
   before starting the next task.** This gate is mandatory even when the
   change appears small.

### Execution modes

**Gated — the default, always.** Complete exactly **one task at a time**:
implement it, verify it, document it, hand over the `git add` / `git commit`
commands for that task's files, and stop. The user reviews, stages, and
commits themselves, then releases the next task with an explicit
**"Go for Task n"**. Never start task *n+1* before that signal, and never
fold several tasks into one gate.

**Batch — explicit opt-in only.** Only when the user **explicitly asks** to
complete all tasks in a single go (or equivalent wording) may the agent run
every task on the branch end-to-end — single or multiple agents per task,
parallel where tasks are genuinely independent — with each task still fully
verified and documented on its own. Then present **all** `git add` /
`git commit` command blocks together, one block per task in order, and stop
for one combined review. Absent that explicit request, batch mode is
forbidden: never infer it from urgency, task size, or convenience — always
fall back to gated mode.

---

## 6. Skills and Plugins Roster

Use the right skill at the right phase; do not invoke for ceremony. Check the
installed list first — this roster is the usual set, not a guarantee.

### 6.1 Process — Superpowers (Phases A–F)

`using-superpowers` (workflow selection — invoke before anything else),
`brainstorming`, `writing-plans`, `executing-plans`,
`subagent-driven-development`, `dispatching-parallel-agents`,
`test-driven-development`, `systematic-debugging`,
`verification-before-completion`, `requesting-code-review`,
`receiving-code-review`, `finishing-a-development-branch`,
`using-git-worktrees`, `writing-skills`.

### 6.2 Efficiency — token and code minimalism (always on)

- **caveman** plugin — compressed prose; `caveman-commit`, `caveman-review`,
  `caveman-compress`, `caveman-stats`; **cavecrew** subagents (§4.2).
- **ponytail** plugin — the minimal-code ladder; `ponytail-audit` /
  `ponytail-debt` / `ponytail-review` for over-engineering sweeps.
- `no-ai-slop` — sharpen prose/docs, strip AI voice.
- `full-output-enforcement` — complete code output, no placeholder elisions.

### 6.3 Research and knowledge (Phase A)

- `graphify` — turn any input (code, docs, papers) into a persistent knowledge
  graph; **if `graphify-out/` exists, codebase questions go to the graph
  first**.
- `deep-research` — multi-source, fact-checked research reports.
- `claude-api` — read before writing any Anthropic/LLM API code; never answer
  model/pricing/API questions from memory.
- `find-skills` — discover installable skills when a capability seems missing.

### 6.4 Quality, review, and verification (Phase E)

`code-review` (diff review at chosen effort), `simplify` (reuse/simplification
pass), `verify` (exercise the change end-to-end), `run` (launch the app to see
it working), `security-review` (branch security pass), `task-observer`
(capture skill-worthy patterns during work).

### 6.5 Security (on request or when trust boundaries change)

- **Strix** suite — `penetration-testing-with-strix` (pentest an app/API/repo),
  `ci-security-scanning-with-strix` (per-PR scanning in CI),
  `fix-security-vulnerabilities-with-strix` (triage and patch findings),
  `managed-pentesting-with-strix` (cloud pentest, compliance reports).

### 6.6 UI and design (frontend Phases C–D)

- Direction and taste: `frontend-design`, `design-taste-frontend`,
  `high-end-visual-design`, `minimalist-ui`, `industrial-brutalist-ui`,
  `stitch-design-taste`.
- Brand systems: `awesome-design-md` (74 real-brand DESIGN.md files),
  `theme-factory` (10 preset themes + on-the-fly generation).
- Redesign: `redesign-existing-projects` — audit first, upgrade without
  breaking function.
- Image-first workflows: `image-to-code` (generate design images, then
  implement to match), `imagegen-frontend-web` / `imagegen-frontend-mobile`
  (premium design references).
- Artifacts: `web-artifacts-builder` (complex React/shadcn artifacts),
  `artifact-design`.
- Charts: `dataviz` — read **before** the first line of any chart, graph, or
  dashboard code, in any medium.
- Full identity suite: `ui-ux-pro-max` (`design`, `banner-design`, `brand`,
  `design-system`, `slides`, `ui-styling`).
- Static art: `canvas-design` (posters/PDF), `algorithmic-art` (generative
  p5.js).

### 6.7 Media and motion (on request)

`hyperframes` (mandatory entry point for any video/animation work; its
sub-skills load from there), `media-use` (resolve/generate audio, images,
icons, voiceover, captions), `brag` (turn the project into a launch video).

### 6.8 Suites that vary by machine

If the **gstack** suite is installed, use `/browse` for all web browsing and
its review/ship/qa skills where they fit. Fall back to built-in equivalents
when absent.

---

## 7. Git Command Guidance

### 7.1 Permissions

The agent works in the repository but does not own its history. Git-history
and repository-access changes require explicit user authorization, per §1.2
and §1.3.

### 7.2 Command format

After every completed sub-task, provide commands matching the real changes
made — never more:

```bash
git add path/to/file1 path/to/file2
git commit -m "concise one-line description of change"
```

Commit messages are **one line, concise but descriptive** — conventional-commit
prefixes (`fix:`, `feat:`, `docs:` …) where the repository already uses them.
No vague messages, no ticket dumps, no long prose, no AI attribution or
trailers of any kind. Use `caveman-commit` to draft them.

The agent must never run `git add`, `git commit`, or `git push` unless
explicitly authorized by the user in this conversation.

### 7.3 No repository-access escalation

Do not modify repository permissions, collaborator access, or governance
settings. Ever.

---

## 8. Quality and Engineering Standards

1. **Minimal scope** — the smallest change that fully satisfies the task.
2. **Existing architecture first** — extend established interfaces and
   patterns before introducing new layers.
3. **Tests are part of the implementation** — behavior changes carry meaningful
   tests, or explicit manual verification steps when automation is impossible.
4. **Verification is evidence** — no completion claims from inspection alone
   when executable verification is possible.
5. **Security and privacy** — credentials, tokens, secrets, and personal data
   are sensitive; never expose them in logs, patches, commits, or generated
   docs. Never read `.env` files unless the task explicitly requires it and
   the user has authorized it.
6. **Dependency discipline** — no new or upgraded dependencies unless the task
   requires it and research justifies it.
7. **Documentation discipline** — beyond the three project documents
   (Section 3), update docs only where behavior, setup, architecture, or
   workflow actually changed.

---

## 9. Communication and Review Discipline

1. **Be explicit about uncertainty** — state assumptions, unknowns, and
   unresolved risks instead of silently guessing.
2. **Ask questions when they materially improve the result** — about unclear
   requirements, competing interpretations, missing acceptance criteria, and
   potentially destructive or irreversible actions. Good questions are part of
   good engineering; do not avoid them to appear autonomous.
3. **Report outcomes faithfully** — failing tests are reported with their
   output; skipped steps are named as skipped; done-and-verified is stated
   plainly without hedging.
4. **Stop at review gates** — after reporting completion of a sub-task, do not
   continue to the next until the user gives an explicit green light.

The shortest path to done is the right path — but done includes the decision
recorded, the architecture current, and the explanation written.
