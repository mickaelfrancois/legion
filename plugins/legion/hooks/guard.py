"""Hook PreToolUse (legion) : applique le perimetre d'ecriture de la battle active.

Quand une battle est active (pointeur `.legion/active-battle`) et que son
`guard.allow` est non vide (pose par `/freeze` ou `/guard`), toute ecriture
hors perimetre est **bloquee** (exit 2 + message stderr).

Regles :
- Pas de battle active, ou `guard.allow` vide -> exit 0 (edition libre).
- `.legion/**` toujours autorise (etat de la battle, ecrit par l'orchestrateur).
- `.gitignore` (racine) toujours autorise : la preflight de `start` propose d'y
  ajouter `.legion/` (mise en place de l'orchestrateur) ; cette exception garantit
  que l'edition passe meme si un perimetre est actif (sinon : no-op silencieux).
- Memoire projet de Claude (`~/.claude/projects/*/memory/**`) toujours autorisee :
  le guard regit le perimetre d'ecriture *du repo*, pas l'infra memoire de Claude
  (ou /retro persiste une learning durable, parfois perimetre encore actif).
- **Confinement des gates** : un sous-agent gate (`agent_type` =
  `<plugin>:architect`/`lint`/`reviewer`/`test-engineer`/`security`/`pr-triage`) ne peut
  ecrire QUE son unique artefact dans `.legion/battles/<active>/` (ni code, ni
  `battle.json`) -> hors de la -> exit 2. Et cet artefact ne peut pas etre **vide** :
  un `Write` a contenu blanc (0 octet) est bloque (RETEX A1 ; le delivery check de
  l'orchestrateur reste le filet pour le cas ou la gate n'ecrit rien du tout). Rend structurelle (portee par le hook) la
  garantie « une gate ne touche pas le code ». La session principale (`agent_type`
  "claude") et le `builder` (`<plugin>:builder`) ne sont **pas** confines : regles
  de perimetre standard ci-dessous. S'applique meme guard non arme.
- **Builder sous `.legion/`** : le `builder` n'y ecrit QUE son rapport (battle active) :
  `build-report.md` (BUILD agrege) ou `build-report-<slice_id>.md` (GH#129 : un fichier par
  slice, id conforme a `battle_state._ID_RE`, nom lu par `battle_state.slice_report_id`). Sinon `.legion/**` (toujours autorise) lui permettrait de
  reecrire `battle.json` -- donc d'elargir son propre `guard.allow`. Hors `.legion/`,
  regles de perimetre standard. S'applique meme guard non arme.
- **Bloc `guard` invalide** (GH#104 : non-dict, `allow`/`deny` non-listes ou contenant un
  non-`str`, cf. `battle_state.guard_of`) -> perimetre inconnu -> **fail-closed** : toute
  ecriture hors toujours-autorises (`.legion/**`, `.gitignore`, memoire Claude) est bloquee
  (exit 2), quel que soit l'appelant. Evalue APRES le confinement des gates et le blocage du
  builder sous `.legion/` (inchanges). Reparation : `battle_state.py set-guard`.
- **`battle.json` illisible** (JSON invalide, racine non-dict...) : meme fail-closed, mais
  `set-guard`/`close` refusent aussi de lire ce fichier : le message propose donc de le
  retablir (`git checkout`) ou de le corriger a la main (`.legion/**` reste ecrivable), ou de
  vider `.legion/active-battle` pour desactiver la battle.
- **Entree stdin** (GH#106) : vide ou blanche -> exit 0 ; non vide et illisible (JSON invalide,
  entier geant, non decodable, trop imbrique) ou non-objet -> exit 2 (fail-closed, sans bypass
  `LEGION_GUARD_OFF`). Aucun chemin ne sort en exit 1, hors `--self-test`.
- **Bash / PowerShell des gates** (GH#66, couche 2) : le hook est aussi branche sur
  `Bash|PowerShell`. Un appel shell d'une gate (`agent_type` dans `GATE_ARTIFACT`) est analyse
  (`_shell_decision`, best-effort : redirections, `tee`, `rm`/`cp`/`mv`..., `sed -i`, `git`
  d'ecriture, `dotnet format` sans `--verify-no-changes`...) et bloque (exit 2) s'il ecrit hors
  `/dev/null`, du dossier temporaire ou de `.legion/battles/<active>/*.log`. Route AVANT
  `_load_active_guard` : la session principale et le builder ne sont jamais filtres (leur shell
  n'est pas bloque par un bloc `guard` invalide). Repli d'import : gate et builder fermes (exit 2),
  session principale silencieuse. Commande non analysable (quote non fermee) -> exit 2. Ne voit ni
  `python -c`, ni `bash -c`, ni `eval` : la garantie est portee par `artifact_check.py tree-verify`.
- **Checkout principal protege (battle en worktree, GH#152, C8)** : quand la battle active porte
  un bloc `worktree` dont `path` existe, toute ecriture (Edit/Write/MultiEdit) qui tombe sous la
  racine d'etat (le checkout principal), hors de `worktree.path`, hors `.legion/**`, hors
  `.claude/worktrees/**` (autres worktrees, ex. builders isoles) et hors memoire Claude est
  bloquee (exit 2), session dans le worktree ou dans le principal (repli), guard arme ou non,
  quel que soit l'appelant non-gate. Bypass `LEGION_GUARD_OFF=1`. Bloc absent / `null` / chemin
  disparu / `battle.json` illisible : regle sans effet (le fail-closed existant s'applique ensuite).
  Evaluee apres le confinement des gates et la regle `.legion/` du builder, avant `_load_active_guard`.
- file_path doit matcher >= 1 glob de `allow` ET aucun de `deny` -> autorise.
- Hors perimetre -> exit 2 (blocage) avec la battle et les globs autorises.
- Bypass delibere : env var `LEGION_GUARD_OFF=1` (log, ne bloque pas).

**Deux racines** (GH#68) : dans un worktree git lie, le cwd du hook est le worktree mais la
battle vit dans le depot principal. `_decide(data, repo_root, state_root)` separe donc la racine
d'**edition** (`repo_root` = cwd du hook : globs `allow`/`deny` et `.gitignore`) de la racine
d'**etat** (`state_root`, `battle_state.resolve_state_root` : pointeur, `battle.json`, confinement
des gates, regle `.legion/` du builder, `.legion/**` toujours autorise, logs de gate). Hors worktree
les deux racines sont confondues : comportement inchange. Depuis un worktree, le builder ecrit son
rapport (`build-report.md` ou `build-report-<slice_id>.md`) dans `<principal>/.legion/battles/<id>/` (chemin absolu) ; le `.legion/` du
worktree n'est pas de l'etat et reste bloque.

Les globs sont relatifs a la racine d'edition (cwd du hook). `**` matche tout
(slash inclus), `*` matche hors-slash, `?` un caractere hors-slash.

Tests CLI hors Claude Code :
    py guard.py --self-test
    echo '{"tool_name":"Edit","tool_input":{"file_path":"src/x.cs"}}' | py guard.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

WRITE_TOOLS = ("Edit", "Write", "MultiEdit")
SHELL_TOOLS = ("Bash", "PowerShell")
ALWAYS_ALLOW = (".legion/**", ".gitignore")  # etat de la battle + .gitignore (setup orchestrateur)

# Confinement des gates. `agent_type` (payload PreToolUse) vaut le nom NAMESPACE du
# sous-agent appelant (`<plugin>:<agent>`) ; la session principale vaut "claude" et
# le builder "<plugin>:builder" (tous deux hors table -> regles de perimetre standard).
# Chaque gate listee ici ne peut ecrire QUE l'artefact associe, dans le dossier de
# la battle active. Le prefixe de plugin (`legion:`) doit matcher le `name` du
# marketplace ; sur un fork (ex. `divalto-legion`) adapter `PLUGIN_PREFIX`.
#
# Les tables (agent -> artefact) sont derivees de la source unique
# `scripts/battle_state.py` (GH#69), sans copie de repli. Chemin resolu depuis `__file__`
# (jamais le cwd). Si l'import echoue (installation incomplete), le hook ne plante pas
# mais se ferme : cf. `_fallback_decision` ; et son --self-test echoue (exit 1).
PLUGIN_PREFIX = "legion:"

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_IMPORT_ERROR: str | None = None
try:
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    from battle_state import GATE_ARTIFACT as _GATE_ARTIFACT_SRC
    from battle_state import PRODUCER_ARTIFACT as _PRODUCER_ARTIFACT_SRC
    # Lecteurs partages de la battle active (GH#85) : le hook ne lit plus le pointeur lui-meme.
    # `guard_of` : forme valide du bloc `guard` (GH#104), source unique partagee avec `validate`.
    from battle_state import active_battle_id, battles_dir, guard_of, load_active_battle
    # Racine d'etat (GH#68) : depot principal depuis un worktree lie, sinon le cwd.
    from battle_state import resolve_state_root
    # Matcher de globs partage avec `artifact_check.py tree-verify --guard` (GH#66, C6).
    from battle_state import glob_match as _glob_match
    # Nom de rapport par slice (GH#129) : source unique partagee avec `merge-reports`.
    from battle_state import slice_report_id as _slice_report_id
    GATE_ARTIFACT = {PLUGIN_PREFIX + g: a for g, a in _GATE_ARTIFACT_SRC.items()}
    # Producteur : hors `.legion/`, regles de perimetre standard ; SOUS `.legion/`, seul
    # son rapport est autorise (`build-report.md` ou `build-report-<slice_id>.md`, GH#129 ;
    # jamais `battle.json` -> pas d'auto-elargissement du guard).
    PRODUCER_ARTIFACT = {PLUGIN_PREFIX + g: a for g, a in _PRODUCER_ARTIFACT_SRC.items()}
except Exception as _exc:  # ImportError, SyntaxError du module... jamais planter a l'import
    _IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"
    GATE_ARTIFACT = {}
    PRODUCER_ARTIFACT = {}

    def resolve_state_root(cwd):  # repli : pas de resolution (les decisions ferment de toute facon)
        return cwd

    def _slice_report_id(name):  # repli : aucun motif reconnu (les decisions ferment de toute facon)
        return None

    def _glob_match(rel_path, patterns):  # jamais appele hors repli ; ferme si c'etait le cas
        raise RuntimeError("battle_state.glob_match indisponible")


def _matches(rel_path: str, patterns) -> bool:
    """Matcher de globs : delegue a la source unique `battle_state.glob_match` (GH#66, C6)."""
    return _glob_match(rel_path, patterns)


def _load_active_guard(repo_root: Path):
    """Retourne (battle_id, allow, deny, valid, unreadable) de la battle active, ou None.

    `valid` faux (bloc `guard` mal forme, GH#104, ou `battle.json` illisible) : `allow`/`deny`
    valent `[]` et ne doivent pas etre interpretes -- l'appelant ferme (fail-closed).
    `unreadable` vrai (GH#106) : `battle.json` present mais illisible (et non simple bloc
    `guard` invalide) -- seul le message de reparation differe.
    """
    active = load_active_battle(repo_root)
    if active is None:
        # Pointeur valide mais `battle.json` present et illisible (JSON invalide, racine
        # non-dict, trop imbrique...) : perimetre inconnu -> fail-closed. Pointeur absent,
        # vide ou battle disparue : pas de battle active.
        battle_id = active_battle_id(repo_root)
        if battle_id is not None and (battles_dir(repo_root) / battle_id / "battle.json").exists():
            return battle_id, [], [], False, True
        return None
    battle_id, data = active
    guard, valid = guard_of(data)
    if not valid:
        return battle_id, [], [], False, False
    allow = guard.get("allow") or []
    deny = guard.get("deny") or []
    return battle_id, allow, deny, True, False


def _relative(repo_root: Path, file_path: str) -> str | None:
    """Chemin relatif posix au repo, ou None si hors repo / non calculable."""
    try:
        target = Path(file_path)
        if not target.is_absolute():
            target = repo_root / target
        rel = os.path.relpath(target.resolve(), repo_root.resolve())
    except (ValueError, OSError):
        return None
    rel = rel.replace("\\", "/")
    if rel.startswith(".."):
        return None  # hors du repo
    return rel


def _rel_state(repo_root: Path, state_root: Path, file_path: str) -> str | None:
    """Chemin relatif a la racine d'etat d'un `file_path` ancre sur la racine d'edition (GH#130).

    Un chemin relatif designe un fichier sous le cwd du hook (le worktree) : l'ancrer sur
    `state_root` ferait passer `<wt>/.legion/...` pour `<principal>/.legion/...`.
    """
    target = Path(file_path)
    if not target.is_absolute():
        target = repo_root / target
    return _relative(state_root, str(target))


def _always_allowed(rel_edit: str | None, rel_state: str | None) -> bool:
    """Toujours autorises (`ALWAYS_ALLOW`) : `.legion/**` relatif a la racine d'**etat**, `.gitignore`
    relatif a la racine d'**edition** (GH#68, C5-A). Racines confondues : `_matches(rel, ALWAYS_ALLOW)`."""
    if rel_state is not None and _matches(rel_state, (ALWAYS_ALLOW[0],)):
        return True
    return rel_edit is not None and _matches(rel_edit, (ALWAYS_ALLOW[1],))


def _is_claude_memory(file_path: str) -> bool:
    """True si file_path vise la memoire projet de Claude (exempte du guard).

    **Ancre sur le home reel** (`~/.claude/projects/<slug>/memory/...`). Un prefixe
    wildcard (`**/.claude/.../memory/**`) exempterait n'importe quel chemin se
    *terminant* par ce motif — y compris un `<repo>/.../.claude/projects/x/memory/x`
    fabrique dans le repo — et trouerait le guard. `relative_to` leve si le chemin
    n'est pas reellement sous le home, ce qui ferme le contournement.

    Exemption ciblee (pas tout `~/.claude/**`) : un builder qui derape ne touche ni
    `settings.json` ni une autre infra Claude, seulement le dossier memoire.
    """
    try:
        abs_path = Path(file_path).expanduser().resolve()
        rel = abs_path.relative_to(Path.home().resolve() / ".claude" / "projects")
    except (ValueError, OSError):
        return False
    parts = rel.parts  # attendu : <slug>/memory/<...>
    return len(parts) >= 3 and parts[1] == "memory"


def _gate_decision(agent_type, rel: str | None, battle_id: str | None):
    """Decision de confinement pour un sous-agent gate (fonction pure, testable).

    - None  -> `agent_type` n'est pas une gate : appliquer les regles standard.
    - True  -> ecriture AUTORISEE (l'unique artefact de la gate, battle active).
    - False -> ecriture BLOQUEE (autre fichier, hors battle, ou chemin hors repo).
    """
    artifact = GATE_ARTIFACT.get(agent_type)
    if artifact is None:
        return None
    if battle_id is None or rel is None:
        return False
    return rel == f".legion/battles/{battle_id}/{artifact}"


def _touches_legion_state(parts) -> bool:
    """True si un composant du chemin (resolu) est un dossier `.legion`, sans tenir
    compte de la casse (APFS / dossiers WSL insensibles : `.LEGION` == `.legion`)."""
    return any(str(p).casefold() == ".legion" for p in parts)


def _producer_state_decision(agent_type, rel: str | None, battle_id: str | None, parts=()):
    """Decision pour un producteur (builder) visant un etat `.legion/` (fonction pure).

    `parts` = composants du chemin **absolu resolu** : un `.legion/` hors de la racine
    du hook (ex. checkout principal vu depuis un worktree, `rel is None`) reste de
    l'etat de battle, donc bloque.

    - None  -> pas un producteur, ou cible hors de tout `.legion/` : regles standard.
    - True  -> son rapport dans la battle active : autorise (`build-report.md`, ou
      `build-report-<slice_id>.md` dont l'id passe `battle_state.slice_report_id`, GH#129).
    - False -> tout autre chemin sous un `.legion/` (`battle.json`, artefact de gate…).
    """
    artifact = PRODUCER_ARTIFACT.get(agent_type)
    if artifact is None:
        return None
    in_repo_state = rel is not None and _touches_legion_state(rel.split("/"))
    if not (in_repo_state or _touches_legion_state(parts)):
        return None
    if battle_id is None or rel is None:
        return False
    prefix = f".legion/battles/{battle_id}/".casefold()
    folded = rel.casefold()
    if folded == prefix + artifact.casefold():
        return True
    if not folded.startswith(prefix):
        return False
    return _slice_report_id(rel[len(prefix):]) is not None


def _resolved_parts(repo_root: Path, file_path: str) -> tuple[str, ...]:
    """Composants du chemin absolu resolu (vide si non calculable)."""
    try:
        target = Path(file_path)
        if not target.is_absolute():
            target = repo_root / target
        return target.resolve().parts
    except (ValueError, OSError):
        return ()


def _fallback_decision(data: dict, repo_root: Path) -> tuple[int, str] | None:
    """Repli quand la source unique est introuvable (C1, option B) : fermer, sans copie.

    - `<plugin>:builder` : bloque pour TOUTE ecriture (le perimetre `/freeze` ne peut plus
      etre evalue sans lecteur de la battle active) ;
    - tout autre `<plugin>:*` (gates) : bloque ;
    - session principale et autres agents : None (le perimetre n'est pas applique,
      cf. l'avertissement de `_decide`).
    """
    agent_type = data.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type.startswith(PLUGIN_PREFIX):
        return None
    detail = "installation legion incomplete (scripts/battle_state.py introuvable)"
    if agent_type == PLUGIN_PREFIX + "builder":
        return 2, (
            f"BLOQUE : {detail}.\nLe `{agent_type}` ne peut rien ecrire tant que "
            f"l'installation n'est pas reparee ({_IMPORT_ERROR})."
        )
    return 2, (
        f"BLOQUE : {detail}.\nLa gate `{agent_type}` ne peut pas ecrire tant que "
        f"l'installation n'est pas reparee ({_IMPORT_ERROR})."
    )


def _is_blank_content(content) -> bool:
    """True si le contenu d'un `Write` est absent ou entierement blanc (artefact 0 octet)."""
    return content is None or not str(content).strip()


def _invalid_guard_decision(
    data: dict, repo_root: Path, battle_id: str, file_path: str, unreadable: bool = False,
    state_root: Path | None = None,
) -> tuple[int, str]:
    """Etat de guard invalide ou illisible : perimetre inconnu -> ferme hors toujours-autorises (GH#104)."""
    if not file_path or _is_claude_memory(file_path):
        return 0, ""
    rel = _relative(repo_root, file_path)
    rel_state = _rel_state(repo_root, state_root or repo_root, file_path)
    if _always_allowed(rel, rel_state):
        return 0, ""  # `.legion/**` (racine d'etat) + `.gitignore` : la reparation reste possible
    target = f"`{rel}`" if rel is not None else "un chemin hors du repo"
    if unreadable:
        # `set-guard`/`close` lisent `battle.json` : ils refusent un fichier illisible.
        return 2, (
            f"BLOQUE par le guard de la battle {battle_id} : `.legion/battles/{battle_id}/battle.json` "
            f"est illisible, le perimetre d'ecriture est inconnu.\n"
            f"Ecriture refusee : {target}.\n"
            f"Retablis-le (`git checkout -- .legion/battles/{battle_id}/battle.json` ou restauration) "
            f"ou corrige-le a la main (`.legion/**` reste modifiable), ou vide `.legion/active-battle` "
            f"pour desactiver la battle.\n"
            f"Bypass delibere : LEGION_GUARD_OFF=1"
        )
    return 2, (
        f"BLOQUE par le guard de la battle {battle_id} : le bloc `guard` de `battle.json` "
        f"est invalide, le perimetre d'ecriture est inconnu.\n"
        f"Ecriture refusee : {target}.\n"
        f"Repare-le : `python3 {_SCRIPTS_DIR / 'battle_state.py'} set-guard --allow <glob...>` "
        f"(`--allow` sans glob pour desarmer), ou ferme la battle "
        f"(`python3 {_SCRIPTS_DIR / 'battle_state.py'} close`).\n"
        f"Bypass delibere : LEGION_GUARD_OFF=1"
    )


# --- Filtre Bash/PowerShell des gates (GH#66, couche 2) -------------------------------
#
# Best-effort : une gate ne doit pas ecrire par shell. Le filtre ne regarde que les
# commandes EN TETE (jamais une sous-chaine) ; il ne voit ni `python -c "open(...)"`, ni
# `bash -c "..."`, ni `eval`. La garantie est portee par `artifact_check.py tree-verify`.

_FILE_CMDS = frozenset({"rm", "mv", "cp", "touch", "mkdir", "chmod", "truncate", "ln"})
_GIT_BLOCKED = frozenset({
    "add", "commit", "checkout", "switch", "reset", "restore", "am", "merge", "rebase",
    "cherry-pick", "clean", "push", "rm", "mv", "update-index", "pull", "revert",
    "update-ref", "replace", "filter-branch", "gc", "prune",
    "commit-tree", "mktree", "notes", "maintenance",   # briques d'ecriture d'etat
})
# `git config` : seules les formes de lecture passent (une gate ne pose pas core.fsmonitor & co).
# `--show-origin` / `--show-scope` / `--name-only` sont de simples modificateurs : git 2.53 ecrit
# avec `--show-scope <cle> <valeur>`. Ils ne rendent donc pas une commande « lecture » a eux seuls.
_GIT_CONFIG_READ = ("--get", "--list", "-l")
_GIT_CONFIG_SUBCMD_WRITE = frozenset({"set", "unset", "edit", "rename-section", "remove-section"})
_GIT_CONFIG_WRITE = ("--unset", "--add", "--replace-all", "--edit", "-e", "--rename-section",
                     "--remove-section", "--set")
_GIT_BRANCH_WRITE = frozenset({"-f", "--force", "-d", "-D", "--delete", "-m", "-M", "--move",
                               "-c", "-C", "--copy", "-u", "--set-upstream-to",
                               "--unset-upstream", "--edit-description"})
_GIT_TAG_WRITE = frozenset({"-f", "--force", "-d", "--delete", "-a", "-s", "-u", "-m", "-F"})
_GIT_OPT_ARG = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--super-prefix", "--config-env",
})
_GIT_WORKTREE_BLOCKED = frozenset({"add", "remove", "move", "prune", "repair"})
_GIT_APPLY_READONLY = frozenset({"--check", "--stat", "--numstat", "--summary"})
_DOTNET_BLOCKED = frozenset({"new", "add", "remove"})
_PKG_MANAGERS = frozenset({"npm", "pnpm", "yarn"})
_PKG_BLOCKED = frozenset({"install", "i", "add", "ci"})
_PS_CMDLETS = frozenset(c.casefold() for c in (
    "Set-Content", "Add-Content", "Out-File", "New-Item", "Remove-Item", "Move-Item",
    "Copy-Item", "Rename-Item", "Clear-Content", "Tee-Object", "Export-Csv", "Export-Clixml",
    "Set-Item", "Set-ItemProperty", "New-ItemProperty", "Remove-ItemProperty",
    "sc", "ac", "ni", "ri", "del", "erase", "rd", "mi", "move", "cpi", "copy", "ren",
))
_PS_IO_FILE = re.compile(
    r"\[(?:System\.)?IO\.File\]::(?:Write|Append|Create|Delete|Move|Copy|Replace)", re.IGNORECASE
)
# Prefixes de commande : le nom reel de la commande suit. Valeur = options qui prennent un argument.
_PREFIXES = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-r", "-t", "-T", "-U", "-D"}),
    "doas": frozenset({"-u"}),
    "env": frozenset({"-u", "-C", "-S"}),
    "nice": frozenset({"-n"}),
    "timeout": frozenset({"-s", "-k"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "xargs": frozenset({"-I", "-n", "-P", "-d", "-L", "-s", "-a", "-E", "-l"}),
    "command": frozenset(), "exec": frozenset(), "nohup": frozenset(),
    "time": frozenset(), "builtin": frozenset(),
    # mots-cles de shell : la commande suit
    "{": frozenset(), "}": frozenset(), "!": frozenset(), "if": frozenset(),
    "then": frozenset(), "else": frozenset(), "elif": frozenset(), "do": frozenset(),
    "while": frozenset(), "until": frozenset(),
}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
_REDIR_OUT = frozenset({">", ">>", ">|", "&>", "&>>", ">&"})
_REDIR_IN = frozenset({"<", "<<", "<<<", "<&"})
_HEREDOC_OP = re.compile(
    r"<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|(\\)?([A-Za-z_][A-Za-z0-9_]*))"
)
_MAX_SUBST_DEPTH = 6


def _skip_arith(line: str, i: int) -> int:
    """`i` sur `$((` ou `((` : index apres la `))` fermante de la ligne (fin de ligne si non fermee).
    Un `<<` y est un decalage arithmetique, pas un heredoc."""
    depth, j, n = 0, i, len(line)
    while j < n:
        if line[j] == "(":
            depth += 1
        elif line[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def _find_heredocs(line: str) -> list[tuple[str, bool, bool]]:
    """Operateurs `<<[-]WORD` d'une ligne, hors quotes et hors `$((...))` :
    liste de (delimiteur, retire_tabs, quote). `quote` : delimiteur quote (`'EOF'`, `"EOF"`,
    `\\EOF`) -> corps litteral ; sinon `$(...)` et backticks s'y executent."""
    found: list[tuple[str, bool, bool]] = []
    quote = None
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
        elif c == "\\":
            i += 2
        elif c in "'\"":
            quote = c
            i += 1
        elif c == "#" and (i == 0 or line[i - 1] in " \t;|&("):
            break
        elif line.startswith("$((", i) or (
            line.startswith("((", i) and (i == 0 or line[i - 1] in " \t;|&")
        ):
            i = _skip_arith(line, i + 1 if line.startswith("$((", i) else i)
        elif line.startswith("<<<", i):
            i += 3
        elif line.startswith("<<", i):
            m = _HEREDOC_OP.match(line, i)
            if m:
                delim = m.group(2) or m.group(3) or m.group(5)
                found.append((delim, m.group(1) == "-", bool(m.group(2) or m.group(3) or m.group(4))))
                i = m.end()
            else:
                i += 2
        else:
            i += 1
    return found


def _body_substitutions(body: str) -> list[str]:
    """Contenus des `$(...)` / backticks d'un corps de heredoc NON quote (bash les execute).
    `\\$` et `\\`` sont litteraux. Leve ValueError si une substitution n'est pas fermee."""
    subs: list[str] = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            i += 2
        elif c == "$" and body.startswith("$(", i):
            inner, i = _match_paren(body, i + 2)
            subs.append(inner)
        elif c == "`":
            inner, i = _match_backtick(body, i + 1)
            subs.append(inner)
        else:
            i += 1
    return subs


def _strip_heredocs(command: str) -> str:
    """Retire les corps de heredoc (`<<[-]'WORD'` ... `WORD`) : pour un delimiteur quote ce n'est
    pas du shell. Pour un delimiteur NON quote, les `$(...)` / backticks du corps s'executent :
    ils sont reinjectes comme lignes `$(...)` a analyser.

    Le contenu d'un `python - <<'EOF' ... a > b ... EOF` ne doit pas faire lire un `>` comme
    une redirection. Heredoc non termine : le reste de la commande est le corps (comme bash).
    """
    out: list[str] = []
    pending: list[tuple[str, bool, bool]] = []
    body: list[str] = []
    for line in command.split("\n"):
        if pending:
            delim, tabs, quoted = pending[0]
            if (line.lstrip("\t") if tabs else line).rstrip("\r") == delim:
                pending.pop(0)
                if not quoted:
                    out.extend("$(" + sub + ")" for sub in _body_substitutions("\n".join(body)))
                body = []
            else:
                body.append(line)
            continue
        out.append(line)
        pending.extend(_find_heredocs(line))
    if pending and not pending[0][2]:  # heredoc non quote non termine : corps jusqu'a la fin
        out.extend("$(" + sub + ")" for sub in _body_substitutions("\n".join(body)))
    return "\n".join(out)


def _match_paren(cmd: str, i: int) -> tuple[str, int]:
    """`i` = index apres `$(` : retourne (contenu, index apres la `)` fermante)."""
    depth, j, n = 1, i, len(cmd)
    while j < n:
        c = cmd[j]
        if c == "\\":
            j += 2
            continue
        if c in "'\"":
            k = cmd.find(c, j + 1)
            if k < 0:
                raise ValueError("quote non fermee dans une substitution")
            j = k + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return cmd[i:j], j + 1
        j += 1
    raise ValueError("substitution `$(` non fermee")


def _match_backtick(cmd: str, i: int) -> tuple[str, int]:
    """`i` = index apres la backtick ouvrante : retourne (contenu, index apres la fermante)."""
    j, n = i, len(cmd)
    while j < n:
        if cmd[j] == "\\":
            j += 2
        elif cmd[j] == "`":
            return cmd[i:j], j + 1
        else:
            j += 1
    raise ValueError("substitution backtick non fermee")


def _tokenize(cmd: str, ps: bool, subs: list[str]) -> list[tuple[str, str]]:
    """Jetons ("w", mot) / ("op", operateur), quotes respectees. Leve ValueError si non analysable.

    Un operateur quote (`grep ">" f`) reste un MOT. Les substitutions `$(...)` / backticks
    sont extraites dans `subs` (analysees a part) et remplacees par `$SUB` dans le mot.
    Les sauts de ligne hors quotes valent `;`. Les commentaires (`#` en debut de mot) sont ignores.
    """
    toks: list[tuple[str, str]] = []
    buf: list[str] = []
    has = quoted = False
    i, n = 0, len(cmd)

    def flush() -> None:
        nonlocal has, quoted
        if has:
            toks.append(("w", "".join(buf)))
        buf.clear()
        has = quoted = False

    while i < n:
        c = cmd[i]
        if c in " \t\r":
            flush()
            i += 1
        elif c == "\n":
            flush()
            toks.append(("op", ";"))
            i += 1
        elif c == "#" and not has:
            j = cmd.find("\n", i)
            i = n if j < 0 else j
        elif c == "\\" and not ps:
            buf.append(cmd[i + 1] if i + 1 < n else "\\")
            has = quoted = True
            i += 2
        elif c == "`" and ps:  # echappement PowerShell : ``Set-Con`tent`` == Set-Content
            if i + 1 < n and cmd[i + 1] == "\n":
                i += 2  # continuation de ligne
            else:
                buf.append(cmd[i + 1] if i + 1 < n else "`")
                has = quoted = True
                i += 2
        elif c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                raise ValueError("quote simple non fermee")
            buf.append(cmd[i + 1 : j])
            has = quoted = True
            i = j + 1
        elif c == '"':
            has = quoted = True
            i += 1
            while True:
                if i >= n:
                    raise ValueError("guillemet non ferme")
                ch = cmd[i]
                if ch == '"':
                    i += 1
                    break
                if ch == "\\" and not ps:
                    if i + 1 < n and cmd[i + 1] in '"\\$`\n':
                        buf.append(cmd[i + 1])
                        i += 2
                    else:
                        buf.append("\\")
                        i += 1
                elif ch == "`" and ps:
                    if i + 1 >= n:
                        raise ValueError("guillemet non ferme")
                    buf.append(cmd[i + 1])
                    i += 2
                elif ch == "$" and cmd.startswith("$(", i):
                    inner, i = _match_paren(cmd, i + 2)
                    subs.append(inner)
                    buf.append("$SUB")
                elif ch == "`":
                    inner, i = _match_backtick(cmd, i + 1)
                    subs.append(inner)
                    buf.append("$SUB")
                else:
                    buf.append(ch)
                    i += 1
        elif c == "$" and cmd.startswith("$(", i):
            inner, i = _match_paren(cmd, i + 2)
            subs.append(inner)
            buf.append("$SUB")
            has = True
        elif c == "`" and not ps:
            inner, i = _match_backtick(cmd, i + 1)
            subs.append(inner)
            buf.append("$SUB")
            has = True
        elif c in ";|&()<>":
            if c in "<>" and has and not quoted and "".join(buf).isdigit():
                buf.clear()  # descripteur de fichier devant la redirection (`2>`)
                has = quoted = False
            else:
                flush()
            if c == ">":
                op = next((o for o in (">>", ">|", ">&") if cmd.startswith(o, i)), ">")
            elif c == "<":
                op = next((o for o in ("<<<", "<<", "<&") if cmd.startswith(o, i)), "<")
            elif c == "&":
                op = next((o for o in ("&&", "&>>", "&>") if cmd.startswith(o, i)), "&")
            elif c == "|":
                op = next((o for o in ("||", "|&") if cmd.startswith(o, i)), "|")
            else:
                op = c
            toks.append(("op", op))
            i += len(op)
        else:
            buf.append(c)
            has = True
            i += 1
    flush()
    return toks


def _split_simple_commands(command: str, ps: bool = False):
    """Decoupe une commande en commandes simples. Retourne (commandes, substitutions).

    Chaque commande = (mots, redirections) ; une redirection = (operateur, cible). Les
    duplications de fd (`2>&1`, `>&2`) et les redirections d'entree sont ignorees ;
    `>(...)` (substitution de processus) n'est pas une cible fichier.
    """
    if not ps:
        command = _strip_heredocs(command)
        command = command.replace("\\\r\n", "").replace("\\\n", "")
    subs: list[str] = []
    toks = _tokenize(command, ps, subs)
    commands: list[tuple[list[str], list[tuple[str, str]]]] = []
    words: list[str] = []
    redirs: list[tuple[str, str]] = []

    def push() -> None:
        nonlocal words, redirs
        if words or redirs:
            commands.append((words, redirs))
        words, redirs = [], []

    i = 0
    while i < len(toks):
        kind, text = toks[i]
        if kind == "w" and text in ("{", "}"):
            push()  # accolade de groupe / de scriptblock (`ForEach-Object { rm $_ }`)
        elif kind == "w":
            words.append(text)
        elif text in _REDIR_OUT or text in _REDIR_IN:
            if i + 1 < len(toks) and toks[i + 1][0] == "w":
                target = toks[i + 1][1]
                i += 1
                if text in _REDIR_OUT and not (
                    text == ">&" and (target == "-" or target.isdigit())
                ):
                    redirs.append((text, target))
        else:
            push()
        i += 1
    push()
    return commands, subs


def _tmp_roots() -> list[Path]:
    roots: list[Path] = []
    for raw in (tempfile.gettempdir(), os.environ.get("TMPDIR"), "/tmp"):
        if raw:
            try:
                r = Path(raw).resolve()
            except (OSError, ValueError):
                continue
            if r not in roots and len(r.parts) > 1:
                roots.append(r)
    return roots


def _allowed_target(
    target: str, root: Path, battle_id: str | None, ps: bool = False, state_root: Path | None = None
) -> bool:
    """True si une gate peut ecrire ici (C2-B) : `/dev/null` (`$null` en PowerShell), un
    `*.log` directement dans `.legion/battles/<active>/` (hors `ci-failed-*`), ou un chemin
    absolu sous le dossier temporaire du systeme, hors du depot. Cible non resolue
    (`$`, backtick, glob, `~`) -> False.

    `root` = racine d'edition (cwd du hook) ; `state_root` = racine d'etat (depot principal depuis
    un worktree, GH#68), par defaut `root` : les `*.log` autorises vivent sous
    `state_root/.legion/battles/<id>/`, et une cible absolue sous l'une OU l'autre racine est interdite."""
    if target == "/dev/null" or (ps and target.casefold() == "$null"):
        return True
    if not target or any(ch in target for ch in "$`*?{") or target.startswith("~"):
        return False
    try:
        raw = Path(target)
        resolved = (raw if raw.is_absolute() else root / raw).resolve()
        root_res = root.resolve()
        state_res = (state_root if state_root is not None else root).resolve()
    except (OSError, ValueError):
        return False
    if battle_id:
        battle_dir = state_res / ".legion" / "battles" / battle_id
        name = resolved.name.casefold()
        if (
            resolved.parent == battle_dir
            and name.endswith(".log")
            and not name.startswith("ci-failed-")
        ):
            return True
    if Path(target).is_absolute():
        for base in (root_res, state_res):
            try:
                resolved.relative_to(base)
                return False  # sous le depot (edition ou etat)
            except ValueError:
                pass
        for tmp in _tmp_roots():
            try:
                rel = resolved.relative_to(tmp)
            except ValueError:
                continue
            if rel.parts:  # pas le dossier temporaire lui-meme
                return True
    return False


def _non_options(args: list[str]) -> list[str]:
    out: list[str] = []
    rest = False
    for a in args:
        if rest:
            out.append(a)
        elif a == "--":
            rest = True
        elif not (a.startswith("-") and len(a) > 1):
            out.append(a)
    return out


def _flag_block_has(arg: str, flag: str, stops: str) -> bool:
    """True si `arg` est un bloc de flags courts (`-pi`, `-Ei`, `-i.bak`) contenant `flag`,
    avant une option qui prend le reste du jeton comme argument (`stops`)."""
    if not arg.startswith("-") or arg.startswith("--"):
        return False
    for ch in arg[1:]:
        if ch == flag:
            return True
        if ch in stops or not (ch.isalnum()):
            return False
    return False


def _git_config_writes(rest: list[str]) -> bool:
    """True si `git config ...` ecrit (config locale : fsmonitor, hooksPath, excludesFile...).
    Formes de lecture : `--get*`, `--list`/`-l`, une seule cle (`--show-*` ne suffit pas)."""
    if rest and rest[0] in _GIT_CONFIG_SUBCMD_WRITE:
        return True
    if any(a.startswith(_GIT_CONFIG_WRITE) for a in rest):
        return True
    if any(a.startswith(_GIT_CONFIG_READ) for a in rest):
        return False
    return len(_non_options(rest)) >= 2  # `git config <cle> <valeur>`


def _git_subcommand(args: list[str]) -> tuple[str | None, list[str]]:
    i = 0
    while i < len(args):
        a = args[i]
        if a in _GIT_OPT_ARG:
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return a, args[i + 1 :]
    return None, []


def _command_name(word: str, ps: bool) -> str:
    """Nom de commande : basename, sans `.exe` ; en PowerShell, insensible a la casse."""
    name = os.path.basename(word.replace("\\", "/"))
    if name.casefold().endswith(".exe"):
        name = name[:-4]
    return name.casefold() if ps else name


def _tar_extracts(args: list[str]) -> bool:
    for k, a in enumerate(args):
        if a in ("--extract", "--get") or a.startswith("--to-command"):
            return True
        if a.startswith("--"):
            continue
        if a.startswith("-") and "x" in a[1:].split("=")[0] and len(a) > 1:
            return True
        if k == 0 and not a.startswith("-") and a.isalpha() and "x" in a:
            return True  # forme ancienne : `tar xzf a.tgz`
    return False


_SED_WRITE = re.compile(r"(?:/[gpIiMmeE0-9]*w[ \t]*\S)|(?:(?:^|[;{}\n])[ \t]*(?:\d+|\$|/[^/\n]*/)?[ \t]*w[ \t]+\S)")


def _argv_hits(
    words: list[str], ps: bool, root: Path, battle_id: str | None, state_root: Path | None = None
) -> list[str]:
    """Motifs d'ecriture d'une commande simple (mots deja decoupes)."""
    hits: list[str] = []
    i, via_xargs = 0, False
    while i < len(words):
        w = words[i]
        if _ASSIGN.match(w):
            i += 1
            continue
        key = _command_name(w, ps)   # basename : `/usr/bin/env rm x` == `env rm x`
        if key not in _PREFIXES:
            break
        via_xargs = via_xargs or key == "xargs"
        takes = _PREFIXES[key]
        i += 1
        while i < len(words) and words[i].startswith("-") and len(words[i]) > 1:
            i += 2 if words[i] in takes else 1
        if key == "timeout" and i < len(words):
            i += 1  # duree
    if i >= len(words):
        return hits
    name = _command_name(words[i], ps)
    args = words[i + 1 :]

    def all_allowed(items: list[str]) -> bool:
        return all(_allowed_target(a, root, battle_id, ps, state_root) for a in items)

    if name in _FILE_CMDS:
        nonopt = _non_options(args)
        if via_xargs or (nonopt and not all_allowed(nonopt)):
            hits.append(name)
    elif name == "tee":
        nonopt = _non_options(args)
        if nonopt and not all_allowed(nonopt):
            hits.append("tee")
    elif name == "sed":
        if any(a.startswith("--in-place") or _flag_block_has(a, "i", "efl") for a in args):
            hits.append("sed -i")
        elif any(_SED_WRITE.search(a) for a in _non_options(args)):
            hits.append("sed w")
    elif name == "dd":
        if any(a.startswith("of=") and not _allowed_target(a[3:], root, battle_id, ps, state_root)
               for a in args):
            hits.append("dd of=")
    elif name in ("install", "rsync", "patch"):
        if not any(a in ("--dry-run", "-n", "--list-only") for a in args):
            hits.append(name)
    elif name == "unzip":
        if not any(a in ("-l", "-t", "-p", "-Z", "-v") for a in args):
            hits.append("unzip")
    elif name in ("tar", "bsdtar"):
        if _tar_extracts(args):
            hits.append("tar -x")
    elif name == "curl":
        for k, a in enumerate(args):
            tgt = None
            if a in ("-o", "--output") and k + 1 < len(args):
                tgt = args[k + 1]
            elif a.startswith("--output="):
                tgt = a[9:]
            elif a in ("-O", "--remote-name", "--remote-name-all", "-J"):
                tgt = ""
            if tgt is not None and not _allowed_target(tgt, root, battle_id, ps, state_root):
                hits.append("curl -o")
                break
    elif name == "wget":
        tgt = None
        for k, a in enumerate(args):
            if a in ("-O", "--output-document") and k + 1 < len(args):
                tgt = args[k + 1]
            elif a.startswith("--output-document="):
                tgt = a[len("--output-document="):]
        if "--spider" not in args and (tgt is None or (
                tgt != "-" and not _allowed_target(tgt, root, battle_id, ps, state_root))):
            hits.append("wget")
    elif name == "perl":
        if any(_flag_block_has(a, "i", "eEMmIxdDCVF") for a in args):
            hits.append("perl -i")
    elif name == "git":
        sub, rest = _git_subcommand(args)
        if sub in _GIT_BLOCKED:
            hits.append(f"git {sub}")
        elif sub == "config" and _git_config_writes(rest):
            hits.append("git config")
        elif sub == "branch" and any(a in _GIT_BRANCH_WRITE or a.startswith("--set-upstream")
                                     for a in rest):
            hits.append("git branch")
        elif sub == "tag" and any(a in _GIT_TAG_WRITE for a in rest):
            hits.append("git tag")
        elif sub == "hash-object" and any(a == "-w" or (a.startswith("-") and not a.startswith("--")
                                                        and "w" in a[1:]) for a in rest):
            hits.append("git hash-object -w")
        elif sub == "stash" and (not rest or rest[0] not in ("list", "show")):
            hits.append("git stash")
        elif sub == "apply" and not any(a in _GIT_APPLY_READONLY for a in rest):
            hits.append("git apply")
        elif sub == "worktree" and rest and rest[0] in _GIT_WORKTREE_BLOCKED:
            hits.append(f"git worktree {rest[0]}")
    elif name == "dotnet":
        nonopt = _non_options(args)
        sub = nonopt[0] if nonopt else None
        if sub == "format" and not any(a.startswith("--verify-no-changes") for a in args):
            hits.append("dotnet format sans --verify-no-changes")
        elif sub in _DOTNET_BLOCKED:
            hits.append(f"dotnet {sub}")
    elif name in _PKG_MANAGERS:
        nonopt = _non_options(args)
        if nonopt and nonopt[0] in _PKG_BLOCKED:
            hits.append(f"{name} {nonopt[0]}")
    elif re.fullmatch(r"pip[0-9.]*", name):
        nonopt = _non_options(args)
        if nonopt and nonopt[0] == "install":
            hits.append("pip install")
    elif re.fullmatch(r"(python|py)[0-9.]*", name):
        for k, a in enumerate(args[:-1]):
            if a == "-m" and args[k + 1] == "pip" and "install" in args[k + 2 :]:
                hits.append("pip install")
                break
    elif name == "find":
        if "-delete" in args:
            hits.append("find -delete")
        for k, a in enumerate(args):
            if a in ("-exec", "-execdir", "-ok", "-okdir"):
                sub_words: list[str] = []
                for b in args[k + 1 :]:
                    if b in (";", "+"):
                        break
                    sub_words.append(b)
                hits.extend(_argv_hits(sub_words, ps, root, battle_id, state_root))
    if ps and name in _PS_CMDLETS:
        hits.append(words[i])
    if ps and any(a.casefold() in ("-outfile", "-filepath", "-literalpath") for a in args) \
            and name not in _PS_CMDLETS and name in ("invoke-webrequest", "iwr", "invoke-restmethod",
                                                    "irm", "curl", "wget"):
        hits.append(words[i] + " -OutFile")
    return hits


def _command_hits(
    command: str, ps: bool, root: Path, battle_id: str | None, depth: int = 0,
    state_root: Path | None = None,
) -> list[str]:
    """Libelles des motifs d'ecriture trouves dans `command` (liste vide = rien a signaler).

    Leve `ValueError` si la commande n'est pas analysable (quote non fermee...) : l'appelant ferme.
    """
    if depth > _MAX_SUBST_DEPTH:
        raise ValueError("substitutions trop imbriquees")
    hits: list[str] = []
    commands, subs = _split_simple_commands(command, ps)
    for words, redirs in commands:
        for op, target in redirs:
            if not _allowed_target(target, root, battle_id, ps, state_root):
                hits.append(f"redirection `{op} {target}`")
        hits.extend(_argv_hits(words, ps, root, battle_id, state_root))
    for sub in subs:
        hits.extend(_command_hits(sub, ps, root, battle_id, depth + 1, state_root))
    return hits


def _shell_write_hits(
    command: str, shell: str, root: Path, battle_id: str | None, state_root: Path | None = None
) -> list[str]:
    """Motifs d'ecriture (dedoublonnes, ordre stable) d'une commande `Bash` / `PowerShell`."""
    ps = shell == "PowerShell"
    hits = _command_hits(command, ps, root, battle_id, 0, state_root)
    if ps and _PS_IO_FILE.search(command):
        hits.append("[IO.File]::Write*")
    return list(dict.fromkeys(hits))


def _shell_decision(data: dict, repo_root: Path, state_root: Path | None = None) -> tuple[int, str]:
    """Decision pour un appel `Bash` / `PowerShell` (routee AVANT toute lecture du guard).

    Seules les gates sont filtrees : la session principale et le builder ne passent jamais
    par `_load_active_guard` (un bloc `guard` invalide ne doit pas bloquer leur shell, cf.
    la reparation `set-guard`) ; leur perimetre d'ecriture est controle par l'empreinte de l'arbre.
    `state_root` : racine de l'etat `.legion/` (defaut `repo_root`), cf. `_decide`.
    """
    if _IMPORT_ERROR is not None:
        fallback = _fallback_decision(data, repo_root)
        return fallback if fallback is not None else (0, "")  # session principale : silencieux
    agent_type = data.get("agent_type")
    if agent_type not in GATE_ARTIFACT:
        return 0, ""
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return 2, (
            f"BLOQUE : la gate `{agent_type}` a lance un appel shell sans commande lisible "
            f"(fail-closed)."
        )
    state = state_root or repo_root
    battle_id = active_battle_id(state)
    try:
        hits = _shell_write_hits(command, str(data.get("tool_name")), repo_root, battle_id, state)
    except ValueError as exc:
        return 2, (
            f"BLOQUE : la gate `{agent_type}` a lance une commande non analysable ({exc}). "
            f"Simplifie-la (une gate n'ecrit pas par shell)."
        )
    if not hits:
        return 0, ""
    shown = command if len(command) <= 200 else command[:200] + "..."
    return 2, (
        f"BLOQUE : la gate `{agent_type}` ne peut pas ecrire par shell "
        f"(motif : {'; '.join(hits)}).\nCommande : `{shown}`\n"
        f"Une ecriture hors de son artefact fait PERDRE le verdict (battle.md §E, faute de gate).\n"
        f"Cibles autorisees pour un log : /dev/null, le dossier temporaire du systeme (hors depot), "
        f"`.legion/battles/<id>/*.log`."
    )


def _worktree_main_decision(
    repo_root: Path, state: Path, file_path: str
) -> tuple[int, str] | None:
    """Regle C8 (GH#152) : en mode worktree, le checkout principal est ferme a l'ecriture.

    None -> la regle ne s'applique pas (pas de battle active lisible, pas de bloc `worktree`,
    chemin disparu, cible hors du principal, dans le worktree, sous `.legion/**`, sous
    `.claude/worktrees/**` ou memoire Claude). (2, message) -> ecriture bloquee.
    """
    if not file_path:
        return None
    try:
        active = load_active_battle(state)
        if active is None:
            return None
        battle_id, data = active
        block = data.get("worktree") if isinstance(data, dict) else None
        wt_path = block.get("path") if isinstance(block, dict) else None
        if not isinstance(wt_path, str) or not wt_path.strip() or not Path(wt_path).exists():
            return None
        if _is_claude_memory(file_path):
            return None
        target = Path(file_path)
        if not target.is_absolute():
            target = repo_root / target
        rel_main = _relative(state, str(target))
        if rel_main is None:
            return None  # hors du checkout principal
        if _relative(Path(wt_path), str(target)) is not None:
            return None  # dans le worktree de la battle
        if _matches(rel_main, (ALWAYS_ALLOW[0],)) or _matches(rel_main, (".claude/worktrees/**",)):
            return None
    except Exception:  # lecture impossible : le fail-closed existant s'applique ensuite
        return None
    return 2, (
        f"BLOQUE : la battle {battle_id} travaille dans le worktree `{wt_path}` ; "
        f"le checkout principal (`{state}`) est protege.\n"
        f"Tentative : `{rel_main}`. Ecris dans le worktree (relance `claude` depuis `{wt_path}` "
        f"si la session est restee dans le principal).\n"
        f"Bypass delibere : LEGION_GUARD_OFF=1"
    )


def _decide(data: dict, repo_root: Path, state_root: Path | None = None) -> tuple[int, str]:
    """Retourne (exit_code, message). exit 2 = blocage.

    Deux racines (GH#68) : `repo_root` = racine d'**edition** (cwd du hook, le worktree),
    qui sert aux globs `allow`/`deny` et a `.gitignore` ; `state_root` = racine d'**etat**
    (depot principal depuis un worktree lie, defaut `repo_root`), qui sert au pointeur, a
    `battle.json`, au confinement des gates et a la regle `.legion/` du builder.
    """
    state = state_root or repo_root
    tool_name = data.get("tool_name")
    if tool_name in SHELL_TOOLS:
        # Route AVANT `_load_active_guard` et la logique d'ecriture (cf. `_shell_decision`).
        return _shell_decision(data, repo_root, state)
    if tool_name not in WRITE_TOOLS:
        return 0, ""

    file_path = (data.get("tool_input") or {}).get("file_path", "")

    if _IMPORT_ERROR is not None:
        fallback = _fallback_decision(data, repo_root)
        if fallback is not None:
            return fallback
        # Session principale : edition libre, mais le perimetre /freeze ne peut plus etre lu.
        return 0, (
            f"[guard] /freeze non applique : installation legion incomplete ({_IMPORT_ERROR})."
        )

    # Confinement des gates : une gate n'ecrit QUE son artefact (cf. GATE_ARTIFACT).
    # Prioritaire sur tout le reste, et actif meme guard non arme.
    agent_type = data.get("agent_type")
    if agent_type in GATE_ARTIFACT:
        battle_id = active_battle_id(state)
        rel = _rel_state(repo_root, state, file_path) if file_path else None
        if _gate_decision(agent_type, rel, battle_id):
            # Confinement OK (bon artefact). Refuser EN PLUS un artefact vide : un `Write`
            # a contenu blanc produit un 0 octet qui passe le confinement mais echouerait
            # le delivery check de l'orchestrateur (RETEX 9c1d10e5a233 / A1).
            if data.get("tool_name") == "Write" and _is_blank_content(
                (data.get("tool_input") or {}).get("content")
            ):
                return 2, (
                    f"BLOQUE : la gate `{agent_type}` ne peut pas ecrire un artefact "
                    f"VIDE (`{GATE_ARTIFACT[agent_type]}`).\nEcris le contenu COMPLET de "
                    f"l'artefact, relis-le, puis retourne ton verdict.\n(Un artefact "
                    f"0 octet echouerait au delivery check de l'orchestrateur.)"
                )
            return 0, ""
        expected = f".legion/battles/{battle_id or '<aucune battle active>'}/{GATE_ARTIFACT[agent_type]}"
        return 2, (
            f"BLOQUE : la gate `{agent_type}` ne peut ecrire QUE son artefact "
            f"`{expected}`.\nTentative : `{rel}`.\n"
            f"Une gate retourne son verdict + le chemin de son artefact ; elle "
            f"n'ecrit ni code, ni `battle.json`, ni l'artefact d'une autre gate."
        )

    # Producteur sous `.legion/` : seul son rapport (pas d'auto-elargissement du guard).
    if agent_type in PRODUCER_ARTIFACT and file_path:
        battle_id = active_battle_id(state)
        rel = _rel_state(repo_root, state, file_path)
        decision = _producer_state_decision(
            agent_type, rel, battle_id, _resolved_parts(repo_root, file_path.replace("\\", "/"))
        )
        if decision is False:
            base = f".legion/battles/{battle_id or '<aucune battle active>'}/"
            expected = f"{base}{PRODUCER_ARTIFACT[agent_type]}"
            expected_slice = f"{base}build-report-<slice_id>.md"
            if state != repo_root:  # worktree : le rapport vit dans le depot principal
                expected = (state / expected).as_posix()
                expected_slice = (state / expected_slice).as_posix()
            return 2, (
                f"BLOQUE : le `{agent_type}` n'ecrit sous `.legion/` QUE son rapport : "
                f"`{expected}` (BUILD agrege) ou `{expected_slice}` (une slice).\nTentative : `{rel}`.\n"
                f"L'etat de la battle (`battle.json`, perimetre, artefacts de gate) "
                f"appartient a l'orchestrateur."
            )
        if decision is True:
            return 0, ""

    # Mode worktree (C8) : checkout principal ferme, guard arme ou non.
    wt_block = _worktree_main_decision(repo_root, state, file_path)
    if wt_block is not None:
        return wt_block

    active = _load_active_guard(state)
    if active is None:
        return 0, ""
    battle_id, allow, deny, valid, unreadable = active
    if not valid:
        return _invalid_guard_decision(
            data, repo_root, battle_id, file_path, unreadable=unreadable, state_root=state
        )
    if not allow:
        return 0, ""  # guard non arme

    if not file_path:
        return 0, ""

    if _is_claude_memory(file_path):
        return 0, ""  # memoire de Claude : hors perimetre repo, jamais bloquee

    rel = _relative(repo_root, file_path)
    # `.legion/**` (racine d'etat) et `.gitignore` (racine d'edition) : AVANT le blocage « hors du
    # repo », pour que la session principale ecrive `<principal>/.legion/**` depuis un worktree.
    if _always_allowed(rel, _rel_state(repo_root, state, file_path)):
        return 0, ""
    if rel is None:
        return 2, (
            f"BLOQUE par le guard de la battle {battle_id} : ecriture hors du repo "
            f"alors qu'un perimetre est actif.\nGlobs autorises : {allow}\n"
            f"Bypass delibere : LEGION_GUARD_OFF=1"
        )

    if deny and _matches(rel, deny):
        return 2, (
            f"BLOQUE par le guard de la battle {battle_id} : `{rel}` est dans `deny`.\n"
            f"Bypass delibere : LEGION_GUARD_OFF=1"
        )
    if _matches(rel, allow):
        return 0, ""

    return 2, (
        f"BLOQUE par le guard de la battle {battle_id} (/freeze actif).\n"
        f"`{rel}` est hors du perimetre d'ecriture.\n"
        f"Globs autorises : {allow}\n"
        f"Si l'edition est legitime, elargis le perimetre (`/freeze <glob>`) ou "
        f"bypass ponctuellement : LEGION_GUARD_OFF=1"
    )


def _safe_decide(data, repo_root: Path, state_root: Path | None = None) -> tuple[int, str]:
    """`_decide` avec filet final : toute exception imprevue -> exit 2 (jamais exit 1, non bloquant).

    `state_root` n'est transmis que s'il differe de `repo_root` (appel a deux arguments sinon)."""
    try:
        if state_root is None or state_root == repo_root:
            return _decide(data, repo_root)
        return _decide(data, repo_root, state_root)
    except BaseException as exc:  # noqa: BLE001 - fail-closed, y compris RecursionError
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return 2, (
            f"BLOQUE par le guard : erreur interne ({type(exc).__name__}: {exc}). "
            f"Ecriture refusee par prudence (fail-closed).\n"
            f"Bypass delibere : LEGION_GUARD_OFF=1"
        )


def _parse_payload(raw: str) -> tuple[int | None, dict | None, str]:
    """Analyse le stdin du hook (fonction pure). Retourne (code, data, message).

    - vide ou blanc -> (0, None, "") : rien a decider ;
    - illisible (toute exception du parseur : JSON invalide, entier geant, trop imbrique) ou
      non-objet -> (2, None, message) : fail-closed ;
    - objet JSON -> (None, data, "") : a decider.
    """
    if not raw.strip():
        return 0, None, ""
    try:
        data = json.loads(raw)
    except BaseException as exc:  # noqa: BLE001 - fail-closed, y compris RecursionError
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return 2, None, (
            f"BLOQUE par le guard : entree du hook illisible "
            f"({type(exc).__name__}: {str(exc)[:200]}). "
            f"Ecriture refusee par prudence (fail-closed)."
        )
    if not isinstance(data, dict):
        return 2, None, (
            f"BLOQUE par le guard : entree du hook invalide "
            f"(objet JSON attendu, recu {type(data).__name__})."
        )
    return None, data, ""


def _emit(message: str) -> None:
    """Ecrit sur stderr en best-effort : un echec d'ecriture ne change jamais le code de sortie."""
    if not message:
        return
    try:
        print(message, file=sys.stderr)
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()

    # Filet global (GH#106) : aucun chemin ne sort en exit 1 (lecture, cwd, decision, ecriture).
    try:
        code, data, message = _parse_payload(sys.stdin.read())
        if code is not None:
            _emit(message)  # rejet de parsing : pas de bypass LEGION_GUARD_OFF
            return code

        edit_root = Path.cwd()
        state_root = resolve_state_root(edit_root)   # depot principal depuis un worktree (GH#68)
        code, message = _safe_decide(data, edit_root, state_root)

        if code == 2 and os.environ.get("LEGION_GUARD_OFF") == "1":
            _emit(f"[guard bypass] {message}")
            return 0
        _emit(message)
        return code
    except BaseException as exc:  # noqa: BLE001 - fail-closed
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        _emit(
            f"BLOQUE par le guard : erreur interne ({type(exc).__name__}: {str(exc)[:200]}). "
            f"Ecriture refusee par prudence (fail-closed)."
        )
        return 2


def _ev(tool: str, agent: str, path: str, **extra) -> dict:
    return {"tool_name": tool, "agent_type": agent, "tool_input": {"file_path": path, **extra}}


def _t_guard_pointer_blank(bs) -> None:
    """Pointeur vide (ecrit par `close`) : aucune battle active."""
    for value in ("", "  \n"):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            bs._write_pointer(root, value)
            code, msg = _decide(_ev("Write", "legion:reviewer", ".legion/battles/B/gate-review.md",
                                    content="x"), root)
            assert code == 2 and "<aucune battle active>" in msg, (code, msg)
            assert _decide(_ev("Edit", "claude", "src/x.cs"), root)[0] == 0
            assert _decide(_ev("Edit", "legion:builder", ".legion/battles/B/battle.json"), root)[0] == 2


def _t_guard_invalid_id(bs) -> None:
    """Id invalide dans le pointeur : gate bloquee, /freeze non applique a la session principale."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        bs._write_pointer(root, "../x")
        assert _decide(_ev("Write", "legion:reviewer", ".legion/battles/B/gate-review.md",
                           content="x"), root)[0] == 2
        assert _decide(_ev("Edit", "claude", "src/x.cs"), root)[0] == 0


def _t_guard_unreadable_battle_json(bs) -> None:
    """`battle.json` malforme : le confinement des gates reste actif (pointeur seul)."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / ".legion" / "battles" / "B").mkdir(parents=True)
        bs._write_pointer(root, "B")
        (root / ".legion" / "battles" / "B" / "battle.json").write_text("{oops", encoding="utf-8")
        art = _ev("Write", "legion:reviewer", ".legion/battles/B/gate-review.md", content="# R")
        assert _decide(art, root)[0] == 0
        assert _decide(_ev("Write", "legion:reviewer", "src/x.cs", content="x"), root)[0] == 2


def _t_guard_freeze_nominal(bs) -> None:
    """/freeze arme (`allow: ["src/**"]`) : perimetre applique via le lecteur partage."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / ".legion" / "battles" / "B").mkdir(parents=True)
        bs._write_pointer(root, "B")
        (root / ".legion" / "battles" / "B" / "battle.json").write_text(
            '{"guard":{"allow":["src/**"]}}', encoding="utf-8")
        assert _decide(_ev("Edit", "claude", "src/x.cs"), root)[0] == 0
        assert _decide(_ev("Edit", "claude", "docs/x.md"), root)[0] == 2
        assert _decide(_ev("Edit", "claude", ".legion/battles/B/battle.json"), root)[0] == 0


def _guard_repo(root: Path, guard_json: str) -> None:
    (root / ".legion" / "battles" / "B").mkdir(parents=True)
    (root / ".legion" / "battles" / "B" / "battle.json").write_text(
        '{"guard":' + guard_json + '}', encoding="utf-8")


def _t_guard_invalid_block(bs) -> None:
    """Bloc `guard` non-dict : fail-closed hors toujours-autorises, reparation possible."""
    for raw in ('["x"]', '"x"'):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            bs._write_pointer(root, "B")
            _guard_repo(root, raw)
            code, msg = _decide(_ev("Edit", "claude", "src/x.cs"), root)
            assert code == 2 and "set-guard" in msg, (code, msg)
            for ok in (".legion/battles/B/battle.json", ".gitignore"):
                assert _decide(_ev("Edit", "claude", ok), root)[0] == 0, ok
            assert _decide(_ev("Edit", "claude", str(Path.home() / ".claude/projects/p/memory/x.md")), root)[0] == 0
            assert _decide({"tool_name": "Bash", "agent_type": "claude"}, root)[0] == 0
            assert _decide(_ev("Edit", "legion:builder", "src/x.cs"), root)[0] == 2
            assert _decide(_ev("Edit", "legion:builder", ".legion/battles/B/battle.json"), root)[0] == 2
            assert _decide(_ev("Edit", "claude", "../outside.txt"), root)[0] == 2


def _t_guard_invalid_fields(bs) -> None:
    for raw in ('{"allow":"src/**"}', '{"allow":["src/**"],"deny":"x"}', '{"allow":["a",1]}'):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            bs._write_pointer(root, "B")
            _guard_repo(root, raw)
            assert _decide(_ev("Edit", "claude", "src/x.cs"), root)[0] == 2, raw


def _t_guard_valid_unarmed(bs) -> None:
    for raw in ("{}", '{"allow":[]}', '{"allow":null}', "null"):   # `guard: null` = non arme
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            bs._write_pointer(root, "B")
            _guard_repo(root, raw)
            assert _decide(_ev("Edit", "claude", "src/x.cs"), root)[0] == 0, raw
    with tempfile.TemporaryDirectory() as d:  # guard absent
        root = Path(d)
        bs._write_pointer(root, "B")
        (root / ".legion" / "battles" / "B").mkdir(parents=True)
        (root / ".legion" / "battles" / "B" / "battle.json").write_text("{}", encoding="utf-8")
        assert _decide(_ev("Edit", "claude", "src/x.cs"), root)[0] == 0


def _t_guard_invalid_confinement(bs) -> None:
    """Le confinement des gates reste prioritaire sur la branche fail-closed."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        bs._write_pointer(root, "B")
        _guard_repo(root, '["x"]')
        art = ".legion/battles/B/gate-review.md"
        assert _decide(_ev("Write", "legion:reviewer", art, content="# R"), root)[0] == 0
        assert _decide(_ev("Write", "legion:reviewer", "src/x.cs", content="x"), root)[0] == 2
        assert _decide(_ev("Write", "legion:reviewer", art, content=""), root)[0] == 2


def _t_guard_unreadable_active(bs) -> None:
    """Pointeur valide + `battle.json` illisible : fail-closed ; pointeur vide/battle absente : libre."""
    for raw in ("{oops", "", "[]", "[" * 200000):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".legion" / "battles" / "B").mkdir(parents=True)
            bs._write_pointer(root, "B")
            (root / ".legion" / "battles" / "B" / "battle.json").write_text(raw, encoding="utf-8")
            code, msg = _decide(_ev("Edit", "claude", "src/x.cs"), root)
            assert code == 2 and "set-guard" not in msg and "close" not in msg, (raw[:8], code, msg)
            assert "battle.json" in msg and ".legion/active-battle" in msg, (raw[:8], msg)
            assert _decide(_ev("Edit", "claude", ".legion/battles/B/battle.json"), root)[0] == 0
            assert _decide(_ev("Edit", "legion:builder", "src/x.cs"), root)[0] == 2
            art = ".legion/battles/B/gate-review.md"   # confinement des gates inchange
            assert _decide(_ev("Write", "legion:reviewer", art, content="# R"), root)[0] == 0
            assert _decide(_ev("Write", "legion:reviewer", "src/x.cs", content="x"), root)[0] == 2
    with tempfile.TemporaryDirectory() as d:  # pointeur valide, battle.json absent : inchange
        root = Path(d)
        bs._write_pointer(root, "B")
        assert _decide(_ev("Edit", "claude", "src/x.cs"), root)[0] == 0


def _t_guard_safety_net() -> None:
    """Exception imprevue dans la decision -> exit 2 avec message, jamais exit 1."""
    global _decide
    import io
    real, real_stdin, real_stderr, real_argv = _decide, sys.stdin, sys.stderr, sys.argv
    sys.argv = [real_argv[0]]   # main() ne doit pas relancer --self-test

    def boom(data, root, *_):
        raise RuntimeError("injecte")

    def deep(data, root, *_):
        raise RecursionError("injecte")

    try:
        for fn in (boom, deep):
            _decide = fn
            assert _safe_decide({"tool_name": "Edit"}, Path("."))[0] == 2
            sys.stdin, sys.stderr = io.StringIO('{"tool_name":"Edit"}'), io.StringIO()
            code = main()
            err = sys.stderr.getvalue()
            assert code == 2 and "erreur interne" in err and "injecte" in err, (code, err)
        sys.stdin, sys.stderr = io.StringIO("[" * 200000), io.StringIO()
        assert main() == 2
    finally:
        _decide, sys.stdin, sys.stderr, sys.argv = real, real_stdin, real_stderr, real_argv


def _run_main(stdin, stderr=None, **patches):
    """Execute `main()` avec stdin/stderr/argv (et attributs de module) remplaces, restaures en `finally`."""
    import io
    g = globals()
    saved = {k: g[k] for k in patches}
    real_stdin, real_stderr, real_argv = sys.stdin, sys.stderr, sys.argv
    err = stderr if stderr is not None else io.StringIO()
    sys.argv = [real_argv[0]]
    sys.stdin = io.StringIO(stdin) if isinstance(stdin, str) else stdin
    sys.stderr = err
    g.update(patches)
    try:
        return main(), err
    finally:
        g.update(saved)
        sys.stdin, sys.stderr, sys.argv = real_stdin, real_stderr, real_argv


def _t_guard_stdin_blank() -> None:
    for raw in ("", "  \n\t"):
        assert _parse_payload(raw) == (0, None, ""), raw
        code, err = _run_main(raw)
        assert code == 0 and err.getvalue() == "", (raw, code, err.getvalue())


def _t_guard_stdin_invalid_json() -> None:
    for raw in ("{oops", "not json", '{"tool_name":'):
        code, data, msg = _parse_payload(raw)
        assert code == 2 and data is None and "illisible" in msg, (raw, code, msg)
        code, err = _run_main(raw)
        assert code == 2 and "illisible" in err.getvalue(), (raw, code, err.getvalue())


def _t_guard_stdin_huge_int() -> None:
    raw = "1" * 5000
    code, data, msg = _parse_payload(raw)
    assert code == 2 and data is None and len(msg) < 500, (code, len(msg))
    code, err = _run_main(raw)
    assert code == 2 and len(err.getvalue()) < 500, (code, len(err.getvalue()))


def _t_guard_stdin_non_object() -> None:
    for raw in ("[]", "[1]", "42", '"x"', "null"):
        code, data, msg = _parse_payload(raw)
        assert code == 2 and data is None and "objet JSON attendu" in msg, (raw, code, msg)
        code, err = _run_main(raw)
        assert code == 2 and "objet JSON attendu" in err.getvalue(), (raw, code, err.getvalue())


def _t_guard_stdin_undecodable() -> None:
    class _Bad:
        def read(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "injecte")

    code, err = _run_main(_Bad())
    assert code == 2 and "erreur interne" in err.getvalue(), (code, err.getvalue())


def _t_guard_main_no_exit_1() -> None:
    """Le filet couvre `Path.cwd()` et l'ecriture stderr ; le nominal reste en 0."""
    class _NoCwd:
        @staticmethod
        def cwd():
            raise FileNotFoundError("cwd supprime")

    code, err = _run_main('{"tool_name":"Bash"}', Path=_NoCwd)
    assert code == 2 and "erreur interne" in err.getvalue(), (code, err.getvalue())

    class _BrokenErr:
        def write(self, s):
            raise OSError("pipe casse")

        def flush(self):
            raise OSError("pipe casse")

    code, _ = _run_main('{"tool_name":"Edit"}', stderr=_BrokenErr(), _decide=lambda d, r, *_: (2, "x"))
    assert code == 2, code
    code, err = _run_main('{"tool_name":"Bash"}')
    assert code == 0 and err.getvalue() == "", (code, err.getvalue())


def _t_guard_repair_messages(bs) -> None:
    """Deux causes, deux reparations : bloc invalide -> set-guard ; battle.json illisible -> fichier/pointeur."""
    for raw in ('["x"]', '"x"'):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            bs._write_pointer(root, "B")
            _guard_repo(root, raw)
            code, msg = _decide(_ev("Edit", "claude", "src/x.cs"), root)
            assert code == 2 and "set-guard" in msg and "active-battle" not in msg, (raw, code, msg)
    for raw in ("{oops", "", "[]", "[" * 200000):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".legion" / "battles" / "B").mkdir(parents=True)
            bs._write_pointer(root, "B")
            (root / ".legion" / "battles" / "B" / "battle.json").write_text(raw, encoding="utf-8")
            code, msg = _decide(_ev("Edit", "claude", "src/x.cs"), root)
            assert code == 2 and "set-guard" not in msg and "close" not in msg, (raw[:8], code, msg)
            assert "battle.json" in msg and ".legion/active-battle" in msg, (raw[:8], msg)


def _shell_repo(bs, root: Path, guard_json: str | None = None) -> None:
    (root / ".legion" / "battles" / "B").mkdir(parents=True)
    bs._write_pointer(root, "B")
    body = "{}" if guard_json is None else '{"guard":' + guard_json + "}"
    (root / ".legion" / "battles" / "B" / "battle.json").write_text(body, encoding="utf-8")


def _sh(cmd, root: Path, agent: str = "legion:lint", tool: str = "Bash") -> tuple[int, str]:
    return _decide({"tool_name": tool, "agent_type": agent, "tool_input": {"command": cmd}}, root)


def _t_shell_blocked(bs) -> None:
    """G1/G2/G4/G5 : ecritures de gate bloquees, motif nomme dans le message."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _shell_repo(bs, root)
        blocked = {
            "echo x > src/a.cs": "redirection",
            "sed -i 's/a/b/' f": "sed -i",
            "sed -Ei 's/a/b/' f": "sed -i",
            "sed --in-place s/a/b/ f": "sed -i",
            "sed -i.bak s/a/b/ f": "sed -i",
            "perl -pi -e 's/a/b/' f": "perl -i",
            "git checkout -- f": "git checkout",
            "git -C . add f": "git add",
            "git -c core.x=1 --no-pager commit -m x": "git commit",
            "git stash": "git stash",
            "git stash push": "git stash",
            "git apply p.diff": "git apply",
            "git update-index --assume-unchanged f": "git update-index",
            "git worktree add ../w": "git worktree add",
            "dotnet format": "dotnet format",
            "dotnet format whitespace": "dotnet format",
            "dotnet new console": "dotnet new",
            "npm install": "npm install",
            "pip install x": "pip install",
            "python -m pip install x": "pip install",
            "rm f": "rm",
            "cp a b": "cp",
            "mkdir out": "mkdir",
            "tee src/x": "tee",
            "cmd >> src/x": "redirection",
            "cmd >| src/x": "redirection",
            "cmd &> src/x": "redirection",
            "cmd 2> src/x": "redirection",
            "echo a\nrm x": "rm",
            "(rm x)": "rm",
            "echo $(rm x)": "rm",
            'echo "$(rm x)"': "rm",
            "echo `rm x`": "rm",
            "find . -exec rm {} \\;": "rm",
            "find . -delete": "find -delete",
            "xargs rm": "rm",
            "ls | xargs -n1 rm": "rm",
            "sudo rm x": "rm",
            "sudo -u me rm x": "rm",
            "/bin/rm x": "rm",
            "\\rm x": "rm",
            "FOO=1 rm x": "rm",
            "env A=1 rm x": "rm",
            "time rm x": "rm",
            "for f in a; do rm $f; done": "rm",
            "true && rm x": "rm",
            "true || rm x": "rm",
            "{ rm x; }": "rm",
            "cmd > $OUT": "redirection",
            'cmd > "$(mktemp)"': "redirection",
            'rm -rf "$TMP/x"': "rm",
            "cmd > /tmp/../etc/x": "redirection",
            "cmd > .legion/battles/B/battle.json": "redirection",
            "cmd > .legion/active-battle": "redirection",
            "cmd > .legion/battles/B/gate-review.md": "redirection",
            "cmd > .legion/battles/B/ci-failed-1.log": "redirection",
            "cmd > .legion/battles/AUTRE/x.log": "redirection",
            "cmd > .legion/battles/B/sub/x.log": "redirection",
            "cmd > *.log": "redirection",
            "cmd > ~/x": "redirection",
            "echo 'unclosed": "non analysable",
            'echo "unclosed': "non analysable",
            "echo $(rm x": "non analysable",
            # auto-correction 1 (security + review)
            "git config core.fsmonitor /tmp/x": "git config",
            "git config --local core.trustctime false": "git config",
            "git config --unset core.fsmonitor": "git config",
            "git config set core.hooksPath h": "git config",
            "git config --add x y": "git config",
            "git update-ref HEAD abc": "git update-ref",
            # auto-correction 2 : --show-scope ecrit ; briques d'ecriture d'etat git
            "git config --show-scope core.fsmonitor x": "git config",
            "git config --show-origin a b": "git config",
            "git commit-tree abc -m x": "git commit-tree",
            "git mktree": "git mktree",
            "git hash-object -w f": "git hash-object -w",
            "git notes add -m x": "git notes",
            "git maintenance register": "git maintenance",
            "git replace a b": "git replace",
            "git branch -f main abc": "git branch",
            "git tag -f v1": "git tag",
            "dd of=src/a.cs if=x": "dd of=",
            "install a src/b": "install",
            "rsync -a x src/": "rsync",
            "patch -p1 < x.diff": "patch",
            "tar -xf a.tgz": "tar -x",
            "tar xzf a.tgz": "tar -x",
            "unzip a.zip": "unzip",
            "curl -o src/a http://x": "curl -o",
            "curl -O http://x/a": "curl -o",
            "wget -O src/a http://x": "wget",
            "wget http://x/a": "wget",
            "sed 's/a/b/w src/a' f": "sed w",
            "sed -n '/x/w out.txt' f": "sed w",
            "/usr/bin/env rm x": "rm",
            "/usr/bin/sudo rm x": "rm",
            "cat <<EOF\n$(rm x)\nEOF": "rm",
            "cat <<EOF\n`rm x`\nEOF": "rm",
            "cat <<EOF\nok\nEOF\nrm y": "rm",
            "echo $((1<<X))\nrm -rf src\nX": "rm",
        }
        for cmd, motif in blocked.items():
            code, msg = _sh(cmd, root)
            assert code == 2 and motif in msg and "legion:lint" in msg, (cmd, code, msg)
            assert motif == "non analysable" or "PERDRE" in msg, (cmd, msg)
        for cmd in ("Set-Content f x", "sc f x", "Out-File f", "echo x > f", "set-content f x",
                    "Remove-Item -Recurse f", "[IO.File]::WriteAllText('a','b')",
                    "[System.IO.File]::AppendAllText('a','b')", "ni f", "git commit -m x",
                    "dotnet format", "1..3 | ForEach-Object { rm $_ }"):
            code, msg = _sh(cmd, root, tool="PowerShell")
            assert code == 2, (cmd, code, msg)
        for cmd in ("Set-Con`tent f x", "Tee-Object -FilePath f", "Export-Csv f", "Set-Item f x",
                    "Invoke-WebRequest http://x -OutFile src/a", "iwr http://x -OutFile a",
                    "& 'C:\\Windows\\System32\\Set-Content' f"):
            code, msg = _sh(cmd, root, tool="PowerShell")
            assert code == 2, (cmd, code, msg)
        assert _sh("Set-Content f x", root)[0] == 0   # cmdlet PowerShell : sans effet sous Bash
        assert "IO.File" in _sh("[IO.File]::WriteAllText('a','b')", root, tool="PowerShell")[1]


def _t_shell_allowed(bs) -> None:
    """G3 : commandes reelles des gates (faux positifs) -> exit 0."""
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        root = Path(d)
        _shell_repo(bs, root)
        tmp = out.replace("\\", "/")
        allowed = [
            "dotnet test", "dotnet build", "dotnet build -c Release --no-restore 2>&1",
            "dotnet test --no-build --logger 'trx;LogFileName=x.trx'",
            "dotnet format --verify-no-changes --include a.cs",
            "dotnet format whitespace --verify-no-changes",
            "dotnet list package --vulnerable", "dotnet restore",
            "git log", "git log --oneline -5", "git diff", "git diff --stat HEAD~1", "git show HEAD",
            "git status", "git status --porcelain", "git stash list", "git stash show -p",
            "git blame f", "git grep x", "git rev-parse HEAD", "git -C . log", "git apply --check p",
            "git worktree list", "git branch --show-current", "git ls-files",
            'grep -rn "a>b" src', "grep -rn 'a > b' src", 'grep ">" f', "grep rm f",
            "git log --format='%h > %s'", "cat f | grep x | wc -l",
            "cmd 2>&1", "cmd >&2", "cmd 1>&2", "cmd > /dev/null", "cmd 2>/dev/null",
            "cmd &> /dev/null", "cmd > /dev/null 2>&1", "cmd >> /dev/null",
            "cmd > .legion/battles/B/x.log", "cmd 2>&1 | tee .legion/battles/B/run.log",
            "cmd > ./.legion/battles/B/x.log 2>&1", "cmd < input.txt", "cat <<< hello",
            "python - <<'EOF'\nprint(1 > 0)\nx = a > b\nEOF",
            "cat <<EOF\nrm x > y\nEOF",
            "python - <<-EOF\n\trm x > y\n\tEOF\necho done",
            f"mkdir -p {tmp}/x", f"cmd > {tmp}/o.txt", f"rm -rf {tmp}/x",
            f"touch {tmp}/t", f"tee {tmp}/t", "rm -f /dev/null",
            "ls -la", "echo done # rm x > y", "echo 'a; rm x'", 'echo "a && rm x"',
            "echo $((1 + 2))", "x=$(git rev-parse HEAD)", "echo $(git log -1)",
            "sed -n '1,5p' f", "sed -e 's/a/b/' f", "sed 's/i/x/' f", "sed -es/i/x/ f",
            "perl -e 'print 1'", "perl -Mstrict -e 1", "find . -name '*.cs'", "find . -exec grep x {} +",
            "git diff > /dev/null", "python -m pytest", "python -m pip list", "pip list",
            "npm test", "npm run build", "npm list", "yarn test",
            "cd src && ls", "cd . && git status", "echo a\ngit status", "test -f x || echo no",
            "ls \\\n  -la", "diff <(git show HEAD:f) f", "cmd 2> >(cat)",
            # auto-correction 1 : formes de lecture des commandes nouvellement filtrees
            "git config --get core.fsmonitor", "git config --list", "git config -l",
            "git config --local --list", "git config user.name", "git config --get-all x",
            "git config --show-origin --list", "git config --show-scope --get x",
            "git hash-object f", "git branch -a", "git branch --list",
            "git tag", "git tag -l", "git tag --list 'v*'", "git remote -v",
            f"curl -o {tmp}/a http://x", "curl -s http://x", f"wget -O {tmp}/a http://x",
            "wget -O - http://x", "tar -tf a.tgz", "tar -cf /dev/null x", "unzip -l a.zip",
            "patch --dry-run -p1 < x.diff", "dd if=a of=/dev/null", f"dd if=a of={tmp}/o",
            "sed -n 's/a/b/p' f", "sed '1d' f",
            "cat <<EOF\n$HOME `date` \\$(rm x)\nEOF", "cat <<'EOF'\n$(rm x)\nEOF",
            "cat <<\\EOF\n$(rm x)\nEOF", "echo $((1<<3))", "echo $((1 << 3))\nls",
            "cat <<EOF\n$(git rev-parse HEAD)\nEOF",
        ]
        for cmd in allowed:
            code, msg = _sh(cmd, root)
            assert code == 0 and msg == "", (cmd, code, msg)
        for cmd in ("Get-ChildItem C:\\repo\\src", "git status 2>&1", "dotnet test > $null",
                    "Write-Output 'a > b'",
                    "git log --format='%h > %s'"):
            code, msg = _sh(cmd, root, tool="PowerShell")
            assert code == 0 and msg == "", (cmd, code, msg)
        # cible temporaire = dossier temporaire lui-meme, ou sous le depot : bloque
        for cmd in (f"rm -rf {Path(out).parent}", f"cmd > {root}/src/a.cs", f"cmd > {root}/.legion/x.log"):
            assert _sh(cmd, root)[0] == 2, cmd


def _t_shell_pure() -> None:
    """Fonctions pures : heredocs, decoupage, cibles."""
    assert _strip_heredocs("cat <<'E'\nx > y\nE\nls") == "cat <<'E'\nls"
    assert _strip_heredocs("cat <<E\nx\nE") == "cat <<E"
    assert _strip_heredocs("echo '<<X'\nrm x\nX") == "echo '<<X'\nrm x\nX"  # << quote : pas un heredoc
    assert _strip_heredocs("cat <<A <<B\n1\nA\n2\nB\nls") == "cat <<A <<B\nls"
    assert _strip_heredocs("cat <<E\n$(rm x)\nE") == "cat <<E\n$(rm x)"     # non quote : reinjecte
    assert _strip_heredocs("cat <<'E'\n$(rm x)\nE") == "cat <<'E'"           # quote : litteral
    assert _strip_heredocs("cat <<\\E\n$(rm x)\nE") == "cat <<\\E"
    assert _strip_heredocs("cat <<E\n\\$(rm x)\nE") == "cat <<E"            # `\$` litteral
    assert _find_heredocs("echo $((1<<X))") == [] and _find_heredocs("(( a << 2 ))") == []
    assert _find_heredocs("cat <<E") == [("E", False, False)]
    assert _find_heredocs("cat <<-'E'") == [("E", True, True)]
    assert _command_name("/usr/bin/env", False) == "env" and _command_name("SUDO.exe", True) == "sudo"
    cmds, subs = _split_simple_commands("a 2>&1 | b >o.txt; c > /dev/null && d $(e f)")
    assert [w for w, _ in cmds] == [["a"], ["b"], ["c"], ["d", "$SUB"]], cmds
    assert cmds[1][1] == [(">", "o.txt")] and cmds[0][1] == [] and subs == ["e f"], (cmds, subs)
    assert _split_simple_commands('grep ">" f')[0] == [(["grep", ">", "f"], [])]
    assert _split_simple_commands("echo a\nrm x")[0] == [(["echo", "a"], []), (["rm", "x"], [])]
    assert _split_simple_commands("echo a # b > c")[0] == [(["echo", "a"], [])]
    assert _split_simple_commands("cmd >&2")[0] == [(["cmd"], [])]
    assert _split_simple_commands("cmd >& out")[0] == [(["cmd"], [(">&", "out")])]
    assert _split_simple_commands("dir C:\\a\\b", ps=True)[0] == [(["dir", "C:\\a\\b"], [])]
    try:
        _split_simple_commands("echo 'x")
        raise AssertionError("quote non fermee doit lever ValueError")
    except ValueError:
        pass
    root = Path("/nonexistent-root")
    assert _allowed_target("/dev/null", root, "B") and not _allowed_target("$null", root, "B")
    assert _allowed_target("$null", root, "B", ps=True)
    assert _allowed_target(".legion/battles/B/x.log", root, "B")
    assert not _allowed_target(".legion/battles/B/x.log", root, None)
    assert not _allowed_target(".legion/battles/B/x.LOG.md", root, "B")
    assert not _allowed_target(".legion/battles/B/CI-FAILED-2.log", root, "B")
    assert not _allowed_target("", root, "B") and not _allowed_target("a*b", root, "B")


def _t_shell_routing(bs) -> None:
    """G6-G10 : routage avant le guard, non-gates libres, fail-closed, bypass, hooks.json."""
    for guard_json in (None, '["x"]', '"x"', '{"allow":"src/**"}'):   # G7 : bloc invalide
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _shell_repo(bs, root, guard_json)
            for agent in ("claude", "legion:builder", "some:other", None):
                for cmd in ("rm -rf src", "git commit -m x", "echo > src/a"):
                    ev = {"tool_name": "Bash", "tool_input": {"command": cmd}}
                    if agent is not None:
                        ev["agent_type"] = agent
                    assert _decide(ev, root) == (0, ""), (guard_json, agent, cmd)
            assert _sh("rm x", root)[0] == 2 and _sh("git status", root)[0] == 0
    with tempfile.TemporaryDirectory() as d:   # battle.json illisible
        root = Path(d)
        _shell_repo(bs, root)
        (root / ".legion" / "battles" / "B" / "battle.json").write_text("{oops", encoding="utf-8")
        assert _sh("rm -rf src", root, agent="claude")[0] == 0
        assert _sh("git commit", root, agent="legion:builder")[0] == 0
        assert _sh("rm x", root, agent="legion:reviewer")[0] == 2
        assert _sh("git log", root, agent="legion:reviewer")[0] == 0
    with tempfile.TemporaryDirectory() as d:   # pas de battle active : la gate reste filtree
        root = Path(d)
        for agent in GATE_ARTIFACT:
            assert _sh("echo x > src/a", root, agent=agent)[0] == 2, agent
            assert _sh("cmd > .legion/battles/B/x.log", root, agent=agent)[0] == 2, agent
            assert _sh("cmd > /dev/null", root, agent=agent)[0] == 0, agent
        # G9 : tool_input absent / command non-str -> 2
        for ti in (None, {}, {"command": None}, {"command": 5}, {"command": ["rm"]}, "rm x"):
            ev = {"tool_name": "Bash", "agent_type": "legion:lint"}
            if ti is not None:
                ev["tool_input"] = ti
            assert _decide(ev, root)[0] == 2, ti
        assert _decide({"tool_name": "Bash", "agent_type": "legion:builder"}, root)[0] == 0
        assert _sh("", root)[0] == 0
        # exception injectee dans _shell_decision -> _safe_decide : 2, jamais 1
        global _shell_decision
        real = _shell_decision
        try:
            def boom(data, r, *_):
                raise RuntimeError("injecte")
            _shell_decision = boom
            code, msg = _safe_decide({"tool_name": "Bash", "agent_type": "legion:lint"}, root)
            assert code == 2 and "injecte" in msg, (code, msg)
        finally:
            _shell_decision = real
        # G10 : bypass via main() ; payload illisible non contournable
        payload = json.dumps({"tool_name": "Bash", "agent_type": "legion:lint",
                              "tool_input": {"command": "rm x"}})
        saved_env, saved_cwd = os.environ.get("LEGION_GUARD_OFF"), os.getcwd()
        os.environ.pop("LEGION_GUARD_OFF", None)
        os.chdir(root)   # main() decide depuis le cwd du hook
        try:
            code, err = _run_main(payload)
            assert code == 2 and "ne peut pas ecrire par shell" in err.getvalue(), (code, err.getvalue())
            os.environ["LEGION_GUARD_OFF"] = "1"
            code, err = _run_main(payload)
            assert code == 0 and "[guard bypass]" in err.getvalue(), (code, err.getvalue())
            code, _ = _run_main("{oops")   # payload illisible : pas de bypass
            assert code == 2
        finally:
            os.chdir(saved_cwd)
            if saved_env is None:
                os.environ.pop("LEGION_GUARD_OFF", None)
            else:
                os.environ["LEGION_GUARD_OFF"] = saved_env
    # G11 : hooks.json branche guard.py ET careful.py sur Bash|PowerShell
    hooks = json.loads((Path(__file__).resolve().parent / "hooks.json").read_text(encoding="utf-8"))
    entry = [e for e in hooks["hooks"]["PreToolUse"] if e["matcher"] == "Bash|PowerShell"]
    assert len(entry) == 1, entry
    cmds = [h["command"] for h in entry[0]["hooks"]]
    assert any("hooks/guard.py" in c for c in cmds) and any("hooks/careful.py" in c for c in cmds), cmds


# --- Deux racines (GH#68) : guard lance depuis un worktree git lie ----------------------

def _wt_guard_json(main: Path, guard_json: str | None = None, raw: str | None = None) -> None:
    """(Re)ecrit `battle.json` de la battle `B` du depot principal de la fixture."""
    bdir = main / ".legion" / "battles" / "B"
    bdir.mkdir(parents=True, exist_ok=True)
    body = raw if raw is not None else '{"guard":' + (guard_json or '{"allow":["src/**"]}') + "}"
    (bdir / "battle.json").write_text(body, encoding="utf-8")


def _with_wt(bs, name: str, body) -> None:
    """Fixture worktree reelle (`battle_state._git_worktree_fixture`) ; SKIP si git est inutilisable.
    `main` a la battle `B` active avec `allow=["src/**"]`."""
    with tempfile.TemporaryDirectory() as tmp:
        fx = bs._git_worktree_fixture(Path(tmp))
        if fx is None:
            print(f"SKIP: {name} (git absent ou inutilisable)", file=sys.stderr)
            return
        main, wt, wt_out = (Path(os.path.realpath(p)) for p in fx)
        _wt_guard_json(main)
        body(main, wt, wt_out)


def _run_hook(payload: dict, cwd: Path, **env_extra) -> tuple[int, str]:
    """Lance `guard.py` en sous-processus avec `cwd` (l'appel reel du hook) : (code, stderr)."""
    import subprocess
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "LEGION_GUARD_OFF")}
    env.update(env_extra)
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve())], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=str(cwd), env=env, timeout=60)
    return proc.returncode, proc.stderr


def _t_guard_wt_subprocess(bs) -> None:
    """G1 (reproduction), G2, G4, G13 : le hook reel, lance avec `cwd=<worktree>`."""
    def body(main, wt, wt_out):
        code, err = _run_hook(_ev("Edit", "claude", str(wt / "docs" / "x.md")), wt)          # G1
        assert code == 2 and "hors du perimetre" in err, (code, err)
        for path in (str(wt / "src" / "a.py"), "src/a.py"):                                   # G2
            code, err = _run_hook(_ev("Edit", "claude", path), wt)
            assert code == 0, (path, code, err)
        report = str(main / ".legion" / "battles" / "B" / "build-report.md")                 # G4
        code, err = _run_hook(_ev("Write", "legion:builder", report, content="# B"), wt)
        assert code == 0, (code, err)
        slice_report = str(main / ".legion" / "battles" / "B" / "build-report-slice-1.md")   # G9
        code, err = _run_hook(_ev("Write", "legion:builder", slice_report, content="## slice-1"), wt)
        assert code == 0, (code, err)
        code, err = _run_hook(_ev("Edit", "claude", str(wt / "docs" / "x.md")), wt, LEGION_GUARD_OFF="1")
        assert code == 0 and "[guard bypass]" in err, (code, err)                            # G13
    _with_wt(bs, "_t_guard_wt_subprocess", body)


def _t_guard_wt_standard(bs) -> None:
    """G3 : `deny` evalue sur la racine d'edition (le worktree)."""
    def body(main, wt, wt_out):
        _wt_guard_json(main, '{"allow":["src/**"],"deny":["src/secret/**"]}')
        assert _decide(_ev("Edit", "claude", str(wt / "src/secret/k.py")), wt, main)[0] == 2
        assert _decide(_ev("Edit", "claude", str(wt / "src/ok.py")), wt, main)[0] == 0
        assert _decide(_ev("Edit", "claude", str(wt_out / "src/ok.py")), wt_out, main)[0] == 0
    _with_wt(bs, "_t_guard_wt_standard", body)


def _t_guard_wt_builder(bs) -> None:
    """G4 (unit), G5, G6 : le builder n'ecrit que `<principal>/.legion/battles/B/build-report.md`."""
    def body(main, wt, wt_out):
        for edit_root in (wt, wt_out):
            ok = _ev("Write", "legion:builder", str(main / ".legion/battles/B/build-report.md"), content="# B")
            assert _decide(ok, edit_root, main)[0] == 0, edit_root
            bad = _ev("Write", "legion:builder", str(edit_root / ".legion/battles/B/build-report.md"), content="# B")
            code, msg = _decide(bad, edit_root, main)                                           # G5
            expected = (main / ".legion/battles/B/build-report.md").as_posix()
            assert code == 2 and expected in msg, (edit_root, code, msg)
            for name in ("build-report-slice-1.md", "build-report-slice-2.md"):                 # G8
                ok = _ev("Write", "legion:builder", str(main / ".legion/battles/B" / name), content="## s")
                assert _decide(ok, edit_root, main)[0] == 0, (edit_root, name)
            bad = _ev("Write", "legion:builder", str(edit_root / ".legion/battles/B/build-report-slice-1.md"),
                      content="## s")
            code, msg = _decide(bad, edit_root, main)
            expected = (main / ".legion/battles/B/build-report-<slice_id>.md").as_posix()
            assert code == 2 and expected in msg and "build-report.md" in msg, (edit_root, code, msg)   # G8, G11
            for target in (".legion/battles/B/battle.json", ".legion/active-battle"):         # G6
                assert _decide(_ev("Edit", "legion:builder", str(main / target)), edit_root, main)[0] == 2, target
        _wt_guard_json(main, "{}")   # guard non arme : memes refus
        assert _decide(_ev("Edit", "legion:builder", str(main / ".legion/battles/B/battle.json")), wt, main)[0] == 2
        bad = _ev("Write", "legion:builder", str(wt / ".legion/battles/B/build-report.md"), content="# B")
        assert _decide(bad, wt, main)[0] == 2
        ok = _ev("Write", "legion:builder", str(main / ".legion/battles/B/build-report-slice-1.md"), content="## s")
        assert _decide(ok, wt, main)[0] == 0                                                   # G10 (worktree)
        assert _decide(_ev("Edit", "legion:builder", str(wt / "src/a.py")), wt, main)[0] == 0
    _with_wt(bs, "_t_guard_wt_builder", body)


def _t_guard_wt_gate(bs) -> None:
    """G7 : une gate ecrit son artefact dans le depot principal, rien d'autre."""
    def body(main, wt, wt_out):
        art = str(main / ".legion/battles/B/gate-review.md")
        assert _decide(_ev("Write", "legion:reviewer", art, content="# R"), wt, main)[0] == 0
        assert _decide(_ev("Write", "legion:reviewer", art, content=""), wt, main)[0] == 2
        for bad in (wt / "src/x.py", wt / ".legion/battles/B/gate-review.md", main / ".legion/battles/B/plan.md"):
            assert _decide(_ev("Write", "legion:reviewer", str(bad), content="# R"), wt, main)[0] == 2, bad
    _with_wt(bs, "_t_guard_wt_gate", body)


def _t_guard_wt_always_allowed(bs) -> None:
    """G8 (C5-A) : `.legion/**` sur la racine d'etat, `.gitignore` sur la racine d'edition."""
    def body(main, wt, wt_out):
        assert _decide(_ev("Edit", "claude", str(main / ".legion/battles/B/battle.json")), wt, main)[0] == 0
        assert _decide(_ev("Edit", "claude", str(wt / ".gitignore")), wt, main)[0] == 0
        assert _decide(_ev("Edit", "claude", str(wt / ".legion/x")), wt, main)[0] == 2
        assert _decide(_ev("Edit", "claude", str(wt_out / ".legion/x")), wt_out, main)[0] == 2
    _with_wt(bs, "_t_guard_wt_always_allowed", body)


def _t_guard_wt_relative(bs) -> None:
    """GH#130 : un `file_path` relatif s'ancre sur la racine d'edition (le worktree), pas l'etat."""
    def body(main, wt, wt_out):
        report, review = ".legion/battles/B/build-report.md", ".legion/battles/B/gate-review.md"
        assert _decide(_ev("Write", "legion:builder", report, content="# B"), wt, main)[0] == 2
        assert _decide(_ev("Write", "legion:reviewer", review, content="# R"), wt, main)[0] == 2
        assert _decide(_ev("Write", "legion:builder", str(main / report), content="# B"), wt, main)[0] == 0
        assert _decide(_ev("Write", "legion:reviewer", str(main / review), content="# R"), wt, main)[0] == 0
        assert _decide(_ev("Edit", "claude", ".legion/battles/B/battle.json"), wt, main)[0] == 2
        # Racines confondues (pas de worktree) : relatif et absolu decident pareil.
        for agent, rel, content in (("legion:builder", report, "# B"), ("legion:reviewer", review, "# R")):
            relative = _decide(_ev("Write", agent, rel, content=content), main)[0]
            absolute = _decide(_ev("Write", agent, str(main / rel), content=content), main)[0]
            assert relative == absolute == 0, (agent, relative, absolute)
    _with_wt(bs, "_t_guard_wt_relative", body)


def _t_guard_wt_invalid(bs) -> None:
    """G9 : bloc `guard` invalide / `battle.json` illisible dans le principal -> fail-closed en worktree."""
    def body(main, wt, wt_out):
        for raw in ('{"guard":["x"]}', "{oops"):
            _wt_guard_json(main, raw=raw)
            assert _decide(_ev("Edit", "claude", str(wt / "src/a.py")), wt, main)[0] == 2, raw
            assert _decide(_ev("Edit", "claude", str(main / ".legion/battles/B/battle.json")), wt, main)[0] == 0, raw
            assert _decide(_ev("Edit", "claude", str(wt / ".gitignore")), wt, main)[0] == 0, raw
    _with_wt(bs, "_t_guard_wt_invalid", body)


def _t_guard_wt_shell(bs) -> None:
    """G10 : filtre shell des gates, logs autorises dans le dossier de la battle du principal."""
    def body(main, wt, wt_out):
        for cmd, want in ((f"echo x > {main}/.legion/battles/B/x.log", 0),
                          (f"echo x > {wt}/src/a", 2), (f"echo x > {main}/src/a", 2)):
            ev = {"tool_name": "Bash", "agent_type": "legion:lint", "tool_input": {"command": cmd}}
            assert _decide(ev, wt, main)[0] == want, (cmd, want)
    _with_wt(bs, "_t_guard_wt_shell", body)


def _t_guard_wt_compat(bs) -> None:
    """G11 : sans `state_root` (ou confondu) la decision est celle d'avant ; G12 : `main()` hors git."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _shell_repo(bs, root, '{"allow":["src/**"]}')
        for ev in (_ev("Edit", "claude", "src/x.cs"), _ev("Edit", "claude", "docs/x.md"),
                   _ev("Write", "legion:builder", ".legion/battles/B/build-report.md", content="#"),
                   _ev("Edit", "legion:builder", ".legion/battles/B/battle.json")):
            assert _decide(ev, root) == _decide(ev, root, root) == _safe_decide(ev, root, root), ev
        real_cwd = os.getcwd()
        os.chdir(root)   # G12 : dossier non git, `main()` via stdin
        try:
            for path, want in (("src/x.cs", 0), ("docs/x.md", 2)):
                code, _ = _run_main(json.dumps(_ev("Edit", "claude", path)))
                assert code == want, (path, code)
        finally:
            os.chdir(real_cwd)


def _wt_battle_json(main: Path, wt: Path, allow: str = '["src/**"]', worktree=True) -> None:
    """`battle.json` de `B` avec bloc `worktree` (path = `wt`), ou sans (`worktree=False`)."""
    bdir = main / ".legion" / "battles" / "B"
    bdir.mkdir(parents=True, exist_ok=True)
    doc = {"guard": {"allow": json.loads(allow)}}
    if worktree:
        doc["worktree"] = {"path": str(wt), "branch": "me/1", "base": "0" * 40,
                           "created_at": "2026-10-01T00:00:00Z"}
    (bdir / "battle.json").write_text(json.dumps(doc), encoding="utf-8")


def _t_guard_wt_main_protected(bs) -> None:
    """C8 : checkout principal ferme en mode worktree (session dans le worktree, puis dans le principal)."""
    def body(main, wt, wt_out):
        _wt_battle_json(main, wt)
        spec = str(main / ".legion" / "battles" / "B" / "spec.md")
        for root in (wt, main):   # session dans le worktree, puis repli dans le principal
            def ev(p, a="claude"):
                return _ev("Edit", a, p)
            assert "protege" not in _decide(ev(str(wt / "src" / "x")), root, main)[1], root  # ni C8
            code, msg = _decide(ev(str(main / "src" / "x")), root, main)
            assert code == 2 and str(wt) in msg and "LEGION_GUARD_OFF=1" in msg, (root, code, msg)
            assert _decide(ev(spec), root, main)[0] == 0, root
            assert "protege" not in _decide(ev(str(main / ".claude" / "worktrees" / "agent-x" / "src" / "a")), root, main)[1]  # ni C8
            assert "protege" not in _decide(ev(str(wt_out / "src" / "x")), root, main)[1]  # ni C8
            assert "protege" not in _decide(ev(str(Path.home() / ".claude/projects/p/memory/x.md")), root, main)[1]  # ni C8
            assert _decide(ev(str(main / "src" / "x"), "legion:builder"), root, main)[0] == 2
            assert _decide(ev(str(main / "src" / "x"), "legion:lint"), root, main)[0] == 2  # gate : confinement
        # relatif depuis le worktree : <wt>/src/x permis, <wt>/.legion/x bloque (guard arme)
        assert _decide(_ev("Edit", "claude", "src/x"), wt, main)[0] == 0
        assert _decide(_ev("Edit", "claude", ".legion/x"), wt, main)[0] == 2
        # guard non arme : le principal reste protege
        _wt_battle_json(main, wt, "[]")
        assert _decide(_ev("Edit", "claude", str(main / "src" / "x")), wt, main)[0] == 2
        assert _decide(_ev("Edit", "claude", str(wt / "docs" / "x")), wt, main)[0] == 0
        # sous-processus reel (cwd = worktree) + bypass
        _wt_battle_json(main, wt)
        code, err = _run_hook(_ev("Edit", "claude", str(main / "src" / "x")), wt)
        assert code == 2 and "protege" in err, (code, err)
        code, err = _run_hook(_ev("Edit", "claude", str(main / "src" / "x")), wt, LEGION_GUARD_OFF="1")
        assert code == 0 and "[guard bypass]" in err, (code, err)
    _with_wt(bs, "_t_guard_wt_main_protected", body)


def _t_guard_wt_main_inactive(bs) -> None:
    """C8 sans effet : bloc absent / null / chemin disparu / invalide ; battle.json illisible = fail-closed existant."""
    def body(main, wt, wt_out):
        target = str(main / "src" / "x")
        _wt_battle_json(main, wt, worktree=False)                     # legacy : decisions d'avant
        assert _decide(_ev("Edit", "claude", target), main)[0] == 0
        assert _decide(_ev("Edit", "claude", str(main / "docs" / "x")), main)[0] == 2   # guard arme, hors allow
        bdir = main / ".legion" / "battles" / "B"
        (bdir / "battle.json").write_text('{"guard":{"allow":["src/**"]},"worktree":null}', encoding="utf-8")
        assert _decide(_ev("Edit", "claude", target), main)[0] == 0
        _wt_battle_json(main, wt_out / "disparu")                      # chemin inexistant
        assert _decide(_ev("Edit", "claude", target), main)[0] == 0
        for bad in ('{"guard":{"allow":["src/**"]},"worktree":"x"}',
                    '{"guard":{"allow":["src/**"]},"worktree":{"path":3}}'):
            (bdir / "battle.json").write_text(bad, encoding="utf-8")
            assert _decide(_ev("Edit", "claude", target), main)[0] == 0, bad
        (bdir / "battle.json").write_text("{pas du json", encoding="utf-8")   # illisible : fail-closed
        assert _decide(_ev("Edit", "claude", target), main)[0] == 2
    _with_wt(bs, "_t_guard_wt_main_inactive", body)


def _self_test() -> int:
    global _IMPORT_ERROR
    if _IMPORT_ERROR is not None:
        print(f"FAIL: import de scripts/battle_state.py impossible ({_IMPORT_ERROR})", file=sys.stderr)
        return 1
    import battle_state
    # tables derivees de la source unique (memes cles prefixees, memes artefacts)
    assert GATE_ARTIFACT == {PLUGIN_PREFIX + g: a for g, a in battle_state.GATE_ARTIFACT.items()}
    assert PRODUCER_ARTIFACT == {PLUGIN_PREFIX + g: a for g, a in battle_state.PRODUCER_ARTIFACT.items()}
    assert set(GATE_ARTIFACT) == {f"legion:{g}" for g in battle_state.GATES}
    assert PRODUCER_ARTIFACT == {"legion:builder": "build-report.md"}
    # glob matching
    assert _matches("src/Billing.Api/Foo.cs", ["src/Billing.Api/**"])
    assert _matches("tests/Bar.cs", ["tests/**"])
    assert not _matches("src/Other/Foo.cs", ["src/Billing.Api/**"])
    assert _matches("a/b.cs", ["a/*.cs"])
    assert not _matches("a/b/c.cs", ["a/*.cs"])  # * ne franchit pas le slash
    assert _matches(".legion/battles/x/battle.json", ALWAYS_ALLOW)
    assert _matches(".gitignore", ALWAYS_ALLOW)            # setup orchestrateur, sous freeze
    assert not _matches("src/x/.gitignore", ALWAYS_ALLOW)  # ancre : seul le .gitignore racine
    # memoire de Claude : exemptee, mais ANCREE sur le home reel (pas un suffixe wildcard)
    assert _is_claude_memory(str(Path.home() / ".claude/projects/p/memory/x.md"))
    assert _is_claude_memory(str(Path.home() / ".claude/projects/p/memory/sub/x.md"))
    assert not _is_claude_memory(str(Path.home() / ".claude/projects/p/other/x.md"))
    assert not _is_claude_memory(str(Path.home() / ".claude/settings.json"))
    # contournement par suffixe (chemin hors home se terminant par le motif) -> bloque
    assert not _is_claude_memory("C:/repo/.claude/projects/x/memory/evil.sh")
    assert not _is_claude_memory("C:/repo/src/Foo.cs")
    # confinement des gates (fonction pure)
    assert _gate_decision("claude", "src/x.cs", "B") is None            # session principale -> standard
    assert _gate_decision("legion:builder", "src/x.cs", "B") is None    # builder -> standard
    assert _gate_decision("legion:reviewer", ".legion/battles/B/gate-review.md", "B") is True
    assert _gate_decision("legion:architect", ".legion/battles/B/plan.md", "B") is True
    assert _gate_decision("legion:lint", ".legion/battles/B/gate-lint.md", "B") is True
    assert _gate_decision("legion:lint", ".legion/battles/B/gate-review.md", "B") is False  # pas SON artefact
    assert _gate_decision("legion:pr-triage", ".legion/battles/B/pr-feedback.md", "B") is True
    assert _gate_decision("legion:reviewer", ".legion/battles/B/gate-test.md", "B") is False   # pas SON artefact
    assert _gate_decision("legion:reviewer", "src/Foo.cs", "B") is False                        # pas de code
    assert _gate_decision("legion:reviewer", ".legion/battles/B/battle.json", "B") is False     # pas battle.json
    assert _gate_decision("legion:reviewer", ".legion/battles/B/gate-review.md", None) is False # hors battle active
    assert _gate_decision("legion:reviewer", None, "B") is False                                # chemin hors repo
    # producteur (builder) sous .legion/ : seul build-report.md de la battle active
    assert _producer_state_decision("legion:builder", ".legion/battles/B/build-report.md", "B") is True
    assert _producer_state_decision("legion:builder", ".legion/battles/B/battle.json", "B") is False
    assert _producer_state_decision("legion:builder", battle_state._pointer_path(Path(".")).as_posix(), "B") is False
    assert _producer_state_decision("legion:builder", ".legion/battles/B/gate-review.md", "B") is False
    assert _producer_state_decision("legion:builder", ".legion/battles/B/build-report.md", None) is False
    assert _producer_state_decision("legion:builder", "src/x.cs", "B") is None      # hors .legion -> standard
    assert _producer_state_decision("claude", ".legion/battles/B/battle.json", "B") is None  # orchestrateur libre
    # rapport par slice (GH#129) : G1 fichiers distincts, G4 autre battle, G5 hors motif, G7 casse
    for name in ("build-report-slice-1.md", "build-report-slice-2.md", "build-report-s1.md"):
        assert _producer_state_decision("legion:builder", f".legion/battles/B/{name}", "B") is True, name
    assert _producer_state_decision("legion:builder", ".Legion/battles/B/Build-Report-Slice-1.md", "B") is True
    assert _producer_state_decision("legion:builder", ".legion/battles/AUTRE/build-report-slice-1.md", "B") is False
    for name in ("build-report-.md", "build-report--x.md", "build-report-a_b.md", "build-report-a.b.md",
                 "build-report-s1.md.bak", "build-report-s1.txt", "x/build-report-s1.md",
                 "gate-review.md", "battle.json"):
        assert _producer_state_decision("legion:builder", f".legion/battles/B/{name}", "B") is False, name
    assert _producer_state_decision("legion:builder", ".legion/battles/B/build-report-slice-1.md", None) is False
    assert _producer_state_decision("legion:reviewer", ".legion/battles/B/build-report-slice-1.md", "B") is None
    # casse (disque insensible) : .LEGION est le meme dossier -> bloque ; rapport en casse differente -> ok
    assert _producer_state_decision("legion:builder", ".LEGION/battles/B/battle.json", "B") is False
    assert _producer_state_decision("legion:builder", ".Legion/battles/B/Build-Report.md", "B") is True
    # .legion hors de la racine du hook (checkout principal vu d'un worktree) -> bloque
    assert _producer_state_decision("legion:builder", None, "B",
                                    ("/", "main", ".legion", "battles", "B", "battle.json")) is False
    assert _producer_state_decision("legion:builder", None, "B", ("/", "tmp", "x.cs")) is None
    # decision : pas de write tool -> 0
    assert _decide({"tool_name": "Bash"}, Path.cwd())[0] == 0
    # artefact vide (RETEX A1) : contenu blanc -> bloque ; contenu reel -> autorise
    assert _is_blank_content("") and _is_blank_content("  \n\t ") and _is_blank_content(None)
    assert not _is_blank_content("# Review\nverdict")
    with tempfile.TemporaryDirectory() as _d:
        _root = Path(_d)
        (_root / ".legion" / "battles" / "B").mkdir(parents=True)
        battle_state._write_pointer(_root, "B")
        (_root / ".legion" / "battles" / "B" / "battle.json").write_text(
            '{"guard":{"allow":[]}}', encoding="utf-8"
        )
        _art = {"file_path": ".legion/battles/B/gate-review.md"}
        # une gate qui Write son artefact VIDE -> bloque (meme guard non arme)
        assert _decide({"tool_name": "Write", "agent_type": "legion:reviewer",
                        "tool_input": {**_art, "content": "   \n"}}, _root)[0] == 2
        # le meme artefact avec un contenu reel -> autorise
        assert _decide({"tool_name": "Write", "agent_type": "legion:reviewer",
                        "tool_input": {**_art, "content": "# Review\nverdict"}}, _root)[0] == 0
        # un Edit (pas de contenu complet) n'est pas soumis a la regle du Write vide
        assert _decide({"tool_name": "Edit", "agent_type": "legion:reviewer",
                        "tool_input": _art}, _root)[0] == 0
        # builder : battle.json bloque (meme guard non arme), build-report.md autorise,
        # code hors .legion laisse aux regles standard (guard non arme -> libre)
        assert _decide({"tool_name": "Edit", "agent_type": "legion:builder",
                        "tool_input": {"file_path": ".legion/battles/B/battle.json"}}, _root)[0] == 2
        assert _decide({"tool_name": "Write", "agent_type": "legion:builder",
                        "tool_input": {"file_path": ".legion/battles/B/build-report.md",
                                       "content": "# Build"}}, _root)[0] == 0
        assert _decide({"tool_name": "Write", "agent_type": "legion:builder",
                        "tool_input": {"file_path": ".legion/battles/B/build-report-slice-1.md",
                                       "content": "## slice-1"}}, _root)[0] == 0   # G10 : rapport de slice
        code, msg = _decide({"tool_name": "Edit", "agent_type": "legion:builder",
                             "tool_input": {"file_path": ".legion/battles/B/gate-review.md"}}, _root)
        assert code == 2 and "build-report.md" in msg and "build-report-<slice_id>.md" in msg, (code, msg)  # G11
        assert _decide({"tool_name": "Edit", "agent_type": "legion:builder",
                        "tool_input": {"file_path": "src/x.cs"}}, _root)[0] == 0
        # session principale (orchestrateur) : battle.json toujours ecrivable
        assert _decide({"tool_name": "Edit", "agent_type": "claude",
                        "tool_input": {"file_path": ".legion/battles/B/battle.json"}}, _root)[0] == 0
    # repli simule (import echoue) : gates bloquees, builder bloque sous .legion/ seulement,
    # session principale en regles standard
    saved, _IMPORT_ERROR = _IMPORT_ERROR, "ImportError: simule"
    try:
        with tempfile.TemporaryDirectory() as _d:
            _root = Path(_d)
            _w = {"tool_name": "Write"}
            code, msg = _decide({**_w, "agent_type": "legion:reviewer",
                                 "tool_input": {"file_path": ".legion/battles/B/gate-review.md",
                                                "content": "x"}}, _root)
            assert code == 2 and "installation legion incomplete" in msg, (code, msg)
            assert _decide({**_w, "agent_type": "legion:lint",
                            "tool_input": {"file_path": "src/x.cs"}}, _root)[0] == 2
            assert _decide({**_w, "agent_type": "legion:builder",
                            "tool_input": {"file_path": ".legion/battles/B/build-report.md"}}, _root)[0] == 2
            assert _decide({**_w, "agent_type": "legion:builder",
                            "tool_input": {"file_path": ".LEGION/battles/B/battle.json"}}, _root)[0] == 2
            assert _decide({**_w, "agent_type": "legion:builder",
                            "tool_input": {"file_path": ".legion/battles/B/build-report-slice-1.md"}}, _root)[0] == 2  # G12
            # C1 (option B) : le builder est bloque pour TOUTE ecriture en repli
            assert _decide({**_w, "agent_type": "legion:builder",
                            "tool_input": {"file_path": "src/x.cs"}}, _root)[0] == 2
            # session principale : libre, avec un avertissement stderr
            code, msg = _decide({**_w, "agent_type": "claude",
                                 "tool_input": {"file_path": ".legion/battles/B/battle.json"}}, _root)
            assert code == 0 and "/freeze non applique" in msg, (code, msg)
            # Bash en repli (GH#66) : gate et builder fermes, session principale silencieuse
            _bash = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
            assert _decide({**_bash, "agent_type": "legion:reviewer"}, _root)[0] == 2
            assert _decide({**_bash, "agent_type": "legion:builder"}, _root)[0] == 2
            assert _decide({**_bash, "agent_type": "claude"}, _root) == (0, "")
    finally:
        _IMPORT_ERROR = saved

    _t_guard_pointer_blank(battle_state)
    _t_guard_invalid_id(battle_state)
    _t_guard_unreadable_battle_json(battle_state)
    _t_guard_freeze_nominal(battle_state)
    _t_guard_invalid_block(battle_state)
    _t_guard_invalid_fields(battle_state)
    _t_guard_valid_unarmed(battle_state)
    _t_guard_invalid_confinement(battle_state)
    _t_guard_unreadable_active(battle_state)
    _t_guard_safety_net()
    _t_guard_stdin_blank()
    _t_guard_stdin_invalid_json()
    _t_guard_stdin_huge_int()
    _t_guard_stdin_non_object()
    _t_guard_stdin_undecodable()
    _t_guard_main_no_exit_1()
    _t_guard_repair_messages(battle_state)
    _t_shell_pure()
    _t_shell_blocked(battle_state)
    _t_shell_allowed(battle_state)
    _t_shell_routing(battle_state)
    _t_guard_wt_subprocess(battle_state)
    _t_guard_wt_standard(battle_state)
    _t_guard_wt_builder(battle_state)
    _t_guard_wt_gate(battle_state)
    _t_guard_wt_always_allowed(battle_state)
    _t_guard_wt_relative(battle_state)
    _t_guard_wt_invalid(battle_state)
    _t_guard_wt_shell(battle_state)
    _t_guard_wt_compat(battle_state)
    _t_guard_wt_main_protected(battle_state)
    _t_guard_wt_main_inactive(battle_state)

    print("OK: guard self-test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
