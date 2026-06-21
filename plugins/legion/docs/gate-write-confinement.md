# Mémo — Confinement d'écriture des gates (discipline de contexte)

> **Objet.** Réduire l'empreinte de contexte d'un pipeline de gates type *legion*
> (`architect` / `lint` / `reviewer` / `test-engineer` / `security` / `pr-triage` + un
> producteur `builder`). Ce document explique le **problème**, le **pré-requis
> technique** (validé empiriquement), la **solution** et son **implémentation
> fichier par fichier**. Il est écrit pour être **transposé à un autre plugin** du
> même patron (ex. `divalto-legion`) : partout où apparaît `<plugin>`, remplacer par
> le nom du plugin **tel qu'enregistré dans le marketplace** (pour `legion` :
> `legion`). Implémentation de référence : branche `feat/recon-skill` de `legion`.

---

## 1. Le problème — pourquoi les gates coûtent du contexte

Dans ce patron, l'orchestrateur (`/battle`) délègue chaque phase à un **sous-agent**
en contexte isolé, puis ne garde que ce que le sous-agent **retourne**. Or il y a une
**asymétrie** dans ce qui remonte :

| Acteur | Écrit son artefact ? | Ce qui remonte dans la session orchestratrice | Poids |
|---|---|---|---|
| `builder` (producteur) | **Oui** (`build-report.md`) | un retour JSON court `{slice_id, build_ok, warnings, …}` | **maigre** |
| Les **gates** (lecture seule) | **Non** | verdict court **+ le contenu COMPLET** de `gate-*.md` / `plan.md` / `pr-feedback.md` | **lourd** |

La cause : l'invariant historique « **gates pures** » impose que *seul l'orchestrateur
écrit l'état de la battle*. Comme une gate ne peut pas écrire, elle doit **faire
transiter** son artefact entier par l'orchestrateur (qui le persiste). Ce contenu
**reste ensuite dans le transcript** de la session orchestratrice pour le reste de la
battle, **amplifié par les boucles** `revise → build → re-gate` (une version complète
de l'artefact réinjectée à chaque round). Ordre de grandeur : 8–15k tokens sur une
battle qui bagarre 3 rounds.

Un retour de sous-agent entre **définitivement** dans le transcript : on ne peut pas
l'« oublier » sans `/clear` (impossible à automatiser depuis un plugin). **Le seul
moyen de réduire l'empreinte est que la gate n'ait pas à retourner le gros contenu —
donc qu'elle l'écrive elle-même.**

---

## 2. Pré-requis technique — `agent_type` dans le payload `PreToolUse`

Faire écrire les gates ne doit pas sacrifier la garantie « une gate ne touche pas le
code ». Il faut donc un **hook** `PreToolUse` qui **confine** chaque gate à son seul
artefact. Cela suppose que le hook **connaisse l'identité du sous-agent appelant**.

**Fait établi** (doc Claude Code « Hooks › Common Input Fields », **confirmé
empiriquement**) : le payload `PreToolUse` contient `agent_type` (et `agent_id`).

Résultats mesurés (instrumentation jetable du hook live + écritures déclenchées) :

| Source de l'écriture | `agent_id` | `agent_type` |
|---|---|---|
| Session principale (orchestrateur) | **absent** | `"claude"` |
| Sous-agent générique | présent | `"general-purpose"` |
| Sous-agent de plugin | présent | **`"<plugin>:<agent>"`** (ex. `legion:builder`) |

Conclusions réutilisables :
- `agent_type` est **toujours présent** ; la session principale vaut `"claude"`
  (≠ ce que dit parfois la doc — vérifier soi-même).
- L'identité d'un sous-agent de plugin est **namespacée** : `<plugin>:<agent>`.
- `agent_id` n'est présent **que** pour un sous-agent → c'est le discriminant
  « est-ce un sous-agent ? » si besoin.

> ⚠️ **À re-vérifier sur la cible** (`divalto-legion`) avant d'implémenter — la valeur
> exacte d'`agent_type` dépend du **nom du plugin** dans le marketplace. Méthode (5 min,
> réversible) :
> 1. repérer le hook **live** (souvent une copie installée, pas le checkout :
>    `~/.claude/plugins/cache/<mkt>/<plugin>/<version>/hooks/guard.py`) ;
> 2. y insérer en tête de `main()`, après le `json.load(sys.stdin)`, un log jetable :
>    `open(<chemin temp>, "a").write(json.dumps({"agent_type": data.get("agent_type"), "file": __file__}) + "\n")` ;
> 3. lancer un sous-agent du plugin (un qui a `Write`, ex. le `builder`) avec une
>    consigne « écris ce fichier temp et rien d'autre » ;
> 4. lire le log → confirmer `agent_type == "divalto-legion:builder"` ;
> 5. **retirer l'instrumentation** et lancer le `--self-test` du hook.
>
> Le hook **live** étant la version *installée*, le code ci-dessous se modifie dans le
> **source du repo** et ne prend effet qu'après **réinstall/publication** du plugin.

---

## 3. La solution — invariant « gate à écriture confinée »

On remplace « gates pures » par :

> Une gate est **lecture seule sur le code** et n'écrit **qu'un seul fichier** : son
> propre artefact, dans le dossier de la battle. Le hook `guard.py` l'y **confine** via
> `agent_type` (toute autre écriture → `exit 2`). La gate **retourne** alors son
> verdict + le **chemin** de l'artefact — jamais le contenu.

La garantie « une gate ne touche pas le code » devient **structurelle** (portée par le
hook), au lieu d'être seulement déclarée dans le prompt. L'orchestrateur écrit le reste
(`battle.json`, `spec.md`, artefacts de PR) et **lit** les artefacts de gate sur disque
au besoin.

**Pourquoi c'est sûr sans politique de dégradation.** La règle ne s'arme que pour un
`agent_type` listé comme gate. Tout le reste (orchestrateur `"claude"`, `builder`
`<plugin>:builder`, éditions hors-battle) conserve le comportement existant. Aucun
risque d'« ouverture » accidentelle : un `agent_type` inconnu n'est jamais traité comme
une gate confinée.

---

## 4. Implémentation, fichier par fichier

### 4.1 `hooks/guard.py` — le cœur

Ajouter la table (clés **namespacées** — adapter le préfixe au plugin) :

```python
GATE_ARTIFACT = {
    "<plugin>:architect": "plan.md",
    "<plugin>:lint": "gate-lint.md",
    "<plugin>:reviewer": "gate-review.md",
    "<plugin>:test-engineer": "gate-test.md",
    "<plugin>:security": "gate-security.md",
    "<plugin>:pr-triage": "pr-feedback.md",
}
```

Deux helpers — un lecteur du pointeur **indépendant de `guard.allow`** (le confinement
doit s'appliquer même guard non armé), et une **fonction pure testable** :

```python
def _active_battle_id(repo_root):
    pointer = repo_root / ACTIVE_POINTER
    if not pointer.is_file():
        return None
    return pointer.read_text(encoding="utf-8").strip() or None

def _gate_decision(agent_type, rel, battle_id):
    """None = pas une gate (règles standard) ; True = autorisé ; False = bloqué."""
    artifact = GATE_ARTIFACT.get(agent_type)
    if artifact is None:
        return None
    if battle_id is None or rel is None:
        return False
    return rel == f".legion/battles/{battle_id}/{artifact}"
```

Brancher en **tête** de la décision (avant la logique de périmètre existante), juste
après le filtre `tool_name not in WRITE_TOOLS` :

```python
agent_type = data.get("agent_type")
file_path = (data.get("tool_input") or {}).get("file_path", "")
if agent_type in GATE_ARTIFACT:
    battle_id = _active_battle_id(repo_root)
    rel = _relative(repo_root, file_path) if file_path else None
    if _gate_decision(agent_type, rel, battle_id):
        return 0, ""
    expected = f".legion/battles/{battle_id or '<aucune>'}/{GATE_ARTIFACT[agent_type]}"
    return 2, f"BLOQUE : la gate `{agent_type}` ne peut écrire QUE `{expected}`."
```

Ajouter des assertions au `--self-test` (autorisé / mauvais artefact / code /
`battle.json` / hors battle / non-gate).

### 4.2 `agents/*.md` (les 6 gates)

Pour **chaque** gate :
- **Frontmatter** : ajouter `Write` à `tools:` ; reformuler `description:` en « écrit
  son seul artefact … et retourne verdict + chemin ».
- **Rôle / Output / Anti-patterns** : « tu **écris** ton artefact `<artefact>` dans le
  dossier de la battle, puis tu **retournes uniquement** le bloc verdict + le chemin
  (`ARTIFACT: …`), pas le contenu ». Anti-pattern : « n'écris QUE ton artefact (le
  guard t'y confine) : pas de code, pas de `battle.json` ».
- **Cas `pr-triage`** : il **écrit** `pr-feedback.md` **et** continue de **retourner**
  le bloc `TRIAGE` JSON (machine-lisible) sur lequel l'orchestrateur route. Préciser le
  **multi-round** : si l'artefact existe déjà, lire puis ré-écrire l'ensemble en
  ajoutant le nouveau `## Round <n>` (un `Write` remplace le fichier). **Modèle :
  `sonnet`** (et non haiku) — la lecture + ré-écriture en append multi-round est une
  manipulation de fichier que sonnet tient plus fiablement ; le garde-fou de livraison
  (§4.5) la contrôle de toute façon.

### 4.3 `commands/battle.md` (orchestrateur)

- Préambule : « chaque gate écrit son propre artefact (guard-confiné) et retourne
  verdict + chemin ; l'orchestrateur persiste le reste et lit les artefacts au besoin ».
- Étape architect (PLAN) et étape gates (REVIEW/TEST/SEC) : **ne plus** « écrire le
  contenu retourné » — l'artefact est déjà sur disque ; n'enregistrer que
  `verdict`/`status` dans `battle.json`.
- Boucle `revise` : **briefer le builder par CHEMIN** d'artefact (il lit le détail des
  FAIL depuis le disque) — ne pas tirer le contenu complet dans la session pour le
  briefer (sinon on re-remplit le contexte que le confinement vient d'épargner).
- `pr-triage` : la gate écrit `pr-feedback.md` ; l'orchestrateur le **complète** ensuite
  (SHA, résolutions) — cette écriture-là est la sienne (il n'est pas confiné).

### 4.4 Doctrine — `ARCHITECTURE.md` + `skills/*/SKILL.md`

Remplacer l'énoncé « gates pures / ne touchent jamais le disque » par l'invariant
« gate à écriture confinée » (§3), et documenter l'étape 0 de `guard.py` (confinement
par `agent_type`) ainsi que la colonne « lecture seule **sur le code** » du tableau des
gates.

### 4.5 Garde-fou : vérification de livraison d'artefact (orchestrateur)

**Contrepartie obligatoire du levier.** Avant, un verdict retourné s'accompagnait du
contenu : sa présence *prouvait* l'artefact. Maintenant la gate écrit elle-même —
**le verdict ne prouve plus rien**. Sans garde-fou, une gate qui « oublie » d'écrire
(ou écrit un round périmé) ferait avancer le pipeline sur un artefact absent/obsolète.

L'orchestrateur applique donc, **autour de chaque invocation de gate**, un check
**déterministe** et **métadonnées-seules** (il ne lit jamais le contenu — sinon il
re-remplirait le contexte que le confinement épargne) :

1. **Avant** d'invoquer : résoudre le chemin canonique
   `.legion/battles/<id>/<artefact>` et, **s'il existe déjà** (round de re-loop),
   capturer son mtime (`(Get-Item <path>).LastWriteTimeUtc`).
2. La gate retourne `VERDICT … ARTIFACT: <chemin>`.
3. **Après** : vérifier **les quatre** — (a) le fichier **existe** ; (b) il est **non
   vide** (`(Get-Item <path>).Length > 0` — une gate peut rendre un verdict en laissant
   un artefact **0 octet** ; le fichier existe alors, mais ne prouve rien) ; (c) le
   `ARTIFACT:` retourné **== le chemin canonique** attendu (le guard bloque déjà une
   mauvaise *écriture* ; ceci attrape un mauvais chemin dans la *chaîne retournée*) ;
   (d) il a été **écrit à ce passage** (n'existait pas avant, ou mtime **strictement
   postérieur** à l'étape 1 — un résidu de round précédent ne doit jamais passer pour frais).
4. **À tout échec** → ne pas enregistrer le verdict, ne pas avancer ; **re-invoquer la
   gate une fois** (rappel explicite « écris ton artefact à `<chemin exact>` d'abord ») ;
   si l'échec persiste → phase `blocked`, remonter à l'humain, stop. **Jamais
   d'avancée sur un verdict dont l'artefact frais n'est pas confirmé.**

> Alternative envisagée puis écartée : un hook `SubagentStop` qui empêcherait la gate
> de terminer sans avoir écrit. Plus « exécutable » (philosophie § 6 de legion) mais
> sur-ingénieré ici — risque faible (prompt d'écriture explicite), et un hook qui
> relance mal pourrait coincer une gate en boucle. Le check orchestrateur, **placé sur
> le seul séquenceur** (qui décide d'avancer ou de bloquer), couvre le risque sans ce
> coût. À reconsidérer si, en pratique, des gates ratent l'écriture malgré tout.

---

## 5. Tests & vérification

- `python hooks/guard.py --self-test` → vert.
- Tests stdin (simulent l'appel réel du hook) :
  ```bash
  echo '{"tool_name":"Write","agent_type":"<plugin>:reviewer","tool_input":{"file_path":"gate-review.md"}}' | python hooks/guard.py   # attendu : exit 2 (hors battle / mauvais chemin)
  echo '{"tool_name":"Write","agent_type":"claude","tool_input":{"file_path":"src/x.cs"}}'                 | python hooks/guard.py   # attendu : exit 0 (orchestrateur, non confiné)
  echo '{"tool_name":"Read","agent_type":"<plugin>:reviewer","tool_input":{"file_path":"x"}}'             | python hooks/guard.py   # attendu : exit 0 (pas un write tool)
  ```
- Test bout-en-bout : lancer une vraie battle après réinstall du plugin et vérifier
  qu'une gate écrit bien son `gate-*.md` (le guard ne bloque pas) mais est **bloquée**
  si elle tente d'écrire ailleurs.

---

## 6. Gain attendu & limites

- **Gain plein** sur `accept` / `accept_with_opportunity` : l'artefact de gate n'entre
  **jamais** dans le contexte orchestrateur.
- **Sur `revise`** : l'orchestrateur a besoin du détail des FAIL pour corriger. En
  **briefant le builder par chemin** (le builder lit l'artefact du disque), le contenu
  reste hors du transcript orchestrateur même en boucle. Si l'orchestrateur relit
  lui-même l'artefact pour rapporter à l'humain, le contenu revient — mais **une fois**,
  au lieu des deux émissions (verdict + contenu) de l'ancien modèle.
- **Qualité d'analyse : inchangée.** Prompts, modèles, outils de lecture/MCP et skills
  des gates ne changent pas — seul le *canal* de l'artefact change. Le contexte
  orchestrateur plus léger **améliore** même la lucidité sur les battles longues.
- **Coût** : le hook devient un point de passage critique ; bien le couvrir par
  `--self-test`. Une erreur de **clé namespacée** (mauvais préfixe de plugin) ⇒ la gate
  retombe en règles standard (peut écrire dans tout `.legion/**`) **ou** est bloquée
  selon le cas — d'où l'étape de validation empirique (§2) **obligatoire** par plugin.
- **Livraison** : le verdict ne prouvant plus l'artefact, le **garde-fou §4.5** (check
  de livraison côté orchestrateur) est **indissociable** du levier — ne pas le porter
  laisserait passer un verdict sans artefact frais.

---

## 7. Checklist de portage vers `<plugin>`

- [ ] Valider `agent_type == "<plugin>:<agent>"` sur l'install cible (§2).
- [ ] `guard.py` : table `GATE_ARTIFACT` (préfixe `<plugin>:`), helpers, branchement, self-tests.
- [ ] 6 `agents/*.md` : `Write` + description + Rôle/Output/Anti-patterns (+ `lint` : .NET-only, `dotnet format` verify-only ; + `pr-triage` : TRIAGE JSON conservé, multi-round, **modèle sonnet**).
- [ ] `battle.md` : préambule, PLAN, gates, boucle `revise` (brief par chemin), `pr-triage`, guardrails.
- [ ] `battle.md` : **garde-fou de livraison §4.5** (check existence + chemin canonique + mtime, autour de chaque gate) — **indissociable du levier**.
- [ ] `ARCHITECTURE.md` + `SKILL.md` : nouvel invariant + étape 0 du guard + vérif de livraison.
- [ ] `--self-test` vert + tests stdin + un run réel post-réinstall.
