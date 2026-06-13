---
description: Toggle careful mode on the active battle — the careful.py hook then warns (never blocks) on destructive shell commands.
argument-hint: (no args = on) | off
---

Toggle **careful mode** on the active battle. Arguments: `$ARGUMENTS`

1. Resolve the active battle (`.legion/active-battle` → `battle.json`). No active
   battle → say so and stop.

2. Set `guard.careful` to `true` (default / no arg) or `false` (`off`). Persist
   `battle.json`.

3. Confirm. When on, the `careful.py` PreToolUse hook warns on destructive
   commands (`rm -rf`, `git reset --hard`, `git push --force`,
   `Remove-Item -Recurse -Force`, `dotnet ef database drop`, `DROP TABLE`…) —
   it **warns, never blocks**. The destructive-pattern list lives in
   `hooks/careful.py`.
