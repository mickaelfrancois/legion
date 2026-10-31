## §J — close a battle (after the PR is merged, or after an abort)

Use once the PR is **merged**: it removes the battle's worktree and local branch and brings the
main checkout up to date. Also usable for an **aborted** battle that has a `worktree` block (see
"Aborted battle" below). Only the human asks for it; never close on your own initiative.

**Allowed arguments** — nothing else: an optional `<battle-id>`. Never paste `$ARGUMENTS` into a
shell command: read the value, check it against `^[A-Za-z0-9][A-Za-z0-9-]*$`, refuse anything
else (an option, an extra word), then pass it quoted. Without `<battle-id>`, use the active
battle; if none is active (the pointer is cleared once a battle is closed), ask for the id.

**Run from the main checkout.** `close` removes the worktree: it refuses when the session is
inside it (`not_inside`). The session normally never entered it. Recovery only: if
`battle_worktree.py where --battle <id>` says `inside: true`, try
`ExitWorktree` with action `keep` (never `remove`), then run `where` again. Still inside, or the
tool is absent: ask the user to relaunch `claude` from the main repo (`state_root`) and run
`/legion:battle close <id>` again, and stop. Resolve `<state>` as in the box at the top.

1. **Refresh the PR state** (the proof of the merge comes from it). Same call as §C, from the
   validated `<n>` (tail of `delivery.pr_url`, `^[0-9]+$`), and only if `gh` exited `0`:

   ```bash
   gh pr view <n> --json state,mergedAt,statusCheckRollup,url,headRefName,headRefOid > "<state>/.legion/battles/<id>/pr-status.json"
   python "<plugin-root>/scripts/battle_state.py" set-delivery --pr-json "<state>/.legion/battles/<id>/pr-status.json" --battle <id>
   ```

   This records `delivery.head_ref` and `delivery.head_oid` too: `close` needs `head_oid` to
   prove that a squash or rebase merge lost no local commit. No `gh` or no `pr_url`: the PR
   cannot be proven merged, so stop and say so.
2. **Fetch**: `git fetch --no-tags origin` (the scripts never use the network).
3. **Check**: `python "<plugin-root>/scripts/battle_worktree.py" close-check --battle <id>`.
   It is read-only and prints `{ ok, checks:[{name, ok, detail}], modified, untracked }`. The
   controls are `pr_merged`, `contained` (the branch is an ancestor of `origin/<default>`, or the
   PR is merged and `delivery.head_oid` equals the local tip), `worktree_clean` and `not_inside`.
   A failed control: relay its `detail` (and the `modified` / `untracked` lists) and **stop**.
   Never remove, commit or stash anything for the user.
4. **Retrospective.** If `phases.reflect` is not `done`, run `/legion:retro` for this battle
   first (it closes the battle state and files the lessons), then continue. `close` refuses
   while `reflect` is not `done`.
5. **Close**: `python "<plugin-root>/scripts/battle_worktree.py" close --battle <id>`.
   It replays `close-check`, then runs `git worktree remove` (never `--force`), `git branch -D`
   (only after the `contained` proof), `git worktree prune`, and fast-forwards the main checkout
   onto `origin/<default>` only if it is on `<default>` and clean. It prints
   `{ ok, removed, branch_deleted, pruned, main_updated, warnings }`. Relay every warning:
   `main_updated: false` with a warning is **not** an error (the main checkout was on another
   branch or dirty; nothing was forced). Exit `2` carries a `reason`: relay it, do not work
   around it. A second run is harmless (idempotent).
   A battle without a `worktree` block (legacy or `--in-place`) goes through the same steps: the
   script proves the merge and deletes the local branch, and checks out `<default>` first if the
   main checkout sits on it (clean tree only).
6. **Report**: worktree removed or not, branch deleted or not, main checkout updated or not,
   warnings. The remote branch is not deleted (GitHub's auto-delete setting decides).

**Aborted battle.** When `battle.json` has `aborted` set **and** a `worktree` block, `close-check`
and `close` switch to an "aborted" mode (`aborted: true` in their output). It replaces the merge
proof with a proof that no commit is lost. Same steps, with these differences:

- Skip step 1 (no PR proof) and step 4 (the retrospective is optional after an abort).
- Step 2 becomes `git fetch --prune --no-tags origin`. The `--prune` is part of the proof: without
  it a deleted remote branch leaves a stale `origin/<branch>` that would pass for a saved commit.
- Step 3 controls are `branch_safe`, `unpushed`, `worktree_on_branch`, `worktree_clean` and
  `not_inside` (no `pr_merged`, no `contained`). `unpushed` is ok when the branch is gone, when its
  tip equals `worktree.base` (and `base` is still reachable from another ref), or when its tip is
  contained in a `refs/remotes/origin/**` ref. `worktree_on_branch` fails on a detached or
  wrong-branch worktree (its commits would vanish with it).
- Step 5 never updates the main checkout: expect `main_updated: false`, without a warning. A commit
  that is not on `origin` gives exit `2`, `reason: "unpushed"`, and nothing is removed.
- Never push, commit or stash anything for the user. An aborted battle calls no `battle_state.py`
  mutation (they are refused).
