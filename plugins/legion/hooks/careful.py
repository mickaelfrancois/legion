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
import tempfile
from pathlib import Path

# Lecteur partage de la battle active (source unique : scripts/battle_state.py, GH#85).
# Chemin resolu depuis `__file__` (jamais le cwd). Si l'import echoue (installation
# incomplete), le hook ne plante pas : il se desactive (jamais de blocage) et son
# --self-test echoue (exit 1).
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_IMPORT_ERROR: str | None = None
try:
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    from battle_state import guard_of, load_active_battle, resolve_state_root
except Exception as _exc:  # ImportError, SyntaxError du module... jamais planter a l'import
    _IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

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
    active = load_active_battle(repo_root)
    if active is None:
        return False
    guard, valid = guard_of(active[1])  # GH#104 : bloc invalide -> careful inactif
    return valid and bool(guard.get("careful"))


def _command_text(data: dict) -> str:
    inp = data.get("tool_input") or {}
    return str(inp.get("command", ""))


def _match(command: str):
    for pattern, label in DESTRUCTIVE:
        if pattern.search(command):
            return label
    return None


def _handle(data: dict, repo_root: Path) -> tuple[int, str]:
    """Decision du hook : (exit_code, message stderr). Ne bloque jamais (exit 0)."""
    if data.get("tool_name") not in SHELL_TOOLS:
        return 0, ""
    if _IMPORT_ERROR is not None:
        return 0, f"[careful] careful desactive : installation legion incomplete ({_IMPORT_ERROR})."
    if not _careful_active(repo_root):
        return 0, ""
    label = _match(_command_text(data))
    if label:
        return 0, (
            f"[careful] Commande destructrice detectee : {label}.\n"
            f"Mode `careful` actif sur la battle -- verifie l'intention avant de "
            f"valider. (Cet avertissement ne bloque pas.)"
        )
    return 0, ""


def _t_careful_match() -> None:
    assert _match("rm -rf build") == "rm -rf"
    assert _match("git push --force origin main") == "git push --force"
    assert _match("dotnet build") is None
    assert _match("Remove-Item -Recurse -Force .\\bin") is not None


def _make_repo(d: str, pointer: str | None, battle_json: str | None = None) -> Path:
    import battle_state
    root = Path(d)
    if pointer is not None:
        battle_state._write_pointer(root, pointer)
    if battle_json is not None:
        bdir = root / ".legion" / "battles" / "b1"
        bdir.mkdir(parents=True)
        (bdir / "battle.json").write_text(battle_json, encoding="utf-8")
    return root


def _t_careful_pointer_blank() -> None:
    rm = {"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}
    for value in ("", "  \n"):
        with tempfile.TemporaryDirectory() as d:
            root = _make_repo(d, value)
            assert _careful_active(root) is False
            assert _handle(rm, root) == (0, "")


def _t_careful_invalid_id() -> None:
    with tempfile.TemporaryDirectory() as d:
        assert _careful_active(_make_repo(d, "../x")) is False


def _t_careful_nominal() -> None:
    rm = {"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}
    with tempfile.TemporaryDirectory() as d:
        root = _make_repo(d, "b1", '{"guard":{"careful":true}}')
        assert _careful_active(root) is True
        code, msg = _handle(rm, root)
        assert code == 0 and "rm -rf" in msg, (code, msg)
        assert _handle({"tool_name": "Bash", "tool_input": {"command": "ls"}}, root) == (0, "")
    with tempfile.TemporaryDirectory() as d:
        root = _make_repo(d, "b1", '{"guard":{"careful":false}}')
        assert _careful_active(root) is False
        assert _handle(rm, root) == (0, "")


def _t_careful_invalid_guard() -> None:
    rm = {"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}
    for raw in ('["x"]', '"x"', '{"allow":"s","careful":true}'):
        with tempfile.TemporaryDirectory() as d:
            root = _make_repo(d, "b1", '{"guard":' + raw + '}')
            assert _careful_active(root) is False, raw
            assert _handle(rm, root) == (0, ""), raw


def _t_careful_import_fallback() -> None:
    global _IMPORT_ERROR
    saved, _IMPORT_ERROR = _IMPORT_ERROR, "ImportError: simule"
    try:
        with tempfile.TemporaryDirectory() as d:
            code, msg = _handle({"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}, Path(d))
            assert code == 0 and "desactive" in msg, (code, msg)
    finally:
        _IMPORT_ERROR = saved


def _run_hook(cwd: Path, payload: dict):
    import subprocess
    return subprocess.run([sys.executable, str(Path(__file__).resolve())], input=json.dumps(payload),
                          cwd=str(cwd), capture_output=True, text=True, timeout=30)


def _careful_worktree(careful: bool):
    import battle_state
    rm = {"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}
    with tempfile.TemporaryDirectory() as tmp:
        fx = battle_state._git_worktree_fixture(Path(tmp))
        if fx is None:
            print("SKIP: careful worktree (git absent ou inutilisable)", file=sys.stderr)
            return
        main, wt, _ = fx
        bdir = main / ".legion" / "battles" / "B"
        bdir.mkdir(parents=True)
        (bdir / "battle.json").write_text(json.dumps({"guard": {"careful": careful}}), encoding="utf-8")
        proc = _run_hook(wt, rm)
        assert proc.returncode == 0, proc
        if careful:
            assert "[careful]" in proc.stderr, proc.stderr  # C1
        else:
            assert proc.stderr == "", proc.stderr  # C2


def _t_careful_worktree_on() -> None:  # C1
    _careful_worktree(True)


def _t_careful_worktree_off() -> None:  # C2
    _careful_worktree(False)


def _t_careful_not_git() -> None:  # C3
    rm = {"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}
    with tempfile.TemporaryDirectory() as d:
        assert _handle(rm, Path(d)) == (0, "")
        assert _run_hook(Path(d), rm).returncode == 0


def _self_test() -> int:
    if _IMPORT_ERROR is not None:
        print(f"FAIL: import de scripts/battle_state.py impossible ({_IMPORT_ERROR})", file=sys.stderr)
        return 1
    _t_careful_match()
    _t_careful_pointer_blank()
    _t_careful_invalid_id()
    _t_careful_nominal()
    _t_careful_invalid_guard()
    _t_careful_import_fallback()
    _t_careful_worktree_on()
    _t_careful_worktree_off()
    _t_careful_not_git()
    print("OK: careful self-test passed", file=sys.stderr)
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return 0

    # GH#68 : depuis un worktree lie, la battle vit dans le depot principal (racine d'etat).
    cwd = Path.cwd()
    root = cwd if _IMPORT_ERROR is not None else resolve_state_root(cwd)
    code, message = _handle(data, root)
    if message:
        print(message, file=sys.stderr)
    return code  # ne bloque jamais


if __name__ == "__main__":
    sys.exit(main())
