---
description: Consolidated view of legion battles across all repos (the multi-repo Conductor view). Reads the global fleet index (per-battle shards).
argument-hint: (no args = active) | all | prune | stale=<hours>
---

Show the **fleet**: every battle tracked across repos, from the global shard index
`~/.claude/legion/fleet.d/` (base overridable via `$LEGION_FLEET`).
Arguments: `$ARGUMENTS`

1. **Read the index** = read **every** `fleet.d/*.json` file; each file is one
   battle entry (key `<repo_path>::<id>`). Aggregate them into a list. If the
   directory is missing or has no shards, say there are no tracked battles and
   stop. (One file per battle — never a shared index — so concurrent Claude
   sessions never overwrite each other's entries.)

2. Render one line per battle, ordered as below (step 2b):

   ```
   REPO              BATTLE                       PHASE     STATUS       UPDATED
   billing-api       2026-06-08-GH-1234           build     in_progress  10:00
   orders-api        2026-06-08-GH-1240           review    blocked      09:30
   ```

   - default (no arg): show only battles with `battle_status` not in
     `{closed, aborted}` — i.e. still in flight.
   - `all`: show every entry (including closed and aborted battles — the index keeps
     history).
   - highlight `blocked` battles first — those need attention (a gate returned
     `revise`/`reject`).

2b. **Flag stale battles** (computed by you at read time — no new field in the shard).
   A battle is **stale** when `battle_status` is `active` or `blocked` **and**
   `now - updated` is greater than the threshold: **24 hours** by default, or the
   integer given by `stale=<hours>`. If `<hours>` is not an integer greater than 0, use
   24 and say so in one warning line. If `updated` is missing or unreadable, the battle
   is **not** stale. `updated` is the time of the last shard upsert, not of the last
   real activity: it is a proxy, say so when you report stale battles. A stale battle
   is probably orphaned; the user can end it with `/legion:battle abort <id>` (from its
   repo).

   Mark it with `stale` after the status. **Order**: `blocked` battles first (a `blocked`
   and stale battle stays in this group, with the `stale` mark), then the stale ones
   (all `active`), then the rest, each group by `updated` descending. `stale=<hours>`
   can be combined with `all`.

3. `prune`: for each shard whose `repo_path` no longer contains the battle
   (`.legion/battles/<id>/battle.json` absent), **delete that shard file** —
   stale after a repo move or a deleted battle. **Closed battles are kept**
   (history); prune only removes shards whose battle vanished from disk. Report
   what was removed.

## Index schema (for downstream consumers)

Each shard (`fleet.d/*.json`) is **one battle entry** carrying, beyond the CLI
columns: `title`, `profile`, `battle_status` (`active` | `blocked` | `closed` | `aborted`),
`pr_url`, `pr_state` (`open` | `merged` | `closed`) and `ci` (`pass` | `fail` | `pending` | `none`; both absent until first written), and `repo_path` + `id` (which locate the artifacts at
`<repo_path>/.legion/battles/<id>/`), plus an approximate usage snapshot
`tokens_total` (Σ input+output), `tokens` (breakdown) and `skills` (the skills
actually used, main + subagents — projected from `usage.jsonl`), plus `slices_done` /
`slices_total` (absent when the battle declares no slices). A consumer (a
local UI) lists every battle across repos by reading all shards, then opens the
markdown artifacts in place — the files stay in their repo, the index just points
to them. Fields may be `null`/absent on entries not yet rewritten since the schema
grew; read defensively.

This view does not multiplex sessions: one Claude session drives one repo. The
fleet makes battles **observable and resumable** — to act on one, open its repo
and run `/legion:battle resume <id>`.
