## §C — status

List every battle under `<state>/.legion/battles/`, showing id, profile, and the
current phase with its status/verdict. One line per battle. Read-only: read each
`battle.json` with `Read`. Add the marker `aborted` to the line of a battle whose
`battle.json` carries an `aborted` field. Add the mode to each line: `worktree <path> · <branch>`
when `battle.json` has a `worktree` block (read both values from it), else `en place`.

**Refresh the PR state.** For each battle that has `delivery.pr_url` and is neither closed
(`reflect` done) nor `aborted`, do the following. This is the **only** write of `status`:
it updates the delivery metadata (`pr_state`, `ci`, `merge`, `checked_at`) and never changes a phase.

1. Take `<n>` from the tail of `pr_url` and check it matches `^[0-9]+$`. If not, skip the
   call and say so. No free-form string ever reaches the shell.
2. Run:
   ```bash
   gh pr view <n> --json state,mergedAt,statusCheckRollup,url,headRefName,headRefOid,mergeable,mergeStateStatus > "<state>/.legion/battles/<id>/pr-status.json"
   ```
3. Only if `gh` exited `0`, run:
   ```bash
   python "<plugin-root>/scripts/battle_state.py" set-delivery --pr-json "<state>/.legion/battles/<id>/pr-status.json" --battle <id>
   ```
   Exit `2` means the file was refused: relay the `reason`. The JSON printed on stdout
   also carries `failing` (`[{name, url}]`, the failed checks, not persisted).

Add `PR <pr_state> · CI <ci>` to the battle's line, then `· conflit avec la branche par
défaut` if `merge == "conflict"`, or `· état de fusion indéterminé` if `merge == "unknown"`
(nothing for `ok` or `null`). Suggest the next step: `merged` →
`/legion:retro`; `ci == fail` or `merge == "conflict"` → `/legion:battle address`; `closed` →
report it. `merge == "unknown"` gets **no** merge-related suggestion, no waiting, no polling.
Without `gh`, or if `gh` fails, keep the plain line and add "état PR non rafraîchi (gh
indisponible)".
