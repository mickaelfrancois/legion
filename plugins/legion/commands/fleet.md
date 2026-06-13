---
description: Consolidated view of legion battles across all repos (the multi-repo Conductor view). Reads the global fleet index (per-battle shards).
argument-hint: (no args = active) | all | prune
---

Show the **fleet**: every battle tracked across repos, from the global shard index
`~/.claude/legion/fleet.d/` (base overridable via `$LEGION_FLEET`).
Arguments: `$ARGUMENTS`

1. **Read the index** = read **every** `fleet.d/*.json` file; each file is one
   battle entry (key `<repo_path>::<id>`). Aggregate them into a list. If the
   directory is missing or has no shards, say there are no tracked battles and
   stop. (One file per battle — never a shared index — so concurrent Claude
   sessions never overwrite each other's entries.)

2. Render one line per battle, sorted by `updated` descending:

   ```
   REPO              BATTLE                       PHASE     STATUS       UPDATED
   billing-api       2026-06-08-GH-1234           build     in_progress  10:00
   orders-api        2026-06-08-GH-1240           review    blocked      09:30
   ```

   - default (no arg): show only battles with `battle_status != "closed"` —
     i.e. still in flight.
   - `all`: show every entry (including closed battles — the index keeps history).
   - highlight `blocked` battles first — those need attention (a gate returned
     `revise`/`reject`).

3. `prune`: for each shard whose `repo_path` no longer contains the battle
   (`.legion/battles/<id>/battle.json` absent), **delete that shard file** —
   stale after a repo move or a deleted battle. **Closed battles are kept**
   (history); prune only removes shards whose battle vanished from disk. Report
   what was removed.

## Index schema (for downstream consumers)

Each shard (`fleet.d/*.json`) is **one battle entry** carrying, beyond the CLI
columns: `title`, `profile`, `battle_status` (`active` | `blocked` | `closed`),
`pr_url`, and `repo_path` + `id` (which locate the artifacts at
`<repo_path>/.legion/battles/<id>/`), plus an approximate usage snapshot
`tokens_total` (Σ input+output), `tokens` (breakdown) and `skills` (the skills
actually used, main + subagents — projected from `usage.jsonl`). A consumer (a
local UI) lists every battle across repos by reading all shards, then opens the
markdown artifacts in place — the files stay in their repo, the index just points
to them. Fields may be `null`/absent on entries not yet rewritten since the schema
grew; read defensively.

This view does not multiplex sessions: one Claude session drives one repo. The
fleet makes battles **observable and resumable** — to act on one, open its repo
and run `/legion:battle resume <id>`.
