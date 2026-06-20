---
description: Close a battle with a retrospective — synthesize its artifacts into retro.md, persist one durable code learning to project memory, journal any tooling (plugin) RETEX centrally, and close the battle.
argument-hint: (no args = active battle) | <battle-id>
---

Run the **REFLECT** phase. Arguments: `$ARGUMENTS`

1. **Resolve the battle**: the given `<battle-id>`, else the active one
   (`.legion/active-battle`). No battle → say so and stop.

2. **Read its artifacts** under `.legion/battles/<id>/`: `spec.md`, `plan.md`,
   every `gate-*.md`, `build-report.md`, `pr-body.md`. Reconstruct the story: what
   shipped, what got blocked and why (gate `revise`/`reject` + the FAILs), how
   many build/gate round-trips, which opportunities were logged.

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
   identifiers & file names stay English):

   ```markdown
   # Retro — <title> (<battle-id>)

   ## Outcome
   - Shipped: <yes/no> — <one line>
   - Round-trips: build×N, review×N, ...

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

4. **Close the battle first** (release the guard before writing out-of-repo): set
   `phases.reflect.status = "done"` in `battle.json` (this resyncs the fleet,
   dropping it from the active view), and clear `.legion/active-battle` (the guard
   relaxes — the battle is over). `retro.md` lives under `.legion/` (always
   writable), so it is already persisted by step 3 regardless of order.

   > **Order matters.** The out-of-repo writes that follow — the memory write (step 5,
   > `~/.claude/projects/<slug>/memory/`) and the central RETEX journal (step 6,
   > `~/.claude/legion/plugin-retex.jsonl`) — both land **outside the repo**.
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
   `.legion/battles/<id>/plugin-retex.json` (a JSON array of
   `{plugin, area, severity, observation, suggestion, title, intent, phase, profile}`),
   then append them to the cross-battle journal:

   ```bash
   python "$CLAUDE_PLUGIN_ROOT/scripts/plugin_retex.py" append \
     --file ".legion/battles/<id>/plugin-retex.json" --battle "<id>" --repo "<repo>"
   ```

   **Embed the battle context in each item** so the entry stays self-contained once
   the repo/worktree is gone (the journal lives in `~/.claude/legion/`, the battle
   artifacts do not — this is the whole point: a RETEX must be actionable without the
   repo). For every item set: `title` and `profile` from `battle.json`, `intent` a
   one-line summary of `spec.md`'s intent, and `phase` = the phase where the friction
   surfaced (`think|plan|build|review|test|deliver|reflect`) — **per item**, since one
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

7. **Report**: the outcome summary, the learning persisted (or why none), the
   plugin-RETEX items journaled (or `RAS`), and the path to `retro.md`. The
   tooling-friction notes also stay in `retro.md` — review them when you next
   iterate on the plugin itself.

Delegation: this is the only stack phase that writes to long-term memory. The
code/project learning goes to Claude memory; the **tooling** learning goes to the
central plugin-retex journal. Keep battle artifacts in the battle dir; keep memory
lean.
