# legion — Architecture & Doctrine

> Stack d'agents Claude inspirée de [gstack](https://github.com/garrytan/gstack),
> adaptée à un environnement **solo/indie : projets .NET perso hébergés sur GitHub**.
>
> Version allégée d'un plugin d'orchestration d'entreprise : on garde le squelette
> d'orchestration et les garde-fous, on retire ce qui ne sert qu'en équipe (campagnes
> US→tâches, boucle de revue PR multi-round, DAG NuGet multi-repo).

---

## 1. Principe directeur

`legion` est un **chef d'orchestre, pas une réimplémentation**.

Il n'apporte que la **colonne vertébrale de workflow**, les **agents-gates de
revue**, l'**orchestration multi-repo** (observabilité) et les **garde-fous
exécutables**. Tout le travail .NET concret (scaffold, tests, sécu) est **délégué**
à [`dotnet-claude-kit`](https://github.com/trossitec/dotnet-claude-kit).

Conséquence de design : si une capacité existe déjà comme skill, `legion`
l'**invoque** — il ne la duplique pas. Sa valeur propre est la *coordination*.

---

## 2. Vocabulaire

| Terme | Définition |
|---|---|
| **Battle** | Unité de travail bout-en-bout sur **une** feature/issue, dans **un** repo. Possède un identifiant et un dossier d'état. |
| **Phase** | Étape du pipeline (Think → Plan → Build → Review → Test → Deliver → Reflect). |
| **Gate** | Point de contrôle tenu par un **sous-agent** isolé qui rend un verdict. Une phase ne se ferme qu'après un verdict `accept`/`accept_with_opportunity` de sa gate. |
| **Artefact** | Fichier markdown produit par une phase et consommé par la suivante (`spec.md`, `plan.md`, `gate-*.md`…). C'est le mécanisme de hand-off **et** la mémoire de battle. |
| **Fleet** | Vue consolidée des battles actives **à travers les repos**. Équivalent du *Conductor* de gstack adapté au multi-repo. |
| **Guard** | Garde-fou exécutable (hook `PreToolUse`) limitant les écritures au périmètre déclaré de la battle. |

> Contrairement à la version d'entreprise, le terme *wire/stockage* est aligné sur
> le domaine : tout dit **battle** (`.legion/battles/<id>/`, `battle.json`,
> `active-battle`, `battle_status`). Aucune donnée historique à préserver en solo.

---

## 3. Le pipeline

```
  THINK     PLAN        BUILD        REVIEW      TEST       DELIVER    REFLECT
 ┌──────┐ ┌─────────┐ ┌──────────┐ ┌─────────┐ ┌────────┐ ┌────────┐ ┌───────┐
 │intake│→│architect│→│ builder  │→│ reviewer│→│  test- │→│ deliver│→│ retro │
 │      │ │  GATE   │ │ PRODUCER │ │  GATE   │ │engineer│ │  (PR)  │ │       │
 └──────┘ └─────────┘ └──────────┘ └─────────┘ │  GATE  │ └────────┘ └───────┘
  spec.md  plan.md     code +       gate-      └────────┘ pr-body.md  retro.md
                       build-report review.md  gate-test.md
                                 + security GATE → gate-security.md
```

Deux natures d'acteurs :
- **Producteur** (`builder`) : *écrit* du code à partir de `plan.md`, rend un
  compte-rendu (`build-report.md`) — pas de verdict, c'est lui le sujet de la revue.
- **Gates** : *jugent* un livrable et rendent un verdict. Le pipeline n'avance pas
  sur un `revise`.

Chaque flèche est un **hand-off d'artefact** : chaque acteur lit l'artefact amont
et produit le sien. Un `revise`/`reject` d'une gate de revue (reviewer/test/security)
reboucle vers **BUILD** — jamais vers l'architect (l'archi est verrouillée pendant
le build).

**Auto-enchaînement BUILD → gates** : un build `build_ok` **sans warning** enchaîne
directement la cascade `review → test → security`, jusqu'au premier `revise`/`reject`
ou jusqu'à `deliver` (exclu : la livraison reste manuelle/confirmée). Des warnings =
remarques → pas d'enchaînement, l'humain tranche.

---

## 4. Les gates sont des sous-agents

Chaque gate est un **sous-agent** (`Agent` tool, `subagent_type`) et **non une
commande**. Raison : un sous-agent démarre en **session vierge** (aucun biais du
contexte de production) avec son **propre budget de contexte** — il peut lire 30
fichiers pour challenger l'archi et ne remonter que son verdict.

> **Invariant « gates pures ».** Un sous-agent gate ne touche **jamais** le disque :
> il lit et **retourne** (verdict + contenu en clair). Seuls l'**orchestrateur**
> (`/battle`) et le **builder** écrivent l'état de la battle.

| Verdict | Sens | Effet sur le pipeline |
|---|---|---|
| `accept` | 0 FAIL, critères passés | Phase fermée, on avance. |
| `accept_with_opportunity` | 0 FAIL mais ≥1 amélioration repérée | On avance ; l'opportunité est tracée dans l'artefact. |
| `revise` | ≥1 FAIL | **Stop**. Correction requise (retour BUILD) avant re-soumission. |
| `reject` | Régression majeure / livrable inexploitable | **Stop**. Re-conception requise. |

### 4.1 Les quatre gates

| Sous-agent | Lecture seule ? | Modèle | Mandat |
|---|---|---|---|
| **architect** | Oui (Read/Grep/Glob) | opus | Scope justifié ? Archi conforme Clean Architecture ? Matrice de tests couvrante ? |
| **reviewer** | Oui + MCP Roslyn | sonnet | Correction, conformité au plan, performance, lisibilité, antipatterns, dead code. |
| **test-engineer** | Non (exécute les tests) | sonnet | Les tests existent, passent, et couvrent la matrice du `plan.md`. |
| **security** | Oui | opus | OWASP/secrets/NuGet vulnérables/auth — délègue à `security-scan`. |

> **Paliers de modèle.** Opus pour les gates à plus fort discernement et coût
> d'erreur (`architect`, `security`) ; sonnet pour `reviewer`/`test-engineer` où le
> jugement est borné par des signaux objectifs (Roslyn, `dotnet test`).

### 4.2 Le producteur `builder` (≠ gate)

Seul sous-agent **producteur** : il écrit du code (Read/Grep/Glob + Edit/Write/Bash),
**soumis à `guard.py`** (même périmètre que la session). Modèle **sonnet** (fixe) —
une slice trop complexe se **découpe** au PLAN. Mandat : coder **une** slice du
`plan.md`, build vert localement, rendre `build-report.md`. Il n'invoque aucune gate
(c'est l'orchestrateur qui séquence builder → gates).

Deux modes : *inline* (la session principale code, défaut) ; *autonome*
(`/battle build --auto` délègue chaque slice à un `builder`, parallélisable en
worktrees).

---

## 5. Modèle d'état

Deux niveaux : **par repo** (la battle) et **global** (le fleet).

### 5.1 Par repo — `.legion/battles/<battle-id>/`

```
.legion/
├── active-battle          # pointeur : id de la battle active (lu par les hooks)
└── battles/
    └── 2026-06-08-GH-1234/
        ├── battle.json        # métadonnées + profil + statut des phases
        ├── spec.md            # THINK
        ├── plan.md            # PLAN
        ├── build-report.md    # BUILD
        ├── gate-review.md     # REVIEW
        ├── gate-test.md       # TEST
        ├── gate-security.md   # (sécurité)
        ├── pr-body.md         # DELIVER (corps de PR)
        ├── wi-comment.md      # DELIVER (note postée sur l'issue)
        ├── usage.jsonl        # transverse (append-only) : tokens + skills réels
        └── retro.md           # REFLECT
```

`.legion/` est **par repo** et **git-ignoré** : mémoire de battle **locale** (la
trace pérenne vit dans la PR + l'issue). Une nouvelle session reprend la battle en
lisant `battle.json`, sans contexte conversationnel. Schéma : voir
[`docs/ui-integration.md §3.1`](docs/ui-integration.md).

### 5.2 Global — `~/.claude/legion/fleet.d/` (un shard par battle)

Index des battles **à travers les repos** (actives ET clôturées). Point d'entrée
unique pour tout consommateur (`/fleet`, une UI). **Un fichier par battle** :
`fleet.d/<sha1(repo_path::id)>.json`. Base surchargeable via `LEGION_FLEET`.

**Pourquoi des shards et pas un fichier unique ?** Plusieurs sessions Claude tournent
en parallèle (1 par repo). Un `fleet.json` partagé impliquait un read-modify-write →
*lost update*. Avec un shard par battle, chaque session n'écrit **que son fichier**
(atomique, temp + `os.replace`) → aucune perte. Les lecteurs agrègent tous les
`*.json`. Le coût/skills sont projetés depuis `usage.jsonl` par le hook
`fleet_sync` à chaque écriture de `battle.json`.

---

## 6. Garde-fous (le seul pilier qui exige du code)

Une consigne en prompt est contournable. Un garde-fou n'a de valeur que s'il
**bloque réellement** → hook `PreToolUse` (Python, `exit 2` = blocage avec message
stderr).

### 6.1 `guard.py` — périmètre d'écriture

```
PreToolUse(Edit|Write|MultiEdit) :
  1. Lire la battle active (.legion/active-battle → battle.json → guard.allow/deny).
  2. Le file_path visé est-il dans `allow` et hors `deny` ?
     - oui  → exit 0 (autorisé)
     - non  → exit 2 + message : "hors périmètre de la battle <id>. /freeze actif."
  3. `.legion/**` toujours autorisé. Bypass délibéré : env var LEGION_GUARD_OFF=1.
```

### 6.2 Commandes de pilotage

| Commande | Effet sur `battle.json.guard` |
|---|---|
| `/freeze <glob…>` | Restreint `allow` aux globs fournis. |
| `/guard` | Active un preset combiné (périmètre déduit du plan + deny fichiers sensibles). |
| `/careful` | Mode avertissement sur commandes destructrices (warn, pas block — `careful.py`). |

---

## 7. Orchestration multi-repo

**Contrainte fondamentale : 1 session Claude Code = 1 repo.** Le « parallèle » réel
se fait en lançant plusieurs sessions/terminaux. `legion` ne *crée* pas le
parallélisme — il le **rend observable et reprenable** :

1. Chaque session pilote **sa** battle dans **son** repo (état local `.legion/`).
2. À chaque transition de phase, le hook `fleet_sync` réécrit le **shard** de la
   battle dans `fleet.d/`.
3. `/fleet` agrège la vue de tous les repos → quelle battle est bloquée sur quelle
   gate.
4. `/battle resume <id>` reprend une battle depuis son `battle.json` (cross-session).

> **Anti-objectif :** ne pas piloter N repos depuis une seule session. Le plugin
> fournit la **continuité d'état** et la **vue consolidée**, pas un multiplexage.

> **Écart assumé vs version d'entreprise** : pas de résolution NuGet `-local` ni de
> DAG de tâches inter-repos. Une tâche qui touche plusieurs repos perso se mène en
> plusieurs battles séquentielles, l'humain ouvrant chaque repo — `/fleet` les rend
> visibles.

---

## 8. Inventaire des composants

```
plugins/legion/
├── .claude-plugin/plugin.json
├── ARCHITECTURE.md              # ce document
├── README.md                    # guide d'utilisation
├── docs/ui-integration.md       # contrat de données figé (pour une UI read-only)
├── commands/
│   ├── battle.md                # orchestrateur : start|build|review|test|deliver|resume|status
│   ├── retro.md                 # REFLECT
│   ├── fleet.md                 # vue multi-repo
│   ├── freeze.md / guard.md / careful.md
├── agents/
│   ├── architect.md             # gate PLAN
│   ├── builder.md               # PRODUCER BUILD (Edit/Write/Bash, worktree)
│   ├── reviewer.md              # gate REVIEW (+ MCP Roslyn)
│   ├── test-engineer.md         # gate TEST
│   └── security.md              # gate sécurité
├── hooks/
│   ├── hooks.json               # PreToolUse: guard,careful · PostToolUse: fleet_sync · Stop/SubagentStop: usage_track
│   ├── guard.py                 # périmètre d'écriture (exit 2 = block)
│   ├── careful.py               # avertit sur commandes destructrices (warn)
│   ├── fleet_sync.py            # écrit le shard fleet.d/<battle> à chaque écriture de battle.json
│   └── usage_track.py           # append tokens + skills réels à la battle active
└── skills/battle-workflow/SKILL.md   # la doctrine opérationnelle (résumé de ce doc)
```

> Aucun `scripts/` : la couche tickets/PR passe par le CLI **`gh`** appelé
> directement depuis `battle.md` (auth & JSON gérés par `gh`, zéro script réseau).

---

## 9. Conventions adoptées

- **Prose FR, identifiants/fichiers EN**. **Sujets de commit et titres de PR** :
  anglais, format **Conventional Commits** `type(scope): subject`.
- **Hooks Python** lancés via `python "$CLAUDE_PLUGIN_ROOT/hooks/<x>.py"`, `exit 2`
  pour bloquer, bypass par env var, `--self-test`. (Le launcher `py` marche aussi si
  installé — ajuster `hooks.json`.)
- **Agents** : frontmatter `name`/`description`/`model`/`tools` (whitelist)/
  `permissionMode`. Lecture seule stricte pour les gates de revue.
- **Verdict en cascade** `accept | accept_with_opportunity | revise | reject`.
- **Shell** : pas de `cd`, pas de chaînage `&&`/`;`/`|`, pas de redirection.
- **Pas de duplication** : toute capacité existante est invoquée via `Skill`.

---

## 10. Couche GitHub (`gh`)

La source de tickets et la couche PR passent par le CLI GitHub :

| Étape | Appel `gh` |
|---|---|
| THINK (intake) | `gh issue view <n> --json title,body,labels` |
| THINK (marquer démarré) | `gh issue edit <n> --add-assignee @me` |
| DELIVER (PR) | `gh pr create --title <conv> --body-file pr-body.md` (corps avec `Closes #<n>`) |
| DELIVER (note issue) | `gh issue comment <n> --body-file wi-comment.md` |

La fermeture de l'issue est **automatique au merge** via `Closes #<n>` dans le corps
de PR — pas de transition d'état explicite à gérer. Dégradation gracieuse : si `gh`
est absent/non authentifié, l'intake bascule inline et le `deliver` rend la main avec
les commandes à exécuter manuellement.
