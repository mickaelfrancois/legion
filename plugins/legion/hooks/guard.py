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
- **Builder sous `.legion/`** : le `builder` n'y ecrit QUE son `build-report.md`
  (battle active). Sinon `.legion/**` (toujours autorise) lui permettrait de
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
- file_path doit matcher >= 1 glob de `allow` ET aucun de `deny` -> autorise.
- Hors perimetre -> exit 2 (blocage) avec la battle et les globs autorises.
- Bypass delibere : env var `LEGION_GUARD_OFF=1` (log, ne bloque pas).

Les globs sont relatifs a la racine du repo (cwd du hook). `**` matche tout
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
    GATE_ARTIFACT = {PLUGIN_PREFIX + g: a for g, a in _GATE_ARTIFACT_SRC.items()}
    # Producteur : hors `.legion/`, regles de perimetre standard ; SOUS `.legion/`, seul
    # son rapport est autorise (jamais `battle.json` -> pas d'auto-elargissement du guard).
    PRODUCER_ARTIFACT = {PLUGIN_PREFIX + g: a for g, a in _PRODUCER_ARTIFACT_SRC.items()}
except Exception as _exc:  # ImportError, SyntaxError du module... jamais planter a l'import
    _IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"
    GATE_ARTIFACT = {}
    PRODUCER_ARTIFACT = {}


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Traduit un glob (`**`, `*`, `?`) en regex ancree, en chemins posix."""
    pattern = pattern.replace("\\", "/")
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1  # le .* couvre deja le slash
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _matches(rel_path: str, patterns) -> bool:
    rel_path = rel_path.replace("\\", "/")
    return any(_glob_to_regex(p).search(rel_path) for p in patterns)


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
    - True  -> son rapport dans la battle active : autorise.
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
    return rel.casefold() == f".legion/battles/{battle_id}/{artifact}".casefold()


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
    data: dict, repo_root: Path, battle_id: str, file_path: str, unreadable: bool = False
) -> tuple[int, str]:
    """Etat de guard invalide ou illisible : perimetre inconnu -> ferme hors toujours-autorises (GH#104)."""
    if not file_path or _is_claude_memory(file_path):
        return 0, ""
    rel = _relative(repo_root, file_path)
    if rel is not None and _matches(rel, ALWAYS_ALLOW):
        return 0, ""  # `.legion/**` + `.gitignore` : la reparation reste possible
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


def _decide(data: dict, repo_root: Path) -> tuple[int, str]:
    """Retourne (exit_code, message). exit 2 = blocage."""
    if data.get("tool_name") not in WRITE_TOOLS:
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
        battle_id = active_battle_id(repo_root)
        rel = _relative(repo_root, file_path) if file_path else None
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
        battle_id = active_battle_id(repo_root)
        rel = _relative(repo_root, file_path)
        decision = _producer_state_decision(
            agent_type, rel, battle_id, _resolved_parts(repo_root, file_path.replace("\\", "/"))
        )
        if decision is False:
            expected = f".legion/battles/{battle_id or '<aucune battle active>'}/{PRODUCER_ARTIFACT[agent_type]}"
            return 2, (
                f"BLOQUE : le `{agent_type}` n'ecrit sous `.legion/` QUE son rapport "
                f"`{expected}`.\nTentative : `{rel}`.\n"
                f"L'etat de la battle (`battle.json`, perimetre, artefacts de gate) "
                f"appartient a l'orchestrateur."
            )
        if decision is True:
            return 0, ""

    active = _load_active_guard(repo_root)
    if active is None:
        return 0, ""
    battle_id, allow, deny, valid, unreadable = active
    if not valid:
        return _invalid_guard_decision(data, repo_root, battle_id, file_path, unreadable=unreadable)
    if not allow:
        return 0, ""  # guard non arme

    if not file_path:
        return 0, ""

    if _is_claude_memory(file_path):
        return 0, ""  # memoire de Claude : hors perimetre repo, jamais bloquee

    rel = _relative(repo_root, file_path)
    if rel is None:
        return 2, (
            f"BLOQUE par le guard de la battle {battle_id} : ecriture hors du repo "
            f"alors qu'un perimetre est actif.\nGlobs autorises : {allow}\n"
            f"Bypass delibere : LEGION_GUARD_OFF=1"
        )

    if _matches(rel, ALWAYS_ALLOW):
        return 0, ""
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


def _safe_decide(data, repo_root: Path) -> tuple[int, str]:
    """`_decide` avec filet final : toute exception imprevue -> exit 2 (jamais exit 1, non bloquant)."""
    try:
        return _decide(data, repo_root)
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

        code, message = _safe_decide(data, Path.cwd())

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

    def boom(data, root):
        raise RuntimeError("injecte")

    def deep(data, root):
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

    code, _ = _run_main('{"tool_name":"Edit"}', stderr=_BrokenErr(), _decide=lambda d, r: (2, "x"))
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
            # C1 (option B) : le builder est bloque pour TOUTE ecriture en repli
            assert _decide({**_w, "agent_type": "legion:builder",
                            "tool_input": {"file_path": "src/x.cs"}}, _root)[0] == 2
            # session principale : libre, avec un avertissement stderr
            code, msg = _decide({**_w, "agent_type": "claude",
                                 "tool_input": {"file_path": ".legion/battles/B/battle.json"}}, _root)
            assert code == 0 and "/freeze non applique" in msg, (code, msg)
            assert _decide({"tool_name": "Bash", "agent_type": "legion:reviewer"}, _root)[0] == 0
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

    print("OK: guard self-test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
