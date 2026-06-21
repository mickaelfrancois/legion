---
description: Orchestrate a legion battle (start | build | review | test | deliver | address | resume | status). Creates per-repo battle state, runs the gate pipeline, persists artifacts.
argument-hint: start <issue|slug> | build [slice|all] [--auto] | review | test | deliver | address | resume <battle-id> | status
---

You are the **battle orchestrator** of `legion`. Load the `battle-workflow` skill
doctrine before acting. Producers and gates each write their **own** artifact: the
`builder` writes `build-report.md`, every gate writes its single `gate-*.md` /
`plan.md` / `pr-feedback.md` (the `guard.py` hook **confines** each gate to that one
file). You persist everything else — `battle.json`, `spec.md`, the PR artifacts —
and you read the gate artifacts from disk when you need their detail. A gate returns
only its **verdict + the artifact path** (plus, for `pr-triage`, the TRIAGE JSON),
never the full content — that keeps the gate's output out of this orchestrating
session's context.

> **Surfacing commands to the user — always namespace them.** This plugin's
> commands are **namespaced**: the user must type `/legion:battle …`
> (bare `/battle` resolves to *Unknown command*). Whenever you suggest a next step,
> write the **namespaced** form so it is copy-pasteable —
> `/legion:battle review`, `/legion:battle build`, `/legion:retro`,
> `/legion:fleet`. (Bare `/battle …` below is doctrinal shorthand — never relay it
> verbatim to the user.)

Arguments: `$ARGUMENTS`

## Dispatch

- `start <issue|slug>` → §A
- `build [slice|all] [--auto]` → §D
- `review` / `test` → §E (review gate, then test gate, then security if required)
- `deliver` → §G (branch, commit, push, PR linked to the issue)
- `address` → §H (handle human PR review comments — repeatable, post-deliver)
- `resume <battle-id>` → §B
- `status` (or empty) → §C

---

## §A — start a battle

### §A.preflight — environment checks

**Before anything else:**

1. **Python** — run `python --version`. The plugin's **hooks**
   (`guard`/`careful`/`fleet_sync`/`usage_track`) are launched by Claude Code as
   `python "…"` and **fail silently** if `python` is missing — so a battle started
   without it runs with **no write-scope guardrail, no fleet index, no usage
   tracking**. If it fails, **stop and tell the user clearly** (do not proceed
   unless they explicitly ask to continue without the guardrails):

   > ⚠️ `python` introuvable. Sans lui, les garde-fous, l'index *fleet* et le suivi
   > sont **inactifs**. Installe Python 3 (`winget install Python.Python.3.13`),
   > puis **rouvre Claude Code** (les hooks se chargent au démarrage de session).

2. **GitHub CLI** — for a **numeric** `<issue>` intake and for `deliver`, run
   `gh auth status`. If `gh` is missing or unauthenticated → **warn**; numeric
   intake falls back to inline, and `deliver` will need the user to open the PR
   manually. A **slug** battle needs no `gh`.

### §A.preflight — reading files & console encoding

- **Read plugin/repo files with the `Read` tool, never `cat`/`type`** (RETEX): on a
  Windows cp1252 console, dumping a file with non-ASCII can crash with
  `UnicodeEncodeError`. Optionally set **`PYTHONUTF8=1`** for the session so all
  Python uses UTF-8 stdio.

### §A.preflight — detect the stack (.NET by default)

This plugin is built for **.NET** battles: the gates assume Roslyn (the
`cwm-roslyn-navigator` MCP), `dotnet build` / `dotnet test`, and the
`dotnet-claude-kit` skills. That is the default and needs no extra step. But a repo
may not be .NET (RETEX: a non-.NET battle had to neutralize these assumptions by
hand in every gate prompt). **Detect once per session** and record the result in
`battle.json.stack` (written at §A.1) for the gates and `deliver`:

- **.NET** — a `*.csproj` / `*.sln` / `*.slnx` exists at or under the repo root.
  Proceed as documented. **But if no `*.sln` exists** (csproj-only repo, or a repo
  whose only solution is a `*.slnx`, e.g. a single service): `dotnet build` /
  `dotnet test` / `dotnet format` run from the repo root then fail with *"Specify
  which project or solution file to use"*, and `dotnet format <sln>.slnx` is only
  recognized by a recent SDK (≥ .NET 9). Record the explicit target(s) in
  `battle.json.stack` (`build_target` = the buildable csproj — or the `.slnx` when
  the SDK supports it, but a **`.csproj` is the deterministic choice** that works on
  any SDK, incl. for the `lint` gate's `dotnet format`; `test_target` = the test
  csproj) so the BUILD step (§D), the `lint` gate and the test gate (§E) pass them
  explicitly instead of relying on a solution. With a `.sln` present, leave both
  `null` (commands run from the root unchanged).
- **Non-.NET** — no `*.csproj`/`*.sln`, but a `package.json` / `tsconfig.json`
  (Node/TS), `pyproject.toml`, `go.mod`, `Cargo.toml`, … is present. Flag the
  battle **non-.NET** (`stack.kind`) and apply the **Non-.NET stack** rules in §E
  for every gate.

If both are absent or it is ambiguous, **ask the user** which stack applies. Note
the detected stack at the top of `spec.md` so a resumed session inherits it.

### §A.1 — per-battle flow

1. **Derive the battle id** as `<YYYY-MM-DD>-<token>` using today's date and the
   issue/slug token (sanitize to `[A-Za-z0-9-]`; a numeric issue `1234` → token
   `GH-1234`). If no token is given, ask for one short slug — do not invent it.

2. **Create the battle directory** `.legion/battles/<id>/` in the current repo
   (relative to the working directory — never `cd`). Write the battle id into
   `.legion/active-battle` — this pointer is what the `guard.py` / `careful.py`
   hooks read to know which battle is active.

   **Mark THINK in progress first** (same discipline as BUILD, §D): immediately
   write a minimal `battle.json` with `phases.think.status = "in_progress"` (id,
   ticket if known, the default `profile`/`required_gates` — refined at step 4).
   Seeding `spec.md` can be long (reading the issue, exploring code to scope it);
   until `battle.json` exists, `.legion/active-battle` points at an empty dir and
   `/fleet` (or a resumed session) sees nothing in progress. The battle must never
   be statusless once started.

   **Ensure `.legion/` is git-ignored**: battle artifacts are **local**, never
   committed (the durable trace lives in the PR + issue). Verify the ignore **on a
   file inside** the directory: `git check-ignore -v .legion/active-battle`. If the
   file is **not** ignored, **propose adding a line `.legion/`** to the repo's
   `.gitignore` (with the user's OK). Best-effort, never blocking — the real safety
   net is the `deliver` staging discipline (§G.2). **Note:** if you do add the line,
   that edits `.gitignore` — a tracked change `deliver` must resolve explicitly
   (§G.2), not leave dangling.

3. **THINK — seed `spec.md`.** Branch on the shape of `<issue|slug>`:

   - **Numeric** (e.g. `1234`) → treat it as a **GitHub issue number** in the
     current repo. Read it:

     ```bash
     gh issue view 1234 --json number,title,body,labels
     ```

     Seed `spec.md` from its title / body / labels (acceptance criteria & repro
     steps are usually in the body). Record `ticket = "GH#1234"` in `battle.json`.
     - If `gh` is missing/unauthenticated or the issue can't be read → **warn**,
       fall back to inline intake, and note the degradation at the top of `spec.md`.

     Then **mark the issue as started** (best-effort, never blocking):

     ```bash
     gh issue edit 1234 --add-assignee @me
     ```

     (optionally add an `in-progress` label if the repo uses one). On failure →
     **warn and continue** — starting the battle never depends on this.

   - **Non-numeric** (a label / slug) → **inline intake**: write `spec.md` from the
     user's request in the conversation. No GitHub call.

   In both cases `spec.md` must contain: a systematic **`## En bref`** section at the
   top (1-3 lines, reusing the "Intention" seed), then intent, in-scope, explicitly
   out-of-scope, assumptions, acceptance criteria. Apply the **writing charter**
   (`battle-workflow` § « Charte de style des documents ») — simple, precise language;
   reference it, do not copy it. **Write it in French**
   (identifiers & file names stay English). Before locking the plan, reread `spec.md`
   against the charter (five rules + systematic « En bref »). If the seeded content is
   thin, ask the user to fill the gaps before locking the plan. For a rough issue, the
   cleaner fix is **upstream**: `/legion:recon <n>` sharpens the issue *before*
   `start` reads it (it appends a « Cadrage » section), so the seed comes in already
   sharp — suggest it when the issue is vague rather than patching `spec.md` here.

4. **Complete `battle.json`** (the minimal one from step 2) following the schema
   **inlined in the `battle-workflow` doctrine** (State layout) — already in context.
   Default `profile` to `feature` and `required_gates` to
   `["architect","lint","reviewer","test-engineer"]` unless the user states a
   different profile. (`lint` is **.NET-only** — on a **non-.NET** stack it
   self-retires at run time with a withdrawal banner and a neutral `accept`, so
   keeping it in the default set is safe; see §E.) Flip `phases.think.status` from `in_progress` to `done`, everything
   else `pending`, `phases.plan.status = "in_progress"`.

   **Write the `run` block.** Set `run.mode` based on the flag passed to `start`:
   - `--step` → `run.mode = "step"` (pas-à-pas : chaque transition de phase rend la
     main, comportement des battles antérieures à la feature).
   - Absent (default) → `run.mode = "autonomous"` (enchaînement autonome après
     l'approbation du plan).
   Initialize `run.autocorrect = { "per_gate": {}, "total": 0 }`.

   > **`--step` vs `--auto` : deux dimensions orthogonales.**
   > `--step` sur `start` (= `run.mode`) pilote la **cadence d'arrêt** de
   > l'orchestrateur entre les phases. `--auto` sur `build` (= délégation au
   > sous-agent `builder`) pilote la **délégation de la production de code**. Les deux
   > sont indépendants : on peut avoir un run `--step` avec ou sans `--auto`, et
   > inversement. Ne pas confondre.

   **Derive `guard.allow` from the real solution layout — never ship the
   placeholder blind.** The schema's `["src/**","tests/**"]` is a *placeholder*: on a
   repo whose projects sit at the root (e.g. `HttpForge/`, `HttpForge.Tests/`) it
   matches nothing, so the guard is armed but silently blocks **every** builder edit
   with no obvious cause. Before writing, detect the project directories
   (`Glob '**/*.csproj'`, take their containing folders) and set `guard.allow` to
   `["<Proj>/**", "<Proj.Tests>/**", …]`. If the placeholder globs would match no
   path in the repo, **warn the user and propose the derived globs** rather than
   locking a dead perimeter. (RETEX: the generic default mismatched a root-projects
   repo on two consecutive battles — the builder would have been blocked without it.)

5. **Invoke the `architect` gate** with the `Agent` tool
   (`subagent_type: architect`). Pass a self-contained prompt: the absolute path
   of `spec.md`, the battle directory, and the repo root. The agent **writes**
   `plan.md` itself (the guard confines it to that single file) and **returns** a
   verdict + the artifact path — not the content.

6. **Record the result.** First run the **gate artifact delivery check** (§E) on
   `plan.md` — the `architect` must have actually written it this pass. `plan.md` is
   already on disk — do **not** re-write it from a returned blob. Once delivery is
   confirmed, update `battle.json`: `phases.plan.verdict` and `phases.plan.status`.
   Read `plan.md` from disk only if you need its detail to report.
   - `revise` / `reject` → `status = "blocked"`; relay the FAILs verbatim and ask
     the user how to adjust the spec. Do **not** advance.
   - `accept` / `accept_with_opportunity` → `status = "done"`. Lire `plan.md` pour
     présenter le résumé, les éventuelles opportunités, et la section
     **« Choix ouverts à arbitrer »** (si elle est présente). Puis demander
     **l'unique approbation explicite** de l'humain — toujours obligatoire, même si
     aucun choix ouvert n'est listé :

     > « Le plan est prêt. Voici le résumé + les choix ouverts. **OK pour lancer le
     > build ?** »

     **Sur OK** : enchaîner directement vers §D (BUILD) dans la même session, en
     annonçant l'enchaînement — ne plus rendre la main. En mode `--step`, rendre la
     main après l'OK (comportement pas-à-pas, cf. §B).

     **Sur modification demandée** : relayer les ajustements et demander à l'humain de
     corriger `spec.md` avant de relancer PLAN. Ce cas est en amont du point
     d'arbitrage et reste légitime.

7. **Report** the battle id, the phase statuses, and the next action.

---

## §B — resume a battle

Read `.legion/battles/<battle-id>/battle.json`. Re-point `.legion/active-battle`
to this id (so the guard hooks track the resumed battle). Summarize phase
statuses and the last verdict. Announce the next pending phase and what it needs.
Do not re-run completed phases unless asked.

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

> **Tableau mode × transition → rend la main ?**
>
> | Mode | Après approbation plan | Après BUILD | Après chaque gate | Avant push/PR (DELIVER) |
> |------|------------------------|-------------|-------------------|------------------------|
> | `autonomous` | Oui (point d'arbitrage) | Non — cascade | Non — cascade | Non (filets = barrière) |
> | `step` | Oui | Oui | Oui | Oui (CONFIRM §G.4) |

---

## §C — status

List every battle under `.legion/battles/`, showing id, profile, and the
current phase with its status/verdict. One line per battle.

---

## §D — build a slice (BUILD phase)

Precondition: `phases.plan.status == "done"` with an `accept` /
`accept_with_opportunity` verdict. Otherwise refuse and point to PLAN.

Resolve the target from the argument: a specific `slice-N`, or `all` (every
slice listed in `plan.md`, in order). Default to the first not-yet-built slice.

**Mark the phase in progress first.** Before coding or delegating, set
`phases.build.status = "in_progress"` in `battle.json` and **persist it**. The
phase must **never stay `pending`** once a build has started — that's how `/fleet`
and a resumed session see work happening. You will reclassify it at the end.

**Mode — inline (default).** Code the slice yourself in this session, applying the
same conventions as the `builder` agent: load `dotnet-claude-kit:clean-architecture`
+ `dotnet-claude-kit:modern-csharp` (code) and `dotnet-claude-kit:testing` (tests);
`dotnet-claude-kit:scaffold` for from-scratch scaffolding. Verify with
`dotnet build` from the current directory (or `dotnet build <stack.build_target>`
when `battle.json.stack.build_target` is set — repo without a `.sln`); **note the
warning count** from the build summary. For `all`, build **each slice in order**,
collecting a `{ slice_id, build_ok, warnings }` per slice. Write `build-report.md`.

**Mode — `--auto`.** Delegate to the `builder` agent via the `Agent` tool
(`subagent_type: builder`). Pass a self-contained prompt: battle dir, `plan.md`
path, `slice_id`, the `guard.allow` globs from `battle.json`, and — when set —
`stack.build_target` (the explicit build target for a repo without a `.sln`). For `all`,
dispatch independent slices in parallel with `isolation: worktree`; keep
dependent slices sequential. Collect each
`{ slice_id, build_ok, warnings, files_touched }`.

After build (either mode), **classify the result and persist `battle.json`
immediately** — for `all`, only after **every targeted slice** has a result:
- **any `build_ok == false`** → `phases.build.status = "blocked"`; relay the
  residual errors. En mode `autonomous`, entrer dans la boucle d'auto-correction
  (§E — boucle de `revise`) ; en mode `step`, stop. Do not advance until resolved.
- **all `build_ok` (with or without warnings)** → `phases.build.status = "done"`.
  Warnings are **non-blocking remarks**: log them (in `build-report.md` and in the
  relay to the user), then **auto-advance straight into §E** (the gate cascade)
  without waiting for a separate command — exactly as for a clean build. Announce
  the chaining and the warning count so the user sees them. En mode `step`, rendre
  la main après avoir annoncé les warnings, sans enchaîner automatiquement.

Persisting the phase status is **not optional**: a build that produced code but
left `phases.build.status` at `pending`/`in_progress` is a bug — always write the
final `done`/`blocked` before handing back.

The orchestrator sequences `builder → gates` — the builder never calls a gate.

## §E — review / test gates

Precondition: `phases.build.status == "done"`. Each gate is a subagent that is
**read-only on the code** but **writes its own single artifact** (`gate-*.md` — the
`guard.py` hook confines it to that one file): it **returns** only a verdict + the
artifact path, not the content. Apply this shared loop for each gate, in order
`lint → reviewer → test-engineer → security`, skipping any gate not in
`battle.json.required_gates`.

### Gate artifact delivery check (shared — every gate, incl. `architect` and `pr-triage`)

A gate now writes its own artifact, so a returned verdict no longer **proves** the
artifact exists. A verdict counts **only if its artifact was written this pass**.
Wrap every gate invocation with this check — it is deterministic (metadata only, you
never read the artifact's content, which would refill the context the confinement
spares):

1. **Before** invoking, resolve the expected artifact path
   `.legion/battles/<id>/<artifact>` (architect → `plan.md`, lint → `gate-lint.md`,
   reviewer → `gate-review.md`, test-engineer → `gate-test.md`, security →
   `gate-security.md`, pr-triage → `pr-feedback.md`) and, **if it already exists** (a
   re-loop round),
   capture its current modified-time (`(Get-Item <path>).LastWriteTimeUtc`).
2. The gate returns `VERDICT … ARTIFACT: <path>`.
3. **After** the return, verify **all four** — metadata only, no content read:
   - the file at the expected path **exists**;
   - it is **non-empty** — `(Get-Item <path>).Length > 0`. A gate can return a verdict
     yet leave a **0-byte** artifact (the `Write` never landed, or wrote nothing); such
     an empty file still **exists**, so the existence check alone would wave it through.
     (RETEX: an empty `gate-review.md` would have passed the literal check.)
   - the returned `ARTIFACT:` path **equals** the expected canonical path (the guard
     already blocks a wrong *write*; this catches a wrong path in the *returned* string);
   - it was **written this pass** — it either did not exist before, or its
     modified-time is now **strictly newer** than the value captured in step 1 (so a
     stale artifact from a previous round is never mistaken for a fresh one).
4. **On any failure** → do **not** record the verdict and do **not** advance.
   Re-invoke the gate **once** with an explicit reminder ("write your artifact to
   `<exact path>` first, then return your verdict"). If it still fails → set the phase
   `status = "blocked"`, surface it to the user, and stop. **Never advance the pipeline
   on a verdict whose fresh artifact you could not confirm.**

The gate identifier (used for `subagent_type` and `required_gates`) is **not**
the phase key written to `battle.json.phases`. Map gate → phase key before
persisting: `lint → lint`, `reviewer → review`, `test-engineer → test`,
`security → security`. Writing under the gate name
(`phases.reviewer`/`phases.test-engineer`) is a bug — the UI reads the canonical
keys `lint`/`review`/`test` and would show the phase as pending even though the
gate ran.

1. **Invoke** the gate via `Agent` (`subagent_type`: `lint` | `reviewer` |
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
   targets the test project explicitly.
2. **Run the gate artifact delivery check** (above) on the gate's artifact, then
   **record the verdict.** The gate already wrote its artifact (`gate-review.md` /
   `gate-test.md` / `gate-security.md`) on disk — do **not** re-write it from a
   returned blob. Once delivery is confirmed, record `phases.<phase-key>.verdict` +
   `status` in `battle.json` (using the phase key from the mapping above, e.g. the
   `reviewer` gate writes `phases.review`).
3. **Branch on the verdict** (cascade):
   - `accept` / `accept_with_opportunity` → `status = "done"`, continue to the
     next gate. Log any opportunity.
   - `reject` → `status = "blocked"`, **escalade immédiate** (cas 1 de la taxonomie).
     Relay the verdict's one-line RAISON and hand back — zero tentative de correction.
     La replanification est requise.
   - `revise` → `status = "blocked"`. En mode `step`, relay the RAISON and hand back
     (the fix loops back to BUILD). En mode `autonomous`, entrer dans la **boucle
     d'auto-correction** :

     **Boucle d'auto-correction** (mode `autonomous` uniquement) :
     a. Incrémenter `run.autocorrect.per_gate[<gate>]` et `run.autocorrect.total`.
     b. Vérifier les bornes **avant** de relancer :
        - Si `run.autocorrect.per_gate[<gate>] >= 2` → **escalade** (cas 2 : plafond
          par gate atteint, 2 tentatives maximum).
        - Si `run.autocorrect.total >= 6` → **escalade** (cas 2 : plafond global
          atteint, 6 tentatives maximum au global).
     c. **Détecter le progrès par l'identité des FAIL, pas par le compte brut.**
        Compare l'**ensemble** des FAIL du nouveau `gate-*.md` à celui du run précédent,
        par cible (`fichier:ligne` + dimension, ex. `R2`/`S3`). **Progrès** = au moins
        un FAIL ciblé au run précédent a **disparu** (résolu) — même si le compte total
        est stable parce qu'un nouveau FAIL d'une autre cause est apparu. **Non-progrès**
        = aucun FAIL précédent résolu (le même ensemble persiste ou grossit) →
        **escalade immédiate** (cas 2). Le compte brut seul est trompeur : « 1 FAIL
        corrigé, 1 autre découvert » est un compte stable mais un vrai progrès.
     d. Passer au builder le **chemin de l'artefact** `gate-*.md` (lire depuis le
        disque, ne pas injecter le contenu dans ce contexte) et relancer BUILD pour
        cette slice. Puis re-invoquer la gate. Retour à l'étape (a).

     **Escalade** : `status = "blocked"`, relayer le détail du blocage (gate, FAIL-count,
     tentatives effectuées), rendre la main. **Pass the builder the artifact path**
     (`gate-review.md` / `gate-test.md`) so it reads the FAIL detail from disk — do not
     pull the full gate content into this session just to brief it (that would refill
     the context the confinement is meant to spare).

When all required review/test/security gates are `done`: in mode `autonomous`,
**enchaîner directement vers §G (DELIVER)** sans attendre une commande séparée —
annoncer l'enchaînement. En mode `step`, annoncer la disponibilité pour DELIVER et
rendre la main.

### Non-.NET stack — gate adaptation

When the battle is flagged **non-.NET** (§A.preflight stack detection), the default
.NET assumptions do not hold. For **every** gate prompt (here in §E and the
`architect` gate in §A.1), make the deviation explicit so the subagent does not
fall back to .NET tooling:

- **Drop the .NET tooling assumptions** from the prompt: no Roslyn / `cwm-roslyn`
  MCP, no `dotnet build` / `dotnet test`. State the repo's **actual** build/test/lint
  commands instead (read them from `package.json` scripts, `Makefile`, CI config —
  whatever the repo uses) and pass them in the prompt.
- **Tell the subagent not to load `dotnet-claude-kit` skills** (RETEX: subagents
  loaded .NET skills for lack of an equivalent). Instruct it to reason from the
  repo's own conventions and the generic review/test lenses, not a .NET ruleset.
- **Keep the verdict contract unchanged** (`accept` / `accept_with_opportunity` /
  `revise` / `reject`) — only the *toolchain* the gate reasons over changes, not the
  cascade or how you persist it.

The orchestrator owns this: the agents default to .NET; it is the gate prompt that
carries the non-.NET deviation per battle.

---

## §F — Autonomie & escalade

### Taxonomie d'escalade (liste close)

L'orchestrateur rend la main à l'humain **uniquement** dans les cas suivants.
Toute correction déterministe se fait sans lui.

| Cas | Déclencheur | Action |
|-----|-------------|--------|
| **1. `reject`** | Une gate rend un verdict `reject` (régression majeure, redesign requis). | Escalade **immédiate**, zéro tentative de correction. Relayer le verdict + RAISON. |
| **2. Boucle non convergente** | Aucun FAIL ciblé résolu d'une tentative à l'autre (progrès = identité des FAIL, pas le compte brut), ou plafond atteint (2 tentatives/gate, 6 tentatives au global). | Escalade avec le détail : gate, FAIL résolus/persistants/nouveaux, tentatives effectuées. |
| **3. Déviation du plan** | La correction requise sort du périmètre figé (slices de `plan.md` ou `guard.allow`). | Escalade : re-planification nécessaire. Ne pas modifier `plan.md` en cours de run. |
| **4. Filets DELIVER déclenchés** | Base locale en retard sur `origin`, remote vide, fichier hors whitelist, `.gitignore` auto-induit à arbitrer (§G.0). | Escalade : résoudre le filet d'abord, puis DELIVER peut reprendre. |
| **5. Préflight défaillant** | `python` absent, `gh` absent/non authentifié, stack ambiguë (§A.preflight). | Escalade : résoudre l'environnement avant toute battle. |

> **Hors liste = pas d'escalade.** Toute autre situation (warning de build,
> `accept_with_opportunity`, opportunité de découpe) est résolue automatiquement.

### Budgets de boucle (deux niveaux distincts)

Les deux budgets sont **indépendants et non additionnés** :

- **Boucle interne `build-fix` du builder** : 3 tentatives sur `dotnet build`.
  Gérée par le builder lui-même ; le builder ne décide pas d'escalader (il rapporte
  `build_ok: false` si son budget est épuisé).
- **Boucle orchestrateur (re-gate)** : 2 tentatives par gate (maximum ferme), plafond global de 6 tentatives au global (maximum ferme)
  sur le run. Un `build_ok: false` du builder après ses 3 essais **compte pour
  1 tentative** de la boucle orchestrateur.

La boucle orchestrateur opère un cran au-dessus : elle borne les **re-gate**, pas
les re-builds internes du builder.

---

## §G — deliver (branch, commit, push, PR) — final step

Precondition: every required review/test/security gate `done`. This step **writes
and pushes**. En mode `autonomous` (chemin heureux), la PR est composée, poussée et
ouverte **sans OK bloquant** — l'humain relit le code sur GitHub. Les filets §G.0
(ci-dessous) sont la **dernière barrière** : chacun, s'il se déclenche, **escalade**
(cas 4 de la taxonomie — §F). En mode `step`, le comportement historique est
maintenu : afficher l'effet sortant et attendre un OK explicite (§G.4 ci-dessous).
Once the PR is open, hand back: the human reviews it. New review comments are handled
by `/legion:battle address` (§H, repeatable); when the PR is stabilized,
`/legion:retro` closes the battle.

0. **Pre-branch safety nets.** Before branching, two checks:

   a. **Base freshness** — the gates must have judged the base you actually ship.
      Fetch and compare HEAD against the remote default branch: `git fetch origin`,
      then `git rev-list --count HEAD..origin/<default>` (resolve `<default>` via
      `git symbolic-ref refs/remotes/origin/HEAD`, fallback
      `gh repo view --json defaultBranchRef`). **> 0 → the local base is behind
      origin** (work already merged elsewhere under another SHA, or a dependency/SDK
      migration you don't have locally): **stop, integrate first** (rebase or merge
      origin), then **re-run BUILD + the review/test gates on the updated base**
      before delivering. A rebase changes what ships, so gate verdicts on the stale
      base do **not** carry over. (RETEX: a base behind origin's default was only
      caught at deliver, after the gates had validated a base that wasn't shipped.)

   b. **Empty remote** — the flow assumes the remote already has a base branch.
      Check `git ls-remote --heads origin` — **no heads** means an uninitialized
      repo. Delivering a feature branch into it makes that branch the **default**,
      so the PR's source == target → `gh pr create` fails (HTTP 400), no PR
      possible. If the remote is empty, **stop the normal flow** and offer a choice
      (do **not** branch yet):
      - **Bootstrap a base** — create an initial commit (empty is fine) on the
        default branch, push it (`git push -u origin <default>`) so it becomes the
        default, then replant the feature work on `<me>/<token>` and continue from
        step 1.
      - **Import directly on the default branch** — push the work as the default
        branch (no PR for this first drop; the repo gets its history). Subsequent
        battles deliver normally.

      **Do not script the default-branch switch** — flipping a repo's default
      branch is a **repo-admin UI action** on GitHub; surface it to the user rather
      than attempting it by script.

1. **Branch name** — `<me>/<token>`: `<me>` from `git config user.email` (local
   part before `@`; fallback `user.name`), `<token>` from `battle.json.ticket`
   (`GH#<n>` → `<n>`) or the battle slug if no issue. Create/switch:
   `git checkout -b <me>/<token>` (or switch if it already exists). Never `cd`.

2. **Commit** — stage the **code/test changes only**, by an **explicit whitelist
   of paths** (e.g. `git add src/… tests/…`). **Never `git add -A`/`.`** and never
   trust the `.legion/` ignore: as a defensive net, **unstage the battle dir
   unconditionally before committing**: `git reset -q HEAD .legion` (and verify
   with `git status` that no `.legion/` path is staged).

   **Resolve the `.gitignore` change `start` may have introduced.** If §A.1 added a
   `.legion/` line to the repo's `.gitignore`, that is a pending working-tree change
   the issue did not ask for — and it falls **outside** the whitelist/`git reset`
   discipline above (it is not a `.legion/` path). Decide its fate **explicitly**,
   do not leave it dangling: either **commit it first** as a standalone
   `chore: ignore .legion battle artifacts`, or **add `.gitignore` to the whitelist**
   of this commit (it is repo hygiene this battle introduced). Confirm the choice
   with the user. (RETEX: this self-induced change was ambiguous at commit time.)

   **`<summary>` — English, Conventional Commits format** `type(scope): subject`
   (imperative mood, no trailing period, ≤ ~70 chars). `type` ∈
   `feat|fix|refactor|perf|docs|test|build|ci|chore`; `scope` = the touched area.
   This subject feeds **both** the commit subject **and** the PR title (step 5).

   Then commit with the global co-author trailer:
   ```
   <summary>

   <1–3 lines: what & why, from spec.md>

   Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
   ```

   **Where the commit-message file lives (if you use one).** A multi-line body with
   accents or other non-ASCII punctuation is fragile through `-m` on a Windows console;
   writing the message to a file and committing with `git commit -F <file>` is more
   robust. When you do, that file **must live under `.legion/battles/<id>/`** (e.g.
   `.legion/battles/<id>/commit-msg.txt`) — **never outside the repo** (no
   `$CLAUDE_JOB_DIR/tmp/…`). Reason: once a write scope is armed (`/freeze` or
   `/guard`), the `guard.py` hook blocks every write outside it, while `.legion/**` is
   always allowed and git-ignored — so a scratch message file there is writable yet
   never committed (the path whitelist + `git reset -q HEAD .legion` above already
   guarantee it). `git commit -m` stays fine for a short one-line subject.

3. **Compose the PR body** → write `.legion/battles/<id>/pr-body.md` from the
   artifacts: intent/scope (`spec.md`), approach (`plan.md`), and the gate verdicts
   (review/test/security `accept`). **Written in French** (identifiers & file names
   stay English). Apply the **writing charter**
   (`battle-workflow` § « Charte de style des documents ») — simple, precise language;
   **keep it synthetic, with no separate « En bref »** (the body is short by design),
   and reread it against the charter before pushing. For a numeric issue, **end the body with `Closes #<n>`** so
   merging the PR auto-closes the issue. This is the payoff of the artifact
   pipeline — the PR documents itself.

4. **CONFIRM (mode `step` uniquement)** — En mode `step`, avant de pousser, afficher
   à l'utilisateur **tout l'effet sortant** : branche cible, message de commit,
   `git diff --stat`, titre/cible de la PR. **Attendre un OK explicite.** Ne pas
   pousser avant.

   En mode `autonomous`, sauter ce CONFIRM : passer directement à l'étape 5. Les
   filets §G.0 sont la seule barrière — la discipline de staging (whitelist de chemins,
   `git reset -q HEAD .legion`) reste intacte et **non affaiblie** : elle s'applique
   quelle que soit la valeur de `run.mode`.

5. **Push & open the PR**:
   ```bash
   git push -u origin <me>/<token>
   gh pr create --title "<summary>" --body-file ".legion/battles/<id>/pr-body.md" --fill-first --base <default-branch>
   ```
   Use the repo's default branch as `--base` (read it once, e.g.
   `gh repo view --json defaultBranchRef`). Record the printed PR URL in
   `battle.json` (`delivery.pr_url`). If `gh` is unavailable → give the user the
   push command + a ready-to-paste PR body and stop.

6. **Comment the issue** (numeric issue only; best-effort, never blocking). Write a
   **short battle-review comment** to `.legion/battles/<id>/wi-comment.md`: 3–5
   lines, **in French**, what was delivered and why it matters (from `spec.md` +
   gate outcomes); end with the PR URL. Apply the **writing charter** (`battle-workflow`
   § « Charte de style des documents ») — simple, precise language; it is already short,
   so **no separate « En bref »**; reread it against the charter before posting. Then:
   ```bash
   gh issue comment <n> --body-file ".legion/battles/<id>/wi-comment.md"
   ```
   The issue itself closes on merge via `Closes #<n>` — do not close it here. On
   failure → **warn and continue**: the PR is already created.

7. **Close the phase** — set `phases.deliver.status = "done"` in `battle.json`
   (record `delivery.pr_url`). Report the PR URL; if the PR draws review comments,
   point to `/legion:battle address` (§H); suggest `/legion:retro` once the PR is
   stabilized.

## §H — address (handle PR review comments) — repeatable, post-deliver

Precondition: `delivery.pr_url` is set **and** the PR is still open. Resolve the PR
number `<n>` from the `pr_url` tail and check `gh pr view <n> --json state -q .state`
== `OPEN`. If `MERGED`/`CLOSED`, or there is no `pr_url` → refuse (nothing to
address, or deliver hasn't happened).

This phase is **optional and repeatable**: the human may comment in several waves.
Each run is a **round** (`phases.address.round`, incremented). All battle-state
writes stay yours; `pr-triage` only returns. Battle artifacts live under `.legion/`
(git-ignored), so the temp files below are never committed.

Resolve `<owner>`/`<repo>` once: `gh repo view --json nameWithOwner -q .nameWithOwner`.

1. **Fetch active threads.** GitHub exposes review-thread resolution **only via
   GraphQL** (the REST API does not return `isResolved`):

   ```bash
   gh api graphql -F owner=<owner> -F repo=<repo> -F number=<n> -f query='
     query($owner:String!,$repo:String!,$number:Int!){
       repository(owner:$owner,name:$repo){ pullRequest(number:$number){
         reviewThreads(first:100){ nodes{
           id isResolved isOutdated
           comments(first:50){ nodes{ databaseId author{login} body path line } } } } } } }'
   ```

   **Keep only actionable threads**: `isResolved == false` **and** carrying ≥1 human
   comment (real author + body; skip bot/automation authors and empty system
   threads). For each kept thread record `{ thread_id: <node id>, file: <path>,
   line, comments:[{id: <databaseId>, author, content}] }` and write the list to
   `.legion/battles/<id>/_threads.json`. **Empty list** → announce "no active review
   comment" and **stop**.

2. **Triage.** Invoke the `pr-triage` gate via `Agent` (`subagent_type: pr-triage`).
   Self-contained prompt: `plan.md` path, `_threads.json` path, battle dir, repo
   root, the PR branch `<me>/<token>`. The gate **writes** `pr-feedback.md` itself
   (appending this round when the file already exists — the guard confines it to
   that one file) and **returns** the `TRIAGE:` JSON block. **Run the gate artifact
   delivery check** (§E) on `pr-feedback.md` — the modified-time guard especially
   matters here, since the file usually exists from a previous round and the gate must
   have **re-written** it this round (appended). Then set `phases.address =
   { "status": "in_progress", "round": <n> }` and parse the returned `TRIAGE` JSON
   to route. You complete `pr-feedback.md` later (step 4: commit SHAs + resolutions)
   — that later write is yours (the orchestrator is not guard-confined), not the
   gate's.

3. **Apply, thread by thread** (JSON order). `target: none` threads produce **no**
   code — reply only (step 7).
   - **`target: builder`** → code the fix (inline by default, or delegate to the
     `builder` via `--auto`), **inside `guard.allow`**. Then **one commit per
     thread** — stage the code/test changes only (never `git add -A`; defensive
     `git reset -q HEAD .legion` first):
     ```bash
     git commit -m "fix(review): <summary>"
     ```
     Capture the short SHA (`git rev-parse --short HEAD`) for the reply + artifact.
   - **`target: architect`** → re-judge via the `architect` gate (§A.1 step 5). On
     `revise`/`reject`, update `plan.md`, then the `builder` applies → commit.
   - **Re-gate by blast radius** (`requires_regate`): for `code-logic` / `test`
     threads, re-run the **§E** cascade (`reviewer` then `test-engineer`) **on the
     fix**. A `revise` loops back to BUILD (fix, re-commit) before the thread may be
     resolved. `code-trivial` skips the re-gate.

4. **Update `pr-feedback.md`** — fill each thread's **Commit** (SHA) and target
   **Resolution**.

5. **CONFIRM (outward effects).** Show the user, for this round: the commits created
   (SHA + message), and per thread the reply to be posted + whether the thread will
   be resolved. **Wait for explicit OK.** For any `disagreement` → resolve-as-wontFix,
   require **per-thread** confirmation — never close a disagreement without the
   user's agreement.

6. **Push**: `git push origin <me>/<token>` (the commits join the existing PR).

7. **Reply + resolve** each thread via `gh api graphql` (**best-effort per thread**:
   one failure does not abort the others — warn and continue):
   - reply = `reply_fr` + (if a commit) ` « Corrigé en <sha>. »` :
     ```bash
     gh api graphql -F tid=<thread_id> -F body='<reply>' -f query='
       mutation($tid:ID!,$body:String!){
         addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$tid, body:$body}){ comment{ id } } }'
     ```
   - **resolution** — GitHub has no `fixed`/`wontFix` distinction: a thread is
     resolved or not (the label is kept only in `pr-feedback.md` + `battle.json`):
     - actionable **fixed**, or a confirmed **`disagreement`** (wontFix) → resolve:
       ```bash
       gh api graphql -F tid=<thread_id> -f query='
         mutation($tid:ID!){ resolveReviewThread(input:{threadId:$tid}){ thread{ isResolved } } }'
       ```
     - `question` → **do not** resolve (leave it `active`; the author decides).

   **Verify each resolution actually applied** (RETEX). Re-run the step-1 query and
   confirm each thread's real `isResolved` matches the intended resolution. Persist
   (step 8) the **observed** status, never the value you meant to write. Any
   mismatch → re-issue the resolve (or surface it to the user); do not mark the
   round `done` while a thread you meant to close is still unresolved server-side.

8. **Persist `battle.json`** — `phases.address = { "status": "done", "round": <n>,
   "threads": [ { "id", "target", "kind", "commit": "<sha|null>",
   "resolution": "fixed|active|wontFix" } ] }` using the **re-fetched** statuses from
   step 7. Delete the temp file (`_threads.json`).

9. **Report** — threads handled / resolved / left open, commits pushed, and the
   reminder: new comments → re-run `/legion:battle address` (next round). Once the
   PR is merged/stabilized, `/legion:retro` closes the battle.

## Guardrails

- Never `cd` / `Set-Location`; operate from the current directory.
- Persist `battle.json` / `spec.md` / PR artifacts yourself; each gate writes **only
  its own** `gate-*.md` / `plan.md` / `pr-feedback.md` (guard-confined) and returns
  verdict + path — nothing else.
- Stop on `revise`/`reject` — the pipeline does not advance.
- Delegate concrete .NET reasoning to `dotnet-claude-kit` skills; do not duplicate
  them here.
