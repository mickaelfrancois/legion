## §E — review / test gates

Precondition: `phases.build.status == "done"`. Each gate is a subagent that is
**read-only on the code** but **writes its own single artifact** (`gate-*.md` — the
`guard.py` hook confines it to that one file): it **returns** only a verdict + the
artifact path, not the content. Apply this shared loop for each gate, in order
`lint → reviewer → test-engineer → security`, skipping any gate not in
`battle.json.required_gates`. `security` runs when it is in `required_gates`, whether the
profile put it there or the script added it after a sensitive slice.

The shared checks of this section (tree integrity check, gate artifact delivery check, non-.NET stack adaptation) live in `<plugin-root>/doctrine/battle/checks.md`: read it with this part.

The gate identifier (used for `subagent_type` and `required_gates`) is **not**
the phase key written to `battle.json.phases`. The mapping gate → phase key is
**derived from `GATE_PHASE` in `scripts/battle_state.py`** (single source of truth;
the list below is a readable copy of it): `architect → plan`, `lint → lint`,
`reviewer → review`, `test-engineer → test`, `security → security`,
`pr-triage → address`. Pass the **phase key** (never the gate name) to
`battle_state.py`: the script rejects an unknown phase, and
`bump-autocorrect` answers with a `suggestion` when given a gate name. Writing under
the gate name (`phases.reviewer`/`phases.test-engineer`) would be a bug — the UI reads
the canonical keys `lint`/`review`/`test` and would show the phase as pending even
though the gate ran.

1. **Invoke** the gate via `Agent`, wrapped in the **tree integrity check** (shared checks, `<plugin-root>/doctrine/battle/checks.md`)
   (`tree-snapshot` first, `tree-verify` right after the return) (`subagent_type`: `lint` | `reviewer` |
   `test-engineer` | `security`). Self-contained prompt: battle dir, the upstream
   artifacts it needs (`build-report.md`, `plan.md`, touched files), repo root. For
   `lint`, also pass the **format target** (`stack.build_target` when set — a
   `.csproj` is the deterministic choice for `dotnet format`; absent ⇒ format runs
   from the root), **the list of .NET files touched by the slice** (from
   `build-report.md`) so `lint` scopes `dotnet format --include` to the **diff** and
   **never judges pre-existing formatting outside the slice**, **and**, when the
   battle is **non-.NET**, tell `lint` to self-retire (it writes a withdrawal banner
   and returns a neutral `accept` — see §E "Non-.NET stack"). For `test-engineer`,
   also pass `stack.test_target` when set (repo without a `.sln`) so `dotnet test`
   targets the test project explicitly; on a **bug battle**, also say so and pass the
   `regression` block of `battle.json` and `worktree.base` (rule T5 of the gate).
2. **Run the tree integrity check, then the gate artifact delivery check** (shared checks, `<plugin-root>/doctrine/battle/checks.md`) on
   the gate's artifact, then
   **record the verdict.** The gate already wrote its artifact (`gate-review.md` /
   `gate-test.md` / `gate-security.md`) on disk — do **not** re-write it from a
   returned blob. Once delivery is confirmed, record the verdict with
   `battle_state.py transition <phase-key> done|blocked --verdict <verdict> --battle <id>` (phase
   key from the mapping above, e.g. the `reviewer` gate → `transition review …`).
   Use `done` for `accept` / `accept_with_opportunity`, `blocked` for `revise` /
   `reject`; the script refuses an incoherent verdict/status pair. Mark the gate
   started with `transition <phase-key> in_progress` before invoking it.
3. **Branch on the verdict** (cascade):
   - `accept` / `accept_with_opportunity` → `transition <phase-key> done`, apply the reset point
     T3 (« Reset de contexte » section of the core file; skipped in a cascade replayed after a
     correction), then continue to the next gate (or §G). Log any opportunity. **Out-of-scope observations** the gate recorded in
     its `## Hors périmètre — candidats issue` section are collected at REFLECT
     (`/legion:retro`, step 7) and filed as deduplicated GitHub issues on the target
     repo — nothing to do here beyond recording the verdict.
   - `reject` → `transition <phase-key> blocked --verdict reject`, **escalade immédiate** (cas 1 de la taxonomie).
     Relay the verdict's one-line RAISON and hand back — zero tentative de correction.
     La replanification est requise.
   - `revise` → `transition <phase-key> blocked --verdict revise`. En mode `step`, relay the RAISON and hand back
     (the fix loops back to BUILD). En mode `autonomous`, entrer dans la **boucle
     d'auto-correction** :

     **Boucle d'auto-correction** (mode `autonomous` uniquement) :
     a. Construire le JSON des FAIL : **lire** le `gate-*.md` frais (depuis le disque,
        sans en recopier le contenu dans la conversation) et en extraire chaque FAIL
        comme un objet `{"target": "fichier:ligne", "dimension": "R2"}` (`dimension` =
        le code de la dimension du gate, ex. `R2`/`S3`). Exemple :
        `'[{"target":"src/A.cs:42","dimension":"R2"},{"target":"src/B.cs:7","dimension":"S3"}]'`.
        Une liste vide `'[]'` est valide.
     b. Appeler
        `battle_state.py bump-autocorrect <phase-key> --fails '<json des FAIL>' --battle <id>`.
        La clé est la **clé de phase** (`build` / `lint` / `review` / `test` /
        `security` — même mapping gate → phase que ci-dessus), **jamais** le nom de gate
        (`reviewer`, `test-engineer`) : `scripts/eval.py` lit `per_gate.<phase>`.
        Un `build_ok: false` survenu **pendant la correction d'une gate** compte sous la
        clé de cette gate (c'est la même tentative) : appeler
        `battle_state.py bump-autocorrect <phase-key> --build-failure --battle <id>` (au lieu de `--fails`),
        ce qui compte la tentative **sans remplacer** les FAIL enregistrés de la gate ni
        déclencher le contrôle de non-progrès. Un `build_ok: false` **hors de toute
        gate** (premier build d'une slice, §D) compte sous la clé `build`.
        Le script applique lui-même, **avant** toute relance, les bornes (2 tentatives
        par clé, 6 au global — §F) et la détection de progrès par **identité des FAIL**
        (`cible` + `dimension`, pas le compte brut : « 1 FAIL corrigé, 1 autre découvert »
        est un vrai progrès). Il incrémente `run.autocorrect` uniquement sur `continue`.
        Ne recalcule rien à la main.
     c. Lire la décision dans le JSON de sortie :
        - `continue` → étape (d).
        - `escalate` → **escalade** (cas 2 de la taxonomie, §F) : plafond atteint ou
          non-progrès. Rendre la main en relayant `reason` et le détail
          `resolved` / `persisting` / `new` (les identités de FAIL) plus les compteurs
          (`per_phase`, `total`).
        - exit `2` sans `decision` (clé invalide, `fails` mal formé) → corriger l'appel
          et le rejouer ; ne pas avancer.
     d. Passer au builder le **chemin de l'artefact** `gate-*.md` (lire depuis le
        disque, ne pas injecter le contenu dans ce contexte) et relancer BUILD pour
        cette slice. Passer aussi le `slice_id` de la slice visée (celle dont
        `slices[].files` contient les fichiers en FAIL, sinon la dernière slice) : le
        builder **ajoute** une sous-section `### Correction (<gate>)` à son
        `build-report-<slice_id>.md`. Puis lancer `merge-reports` avant de relancer la
        cascade, pour que `build-report.md` reflète la correction. Lancer aussi
        `battle_state.py touch-files --files <files_touched du builder> --battle <id>` :
        un fichier sensible touché par la correction ajoute `security` à `required_gates`
        (relayer son `warnings` à l'utilisateur). Ce BUILD correctif n'appelle **jamais** `slice …` : les slices restent
        `done` (l'invalidation de la cascade ne « dé-construit » pas une slice). S'il
        échoue, enregistrer seulement `transition build blocked` (et
        `bump-autocorrect <phase-key> --build-failure`) ; les slices restent `done`. Les
        verdicts de cascade portent `covers` (ids des slices `done`), écrit par le script.
        Le `continue` de `bump-autocorrect` a **déjà invalidé** la cascade
        (`lint`/`review`/`test`/`security` présentes et `done`/`blocked` → `pending`,
        `verdict` remis à `null`, `fails` conservés) : le code corrigé n'est plus couvert
        par aucun verdict. Après le BUILD correctif, **relancer toute la cascade requise
        depuis `lint`** dans l'ordre (`transition <phase-key> in_progress`, puis verdict
        comme ci-dessus), et pas seulement la gate qui avait demandé la correction :
        DELIVER reste refusé tant qu'une gate requise n'est pas revenue à `done`.
        Retour à l'étape (a).

     **Escalade** : la phase reste `blocked` (déjà posée par `transition … blocked`),
     relayer le détail du blocage (gate, FAIL résolus/persistants/nouveaux, tentatives
     effectuées), rendre la main. **Pass the builder the artifact path**
     (`gate-review.md` / `gate-test.md`) so it reads the FAIL detail from disk — do not
     pull the full gate content into this session just to brief it (that would refill
     the context the confinement is meant to spare).

When all required review/test/security gates are `done`: in mode `autonomous`,
**enchaîner directement vers §G (DELIVER)** sans attendre une commande séparée —
annoncer l'enchaînement. En mode `step`, annoncer la disponibilité pour DELIVER et
rendre la main.

#### Polishing round

Optional, **at most once per battle**, between the last gate and DELIVER.

- **Trigger**: every required gate is `accept*` (all `done`) and at least one carries a
  useful WARN worth fixing before shipping.
- **Procedure**: `battle_state.py invalidate --reason polish --battle <id>`, then a corrective BUILD
  (pass the builder the WARN detail by artifact path and the `slice_id` to update — it appends a `### Correction (<gate>)` subsection to that slice's report — then run `merge-reports`), then run
  `battle_state.py touch-files --files <files_touched by the builder> --battle <id>` (a sensitive file adds
  `security`; relay its `warnings`), then re-run the **whole required
  cascade from `lint`**.
- **Outside the 2/6 budget**: the polishing round never touches `run.autocorrect`. A
  `revise` raised *during* the round enters the normal loop above, which does consume
  the budget.
- **Enforced by the script**: a second `polish` is refused (exit `2`), and so is one
  requested when a required gate is not `done` or `deliver` is not `pending`. Relay the
  `reason` and go on to DELIVER.
