---
name: battle-workflow
description: Operational doctrine for the legion battle pipeline (Think→Plan→Build→Review→Test→Deliver→Reflect). Use whenever running, resuming or reasoning about a battle, a gate, a builder slice, delivery/PR, /battle, /fleet, /retro, or a GitHub-issue-driven task. Defines the phases, the producer/gate split, the verdict cascade, the per-repo state layout and the delegation rules.
---

# Battle workflow — legion doctrine

`legion` is an **orchestrator, not a reimplementation**. It coordinates a battle
pipeline and delegates every concrete .NET task to existing skills
(`dotnet-claude-kit`). If a capability already exists as a skill, **invoke it** —
never duplicate it.

The full design rationale lives in the plugin's `ARCHITECTURE.md`. This skill is
the operational summary loaded at run time.

> **Commands are namespaced.** When you surface a command to the user, always write
> the **namespaced** form — `/legion:battle review`, `/legion:retro`,
> `/legion:fleet` — never bare `/battle` (which resolves to *Unknown command*).
> The bare `/battle`, `/retro`, `/fleet` used throughout this doctrine is shorthand
> for reading, not for relaying verbatim.

## The pipeline

```
THINK  → PLAN     → BUILD    → REVIEW  → TEST     → DELIVER → REFLECT
start    architect  builder    reviewer  test-eng   deliver   retro
         (gate)     (producer) (gate)    (gate)     (PR)
```

Each step **hands off a markdown artifact** to the next. A gate reads the
upstream artifact, judges it, and emits a verdict. The pipeline does **not**
advance on `revise` or `reject` — a fix loops back to BUILD.

**Auto-advance on a clean build.** A build that is `build_ok` with **0 warnings**
chains straight into the gate cascade `review → test → security` (no separate
command), stopping at the first `revise`/`reject` or before `deliver` (delivery
is always a manual, confirmed action). A build with **warnings** does *not*
auto-advance — warnings are remarks; the user fixes them or runs `/battle review`
explicitly. Errors (`build_ok == false`) block as before.

## Two natures of actor

- **Producer** — `builder` only. It *writes* code from `plan.md` and reports in
  `build-report.md`. Not read-only, emits no verdict: its output is what the
  gates review.
- **Gates** — `architect`, `reviewer`, `test-engineer`, `security`. They *judge* a
  deliverable and return a verdict. Read-only (or execute-only), never write battle
  state (the orchestrator persists). Invariant "pure gates".

Sequencing rule: the **orchestrator** (`/battle`) chains `builder → gates`. A gate
never invokes another agent; the builder never invokes a gate.

## Verdict cascade

| Verdict | Meaning | Pipeline effect |
|---|---|---|
| `accept` | 0 FAIL, criteria met | Phase closed, advance. |
| `accept_with_opportunity` | 0 FAIL, ≥1 improvement spotted | Advance; opportunity logged in the artifact. |
| `revise` | ≥1 FAIL | **Stop.** Fix (back to BUILD) and re-run the gate. |
| `reject` | Major regression / unusable | **Stop.** Redesign required. |

## Phases and delegation

| Phase | Driver | Artifact | Delegates to |
|---|---|---|---|
| THINK | `/battle start` | `spec.md` | `gh issue view <n>` (numeric id), else inline |
| PLAN | `architect` gate | `plan.md` (+ test matrix) | `dotnet-claude-kit:clean-architecture`, `:modern-csharp` |
| BUILD | `builder` producer | code + `build-report.md` | `dotnet-claude-kit:scaffold`, `:build-fix`, `:modern-csharp`, `:testing` |
| REVIEW | `reviewer` gate | `gate-review.md` | `dotnet-claude-kit:code-review` (multi-dim: correctness, **plan conformance**, **performance**, conventions, dead code) + Roslyn MCP |
| TEST | `test-engineer` gate | `gate-test.md` | `dotnet-claude-kit:testing`, `:tdd`, `:verify` |
| (sec) | `security` gate | `gate-security.md` | `dotnet-claude-kit:security-scan` |
| DELIVER | `/battle deliver` | `pr-body.md`, `wi-comment.md` | `gh pr create` (PR with `Closes #<n>`), `gh issue comment` |
| REFLECT | `/retro` | `retro.md` | Claude memory |

Gates are optional per battle **profile** (`feature` / `hotfix` / `security` /
`spike`); `battle.json.required_gates` declares which ones block.

## DELIVER

`/battle deliver` (confirmation before push) branches `<me>/<token>` → commit
(Conventional Commits subject + co-author trailer) → compose `pr-body.md` from the
artifacts → push → open the PR via `gh pr create`. For a numeric issue, the PR body
ends with **`Closes #<n>`** so merging auto-closes the issue. Best-effort, non-blocking:
post a short business-facing comment to the issue (`wi-comment.md`) via
`gh issue comment`. Then the human reviews — suggest `/retro` once stabilized.

## State layout

Per repo — `.legion/battles/<battle-id>/`:

```
battle.json   # metadata, profile, required_gates, per-phase status, guard, delivery.pr_url
spec.md  plan.md  build-report.md  gate-review.md  gate-test.md
gate-security.md  pr-body.md  wi-comment.md  usage.jsonl  retro.md
```

`battle.json` schema (write it from this — **no need to open `ARCHITECTURE.md` at
run time**):

```jsonc
{
  "id": "2026-06-08-GH-1234",          // <YYYY-MM-DD>-<token>
  "repo": "billing-api",
  "ticket": "GH#1234",                  // "GH#<n>" or a free slug
  "title": "…",
  "profile": "feature",                 // feature | hotfix | security | spike
  "required_gates": ["architect", "reviewer", "test-engineer"],
  "phases": {
    "think":   { "status": "done", "artifact": "spec.md" },
    "plan":    { "status": "in_progress", "artifact": "plan.md", "verdict": null },
    "build":   { "status": "pending" },
    "review":  { "status": "pending" },
    "test":    { "status": "pending" },
    "deliver": { "status": "pending" },
    "reflect": { "status": "pending" }
  },
  // guard.allow = PLACEHOLDER — `/battle start` derives it from detected *.csproj
  // dirs (e.g. ["HttpForge/**","HttpForge.Tests/**"]); src/**+tests/** matches
  // nothing on a root-projects repo and silently blocks every builder edit.
  "guard": { "allow": ["src/**", "tests/**"], "deny": [], "careful": false },
  "delivery": { "pr_url": null }
}
```

Status ∈ `pending | in_progress | done | blocked`; gate `verdict` ∈
`accept | accept_with_opportunity | revise | reject`.

`usage.jsonl` (append-only) records the **approximate token cost** and the
**skills actually used** — written by the `usage_track` hook on `Stop`
(main session, delta) and `SubagentStop` (delegated builder/gates, which are
invisible to the main session's hooks). `/retro` aggregates it; `fleet_sync`
projects `tokens_total` + `skills` into the shard for the UI.

> A skill is recorded **only when invoked via the `Skill` tool**. The gates and
> the builder therefore carry `Skill` in their tool whitelist so "load
> `code-review`/`scaffold`/…" actually loads the skill (real instructions) **and**
> shows up in the subagent's `skills`. A gate that only applied a skill's method
> from memory would leave `skills` empty.

A new session **resumes** a battle by reading `battle.json` — no conversational
context required. The active battle is pointed to by `.legion/active-battle`.

Global — `~/.claude/legion/`: `fleet.d/` (cross-repo battle index, one JSON shard
per battle, read by `/fleet` and any UI; concurrency-safe — one file per battle, no
shared index, so parallel Claude sessions never overwrite each other). One Claude
session = one repo; the fleet makes battles observable and resumable, it does not
multiplex sessions.

A retro feeds one improvement loop: the **code/project** learning → Claude project
memory. Persist only what would change how the *next* battle is run.

## Guardrails

`/freeze`, `/guard`, `/careful` set `battle.json.guard`. PreToolUse hooks enforce:
`guard.py` **blocks** edits outside `guard.allow` (`exit 2`), `careful.py`
**warns** (never blocks) on destructive shell commands. Bypass:
`LEGION_GUARD_OFF=1`. The `builder` is subject to the same guard.

## Conventions

- **Every markdown artifact is written in French** (`spec.md`, `plan.md`,
  `build-report.md`, `gate-*.md`, `pr-body.md`, `wi-comment.md`, `retro.md`).
  English stays for identifiers, file names, commit messages **and PR titles**.
- **Commit subjects and PR titles follow Conventional Commits** — English,
  `type(scope): subject` (imperative, no trailing period); `type` ∈
  `feat|fix|refactor|perf|docs|test|build|ci|chore`.
- No `Set-Location`/`cd`, no `&&`/`;`/`|` chaining, no shell redirection; run
  `dotnet` from the current directory.
- Hooks are launched via `python "$CLAUDE_PLUGIN_ROOT/hooks/<x>.py"`. If `python`
  is missing the hooks fail silently — the `/battle start` preflight checks it.
- Iterate in small validated steps; never emit large unvalidated blocks.
- Delegate, never duplicate — but stay self-contained for core paths.
