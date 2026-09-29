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
- `PROFILES`, `DEFAULT_PROFILE` (gates requises par profil ; `init` en dérive `required_gates`) ;
- `CAP_PER_PHASE`, `CAP_TOTAL` ;
- `SUBCOMMANDS` (sous-commandes du CLI, dans l'ordre du parser ; la doctrine est testée contre elle).

Lecteurs partagés (hooks, GH#85), en lecture seule, sans exception : `active_battle_id(repo_root)
-> str | None` (pointeur + liste blanche d'id, ne lit pas `battle.json`),
`load_active_battle(repo_root) -> (id, dict) | None` (dict brut de `battle.json`),
`battles_dir(root)` et `guard_of(battle) -> (dict | None, bool)` (GH#104 : bloc `guard` et sa
validité ; pur, ne lève jamais ; source unique de la forme valide, partagée avec `validate`).

Cœur pur :
- `check_transition(battle, phase, status, verdict, fails, round_, threads) -> (ok, reason)` ;
- `apply_transition(..., now_iso=None) -> battle` (copie ; lève `ValueError` si la transition est refusée) ;
  `plan in_progress` (re-plan, GH#93) invalide d'office la cascade `done`/`blocked`/`in_progress` (raison `replan`,
  un seul événement dans `run.invalidations` ; rien si la cascade est déjà `pending`) ;
- `approve_plan(battle, now_iso) -> (ok, reason, battle)` ;
- `bump_autocorrect(battle, phase, fails, keep_fails=False, now_iso=None) -> (decision, reason, battle, detail)`,
  decision ∈ `continue | escalate | refuse` ; sur `continue`, invalide d'office la cascade
  (detail gagne la cle `invalidated`) ;
- `invalidate(battle, reason, now_iso) -> (ok, reason, battle, detail)` : remet a `pending`
  les gates de cascade `done`/`blocked` (les FAIL sont conserves), trace dans
  `run.invalidations` ; `reason == "polish"` = ronde de polissage, une seule fois, hors budget ;
- `fail_identity(item) -> str`, `normalize(battle)`, `validate(battle) -> (errors, warnings)`,
  `derive_required_phases(battle)` ;
- `cascade_missing(battle) -> [{phase, status}]` (#88) : phases requises non `done`, dans l'ordre
  de `required_gates` (phase absente : statut `absente`) ; `deliver` et `address done` l'exigent
  vide (sortie `check-cascade`) ;
- `build` (`in_progress`, `done` et `blocked`) exige un plan `done`/`accept*` approuvé (#86) ;
  la cascade (`in_progress`/`done`) exige aussi ce plan approuvé, ou la voie legacy (GH#97) ;
- état par slice (GH#70), champ racine optionnel `slices: [{id, status, warnings?, files?}]`
  (absent ou vide = BUILD agrégé, comportement legacy) :
  `set_slices(battle, ids, replace=False) -> (ok, reason, battle, detail)` (remplacement tant que
  `build` est `pending` ; ajout seul en `in_progress`/`blocked` ; refus en `done` ; `replace=True`
  (#90) : remplacement aussi pendant un re-plan ouvert, `plan.approved_at` à `null`, les ids
  conservés gardent leur entrée, `detail.removed` liste les retirés, `build done` repasse à
  `blocked` si une slice du résultat n'est pas `done` ; `replace=True` sans id (GH#93) vide la
  liste = retour au BUILD agrégé, mêmes conditions),
  `security_hits(files) -> list[str]` (fichiers sensibles, sans doublon, ordre reçu) et
  `mark_security_auto(battle, files, now_iso) -> (battle, hits_added)` (copie ; ajoute `security`
  en fin de `required_gates` + `run.security_auto`, idempotent),
  `update_slice(battle, slice_id, status, warnings=None, files=None) -> (ok, reason, battle)`
  (`build` doit être `in_progress`), `next_slice(battle) -> {id, status} | None` (première
  slice non `done`). `build done` exige toutes les slices `done` ; un verdict de gate de cascade
  écrit `phases.<p>.covers` (ids des slices `done`), remis à `null` avec le verdict.

Usage (options globales `--battle <id>` défaut : pointeur `.legion/active-battle`, et
`--repo <path>` défaut : cwd) :
    python battle_state.py init <id> --ticket T --title T --profile P [--step] [--required-gates g…]
    python battle_state.py transition <phase> <status> [--verdict v] [--fails json]
                                      [--round n] [--threads json]
    python battle_state.py approve-plan | close | validate
    python battle_state.py bump-autocorrect <phase> (--fails <json> | --build-failure)
    python battle_state.py invalidate [--reason R]     # defaut R = manual
    python battle_state.py set-delivery --pr-url <url>
    python battle_state.py set-guard [--allow [g…]] [--deny [g…]] [--careful on|off]
    python battle_state.py set-meta [--title] [--profile] [--required-gates …] [--stack-kind]
                                    [--build-target] [--test-target]
    python battle_state.py set-slices <id> [<id>…] | set-slices --replace [<id>…]
    python battle_state.py slice <id> <in_progress|done|blocked> [--warnings N] [--files [f…]]
    python battle_state.py next-slice                  # lecture seule (ni écriture ni synchro)
    python battle_state.py check-cascade               # lecture seule ; exit 2 = cascade incomplète
    python battle_state.py activate <id>
    python battle_state.py --self-test   # tests hermétiques, sort 0 offline

Sortie : un objet JSON sur stdout (`ok`, `reason` si refus). Écriture atomique ; après chaque
mutation, le shard fleet est réécrit (échec = `warnings`, jamais bloquant).

Codes de sortie : 0 = ok (dont `bump-autocorrect` escalate), 1 = self-test en échec,
2 = usage invalide / refus (dont `check-cascade` : cascade incomplète).
"""

from __future__ import annotations

import argparse
import copy
import fnmatch
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
DEFAULT_PROFILE = "feature"
PROFILES: dict[str, tuple[str, ...]] = {
    "feature": DEFAULT_REQUIRED_GATES,
    "hotfix": ("lint", "reviewer", "test-engineer"),
    "security": ("architect", "lint", "reviewer", "test-engineer", "security"),
    "spike": ("architect",),
}

SECURITY_DEP_NAMES: frozenset[str] = frozenset({
    "directory.packages.props", "packages.lock.json", "package.json", "package-lock.json",
    "pyproject.toml", "go.mod", "cargo.toml",
    "yarn.lock", "pnpm-lock.yaml", "go.sum", "cargo.lock", "poetry.lock", "uv.lock",
    "pipfile", "pipfile.lock", "nuget.config",
    "program.cs", "startup.cs",
})
SECURITY_NAME_GLOBS: tuple[str, ...] = (
    "*.csproj", "requirements*.txt", "appsettings*.json", "*.env", ".env*",
)
SECURITY_EXTENSIONS: tuple[str, ...] = (".pem", ".key", ".pfx", ".p12")
SECURITY_WORDS: frozenset[str] = frozenset({
    "auth", "authn", "authz", "authentication", "authorization", "authorize", "oauth",
    "token", "tokens",
})
SECURITY_SUBSTRINGS: tuple[str, ...] = (
    "secret", "credential", "password", "passwd", "endpoint", "controller",
)
_WORD_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

CAP_PER_PHASE = 2
CAP_TOTAL = 6

# Sous-commandes du CLI, dans l'ordre du parser (`_build_parser_parts`) ; `_t_subcommands_constant`
# verrouille l'égalité et `_t_doc_subcommands` compare la doctrine à cette constante.
SUBCOMMANDS: tuple[str, ...] = ("init", "transition", "approve-plan", "bump-autocorrect",
                                "invalidate", "set-delivery", "set-guard", "set-meta",
                                "set-slices", "slice", "next-slice", "check-cascade",
                                "activate", "close", "abort", "validate")

# Commandes encore permises sur une battle abandonnée (GH#75) : lecture de diagnostic seule.
ABORT_ALLOWED: tuple[str, ...] = ("validate",)

# Motif des identifiants (battle et slice) : liste blanche, aucun séparateur de chemin.
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")


# --- Helpers de lecture (tolerants au schema legacy) -------------------------------------

def _phase_entry(battle: dict, phase: str) -> dict:
    phases = battle.get("phases")
    entry = phases.get(phase) if isinstance(phases, dict) else None
    return entry if isinstance(entry, dict) else {}


def _status(battle: dict, phase: str) -> str:
    return _phase_entry(battle, phase).get("status", "pending")


def _slices(battle: dict) -> list[dict]:
    """Entrées `slices` de type objet ; `[]` si le champ est absent ou n'est pas une liste."""
    raw = battle.get("slices") if isinstance(battle, dict) else None
    return [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []


def _done_ids(battle: dict) -> list[str]:
    return [s.get("id") for s in _slices(battle) if s.get("status") == "done"]


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


def cascade_missing(battle: dict) -> list[dict]:
    """Phases requises (`derive_required_phases`) qui ne sont pas `done`, dans l'ordre de
    `required_gates`, sous la forme {phase, status} (phase absente : statut "absente"). Pur."""
    out: list[dict] = []
    for p in derive_required_phases(battle):
        st = _phase_entry(battle, p).get("status", "absente")
        if st != "done":
            out.append({"phase": p, "status": st})
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

def _plan_approved(battle: dict) -> bool:
    """Le plan est-il approuvé ? `approved_at` renseigné, ou voie legacy : battle antérieure au
    champ (clé `approved_at` absente) dont `build` est déjà sorti de `pending`. Un re-plan laisse
    la clé à null : nouvelle approbation obligatoire. Règle partagée par BUILD et la cascade."""
    plan = _phase_entry(battle, "plan")
    if plan.get("approved_at"):
        return True
    return "approved_at" not in plan and _status(battle, "build") != "pending"


def is_aborted(battle) -> bool:
    """Vrai si la battle est abandonnée (GH#75) : clé `aborted` présente et non `null`. Pur.
    Une valeur malformée compte aussi comme abandonnée : on bloque plutôt que de laisser passer."""
    return isinstance(battle, dict) and battle.get("aborted") is not None


_ABORTED_REASON = "battle abandonnée : plus aucune transition (seul `validate` passe)"


def aborted_refusal(battle, cmd: str) -> str | None:
    """Raison du refus si la battle est abandonnée et que `cmd` n'est pas dans `ABORT_ALLOWED`,
    sinon None. Pur."""
    if is_aborted(battle) and cmd not in ABORT_ALLOWED:
        return f"{_ABORTED_REASON} ; commande refusée : {cmd}"
    return None


def abort_battle(battle: dict, reason, now_iso: str) -> tuple[bool, str, dict]:
    """Abandonne la battle (GH#75). Pur, même forme que `approve_plan`. Refus si la battle est
    close (`reflect` done) ou déjà abandonnée. `reason` absent ou vide -> `null`."""
    if is_aborted(battle):
        return False, "battle déjà abandonnée", battle
    if _status(battle, "reflect") == "done":
        return False, "abort refusé : la battle est close (reflect done)", battle
    out = copy.deepcopy(battle)
    out["aborted"] = {"at": now_iso, "reason": reason or None}
    return True, "", out


def check_transition(battle: dict, phase: str, status: str, verdict: str | None = None,
                     fails=None, round_=None, threads=None) -> tuple[bool, str]:
    """Précondition d'une transition de phase. Pur. Retourne (ok, raison du refus)."""
    if is_aborted(battle):
        return False, _ABORTED_REASON
    if phase not in PHASES:
        return False, f"phase inconnue : {phase!r}"
    if status not in STATUSES:
        return False, f"statut inconnu : {status!r}"
    if status == "pending":
        return False, "retour à 'pending' refusé (pas de retour arrière) ; utiliser `invalidate`"
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

    # build : l'approbation du plan vaut pour toute sortie de `pending`, `blocked` compris (#86)
    if phase == "build":
        plan = _phase_entry(battle, "plan")
        if plan.get("status") != "done" or not _is_accept(plan.get("verdict")):
            return False, "build exige un plan done avec verdict accept*"
        if not _plan_approved(battle):
            return False, "plan non approuvé (approve-plan requis avant build)"

    if status == "blocked":
        return True, ""

    # preconditions d'entree (in_progress | done)
    if phase == "plan":
        if _status(battle, "think") != "done":
            return False, "plan exige think done"
    elif phase == "build":
        if status == "done":
            todo = [str(s.get("id")) for s in _slices(battle) if s.get("status") != "done"]
            if todo:
                return False, f"build done exige toutes les slices done (restantes : {', '.join(todo)})"
    elif phase in CASCADE_PHASES:
        if _status(battle, "build") != "done":
            return False, f"{phase} exige build done"
        if not _plan_approved(battle):
            return False, "plan non approuvé (approve-plan requis avant la cascade)"
    elif phase == "deliver":
        missing = cascade_missing(battle)
        if missing:
            return False, f"deliver exige la phase {missing[0]['phase']} done (statut : {missing[0]['status']})"
    elif phase == "address":
        delivery = battle.get("delivery")
        if not (isinstance(delivery, dict) and delivery.get("pr_url")):
            return False, "address exige delivery.pr_url"
        if status == "done":  # #88 : ne pas clore un fil ADDRESS sur une cascade à rejouer
            missing = cascade_missing(battle)
            if missing:
                return False, (f"address done exige la phase {missing[0]['phase']} done "
                               f"(statut : {missing[0]['status']})")
    return True, ""


def apply_transition(battle: dict, phase: str, status: str, verdict: str | None = None,
                     fails=None, round_=None, threads=None, now_iso=None) -> dict:
    """Applique la transition sur une copie. Lève ValueError si `check_transition` refuse.

    `plan in_progress` (re-plan, GH#93) invalide la cascade `done`/`blocked`/`in_progress` (raison `replan`,
    `now_iso` horodate l'événement) ; sans cascade rendue, rien n'est écrit."""
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
        if "covers" in entry:
            entry["covers"] = None  # covers suit verdict
    if verdict is not None and phase in CASCADE_PHASES and _slices(battle):
        entry["covers"] = _done_ids(battle)
    if phase == "plan" and status == "in_progress":
        entry["approved_at"] = None  # clé présente à null : nouvelle approbation requise, pas de legacy
        _invalidate_cascade(out, "replan", now_iso)  # les verdicts rendus sur l'ancien plan sont caducs
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


# --- Etat par slice (GH#70) --------------------------------------------------------------

def set_slices(battle: dict, ids, replace: bool = False,
               now_iso=None) -> tuple[bool, str, dict, dict]:
    """Déclare les slices du BUILD. Pur. Retourne (ok, raison du refus, battle, detail).

    `build` pending : remplacement complet (idempotent), toutes les slices `pending`.
    `build` in_progress/blocked : ajout seul (la liste doit contenir tous les ids existants ;
    les statuts existants sont conservés, les nouveaux ids arrivent en `pending`).
    `build` done : refus (préserve « build done => toutes les slices done »).

    `replace=True` (re-découpage, #90) : autorisé si `build` est `pending`, ou si un re-plan
    est ouvert (`plan.approved_at` présent et à `None`) ; sinon refus. La nouvelle liste suit
    l'ordre de `ids` ; un id conservé garde son entrée entière, un nouvel id arrive en
    `pending`, un id absent est retiré (`detail["removed"]`). Si `build` est `done` et qu'une
    slice du résultat n'est pas `done`, `build` repasse à `blocked` (l'invariant tient) et la cascade est
    invalidée (raison `replan`, `detail["invalidated"]`) : elle doit être rejouée.

    `replace=True` sans id (GH#93) vide la liste (retour au BUILD agrégé), sous les mêmes
    conditions ; sans `replace`, une liste vide reste refusée."""
    if not isinstance(ids, (list, tuple)) or (not ids and not replace):
        return False, "set-slices exige au moins un id de slice", battle, {}
    for sid in ids:
        if not isinstance(sid, str) or not _ID_RE.fullmatch(sid):
            return False, f"identifiant de slice invalide : {sid!r} (attendu : [A-Za-z0-9-])", battle, {}
    if len(set(ids)) != len(ids):
        dup = sorted({i for i in ids if list(ids).count(i) > 1})
        return False, f"identifiant(s) de slice en double : {', '.join(dup)}", battle, {}
    st = _status(battle, "build")
    existing = _slices(battle)
    if replace:
        plan = _phase_entry(battle, "plan")
        replan_open = "approved_at" in plan and plan["approved_at"] is None
        if st != "pending" and not replan_open:
            return False, ("set-slices --replace refusé : ni build pending ni re-plan ouvert "
                           "(plan.approved_at à null)"), battle, {}
        kept = {s.get("id"): s for s in existing}
        new = [copy.deepcopy(kept[i]) if i in kept else {"id": i, "status": "pending"}
               for i in ids]
        removed = [str(s.get("id")) for s in existing if s.get("id") not in ids]
        out = copy.deepcopy(battle)
        out["slices"] = new
        detail = {"slices": copy.deepcopy(new), "removed": removed}
        if st == "done" and any(s.get("status") != "done" for s in new):
            out["phases"]["build"]["status"] = "blocked"
            detail["invalidated"] = _invalidate_cascade(out, "replan", now_iso)
        return True, "", out, detail
    if st == "done":
        return False, "set-slices refusé : build est done", battle, {}
    if st == "pending":
        new = [{"id": i, "status": "pending"} for i in ids]
    else:
        missing = [str(s.get("id")) for s in existing if s.get("id") not in ids]
        if missing:
            return False, (f"set-slices refusé : build {st}, seul l'ajout est permis "
                           f"(slices retirées : {', '.join(missing)})"), battle, {}
        known = {s.get("id") for s in existing}
        new = copy.deepcopy(existing) + [{"id": i, "status": "pending"} for i in ids if i not in known]
    out = copy.deepcopy(battle)
    out["slices"] = new
    return True, "", out, {"slices": copy.deepcopy(new)}


def update_slice(battle: dict, slice_id: str, status: str, warnings=None,
                 files=None) -> tuple[bool, str, dict]:
    """Met à jour le statut d'une slice (pas de monotonie imposée). Pur. `build` doit être
    `in_progress`, la slice déclarée, `status` ∈ in_progress|done|blocked."""
    if status not in STATUSES or status == "pending":
        return False, f"statut de slice invalide : {status!r} (attendu : in_progress, done, blocked)", battle
    if _status(battle, "build") != "in_progress":
        return False, f"slice exige build in_progress (statut : {_status(battle, 'build')})", battle
    if not _slices(battle):
        return False, "aucune slice déclarée (set-slices requis)", battle
    if warnings is not None and (isinstance(warnings, bool) or not isinstance(warnings, int)
                                 or warnings < 0):
        return False, f"warnings doit être un entier >= 0 (reçu : {warnings!r})", battle
    if files is not None and (not isinstance(files, list)
                              or not all(isinstance(f, str) for f in files)):
        return False, "files doit être une liste de chemins", battle
    out = copy.deepcopy(battle)
    for entry in out["slices"]:
        if isinstance(entry, dict) and entry.get("id") == slice_id:
            entry["status"] = status
            if warnings is not None:
                entry["warnings"] = warnings
            if files is not None:
                entry["files"] = list(files)
            return True, "", out
    return False, f"slice inconnue : {slice_id!r}", battle


def _security_words(segment: str) -> list[str]:
    return [w.lower() for w in _WORD_SPLIT.split(segment) if w]


def _is_sensitive(path: str) -> bool:
    segments = [seg for seg in path.replace("\\", "/").removeprefix("./").split("/") if seg]
    if not segments:
        return False
    name = segments[-1].lower()
    if name in SECURITY_DEP_NAMES or name.endswith(SECURITY_EXTENSIONS):
        return True
    if any(fnmatch.fnmatchcase(name, g) for g in SECURITY_NAME_GLOBS):
        return True
    for seg in segments:
        if SECURITY_WORDS.intersection(_security_words(seg)):
            return True
        low = seg.lower()
        if any(sub in low for sub in SECURITY_SUBSTRINGS):
            return True
    return False


def security_hits(files) -> list[str]:
    """Fichiers sensibles parmi `files` (dépendances, config, clés, auth, jeton, secrets,
    endpoints), sans doublon, dans l'ordre reçu. Pur ; ignore ce qui n'est pas une chaîne."""
    hits: list[str] = []
    for f in files or []:
        if isinstance(f, str) and f not in hits and _is_sensitive(f):
            hits.append(f)
    return hits


def mark_security_auto(battle: dict, files, now_iso: str) -> tuple[dict, list[str]]:
    """Ajoute `security` en fin de `required_gates` si `files` contient un fichier sensible et
    que la gate n'y est pas déjà ; trace `run.security_auto = {at, files}`. Pur (copie).
    Idempotent : gate déjà présente ou aucun fichier sensible => (battle, [])."""
    hits = security_hits(files)
    gates = _required_gates(battle)
    if not hits or "security" in gates:
        return battle, []
    out = copy.deepcopy(battle)
    out["required_gates"] = gates + ["security"]
    if not isinstance(out.get("run"), dict):
        out["run"] = {}
    out["run"]["security_auto"] = {"at": now_iso, "files": hits}
    return out, hits


def next_slice(battle: dict) -> dict | None:
    """Première slice non `done`, dans l'ordre déclaré, sous la forme {id, status} ; `None` si
    toutes sont `done` ou si aucune n'est déclarée. Pur."""
    for s in _slices(battle):
        if s.get("status") != "done":
            return {"id": s.get("id"), "status": s.get("status", "pending")}
    return None


# --- Invalidation de la cascade ----------------------------------------------------------

def _invalidate_cascade(out: dict, reason: str, now_iso) -> list[str]:
    """Remet a `pending` chaque phase de cascade PRESENTE `done`/`blocked` (et `in_progress` si
    `reason == "replan"`, GH#97) (verdict -> null,
    `fails` conserves, `invalidated_at` pose). Mute `out` (copie deja faite). Trace un
    evenement dans `run.invalidations` seulement si une phase a change. Retourne les phases."""
    phases = out.get("phases")
    changed: list[str] = []
    if isinstance(phases, dict):
        targets = ("done", "blocked", "in_progress") if reason == "replan" else ("done", "blocked")
        for p in CASCADE_PHASES:
            entry = phases.get(p)
            if isinstance(entry, dict) and entry.get("status") in targets:
                entry["status"] = "pending"
                entry["verdict"] = None
                if "covers" in entry:
                    entry["covers"] = None
                entry["invalidated_at"] = now_iso
                changed.append(p)
    if changed:
        out.setdefault("run", {}).setdefault("invalidations", []).append(
            {"at": now_iso, "reason": reason, "phases": list(changed)})
    return changed


def invalidate(battle: dict, reason: str, now_iso) -> tuple[bool, str, dict, dict]:
    """Invalide les gates de cascade deja acceptees/bloquees apres une correction. Pur.

    Retourne (ok, raison du refus, battle, detail) ; detail = {"invalidated": [phases]}.
    `reason == "polish"` (ronde de polissage) : refusee si deja faite, si une gate requise
    n'est pas `done` (DELIVER pas pret) ou si `deliver` n'est pas `pending`. Ne touche jamais
    `run.autocorrect` (hors budget 2/6)."""
    if not isinstance(reason, str) or not reason.strip():
        return False, "raison d'invalidation vide", battle, {}
    run = battle.get("run") if isinstance(battle.get("run"), dict) else {}
    if reason == "polish":
        prior = run.get("invalidations")
        if isinstance(prior, list) and any(isinstance(e, dict) and e.get("reason") == "polish"
                                           for e in prior):
            return False, "ronde de polissage déjà effectuée (une seule autorisée)", battle, {}
        ok, why = check_transition(battle, "deliver", "in_progress")
        if not ok:
            return False, f"polish exige DELIVER prêt : {why}", battle, {}
        if _status(battle, "deliver") != "pending":
            return False, "polish exige deliver pending (avant DELIVER)", battle, {}
    out = copy.deepcopy(battle)
    changed = _invalidate_cascade(out, reason, now_iso)
    return True, "", out, {"invalidated": changed}


# --- Auto-correction ---------------------------------------------------------------------

def bump_autocorrect(battle: dict, phase: str, fails, keep_fails: bool = False,
                     now_iso=None) -> tuple[str, str, dict, dict]:
    """Décide continue / escalate pour une ronde d'auto-correction. Pur.

    Retourne (decision, reason, battle, detail). `refuse` = clé invalide (battle inchangée).
    `escalate` n'incrémente pas les compteurs mais enregistre les FAIL. `keep_fails=True` (échec
    de build survenu pendant la correction d'une gate) compte la tentative sous la clé mais
    laisse `phases.<clé>.fails` intacts et saute le contrôle de non-progrès. detail =
    {per_phase, total, resolved, persisting, new} (+ `invalidated` sur `continue` : la cascade
    est invalidee d'office, raison `autocorrect:<phase>`, cf. `invalidate`).
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
    invalidated = _invalidate_cascade(out, f"autocorrect:{phase}", now_iso) \
        if decision == "continue" else None
    detail = {
        "per_phase": per_gate.get(phase, per),
        "total": ac["total"],
        "resolved": resolved,
        "persisting": persisting,
        "new": added,
    }
    if invalidated is not None:
        detail["invalidated"] = invalidated
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


def _guard_problem(guard) -> str | None:
    """Diagnostic de forme du bloc `guard` (None = valide). Pur, ne lève jamais.

    Valide : `None` (bloc null = absent, non armé) ou dict dont `allow` et `deny` sont
    absents, null ou une liste de `str`.
    Source unique de la règle, partagée par `guard_of` et `validate` (GH#104).
    """
    if guard is None:
        return None
    if not isinstance(guard, dict):
        return f"guard n'est pas un objet ({type(guard).__name__})"
    for key in ("allow", "deny"):
        val = guard.get(key)
        if val is None:
            continue
        if not isinstance(val, list):
            return f"guard.{key} n'est pas une liste ({type(val).__name__})"
        if not all(isinstance(x, str) for x in val):
            return f"guard.{key} contient un élément qui n'est pas une chaîne"
    return None


def guard_of(battle) -> tuple[dict | None, bool]:
    """Bloc `guard` d'une battle et sa validité : `(guard, True)` ou `(None, False)`.

    `guard` absent ou null (ou battle non-dict) -> `({}, True)` : bloc non armé.
    Pur, ne lève jamais (GH#104).
    """
    if not isinstance(battle, dict) or battle.get("guard") is None:
        return {}, True
    guard = battle["guard"]
    if _guard_problem(guard) is not None:
        return None, False
    return guard, True


def validate(battle) -> tuple[list[str], list[str]]:
    """Valide la structure. Champs inconnus tolérés. Retourne (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(battle, dict):
        return ["battle.json n'est pas un objet"], warnings
    if not battle.get("id"):
        warnings.append("champ id absent")
    phases = battle.get("phases")
    if "profile" in battle:
        prof = battle["profile"]
        if not isinstance(prof, str) or prof not in PROFILES:
            warnings.append(f"profil inconnu : {prof!r} (attendu : {', '.join(PROFILES)})")
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
        covers = entry.get("covers")
        if covers is not None and not isinstance(covers, list):
            warnings.append(f"phases.{name}.covers n'est ni une liste ni null")
        st = entry.get("status")
        if st not in STATUSES:
            errors.append(f"phases.{name}.status invalide : {st!r}")
        v = entry.get("verdict")
        if v is not None and v not in VERDICTS:
            errors.append(f"phases.{name}.verdict invalide : {v!r}")
    if "slices" in battle:
        sl = battle["slices"]
        if not isinstance(sl, list):
            errors.append("slices n'est pas une liste")
        else:
            seen: set = set()
            for i, s in enumerate(sl):
                if not isinstance(s, dict):
                    errors.append(f"slices[{i}] n'est pas un objet")
                    continue
                sid = s.get("id")
                if not isinstance(sid, str) or not _ID_RE.fullmatch(sid):
                    errors.append(f"slices[{i}].id invalide : {sid!r}")
                elif sid in seen:
                    errors.append(f"slices[{i}].id en double : {sid!r}")
                else:
                    seen.add(sid)
                if s.get("status") not in STATUSES:
                    errors.append(f"slices[{i}].status invalide : {s.get('status')!r}")
    if "guard" in battle:
        problem = _guard_problem(battle["guard"])
        if problem:
            errors.append(problem)
    ab = battle.get("aborted")
    if ab is not None and not (isinstance(ab, dict) and ab.get("at")):
        warnings.append("aborted n'est pas un objet avec `at`")
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


def battles_dir(root: Path) -> Path:
    return root / ".legion" / "battles"


def _pointer_path(root: Path) -> Path:
    return root / ".legion" / "active-battle"


def _battle_dir(root: Path, battle_id: str) -> Path:
    if not isinstance(battle_id, str) or not _ID_RE.fullmatch(battle_id):
        raise _Refuse(f"identifiant de battle invalide : {battle_id!r} (attendu : [A-Za-z0-9-])")
    base = battles_dir(root)
    target = base / battle_id
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError:
        raise _Refuse(f"identifiant de battle hors de .legion/battles/ : {battle_id!r}")
    return target


def _read_pointer(root: Path) -> str:
    try:
        return _pointer_path(root).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):   # ValueError : UnicodeDecodeError sur un pointeur non UTF-8
        return ""


def active_battle_id(repo_root: Path) -> str | None:
    """Id de la battle active (pointeur `.legion/active-battle`), ou `None`.

    Lecture seule, ne lève jamais : `None` si le pointeur est absent, vide, blanc, illisible
    ou si l'id ne passe pas la liste blanche `_ID_RE`. Ne lit pas `battle.json`.
    """
    try:
        value = _read_pointer(repo_root)
    except Exception:  # noqa: BLE001 - contrat : jamais d'exception
        return None
    if not value or not _ID_RE.fullmatch(value):
        return None
    return value


def load_active_battle(repo_root: Path) -> tuple[str, dict] | None:
    """`(id, battle.json brut)` de la battle active, ou `None`.

    Lecture seule, ne lève jamais : `None` si pas de battle active, dossier hors de
    `.legion/battles/`, `battle.json` absent/illisible/invalide ou racine non-dict.
    Renvoie le dict brut (sans `normalize`).
    """
    battle_id = active_battle_id(repo_root)
    if battle_id is None:
        return None
    try:
        path = _battle_dir(repo_root, battle_id) / "battle.json"
        data = json.loads(path.read_text(encoding="utf-8"))
    except (_Refuse, OSError, ValueError, RecursionError):  # JSON trop imbrique = illisible
        return None
    if not isinstance(data, dict):
        return None
    return battle_id, data


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
    except (OSError, ValueError, RecursionError) as exc:
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


def _build_parser_parts():
    """Construit le parser ; retourne (parser, action des sous-parsers). `action.choices` (API
    publique d'argparse) donne les sous-commandes dans l'ordre de déclaration."""
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
    s = add("invalidate")
    s.add_argument("--reason", default="manual")
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
    s = add("set-slices")
    s.add_argument("ids", nargs="*")  # le refus « au moins un id » vit dans le cœur (--replace : vide permis)
    s.add_argument("--replace", action="store_true",
                   help="re-découpage : remplace la liste (build pending ou re-plan ouvert)")
    s = add("slice")
    s.add_argument("id")
    s.add_argument("status", choices=("in_progress", "done", "blocked"))
    s.add_argument("--warnings", type=int, default=None)
    s.add_argument("--files", nargs="*", default=None)
    add("next-slice")
    add("check-cascade")
    s = add("activate")
    s.add_argument("id")
    add("close")
    s = add("abort")
    s.add_argument("--reason", default=None)
    add("validate")
    return p, sub


def _build_parser() -> argparse.ArgumentParser:
    return _build_parser_parts()[0]


def _check_profile(profile) -> None:
    if profile not in PROFILES:
        raise _Refuse(f"profil inconnu : {profile!r} (attendu : {', '.join(PROFILES)})")


def _new_battle(root: Path, args) -> dict:
    phases = {"think": {"status": "in_progress"}}
    for name in PHASES[1:]:
        if name not in ("security", "address"):
            phases[name] = {"status": "pending"}
    return {
        "id": args.id, "repo": root.name, "ticket": args.ticket, "title": args.title,
        "profile": args.profile,
        "required_gates": list(args.required_gates or PROFILES[args.profile]),
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
                               args.round_, threads, now_iso=_now_iso())
        extra = {"phase": args.phase, "status": args.status}
        if args.phase == "plan" and args.status == "in_progress":
            before = (battle.get("run") or {}).get("invalidations") or []
            after = (out.get("run") or {}).get("invalidations") or []
            if len(after) > len(before):
                extra["invalidated"] = list(after[-1].get("phases", []))
        return out, extra
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
                                                         keep_fails=args.build_failure,
                                                         now_iso=_now_iso())
        if decision == "refuse":
            raise _Refuse(reason, **detail)
        return out, {"decision": decision, "reason": reason, **detail}
    if cmd == "invalidate":
        ok, reason, out, detail = invalidate(battle, args.reason, _now_iso())
        if not ok:
            raise _Refuse(reason)
        return out, {"reason": args.reason, **detail}
    if cmd == "set-slices":
        ok, reason, out, detail = set_slices(battle, args.ids, replace=args.replace,
                                                  now_iso=_now_iso())
        if not ok:
            raise _Refuse(reason)
        return out, detail
    if cmd == "slice":
        ok, reason, out = update_slice(battle, args.id, args.status, args.warnings, args.files)
        if not ok:
            raise _Refuse(reason)
        entry = next(s for s in out["slices"] if s.get("id") == args.id)
        detail = {"slice": copy.deepcopy(entry), "slices_done": len(_done_ids(out)),
                  "slices_total": len(_slices(out))}
        if args.status == "done" and args.files:
            out, added = mark_security_auto(out, args.files, _now_iso())
            if added:
                detail["security_auto"] = copy.deepcopy(out["run"]["security_auto"])
                detail["_warnings"] = [
                    f"security ajoutée à required_gates (fichiers sensibles : {', '.join(added)})"]
        return out, detail
    out = copy.deepcopy(battle)
    if cmd == "set-delivery":
        out.setdefault("delivery", {})["pr_url"] = args.pr_url
        return out, {"pr_url": args.pr_url}
    if cmd == "set-guard":
        if args.allow is None and args.deny is None and args.careful is None:
            raise _Refuse("set-guard : aucune option (--allow, --deny ou --careful)")
        repaired = not isinstance(out.get("guard", {}), dict)
        if repaired:   # GH#104 : bloc non-dict remplacé avant d'appliquer les options
            out["guard"] = {"allow": [], "deny": [], "careful": False}
        guard = out.setdefault("guard", {})
        if args.allow is not None:
            guard["allow"] = list(args.allow)
        if args.deny is not None:
            guard["deny"] = list(args.deny)
        if args.careful is not None:
            guard["careful"] = args.careful == "on"
        detail = {"guard": guard}
        if repaired:
            detail["repaired"] = True
        return out, detail
    if cmd == "set-meta":
        touched = []
        if args.profile is not None:
            _check_profile(args.profile)
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
    if cmd == "abort":
        ok, reason, out = abort_battle(battle, args.reason, _now_iso())
        if not ok:
            raise _Refuse(reason)
        return out, {"aborted": out["aborted"]}
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
        mutation_warnings: list[str] = []

        if args.cmd == "init":
            bdir = _battle_dir(root, args.id)
            if (bdir / "battle.json").exists():
                raise _Refuse(f"battle déjà existante : {args.id}")
            unknown = [g for g in (args.required_gates or []) if g not in GATES]
            if unknown:
                raise _Refuse(f"gate(s) inconnue(s) : {', '.join(unknown)}")
            _check_profile(args.profile)
            bdir.mkdir(parents=True, exist_ok=True)
            _save(bdir, _new_battle(root, args))
            _write_pointer(root, args.id)
            init_gates = _load(bdir)["required_gates"]
            result.update(battle=args.id, profile=args.profile, required_gates=init_gates)
        elif args.cmd == "activate":
            bdir = _battle_dir(root, args.id)
            refusal = aborted_refusal(_load(bdir), "activate")
            if refusal:
                raise _Refuse(refusal)
            _write_pointer(root, args.id)
            return 0, {"ok": True, "battle": args.id, "active": args.id}
        else:
            bid = explicit or _read_pointer(root)
            if not bid:
                raise _Refuse("aucune battle active (utiliser --battle <id> ou `activate`)")
            bdir = _battle_dir(root, bid)
            battle = _load(bdir)
            refusal = aborted_refusal(battle, args.cmd)
            if refusal:
                raise _Refuse(refusal)
            if args.cmd == "validate":
                errors, warnings = validate(battle)
                return (0 if not errors else 2), {"ok": not errors, "battle": bid,
                                                  "errors": errors, "warnings": warnings}
            if args.cmd == "next-slice":  # lecture seule : ni _save ni _sync_fleet
                return 0, {"ok": True, "battle": bid, "slice": next_slice(battle),
                           "slices_done": len(_done_ids(battle)),
                           "slices_total": len(_slices(battle))}
            if args.cmd == "check-cascade":  # lecture seule : ni _save ni _sync_fleet
                missing = cascade_missing(battle)
                if missing:
                    return 2, {"ok": False, "battle": bid,
                               "reason": "cascade incomplète : "
                                         + ", ".join(f"{m['phase']} ({m['status']})" for m in missing),
                               "missing": missing}
                return 0, {"ok": True, "battle": bid, "required": derive_required_phases(battle)}
            new, extra = _mutation(args, battle)
            _save(bdir, new)
            mutation_warnings = extra.pop("_warnings", [])
            result.update(battle=bid, **extra)
            if args.cmd in ("close", "abort") and _read_pointer(root) == bid:
                _write_pointer(root, "")
                result["pointer_cleared"] = True

        result["warnings"] = mutation_warnings + _sync_fleet(bdir, root, upsert, fleet_dir)
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
    assert set(PROFILES) == {"feature", "hotfix", "security", "spike"}
    assert all(g in GATES for gates in PROFILES.values() for g in gates)
    assert PROFILES["feature"] == DEFAULT_REQUIRED_GATES and DEFAULT_PROFILE in PROFILES
    assert "security" in PROFILES["security"] and "architect" not in PROFILES["hotfix"]
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
    assert "plan" in _refused(b, "build", "blocked")   # #86 : blocked exige aussi un plan accept*
    b["phases"]["plan"] = {"status": "done", "verdict": "accept", "approved_at": "T"}
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
    b["phases"].update({p: {"status": "done", "verdict": "accept"}
                        for p in ("plan", "lint", "review", "test")})   # cascade done (#88)
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


def _casc(**st):
    """Fixture cascade : build done + statuts donnes (phase=(statut, verdict) | statut)."""
    return _fx({"build": "done", **st})


def _t_invalidate_cascade() -> None:
    b = _casc(plan=("done", "accept"), lint=("done", "accept"), test="pending",
              deliver="pending")
    b["phases"]["review"] = {"status": "blocked", "verdict": "revise",
                             "fails": [{"target": "A", "dimension": "R1"}]}
    snap = copy.deepcopy(b)
    ok, _, out, det = invalidate(b, "manual", "T1")
    assert ok and det == {"invalidated": ["lint", "review"]}, det
    assert b == snap  # entree non mutee
    for p in ("lint", "review"):
        e = out["phases"][p]
        assert e["status"] == "pending" and e["verdict"] is None and e["invalidated_at"] == "T1"
    assert out["phases"]["review"]["fails"] == [{"target": "A", "dimension": "R1"}]
    assert out["phases"]["test"] == {"status": "pending"} and "security" not in out["phases"]
    for p in ("plan", "build", "deliver"):
        assert out["phases"][p] == b["phases"][p], p
    assert out["run"]["invalidations"] == [{"at": "T1", "reason": "manual",
                                            "phases": ["lint", "review"]}]


def _t_invalidate_noop() -> None:
    b = _casc(lint="in_progress", review="pending")
    ok, _, out, det = invalidate(b, "manual", "T1")
    assert ok and det["invalidated"] == [] and "run" not in out, (det, out)
    assert out["phases"] == b["phases"]


def _t_invalidate_empty_reason() -> None:
    b = _casc(lint=("done", "accept"))
    for r in ("", "   ", None):
        ok, reason, same, _ = invalidate(b, r, "T")
        assert not ok and reason and same is b, r


def _t_bump_continue_invalidates() -> None:
    b = _casc(lint=("done", "accept"), review=("done", "accept"), test=("blocked", "revise"))
    for kw in ({}, {"keep_fails": True}):
        d, _, out, det = bump_autocorrect(b, "test", [{"target": "A", "dimension": "R1"}],
                                          now_iso="T", **kw)
        assert d == "continue", (kw, det)
        assert det["invalidated"] == ["lint", "review", "test"], det
        for p in ("lint", "review", "test"):
            assert out["phases"][p]["status"] == "pending", (kw, p)
        assert out["run"]["invalidations"][0]["reason"] == "autocorrect:test"
        assert out["run"]["invalidations"][0]["at"] == "T"


def _t_bump_escalate_no_invalidation() -> None:
    b = _casc(lint=("done", "accept"), review=("blocked", "revise"))
    _, _, b, _ = _bump(b, "review", ["A"])
    b["phases"]["review"]["status"] = "blocked"   # re-passe par la gate, toujours en echec
    b["phases"]["lint"]["status"] = "done"
    before = copy.deepcopy(b["phases"])
    n_events = len(b["run"]["invalidations"])
    d, _, out, det = _bump(b, "review", ["A"])   # non-progres
    assert d == "escalate" and "invalidated" not in det
    assert {p: {k: v for k, v in e.items() if k != "fails"} for p, e in out["phases"].items()} == \
        {p: {k: v for k, v in e.items() if k != "fails"} for p, e in before.items()}
    assert len(out["run"]["invalidations"]) == n_events
    d, _, out, det = _bump(_casc(lint=("done", "accept")), "plan", ["A"])
    assert d == "refuse" and "run" not in out


def _t_deliver_after_invalidation() -> None:
    b = _fx({"build": "done", "plan": ("done", "accept"), "lint": ("done", "accept"),
             "review": ("done", "accept"), "test": ("done", "accept")})
    assert check_transition(b, "deliver", "in_progress")[0]
    _, _, b, _ = invalidate(b, "manual", "T")
    assert "lint" in _refused(b, "deliver", "in_progress")
    for p in ("lint", "review", "test"):
        b = apply_transition(b, p, "in_progress")
        b = apply_transition(b, p, "done", "accept")
    assert check_transition(b, "deliver", "in_progress")[0]


def _t_progress_after_invalidation() -> None:
    b = _casc(review=("blocked", "revise"))
    d, _, b, _ = _bump(b, "review", ["A"])
    assert d == "continue" and b["phases"]["review"]["status"] == "pending"
    assert b["phases"]["review"]["fails"] == [{"target": "A", "dimension": "R1"}]
    b = apply_transition(b, "review", "in_progress")
    b = apply_transition(b, "review", "blocked", "revise")
    d, _, b2, _ = _bump(b, "review", ["A"])
    assert d == "escalate"
    d, _, _, det = _bump(b, "review", ["B"])
    assert d == "continue" and det["resolved"] == ["A|R1"], det


def _t_polish_once() -> None:
    done = {"build": "done", "plan": ("done", "accept"), "lint": ("done", "accept"),
            "review": ("done", "accept"), "test": ("done", "accept"), "deliver": "pending"}
    b = _fx(done, run={"autocorrect": {"per_gate": {"review": 1}, "total": 1}})
    ok, reason, out, det = invalidate(b, "polish", "T")
    assert ok, reason
    assert det["invalidated"] == ["lint", "review", "test"]
    assert out["run"]["autocorrect"] == {"per_gate": {"review": 1}, "total": 1}
    for p in ("lint", "review", "test"):
        out = apply_transition(out, p, "in_progress")
        out = apply_transition(out, p, "done", "accept")
    ok, reason, _, _ = invalidate(out, "polish", "T2")
    assert not ok and "polissage" in reason, reason
    # gate requise non done
    bad = _fx({**done, "test": "pending"})
    ok, reason, _, _ = invalidate(bad, "polish", "T")
    assert not ok and "test" in reason, reason
    # deliver deja entame
    bad = _fx({**done, "deliver": "done"})
    ok, reason, _, _ = invalidate(bad, "polish", "T")
    assert not ok and "pending" in reason, reason
    # une autre raison n'est pas soumise a ces controles
    assert invalidate(bad, "rebase", "T")[0]


def _sl(*pairs, build="in_progress", **extra) -> dict:
    """Fixture slices : pairs = (id, statut) ; build au statut donné."""
    return _fx({"think": "done", "plan": ("done", "accept"), "build": build},
               slices=[{"id": i, "status": s} for i, s in pairs], **extra)


def _t_set_slices_create() -> None:
    b = _fx({"think": "done", "plan": ("done", "accept"), "build": "pending"})
    snap = copy.deepcopy(b)
    ok, reason, b1, det = set_slices(b, ["slice-1", "slice-2"])
    assert ok, reason
    want = [{"id": "slice-1", "status": "pending"}, {"id": "slice-2", "status": "pending"}]
    assert b1["slices"] == want and det == {"slices": want}
    assert b == snap and "slices" not in b   # entrée non mutée
    ok, _, b2, _ = set_slices(b1, ["slice-1"])   # BUILD pas commencé : remplacement
    assert ok and b2["slices"] == [{"id": "slice-1", "status": "pending"}]
    assert len(b1["slices"]) == 2
    ok, reason, same, _ = set_slices(b, [])
    assert not ok and same is b


def _t_set_slices_after_start() -> None:
    for st in ("in_progress", "blocked"):
        b = _sl(("slice-1", "done"), ("slice-2", "pending"), build=st)
        ok, reason, out, _ = set_slices(b, ["slice-1", "slice-2", "slice-3"])
        assert ok, (st, reason)
        assert out["slices"] == [{"id": "slice-1", "status": "done"},
                                 {"id": "slice-2", "status": "pending"},
                                 {"id": "slice-3", "status": "pending"}]
        ok, reason, same, _ = set_slices(b, ["slice-2"])   # retrait
        assert not ok and "slice-1" in reason and same is b
        ok, _, out, _ = set_slices(b, ["slice-1", "slice-2"])   # idempotent
        assert ok and out["slices"] == b["slices"]
    done = _sl(("slice-1", "done"), build="done")
    ok, reason, _, _ = set_slices(done, ["slice-1", "slice-2"])   # C3
    assert not ok and "done" in reason
    ok, reason, _, _ = set_slices(_sl(build="pending"), ["a", "a"])
    assert not ok and "double" in reason


def _t_set_slices_invalid_id() -> None:
    b = _sl(("slice-1", "pending"), build="pending")
    snap = copy.deepcopy(b)
    for bad in ("a/b", "..", "-x", "a b", "a_b", ""):
        ok, reason, same, _ = set_slices(b, ["slice-1", bad])
        assert not ok and "invalide" in reason, (bad, reason)
        assert same is b and b == snap


def _t_slice_update() -> None:
    b = _sl(("slice-1", "pending"), ("slice-2", "pending"), build="pending")
    ok, reason, _ = update_slice(b, "slice-1", "done")
    assert not ok and "build" in reason
    b = _sl(("slice-1", "pending"), ("slice-2", "pending"))
    snap = copy.deepcopy(b)
    ok, reason, out = update_slice(b, "slice-1", "done", warnings=2, files=["a.py"])
    assert ok, reason
    assert out["slices"][0] == {"id": "slice-1", "status": "done", "warnings": 2, "files": ["a.py"]}
    assert out["slices"][1] == {"id": "slice-2", "status": "pending"} and b == snap
    assert not update_slice(b, "slice-9", "done")[0]
    assert not update_slice(b, "slice-1", "pending")[0]
    assert not update_slice(b, "slice-1", "done", warnings=-1)[0]
    assert not update_slice(_fx({"build": "in_progress"}), "slice-1", "done")[0]
    ok, _, back = update_slice(out, "slice-1", "in_progress")   # non monotone
    assert ok and back["slices"][0]["status"] == "in_progress"


def _t_build_done_requires_slices() -> None:
    b = _sl(("slice-1", "done"), ("slice-2", "pending"))
    assert "slice-2" in _refused(b, "build", "done")
    _, _, b = update_slice(b, "slice-2", "done")
    assert check_transition(b, "build", "done")[0]


def _t_build_blocked_with_slice_blocked() -> None:
    b = _sl(("slice-1", "in_progress"))
    b["phases"]["plan"]["approved_at"] = "T"
    _, _, b = update_slice(b, "slice-1", "blocked")
    b = apply_transition(b, "build", "blocked")
    assert b["phases"]["build"]["status"] == "blocked"
    b = apply_transition(b, "build", "in_progress")
    for st in ("in_progress", "done"):
        _, _, b = update_slice(b, "slice-1", st)
    assert check_transition(b, "build", "done")[0]


def _t_covers_on_cascade_verdict() -> None:
    b = _sl(("slice-1", "done"), ("slice-2", "done"), build="done")
    out = apply_transition(b, "review", "in_progress")
    assert "covers" not in out["phases"]["review"]
    out = apply_transition(out, "review", "done", "accept")
    assert out["phases"]["review"]["covers"] == ["slice-1", "slice-2"]
    out2 = apply_transition(b, "review", "blocked", "revise")
    assert out2["phases"]["review"]["covers"] == ["slice-1", "slice-2"]   # C2
    out3 = apply_transition(out, "review", "in_progress")
    assert out3["phases"]["review"]["covers"] is None and out3["phases"]["review"]["verdict"] is None
    ok, _, inv, _ = invalidate(out, "manual", "T")
    assert ok and inv["phases"]["review"]["covers"] is None and inv["slices"] == out["slices"]
    plan = apply_transition(_fx({"think": "done", "plan": "in_progress"}, slices=b["slices"]),
                            "plan", "done", "accept")
    assert "covers" not in plan["phases"]["plan"]


def _t_next_slice() -> None:
    b = _sl(("slice-1", "done"), ("slice-2", "blocked"), ("slice-3", "pending"))
    assert next_slice(b) == {"id": "slice-2", "status": "blocked"}
    assert next_slice(_sl(("slice-1", "done"), ("slice-2", "done"))) is None
    assert next_slice(_fx({"build": "in_progress"})) is None


def _t_slices_legacy_unchanged() -> None:
    b = _fx({"think": "done", "plan": ("done", "accept"), "build": "in_progress"})
    b["phases"]["plan"]["approved_at"] = "T"
    snap = copy.deepcopy(b)
    b = apply_transition(b, "build", "done")
    b = apply_transition(b, "review", "in_progress")
    b = apply_transition(b, "review", "done", "accept")
    assert "covers" not in b["phases"]["review"] and "slices" not in b
    assert validate(b) == ([], [])
    assert not update_slice(snap, "slice-1", "done")[0]
    assert "slices" not in normalize(snap)
    b2 = dict(snap, slices=[])   # liste vide = agrégé
    assert check_transition(b2, "build", "done")[0]


def _t_validate_slices() -> None:
    def errs(slices):
        return validate(_fx({"build": "in_progress"}, slices=slices))[0]
    assert validate(_fx({"build": "in_progress"})) == ([], [])
    assert len(errs("x")) == 1
    assert len(errs(["x"])) == 1
    assert len(errs([{"id": "a/b", "status": "pending"}])) == 1
    assert len(errs([{"id": "a", "status": "pending"}, {"id": "a", "status": "done"}])) == 1
    assert len(errs([{"id": "a", "status": "nope"}])) == 1
    assert errs([{"id": "a", "status": "done", "warnings": 1, "files": []}]) == []
    b = _fx({"review": "pending"})
    b["phases"]["review"]["covers"] = "slice-1"
    errors, warns = validate(b)
    assert not errors and len(warns) == 1
    b["phases"]["review"]["covers"] = None
    assert validate(b) == ([], [])


def _appr(battle: dict, at="T") -> dict:
    battle["phases"]["plan"]["approved_at"] = at
    return battle


def _t_build_blocked_requires_approval() -> None:
    b = _fx({"think": "done", "plan": ("done", "accept"), "build": "pending"})
    assert "plan non approuvé" in _refused(b, "build", "blocked")
    _, _, b1 = approve_plan(b, "T1")
    assert check_transition(b1, "build", "blocked")[0]
    for plan in ("in_progress", ("blocked", "revise")):
        bad = _appr(_fx({"think": "done", "plan": plan, "build": "pending"}))
        assert "build exige un plan done" in _refused(bad, "build", "blocked")
    # re-plan ouvert (approved_at null), build deja entamee : plus de legacy
    replan = _fx({"think": "done", "plan": ("done", "accept"), "build": "in_progress"})
    replan["phases"]["plan"]["approved_at"] = None
    assert "plan non approuvé" in _refused(replan, "build", "blocked")
    _, _, ok = approve_plan(replan, "T2")
    assert check_transition(ok, "build", "blocked")[0]


def _t_legacy_build_blocked_advances() -> None:
    b = _fx({"think": "done", "plan": ("done", "accept"), "build": "blocked"})
    assert "approved_at" not in b["phases"]["plan"]
    b1 = apply_transition(b, "build", "blocked")
    assert b1["phases"]["build"]["status"] == "blocked"
    b2 = apply_transition(b, "build", "in_progress")
    assert b2["phases"]["build"]["status"] == "in_progress"


def _t_cascade_missing() -> None:
    done = {p: ("done", "accept") for p in ("plan", "lint", "review", "test")}
    b = _fx({"build": "done", **done})
    assert cascade_missing(b) == []
    ok, _, inv, _ = invalidate(b, "manual", "T")
    assert ok
    assert cascade_missing(inv) == [{"phase": "lint", "status": "pending"},
                                    {"phase": "review", "status": "pending"},
                                    {"phase": "test", "status": "pending"}]
    assert "security" not in inv["phases"] and all(m["phase"] != "security" for m in cascade_missing(inv))
    sec = _fx({"build": "done", **done}, required_gates=["architect", "lint", "security"])
    assert cascade_missing(sec) == [{"phase": "security", "status": "absente"}]
    assert cascade_missing(_fx({"build": "done"}, required_gates=["architect", "pr-triage"])) == [
        {"phase": "plan", "status": "absente"}]


def _t_address_done_requires_cascade() -> None:
    done = {p: ("done", "accept") for p in ("plan", "lint", "review", "test")}
    b = _fx({"build": "done", "deliver": "done", **done},
            delivery={"pr_url": "https://example.test/pr/1"})
    ok, _, inv, _ = invalidate(b, "manual", "T")
    assert ok
    assert "lint" in _refused(inv, "address", "done")
    assert check_transition(inv, "address", "in_progress")[0]
    assert check_transition(inv, "address", "blocked")[0]
    assert check_transition(b, "address", "done")[0]
    assert "pr_url" in _refused(_fx({"deliver": "done", **done}), "address", "done")


def _t_set_slices_replace_refused() -> None:
    for st in ("in_progress", "blocked"):
        b = _appr(_sl(("s1", "done"), ("s2", "pending"), build=st))
        snap = copy.deepcopy(b)
        ok, reason, same, _ = set_slices(b, ["s2", "s3"], replace=True)
        assert not ok and "re-plan" in reason and same is b and b == snap, (st, reason)
    legacy = _sl(("s1", "done"), build="in_progress")   # clé approved_at absente
    ok, reason, _, _ = set_slices(legacy, ["s2"], replace=True)
    assert not ok and "re-plan" in reason
    done = _appr(_sl(("s1", "done"), build="done"))
    assert not set_slices(done, ["s1", "s2"], replace=True)[0]


def _t_set_slices_replace_replan() -> None:
    b = _sl(("s1", "done"), ("s2", "in_progress"), build="in_progress")
    b["slices"][1].update(warnings=2, files=["a.py"])
    b["phases"]["plan"]["approved_at"] = None   # re-plan ouvert
    snap = copy.deepcopy(b)
    ok, reason, out, det = set_slices(b, ["s2", "s3"], replace=True)
    assert ok, reason
    assert out["slices"] == [{"id": "s2", "status": "in_progress", "warnings": 2, "files": ["a.py"]},
                             {"id": "s3", "status": "pending"}]
    assert det["removed"] == ["s1"] and det["slices"] == out["slices"]
    assert b == snap and out["phases"]["build"]["status"] == "in_progress"
    ok, _, out2, _ = set_slices(b, ["s3", "s2"], replace=True)   # ordre = ordre des ids
    assert ok and [s["id"] for s in out2["slices"]] == ["s3", "s2"]
    # build pending : comme aujourd'hui, tout pending
    p = _sl(("s1", "done"), build="pending")
    ok, _, out3, det3 = set_slices(p, ["a", "b"], replace=True)
    assert ok and out3["slices"] == [{"id": "a", "status": "pending"}, {"id": "b", "status": "pending"}]
    assert det3["removed"] == ["s1"]


def _t_set_slices_replace_invalidates_cascade() -> None:
    b = _sl(("s1", "done"), build="done")
    b["phases"]["plan"]["approved_at"] = None
    for p in ("lint", "review", "test"):
        b["phases"][p] = {"status": "done", "verdict": "PASS"}
    ok, reason, out, det = set_slices(b, ["s1", "s2"], replace=True, now_iso="T")
    assert ok, reason
    assert out["phases"]["build"]["status"] == "blocked"
    assert det["invalidated"] == ["lint", "review", "test"]
    assert all(out["phases"][p]["status"] == "pending" for p in ("lint", "review", "test"))
    assert out["run"]["invalidations"][-1]["reason"] == "replan"
    assert b["phases"]["test"]["status"] == "done"   # entrée non mutée
    # retraits seuls : build reste done, cascade intacte
    two = _sl(("s1", "done"), ("s2", "done"), build="done")
    two["phases"]["plan"]["approved_at"] = None
    two["phases"]["lint"] = {"status": "done", "verdict": "PASS"}
    ok, _, out2, det2 = set_slices(two, ["s1"], replace=True, now_iso="T")
    assert ok and out2["phases"]["lint"]["status"] == "done" and "invalidated" not in det2


def _t_set_slices_replace_build_done() -> None:
    b = _sl(("s1", "done"), build="done")
    b["phases"]["plan"]["approved_at"] = None
    ok, reason, out, det = set_slices(b, ["s1", "s2"], replace=True)
    assert ok, reason
    assert out["phases"]["build"]["status"] == "blocked" and det["removed"] == []
    assert b["phases"]["build"]["status"] == "done"   # entrée non mutée
    assert "build" in _refused(out, "lint", "in_progress")
    assert "plan non approuvé" in _refused(out, "build", "in_progress")
    # retraits seuls : build reste done
    two = _sl(("s1", "done"), ("s2", "done"), build="done")
    two["phases"]["plan"]["approved_at"] = None
    ok, _, out2, det2 = set_slices(two, ["s1"], replace=True)
    assert ok and out2["phases"]["build"]["status"] == "done" and det2["removed"] == ["s2"]


def _t_replan_invalidates_cascade() -> None:
    b = _fx({"think": "done", "plan": ("done", "accept"), "build": "done",
             "lint": ("done", "accept"), "review": "blocked"})
    b["phases"]["plan"]["approved_at"] = "T0"
    b["phases"]["review"].update(verdict="revise", fails=["a|X"])
    snap = copy.deepcopy(b)
    out = apply_transition(b, "plan", "in_progress", now_iso="T")
    assert b == snap   # entrée non mutée
    for p in ("lint", "review"):
        e = out["phases"][p]
        assert e["status"] == "pending" and e["verdict"] is None and e["invalidated_at"] == "T", (p, e)
    assert out["phases"]["review"]["fails"] == ["a|X"]
    assert out["run"]["invalidations"] == [{"at": "T", "reason": "replan", "phases": ["lint", "review"]}]
    assert out["phases"]["plan"]["approved_at"] is None
    assert cascade_missing(out)


def _t_first_plan_no_invalidation_event() -> None:
    b = _fx({"think": "done", "plan": "pending"})
    out = apply_transition(b, "plan", "in_progress", now_iso="T")
    assert "run" not in out
    # boucle revise du plan : cascade déjà pending, aucun événement
    r = _fx({"think": "done", "plan": ("blocked", "revise"), "build": "pending",
             "lint": "pending", "review": "pending"})
    assert "run" not in apply_transition(r, "plan", "in_progress", now_iso="T")


def _t_replan_then_replace_single_event() -> None:
    b = _sl(("s1", "done"), build="done")
    b["phases"]["plan"]["approved_at"] = "T0"
    for p in ("lint", "review"):
        b["phases"][p] = {"status": "done", "verdict": "accept"}
    b = apply_transition(b, "plan", "in_progress", now_iso="T1")
    b = apply_transition(b, "plan", "done", "accept", now_iso="T2")
    assert b["phases"]["plan"]["approved_at"] is None
    ok, reason, out, det = set_slices(b, ["s1", "s2"], replace=True, now_iso="T3")
    assert ok, reason
    assert out["phases"]["build"]["status"] == "blocked"
    assert det["invalidated"] == [] and len(out["run"]["invalidations"]) == 1


def _t_set_slices_replace_empty() -> None:
    for st in ("in_progress", "blocked"):
        b = _sl(("s1", "done"), ("s2", "pending"), build=st)
        b["phases"]["plan"]["approved_at"] = None   # re-plan ouvert
        snap = copy.deepcopy(b)
        ok, reason, out, det = set_slices(b, [], replace=True)
        assert ok, (st, reason)
        assert out["slices"] == [] and det == {"slices": [], "removed": ["s1", "s2"]}
        assert out["phases"]["build"]["status"] == st and b == snap
        assert next_slice(out) is None
        ok, reason, appr = approve_plan(out, "T")
        assert ok, reason
        assert check_transition(appr, "build", "done")[0]   # plus aucune slice à finir
    p = _sl(("s1", "pending"), build="pending")   # premier passage
    ok, _, out, det = set_slices(p, [], replace=True)
    assert ok and out["slices"] == [] and det["removed"] == ["s1"]
    d = _sl(("s1", "done"), build="done")
    d["phases"]["plan"]["approved_at"] = None
    ok, _, out, det = set_slices(d, [], replace=True)
    assert ok and out["slices"] == [] and out["phases"]["build"]["status"] == "done"
    assert "invalidated" not in det


def _t_set_slices_replace_empty_refused() -> None:
    b = _appr(_sl(("s1", "pending"), build="in_progress"))
    snap = copy.deepcopy(b)
    ok, reason, same, _ = set_slices(b, [], replace=True)
    assert not ok and "re-plan" in reason and same is b and b == snap
    legacy = _sl(("s1", "done"), build="in_progress")   # clé approved_at absente
    ok, reason, same, _ = set_slices(legacy, [], replace=True)
    assert not ok and "re-plan" in reason and same is legacy
    ok, reason, same, _ = set_slices(b, [])
    assert not ok and "au moins un id" in reason and same is b


def _t_subcommands_constant() -> None:
    assert tuple(_build_parser_parts()[1].choices) == SUBCOMMANDS
    assert len(set(SUBCOMMANDS)) == len(SUBCOMMANDS)


def _t_doc_subcommands() -> None:
    root = Path(__file__).resolve().parents[1]
    docs = [root / "commands/battle.md", root / "skills/battle-workflow/SKILL.md",
            root / "ARCHITECTURE.md"]
    if not all(d.is_file() for d in docs):
        print("SKIP: _t_doc_subcommands (fichiers de doctrine absents, cache de plugin ?)",
              file=sys.stderr)
        return
    known = set(SUBCOMMANDS)
    for d in docs:
        text = d.read_text(encoding="utf-8")
        m = re.search(r"(?:[Ss]ubcommands|sous-commandes)[:\s]*`init`.*?`validate`", text, re.S)
        assert m, f"liste de sous-commandes introuvable dans {d.name}"
        cited = set(re.findall(r"`([a-z][a-z-]*)`", m.group(0)))
        assert cited == known, (d.name, sorted(cited ^ known))
        assert "set-slices --replace" in text, f"set-slices --replace non cité dans {d.name}"


def _t_doc_profiles() -> None:
    root = Path(__file__).resolve().parents[1]
    docs = [root / "commands/battle.md", root / "skills/battle-workflow/SKILL.md"]
    if not all(d.is_file() for d in docs):
        print("SKIP: _t_doc_profiles (fichiers de doctrine absents, cache de plugin ?)",
              file=sys.stderr)
        return
    for d in docs:
        text = d.read_text(encoding="utf-8")
        for prof, gates in PROFILES.items():
            rows = [ln for ln in text.splitlines() if ln.lstrip().startswith(f"| `{prof}`")]
            assert rows, f"profil {prof} absent de la table de {d.name}"
            cited = set(re.findall(r"`([a-z][a-z-]*)`", rows[0].split("|")[2]))
            assert cited == set(gates), (d.name, prof, sorted(cited ^ set(gates)))


def _t_doc_abort_stale() -> None:
    root = Path(__file__).resolve().parents[1]
    battle_md, fleet_md = root / "commands/battle.md", root / "commands/fleet.md"
    if not (battle_md.is_file() and fleet_md.is_file()):
        print("SKIP: _t_doc_abort_stale (fichiers de doctrine absents, cache de plugin ?)",
              file=sys.stderr)
        return
    battle = battle_md.read_text(encoding="utf-8")
    assert re.search(r"^## §I — abort", battle, re.M), "section abort absente de battle.md"
    assert "--remove-assignee" in battle, "désassignation gh absente de battle.md"
    fleet = fleet_md.read_text(encoding="utf-8")
    assert "stale=" in fleet and "24" in fleet, "stale / seuil absents de fleet.md"


def _t_cascade_refused_during_replan() -> None:
    b = _fx({"think": "done", "plan": ("done", "accept"), "build": "done"})
    b["phases"]["plan"]["approved_at"] = "T0"
    b = apply_transition(b, "plan", "in_progress", now_iso="T")
    b = apply_transition(b, "plan", "done", "accept")   # approved_at reste à null
    assert b["phases"]["plan"]["approved_at"] is None
    for p in CASCADE_PHASES:
        assert "plan non approuvé" in _refused(b, p, "in_progress"), p
        assert "plan non approuvé" in _refused(b, p, "done", verdict="accept"), p
        assert check_transition(b, p, "blocked", "revise")[0], p   # C1 : blocked hors spec, permis
    ok, reason, b2 = approve_plan(b, "T1")
    assert ok, reason
    for p in CASCADE_PHASES:
        assert check_transition(b2, p, "in_progress")[0], p
        assert check_transition(b2, p, "done", "accept")[0], p


def _t_cascade_legacy_no_approval_key() -> None:
    b = _fx({"plan": ("done", "accept"), "build": "done"})
    assert "approved_at" not in b["phases"]["plan"]
    for p in CASCADE_PHASES:
        assert check_transition(b, p, "in_progress")[0], p
        assert check_transition(b, p, "done", "accept")[0], p
    b["phases"]["plan"]["approved_at"] = None
    for p in CASCADE_PHASES:
        assert "plan non approuvé" in _refused(b, p, "in_progress"), p
        assert "plan non approuvé" in _refused(b, p, "done", verdict="accept"), p


def _t_replan_invalidates_in_progress_gate() -> None:
    b = _fx({"think": "done", "plan": ("done", "accept"), "build": "done",
             "lint": ("done", "accept"), "review": "in_progress"})
    b["phases"]["plan"]["approved_at"] = "T0"
    snap = copy.deepcopy(b)
    out = apply_transition(b, "plan", "in_progress", now_iso="T")
    assert b == snap   # entrée non mutée
    for p in ("lint", "review"):
        e = out["phases"][p]
        assert e["status"] == "pending" and e["verdict"] is None and e["invalidated_at"] == "T", (p, e)
    assert out["run"]["invalidations"] == [{"at": "T", "reason": "replan", "phases": ["lint", "review"]}]


def _t_polish_keeps_in_progress_gate() -> None:
    b = _fx({"build": "done", "plan": ("done", "accept"), "lint": ("done", "accept"),
             "review": ("done", "accept"), "test": ("done", "accept"),
             "security": "in_progress", "deliver": "pending"})
    ok, reason, out, det = invalidate(b, "polish", "T")
    assert ok, reason
    assert det["invalidated"] == ["lint", "review", "test"], det
    assert out["phases"]["security"] == {"status": "in_progress"}, out["phases"]["security"]
    b2 = _fx({"build": "done", "plan": ("done", "accept"), "review": "in_progress"})
    ok, reason, out2, det2 = invalidate(b2, "rebase", "T")
    assert ok, reason
    assert det2["invalidated"] == [] and out2["phases"]["review"]["status"] == "in_progress"


def _t_guard_of() -> None:
    assert guard_of({}) == ({}, True) and guard_of({"guard": {}}) == ({}, True)
    assert guard_of("x") == ({}, True) and guard_of(None) == ({}, True)
    g = {"allow": ["src/**"]}
    assert guard_of({"guard": g}) == (g, True)
    g = {"allow": None, "deny": None}
    assert guard_of({"guard": g}) == (g, True)
    assert guard_of({"guard": None}) == ({}, True)   # guard: null = bloc absent (non armé)
    for bad in ([], "x", 3, {"allow": "src/**"}, {"deny": {}}, {"allow": ["a", 1]},
                {"deny": 5}, {"allow": [None]}):
        assert guard_of({"guard": bad}) == (None, False), bad


def _t_validate_guard() -> None:
    def errs(g):
        return validate({"id": "x", "phases": {}, "guard": g})[0]
    assert any("guard" in e for e in errs([]))
    assert any("guard" in e for e in errs({"allow": "src/**"}))
    assert any("guard" in e for e in errs({"allow": ["a", 1]}))
    assert errs({"allow": ["a"], "deny": [], "careful": True}) == []
    assert errs({}) == [] and errs({"allow": None}) == []
    assert errs(None) == []   # guard: null = bloc absent (non armé)
    assert validate({"id": "x", "phases": {}})[0] == []


def _t_set_guard_repair() -> None:
    with _Repo() as r:
        r.init()
        b = r.load()
        b["guard"] = ["x"]
        r.path().write_text(json.dumps(b), encoding="utf-8")
        code, res = r.run("validate")
        assert code != 0 or res["errors"], res
        res = r.ok("set-guard", "--allow", "src/**")
        assert res.get("repaired") is True, res
        assert r.load()["guard"] == {"allow": ["src/**"], "deny": [], "careful": False}
        code, res = r.run("validate")
        assert code == 0 and res["errors"] == [], res
        res = r.ok("set-guard", "--deny", "d")
        assert not res.get("repaired")
        b = r.load()
        b["guard"] = "x"
        r.path().write_text(json.dumps(b), encoding="utf-8")
        r.ok("set-guard", "--careful", "on")
        assert r.load()["guard"] == {"allow": [], "deny": [], "careful": True}
        b = r.load()
        b["guard"] = {"allow": "s"}
        r.path().write_text(json.dumps(b), encoding="utf-8")
        r.ok("set-guard", "--deny", "d")
        assert r.load()["guard"]["allow"] == "s"
        code, res = r.run("validate")
        assert code != 0 or res["errors"], res
        r.ok("set-guard", "--allow", "a")
        code, res = r.run("validate")
        assert code == 0 and res["errors"] == [], res


def _t_is_aborted() -> None:
    assert not is_aborted(_fx())
    assert not is_aborted(_fx(aborted=None))
    assert is_aborted(_fx(aborted={"at": "T", "reason": None}))
    assert is_aborted(_fx(aborted="x"))
    assert not is_aborted("pas un objet")


def _t_abort_core() -> None:
    b = _fx({"think": "in_progress"})
    snap = copy.deepcopy(b)
    ok, reason, out = abort_battle(b, "piste morte", "T")
    assert ok and out["aborted"] == {"at": "T", "reason": "piste morte"}, (ok, reason, out)
    assert b == snap   # entrée non mutée
    for empty in ("", None):
        ok, _, out = abort_battle(b, empty, "T")
        assert ok and out["aborted"] == {"at": "T", "reason": None}


def _t_abort_refused_closed() -> None:
    b = _fx({"reflect": "done"})
    ok, reason, out = abort_battle(b, "x", "T")
    assert not ok and "close" in reason and out == b and "aborted" not in out


def _t_abort_refused_twice() -> None:
    ok, _, out = abort_battle(_fx({"think": "in_progress"}), "x", "T")
    ok2, reason, out2 = abort_battle(out, "y", "T2")
    assert ok and not ok2 and "déjà abandonnée" in reason and out2 == out


def _t_transition_refused_after_abort() -> None:
    b = _fx({"think": "done", "plan": ("done", "accept")}, aborted={"at": "T", "reason": None})
    for phase, status in (("think", "in_progress"), ("plan", "in_progress"), ("build", "in_progress"),
                          ("lint", "done"), ("plan", "blocked")):
        ok, reason = check_transition(b, phase, status)
        assert not ok and "abandonnée" in reason, (phase, status, reason)


def _t_aborted_refusal() -> None:
    ab = _fx(aborted={"at": "T", "reason": None})
    assert aborted_refusal(ab, "validate") is None
    for cmd in ("transition", "close", "next-slice", "check-cascade", "activate", "abort"):
        assert "abandonnée" in (aborted_refusal(ab, cmd) or ""), cmd
    assert aborted_refusal(_fx(), "transition") is None
    assert aborted_refusal(_fx(aborted=None), "close") is None


def _t_validate_aborted() -> None:
    for bad in ("x", {}):
        errors, warnings = validate(_fx({"think": "done"}, aborted=bad))
        assert not errors and any("aborted" in w for w in warnings), (bad, errors, warnings)
    errors, warnings = validate(_fx({"think": "done"}, aborted={"at": "T", "reason": None}))
    assert not errors and not any("aborted" in w for w in warnings)


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
    _t_invalidate_cascade, _t_invalidate_noop, _t_invalidate_empty_reason,
    _t_bump_continue_invalidates, _t_bump_escalate_no_invalidation,
    _t_deliver_after_invalidation, _t_progress_after_invalidation, _t_polish_once,
    _t_set_slices_create, _t_set_slices_after_start, _t_set_slices_invalid_id,
    _t_slice_update, _t_build_done_requires_slices, _t_build_blocked_with_slice_blocked,
    _t_covers_on_cascade_verdict, _t_next_slice, _t_slices_legacy_unchanged,
    _t_validate_slices, _t_build_blocked_requires_approval, _t_legacy_build_blocked_advances,
    _t_cascade_missing, _t_address_done_requires_cascade, _t_set_slices_replace_refused,
    _t_set_slices_replace_replan, _t_set_slices_replace_build_done, _t_set_slices_replace_invalidates_cascade,
    _t_replan_invalidates_cascade, _t_first_plan_no_invalidation_event,
    _t_replan_then_replace_single_event, _t_set_slices_replace_empty,
    _t_set_slices_replace_empty_refused, _t_subcommands_constant, _t_doc_subcommands, _t_doc_profiles, _t_doc_abort_stale,
    _t_cascade_refused_during_replan, _t_cascade_legacy_no_approval_key,
    _t_replan_invalidates_in_progress_gate, _t_polish_keeps_in_progress_gate,
    _t_guard_of, _t_validate_guard, _t_is_aborted, _t_abort_core, _t_abort_refused_closed,
    _t_abort_refused_twice, _t_transition_refused_after_abort, _t_aborted_refusal,
    _t_validate_aborted,
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
        r.ok("set-meta", "--title", "Nouveau", "--profile", "hotfix", "--required-gates",
             "architect", "lint", "--stack-kind", ".net", "--build-target", "src/a.csproj")
        b = r.load()
        assert b["title"] == "Nouveau" and b["profile"] == "hotfix"
        assert b["required_gates"] == ["architect", "lint"]
        assert b["stack"] == {"kind": ".net", "build_target": "src/a.csproj", "test_target": None}
        assert b["phases"] == before["phases"] and b["guard"] == before["guard"]
        r.ok("set-meta", "--build-target", "")
        assert r.load()["stack"]["build_target"] is None
        r.refused("set-meta", "--required-gates", "reviewr")
        r.refused("set-meta")
        assert "bugfix" in r.refused("set-meta", "--profile", "bugfix")


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
        code, res = _cli(td, *g, "init", "b1", "--ticket", "GH#1", "--title", "T", "--profile", "feature")
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
        assert code == 2 and "address done" in res["reason"]   # #88 : cascade incomplète
        code, res = _cli(td, *g, "transition", "address", "in_progress", "--round", "1")
        assert code == 0
        b = json.loads((repo / ".legion/battles/b1/battle.json").read_text(encoding="utf-8"))
        for p in ("plan", "lint", "review", "test"):
            b["phases"][p] = {"status": "done", "verdict": "accept"}
        (repo / ".legion/battles/b1/battle.json").write_text(json.dumps(b), encoding="utf-8")
        code, res = _cli(td, *g, "transition", "address", "done", "--round", "1", "--threads",
                         '[{"id":"T1"}]')
        assert code == 0, res
        b = json.loads((repo / ".legion/battles/b1/battle.json").read_text(encoding="utf-8"))
        assert b["phases"]["address"]["round"] == 1 and b["phases"]["address"]["threads"] == [{"id": "T1"}]


def _t_id_whitelist() -> None:
    with _Repo() as r:
        for bad in ("D:x", "a/b", "a\\b", "..", ".", "-x", "a b", "a_b", "é", "x:y"):
            reason = r.refused("init", bad, "--ticket", "T", "--title", "T", "--profile", "feature")
            assert "invalide" in reason, (bad, reason)
        r.init("2026-09-29-GH-69")
        # lien symbolique sortant de .legion/battles/
        outside = r.root / "dehors"
        outside.mkdir()
        try:
            (r.root / ".legion" / "battles" / "lnk").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        reason = r.refused("init", "lnk", "--ticket", "T", "--title", "T", "--profile", "feature")
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


def _t_invalidate_cli() -> None:
    with _Repo() as r:
        r.init()
        b = r.load()
        b["phases"]["build"]["status"] = "done"
        b["phases"]["lint"] = {"status": "done", "verdict": "accept"}
        b["phases"]["review"] = {"status": "done", "verdict": "accept"}
        b["phases"]["test"] = {"status": "done", "verdict": "accept"}
        b["phases"]["plan"] = {"status": "done", "verdict": "accept", "approved_at": "T"}
        r.path().write_text(json.dumps(b), encoding="utf-8")
        n = len(r.calls)
        res = r.ok("invalidate")
        assert res["reason"] == "manual" and res["invalidated"] == ["lint", "review", "test"], res
        assert len(r.calls) == n + 1   # synchro fleet appelee
        assert r.load()["run"]["invalidations"][0]["reason"] == "manual"
        for p in ("lint", "review", "test"):
            r.ok("transition", p, "in_progress")
            r.ok("transition", p, "done", "--verdict", "accept")
        assert r.ok("invalidate", "--reason", "polish")["reason"] == "polish"
        for p in ("lint", "review", "test"):
            r.ok("transition", p, "in_progress")
            r.ok("transition", p, "done", "--verdict", "accept")
        assert "polissage" in r.refused("invalidate", "--reason", "polish")
        assert "invalidate" in r.refused("transition", "review", "pending")
        code, res = r.run("validate")
        assert code == 0 and res["errors"] == [], res


def _t_slices_cli() -> None:
    with _Repo() as r:
        r.init()
        r.ok("transition", "think", "done")
        r.ok("transition", "plan", "in_progress")
        r.ok("transition", "plan", "done", "--verdict", "accept")
        r.ok("approve-plan")
        n = len(r.calls)
        res = r.ok("set-slices", "slice-1", "slice-2")
        assert [s["id"] for s in res["slices"]] == ["slice-1", "slice-2"]
        assert len(r.calls) == n + 1   # synchro appelée
        assert "build" in r.refused("slice", "slice-1", "done")   # build pending
        r.ok("transition", "build", "in_progress")
        res = r.ok("slice", "slice-1", "done", "--warnings", "1", "--files", "a.py", "b.py")
        assert res["slice"] == {"id": "slice-1", "status": "done", "warnings": 1,
                                "files": ["a.py", "b.py"]}
        assert res["slices_done"] == 1 and res["slices_total"] == 2
        n = len(r.calls)
        res = r.ok("next-slice")
        assert res["slice"] == {"id": "slice-2", "status": "pending"}
        assert len(r.calls) == n   # lecture seule : pas de synchro
        assert "slice-2" in r.refused("transition", "build", "done")
        r.ok("slice", "slice-2", "done")
        r.ok("transition", "build", "done")
        assert r.ok("next-slice")["slice"] is None
        assert "build" in r.refused("set-slices", "slice-9")   # build done
        code, res = r.run("validate")
        assert code == 0 and res["errors"] == [], res
        r.refused("slice", "slice-1", "pending")   # statut hors choix
        r.refused("set-slices")                     # au moins un id


def _t_check_cascade_cli() -> None:
    with _Repo() as r:
        r.init()
        b = r.load()
        b["phases"]["build"]["status"] = "done"
        for p in ("plan", "lint", "review", "test"):
            b["phases"][p] = {"status": "done", "verdict": "accept"}
        r.path().write_text(json.dumps(b), encoding="utf-8")
        res = r.ok("check-cascade")
        assert res["required"] == ["plan", "lint", "review", "test"], res
        r.ok("invalidate")
        n = len(r.calls)
        before = r.path().read_bytes()
        code, res = r.run("check-cascade")
        assert code == 2 and res["ok"] is False and res["missing"], res
        assert res["missing"][0] == {"phase": "lint", "status": "pending"}
        assert r.path().read_bytes() == before and len(r.calls) == n   # lecture seule


def _t_set_slices_replace_cli() -> None:
    with _Repo() as r:
        r.init()
        r.ok("transition", "think", "done")
        r.ok("transition", "plan", "in_progress")
        r.ok("transition", "plan", "done", "--verdict", "accept")
        r.ok("set-slices", "s1", "s2")
        r.ok("approve-plan")
        r.ok("transition", "build", "in_progress")
        assert "re-plan" in r.refused("set-slices", "--replace", "a", "b")   # approuvé
        r.ok("transition", "plan", "in_progress")                            # re-plan ouvert
        r.ok("transition", "plan", "done", "--verdict", "accept")
        res = r.ok("set-slices", "--replace", "s2", "a")
        assert res["removed"] == ["s1"] and [s["id"] for s in res["slices"]] == ["s2", "a"]
        r.ok("approve-plan")
        assert "re-plan" in r.refused("set-slices", "--replace", "a", "b")   # fenêtre refermée


def _t_replan_invalidates_cli() -> None:
    with _Repo() as r:
        r.init()
        b = r.load()
        b["phases"]["think"]["status"] = "done"
        b["phases"]["plan"] = {"status": "done", "verdict": "accept", "approved_at": "T0"}
        b["phases"]["build"]["status"] = "done"
        for p in ("lint", "review", "test"):
            b["phases"][p] = {"status": "done", "verdict": "accept"}
        r.path().write_text(json.dumps(b), encoding="utf-8")
        res = r.ok("transition", "plan", "in_progress")
        assert res["invalidated"] == ["lint", "review", "test"], res
        code, res = r.run("check-cascade")
        assert code == 2 and res["ok"] is False
        r.ok("transition", "plan", "done", "--verdict", "accept")
        assert "plan non approuvé" in r.refused("transition", "lint", "in_progress")
        r.ok("set-slices", "--replace", "s1", "s2")
        b = r.load()
        assert b["phases"]["build"]["status"] == "blocked"
        assert len(b["run"]["invalidations"]) == 1
        assert "invalidated" not in r.ok("transition", "plan", "in_progress")   # rien à invalider


def _t_set_slices_replace_empty_cli() -> None:
    with _Repo() as r:
        r.init()
        r.ok("transition", "think", "done")
        r.ok("transition", "plan", "in_progress")
        r.ok("transition", "plan", "done", "--verdict", "accept")
        r.ok("set-slices", "s1", "s2")
        r.ok("approve-plan")
        r.ok("transition", "build", "in_progress")
        r.ok("transition", "plan", "in_progress")   # re-plan ouvert
        r.ok("transition", "plan", "done", "--verdict", "accept")
        res = r.ok("set-slices", "--replace")
        assert res["slices"] == [] and res["removed"] == ["s1", "s2"], res
        assert r.load()["slices"] == []
        r.refused("set-slices")   # ni ids ni --replace
        r.ok("approve-plan")
        assert "re-plan" in r.refused("set-slices", "--replace")   # fenêtre refermée


def _t_active_battle_id() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        assert active_battle_id(root) is None                      # pas de .legion
        for raw in ("", "  \n"):
            _write_pointer(root, raw)
            assert active_battle_id(root) is None, raw
        for bad in ("../x", "a/b", "-x", "a b"):
            _write_pointer(root, bad)
            assert active_battle_id(root) is None, bad
        _pointer_path(root).write_bytes(b"\xff\xfe\x80b1")        # non UTF-8
        assert active_battle_id(root) is None
        _pointer_path(root).unlink()
        _pointer_path(root).mkdir()                                # dossier
        assert active_battle_id(root) is None
        _pointer_path(root).rmdir()
        _write_pointer(root, "b1\n")
        assert active_battle_id(root) == "b1"


def _t_load_active_battle() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        assert load_active_battle(root) is None
        for raw in ("", "  \n", "../x", "a/b", "-x", "a b"):
            _write_pointer(root, raw)
            assert load_active_battle(root) is None, raw
        _write_pointer(root, "b1")
        assert load_active_battle(root) is None                    # battle.json absent
        bdir = battles_dir(root) / "b1"
        bdir.mkdir(parents=True)
        (bdir / "battle.json").write_text("{ pas du json", encoding="utf-8")
        assert load_active_battle(root) is None
        (bdir / "battle.json").write_text("[]", encoding="utf-8")
        assert load_active_battle(root) is None
        (bdir / "battle.json").write_text("[" * 200000, encoding="utf-8")   # RecursionError
        assert load_active_battle(root) is None
        try:
            _load(bdir)
            raise AssertionError("_load doit refuser un JSON trop imbrique")
        except _Refuse as exc:
            assert "illisible" in exc.reason, exc.reason
        content = {"guard": {"allow": ["src/**"]}, "x": 1}
        (bdir / "battle.json").write_text(json.dumps(content), encoding="utf-8")
        assert load_active_battle(root) == ("b1", content)
        # lien symbolique sortant de .legion/battles/
        outside = root / "dehors"
        outside.mkdir()
        (outside / "battle.json").write_text("{}", encoding="utf-8")
        try:
            (battles_dir(root) / "lnk").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        _write_pointer(root, "lnk")
        assert load_active_battle(root) is None


def _t_active_reader_after_close() -> None:
    with _Repo() as r:
        r.init()
        assert active_battle_id(r.root) == "b1"
        assert load_active_battle(r.root) is not None
        r.ok("close")
        assert _pointer_path(r.root).read_text(encoding="utf-8") == ""
        assert active_battle_id(r.root) is None
        assert load_active_battle(r.root) is None


def _t_abort_cli() -> None:
    with _Repo() as r:
        r.init("b1")
        r.init("b2")
        pointer = r.root / ".legion" / "active-battle"
        r.ok("activate", "b1")
        n = len(r.calls)
        res = r.ok("abort", "--battle", "b2", "--reason", "autre")
        assert pointer.read_text(encoding="utf-8") == "b1" and "pointer_cleared" not in res
        assert len(r.calls) == n + 1
        n = len(r.calls)
        res = r.ok("abort", "--reason", "piste morte")
        assert r.load("b1")["aborted"]["reason"] == "piste morte" and res["pointer_cleared"]
        assert pointer.read_text(encoding="utf-8") == "" and len(r.calls) == n + 1
        assert "abandonnée" in r.refused("abort", "--battle", "b1")
        r.init("b3")
        r.ok("close")
        assert "close" in r.refused("abort", "--battle", "b3")


def _t_commands_refused_after_abort_cli() -> None:
    with _Repo() as r:
        r.init()
        r.ok("abort")
        before = r.path().read_bytes()
        n = len(r.calls)
        cmds = (("transition", "think", "done"), ("approve-plan",), ("set-guard", "--allow", "a"),
                ("set-meta", "--title", "x"), ("set-slices", "s1"), ("slice", "s1", "done"),
                ("invalidate",), ("close",), ("next-slice",), ("check-cascade",),
                ("abort",))
        for argv in cmds:
            assert "abandonnée" in r.refused(*argv, "--battle", "b1"), argv
        assert "abandonnée" in r.refused("activate", "b1")
        assert r.path().read_bytes() == before and len(r.calls) == n
        code, res = r.run("validate", "--battle", "b1")
        assert code == 0 and res["ok"], res


def _t_init_profiles() -> None:
    with _Repo() as r:
        res = r.ok("init", "h1", "--ticket", "GH#1", "--title", "T", "--profile", "hotfix")
        assert res["profile"] == "hotfix" and res["required_gates"] == ["lint", "reviewer", "test-engineer"]
        assert r.load("h1")["required_gates"] == ["lint", "reviewer", "test-engineer"]
        for prof in ("security", "spike"):
            res = r.ok("init", prof, "--ticket", "GH#1", "--title", "T", "--profile", prof)
            assert res["required_gates"] == list(PROFILES[prof])
            b = r.load(prof)
            assert b["required_gates"] == list(PROFILES[prof]) and "security" not in b["phases"]
        res = r.init("f1")
        assert res["required_gates"] == list(DEFAULT_REQUIRED_GATES)
        assert r.load("f1")["run"]["mode"] == "autonomous"
        r.ok("init", "h2", "--ticket", "GH#1", "--title", "T", "--profile", "hotfix",
             "--required-gates", "architect")
        assert r.load("h2")["required_gates"] == ["architect"]
        ptr = (r.root / ".legion" / "active-battle").read_text(encoding="utf-8")
        reason = r.refused("init", "bad", "--ticket", "GH#1", "--title", "T", "--profile", "bugfix")
        assert all(p in reason for p in PROFILES) and "bugfix" in reason
        assert not (r.root / ".legion" / "battles" / "bad").exists()
        assert (r.root / ".legion" / "active-battle").read_text(encoding="utf-8") == ptr
        assert "invalide" in r.refused("init", "a/b", "--ticket", "T", "--title", "T",
                                       "--profile", "bugfix")


def _t_set_meta_profile() -> None:
    with _Repo() as r:
        r.init()
        before = r.load()["required_gates"]
        r.ok("set-meta", "--profile", "hotfix")
        b = r.load()
        assert b["profile"] == "hotfix" and b["required_gates"] == before
        raw = r.path().read_bytes()
        assert "bugfix" in r.refused("set-meta", "--profile", "bugfix")
        assert r.path().read_bytes() == raw
        # battle legacy au profil inconnu : les autres champs restent modifiables
        b["profile"] = "bugfix"
        r.path().write_text(json.dumps(b), encoding="utf-8")
        r.ok("set-meta", "--title", "Autre")
        assert r.load()["profile"] == "bugfix"


def _t_validate_profile() -> None:
    with _Repo() as r:
        r.init()
        b = r.load()
        b["profile"] = "bugfix"
        r.path().write_text(json.dumps(b), encoding="utf-8")
        code, res = r.run("validate")
        assert code == 0 and res["errors"] == [] and any("bugfix" in w for w in res["warnings"]), res
    b = {"id": "x", "phases": {"think": {"status": "pending"}}}
    errors, warnings = validate(b)
    assert errors == [] and not any("profil" in w for w in warnings)
    b["profile"] = 3
    assert any("profil" in w for w in validate(b)[1])


def _t_hotfix_e2e() -> None:
    with _Repo() as r:
        r.ok("init", "hf", "--ticket", "GH#1", "--title", "T", "--profile", "hotfix")
        r.ok("transition", "think", "done")
        r.ok("transition", "plan", "in_progress")
        r.ok("transition", "plan", "done", "--verdict", "accept")
        r.ok("set-slices", "--replace", "slice-1")
        assert "plan" in r.refused("transition", "build", "in_progress")   # pas encore approuvé
        r.ok("approve-plan")
        r.ok("transition", "build", "in_progress")
        r.ok("slice", "slice-1", "in_progress")
        r.ok("slice", "slice-1", "done", "--files", "src/fix.py")
        r.ok("transition", "build", "done")
        r.refused("transition", "deliver", "in_progress")
        for ph in ("lint", "review"):
            r.ok("transition", ph, "in_progress")
            r.ok("transition", ph, "done", "--verdict", "accept")
        r.refused("transition", "deliver", "in_progress")   # test pas done
        r.ok("transition", "test", "in_progress")
        r.ok("transition", "test", "done", "--verdict", "accept")
        r.ok("check-cascade")
        r.ok("transition", "deliver", "in_progress")
        r.ok("transition", "deliver", "done")


_SEC_POS = (
    "src/Auth/Login.cs", "Api/AuthController.cs", "OAuthOptions.cs", "JwtTokenService.cs",
    "a/b/App.csproj", "appsettings.Development.json", ".env.local", "prod.env",
    "requirements-dev.txt", "config/ClientSecrets.json", "UsersEndpoints.cs", "certs/api.pfx",
    "web\\Program.cs", "./src/UserAuthService.cs", "yarn.lock", "nuget.config", "k/id.PEM",
    "Directory.Packages.props", "db/passwd.txt",
)
_SEC_NEG = (
    "Authors.cs", "docs/author-guide.md", "Tokenizer.py", "src/Billing/Invoice.cs", "README.md",
    "plugins/legion/agents/security.md",
)


def _t_security_hits() -> None:
    for f in _SEC_POS:
        assert security_hits([f]) == [f], f
    for f in _SEC_NEG:
        assert security_hits([f]) == [], f
    # faux négatifs connus et acceptés
    for f in ("src/login.py", "Dockerfile", ".github/workflows/ci.yml", "JwtHelper.cs"):
        assert security_hits([f]) == [], f
    assert security_hits(["a.py", "Auth/x.cs", "Auth/x.cs", "b.env", None, 3]) == ["Auth/x.cs", "b.env"]
    assert security_hits(None) == [] and security_hits([]) == []
    # mark_security_auto : legacy sans required_gates, idempotence, copie
    legacy = {"id": "x"}
    out, added = mark_security_auto(legacy, ["src/Auth/L.cs"], "T")
    assert out["required_gates"] == list(DEFAULT_REQUIRED_GATES) + ["security"] and added
    assert out["run"]["security_auto"] == {"at": "T", "files": ["src/Auth/L.cs"]}
    assert "required_gates" not in legacy and "run" not in legacy
    out2, added2 = mark_security_auto(out, ["b.env"], "T2")
    assert out2 is out and added2 == [] and out["run"]["security_auto"]["at"] == "T"
    same, none = mark_security_auto(legacy, ["README.md"], "T")
    assert same is legacy and none == []


def _t_security_auto_slice() -> None:
    def prep(r, bid="b1", *extra):
        r.init(bid, *extra)
        r.ok("transition", "think", "done")
        r.ok("transition", "plan", "in_progress")
        r.ok("transition", "plan", "done", "--verdict", "accept")
        r.ok("set-slices", "--replace", "s1", "s2")
        r.ok("approve-plan")
        r.ok("transition", "build", "in_progress")

    with _Repo() as r:
        prep(r)
        res = r.ok("slice", "s1", "done", "--files", "src/util.py")   # neutre
        assert "security_auto" not in res and res["warnings"] == []
        b = r.load()
        assert "security" not in b["required_gates"] and "security_auto" not in b["run"]
        r.ok("slice", "s2", "blocked", "--files", "src/Auth/X.cs")   # blocked : rien
        r.ok("slice", "s2", "in_progress", "--files", "src/Auth/X.cs")
        r.ok("slice", "s2", "done")   # --files absent
        r.ok("slice", "s2", "done", "--files")   # --files vide
        assert "security" not in r.load()["required_gates"]
        res = r.ok("slice", "s2", "done", "--files", "src/Auth/Login.cs", "src/a.py")
        b = r.load()
        assert b["required_gates"] == list(DEFAULT_REQUIRED_GATES) + ["security"]
        assert b["run"]["security_auto"]["files"] == ["src/Auth/Login.cs"]
        assert res["security_auto"] == b["run"]["security_auto"]
        assert len(res["warnings"]) == 1 and "src/Auth/Login.cs" in res["warnings"][0]
        assert "security" not in b["phases"]
        at = b["run"]["security_auto"]["at"]
        res = r.ok("slice", "s1", "done", "--files", "x.env")   # idempotent
        assert res["warnings"] == [] and "security_auto" not in res
        b = r.load()
        assert b["run"]["security_auto"]["at"] == at and b["required_gates"].count("security") == 1
        # DELIVER bloqué tant que security n'est pas done
        r.ok("transition", "build", "done")
        for ph in ("lint", "review", "test"):
            r.ok("transition", ph, "in_progress")
            r.ok("transition", ph, "done", "--verdict", "accept")
        code, res = r.run("check-cascade")
        assert code == 2 and "security (absente)" in res["reason"], res
        assert "security" in r.refused("transition", "deliver", "in_progress")
        r.ok("transition", "security", "in_progress")
        r.ok("transition", "security", "done", "--verdict", "accept")
        r.ok("check-cascade")
        r.ok("transition", "deliver", "in_progress")
    with _Repo() as r:   # profil security : déjà présente, aucune trace
        prep(r, "b1", "--required-gates", "architect", "security")
        res = r.ok("slice", "s1", "done", "--files", "src/Auth/Login.cs")
        assert res["warnings"] == [] and "security_auto" not in r.load()["run"]
    with _Repo() as r:   # legacy sans required_gates
        prep(r)
        b = r.load()
        del b["required_gates"]
        r.path().write_text(json.dumps(b), encoding="utf-8")
        r.ok("slice", "s1", "done", "--files", "Program.cs")
        assert r.load()["required_gates"] == list(DEFAULT_REQUIRED_GATES) + ["security"]
    with _Repo() as r:   # synchro fleet en échec : les deux avis sont conservés
        prep(r)
        def boom(*a):
            raise RuntimeError("kaboom")
        code, res = run_command(["--repo", str(r.root), "slice", "s1", "done", "--files",
                                 "src/Auth/Login.cs"], boom, r.fleet)
        assert code == 0 and len(res["warnings"]) == 2, res
        assert "security ajoutée" in res["warnings"][0] and "kaboom" in res["warnings"][1]


_INTEGRATION_TESTS = (
    _t_security_hits, _t_security_auto_slice,
    _t_init_profiles, _t_set_meta_profile, _t_validate_profile, _t_hotfix_e2e,
    _t_set_guard, _t_set_meta, _t_init, _t_activate_close, _t_atomic_and_corrupt,
    _t_fleet_sync_called, _t_phases_cs, _t_cli_exit_codes, _t_import_no_side_effect,
    _t_id_whitelist, _t_atomic_keeps_mode, _t_fleet_sync_explicit_path,
    _t_bump_build_failure_keeps_fails, _t_invalidate_cli, _t_slices_cli,
    _t_check_cascade_cli, _t_set_slices_replace_cli, _t_replan_invalidates_cli,
    _t_set_slices_replace_empty_cli, _t_active_battle_id, _t_load_active_battle,
    _t_active_reader_after_close, _t_set_guard_repair, _t_abort_cli,
    _t_commands_refused_after_abort_cli,
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
