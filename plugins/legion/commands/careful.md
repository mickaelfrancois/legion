---
description: Toggle careful mode on the active battle — the careful.py hook then warns (never blocks) on destructive shell commands.
argument-hint: (no args = on) | off
---

Toggle **careful mode** on the active battle. Arguments: `$ARGUMENTS`

1. Resolve the battle of **this session**: the id known from the conversation, or the id given
   in the arguments → `<state>/.legion/battles/<id>/battle.json`, where `<state>` is the main
   repo root (even when the session runs in the battle's worktree). `active-battle` is only a
   fallback pointer and may name another session's battle. No battle → say so and stop.
   Every `battle_state.py` call below carries `--battle <id>`. If the id is unknown, run the command **without** `--battle`: the guard then blocks it and names the session's battle when the pointer belongs to another session.

2. Set `guard.careful` through the state script — `on` by default / no arg, `off`
   for `off`:
   ```bash
   python "$CLAUDE_PLUGIN_ROOT/scripts/battle_state.py" set-guard --careful on --battle <id>
   ```
   (`python3` when `python` is absent.) Never edit `battle.json` by hand: the script
   validates, writes atomically and resyncs the fleet. Exit `2` = refused → relay the
   JSON `reason` and stop.

3. Confirm. When on, the `careful.py` PreToolUse hook warns on destructive
   commands (`rm -rf`, `git reset --hard`, `git push --force`,
   `Remove-Item -Recurse -Force`, `dotnet ef database drop`, `DROP TABLE`…) —
   it **warns, never blocks**. The destructive-pattern list lives in
   `hooks/careful.py`.
