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
- `PR_STATES`, `CI_STATES` (état de la PR et agrégat CI, GH#74 ; champs `delivery.pr_state` / `delivery.ci`) ;
- `SLICE_REPORT_PREFIX`, `slice_report_name(id)` et `slice_report_id(nom)` (GH#129 : nom du rapport de
  build d'une slice, `build-report-<id>.md`, partagé avec `guard.py`) ;
- `SUBCOMMANDS` (sous-commandes du CLI, dans l'ordre du parser ; la doctrine est testée contre elle).

Lecteurs partagés (hooks, GH#85), en lecture seule, sans exception : `active_battle_id(repo_root)
-> str | None` (pointeur + liste blanche d'id, ne lit pas `battle.json`),
`load_active_battle(repo_root) -> (id, dict) | None` (dict brut de `battle.json`),
`battles_dir(root)` et `guard_of(battle) -> (dict | None, bool)` (GH#104 : bloc `guard` et sa
validité ; pur, ne lève jamais ; source unique de la forme valide, partagée avec `validate`),
`resolve_state_root(cwd) -> Path` (GH#68 : racine de l'état `.legion/` ; dans un worktree lié, le
dépôt principal si une battle y est active, sinon le cwd ; ne lève jamais), `main_repo_root(cwd)
-> Path` (GH#128 : le dépôt principal d'un worktree lié même sans battle active, sinon le cwd ;
utilisé par `init` / `activate`), `worktree_battle_of(state_root, path) -> (id, dict) | None`
(GH#166 : la battle vivante dont le worktree contient `path`, déduite du chemin seul, sans le
pointeur ni le cwd ; lit un seul `battle.json`). Le CLI part de
`_repo_toplevel(cwd)` (GH#134 : racine du dépôt depuis un sous-dossier) ; `resolve_state_root` et
`main_repo_root` font de même d'eux-mêmes (GH#154), donc les hooks aussi.

Battle par session (GH#170), lecture seule sauf mention, jamais d'exception sauf `bind_session` /
`unbind_battle` (écrivent) : `session_keys(payload) -> list[str]` (clés de session du payload d'un
hook : `session_id`, sinon dérivée du transcript), `bind_session(state_root, key, battle_id)` et
`unbind_battle(state_root, battle_id) -> int` (liaisons `.legion/sessions/<clé>.json`),
`session_battle_of(state_root, payload) -> (id, dict) | None`, `resolve_battle(state_root,
payload) -> (id, dict, source)` avec `source` ∈ `session | pointer | foreign | none` (ordre :
session, puis pointeur s'il n'appartient pas à une autre session), `probe(payload, state_root)`
(sonde opt-in, noms de clés seulement).

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
- `pr_status_from_gh(obj) -> {pr_state, ci, failing}` (GH#74) : interprète la sortie JSON de
  `gh pr view --json state,mergedAt,statusCheckRollup,url` (déjà parsée). Pure, sans réseau ; lève
  `ValueError` sur toute entrée invalide ou valeur inconnue (`STALE` -> `pending`) ; précédence
  `fail` > `pending` > `pass`, `none` si le rollup est vide ou `null` ;
- `ci_fails(failing) -> [{target, dimension: "CI"}]` (GH#74) : FAIL du round CI, `target`
  assaini (`ci:<nom>`, sûr en shell, `#2`… pour un nom en double), sortie de `set-delivery --pr-json`.
- `fail_identity(item) -> str`, `normalize(battle)`, `validate(battle) -> (errors, warnings)`,
  `derive_required_phases(battle)` ;
- `cascade_missing(battle) -> [{phase, status}]` (#88) : phases requises non `done`, dans l'ordre
  de `required_gates` (phase absente : statut `absente`) ; `deliver` et `address done` l'exigent
  vide (sortie `check-cascade`) ; `deliver` exige en plus `build` done, quel que soit le profil (#114) ;
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

Rapports de build (GH#129) : `merge_build_reports(battle_id, ids, reports, aggregated_exists) ->
(ok, reason, texte, detail)` (pur ; `reports` : id -> texte | None) consolide les
`build-report-<slice_id>.md` en `build-report.md` (sections dans l'ordre de `slices`, « Hors périmètre »
regroupé en une section finale). Refus si un rapport manque ; sans slice déclarée, `build-report.md`
existant (BUILD agrégé) est conservé tel quel.

Usage (options globales `--battle <id>` défaut : pointeur `.legion/active-battle`, et
`--repo <path>` défaut : racine du dépôt contenant le cwd, même depuis un sous-dossier (GH#134) ;
depuis un worktree lié, dépôt principal — battle active pour les lectures/mutations, toujours
pour `init`/`activate` (GH#128) — sinon cette racine, ou le cwd hors dépôt) :
    python battle_state.py init <id> --ticket T --title T --profile P [--step] [--required-gates g…]
    python battle_state.py transition <phase> <status> [--verdict v] [--fails json]
                                      [--round n] [--threads json]
    python battle_state.py approve-plan | close | validate
    python battle_state.py bump-autocorrect <phase> (--fails <json> | --build-failure)
    python battle_state.py invalidate [--reason R]     # defaut R = manual
    python battle_state.py set-delivery (--pr-url <url> | --pr-json <fichier>)
                                    # --pr-json persiste aussi headRefName/headRefOid (GH#152)
    python battle_state.py set-guard [--allow [g…]] [--deny [g…]] [--careful on|off]
    python battle_state.py set-meta [--title] [--profile] [--required-gates …] [--stack-kind]
                                    [--build-target] [--test-target]
                                    [--worktree-path P --worktree-branch B --worktree-base SHA]
    python battle_state.py set-slices <id> [<id>…] | set-slices --replace [<id>…]
    python battle_state.py slice <id> <in_progress|done|blocked> [--warnings N] [--files [f…]]
    python battle_state.py next-slice                  # lecture seule (ni écriture ni synchro)
    python battle_state.py check-cascade               # lecture seule ; exit 2 = cascade incomplète
    python battle_state.py merge-reports               # écrit build-report.md ; exit 2 = rapport manquant
    python battle_state.py activate <id>
    python battle_state.py --self-test   # tests hermétiques, sort 0 offline

Bloc optionnel `worktree` (GH#152) : `{path, branch, base, created_at}`, écrit par `set-meta
--worktree-path/--worktree-branch/--worktree-base` (les trois ensemble ou aucun). `path` doit être
`<racine d'état>/.claude/worktrees/<id>`, `branch` un nom de branche git valide, `base` un sha de 40
hexa. Absent ou `null` = mode en place. `delivery.head_ref` / `delivery.head_oid` : branche et tip de
la PR (`headRefName` / `headRefOid` de `gh pr view`), posés par `set-delivery --pr-json` s'ils sont
fournis, remis à `null` par `--pr-url`.

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
import subprocess
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

PR_STATES: tuple[str, ...] = ("open", "merged", "closed")
CI_STATES: tuple[str, ...] = ("pass", "fail", "pending", "none")

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
                                "merge-reports", "activate", "close", "abort", "validate",
                                "session-status", "touch-files")

# Commandes encore permises sur une battle abandonnée (GH#75) : lecture de diagnostic seule.
ABORT_ALLOWED: tuple[str, ...] = ("validate",)

# Motif des identifiants (battle et slice) : liste blanche, aucun séparateur de chemin.
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")

# Rapport de build par slice (GH#129) : `build-report-<slice_id>.md`, écrit par le builder de la
# slice ; `merge-reports` les consolide dans `build-report.md` (PRODUCER_ARTIFACT). Source unique du nom.
SLICE_REPORT_PREFIX = "build-report-"
_SLICE_REPORT_RE = re.compile(re.escape(SLICE_REPORT_PREFIX) + r"([A-Za-z0-9][A-Za-z0-9-]*)\.md",
                              re.IGNORECASE)


def slice_report_name(slice_id: str) -> str:
    """Nom du rapport d'une slice. Lève `ValueError` si `slice_id` ne respecte pas `_ID_RE`."""
    if not isinstance(slice_id, str) or not _ID_RE.fullmatch(slice_id):
        raise ValueError(f"identifiant de slice invalide : {slice_id!r} (attendu : [A-Za-z0-9-])")
    return f"{SLICE_REPORT_PREFIX}{slice_id}.md"


def slice_report_id(name) -> "str | None":
    """Id lu dans un nom de fichier `build-report-<id>.md` (casse ignorée, `fullmatch` : aucun
    séparateur, aucun suffixe) ; `None` sinon. Pure, ne lève jamais."""
    if not isinstance(name, str):
        return None
    m = _SLICE_REPORT_RE.fullmatch(name)
    return m.group(1) if m else None


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
        if _status(battle, "build") != "done":  # #114 : rien à livrer sans BUILD (spike inclus)
            return False, f"deliver exige build done (statut : {_status(battle, 'build')})"
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

# --- Consolidation des rapports de build (GH#129) ----------------------------------------

_OOS_TITLE = "## Hors périmètre — candidats issue"
_OOS_HEADER = ("> Agrégé depuis les rapports de slice, dans l'ordre des slices. "
               "`/legion:retro` dédoublonne et matérialise en issues.")
_OOS_RE = re.compile(r"##\s+Hors\s+p\S*rim\S*tre\b", re.IGNORECASE)


def _split_report(text: str, slice_id: str) -> tuple[str, list[str], bool]:
    """Découpe le rapport d'une slice : (corps sans titre H1 ni section « Hors périmètre »,
    lignes des entrées « Hors périmètre » sans leur en-tête, titre `## <id>` injecté ?).
    Les blocs de code (```) sont ignorés pour repérer les titres."""
    body: list[str] = []
    oos: list[str] = []
    in_fence = False
    in_oos = False
    has_title = False
    title_re = re.compile(r"##\s+" + re.escape(slice_id) + r"\s*", re.IGNORECASE)
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            if _OOS_RE.match(line):
                in_oos = True
                continue
            if line.startswith("## "):
                in_oos = False
            elif re.match(r"#\s", line):   # titre H1 : retiré
                continue
        if in_oos:
            oos.append(line)
            continue
        if not in_fence and title_re.fullmatch(line):
            has_title = True
        body.append(line)
    entries: list[str] = []
    started = False
    for line in oos:   # l'en-tête (citations, blancs) précède la première entrée `###`
        if not started and not line.startswith("###"):
            continue
        started = True
        entries.append(line)
    body_text = "\n".join(body).strip("\n")
    injected = not has_title
    if injected:
        body_text = f"## {slice_id}\n\n{body_text}".rstrip("\n")
    return body_text, entries, injected


def merge_build_reports(battle_id: str, ids, reports: dict,
                        aggregated_exists: bool) -> tuple[bool, str, "str | None", dict]:
    """Consolide les rapports par slice en `build-report.md` (GH#129). Pure : `reports` est un
    dict `id -> texte | None`, aucun accès disque. Retourne `(ok, reason, texte, detail)` ;
    `texte` est `None` si rien n'est à écrire.
    - aucune slice déclarée (`ids` vide) : ok sans réécriture si `aggregated_exists`
      (`detail.aggregated`), sinon refus ;
    - sinon : refus si un id est invalide ou si un rapport manque ou est blanc
      (`detail.missing`) ; ordre des sections = ordre de `ids` ; les sections « Hors périmètre »
      sont regroupées dans une seule section finale ; `detail.warnings` liste les titres injectés."""
    ids = list(ids) if isinstance(ids, (list, tuple)) else []
    if not ids:
        if aggregated_exists:
            return True, "", None, {"aggregated": True, "merged": [], "warnings": []}
        return False, ("aucune slice déclarée et build-report.md absent ou blanc : écrire "
                       "build-report.md (BUILD agrégé) avant merge-reports"), None, {}
    bad = [i for i in ids if not isinstance(i, str) or not _ID_RE.fullmatch(i)]
    if bad:
        return False, f"id de slice invalide dans battle.json : {bad!r}", None, {"invalid": bad}
    ordered = list(dict.fromkeys(ids))
    missing = [i for i in ordered
               if not isinstance(reports.get(i), str) or not reports[i].strip()]
    if missing:
        return False, (f"rapport de slice manquant ou blanc : {', '.join(missing)} — écrire "
                       + ", ".join(slice_report_name(i) for i in missing)
                       + " (battle ancienne : découper build-report.md en rapports de slice)"), \
            None, {"missing": missing}
    sections: list[str] = []
    entries: list[str] = []
    warnings: list[str] = []
    for i in ordered:
        body, oos, injected = _split_report(reports[i], i)
        if injected:
            warnings.append(f"{slice_report_name(i)} : titre `## {i}` absent, injecté")
        sections.append(body)
        if any(line.strip() for line in oos):
            entries.append("\n".join(oos).strip("\n"))
    parts = [f"# Build report ({battle_id})"] + sections
    if entries:
        parts += [_OOS_TITLE + "\n\n" + _OOS_HEADER + "\n\n" + "\n\n".join(entries)]
    text = "\n\n".join(p.strip("\n") for p in parts) + "\n"
    return True, "", text, {"merged": ordered, "warnings": warnings}


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


def glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """Traduit un glob (`**`, `*`, `?`) en regex ancrée, en chemins posix.

    Source unique du matcher de périmètre (GH#66) : `guard.py` et `artifact_check.py`
    (`tree-verify --guard`) doivent matcher à l'identique.
    """
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
                    i += 1  # le .* couvre déjà le slash
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


def glob_match(rel_path: str, patterns) -> bool:
    """True si `rel_path` (posix ou `\\`) matche au moins un glob de `patterns`."""
    rel_path = rel_path.replace("\\", "/")
    return any(glob_to_regex(p).search(rel_path) for p in patterns)


_SHA40_RE = re.compile(r"[0-9a-f]{40}")
_REF_BAD_CHARS = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def _branch_problem(name) -> "str | None":
    """Contrôle pur d'un nom de branche (règles de `git check-ref-format --branch`). `None` si valide."""
    if not isinstance(name, str) or not name:
        return "branche absente ou vide"
    if name == "@" or name.startswith("-") or name.startswith("/") or name.endswith("/") \
            or name.endswith(".") or "//" in name or ".." in name or "@{" in name \
            or _REF_BAD_CHARS.search(name):
        return f"nom de branche invalide : {name!r}"
    for part in name.split("/"):
        if part.startswith(".") or part.endswith(".lock"):
            return f"nom de branche invalide : {name!r}"
    return None


def _worktree_problem(wt, battle_id=None) -> "str | None":
    """Contrôle structurel pur du bloc `worktree` (absent ou `null` : valide). `None` si valide."""
    if wt is None:
        return None
    if not isinstance(wt, dict):
        return "worktree n'est ni un objet ni null"
    path = wt.get("path")
    if not isinstance(path, str) or not path:
        return "worktree.path absent ou vide"
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if len(parts) < 3 or parts[-3:-1] != [".claude", "worktrees"] or not _ID_RE.fullmatch(parts[-1]):
        return f"worktree.path hors de .claude/worktrees/<id> : {path!r}"
    if isinstance(battle_id, str) and battle_id and parts[-1] != battle_id:
        return f"worktree.path : dernier segment {parts[-1]!r} différent de l'id {battle_id!r}"
    problem = _branch_problem(wt.get("branch"))
    if problem:
        return f"worktree.branch : {problem}"
    base = wt.get("base")
    if not isinstance(base, str) or not _SHA40_RE.fullmatch(base):
        return f"worktree.base n'est pas un sha de 40 hexa : {base!r}"
    return None


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
    if "delivery" in battle:
        dl = battle["delivery"]
        if not isinstance(dl, dict):
            warnings.append("delivery n'est pas un objet")
        else:
            if dl.get("pr_state") is not None and dl["pr_state"] not in PR_STATES:
                warnings.append(f"delivery.pr_state inconnu : {dl['pr_state']!r}")
            if dl.get("ci") is not None and dl["ci"] not in CI_STATES:
                warnings.append(f"delivery.ci inconnu : {dl['ci']!r}")
            if dl.get("checked_at") is not None and not isinstance(dl["checked_at"], str):
                warnings.append("delivery.checked_at n'est pas une chaîne")
            for key in ("head_ref", "head_oid"):
                if dl.get(key) is not None and not isinstance(dl[key], str):
                    warnings.append(f"delivery.{key} n'est pas une chaîne")
    if "worktree" in battle:
        problem = _worktree_problem(battle["worktree"], battle.get("id"))
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


# --- Statut PR / CI (GH#74) --------------------------------------------------------------

_GH_PR_STATES = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}
_CHECK_STATUSES = ("COMPLETED", "QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED")
_CHECK_FAIL = ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE")
_CHECK_PASS = ("SUCCESS", "NEUTRAL", "SKIPPED")
_CHECK_CONCLUSIONS = _CHECK_FAIL + _CHECK_PASS + ("STALE",)
_CONTEXT_FAIL = ("FAILURE", "ERROR")
_CONTEXT_PENDING = ("PENDING", "EXPECTED")
_CONTEXT_STATES = _CONTEXT_FAIL + _CONTEXT_PENDING + ("SUCCESS",)


def _check_verdict(el) -> tuple[str, str | None, str | None]:
    """Un élément de `statusCheckRollup` -> (`pass`|`fail`|`pending`, nom, url). `ValueError` si invalide."""
    if not isinstance(el, dict):
        raise ValueError(f"élément de statusCheckRollup non objet : {el!r}")
    typename = el.get("__typename")
    if typename is None:
        if "status" in el or "conclusion" in el:
            typename = "CheckRun"
        elif "state" in el:
            typename = "StatusContext"
        else:
            raise ValueError("élément de statusCheckRollup sans status/conclusion ni state")
    if typename == "CheckRun":
        status = el.get("status")
        if status not in _CHECK_STATUSES:
            raise ValueError(f"CheckRun.status inconnu : {status!r}")
        conclusion = el.get("conclusion")
        if conclusion in ("", None):
            if status == "COMPLETED":
                raise ValueError("CheckRun COMPLETED sans conclusion")
            verdict = "pending"
        elif conclusion not in _CHECK_CONCLUSIONS:
            raise ValueError(f"CheckRun.conclusion inconnue : {conclusion!r}")
        elif status != "COMPLETED" or conclusion == "STALE":
            verdict = "pending"
        elif conclusion in _CHECK_FAIL:
            verdict = "fail"
        else:
            verdict = "pass"
        return verdict, el.get("name"), el.get("detailsUrl")
    if typename == "StatusContext":
        state = el.get("state")
        if state not in _CONTEXT_STATES:
            raise ValueError(f"StatusContext.state inconnu : {state!r}")
        verdict = "fail" if state in _CONTEXT_FAIL else "pending" if state in _CONTEXT_PENDING else "pass"
        return verdict, el.get("context"), el.get("targetUrl")
    raise ValueError(f"__typename inconnu : {typename!r}")


def pr_status_from_gh(obj) -> dict:
    """Sortie de `gh pr view --json state,mergedAt,statusCheckRollup,url[,headRefName,headRefOid]`
    (déjà parsée) -> `{"pr_state", "ci", "failing"}` (+ `head_ref` / `head_oid` si `headRefName` /
    `headRefOid` sont des chaînes non vides ; absents ou invalides = clés omises). Pure (ni disque ni réseau). `state` fait foi (`mergedAt` est
    ignoré) ; `ci` : `fail` > `pending` > `pass`, `none` si le rollup est `[]` ou `null` ;
    `failing` : `[{"name", "url"}]` des checks en échec. `ValueError` sur toute entrée invalide."""
    if not isinstance(obj, dict):
        raise ValueError("la sortie de gh n'est pas un objet JSON")
    state = obj.get("state")
    if not isinstance(state, str) or state not in _GH_PR_STATES:
        raise ValueError(f"state absent ou inconnu : {state!r}")
    if "statusCheckRollup" not in obj:
        raise ValueError("statusCheckRollup absent (demander --json ...,statusCheckRollup)")
    rollup = obj["statusCheckRollup"]
    if rollup is not None and not isinstance(rollup, list):
        raise ValueError("statusCheckRollup n'est ni une liste ni null")
    verdicts: list[str] = []
    failing: list[dict] = []
    for el in rollup or []:
        verdict, name, url = _check_verdict(el)
        verdicts.append(verdict)
        if verdict == "fail":
            failing.append({"name": name, "url": url})
    if not verdicts:
        ci = "none"
    elif "fail" in verdicts:
        ci = "fail"
    elif "pending" in verdicts:
        ci = "pending"
    else:
        ci = "pass"
    out = {"pr_state": _GH_PR_STATES[state], "ci": ci, "failing": failing}
    for src, key in (("headRefName", "head_ref"), ("headRefOid", "head_oid")):
        val = obj.get(src)
        if isinstance(val, str) and val:
            out[key] = val
    return out


_CI_TARGET_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def ci_fails(failing) -> list[dict]:
    """FAIL du round CI pour `bump-autocorrect build --fails` : `[{"target", "dimension": "CI"}]`.
    Pur. `target` = `ci:<nom>` assaini (hors `[A-Za-z0-9._-]` -> `_`, 80 caractères au plus) :
    stable d'un run à l'autre (le contrôle de non-progrès compare les noms) et sans guillemet, donc
    sûr dans une commande shell. Deux checks de même nom (matrice) reçoivent `#2`, `#3`… dans
    l'ordre du rollup."""
    out: list[dict] = []
    seen: dict[str, int] = {}
    for f in failing or []:
        name = f.get("name") if isinstance(f, dict) else None
        base = "ci:" + (_CI_TARGET_UNSAFE.sub("_", str(name)).strip("_")[:80] or "check")
        seen[base] = seen.get(base, 0) + 1
        target = base if seen[base] == 1 else f"{base}#{seen[base]}"
        out.append({"target": target, "dimension": "CI"})
    return out


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


def _is_live(battle) -> bool:
    """Vrai si la battle n'est ni abandonnée ni close (`reflect` pas `done`). Pur."""
    return isinstance(battle, dict) and not is_aborted(battle) and _status(battle, "reflect") != "done"


def worktree_battle_of(state_root, path) -> tuple[str, dict] | None:
    """`(id, battle.json brut)` de la battle vivante dont le worktree contient `path`, ou `None`.

    GH#166. Lecture seule, ne lève jamais, un seul `battle.json` lu (aucun balayage). Le résultat
    ne dépend ni du cwd, ni du pointeur. Il faut, ensemble : `realpath(path)` sous
    `realpath(<state>/.claude/worktrees/<seg>)` avec `<seg>` conforme à `_ID_RE` ; un
    `<state>/.legion/battles/<seg>/battle.json` lisible et de type objet ;
    `realpath(worktree.path)` égal à ce dossier ; la battle vivante (ni abandonnée, ni close).
    """
    try:
        base = Path(os.path.realpath(Path(state_root) / ".claude" / "worktrees"))
        target = Path(os.path.realpath(path))
        rel = target.relative_to(base)
        if not rel.parts:
            return None
        seg = rel.parts[0]
        if not _ID_RE.fullmatch(seg):
            return None
        data = json.loads((battles_dir(Path(state_root)) / seg / "battle.json")
                          .read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not _is_live(data):
            return None
        wt = data.get("worktree")
        wt_path = wt.get("path") if isinstance(wt, dict) else None
        if not isinstance(wt_path, str) or not wt_path:
            return None
        if Path(os.path.realpath(wt_path)) != base / seg:
            return None
        return seg, data
    except Exception:  # noqa: BLE001 - contrat : jamais d'exception
        return None


# --- Battle par session (GH#170) -----------------------------------------------------------

_SESSION_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _sessions_dir(root: Path) -> Path:
    return Path(root) / ".legion" / "sessions"


def _valid_key(key) -> bool:
    return isinstance(key, str) and _SESSION_KEY_RE.fullmatch(key) is not None


def _transcript_kind_key(path) -> tuple[str, str | None]:
    """`(type, clé)` d'un chemin de transcript : `subagent` (`<sid>/subagents/agent-*.jsonl`),
    `main` (`<sid>.jsonl`) ou `autre`. La clé n'est pas validée ici. Pur, sans exception."""
    try:
        if not isinstance(path, str) or not path:
            return "absent", None
        parts = [x for x in path.replace("\\", "/").split("/") if x]
        if len(parts) >= 3 and parts[-2] == "subagents" and parts[-1].startswith("agent-") \
                and parts[-1].endswith(".jsonl"):
            return "subagent", parts[-3]
        if parts and parts[-1].endswith(".jsonl") and len(parts[-1]) > len(".jsonl"):
            return "main", parts[-1][:-len(".jsonl")]
        return "autre", None
    except Exception:  # noqa: BLE001 - contrat : jamais d'exception
        return "autre", None


def _transcript_keys(payload) -> list[str]:
    """Clés dérivées des transcripts du payload : sous-agent d'abord, puis principal."""
    out: list[str] = []
    try:
        found = [_transcript_kind_key(payload.get(f)) for f in ("agent_transcript_path", "transcript_path")]
        for want in ("subagent", "main"):
            for kind, key in found:
                if kind == want and _valid_key(key) and key not in out:
                    out.append(key)
    except Exception:  # noqa: BLE001
        return []
    return out


def session_keys(payload) -> list[str]:
    """Clés de session d'un payload de hook, sans doublon, conformes à `^[A-Za-z0-9_-]{1,128}$`
    (aucune traversée de chemin) : `session_id`, puis la clé dérivée du transcript (dossier parent
    d'un `<sid>/subagents/agent-*.jsonl`, sinon nom de `<sid>.jsonl`). `[]` sans information.
    Lecture seule, ne lève jamais."""
    try:
        if not isinstance(payload, dict):
            return []
        keys: list[str] = []
        sid = payload.get("session_id")
        if _valid_key(sid):
            keys.append(sid)
        for k in _transcript_keys(payload):
            if k not in keys:
                keys.append(k)
        return keys
    except Exception:  # noqa: BLE001 - contrat : jamais d'exception
        return []


def _read_binding(path: Path) -> tuple[str, dict] | None:
    """`(battle_id, contenu)` d'un fichier de liaison lisible et conforme, sinon `None`."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        bid = data.get("battle") if isinstance(data, dict) else None
        if isinstance(bid, str) and _ID_RE.fullmatch(bid):
            return bid, data
    except Exception:  # noqa: BLE001
        pass
    return None


def _live_data(root: Path, battle_id: str) -> dict | None:
    """`battle.json` brut de la battle si elle est lisible et vivante, sinon `None`."""
    try:
        data = json.loads((battles_dir(Path(root)) / battle_id / "battle.json")
                          .read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and _is_live(data) else None
    except Exception:  # noqa: BLE001
        return None


def _list_bindings(root: Path) -> list[tuple[Path, str | None, dict | None]]:
    """`(fichier, battle_id | None, contenu | None)` pour chaque `*.json` de `.legion/sessions/`
    (`None` si le fichier est illisible). Liste vide si le dossier est absent."""
    out = []
    try:
        files = sorted(_sessions_dir(root).glob("*.json"))
    except OSError:
        return out
    for f in files:
        b = _read_binding(f)
        out.append((f, b[0] if b else None, b[1] if b else None))
    return out


def bind_session(state_root: Path, key: str, battle_id: str) -> None:
    """Lie la session `key` à `battle_id` : écrit `.legion/sessions/<key>.json` de façon atomique,
    supprime les autres liaisons vers `battle_id` (une seule session pilote une battle : la
    dernière activation l'emporte) et celles vers des battles mortes ou illisibles. Lève
    `ValueError` si `key` ou `battle_id` est invalide, `OSError` si l'écriture échoue."""
    if not _valid_key(key):
        raise ValueError(f"clé de session invalide : {key!r}")
    if not isinstance(battle_id, str) or not _ID_RE.fullmatch(battle_id):
        raise ValueError(f"identifiant de battle invalide : {battle_id!r}")
    sdir = _sessions_dir(state_root)
    sdir.mkdir(parents=True, exist_ok=True)
    mine = sdir / f"{key}.json"
    _atomic_write(mine, {"battle": battle_id, "bound_at": _now_iso()})
    for f, bid, _data in _list_bindings(state_root):
        if f == mine:
            continue
        if bid is None or bid == battle_id or _live_data(state_root, bid) is None:
            try:
                f.unlink()
            except OSError:
                pass


def unbind_battle(state_root: Path, battle_id: str) -> int:
    """Supprime toutes les liaisons vers `battle_id` ; renvoie leur nombre. Une erreur sur un
    fichier est ignorée (il n'est alors pas compté)."""
    n = 0
    for f, bid, _data in _list_bindings(state_root):
        if bid == battle_id:
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


def session_battle_of(state_root, payload) -> tuple[str, dict] | None:
    """`(id, battle.json brut)` de la première clé du payload liée à une battle vivante lisible,
    sinon `None`. Lecture seule, ne lève jamais."""
    try:
        for key in session_keys(payload):
            b = _read_binding(_sessions_dir(Path(state_root)) / f"{key}.json")
            if b is None:
                continue
            data = _live_data(Path(state_root), b[0])
            if data is not None:
                return b[0], data
        return None
    except Exception:  # noqa: BLE001 - contrat : jamais d'exception
        return None


def resolve_battle(state_root, payload) -> tuple[str | None, dict | None, str]:
    """`(id, battle.json brut, source)` de la battle d'une session de hook. Lecture seule, ne lève
    jamais. `source` : `session` (liaison de la session) ; `pointer` (pointeur actif, comportement
    historique : aussi sans clé, ou sans `.legion/sessions/`) ; `foreign` (le payload a une clé non
    liée et le pointeur est lié à une autre session : `(None, None, "foreign")`) ; `none`."""
    try:
        root = Path(state_root)
        if _sessions_dir(root).is_dir():
            found = session_battle_of(root, payload)
            if found is not None:
                return found[0], found[1], "session"
            pointed = active_battle_id(root)
            if pointed and session_keys(payload) \
                    and any(bid == pointed for _f, bid, _d in _list_bindings(root)):
                return None, None, "foreign"
        loaded = load_active_battle(root)
        if loaded is not None:
            return loaded[0], loaded[1], "pointer"
        return None, None, "none"
    except Exception:  # noqa: BLE001 - contrat : jamais d'exception
        return None, None, "none"


def _has_live_binding(root: Path) -> bool:
    try:
        return any(bid is not None and _live_data(root, bid) is not None
                   for _f, bid, _d in _list_bindings(root))
    except Exception:  # noqa: BLE001
        return False


def probe(payload, state_root) -> None:
    """Sonde opt-in (`LEGION_HOOK_PROBE=1` ou fichier `.legion/hook-probe`) : ajoute une ligne à
    `.legion/hook-probe.jsonl` avec seulement des **noms** de clés et des booléens/types (aucune
    valeur de `session_id`, aucun chemin). Ne lève jamais."""
    try:
        root = Path(state_root)
        if os.environ.get("LEGION_HOOK_PROBE") != "1" and not (root / ".legion" / "hook-probe").exists():
            return
        p = payload if isinstance(payload, dict) else {}
        sid = p.get("session_id")
        tkind = "absent"
        for f in ("agent_transcript_path", "transcript_path"):
            kind, _k = _transcript_kind_key(p.get(f))
            if kind != "absent":
                tkind = kind
                break
        derived = _transcript_keys(p)
        event, agent = p.get("hook_event_name"), p.get("agent_type")
        line = {
            "at": _now_iso(),
            "event": event if isinstance(event, str) and len(event) <= 40 else None,
            "agent_type": agent if isinstance(agent, str) and len(agent) <= 80 else None,
            "keys": sorted(str(k) for k in p)[:64],
            "session_id_present": _valid_key(sid),
            "transcript_kind": tkind,
            "transcript_key_equals_session_id": bool(derived) and _valid_key(sid) and derived[0] == sid,
        }
        target = root / ".legion" / "hook-probe.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - contrat : jamais d'exception
        return


def _pointer_taken(root: Path, new_id: str) -> tuple[str | None, list[str]]:
    """`(previous_active, warnings)` : si le pointeur nomme une autre battle vivante (GH#166)."""
    prev = active_battle_id(root)
    if not prev or prev == new_id:
        return None, []
    try:
        data = json.loads((_battle_dir(root, prev) / "battle.json").read_text(encoding="utf-8"))
    except (_Refuse, OSError, ValueError, RecursionError):
        return None, []
    if not _is_live(data):
        return None, []
    return prev, [f"pointer_taken: la battle {prev} était active ; ses hooks et ses commandes "
                  f"sans --battle visent désormais {new_id}"]


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


_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")


def _pick_state_root(cwd: Path, git_dir: str, common_dir: str, main_has_battle: bool) -> Path:
    """Cœur pur de `resolve_state_root` (GH#68) : choisit la racine d'état.

    `git_dir` / `common_dir` sont les deux lignes de `git rev-parse --git-dir --git-common-dir`
    (éventuellement relatives au `cwd`). Renvoie `cwd` sauf dans un worktree lié dont le dépôt
    principal porte une battle active (`main_has_battle`) : alors `common_dir.parent`.
    `git_dir == common_dir` (dépôt principal, sous-dossier, sous-module) et un `common_dir` qui
    n'est pas nommé `.git` (worktree d'un dépôt bare) donnent `cwd`.
    """
    def _abs(raw: str) -> Path:
        p = Path(raw)
        return Path(os.path.realpath(p if p.is_absolute() else cwd / p))

    gd, cd = _abs(git_dir), _abs(common_dir)
    if gd == cd or cd.name != ".git":
        return cwd
    return cd.parent if main_has_battle else cwd


def _linked_main_root(cwd: Path) -> Path | None:
    """Dépôt principal d'un worktree lié non bare (GH#128, partagé par `resolve_state_root` et
    `main_repo_root`), chemin réel ; `None` sinon. Chemin rapide sans sous-processus quand
    `cwd/.git` est un dossier. Toute erreur (git absent, délai, code != 0, sortie incomplète)
    donne `None`. L'environnement git hérité est nettoyé. Ne lève jamais.
    """
    try:
        cwd = Path(cwd)
        if (cwd / ".git").is_dir():
            return None
        env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--git-dir", "--git-common-dir"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2, env=env)
        if proc.returncode != 0:
            return None
        lines = proc.stdout.splitlines()
        if len(lines) < 2:
            return None
        git_dir, common_dir = lines[0].strip(), lines[1].strip()
        picked = _pick_state_root(cwd, git_dir, common_dir, True)
        return None if picked == cwd else picked
    except Exception:  # noqa: BLE001 - contrat : jamais d'exception
        return None


def _repo_toplevel(cwd: Path) -> Path | None:
    """Racine du dépôt (ou du worktree) contenant `cwd` (GH#134), chemin réel ; `None` pour garder
    le `cwd`. Chemin rapide sans sous-processus quand `cwd/.git` existe (dossier ou fichier).
    Sinon `git rev-parse --show-toplevel --show-superproject-working-tree` : code != 0, git
    absent, délai, sortie vide, non absolue ou seconde ligne non vide (sous-module) donnent `None`.
    L'environnement git hérité est nettoyé. Ne lève jamais.
    """
    try:
        cwd = Path(cwd)
        if (cwd / ".git").exists():
            return cwd
        env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel", "--show-superproject-working-tree"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2, env=env)
        if proc.returncode != 0:
            return None
        lines = [ln.strip() for ln in proc.stdout.splitlines()]
        if not lines or not lines[0] or any(lines[1:]) or not os.path.isabs(lines[0]):
            return None
        return Path(os.path.realpath(lines[0]))
    except Exception:  # noqa: BLE001 - contrat : jamais d'exception
        return None


def resolve_state_root(cwd: Path) -> Path:
    """Racine de l'état `.legion/` pour un `cwd` (GH#68). Lecture seule, ne lève jamais.

    Part de la racine du dépôt ou du worktree contenant `cwd` (`_repo_toplevel`, GH#154 : un
    sous-dossier donne sa racine ; `cwd` si inconnue). Dans un worktree lié (`git rev-parse --git-dir --git-common-dir` : deux dossiers distincts),
    renvoie le dépôt principal s'il a une battle active (pointeur, ou liaison de session vivante
    dans `.legion/sessions/`, GH#170), sinon la racine ci-dessus. Chemin
    rapide sans sous-processus quand `cwd/.git` est un dossier. Toute erreur (git absent, délai, code != 0,
    sortie incomplète) renvoie `cwd` tel quel. L'environnement git hérité est nettoyé.
    """
    try:
        base = _repo_toplevel(cwd) or Path(cwd)
        main = _linked_main_root(base)
        if main is not None and (active_battle_id(main) is not None or _has_live_binding(main)):
            return main
        return base
    except Exception:  # noqa: BLE001 - contrat : jamais d'exception
        return Path(cwd) if isinstance(cwd, (str, os.PathLike)) else cwd


def main_repo_root(cwd: Path) -> Path:
    """Dépôt principal pour un `cwd` (GH#128) : celui d'un worktree lié non bare, même sans battle
    active, sinon la racine du dépôt contenant `cwd` (GH#154, `_repo_toplevel`). Utilisé par
    `init` / `activate`. Lecture seule, ne lève jamais."""
    try:
        base = _repo_toplevel(cwd) or Path(cwd)
        main = _linked_main_root(base)
        return main if main is not None else base
    except Exception:  # noqa: BLE001 - contrat : jamais d'exception
        return Path(cwd) if isinstance(cwd, (str, os.PathLike)) else cwd


def _cli_root(args, cwd: Path) -> Path:
    """Racine d'état du CLI (GH#128, GH#134). `--repo` non vide l'emporte ; sinon on part de la
    racine du dépôt contenant le cwd (`_repo_toplevel`, le cwd si inconnue), puis `init` /
    `activate` visent toujours le dépôt principal (`main_repo_root`), les autres
    `resolve_state_root` (qui normalisent aussi d'eux-mêmes, GH#154, comme les hooks)."""
    repo = getattr(args, "repo", None)
    if repo:
        return Path(repo)
    base = _repo_toplevel(cwd) or Path(cwd)
    if getattr(args, "cmd", None) in ("init", "activate"):
        return main_repo_root(base)
    return resolve_state_root(base)


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
    common.add_argument("--battle", default=argparse.SUPPRESS, help="id (défaut : pointeur active-battle ; la doctrine passe toujours --battle)")
    common.add_argument("--repo", default=argparse.SUPPRESS, help="racine de l'état (défaut : racine du dépôt contenant le cwd, même depuis un sous-dossier ; depuis un worktree lié, dépôt principal — battle active pour les lectures/mutations, toujours pour init/activate ; hors dépôt, le cwd)")
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
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--pr-url")
    g.add_argument("--pr-json", help="fichier JSON de `gh pr view --json "
                                     "state,mergedAt,statusCheckRollup,url[,headRefName,headRefOid]` "
                                     "(GH#74, GH#152)")
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
    s.add_argument("--worktree-path")
    s.add_argument("--worktree-branch")
    s.add_argument("--worktree-base")
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
    add("merge-reports")
    s = add("activate")
    s.add_argument("id")
    add("close")
    s = add("abort")
    s.add_argument("--reason", default=None)
    add("validate")
    add("session-status")
    s = add("touch-files")
    s.add_argument("--files", nargs="+", required=True)
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


def _git_branch_check(name: str) -> None:
    """Confirme `name` avec `git check-ref-format --branch` (best-effort : git absent = ignoré)."""
    try:
        res = subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True,
                             stdin=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return
    if res.returncode != 0:
        raise _Refuse(f"set-meta : worktree.branch refusée par git check-ref-format : {name!r}")


def _mutation(args, battle: dict, root: "Path | None" = None) -> tuple[dict, dict]:
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
    if cmd == "touch-files":   # GH#115 : fichiers touchés hors `slice … done` (correctif, polissage, §H)
        reflect = ((battle.get("phases") or {}).get("reflect") or {})
        if isinstance(reflect, dict) and reflect.get("status") == "done":
            raise _Refuse("battle close : touch-files refusé")
        out, added = mark_security_auto(battle, args.files, _now_iso())
        detail = {"files": list(args.files), "security_added": bool(added)}
        if added:
            detail["security_auto"] = copy.deepcopy(out["run"]["security_auto"])
            detail["_warnings"] = [
                f"security ajoutée à required_gates (fichiers sensibles : {', '.join(added)})"]
        return out, detail
    out = copy.deepcopy(battle)
    if cmd == "set-delivery":
        if args.pr_url is not None:   # nouvelle PR : l'état de suivi repart de zéro
            out["delivery"] = {**(out["delivery"] if isinstance(out.get("delivery"), dict) else {}),
                               "pr_url": args.pr_url, "pr_state": "open", "ci": None,
                               "checked_at": None, "head_ref": None, "head_oid": None}
            return out, {"pr_url": args.pr_url}
        delivery = out.get("delivery")
        pr_url = delivery.get("pr_url") if isinstance(delivery, dict) else None
        if not pr_url:
            raise _Refuse("set-delivery --pr-json exige delivery.pr_url")
        try:
            raw = Path(args.pr_json).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise _Refuse(f"--pr-json illisible : {type(exc).__name__}: {exc}")
        try:
            gh = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise _Refuse(f"--pr-json : JSON invalide ({type(exc).__name__})")
        try:
            status = pr_status_from_gh(gh)
        except ValueError as exc:
            raise _Refuse(f"--pr-json : {exc}")
        if gh.get("url") is not None and gh["url"] != pr_url:
            raise _Refuse(f"--pr-json : url {gh['url']!r} différente de delivery.pr_url {pr_url!r}")
        checked_at = _now_iso()
        delivery.update(pr_state=status["pr_state"], ci=status["ci"], checked_at=checked_at)
        heads = {k: status[k] for k in ("head_ref", "head_oid") if k in status}
        delivery.update(heads)
        return out, {"pr_state": status["pr_state"], "ci": status["ci"],
                     "checked_at": checked_at, "failing": status["failing"],
                     "ci_fails": ci_fails(status["failing"]), **heads}
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
        wt_opts = (args.worktree_path, args.worktree_branch, args.worktree_base)
        if any(v is not None for v in wt_opts):
            if any(v is None for v in wt_opts):
                raise _Refuse("set-meta : --worktree-path, --worktree-branch et --worktree-base "
                              "vont ensemble")
            bid = out.get("id")
            wt = {"path": args.worktree_path, "branch": args.worktree_branch,
                  "base": args.worktree_base, "created_at": _now_iso()}
            problem = _worktree_problem(wt, bid)
            if problem:
                raise _Refuse(f"set-meta : {problem}")
            if root is not None:
                expected = Path(root) / ".claude" / "worktrees" / str(bid)
                if os.path.realpath(args.worktree_path) != os.path.realpath(expected):
                    raise _Refuse(f"set-meta : worktree.path {args.worktree_path!r} différent de "
                                  f"{str(expected)!r}")
            _git_branch_check(args.worktree_branch)
            out["worktree"] = wt
            touched.append("worktree")
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


def _read_report(path: Path) -> "str | None":
    """Texte du fichier, `None` s'il n'existe pas ; refus s'il est illisible."""
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _Refuse(f"rapport illisible : {path.name} ({type(exc).__name__})")


def _merge_reports_cmd(bdir: Path, bid: str, battle: dict) -> tuple[int, dict]:
    """`merge-reports` : lit les rapports par slice, écrit `build-report.md` de façon atomique
    (seulement si tous les contrôles passent). Ne modifie jamais `battle.json`, ne synchronise pas le fleet."""
    ids = [s.get("id") for s in _slices(battle)]
    reports = {i: _read_report(bdir / slice_report_name(i))
               for i in ids if isinstance(i, str) and _ID_RE.fullmatch(i)}
    target = bdir / "build-report.md"
    current = _read_report(target)
    ok, reason, text, detail = merge_build_reports(
        bid, ids, reports, aggregated_exists=bool(current and current.strip()))
    if not ok:
        return 2, {"ok": False, "battle": bid, "reason": reason, **detail}
    warnings = list(detail.get("warnings", []))
    declared = {i.lower() for i in ids if isinstance(i, str)}
    if bdir.is_dir():
        for child in sorted(bdir.iterdir()):
            extra_id = slice_report_id(child.name)
            if extra_id is not None and extra_id.lower() not in declared:
                warnings.append(f"extra : {child.name} (slice `{extra_id}` non déclarée), non fusionné")
    if text is not None:
        try:
            _write_text_atomic(target, text)
        except OSError as exc:
            raise _Refuse(f"écriture de build-report.md impossible : {exc}")
    result = {"ok": True, "battle": bid, "merged": detail.get("merged", []),
              "path": str(target), "warnings": warnings}
    if detail.get("aggregated"):
        result["aggregated"] = True
    return 0, result


def run_command(argv: list[str], upsert=None, fleet_dir=None, cwd=None) -> tuple[int, dict]:
    """Exécute une sous-commande. Retourne (code, résultat JSON). Ne lève jamais.
    `upsert`/`fleet_dir` : injection pour les tests (sinon `fleet_sync` réel) ; `cwd` : répertoire
    de départ de la résolution de racine (défaut `Path.cwd()`, GH#128)."""
    try:
        args = _build_parser().parse_args(argv)
        if not args.cmd:
            raise _Refuse("usage invalide : sous-commande requise")
        root = _cli_root(args, Path(cwd) if cwd else Path.cwd())
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
            previous, mutation_warnings = _pointer_taken(root, args.id)
            _write_pointer(root, args.id)
            init_gates = _load(bdir)["required_gates"]
            result.update(battle=args.id, profile=args.profile, required_gates=init_gates,
                          previous_active=previous)
        elif args.cmd == "activate":
            bdir = _battle_dir(root, args.id)
            refusal = aborted_refusal(_load(bdir), "activate")
            if refusal:
                raise _Refuse(refusal)
            previous, taken = _pointer_taken(root, args.id)
            _write_pointer(root, args.id)
            return 0, {"ok": True, "battle": args.id, "active": args.id,
                       "previous_active": previous, "warnings": taken}
        elif args.cmd == "session-status":  # lecture seule : ni _save ni _sync_fleet (GH#170)
            bid = explicit or _read_pointer(root)
            if not bid:
                raise _Refuse("aucune battle active (utiliser --battle <id> ou `activate`)")
            _battle_dir(root, bid)
            mine = [d for _f, b, d in _list_bindings(root) if b == bid]
            stamps = sorted(str(d.get("bound_at")) for d in mine if isinstance(d, dict) and d.get("bound_at"))
            return 0, {"ok": True, "battle": bid, "bound": bool(mine), "keys": len(mine),
                       "bound_at": stamps[-1] if stamps else None}
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
            if args.cmd == "merge-reports":  # lit battle.json, écrit build-report.md, pas de synchro
                return _merge_reports_cmd(bdir, bid, battle)
            new, extra = _mutation(args, battle, root)
            _save(bdir, new)
            mutation_warnings = extra.pop("_warnings", [])
            result.update(battle=bid, **extra)
            if args.cmd in ("close", "abort"):
                result["unbound"] = unbind_battle(root, bid)
                if _read_pointer(root) == bid:
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
    done = {"plan": ("done", "accept"), "build": "done", "lint": ("done", "accept"),
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
    b = _fx({"plan": ("done", "accept"), "build": "done"}, required_gates=["architect", "pr-triage"])
    assert check_transition(b, "deliver", "done")[0]
    # #114 : profil spike (architect seul) — deliver exige build done.
    b = _fx({"plan": ("done", "accept")}, required_gates=["architect"])
    assert "build" in _refused(b, "deliver", "in_progress")
    b["phases"]["build"] = {"status": "done"}
    assert check_transition(b, "deliver", "in_progress")[0]


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


# --- merge-reports (GH#129) : cœur pur -------------------------------------------------------

def _rep(sid: str, oos: str = "", h1: bool = True, title: bool = True) -> str:
    out = [f"# Build report — {sid} (b)", ""] if h1 else []
    if title:
        out += [f"## {sid}", ""]
    out += [f"fait {sid}", ""]
    if oos:
        out += ["## Hors périmètre — candidats issue", "", "> en-tête de slice", "",
                f"### {oos}", f"- Zone : {sid}", ""]
    return "\n".join(out)


def _t_slice_report_names() -> None:              # M1, M2
    assert slice_report_name("slice-1") == "build-report-slice-1.md"
    assert slice_report_id("build-report-slice-1.md") == "slice-1"
    assert slice_report_id("Build-Report-Slice-1.md") == "Slice-1"
    for bad in ("build-report.md", "build-report-.md", "build-report--x.md", "build-report-a_b.md",
                "build-report-a.b.md", "build-report-s1.md.bak", "build-report-s1.txt",
                "x/build-report-s1.md", "build-report-s1.md\n", None):
        assert slice_report_id(bad) is None, bad
    for bad in ("../x", "", "a/b", "-x", None):
        try:
            slice_report_name(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def _t_merge_nominal() -> None:                   # M3
    ids = ["slice-2", "slice-10", "slice-1"]
    ok, reason, text, detail = merge_build_reports(
        "bx", ids, {i: _rep(i) for i in ids}, False)
    assert ok and not reason and detail["merged"] == ids and detail["warnings"] == [], detail
    pos = [text.index(f"## {i}\n") for i in ids]
    assert pos == sorted(pos), pos
    assert text.startswith("# Build report (bx)\n") and text.count("# Build report") == 1
    assert "\n# Build report — " not in text and text.endswith("\n") and not text.endswith("\n\n")
    assert "Hors périmètre" not in text
    again = merge_build_reports("bx", ids, {i: _rep(i) for i in ids}, False)[2]
    assert again == text


def _t_merge_missing_blank() -> None:             # M4 (cœur), M5
    reps = {"slice-1": _rep("slice-1"), "slice-2": None, "slice-3": "  \n\t\n", "slice-4": ""}
    ok, reason, text, detail = merge_build_reports("b", list(reps), reps, True)
    assert not ok and text is None and detail["missing"] == ["slice-2", "slice-3", "slice-4"]
    for i in ("slice-2", "slice-3", "slice-4"):
        assert i in reason and f"build-report-{i}.md" in reason


def _t_merge_out_of_scope() -> None:              # M6, M7
    reps = {"slice-1": _rep("slice-1", oos="Opp A"), "slice-2": _rep("slice-2"),
            "slice-3": _rep("slice-3", oos="Opp C")}
    ok, _, text, _ = merge_build_reports("b", list(reps), reps, False)
    assert ok and text.count("## Hors périmètre") == 1 and text.count("> ") == 1
    head = text.index("## Hors périmètre")
    assert head > text.index("## slice-3\n") and "en-tête de slice" not in text
    tail = text[head:]
    assert tail.index("### Opp A") < tail.index("### Opp C") and "### Opp" not in text[:head]
    assert tail.count("## Hors") == 1 and "## slice" not in tail
    # section présente mais vide : pas de section finale
    empty = {"slice-1": _rep("slice-1") + "\n## Hors périmètre — candidats issue\n\n> x\n"}
    _, _, text2, _ = merge_build_reports("b", ["slice-1"], empty, False)
    assert "Hors périmètre" not in text2
    # section au milieu du rapport : le reste de la slice est conservé
    mid = "## slice-1\n\nA\n\n## Hors périmètre — candidats issue\n\n### T\n- z\n\n## Suite\n\nB\n"
    _, _, text3, _ = merge_build_reports("b", ["slice-1"], {"slice-1": mid}, False)
    assert "## Suite" in text3 and text3.index("## Suite") < text3.index("### T")


def _t_merge_titles() -> None:                    # M8
    reps = {"slice-1": _rep("slice-1", title=False), "slice-2": _rep("slice-2", h1=False)}
    ok, _, text, detail = merge_build_reports("b", list(reps), reps, False)
    assert ok and "# Build report — " not in text
    assert "## slice-1\n" in text and len(detail["warnings"]) == 1 and "slice-1" in detail["warnings"][0]
    fenced = {"slice-1": "## slice-1\n\n```\n# commentaire\n## Hors périmètre — candidats issue\n```\n"}
    _, _, text2, _ = merge_build_reports("b", ["slice-1"], fenced, False)
    assert "# commentaire" in text2 and text2.count("## Hors périmètre") == 1  # dans le bloc de code


def _t_merge_aggregated_and_invalid() -> None:   # M10, M11 (cœur)
    ok, _, text, detail = merge_build_reports("b", [], {}, True)
    assert ok and text is None and detail["aggregated"] is True
    ok, reason, text, _ = merge_build_reports("b", [], {}, False)
    assert not ok and text is None and reason
    for bad in (["../x"], ["slice-1", 3], [None]):
        ok, reason, text, _ = merge_build_reports("b", bad, {}, False)
        assert not ok and text is None and "invalide" in reason, bad


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
        # `session-status` (GH#170) suit `validate` dans SUBCOMMANDS : la doctrine le cite juste
        # après la plage `init`..`validate`, vérifié ci-dessous.
        assert cited == known - {"session-status", "touch-files"}, (d.name, sorted(cited ^ known))
        if d.name == "battle.md":
            assert "session-status" in text, "session-status non cité dans battle.md"
        assert "touch-files" in text, f"touch-files non cité dans {d.name}"
        assert "set-slices --replace" in text, f"set-slices --replace non cité dans {d.name}"


def _t_doc_plugin_root_braces() -> None:   # GH#172 : seule la forme ${CLAUDE_PLUGIN_ROOT} est substituée
    root = Path(__file__).resolve().parents[1]
    files = sorted((root / "commands").glob("*.md")) + sorted((root / "skills").glob("*/SKILL.md"))
    if not files:
        print("SKIP: _t_doc_plugin_root_braces (fichiers de doctrine absents, cache de plugin ?)",
              file=sys.stderr)
        return
    for f in files:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            assert "$CLAUDE_PLUGIN_ROOT" not in line, (
                f"{f.name}:{n} : $CLAUDE_PLUGIN_ROOT sans accolades (non substitué) : {line.strip()}")
    assert "${CLAUDE_PLUGIN_ROOT}/scripts/battle_state.py" in (root / "commands/battle.md").read_text(
        encoding="utf-8"), "battle.md : forme ${CLAUDE_PLUGIN_ROOT} absente"


def _t_doc_concurrent() -> None:   # GH#170 : doctrine des battles concurrentes
    root = Path(__file__).resolve().parents[1]
    names = ("battle", "retro", "freeze", "careful", "guard")
    paths = {n: root / f"commands/{n}.md" for n in names}
    if not all(p.is_file() for p in paths.values()):
        print("SKIP: _t_doc_concurrent (fichiers de doctrine absents, cache de plugin ?)",
              file=sys.stderr)
        return
    docs = {n: p.read_text(encoding="utf-8") for n, p in paths.items()}
    battle = docs["battle"]
    assert "session-status" in battle, "session-status absent de battle.md"
    assert "concurrent_battle" not in "".join(docs.values()), "concurrent_battle subsiste"
    assert "in_place_live" in battle, "in_place_live non documenté"
    # appels battle_state.py de freeze / careful / guard / retro : toujours --battle
    for n in ("freeze", "careful", "guard", "retro"):
        for line in docs[n].splitlines():
            if "battle_state.py" not in line or "CLAUDE_PLUGIN_ROOT" not in line:
                continue
            sub = line.split("battle_state.py\"", 1)[1].split()
            if sub and sub[0] in ("init", "activate", "session-status", "validate"):
                continue
            assert "--battle" in line, f"{n}.md : appel sans --battle : {line.strip()}"
    # §E : instantané et vérification par battle
    e_sec = battle[battle.index("## §E — review / test gates"):battle.index("### Gate artifact delivery check")]
    assert "tree-snapshot --battle" in e_sec or "tree-snapshot` --battle" in e_sec, "§E : tree-snapshot --battle absent"
    assert "tree-verify --battle" in e_sec, "§E : tree-verify --battle absent"
    assert "git fetch --no-tags origin" in battle, "fetch --no-tags absent de battle.md"


def _t_doc_pr_tracking() -> None:
    root = Path(__file__).resolve().parents[1]
    battle_md, retro_md = root / "commands/battle.md", root / "commands/retro.md"
    if not (battle_md.is_file() and retro_md.is_file()):
        print("SKIP: _t_doc_pr_tracking (fichiers de doctrine absents, cache de plugin ?)",
              file=sys.stderr)
        return
    battle, retro = battle_md.read_text(encoding="utf-8"), retro_md.read_text(encoding="utf-8")
    for needle in ("set-delivery --pr-json", "statusCheckRollup", "--log-failed",
                   "bump-autocorrect build"):
        assert needle in battle, f"{needle!r} absent de battle.md"
    assert "set-delivery --pr-json" in retro, "set-delivery --pr-json absent de retro.md"
    for name, text in (("battle.md", battle), ("retro.md", retro)):
        for block in re.findall(r"```.*?```", text, re.S):
            assert "$ARGUMENTS" not in block, f"$ARGUMENTS dans un bloc de code de {name}"


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


def _t_doc_tree_integrity() -> None:
    root = Path(__file__).resolve().parents[1]
    docs = [root / "commands/battle.md", root / "skills/battle-workflow/SKILL.md",
            root / "ARCHITECTURE.md"]
    if not all(d.is_file() for d in docs):
        print("SKIP: _t_doc_tree_integrity (fichiers de doctrine absents, cache de plugin ?)",
              file=sys.stderr)
        return
    for d in docs:
        text = d.read_text(encoding="utf-8")
        assert "**6. Faute d'écriture d'une gate**" in text, f"cas 6 absent de {d.name}"
        assert "**7. Fusion du lot parallèle**" in text, f"cas 7 absent de {d.name}"
    battle = docs[0].read_text(encoding="utf-8")
    for needle in ("tree-snapshot", "tree-verify", "--fingerprint", "--guard", "--base"):
        assert needle in battle, f"{needle!r} absent de battle.md"
    assert "Bash non couvert" not in docs[2].read_text(encoding="utf-8")


def _t_doc_fan_in() -> None:
    battle_md = Path(__file__).resolve().parents[1] / "commands/battle.md"
    if not battle_md.is_file():
        print("SKIP: _t_doc_fan_in (fichiers de doctrine absents, cache de plugin ?)",
              file=sys.stderr)
        return
    text = battle_md.read_text(encoding="utf-8")
    start = text.index("**Mode — `--auto`.**")
    sec = text[start:text.index("After build (either mode)", start)]
    # `fan_in.py` peut être suivi d'un guillemet fermant (`"${CLAUDE_PLUGIN_ROOT}/scripts/fan_in.py" apply`)
    pos: dict[str, int] = {}
    for sub in ("apply", "cleanup"):
        m = re.search(rf'fan_in\.py"? {sub}\b', sec)
        assert m, f"fan_in.py {sub} absent du §D --auto de battle.md"
        pos[sub] = m.start()
    mb = re.search(r'fan_in\.py"? base\b', sec)
    assert mb, "fan_in.py base absent du §D --auto de battle.md"
    assert mb.start() < sec.index("tree-snapshot") and mb.start() < pos["apply"], \
        "fan_in.py base doit précéder tree-snapshot et apply (§D --auto)"
    # GH#157 : chaque appel fan_in.py (base|align|apply|cleanup) porte --battle ; en mode
    # worktree, base / apply / cleanup portent aussi --root "<wt>" (meme ligne ou suivante)
    builder_md = Path(__file__).resolve().parents[1] / "agents/builder.md"
    fan_re = re.compile(r'fan_in\.py"? (base|align|apply|cleanup)\b')
    for name, body in (("battle.md", sec),
                       ("builder.md", builder_md.read_text(encoding="utf-8")
                        if builder_md.is_file() else "")):
        for n, line in enumerate(body.splitlines(), 1):
            if fan_re.search(line) and ("python" in line or "--base" in line):   # appel, pas prose
                assert "--battle" in line, f"{name} (§D) ligne {n} : fan_in.py sans --battle"
    if builder_md.is_file():
        btxt = builder_md.read_text(encoding="utf-8")
        assert "align" in btxt, "builder.md doit citer align"
        assert re.search(r'fan_in\.py>?"? align --base <base> --battle <id>', btxt), \
            "builder.md : la commande align doit porter --battle <id>"
        assert "ignorée quand l'entrée 6 est présente" in btxt, "builder.md : entrée 6 prime sur 7"
    lines = sec.splitlines()
    for sub in ("base", "apply", "cleanup"):
        for n, line in enumerate(lines):
            if re.search(rf'fan_in\.py"? {sub}\b', line):
                assert '--root "<wt>"' in " ".join(lines[n:n + 2]), \
                    f'§D : fan_in.py {sub} sans --root "<wt>" (mode worktree)'
    for n, line in enumerate(lines):   # WARN-2 : le worktree du builder n'est jamais passe a --root
        assert not re.search(r'--root "<builder-wt>"(?! --guard)', line) or "tree-verify --base" in line, \
            f"§D ligne {n}: <builder-wt> passe a --root hors tree-verify --base"
    assert "<builder-wt>" in sec and "<worktree>" not in sec, "§D : placeholders <wt> / <builder-wt> incoherents"
    i_batch = sec.index("--batch-worktrees")
    i_merge = sec.index("merge-reports", pos["cleanup"])
    assert i_batch < pos["apply"] < pos["cleanup"] < i_merge, "ordre du fan-in (§D --auto)"


def _t_doc_worktree_state_root() -> None:
    root = Path(__file__).resolve().parents[1]
    docs = {n: root / p for n, p in (
        ("battle", "commands/battle.md"), ("builder", "agents/builder.md"),
        ("arch", "ARCHITECTURE.md"), ("skill", "skills/battle-workflow/SKILL.md"))}
    if not all(d.is_file() for d in docs.values()):
        print("SKIP: _t_doc_worktree_state_root (fichiers de doctrine absents, cache de plugin ?)",
              file=sys.stderr)
        return
    txt = {n: d.read_text(encoding="utf-8") for n, d in docs.items()}
    assert "--git-common-dir" in txt["battle"] and "absolute" in txt["battle"], "battle.md"
    assert "absolu" in txt["builder"], "builder.md"
    assert "--git-common-dir" in txt["arch"], "ARCHITECTURE.md"
    m = re.search(r"^## Guardrails\n(.*?)(?=^## )", txt["skill"], re.M | re.S)
    assert m and "worktree" in m.group(1), "SKILL.md Guardrails"


def _t_doc_isolated_report() -> None:
    root = Path(__file__).resolve().parents[1]
    docs = {n: root / p for n, p in (
        ("battle", "commands/battle.md"), ("builder", "agents/builder.md"),
        ("arch", "ARCHITECTURE.md"), ("skill", "skills/battle-workflow/SKILL.md"))}
    if not all(d.is_file() for d in docs.values()):
        print("SKIP: _t_doc_isolated_report (fichiers de doctrine absents, cache de plugin ?)",
              file=sys.stderr)
        return
    txt = {n: d.read_text(encoding="utf-8") for n, d in docs.items()}
    b = txt["builder"]
    assert "<<<BUILD-REPORT" in b and "<<<END BUILD-REPORT>>>" in b and "write_failures" in b, \
        "builder.md : marqueurs / write_failures"
    assert "entrée 6" in b.lower() and "y compris par" in b, "builder.md : lien entrée 6 / Bash"
    bm = txt["battle"]
    sec = bm[bm.index("**Mode — `--auto`.**"):bm.index("After build (either mode)")]
    i_mark = sec.index("<<<BUILD-REPORT")
    i_ver = sec.index("artifact_check.py\" verify")
    i_base = sec.index("tree-verify --base")
    i_merge = sec.index("merge-reports", sec.index("fan_in.py\" cleanup"))
    assert i_mark < i_base and i_ver < i_base and i_ver < i_merge, "ordre rapport / tree-verify (§D)"
    assert "An isolated builder writes its own `build-report-<slice_id>.md` at that absolute" not in bm, \
        "ancienne règle du builder isolé encore présente"
    assert "neither `battle.json` nor `.legion/active-battle`" in sec, "fenêtre S1 non documentée"
    assert "rapport absent du retour du builder" in sec, "repli rapport absent"
    assert "write_failures" in txt["skill"] and "write_failures" in txt["arch"], "SKILL/ARCHITECTURE"


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


def _t_doc_worktree_mode() -> None:   # GH#152 : doctrine du mode worktree
    root = Path(__file__).resolve().parents[1]
    battle_md, retro_md = root / "commands/battle.md", root / "commands/retro.md"
    if not (battle_md.is_file() and retro_md.is_file()):
        print("SKIP: _t_doc_worktree_mode (fichiers de doctrine absents, cache de plugin ?)",
              file=sys.stderr)
        return
    battle, retro = battle_md.read_text(encoding="utf-8"), retro_md.read_text(encoding="utf-8")
    for needle in ("battle_worktree.py\" create", "battle_worktree.py\" close-check",
                   "battle_worktree.py\" close --battle", "battle_worktree.py\" where",
                   "--in-place", "set-meta --worktree-path", "ExitWorktree",
                   "close [<battle-id>]", "## §J", "worktree_branch", "reason",
                   "Worktree mode — working from the main checkout", "pointer_taken"):
        assert needle in battle, f"{needle!r} absent de battle.md"
    # GH#166 : la session reste dans le principal ; EnterWorktree seulement dans la ligne de reprise
    rec_a = battle.index("**Recovery line.**")
    rec_b = battle.index("From now on the session works **from the main checkout**")
    assert rec_a < rec_b, "ligne de recuperation mal placee"
    assert "EnterWorktree" not in battle[:rec_a] + battle[rec_b:], \
        "EnterWorktree hors de la ligne de recuperation"
    assert "EnterWorktree" not in retro, "EnterWorktree dans retro.md"
    # §E : instantane et verification sur la racine du worktree ; §G : git -C
    e_sec = battle[battle.index("## §E — review / test gates"):battle.index("### Gate artifact delivery check")]
    assert e_sec.count('--root "<worktree>"') >= 2, '§E : --root "<worktree>" absent (snapshot et verify)'
    g_sec = battle[battle.index("## §G — deliver"):battle.index("## §H — address")]
    assert 'git -C "<worktree>"' in g_sec, "§G : git -C absent"
    assert 'base_freshness.py --repo "<worktree>"' in g_sec, "§G : base_freshness --repo absent"
    assert "--head <worktree.branch>" in g_sec, "§G : gh pr create --head absent"
    # toute mutation battle_state.py porte --battle (hors init, activate, validate)
    mut = r'battle_state\.py"? (transition|slice|set-meta|set-guard|set-slices|approve-plan|' \
          r'bump-autocorrect|invalidate|set-delivery|merge-reports|check-cascade|next-slice|close|abort)\b'
    for name, text in (("battle.md", battle), ("retro.md", retro)):
        for n, line in enumerate(text.splitlines(), 1):
            if re.search(mut, line):
                assert "--battle" in line, f"{name}:{n} : mutation battle_state.py sans --battle"
    assert "<state>" in battle and "<state>" in retro, "ancrage <state> absent"
    # GH#166 : l'en-tete ne doit pas inviter a muter sans --battle
    assert "the active battle\n> there" not in battle and "the active battle there" not in battle, \
        "en-tete : allusion a la battle active du principal"
    assert "every\n> mutation after `init` carries `--battle <id>`" in battle, "en-tete : --battle apres init absent"
    # aucun chemin `.legion/` relatif dans un bloc shell (ancrage sur <state>)
    for name, text in (("battle.md", battle), ("retro.md", retro)):
        for block in re.findall(r"```.*?```", text, re.S):
            assert not re.search(r'["\s]\.legion/', block), f"chemin .legion/ relatif dans un bloc de {name}"
    # §G.1 : pas de checkout en mode worktree ; checkout -b conserve sous --in-place
    g1 = battle[battle.index("1. **Branch name.**"):battle.index("2. **Commit**")]
    wt_part = g1[:g1.index("**`--in-place`**")]
    assert "no `checkout`" in wt_part, "§G.1 worktree : interdiction du checkout absente"
    assert "git checkout" not in wt_part, "§G.1 worktree : un checkout est present"
    assert "git checkout -b <me>/<token>" in g1, "§G.1 --in-place : checkout -b perdu"
    # §D : lot --auto sequentiel en mode worktree
    d = battle[battle.index("**Mode — `--auto`.**"):battle.index("After build (either mode)")]
    # GH#157 : le lot parallele marche aussi en mode worktree (plus d'exception sequentielle)
    assert "Exception — worktree mode" not in d, "§D : l'exception worktree mode est encore presente"
    assert "not available in worktree mode" not in d, "§D : lot parallele encore declare indisponible"
    assert "integration tree" in d and "--root \"<wt>\"" in d, "§D : arbre d'integration absent"
    # GH#159 : nettoyage d'une battle abandonnee (preuve unpushed, fetch --prune)
    i_sec = battle[battle.index("## §I"):battle.index("## §J")]
    assert "git worktree remove" not in i_sec, "§I : git worktree remove a la main de retour"
    j_sec = battle[battle.index("## §J"):battle.index("## Guardrails")]
    assert "unpushed" in j_sec and "--prune" in j_sec, "§J : unpushed / --prune absents"
    # options validees par liste fermee
    assert "closed list" in battle and "`--in-place` (no value" in battle, "liste fermee de start"


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


def _gh(state="OPEN", rollup=()):
    return {"state": state, "mergedAt": None, "statusCheckRollup": list(rollup), "url": "https://x.test/pr/1"}


def _cr(status="COMPLETED", conclusion="SUCCESS", name="build", typename=True, url="https://x.test/run/1"):
    el = {"status": status, "conclusion": conclusion, "name": name, "detailsUrl": url}
    if typename:
        el["__typename"] = "CheckRun"
    return el


def _sc(state="SUCCESS", typename=True):
    el = {"state": state, "context": "ci/ext", "targetUrl": "https://ext.test/1"}
    if typename:
        el["__typename"] = "StatusContext"
    return el


def _ci(*els) -> str:
    return pr_status_from_gh(_gh(rollup=els))["ci"]


def _t_pr_status_states() -> None:
    for gh_state, want in (("OPEN", "open"), ("MERGED", "merged"), ("CLOSED", "closed")):
        r = pr_status_from_gh(_gh(gh_state))
        assert r == {"pr_state": want, "ci": "none", "failing": []}, r


def _t_pr_status_ci_none() -> None:
    for rollup in ([], None):
        obj = _gh()
        obj["statusCheckRollup"] = rollup
        r = pr_status_from_gh(obj)
        assert r["ci"] == "none" and r["failing"] == [], r


def _t_pr_status_checkrun() -> None:
    for c in ("SUCCESS", "NEUTRAL", "SKIPPED"):
        assert _ci(_cr(conclusion=c)) == "pass", c
    for c in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"):
        r = pr_status_from_gh(_gh(rollup=[_cr(conclusion=c, name="unit")]))
        assert r["ci"] == "fail" and r["failing"] == [{"name": "unit", "url": "https://x.test/run/1"}], (c, r)
    for st in ("IN_PROGRESS", "QUEUED", "WAITING", "PENDING", "REQUESTED"):
        assert _ci(_cr(status=st, conclusion="")) == "pending", st
        assert _ci(_cr(status=st, conclusion=None)) == "pending", st


def _t_pr_status_statuscontext() -> None:
    assert _ci(_sc("SUCCESS")) == "pass"
    for st in ("FAILURE", "ERROR"):
        r = pr_status_from_gh(_gh(rollup=[_sc(st)]))
        assert r["ci"] == "fail" and r["failing"] == [{"name": "ci/ext", "url": "https://ext.test/1"}], r
    for st in ("PENDING", "EXPECTED"):
        assert _ci(_sc(st)) == "pending", st


def _t_pr_status_typename_optional() -> None:
    assert _ci(_cr(conclusion="SUCCESS", typename=False)) == "pass"
    assert _ci(_cr(conclusion="FAILURE", typename=False)) == "fail"
    assert _ci(_cr(status="QUEUED", conclusion="", typename=False)) == "pending"
    assert _ci(_sc("SUCCESS", typename=False)) == "pass"
    assert _ci(_sc("ERROR", typename=False)) == "fail"
    assert _ci(_sc("PENDING", typename=False)) == "pending"


def _t_pr_status_precedence() -> None:
    assert _ci(_cr(conclusion="SUCCESS"), _cr(status="IN_PROGRESS", conclusion=""),
               _cr(conclusion="FAILURE")) == "fail"
    assert _ci(_cr(conclusion="SUCCESS"), _cr(status="IN_PROGRESS", conclusion="")) == "pending"
    assert _ci(_cr(conclusion="SUCCESS"), _sc("FAILURE")) == "fail"
    assert pr_status_from_gh(_gh(rollup=[_cr(conclusion="SUCCESS"), _sc("FAILURE")]))["failing"] == [
        {"name": "ci/ext", "url": "https://ext.test/1"}]


def _t_ci_fails() -> None:
    got = ci_fails([{"name": "unit"}, {"name": "build (ubuntu, 3.12)"}, {"name": "build (ubuntu, 3.12)"},
                    {"name": "it's \"bad\"; rm -rf /"}, {"name": None}, "pas un dict"])
    targets = [f["target"] for f in got]
    assert targets == ["ci:unit", "ci:build_ubuntu_3.12", "ci:build_ubuntu_3.12#2",
                       "ci:it_s_bad_rm_-rf", "ci:None", "ci:None#2"], targets
    assert all(f["dimension"] == "CI" for f in got)
    assert all(re.fullmatch(r"ci:[A-Za-z0-9._#-]+", t) for t in targets), targets
    assert ci_fails([]) == [] and ci_fails(None) == []
    assert len(ci_fails([{"name": "x" * 200}])[0]["target"]) == 3 + 80


def _t_pr_status_stale() -> None:
    assert _ci(_cr(conclusion="STALE")) == "pending"


def _t_pr_status_invalid() -> None:
    no_rollup = _gh()
    del no_rollup["statusCheckRollup"]
    no_state = _gh()
    del no_state["state"]
    bad = (
        [], "x", None, no_state, _gh("DRAFT"), no_rollup,
        {**_gh(), "statusCheckRollup": {}}, {**_gh(), "statusCheckRollup": "x"},
        _gh(rollup=["chaine"]), _gh(rollup=[{"name": "x"}]),
        _gh(rollup=[_cr(conclusion="FOO")]), _gh(rollup=[_cr(status="BAR", conclusion="")]),
        _gh(rollup=[_cr(conclusion=None)]), _gh(rollup=[_cr(conclusion="")]),
        _gh(rollup=[_sc("BOOM")]), _gh(rollup=[{"__typename": "Autre", "state": "SUCCESS"}]),
    )
    for obj in bad:
        try:
            r = pr_status_from_gh(obj)
        except ValueError:
            continue
        raise AssertionError(f"ValueError attendue : {obj!r} -> {r!r}")


def _t_validate_delivery() -> None:
    def w(delivery) -> list:
        b = _fx()
        b["delivery"] = delivery
        errors, warnings = validate(b)
        assert errors == [], errors
        return warnings
    assert w({"pr_url": None}) == []
    assert w({"pr_url": "u", "pr_state": "open", "ci": "fail", "checked_at": "2026-01-01T00:00:00+00:00"}) == []
    assert w({"pr_url": "u", "pr_state": "open", "ci": None, "checked_at": None}) == []
    assert any("pr_state" in x for x in w({"pr_state": "foo"}))
    assert any("ci" in x for x in w({"ci": "red"}))
    assert any("checked_at" in x for x in w({"checked_at": 5}))
    assert any("delivery" in x for x in w("chaine"))


def _t_glob_match() -> None:
    m = glob_match
    assert m("src/Billing.Api/Foo.cs", ["src/Billing.Api/**"])
    assert m("tests/Bar.cs", ["tests/**"])
    assert not m("src/Other/Foo.cs", ["src/Billing.Api/**"])
    assert m("a/b.cs", ["a/*.cs"]) and not m("a/b/c.cs", ["a/*.cs"])
    assert m("a/xb", ["a/?b"]) and not m("a/x/b", ["a/?b"])
    assert m("x\\y.cs", ["x/*.cs"])                       # séparateur Windows
    assert m(".legion/battles/x/battle.json", [".legion/**"])
    assert m(".gitignore", [".gitignore"]) and not m("src/.gitignore", [".gitignore"])
    assert m("a.b", ["a.b"]) and not m("axb", ["a.b"])     # `.` littéral
    assert not m("x", []) and m("anything/deep/x", ["**"])


_CORE_TESTS = (
    _t_glob_match,
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
    _t_set_slices_replace_empty_refused, _t_subcommands_constant, _t_doc_subcommands,
    _t_slice_report_names, _t_merge_nominal, _t_merge_missing_blank, _t_merge_out_of_scope,
    _t_merge_titles, _t_merge_aggregated_and_invalid, _t_doc_profiles, _t_doc_pr_tracking, _t_doc_tree_integrity, _t_doc_fan_in, _t_doc_worktree_state_root, _t_doc_isolated_report, _t_doc_abort_stale, _t_doc_worktree_mode, _t_doc_concurrent, _t_doc_plugin_root_braces,
    _t_cascade_refused_during_replan, _t_cascade_legacy_no_approval_key,
    _t_replan_invalidates_in_progress_gate, _t_polish_keeps_in_progress_gate,
    _t_guard_of, _t_validate_guard, _t_is_aborted, _t_abort_core, _t_abort_refused_closed,
    _t_abort_refused_twice, _t_transition_refused_after_abort, _t_aborted_refusal,
    _t_validate_aborted,
    _t_pr_status_states, _t_pr_status_ci_none, _t_pr_status_checkrun,
    _t_pr_status_statuscontext, _t_pr_status_typename_optional, _t_pr_status_precedence,
    _t_pr_status_stale, _t_ci_fails, _t_pr_status_invalid, _t_validate_delivery,
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


_SHA_A = "a" * 40


def _wt_args(r: "_Repo", bid="b1", branch="me/152", base=_SHA_A, path=None) -> list:
    path = path or str(r.root / ".claude" / "worktrees" / bid)
    return ["set-meta", "--worktree-path", path, "--worktree-branch", branch, "--worktree-base", base]


def _t_set_meta_worktree() -> None:
    with _Repo() as r:
        r.init()
        legacy = r.load()
        assert "worktree" not in legacy and validate(legacy)[0] == []   # battle legacy valide
        res = r.ok(*_wt_args(r))
        assert res["updated"] == ["worktree"], res
        b = r.load()
        wt = b["worktree"]
        assert wt["path"] == str(r.root / ".claude" / "worktrees" / "b1") and wt["branch"] == "me/152"
        assert wt["base"] == _SHA_A and datetime.fromisoformat(wt["created_at"])
        assert validate(b)[0] == []
        b["worktree"] = None
        assert validate(b)[0] == []
        before = r.path().read_text(encoding="utf-8")
        assert "ensemble" in r.refused("set-meta", "--worktree-path", str(r.root))
        assert "ensemble" in r.refused("set-meta", "--worktree-branch", "me/1", "--worktree-base", _SHA_A)
        assert "worktree.path" in r.refused(*_wt_args(r, path=str(r.root / "autre" / "b1")))
        assert "worktree.path" in r.refused(*_wt_args(r, path=str(r.root / ".claude" / "worktrees" / "b2")))
        other = Path(r._td.name) / "ailleurs" / ".claude" / "worktrees" / "b1"
        assert "différent" in r.refused(*_wt_args(r, path=str(other)))
        for bad in ("", "-x", "a..b", "a b", "a/", "x.lock", "a@{b", "a~1"):
            assert "branch" in r.refused(*_wt_args(r, branch=bad)), bad
        assert "base" in r.refused(*_wt_args(r, base="abc"))
        assert "base" in r.refused(*_wt_args(r, base="A" * 40))
        assert r.path().read_text(encoding="utf-8") == before
    b = _fx()
    b["id"] = "b1"
    for wt in ("x", {"path": "/x/.claude/worktrees/b1"},
               {"path": "/x/y/b1", "branch": "me/1", "base": _SHA_A},
               {"path": "/x/.claude/worktrees/b1", "branch": "..", "base": _SHA_A},
               {"path": "/x/.claude/worktrees/b2", "branch": "me/1", "base": _SHA_A}):
        b["worktree"] = wt
        assert validate(b)[0], wt
    b["worktree"] = {"path": "/x/.claude/worktrees/b1", "branch": "me/1", "base": _SHA_A}
    assert validate(b)[0] == []


def _t_set_delivery_head_fields() -> None:
    assert "head_oid" not in pr_status_from_gh(_gh())
    st = pr_status_from_gh({**_gh(), "headRefName": "me/152", "headRefOid": _SHA_A})
    assert st["head_ref"] == "me/152" and st["head_oid"] == _SHA_A
    assert "head_ref" not in pr_status_from_gh({**_gh(), "headRefName": 5, "headRefOid": ""})
    with _Repo() as r:
        r.init()
        r.ok("set-delivery", "--pr-url", "https://x.test/pr/1")
        res = r.ok("set-delivery", "--pr-json", _write_gh(r, _gh()))   # sans head*
        d = r.load()["delivery"]
        assert d["head_ref"] is None and d["head_oid"] is None and "head_oid" not in res
        res = r.ok("set-delivery", "--pr-json",
                   _write_gh(r, {**_gh(), "headRefName": "me/152", "headRefOid": _SHA_A}))
        d = r.load()["delivery"]
        assert d["head_ref"] == "me/152" and d["head_oid"] == _SHA_A and res["head_oid"] == _SHA_A
        r.ok("set-delivery", "--pr-url", "https://x.test/pr/2")   # nouvelle PR : remis à null
        d = r.load()["delivery"]
        assert d["head_ref"] is None and d["head_oid"] is None
    b = _fx()
    b["delivery"] = {"pr_url": "u", "head_oid": 5}
    assert any("head_oid" in x for x in validate(b)[1])


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


def _t_worktree_battle_of() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fx = _git_worktree_fixture(Path(tmp))
        if fx is None:
            print("SKIP: _t_worktree_battle_of (git absent ou inutilisable)", file=sys.stderr)
            return
        main, wt, wt_out = fx
        wts = main / ".claude" / "worktrees"

        def put(bid, wt_path, **extra):
            d = main / ".legion" / "battles" / bid
            d.mkdir(parents=True, exist_ok=True)
            data = {"id": bid, "phases": {}, "worktree": {"path": str(wt_path)}}
            data.update(extra)
            (d / "battle.json").write_text(json.dumps(data), encoding="utf-8")

        put("w1", wt)
        got = worktree_battle_of(main, wt / "src" / "x.py")
        assert got is not None and got[0] == "w1" and got[1]["id"] == "w1", got
        assert worktree_battle_of(main, wt)[0] == "w1"
        assert worktree_battle_of(str(main), str(wt / "a"))[0] == "w1"   # str accepté
        # None, sans exception
        assert worktree_battle_of(main, wts / "agent-x" / "f") is None          # pas de battle.json
        assert worktree_battle_of(main, main / "src" / "x.py") is None          # hors .claude/worktrees
        assert worktree_battle_of(main, wts) is None                            # racine seule
        assert worktree_battle_of(main, wts / "bad_id" / "f") is None           # id invalide
        assert worktree_battle_of(main, wt_out / "f") is None                   # worktree hors arbre
        put("w1", wt, aborted={"at": "t", "reason": "r"})
        assert worktree_battle_of(main, wt / "f") is None                       # abandonnée
        put("w1", wt, phases={"reflect": {"status": "done"}})
        assert worktree_battle_of(main, wt / "f") is None                       # close
        put("w1", wts / "autre")
        assert worktree_battle_of(main, wt / "f") is None                       # worktree.path différent
        put("w1", wt_out)
        assert worktree_battle_of(main, wt / "f") is None
        (main / ".legion" / "battles" / "w1" / "battle.json").write_text("[1]", encoding="utf-8")
        assert worktree_battle_of(main, wt / "f") is None                       # racine non-dict
        (main / ".legion" / "battles" / "w1" / "battle.json").write_text("{pas json", encoding="utf-8")
        assert worktree_battle_of(main, wt / "f") is None                       # illisible
        assert worktree_battle_of(None, None) is None
        # lien symbolique : un dossier de .claude/worktrees pointant ailleurs
        try:
            link = wts / "lnk"
            os.symlink(str(wt_out), str(link))
        except (OSError, NotImplementedError):
            return
        put("lnk", link)
        assert worktree_battle_of(main, link / "f") is None


def _t_activate_pointer_taken() -> None:
    with _Repo() as r:
        res = r.init("b1")
        assert res["previous_active"] is None and res["warnings"] == [], res
        res = r.init("b2")                                   # b1 vivante est pointée
        assert res["previous_active"] == "b1", res
        assert any(w.startswith("pointer_taken:") and "b1" in w and "b2" in w
                   for w in res["warnings"]), res
        pointer = r.root / ".legion" / "active-battle"
        assert pointer.read_text(encoding="utf-8") == "b2"
        res = r.ok("activate", "b1")                         # b2 vivante
        assert res["previous_active"] == "b2" and res["warnings"], res
        assert pointer.read_text(encoding="utf-8") == "b1"
        res = r.ok("activate", "b1")                         # même battle : pas d'avertissement
        assert res["previous_active"] is None and res["warnings"] == [], res
        r.ok("close", "--battle", "b2")                      # pointée b1, b2 close
        pointer.write_text("b2", encoding="utf-8")
        res = r.ok("activate", "b1")                         # pointée close : rien
        assert res["previous_active"] is None and res["warnings"] == [], res
        r.ok("abort", "--battle", "b1", "--reason", "x")
        pointer.write_text("b1", encoding="utf-8")
        r.init("b3")                                         # pointée abandonnée : rien
        pointer.write_text("", encoding="utf-8")
        res = r.ok("activate", "b3")                         # pointeur vide : rien
        assert res["previous_active"] is None and res["warnings"] == [], res


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


def _cli(tmp: str, *argv: str, cwd=None) -> tuple[int, dict]:
    import subprocess
    env = dict(os.environ, LEGION_FLEET=str(Path(tmp) / "fleet.json"))
    p = subprocess.run([sys.executable, str(Path(__file__).resolve()), *argv], env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=60,
                       cwd=str(cwd) if cwd else None)
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
        code, res = _cli(td, *g, "init", "b1", "--ticket", "GH#1", "--title", "T", "--profile", "feature", "--profile", "feature")
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
            reason = r.refused("init", bad, "--ticket", "T", "--title", "T", "--profile", "feature", "--profile", "feature")
            assert "invalide" in reason, (bad, reason)
        r.init("2026-09-29-GH-69")
        # lien symbolique sortant de .legion/battles/
        outside = r.root / "dehors"
        outside.mkdir()
        try:
            (r.root / ".legion" / "battles" / "lnk").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        reason = r.refused("init", "lnk", "--ticket", "T", "--title", "T", "--profile", "feature", "--profile", "feature")
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
        res = r.ok("init", "h1", "--ticket", "GH#1", "--title", "T", "--profile", "feature", "--profile", "hotfix")
        assert res["profile"] == "hotfix" and res["required_gates"] == ["lint", "reviewer", "test-engineer"]
        assert r.load("h1")["required_gates"] == ["lint", "reviewer", "test-engineer"]
        for prof in ("security", "spike"):
            res = r.ok("init", prof, "--ticket", "GH#1", "--title", "T", "--profile", "feature", "--profile", prof)
            assert res["required_gates"] == list(PROFILES[prof])
            b = r.load(prof)
            assert b["required_gates"] == list(PROFILES[prof]) and "security" not in b["phases"]
        res = r.init("f1")
        assert res["required_gates"] == list(DEFAULT_REQUIRED_GATES)
        assert r.load("f1")["run"]["mode"] == "autonomous"
        r.ok("init", "h2", "--ticket", "GH#1", "--title", "T", "--profile", "feature", "--profile", "hotfix",
             "--required-gates", "architect")
        assert r.load("h2")["required_gates"] == ["architect"]
        ptr = (r.root / ".legion" / "active-battle").read_text(encoding="utf-8")
        reason = r.refused("init", "bad", "--ticket", "GH#1", "--title", "T", "--profile", "feature", "--profile", "bugfix")
        assert all(p in reason for p in PROFILES) and "bugfix" in reason
        assert not (r.root / ".legion" / "battles" / "bad").exists()
        assert (r.root / ".legion" / "active-battle").read_text(encoding="utf-8") == ptr
        assert "invalide" in r.refused("init", "a/b", "--ticket", "T", "--title", "T", "--profile", "feature",
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
        r.ok("init", "hf", "--ticket", "GH#1", "--title", "T", "--profile", "feature", "--profile", "hotfix")
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


def _t_touch_files() -> None:   # GH#115
    def prep(r):
        r.init("b1")
        r.ok("transition", "think", "done")
        r.ok("transition", "plan", "in_progress")
        r.ok("transition", "plan", "done", "--verdict", "accept")
        r.ok("set-slices", "--replace", "s1")
        r.ok("approve-plan")
        r.ok("transition", "build", "in_progress")

    with _Repo() as r:
        prep(r)
        before = r.load()["required_gates"]
        res = r.ok("touch-files", "--files", "src/util.py", "--battle", "b1")   # neutre
        assert res["security_added"] is False and res["warnings"] == []
        b = r.load()
        assert b["required_gates"] == before and "security_auto" not in b.get("run", {})
        res = r.ok("touch-files", "--files", "src/util.py", "src/Auth/Login.cs", "--battle", "b1")
        b = r.load()
        assert b["required_gates"] == list(DEFAULT_REQUIRED_GATES) + ["security"]
        assert b["run"]["security_auto"]["files"] == ["src/Auth/Login.cs"]
        assert res["security_added"] is True and len(res["warnings"]) == 1
        at = b["run"]["security_auto"]["at"]
        res = r.ok("touch-files", "--files", "x.env", "--battle", "b1")   # idempotent
        b = r.load()
        assert res["warnings"] == [] and res["security_added"] is False
        assert b["run"]["security_auto"]["at"] == at and b["required_gates"].count("security") == 1
        code, _ = r.run("touch-files", "--battle", "b1")   # --files requis
        assert code == 2
        r.ok("abort", "--battle", "b1")
        code, res = r.run("touch-files", "--files", "a.env", "--battle", "b1")
        assert code == 2 and "abandonnée" in res["reason"], res
    with _Repo() as r:
        prep(r)
        b = r.load()
        b["phases"]["reflect"] = {"status": "done"}
        r.path().write_text(json.dumps(b), encoding="utf-8")
        code, res = r.run("touch-files", "--files", "a.env", "--battle", "b1")
        assert code == 2 and "close" in res["reason"], res


def _write_gh(r: "_Repo", obj, name="pr-status.json", raw: str | None = None) -> str:
    f = r.root / name
    f.write_text(raw if raw is not None else json.dumps(obj), encoding="utf-8")
    return str(f)


def _red_gh() -> dict:
    return _gh("OPEN", [_cr(conclusion="FAILURE", name="unit"), _cr(conclusion="SUCCESS", name="lint")])


def _t_set_delivery_pr_json_cli() -> None:
    with _Repo() as r:
        r.init()
        r.ok("set-delivery", "--pr-url", "https://x.test/pr/1")
        before = r.load()
        assert before["delivery"] == {"pr_url": "https://x.test/pr/1", "pr_state": "open",
                                      "ci": None, "checked_at": None,
                                      "head_ref": None, "head_oid": None}
        n = len(r.calls)
        res = r.ok("set-delivery", "--pr-json", _write_gh(r, _red_gh()))
        assert res["pr_state"] == "open" and res["ci"] == "fail"
        assert res["failing"] == [{"name": "unit", "url": "https://x.test/run/1"}]
        assert res["ci_fails"] == [{"target": "ci:unit", "dimension": "CI"}]
        after = r.load()
        d = after["delivery"]
        assert d["pr_state"] == "open" and d["ci"] == "fail" and d["pr_url"] == "https://x.test/pr/1"
        assert datetime.fromisoformat(d["checked_at"]) and "failing" not in d
        assert after["phases"] == before["phases"] and "failing" not in after
        assert len(r.calls) == n + 1


def _t_set_delivery_pr_json_requires_url() -> None:
    with _Repo() as r:
        r.init()
        before = r.path().read_text(encoding="utf-8")
        assert "pr_url" in r.refused("set-delivery", "--pr-json", _write_gh(r, _gh()))
        assert r.path().read_text(encoding="utf-8") == before


def _t_set_delivery_pr_json_bad_input() -> None:
    with _Repo() as r:
        r.init()
        r.ok("set-delivery", "--pr-url", "https://x.test/pr/1")
        before = r.path().read_text(encoding="utf-8")
        r.refused("set-delivery", "--pr-json", str(r.root / "absent.json"))
        r.refused("set-delivery", "--pr-json", _write_gh(r, None, raw=""))
        r.refused("set-delivery", "--pr-json", _write_gh(r, None, raw="{ pas du json"))
        r.refused("set-delivery", "--pr-json", _write_gh(r, None, raw="[" * 100000))
        r.refused("set-delivery", "--pr-json", _write_gh(r, {"state": "DRAFT", "statusCheckRollup": []}))
        r.refused("set-delivery", "--pr-json", str(r.root))   # un dossier
        assert r.path().read_text(encoding="utf-8") == before


def _t_set_delivery_url_mismatch() -> None:
    with _Repo() as r:
        r.init()
        r.ok("set-delivery", "--pr-url", "https://x.test/pr/1")
        before = r.path().read_text(encoding="utf-8")
        other = {**_gh(), "url": "https://x.test/pr/2"}
        assert "pr_url" in r.refused("set-delivery", "--pr-json", _write_gh(r, other))
        assert r.path().read_text(encoding="utf-8") == before
        no_url = _gh()
        del no_url["url"]
        r.ok("set-delivery", "--pr-json", _write_gh(r, no_url))
        assert r.load()["delivery"]["pr_state"] == "open"


def _t_set_delivery_options_exclusive() -> None:
    with _Repo() as r:
        r.init()
        f = _write_gh(r, _gh())
        r.refused("set-delivery", "--pr-url", "https://x.test/pr/1", "--pr-json", f)
        r.refused("set-delivery")


def _t_set_delivery_pr_url_resets() -> None:
    with _Repo() as r:
        r.init()
        r.ok("set-delivery", "--pr-url", "https://x.test/pr/1")
        merged = _gh("MERGED", [_cr(conclusion="SUCCESS")])
        r.ok("set-delivery", "--pr-json", _write_gh(r, merged))
        d = r.load()["delivery"]
        assert d["pr_state"] == "merged" and d["ci"] == "pass" and d["checked_at"]
        r.ok("set-delivery", "--pr-url", "https://x.test/pr/2")
        d = r.load()["delivery"]
        assert d == {"pr_url": "https://x.test/pr/2", "pr_state": "open", "ci": None,
                     "checked_at": None, "head_ref": None, "head_oid": None}, d


def _t_set_delivery_aborted() -> None:
    with _Repo() as r:
        r.init()
        r.ok("set-delivery", "--pr-url", "https://x.test/pr/1")
        r.ok("abort")
        assert "abandonnée" in r.refused("set-delivery", "--battle", "b1", "--pr-json",
                                         _write_gh(r, _gh()))



def _mr_setup(r: "_Repo", ids, reports=None) -> Path:
    """Battle `b1` avec `slices` posées à la main (hors périmètre du test) + rapports par slice."""
    r.init()
    b = r.load()
    b["slices"] = [{"id": i, "status": "done"} for i in ids]
    r.path().write_text(json.dumps(b), encoding="utf-8")
    bdir = r.path().parent
    for i, text in (reports or {}).items():
        (bdir / f"build-report-{i}.md").write_text(text, encoding="utf-8")
    return bdir


def _t_merge_reports_cli() -> None:               # M3 (CLI), M4, M9, M12, M13
    with _Repo() as r:
        ids = ["slice-2", "slice-1"]
        bdir = _mr_setup(r, ids, {"slice-1": _rep("slice-1")})
        (bdir / "build-report.md").write_text("ANCIEN", encoding="utf-8")
        before = r.path().read_bytes()
        n = len(r.calls)
        code, res = r.run("merge-reports")
        assert code == 2 and res["ok"] is False and res["missing"] == ["slice-2"], res
        assert (bdir / "build-report.md").read_bytes() == b"ANCIEN"
        (bdir / "build-report-slice-2.md").write_text(_rep("slice-2", oos="Opp"), encoding="utf-8")
        (bdir / "build-report-old.md").write_text("OLD-CONTENT", encoding="utf-8")
        res = r.ok("merge-reports")
        assert res["merged"] == ids and res["path"] == str(bdir / "build-report.md"), res
        assert len(res["warnings"]) == 1 and "extra" in res["warnings"][0] and "old" in res["warnings"][0]
        out = (bdir / "build-report.md").read_text(encoding="utf-8")
        assert out.index("## slice-2") < out.index("## slice-1") and "OLD-CONTENT" not in out
        assert "### Opp" in out
        first = (bdir / "build-report.md").read_bytes()
        r.ok("merge-reports")
        assert (bdir / "build-report.md").read_bytes() == first           # M12
        assert r.path().read_bytes() == before and len(r.calls) == n      # M13
        assert not list(bdir.glob("*.tmp"))


def _t_merge_reports_aggregated_cli() -> None:    # M10, M11
    with _Repo() as r:
        bdir = _mr_setup(r, [])
        assert "build-report" in r.refused("merge-reports")
        (bdir / "build-report.md").write_text("  \n", encoding="utf-8")
        r.refused("merge-reports")
        (bdir / "build-report.md").write_text("# Build report\n## x\n", encoding="utf-8")
        res = r.ok("merge-reports")
        assert res["aggregated"] is True and res["merged"] == []
        assert (bdir / "build-report.md").read_text(encoding="utf-8") == "# Build report\n## x\n"
    with _Repo() as r:   # id invalide édité à la main
        _mr_setup(r, ["../x"])
        assert "invalide" in r.refused("merge-reports")
        assert not (r.root / ".legion" / "x").exists()


def _t_merge_reports_aborted_cli() -> None:       # M14
    with _Repo() as r:
        bdir = _mr_setup(r, ["slice-1"], {"slice-1": _rep("slice-1")})
        r.ok("abort")
        assert "abandonnée" in r.refused("merge-reports", "--battle", "b1")
        assert not (bdir / "build-report.md").exists()


def _t_merge_reports_worktree() -> None:          # M15
    def body(main, wt, wt_out):
        tmp = str(main.parent)
        (main / ".legion" / "active-battle").unlink()
        _seed_battle(tmp, main)
        bj = main / ".legion" / "battles" / "B" / "battle.json"
        b = json.loads(bj.read_text(encoding="utf-8"))
        b["slices"] = [{"id": "slice-1", "status": "done"}]
        bj.write_text(json.dumps(b), encoding="utf-8")
        (bj.parent / "build-report-slice-1.md").write_text(_rep("slice-1"), encoding="utf-8")
        code, res = _cli(tmp, "merge-reports", cwd=wt)
        assert code == 0 and res["merged"] == ["slice-1"], res
        assert (bj.parent / "build-report.md").is_file()
        assert not (wt / ".legion").exists()
    _with_fixture("_t_merge_reports_worktree", body)


def _t_merge_reports_subcommand() -> None:        # M16
    assert "merge-reports" in SUBCOMMANDS
    parser = _build_parser()
    assert "merge-reports" in parser.format_help()
    assert parser.parse_args(["merge-reports"]).cmd == "merge-reports"


_INTEGRATION_TESTS = (
    _t_security_hits, _t_security_auto_slice, _t_touch_files,
    _t_init_profiles, _t_set_meta_profile, _t_set_meta_worktree, _t_set_delivery_head_fields,
    _t_validate_profile, _t_hotfix_e2e,
    _t_set_guard, _t_set_meta, _t_init, _t_activate_close, _t_activate_pointer_taken, _t_atomic_and_corrupt,
    _t_fleet_sync_called, _t_phases_cs, _t_cli_exit_codes, _t_import_no_side_effect,
    _t_id_whitelist, _t_atomic_keeps_mode, _t_fleet_sync_explicit_path,
    _t_bump_build_failure_keeps_fails, _t_invalidate_cli, _t_slices_cli,
    _t_check_cascade_cli, _t_set_slices_replace_cli, _t_replan_invalidates_cli,
    _t_set_slices_replace_empty_cli, _t_active_battle_id, _t_load_active_battle,
    _t_active_reader_after_close, _t_set_guard_repair, _t_abort_cli,
    _t_commands_refused_after_abort_cli,
    _t_set_delivery_pr_json_cli, _t_set_delivery_pr_json_requires_url,
    _t_set_delivery_pr_json_bad_input, _t_set_delivery_url_mismatch,
    _t_set_delivery_options_exclusive, _t_set_delivery_pr_url_resets, _t_set_delivery_aborted,
    _t_merge_reports_cli, _t_merge_reports_aggregated_cli, _t_merge_reports_aborted_cli,
    _t_merge_reports_subcommand,
)


# --- resolve_state_root (GH#68) -----------------------------------------------------------

def _git_worktree_fixture(base: Path):
    """`(main, wt, wt_out)` : dépôt principal (battle `B` active), worktree dans l'arbre
    (`.claude/worktrees/w1`) et worktree hors arbre ; `None` si git est absent ou échoue.
    Environnement git isolé (pas de config globale/système)."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    main, wt, wt_out = base / "main", base / "main" / ".claude" / "worktrees" / "w1", base / "wt-out"
    ident = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]

    def git(*args, cwd):
        subprocess.run(["git", *ident, *args], cwd=str(cwd), env=env, check=True,
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    try:
        main.mkdir(parents=True)
        git("-c", "init.defaultBranch=main", "init", cwd=main)
        (main / ".gitignore").write_text(".legion/\n.claude/\n", encoding="utf-8")
        git("add", ".gitignore", cwd=main)
        git("commit", "-m", "init", cwd=main)
        git("worktree", "add", "--detach", str(wt), cwd=main)
        git("worktree", "add", "--detach", str(wt_out), cwd=main)
    except (OSError, subprocess.SubprocessError):
        return None
    _write_pointer(main, "B")
    return main, wt, wt_out


def _rp(p) -> Path:
    return Path(os.path.realpath(p))


def _with_fixture(name: str, body) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fx = _git_worktree_fixture(Path(tmp))
        if fx is None:
            print(f"SKIP: {name} (git absent ou inutilisable)", file=sys.stderr)
            return
        body(*fx)


def _t_resolve_root_fast_path() -> None:          # R1
    def body(main, wt, wt_out):
        assert resolve_state_root(main) == main
    _with_fixture("_t_resolve_root_fast_path", body)


def _t_resolve_root_subdir() -> None:             # R2
    def body(main, wt, wt_out):
        (main / "sub").mkdir()
        assert _rp(resolve_state_root(main / "sub")) == _rp(main)
    _with_fixture("_t_resolve_root_subdir", body)


def _t_resolve_root_subdir_worktree() -> None:    # R2b, R2c, R2d
    def body(main, wt, wt_out):
        (main / "sub").mkdir()
        (wt / "sub").mkdir()
        assert _rp(resolve_state_root(wt / "sub")) == _rp(main)             # R2b
        assert _rp(main_repo_root(main / "sub")) == _rp(main)               # R2d
        assert _rp(main_repo_root(wt / "sub")) == _rp(main)
        (main / ".legion" / "active-battle").write_text("", encoding="utf-8")
        got = resolve_state_root(wt / "sub")                                # R2c
        assert _rp(got) == _rp(wt) == _rp(resolve_state_root(wt)), got
    _with_fixture("_t_resolve_root_subdir_worktree", body)


def _t_resolve_root_worktree() -> None:           # R3, R4
    def body(main, wt, wt_out):
        for w in (wt, wt_out):
            got = resolve_state_root(w)
            assert _rp(got) == _rp(main), (w, got)
        res = load_active_battle(resolve_state_root(wt))
        assert active_battle_id(resolve_state_root(wt)) == "B"
        assert res is None or res[0] == "B"
    _with_fixture("_t_resolve_root_worktree", body)


def _t_resolve_root_no_main_battle() -> None:     # R5
    def body(main, wt, wt_out):
        (main / ".legion" / "active-battle").write_text("", encoding="utf-8")
        assert resolve_state_root(wt) == wt
        (main / ".legion" / "active-battle").unlink()
        assert resolve_state_root(wt) == wt
    _with_fixture("_t_resolve_root_no_main_battle", body)


def _t_resolve_root_not_git() -> None:            # R6
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        assert resolve_state_root(d) == d
        ghost = d / "absent"
        assert resolve_state_root(ghost) == ghost


def _t_resolve_root_git_failures() -> None:       # R7
    import subprocess as _sp
    real = _sp.run

    def boom(exc):
        def fake(*a, **k):
            raise exc
        return fake

    def bad_code(*a, **k):
        return _sp.CompletedProcess(a, 128, "", "fatal")

    def short(*a, **k):
        return _sp.CompletedProcess(a, 0, "only-one\n", "")
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        try:
            for fake in (boom(FileNotFoundError("git")),
                         boom(_sp.TimeoutExpired("git", 2)), bad_code, short):
                _sp.run = fake
                assert resolve_state_root(d) == d
        finally:
            _sp.run = real


def _t_pick_state_root_pure() -> None:            # R8
    nominal = _pick_state_root(Path("/r/wt"), "/r/main/.git/worktrees/wt", "/r/main/.git", True)
    assert nominal == Path(os.path.realpath("/r/main"))
    rel = _pick_state_root(Path("/r/main/.claude/worktrees/w1"),
                           "/r/main/.git/worktrees/w1", "../../../.git", True)
    assert rel == Path(os.path.realpath("/r/main")), rel
    wt = Path("/r/wt")
    assert _pick_state_root(wt, "/r/main/.git", "/r/main/.git", True) == wt      # sous-module / principal
    assert _pick_state_root(wt, "/r/x.git/worktrees/w", "/r/x.git", True) == wt  # bare
    assert _pick_state_root(wt, "/r/main/.git/worktrees/wt", "/r/main/.git", False) == wt


def _t_resolve_root_ignores_git_dir_env() -> None:  # R9
    def body(main, wt, wt_out):
        old = os.environ.get("GIT_DIR")
        os.environ["GIT_DIR"] = str(main / "nope")
        try:
            assert _rp(resolve_state_root(wt)) == _rp(main)
        finally:
            if old is None:
                os.environ.pop("GIT_DIR", None)
            else:
                os.environ["GIT_DIR"] = old
    _with_fixture("_t_resolve_root_ignores_git_dir_env", body)


# --- Battle par session (GH#170) ----------------------------------------------------------

def _sess_files(root: Path) -> list[str]:
    d = root / ".legion" / "sessions"
    return sorted(f.name for f in d.glob("*.json")) if d.is_dir() else []


def _t_session_keys() -> None:
    assert session_keys({"session_id": "abc-1_X"}) == ["abc-1_X"]
    base = "/h/.claude/projects/p"
    assert session_keys({"transcript_path": f"{base}/sidM.jsonl"}) == ["sidM"]
    sub = {"transcript_path": f"{base}/sidP/subagents/agent-a1.jsonl"}
    assert session_keys(sub) == ["sidP"]
    both = {"session_id": "sidP", "agent_transcript_path": f"{base}/sidP/subagents/agent-a1.jsonl",
            "transcript_path": f"{base}/sidP.jsonl"}
    assert session_keys(both) == ["sidP"]                         # sans doublon
    assert session_keys({"session_id": "s1", "transcript_path": f"{base}/s2.jsonl"}) == ["s1", "s2"]
    assert session_keys({"transcript_path": "C:\\x\\sidW\\subagents\\agent-1.jsonl"}) == ["sidW"]
    for bad in ("../x", "", "a" * 200, "a/b", "a b", None, 7):
        assert session_keys({"session_id": bad}) == [], bad
    assert session_keys({"transcript_path": "/x/../.jsonl"}) == []
    assert session_keys({"transcript_path": "/x/a b.jsonl"}) == []
    assert session_keys({}) == [] and session_keys(None) == [] and session_keys("x") == []
    assert session_keys({"transcript_path": 5, "agent_transcript_path": ["x"]}) == []


def _t_bind_unbind() -> None:
    with _Repo() as r:
        r.init("a")
        r.init("b")
        bind_session(r.root, "k1", "a")
        bind_session(r.root, "k2", "b")
        assert _sess_files(r.root) == ["k1.json", "k2.json"]
        bind_session(r.root, "k3", "a")                      # H2 : la dernière activation l'emporte
        assert _sess_files(r.root) == ["k2.json", "k3.json"]
        data = json.loads((r.root / ".legion" / "sessions" / "k3.json").read_text(encoding="utf-8"))
        assert data["battle"] == "a" and data["bound_at"]
        assert not list((r.root / ".legion" / "sessions").glob("*.tmp"))
        r.ok("close", "--battle", "b")                       # close délie b
        assert _sess_files(r.root) == ["k3.json"]
        r.init("c")
        (r.root / ".legion" / "sessions" / "junk.json").write_text("{pas json", encoding="utf-8")
        (r.root / ".legion" / "sessions" / "ghost.json").write_text('{"battle": "nope"}', encoding="utf-8")
        bind_session(r.root, "k4", "c")                      # purge : illisible, battle morte
        assert _sess_files(r.root) == ["k3.json", "k4.json"]
        r.ok("abort", "--battle", "c", "--reason", "x")
        bind_session(r.root, "k5", "a")                      # c abandonnée : sa liaison est purgée
        assert _sess_files(r.root) == ["k5.json"]
        assert unbind_battle(r.root, "a") == 1 and unbind_battle(r.root, "a") == 0
        for bad in (("../x", "a"), ("", "a"), ("k", "../a"), ("k", "")):
            try:
                bind_session(r.root, *bad)
            except ValueError:
                pass
            else:
                raise AssertionError(bad)
        assert unbind_battle(r.root / "absent", "a") == 0


def _t_resolve_battle() -> None:
    with _Repo() as r:
        ka, kb, kc = {"session_id": "sidA"}, {"session_id": "sidB"}, {"session_id": "sidC"}
        r.init("a")
        r.init("b")                                          # pointeur sur b
        assert not (r.root / ".legion" / "sessions").exists()
        assert resolve_battle(r.root, ka)[0::2] == ("b", "pointer")           # pas de dossier
        bind_session(r.root, "sidA", "a")
        bind_session(r.root, "sidB", "b")
        got = resolve_battle(r.root, ka)
        assert (got[0], got[2]) == ("a", "session") and got[1]["id"] == "a", got
        assert resolve_battle(r.root, kb)[0::2] == ("b", "session")
        assert resolve_battle(r.root, kc) == (None, None, "foreign")          # pointeur b lié ailleurs
        assert resolve_battle(r.root, {})[0::2] == ("b", "pointer")           # sans clé : pointeur
        assert resolve_battle(r.root, None)[0::2] == ("b", "pointer")
        sub = {"transcript_path": "/x/sidA/subagents/agent-1.jsonl"}
        assert resolve_battle(r.root, sub)[0::2] == ("a", "session")
        r.ok("activate", "a")                                # pointeur a, lié à sidA
        assert resolve_battle(r.root, kc) == (None, None, "foreign")
        (r.root / ".legion" / "sessions" / "sidB.json").write_text("{pas json", encoding="utf-8")
        assert resolve_battle(r.root, kb) == (None, None, "foreign")          # liaison illisible : repli
        (r.root / ".legion" / "sessions" / "sidA.json").unlink()              # plus de liaison vers a
        assert resolve_battle(r.root, kc)[0::2] == ("a", "pointer")
        bind_session(r.root, "sidA", "a")
        r.ok("close", "--battle", "a")
        assert resolve_battle(r.root, ka) == (None, None, "none")             # pointeur vidé, liaison purgée
        r.root.joinpath(".legion", "sessions", "sidZ.json").write_text('{"battle": "a"}', encoding="utf-8")
        assert resolve_battle(r.root, {"session_id": "sidZ"})[2] == "none"    # battle close : ignorée
        assert resolve_battle(r.root / "absent", ka) == (None, None, "none")


def _t_state_root_sessions() -> None:
    def body(main, wt, wt_out):
        (main / ".legion" / "active-battle").write_text("", encoding="utf-8")
        assert resolve_state_root(wt) == wt                                   # rien de vivant
        bdir = main / ".legion" / "battles" / "A"
        bdir.mkdir(parents=True)
        (bdir / "battle.json").write_text(json.dumps({"id": "A", "phases": {}}), encoding="utf-8")
        bind_session(main, "sidA", "A")
        assert resolve_state_root(wt) == main                                 # liaison vivante
        (bdir / "battle.json").write_text(json.dumps({"id": "A", "phases": {}, "aborted": {"at": "t"}}),
                                          encoding="utf-8")
        assert resolve_state_root(wt) == wt                                   # liaison sur battle morte
    _with_fixture("_t_state_root_sessions", body)


def _t_session_status_cli() -> None:
    with _Repo() as r:
        r.init("a")
        res = r.ok("session-status", "--battle", "a")
        assert res["bound"] is False and res["keys"] == 0 and res["battle"] == "a", res
        bind_session(r.root, "k1", "a")
        res = r.ok("session-status", "--battle", "a")
        assert res["bound"] is True and res["keys"] == 1 and res["bound_at"], res
        assert r.ok("session-status")["battle"] == "a"                        # pointeur
        assert "invalide" in r.refused("session-status", "--battle", "../x")
        res = r.ok("close", "--battle", "a")
        assert res["unbound"] == 1 and res["pointer_cleared"] is True, res
        r.init("b")
        res = r.ok("abort", "--battle", "b", "--reason", "x")
        assert res["unbound"] == 0, res
        assert r.ok("session-status", "--battle", "b")["bound"] is False       # lecture seule sur abandonnée
        assert "aucune battle active" in r.refused("session-status")


def _t_probe() -> None:
    with _Repo() as r:
        payload = {"session_id": "secret-sid-123", "hook_event_name": "PreToolUse", "agent_type": "x:y",
                   "transcript_path": "/home/u/secret-sid-123.jsonl", "tool_input": {"command": "x"}}
        old = os.environ.pop("LEGION_HOOK_PROBE", None)
        try:
            probe(payload, r.root)
            assert not (r.root / ".legion" / "hook-probe.jsonl").exists()      # désactivée
            os.environ["LEGION_HOOK_PROBE"] = "1"
            probe(payload, r.root)
            probe(None, r.root)
            probe(payload, r.root / "absent" / "x")
        finally:
            os.environ.pop("LEGION_HOOK_PROBE", None)
            if old is not None:
                os.environ["LEGION_HOOK_PROBE"] = old
        text = (r.root / ".legion" / "hook-probe.jsonl").read_text(encoding="utf-8")
        assert "secret-sid-123" not in text and "/home/u" not in text, text
        rows = [json.loads(x) for x in text.splitlines()]
        assert rows[0]["keys"] == sorted(payload) and rows[0]["session_id_present"] is True
        assert rows[0]["transcript_kind"] == "main" and rows[0]["transcript_key_equals_session_id"] is True
        assert rows[0]["event"] == "PreToolUse" and rows[0]["agent_type"] == "x:y"
        (r.root / ".legion" / "hook-probe.jsonl").unlink()
        (r.root / ".legion" / "hook-probe").write_text("", encoding="utf-8")   # activation par fichier
        probe({"a": 1}, r.root)
        assert len((r.root / ".legion" / "hook-probe.jsonl").read_text(encoding="utf-8").splitlines()) == 1


_SESSION_TESTS = (
    _t_session_keys, _t_bind_unbind, _t_resolve_battle, _t_state_root_sessions,
    _t_session_status_cli, _t_probe,
)


_RESOLVE_ROOT_TESTS = (
    _t_worktree_battle_of, _t_resolve_root_fast_path, _t_resolve_root_subdir, _t_resolve_root_subdir_worktree,
    _t_resolve_root_worktree,
    _t_resolve_root_no_main_battle, _t_resolve_root_not_git, _t_resolve_root_git_failures,
    _t_pick_state_root_pure, _t_resolve_root_ignores_git_dir_env,
)


# --- CLI : racine d'état par défaut (GH#128) ----------------------------------------------

def _bj(root: Path, bid: str) -> dict:
    return json.loads((root / ".legion" / "battles" / bid / "battle.json").read_text(encoding="utf-8"))


def _seed_battle(tmp: str, main: Path, bid: str = "B") -> None:
    code, res = _cli(tmp, "--repo", str(main), "init", bid, "--ticket", "GH#1", "--title", "T", "--profile", "feature")
    assert code == 0, res


def _t_cli_worktree_transition() -> None:         # S1
    def body(main, wt, wt_out):
        tmp = str(main.parent)
        (main / ".legion" / "active-battle").unlink()
        _seed_battle(tmp, main)
        for w in (wt, wt_out):
            code, res = _cli(tmp, "transition", "think", "done", cwd=w)
            assert code == 0, res
            assert _bj(main, "B")["phases"]["think"]["status"] == "done"
            assert not (w / ".legion").exists(), w
    _with_fixture("_t_cli_worktree_transition", body)


def _t_cli_worktree_set_guard() -> None:          # S2
    def body(main, wt, wt_out):
        tmp = str(main.parent)
        (main / ".legion" / "active-battle").unlink()
        _seed_battle(tmp, main)
        code, res = _cli(tmp, "set-guard", "--allow", "src/**", cwd=wt)
        assert code == 0, res
        assert _bj(main, "B")["guard"]["allow"] == ["src/**"]
        assert not (wt / ".legion").exists()
    _with_fixture("_t_cli_worktree_set_guard", body)


def _t_cli_explicit_repo_wins() -> None:          # S3
    def body(main, wt, wt_out):
        tmp = str(main.parent)
        code, res = _cli(tmp, "--repo", str(wt), "init", "Y", "--ticket", "GH#2", "--title", "T", "--profile", "feature", cwd=wt)
        assert code == 0, res
        assert (wt / ".legion" / "battles" / "Y" / "battle.json").exists()
        assert not (main / ".legion" / "battles" / "Y").exists()
        _seed_battle(tmp, main)
        with tempfile.TemporaryDirectory() as out:
            code, res = _cli(tmp, "--repo", str(main), "validate", cwd=out)
            assert code == 0, res
    _with_fixture("_t_cli_explicit_repo_wins", body)


def _t_cli_init_from_worktree() -> None:          # S4
    def body(main, wt, wt_out):
        tmp = str(main.parent)
        (main / ".legion" / "active-battle").write_text("", encoding="utf-8")
        code, res = _cli(tmp, "init", "X", "--ticket", "GH#3", "--title", "T", "--profile", "feature", cwd=wt)
        assert code == 0, res
        assert _bj(main, "X")["repo"] == "main"
        assert active_battle_id(main) == "X"
        assert not (wt / ".legion").exists()
        code, res = _cli(tmp, "transition", "think", "done", cwd=wt)
        assert code == 0, res
        assert _bj(main, "X")["phases"]["think"]["status"] == "done"
    _with_fixture("_t_cli_init_from_worktree", body)


def _t_cli_init_switches_pointer() -> None:       # S5
    def body(main, wt, wt_out):
        tmp = str(main.parent)
        _seed_battle(tmp, main)
        assert active_battle_id(main) == "B"
        code, res = _cli(tmp, "init", "X", "--ticket", "GH#4", "--title", "T", "--profile", "feature", cwd=wt)
        assert code == 0, res
        assert (main / ".legion" / "battles" / "X" / "battle.json").exists()
        assert active_battle_id(main) == "X"
    _with_fixture("_t_cli_init_switches_pointer", body)


def _t_cli_activate_from_worktree() -> None:      # S6
    def body(main, wt, wt_out):
        tmp = str(main.parent)
        _seed_battle(tmp, main)
        (main / ".legion" / "active-battle").write_text("", encoding="utf-8")
        code, res = _cli(tmp, "activate", "B", cwd=wt)
        assert code == 0, res
        assert active_battle_id(main) == "B"
        assert not (wt / ".legion").exists()
    _with_fixture("_t_cli_activate_from_worktree", body)


def _t_cli_default_unchanged() -> None:           # S7
    def body(main, wt, wt_out):
        tmp = str(main.parent)
        code, res = _cli(tmp, "init", "M", "--ticket", "GH#5", "--title", "T", "--profile", "feature", cwd=main)
        assert code == 0, res
        assert (main / ".legion" / "battles" / "M" / "battle.json").exists()
        code, res = _cli(tmp, "transition", "think", "done", cwd=main)
        assert code == 0, res
        assert _bj(main, "M")["phases"]["think"]["status"] == "done"
        with tempfile.TemporaryDirectory() as out:
            code, res = _cli(tmp, "init", "N", "--ticket", "GH#6", "--title", "T", "--profile", "feature", cwd=out)
            assert code == 0, res
            assert (Path(out) / ".legion" / "battles" / "N" / "battle.json").exists()
    _with_fixture("_t_cli_default_unchanged", body)


def _t_cli_root_unit() -> None:                   # S8
    import subprocess as _sp
    ns = argparse.Namespace

    def body(main, wt, wt_out):
        (main / "sub").mkdir()
        assert _cli_root(ns(cmd="init", repo="/x/r"), wt) == Path("/x/r")
        assert _cli_root(ns(cmd="init", repo=""), main) == main
        assert _cli_root(ns(cmd="init"), main) == main
        assert _rp(_cli_root(ns(cmd="init"), main / "sub")) == _rp(main)   # GH#134
        assert _rp(_cli_root(ns(cmd="init"), wt)) == _rp(main)
        assert _rp(_cli_root(ns(cmd="activate"), wt)) == _rp(main)
        assert _rp(main_repo_root(wt_out)) == _rp(main)
        (main / ".legion" / "active-battle").write_text("", encoding="utf-8")
        assert _cli_root(ns(cmd="transition"), wt) == wt       # lecture : principal sans battle
        assert _rp(_cli_root(ns(cmd="init"), wt)) == _rp(main)  # init : toujours le principal
    _with_fixture("_t_cli_root_unit", body)

    def bad_code(*a, **k):
        return _sp.CompletedProcess(a, 128, "", "fatal")

    def missing(*a, **k):
        raise FileNotFoundError("git")
    real = _sp.run
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        assert _linked_main_root(d) is None
        try:
            for fake in (bad_code, missing):
                _sp.run = fake
                assert main_repo_root(d) == d
                assert _cli_root(ns(cmd="init"), d) == d
                assert _cli_root(ns(cmd="validate"), d) == d
        finally:
            _sp.run = real


def _t_cli_subdir_init() -> None:                 # T1 (GH#134)
    def body(main, wt, wt_out):
        tmp = str(main.parent)
        (main / "sub").mkdir()
        code, res = _cli(tmp, "init", "N", "--ticket", "GH#7", "--title", "T", "--profile", "feature", cwd=main / "sub")
        assert code == 0, res
        assert (main / ".legion" / "battles" / "N" / "battle.json").exists()
        assert not (main / "sub" / ".legion").exists()
        assert active_battle_id(main) == "N"
        assert _bj(main, "N")["repo"] == "main"
    _with_fixture("_t_cli_subdir_init", body)


def _t_cli_subdir_mutations() -> None:            # T2 (GH#134)
    def body(main, wt, wt_out):
        tmp = str(main.parent)
        (main / "sub").mkdir()
        _seed_battle(tmp, main)
        code, res = _cli(tmp, "transition", "think", "done", cwd=main / "sub")
        assert code == 0, res
        assert _bj(main, "B")["phases"]["think"]["status"] == "done"
        code, res = _cli(tmp, "set-guard", "--allow", "src/**", cwd=main / "sub")
        assert code == 0, res
        assert _bj(main, "B")["guard"]["allow"] == ["src/**"]
        assert not (main / "sub" / ".legion").exists()
    _with_fixture("_t_cli_subdir_mutations", body)


def _t_cli_worktree_subdir() -> None:             # T3 (GH#134)
    def body(main, wt, wt_out):
        tmp = str(main.parent)
        (wt / "sub").mkdir()
        _seed_battle(tmp, main)
        code, res = _cli(tmp, "transition", "think", "done", cwd=wt / "sub")
        assert code == 0, res
        assert _bj(main, "B")["phases"]["think"]["status"] == "done"
        assert not (wt / "sub" / ".legion").exists()
        assert not (wt / ".legion").exists()
    _with_fixture("_t_cli_worktree_subdir", body)


def _t_cli_subdir_unit() -> None:                 # T4-T6 (GH#134)
    import subprocess as _sp
    ns = argparse.Namespace

    def body(main, wt, wt_out):
        (main / "sub").mkdir()
        assert _rp(_cli_root(ns(cmd="init"), main / "sub")) == _rp(main)
        assert _rp(_cli_root(ns(cmd="validate"), main / "sub")) == _rp(main)
        assert _cli_root(ns(cmd="validate", repo="/x/r"), main / "sub") == Path("/x/r")
    _with_fixture("_t_cli_subdir_unit", body)

    def fail(code, out=""):
        return lambda *a, **k: _sp.CompletedProcess(a, code, out, "")

    def boom(exc):
        def fake(*a, **k):
            raise exc
        return fake
    real = _sp.run
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        try:
            for fake in (boom(FileNotFoundError("git")), boom(_sp.TimeoutExpired("git", 2)),
                         fail(128), fail(0, ""), fail(0, "/top\n/super\n")):
                _sp.run = fake
                assert _repo_toplevel(d) is None
                assert _cli_root(ns(cmd="validate"), d) == d
        finally:
            _sp.run = real
        assert _repo_toplevel(d) is None      # dossier non git


def _t_cli_help_repo_default() -> None:           # S9
    parser = _build_parser()
    helps = [parser.format_help()]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps.append(action.choices["validate"].format_help())
    for h in helps:
        assert "défaut : cwd" not in h, h
    assert "dépôt principal" in helps[-1], helps[-1]


_CLI_ROOT_TESTS = (
    _t_cli_worktree_transition, _t_cli_worktree_set_guard, _t_cli_explicit_repo_wins,
    _t_cli_init_from_worktree, _t_cli_init_switches_pointer, _t_cli_activate_from_worktree,
    _t_cli_default_unchanged, _t_cli_root_unit, _t_cli_help_repo_default,
    _t_merge_reports_worktree,
    _t_cli_subdir_init, _t_cli_subdir_mutations, _t_cli_worktree_subdir, _t_cli_subdir_unit,
)


def _self_test() -> int:
    failed = 0
    tests = _CORE_TESTS + _INTEGRATION_TESTS + _RESOLVE_ROOT_TESTS + _CLI_ROOT_TESTS + _SESSION_TESTS
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
