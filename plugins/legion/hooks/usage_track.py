"""Hook (legion) : agrege le cout token + les skills reellement utilises
dans la battle active, pour restitution (`/retro`) et affichage UI (shard fleet).

Le payload de hook n'expose PAS les tokens : la source de verite est le transcript
JSONL (chaque tour assistant porte un bloc `message.usage`, chaque skill un
`tool_use` `name="Skill"` / `input.skill`). On branche sur deux events :

- **SubagentStop** : un sous-agent (builder, gate) vient de finir. On lit SON
  transcript (`agent_transcript_path`), on somme son usage + collecte ses skills,
  on append a la battle active. C'est ainsi qu'on capte le travail delegue, invisible
  aux hooks de la session principale.
- **Stop** : fin d'un tour principal. On recalcule le total token de la session et
  on append le DELTA depuis le dernier passage (curseur baseline) + les skills du
  delta. Attribue l'orchestrateur inline a la battle active.

Sortie = append-only `.legion/battles/<active>/usage.jsonl` (pas de
read-modify-write partage => sur en concurrence). La battle est
resolue par `battle_state.resolve_battle` (GH#170) : liaison de la session du payload, sinon pointeur
sous `.legion/` de la racine d'etat (depot principal depuis un worktree lie, sinon le cwd, GH#68) ;
une session `foreign` (autre session liee au pointeur) n'attribue rien (#171). Le curseur du `Stop`
est par cle de session (`.usage-main-<cle>.json`), `.usage-main.json` sans cle. Sans battle active => no-op immediat (le hook tourne
dans toutes les sessions, il doit etre quasi gratuit hors battle).

Tests : py usage_track.py --self-test
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Lecteurs partages de la battle active (source unique : scripts/battle_state.py, GH#85).
# Chemin resolu depuis `__file__`. Si l'import echoue, le hook est un no-op et son
# --self-test echoue (exit 1).
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_IMPORT_ERROR: str | None = None
try:
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    from battle_state import battles_dir, probe, resolve_battle, resolve_state_root, session_keys
except Exception as _exc:  # ImportError, SyntaxError du module... jamais planter a l'import
    _IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

_TOKEN_KEYS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_creation": "cache_creation_input_tokens",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            os.remove(tmp)


def _iter_records(transcript_path: str):
    """Itere les lignes JSONL parsees d'un transcript, tolerant aux lignes KO."""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _tally(records) -> tuple[dict, list]:
    """Somme l'usage (4 compteurs) et collecte les skills (dans l'ordre) sur un
    transcript. `usage` et `content` vivent sous `message` (fallback top-level)."""
    tokens = {k: 0 for k in _TOKEN_KEYS}
    skills = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else rec
        usage = msg.get("usage")
        if isinstance(usage, dict):
            for short, full in _TOKEN_KEYS.items():
                tokens[short] += usage.get(full) or 0
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill":
                    skill = (block.get("input") or {}).get("skill")
                    if skill:
                        skills.append(skill)
    return tokens, skills


def _active_battle_dir(cwd: str, data: dict | None = None) -> Path | None:
    """Dossier de la battle de la session, ou None (import KO, session `foreign`, pointeur
    absent/vide/invalide, dossier absent). Un pointeur vide ne doit jamais viser
    `.legion/battles/` lui-meme."""
    if _IMPORT_ERROR is not None:
        return None
    root = resolve_state_root(Path(cwd))
    battle_id, _battle, _source = resolve_battle(root, data if isinstance(data, dict) else {})
    if battle_id is None:
        return None
    battle_dir = battles_dir(root) / battle_id
    return battle_dir if battle_dir.is_dir() else None


def _append_usage(battle_dir: Path, entry: dict) -> None:
    """Append-only : une ligne JSON par contribution. Pas de relecture/reecriture."""
    try:
        with open(battle_dir / "usage.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def handle_subagent(data: dict, battle_dir: Path) -> None:
    tpath = data.get("agent_transcript_path") or data.get("transcript_path")
    if not tpath:
        return
    tokens, skills = _tally(_iter_records(tpath))
    if any(tokens.values()) or skills:
        _append_usage(battle_dir, {
            "scope": "subagent",
            "agent_type": data.get("agent_type"),
            "skills": skills,
            "tokens": tokens,
            "ts": _now_iso(),
        })


def handle_stop(data: dict, battle_dir: Path) -> None:
    """Attribue a la battle le DELTA de la session depuis le dernier Stop (curseur
    baseline). Le 1er passage ne fait qu'initialiser la baseline (rien attribue)."""
    tpath = data.get("transcript_path")
    if not tpath:
        return
    tokens, skills = _tally(_iter_records(tpath))
    keys = session_keys(data)
    cursor_path = battle_dir / (f".usage-main-{keys[0]}.json" if keys else ".usage-main.json")
    cursor = _read_json(cursor_path)

    _atomic_write(cursor_path, {"tokens": tokens, "skills_count": len(skills)})
    if cursor is None:
        return  # baseline posee, on attribue a partir du prochain tour

    base = cursor.get("tokens") or {}
    delta = {k: tokens[k] - (base.get(k) or 0) for k in tokens}
    if any(v < 0 for v in delta.values()):
        return  # transcript compacte/reset : on repart de la nouvelle baseline
    new_skills = skills[cursor.get("skills_count", 0):]
    if any(v > 0 for v in delta.values()) or new_skills:
        _append_usage(battle_dir, {
            "scope": "main",
            "skills": new_skills,
            "tokens": delta,
            "ts": _now_iso(),
        })


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return 0

    if _IMPORT_ERROR is not None:
        print(f"[usage_track] suivi d'usage desactive : installation legion incomplete ({_IMPORT_ERROR}).",
              file=sys.stderr)
        return 0
    cwd = data.get("cwd") or os.getcwd()
    probe(data, resolve_state_root(Path(cwd)))  # sonde opt-in (noms de cles seulement)
    battle_dir = _active_battle_dir(cwd, data)
    if battle_dir is None:
        return 0  # pas de battle active : no-op

    try:
        event = data.get("hook_event_name")
        if event == "SubagentStop":
            handle_subagent(data, battle_dir)
        elif event == "Stop":
            handle_stop(data, battle_dir)
    except OSError:
        pass  # best-effort : ne jamais casser la session pour un suivi d'usage
    return 0


def _t_worktree() -> None:  # U1, U2
    import battle_state
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        fx = battle_state._git_worktree_fixture(Path(tmp))
        if fx is None:
            print("SKIP: usage_track worktree (git absent ou inutilisable)", file=sys.stderr)
            return
        main, wt, _ = fx
        bdir = main / ".legion" / "battles" / "B"
        bdir.mkdir(parents=True)
        (bdir / "battle.json").write_text("{}", encoding="utf-8")
        got = _active_battle_dir(str(wt))
        assert got is not None and os.path.realpath(got) == os.path.realpath(bdir), got  # U1
        tp = Path(tmp) / "t.jsonl"
        tp.write_text(json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 7, "output_tokens": 3}, "content": []}}) + "\n", encoding="utf-8")
        payload = {"hook_event_name": "SubagentStop", "agent_type": "builder",
                   "agent_transcript_path": str(tp), "cwd": str(wt)}
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve())], input=json.dumps(payload),
                              cwd=str(wt), capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc
        lines = (bdir / "usage.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["tokens"]["input"] == 7, lines  # U2
        assert not (wt / ".legion").exists()


def _t_sessions() -> None:  # U-S1 a U-S4
    import battle_state

    def line(n):
        return json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": n, "output_tokens": 0}, "content": []}}) + "\n"

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for bid in ("A", "B"):
            (root / ".legion" / "battles" / bid).mkdir(parents=True)
            (root / ".legion" / "battles" / bid / "battle.json").write_text("{}", encoding="utf-8")
        a_dir = root / ".legion" / "battles" / "A"
        battle_state._write_pointer(root, "B")
        battle_state.bind_session(root, "sidA", "A")
        tp = root / "t.jsonl"
        tp.write_text(line(4), encoding="utf-8")
        sub = {"hook_event_name": "SubagentStop", "agent_type": "builder", "session_id": "sidA",
               "agent_transcript_path": str(tp)}
        got = _active_battle_dir(str(root), sub)
        assert got is not None and got.name == "A", got
        handle_subagent(sub, got)
        assert len((a_dir / "usage.jsonl").read_text(encoding="utf-8").splitlines()) == 1  # U-S1
        assert not (root / ".legion" / "battles" / "B" / "usage.jsonl").exists()
        # U-S2 : session non liee alors que le pointeur est lie a une autre session
        battle_state._write_pointer(root, "A")
        assert _active_battle_dir(str(root), {"hook_event_name": "Stop", "session_id": "foreign"}) is None
        # U-S3 : curseur par cle, deux sessions successives sur A (H2 : une seule liaison par battle)
        for sid, totals in (("sidC", (10, 25)), ("sidD", (100, 130))):
            battle_state.bind_session(root, sid, "A")
            payload = {"hook_event_name": "Stop", "session_id": sid, "transcript_path": str(tp)}
            for total in totals:
                tp.write_text(line(total), encoding="utf-8")
                handle_stop(payload, a_dir)
        usage = [json.loads(x) for x in (a_dir / "usage.jsonl").read_text(encoding="utf-8").splitlines()]
        mains = [u["tokens"]["input"] for u in usage if u["scope"] == "main"]
        assert mains == [15, 30], mains  # delta juste, ni negatif ni fantome
        assert (a_dir / ".usage-main-sidC.json").exists() and (a_dir / ".usage-main-sidD.json").exists()
        # U-S4 : sans cle -> curseur historique
        nokey = root / "t.x.jsonl"  # nom de transcript non conforme : aucune cle derivable
        nokey.write_text(line(1), encoding="utf-8")
        handle_stop({"hook_event_name": "Stop", "transcript_path": str(nokey)}, a_dir)
        assert (a_dir / ".usage-main.json").exists()


def _self_test() -> int:
    global _IMPORT_ERROR
    if _IMPORT_ERROR is not None:
        print(f"FAIL: import de scripts/battle_state.py impossible ({_IMPORT_ERROR})", file=sys.stderr)
        return 1
    import battle_state
    line_a = {"type": "assistant", "message": {"usage": {
        "input_tokens": 100, "output_tokens": 20,
        "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0},
        "content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "scaffold"}}]}}
    line_b = {"type": "assistant", "message": {"usage": {"input_tokens": 50, "output_tokens": 10},
        "content": [{"type": "tool_use", "name": "Bash", "input": {}},
                    {"type": "tool_use", "name": "Skill", "input": {"skill": "build-fix"}}]}}
    with tempfile.TemporaryDirectory() as d:
        tp = Path(d) / "t.jsonl"
        tp.write_text("\n".join(json.dumps(x) for x in (line_a, line_b)) + "\n", encoding="utf-8")
        tokens, skills = _tally(_iter_records(str(tp)))
        assert tokens == {"input": 150, "output": 30, "cache_read": 5, "cache_creation": 0}, tokens
        assert skills == ["scaffold", "build-fix"], skills
        # transcript vide / illisible -> zero, pas d'exception
        assert _tally(_iter_records(str(Path(d) / "absent.jsonl"))) == ({k: 0 for k in _TOKEN_KEYS}, [])
        # resolution de la battle active (lecteur partage)
        repo = Path(d) / "repo"
        (repo / ".legion" / "battles" / "b1").mkdir(parents=True)
        (repo / ".legion" / "battles" / "b1" / "battle.json").write_text("{}", encoding="utf-8")  # resolve_battle exige un battle.json lisible
        battle_state._write_pointer(repo, "b1")
        assert _active_battle_dir(str(repo)).name == "b1"
        assert _active_battle_dir(str(Path(d) / "norepo")) is None
        # pointeur vide/blanc (ecrit par `close`) : PAS `.legion/battles/` (bug GH#85)
        for value in ("", "  \n"):
            battle_state._write_pointer(repo, value)
            assert _active_battle_dir(str(repo)) is None, value
        # id invalide
        battle_state._write_pointer(repo, "../x")
        assert _active_battle_dir(str(repo)) is None
        # import simule en echec : no-op
        saved, _IMPORT_ERROR = _IMPORT_ERROR, "ImportError: simule"
        try:
            battle_state._write_pointer(repo, "b1")
            assert _active_battle_dir(str(repo)) is None
        finally:
            _IMPORT_ERROR = saved
    _t_sessions()
    _t_worktree()
    print("OK: usage_track self-test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
