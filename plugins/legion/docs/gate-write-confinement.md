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
| `builder` (producteur) | **Oui** (`build-report-<slice_id>.md`, ou `build-report.md` pour un BUILD agrégé ; consolidé ensuite par `merge-reports`) | un retour JSON court `{slice_id, build_ok, warnings, …}` | **maigre** |
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

La garantie « une gate ne touche pas le code » devient **structurelle**, au lieu d'être
seulement déclarée dans le prompt. Elle repose sur l'**empreinte de l'arbre** (§ 4.6), le
hook n'étant qu'un premier filtre. L'orchestrateur écrit le reste
(`spec.md`, artefacts de PR ; `battle.json` via `battle_state.py`) et **lit** les artefacts de gate sur disque
au besoin.

**Pourquoi c'est sûr sans politique de dégradation.** La règle ne s'arme que pour un
`agent_type` listé comme gate. Tout le reste (orchestrateur `"claude"`, `builder`
`<plugin>:builder`, éditions hors-battle) conserve le comportement existant. Aucun
risque d'« ouverture » accidentelle : un `agent_type` inconnu n'est jamais traité comme
une gate confinée.

---

## 4. Implémentation, fichier par fichier

### 4.1 `hooks/guard.py` — le cœur

Ajouter la table (clés **namespacées** — adapter le préfixe au plugin). Dans legion, cette
table est désormais **dérivée** de `battle_state.GATE_ARTIFACT` (source unique, préfixe
`legion:` ajouté par `guard.py`, repli fail-closed si le script est introuvable) ; le
bloc ci-dessous reste l'illustration du principe :

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

> Dans legion, ce lecteur est `battle_state.active_battle_id` (partagé par `guard.py`, `careful.py` et
> `usage_track.py`, id validé par liste blanche). Le bloc ci-dessous reste une illustration.

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
re-remplirait le contexte que le confinement épargne). Le calcul est porté par le script
`scripts/artifact_check.py` (Python stdlib, identique sous Windows, Linux, WSL et macOS,
sans PowerShell) :

1. **Avant** d'invoquer : résoudre le chemin canonique
   `.legion/battles/<id>/<artefact>` et, **s'il existe déjà** (round de re-loop),
   capturer son mtime avec `artifact_check.py snapshot <chemin>` (JSON
   `{exists, mtime_ns, size}` ; ne garder `mtime_ns` que si `exists` est vrai).
2. La gate retourne `VERDICT … ARTIFACT: <chemin>`.
3. **Après** : lancer
   `artifact_check.py verify <chemin> [--since <mtime_ns>] --returned <ARTIFACT: retourné>`
   (toujours passer `--returned`), qui vérifie **les quatre** — (a) le fichier **existe** ; (b) il est **non
   vide** (taille > 0 — une gate peut rendre un verdict en laissant
   un artefact **0 octet** ; le fichier existe alors, mais ne prouve rien) ; (c) le
   `ARTIFACT:` retourné **== le chemin canonique** attendu (le guard bloque déjà une
   mauvaise *écriture* ; ceci attrape un mauvais chemin dans la *chaîne retournée*) ;
   (d) il a été **écrit à ce passage** (n'existait pas avant, ou mtime **strictement
   postérieur** à l'étape 1 — un résidu de round précédent ne doit jamais passer pour frais).
4. **À tout échec** (`ok:false`, exit `2`) → ne pas enregistrer le verdict, ne pas avancer ; **re-invoquer la
   gate une fois** (rappel explicite « écris ton artefact à `<chemin exact>` d'abord ») ;
   si l'échec persiste → phase `blocked`, remonter à l'humain, stop. **Jamais
   d'avancée sur un verdict dont l'artefact frais n'est pas confirmé.**

> Alternative envisagée puis écartée : un hook `SubagentStop` qui empêcherait la gate
> de terminer sans avoir écrit. Plus « exécutable » (philosophie § 6 de legion) mais
> sur-ingénieré ici — risque faible (prompt d'écriture explicite), et un hook qui
> relance mal pourrait coincer une gate en boucle. Le check orchestrateur, **placé sur
> le seul séquenceur** (qui décide d'avancer ou de bloquer), couvre le risque sans ce
> coût. À reconsidérer si, en pratique, des gates ratent l'écriture malgré tout.

### 4.6 Deux couches : empreinte de l'arbre et filtre Bash (#66)

Le hook `PreToolUse(Edit|Write|MultiEdit)` ne voit pas l'outil `Bash`. Une gate (ou un
builder) pouvait donc écrire par `echo > f`, `sed -i` ou `git checkout`. Deux couches
comblent ce trou.

**Couche 1 — empreinte de l'arbre (la garantie).** `artifact_check.py tree-snapshot` prend
une empreinte : `HEAD`, entrées de `git status` avec le hash de leur contenu, état protégé
(`.legion/active-battle`, `battle.json`), masques d'index, config git locale et worktree,
hooks (dossier réel et `core.hooksPath`), `info/attributes`, `.gitignore` eux-mêmes ignorés, la
config git globale et système, les fichiers `attributes`/`ignore` effectifs (explicites ou XDG)
et **l'état du dossier git** (voir ci-dessous).

*État du dossier git.* L'empreinte hashe les chemins du git-dir (et du dossier commun pour un
worktree lié) **que git lit** : `HEAD` et tout `*_HEAD` (`MERGE_HEAD`, `CHERRY_PICK_HEAD`,
`REVERT_HEAD`, `ORIG_HEAD`, `REBASE_HEAD`…), `BISECT_*`, `AUTO_MERGE`, `MERGE_MSG`, `SQUASH_MSG`,
`refs/` (tags, remotes, notes, stash, **branches**), `packed-refs`, `rebase-merge/`, `rebase-apply/`,
`sequencer/`, `shallow`, `info/` (grafts…), `modules/`, `commondir`, `config.worktree`, ainsi que
la liste des worktrees enregistrés (chemin, branche, `gitdir`). Tout écart est la faute
`[git-state]`, jamais filtrable. Un fichier que git ne connaît pas à la racine du git-dir (par
exemple les `.git/sg-hook-once-toolu_*` qu'écrit un hook tiers à chaque appel d'outil) est inerte :
git l'ignore, ce n'est pas une faute. Exclusions, chacune justifiée : `objects/` (limite, voir
plus bas) ; `index`, `*.lock`, `sharedindex.*` (un `git status`/`diff` de l'orchestrateur
rafraîchit l'index : octets instables) ; `logs/` (reflogs), `FETCH_HEAD`, `COMMIT_EDITMSG`,
`gc.log`, `gc.pid` (journaux écrits par git, jamais lus pour décider d'un commit ou d'un push) ;
les **seules** branches extraites dans un worktree enregistré (`refs/heads/<b>` et lignes de
`packed-refs`) : celles des builders isolés bougent pendant un lot. Toute autre branche est
hashée : une gate qui crée la branche de livraison `<me>/<token>` est vue. Le dossier `worktrees/`
du dossier commun est remplacé par la liste des worktrees (un faux `.git/worktrees/x` qui ferait
sortir un dossier suivi de l'empreinte apparaît dans cette liste). Exception unique : après un
lot de builders `--auto` isolés, `tree-verify --batch-worktrees` sur le tronc accepte les
worktrees **apparus depuis le snapshot** qui sont sous `<racine>/.claude/worktrees/`, non
`prunable`, et dont l'entrée admin et le fichier `.git` se renvoient l'un à l'autre. Un worktree
disparu ou redirigé, ou tout autre worktree, reste une faute. L'exception est aussi refusée
(faute) si `.claude`, `.claude/worktrees` ou un composant du chemin du worktree est un lien
symbolique, ou si le chemin du worktree contient des fichiers suivis par le tronc
(`git ls-files -- <chemin>` non vide). `hooks/`, `config`, `info/exclude`
et `info/attributes` sont hashés à part.
Le contenu de l'index est couvert autrement : `git status` tourne sur une copie de l'index sans
données stat (`ls-files -s` puis `update-index --index-info` dans un index temporaire), donc git
rehashe le contenu de chaque fichier suivi. Un `.git/index` réécrit (blob d'origine, stat du nouveau
fichier) ne masque plus une modification, et le rafraîchissement de l'index réel ne crée pas de
fausse faute. Un `GIT_INDEX_FILE` hérité du shell est ignoré. Pour un builder isolé (`--base`),
le git-dir privé du worktree ne doit contenir que les entrées que git y crée (`HEAD`, `commondir`,
`gitdir`, `index`, `logs`, `refs`, `ORIG_HEAD`…) : tout autre fichier d'état est `[git-state]`.
Chaque appel `git` impose `core.fsmonitor=false`, `core.untrackedCache=false`,
`core.trustctime=true`, `core.checkStat=default`, `core.ignoreStat=false` et
`GIT_NO_REPLACE_OBJECTS=1` : un réglage posé par la gate ne rend pas un fichier modifié
invisible. Un écart de config, de hooks ou d'attributs est une faute nommée (`[git-config]`,
`[git-hooks]`, `[git-attributes]`), jamais filtrable. Un `HEAD` qui bouge sans chemin
(commit vide, `reset --soft`) est la faute `[HEAD]`. `tree-verify` recalcule l'empreinte après
l'agent. Pour une gate, tout changement est une faute. Pour un builder, `--guard` ne
signale que les chemins hors de `guard.allow` (ou dans `deny`), lus dans la battle active de la racine d'état (dépôt principal depuis un worktree).
Un builder isolé en worktree se vérifie avec `--base <sha> --root <worktree>`. L'orchestrateur
retient l'empreinte de `tree-snapshot` et la passe à `tree-verify --fingerprint` : un
snapshot réécrit est refusé. Une faute de gate rend le verdict caduc (escalade cas 6).

**Couche 2 — filtre Bash (premier filtre).** `guard.py` est lancé sur `Bash|PowerShell` et
bloque, pour les gates seulement, les écritures évidentes en tête de commande (voir
`ARCHITECTURE.md` § 6.1), dont `git config` en écriture (les formes `--get*`, `--list`, une
seule clé passent ; `--show-origin`/`--show-scope` ne sont que des modificateurs : `git config
--show-scope <clé> <valeur>` écrit) et les briques d'écriture d'état `git commit-tree`, `mktree`,
`hash-object -w`, `notes`, `maintenance`. Le nom de commande est le basename (`/usr/bin/env rm x` est vu). Le corps
d'un heredoc à délimiteur non quoté est analysé pour ses `$(…)` et backticks ; un `<<` dans
`$((…))` n'est pas un heredoc. Les redirections vers `/dev/null`, vers un `*.log` de la battle
ou vers le dossier temporaire restent permises. Le filtre est best-effort et fail-closed
sur une commande non analysable.

**Limites assumées.**
- Le filtre ne voit ni `python -c "open(...).write"`, ni `bash -c`, ni `eval`. La couche 1
  les détecte après coup (état du dossier git, index et config comprises).
- Limites de l'état git : `objects/` n'est pas empreinté (`objects/info/alternates` inclus).
  Cas connu : une gate peut écrire, sous le sha du futur blob d'un fichier de la slice, un objet
  libre au contenu différent ; le `git add` suivant ne le réécrit pas et le commit local porte ce
  contenu. Le `git push` échoue (le receveur recalcule les hash : « missing necessary
  objects »), donc rien n'est livré. Piste : `git fsck --no-dangling` après le commit de DELIVER.
  En mode `--base`, `gitstate` commun est recopié de `after` :
  une écriture du builder dans le dossier commun est vue par le `tree-verify` du tronc qui suit
  le lot (l'ordre imposé par `battle.md` doit rester). Garde-fou de doctrine en plus : `battle.md`
  refuse tout `git commit` si `MERGE_HEAD`, `CHERRY_PICK_HEAD` ou `REVERT_HEAD` répond, et refuse de
  réutiliser une branche `<me>/<token>` déjà présente sans livraison enregistrée (cas 4), sauf
  reprise d'un DELIVER interrompu : branche au même commit que `HEAD`, ou un seul commit
  au-dessus de `HEAD` dont les chemins sont dans la liste blanche et dont le contenu est
  identique à l'arbre jugé. Aucun champ d'état n'enregistre la branche.
  Trois limites restantes, sans chemin prouvé de livraison de contenu non jugé : (1) le format de
  refs `reftable` (défaut prévu de git 3.0) n'est pas empreinté : `reftable/` n'est pas dans
  `_STATE_READ` ; piste : hasher `git for-each-ref` ou refuser si `extensions.refStorage` vaut
  `reftable`. (2) Sur un système de fichiers insensible à la casse (NTFS, APFS), `_git_reads`
  compare sans tenir compte de la casse : `.git/merge_head` est vu ; l'inverse, sur un système
  sensible, est une fausse faute rare et sans danger. Les autres noms (`Packed-Refs`…) suivent la
  même comparaison. (3) Un worktree du lot accepté par `--batch-worktrees` peut porter la branche
  `<me>/<token>` (ses branches sont exclues des refs) ; le vérificateur ne connaît pas ce nom, et
  `git checkout` d'une branche extraite ailleurs échoue, ce qui ferme le chemin en doctrine.
  Le sha de l'entrée d'index fait partie de l'empreinte de chaque chemin de `status` : un blob
  indexé changé sous « MM » est vu. Le worktree `--batch-worktrees` suppose que les worktrees du
  harness restent en place jusqu'au `tree-verify` du tronc.
- La couche 1 ne voit ni un dossier vide créé, ni un fichier git-ignoré, ni un déplacement de branche
  dans un worktree enregistré (`git branch -f` sur une branche de builder). Le filtre bloque `git push`. Exception : un `.gitignore`
  qui s'ignore lui-même (`*`) est empreinté ; seul celui au sommet d'un dossier neuf replié
  l'est, un `.gitignore` plus profond dans un dossier ignoré échappe. Un fichier ignoré par
  `core.excludesFile` global n'est plus invisible : la config globale/système et les fichiers
  `attributes`/`ignore` effectifs sont hashés (`[git-config]`). Reste hors empreinte : les
  fichiers inclus par `include.path`, et toute autre écriture hors du dépôt.
- Tout `.legion/` hors état protégé est exclu de l'empreinte : une gate qui réécrit `plan.md`,
  `spec.md`, l'artefact d'une autre gate ou le `battle.json` d'une autre battle n'est pas
  vue. Les verdicts restent protégés (ils sont dans le `battle.json` actif).
- Les écritures hors du dépôt ne sont pas vues, ni celles dans un worktree enregistré (exclu
  de l'empreinte de l'arbre principal). Le vérificateur lui-même (`artifact_check.py`,
  `guard.py`, dans l'installation du plugin) n'est pas protégé : il faudrait un sandbox (#49).
- Le filtre ne connaît qu'une liste finie de commandes : d'autres écritures (`awk` avec
  redirection interne, `git diff --output=`, `Set-Content` par alias exotique…) passent.
- Aucun filtre ne s'applique si `agent_type` manque sur l'appel (H1) ; la couche 1 reste la
  garantie. Pas de `python`/`python3` sur le PATH : le hook n'est pas bloquant (motif déjà
  présent pour `careful.py`).
- L'orchestrateur n'écrit jamais `battle.json` entre `tree-snapshot` et `tree-verify`
  (l'état protégé est dans l'empreinte) ; pour un lot de gates en parallèle, les verdicts
  sont enregistrés après le `tree-verify` du lot.
- Une modification de l'humain pendant une gate (IDE, formateur à l'enregistrement) est
  imputée à la gate ; la liste des chemins permet de trancher.
- `bin/`, `obj/`, `TestResults/`, `coverage/` doivent être git-ignorés, sinon `lint` et
  `test-engineer` tombent en faute.
- `agent_type` sur un appel `Bash` reste à confirmer sur une vraie session (méthode du § 2).
  S'il manquait, seule la couche 2 serait inactive ; la couche 1 resterait la garantie.

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
- **Écritures par Bash** : le hook seul ne les couvre pas. La garantie tient à l'empreinte de
  l'arbre (§ 4.6) ; le filtre Bash n'est qu'un premier filtre, aux limites listées au § 4.6.
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
