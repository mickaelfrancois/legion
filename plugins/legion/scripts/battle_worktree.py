"""Worktree d'une battle legion (GH#152, lot 1), version exécutable.

Une battle tourne par défaut dans un worktree dédié `<principal>/.claude/worktrees/<id>`, sur une
branche `<me>/<token>`. L'état `.legion/` reste dans le dépôt principal. Ce script fait les
opérations **git** de ce cycle de vie, de façon déterministe et testée. Il n'écrit **jamais**
`battle.json` ni rien sous `.legion/` (`battle_state.py` en est l'unique écrivain) et n'utilise
**jamais** le réseau : l'orchestrateur lance `git fetch` et `gh` avant `close-check` / `close`.
Il lit `battle.json` (bloc optionnel `worktree` {path, branch, base, created_at}, `delivery.pr_state`,
`delivery.head_ref`, `delivery.head_oid`, `phases.reflect`, `aborted`) et tolère l'absence de ces champs.

Sous-commandes (toutes partent du dépôt principal, déduit du cwd ; jamais du pointeur de battle
active) :

- `create --battle <id>` : crée le worktree et la branche sur `HEAD` du principal, puis recopie les
  fichiers locaux déclarés dans `.worktreeinclude` (syntaxe gitignore ; fichiers ignorés par git,
  réguliers, jamais de lien symbolique, rien sous `.legion/` ni `.claude/`, jamais d'écrasement).
  Refus (`reason`) : `aborted`, `dirty` (principal modifié ou fichiers non suivis non ignorés),
  `not_ignored` (`.legion/` ou `.claude/worktrees/<id>` non ignoré), `branch_exists`,
  `path_exists`, `no_commit`, `invalid`. Plus de refus `concurrent_battle` : les battles en parallèle
  sont permises ; le résultat porte `concurrent: [ids]` (autres battles avec worktree actif) et un
  `warnings` `in_place_live` si une autre battle vit dans le principal (sans worktree). Idempotent :
  worktree déjà conforme (bon chemin, bonne branche, `HEAD` == base) -> `created:false`.
- `where [--battle <id>]` : `{state_root, mode, worktree_path, branch, exists, inside,
  cwd_toplevel, current_branch, worktree_branch, main_branch, reason}`. En mode worktree, `ok`
  vaut vrai si le worktree existe, est enregistré et est sur `worktree.branch` (la session reste
  dans le principal : `inside`, `cwd_toplevel` et `current_branch` sont informatifs). `reason` :
  `missing`, `wrong_branch` ou `null` si `ok`. Toujours vrai en mode `in_place` ou `none`.
  Lecture seule.
- `close-check --battle <id>` : contrôles `pr_merged`, `contained`, `worktree_clean`, `not_inside`.
  `contained` : la branche locale est ancêtre de `origin/<default>`, **ou** la PR est mergée et
  `delivery.head_oid` == tip local (merge squash / rebase). Lecture seule.
- `close --battle <id>` : exige `phases.reflect` `done`, rejoue `close-check`, puis `git worktree
  remove` (sans `--force`), `git branch -D` (après la preuve `contained`), `git worktree prune`,
  puis `git merge --ff-only origin/<default>` dans le principal seulement s'il est sur `<default>`
  et propre (sinon `main_updated:false` + warning). Battle sans bloc `worktree` : même preuve,
  puis suppression de la branche locale (`checkout <default>` d'abord, arbre propre exigé).
  Idempotent.

Usage :
    python battle_worktree.py create --battle <id>
    python battle_worktree.py where [--battle <id>]
    python battle_worktree.py close-check --battle <id>
    python battle_worktree.py close --battle <id>
    python battle_worktree.py --self-test

Sortie : un objet JSON sur stdout. Codes de sortie : 0 = ok ; 2 = refus ou échec (`ok:false`,
`refused:true`, `reason`) ; 1 = erreur d'usage.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import artifact_check as ac  # noqa: E402  (options git imposées)
import battle_state as bs  # noqa: E402

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")
_MAX_LIST = 50


class _Refusal(Exception):
    def __init__(self, reason: str, detail: str = "", **extra) -> None:
        super().__init__(detail or reason)
        self.reason, self.detail, self.extra = reason, detail, extra


def _refused(exc: _Refusal) -> dict:
    return {"ok": False, "refused": True, "reason": exc.reason, "detail": exc.detail, **exc.extra}


# --- git -----------------------------------------------------------------------------------

def _run(root: str, *args: str) -> tuple[int, str, str]:
    env = {k: v for k, v in os.environ.items() if k not in bs._GIT_ENV_DROP}
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        p = subprocess.run(["git", "--no-optional-locks", *ac._GIT_FORCED, "-C", root, *args],
                           capture_output=True, env=env, stdin=subprocess.DEVNULL, timeout=120)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise _Refusal("git", f"git indisponible ({type(exc).__name__}: {exc})") from exc
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def _git(root: str, *args: str) -> str:
    rc, out, err = _run(root, *args)
    if rc != 0:
        msg = err.strip().splitlines()
        raise _Refusal("git", f"git {args[0]} a échoué : {msg[0] if msg else 'code ' + str(rc)}")
    return out


def _real(p: str) -> str:
    return os.path.realpath(p)


def _is_under(path: str, parent: str) -> bool:
    path, parent = _real(path), _real(parent)
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def _main_root(cwd: str) -> str:
    try:
        top = ac._toplevel(cwd)
    except ac._Refuse as exc:
        raise _Refusal("not_a_repo", str(exc)) from exc
    return _real(str(bs.main_repo_root(Path(top))))


def _status(root: str) -> tuple[list[str], list[str]]:
    """`(modified, untracked)` : `git status --porcelain -uall`, fichiers ignorés exclus."""
    out = _git(root, "status", "--porcelain=v1", "-z", "-uall", "--no-renames")
    modified, untracked = [], []
    for ent in out.split("\0"):
        if len(ent) < 4:
            continue
        (untracked if ent[:2] == "??" else modified).append(ent[3:])
    return modified, untracked


def _default_branch(main: str) -> str:
    rc, out, _ = _run(main, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    name = out.strip()
    if rc == 0 and name.startswith("refs/remotes/origin/"):
        return name[len("refs/remotes/origin/"):]
    for cand in ("main", "master"):
        if _run(main, "show-ref", "--verify", "-q", f"refs/remotes/origin/{cand}")[0] == 0:
            return cand
    raise _Refusal("no_default", "branche par défaut d'origin introuvable (origin/HEAD, origin/main, origin/master)")


def _branch_tip(root: str, branch: str) -> str | None:
    rc, out, _ = _run(root, "rev-parse", "-q", "--verify", f"refs/heads/{branch}^{{commit}}")
    return out.strip() if rc == 0 else None


def _registered(main: str) -> dict[str, str | None]:
    """Worktrees enregistrés : `{chemin réel: branche (sans refs/heads/) ou None}`."""
    out = _git(main, "worktree", "list", "--porcelain")
    res: dict[str, str | None] = {}
    cur = None
    for line in out.split("\n"):
        if line.startswith("worktree "):
            cur = _real(line[len("worktree "):])
            res[cur] = None
        elif cur is not None and line.startswith("branch "):
            ref = line[len("branch "):]
            res[cur] = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    return res


# --- battle.json (lecture seule) -----------------------------------------------------------

def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _load_battle(main: str, battle_id: str) -> dict:
    if not isinstance(battle_id, str) or not bs._ID_RE.fullmatch(battle_id):
        raise _Refusal("invalid", f"identifiant de battle invalide : {battle_id!r}")
    try:
        data = _read_json(Path(main) / ".legion" / "battles" / battle_id / "battle.json")
    except (OSError, ValueError, RecursionError) as exc:
        raise _Refusal("invalid", f"battle.json illisible pour {battle_id} ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise _Refusal("invalid", "battle.json n'est pas un objet")
    return data


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _wt_block(battle: dict) -> dict | None:
    wt = battle.get("worktree")
    if not isinstance(wt, dict) or not isinstance(wt.get("path"), str) or not wt["path"]:
        return None
    return wt


def _reflect_done(battle: dict) -> bool:
    return _dict(_dict(battle.get("phases")).get("reflect")).get("status") == "done"


def _token(battle_id: str, battle: dict) -> str:
    ticket = battle.get("ticket")
    m = re.fullmatch(r"GH#(\d+)", ticket) if isinstance(ticket, str) else None
    if m:
        return m.group(1)
    slug = _DATE_PREFIX_RE.sub("", battle_id)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", slug).strip("-.") or battle_id


def _branch_name(main: str, battle_id: str, battle: dict) -> str:
    me = ""
    for key in ("user.email", "user.name"):
        rc, out, _ = _run(main, "config", "--get", key)
        val = out.strip()
        if rc == 0 and val:
            me = val.split("@")[0] if key == "user.email" else val
            me = re.sub(r"[^A-Za-z0-9._-]+", "-", me).strip("-.")
            if me:
                break
    if not me:
        raise _Refusal("invalid", "ni user.email ni user.name configuré : branche <me>/<token> impossible")
    name = f"{me}/{_token(battle_id, battle)}"
    if _run(main, "check-ref-format", "--branch", name)[0] != 0:
        raise _Refusal("invalid", f"nom de branche invalide : {name!r}")
    return name


# --- create --------------------------------------------------------------------------------

def _ignored(main: str, rel: str) -> bool:
    return _run(main, "check-ignore", "-q", "--no-index", rel)[0] == 0


def _others(main: str, battle_id: str) -> tuple[list[str], list[str]]:
    """Autres battles vivantes : `(avec worktree actif, en place dans le principal)`."""
    bdir = Path(main) / ".legion" / "battles"
    try:
        names = sorted(p.name for p in bdir.iterdir() if p.is_dir())
    except OSError:
        return [], []
    with_wt: list[str] = []
    in_place: list[str] = []
    for name in names:
        if name == battle_id:
            continue
        try:
            other = _read_json(bdir / name / "battle.json")
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(other, dict) or other.get("aborted") is not None or _reflect_done(other):
            continue
        wt = _wt_block(other)
        if wt is None:
            in_place.append(name)
        elif os.path.isdir(wt["path"]):
            with_wt.append(name)
    return with_wt, in_place


def _concurrent(main: str, battle_id: str) -> list[str]:
    return _others(main, battle_id)[0]


def _copy_local(main: str, wt: str) -> tuple[list[str], list[dict]]:
    """Recopie les fichiers listés par `.worktreeinclude` (ignorés par git). `(copied, skipped)`."""
    inc = os.path.join(main, ".worktreeinclude")
    copied: list[str] = []
    skipped: list[dict] = []
    if not os.path.isfile(inc):
        return copied, skipped
    rc, out, _ = _run(main, "ls-files", "-z", "--others", "--ignored", f"--exclude-from={inc}")
    if rc != 0:
        return copied, [{"path": ".worktreeinclude", "why": "lecture impossible"}]
    for rel in (p for p in out.split("\0") if p):
        parts = rel.split("/")
        if rel.startswith("/") or ".." in parts or parts[0] in (".legion", ".claude"):
            skipped.append({"path": rel, "why": "chemin réservé"})
            continue
        if not _ignored(main, rel):
            skipped.append({"path": rel, "why": "non ignoré par git"})
            continue
        src, dst = os.path.join(main, rel), os.path.join(wt, rel)
        if os.path.islink(src) or not os.path.isfile(src):
            skipped.append({"path": rel, "why": "lien symbolique ou fichier non régulier"})
            continue
        if any(os.path.islink(os.path.join(main, *parts[:i + 1])) for i in range(len(parts) - 1)):
            skipped.append({"path": rel, "why": "dossier parent en lien symbolique"})
            continue
        if os.path.lexists(dst):
            skipped.append({"path": rel, "why": "existe déjà"})
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not _is_under(os.path.dirname(dst), wt):
            skipped.append({"path": rel, "why": "hors du worktree"})
            continue
        shutil.copy2(src, dst)
        copied.append(rel)
    return copied, skipped


def create(cwd: str, battle_id: str) -> dict:
    try:
        return _create(cwd, battle_id)
    except _Refusal as exc:
        return _refused(exc)


def _create(cwd: str, battle_id: str) -> dict:
    main = _main_root(cwd)
    battle = _load_battle(main, battle_id)
    if battle.get("aborted") is not None:
        raise _Refusal("aborted", "battle abandonnée")
    branch = _branch_name(main, battle_id, battle)
    path = os.path.join(main, ".claude", "worktrees", battle_id)
    rc, head, _ = _run(main, "rev-parse", "-q", "--verify", "HEAD^{commit}")
    if rc != 0:
        raise _Refusal("no_commit", "le dépôt principal n'a aucun commit")
    head = head.strip()
    # Idempotence : worktree déjà conforme (même chemin, même branche, HEAD == base).
    reg = _registered(main)
    if _real(path) in reg and os.path.isdir(path):
        base = _dict(battle.get("worktree")).get("base")
        base = base if isinstance(base, str) and _SHA_RE.fullmatch(base) else head
        wt_head = _run(path, "rev-parse", "-q", "--verify", "HEAD^{commit}")[1].strip()
        if reg[_real(path)] == branch and wt_head == base:
            return {"ok": True, "path": path, "branch": branch, "base": base, "created": False,
                    "copied": [], "skipped": []}
        raise _Refusal("path_exists", f"{path} existe déjà et ne correspond pas (branche/base)")
    concurrent, live = _others(main, battle_id)
    warnings: list[str] = []
    if live:
        warnings.append("in_place_live : battle(s) " + ", ".join(live)
                        + " en place dans le principal (sans worktree) : le principal reste partagé")
    bad = [r for r in (".legion/x", f".claude/worktrees/{battle_id}/x") if not _ignored(main, r)]
    if bad:
        raise _Refusal("not_ignored", "à ignorer dans .gitignore : " + ", ".join(b[:-2] for b in bad))
    modified, untracked = _status(main)
    if modified or untracked:
        raise _Refusal("dirty", "le dépôt principal n'est pas propre",
                       modified=modified[:_MAX_LIST], untracked=untracked[:_MAX_LIST])
    if _branch_tip(main, branch) is not None:
        raise _Refusal("branch_exists", f"la branche {branch} existe déjà")
    if os.path.lexists(path):
        raise _Refusal("path_exists", f"{path} existe déjà")
    _git(main, "worktree", "add", "-q", "-b", branch, path, head)
    copied, skipped = _copy_local(main, path)
    return {"ok": True, "path": path, "branch": branch, "base": head, "created": True,
            "copied": copied, "skipped": skipped, "concurrent": concurrent, "warnings": warnings}


# --- where ---------------------------------------------------------------------------------

def where(cwd: str, battle_id: str | None = None) -> dict:
    try:
        return _where(cwd, battle_id)
    except _Refusal as exc:
        return _refused(exc)


def _where(cwd: str, battle_id: str | None) -> dict:
    main = _main_root(cwd)
    if battle_id is None:
        active = bs.load_active_battle(Path(main))
        battle = active[1] if active else None
    else:
        battle = _load_battle(main, battle_id)
    try:
        top = ac._toplevel(cwd)
    except ac._Refuse:
        top = None
    rc, cur, _ = _run(cwd, "branch", "--show-current")
    current = cur.strip() if rc == 0 else None
    res = {"ok": True, "state_root": main, "mode": "none", "worktree_path": None, "branch": None,
           "exists": False, "inside": False, "cwd_toplevel": top, "current_branch": current,
           "worktree_branch": None, "main_branch": None, "reason": None}
    rc, mcur, _ = _run(main, "branch", "--show-current")
    res["main_branch"] = mcur.strip() if rc == 0 else None
    if battle is None:
        return res
    wt = _wt_block(battle)
    if wt is None:
        res["mode"] = "in_place"
        return res
    path = _real(wt["path"])
    branch = wt.get("branch") if isinstance(wt.get("branch"), str) else None
    res.update(mode="worktree", worktree_path=path, branch=branch,
               exists=os.path.isdir(path) and path in _registered(main),
               inside=_is_under(cwd, path))
    if not res["exists"]:
        res.update(ok=False, reason="missing")
        return res
    rc, wcur, _ = _run(path, "branch", "--show-current")
    res["worktree_branch"] = wcur.strip() if rc == 0 else None
    res["ok"] = bool(branch and res["worktree_branch"] == branch)
    res["reason"] = None if res["ok"] else "wrong_branch"
    return res


# --- close-check / close -------------------------------------------------------------------

def _tip_and_branch(main: str, battle_id: str,
                    battle: dict) -> tuple[str | None, str, dict | None, str]:
    """Tip, nom de branche, bloc worktree et provenance du nom (`worktree`, `head_ref` ou `derived`).

    `derived` = nom recalculé depuis `git config` : il peut différer de la branche livrée (GH#161)."""
    wt = _wt_block(battle)
    delivery = _dict(battle.get("delivery"))
    if wt and isinstance(wt.get("branch"), str):
        branch, source = wt["branch"], "worktree"
    elif isinstance(delivery.get("head_ref"), str) and delivery["head_ref"]:
        branch, source = delivery["head_ref"], "head_ref"
    else:
        branch, source = _branch_name(main, battle_id, battle), "derived"
    return _branch_tip(main, branch), branch, wt, source


def close_check(cwd: str, battle_id: str) -> dict:
    try:
        return _close_check(cwd, battle_id)[0]
    except _Refusal as exc:
        return _refused(exc)


def _close_check(cwd: str, battle_id: str) -> tuple[dict, dict]:
    main = _main_root(cwd)
    battle = _load_battle(main, battle_id)
    delivery = _dict(battle.get("delivery"))
    tip, branch, wt, source = _tip_and_branch(main, battle_id, battle)
    default = _default_branch(main)
    merged = delivery.get("pr_state") == "merged"
    checks = [{"name": "pr_merged", "ok": merged,
               "detail": f"delivery.pr_state = {delivery.get('pr_state')!r}"}]
    shape_ok = (branch != default and not branch.startswith("-")
                and _run(main, "check-ref-format", "--branch", branch)[0] == 0)
    checks.append({"name": "branch_safe", "ok": shape_ok,
                   "detail": "forme attendue, distincte de la branche par défaut" if shape_ok else
                             f"branche à supprimer {branch!r} : branche par défaut ({default}) ou nom invalide"})
    if tip is None and source == "derived":
        contained = {"name": "contained", "ok": False,
                     "detail": f"branche {branch} absente, mais ce nom est déduit de git config, pas de la "
                               "PR : rafraîchir la PR (gh pr view … headRefName,headRefOid puis "
                               "set-delivery --pr-json) avant close"}
    elif tip is None:
        contained = {"name": "contained", "ok": True, "detail": f"branche {branch} absente (déjà supprimée)"}
    else:
        rc, _, _ = _run(main, "merge-base", "--is-ancestor", tip, f"refs/remotes/origin/{default}")
        if rc == 0:
            contained = {"name": "contained", "ok": True, "detail": f"{branch} est ancêtre de origin/{default}"}
        elif merged and delivery.get("head_oid") == tip:
            contained = {"name": "contained", "ok": True,
                         "detail": "PR mergée et head_oid == tip local (squash/rebase)"}
        else:
            contained = {"name": "contained", "ok": False,
                         "detail": f"{branch} ({tip[:12]}) n'est pas dans origin/{default} : "
                                   "commits locaux non poussés ou fetch manquant"}
    checks.append(contained)
    modified: list[str] = []
    untracked: list[str] = []
    if wt:
        path = _real(wt["path"])
        if os.path.isdir(path) and path in _registered(main):
            modified, untracked = _status(path)
            clean = not modified and not untracked
            detail = "propre" if clean else f"{len(modified)} modifié(s), {len(untracked)} non suivi(s)"
        else:
            clean, detail = True, "worktree absent"
        checks.append({"name": "worktree_clean", "ok": clean, "detail": detail})
        inside = _is_under(cwd, path)
        checks.append({"name": "not_inside", "ok": not inside,
                       "detail": "la session est dans le worktree à supprimer" if inside else "hors du worktree"})
    res = {"ok": all(c["ok"] for c in checks), "checks": checks,
           "modified": modified[:_MAX_LIST], "untracked": untracked[:_MAX_LIST]}
    return res, {"main": main, "battle": battle, "tip": tip, "branch": branch, "wt": wt,
                 "default": default}


def close(cwd: str, battle_id: str) -> dict:
    try:
        return _close(cwd, battle_id)
    except _Refusal as exc:
        return _refused(exc)


def _close(cwd: str, battle_id: str) -> dict:
    res, ctx = _close_check(cwd, battle_id)
    main, battle, branch, wt, default = ctx["main"], ctx["battle"], ctx["branch"], ctx["wt"], ctx["default"]
    if not _reflect_done(battle):
        raise _Refusal("reflect_pending", "phases.reflect n'est pas `done` : lancer /legion:retro d'abord")
    failed = [c for c in res["checks"] if not c["ok"]]
    if failed:
        raise _Refusal(failed[0]["name"], "; ".join(f"{c['name']}: {c['detail']}" for c in failed),
                       checks=res["checks"], modified=res["modified"], untracked=res["untracked"])
    warnings: list[str] = []
    removed = branch_deleted = False
    main_cur = _run(main, "branch", "--show-current")[1].strip()
    if ctx["tip"] is not None and _branch_tip(main, branch) != ctx["tip"]:
        raise _Refusal("branch_moved", f"{branch} a bougé depuis close-check : rien n'a été supprimé")
    if ctx["tip"] is not None and main_cur == branch:
        if _status(main) != ([], []):
            raise _Refusal("main_dirty", f"le principal est sur {branch} et n'est pas propre : "
                                         "rien n'a été supprimé")
        _git(main, "checkout", "-q", default)
        main_cur = default
    if wt:
        path = _real(wt["path"])
        if os.path.isdir(path) and path in _registered(main):
            _git(main, "worktree", "remove", path)
            removed = True
    if ctx["tip"] is not None and _branch_tip(main, branch) is not None:
        rc, _, err = _run(main, "update-ref", "-d", f"refs/heads/{branch}", ctx["tip"])
        if rc != 0:
            raise _Refusal("branch_moved", f"{branch} a bougé pendant la clôture, non supprimée : "
                                           f"{(err.strip().splitlines() or ['?'])[0]}")
        branch_deleted = True
    pruned = _run(main, "worktree", "prune")[0] == 0
    main_updated = False
    if main_cur != default:
        warnings.append(f"principal sur {main_cur or 'HEAD détaché'}, pas sur {default} : non avancé")
    elif _status(main) != ([], []):
        warnings.append(f"principal sur {default} mais pas propre : non avancé")
    else:
        rc, _, err = _run(main, "merge", "--ff-only", "-q", f"refs/remotes/origin/{default}")
        if rc == 0:
            main_updated = True
        else:
            warnings.append(f"merge --ff-only origin/{default} impossible : "
                            f"{(err.strip().splitlines() or ['?'])[0]}")
    return {"ok": True, "removed": removed, "branch_deleted": branch_deleted, "pruned": pruned,
            "main_updated": main_updated, "warnings": warnings}


# --- CLI -----------------------------------------------------------------------------------

def _usage(msg: str) -> int:
    print(json.dumps({"ok": False, "usage": msg}, ensure_ascii=False), file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in args:
        return _self_test()
    if not args or args[0] not in ("create", "where", "close-check", "close"):
        return _usage("sous-commande attendue : create | where | close-check | close | --self-test")
    cmd, rest = args[0], args[1:]
    if len(rest) not in (0, 2) or (rest and rest[0] != "--battle") or (cmd != "where" and not rest):
        return _usage(f"{cmd} --battle <id>" + (" (facultatif pour where)" if cmd == "where" else ""))
    bid = rest[1] if rest else None
    fn = {"create": create, "close-check": close_check, "close": close}.get(cmd)
    res = where(os.getcwd(), bid) if fn is None else fn(os.getcwd(), bid)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["ok"] else 2


# --- Self-test : vrais dépôts git jetables, origin bare local, hors ligne ----------------------

_DEFAULT_IGNORE = ".legion/\n.claude/\n.env\nappsettings.Development.json\nlinked.cfg\n"


class _Fx:
    """Principal (branche `main`, remote `origin` bare local) + battle `B` (ticket GH#7)."""

    def __init__(self, base: str, gitignore: str = _DEFAULT_IGNORE) -> None:
        os.makedirs(base, exist_ok=True)
        self.base = base
        self.origin = os.path.join(base, "origin.git")
        self.main = _real(os.path.join(base, "main"))
        os.makedirs(self.main)
        self.git(base, "init", "-q", "--bare", "-b", "main", self.origin)
        self.git(self.main, "init", "-q", "-b", "main")
        self.git(self.main, "config", "user.email", "me@x.io")
        self.git(self.main, "config", "user.name", "Me")
        self.git(self.main, "config", "commit.gpgsign", "false")
        self.write(self.main, ".gitignore", gitignore)
        self.write(self.main, ".worktreeinclude", ".env\nappsettings.Development.json\nlinked.cfg\n.legion/\n")
        self.write(self.main, "src/a.txt", "a\n")
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", "init")
        self.git(self.main, "remote", "add", "origin", self.origin)
        self.git(self.main, "push", "-q", "-u", "origin", "main")
        self.git(self.main, "remote", "set-head", "origin", "main")
        self.battle()

    @staticmethod
    def git(cwd: str, *args: str) -> str:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        assert p.returncode == 0, (args, p.stderr)
        return p.stdout.strip()

    @staticmethod
    def write(root: str, rel: str, text: str) -> None:
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)

    def battle(self, bid: str = "B", **fields) -> None:
        body = {"id": bid, "ticket": "GH#7", "phases": {"reflect": {"status": "pending"}}}
        body.update(fields)
        self.write(self.main, f".legion/battles/{bid}/battle.json", json.dumps(body))

    @staticmethod
    def wt_block(res: dict) -> dict:
        return {"path": res["path"], "branch": res["branch"], "base": res["base"], "created_at": "T"}

    def merge_to_origin(self, branch: str, squash: bool = False) -> None:
        """Pousse `branch`, la merge dans `origin/main` depuis un clone tiers, puis fetch."""
        self.git(self.main, "push", "-q", "origin", branch)
        other = os.path.join(self.base, f"merger-{branch.replace('/', '_')}-{int(squash)}")
        self.git(self.base, "clone", "-q", self.origin, other)
        self.git(other, "config", "user.email", "o@x.io")
        self.git(other, "config", "user.name", "O")
        self.git(other, "config", "commit.gpgsign", "false")
        if squash:
            self.git(other, "merge", "--squash", f"origin/{branch}")
            self.git(other, "commit", "-q", "-m", "squash")
        else:
            self.git(other, "merge", "-q", "--no-ff", "-m", "merge", f"origin/{branch}")
        self.git(other, "push", "-q", "origin", "main")
        self.git(self.main, "fetch", "-q", "origin")

    def commit_in(self, wt: str, name: str) -> str:
        self.write(wt, name, name + "\n")
        self.git(wt, "add", "-A")
        self.git(wt, "commit", "-q", "-m", name)
        return self.git(wt, "rev-parse", "HEAD")

    def created(self) -> dict:
        res = create(self.main, "B")
        assert res["ok"] and res["created"], res
        return res


def _self_test() -> int:
    saved = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
    os.environ.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    try:
        names = [n for n in sorted(globals()) if n.startswith("_t_")]
        failed = 0
        for name in names:
            tmp = tempfile.mkdtemp(prefix="bwt-")
            try:
                globals()[name](_real(tmp))
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        print(f"{len(names) - failed}/{len(names)} tests passés")
        return 1 if failed else 0
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _snapshot(fx: _Fx) -> tuple:
    g = fx.git
    return (g(fx.main, "rev-parse", "HEAD"), g(fx.main, "branch", "--show-current"),
            g(fx.main, "status", "--porcelain=v1", "-uall"), g(fx.main, "ls-files", "-s"))


def _t_create_nominal(tmp: str) -> None:
    fx = _Fx(tmp)
    fx.write(fx.main, ".env", "SECRET=1\n")
    fx.write(fx.main, "appsettings.Development.json", "{}\n")
    os.symlink(os.path.join(fx.main, ".env"), os.path.join(fx.main, "linked.cfg"))
    fx.write(fx.main, ".legion/notes.txt", "x\n")
    before = _snapshot(fx)
    res = create(fx.main, "B")
    assert res["ok"] and res["created"] is True, res
    assert res["path"] == os.path.join(fx.main, ".claude", "worktrees", "B"), res
    assert res["branch"] == "me/7" and res["base"] == before[0], res
    assert sorted(res["copied"]) == [".env", "appsettings.Development.json"], res
    assert "linked.cfg" in {s["path"] for s in res["skipped"]}, res
    wt = res["path"]
    assert os.path.isfile(os.path.join(wt, ".env")) and not os.path.lexists(os.path.join(wt, "linked.cfg"))
    assert not os.path.exists(os.path.join(wt, ".legion"))
    assert fx.git(wt, "branch", "--show-current") == "me/7" and fx.git(wt, "rev-parse", "HEAD") == before[0]
    assert _snapshot(fx) == before, "principal modifié"
    # jamais d'écrasement
    fx.write(wt, ".env", "LOCAL\n")
    copied, skipped = _copy_local(fx.main, wt)
    assert ".env" not in copied and open(os.path.join(wt, ".env")).read() == "LOCAL\n", (copied, skipped)


def _t_token_slug(tmp: str) -> None:
    assert _token("2026-10-01-mon-slug", {"ticket": None}) == "mon-slug"
    assert _token("2026-10-01-GH-152", {"ticket": "GH#152"}) == "152"


def _t_create_dirty(tmp: str) -> None:
    fx = _Fx(tmp)
    fx.write(fx.main, "src/a.txt", "changed\n")
    fx.write(fx.main, "new.txt", "n\n")
    res = create(fx.main, "B")
    assert not res["ok"] and res["reason"] == "dirty", res
    assert "src/a.txt" in res["modified"] and "new.txt" in res["untracked"], res
    assert not os.path.exists(os.path.join(fx.main, ".claude", "worktrees", "B"))
    assert _branch_tip(fx.main, "me/7") is None


def _t_create_not_ignored(tmp: str) -> None:
    fx = _Fx(os.path.join(tmp, "one"), gitignore=".legion/\n")
    res = create(fx.main, "B")
    assert not res["ok"] and res["reason"] == "not_ignored" and ".claude/worktrees/B" in res["detail"], res
    fx2 = _Fx(os.path.join(tmp, "two"), gitignore=".claude/\n")
    res = create(fx2.main, "B")
    assert not res["ok"] and res["reason"] == "not_ignored" and ".legion" in res["detail"], res


def _t_create_branch_path_exists(tmp: str) -> None:
    fx = _Fx(tmp)
    fx.git(fx.main, "branch", "me/7")
    before = _snapshot(fx)
    res = create(fx.main, "B")
    assert not res["ok"] and res["reason"] == "branch_exists", res
    fx.git(fx.main, "branch", "-q", "-D", "me/7")
    fx.write(fx.main, ".claude/worktrees/B/x", "x\n")
    res = create(fx.main, "B")
    assert not res["ok"] and res["reason"] == "path_exists", res
    assert _branch_tip(fx.main, "me/7") is None and _snapshot(fx) == before


def _t_create_idempotent(tmp: str) -> None:
    fx = _Fx(tmp)
    first = fx.created()
    fx.battle(worktree=fx.wt_block(first))
    again = create(fx.main, "B")
    assert again["ok"] and again["created"] is False, again
    assert (again["path"], again["branch"], again["base"]) == (first["path"], first["branch"], first["base"])


def _t_create_concurrent_and_aborted(tmp: str) -> None:
    fx = _Fx(tmp)
    other = os.path.join(fx.main, ".claude", "worktrees", "C")
    os.makedirs(other)
    fx.battle("C", worktree={"path": other, "branch": "me/9", "base": "0" * 40})
    res = create(fx.main, "B")
    assert res["ok"] and res["concurrent"] == ["C"] and res["warnings"] == [], res
    fx.battle("P")  # battle vivante en place (sans worktree)
    fx.git(fx.main, "worktree", "remove", "--force", res["path"])
    fx.git(fx.main, "branch", "-D", res["branch"])
    res = create(fx.main, "B")
    assert res["ok"] and res["concurrent"] == ["C"], res
    assert any("in_place_live" in w and "P" in w for w in res["warnings"]), res
    fx.git(fx.main, "worktree", "remove", "--force", res["path"])
    fx.git(fx.main, "branch", "-D", res["branch"])
    fx.battle("C", worktree={"path": other}, aborted={"at": "T", "reason": None})
    fx.battle("P", aborted={"at": "T", "reason": None})
    res = create(fx.main, "B")
    assert res["ok"] and res["concurrent"] == [] and res["warnings"] == [], res
    fx.battle("D", aborted={"at": "T", "reason": None})
    res = create(fx.main, "D")
    assert not res["ok"] and res["reason"] == "aborted", res


def _t_where(tmp: str) -> None:
    fx = _Fx(tmp)
    res = fx.created()
    fx.battle(worktree=fx.wt_block(res))
    wt = res["path"]
    sub = os.path.join(wt, "src")
    for cwd, inside in ((wt, True), (sub, True), (fx.main, False)):
        w = where(cwd, "B")
        assert w["state_root"] == fx.main and w["inside"] is inside and w["mode"] == "worktree", (cwd, w)
        assert w["ok"] is True and w["reason"] is None and w["exists"] and w["branch"] == "me/7", (cwd, w)
        assert w["worktree_branch"] == "me/7" and w["main_branch"] == fx.git(fx.main, "branch", "--show-current").strip(), w
    assert where(wt, "B")["cwd_toplevel"] == wt and where(wt, "B")["current_branch"] == "me/7"
    fx.battle("L")
    assert where(fx.main, "L")["mode"] == "in_place"
    assert where(fx.main, "L")["ok"] is True and where(fx.main, "L")["reason"] is None
    fx.git(wt, "switch", "-q", "-c", "autre")
    w = where(fx.main, "B")
    assert w["ok"] is False and w["reason"] == "wrong_branch" and w["worktree_branch"] == "autre", w
    fx.git(wt, "switch", "-q", "me/7")
    assert where(fx.main, "B")["ok"] is True
    shutil.rmtree(wt)
    w = where(fx.main, "B")
    assert w["ok"] is False and w["reason"] == "missing" and w["exists"] is False, w


def _t_close_check_states(tmp: str) -> None:
    fx = _Fx(tmp)
    res = fx.created()
    wt, br = res["path"], res["branch"]
    fx.commit_in(wt, "f1.txt")
    fx.battle(worktree=fx.wt_block(res), delivery={"pr_state": "open"})
    cc = close_check(fx.main, "B")
    names = {c["name"]: c["ok"] for c in cc["checks"]}
    assert not cc["ok"] and names["pr_merged"] is False and names["contained"] is False, cc
    fx.merge_to_origin(br)
    fx.battle(worktree=fx.wt_block(res), delivery={"pr_state": "merged"})
    cc = close_check(fx.main, "B")
    assert cc["ok"], cc
    fx.write(wt, "src/a.txt", "dirty\n")
    fx.write(wt, "u.txt", "u\n")
    cc = close_check(fx.main, "B")
    assert not cc["ok"] and "src/a.txt" in cc["modified"] and "u.txt" in cc["untracked"], cc
    cc = close_check(wt, "B")
    assert {c["name"]: c["ok"] for c in cc["checks"]}["not_inside"] is False, cc


def _t_close_check_squash(tmp: str) -> None:
    fx = _Fx(tmp)
    res = fx.created()
    wt, br = res["path"], res["branch"]
    tip = fx.commit_in(wt, "f1.txt")
    fx.merge_to_origin(br, squash=True)
    assert _run(fx.main, "merge-base", "--is-ancestor", tip, "refs/remotes/origin/main")[0] != 0
    fx.battle(worktree=fx.wt_block(res), delivery={"pr_state": "merged", "head_oid": tip})
    cc = close_check(fx.main, "B")
    assert cc["ok"], cc
    fx.commit_in(wt, "f2.txt")  # commit local non poussé
    cc = close_check(fx.main, "B")
    assert not cc["ok"] and {c["name"]: c["ok"] for c in cc["checks"]}["contained"] is False, cc


def _merged_fx(tmp: str, squash: bool = False) -> tuple[_Fx, dict]:
    fx = _Fx(tmp)
    res = fx.created()
    tip = fx.commit_in(res["path"], "f1.txt")
    fx.merge_to_origin(res["branch"], squash=squash)
    fx.battle(worktree=fx.wt_block(res), delivery={"pr_state": "merged", "head_oid": tip},
              phases={"reflect": {"status": "done"}})
    return fx, res


def _t_close_nominal_and_replay(tmp: str) -> None:
    fx, res = _merged_fx(tmp)
    out = close(res["path"], "B")
    assert not out["ok"] and out["reason"] == "not_inside" and os.path.isdir(res["path"]), out
    assert _branch_tip(fx.main, res["branch"]) is not None
    out = close(fx.main, "B")
    assert out["ok"] and out["removed"] and out["branch_deleted"] and out["main_updated"], out
    assert not os.path.exists(res["path"]) and _branch_tip(fx.main, res["branch"]) is None
    assert fx.git(fx.main, "rev-parse", "HEAD") == fx.git(fx.main, "rev-parse", "origin/main")
    assert fx.git(fx.main, "worktree", "list", "--porcelain").count("worktree ") == 1
    again = close(fx.main, "B")
    assert again["ok"] and not again["removed"] and not again["branch_deleted"], again


def _t_close_reflect_pending(tmp: str) -> None:
    fx, res = _merged_fx(tmp)
    fx.battle(worktree=fx.wt_block(res), delivery={"pr_state": "merged"})
    out = close(fx.main, "B")
    assert not out["ok"] and out["reason"] == "reflect_pending" and os.path.isdir(res["path"]), out


def _t_close_main_elsewhere_or_dirty(tmp: str) -> None:
    fx, res = _merged_fx(os.path.join(tmp, "a"))
    fx.git(fx.main, "checkout", "-q", "-b", "other")
    out = close(fx.main, "B")
    assert out["ok"] and out["removed"] and out["main_updated"] is False and out["warnings"], out
    assert fx.git(fx.main, "branch", "--show-current") == "other"
    fx, res = _merged_fx(os.path.join(tmp, "b"))
    fx.write(fx.main, "src/a.txt", "dirty\n")
    out = close(fx.main, "B")
    assert out["ok"] and out["removed"] and out["main_updated"] is False and out["warnings"], out
    assert fx.git(fx.main, "branch", "--show-current") == "main"


def _t_close_squash(tmp: str) -> None:
    fx, res = _merged_fx(tmp, squash=True)
    out = close(fx.main, "B")
    assert out["ok"] and out["removed"] and out["branch_deleted"] and out["main_updated"], out


def _t_close_dirty_worktree_refused(tmp: str) -> None:
    fx, res = _merged_fx(tmp)
    fx.write(res["path"], "u.txt", "u\n")
    out = close(fx.main, "B")
    assert not out["ok"] and out["reason"] == "worktree_clean" and "u.txt" in out["untracked"], out
    assert os.path.isdir(res["path"]) and _branch_tip(fx.main, res["branch"]) is not None


def _t_close_in_place(tmp: str) -> None:
    fx = _Fx(os.path.join(tmp, "a"))
    fx.git(fx.main, "checkout", "-q", "-b", "me/7")
    tip = fx.commit_in(fx.main, "f1.txt")
    fx.merge_to_origin("me/7")
    fx.battle(delivery={"pr_state": "merged", "head_ref": "me/7", "head_oid": tip},
              phases={"reflect": {"status": "done"}})
    out = close(fx.main, "B")
    assert out["ok"] and out["branch_deleted"] and not out["removed"] and out["main_updated"], out
    assert fx.git(fx.main, "branch", "--show-current") == "main" and _branch_tip(fx.main, "me/7") is None
    assert close(fx.main, "B")["ok"]
    fx = _Fx(os.path.join(tmp, "b"))
    fx.git(fx.main, "checkout", "-q", "-b", "me/7")
    tip = fx.commit_in(fx.main, "f1.txt")
    fx.merge_to_origin("me/7")
    fx.battle(delivery={"pr_state": "merged", "head_ref": "me/7"}, phases={"reflect": {"status": "done"}})
    fx.write(fx.main, "src/a.txt", "dirty\n")
    out = close(fx.main, "B")
    assert not out["ok"] and out["reason"] == "main_dirty" and _branch_tip(fx.main, "me/7") == tip, out


def _t_close_branch_moved(tmp: str) -> None:
    fx, res = _merged_fx(tmp)
    real = globals()["_close_check"]

    def racing(cwd: str, battle_id: str):
        out = real(cwd, battle_id)
        fx.commit_in(res["path"], "late.txt")  # commit ajouté après le check
        return out
    globals()["_close_check"] = racing
    try:
        out = close(fx.main, "B")
    finally:
        globals()["_close_check"] = real
    assert not out["ok"] and out["reason"] == "branch_moved", out
    assert os.path.isdir(res["path"]) and _branch_tip(fx.main, res["branch"]) is not None, out


def _t_close_default_branch_refused(tmp: str) -> None:
    fx = _Fx(tmp)
    fx.git(fx.main, "checkout", "-q", "-b", "other")
    fx.battle(delivery={"pr_state": "merged", "head_ref": "main"}, phases={"reflect": {"status": "done"}})
    cc = close_check(fx.main, "B")
    assert not cc["ok"] and {c["name"]: c["ok"] for c in cc["checks"]}["branch_safe"] is False, cc
    out = close(fx.main, "B")
    assert not out["ok"] and out["reason"] == "branch_safe", out
    assert _branch_tip(fx.main, "main") is not None
    fx.battle(delivery={"pr_state": "merged", "head_ref": "-bad"}, phases={"reflect": {"status": "done"}})
    assert close(fx.main, "B")["reason"] == "branch_safe"


def _t_close_derived_name_absent(tmp: str) -> None:
    """GH#161 : un nom déduit de git config qui ne correspond à aucune branche n'est pas « déjà supprimée »."""
    fx = _Fx(os.path.join(tmp, "a"))
    fx.git(fx.main, "checkout", "-q", "-b", "livree/7")       # branche livrée != nom déduit (me/7)
    fx.commit_in(fx.main, "f1.txt")
    fx.merge_to_origin("livree/7")
    fx.git(fx.main, "checkout", "-q", "main")
    fx.battle(delivery={"pr_state": "merged"}, phases={"reflect": {"status": "done"}})
    cc = close_check(fx.main, "B")
    checks = {c["name"]: c for c in cc["checks"]}
    assert not cc["ok"] and not checks["contained"]["ok"], cc
    assert "headRefName" in checks["contained"]["detail"], cc
    out = close(fx.main, "B")
    assert not out["ok"] and out["reason"] == "contained", out
    assert _branch_tip(fx.main, "livree/7") is not None, out
    fx = _Fx(os.path.join(tmp, "b"))                        # nom déduit présent : comportement inchangé
    fx.git(fx.main, "checkout", "-q", "-b", "me/7")
    fx.commit_in(fx.main, "f1.txt")
    fx.merge_to_origin("me/7")
    fx.git(fx.main, "checkout", "-q", "main")
    fx.battle(delivery={"pr_state": "merged"}, phases={"reflect": {"status": "done"}})
    assert close_check(fx.main, "B")["ok"]
    out = close(fx.main, "B")
    assert out["ok"] and out["branch_deleted"] and _branch_tip(fx.main, "me/7") is None, out
    fx = _Fx(os.path.join(tmp, "c"))                        # nom prouvé (head_ref) absent : idempotent
    fx.battle(delivery={"pr_state": "merged", "head_ref": "livree/7"}, phases={"reflect": {"status": "done"}})
    cc = close_check(fx.main, "B")
    assert cc["ok"] and "déjà supprimée" in {c["name"]: c for c in cc["checks"]}["contained"]["detail"], cc


def _t_cross_state_guard(tmp: str) -> None:
    """Contrat croisé : vrai CLI `battle_state` (set-meta) -> `where`/`close-check` -> règle C8 de guard.py."""
    import importlib.util
    fx = _Fx(tmp)
    cli = os.path.join(_SCRIPTS_DIR, "battle_state.py")

    def bs_cli(*args: str) -> None:
        p = subprocess.run([sys.executable, cli, *args, "--repo", fx.main], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, cwd=fx.main)
        assert p.returncode == 0, (args, p.stdout, p.stderr)

    (Path(fx.main) / ".legion" / "battles" / "B" / "battle.json").unlink()
    bs_cli("init", "B", "--ticket", "GH#7", "--title", "t", "--profile", next(iter(bs.PROFILES)))
    bs_cli("activate", "B")
    res = fx.created()
    bs_cli("set-meta", "--worktree-path", res["path"], "--worktree-branch", res["branch"],
           "--worktree-base", res["base"])
    w = where(res["path"], "B")
    assert w["ok"] and w["mode"] == "worktree" and w["exists"] and w["branch"] == res["branch"], w
    cc = close_check(fx.main, "B")
    names = {c["name"]: c["ok"] for c in cc["checks"]}
    assert names["branch_safe"] and names["worktree_clean"] and names["not_inside"], cc
    spec = importlib.util.spec_from_file_location(
        "guard_under_test", os.path.join(os.path.dirname(_SCRIPTS_DIR), "hooks", "guard.py"))
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    main_p, wt_p = Path(fx.main), Path(res["path"])
    ev = lambda p: {"tool_name": "Edit", "agent_type": "claude", "tool_input": {"file_path": p}}  # noqa: E731
    code, msg = guard._decide(ev(str(main_p / "src" / "x.txt")), wt_p, main_p)
    assert code == 2 and "protege" in msg and str(wt_p) in msg, (code, msg)
    assert "protege" not in guard._decide(ev(str(wt_p / "src" / "x.txt")), wt_p, main_p)[1]


def _t_cross_main_session(tmp: str) -> None:
    """Test croise GH#166 : session dans le principal (cwd=main), vrais CLI et vrai hook, vrai worktree."""
    fx = _Fx(tmp)
    scripts = Path(_SCRIPTS_DIR)
    hook = scripts.parent / "hooks" / "guard.py"
    main_p = Path(fx.main)
    clean_env = {k: v for k, v in os.environ.items()
                 if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "LEGION_GUARD_OFF")}

    def cli(script: str, *args: str, ok: bool = True) -> dict:
        p = subprocess.run([sys.executable, str(scripts / script), *args], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, cwd=fx.main, env=clean_env)
        assert (p.returncode == 0) == ok, (script, args, p.returncode, p.stdout, p.stderr)
        return json.loads(p.stdout) if p.stdout.strip().startswith("{") else {"raw": p.stdout}

    def edit(path: str) -> tuple[int, str]:
        payload = {"tool_name": "Edit", "agent_type": "claude", "tool_input": {"file_path": path}}
        p = subprocess.run([sys.executable, str(hook)], input=json.dumps(payload), capture_output=True,
                           text=True, cwd=fx.main, env=clean_env, timeout=60)
        return p.returncode, p.stderr

    # 1. init / create / set-meta / set-guard, tous depuis le principal
    (main_p / ".legion" / "battles" / "B" / "battle.json").unlink()
    cli("battle_state.py", "init", "B", "--ticket", "GH#7", "--title", "t", "--profile", next(iter(bs.PROFILES)),
        "--repo", fx.main)
    res = cli("battle_worktree.py", "create", "--battle", "B")
    assert res["ok"] and res["created"], res
    wt = res["path"]
    cli("battle_state.py", "set-meta", "--worktree-path", wt, "--worktree-branch", res["branch"],
        "--worktree-base", res["base"], "--repo", fx.main, "--battle", "B")
    cli("battle_state.py", "set-guard", "--allow", "src/**", "--repo", fx.main, "--battle", "B")

    # 2. where : ok depuis le principal, hors du worktree
    w = cli("battle_worktree.py", "where", "--battle", "B")
    assert w["ok"] and w["reason"] is None and w["inside"] is False and w["mode"] == "worktree", w

    # 3. decisions du hook reel (cwd = principal)
    assert edit(os.path.join(wt, "src", "x.txt"))[0] == 0                     # G-WT1
    code, msg = edit(os.path.join(wt, "docs", "x.txt"))                       # G-WT2
    assert code == 2 and "docs/x.txt" in msg, (code, msg)
    code, msg = edit(os.path.join(fx.main, "src", "x.txt"))                   # G-WT3 (C8)
    assert code == 2 and "protege" in msg, (code, msg)

    # 4. tree-snapshot / tree-verify --root <wt> --guard, depuis le principal
    snap = os.path.join(tmp, "snap.json")
    s = cli("artifact_check.py", "tree-snapshot", "--out", snap, "--root", wt)
    assert s["ok"], s
    fx.write(wt, "src/x.txt", "x\n")
    v = cli("artifact_check.py", "tree-verify", "--before", snap, "--fingerprint", s["fingerprint"],
            "--root", wt, "--guard")
    assert v["ok"] is True, v
    fx.write(wt, "docs/y.txt", "y\n")
    v = cli("artifact_check.py", "tree-verify", "--before", snap, "--fingerprint", s["fingerprint"],
            "--root", wt, "--guard", ok=False)
    assert v["ok"] is False and "docs/y.txt" in json.dumps(v), v
    os.unlink(os.path.join(wt, "docs", "y.txt"))

    # 5. commit dans le worktree : la branche et HEAD du principal ne bougent pas
    head, branch = fx.git(fx.main, "rev-parse", "HEAD"), fx.git(fx.main, "branch", "--show-current")
    fx.git(wt, "add", "src/x.txt")
    fx.git(wt, "commit", "-q", "-m", "x")
    assert (fx.git(fx.main, "rev-parse", "HEAD"), fx.git(fx.main, "branch", "--show-current")) == (head, branch)
    assert cli("battle_worktree.py", "where", "--battle", "B")["ok"] is True

    # 6. deux battles : une autre battle in-place pointee ne change rien pour le worktree de B
    other = cli("battle_state.py", "init", "C", "--ticket", "GH#8", "--title", "t",
                "--profile", next(iter(bs.PROFILES)), "--repo", fx.main)
    assert any("pointer_taken" in m for m in other.get("warnings", [])), other
    cli("battle_state.py", "set-guard", "--allow", "c/**", "--repo", fx.main, "--battle", "C")
    assert edit(os.path.join(wt, "src", "x.txt"))[0] == 0                     # battle proprietaire = B
    assert edit(os.path.join(wt, "c", "x.txt"))[0] == 2


def _t_cross_two_battles(tmp: str) -> None:
    """Test croise GH#170 : deux battles en worktree, deux sessions (sidA, sidB) et une session non liee
    (sidC), cwd = principal. Vrais CLI et vrais hooks (sous-processus, sources du depot), vrai remote nu."""
    if shutil.which("git") is None:
        print("SKIP: _t_cross_two_battles (git absent)", file=sys.stderr)
        return
    fx = _Fx(tmp)
    scripts = Path(_SCRIPTS_DIR)
    hooks = scripts.parent / "hooks"
    main_p = Path(fx.main)
    bs_py = str(scripts / "battle_state.py")
    clean_env = {k: v for k, v in os.environ.items()
                 if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "LEGION_GUARD_OFF",
                              "LEGION_HOOK_PROBE")}
    profile = next(iter(bs.PROFILES))

    def run(script: Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
        p = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, cwd=fx.main, env=clean_env, timeout=120)
        assert (p.returncode == 0) == ok, (script.name, args, p.returncode, p.stdout, p.stderr)
        return p

    def cli(script: str, *args: str, ok: bool = True) -> dict:
        p = run(scripts / script, *args, ok=ok)
        return json.loads(p.stdout) if p.stdout.strip().startswith("{") else {"raw": p.stdout}

    def hook(name: str, payload: dict) -> tuple[int, str]:
        p = subprocess.run([sys.executable, str(hooks / name)], input=json.dumps(payload), capture_output=True,
                           text=True, cwd=fx.main, env=clean_env, timeout=60)
        return p.returncode, p.stderr

    def bsc(*args: str, ok: bool = True) -> dict:
        return cli("battle_state.py", *args, "--repo", fx.main, ok=ok)

    def write(path: str, sid: str | None = "sidA", agent: str = "legion:architect", **extra) -> tuple[int, str]:
        payload = {"tool_name": "Write", "agent_type": agent, "cwd": fx.main,
                   "tool_input": {"file_path": path, "content": "x"}, **extra}
        if sid:
            payload["session_id"] = sid
        return hook("guard.py", payload)

    def bash_guard(command: str, sid: str | None) -> tuple[int, str]:
        payload = {"tool_name": "Bash", "agent_type": "claude", "cwd": fx.main, "tool_input": {"command": command}}
        if sid:
            payload["session_id"] = sid
        return hook("guard.py", payload)

    def edit(path: str, sid: str | None) -> tuple[int, str]:
        payload = {"tool_name": "Edit", "agent_type": "claude", "cwd": fx.main, "tool_input": {"file_path": path}}
        if sid:
            payload["session_id"] = sid
        return hook("guard.py", payload)

    def pointer() -> str:
        f = main_p / ".legion" / "active-battle"
        return f.read_text(encoding="utf-8").strip() if f.is_file() else ""

    def plan_of(x: str) -> str:
        return str(main_p / ".legion" / "battles" / x / "plan.md")

    # 1. init + session_bind + create + set-meta + set-guard, pour A (sidA) puis B (sidB)
    (main_p / ".legion" / "battles" / "B" / "battle.json").unlink()
    wts: dict[str, dict] = {}
    for x, sid, ticket in (("A", "sidA", "GH#7"), ("B", "sidB", "GH#8")):
        cmd = f"python {bs_py} init {x} --ticket {ticket} --title t --profile {profile} --repo {fx.main}"
        out = run(Path(bs_py), "init", x, "--ticket", ticket, "--title", "t", "--profile", profile,
                  "--repo", fx.main)
        code, err = hook("session_bind.py", {
            "hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": sid, "cwd": fx.main,
            "tool_input": {"command": cmd}, "tool_response": {"stdout": out.stdout}})
        assert code == 0, err
        res = cli("battle_worktree.py", "create", "--battle", x)
        assert res["ok"] and res["created"], res                                 # la 2e create reussit
        wts[x] = res
        bsc("set-meta", "--worktree-path", res["path"], "--worktree-branch", res["branch"],
            "--worktree-base", res["base"], "--battle", x)
        bsc("set-guard", "--allow", "src/**", "--battle", x)
    assert wts["B"]["concurrent"] == ["A"], wts["B"]
    assert pointer() == "B", pointer()
    wtA, wtB = wts["A"]["path"], wts["B"]["path"]

    # 2. session-status
    st = bsc("session-status", "--battle", "A")
    assert st["bound"] is True and st["keys"] == 1, st
    assert bsc("session-status", "--battle", "B")["bound"] is True

    # 3. gate architect : confinee a l'artefact de la battle de la session, malgre le pointeur sur B
    assert write(plan_of("A"), "sidA")[0] == 0
    code, msg = write(plan_of("B"), "sidA")
    assert code == 2, (code, msg)
    assert write(plan_of("B"), "sidB")[0] == 0
    assert write(plan_of("A"), "sidB")[0] == 2

    # 4. sous-agent sans session_id : cle derivee du transcript
    sub = {"transcript_path": str(Path(tmp) / "sidA" / "subagents" / "agent-1.jsonl")}
    assert write(plan_of("A"), None, **sub)[0] == 0
    assert write(plan_of("B"), None, **sub)[0] == 2

    # 5. guard de la session : C8 de A (principal protege), sidC non liee
    assert edit(os.path.join(wtA, "src", "x"), "sidA")[0] == 0
    code, msg = edit(os.path.join(fx.main, "src", "x"), "sidA")
    assert code == 2 and "protege" in msg, (code, msg)
    assert edit(os.path.join(fx.main, "src", "x"), "sidC")[0] == 0           # foreign : pas le guard de B

    # 6. careful de A : avertit sidA, silence pour sidB
    bsc("set-guard", "--careful", "on", "--battle", "A")
    rm = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": fx.main,
          "tool_input": {"command": "rm -rf x"}}
    code, err = hook("careful.py", {**rm, "session_id": "sidA"})
    assert code == 0 and "[careful]" in err, (code, err)
    code, err = hook("careful.py", {**rm, "session_id": "sidB"})
    assert code == 0 and err == "", (code, err)

    # 7. usage_track : la ligne va dans A
    tp = Path(tmp) / "agent-usage.jsonl"
    tp.write_text(json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": 5, "output_tokens": 2}, "content": []}}) + "\n", encoding="utf-8")
    code, err = hook("usage_track.py", {"hook_event_name": "SubagentStop", "agent_type": "builder",
                                        "session_id": "sidA", "agent_transcript_path": str(tp), "cwd": fx.main})
    assert code == 0, err
    ua = main_p / ".legion" / "battles" / "A" / "usage.jsonl"
    assert ua.is_file() and len(ua.read_text(encoding="utf-8").splitlines()) == 1
    assert not (main_p / ".legion" / "battles" / "B" / "usage.jsonl").exists()

    # 8. empreinte de A pendant que B vit sa vie, jusqu'a sa cloture
    snap = os.path.join(tmp, "snap.json")
    s = cli("artifact_check.py", "tree-snapshot", "--out", snap, "--root", wtA, "--battle", "A")
    assert s["ok"], s

    def verify(ok: bool = True) -> dict:
        return cli("artifact_check.py", "tree-verify", "--before", snap, "--fingerprint", s["fingerprint"],
                   "--root", wtA, "--battle", "A", "--guard", ok=ok)

    fx.commit_in(wtB, "b.txt")
    fx.git(wtB, "push", "-q", "-u", "origin", wts["B"]["branch"])
    fx.git(fx.main, "fetch", "-q", "--no-tags", "origin")
    bsc("activate", "B")
    assert verify()["ok"] is True
    fx.git(fx.main, "worktree", "remove", "--force", wtB)
    bj = main_p / ".legion" / "battles" / "B" / "battle.json"
    body = json.loads(bj.read_text(encoding="utf-8"))
    body.setdefault("phases", {})["reflect"] = {"status": "done"}
    bj.write_text(json.dumps(body), encoding="utf-8")
    closed = bsc("close", "--battle", "B")
    assert closed["ok"] and closed.get("unbound") == 1, closed
    assert verify()["ok"] is True
    fx.write(wtA, "docs/x", "x\n")
    v = verify(ok=False)
    assert v["ok"] is False and "docs/x" in json.dumps(v), v
    os.unlink(os.path.join(wtA, "docs", "x"))
    fx.write(fx.main, "x", "x\n")
    v = verify(ok=False)
    assert v["ok"] is False and "main-tree" in json.dumps(v), v
    os.unlink(os.path.join(fx.main, "x"))

    # 9. garde-fou des mutations : le pointeur est sur une autre battle (D) que celle de sidA
    bsc("init", "D", "--ticket", "GH#9", "--title", "t", "--profile", profile)
    assert pointer() == "D", pointer()
    code, msg = bash_guard(f"python {bs_py} set-guard --allow y --repo {fx.main}", "sidA")
    assert code == 2 and "A" in msg and "--battle" in msg, (code, msg)
    assert bash_guard(f"python {bs_py} set-guard --allow y --repo {fx.main} --battle A", "sidA")[0] == 0

    # 10. payloads sans cle (ancien harnais) : le pointeur decide comme avant
    assert write(plan_of("D"), None)[0] == 0
    assert write(plan_of("A"), None)[0] == 2

    # 11. close A : la liaison de sidA disparait
    assert bsc("session-status", "--battle", "A")["bound"] is True
    abj = main_p / ".legion" / "battles" / "A" / "battle.json"
    body = json.loads(abj.read_text(encoding="utf-8"))
    body.setdefault("phases", {})["reflect"] = {"status": "done"}
    abj.write_text(json.dumps(body), encoding="utf-8")
    fx.git(fx.main, "worktree", "remove", "--force", wtA)
    closed = bsc("close", "--battle", "A")
    assert closed["ok"] and closed.get("unbound") == 1, closed
    sdir = main_p / ".legion" / "sessions"
    left = [f.name for f in sdir.glob("*.json")
            if json.loads(f.read_text(encoding="utf-8")).get("battle") == "A"] if sdir.is_dir() else []
    assert left == [], left


if __name__ == "__main__":
    sys.exit(main())
