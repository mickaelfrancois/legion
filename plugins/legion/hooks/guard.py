"""Hook PreToolUse (legion) : applique le perimetre d'ecriture de la battle active.

Quand une battle est active (pointeur `.legion/active-battle`) et que son
`guard.allow` est non vide (pose par `/freeze` ou `/guard`), toute ecriture
hors perimetre est **bloquee** (exit 2 + message stderr).

Regles :
- Pas de battle active, ou `guard.allow` vide -> exit 0 (edition libre).
- `.legion/**` toujours autorise (etat de la battle, ecrit par l'orchestrateur).
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
ALWAYS_ALLOW = (".legion/**",)  # l'etat de la battle reste ecrivable sous freeze


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


def _decide(data: dict, repo_root: Path) -> tuple[int, str]:
    """Retourne (exit_code, message). exit 2 = blocage."""
    if data.get("tool_name") not in WRITE_TOOLS:
        return 0, ""

    active = _load_active_guard(repo_root)
    if active is None:
        return 0, ""
    battle_id, allow, deny = active
    if not allow:
        return 0, ""  # guard non arme

    file_path = (data.get("tool_input") or {}).get("file_path", "")
    if not file_path:
        return 0, ""

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
    # decision : pas de write tool -> 0
    assert _decide({"tool_name": "Bash"}, Path.cwd())[0] == 0
    print("OK: guard self-test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
