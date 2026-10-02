---
description: Activate the combined guard preset on the active battle — derive the write scope from plan.md slices and deny-list sensitive files.
argument-hint: (no args) | off
---

Activate (or clear) the **combined guard preset** on the active battle.
Arguments: `$ARGUMENTS`

1. Resolve the battle of **this session**: the id known from the conversation, or the id given
   in the arguments → `<state>/.legion/battles/<id>/battle.json`, where `<state>` is the main
   repo root (even when the session runs in the battle's worktree). `active-battle` is only a
   fallback pointer and may name another session's battle. No battle → say so and stop.
   Every `battle_state.py` call below carries `--battle <id>`. If the id is unknown, run the command **without** `--battle`: the guard then blocks it and names the session's battle when the pointer belongs to another session.

2. If `off`: clear `guard.allow` and `guard.deny`, leave `guard.careful` untouched:
   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/battle_state.py" set-guard --allow --deny --battle <id>
   ```
   Then stop.

3. Otherwise build the preset:
   - **allow** ← the file targets declared in the slices of `plan.md` (the paths
     the architect locked), plus the **test directories derived from the real
     layout** — never a blind `tests/**`:
     - **.NET** (`battle.json.stack.kind`): `Glob '**/*.csproj'`, keep the test
       projects (name ending in `.Tests` / `.UnitTests` / `.IntegrationTests`, or
       referencing a test SDK such as `xunit` / `NUnit` / `MSTest`), and add
       `<containing folder>/**` for each. A repo whose projects sit at the root
       (`HttpForge.Tests/`) gets `HttpForge.Tests/**`, not a `tests/**` that
       matches nothing. (`/legion:battle start` §A.1 step 4 derives the whole
       perimeter from every csproj folder; this preset narrows it to the plan, so
       it only adds the test folders.)
     - **Non-.NET**: add the test roots that actually exist — `test/`, `tests/`,
       `__tests__/`, `spec/`, or the roots configured by the test runner
       (`jest` / `vitest` config, `pyproject.toml` `testpaths`, …).
   - **Check every allow glob matches at least one existing path** (or a path a
     slice declares it will create). A glob that matches nothing is a dead perimeter
     that silently blocks the builder: **warn the user**, propose the derived
     replacement, and **persist only after their confirmation** — never persist a
     dead glob blind.
   - **deny** ← sensitive-file patterns regardless of allow:
     `**/appsettings*.json` (secrets sections), `**/*.pfx`, `**/*.pem`,
     `**/secrets.json`, `**/.env`, and any path the repo marks as protected.

4. Persist the preset through the state script (it replaces both lists and leaves
   `guard.careful` untouched):
   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/battle_state.py" set-guard --allow <allow globs…> --deny <deny globs…> --battle <id>
   ```
   (`python3` when `python` is absent.) Never edit `battle.json` by hand: the script
   validates, writes atomically and resyncs the fleet. Exit `2` = refused → relay the
   JSON `reason` and stop.
   Summarize the derived allow/deny and point out that `guard.py` enforces it.

This is the "lock it down to the plan" preset; `/freeze` is the manual,
explicit-globs variant.
