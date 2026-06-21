# legion — contrat d'intégration pour une UI de suivi

Doc destinée à l'agent/dev qui construit une **UI locale read-only** affichant
l'avancement des battles legion (p.ex. l'adaptation solo de Legatus, à venir dans
`ui/legatus/`). Elle décrit *où* lire les données et *quel* schéma attendre. Elle
est **agnostique de la techno** de l'UI.

> Règle d'or : l'UI est **strictement read-only**. Le seul écrivain de l'état d'une
> battle est l'orchestrateur (`/battle`) et son hook. L'UI ne doit **jamais** écrire
> dans `fleet.d/` ni dans un `.legion/` — une écriture concurrente avec le hook
> pourrait abîmer un shard ou l'état d'une battle.

> **Ce contrat est figé.** Les noms (`battle.json`, `.legion/battles/`,
> `active-battle`, `battle_status`) sont stables : l'UI peut s'appuyer dessus.

---

## 1. Vue d'ensemble du flux de données

```
┌─ repo A ─────────────┐   ┌─ repo B ─────────────┐      ~/.claude/legion/
│ .legion/             │   │ .legion/             │      ┌────────────────────────┐
│   active-battle      │   │   active-battle      │ ───► │ fleet.d/  (INDEX)       │ ◄─ l'UI lit ICI
│   battles/<id>/      │   │   battles/<id>/      │      │   <sha1>.json (1/battle)│
│     battle.json  ◄───┼───┼──── source de vérité │      └────────────────────────┘
│     *.md             │   │     *.md             │          │ chaque shard pointe vers
└──────────────────────┘   └──────────────────────┘          ▼ repo_path + id
                                                       l'UI ouvre les .md en place
```

1. **Point d'entrée unique** : le dossier `fleet.d/` (hors repo) contient **un
   fichier JSON par battle** (« shard ») couvrant **tous** les repos. L'UI lit tous
   les shards et les agrège. Elle commence toujours par là.
2. Chaque entrée porte `repo_path` + `id` → l'UI localise les artefacts à
   `<repo_path>/.legion/battles/<id>/` et lit les `.md` **sur place** (ils restent
   dans leur repo, **git-ignorés/locaux** ; l'index ne les duplique pas).

---

## 2. L'index global — dossier `fleet.d/` (un shard par battle)

**Chemin** : `~/.claude/legion/fleet.d/` (soit `%USERPROFILE%\.claude\legion\fleet.d\`
sous Windows). La variable d'environnement `LEGION_FLEET`, si définie, donne
l'emplacement de base (son dossier parent) — l'UI doit la respecter et en dériver
`…/fleet.d/`.

**Structure** : un fichier `<sha1>.json` **par battle** (le nom est opaque ; ne pas
le parser — l'identité est dans le contenu). Pour obtenir la liste des battles, l'UI
**lit tous les `*.json` du dossier** et les agrège (dédup par `repo_path::id`).
Chaque fichier contient **une** entrée, pas un tableau :

```jsonc
// fleet.d/1a75ef9c2b60c829.json — une entrée = une battle
{
  "id": "2026-06-08-GH-1234",              // id de la battle (date + ticket)
  "repo": "billing-api",                    // nom court du repo
  "repo_path": "C:/src/billing-api",        // racine du repo (cf. §5 séparateurs)
  "ticket": "GH#1234",                      // "GH#<n>" ou slug libre ; peut être null
  "title": "Ajout endpoint facturation",    // titre lisible ; peut être null
  "profile": "feature",                     // feature | hotfix | security | spike
  "phase": "build",                         // phase COURANTE (cf. §4)
  "status": "in_progress",                  // statut de la phase courante
  "battle_status": "active",                // GLOBAL : active | blocked | closed
  "pr_url": null,                            // URL de la PR une fois deliver fait
  "tokens_total": 184320,                    // coût approx. = Σ(input+output) ; absent si rien encore
  "tokens": { "input": 150000, "output": 34320, "cache_read": 0, "cache_creation": 0 },
  "skills": ["scaffold", "code-review", "build-fix"],  // skills RÉELLEMENT utilisés (main + subagents)
  "updated": "2026-06-08T10:00:00+00:00"    // ISO-8601 UTC du dernier upsert
}
```

> **Coût & skills (`tokens_total`, `tokens`, `skills`)** : projection de l'artefact
> `usage.jsonl` de la battle (cf. §3). C'est un **snapshot approximatif** rafraîchi à
> chaque écriture de `battle.json` (transition de phase) — pas en continu. Pour un
> affichage *live*, l'UI peut lire directement `<repo_path>/.legion/battles/<id>/
> usage.jsonl` (append-only : une ligne par contribution `{scope, agent_type?,
> skills[], tokens{…}}`) et agréger elle-même. `tokens_total` = `input + output`.

> **Pourquoi un dossier de shards et pas un fichier unique ?** Plusieurs sessions
> Claude tournent en parallèle (une par repo). Un fichier partagé subirait des
> *lost updates*. Avec un shard par battle, chaque session n'écrit que **son**
> fichier → aucune perte. L'UI ne fait que **lire** : elle scanne le dossier sans
> risque, à tout moment.

**Sémantique des champs de statut** (les deux coexistent, ne pas confondre) :

| Champ | Portée | Valeurs | Usage UI |
|---|---|---|---|
| `phase` + `status` | la **phase courante** | phase ∈ §4 ; status ∈ `pending`/`in_progress`/`done`/`blocked` | « où en est la battle » (ex. *build — in_progress*) |
| `battle_status` | la **battle entière** | `active` / `blocked` / `closed` | filtre liste : en cours / à traiter / terminé |

- `battle_status == "blocked"` ⇒ une gate a renvoyé `revise`/`reject` : **réclame
  l'attention de l'humain** (à mettre en avant dans l'UI).
- `battle_status == "closed"` ⇒ rétro faite. L'index **garde** les battles clôturées
  (historique) ; elles ne disparaissent pas.

**Lecture défensive obligatoire** : `title`, `profile`, `battle_status`, `pr_url`
peuvent être `null` (ou absents) sur une entrée écrite avant l'enrichissement du
schéma. Traiter l'absence sans planter ; ré-hydratation au prochain upsert.

**Tri / filtres recommandés** : trier par `updated` décroissant ; vue par défaut =
`battle_status != "closed"` ; remonter les `blocked` en tête.

---

## 3. Les artefacts d'une battle — `<repo_path>/.legion/battles/<id>/`

À partir de `repo_path` + `id`, l'UI ouvre ce dossier. Fichiers (tous en Markdown
sauf `battle.json`) :

| Fichier | Phase | Contenu |
|---|---|---|
| `battle.json` | — | métadonnées + statut par phase (cf. §3.1) — **source de vérité** |
| `spec.md` | THINK | intention, périmètre in/out, critères d'acceptation |
| `plan.md` | PLAN | décision d'archi, slices, matrice de tests |
| `build-report.md` | BUILD | fichiers touchés, ce qui a été fait, tests, **compte de warnings** |
| `gate-lint.md` | LINT | verdict lint (formatage .NET, `dotnet format` verify-only ; .NET-only) |
| `gate-review.md` | REVIEW | verdict reviewer + détail (qualité, antipatterns) |
| `gate-test.md` | TEST | verdict test-engineer (tests verts + couverture) |
| `gate-security.md` | (sécurité) | verdict security (si gate requise) |
| `pr-body.md` | DELIVER | corps de la PR (composé des artefacts) |
| `wi-comment.md` | DELIVER | note de revue postée sur l'issue |
| `usage.jsonl` | (transverse) | append-only : coût tokens + skills réellement utilisés (1 ligne/contribution) |
| `retro.md` | REFLECT | rétrospective + apprentissage |

Un artefact **absent** = phase non encore atteinte (normal). Ne pas le considérer
comme une erreur.

Le pointeur `<repo_path>/.legion/active-battle` contient l'`id` de la battle active
du repo (utile si l'UI veut signaler « la battle en cours dans ce repo »).

### 3.1 Schéma de `battle.json`

```jsonc
{
  "id": "2026-06-08-GH-1234",
  "repo": "billing-api",
  "ticket": "GH#1234",
  "title": "Ajout endpoint facturation",
  "profile": "feature",
  "required_gates": ["architect", "lint", "reviewer", "test-engineer"],
  "phases": {
    "think":   { "status": "done", "artifact": "spec.md" },
    "plan":    { "status": "done", "artifact": "plan.md", "verdict": "accept" },
    "build":   { "status": "in_progress" },
    "lint":    { "status": "pending" },
    "review":  { "status": "pending" },
    "test":    { "status": "pending" },
    "deliver": { "status": "pending" },
    "reflect": { "status": "pending" }
  },
  "guard": { "allow": ["src/Billing.Api/**", "tests/**"], "deny": [], "careful": false },
  "delivery": { "pr_url": null }
}
```

Le shard `fleet.d/*.json` est une **projection** de ce fichier ; pour le détail par
phase (verdicts, artefacts), l'UI lit `phases` ici. La source de vérité est toujours
`battle.json`, pas l'index.

---

## 4. Phases et verdicts

Pipeline : `THINK → PLAN → BUILD → LINT → REVIEW → TEST → DELIVER → REFLECT`.

- **Clés de phase** (dans `phases` et le champ `phase` de l'index) : `think`,
  `plan`, `build`, `lint`, `review`, `test`, `deliver`, `reflect`. La clé `lint`
  est .NET-only (gate de formatage) ; sur une stack non-.NET elle reste `pending`
  ou `done` (retrait neutre) — lecture défensive habituelle.
- **Statut de phase** : `pending` | `in_progress` | `done` | `blocked`.
- **Verdict de gate** (champ `verdict` des phases tenues par une gate :
  plan/review/test/sécurité) : `accept` | `accept_with_opportunity` | `revise` |
  `reject`. Un `revise`/`reject` ⇒ phase `blocked` ⇒ battle `blocked`.

Affichage suggéré : une frise des 8 phases avec leur statut ; badge « bloqué » +
extrait du verdict (lu dans le `gate-*.md`) quand `battle_status == "blocked"`.

---

## 5. Pièges et conventions

- **Séparateurs de chemin** : `repo_path` est stocké tel que produit par l'hôte
  (Windows ⇒ antislashs `C:\src\...`). Normaliser avant de composer le chemin
  des artefacts ; ne pas supposer `/`.
- **Fraîcheur** : le shard d'une battle est réécrit par un hook `PostToolUse` à
  **chaque écriture de son `battle.json`** par l'orchestrateur — donc à chaque
  transition de phase, pas en temps réel. Pour du quasi-temps-réel, l'UI peut
  *watcher* le dossier `fleet.d/` et/ou les `<repo_path>/.legion/battles/`.
- **Shard orphelin** : si une battle est supprimée/déplacée, son shard subsiste
  jusqu'à `/fleet prune`. L'UI doit tolérer un `repo_path` introuvable (ex. badge
  « repo introuvable »), pas planter.
- **Encodage** : tous les fichiers sont en UTF-8.

---

## 6. Démarche minimale d'une v1

1. Lire tous les shards `fleet.d/*.json` et les agréger (base via `LEGION_FLEET` si
   défini). Dossier absent/vide → « aucune battle suivie ».
2. Liste filtrable : une ligne par battle (`repo`, `title`/`ticket`, frise de phase
   via `phase`/`status`, badge `battle_status`, lien `pr_url`, **coût `tokens_total`
   et `skills`** depuis le shard).
3. Au clic : ouvrir `<repo_path>/.legion/battles/<id>/`, rendre les `.md` présents
   (spec → plan → build-report → gates → pr-body → wi-comment → retro), le détail
   `phases` de `battle.json`, et le détail d'usage (`usage.jsonl` : tokens + skills
   par contribution, main vs subagents).
4. Optionnel : watcher pour rafraîchir.
