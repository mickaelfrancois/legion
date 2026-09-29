"""Harness d'éval des gates de legion (GH#48) : mesurer, sur les battles closes du fleet,
à quelle fréquence chaque gate pousse un `revise`, combien de rondes d'auto-correction ça
coûte, combien de battles elle bloque en `reject`, et le coût moyen/battle. Objectif :
matière chiffrée pour calibrer les gates et prouver leur valeur. v1 volontairement
incrémentale (la spec veut commencer par reject-rate + rondes).

Comme `base_freshness.py` / `opportunity.py`, le cœur est **pur** (ni réseau ni disque) :
`_is_closed`, `_gate_metrics`, `_cost_stats`, `_render` prennent des dicts déjà chargés et
se testent hermétiquement via `--self-test`. La couche I/O (`_iter_shards`, `_load_battle`,
`_read_json`) est **best-effort** : toute lecture échouée dégrade vers `None` sans jamais
crasher (un `repo_path` d'une autre machine, un JSON purgé/malformé ne doit pas tuer le run).

Le script **lit** les artefacts legion sans jamais les écrire (spec : aucune modif des
gates/schémas). Contrat de données confirmé sur artefacts réels :
- **Shard** (`$LEGION_FLEET`, défaut `~/.claude/legion/fleet.d/*.json`, un par battle) :
  `id, repo, repo_path, status, phase, profile, battle_status` + (si `usage.jsonl` présent)
  `tokens_total`. `tokens_total` **peut manquer** → toujours `.get`, ne compter dans la
  moyenne coût que les shards qui le portent.
- **Battle close** : `status == "done"` **ou** `phase == "reflect"` (prédicat de la spec).
- **`battle.json`** (sous `<repo_path>/.legion/battles/<id>/`) : verdicts sous
  `phases.<phase>.verdict`, statut de phase sous `phases.<phase>.status`, rondes revise sous
  `run.autocorrect.per_gate.<phase>`. Clés côté **phase** (`plan`/`review`/`test`/…), pas
  côté gate — d'où l'agrégation par phase (C1 : le nom de gate n'est qu'un libellé).

Métriques **par gate**, une ligne/gate (nom de phase → libellé gate) :
- **revise-rate** = # battles où `per_gate[phase] > 0` / # battles où la gate a tourné ;
- **rondes moyennes** = moyenne de `per_gate.get(phase, 0)` sur les battles où elle a tourné
  (absent compté 0, sinon la moyenne est biaisée vers le haut) ;
- **reject count** = # battles où `phases[phase].verdict == "reject"` ;
- **coût moyen/battle** = moyenne de `tokens_total` sur les shards qui le portent.

« La gate a tourné » (C2) = `phases[phase]` existe **et** `status == "done"` (verdict rendu) :
une gate requise mais jamais atteinte (battle avortée) ne gonfle pas le dénominateur.

Usage :
    python eval.py               # rapport markdown sur le fleet réel
    python eval.py --self-test   # tests hermétiques (fixtures tmpdir), sort 0 offline
"""

from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

# Phases-gates rapportées, dans l'ordre du pipeline : source unique `battle_state.py`
# (GH#69, même dossier `scripts/`, import direct). Clé = nom de **phase** (natif des deux
# sources de métriques) ; la valeur est le libellé de présentation (C1 : gate entre
# parenthèses, table figée — aucune dépendance de l'agrégation à cette table).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from battle_state import VERDICT_PHASES  # noqa: E402

GATE_PHASES: tuple[str, ...] = VERDICT_PHASES
_GATE_LABEL = {
    "plan": "plan (architect)",
    "review": "review (reviewer)",
    "test": "test (test-engineer)",
    "lint": "lint",
    "security": "security",
}


# --------------------------------------------------------------------------- #
# Cœur pur — agrégation (ni disque ni réseau, 100 % testable)                 #
# --------------------------------------------------------------------------- #

def _is_closed(shard: dict) -> bool:
    """Battle close (spec) : `status == "done"` **ou** `phase == "reflect"`."""
    if not isinstance(shard, dict):
        return False
    return shard.get("status") == "done" or shard.get("phase") == "reflect"


def _phase_ran(battle: dict, phase: str) -> bool:
    """C2 : la gate a tourné = la phase existe **et** a rendu un verdict (`status==done`)."""
    ph = (battle.get("phases") or {}).get(phase) if isinstance(battle, dict) else None
    return isinstance(ph, dict) and ph.get("status") == "done"


def _gate_metrics(battles: list) -> dict:
    """Métriques par phase-gate sur les battles **avec artefacts** (dicts `battle.json`).

    Retourne `{phase: {ran, revise_rate, avg_rounds, reject}}`. `revise_rate`/`avg_rounds`
    valent `None` quand la gate n'a jamais tourné (ran == 0) — distinct de 0.0 (« a tourné,
    n'a jamais poussé »)."""
    battles = [b for b in battles if isinstance(b, dict)]
    out: dict = {}
    for phase in GATE_PHASES:
        ran = revised = reject = 0
        rounds_sum = 0
        for b in battles:
            # Garde chaque étage avec `or {}` : une valeur `null` explicite (clé présente,
            # valeur None — distincte d'absente) ne doit pas lever, sinon un `battle.json`
            # partiellement corrompu crasherait le run (plan : « ne jamais crasher »).
            per_gate = ((b.get("run") or {}).get("autocorrect") or {}).get("per_gate") or {}
            if _phase_ran(b, phase):
                ran += 1
                rounds = per_gate.get(phase, 0) or 0
                rounds_sum += rounds
                if rounds > 0:
                    revised += 1
            ph = (b.get("phases") or {}).get(phase)
            if isinstance(ph, dict) and ph.get("verdict") == "reject":
                reject += 1
        out[phase] = {
            "ran": ran,
            "revise_rate": (revised / ran) if ran else None,
            "avg_rounds": (rounds_sum / ran) if ran else None,
            "reject": reject,
        }
    return out


def _cost_stats(closed_shards: list) -> dict:
    """Coût moyen/battle depuis `tokens_total` — seuls les shards qui le portent comptent
    (dénominateur = # shards au coût connu, annoncé distinctement de N/M)."""
    vals = [
        s["tokens_total"]
        for s in closed_shards
        if isinstance(s, dict) and isinstance(s.get("tokens_total"), (int, float))
    ]
    return {
        "avg_tokens": (sum(vals) / len(vals)) if vals else None,
        "k_with_cost": len(vals),
    }


# --------------------------------------------------------------------------- #
# Rendu markdown — source unique du format (accents FR)                        #
# --------------------------------------------------------------------------- #

def _fmt_int(n: float) -> str:
    """Entier avec espace fine comme séparateur de milliers (lisible FR)."""
    return f"{round(n):,}".replace(",", " ")


def _render(metrics: dict, cost: dict, coverage: dict) -> str:
    """Rapport markdown : en-tête (indicatif + couverture), tableau par-gate, ligne coût."""
    lines = [
        "# Éval des gates legion",
        "",
        f"> Chiffres **indicatifs** (faible volume) — {coverage['n_with_artifacts']} battles "
        f"analysées / {coverage['m_closed']} closes, {coverage['k_with_cost']} au coût connu.",
        "",
        "| Gate | tourné | revise-rate | rondes moy. | reject |",
        "|------|--------|-------------|-------------|--------|",
    ]
    for phase in GATE_PHASES:
        m = metrics[phase]
        rr = "—" if m["revise_rate"] is None else f"{m['revise_rate'] * 100:.0f} %"
        ar = "—" if m["avg_rounds"] is None else f"{m['avg_rounds']:.2f}"
        lines.append(
            f"| {_GATE_LABEL[phase]} | {m['ran']} | {rr} | {ar} | {m['reject']} |"
        )
    lines.append("")
    avg = cost["avg_tokens"]
    cost_str = "indisponible" if avg is None else f"{_fmt_int(avg)} tokens"
    lines.append(
        f"**Coût moyen/battle** : {cost_str} (sur {cost['k_with_cost']} battles au coût connu)."
    )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Couche I/O — best-effort, dégrade sans crasher                              #
# --------------------------------------------------------------------------- #

def _read_json(path: str) -> object | None:
    """Lit un JSON ; `None` si absent/illisible/malformé (jamais de crash)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _resolve_glob() -> str:
    """Motif des shards : `$LEGION_FLEET` sinon `~/.claude/legion/fleet.d/*.json`."""
    return os.environ.get("LEGION_FLEET") or str(
        Path.home() / ".claude" / "legion" / "fleet.d" / "*.json"
    )


def _iter_shards() -> list:
    """Charge tous les shards du fleet (un par battle). Ignore silencieusement les fichiers
    illisibles/malformés (calque `_read_json` → `None`)."""
    shards = []
    for p in sorted(glob.glob(_resolve_glob())):
        d = _read_json(p)
        if isinstance(d, dict):
            shards.append(d)
    return shards


def _load_battle(shard: dict) -> dict | None:
    """`battle.json` sous `<repo_path>/.legion/battles/<id>/`, ou `None` si le dossier est
    absent (artefacts purgés, `repo_path` d'une autre machine) — branche dégradée."""
    repo_path = shard.get("repo_path")
    battle_id = shard.get("id")
    if not repo_path or not battle_id:
        return None
    battle_dir = Path(repo_path) / ".legion" / "battles" / str(battle_id)
    try:
        if not battle_dir.is_dir():
            return None
    except OSError:
        return None
    battle = _read_json(str(battle_dir / "battle.json"))
    return battle if isinstance(battle, dict) else None


def analyze() -> tuple[dict, dict, dict]:
    """Pipeline complet : énumère les shards, isole les closes, charge les `battle.json`
    disponibles, calcule métriques + coût + couverture. Ne crashe sur aucun artefact absent."""
    shards = _iter_shards()
    closed = [s for s in shards if _is_closed(s)]
    battles = []
    for s in closed:
        b = _load_battle(s)
        if b is not None:
            battles.append(b)
    metrics = _gate_metrics(battles)
    cost = _cost_stats(closed)
    coverage = {
        "total_shards": len(shards),
        "m_closed": len(closed),
        "n_with_artifacts": len(battles),
        "k_with_cost": cost["k_with_cost"],
    }
    return metrics, cost, coverage


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def main() -> int:
    args = sys.argv[1:]
    if "--self-test" in args:
        return _self_test()
    metrics, cost, coverage = analyze()
    print(_render(metrics, cost, coverage))
    return 0


# --------------------------------------------------------------------------- #
# self-test — hermétique (stdlib seule, fixtures en TemporaryDirectory)        #
# --------------------------------------------------------------------------- #

def _self_test() -> int:
    import battle_state
    assert GATE_PHASES == battle_state.VERDICT_PHASES, GATE_PHASES  # source unique, ordre pipeline
    assert set(_GATE_LABEL) == set(GATE_PHASES), (set(_GATE_LABEL), GATE_PHASES)
    import tempfile

    # ---- cœur pur : _is_closed ----
    assert _is_closed({"status": "done"})
    assert _is_closed({"phase": "reflect"})
    assert not _is_closed({"status": "active", "phase": "build"})
    assert not _is_closed("pas un dict")  # type: ignore[arg-type]

    # ---- cœur pur : _gate_metrics ----
    b1 = {"phases": {"review": {"status": "done", "verdict": "accept"},
                     "test": {"status": "done", "verdict": "reject"}},
          "run": {"autocorrect": {"per_gate": {"review": 2, "test": 0}}}}
    b2 = {"phases": {"review": {"status": "done", "verdict": "accept"},
                     "test": {"status": "done", "verdict": "accept"}},
          "run": {"autocorrect": {"per_gate": {}}}}
    m = _gate_metrics([b1, b2])
    # review : a tourné 2×, 1 revise (b1: 2 rondes) → rate 0.5, moyenne (2+0)/2 = 1.0
    assert m["review"]["ran"] == 2, m
    assert m["review"]["revise_rate"] == 0.5, m
    assert m["review"]["avg_rounds"] == 1.0, m
    assert m["review"]["reject"] == 0, m
    # test : a tourné 2×, 0 revise (rondes {0, absent→0}) → rate 0.0, 1 reject (b1)
    assert m["test"]["ran"] == 2 and m["test"]["revise_rate"] == 0.0, m
    assert m["test"]["avg_rounds"] == 0.0 and m["test"]["reject"] == 1, m
    # gate jamais atteinte → exclue du dénominateur (None, pas 0.0)
    assert m["lint"]["ran"] == 0 and m["lint"]["revise_rate"] is None, m
    assert m["security"]["avg_rounds"] is None, m
    # phase présente mais status != done → ne compte pas comme "a tourné"
    b3 = {"phases": {"review": {"status": "in_progress"}}, "run": {}}
    assert _gate_metrics([b3])["review"]["ran"] == 0
    # robustesse : `run.autocorrect` / `phases` à `null` explicite (clé présente, valeur
    # None) ne doit pas lever — un battle.json partiellement corrompu dégrade sans crash.
    b4 = {"phases": None, "run": {"autocorrect": None}}
    assert _gate_metrics([b4])["review"]["ran"] == 0
    assert not _phase_ran({"phases": None}, "review")

    # ---- cœur pur : _cost_stats (shard sans tokens_total exclu du dénominateur) ----
    cs = _cost_stats([{"tokens_total": 100}, {"tokens_total": 200}, {"repo": "x"}])
    assert cs["avg_tokens"] == 150.0 and cs["k_with_cost"] == 2, cs
    assert _cost_stats([{"repo": "x"}])["avg_tokens"] is None

    # ---- cœur pur : _render (accents FR, libellé double, valeurs) ----
    md = _render(m, cs, {"n_with_artifacts": 2, "m_closed": 3, "k_with_cost": 2})
    assert "battles analysées" in md and "rondes moy." in md, md
    assert "| review (reviewer) |" in md and "| plan (architect) |" in md, md
    assert "2 battles analysées / 3 closes" in md, md
    assert "Coût moyen/battle" in md and "150 tokens" in md, md

    # ---- intégration : fleet synthétique en tmpdir, LEGION_FLEET pointé dessus ----
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fleet_d = tmp_path / "fleet.d"
        fleet_d.mkdir()
        repo = tmp_path / "repo"

        def _write(path: Path, obj: dict) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

        # Battle A : close, artefacts présents, review revisé (2 rondes), reject sur test.
        _write(fleet_d / "a.json",
               {"id": "A", "repo_path": str(repo), "status": "done",
                "phase": "reflect", "tokens_total": 100})
        _write(repo / ".legion" / "battles" / "A" / "battle.json", b1)
        # Battle B : close, artefacts présents, aucune revise.
        _write(fleet_d / "b.json",
               {"id": "B", "repo_path": str(repo), "status": "done",
                "phase": "reflect", "tokens_total": 200})
        _write(repo / ".legion" / "battles" / "B" / "battle.json", b2)
        # Battle C : close, SANS dossier artefacts (repo_path inexistant → branche dégradée),
        #            coût connu → compte en M et K mais pas en N.
        _write(fleet_d / "c.json",
               {"id": "C", "repo_path": str(tmp_path / "absent"), "status": "done",
                "phase": "reflect", "tokens_total": 300})
        # Battle D : active → exclue des closes.
        _write(fleet_d / "d.json",
               {"id": "D", "repo_path": str(repo), "status": "active", "phase": "build",
                "tokens_total": 999})
        # Shard malformé → ignoré, aucun crash.
        (fleet_d / "bad.json").write_text("ceci n'est pas du JSON", encoding="utf-8")

        prev = os.environ.get("LEGION_FLEET")
        os.environ["LEGION_FLEET"] = str(fleet_d / "*.json")
        try:
            metrics, cost, coverage = analyze()
        finally:
            if prev is None:
                os.environ.pop("LEGION_FLEET", None)
            else:
                os.environ["LEGION_FLEET"] = prev

        # Couverture : A,B,C closes (D active exclue, bad ignoré) ; A,B avec artefacts.
        assert coverage["m_closed"] == 3, coverage
        assert coverage["n_with_artifacts"] == 2, coverage
        assert coverage["total_shards"] == 4, coverage  # a,b,c,d (bad non parsé)
        # Coût : {100, 200, 300} closes au coût connu → moyenne 200, K = 3.
        assert cost["avg_tokens"] == 200.0 and cost["k_with_cost"] == 3, cost
        # Métriques identiques au calcul pur (seules A,B ont des artefacts).
        assert metrics["review"]["revise_rate"] == 0.5, metrics
        assert metrics["review"]["avg_rounds"] == 1.0, metrics
        assert metrics["test"]["reject"] == 1, metrics
        # plan n'a de phase dans aucune fixture (A/B) → jamais tourné → None, pas 0.0.
        assert metrics["plan"]["ran"] == 0 and metrics["plan"]["revise_rate"] is None, metrics
        # Le rendu ne crashe pas et annonce la couverture réelle.
        report = _render(metrics, cost, coverage)
        assert "2 battles analysées / 3 closes" in report, report

        # `battle.json` présent mais corrompu (dossier existe, JSON cassé) → `_load_battle`
        # dégrade en None sans crash (15e ligne de la matrice : dossier présent ≠ lisible).
        bad_repo = tmp_path / "badrepo"
        (bad_repo / ".legion" / "battles" / "E").mkdir(parents=True)
        (bad_repo / ".legion" / "battles" / "E" / "battle.json").write_text(
            "{ ceci n'est pas du JSON", encoding="utf-8"
        )
        assert _load_battle({"id": "E", "repo_path": str(bad_repo)}) is None

    print("OK: eval self-test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    # Reconfigure stdin aussi (calque `opportunity.py`) : accents FR corrects même si une
    # future sous-commande lit stdin, et console cp1252 Windows pour stdout/stderr.
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
