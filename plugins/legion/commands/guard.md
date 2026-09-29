---
description: Activate the combined guard preset on the active battle — derive the write scope from plan.md slices and deny-list sensitive files.
argument-hint: (no args) | off
---

Activate (or clear) the **combined guard preset** on the active battle.
Arguments: `$ARGUMENTS`

1. Resolve the active battle (`.legion/active-battle` → `battle.json`). No active
   battle → say so and stop.

2. If `off`: clear `guard.allow` and `guard.deny`, leave `guard.careful` untouched.

3. Otherwise build the preset:
   - **allow** ← the file targets declared in the slices of `plan.md` (the paths
     the architect locked), plus the **test project directories derived from the
     real layout** — never a blind `tests/**`. Detect them like `/legion:battle
     start` does (§A.1 step 4): `Glob '**/*.csproj'`, keep the test projects (name
     ending in `.Tests` / `.UnitTests` / `.IntegrationTests`, or referencing a test
     SDK such as `xunit` / `NUnit` / `MSTest`), and add `<containing folder>/**` for
     each. A repo whose projects sit at the root (`HttpForge.Tests/`) gets
     `HttpForge.Tests/**`, not a `tests/**` that matches nothing. This keeps edits
     within what the plan actually touches.
   - **Check every allow glob matches at least one existing path** (or a path a
     slice declares it will create). A glob that matches nothing is a dead perimeter
     that silently blocks the builder: **warn the user** and show the derived
     replacement instead of persisting it blind.
   - **deny** ← sensitive-file patterns regardless of allow:
     `**/appsettings*.json` (secrets sections), `**/*.pfx`, `**/*.pem`,
     `**/secrets.json`, `**/.env`, and any path the repo marks as protected.

4. Persist `battle.json`. Summarize the derived allow/deny and point out that
   `guard.py` enforces it.

This is the "lock it down to the plan" preset; `/freeze` is the manual,
explicit-globs variant.
