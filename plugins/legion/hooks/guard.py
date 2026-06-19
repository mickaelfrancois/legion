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
  `<plugin>:architect`/`reviewer`/`test-engineer`/`security`/`pr-triage`) ne peut
  ecrire QUE son unique artefact dans `.legion/battles/<active>/` (ni code, ni
  `battle.json`) -> hors de la -> exit 2. Rend structurelle (portee par le hook) la
  garantie « une gate ne touche pas le code ». La session principale (`agent_type`
  "claude") et le `builder` (`<plugin>:builder`) ne sont **pas** confines : regles
  de perimetre standard ci-dessous. S'applique meme guard non arme.
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
from pathlib import Path

ACTIVE_POINTER = Path(".legion/active-battle")
BATTLES_DIR = Path(".legion/battles")
WRITE_TOOLS = ("Edit", "Write", "MultiEdit")
ALWAYS_ALLOW = (".legion/**", ".gitignore")  # etat de la battle + .gitignore (setup orchestrateur)

# Confinement des gates. `agent_type` (payload PreToolUse) vaut le nom NAMESPACE du
# sous-agent appelant (`<plugin>:<agent>`) ; la session principale vaut "claude" et
# le builder "<plugin>:builder" (tous deux hors table -> regles de perimetre standard).
# Chaque gate listee ici ne peut ecrire QUE l'artefact associe, dans le dossier de
# la battle active. Le prefixe de plugin (`legion:`) doit matcher le `name` du
# marketplace ; sur un fork (ex. `divalto-legion`) adapter le prefixe.
GATE_ARTIFACT = {
    "legion:architect": "plan.md",
    "legion:reviewer": "gate-review.md",
    "legion:test-engineer": "gate-test.md",
    "legion:security": "gate-security.md",
    "legion:pr-triage": "pr-feedback.md",
}


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
    """Retourne (battle_id, allow, deny) de la battle active, ou None."""
    pointer = repo_root / ACTIVE_POINTER
    if not pointer.is_file():
        return None
    battle_id = pointer.read_text(encoding="utf-8").strip()
    if not battle_id:
        return None
    battle_json = repo_root / BATTLES_DIR / battle_id / "battle.json"
    if not battle_json.is_file():
        return None
    try:
        data = json.loads(battle_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    guard = data.get("guard") or {}
    allow = guard.get("allow") or []
    deny = guard.get("deny") or []
    return battle_id, allow, deny


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


def _active_battle_id(repo_root: Path) -> str | None:
    """Id de la battle active (pointeur seul), independamment de `guard.allow`.

    `_load_active_guard` renvoie None des que `guard.allow` est vide ; le confinement
    des gates doit s'appliquer meme guard non arme, d'ou ce lecteur dedie du pointeur.
    """
    pointer = repo_root / ACTIVE_POINTER
    if not pointer.is_file():
        return None
    battle_id = pointer.read_text(encoding="utf-8").strip()
    return battle_id or None


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


def _decide(data: dict, repo_root: Path) -> tuple[int, str]:
    """Retourne (exit_code, message). exit 2 = blocage."""
    if data.get("tool_name") not in WRITE_TOOLS:
        return 0, ""

    file_path = (data.get("tool_input") or {}).get("file_path", "")

    # Confinement des gates : une gate n'ecrit QUE son artefact (cf. GATE_ARTIFACT).
    # Prioritaire sur tout le reste, et actif meme guard non arme.
    agent_type = data.get("agent_type")
    if agent_type in GATE_ARTIFACT:
        battle_id = _active_battle_id(repo_root)
        rel = _relative(repo_root, file_path) if file_path else None
        if _gate_decision(agent_type, rel, battle_id):
            return 0, ""
        expected = f".legion/battles/{battle_id or '<aucune battle active>'}/{GATE_ARTIFACT[agent_type]}"
        return 2, (
            f"BLOQUE : la gate `{agent_type}` ne peut ecrire QUE son artefact "
            f"`{expected}`.\nTentative : `{rel}`.\n"
            f"Une gate retourne son verdict + le chemin de son artefact ; elle "
            f"n'ecrit ni code, ni `battle.json`, ni l'artefact d'une autre gate."
        )

    active = _load_active_guard(repo_root)
    if active is None:
        return 0, ""
    battle_id, allow, deny = active
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


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return 0

    code, message = _decide(data, Path.cwd())

    if code == 2 and os.environ.get("LEGION_GUARD_OFF") == "1":
        print(f"[guard bypass] {message}", file=sys.stderr)
        return 0
    if message:
        print(message, file=sys.stderr)
    return code


def _self_test() -> int:
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
    assert _gate_decision("legion:pr-triage", ".legion/battles/B/pr-feedback.md", "B") is True
    assert _gate_decision("legion:reviewer", ".legion/battles/B/gate-test.md", "B") is False   # pas SON artefact
    assert _gate_decision("legion:reviewer", "src/Foo.cs", "B") is False                        # pas de code
    assert _gate_decision("legion:reviewer", ".legion/battles/B/battle.json", "B") is False     # pas battle.json
    assert _gate_decision("legion:reviewer", ".legion/battles/B/gate-review.md", None) is False # hors battle active
    assert _gate_decision("legion:reviewer", None, "B") is False                                # chemin hors repo
    # decision : pas de write tool -> 0
    assert _decide({"tool_name": "Bash"}, Path.cwd())[0] == 0
    print("OK: guard self-test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
