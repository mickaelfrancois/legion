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
| **.NET SDK** | `dotnet build` / `dotnet test` lancés par le builder et la gate test | ✅ obligatoire |
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

## Le pipeline en 7 étapes

```
THINK → PLAN → BUILD → REVIEW → TEST → DELIVER → REFLECT
```

Chaque étape produit un fichier dans `.legion/battles/<id>/`, lu par la suivante.
Les étapes PLAN / REVIEW / TEST sont tenues par des **gates** : un agent qui rend
un verdict. **Si le verdict est `revise`, le pipeline s'arrête** — tu corriges,
puis tu relances.

**Enchaînement BUILD → gates** : un build **propre** (0 erreur, 0 warning) enchaîne
automatiquement `review → test → security` jusqu'au premier `revise` ou jusqu'à
`deliver` (exclu — la livraison reste une action manuelle confirmée).

---

## Exemple complet

```text
# 1. Démarrer une battle
/legion:battle start 42            ← id numérique = issue GitHub : tire titre/body/labels
/legion:battle start refonte-tva   ← libellé libre : la spec est rédigée depuis la conversation

# 2. (recommandé) Verrouiller le périmètre d'écriture
/legion:freeze src/Billing.Api/** tests/**

# 3. Coder
/legion:battle build               ← tu codes en direct avec Claude (recommandé)
/legion:battle build --auto        ← ou tu délègues à l'agent builder (autonome)

# 4. Gates de revue (enchaînées auto si le build est propre)
/legion:battle review              ← reviewer → test-engineer → security

# 5. Livrer
/legion:battle deliver             ← branche <moi>/<n> → commit → push → PR (Closes #<n>)

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
| `/legion:battle start <issue\|slug>` | Démarre une battle. `<issue>` numérique = issue GitHub (tirée auto) ; sinon libellé libre. |
| `/legion:battle build [slice\|all] [--auto]` | Code une slice (toi en direct, ou l'agent builder). |
| `/legion:battle review` / `test` | Lance les gates reviewer → test-engineer → security. |
| `/legion:battle deliver` | Branche `<moi>/<n>`, commit, push, PR avec `Closes #<n>` (confirmation avant push). |
| `/legion:battle resume <id>` | Reprend une battle existante. |
| `/legion:battle status` | État des phases de la battle courante. |
| `/legion:retro [<id>]` | Rétrospective + apprentissage en mémoire + clôture. |
| `/legion:fleet [all\|prune]` | Vue consolidée des battles multi-repo. |
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

---

Détails de conception : voir [`ARCHITECTURE.md`](ARCHITECTURE.md). Contrat de données
pour une UI de suivi : voir [`docs/ui-integration.md`](docs/ui-integration.md).
