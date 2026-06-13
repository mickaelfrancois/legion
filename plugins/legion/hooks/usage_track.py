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
read-modify-write partage => sur en concurrence). `active` lu dans
`<cwd>/.legion/active-battle`. Sans battle active => no-op immediat (le hook tourne
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


def _active_battle_dir(cwd: str) -> Path | None:
    base = Path(cwd) / ".legion"
    try:
        battle_id = (base / "active-battle").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    battle_dir = base / "battles" / battle_id
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
    cursor_path = battle_dir / ".usage-main.json"
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

    battle_dir = _active_battle_dir(data.get("cwd") or os.getcwd())
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


def _self_test() -> int:
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
        # active-battle resolution
        repo = Path(d) / "repo"
        (repo / ".legion" / "battles" / "b1").mkdir(parents=True)
        (repo / ".legion" / "active-battle").write_text("b1", encoding="utf-8")
        assert _active_battle_dir(str(repo)).name == "b1"
        assert _active_battle_dir(str(Path(d) / "norepo")) is None
    print("OK: usage_track self-test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
