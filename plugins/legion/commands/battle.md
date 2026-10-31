---
description: Orchestrate a legion battle (start | build | review | test | deliver | address | resume | status | close | abort). Creates per-repo battle state, runs the gate pipeline, persists artifacts.
argument-hint: start <issue|slug> [--profile <p>] [--step] [--in-place] [--bug] | build [slice|all] [--auto] | review | test | deliver | address | resume <battle-id> | status | close [<battle-id>] | abort [<battle-id>] [--reason <text>]
---

You are the **battle orchestrator** of `legion`. Load the `battle-workflow` skill
doctrine before acting. Producers and gates each write their **own** artifact: the
`builder` writes its per-slice report `build-report-<slice_id>.md` (you consolidate them into `build-report.md` with `merge-reports`), every gate writes its single `gate-*.md` /
`plan.md` / `pr-feedback.md` (the `guard.py` hook **confines** each gate to that one
file). You persist everything else — `spec.md`, the PR artifacts — and `battle.json`
through `battle_state.py` only (see the box below); you read the gate artifacts from disk when you need their detail. A gate returns
only its **verdict + the artifact path** (plus, for `pr-triage`, the TRIAGE JSON),
never the full content — that keeps the gate's output out of this orchestrating
session's context.

> **State writes — `battle_state.py` is the only writer of `battle.json`.** Never
> edit `battle.json` (nor `.legion/active-battle`) by hand. Every mutation goes
> through the deterministic script, called as
> `python "${CLAUDE_PLUGIN_ROOT}/scripts/battle_state.py" <sub> …` (fall back to
> `python3`, same interpreter as the other script calls). Subcommands: `init`,
> `transition`, `approve-plan`, `set-slices`, `slice`, `next-slice`, `check-cascade`, `merge-reports`, `bump-autocorrect`,
> `invalidate`, `set-delivery`, `set-guard`, `set-meta`, `activate`, `close`, `abort`, `validate`, plus the read-only `session-status`, and `touch-files`, which writes `battle.json` (never call it between `tree-snapshot` and `tree-verify`). The script prints one JSON object on
> stdout; **read it**. Exit code `2` means **refused** (or invalid usage): relay the
> `reason` to the user and **do not advance**. The script enforces the phase
> preconditions and the auto-correction budgets; you no longer re-check them by hand.
> `set-delivery --pr-json <file>` records the PR/CI state read by `gh`; the script only
> parses the file and never touches the network. Without `--repo`, the script starts from the repo
> root containing the cwd (`git rev-parse --show-toplevel`), so a subfolder never gets its own
> `.legion/`; from a linked worktree it then targets the **main repo**; `init` and
> `activate` always target the main repo. The session stays in the main checkout, and every
> mutation after `init` carries `--battle <id>` (never rely on the active-battle pointer). The hooks keep their own
> resolution (raw cwd). An explicit `--repo <path>` wins.
> Reading `battle.json` with `Read` stays allowed.

> **State root `<state>` — anchor every `.legion/` path on it.** A battle runs by default in
> a dedicated worktree `<main>/.claude/worktrees/<id>` (§A.1), but its state stays in the
> **main repo**: `.legion/` of the worktree is never the battle's state. So every `.legion/…`
> path in this file means `<state>/.legion/…`, where `<state>` is the `state_root` printed by
> `python "${CLAUDE_PLUGIN_ROOT}/scripts/battle_worktree.py" where --battle <id>`. Take that value
> once per session (and again after a `resume`), check it is an existing absolute directory,
> and write **absolute** paths in shell commands and in `Read` / `Write` calls. Never build a
> `.legion/` path from the cwd. `battle_worktree.py` always starts from the main repo and
> from an explicit `--battle <id>`, never from the active-battle pointer (which `close` and
> `abort` clear). It prints one JSON object (exit `0` ok, `2` refused, `1` usage error), never
> writes `battle.json` or `.legion/`, and never uses the network.

> **Surfacing commands to the user — always namespace them.** This plugin's
> commands are **namespaced**: the user must type `/legion:battle …`
> (bare `/battle` resolves to *Unknown command*). Whenever you suggest a next step,
> write the **namespaced** form so it is copy-pasteable —
> `/legion:battle review`, `/legion:battle build`, `/legion:retro`,
> `/legion:fleet`. (Bare `/battle …` below is doctrinal shorthand — never relay it
> verbatim to the user.)

> **Plugin root `<plugin-root>` — used by the doctrine parts.** This command is split: the
> sections §A to §J live in parts under `<plugin-root>/doctrine/battle/`, read on demand with
> `Read` (see « Doctrine parts » below). `<plugin-root>` = `${CLAUDE_PLUGIN_ROOT}` (substituted
> when this command loads). `Read` does not substitute anything: in every shell command and every
> `Read` taken from a part, replace `<plugin-root>` with that path first.

Arguments: `$ARGUMENTS`

## Dispatch

- `start <issue|slug> [--profile <p>] [--step] [--in-place] [--bug]` → §A. **Validate the options before any shell call.** The accepted options are a closed list: `--profile` with one of `feature`, `hotfix`, `security`, `spike` (default `feature`), `--step`, `--in-place` (no value: skip the dedicated worktree and work in the main checkout, as before), and `--bug` (no value: mark a **bug battle**, see « Bug battle » in §A.1 step 2). Anything else: refuse and relay the list to the user. Never substitute `$ARGUMENTS` raw into a shell command: parse it, check each value against the list, and pass only the validated values.
- `build [slice|all] [--auto]` → §D
- `review` / `test` → §E (review gate, then test gate, then security if required)
- `deliver` → §G (branch, commit, push, PR linked to the issue)
- `address` → §H (handle human PR review comments — repeatable, post-deliver)
- `resume <battle-id>` → §B
- `status` (or empty) → §C
- `close [<battle-id>]` → §J (after the PR is merged, **or for an aborted battle**: remove the worktree and the local branch; a delivered battle also updates the main checkout). The only accepted value is a `<battle-id>` matching `^[A-Za-z0-9][A-Za-z0-9-]*$`; refuse any option.
- `abort [<battle-id>] [--reason <text>]` → §I (abandon a battle that will not be delivered)

### Doctrine parts (load on demand)

Each `/battle <sub>` reads only the parts of its own phase, under `<plugin-root>/doctrine/battle/`:

| Subcommand | Parts to read |
|---|---|
| `start` | `A-start.md`, `checks.md` (delivery check of the `architect` gate); then, in the cascade, the parts of each phase reached |
| `build` | `D-build.md`, `checks.md` |
| `review` / `test` | `E-gates.md`, `checks.md` |
| `deliver` | `G-deliver.md` |
| `address` | `H-address.md`, `checks.md` (+ `E-gates.md` if the cascade is replayed) |
| `resume` | `B-resume.md`, then the part(s) of the pending phase |
| `status` | `C-status.md` |
| `close` | `J-close.md` |
| `abort` | `I-abort.md` |

- Read a part **once per session**, **before** acting in its phase, including when a run chains
  into the next phase (read that phase's part first). After a compaction, read it again.
- `§F` (below) and the Guardrails stay in this file: they apply to every phase.
- References to « `battle.md` §X » or « `commands/battle.md` §X » anywhere in the repository
  mean **this doctrine** (this file plus its parts). The `## §X …` headings are unchanged.

## Reset de contexte (transitions de phase)

Goal: cut the token cost of the orchestrating session. Every turn rereads the whole context, so a
long session pays for it on each turn. At a **phase transition**, when the context is large, the
orchestrator offers a fresh session instead of dragging the context along. It only **offers**: the
user decides, and nothing resets on its own.

**Where it applies.** Evaluate the reset point **only** at these three transitions:

| # | Where | Exact moment |
|---|---|---|
| T1 | `A-start.md` step 6 | After the human OK and `approve-plan`, **before** `transition build in_progress` |
| T2 | `D-build.md` | After `transition build done` (every slice `done`, `merge-reports` run), **before** the first gate of §E. Never after a `slice <id> done`. |
| T3 | `E-gates.md` | After each `transition <phase> done` carrying an `accept*` verdict, **before** the next gate or §G |

Never evaluate it: during the auto-correction loop or in a cascade replayed after a correction
(`revise`, polishing round, `address` round); after `deliver done` (the run ends and hands back
anyway); in §B, §C, §I and §J. `measure` also answers `propose: false` while a phase or a slice is
`in_progress`, so the reset never lands in the middle of a slice.

**Procedure.**

1. Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/context_check.py" measure --battle <id>` (read-only, JSON
   on stdout, threshold `RESET_THRESHOLD = 120000` fixed in the script). If the exit code is not `0`,
   the JSON is unreadable, `measurable` is `false` or `propose` is not `true`: **go on silently**, no
   message and no error. A context that cannot be measured never blocks a battle.
2. If `propose` is `true` **and** the user has not already declined in this session (see 3): write
   `<state>/.legion/battles/<id>/resume-notes.md` with `Write` (the `header` field of the JSON verbatim
   as the first lines, then the template below; 8 KB at most; never copy an artifact), then **hand
   back**, even in `autonomous` mode (escalation case 8, §F). Message, in French:
   « Contexte ≈ N k jetons (> 120k) à la transition X → Y. Notes : `<chemin>`. Pour repartir léger :
   `/clear` puis `/legion:battle resume <id>`. Pour continuer ici : « continuer ». »
3. On « continuer »: resume the cascade where it stopped. Make **no new proposal** in this session
   (the refusal lives in this session's context; a new session starts clean).

In `step` mode the proposal is added to the usual hand-back message of the transition instead of
being a separate stop. Each new reset overwrites `resume-notes.md`. `/battle resume` reads it
(`B-resume.md`) and drops it when the battle has moved on since (`context_check.py notes`).

```markdown
---
battle: <id>
written_at: <ISO-8601 UTC>
context_tokens: <n>
phases: <k=status …>
---
# Notes de reprise — <id>

## État
<phase qui vient de se terminer + verdict ; mode run ; worktree et branche>

## Prochaine action
<sous-commande namespacée + partie à lire, ex. `/legion:battle review` → `doctrine/battle/E-gates.md`>

## Décisions utiles
<arbitrages pris pendant la session et absents des artefacts (10 puces au plus)>

## Pièges rencontrés
<erreurs d'outil, refus de script, contournements validés (10 puces au plus)>

## Artefacts
- Déjà jugés (ne pas relire) : `<chemin>` — <verdict>
- À lire pour la prochaine phase : `<chemin>` (ou « aucun : l'agent lit ses entrées par chemin »)
```

Written in French like every battle artifact. The five first lines (`---` to `---`) are the `header`
returned by `measure`, copied as is: `notes` compares its `phases:` line with the live state.

---

## §F — Autonomie & escalade

### Taxonomie d'escalade (liste close)

L'orchestrateur rend la main à l'humain **uniquement** dans les cas suivants.
Toute correction déterministe se fait sans lui.

| Cas | Déclencheur | Action |
|-----|-------------|--------|
| **1. `reject`** | Une gate rend un verdict `reject` (régression majeure, redesign requis). | Escalade **immédiate**, zéro tentative de correction. Relayer le verdict + RAISON. |
| **2. Boucle non convergente** | Aucun FAIL ciblé résolu d'une tentative à l'autre (progrès = identité des FAIL, pas le compte brut), ou plafond atteint (2 tentatives/gate, 6 tentatives au global). | Escalade avec le détail : gate, FAIL résolus/persistants/nouveaux, tentatives effectuées. |
| **3. Déviation du plan** | La correction requise sort du périmètre figé (slices de `plan.md` ou `guard.allow`), y compris une résolution de conflit (ronde conflit d'`address`) hors `guard.allow`. | Escalade : re-planification nécessaire. Ne pas modifier `plan.md` en cours de run. |
| **4. Filets DELIVER déclenchés** | Base en retard sur `origin` **avec delta d'arbre intersectant** les fichiers touchés (un delta vide ou disjoint est waivé sans escalade — §G.0.a), remote vide, fichier hors whitelist, `.gitignore` auto-induit à arbitrer (§G.0). | Escalade : résoudre le filet d'abord, puis DELIVER peut reprendre. |
| **5. Préflight défaillant** | `python` absent, `gh` absent/non authentifié, stack ambiguë (§A.preflight). | Escalade : résoudre l'environnement avant toute battle. |
| **6. Faute d'écriture d'une gate** | `tree-verify` détecte une écriture d'une gate dans l'arbre (§E, contrôle d'intégrité de l'arbre). Le verdict ne compte pas. | Escalade : phase `blocked` sans verdict, ni nouvelle tentative ni restauration. Relayer `changed` / `out_of_scope` ; l'humain diagnostique l'agent (ou son propre éditeur). |
| **7. Fusion du lot parallèle** | Le fan-in d'un lot `--auto` échoue : conflit (`overlap`, `dirty`, `apply`) rapporté par `fan_in.py apply`, ou vérification du projet rouge après la fusion (§D). Un refus hors périmètre reste en cas 3. | Escalade : slices du lot `blocked`, worktrees **conservés**. Relayer la slice, le type de conflit et les fichiers ; le lot est à re-découper (slices en réalité dépendantes), pas le périmètre à élargir. |
| **8. Contexte saturé (proposition de reset)** | À un point de reset (T1, T2 ou T3, section « Reset de contexte »), `context_check.py measure` rend `propose: true` (contexte > 120k, aucune phase ni slice en cours) et l'humain n'a pas déjà refusé dans la session. | Écrire `resume-notes.md`, rendre la main avec la proposition `/clear` puis `/legion:battle resume <id>` ; sur « continuer », reprendre sans nouvelle proposition. Mesure impossible : aucune proposition, aucune erreur. |

> **Hors liste = pas d'escalade.** Toute autre situation (warning de build,
> `accept_with_opportunity`, opportunité de découpe) est résolue automatiquement.

### Budgets de boucle (deux niveaux distincts)

Les deux budgets sont **indépendants et non additionnés** :

- **Boucle interne `build-fix` du builder** : 3 tentatives sur `dotnet build`.
  Gérée par le builder lui-même ; le builder ne décide pas d'escalader (il rapporte
  `build_ok: false` si son budget est épuisé).
- **Boucle orchestrateur (re-gate)** : 2 tentatives par gate (maximum ferme), plafond global de 6 tentatives au global (maximum ferme)
  sur le run. Ces budgets sont **appliqués par le script** (`bump-autocorrect`, §E :
  `CAP_PER_PHASE = 2`, `CAP_TOTAL = 6` dans `battle_state.py`) ; l'orchestrateur lit la
  décision `continue` / `escalate` et ne compte pas à la main. Un `build_ok: false` du builder après ses 3 essais **compte pour
  1 tentative** de la boucle orchestrateur. La **ronde de polissage** (§E, `invalidate
  --reason polish`, une seule fois) ne consomme **pas** ce budget.
- **Round CI d'`address`** (§H étape 0) : il compte sous la clé `build`
  (`bump-autocorrect build`). Un échec CI qui ne vient pas du code ne compte pas.
- **Ronde conflit d'`address`** : n'appelle pas `bump-autocorrect`, hors budget (comme la
  ronde de polissage) ; un `revise` pendant la cascade rejouée suit §E et compte.

La boucle orchestrateur opère un cran au-dessus : elle borne les **re-gate**, pas
les re-builds internes du builder.

---

## Guardrails

- Never `cd` / `Set-Location`; operate from the current directory (worktree mode: target `<wt>` by absolute path or directory option, see §A.1 step 2b).
- Worktree mode: write nothing to the main checkout except under `.legion/`; `guard.py`
  blocks it. Never remove a worktree or a branch by hand: `close` (§J) is the only way, and it
  never uses `--force`.
- Mutate `battle.json` (and `.legion/active-battle`) **only through `battle_state.py`** —
  never by hand. Persist `spec.md` / PR artifacts yourself; each gate writes **only
  its own** `gate-*.md` / `plan.md` / `pr-feedback.md` (guard-confined) and returns
  verdict + path — nothing else.
- Never **advance** on `revise`/`reject`. `reject` → immediate escalation. `revise` on a
  review-cascade gate (lint/reviewer/test-engineer/security) → in `autonomous` mode,
  the bounded auto-correction loop (§E, budgets §F); in `step` mode, hand back. A PLAN
  (`architect`) `revise` **always** hands back to adjust the spec (§A.1 step 6).
- Do the concrete .NET work with the .NET SDK and the agents' internal rule lists;
  never require a third-party plugin, and ignore one if installed.
