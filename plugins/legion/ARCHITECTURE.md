# legion — Architecture & Doctrine

> Stack d'agents Claude inspirée de [gstack](https://github.com/garrytan/gstack),
> adaptée à un environnement **solo/indie : projets .NET perso hébergés sur GitHub**.
>
> Version allégée d'un plugin d'orchestration d'entreprise : on garde le squelette
> d'orchestration et les garde-fous, on retire ce qui ne sert qu'en équipe (campagnes
> US→tâches, DAG NuGet multi-repo). On garde en revanche une **boucle de revue PR**
> mono-repo, légère, branchée sur `gh` (phase ADDRESS, §11).

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
| **Phase** | Étape du pipeline (Think → Plan → Build → Lint → Review → Test → Deliver → [Address] → Reflect ; *Lint* est .NET-only et se retire sur stack non-.NET, *Address* optionnelle/répétable post-deliver). |
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
  THINK     PLAN        BUILD        LINT      REVIEW      TEST       DELIVER    REFLECT
 ┌──────┐ ┌─────────┐ ┌──────────┐ ┌──────┐ ┌─────────┐ ┌────────┐ ┌────────┐ ┌───────┐
 │intake│→│architect│→│ builder  │→│ lint │→│ reviewer│→│  test- │→│ deliver│→│ retro │
 │      │ │  GATE   │ │ PRODUCER │ │ GATE │ │  GATE   │ │engineer│ │  (PR)  │ │       │
 └──────┘ └─────────┘ └──────────┘ └──────┘ └─────────┘ │  GATE  │ └────────┘ └───────┘
  spec.md  plan.md     code +       gate-     gate-      └────────┘ pr-body.md  retro.md
                       build-report lint.md   review.md  gate-test.md
                                 + security GATE → gate-security.md
```

> La gate **LINT** (formatage .NET, verify-only) est en tête de la cascade de revue ;
> elle est **.NET-only** et se retire (verdict neutre) sur une stack non-.NET.

Deux natures d'acteurs :
- **Producteur** (`builder`) : *écrit* du code à partir de `plan.md`, rend un
  compte-rendu (`build-report.md`) — pas de verdict, c'est lui le sujet de la revue.
- **Gates** : *jugent* un livrable et rendent un verdict. Le pipeline n'avance pas
  sur un `revise`.

Chaque flèche est un **hand-off d'artefact** : chaque acteur lit l'artefact amont
et produit le sien. Un `revise`/`reject` d'une gate de revue (reviewer/test/security)
reboucle vers **BUILD** — jamais vers l'architect (l'archi est verrouillée pendant
le build).

**Run autonome (défaut).** Après l'approbation du plan (unique point d'arbitrage), le
pipeline enchaîne automatiquement BUILD → gates → DELIVER → ouverture de la PR, sans
rendre la main — sauf escalade (taxonomie §12). En mode `step` (`--step` sur `start`),
chaque transition de phase rend la main (comportement pas-à-pas).

**Warnings non bloquants.** Un build `build_ok` — avec ou sans warnings — enchaîne
directement la cascade `lint → review → test → security`. Les warnings sont loggés dans
`build-report.md` et relayés à l'utilisateur, puis la cascade continue sans
interruption. Seul `build_ok == false` bloque (→ boucle d'auto-correction, §12).
Quand toutes les gates requises sont `done`, la cascade enchaîne automatiquement vers
DELIVER (en mode `autonomous`).

**DELIVER sans OK bloquant (mode `autonomous`).** Quand toutes les gates sont `done`
et le mode est `autonomous`, l'orchestrateur compose `pr-body.md`, pousse et ouvre la
PR **sans attendre d'OK explicite** — l'humain relit le code sur GitHub. Les **filets
§G.0** (`base en retard sur origin`, remote vide, fichier hors whitelist, `.gitignore`
auto-induit) sont la **dernière barrière** : chacun, s'il se déclenche, **escalade**
(cas 4 de la taxonomie — §12). La discipline de staging (whitelist de chemins, `git
reset -q HEAD .legion`) reste stricte et non affaiblie, quel que soit le mode. En mode
`step`, le comportement historique est maintenu : afficher l'effet sortant et attendre
un OK explicite.

**L'autonomie s'arrête à l'ouverture de la PR.** La phase ADDRESS (commentaires humains
post-livraison) garde sa confirmation humaine — elle est hors scope de cet enchaînement
autonome.

**Boucle ADDRESS** (optionnelle, post-deliver, §11) : une fois la PR ouverte, les
commentaires de revue humaine sont traités par `/battle address` — la gate
`pr-triage` classe chaque fil, l'orchestrateur applique les corrections (invalidation puis
cascade depuis `lint` selon le rayon d'impact ; `lint` seul pour `code-trivial`) puis répond/résout. Pas de commentaire → la
phase n'existe pas.

---

## 4. Les gates sont des sous-agents

Chaque gate est un **sous-agent** (`Agent` tool, `subagent_type`) et **non une
commande**. Raison : un sous-agent démarre en **session vierge** (aucun biais du
contexte de production) avec son **propre budget de contexte** — il peut lire 30
fichiers pour challenger l'archi et ne remonter que son verdict.

> **Invariant « gate à écriture confinée ».** Un sous-agent gate est **lecture seule
> sur le code** et n'écrit **qu'un seul fichier** : son propre artefact
> (`plan.md` / `gate-lint.md` / `gate-review.md` / `gate-test.md` /
> `gate-security.md` / `pr-feedback.md`) dans le dossier de la battle. Le hook
> `guard.py` l'y **confine**
> via `agent_type` (toute autre écriture — code, `battle.json`, artefact d'une autre
> gate → `exit 2`) : la garantie « une gate ne touche pas le code » est ainsi
> **structurelle** (portée par le hook), non plus seulement déclarative.
> **L'invariant immuable est « une gate ne touche pas le _code_ » (juge ≠ partie) —
> *pas* « une gate n'écrit rien ».** Qu'une gate écrive son propre artefact ne le viole
> pas : ce canal sert la **discipline de contexte** (sortir le contenu des artefacts du
> transcript orchestrateur). Le *pourquoi* complet :
> [`docs/gate-write-confinement.md`](docs/gate-write-confinement.md) §1. La gate
> **retourne** alors son verdict + le **chemin** de l'artefact — *jamais* le contenu
> en clair : c'est ce qui garde le travail des gates **hors du contexte** de la
> session orchestratrice (le levier de discipline de contexte). L'**orchestrateur**
> (`/battle`) écrit le reste de l'état (`spec.md`, artefacts de PR ; `battle.json`
> uniquement **via** `scripts/battle_state.py`, cf. § 5.1) et
> **lit** les artefacts de gate sur disque au besoin ; le **builder** écrit le code +
> `build-report.md`. Cas particulier : `pr-triage` écrit `pr-feedback.md` **et**
> retourne en plus le bloc TRIAGE JSON (machine-lisible) sur lequel l'orchestrateur
> route.
>
> **Contrepartie : vérif de livraison.** Comme le verdict ne *prouve* plus que
> l'artefact existe, l'orchestrateur applique un **check de livraison** déterministe
> autour de chaque gate (métadonnées seules — il ne lit pas le contenu) : l'artefact
> attendu doit **exister**, être **non vide**, au **chemin canonique**, et avoir été
> **écrit à ce passage** (mtime postérieur — pas un résidu d'un round précédent). À défaut, il ne persiste pas
> le verdict, re-invoque la gate une fois, puis bloque la phase. Détail : `battle.md` §E.

| Verdict | Sens | Effet sur le pipeline |
|---|---|---|
| `accept` | 0 FAIL, critères passés | Phase fermée, on avance. |
| `accept_with_opportunity` | 0 FAIL mais ≥1 amélioration repérée | On avance ; l'opportunité est tracée dans l'artefact. |
| `revise` | ≥1 FAIL | **N'avance pas**. Gate de revue (lint/reviewer/test/security) : correction (retour BUILD) puis re-soumission — automatique en `autonomous` (boucle bornée, §12), rend la main en `step`. Gate PLAN (`architect`) : rend **toujours** la main pour ajuster la spec. |
| `reject` | Régression majeure / livrable inexploitable | **Stop — escalade immédiate** (cas 1, §12), quel que soit le mode. Re-conception requise. |

> **Deux canaux distincts d'une gate.** Le verdict ci-dessus juge le **diff de la slice**.
> En parallèle, une gate (et le `builder`) peut consigner dans son artefact une section
> `## Hors périmètre — candidats issue` : des observations **hors du diff** (dette
> pré-existante, code adjacent, amélioration) qui **ne pèsent jamais sur le verdict**. Le
> REFLECT les agrège, les dédoublonne (`scripts/opportunity.py`) et les matérialise en
> **issues GitHub** sur le repo cible — c'est la **3ᵉ sortie** du REFLECT, à côté de
> l'apprentissage code (mémoire projet) et du RETEX outillage (journal central, §8).

### 4.1 Les cinq gates

| Sous-agent | Lecture seule (sur le code) ? | Modèle | Mandat |
|---|---|---|---|
| **architect** | Oui (Read/Grep/Glob) | opus | Scope justifié ? Archi conforme Clean Architecture ? Matrice de tests couvrante ? |
| **lint** | Oui (exécute `dotnet format --verify-no-changes`, non-mutant) | sonnet | Formatage .NET conforme — première gate de la cascade. **.NET-only** : se retire (verdict neutre) sur stack non-.NET. |
| **reviewer** | Oui + MCP Roslyn | sonnet | Correction, conformité au plan, performance, lisibilité, antipatterns, dead code. |
| **test-engineer** | Oui sur le code (exécute les tests) | sonnet | Les tests existent, passent, et couvrent la matrice du `plan.md`. |
| **security** | Oui | opus | OWASP/secrets/NuGet vulnérables/auth — délègue à `security-scan`. |

> Chacune **écrit son seul artefact** (`plan.md` / `gate-*.md`), confinée par
> `guard.py` ; aucune ne touche le code (cf. invariant § 4). `Write` figure donc dans
> leur whitelist d'outils, mais le hook borne cette écriture à l'unique artefact.

> **Paliers de modèle.** Opus pour les gates à plus fort discernement et coût
> d'erreur (`architect`, `security`) ; sonnet pour `lint`/`reviewer`/`test-engineer`
> où le jugement est borné par des signaux objectifs (`dotnet format`, Roslyn,
> `dotnet test`).

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
        ├── battle.json        # métadonnées + profil + statut des phases (écrit par battle_state.py seul)
        ├── spec.md            # THINK
        ├── plan.md            # PLAN
        ├── build-report.md    # BUILD
        ├── gate-lint.md       # LINT (.NET-only)
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

**Écrivain unique.** `battle.json` et le pointeur `active-battle` ne sont écrits que par
`scripts/battle_state.py` (sous-commandes `init`, `transition`, `approve-plan`,
`set-slices`, `slice`, `next-slice`, `check-cascade`, `bump-autocorrect`, `invalidate`, `set-delivery`,
`set-guard`, `set-meta`, `activate`, `close`, `abort`, `validate`). `build done` exige que toutes les
slices déclarées (`set-slices`) soient `done` ; `set-slices --replace` remplace la liste
pendant un re-plan ouvert (`approved_at` à `null`) ou tant que `build` est `pending`
(déclarer les slices **avant** `approve-plan`) ; sans id, elle vide la liste (BUILD agrégé,
plan sans ligne `[slice-…]`). Un re-plan (`transition plan in_progress`) invalide la
cascade déjà rendue (raison `replan`, un seul événement) ; si `build` était `done` et
qu'une slice résultante ne l'est pas, `build` passe à `blocked`. `check-cascade` (lecture seule) dit si les
phases requises sont `done` avant un push ADDRESS ; `address done` porte la même
précondition. Le script vérifie chaque transition de phase (ex. `build` — y compris `blocked` — refusé
tant que le plan n'est pas `accept*` **et** approuvé : `phases.plan.approved_at`), écrit de façon
atomique (temp + `os.replace`) et resynchronise le shard fleet. `abort [--reason]` marque une
battle comme abandonnée (`aborted = {at, reason}`) : refusé si la battle est close ou déjà
abandonnée ; ensuite seul `validate` passe et le pointeur `active-battle` est vidé si
elle était active. Il porte aussi la **source
unique** des tables `PHASES` / `GATE_PHASE` / `GATE_ARTIFACT` / `PRODUCER_ARTIFACT`, dont
dérivent `guard.py`, `fleet_sync.py` et `eval.py`. Les command-files l'appellent ; ils
n'éditent plus `battle.json` à la main.

### 5.2 Global — `~/.claude/legion/fleet.d/` (un shard par battle)

Index des battles **à travers les repos** (actives ET clôturées). Point d'entrée
unique pour tout consommateur (`/fleet`, une UI). **Un fichier par battle** :
`fleet.d/<sha1(repo_path::id)>.json`. Base surchargeable via `LEGION_FLEET`.

**Pourquoi des shards et pas un fichier unique ?** Plusieurs sessions Claude tournent
en parallèle (1 par repo). Un `fleet.json` partagé impliquait un read-modify-write →
*lost update*. Avec un shard par battle, chaque session n'écrit **que son fichier**
(atomique, temp + `os.replace`) → aucune perte. Les lecteurs agrègent tous les
`*.json`. Le coût/skills sont projetés depuis `usage.jsonl` par le hook
`fleet_sync` à chaque écriture de `battle.json` (les compteurs `slices_done` /
`slices_total` y sont aussi projetés, absents si la battle ne déclare pas de slices) : `battle_state.py` appelle la synchro
lui-même après chaque écriture, et le hook `PostToolUse` reste le filet pour les autres
écritures.

---

## 6. Garde-fous (le seul pilier qui exige du code)

Une consigne en prompt est contournable. Un garde-fou n'a de valeur que s'il
**bloque réellement** → hook `PreToolUse` (Python, `exit 2` = blocage avec message
stderr).

### 6.1 `guard.py` — périmètre d'écriture

```
PreToolUse(Edit|Write|MultiEdit) :
  0. CONFINEMENT DES GATES (prioritaire, actif même guard non armé).
     Si `agent_type` ∈ GATE_ARTIFACT (table dérivée de `battle_state.GATE_ARTIFACT`,
     avec repli fail-closed si le script est introuvable ; legion:architect → plan.md,
     legion:lint → gate-lint.md, legion:reviewer → gate-review.md,
     legion:test-engineer → gate-test.md, legion:security → gate-security.md,
     legion:pr-triage → pr-feedback.md) :
       - file_path == .legion/battles/<active>/<artefact de la gate> :
           · `Write` à contenu blanc (artefact 0 octet) → exit 2 (RETEX A1)
           · sinon → exit 0
       - sinon (autre fichier, code, battle.json, hors battle)        → exit 2
     La session principale (agent_type "claude") et le builder (legion:builder)
     ne sont pas dans la table → règles de périmètre standard ci-dessous.
  0b. BUILDER SOUS .legion/ (actif même guard non armé). Si `agent_type` ==
     legion:builder et file_path ∈ .legion/** : seul
     .legion/battles/<active>/build-report.md → exit 0 ; tout autre chemin
     (battle.json, active-battle, artefact de gate) → exit 2. Empêche le builder
     d'élargir son propre `guard.allow`. Comparaison insensible à la casse, et tout
     `.legion/` visé hors de la racine du hook (checkout principal vu d'un worktree)
     est aussi bloqué. Limite : l'outil `Bash` n'est pas couvert (#66).
  1. Lire la battle active (.legion/active-battle → battle.json → guard.allow/deny).
  2. Le file_path visé est-il dans `allow` et hors `deny` ?
     - oui  → exit 0 (autorisé)
     - non  → exit 2 + message : "hors périmètre de la battle <id>. /freeze actif."
  2b. Bloc `guard` invalide (non-dict, `allow`/`deny` non-listes ou contenant un
     non-`str`, règle unique `battle_state.guard_of`) → périmètre inconnu, fail-closed :
     exit 2 hors `.legion/**`, `.gitignore` et mémoire Claude, quel que soit l'appelant
     (évalué après 0 et 0b). Réparer via `battle_state.py set-guard`. `guard: null` = bloc
     absent (non armé). Même branche si le pointeur désigne une battle dont `battle.json`
     existe mais est illisible (JSON invalide ou trop imbriqué, racine non-dict) : `set-guard`
     et `close` refusent aussi ce fichier, donc réparer le fichier (`git checkout` ou à la main,
     `.legion/**` reste modifiable) ou vider `.legion/active-battle`. Toute exception imprévue
     de la décision → exit 2 (jamais exit 1). Entrée stdin : vide ou blanche → exit 0 ; illisible
     ou non-objet JSON → exit 2 ; jamais exit 1 (hors `--self-test`).
  3. `.legion/**` toujours autorisé (sauf builder, cf. 0b). Bypass délibéré : env var LEGION_GUARD_OFF=1.
```

> **Pourquoi `agent_type`.** Le payload `PreToolUse` porte `agent_type` (nom
> namespacé du sous-agent appelant, ex. `legion:reviewer` ; la session principale
> vaut `"claude"`). C'est ce signal qui permet de confiner une gate à son seul
> artefact **sans** lui interdire d'écrire (elle a besoin de `Write`), et donc de
> sortir le contenu des artefacts du contexte orchestrateur (cf. invariant § 4).

### 6.2 Commandes de pilotage

Les trois commandes écrivent `battle.json.guard` **via** `battle_state.py set-guard`
(jamais à la main).

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
2. À chaque transition de phase, `battle_state.py` (puis le hook `fleet_sync`, en filet)
   réécrit le **shard** de la battle dans `fleet.d/`.
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
├── docs/gate-write-confinement.md # pourquoi une gate écrit son seul artefact (invariant §4)
├── commands/
│   ├── battle.md                # orchestrateur : start|build|review|test|deliver|address|resume|status
│   ├── retro.md                 # REFLECT
│   ├── fleet.md                 # vue multi-repo
│   ├── issues.md                # pré-THINK : liste les issues ouvertes du repo
│   ├── legatus.md               # lance l'UI web Legatus (http://localhost:5021)
│   ├── freeze.md / guard.md / careful.md
├── agents/
│   ├── architect.md             # gate PLAN
│   ├── builder.md               # PRODUCER BUILD (Edit/Write/Bash, worktree)
│   ├── lint.md                  # gate LINT (dotnet format --verify-no-changes, .NET-only)
│   ├── reviewer.md              # gate REVIEW (+ MCP Roslyn)
│   ├── test-engineer.md         # gate TEST
│   ├── security.md              # gate sécurité
│   └── pr-triage.md             # gate ADDRESS (triage des retours de PR)
├── hooks/
│   ├── hooks.json               # PreToolUse: guard,careful · PostToolUse: fleet_sync · Stop/SubagentStop: usage_track
│   ├── guard.py                 # périmètre d'écriture + confinement gates + artefact non vide (exit 2 = block)
│   ├── careful.py               # avertit sur commandes destructrices (warn)
│   ├── fleet_sync.py            # écrit le shard fleet.d/<battle> à chaque écriture de battle.json (PHASE_ORDER dérivé de battle_state)
│   └── usage_track.py           # append tokens + skills réels à la battle active
├── scripts/
│   ├── plugin_retex.py          # journal central RETEX outillage (append/list/resolve, --self-test)
│   ├── base_freshness.py        # filet base-freshness §G.0.a (verdict déterministe, --self-test)
│   ├── artifact_check.py        # delivery check d'artefact de gate §E (snapshot/verify métadonnées-seules, --self-test)
│   ├── legatus.py               # lanceur Legatus multi-OS (`/legion:legatus` : dotnet, port 5021, détaché, navigateur ; --dry-run, --self-test)
│   ├── opportunity.py           # opportunités hors-scope → issues GitHub (fingerprint/dédup/render, --self-test)
│   ├── battle_state.py          # SEUL écrivain de battle.json/active-battle : transitions vérifiées, budgets 2/6, source unique des tables + lecteur partagé de la battle active pour les hooks (--self-test)
│   └── eval.py                  # éval des gates sur les battles closes du fleet (revise-rate, rondes, coût, --self-test)
└── skills/
    ├── battle-workflow/SKILL.md # la doctrine opérationnelle (résumé de ce doc)
    └── recon/SKILL.md           # pré-THINK : cadrage d'une issue floue (section « Cadrage »)
```

> La couche tickets/PR passe par le CLI **`gh`** appelé directement depuis `battle.md`
> (auth & JSON gérés par `gh`, zéro script réseau). Le dossier `scripts/` ne contient
> que des utilitaires **locaux et déterministes** (journal RETEX, filet base-freshness,
> delivery check d'artefact, dédup des opportunités hors-scope, éval des gates), chacun couvert par
> `--self-test` ; aucun n'appelle le réseau (la couche `gh` — dont la création des issues
> d'opportunité — reste dans les command-files). **Exception « lanceur local »** :
> `legatus.py` lance un process (`dotnet run`, détaché) et ouvre un navigateur ; il ne
> touche qu'un socket loopback (sonde du port 5021) et n'envoie rien vers l'extérieur.

---

## 9. Conventions adoptées

- **Prose FR, identifiants/fichiers EN**. **Sujets de commit et titres de PR** :
  anglais, format **Conventional Commits** `type(scope): subject`.
- **Hooks Python** lancés via `$(command -v python || command -v python3) "$CLAUDE_PLUGIN_ROOT/hooks/<x>.py"`
  (`python` d'abord — Windows ; `python3` en repli — Linux/macOS sans
  `python-is-python3`), `exit 2` pour bloquer, bypass par env var, `--self-test`.
  Les hooks tournent sous `sh` (Linux) / Git Bash (Windows), d'où la substitution
  POSIX. (`python3` seul est exclu : sous Windows c'est souvent l'alias du
  Microsoft Store.)
- **Agents** : frontmatter `name`/`description`/`model`/`tools` (whitelist)/
  `permissionMode`. Lecture seule **sur le code** pour les gates de revue : elles
  écrivent leur seul artefact, confinées par le guard (cf. §4, §6.1).
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
| ADDRESS (fils) | `gh api graphql … reviewThreads { isResolved comments }` (REST n'expose pas `isResolved`) |
| ADDRESS (réponse) | `gh api graphql … addPullRequestReviewThreadReply` |
| ADDRESS (résolution) | `gh api graphql … resolveReviewThread` |

La fermeture de l'issue est **automatique au merge** via `Closes #<n>` dans le corps
de PR — pas de transition d'état explicite à gérer. Dégradation gracieuse : si `gh`
est absent/non authentifié, l'intake bascule inline et le `deliver` rend la main avec
les commandes à exécuter manuellement.

---

## 11. Phase ADDRESS (optionnelle, post-deliver)

Une fois la PR ouverte, les retours de revue humaine sont traités par
`/battle address` — une **boucle mono-repo légère** (l'équivalent allégé de la revue
PR multi-round d'entreprise), branchée sur `gh`. Optionnelle (rien à traiter → la
phase n'existe pas) et **répétable** : un *round* par vague de commentaires
(`phases.address.round`).

Découpage acteur/orchestrateur, fidèle à l'invariant « gate à écriture confinée » :

- **`pr-triage` (gate, lecture seule sur le code, sonnet)** : reçoit le JSON des fils
  non résolus, lit le code visé + le `plan.md`, **écrit** son artefact `pr-feedback.md`
  (le guard l'y confine) et **retourne** le plan de tri par fil — `target`
  (`builder`/`architect`/`none`), `kind` (`code-trivial`/`code-logic`/`test`/
  `question`/`disagreement`), `requires_regate` — plus un brouillon de réponse FR.
  Il ne code pas, ne poste rien, ne résout rien.
- **Orchestrateur (`/battle address`)** : route chaque fil (un commit par fil ;
  `code-logic`/`test` : `invalidate` puis cascade complète depuis `lint` ; `code-trivial` : `lint` seul), **confirme** les effets sortants,
  lance `check-cascade` (exit `2` : pas de push), pousse, puis **répond + résout** chaque fil via `gh api graphql`, et persiste
  `phases.address`.

GitHub n'expose l'état résolu des fils que par **GraphQL** (`reviewThreads.isResolved`)
et n'a **pas** la distinction `fixed`/`wontFix` : `fixed` et `disagreement` confirmé
résolvent tous deux le fil (`resolveReviewThread`), une `question` reste ouverte. La
résolution est **revérifiée** après coup (re-fetch) avant d'être persistée — on ne
fait jamais confiance à la valeur qu'on a voulu écrire. État :
`phases.address = { status, round, threads:[{id, target, kind, commit, resolution}] }`.

---

## 12. Run autonome & taxonomie d'escalade

### Principe directeur

Après l'approbation du plan (unique point d'arbitrage), l'orchestrateur enchaîne
BUILD → gates → DELIVER → ouverture de la PR **sans interaction humaine intermédiaire**,
sauf escalade. L'humain devient un **arbitre**, pas un opérateur qui relance chaque
étape.

**Frontière de l'autonomie :** l'enchaînement autonome s'arrête à l'ouverture de la PR.
La phase ADDRESS (commentaires humains post-livraison) garde sa confirmation humaine.

### Mode d'exécution (`run.mode`)

`run.mode` ∈ `"autonomous"` | `"step"` — persisté dans `battle.json` (par `battle_state.py init`).

- `"autonomous"` (défaut) : enchaînement automatique de toutes les phases après
  l'approbation du plan.
- `"step"` : chaque transition de phase rend la main (comportement des battles
  antérieures à la feature). Activé par `--step` sur `start`.

**Rétrocompatibilité :** un champ `run` absent dans `battle.json` (battle antérieure)
équivaut à `"autonomous"` par défaut — aucune battle en cours n'est cassée.

**`--step` vs `--auto` — deux dimensions orthogonales :**
- `--step` sur `start` (= `run.mode`) : **cadence d'arrêt** de l'orchestrateur
  entre les phases.
- `--auto` sur `build` (= délégation builder) : **délégation de la production de code**
  à un sous-agent `builder` isolé.
Ces deux flags sont indépendants et ne se confondent pas.

### Taxonomie d'escalade (liste close)

L'orchestrateur rend la main à l'humain **uniquement** dans les cas suivants.
Toute correction déterministe se fait sans lui.

| Cas | Déclencheur | Action |
|-----|-------------|--------|
| **1. `reject`** | Une gate rend un verdict `reject`. | Escalade immédiate, zéro tentative. |
| **2. Boucle non convergente** | Aucun FAIL ciblé résolu d'une tentative à l'autre (progrès = identité des FAIL, pas le compte brut), ou plafond atteint (2 tentatives/gate, 6 tentatives au global). | Escalade avec le détail : gate, FAIL résolus/persistants/nouveaux, tentatives. |
| **3. Déviation du plan** | La correction requise sort du périmètre figé (slices de `plan.md` ou `guard.allow`). | Escalade : re-planification nécessaire. |
| **4. Filets DELIVER** | Base locale en retard sur `origin`, remote vide, fichier hors whitelist de commit, `.gitignore` auto-induit. | Escalade : résoudre le filet, puis DELIVER reprend. |
| **5. Préflight défaillant** | `python` absent, `gh` absent/non authentifié, stack ambiguë. | Escalade : résoudre l'environnement. |

Hors liste = pas d'escalade.

### Boucle d'auto-correction bornée (deux niveaux distincts)

| Niveau | Acteur | Budget | Unité mesurée | Décision d'escalade |
|--------|--------|--------|---------------|---------------------|
| Interne (build-fix) | `builder` | 3 tentatives | Erreurs `dotnet build` | Le builder rapporte `build_ok: false`, **ne décide pas d'escalader** |
| Externe (re-gate) | Orchestrateur, via `battle_state.py bump-autocorrect` | 2/gate, 6 au global (maximums fermes) | Ensemble des FAIL du `gate-*.md` (par identité) | Le script rend `continue` ou `escalate` (non-progrès ou plafond) ; l'orchestrateur applique |

Les deux budgets sont **non additionnés** : un `build_ok: false` du builder après ses 3
essais **compte pour 1 tentative** de la boucle orchestrateur. La boucle interne repart
de 0 à chaque nouvelle demande de correction.

**Détection de progrès (par identité, pas par compte) :** compare l'**ensemble** des
FAIL du `gate-*.md` entre deux tentatives, par cible (`fichier:ligne` + dimension).
Progrès = au moins un FAIL ciblé au run précédent a **disparu** (résolu), même si le
compte total est stable parce qu'un nouveau FAIL d'une autre cause est apparu. Aucun
FAIL précédent résolu = non-progrès → escalade immédiate (pas d'attente du plafond).
Le script `bump-autocorrect` applique cette comparaison et les plafonds (2/gate, 6 au
global) : ils ne reposent plus sur la discipline de l'orchestrateur. Le compte brut seul est trompeur : « 1 FAIL corrigé, 1 autre découvert » est un compte
stable mais un vrai progrès.

**Invalidation de la cascade.** Toute correction de code rend caducs les verdicts déjà
rendus. Sur `continue`, `bump-autocorrect` invalide d'office les phases de cascade
présentes (`lint`, `review`, `test`, `security`) qui sont `done` ou `blocked` : statut
`pending`, `verdict` à `null`, `fails` conservés (le contrôle de progrès reste correct),
`invalidated_at` posé, et une entrée `{at, reason, phases}` ajoutée à `run.invalidations`
si au moins une phase a changé. Après le BUILD correctif, l'orchestrateur relance **toute
la cascade requise depuis `lint`**. La sous-commande `invalidate [--reason R]` applique la
même règle hors boucle (raisons : `rebase`, `address:<n>`, `polish`, `manual`). Un nouveau
passage `transition plan in_progress` (re-plan) invalide aussi la cascade (raison `replan`), y compris une gate `in_progress`
(les autres raisons ne touchent que `done`/`blocked`) ; tant que le plan n'est pas ré-approuvé,
toute transition de cascade (`in_progress`/`done`) est refusée, comme `build`.
`set-slices --replace` le fait en défense, sans second événement si elle est déjà `pending`.
`transition … pending` reste refusé.

**Ronde de polissage.** Une seule fois par battle, quand toutes les gates sont `accept*` et
avant DELIVER : `invalidate --reason polish`, BUILD correctif, cascade depuis `lint`. Elle
est **hors budget** 2/6 (elle ne touche pas `run.autocorrect`) ; un `revise` pendant la
ronde entre dans la boucle normale. Le script refuse une seconde ronde, ou une ronde
demandée alors que DELIVER n'est pas prêt.

### Choix ouverts exposés par l'architecte

L'architecte documente dans `plan.md` (section « Choix ouverts à arbitrer ») chaque
décision de conception où plusieurs options valides existaient. L'objectif est de
résoudre un maximum d'options **en amont** pour qu'aucune ne reste à arbitrer pendant
le run. C'est cette section que l'orchestrateur présente à l'humain au point
d'arbitrage unique (approbation du plan).
