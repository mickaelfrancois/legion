"""Hook PreToolUse (legion) : mode `careful` -- avertit sur les commandes
destructrices SANS bloquer.

Active uniquement si une battle est active et que son `guard.careful` est vrai
(pose par `/careful`). Dans ce mode, une commande Bash/PowerShell qui matche un
motif destructeur declenche un avertissement stderr (exit 0 -- jamais de blocage,
contrairement a `guard.py`). Objectif : faire reflechir, pas empecher.

Tests CLI :
    py careful.py --self-test
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ACTIVE_POINTER = Path(".legion/active-battle")
BATTLES_DIR = Path(".legion/battles")
SHELL_TOOLS = ("Bash", "PowerShell")

# Motifs destructeurs (regex, libelle). Defauts -- ajustables selon la pratique.
DESTRUCTIVE = [
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r", re.I), "rm -rf"),
    (re.compile(r"\bgit\s+reset\s+--hard", re.I), "git reset --hard"),
    (re.compile(r"\bgit\s+push\b.*(--force|\s-f\b)", re.I), "git push --force"),
    (re.compile(r"\bgit\s+clean\s+-[a-z]*f", re.I), "git clean -f"),
    (re.compile(r"\bgit\s+checkout\s+--\s", re.I), "git checkout -- (discard)"),
    (re.compile(r"Remove-Item\b.*-Recurse\b.*-Force|Remove-Item\b.*-Force\b.*-Recurse", re.I), "Remove-Item -Recurse -Force"),
    (re.compile(r"\bdotnet\s+ef\s+database\s+drop", re.I), "dotnet ef database drop"),
    (re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", re.I), "DROP TABLE/DATABASE"),
    (re.compile(r"\bTRUNCATE\s+TABLE\b", re.I), "TRUNCATE TABLE"),
]


def _careful_active(repo_root: Path) -> bool:
    pointer = repo_root / ACTIVE_POINTER
    if not pointer.is_file():
        return False
    battle_id = pointer.read_text(encoding="utf-8").strip()
    if not battle_id:
        return False
    bj = repo_root / BATTLES_DIR / battle_id / "battle.json"
    if not bj.is_file():
        return False
    try:
        data = json.loads(bj.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return bool((data.get("guard") or {}).get("careful"))


def _command_text(data: dict) -> str:
    inp = data.get("tool_input") or {}
    return str(inp.get("command", ""))


def _match(command: str):
    for pattern, label in DESTRUCTIVE:
        if pattern.search(command):
            return label
    return None


def main() -> int:
    if "--self-test" in sys.argv:
        assert _match("rm -rf build") == "rm -rf"
        assert _match("git push --force origin main") == "git push --force"
        assert _match("dotnet build") is None
        assert _match("Remove-Item -Recurse -Force .\\bin") is not None
        print("OK: careful self-test passed", file=sys.stderr)
        return 0

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return 0

    if data.get("tool_name") not in SHELL_TOOLS:
        return 0
    if not _careful_active(Path.cwd()):
        return 0

    label = _match(_command_text(data))
    if label:
        print(
            f"[careful] Commande destructrice detectee : {label}.\n"
            f"Mode `careful` actif sur la battle -- verifie l'intention avant de "
            f"valider. (Cet avertissement ne bloque pas.)",
            file=sys.stderr,
        )
    return 0  # ne bloque jamais


if __name__ == "__main__":
    sys.exit(main())
