---
name: battle-workflow
description: Operational doctrine for the legion battle pipeline (Think→Plan→Build→Lint→Review→Test→Deliver→Reflect). Use whenever running, resuming or reasoning about a battle, a gate, a builder slice, delivery/PR, /battle, /fleet, /retro, or a GitHub-issue-driven task. Defines the phases, the producer/gate split, the verdict cascade, the per-repo state layout and the delegation rules.
---

# Battle workflow — legion doctrine

`legion` is an **orchestrator, not a reimplementation**. It coordinates a battle
pipeline and delegates every concrete .NET task to existing skills
(`dotnet-claude-kit`). If a capability already exists as a skill, **invoke it** —
never duplicate it.

The full design rationale lives in the plugin's `ARCHITECTURE.md`. This skill is
the operational summary loaded at run time.

> **Commands are namespaced.** When you surface a command to the user, always write
> the **namespaced** form — `/legion:battle review`, `/legion:retro`,
> `/legion:fleet` — never bare `/battle` (which resolves to *Unknown command*).
> The bare `/battle`, `/retro`, `/fleet` used throughout this doctrine is shorthand
> for reading, not for relaying verbatim.

## The pipeline

```
THINK  → PLAN     → BUILD    → LINT  → REVIEW  → TEST     → DELIVER → (ADDRESS) → REFLECT
start    architect  builder    lint    reviewer  test-eng   deliver   pr-triage    retro
         (gate)     (producer) (gate)  (gate)    (gate)     (PR)      (gate)
```

> **LINT** (formatage .NET, `dotnet format --verify-no-changes`, verify-only) est la
> première gate de la cascade de revue. **.NET-only** : sur une stack non-.NET elle se
> retire (bannière + verdict neutre `accept`), sans bloquer la cascade.

Each step **hands off a markdown artifact** to the next. A gate reads the
upstream artifact, judges it, and emits a verdict. The pipeline does **not**
advance on `revise` or `reject` — a fix loops back to BUILD.

**Point d'arbitrage unique.** Le seul rendez-vous garanti avec l'humain est
l'**approbation du plan** (après PLAN, avant BUILD). L'orchestrateur présente le
résumé du `plan.md` avec ses choix ouverts et attend un OK explicite. Toujours
obligatoire, même sans choix ouvert. En mode `autonomous` (défaut), sur OK,
l'orchestrateur enchaîne directement BUILD → gates → DELIVER sans rendre la main —
sauf escalade. En mode `step`, chaque transition de phase rend la main.

**Optional pre-THINK: recon.** Before `start` consumes a rough issue, the `recon`
skill (`/legion:recon <issue>`) can sharpen it first — a relentless interview (one
question at a time, exploring the repo to answer its own questions) that appends a
structured « Cadrage » section to the issue. It is **stateless**: it touches no
`.legion/` state and starts no battle — it just makes the issue THINK reads already
sharp, so `spec.md` seeds rich and the `architect` gate has less to push back on. No
phase of its own in the diagram; it feeds THINK.

**ADDRESS is optional and repeatable.** It runs only when the open PR draws human
review comments: `/battle address` triages them (the `pr-triage` gate), loops fixes
back through BUILD/REVIEW/TEST, replies and resolves the threads — one round per
comment wave. No comments → the phase never exists; `/retro` closes the battle as
usual.

**Auto-advance on a successful build (warnings non bloquants).** A build that is
`build_ok` — with **or without warnings** — chains straight into the gate cascade
`lint → review → test → security` (no separate command) in `autonomous` mode. Warnings are
**non-blocking remarks**: they are logged in `build-report.md` and relayed to the user,
then the cascade continues uninterrupted. Only `build_ok == false` blocks the cascade
(→ boucle d'auto-correction). When all required gates are `done`, the cascade chains
automatically into DELIVER (in `autonomous` mode). In `step` mode, each transition
hands back to the user.

## Autonomous run & escalation

### Taxonomie d'escalade (liste close)

L'orchestrateur rend la main à l'humain **uniquement** dans les cas suivants :

| Cas | Déclencheur | Action |
|-----|-------------|--------|
| **1. `reject`** | Une gate rend un verdict `reject`. | Escalade immédiate, zéro tentative. |
| **2. Boucle non convergente** | Aucun FAIL ciblé résolu d'une tentative à l'autre (progrès = identité des FAIL, pas le compte brut), ou plafond atteint (2 tentatives/gate, 6 tentatives au global). | Escalade avec le détail du blocage. |
| **3. Déviation du plan** | La correction sort du périmètre de `plan.md` ou `guard.allow`. | Escalade : re-planification nécessaire. |
| **4. Filets DELIVER** | Base en retard sur `origin`, remote vide, fichier hors whitelist, `.gitignore` auto-induit. | Escalade : résoudre le filet d'abord. |
| **5. Préflight défaillant** | `python` absent, `gh` absent/non authentifié, stack ambiguë. | Escalade : résoudre l'environnement. |

Hors liste = pas d'escalade. Tout ce qui est déterministe se corrige automatiquement.

### Budgets de boucle

- **Builder (boucle interne `build-fix`)** : 3 tentatives sur `dotnet build`. Le
  builder ne décide pas d'escalader — il rapporte `build_ok: false`. Un `build_ok:
  false` après 3 essais **compte pour 1 tentative** de la boucle orchestrateur.
- **Orchestrateur (re-gate)** : 2 tentatives par gate (maximum ferme), plafond global de 6 tentatives au global (maximum ferme).
  Progrès = au moins un FAIL ciblé résolu entre deux tentatives (mesuré par l'identité
  des FAIL — `fichier:ligne` + dimension —, pas le compte brut) ; aucun FAIL précédent
  résolu → escalade immédiate.

## Two natures of actor

- **Producer** — `builder` only. It *writes* code from `plan.md` and reports in
  `build-report.md`. Not read-only, emits no verdict: its output is what the
  gates review.
- **Gates** — `architect`, `lint`, `reviewer`, `test-engineer`, `security`
  (+ `pr-triage`). They *judge* a deliverable. **Read-only on the code**, but each **writes its own
  single artifact** (`plan.md` / `gate-*.md` / `pr-feedback.md`) and returns only its
  **verdict + the artifact path** — never the full content, which keeps it out of the
  orchestrator's context. The `guard.py` hook **confines** each gate to that one file
  (via `agent_type`): invariant "gate à écriture confinée". The orchestrator persists
  the rest (`battle.json`, `spec.md`, PR artifacts) and reads gate artifacts from disk
  on demand. (`pr-triage` also returns its TRIAGE JSON for routing.) Because a verdict
  no longer proves the artifact exists, the orchestrator runs a deterministic
  **delivery check** before trusting it (artifact exists, non-empty, canonical path,
  freshly written this pass via mtime) — see `battle.md` §E.

Sequencing rule: the **orchestrator** (`/battle`) chains `builder → gates`. A gate
never invokes another agent; the builder never invokes a gate.

## Verdict cascade

| Verdict | Meaning | Pipeline effect |
|---|---|---|
| `accept` | 0 FAIL, criteria met | Phase closed, advance. |
| `accept_with_opportunity` | 0 FAIL, ≥1 improvement spotted | Advance; opportunity logged in the artifact. |
| `revise` | ≥1 FAIL | **Stop.** Fix (back to BUILD) and re-run the gate. |
| `reject` | Major regression / unusable | **Stop.** Redesign required. |

## Phases and delegation

| Phase | Driver | Artifact | Delegates to |
|---|---|---|---|
| THINK | `/battle start` | `spec.md` | `gh issue view <n>` (numeric id), else inline |
| PLAN | `architect` gate | `plan.md` (+ test matrix) | `dotnet-claude-kit:clean-architecture`, `:modern-csharp` |
| BUILD | `builder` producer | code + `build-report.md` | `dotnet-claude-kit:scaffold`, `:build-fix`, `:modern-csharp`, `:testing` |
| LINT | `lint` gate | `gate-lint.md` | `dotnet format --verify-no-changes` (.NET-only, verify; self-retires on non-.NET) |
| REVIEW | `reviewer` gate | `gate-review.md` | `dotnet-claude-kit:code-review` (multi-dim: correctness, **plan conformance**, **performance**, conventions, dead code) + Roslyn MCP |
| TEST | `test-engineer` gate | `gate-test.md` | `dotnet-claude-kit:testing`, `:tdd`, `:verify` |
| (sec) | `security` gate | `gate-security.md` | `dotnet-claude-kit:security-scan` |
| DELIVER | `/battle deliver` | `pr-body.md`, `wi-comment.md` | `gh pr create` (PR with `Closes #<n>`), `gh issue comment` |
| (ADDRESS) | `pr-triage` gate | `pr-feedback.md` | `gh api graphql` (PR review threads); loops fixes back to BUILD/REVIEW/TEST |
| REFLECT | `/retro` | `retro.md` | Claude memory |

Gates are optional per battle **profile** (`feature` / `hotfix` / `security` /
`spike`); `battle.json.required_gates` declares which ones block.

## DELIVER

`/battle deliver` branches `<me>/<token>` → commit (Conventional Commits subject +
co-author trailer) → compose `pr-body.md` from the artifacts → push → open the PR via
`gh pr create`. For a numeric issue, the PR body ends with **`Closes #<n>`** so merging
auto-closes the issue.

**En mode `autonomous` (chemin heureux)** : la PR est composée, poussée et ouverte
**sans OK bloquant** — l'humain relit le code sur GitHub. Les **filets §G.0** (base en
retard sur `origin`, remote vide, fichier hors whitelist, `.gitignore` auto-induit) sont
la **dernière barrière** : chacun, s'il se déclenche, **escalade** (cas 4). La discipline
de staging (whitelist de chemins, `git reset -q HEAD .legion`) reste stricte et non
affaiblie, quel que soit le mode.

**En mode `step`** : afficher l'effet sortant et attendre un OK explicite avant de
pousser.

Best-effort, non-blocking: post a short business-facing comment to the issue
(`wi-comment.md`) via `gh issue comment`. Then the human reviews — review comments are
handled by `/battle address` (ADDRESS, below), and `/retro` closes the battle once
stabilized.

## ADDRESS (optional, repeatable, post-deliver)

`/battle address` handles **human PR review comments** once the PR is open. It
fetches the unresolved review threads (`gh api graphql` — the REST API does not
expose `isResolved`), hands them to the read-only `pr-triage` gate (classifies each
thread → `target` builder/architect/none, `kind`, `requires_regate` + drafts a FR
reply), then the orchestrator applies the fixes (one commit per thread, re-gating
`code-logic`/`test` through REVIEW/TEST), pushes, and **replies + resolves** each
thread — verifying the resolution actually stuck server-side before persisting.
Repeatable: one **round** per comment wave (`phases.address.round`). Artifact:
`pr-feedback.md`. GitHub has no `fixed`/`wontFix` distinction — both resolve the
thread; a `question` is left unresolved for the author.

## Vocabulaire d'autonomie

> Ces termes sont définis ici une seule fois ; les autres sections y réfèrent sans les
> redéfinir.

| Terme | Définition |
|---|---|
| **Run autonome** | Enchaînement BUILD → gates → DELIVER déclenché automatiquement après l'approbation du plan, sans intervention humaine intermédiaire, sauf escalade. |
| **Arbitrage** | Décision que seul l'humain peut trancher (taxonomie d'escalade ci-dessous). L'orchestrateur s'arrête et rend la main uniquement dans ces cas. |
| **Boucle d'auto-correction** | Sur `revise` d'une gate ou `build_ok == false`, l'orchestrateur re-build et re-gate sans rendre la main, jusqu'à convergence ou épuisement du budget (2 tentatives/gate, 6 tentatives au global — maximums fermes). |
| **Escalade** | L'orchestrateur rend la main à l'humain avec le détail du blocage. Toujours motivée par un cas de la taxonomie d'escalade. |

## State layout

Per repo — `.legion/battles/<battle-id>/`:

```
battle.json   # metadata, profile, required_gates, per-phase status, guard, delivery.pr_url
spec.md  plan.md  build-report.md  gate-lint.md  gate-review.md  gate-test.md
gate-security.md  pr-body.md  wi-comment.md  usage.jsonl  retro.md
```

`battle.json` schema (write it from this — **no need to open `ARCHITECTURE.md` at
run time**):

```jsonc
{
  "id": "2026-06-08-GH-1234",          // <YYYY-MM-DD>-<token>
  "repo": "billing-api",
  "ticket": "GH#1234",                  // "GH#<n>" or a free slug
  "title": "…",
  "profile": "feature",                 // feature | hotfix | security | spike
  "required_gates": ["architect", "lint", "reviewer", "test-engineer"],
  "phases": {
    "think":   { "status": "done", "artifact": "spec.md" },
    "plan":    { "status": "in_progress", "artifact": "plan.md", "verdict": null },
    "build":   { "status": "pending" },
    "lint":    { "status": "pending" },     // .NET-only — self-retires (neutral accept) on non-.NET
    "review":  { "status": "pending" },
    "test":    { "status": "pending" },
    "deliver": { "status": "pending" },
    "reflect": { "status": "pending" }
    // "address" is NOT in the default set — `/battle address` adds it on demand
    // (optional, repeatable): { status, round, threads:[{id,target,kind,commit,resolution}] }
  },
  // guard.allow = PLACEHOLDER — `/battle start` derives it from detected *.csproj
  // dirs (e.g. ["HttpForge/**","HttpForge.Tests/**"]); src/**+tests/** matches
  // nothing on a root-projects repo and silently blocks every builder edit.
  "guard": { "allow": ["src/**", "tests/**"], "deny": [], "careful": false },
  "stack": { "kind": ".net", "build_target": null, "test_target": null },  // *_target: explicit csproj when the repo has NO .sln; null ⇒ run dotnet from root
  // run — mode d'exécution de la battle (écrit par /battle start).
  // Absent (battle antérieure à la feature) ⇒ "autonomous" par défaut : aucune battle
  // en cours n'est cassée par l'ajout de ce champ.
  // --step sur start écrit run.mode = "step" (comportement pas-à-pas, cf. §B/§D/§E/§G).
  "run": {
    "mode": "autonomous",               // "autonomous" | "step"
    "autocorrect": {
      "per_gate": {},                   // { "review": 1, "test": 0, … } — tentatives par gate
      "total": 0                        // compteur global de tentatives (plafond : 6 au global, maximum ferme)
    }
  },
  "delivery": { "pr_url": null }
}
```

Status ∈ `pending | in_progress | done | blocked`; gate `verdict` ∈
`accept | accept_with_opportunity | revise | reject`.

`usage.jsonl` (append-only) records the **approximate token cost** and the
**skills actually used** — written by the `usage_track` hook on `Stop`
(main session, delta) and `SubagentStop` (delegated builder/gates, which are
invisible to the main session's hooks). `/retro` aggregates it; `fleet_sync`
projects `tokens_total` + `skills` into the shard for the UI.

> A skill is recorded **only when invoked via the `Skill` tool**. The gates and
> the builder therefore carry `Skill` in their tool whitelist so "load
> `code-review`/`scaffold`/…" actually loads the skill (real instructions) **and**
> shows up in the subagent's `skills`. A gate that only applied a skill's method
> from memory would leave `skills` empty.

A new session **resumes** a battle by reading `battle.json` — no conversational
context required. The active battle is pointed to by `.legion/active-battle`.
**`run.mode` est lu et respecté au resume** : une battle `step` ne s'emballe pas,
une battle `autonomous` ré-enchaîne depuis la phase pending. Un champ `run` absent
(battle antérieure à la feature) → comportement `autonomous` par défaut.

Global — `~/.claude/legion/`: `fleet.d/` (cross-repo battle index, one JSON shard
per battle, read by `/fleet` and any UI; concurrency-safe — one file per battle, no
shared index, so parallel Claude sessions never overwrite each other). One Claude
session = one repo; the fleet makes battles observable and resumable, it does not
multiplex sessions.

A retro feeds one improvement loop: the **code/project** learning → Claude project
memory. Persist only what would change how the *next* battle is run.

## Guardrails

`/freeze`, `/guard`, `/careful` set `battle.json.guard`. PreToolUse hooks enforce:
`guard.py` **blocks** edits outside `guard.allow` (`exit 2`), `careful.py`
**warns** (never blocks) on destructive shell commands. Bypass:
`LEGION_GUARD_OFF=1`. The `builder` is subject to the same guard. **Gate
confinement**: `guard.py` also reads `agent_type` and restricts each gate
(`architect`/`lint`/`reviewer`/`test-engineer`/`security`/`pr-triage`) to writing
**only** its own artifact under `.legion/battles/<active>/` — any other write (code,
`battle.json`, another gate's file) is blocked, even when the perimeter guard is not
armed.

## Conventions

- **Every markdown *battle artifact* is written in French** (`spec.md`, `plan.md`,
  `build-report.md`, `gate-*.md`, `pr-body.md`, `wi-comment.md`, `retro.md`).
  English stays for identifiers, file names, commit messages **and PR titles**.
- **Command-files are English, not French.** The plugin's own `commands/*.md` (prompt
  instructions to the orchestrator — e.g. `battle.md`, `retro.md`) are written in
  **English**. The "every battle artifact in French" rule above covers the *artifacts a
  run produces*, **not** the plugin's command-files. A builder that creates or edits a
  default-plugin command-file writes it in English. (RETEX: a command-file drafted in
  French was bounced by REVIEW — the convention read too broadly.)
- **Commit subjects and PR titles follow Conventional Commits** — English,
  `type(scope): subject` (imperative, no trailing period); `type` ∈
  `feat|fix|refactor|perf|docs|test|build|ci|chore`.
- No `Set-Location`/`cd`, no `&&`/`;`/`|` chaining, no shell redirection; run
  `dotnet` from the current directory.
- Hooks are launched via `python "$CLAUDE_PLUGIN_ROOT/hooks/<x>.py"`. If `python`
  is missing the hooks fail silently — the `/battle start` preflight checks it.
- Iterate in small validated steps; never emit large unvalidated blocks.
- Delegate, never duplicate — but stay self-contained for core paths.

## Charte de style des documents

**Single source** for the style of every document a battle produces for a human:
`spec.md`, `plan.md`, `build-report.md`, `gate-*.md`, `pr-feedback.md`, `retro.md`,
`pr-body.md`, `wi-comment.md`. Each producer **references** this charter — it never
copies the rules. The charter does **not** cover machine state (`battle.json`,
`usage.jsonl`, `fleet.d/*.json`, `active-battle`), commit subjects / PR titles
(Conventional Commits — see ## Conventions), or code identifiers.

The charter is written in English (it is a prompt instruction), but it prescribes
artifacts **in French** and a French label « En bref » — consistent with ## Conventions.

### Five rules — simple, precise language

1. **One idea per sentence.** Keep sentences short.
2. **Active voice, present tense.** Prefer "the gate blocks the push" over "the push
   may be blocked".
3. **The exact word.** No filler, no hedging ("just", "basically", "I think", "rather").
4. **Concrete and sourced.** Cite any reference as `file:line`; name the thing, never "it".
5. **Decisive information first.** Lead with the conclusion; details follow.

### « En bref » summary

A long document opens with an **« En bref »** section, so the reader decides fast
whether to read on. The label stays French (« En bref »), like every artifact.

- **Systematic** on `spec.md` and `plan.md` (approval documents): always present.
- **Conditional** on `gate-*.md`, `retro.md`, `build-report.md`, `pr-feedback.md`:
  add it once the document passes **~40 lines**.
- **None** on `pr-body.md` and `wi-comment.md`: they are short by design — apply the
  five rules, but add no separate « En bref ».

**Format.** 1-3 lines or a few bullets, right after the title, before any other
section. State what matters — the decision, the verdict, the outcome — not a table of
contents. Reuse the document's existing summary seed instead of duplicating content
(the spec's "Intention", the plan's approach + "Choix ouverts à arbitrer", a gate's
verdict block).

An « En bref » **never thins** a reference document: keep every slice, every
`file:line` signal, every matrix row. The summary is added on top, not as a replacement.

### Self-check before returning

Before handing back an artifact, reread it against this charter:
- Five rules applied (short active sentences, no filler, sourced, decisive-first)?
- « En bref » present where required (systematic, or conditional past ~40 lines)?
- Reference document still complete (no slice, signal or matrix row removed)?
