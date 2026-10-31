## §I — abort a battle

Use when a battle will never be delivered (dead end, superseded, wrong scope). Only the
human decides: never abort on your own initiative.

**Allowed arguments** — nothing else: an optional `<battle-id>` and an optional
`--reason <text>`. Never paste `$ARGUMENTS` into a shell command. Read the two values
out of it, refuse anything else (unknown option, extra word) and ask the user to
correct, then pass each value as its own quoted argument:

```bash
python "<plugin-root>/scripts/battle_state.py" abort [--battle "<battle-id>"] [--reason "<text>"]
```

Always pass `--battle <battle-id>` (the id of this session's battle): without it the script
targets the `.legion/active-battle` pointer, which may belong to another session. Likewise
`close`. Without `--reason` the reason is stored as `null`.

1. **Run the script and read its JSON.** Exit `2` means **refused** (the battle is
   already closed, already aborted, or does not exist): relay the `reason` and stop.
2. **On success** the script wrote `aborted = { at, reason }` in `battle.json`, cleared
   `.legion/active-battle` if it pointed at this battle, dropped the session bindings to it
   (`unbound: n`), and resynced the fleet shard
   (`battle_status = "aborted"`). From now on every command except `validate` is refused
   for this battle (`activate` and `next-slice` included).
3. **Release the issue** (only if `ticket` is `GH#<n>`; best-effort, never blocking):

   ```bash
   gh issue edit <n> --remove-assignee @me
   ```

   `<n>` is the number read from `battle.json.ticket`, never from user input. On failure
   (or if `gh` is missing / unauthenticated) → **warn and continue**: the abort is already
   recorded. **Do not close the issue and do not touch the PR.**
4. **If `delivery.pr_url` is set**, tell the human the PR is still open and that closing
   it is their call. A retrospective (`/legion:retro`) is optional, not required.
5. The artifacts stay on disk under `<state>/.legion/battles/<id>/`. **The worktree and its
   branch are kept** (`abort` never removes them: they may hold unpushed work). If
   `battle.json` has a `worktree` block, tell the human its `path` and `branch` and that
   `/legion:battle close <id>` (§J) removes them, but only when no commit would be lost (it
   refuses with `unpushed` otherwise); pushing or dropping those commits stays the human's call.
   Report: battle id, reason, assignee released or not, PR to handle or not, worktree kept or
   not.
