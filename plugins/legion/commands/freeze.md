---
description: Restrict the active battle's write scope to the given globs. The guard.py hook then blocks any edit outside them.
argument-hint: <glob> [<glob> ...] | off
---

Set the write perimeter of the **active battle**. Arguments: `$ARGUMENTS`

1. Resolve the active battle: read `.legion/active-battle` (battle id), then
   `.legion/battles/<id>/battle.json`. If there is no active battle, say so and
   stop — `/freeze` only applies inside a battle.

2. If the argument is `off`: clear `guard.allow` (set to `[]`) so editing is
   unrestricted again. Otherwise set `guard.allow` to the **exact list of globs**
   provided (repo-relative, e.g. `src/Billing.Api/** tests/**`). Do not invent
   globs — use what the user gave.

3. Persist `battle.json`. Confirm the new perimeter and remind that `.legion/**`
   stays writable and that `LEGION_GUARD_OFF=1` is the deliberate bypass.

Note: the enforcement is the `guard.py` PreToolUse hook — this command only
declares the scope.
