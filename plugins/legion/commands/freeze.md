---
description: Restrict the active battle's write scope to the given globs. The guard.py hook then blocks any edit outside them.
argument-hint: <glob> [<glob> ...] | off
---

Set the write perimeter of the **active battle**. Arguments: `$ARGUMENTS`

1. Resolve the active battle: read `<state>/.legion/active-battle` (battle id), then
   `<state>/.legion/battles/<id>/battle.json`, where `<state>` is the main repo root (even when
   the session runs in the battle's worktree). If there is no active battle, say so and
   stop — `/freeze` only applies inside a battle.

2. Update the perimeter through the state script:
   - `off` → clear `guard.allow` so editing is unrestricted again:
     ```bash
     python "$CLAUDE_PLUGIN_ROOT/scripts/battle_state.py" set-guard --allow
     ```
   - otherwise → set `guard.allow` to the **exact list of globs** provided
     (repo-relative, e.g. `src/Billing.Api/** tests/**`). Do not invent globs — use
     what the user gave:
     ```bash
     python "$CLAUDE_PLUGIN_ROOT/scripts/battle_state.py" set-guard --allow <glob> [<glob> ...]
     ```
   (`python3` when `python` is absent.) Never edit `battle.json` by hand: the script
   validates, writes atomically and resyncs the fleet. Exit `2` = refused → relay the
   JSON `reason` and stop.

3. Confirm the new perimeter and remind that `.legion/**`
   stays writable and that `LEGION_GUARD_OFF=1` is the deliberate bypass.

Note: the enforcement is the `guard.py` PreToolUse hook — this command only
declares the scope.
