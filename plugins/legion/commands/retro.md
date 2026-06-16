---
description: Close a battle with a retrospective — synthesize its artifacts into retro.md, persist one durable learning to project memory, and close the battle.
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

   > **Order matters.** The memory write (step 5) targets `~/.claude/projects/<slug>/
   > memory/` — **outside the repo**. `guard.py` now exempts that path, but closing
   > the battle here first means the perimeter is already released as a second
   > safety net (RETEX: the memory `Write` was blocked when it ran before the close
   > with a perimeter still active).

5. **Persist ONE durable learning to project memory.** From the `RETEX` section,
   pick the single most reusable, repo-agnostic insight (a recurring pitfall, a
   convention that should have been followed, a gate that keeps catching the same
   thing). Write it as a concise `project` fact in the project's Claude memory —
   **not** the whole retro. Capture rule: persist only what would change how the
   *next* battle is run; skip one-off details. If nothing meets that bar, say so
   and persist nothing.

6. **Report**: the outcome summary, the learning persisted (or why none), and the
   path to `retro.md`. The tooling-friction notes stay in `retro.md` — review them
   when you next iterate on the plugin itself.

Delegation: this is the only stack phase that writes to long-term memory. Keep
battle artifacts in the battle dir; keep memory lean.
