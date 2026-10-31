## §B — resume a battle

Re-point the active battle with
`python "<plugin-root>/scripts/battle_state.py" activate <battle-id>` (so the
guard hooks track the resumed battle; a refusal means the battle does not exist),
then check the state with `… battle_state.py validate --battle <battle-id>` (relay any error or warning).
Read `<state>/.legion/battles/<battle-id>/battle.json`. Summarize phase
statuses and the last verdict. Announce the next pending phase and what it needs.
**An aborted battle is not resumed**: if `activate` is refused because the battle carries
`aborted` (exit `2`), relay the `reason` and stop — do not work around it. To consult it,
use `Read` and `… battle_state.py validate --battle <battle-id>` (the only command an
aborted battle still accepts).
`activate` prints `previous_active` and `warnings`: relay a `pointer_taken` warning and ask
the user before going on (§A.1 step 2b). The `session_bind.py` hook binds this session to the
resumed battle: run `… battle_state.py session-status --battle <battle-id>` and, on
`bound: false`, warn « single-session mode (pointer): do not start another battle in parallel ».
**Check the worktree.** There is no re-entry: the session stays in the main checkout. If
`battle.json` has a `worktree` block (worktree mode), run
`python "<plugin-root>/scripts/battle_worktree.py" where --battle <battle-id>`. `ok: true`
means the worktree exists on its branch: go on from the main checkout (block « Worktree mode —
working from the main checkout », §A.1 step 2b). `ok: false` carries `reason`: `missing` (the
worktree is gone) or `wrong_branch` (see `worktree_branch`): relay it and stop (escalation
case 4). If `inside: true`, apply the recovery line of §A.1 step 2b. A battle **without** a
`worktree` block (legacy or `--in-place`) resumes in place, as before.
Do not re-run completed phases unless asked. If `build` is `in_progress` or `blocked`,
run `… battle_state.py next-slice --battle <battle-id>`: it names the slice to resume from (read-only, JSON
`{slice, slices_done, slices_total}`). Never replay slices already `done`.

**Resume notes.** Right after `validate`, run
`python "<plugin-root>/scripts/context_check.py" notes --battle <battle-id>` (read-only) and branch on
its `reason` (an exit code other than `0` or unreadable JSON counts as `absent`):
- `fresh` → the notes match the live state. Read `battle.json` as above, then `Read` **only**
  `<state>/.legion/battles/<battle-id>/resume-notes.md` and the part of the next phase (named under
  « Prochaine action »). Do **not** reread `spec.md`, `plan.md` or the `gate-*.md` already judged:
  the agents read their inputs by path. Then follow `run.mode` below.
- `no_header` → notes written by hand, without header: read them as free context, then resume the
  classic way (do not skip the artifacts).
- `stale`, `unreadable`, `other_battle` → say « notes périmées, ignorées » and resume the classic way.
- `absent` → classic resume, silently.

**Lire et respecter `run.mode`.** Le mode persisté dans `battle.json.run.mode`
détermine le comportement de la session reprise :
- `"step"` → chaque transition de phase rend la main (comportement pas-à-pas) : une
  battle reprise en mode `step` **ne s'emballe pas**, même si les phases précédentes
  s'étaient enchaînées automatiquement.
- `"autonomous"` → ré-enchaîner depuis la phase pending sans demander d'OK redondant
  (le point d'arbitrage a déjà eu lieu avant la première exécution de BUILD).
- Champ `run` absent (battle démarrée avant la feature) → se comporter comme
  `"autonomous"` par défaut. N'écrire pas rétroactivement le champ si la battle est
  en cours de progression — ne casser aucune battle existante.

**Battle legacy sans approbation.** Si `transition build in_progress` (§D) est refusé
avec « plan non approuvé » (battle antérieure à `approved_at`, ou plan relancé depuis ;
le même refus vaut pour `transition build blocked`), ne pas contourner : demander l'**OK humain** (« OK pour lancer le build ? »), puis
enregistrer avec `python "<plugin-root>/scripts/battle_state.py" approve-plan --battle <id>`
et retenter.

> **Tableau mode × transition → rend la main ?**
>
> | Mode | Après approbation plan | Après BUILD | Après chaque gate | Avant push/PR (DELIVER) |
> |------|------------------------|-------------|-------------------|------------------------|
> | `autonomous` | Oui (point d'arbitrage) | Non — cascade | Non — cascade | Non (filets = barrière) |
> | `step` | Oui | Oui | Oui | Oui (CONFIRM §G.4) |
> | contexte > 120k (les deux modes) | Oui (proposition, T1) | Oui (proposition, T2) | Oui (proposition, T3) | Non |
>
> La ligne « contexte > 120k » est le reset de contexte du noyau (cas 8, §F) : une proposition,
> jamais un reset automatique.
