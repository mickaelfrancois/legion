"""Transitions d'état déterministes de `battle.json` (GH#69) : seul point d'écriture prévu
de l'état d'une battle legion. Les transitions de phase, la politique d'approbation du plan
(#59) et les budgets d'auto-correction (2 par phase, 6 au global) sont tranchés ici, en code,
au lieu d'être recopiés en prose dans les command-files.

Comme `base_freshness.py` / `eval.py`, le cœur est **pur** (ni disque ni réseau) : il prend
et rend des dicts, donc il se teste hermétiquement via `--self-test`. Cette version
contient le cœur pur et la couche I/O + CLI (lecture/écriture atomique, pointeur, synchro
fleet, sous-commandes). Le module n'a aucun effet de bord à l'import : `fleet_sync.py` est
chargé à la demande, par chemin explicite.

Source unique des listes de phases / gates / artefacts (importée par `fleet_sync.py`,
`guard.py`, `eval.py`) :
- `PHASES`, `GATES`, `GATE_PHASE`, `GATE_ARTIFACT`, `PRODUCER_ARTIFACT` ;
- `VERDICT_PHASES`, `CASCADE_PHASES`, `AUTOCORRECT_KEYS` ;
- `STATUSES`, `VERDICTS`, `ACCEPT_VERDICTS`, `DEFAULT_REQUIRED_GATES` ;
- `CAP_PER_PHASE`, `CAP_TOTAL`.

Cœur pur :
- `check_transition(battle, phase, status, verdict, fails, round_, threads) -> (ok, reason)` ;
- `apply_transition(...) -> battle` (copie ; lève `ValueError` si la transition est refusée) ;
- `approve_plan(battle, now_iso) -> (ok, reason, battle)` ;
- `bump_autocorrect(battle, phase, fails, keep_fails=False) -> (decision, reason, battle, detail)`,
  decision ∈ `continue | escalate | refuse` ;
- `fail_identity(item) -> str`, `normalize(battle)`, `validate(battle) -> (errors, warnings)`,
  `derive_required_phases(battle)`.

Usage (options globales `--battle <id>` défaut : pointeur `.legion/active-battle`, et
`--repo <path>` défaut : cwd) :
    python battle_state.py init <id> --ticket T --title T --profile P [--step] [--required-gates g…]
    python battle_state.py transition <phase> <status> [--verdict v] [--fails json]
                                      [--round n] [--threads json]
    python battle_state.py approve-plan | close | validate
    python battle_state.py bump-autocorrect <phase> (--fails <json> | --build-failure)
    python battle_state.py set-delivery --pr-url <url>
    python battle_state.py set-guard [--allow [g…]] [--deny [g…]] [--careful on|off]
    python battle_state.py set-meta [--title] [--profile] [--required-gates …] [--stack-kind]
                                    [--build-target] [--test-target]
    python battle_state.py activate <id>
    python battle_state.py --self-test   # tests hermétiques, sort 0 offline

Sortie : un objet JSON sur stdout (`ok`, `reason` si refus). Écriture atomique ; après chaque
mutation, le shard fleet est réécrit (échec = `warnings`, jamais bloquant).

Codes de sortie : 0 = ok (dont `bump-autocorrect` escalate), 1 = self-test en échec,
2 = usage invalide / refus.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# --- Source unique -----------------------------------------------------------------------

PHASES: tuple[str, ...] = ("think", "plan", "build", "lint", "review", "test", "security",
                           "deliver", "address", "reflect")
GATES: tuple[str, ...] = ("architect", "lint", "reviewer", "test-engineer", "security",
                          "pr-triage")
GATE_PHASE: dict[str, str] = {
    "architect": "plan",
    "lint": "lint",
    "reviewer": "review",
    "test-engineer": "test",
    "security": "security",
    "pr-triage": "address",
}
# Noms d'artefacts sans prefixe de plugin (le prefixe `legion:` reste dans guard.py).
GATE_ARTIFACT: dict[str, str] = {
    "architect": "plan.md",
    "lint": "gate-lint.md",
    "reviewer": "gate-review.md",
    "test-engineer": "gate-test.md",
    "security": "gate-security.md",
    "pr-triage": "pr-feedback.md",
}
PRODUCER_ARTIFACT: dict[str, str] = {"builder": "build-report.md"}

VERDICT_PHASES: tuple[str, ...] = ("plan", "lint", "review", "test", "security")
CASCADE_PHASES: tuple[str, ...] = ("lint", "review", "test", "security")
AUTOCORRECT_KEYS: tuple[str, ...] = ("build",) + CASCADE_PHASES

STATUSES: tuple[str, ...] = ("pending", "in_progress", "done", "blocked")
ACCEPT_VERDICTS: tuple[str, ...] = ("accept", "accept_with_opportunity")
VERDICTS: tuple[str, ...] = ACCEPT_VERDICTS + ("revise", "reject")
DEFAULT_REQUIRED_GATES: tuple[str, ...] = ("architect", "lint", "reviewer", "test-engineer")

CAP_PER_PHASE = 2
CAP_TOTAL = 6


# --- Helpers de lecture (tolerants au schema legacy) -------------------------------------

def _phase_entry(battle: dict, phase: str) -> dict:
    phases = battle.get("phases")
    entry = phases.get(phase) if isinstance(phases, dict) else None
    return entry if isinstance(entry, dict) else {}


def _status(battle: dict, phase: str) -> str:
    return _phase_entry(battle, phase).get("status", "pending")


def _is_accept(verdict) -> bool:
    return verdict in ACCEPT_VERDICTS


def _required_gates(battle: dict) -> list[str]:
    rg = battle.get("required_gates")
    return list(rg) if isinstance(rg, list) else list(DEFAULT_REQUIRED_GATES)


def derive_required_phases(battle: dict) -> list[str]:
    """Phases qui doivent etre `done` avant DELIVER (pr-triage exclu), ordre de required_gates."""
    out: list[str] = []
    for g in _required_gates(battle):
        if g == "pr-triage" or g not in GATE_PHASE:
            continue
        p = GATE_PHASE[g]
        if p not in out:
            out.append(p)
    return out


def fail_identity(item) -> str:
    """Identité d'un FAIL : `<target>|<dimension>` (objet) ; une chaîne équivalente
    `cible|DIM` donne la même identité. Espaces de bord sans effet."""
    if isinstance(item, dict):
        target = str(item.get("target", "")).strip()
        dimension = str(item.get("dimension", "")).strip()
        if not target and not dimension:
            return json.dumps(item, sort_keys=True, ensure_ascii=False)
        return f"{target}|{dimension}"
    s = str(item)
    if "|" in s:
        left, _, right = s.partition("|")
        return f"{left.strip()}|{right.strip()}"
    return " ".join(s.split())


# --- Transitions -------------------------------------------------------------------------

def check_transition(battle: dict, phase: str, status: str, verdict: str | None = None,
                     fails=None, round_=None, threads=None) -> tuple[bool, str]:
    """Précondition d'une transition de phase. Pur. Retourne (ok, raison du refus)."""
    if phase not in PHASES:
        return False, f"phase inconnue : {phase!r}"
    if status not in STATUSES:
        return False, f"statut inconnu : {status!r}"
    if status == "pending":
        return False, "retour à 'pending' refusé (pas de retour arrière)"
    if phase == "reflect":
        return False, "la phase reflect passe uniquement par `close`"

    # coherence verdict <-> statut
    if verdict is not None:
        if verdict not in VERDICTS:
            return False, f"verdict inconnu : {verdict!r}"
        if phase not in VERDICT_PHASES:
            return False, f"la phase {phase} ne porte pas de verdict"
        if _is_accept(verdict) and status != "done":
            return False, f"verdict {verdict} exige le statut done (reçu {status})"
        if verdict in ("revise", "reject") and status != "blocked":
            return False, f"verdict {verdict} exige le statut blocked (reçu {status})"
    elif status == "done" and phase in VERDICT_PHASES:
        return False, f"la phase {phase} ne peut être done sans verdict accept*"

    if fails is not None:
        if phase != "plan":
            return False, "--fails n'est accepté que sur la phase plan"
        if not isinstance(fails, list):
            return False, "fails doit être une liste"
    if (round_ is not None or threads is not None) and phase != "address":
        return False, "round/threads ne sont acceptés que sur la phase address"

    if status == "blocked":
        return True, ""

    # preconditions d'entree (in_progress | done)
    if phase == "plan":
        if _status(battle, "think") != "done":
            return False, "plan exige think done"
    elif phase == "build":
        plan = _phase_entry(battle, "plan")
        if plan.get("status") != "done" or not _is_accept(plan.get("verdict")):
            return False, "build exige un plan done avec verdict accept*"
        # Legacy : battle antérieure au champ (clé `approved_at` absente) déjà entamée. Un
        # re-plan laisse la clé à null : nouvelle approbation obligatoire.
        legacy = "approved_at" not in plan and _status(battle, "build") != "pending"
        if not plan.get("approved_at") and not legacy:
            return False, "plan non approuvé (approve-plan requis avant build)"
    elif phase in CASCADE_PHASES:
        if _status(battle, "build") != "done":
            return False, f"{phase} exige build done"
    elif phase == "deliver":
        for p in derive_required_phases(battle):
            st = _phase_entry(battle, p).get("status", "absente")
            if st != "done":
                return False, f"deliver exige la phase {p} done (statut : {st})"
    elif phase == "address":
        delivery = battle.get("delivery")
        if not (isinstance(delivery, dict) and delivery.get("pr_url")):
            return False, "address exige delivery.pr_url"
    return True, ""


def apply_transition(battle: dict, phase: str, status: str, verdict: str | None = None,
                     fails=None, round_=None, threads=None) -> dict:
    """Applique la transition sur une copie. Lève ValueError si `check_transition` refuse."""
    ok, reason = check_transition(battle, phase, status, verdict, fails, round_, threads)
    if not ok:
        raise ValueError(reason)
    out = copy.deepcopy(battle)
    phases = out.setdefault("phases", {})
    entry = phases.get(phase)
    if not isinstance(entry, dict):
        entry = {}
        phases[phase] = entry
    entry["status"] = status
    if verdict is not None:
        entry["verdict"] = verdict
    elif status == "in_progress" and "verdict" in entry:
        entry["verdict"] = None  # nouveau passage : l'ancien verdict n'a plus cours
    if phase == "plan" and status == "in_progress":
        entry["approved_at"] = None  # clé présente à null : nouvelle approbation requise, pas de legacy
    if _is_accept(verdict) and ("fails" in entry or phase == "plan"):
        entry["fails"] = []
    if fails is not None:
        entry["fails"] = copy.deepcopy(fails)
    if round_ is not None:
        entry["round"] = round_
    if threads is not None:
        entry["threads"] = copy.deepcopy(threads)
    return out


def approve_plan(battle: dict, now_iso: str) -> tuple[bool, str, dict]:
    """Enregistre l'approbation humaine du plan. Idempotent (réécrit l'horodatage)."""
    plan = _phase_entry(battle, "plan")
    if plan.get("status") != "done" or not _is_accept(plan.get("verdict")):
        return False, "approve-plan exige un plan done avec verdict accept*", battle
    out = copy.deepcopy(battle)
    out["phases"]["plan"]["approved_at"] = now_iso
    return True, "", out


# --- Auto-correction ---------------------------------------------------------------------

def bump_autocorrect(battle: dict, phase: str, fails, keep_fails: bool = False) -> tuple[str, str, dict, dict]:
    """Décide continue / escalate pour une ronde d'auto-correction. Pur.

    Retourne (decision, reason, battle, detail). `refuse` = clé invalide (battle inchangée).
    `escalate` n'incrémente pas les compteurs mais enregistre les FAIL. `keep_fails=True` (échec
    de build survenu pendant la correction d'une gate) compte la tentative sous la clé mais
    laisse `phases.<clé>.fails` intacts et saute le contrôle de non-progrès. detail =
    {per_phase, total, resolved, persisting, new}.
    """
    if phase not in AUTOCORRECT_KEYS:
        reason = f"clé d'auto-correction invalide : {phase!r} (attendu : {', '.join(AUTOCORRECT_KEYS)})"
        detail: dict = {}
        if phase in GATE_PHASE and GATE_PHASE[phase] in AUTOCORRECT_KEYS:
            detail["suggestion"] = GATE_PHASE[phase]
            reason += f" ; utiliser la clé de phase {GATE_PHASE[phase]!r}"
        return "refuse", reason, battle, detail
    if keep_fails:
        fails = []
    if not isinstance(fails, list):
        return "refuse", "fails doit être une liste", battle, {}

    out = copy.deepcopy(battle)
    run = out.setdefault("run", {})
    ac = run.setdefault("autocorrect", {})
    per_gate = ac.setdefault("per_gate", {})
    ac.setdefault("total", 0)
    per = per_gate.get(phase, 0)
    total = ac["total"]

    entry = out.setdefault("phases", {}).get(phase)
    if not isinstance(entry, dict):
        entry = {}
        out["phases"][phase] = entry
    prev = entry.get("fails") or []
    new_ids = {fail_identity(f) for f in fails}
    prev_ids = {fail_identity(f) for f in prev}
    resolved = sorted(prev_ids - new_ids)
    persisting = sorted(prev_ids & new_ids)
    added = sorted(new_ids - prev_ids)

    decision, reason = "continue", "ronde d'auto-correction autorisée"
    if per + 1 > CAP_PER_PHASE:
        decision, reason = "escalate", f"plafond par phase atteint ({CAP_PER_PHASE} pour {phase})"
    elif total + 1 > CAP_TOTAL:
        decision, reason = "escalate", f"plafond global atteint ({CAP_TOTAL})"
    elif not keep_fails and per > 0 and prev_ids and not resolved:
        decision, reason = "escalate", "non-progrès : aucun FAIL précédent n'est résolu"

    if not keep_fails:
        entry["fails"] = copy.deepcopy(fails)
    if decision == "continue":
        per_gate[phase] = per + 1
        ac["total"] = total + 1
    detail = {
        "per_phase": per_gate.get(phase, per),
        "total": ac["total"],
        "resolved": resolved,
        "persisting": persisting,
        "new": added,
    }
    return decision, reason, out, detail


# --- Vue legacy et validation ------------------------------------------------------------

def normalize(battle: dict) -> dict:
    """Vue de lecture d'une battle avec les défauts legacy remplis (copie ; ne s'écrit jamais
    telle quelle : ne pas persister, sinon on ajouterait run.mode rétroactivement)."""
    out = copy.deepcopy(battle) if isinstance(battle, dict) else {}
    out.setdefault("phases", {})
    out.setdefault("required_gates", list(DEFAULT_REQUIRED_GATES))
    run = out.setdefault("run", {})
    run.setdefault("mode", "autonomous")
    ac = run.setdefault("autocorrect", {})
    ac.setdefault("per_gate", {})
    ac.setdefault("total", 0)
    out.setdefault("delivery", {}).setdefault("pr_url", None)
    return out


def validate(battle) -> tuple[list[str], list[str]]:
    """Valide la structure. Champs inconnus tolérés. Retourne (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(battle, dict):
        return ["battle.json n'est pas un objet"], warnings
    if not battle.get("id"):
        warnings.append("champ id absent")
    phases = battle.get("phases")
    if not isinstance(phases, dict):
        return ["phases absent ou invalide"], warnings
    for name, entry in phases.items():
        if name in GATES and name not in PHASES:
            warnings.append(f"clé de phase au nom de gate : {name!r} (attendu {GATE_PHASE[name]!r})")
            continue
        if name not in PHASES:
            warnings.append(f"phase inconnue : {name!r}")
        if not isinstance(entry, dict):
            errors.append(f"phases.{name} n'est pas un objet")
            continue
        st = entry.get("status")
        if st not in STATUSES:
            errors.append(f"phases.{name}.status invalide : {st!r}")
        v = entry.get("verdict")
        if v is not None and v not in VERDICTS:
            errors.append(f"phases.{name}.verdict invalide : {v!r}")
    rg = battle.get("required_gates")
    if rg is not None:
        if not isinstance(rg, list):
            errors.append("required_gates n'est pas une liste")
        else:
            for g in rg:
                if g not in GATES:
                    warnings.append(f"required_gates : gate inconnue {g!r}")
    run = battle.get("run")
    if isinstance(run, dict) and isinstance(run.get("autocorrect"), dict):
        per_gate = run["autocorrect"].get("per_gate")
        if isinstance(per_gate, dict):
            for k in per_gate:
                if k not in AUTOCORRECT_KEYS:
                    warnings.append(f"run.autocorrect.per_gate : clé hors phase {k!r}")
    return errors, warnings


# --- Couche I/O --------------------------------------------------------------------------

class _Refuse(Exception):
    """Refus (usage invalide, transition refusée, état illisible) -> exit 2 + JSON ok:false."""

    def __init__(self, reason: str, **extra):
        super().__init__(reason)
        self.reason = reason
        self.extra = extra


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _battles_dir(root: Path) -> Path:
    return root / ".legion" / "battles"


def _pointer_path(root: Path) -> Path:
    return root / ".legion" / "active-battle"


_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")


def _battle_dir(root: Path, battle_id: str) -> Path:
    if not isinstance(battle_id, str) or not _ID_RE.fullmatch(battle_id):
        raise _Refuse(f"identifiant de battle invalide : {battle_id!r} (attendu : [A-Za-z0-9-])")
    base = _battles_dir(root)
    target = base / battle_id
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError:
        raise _Refuse(f"identifiant de battle hors de .legion/battles/ : {battle_id!r}")
    return target


def _read_pointer(root: Path) -> str:
    try:
        return _pointer_path(root).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_pointer(root: Path, value: str) -> None:
    path = _pointer_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(path, value)


def _write_text_atomic(path: Path, text: str) -> None:
    """Fichier temporaire dans le même dossier puis `os.replace` (jamais de fichier tronqué)."""
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:  # nouveau fichier : mode par défaut selon l'umask
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _atomic_write(path: Path, data: dict) -> None:
    _write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _load(battle_dir: Path) -> dict:
    path = battle_dir / "battle.json"
    if not path.is_file():
        raise _Refuse(f"battle introuvable : {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _Refuse(f"battle.json illisible : {type(exc).__name__}: {exc}")
    if not isinstance(data, dict):
        raise _Refuse("battle.json illisible : la racine n'est pas un objet")
    return data


def _save(battle_dir: Path, battle: dict) -> None:
    try:
        _atomic_write(battle_dir / "battle.json", battle)
    except OSError as exc:
        raise _Refuse(f"écriture de battle.json impossible : {exc}")


def _load_fleet_sync():
    """Charge `hooks/fleet_sync.py` par chemin explicite (aucun module homonyme ne peut le
    masquer, pas de `sys.path` modifié)."""
    import importlib.util  # noqa: PLC0415 - import paresseux voulu
    path = Path(__file__).resolve().parent.parent / "hooks" / "fleet_sync.py"
    spec = importlib.util.spec_from_file_location("legion_fleet_sync", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"chargement impossible : {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sync_fleet(battle_dir: Path, repo_root: Path, upsert=None, fleet_dir=None) -> list[str]:
    """Projette la battle dans l'index fleet (le PostToolUse ne voit pas les écritures Bash).
    Import paresseux de `hooks/fleet_sync.py` (pas de cycle au chargement). Toute exception
    devient un warning : la transition reste ok."""
    try:
        if upsert is None or fleet_dir is None:
            fleet_sync = _load_fleet_sync()
            upsert = upsert or fleet_sync.upsert
            fleet_dir = fleet_dir or fleet_sync._fleet_dir()
        upsert(battle_dir, repo_root, fleet_dir)
    except Exception as exc:  # noqa: BLE001 - jamais bloquant
        msg = f"fleet_sync: {type(exc).__name__}: {exc}"
        print(msg, file=sys.stderr)
        return [msg]
    return []


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # pas de usage/exit brut : refus JSON
        raise _Refuse(f"usage invalide : {message}")


def _json_arg(raw: str, what: str):
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise _Refuse(f"{what} : JSON invalide ({exc})")


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--battle", default=argparse.SUPPRESS, help="id (défaut : pointeur active-battle)")
    common.add_argument("--repo", default=argparse.SUPPRESS, help="racine du dépôt (défaut : cwd)")
    p = _Parser(prog="battle_state.py", parents=[common],
                description="Transitions d'état déterministes de battle.json")
    sub = p.add_subparsers(dest="cmd", parser_class=_Parser)

    def add(name, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    s = add("init")
    s.add_argument("id")
    s.add_argument("--ticket", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--profile", required=True)
    s.add_argument("--step", action="store_true")
    s.add_argument("--required-gates", nargs="+", default=None)
    s = add("transition")
    s.add_argument("phase")
    s.add_argument("status")
    s.add_argument("--verdict", default=None)
    s.add_argument("--fails", default=None, help="tableau JSON (plan seulement)")
    s.add_argument("--round", type=int, default=None, dest="round_")
    s.add_argument("--threads", default=None, help="tableau JSON (address seulement)")
    add("approve-plan")
    s = add("bump-autocorrect")
    s.add_argument("phase")
    s.add_argument("--fails", default=None, help="tableau JSON de FAIL")
    s.add_argument("--build-failure", action="store_true",
                   help="échec de build pendant la correction d'une gate : compte sous la clé "
                        "sans remplacer ses FAIL")
    s = add("set-delivery")
    s.add_argument("--pr-url", required=True)
    s = add("set-guard")
    s.add_argument("--allow", nargs="*", default=None)
    s.add_argument("--deny", nargs="*", default=None)
    s.add_argument("--careful", choices=("on", "off"), default=None)
    s = add("set-meta")
    s.add_argument("--title")
    s.add_argument("--profile")
    s.add_argument("--required-gates", nargs="+", default=None)
    s.add_argument("--stack-kind")
    s.add_argument("--build-target")
    s.add_argument("--test-target")
    s = add("activate")
    s.add_argument("id")
    add("close")
    add("validate")
    return p


def _new_battle(root: Path, args) -> dict:
    phases = {"think": {"status": "in_progress"}}
    for name in PHASES[1:]:
        if name not in ("security", "address"):
            phases[name] = {"status": "pending"}
    return {
        "id": args.id, "repo": root.name, "ticket": args.ticket, "title": args.title,
        "profile": args.profile,
        "required_gates": list(args.required_gates or DEFAULT_REQUIRED_GATES),
        "phases": phases,
        "guard": {"allow": [], "deny": [], "careful": False},
        "stack": {"kind": None, "build_target": None, "test_target": None},
        "delivery": {"pr_url": None},
        "run": {"mode": "step" if args.step else "autonomous",
                "autocorrect": {"per_gate": {}, "total": 0}},
    }


def _mutation(args, battle: dict) -> tuple[dict, dict]:
    """Applique la sous-commande `args.cmd` sur `battle` (cœur pur). Retourne (battle, extra)."""
    cmd = args.cmd
    if cmd == "transition":
        fails = _json_arg(args.fails, "--fails") if args.fails is not None else None
        threads = _json_arg(args.threads, "--threads") if args.threads is not None else None
        ok, reason = check_transition(battle, args.phase, args.status, args.verdict, fails,
                                      args.round_, threads)
        if not ok:
            raise _Refuse(reason)
        out = apply_transition(battle, args.phase, args.status, args.verdict, fails,
                               args.round_, threads)
        return out, {"phase": args.phase, "status": args.status}
    if cmd == "approve-plan":
        now = _now_iso()
        ok, reason, out = approve_plan(battle, now)
        if not ok:
            raise _Refuse(reason)
        return out, {"approved_at": now}
    if cmd == "bump-autocorrect":
        if args.build_failure == (args.fails is not None):
            raise _Refuse("usage invalide : exactement un de --fails / --build-failure")
        fails = [] if args.build_failure else _json_arg(args.fails, "--fails")
        decision, reason, out, detail = bump_autocorrect(battle, args.phase, fails,
                                                         keep_fails=args.build_failure)
        if decision == "refuse":
            raise _Refuse(reason, **detail)
        return out, {"decision": decision, "reason": reason, **detail}
    out = copy.deepcopy(battle)
    if cmd == "set-delivery":
        out.setdefault("delivery", {})["pr_url"] = args.pr_url
        return out, {"pr_url": args.pr_url}
    if cmd == "set-guard":
        if args.allow is None and args.deny is None and args.careful is None:
            raise _Refuse("set-guard : aucune option (--allow, --deny ou --careful)")
        guard = out.setdefault("guard", {})
        if args.allow is not None:
            guard["allow"] = list(args.allow)
        if args.deny is not None:
            guard["deny"] = list(args.deny)
        if args.careful is not None:
            guard["careful"] = args.careful == "on"
        return out, {"guard": guard}
    if cmd == "set-meta":
        touched = []
        for key in ("title", "profile"):
            if getattr(args, key) is not None:
                out[key] = getattr(args, key)
                touched.append(key)
        if args.required_gates is not None:
            bad = [g for g in args.required_gates if g not in GATES]
            if bad:
                raise _Refuse(f"gate(s) inconnue(s) : {', '.join(bad)} (attendu : {', '.join(GATES)})")
            out["required_gates"] = list(args.required_gates)
            touched.append("required_gates")
        stack = out.get("stack") if isinstance(out.get("stack"), dict) else {}
        for opt, key in (("stack_kind", "kind"), ("build_target", "build_target"),
                         ("test_target", "test_target")):
            val = getattr(args, opt)
            if val is not None:
                stack[key] = val or None  # "" => null
                out["stack"] = stack
                touched.append(f"stack.{key}")
        if not touched:
            raise _Refuse("set-meta : aucune option")
        return out, {"updated": touched}
    if cmd == "close":
        entry = out.setdefault("phases", {}).get("reflect")
        if not isinstance(entry, dict):
            entry = {}
            out["phases"]["reflect"] = entry
        entry["status"] = "done"
        return out, {"phase": "reflect", "status": "done"}
    raise _Refuse(f"sous-commande inconnue : {cmd}")  # pragma: no cover


def run_command(argv: list[str], upsert=None, fleet_dir=None) -> tuple[int, dict]:
    """Exécute une sous-commande. Retourne (code, résultat JSON). Ne lève jamais.
    `upsert`/`fleet_dir` : injection pour les tests (sinon `fleet_sync` réel)."""
    try:
        args = _build_parser().parse_args(argv)
        if not args.cmd:
            raise _Refuse("usage invalide : sous-commande requise")
        root = Path(getattr(args, "repo", None) or Path.cwd())
        explicit = getattr(args, "battle", None)
        result: dict = {"ok": True}

        if args.cmd == "init":
            bdir = _battle_dir(root, args.id)
            if (bdir / "battle.json").exists():
                raise _Refuse(f"battle déjà existante : {args.id}")
            unknown = [g for g in (args.required_gates or []) if g not in GATES]
            if unknown:
                raise _Refuse(f"gate(s) inconnue(s) : {', '.join(unknown)}")
            bdir.mkdir(parents=True, exist_ok=True)
            _save(bdir, _new_battle(root, args))
            _write_pointer(root, args.id)
            result.update(battle=args.id)
        elif args.cmd == "activate":
            bdir = _battle_dir(root, args.id)
            _load(bdir)
            _write_pointer(root, args.id)
            return 0, {"ok": True, "battle": args.id, "active": args.id}
        else:
            bid = explicit or _read_pointer(root)
            if not bid:
                raise _Refuse("aucune battle active (utiliser --battle <id> ou `activate`)")
            bdir = _battle_dir(root, bid)
            battle = _load(bdir)
            if args.cmd == "validate":
                errors, warnings = validate(battle)
                return (0 if not errors else 2), {"ok": not errors, "battle": bid,
                                                  "errors": errors, "warnings": warnings}
            new, extra = _mutation(args, battle)
            _save(bdir, new)
            result.update(battle=bid, **extra)
            if args.cmd == "close" and _read_pointer(root) == bid:
                _write_pointer(root, "")
                result["pointer_cleared"] = True

        result["warnings"] = _sync_fleet(bdir, root, upsert, fleet_dir)
        return 0, result
    except _Refuse as exc:
        return 2, {"ok": False, "reason": exc.reason, **exc.extra}
    except Exception as exc:  # noqa: BLE001 - jamais de trace Python
        return 2, {"ok": False, "reason": f"erreur interne : {type(exc).__name__}: {exc}"}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--self-test" in args:
        return _self_test()
    try:
        code, result = run_command(args)
    except SystemExit as exc:  # --help d'argparse
        return exc.code if isinstance(exc.code, int) else 0
    print(json.dumps(result, ensure_ascii=False))
    return code


# --- Self-test du coeur ------------------------------------------------------------------

def _fx(states: dict | None = None, **extra) -> dict:
    """Fixture : `states` = {phase: statut | (statut, verdict)}."""
    phases: dict = {}
    for p, s in (states or {}).items():
        st, vd = s if isinstance(s, tuple) else (s, None)
        entry: dict = {"status": st}
        if vd is not None:
            entry["verdict"] = vd
        phases[p] = entry
    b = {"id": "t", "phases": phases}
    b.update(extra)
    return b


def _refused(battle, phase, status, **kw) -> str:
    ok, reason = check_transition(battle, phase, status, **kw)
    assert not ok, f"transition {phase}->{status} {kw} aurait dû être refusée"
    return reason


def _t_source_consistency() -> None:
    assert set(GATE_PHASE) == set(GATES) and set(GATE_ARTIFACT) == set(GATES)
    assert all(p in PHASES for p in GATE_PHASE.values())
    for sub in (VERDICT_PHASES, CASCADE_PHASES, AUTOCORRECT_KEYS):
        assert all(p in PHASES for p in sub if p != "build") and "build" in PHASES
        idx = [PHASES.index(p) for p in sub]
        assert idx == sorted(idx), sub
    assert set(DEFAULT_REQUIRED_GATES) <= set(GATES)
    assert len(set(PHASES)) == len(PHASES) and PHASES[0] == "think" and PHASES[-1] == "reflect"


def _t_unknown_phase_status() -> None:
    b = _fx({"think": "done"})
    _refused(b, "nope", "in_progress")
    _refused(b, "plan", "nope")
    _refused(b, "plan", "pending")
    _refused(b, "reflect", "done")


def _t_verdict_status_coherence() -> None:
    b = _fx({"think": "done", "plan": "in_progress", "build": "done"})
    _refused(b, "plan", "done", verdict="revise")
    _refused(b, "plan", "blocked", verdict="accept")
    _refused(b, "plan", "done")                      # done sans verdict sur phase a verdict
    _refused(b, "build", "done", verdict="accept")   # build ne porte pas de verdict
    _refused(b, "plan", "in_progress", verdict="accept")
    _refused(b, "plan", "blocked", verdict="bogus")
    assert check_transition(b, "plan", "done", verdict="accept")[0]
    assert check_transition(b, "plan", "blocked", verdict="reject")[0]
    assert check_transition(b, "build", "blocked")[0]  # blocked sans verdict permis


def _t_plan_requires_think() -> None:
    assert "think" in _refused(_fx({"think": "in_progress"}), "plan", "in_progress")
    assert check_transition(_fx({"think": "done"}), "plan", "in_progress")[0]


def _t_build_refused_without_approval() -> None:
    b = _fx({"think": "done", "plan": ("done", "accept")})
    assert "plan non approuvé" in _refused(b, "build", "in_progress")


def _t_build_refused_plan_not_accepted() -> None:
    for plan in (("blocked", "revise"), ("blocked", "reject"), "in_progress"):
        b = _fx({"think": "done", "plan": plan})
        b["phases"]["plan"]["approved_at"] = "2026-01-01T00:00:00Z"
        _refused(b, "build", "in_progress")


def _t_approve_plan() -> None:
    bad = _fx({"think": "done", "plan": ("blocked", "revise")})
    ok, _, same = approve_plan(bad, "T0")
    assert not ok and same is bad
    b = _fx({"think": "done", "plan": ("done", "accept_with_opportunity")})
    ok, _, b1 = approve_plan(b, "T1")
    assert ok and b1["phases"]["plan"]["approved_at"] == "T1"
    assert "approved_at" not in b["phases"]["plan"]  # entrée non mutée
    assert check_transition(b1, "build", "in_progress")[0]
    ok, _, b2 = approve_plan(b1, "T2")  # idempotent : réécrit l'horodatage
    assert ok and b2["phases"]["plan"]["approved_at"] == "T2"
    assert check_transition(b2, "build", "in_progress")[0]


def _t_plan_rerun_clears_approval() -> None:
    b = _fx({"think": "done", "plan": ("done", "accept")})
    _, _, b = approve_plan(b, "T1")
    b = apply_transition(b, "plan", "in_progress")
    assert b["phases"]["plan"]["approved_at"] is None
    _refused(b, "build", "in_progress")


def _t_plan_fails_persist_and_clear() -> None:
    fails = [{"target": "a.py:1", "dimension": "R2", "note": "x"}]
    b = _fx({"think": "done", "plan": "in_progress"})
    b = apply_transition(b, "plan", "blocked", verdict="revise", fails=fails)
    assert b["phases"]["plan"]["fails"] == fails
    b = apply_transition(b, "plan", "in_progress")
    b = apply_transition(b, "plan", "done", verdict="accept")
    assert b["phases"]["plan"]["fails"] == []
    b2 = _fx({"think": "done", "plan": "done", "build": "done", "review": "in_progress"})
    assert "plan" in _refused(b2, "review", "blocked", verdict="revise", fails=fails)


def _t_cascade_requires_build_done() -> None:
    for p in CASCADE_PHASES:
        for st in ("in_progress", "done"):
            kw = {"verdict": "accept"} if st == "done" else {}
            b = _fx({"build": "in_progress"})
            assert "build" in _refused(b, p, st, **kw)
            b = _fx({"build": "done"})
            assert check_transition(b, p, st, **kw)[0], (p, st)


def _t_deliver_requires_required_gates() -> None:
    done = {"plan": ("done", "accept"), "lint": ("done", "accept"),
            "review": ("done", "accept"), "test": "pending"}
    b = _fx(done)
    assert "test" in _refused(b, "deliver", "in_progress")
    b = _fx({**done, "test": ("done", "accept")})
    assert check_transition(b, "deliver", "in_progress")[0]  # required_gates absent => defaut
    assert "security" not in b["phases"]  # non requise et absente : ignoree
    b = _fx({**done, "test": ("done", "accept")}, required_gates=["architect", "security"])
    assert "security" in _refused(b, "deliver", "in_progress")
    b["phases"]["security"] = {"status": "done", "verdict": "accept"}
    assert check_transition(b, "deliver", "in_progress")[0]
    # pr-triage exclu meme s'il est declare
    b = _fx({"plan": ("done", "accept")}, required_gates=["architect", "pr-triage"])
    assert check_transition(b, "deliver", "done")[0]


def _t_address_requires_pr_url() -> None:
    b = _fx({"deliver": "done"})
    assert "pr_url" in _refused(b, "address", "in_progress")
    b["delivery"] = {"pr_url": "https://example.test/pr/1"}
    threads = [{"id": "T1", "target": "builder"}]
    b2 = apply_transition(b, "address", "done", round_=2, threads=threads)
    e = b2["phases"]["address"]
    assert e["status"] == "done" and e["round"] == 2 and e["threads"] == threads
    _refused(b, "review", "blocked", round_=1)


def _bump(b, phase, ids):
    return bump_autocorrect(b, phase, [{"target": t, "dimension": "R1"} for t in ids])


def _t_bump_first_round_no_progress_check() -> None:
    d, _, b, det = _bump(_fx({"build": "done"}), "review", ["A"])
    assert d == "continue"
    ac = b["run"]["autocorrect"]
    assert ac["per_gate"]["review"] == 1 and ac["total"] == 1
    assert det["per_phase"] == 1 and det["total"] == 1 and det["resolved"] == []


def _t_bump_progress_by_identity() -> None:
    _, _, b, _ = _bump(_fx({"build": "done"}), "review", ["A", "B"])
    d, _, b, det = _bump(b, "review", ["B", "C"])  # compte stable, mais A resolu
    assert d == "continue", det
    assert det["resolved"] == ["A|R1"] and det["new"] == ["C|R1"] and det["persisting"] == ["B|R1"]
    assert b["run"]["autocorrect"]["per_gate"]["review"] == 2


def _t_bump_no_progress_escalates() -> None:
    _, _, b, _ = _bump(_fx({"build": "done"}), "review", ["A"])
    d, reason, b2, _ = _bump(b, "review", ["A"])
    assert d == "escalate" and "progrès" in reason, reason
    assert b2["run"]["autocorrect"]["per_gate"]["review"] == 1 and b2["run"]["autocorrect"]["total"] == 1
    d, _, b3, _ = _bump(b, "review", ["A", "B"])
    assert d == "escalate"
    assert b3["run"]["autocorrect"]["total"] == 1
    assert len(b3["phases"]["review"]["fails"]) == 2  # FAIL quand meme enregistres


def _t_bump_cap_per_phase() -> None:
    b = _fx({"build": "done"})
    _, _, b, _ = _bump(b, "review", ["A"])
    d, _, b, _ = _bump(b, "review", ["B"])
    assert d == "continue"
    d, reason, b, _ = _bump(b, "review", ["C"])
    assert d == "escalate" and "plafond par phase" in reason, reason
    assert b["run"]["autocorrect"]["per_gate"]["review"] == 2


def _t_bump_cap_total() -> None:
    b = _fx({"build": "done"})
    for p in ("lint", "review", "test"):
        for ids in (["A"], ["B"]):
            d, _, b, _ = _bump(b, p, ids)
            assert d == "continue", (p, ids)
    assert b["run"]["autocorrect"]["total"] == 6
    d, reason, b, _ = _bump(b, "build", ["E"])
    assert d == "escalate" and "plafond global" in reason, reason


def _t_bump_keys() -> None:
    b = _fx({"build": "done"})
    d, _, same, _ = _bump(b, "plan", ["A"])
    assert d == "refuse" and same is b
    d, reason, _, det = _bump(b, "reviewer", ["A"])
    assert d == "refuse" and det["suggestion"] == "review" and "review" in reason
    d, _, _, _ = _bump(b, "build", ["A"])
    assert d == "continue"


def _t_fail_identity() -> None:
    obj = {"target": "a.py:10", "dimension": "R2", "extra": "x"}
    assert fail_identity(obj) == fail_identity("a.py:10|R2") == "a.py:10|R2"
    assert fail_identity({"target": " a.py:10 ", "dimension": " R2 "}) == "a.py:10|R2"
    assert fail_identity("  a.py:10 | R2  ") == "a.py:10|R2"
    assert fail_identity("  texte   libre ") == "texte libre"
    # ordre sans effet : comparaison par ensembles
    _, _, b, _ = _bump(_fx({"build": "done"}), "review", ["A", "B"])
    d, _, _, det = _bump(b, "review", ["B", "A", "C"])
    assert det["resolved"] == [] and det["new"] == ["C|R1"]


def _t_legacy_battle() -> None:
    legacy = {
        "id": "2026-06-08-GH-1", "repo": "r", "ticket": "GH#1", "title": "t",
        "profile": "feature", "required_gates": ["architect", "lint", "reviewer", "test-engineer"],
        "phases": {
            "think": {"status": "done", "artifact": "spec.md"},
            "plan": {"status": "done", "artifact": "plan.md", "verdict": "accept", "fails": []},
            "build": {"status": "in_progress"},
            "lint": {"status": "pending"}, "review": {"status": "pending"},
            "test": {"status": "pending"}, "deliver": {"status": "pending"},
            "reflect": {"status": "pending"},
        },
        "guard": {"allow": ["src/**"], "deny": [], "careful": False},
        "stack": {"kind": ".net", "build_target": None, "test_target": None},
        "delivery": {"pr_url": None},
        "foo": {"inconnu": True},
    }
    snapshot = copy.deepcopy(legacy)
    errors, warnings = validate(legacy)
    assert not errors and not warnings, (errors, warnings)
    b = apply_transition(legacy, "build", "done")   # pas d'approved_at, mais build deja entamee
    b = apply_transition(b, "review", "in_progress")
    _, _, b, _ = _bump(b, "review", ["A"])
    assert "run" in b and "mode" not in b["run"] and b["run"]["autocorrect"]["total"] == 1
    b = apply_transition(b, "review", "blocked", verdict="revise")
    assert "mode" not in b["run"] and b["foo"] == {"inconnu": True}
    assert legacy == snapshot  # entrée jamais mutée
    withnull = copy.deepcopy(legacy)
    withnull["phases"]["plan"]["approved_at"] = None  # clé présente à null : plus legacy
    assert "plan non approuvé" in _refused(withnull, "build", "done")
    view = normalize(legacy)
    assert view["run"]["mode"] == "autonomous" and "run" not in legacy


def _t_validate() -> None:
    ok = _fx({"think": "done"}, champ_inconnu=1)
    assert validate(ok) == ([], [])
    errs, _ = validate(_fx({"think": "weird"}))
    assert errs
    errs, _ = validate(_fx({"plan": ("done", "bogus")}))
    assert errs
    errs, warns = validate({"id": "x", "phases": {"reviewer": {"status": "done"}}})
    assert not errs and any("reviewer" in w for w in warns), (errs, warns)
    assert validate([])[0] and validate({"id": "x"})[0]


def _t_replan_requires_reapproval() -> None:
    # build deja entamee (chemin legacy ouvert) + approbation + re-plan : le null force le re-OK
    b = _fx({"think": "done", "plan": ("done", "accept"), "build": "in_progress"})
    _, _, b = approve_plan(b, "T1")
    assert check_transition(b, "build", "in_progress")[0]
    b = apply_transition(b, "plan", "in_progress")
    assert "approved_at" in b["phases"]["plan"] and b["phases"]["plan"]["approved_at"] is None
    b = apply_transition(b, "plan", "done", verdict="accept")
    assert "plan non approuvé" in _refused(b, "build", "in_progress")
    assert "plan non approuvé" in _refused(b, "build", "done")
    _, _, b = approve_plan(b, "T2")
    assert check_transition(b, "build", "in_progress")[0]


_CORE_TESTS = (
    _t_source_consistency, _t_unknown_phase_status, _t_verdict_status_coherence,
    _t_plan_requires_think, _t_build_refused_without_approval,
    _t_build_refused_plan_not_accepted, _t_approve_plan,
    _t_plan_rerun_clears_approval, _t_replan_requires_reapproval,
    _t_plan_fails_persist_and_clear, _t_cascade_requires_build_done,
    _t_deliver_requires_required_gates, _t_address_requires_pr_url,
    _t_bump_first_round_no_progress_check, _t_bump_progress_by_identity,
    _t_bump_no_progress_escalates, _t_bump_cap_per_phase, _t_bump_cap_total, _t_bump_keys,
    _t_fail_identity, _t_legacy_battle, _t_validate,
)


# --- Tests d'integration (couche I/O + CLI) ----------------------------------------------

class _Repo:
    """Repo temporaire + appels in-process avec un upsert factice (jamais le vrai fleet)."""

    def __init__(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "monrepo"
        self.root.mkdir()
        self.fleet = Path(self._td.name) / "fleet.d"
        self.calls: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._td.cleanup()

    def _upsert(self, battle_dir, repo_root, fleet_dir):
        self.calls.append((battle_dir, repo_root, fleet_dir))

    def run(self, *argv):
        return run_command(["--repo", str(self.root), *argv], self._upsert, self.fleet)

    def ok(self, *argv) -> dict:
        code, res = self.run(*argv)
        assert code == 0 and res["ok"], (argv, code, res)
        return res

    def refused(self, *argv) -> str:
        code, res = self.run(*argv)
        assert code == 2 and res["ok"] is False and res["reason"], (argv, code, res)
        return res["reason"]

    def path(self, bid="b1") -> Path:
        return self.root / ".legion" / "battles" / bid / "battle.json"

    def load(self, bid="b1") -> dict:
        return json.loads(self.path(bid).read_text(encoding="utf-8"))

    def init(self, bid="b1", *extra):
        return self.ok("init", bid, "--ticket", "GH#1", "--title", "T", "--profile", "feature", *extra)


def _t_init() -> None:
    with _Repo() as r:
        r.init()
        b = r.load()
        assert b["phases"]["think"]["status"] == "in_progress" and b["repo"] == "monrepo"
        assert "security" not in b["phases"] and "address" not in b["phases"]
        assert all(b["phases"][p]["status"] == "pending" for p in ("plan", "build", "deliver", "reflect"))
        assert b["run"] == {"mode": "autonomous", "autocorrect": {"per_gate": {}, "total": 0}}
        assert (r.root / ".legion" / "active-battle").read_text(encoding="utf-8") == "b1"
        assert "already" in r.refused("init", "b1", "--ticket", "x", "--title", "x",
                                      "--profile", "x").replace("déjà", "already")
        r.init("b2", "--step", "--required-gates", "architect", "security")
        b2 = r.load("b2")
        assert b2["run"]["mode"] == "step" and b2["required_gates"] == ["architect", "security"]
        assert (r.root / ".legion" / "active-battle").read_text(encoding="utf-8") == "b2"
        r.refused("init", "../x", "--ticket", "x", "--title", "x", "--profile", "x")


def _t_set_guard() -> None:
    with _Repo() as r:
        r.init()
        r.ok("set-guard", "--allow", "a/**", "b/**")
        assert r.load()["guard"] == {"allow": ["a/**", "b/**"], "deny": [], "careful": False}
        r.ok("set-guard", "--deny", "secrets/**", "--careful", "on")
        g = r.load()["guard"]
        assert g["allow"] == ["a/**", "b/**"] and g["deny"] == ["secrets/**"] and g["careful"] is True
        r.ok("set-guard", "--allow")
        g = r.load()["guard"]
        assert g["allow"] == [] and g["deny"] == ["secrets/**"] and g["careful"] is True
        r.ok("set-guard", "--allow", "--deny", "--careful", "off")
        assert r.load()["guard"] == {"allow": [], "deny": [], "careful": False}
        r.refused("set-guard")


def _t_set_meta() -> None:
    with _Repo() as r:
        r.init()
        before = r.load()
        r.ok("set-meta", "--title", "Nouveau", "--profile", "bugfix", "--required-gates",
             "architect", "lint", "--stack-kind", ".net", "--build-target", "src/a.csproj")
        b = r.load()
        assert b["title"] == "Nouveau" and b["profile"] == "bugfix"
        assert b["required_gates"] == ["architect", "lint"]
        assert b["stack"] == {"kind": ".net", "build_target": "src/a.csproj", "test_target": None}
        assert b["phases"] == before["phases"] and b["guard"] == before["guard"]
        r.ok("set-meta", "--build-target", "")
        assert r.load()["stack"]["build_target"] is None
        r.refused("set-meta", "--required-gates", "reviewr")
        r.refused("set-meta")


def _t_activate_close() -> None:
    with _Repo() as r:
        r.init("b1")
        r.init("b2")
        pointer = r.root / ".legion" / "active-battle"
        r.ok("activate", "b1")
        assert pointer.read_text(encoding="utf-8") == "b1"
        r.refused("activate", "inconnu")
        assert pointer.read_text(encoding="utf-8") == "b1"
        r.ok("close", "--battle", "b2")            # le pointeur désigne b1 : conservé
        assert r.load("b2")["phases"]["reflect"]["status"] == "done"
        assert pointer.read_text(encoding="utf-8") == "b1"
        res = r.ok("close")                        # battle active = b1
        assert r.load("b1")["phases"]["reflect"]["status"] == "done" and res["pointer_cleared"]
        assert pointer.read_text(encoding="utf-8") == ""
        assert "aucune battle active" in r.refused("validate")


def _t_atomic_and_corrupt() -> None:
    with _Repo() as r:
        r.init()
        r.ok("set-guard", "--allow", "x/**")
        r.ok("transition", "think", "done")
        assert not list(r.path().parent.glob("*.tmp"))
        assert not list((r.root / ".legion").glob("*.tmp"))
        r.path().write_text("{ pas du json", encoding="utf-8")
        reason = r.refused("transition", "plan", "in_progress")
        assert "illisible" in reason and "Traceback" not in reason
        assert "illisible" in r.refused("validate")
        assert r.path().read_text(encoding="utf-8") == "{ pas du json"  # rien d'écrasé
        r.path().write_text("[1]", encoding="utf-8")
        assert "illisible" in r.refused("validate")


def _t_fleet_sync_called() -> None:
    with _Repo() as r:
        r.init()
        assert len(r.calls) == 1
        assert r.calls[0][0] == r.path().parent and r.calls[0][1] == r.root
        r.ok("transition", "think", "done")
        assert len(r.calls) == 2
        r.refused("transition", "build", "in_progress")   # refus : pas de synchro
        r.ok("validate")
        assert len(r.calls) == 2
        # upsert qui lève => ok + warnings
        def boom(*a):
            raise RuntimeError("kaboom")
        code, res = run_command(["--repo", str(r.root), "set-guard", "--allow", "z"], boom, r.fleet)
        assert code == 0 and res["ok"] and "kaboom" in res["warnings"][0], res
        assert r.load()["guard"]["allow"] == ["z"]
    # appel réel (subprocess, LEGION_FLEET temporaire) : un shard écrit
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        code, res = _cli(td, "--repo", str(repo), "init", "b1", "--ticket", "GH#1", "--title", "T",
                         "--profile", "feature")
        assert code == 0 and res["warnings"] == [], res
        shards = list((Path(td) / "fleet.d").glob("*.json"))
        assert len(shards) == 1
        assert json.loads(shards[0].read_text(encoding="utf-8"))["id"] == "b1"


def _cli(tmp: str, *argv: str) -> tuple[int, dict]:
    import subprocess
    env = dict(os.environ, LEGION_FLEET=str(Path(tmp) / "fleet.json"))
    p = subprocess.run([sys.executable, str(Path(__file__).resolve()), *argv], env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert "Traceback" not in p.stderr, p.stderr
    return p.returncode, json.loads(p.stdout)


def _t_phases_cs() -> None:
    cs = Path(__file__).resolve().parents[3] / "ui/legatus/src/presentation/Models/Phases.cs"
    if not cs.is_file():
        print(f"SKIP: _t_phases_cs ({cs} absent, cache de plugin ?)", file=sys.stderr)
        return
    text = cs.read_text(encoding="utf-8")
    m = re.search(r"Pipeline\s*=\s*\[(.*?)\]\s*;", text, re.S)
    assert m, "Pipeline introuvable dans Phases.cs"
    order = [x.lower() for x in re.findall(r"Phase\.(\w+)", m.group(1))]
    assert order == list(PHASES), (order, PHASES)


def _t_cli_exit_codes() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        g = ["--repo", str(repo)]
        code, res = _cli(td, *g, "init", "b1", "--ticket", "GH#1", "--title", "T", "--profile", "f")
        assert code == 0 and res["ok"] is True
        code, res = _cli(td, *g, "transition", "plan", "in_progress")   # think pas done
        assert code == 2 and res["ok"] is False and "think" in res["reason"]
        code, res = _cli(td, *g, "transition", "think", "done")
        assert code == 0
        code, res = _cli(td, *g, "nimportequoi")
        assert code == 2 and res["ok"] is False
        code, res = _cli(td, *g, "transition", "plan", "blocked", "--verdict", "revise",
                         "--fails", "pas du json")
        assert code == 2 and "JSON" in res["reason"]
        code, res = _cli(td, *g, "transition", "plan", "blocked", "--verdict", "revise",
                         "--fails", '[{"target":"a","dimension":"R1"}]')
        assert code == 0
        code, res = _cli(td, *g, "bump-autocorrect", "reviewer", "--fails", "[]")
        assert code == 2 and res.get("suggestion") == "review"
        # escalate => exit 0
        b = json.loads((repo / ".legion/battles/b1/battle.json").read_text(encoding="utf-8"))
        b["phases"]["build"]["status"] = "done"
        (repo / ".legion/battles/b1/battle.json").write_text(json.dumps(b), encoding="utf-8")
        f = '[{"target":"a","dimension":"R1"}]'
        code, res = _cli(td, *g, "bump-autocorrect", "review", "--fails", f)
        assert code == 0 and res["decision"] == "continue"
        code, res = _cli(td, *g, "bump-autocorrect", "review", "--fails", f)
        assert code == 0 and res["decision"] == "escalate" and res["ok"] is True
        code, res = _cli(td, *g, "validate")
        assert code == 0 and res["errors"] == []
        # transition address avec round/threads
        code, res = _cli(td, *g, "set-delivery", "--pr-url", "https://x.test/pr/1")
        assert code == 0
        code, res = _cli(td, *g, "transition", "address", "done", "--round", "1", "--threads",
                         '[{"id":"T1"}]')
        assert code == 0
        b = json.loads((repo / ".legion/battles/b1/battle.json").read_text(encoding="utf-8"))
        assert b["phases"]["address"]["round"] == 1 and b["phases"]["address"]["threads"] == [{"id": "T1"}]


def _t_id_whitelist() -> None:
    with _Repo() as r:
        for bad in ("D:x", "a/b", "a\\b", "..", ".", "-x", "a b", "a_b", "é", "x:y"):
            reason = r.refused("init", bad, "--ticket", "T", "--title", "T", "--profile", "f")
            assert "invalide" in reason, (bad, reason)
        r.init("2026-09-29-GH-69")
        # lien symbolique sortant de .legion/battles/
        outside = r.root / "dehors"
        outside.mkdir()
        try:
            (r.root / ".legion" / "battles" / "lnk").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        reason = r.refused("init", "lnk", "--ticket", "T", "--title", "T", "--profile", "f")
        assert "hors de" in reason and not list(outside.iterdir())


def _t_atomic_keeps_mode() -> None:
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "x.json"
        f.write_text("a", encoding="utf-8")
        os.chmod(f, 0o644)
        _write_text_atomic(f, "b")
        assert f.read_text(encoding="utf-8") == "b" and (f.stat().st_mode & 0o777) == 0o644
        os.chmod(f, 0o640)
        _write_text_atomic(f, "c")
        assert (f.stat().st_mode & 0o777) == 0o640
        if os.name == "posix":
            g = Path(td) / "new"
            umask = os.umask(0)
            os.umask(umask)
            _write_text_atomic(g, "z")
            assert (g.stat().st_mode & 0o777) == (0o666 & ~umask)


def _t_fleet_sync_explicit_path() -> None:
    import subprocess
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "fleet_sync.py").write_text("raise SystemExit('masque')\n", encoding="utf-8")
        code = ("import sys; sys.path.insert(0, %r); import battle_state as b; "
                "m = b._load_fleet_sync(); print(m.__file__); print(sys.path.count(%r))"
                % (str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent / "hooks")))
        p = subprocess.run([sys.executable, "-c", code], cwd=td, capture_output=True, text=True)
        out = p.stdout.split()
        assert p.returncode == 0 and out[0].endswith(os.path.join("hooks", "fleet_sync.py"), ) \
            and out[1] == "0", (p.stdout, p.stderr)


def _t_bump_build_failure_keeps_fails() -> None:
    b = _fx({"build": "done"})
    d, _, b, _ = _bump(b, "review", ["A", "B"])
    assert d == "continue"
    before = b["phases"]["review"]["fails"]
    d, _, b2, det = bump_autocorrect(b, "review", [], keep_fails=True)
    assert d == "continue" and b2["phases"]["review"]["fails"] == before
    assert det["per_phase"] == 2 and b2["run"]["autocorrect"]["total"] == 2
    d, _, _, _ = bump_autocorrect(b2, "review", [], keep_fails=True)
    assert d == "escalate"   # plafond par phase
    with _Repo() as r:
        r.init()
        assert "exactement" in r.refused("bump-autocorrect", "build")
        assert "exactement" in r.refused("bump-autocorrect", "build", "--fails", "[]", "--build-failure")


def _t_import_no_side_effect() -> None:
    import subprocess
    with tempfile.TemporaryDirectory() as td:
        code = ("import sys, os; sys.path.insert(0, %r); import battle_state; "
                "print(len(os.listdir('.')))" % str(Path(__file__).resolve().parent))
        p = subprocess.run([sys.executable, "-c", code], cwd=td, capture_output=True, text=True)
        assert p.returncode == 0 and p.stdout.strip() == "0" and not p.stderr, (p.stdout, p.stderr)


_INTEGRATION_TESTS = (
    _t_set_guard, _t_set_meta, _t_init, _t_activate_close, _t_atomic_and_corrupt,
    _t_fleet_sync_called, _t_phases_cs, _t_cli_exit_codes, _t_import_no_side_effect,
    _t_id_whitelist, _t_atomic_keeps_mode, _t_fleet_sync_explicit_path,
    _t_bump_build_failure_keeps_fails,
)


def _self_test() -> int:
    failed = 0
    tests = _CORE_TESTS + _INTEGRATION_TESTS
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - on rapporte puis on continue
            failed += 1
            print(f"FAIL: {fn.__name__}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if failed:
        print(f"FAIL: battle_state self-test ({failed}/{len(tests)} en échec)",
              file=sys.stderr)
        return 1
    print("OK: battle_state self-test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # accents FR sur une console cp1252
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
