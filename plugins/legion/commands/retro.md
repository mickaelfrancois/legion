---
description: Close a battle with a retrospective — synthesize its artifacts into retro.md, persist one durable code learning to project memory, journal any tooling (plugin) RETEX centrally, file out-of-scope opportunities as deduplicated GitHub issues on the target repo, and close the battle.
argument-hint: (no args = active battle) | <battle-id>
---

Run the **REFLECT** phase. Arguments: `$ARGUMENTS`

1. **Resolve the battle**: the given `<battle-id>`, else the battle of **this session** (the id
   known from the conversation; its binding is `.legion/sessions/<key>.json`, checked with
   `battle_state.py session-status --battle <id>`). The `.legion/active-battle` pointer (read in
   the **main repo**) is only a last fallback: with concurrent battles it may name another
   session's battle. No battle → say so and stop. Every `battle_state.py` call below carries
   `--battle <id>`.

   **State root `<state>`.** A battle may run in a dedicated worktree
   (`<main>/.claude/worktrees/<id>`), but its state lives in the **main repo**. Every
   `.legion/…` path below means `<state>/.legion/…`, where `<state>` is the `state_root` printed
   by `python "${CLAUDE_PLUGIN_ROOT}/scripts/battle_worktree.py" where --battle <id>` (JSON,
   read-only, no network; `<id>` validated as in step 1b). Check it is an existing absolute
   directory and use **absolute** paths in shell commands and in `Read` / `Write`, never a path
   built from the cwd. This retrospective **removes nothing**: the worktree and the branch stay
   until `/legion:battle close <battle-id>` (after the PR is merged).

1b. **Check the PR state.** Validate `<battle-id>` (from `$ARGUMENTS`) against
   `^[A-Za-z0-9][A-Za-z0-9-]*$` before any shell call; never substitute `$ARGUMENTS` raw.
   If `battle.json` has `delivery.pr_url`, refresh it as in `/legion:battle status` (§C):
   take `<n>` from the tail of `pr_url` (`^[0-9]+$`), run
   ```bash
   gh pr view <n> --json state,mergedAt,statusCheckRollup,url,headRefName,headRefOid > "<state>/.legion/battles/<id>/pr-status.json"
   ```
   and, only if `gh` exited `0`:
   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/battle_state.py" set-delivery --pr-json "<state>/.legion/battles/<id>/pr-status.json" --battle <id>
   ```
   - `open` → warn that the PR is not merged and **ask for confirmation** before going on.
   - `closed` → note it in `retro.md` (`Outcome`: `Shipped: no — PR fermée sans merge`).
   - `merged` → `Shipped: yes`.
   - No `gh`, or `gh` fails → warn and go on.

2. **Read its artifacts** under `<state>/.legion/battles/<id>/`: `spec.md`, `plan.md`,
   every `gate-*.md`, `build-report.md`, `pr-body.md`, and `pr-feedback.md` when the
   battle went through ADDRESS (its rounds show what human review caught). Reconstruct the story: what
   shipped, what got blocked and why (gate `revise`/`reject` + the FAILs), how
   many build/gate round-trips, which opportunities were logged. **Also collect every
   `## Hors périmètre — candidats issue` section** (out-of-scope observations logged by
   the gates and the builder) — you turn the actionable ones into GitHub issues at
   step 7.

   Also read **`usage.jsonl`** if present (written by the `usage_track` hook): each
   line is `{scope, agent_type?, skills[], tokens{input,output,…}}`. Aggregate it:
   `tokens_total = Σ(input+output)`, and the **unique set of skills** actually used
   (across the main session and the delegated subagents). This is approximate (see
   the hook's caveats) — present it as such.

   As you reconstruct, **separate two kinds of friction**: friction in the *code/
   project* (.NET) vs friction in the *tooling* — the plugin itself (a gate too
   strict/lax, a missing step, a confusing message, a guardrail that got in the way,
   a delegated skill that misbehaved, an awkward command flow).

3. **Synthesize `retro.md`** (write it in the battle dir, **in French** —
   identifiers & file names stay English). Apply the **writing charter**
   (`battle-workflow` § « Charte de style des documents ») — simple, precise language;
   reference it, do not copy it. The **« En bref »** is **conditional**: add a
   `## En bref` section at the top only when `retro.md` passes **~40 lines** (1-3
   lines: shipped? + the main learning). Reread `retro.md` against the charter before
   closing the battle:

   ```markdown
   # Retro — <title> (<battle-id>)

   ## Outcome
   - Shipped: <yes/no> — <one line>
   - Round-trips: build×N, review×N, ...
   - PR : <pr_state> · CI : <ci>

   ## Cost (approximate)
   - Tokens: ~<tokens_total> (subagents <Σ>, main <Σ>)
   - Skills used: <scaffold, code-review, build-fix, …> (or "none recorded")

   ## What worked
   - ...

   ## What slowed us down
   - <gate that blocked, root cause — not the symptom>

   ## Decisions taken
   - <archi / scope decisions worth remembering>

   ## RETEX — durable learnings
   - <pattern likely to recur on the NEXT battle, any repo>

   ## Plugin RETEX (tooling)
   - [<severity>] <plugin>/<area> — <what rubbed during the battle>
     → <suggested adaptation>
   <!-- or: "RAS — the tooling did not get in the way this battle" -->
   ```

   **Scope of "the plugin"**: `legion` first (orchestrator, gates, hooks,
   commands); each item may instead name a **delegated** plugin
   (`dotnet-claude-kit`) when it is the source of the friction. `severity` ∈
   `blocker|friction|annoyance|idea`; `area` like `gate:reviewer`, `hook:guard`,
   `command:/battle deliver`, `skill:scaffold`.

4. **Close the battle first** (release the guard before writing out-of-repo):
   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/battle_state.py" close --battle <id>
   ```
   It sets `phases.reflect.status = "done"` (and resyncs the fleet, dropping the
   battle from the active view), clears `<state>/.legion/active-battle` when it points at
   this battle, and drops the session bindings to it (`unbound: n`) — the guard relaxes, the
   battle is over. Never edit `battle.json` by
   hand; exit `2` = refused → relay the JSON `reason`. `retro.md` lives under `.legion/` (always
   writable), so it is already persisted by step 3 regardless of order.

   > **Order matters.** The out-of-repo writes that follow — the memory write (step 5,
   > `~/.claude/projects/<slug>/memory/`), the central RETEX journal (step 6,
   > `~/.claude/legion/plugin-retex.jsonl`), and the opportunity issues (step 7,
   > `gh issue create` on the target repo) — all land **outside the repo**.
   > `guard.py` now exempts the memory path, but closing the battle here first means
   > the perimeter is already released as a second safety net (RETEX: the memory
   > `Write` was blocked when it ran before the close with a perimeter still active).
   > The per-battle `plugin-retex.json` (step 6) lives under `.legion/**`, which the
   > guard always allows, so writing it is safe regardless of order.

5. **Persist ONE durable learning to project memory.** From the `RETEX` section,
   pick the single most reusable, repo-agnostic insight (a recurring pitfall, a
   convention that should have been followed, a gate that keeps catching the same
   thing). Write it as a concise `project` fact in the project's Claude memory —
   **not** the whole retro. Capture rule: persist only what would change how the
   *next* battle is run; skip one-off details. If nothing meets that bar, say so
   and persist nothing.

6. **Persist the Plugin RETEX to the central journal** (tooling improvement loop).
   From the `Plugin RETEX` section, write the structured items to
   `<state>/.legion/battles/<id>/plugin-retex.json` (a JSON array of
   `{plugin, area, severity, observation, suggestion, title, intent, phase, profile}`),
   then append them to the cross-battle journal:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/plugin_retex.py" append \
     --file "<state>/.legion/battles/<id>/plugin-retex.json" --battle "<id>" --repo "<repo>"
   ```

   **Embed the battle context in each item** so the entry stays self-contained once
   the repo/worktree is gone (the journal lives in `~/.claude/legion/`, the battle
   artifacts do not — this is the whole point: a RETEX must be actionable without the
   repo). For every item set: `title` and `profile` from `battle.json`, `intent` a
   one-line summary of `spec.md`'s intent, and `phase` = the phase where the friction
   surfaced (`think|plan|build|lint|review|test|security|deliver|address|reflect`) — **per item**, since one
   battle's frictions can arise at different phases. These four fields are **optional**
   and never affect the entry's stable `id` (derived from `ts|plugin|observation`);
   `--battle`/`--repo` stay as they are.

   If the tooling did not get in the way (`RAS`), skip this — write nothing. This
   journal (`~/.claude/legion/plugin-retex.jsonl`) is how plugin improvements are
   prioritized across all battles; the Legatus UI surfaces the open ones on its
   **RETEX** page (`/retex`), and the CLI lists them with `plugin_retex.py list`.
   Each entry has a stable **id**: once a suggestion is acted on (plugin change
   shipped), **close it** with `plugin_retex.py resolve <id>` (append-only tombstone
   — `list` then hides it; `--all` / `--resolved` and the UI's *Résolus* toggle show
   history). This is what keeps the list from re-surfacing already-handled items.

7. **File out-of-scope opportunities as GitHub issues** (the target-repo backlog loop —
   the **third** REFLECT output, alongside the project-memory learning of step 5 and the
   central plugin-RETEX journal of step 6). During a battle, gates and the builder log
   observations that fall **outside** the feature's scope in a
   `## Hors périmètre — candidats issue` section of their artifact. Turn the actionable
   ones into follow-up issues on the **target repo**, **without duplicates**. All `gh`
   writes stay here — the script never touches the network.

   a. **Aggregate** every `## Hors périmètre — candidats issue` section from the artifacts
      read at step 2 (`plan.md`, each `gate-*.md`, `build-report.md`). No such section
      anywhere → skip this step entirely (write nothing, report `RAS`).

   b. **Filter** — keep only what is **out of scope** AND **actionable** AND
      **substantial**. Drop nits, vague remarks, and anything the shipped work already
      covers. A loose filter spams the repo; be strict.

   c. **Build the candidate list + fetch existing issues.** Write the kept candidates to
      `<state>/.legion/battles/<id>/opportunities.json` (under the battle dir — always writable,
      git-ignored), each entry `{ title, zone, kind, observation, out_of_scope, lead,
      phase }` (the section's sub-template). Fetch existing issues in **one** call:

      ```bash
      gh issue list --state all --label legion-opportunity --json number,title,body,state --limit 500
      ```

      Write `{ "candidates": [...], "issues": [...] }` to
      `<state>/.legion/battles/<id>/opp-dedup-in.json`, then classify (deterministic, offline):

      ```bash
      python "${CLAUDE_PLUGIN_ROOT}/scripts/opportunity.py" dedup --file "<state>/.legion/battles/<id>/opp-dedup-in.json"
      ```

      It returns `{ to_create, duplicates, probable }`. **`duplicates`** (fingerprint
      already on an issue, **open or closed**) are **skipped** — the GitHub close is the
      tombstone, never recreate them. **`probable`** (title overlaps an **open** issue) is
      **advisory**: surface it, let the human decide.

   d. **Render each `to_create` body** — the script owns the format and the fingerprint
      marker (`<!-- legion-opportunity: <fp> -->`), the pivot of the anti-duplicate net:

      ```bash
      python "${CLAUDE_PLUGIN_ROOT}/scripts/opportunity.py" render --file <candidate.json> --battle "<id>" --origin-issue <n> --out "<state>/.legion/battles/<id>/opp-<fingerprint>.md"
      ```

   e. **CONFIRM (outward effect).** Show the user the list to create — each title + body,
      plus any `probable` overlap flag. **Wait for an explicit OK** (same discipline as
      `recon` / `deliver`). **Never create an issue without it.** The human may drop any
      candidate (e.g. a confirmed `probable` duplicate).

   f. **Create** each confirmed candidate (create the label once, best-effort):

      ```bash
      gh label create legion-opportunity --description "Opportunité hors-scope repérée par une battle legion" --color BFD4F2
      gh issue create --label legion-opportunity --title "<title>" --body-file "<state>/.legion/battles/<id>/opp-<fingerprint>.md"
      ```

      `gh label create` fails harmlessly if the label already exists — ignore that error.
      Record the created issue URLs for the report.

   g. **Degrade gracefully if `gh` is absent/unauthenticated**: do not fail — print the
      rendered issues (title + body) **ready to paste** and tell the user to create them
      manually (same fallback as `recon`'s create path).

8. **Report**: the outcome summary, the learning persisted (or why none), the
   plugin-RETEX items journaled (or `RAS`), **the opportunity issues created (or why
   none, or the paste-ready fallback)**, and the path to `retro.md`. The
   tooling-friction notes also stay in `retro.md` — review them when you next
   iterate on the plugin itself.

Delegation: this is the only stack phase that writes to long-term memory. The
code/project learning goes to Claude memory; the **tooling** learning goes to the
central plugin-retex journal; the **out-of-scope opportunities** go to GitHub issues on
the target repo. Keep battle artifacts in the battle dir; keep memory lean.
