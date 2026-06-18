"""Hook PostToolUse (legion) : projette chaque battle dans l'index fleet
global a chaque ecriture de son battle.json.

Multi-repo : chaque depot est autonome (etat local `.legion/`). L'index global
vit dans `~/.claude/legion/fleet.d/` (base surchargeable via env var
`LEGION_FLEET`), lu par `/fleet` et par toute UI.

Declencheur : un Edit/Write/MultiEdit dont le `file_path` se termine par
`battle.json` sous `.legion/battles/<id>/`. On (re)calcule la phase courante et
on ecrit l'entree. Cle = `<repo_path>::<battle_id>` (deux depots peuvent partager
un meme id date-ticket).

Concurrence (multi-Claude) : **un fichier shard par battle** dans `fleet.d/`
(`<sha1(cle)>.json`), ecrit atomiquement (temp + os.replace). Aucun
read-modify-write partage => pas de lost update, meme si N sessions ecrivent en
parallele : chacune ne touche QUE son propre shard. Les lecteurs (`read_fleet`,
`/fleet`) agregent les shards a la lecture. L'ancien fichier unique `fleet.json`
est migre en shards puis supprime (best-effort, idempotent).

Tests CLI :
    py fleet_sync.py --self-test
    py fleet_sync.py --migrate-legacy   # eclate un ancien fleet.json en shards
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

WRITE_TOOLS = ("Edit", "Write", "MultiEdit")
# `address` is optional + repeatable (post-deliver PR review loop); when never run
# its phase entry is simply absent, which _current_phase treats transparently.
PHASE_ORDER = ["think", "plan", "build", "review", "test", "deliver", "address", "reflect"]


def _state_base() -> Path:
    """Racine du state global. `LEGION_FLEET` (historiquement le chemin du
    fichier `fleet.json`) en fixe le dossier parent ; sinon le namespace fige."""
    override = os.environ.get("LEGION_FLEET")
    return Path(override).parent if override else Path.home() / ".claude" / "legion"


def _fleet_dir() -> Path:
    """Dossier des shards (un fichier JSON par battle)."""
    return _state_base() / "fleet.d"


def _legacy_fleet_path() -> Path:
    """Ancien fichier unique, lu/migre puis supprime."""
    override = os.environ.get("LEGION_FLEET")
    return Path(override) if override else _state_base() / "fleet.json"


def _shard_name(key: str) -> str:
    """Nom de fichier stable et sur pour une cle `<repo_path>::<id>` (qui contient
    `:` et `\\`, illegaux sous Windows). Deterministe => meme battle, meme shard."""
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _current_phase(phases: dict) -> tuple[str, str]:
    """Retourne (phase, status) : la phase in_progress si elle existe, sinon la
    derniere phase done, sinon la premiere. Status de cette phase."""
    in_progress = [p for p in PHASE_ORDER if (phases.get(p) or {}).get("status") == "in_progress"]
    if in_progress:
        p = in_progress[-1]
        return p, "in_progress"
    blocked = [p for p in PHASE_ORDER if (phases.get(p) or {}).get("status") == "blocked"]
    if blocked:
        p = blocked[-1]
        return p, "blocked"
    done = [p for p in PHASE_ORDER if (phases.get(p) or {}).get("status") == "done"]
    if done:
        p = done[-1]
        return p, "done"
    return PHASE_ORDER[0], (phases.get(PHASE_ORDER[0]) or {}).get("status", "pending")


def _battle_status(phases: dict) -> str:
    """État GLOBAL de la battle (vs phase courante), pour filtrer terminé/en cours :
    `closed` si la rétro est faite, sinon `blocked` si la phase courante l'est,
    sinon `active`. Dérivé des phases — jamais stocké en double dans battle.json."""
    if (phases.get("reflect") or {}).get("status") == "done":
        return "closed"
    _, status = _current_phase(phases)
    return "blocked" if status == "blocked" else "active"


def _battle_dir_from_path(file_path: str) -> Path | None:
    """Si file_path pointe un battle.json sous .legion/battles/<id>/, retourne
    le dossier de la battle, sinon None."""
    p = Path(file_path.replace("\\", "/"))
    if p.name != "battle.json":
        return None
    if p.parent.parent.name != "battles":
        return None
    return p.parent


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
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            os.remove(tmp)


def upsert(battle_dir: Path, repo_root: Path, fleet_dir: Path) -> bool:
    """Ecrit le shard de CETTE battle (un fichier dedie). Aucune lecture/reecriture
    d'un index partage => sur en concurrence multi-sessions. Retourne True si ecrit."""
    battle = _read_json(battle_dir / "battle.json")
    if battle is None:
        return False
    battle_id = battle.get("id") or battle_dir.name
    repo_path = str(repo_root)
    phases = battle.get("phases") or {}
    phase, status = _current_phase(phases)
    delivery = battle.get("delivery") or {}
    key = f"{repo_path}::{battle_id}"

    entry = {
        "id": battle_id,
        "repo": battle.get("repo") or repo_root.name,
        "repo_path": repo_path,
        "ticket": battle.get("ticket"),
        "title": battle.get("title"),
        "profile": battle.get("profile"),
        "phase": phase,
        "status": status,
        "battle_status": _battle_status(phases),
        "pr_url": delivery.get("pr_url"),
        "updated": _now_iso(),
    }
    entry.update(_read_usage(battle_dir))  # tokens_total, tokens, skills (snapshot)
    _atomic_write(fleet_dir / _shard_name(key), entry)
    return True


def _read_usage(battle_dir: Path) -> dict:
    """Agrege `usage.jsonl` (ecrit par usage_track.py) : tokens cumules + skills
    uniques. Snapshot rafraichi a chaque ecriture de battle.json. {} si absent.
    L'entete `tokens_total` = input+output (le « cout approximatif » pour l'UI)."""
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    skills, seen = [], set()
    try:
        with open(battle_dir / "usage.jsonl", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for k in tokens:
                    tokens[k] += (rec.get("tokens") or {}).get(k) or 0
                for sk in rec.get("skills") or []:
                    if sk not in seen:
                        seen.add(sk)
                        skills.append(sk)
    except OSError:
        return {}
    return {"tokens_total": tokens["input"] + tokens["output"], "tokens": tokens, "skills": skills}


def read_fleet(fleet_dir: Path) -> list:
    """Agrege tous les shards en une liste d'entrees. Ignore les shards illisibles
    (best-effort). C'est la lecture que `/fleet` et une UI doivent reproduire."""
    if not fleet_dir.is_dir():
        return []
    out = {}
    for shard in sorted(fleet_dir.glob("*.json")):
        entry = _read_json(shard)
        if isinstance(entry, dict) and entry.get("id"):
            out[f"{entry.get('repo_path')}::{entry['id']}"] = entry
    return list(out.values())


def _migrate_legacy(fleet_dir: Path, legacy_path: Path) -> None:
    """Eclate un ancien `fleet.json` unique en shards, puis le supprime. Idempotent
    (cle deterministe ; on n'ecrase pas un shard plus recent) et tolerant aux
    courses (best-effort)."""
    if not legacy_path.is_file():
        return
    legacy = _read_json(legacy_path) or {}
    for entry in legacy.get("battles", []):
        key = f"{entry.get('repo_path')}::{entry.get('id')}"
        shard = fleet_dir / _shard_name(key)
        if not shard.exists():
            _atomic_write(shard, entry)
    try:
        legacy_path.unlink()
    except OSError:
        pass


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()

    fleet_dir = _fleet_dir()
    try:
        _migrate_legacy(fleet_dir, _legacy_fleet_path())  # best-effort, idempotent
    except OSError:
        pass
    if "--migrate-legacy" in sys.argv:
        return 0  # migration ponctuelle a la demande (pas de lecture stdin)

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return 0

    if data.get("tool_name") not in WRITE_TOOLS:
        return 0
    file_path = (data.get("tool_input") or {}).get("file_path", "")
    battle_dir = _battle_dir_from_path(file_path)
    if battle_dir is None:
        return 0

    # Racine repo = parent de .legion. battle_dir = <repo>/.legion/battles/<id>
    repo_root = battle_dir.parent.parent.parent
    try:
        upsert(battle_dir, repo_root, fleet_dir)
    except OSError:
        pass  # best-effort : ne jamais casser l'edition pour un sync d'index
    return 0


def _self_test() -> int:
    assert _current_phase({"plan": {"status": "done"}, "build": {"status": "in_progress"}}) == ("build", "in_progress")
    assert _current_phase({"plan": {"status": "done"}}) == ("plan", "done")
    assert _current_phase({"review": {"status": "blocked"}}) == ("review", "blocked")
    assert _battle_status({"reflect": {"status": "done"}}) == "closed"
    assert _battle_status({"review": {"status": "blocked"}}) == "blocked"
    assert _battle_status({"build": {"status": "in_progress"}}) == "active"
    assert _battle_status({}) == "active"
    assert _current_phase({"deliver": {"status": "done"}}) == ("deliver", "done")
    # address (optionnelle, post-deliver) : in_progress => phase courante
    assert _current_phase({"deliver": {"status": "done"}, "address": {"status": "in_progress"}}) == ("address", "in_progress")
    # address jamais jouée (absente) : transparente, reflect=done => closed
    assert _battle_status({"deliver": {"status": "done"}, "reflect": {"status": "done"}}) == "closed"
    assert _battle_dir_from_path("x/.legion/battles/b1/battle.json").name == "b1"
    assert _battle_dir_from_path("x/src/foo.cs") is None
    assert _shard_name("a::b") == _shard_name("a::b") and _shard_name("a::b").endswith(".json")
    assert _shard_name("a::b") != _shard_name("a::c")

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        fleet_dir = base / "fleet.d"
        legacy = base / "fleet.json"
        legacy.write_text(json.dumps({"battles": [
            {"id": "old", "repo_path": "R", "phase": "reflect", "status": "done"}]}), encoding="utf-8")
        _migrate_legacy(fleet_dir, legacy)
        assert not legacy.exists()                       # legacy consommé
        # deux battles distinctes -> deux shards, aucun n'écrase l'autre
        for bid in ("b1", "b2"):
            bp = base / bid / ".legion" / "battles" / bid
            bp.mkdir(parents=True)
            (bp / "battle.json").write_text(
                json.dumps({"id": bid, "phases": {"build": {"status": "in_progress"}}}), encoding="utf-8")
            upsert(bp, base / bid, fleet_dir)
        ids = {e["id"] for e in read_fleet(fleet_dir)}
        assert {"old", "b1", "b2"} <= ids, ids           # rien de perdu

        # usage.jsonl -> tokens/skills agrégés dans le shard
        bp1 = base / "b1" / ".legion" / "battles" / "b1"
        (bp1 / "usage.jsonl").write_text(
            json.dumps({"scope": "subagent", "skills": ["scaffold"], "tokens": {"input": 100, "output": 20}}) + "\n"
            + json.dumps({"scope": "main", "skills": ["scaffold", "build-fix"], "tokens": {"input": 10, "output": 5}}) + "\n",
            encoding="utf-8")
        usage = _read_usage(bp1)
        assert usage["tokens_total"] == 135, usage          # 100+20+10+5
        assert usage["skills"] == ["scaffold", "build-fix"], usage  # dédupliqué, ordre conservé
        upsert(bp1, base / "b1", fleet_dir)
        b1 = next(e for e in read_fleet(fleet_dir) if e["id"] == "b1")
        assert b1["tokens_total"] == 135 and b1["skills"] == ["scaffold", "build-fix"]
        assert b1["battle_status"] == "active", b1

    print("OK: fleet_sync self-test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
