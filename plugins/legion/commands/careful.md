---
description: Toggle careful mode on the active battle — the careful.py hook then warns (never blocks) on destructive shell commands.
argument-hint: (no args = on) | off
---

Toggle **careful mode** on the active battle. Arguments: `$ARGUMENTS`

1. Resolve the active battle (`.legion/active-battle` → `battle.json`). No active
   battle → say so and stop.

2. Set `guard.careful` through the state script — `on` by default / no arg, `off`
   for `off`:
   ```bash
   python "$CLAUDE_PLUGIN_ROOT/scripts/battle_state.py" set-guard --careful on
   ```
   (`python3` when `python` is absent.) Never edit `battle.json` by hand: the script
   validates, writes atomically and resyncs the fleet. Exit `2` = refused → relay the
   JSON `reason` and stop.

3. Confirm. When on, the `careful.py` PreToolUse hook warns on destructive
   commands (`rm -rf`, `git reset --hard`, `git push --force`,
   `Remove-Item -Recurse -Force`, `dotnet ef database drop`, `DROP TABLE`…) —
   it **warns, never blocks**. The destructive-pattern list lives in
   `hooks/careful.py`.
