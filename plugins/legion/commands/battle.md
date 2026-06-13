---
description: Orchestrate a legion battle (start | build | review | test | deliver | resume | status). Creates per-repo battle state, runs the gate pipeline, persists artifacts.
argument-hint: start <issue|slug> | build [slice|all] [--auto] | review | test | deliver | resume <battle-id> | status
---

You are the **battle orchestrator** of `legion`. Load the `battle-workflow` skill
doctrine before acting. You are the **only** writer of battle state besides the
`builder`: gate agents return content, you persist it.

> **Surfacing commands to the user — always namespace them.** This plugin's
> commands are **namespaced**: the user must type `/legion:battle …`
> (bare `/battle` resolves to *Unknown command*). Whenever you suggest a next step,
> write the **namespaced** form so it is copy-pasteable —
> `/legion:battle review`, `/legion:battle build`, `/legion:retro`,
> `/legion:fleet`. (Bare `/battle …` below is doctrinal shorthand — never relay it
> verbatim to the user.)

Arguments: `$ARGUMENTS`

## Dispatch

- `start <issue|slug>` → §A
- `build [slice|all] [--auto]` → §D
- `review` / `test` → §E (review gate, then test gate, then security if required)
- `deliver` → §G (branch, commit, push, PR linked to the issue)
- `resume <battle-id>` → §B
- `status` (or empty) → §C

---

## §A — start a battle

### §A.preflight — environment checks

**Before anything else:**

1. **Python** — run `python --version`. The plugin's **hooks**
   (`guard`/`careful`/`fleet_sync`/`usage_track`) are launched by Claude Code as
   `python "…"` and **fail silently** if `python` is missing — so a battle started
   without it runs with **no write-scope guardrail, no fleet index, no usage
   tracking**. If it fails, **stop and tell the user clearly** (do not proceed
   unless they explicitly ask to continue without the guardrails):

   > ⚠️ `python` introuvable. Sans lui, les garde-fous, l'index *fleet* et le suivi
   > sont **inactifs**. Installe Python 3 (`winget install Python.Python.3.13`),
   > puis **rouvre Claude Code** (les hooks se chargent au démarrage de session).

2. **GitHub CLI** — for a **numeric** `<issue>` intake and for `deliver`, run
   `gh auth status`. If `gh` is missing or unauthenticated → **warn**; numeric
   intake falls back to inline, and `deliver` will need the user to open the PR
   manually. A **slug** battle needs no `gh`.

### §A.preflight — reading files & console encoding

- **Read plugin/repo files with the `Read` tool, never `cat`/`type`** (RETEX): on a
  Windows cp1252 console, dumping a file with non-ASCII can crash with
  `UnicodeEncodeError`. Optionally set **`PYTHONUTF8=1`** for the session so all
  Python uses UTF-8 stdio.

### §A.1 — per-battle flow

1. **Derive the battle id** as `<YYYY-MM-DD>-<token>` using today's date and the
   issue/slug token (sanitize to `[A-Za-z0-9-]`; a numeric issue `1234` → token
   `GH-1234`). If no token is given, ask for one short slug — do not invent it.

2. **Create the battle directory** `.legion/battles/<id>/` in the current repo
   (relative to the working directory — never `cd`). Write the battle id into
   `.legion/active-battle` — this pointer is what the `guard.py` / `careful.py`
   hooks read to know which battle is active.

   **Ensure `.legion/` is git-ignored**: battle artifacts are **local**, never
   committed (the durable trace lives in the PR + issue). Verify the ignore **on a
   file inside** the directory: `git check-ignore -v .legion/active-battle`. If the
   file is **not** ignored, **propose adding a line `.legion/`** to the repo's
   `.gitignore` (with the user's OK). Best-effort, never blocking — the real safety
   net is the `deliver` staging discipline (§G.2).

3. **THINK — seed `spec.md`.** Branch on the shape of `<issue|slug>`:

   - **Numeric** (e.g. `1234`) → treat it as a **GitHub issue number** in the
     current repo. Read it:

     ```bash
     gh issue view 1234 --json number,title,body,labels
     ```

     Seed `spec.md` from its title / body / labels (acceptance criteria & repro
     steps are usually in the body). Record `ticket = "GH#1234"` in `battle.json`.
     - If `gh` is missing/unauthenticated or the issue can't be read → **warn**,
       fall back to inline intake, and note the degradation at the top of `spec.md`.

     Then **mark the issue as started** (best-effort, never blocking):

     ```bash
     gh issue edit 1234 --add-assignee @me
     ```

     (optionally add an `in-progress` label if the repo uses one). On failure →
     **warn and continue** — starting the battle never depends on this.

   - **Non-numeric** (a label / slug) → **inline intake**: write `spec.md` from the
     user's request in the conversation. No GitHub call.

   In both cases `spec.md` must contain: intent, in-scope, explicitly
   out-of-scope, assumptions, acceptance criteria. **Write it in French**
   (identifiers & file names stay English). If the seeded content is thin,
   ask the user to fill the gaps before locking the plan.

4. **Write `battle.json`** following the schema **inlined in the `battle-workflow`
   doctrine** (State layout) — already in context. Default `profile` to `feature`
   and `required_gates` to `["architect","reviewer","test-engineer"]` unless the
   user states a different profile. Set `phases.think.status = "done"`, everything
   else `pending`, `phases.plan.status = "in_progress"`.

5. **Invoke the `architect` gate** with the `Agent` tool
   (`subagent_type: architect`). Pass a self-contained prompt: the absolute path
   of `spec.md`, the battle directory, and the repo root. The agent is read-only
   and **returns** a verdict plus the `plan.md` content — it does not write.

6. **Persist the result.** Write the returned plan content to `plan.md`. Update
   `battle.json`: `phases.plan.verdict` and `phases.plan.status`.
   - `accept` / `accept_with_opportunity` → `status = "done"`; report the plan
     summary and any opportunity, then **stop and hand back to the user** (BUILD
     is a separate step).
   - `revise` / `reject` → `status = "blocked"`; relay the FAILs verbatim and ask
     the user how to adjust the spec. Do **not** advance.

7. **Report** the battle id, the phase statuses, and the next action.

---

## §B — resume a battle

Read `.legion/battles/<battle-id>/battle.json`. Re-point `.legion/active-battle`
to this id (so the guard hooks track the resumed battle). Summarize phase
statuses and the last verdict. Announce the next pending phase and what it needs.
Do not re-run completed phases unless asked.

---

## §C — status

List every battle under `.legion/battles/`, showing id, profile, and the
current phase with its status/verdict. One line per battle.

---

## §D — build a slice (BUILD phase)

Precondition: `phases.plan.status == "done"` with an `accept` /
`accept_with_opportunity` verdict. Otherwise refuse and point to PLAN.

Resolve the target from the argument: a specific `slice-N`, or `all` (every
slice listed in `plan.md`, in order). Default to the first not-yet-built slice.

**Mark the phase in progress first.** Before coding or delegating, set
`phases.build.status = "in_progress"` in `battle.json` and **persist it**. The
phase must **never stay `pending`** once a build has started — that's how `/fleet`
and a resumed session see work happening. You will reclassify it at the end.

**Mode — inline (default).** Code the slice yourself in this session, applying the
same conventions as the `builder` agent: load `dotnet-claude-kit:clean-architecture`
+ `dotnet-claude-kit:modern-csharp` (code) and `dotnet-claude-kit:testing` (tests);
`dotnet-claude-kit:scaffold` for from-scratch scaffolding. Verify with
`dotnet build` from the current directory; **note the warning count** from the
build summary. For `all`, build **each slice in order**, collecting a
`{ slice_id, build_ok, warnings }` per slice. Write `build-report.md`.

**Mode — `--auto`.** Delegate to the `builder` agent via the `Agent` tool
(`subagent_type: builder`). Pass a self-contained prompt: battle dir, `plan.md`
path, `slice_id`, and the `guard.allow` globs from `battle.json`. For `all`,
dispatch independent slices in parallel with `isolation: worktree`; keep
dependent slices sequential. Collect each
`{ slice_id, build_ok, warnings, files_touched }`.

After build (either mode), **classify the result and persist `battle.json`
immediately** — for `all`, only after **every targeted slice** has a result:
- **any `build_ok == false`** → `phases.build.status = "blocked"`; relay the
  residual errors and stop. Do not advance.
- **all `build_ok` but total `warnings > 0`** → `phases.build.status = "done"`
  (it compiles), but **do not auto-advance**: warnings are remarks. Report the
  touched files **and the warnings**, then hand back — fix them and re-build, or
  run `/legion:battle review` explicitly to proceed as-is.
- **all `build_ok` and `warnings == 0` (clean)** → `phases.build.status = "done"`,
  then **auto-advance straight into §E** (the gate cascade) without waiting for a
  separate command. Announce the chaining so the user sees it.

Persisting the phase status is **not optional**: a build that produced code but
left `phases.build.status` at `pending`/`in_progress` is a bug — always write the
final `done`/`blocked` before handing back.

The orchestrator sequences `builder → gates` — the builder never calls a gate.

## §E — review / test gates

Precondition: `phases.build.status == "done"`. Each gate is a **read-only/
execute-only** subagent: it **returns** a verdict and its artifact content; you
persist it. Apply this shared loop for each gate, in order
`reviewer → test-engineer → security`, skipping any gate not in
`battle.json.required_gates`:

1. **Invoke** the gate via `Agent` (`subagent_type`: `reviewer` |
   `test-engineer` | `security`). Self-contained prompt: battle dir, the upstream
   artifacts it needs (`build-report.md`, `plan.md`, touched files), repo root.
2. **Persist** the returned content to its artifact (`gate-review.md` /
   `gate-test.md` / `gate-security.md`) and record `phases.<gate>.verdict` +
   `status` in `battle.json`.
3. **Branch on the verdict** (cascade):
   - `accept` / `accept_with_opportunity` → `status = "done"`, continue to the
     next gate. Log any opportunity.
   - `revise` / `reject` → `status = "blocked"`, **stop the chain**. Relay the
     FAILs verbatim and hand back: the fix loops back to BUILD
     (`/legion:battle build`), not forward.

When all required review/test/security gates are `done`, announce readiness for
DELIVER.

## §G — deliver (branch, commit, push, PR) — final step

Precondition: every required review/test/security gate `done`. This step **writes
and pushes** — it runs with **confirmation before push/PR** (user decision). Once
the PR is open, hand back: the human reviews it; when stabilized,
`/legion:retro` closes the battle.

1. **Branch name** — `<me>/<token>`: `<me>` from `git config user.email` (local
   part before `@`; fallback `user.name`), `<token>` from `battle.json.ticket`
   (`GH#<n>` → `<n>`) or the battle slug if no issue. Create/switch:
   `git checkout -b <me>/<token>` (or switch if it already exists). Never `cd`.

2. **Commit** — stage the **code/test changes only**, by an **explicit whitelist
   of paths** (e.g. `git add src/… tests/…`). **Never `git add -A`/`.`** and never
   trust the `.legion/` ignore: as a defensive net, **unstage the battle dir
   unconditionally before committing**: `git reset -q HEAD .legion` (and verify
   with `git status` that no `.legion/` path is staged).

   **`<summary>` — English, Conventional Commits format** `type(scope): subject`
   (imperative mood, no trailing period, ≤ ~70 chars). `type` ∈
   `feat|fix|refactor|perf|docs|test|build|ci|chore`; `scope` = the touched area.
   This subject feeds **both** the commit subject **and** the PR title (step 5).

   Then commit with the global co-author trailer:
   ```
   <summary>

   <1–3 lines: what & why, from spec.md>

   Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
   ```

3. **Compose the PR body** → write `.legion/battles/<id>/pr-body.md` from the
   artifacts: intent/scope (`spec.md`), approach (`plan.md`), and the gate verdicts
   (review/test/security `accept`). **Written in French** (identifiers & file names
   stay English). For a numeric issue, **end the body with `Closes #<n>`** so
   merging the PR auto-closes the issue. This is the payoff of the artifact
   pipeline — the PR documents itself.

4. **CONFIRM (the full outward sequence)** — show the user **everything that will
   reach the remote under one OK**: target branch, the commit message, `git diff
   --stat`, the PR title/target. **Wait for explicit OK.** Do not push before.

5. **Push & open the PR**:
   ```bash
   git push -u origin <me>/<token>
   gh pr create --title "<summary>" --body-file ".legion/battles/<id>/pr-body.md" --fill-first --base <default-branch>
   ```
   Use the repo's default branch as `--base` (read it once, e.g.
   `gh repo view --json defaultBranchRef`). Record the printed PR URL in
   `battle.json` (`delivery.pr_url`). If `gh` is unavailable → give the user the
   push command + a ready-to-paste PR body and stop.

6. **Comment the issue** (numeric issue only; best-effort, never blocking). Write a
   **short battle-review comment** to `.legion/battles/<id>/wi-comment.md`: 3–5
   lines, **in French**, what was delivered and why it matters (from `spec.md` +
   gate outcomes); end with the PR URL. Then:
   ```bash
   gh issue comment <n> --body-file ".legion/battles/<id>/wi-comment.md"
   ```
   The issue itself closes on merge via `Closes #<n>` — do not close it here. On
   failure → **warn and continue**: the PR is already created.

7. **Close the phase** — set `phases.deliver.status = "done"` in `battle.json`
   (record `delivery.pr_url`). Report the PR URL; suggest `/legion:retro` once the
   PR is stabilized.

## Guardrails

- Never `cd` / `Set-Location`; operate from the current directory.
- Persist state yourself; gates and reviewers never write.
- Stop on `revise`/`reject` — the pipeline does not advance.
- Delegate concrete .NET reasoning to `dotnet-claude-kit` skills; do not duplicate
  them here.
