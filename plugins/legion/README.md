# legion — guide d'utilisation

Une stack d'agents Claude qui transforme une issue en livraison via un pipeline
discipliné, sur tes **projets .NET perso hébergés sur GitHub**. Inspirée de
[gstack](https://github.com/garrytan/gstack), allégée pour le travail solo/indie.

> En une phrase : tu pilotes un **battle** phase par phase ; à chaque étape un
> agent spécialisé revoit le travail et **bloque** tant que ce n'est pas bon.

---

## Prérequis

| Composant | Rôle | Requis ? |
|---|---|---|
| **Claude Code** | héberge le plugin | ✅ obligatoire |
| **Python 3** (`python`) | exécute les 4 hooks (`guard`, `careful`, `fleet_sync`, `usage_track`) | ✅ obligatoire |
| **.NET SDK** | `dotnet build` / `dotnet test` / `dotnet format` lancés par le builder et les gates test / lint | ✅ obligatoire |
| **`gh` (GitHub CLI)** | `/battle start <n>` (lecture d'issue) **et** `/battle deliver` (création de PR) | ⚙️ recommandé |
| **plugin `dotnet-claude-kit`** | fournit la **MCP Roslyn** dont dépend la gate `reviewer`, + les skills délégués (scaffold, code-review, testing, security-scan…) | ✅ fortement recommandé |

> Sans `dotnet-claude-kit`, la gate `reviewer` perd ses signaux Roslyn (diagnostics,
> antipatterns, blast radius) et retombe sur de la revue « à l'œil ». Les autres
> gates fonctionnent, mais la stack est conçue pour s'appuyer dessus.

> ⚠️ **Le `python` doit être trouvable.** Les hooks sont lancés par Claude Code via
> `python "…"` et **échouent en silence** s'il est absent — la battle tourne alors
> sans garde-fous, sans index *fleet*, sans suivi. Vérifie : `python --version`.
> (Si tu préfères le launcher `py`, remplace `python` par `py` dans `hooks/hooks.json`.)

## Installation

1. **Le plugin** — `/plugin marketplace add mickaelfrancois/legion` (ou un chemin
   local vers ce dépôt cloné), puis `/plugin install legion`. Installe aussi
   `dotnet-claude-kit` (sa MCP Roslyn doit être compilée — suis son README).
2. **Python** — `python --version`.
3. **GitHub CLI** — `gh auth login` (scope repo). Vérifie : `gh auth status`.
4. Ouvre Claude Code **dans le repo .NET** sur lequel tu veux travailler.

Les commandes du plugin sont **namespacées** : préfixe-les par `/legion:` (la forme
nue `/battle` renvoie *Unknown command*).

---

## Le pipeline en 8 étapes

```
THINK → PLAN → BUILD → LINT → REVIEW → TEST → DELIVER → (ADDRESS) → REFLECT
```

Chaque étape produit un fichier dans `.legion/battles/<id>/`, lu par la suivante.
Les étapes PLAN / LINT / REVIEW / TEST sont tenues par des **gates** : un agent qui
rend un verdict. **Si le verdict est `revise`, le pipeline s'arrête** — tu corriges,
puis tu relances. La gate **LINT** (formatage .NET, `dotnet format --verify-no-changes`)
est **.NET-only** : sur un repo non-.NET elle se retire sans bloquer. **ADDRESS** est
optionnelle et répétable : elle n'existe que si la PR reçoit des commentaires de revue
(voir l'antisèche, `/legion:battle address`).

### Run autonome (défaut)

Le seul rendez-vous garanti avec toi est l'**approbation du plan** (après PLAN). Sur
ton OK, l'orchestrateur enchaîne seul `build → lint → review → test → security →
deliver`, **pousse et ouvre la PR sans nouvelle confirmation** — tu relis le code sur
GitHub. Un build qui **compile** déclenche la cascade (les warnings sont **non
bloquants** : juste loggés dans `build-report.md` et relayés). Un `revise` ouvre une
**boucle d'auto-correction bornée** (2 essais par gate, 6 au global). L'orchestrateur
ne rend la main que sur **escalade** : `reject`, boucle non convergente, correction
hors périmètre du plan, filet de livraison (base en retard sur `origin`, remote vide…),
ou préflight défaillant (`python`/`gh` absent).

**Pas-à-pas (`--step`).** `/legion:battle start <issue> --step` fait rendre la main à
chaque transition de phase ; en `--step`, `deliver` attend une **confirmation explicite**
avant de pousser. `--step` (sur `start`) et `--auto` (sur `build`) sont **orthogonaux** :
le premier pilote la *cadence d'arrêt*, le second la *délégation du code* à l'agent
`builder`.

---

## Exemple complet

> En mode autonome (défaut), tu lances surtout `start` : après l'approbation du plan,
> les étapes 3 à 5 **s'enchaînent seules** jusqu'à la PR. Les commandes ci-dessous sont
> les points de contrôle — à lancer à la main seulement en `--step`, ou pour reprendre
> après un blocage.

```text
# 0. (optionnel) Cadrer une issue floue AVANT de démarrer
/legion:recon 42                   ← interrogatoire serré + exploration du repo → ajoute une section « Cadrage » à l'issue

# 1. Démarrer une battle
/legion:battle start 42            ← id numérique = issue GitHub : tire titre/body/labels
/legion:battle start refonte-tva   ← libellé libre : la spec est rédigée depuis la conversation
/legion:battle start 42 --step     ← ou : pas-à-pas, rend la main à chaque phase

# 2. (recommandé) Verrouiller le périmètre d'écriture
/legion:freeze src/Billing.Api/** tests/**

# 3. Coder
/legion:battle build               ← tu codes en direct avec Claude (recommandé)
/legion:battle build --auto        ← ou tu délègues à l'agent builder (autonome)

# 4. Gates de revue (enchaînées auto si le build compile)
/legion:battle review              ← lint → reviewer → test-engineer → security

# 5. Livrer (auto en mode autonome ; confirmé en --step)
/legion:battle deliver             ← branche <moi>/<n> → commit → push → PR (Closes #<n>)

# 5b. (optionnel) Traiter les retours de revue sur la PR — répétable
/legion:battle address             ← triage des commentaires → corrige → répond → résout

# 6. Clôturer
/legion:retro                      ← synthèse, 1 apprentissage durable, ferme la battle
```

À tout moment :

```text
/legion:battle status      ← où en est la battle courante ?
/legion:battle resume <id> ← reprendre une battle plus tard
/legion:fleet              ← vue de TOUTES tes battles, à travers TOUS tes repos
```

---

## Antisèche des commandes

| Commande | Ce qu'elle fait |
|---|---|
| `/legion:issues` | *(avant THINK)* Liste les issues GitHub ouvertes du repo courant (`#id  titre  [labels]`) ; rappelle `/legion:recon <n>` et `/legion:battle start <n>`. |
| `/legion:recon <issue>` | *(avant THINK, optionnel)* Affine une issue floue par un interrogatoire serré + exploration du repo, puis ajoute une section « Cadrage » à l'issue (confirmation avant écriture). |
| `/legion:battle start <issue\|slug>` | Démarre une battle. `<issue>` numérique = issue GitHub (tirée auto) ; sinon libellé libre. |
| `/legion:battle build [slice\|all] [--auto]` | Code une slice (toi en direct, ou l'agent builder). |
| `/legion:battle review` / `test` | Lance les gates lint → reviewer → test-engineer → security. |
| `/legion:battle deliver` | Branche `<moi>/<n>`, commit, push, PR avec `Closes #<n>` (auto en mode autonome ; confirmation avant push en `--step`). |
| `/legion:battle address` | *(post-deliver, répétable)* Traite les commentaires de revue de la PR : la gate `pr-triage` classe chaque fil, l'orchestrateur corrige (re-gate si besoin), répond et résout. |
| `/legion:battle resume <id>` | Reprend une battle existante (respecte son `run.mode`). |
| `/legion:battle status` | État des phases de la battle courante. |
| `/legion:retro [<id>]` | Rétrospective + apprentissage en mémoire + clôture. |
| `/legion:fleet [all\|prune]` | Vue consolidée des battles multi-repo. |
| `/legion:legatus` | Lance **Legatus**, l'UI web locale read-only de suivi (http://localhost:5021), depuis n'importe quel repo. |
| `/legion:freeze <globs>` / `off` | Restreint l'écriture aux globs donnés. |
| `/legion:guard` / `off` | Périmètre auto (déduit du plan) + fichiers sensibles protégés. |
| `/legion:careful` / `off` | Avertit (sans bloquer) sur les commandes destructrices. |

---

## Les garde-fous

- **`/freeze`** et **`/guard`** posent un *périmètre d'écriture*. Un hook bloque
  réellement toute tentative d'éditer un fichier hors périmètre — y compris l'agent
  builder. Le dossier `.legion/` reste toujours modifiable.
- **`/careful`** te prévient avant un `rm -rf`, `git reset --hard`, `dotnet ef
  database drop`… sans t'empêcher (le jugement reste humain).
- Bypass ponctuel d'un blocage : variable d'environnement `LEGION_GUARD_OFF=1`.

---

## Multi-repo : comment ça marche

Une session Claude = **un** repo. Tu peux mener plusieurs battles en parallèle en
ouvrant plusieurs repos/terminaux. Chaque repo garde son état dans son propre
`.legion/`. La commande **`/fleet`** agrège tout dans une vue unique : quelle battle
est en cours, laquelle est **bloquée** sur une gate et réclame ton attention.

`/fleet` ne pilote pas les battles à distance — il les rend **visibles**. Pour agir
sur l'une d'elles, ouvre son repo et fais `/legion:battle resume <id>`.

---

## Questions fréquentes

**`<issue>`, c'est quoi exactement ?**
Deux usages. Un **id numérique** (ex: `42`) = **issue GitHub** du repo courant : la
stack tire titre/body/labels via `gh issue view` pour pré-remplir la spec. Un
**libellé** (ex: `refonte-tva`) = juste un nom de battle, tu décris le besoin dans
la conversation. Le mode issue suppose `gh` installé et authentifié ; sinon la stack
bascule en intake inline en te prévenant.

**Je dois suivre toutes les étapes ?**
Non. Le *profil* de la battle (`feature` par défaut) décide quelles gates sont
obligatoires. Un `hotfix` peut sauter l'architecture ; une battle `security` force
la gate sécurité.

**Où est l'état d'une battle ?**
Dans `.legion/battles/<id>/` à la racine du repo. Lisible, **git-ignoré** (la trace
pérenne vit dans la PR + l'issue), et suffisant pour reprendre la battle dans une
autre session.

**La stack écrit du code à ma place ?**
Seulement en `/battle build --auto`. Par défaut, c'est toi (avec Claude) qui codes ;
les agents *revoient*, ils ne produisent pas.

**Jusqu'où ça tourne tout seul ?**
Deux choses à ne pas confondre. *Qui code* : toi par défaut (l'agent `builder`
seulement en `--auto`). *Qui enchaîne les phases* : l'orchestrateur, en autonome par
défaut. Après l'approbation du plan, il enchaîne build → gates → livraison et **ouvre
la PR sans nouvelle confirmation** ; il ne s'arrête que sur escalade (cf. « Run
autonome »). Tu veux valider chaque étape ? Démarre en `--step`.

---

Détails de conception : voir [`ARCHITECTURE.md`](ARCHITECTURE.md). Contrat de données
pour une UI de suivi : voir [`docs/ui-integration.md`](docs/ui-integration.md).
