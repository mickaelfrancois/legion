"""Fan-in d'un lot parallèle legion (`/battle build --auto`, GH#72), version exécutable.

En mode parallèle, chaque builder travaille dans un worktree git isolé. Une fois le lot
vérifié, l'orchestrateur doit **réintégrer** les deltas dans l'**arbre d'intégration** : le dépôt principal, ou, pour une battle en
mode worktree, son `worktree.path` (`--root`). Ce script est le
**seul** qui écrit dans l'arbre de code (`battle_state.py` écrit `battle.json`, `artifact_check.py`
vérifie) ; il n'écrit jamais `battle.json` ni rien sous `.legion/`.

Contexte explicite (GH#157) : `--battle <id>` lit cette battle au lieu du pointeur ; `--root <wt>`
désigne l'arbre d'intégration. Une battle en worktree exige les deux, et `--root` doit être son
`worktree.path` (enregistré, non `prunable`, propriétaire = la battle) ; sans bloc `worktree`,
`--root` est absent ou égal au principal. `--root` sans `--battle` : erreur d'usage. Le cwd est le
principal ou `--root`. Les worktrees de builders restent sous `<principal>/.claude/worktrees/` ;
l'arbre d'intégration et le worktree d'une battle (vivante ou close) sont refusés comme builder.
`base`, `apply` et `cleanup` lisent et écrivent dans `root` (`git worktree remove` part du
principal ; la branche de l'arbre d'intégration n'est jamais supprimée).

Sous-commande `apply` (fail-closed, tout-ou-rien) :

1. **Contexte** : le cwd est la racine du dépôt principal (ou de l'arbre d'intégration) ; la battle
   existe (pointeur actif, ou `--battle <id>` vivante) ; son guard est valide et **armé** (`allow` non vide) ; `--base` est un commit existant ; chaque `--slice` est
   déclarée, au statut `in_progress`, sans doublon ; chaque worktree est enregistré, sous
   `<principal>/.claude/worktrees/`, sans lien symbolique, non `prunable`, non partagé, et son
   HEAD descend de `--base`. Le traitement suit l'ordre de `battle.json.slices`.
2. **Delta** par slice : index temporaire (`read-tree HEAD`, `add -A`, `write-tree`) qui donne
   l'arbre `T`, sans toucher ni l'index réel ni le worktree. Commits du builder et fichiers non
   suivis inclus, fichiers ignorés exclus.
3. **Contrôle de chemin** sur tout le lot : gitlink (dépôt imbriqué), `.legion/`, hors `allow` ou
   dans `deny` -> `refused` + `out_of_scope`.
4. **Contrôle de conflit** sur tout le lot : `overlap` (chemin touché par deux slices), `dirty`
   (le principal diffère de `<base>` sur un chemin du delta), `apply` (`git apply --check`).
5. **Écriture unique** : les patches sont concaténés puis appliqués par un seul `git apply
   --binary` (sans `--index` ni `--3way`) : seul l'arbre de travail change, HEAD et index du
   principal restent intacts. Un conflit ou un refus laisse le principal inchangé.

Sous-commande `cleanup` (après la vérification verte du projet) : mêmes validations d'entrée,
mais statut attendu `done`. Pour chaque slice, le delta est recalculé et **prouvé** présent dans
le principal (chaque fichier a le hash et le mode de l'arbre `T` — lien, exécutable ou normal ;
le bit exécutable est ignoré sous `core.fileMode=false` —, un fichier supprimé est absent) ; sinon le
worktree est conservé (`kept`). Si la preuve tient : `git worktree remove --force` (jamais deux
`--force` : un worktree verrouillé est conservé), puis `git branch -D` seulement pour la branche
extraite par ce worktree, si aucun autre worktree ne l'a extraite et si ce n'est pas la branche
courante du principal. Un refus est non bloquant pour l'orchestrateur (warning).

Sous-commande `base` (avant le lot, depuis la racine du principal) : fige l'arbre de travail du
principal, non commité et fichiers non suivis compris, en un commit **sans ref** (index
temporaire : `read-tree HEAD`, `add -A` sans `.legion/` ni `.claude/worktrees/`, `write-tree`,
`commit-tree -p HEAD`). Ni `HEAD`, ni index, ni branche, ni ref ne bougent. Arbre propre : rend
`HEAD` (aucun objet créé). Un gitlink (dépôt imbriqué) ou un chemin `.legion/` dans l'arbre figé
-> `refused`.

Sous-commande `align --base <sha>` (première action d'un builder, depuis la racine de son
worktree) : avance le worktree sur `<base>` par `git reset --keep` (la branche du harnais est
conservée, avancée). Sans effet si le HEAD descend déjà de `<base>`. Refus (fail-closed) si le
contexte n'est pas un worktree enregistré sous `<principal>/.claude/worktrees/`, si `<base>` est
inconnu, si le HEAD n'est ni un ancêtre de `<base>` ni (builder coupé depuis un principal qui a avancé,
sortie `origin:"main_head"`, réservée aux worktrees détachés ou sur une branche `worktree-agent-*`)
un ancêtre du HEAD du principal, si le worktree n'est pas propre ou
s'il est celui d'une battle. `--battle <id>` (optionnel) est validé, sans autre effet.

Usage :
    python fan_in.py base    [--battle <id> [--root <wt>]]
    python fan_in.py align   --base <sha> [--battle <id>]
    python fan_in.py apply   --base <sha> [--battle <id> [--root <wt>]] --slice <id> <worktree> [...]
    python fan_in.py cleanup --base <sha> [--battle <id> [--root <wt>]] --slice <id> <worktree> [...]
    python fan_in.py --self-test

Sortie : un objet JSON sur stdout
    succès  -> { ok:true, applied:[{slice, worktree, committed, files:[{status, path}]}] }
    refus   -> { ok:false, refused:true, reason, out_of_scope:[{slice, path, why}<=50],
                 out_of_scope_count, applied:[] }
    conflit -> { ok:false, conflict:{slice, kind, files<=50}, reason, applied:[] }
    faute   -> { ok:false, fault:true, reason, applied:[] }
base    -> { ok:true, base, head, frozen:<bool>, files:<n>, root } ; refus -> { ok:false, refused:true, reason }
align   -> { ok:true, aligned:<bool>, head, origin?:"base"|"main_head" } ; refus -> { ok:false, refused:true, reason }
cleanup -> { ok:bool, removed:[{slice, worktree, branch, branch_kept?}], kept:[{slice, worktree,
                 reason}] } ; `ok` = `kept` vide ; refus d'entrée -> { ok:false, refused:true, reason,
                 removed:[], kept:[] }
`apply` et `cleanup` ajoutent `root` en cas de succès (information).
`applied[].files` n'est jamais tronqué : c'est la source fiable de `slice done --files`.
Codes de sortie : 0 = ok ; 2 = conflit, refus ou faute (`ok:false`) ; 1 = erreur d'usage.
Limite assumée : `git apply` valide tout avant d'écrire ; seule une erreur d'E/S pendant
l'écriture pourrait laisser un état partiel.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import artifact_check as ac  # noqa: E402  (source unique des options git imposées)
import battle_state as bs  # noqa: E402

_Refuse = ac._Refuse
_MAX_LIST = ac._MAX_LIST
_WT_RE = re.compile(r"^[A-Za-z0-9._/:\\ -]+$")          # battle.md §D
_DIFF_OPTS = ("--binary", "--full-index", "--no-renames", "--no-color", "--no-ext-diff",
              "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/")
_APPLY_OPTS = ("apply", "--binary", "--whitespace=nowarn")
_GITLINK = "160000"


class _Conflict(Exception):
    """Conflit de fan-in : le principal reste inchangé."""

    def __init__(self, slice_id: str, kind: str, files: list[str], reason: str) -> None:
        super().__init__(reason)
        self.slice_id, self.kind, self.files, self.reason = slice_id, kind, files, reason


class _OutOfScope(Exception):
    """Chemins refusés (périmètre, `.legion/`, gitlink)."""

    def __init__(self, items: list[dict]) -> None:
        super().__init__("chemin hors périmètre")
        self.items = items


# --- git -----------------------------------------------------------------------------------

def _run(root: str, *args: str, data: bytes | None = None) -> tuple[int, bytes, str]:
    """Comme `artifact_check._git` (mêmes options imposées, même environnement) mais rend
    `(code, stdout, stderr)` au lieu de lever : nécessaire pour lire le stderr de `git apply`."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    env.pop("GIT_INDEX_FILE", None)
    try:
        p = subprocess.run(["git", "--no-optional-locks", *ac._GIT_FORCED, "-C", root, *args],
                           capture_output=True, env=env,
                           **({"input": data} if data is not None else {"stdin": subprocess.DEVNULL}))
    except (OSError, ValueError) as exc:
        raise _Refuse(f"git indisponible ({type(exc).__name__}: {exc})") from exc
    return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace")


def _wt_entries(main: str) -> list[dict]:
    """Worktrees enregistrés (`git worktree list --porcelain`), chemins réels."""
    out = ac._git(main, "worktree", "list", "--porcelain")
    entries: list[dict] = []
    cur: dict | None = None
    for line in out.split(b"\n"):
        if line.startswith(b"worktree "):
            cur = {"path": os.path.realpath(os.fsdecode(line[len(b"worktree "):])),
                   "branch": None, "detached": False, "prunable": False, "locked": False}
            entries.append(cur)
        elif cur is None:
            continue
        elif line.startswith(b"branch "):
            cur["branch"] = os.fsdecode(line[len(b"branch "):])
        elif line == b"detached":
            cur["detached"] = True
        elif line.startswith(b"prunable"):
            cur["prunable"] = True
        elif line.startswith(b"locked"):
            cur["locked"] = True
    return entries


# --- contexte et entrées -------------------------------------------------------------------

class _Ctx:
    """Contexte résolu d'une commande : `main` (dépôt principal, lieu de l'état `.legion/`), `root`
    (arbre d'intégration : `worktree.path` en mode worktree, sinon `main`), la battle, ses slices,
    son guard et la branche extraite par `root`."""
    __slots__ = ("main", "state", "root", "battle_id", "slices", "guard", "branch")

    def __init__(self, main: str, root: str, battle_id: str, slices: list[dict], guard: dict,
                 branch: str | None) -> None:
        self.main, self.state, self.root = main, main, root
        self.battle_id, self.slices, self.guard, self.branch = battle_id, slices, guard, branch


def _integration_root(main: str, battle_id: str, data: dict, battle: str | None,
                      root: str | None) -> str:
    """Arbre d'intégration de la battle, validé ; `_Refuse` sinon."""
    wt = data.get("worktree")
    if wt is None:
        if root is None or os.path.realpath(root) == main:
            return main
        raise _Refuse("battle sans worktree : --root doit être le dépôt principal (ou absent)")
    if not isinstance(wt, dict):
        raise _Refuse("bloc `worktree` invalide (fail-closed)")
    if battle is None or root is None:
        raise _Refuse(f"battle en worktree : `--battle {battle_id} --root <worktree.path>` obligatoires")
    wt_path = wt.get("path")
    if not isinstance(wt_path, str) or not wt_path:
        raise _Refuse("`worktree.path` absent ou invalide (fail-closed)")
    real = os.path.realpath(root)
    if real != os.path.realpath(wt_path):
        raise _Refuse("--root diffère de `worktree.path` de la battle")
    if not os.path.isdir(real) or ac._toplevel(real) != real:
        raise _Refuse("--root n'est pas la racine d'un worktree git")
    entry = next((e for e in _wt_entries(main) if e["path"] == real), None)
    if entry is None or entry["prunable"]:
        raise _Refuse("--root n'est pas un worktree enregistré (ou prunable)")
    owner = bs.worktree_battle_of(main, real)
    if owner is None or owner[0] != battle_id:
        raise _Refuse(f"--root n'est pas le worktree de la battle {battle_id}")
    return real


def _context(cwd: str, battle: str | None = None, root: str | None = None) -> _Ctx:
    """Contexte de la commande ; `_Refuse` si il n'est pas sûr (fail-closed).

    `battle` (`--battle`) lit cette battle au lieu du pointeur ; `root` (`--root`) désigne l'arbre
    d'intégration (obligatoire pour une battle en worktree). Le cwd est `main` ou `root`."""
    if battle is not None and not bs._ID_RE.fullmatch(battle):
        raise _Refuse(f"--battle : identifiant invalide ({battle!r})")
    if root is not None and battle is None:
        raise _Refuse("--root exige --battle")
    top = ac._toplevel(cwd)
    main = os.path.realpath(str(bs.main_repo_root(Path(cwd))))
    if battle is None:
        active = bs.load_active_battle(Path(main))
        if active is None:
            raise _Refuse("aucune battle active lisible (fail-closed)")
        battle_id, data = active
    else:
        data = bs._live_data(Path(main), battle)
        if data is None:
            raise _Refuse(f"battle {battle} illisible, close ou abandonnée (fail-closed)")
        battle_id = battle
    real_root = _integration_root(main, battle_id, data, battle, root)
    if os.path.realpath(cwd) != top or top not in (main, real_root):
        raise _Refuse("à lancer depuis la racine du dépôt principal ou de l'arbre d'intégration "
                      "(pas un autre worktree ni un sous-dossier)")
    guard, valid = bs.guard_of(data)
    if not valid:
        raise _Refuse("bloc `guard` invalide (fail-closed)")
    allow = guard.get("allow") or []
    if not allow:
        raise _Refuse("guard non armé (`guard.allow` vide) : le fan-in exige un périmètre")
    slices = data.get("slices")
    if not isinstance(slices, list) or not all(isinstance(s, dict) for s in slices):
        raise _Refuse("`battle.json.slices` absent ou invalide")
    entry = next((e for e in _wt_entries(main) if e["path"] == real_root), None)
    return _Ctx(main, real_root, battle_id, slices,
                {"allow": allow, "deny": guard.get("deny") or []}, entry["branch"] if entry else None)


def _validate_inputs(ctx: _Ctx, base: str, pairs: list[tuple[str, str]],
                     expected_status: str) -> list[tuple[str, str]]:
    """Valide `--base` et les paires `(slice, worktree)` ; rend `[(slice, wt_réel)]` dans l'ordre
    de `battle.json.slices`. Les worktrees de builders vivent sous `<main>/.claude/worktrees/` ;
    l'arbre d'intégration et les worktrees des battles (vivantes ou closes) sont refusés."""
    main, slices = ctx.main, ctx.slices
    if not ac._COMMIT_RE.fullmatch(base):
        raise _Refuse("--base n'est pas un sha hexadécimal complet")
    rc, _, _ = _run(main, "cat-file", "-e", f"{base}^{{commit}}")
    if rc != 0:
        raise _Refuse(f"--base {base[:12]} ne désigne aucun commit existant")
    status = {s.get("id"): s.get("status") for s in slices}
    seen_ids: set[str] = set()
    seen_wt: set[str] = set()
    entries = {e["path"]: e for e in _wt_entries(main)}
    wt_root = os.path.join(main, ".claude", "worktrees") + os.sep
    battle_wts = ac._other_battles(ctx.state, ctx.battle_id).paths | {ctx.root}
    by_id: dict[str, str] = {}
    for sid, wt in pairs:
        if not bs._ID_RE.fullmatch(sid):
            raise _Refuse(f"identifiant de slice invalide : {sid!r}")
        if sid in seen_ids:
            raise _Refuse(f"slice {sid} donnée deux fois")
        seen_ids.add(sid)
        if sid not in status:
            raise _Refuse(f"slice {sid} non déclarée dans battle.json")
        if status[sid] != expected_status:
            raise _Refuse(f"slice {sid} au statut {status[sid]!r} (attendu {expected_status!r})")
        if not _WT_RE.fullmatch(wt):
            raise _Refuse(f"chemin de worktree refusé pour {sid} (caractères hors liste)")
        absolute = os.path.abspath(wt)
        real = os.path.realpath(wt)
        if os.path.normcase(absolute) != os.path.normcase(real) or ac._has_symlink_between(main, absolute):
            raise _Refuse(f"worktree de {sid} : lien symbolique dans le chemin")
        if real == main:
            raise _Refuse(f"worktree de {sid} = dépôt principal")
        if real == ctx.root:
            raise _Refuse(f"worktree de {sid} = arbre d'intégration de la battle")
        if real in battle_wts:
            raise _Refuse(f"worktree de {sid} = worktree d'une battle")
        if not real.startswith(wt_root):
            raise _Refuse(f"worktree de {sid} hors de .claude/worktrees/")
        entry = entries.get(real)
        if entry is None:
            raise _Refuse(f"worktree de {sid} non enregistré par git")
        if entry["prunable"]:
            raise _Refuse(f"worktree de {sid} prunable (répertoire disparu)")
        if real in seen_wt:
            raise _Refuse(f"worktree partagé par deux slices ({sid})")
        seen_wt.add(real)
        rc, _, _ = _run(real, "merge-base", "--is-ancestor", base, "HEAD")
        if rc != 0:
            raise _Refuse(f"le HEAD du worktree de {sid} ne descend pas de --base")
        by_id[sid] = real
    return [(s.get("id"), by_id[s.get("id")]) for s in slices if s.get("id") in by_id]


# --- delta ---------------------------------------------------------------------------------

def _delta(main: str, wt: str, base: str) -> dict:
    """Delta d'un worktree par rapport à `base` : `{tree, committed, entries, patch}`.

    `entries` = `[{status, path, oldmode, newmode}]`. Index temporaire : ni l'index réel ni le
    worktree ne sont touchés."""
    head = ac._git(wt, "rev-parse", "HEAD").decode().strip()
    with tempfile.TemporaryDirectory() as td:
        idx = os.path.join(td, "index")
        ac._git(wt, "read-tree", "HEAD", index=idx)   # HEAD (pas base) : garde un fichier ignoré commité de force
        ac._git(wt, "add", "-A", index=idx)
        tree = ac._git(wt, "write-tree", index=idx).decode().strip()
    raw = ac._git(main, "diff", "--raw", "-z", "--no-renames", "--no-ext-diff", base, tree)
    toks = raw.split(b"\0")
    entries: list[dict] = []
    i = 0
    while i < len(toks) - 1:
        meta = toks[i]
        if not meta.startswith(b":"):
            i += 1
            continue
        fields = meta[1:].decode("ascii", "replace").split()
        entries.append({"status": fields[4][:1], "path": os.fsdecode(toks[i + 1]),
                        "oldmode": fields[0], "newmode": fields[1]})
        i += 2
    patch = ac._git(main, "diff", *_DIFF_OPTS, base, tree)
    return {"tree": tree, "committed": head != base, "entries": entries, "patch": patch}


def _scope_check(deltas: list[tuple[str, str, dict]], guard: dict) -> None:
    """Contrôle de chemin sur tout le lot ; `_OutOfScope` si au moins un chemin est refusé."""
    bad: list[dict] = []
    for sid, _, d in deltas:
        for e in d["entries"]:
            path = e["path"]
            why = None
            if _GITLINK in (e["oldmode"], e["newmode"]):
                why = "gitlink (dépôt imbriqué)"
            elif ac._is_legion(path):
                why = "état .legion/"
            elif not bs.glob_match(path, ac._ROOT_ALWAYS_ALLOW):
                if guard["deny"] and bs.glob_match(path, guard["deny"]):
                    why = "dans guard.deny"
                elif not bs.glob_match(path, guard["allow"]):
                    why = "hors guard.allow"
            if why:
                bad.append({"slice": sid, "path": path, "why": why})
    if bad:
        raise _OutOfScope(bad)


# --- conflits ------------------------------------------------------------------------------

def _main_hash(main: str, rel: str) -> str | None:
    """Hash git du contenu actuel de `rel` dans le principal (filtres et eol comme `git add`) ;
    `None` si absent ; `"<dir>"` pour un dossier."""
    p = os.path.join(main, *rel.split("/"))
    if os.path.islink(p):
        return ac._git(main, "hash-object", "--stdin", data=os.fsencode(os.readlink(p))).decode().strip()
    if os.path.isdir(p):
        return "<dir>"
    if not os.path.lexists(p):
        return None
    return ac._git(main, "hash-object", f"--path={rel}", "--", p).decode().strip()


def _base_hash(main: str, base: str, rel: str) -> str | None:
    rc, out, _ = _run(main, "rev-parse", "--verify", "-q", f"{base}:{rel}")
    return out.decode().strip() if rc == 0 else None


_APPLY_ERR = (re.compile(r"^error: patch failed: (.+?):\d+$"),
              re.compile(r"^error: (.+?): (?:already exists in working directory|does not exist in "
                         r"index|patch does not apply|No such file or directory)$"))


def _apply_files(stderr: str, candidates: list[str]) -> list[str]:
    """Fichiers cités par le stderr de `git apply` ; à défaut, tous les `candidates`."""
    found: list[str] = []
    for line in stderr.splitlines():
        for rx in _APPLY_ERR:
            m = rx.match(line.strip())
            if m and m.group(1) in candidates and m.group(1) not in found:
                found.append(m.group(1))
    return found or list(candidates)


def _conflict_check(main: str, base: str, deltas: list[tuple[str, str, dict]]) -> None:
    """`_Conflict` au premier conflit dans l'ordre des slices, avant toute écriture."""
    touched: dict[str, str] = {}
    for sid, _, d in deltas:
        paths = [e["path"] for e in d["entries"]]
        over = [p for p in paths if p in touched]
        if over:
            raise _Conflict(sid, "overlap", over,
                            f"chemins déjà touchés par {touched[over[0]]} : {', '.join(over[:3])}")
        dirty = [p for p in paths if _main_hash(main, p) != _base_hash(main, base, p)]
        if dirty:
            raise _Conflict(sid, "dirty", dirty,
                            f"le principal diffère de la base sur : {', '.join(dirty[:3])}")
        if paths:
            rc, _, err = _run(main, *_APPLY_OPTS, "--check", "-", data=d["patch"])
            if rc != 0:
                raise _Conflict(sid, "apply", _apply_files(err, paths),
                                "git apply --check refuse le patch")
        for p in paths:
            touched[p] = sid


def _write_batch(main: str, deltas: list[tuple[str, str, dict]]) -> None:
    """Écriture unique : un seul `git apply` du lot concaténé (vérifié d'abord en entier)."""
    patch = b"".join(d["patch"] for _, _, d in deltas)
    if not patch:
        return
    rc, _, err = _run(main, *_APPLY_OPTS, "--check", "-", data=patch)
    if rc != 0:
        sid = next((s for s, _, d in reversed(deltas) if d["entries"]), deltas[-1][0])
        allp = [e["path"] for _, _, d in deltas for e in d["entries"]]
        raise _Conflict(sid, "apply", _apply_files(err, allp), "git apply --check du lot refusé")
    rc, _, err = _run(main, *_APPLY_OPTS, "-", data=patch)
    if rc != 0:
        raise _Refuse(f"git apply a échoué : {err.strip().splitlines()[0] if err.strip() else rc}")


# --- commande apply ------------------------------------------------------------------------

def _fail(**kw) -> dict:
    return {"ok": False, "applied": [], **kw}


def apply_batch(cwd: str, base: str, pairs: list[tuple[str, str]], battle: str | None = None,
                root: str | None = None) -> dict:
    """Cœur de `apply` ; ne lève jamais : toute faute devient un objet `ok:false`. Lit, contrôle
    et écrit dans l'arbre d'intégration (`root`)."""
    try:
        ctx = _context(cwd, battle, root)
        ordered = _validate_inputs(ctx, base, pairs, "in_progress")
        deltas = [(sid, wt, _delta(ctx.main, wt, base)) for sid, wt in ordered]
        _scope_check(deltas, ctx.guard)
        _conflict_check(ctx.root, base, deltas)
        _write_batch(ctx.root, deltas)
    except _OutOfScope as exc:
        return _fail(refused=True, reason="chemins hors périmètre ou interdits",
                     out_of_scope=exc.items[:_MAX_LIST], out_of_scope_count=len(exc.items))
    except _Conflict as exc:
        return _fail(conflict={"slice": exc.slice_id, "kind": exc.kind, "files": exc.files[:_MAX_LIST]},
                     reason=exc.reason)
    except _Refuse as exc:
        return _fail(refused=True, reason=str(exc))
    except Exception as exc:  # noqa: BLE001 - contrat : jamais d'exception non rattrapée
        return _fail(fault=True, reason=f"{type(exc).__name__}: {exc}")
    applied = [{"slice": sid, "worktree": wt, "committed": d["committed"],
                "files": [{"status": e["status"], "path": e["path"]} for e in d["entries"]]}
               for sid, wt, d in deltas]
    return {"ok": True, "applied": applied, "root": ctx.root}


# --- commande cleanup ----------------------------------------------------------------------

def _main_mode(main: str, rel: str) -> str | None:
    """Mode git du fichier `rel` du principal (`120000`, `100755`, `100644`), `None` sinon."""
    p = os.path.join(main, *rel.split("/"))
    if os.path.islink(p):
        return "120000"
    if not os.path.isfile(p):
        return None
    return "100755" if os.stat(p).st_mode & 0o111 else "100644"


def _file_mode_tracked(main: str) -> bool:
    """`core.fileMode` du principal (vrai par défaut) : à faux, git ignore le bit exécutable."""
    rc, out, _ = _run(main, "config", "--bool", "core.fileMode")
    return rc != 0 or out.decode().strip() != "false"


def _same_mode(want: str, here: str | None, exec_bit: bool) -> bool:
    if exec_bit or "120000" in (want, here):
        return want == here                           # lien / fichier : toujours distingués
    return here in ("100644", "100755")


def _prove_in_main(main: str, d: dict) -> list[str]:
    """Chemins du delta `d` que le principal ne contient pas, contenu **et** mode (liste vide =
    preuve faite). Sous `core.fileMode=false`, seul le bit exécutable est ignoré."""
    missing: list[str] = []
    exec_bit = _file_mode_tracked(main)
    for e in d["entries"]:
        path, here = e["path"], _main_hash(main, e["path"])
        if _GITLINK in (e["oldmode"], e["newmode"]):
            missing.append(path)                      # dépôt imbriqué : jamais prouvable
        elif e["status"] == "D":
            if here is not None:
                missing.append(path)
        else:
            rc, out, _ = _run(main, "rev-parse", "--verify", "-q", f"{d['tree']}:{path}")
            if (rc != 0 or here != out.decode().strip()
                    or not _same_mode(e["newmode"], _main_mode(main, path), exec_bit)):
                missing.append(path)
    return missing


def _remove_one(main: str, root: str, sid: str, wt: str,
                entries: list[dict]) -> tuple[dict | None, str | None]:
    """Retire le worktree et, sous conditions, sa branche (jamais celle de l'arbre d'intégration
    `root`). `(removed, None)` ou `(None, raison)`."""
    me = next((e for e in entries if e["path"] == wt), None)
    if me is None:
        return None, "worktree introuvable dans git worktree list"
    if me["locked"]:
        return None, "worktree verrouillé"
    branch = None if me["detached"] else me["branch"]
    rc, _, err = _run(main, "worktree", "remove", "--force", wt)
    if rc != 0:
        return None, f"git worktree remove a échoué : {err.strip().splitlines()[0] if err.strip() else rc}"
    out: dict = {"slice": sid, "worktree": wt, "branch": None}
    if not branch:
        return out, None
    short = branch[len("refs/heads/"):] if branch.startswith("refs/heads/") else branch
    main_branch = next((e["branch"] for e in entries if e["path"] == main), None)
    root_branch = next((e["branch"] for e in entries if e["path"] == root), None)
    if branch == main_branch:
        out["branch_kept"] = "branche courante du principal"
    elif branch == root_branch:
        out["branch_kept"] = "branche de l'arbre d'intégration (battle)"
    elif any(e["path"] != wt and e["branch"] == branch for e in entries):
        out["branch_kept"] = "branche extraite dans un autre worktree"
    else:
        rc, _, err = _run(main, "branch", "-D", short)
        if rc == 0:
            out["branch"] = short
        else:
            out["branch_kept"] = f"git branch -D a échoué : {err.strip().splitlines()[0] if err.strip() else rc}"
    return out, None


def cleanup_batch(cwd: str, base: str, pairs: list[tuple[str, str]], battle: str | None = None,
                  root: str | None = None) -> dict:
    """Cœur de `cleanup` ; ne lève jamais. `ok` vaut vrai si aucun worktree n'est conservé. La
    preuve se fait contre l'arbre d'intégration (`root`)."""
    removed: list[dict] = []
    kept: list[dict] = []
    ctx_root: str | None = None
    try:
        ctx = _context(cwd, battle, root)
        ctx_root = ctx.root
        main = ctx.main
        ordered = _validate_inputs(ctx, base, pairs, "done")
        entries = _wt_entries(main)                   # instantané : les branches se jugent avant tout retrait
        for sid, wt in ordered:
            try:
                missing = _prove_in_main(ctx.root, _delta(main, wt, base))
            except _Refuse as exc:
                kept.append({"slice": sid, "worktree": wt, "reason": f"preuve impossible : {exc}"})
                continue
            if missing:
                kept.append({"slice": sid, "worktree": wt,
                             "reason": f"delta absent du principal : {', '.join(missing[:3])}"})
                continue
            done, why = _remove_one(main, ctx.root, sid, wt, entries)
            if done is None:
                kept.append({"slice": sid, "worktree": wt, "reason": why})
            else:
                removed.append(done)
    except _Refuse as exc:
        return {"ok": False, "refused": True, "reason": str(exc), "removed": [], "kept": []}
    except Exception as exc:  # noqa: BLE001 - contrat : jamais d'exception non rattrapée
        return {"ok": False, "fault": True, "reason": f"{type(exc).__name__}: {exc}",
                "removed": removed[:_MAX_LIST], "kept": kept[:_MAX_LIST]}
    return {"ok": not kept, "removed": removed[:_MAX_LIST], "kept": kept[:_MAX_LIST], "root": ctx_root}


# --- commandes base et align ---------------------------------------------------------------

_BASE_EXCLUDES = (".legion", ".claude/worktrees")
_BASE_IDENT = ("-c", "user.name=legion", "-c", "user.email=legion@localhost", "-c", "commit.gpgsign=false")


def _diff_entries(root: str, a: str, b: str) -> list[tuple[str, str, str]]:
    """`[(oldmode, newmode, path)]` de `git diff --raw` entre deux arbres ou commits."""
    toks = ac._git(root, "diff", "--raw", "-z", "--no-renames", "--no-ext-diff", a, b).split(b"\0")
    out: list[tuple[str, str, str]] = []
    i = 0
    while i < len(toks) - 1:
        if not toks[i].startswith(b":"):
            i += 1
            continue
        fields = toks[i][1:].decode("ascii", "replace").split()
        out.append((fields[0], fields[1], os.fsdecode(toks[i + 1])))
        i += 2
    return out


def base_batch(cwd: str, battle: str | None = None, root: str | None = None) -> dict:
    """Cœur de `base` ; ne lève jamais : toute faute devient un objet `ok:false`. Fige l'arbre
    d'intégration (`root`)."""
    try:
        ctx = _context(cwd, battle, root)
        main = ctx.root
        head = ac._git(main, "rev-parse", "HEAD").decode().strip()
        head_tree = ac._git(main, "rev-parse", "HEAD^{tree}").decode().strip()
        with tempfile.TemporaryDirectory() as td:
            idx = os.path.join(td, "index")
            ac._git(main, "read-tree", "HEAD", index=idx)
            ac._git(main, "add", "-A", index=idx)
            # Les exclusions ne passent pas en pathspec (`git add` refuse un pathspec qui vise un
            # chemin ignoré) : on les retire de l'index puis on y remet la version de HEAD.
            ac._git(main, "rm", "-r", "--cached", "-q", "--ignore-unmatch", "--", *_BASE_EXCLUDES, index=idx)
            kept = ac._git(main, "ls-tree", "-r", "-z", "HEAD", "--", *_BASE_EXCLUDES)
            info = b"".join(m.group(1) + b" " + m.group(2) + b" 0\t" + m.group(3) + b"\n"
                            for m in (re.match(rb"(\d+) \w+ ([0-9a-f]+)\t(.*)", e, re.S)
                                      for e in kept.split(b"\0") if e) if m)
            if info:
                ac._git(main, "update-index", "--index-info", data=info, index=idx)
            tree = ac._git(main, "write-tree", index=idx).decode().strip()
        entries = _diff_entries(main, head_tree, tree)
        for old, new, path in entries:
            if _GITLINK in (old, new):
                raise _Refuse(f"gitlink (dépôt imbriqué) dans l'arbre du principal : {path}")
            if ac._is_legion(path):
                raise _Refuse(f"chemin .legion/ dans l'arbre figé : {path}")
        if tree == head_tree:
            return {"ok": True, "base": head, "head": head, "frozen": False, "files": 0, "root": main}
        base = ac._git(main, *_BASE_IDENT, "commit-tree", tree, "-p", head, "-m",
                       "legion: base du lot").decode().strip()
        if not ac._COMMIT_RE.fullmatch(base):
            raise _Refuse("commit-tree n'a pas rendu de sha")
        return {"ok": True, "base": base, "head": head, "frozen": True, "files": len(entries),
                "root": main}
    except _Refuse as exc:
        return {"ok": False, "refused": True, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - contrat : jamais d'exception non rattrapée
        return {"ok": False, "fault": True, "reason": f"{type(exc).__name__}: {exc}"}


def _align_context(cwd: str, base: str, battle: str | None = None) -> tuple[str, str]:
    """`(worktree du builder, principal)` en chemins réels ; `_Refuse` si le contexte n'est pas sûr."""
    top = ac._toplevel(cwd)
    if os.path.realpath(cwd) != top:
        raise _Refuse("à lancer depuis la racine du worktree du builder (pas un sous-dossier)")
    main = os.path.realpath(str(bs.main_repo_root(Path(cwd))))
    if top == main:
        raise _Refuse("à lancer depuis un worktree de builder, pas depuis le dépôt principal")
    if not top.startswith(os.path.join(main, ".claude", "worktrees") + os.sep):
        raise _Refuse("worktree hors de .claude/worktrees/")
    if os.path.normcase(os.path.abspath(cwd)) != os.path.normcase(top) or ac._has_symlink_between(main, top):
        raise _Refuse("lien symbolique dans le chemin du worktree")
    if bs.worktree_battle_of(main, top) is not None or top in ac._other_battles(main, "").paths:
        raise _Refuse("worktree d'une battle : `align` est réservé aux worktrees de builders")
    if battle is not None:
        if not bs._ID_RE.fullmatch(battle):
            raise _Refuse(f"--battle : identifiant invalide ({battle!r})")
        if bs._live_data(Path(main), battle) is None:
            raise _Refuse(f"battle {battle} illisible, close ou abandonnée (fail-closed)")
    entry = next((e for e in _wt_entries(main) if e["path"] == top), None)
    if entry is None:
        raise _Refuse("worktree non enregistré par git")
    if entry["prunable"]:
        raise _Refuse("worktree prunable")
    if not ac._COMMIT_RE.fullmatch(base):
        raise _Refuse("--base n'est pas un sha hexadécimal complet")
    rc, _, _ = _run(top, "cat-file", "-e", f"{base}^{{commit}}")
    if rc != 0:
        raise _Refuse(f"--base {base[:12]} ne désigne aucun commit existant")
    return top, main


def align_worktree(cwd: str, base: str, battle: str | None = None) -> dict:
    """Cœur de `align` ; ne lève jamais : toute faute devient un objet `ok:false`.

    Origine acceptée du HEAD : ancêtre de `<base>` (`origin:"base"`), ou, worktree propre, ancêtre
    du HEAD du principal (`origin:"main_head"`, builder coupé depuis un principal qui a avancé
    depuis `create` : rien n'est perdu, ses commits restent dans l'histoire du principal). Cette
    seconde origine exige un worktree de harnais : HEAD détaché ou branche `worktree-agent-*`."""
    try:
        top, main = _align_context(cwd, base, battle)
        head = ac._git(top, "rev-parse", "HEAD").decode().strip()
        rc, _, _ = _run(top, "merge-base", "--is-ancestor", base, "HEAD")
        if rc == 0:
            return {"ok": True, "aligned": False, "head": head}
        rc, _, _ = _run(top, "merge-base", "--is-ancestor", "HEAD", base)
        origin = "base"
        if rc != 0:
            entry = next((e for e in _wt_entries(main) if e["path"] == top), None)
            branch = (entry or {}).get("branch") or ""
            if not ((entry or {}).get("detached") or branch.startswith("refs/heads/worktree-agent-")):
                raise _Refuse("le HEAD du worktree n'est pas un ancêtre de --base, et sa branche "
                              "n'est pas une branche de harnais (worktree-agent-*) : alignement refusé")
            main_head = ac._git(main, "rev-parse", "HEAD").decode().strip()
            rc, _, _ = _run(top, "merge-base", "--is-ancestor", "HEAD", main_head)
            if rc != 0:
                raise _Refuse("le HEAD du worktree n'est pas un ancêtre de --base ni du HEAD du "
                              "principal (alignement refusé)")
            origin = "main_head"
        if ac._git(top, "status", "--porcelain", "-uall").strip():
            raise _Refuse("worktree non propre : alignement refusé (aucun travail n'est écrasé)")
        ac._git(top, "reset", "--keep", base)
        now = ac._git(top, "rev-parse", "HEAD").decode().strip()
        if now != base or ac._git(top, "status", "--porcelain", "-uall").strip():
            raise _Refuse("alignement non vérifié (HEAD différent de --base ou statut non vide)")
        return {"ok": True, "aligned": True, "head": now, "origin": origin}
    except _Refuse as exc:
        return {"ok": False, "refused": True, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - contrat : jamais d'exception non rattrapée
        return {"ok": False, "fault": True, "reason": f"{type(exc).__name__}: {exc}"}


# --- CLI -----------------------------------------------------------------------------------

def _usage(msg: str) -> int:
    print(f"usage invalide : {msg}", file=sys.stderr)
    return 1


def _parse_opts(rest: list[str], allowed: tuple[str, ...], pairs_ok: bool = False) -> dict | str:
    """Options `--x <valeur>` (ordre libre, chacune au plus une fois) parmi `allowed`, plus les
    `--slice <id> <worktree>` si `pairs_ok`. `{opt: valeur, "pairs": [...]}` ou un message d'usage.
    `--battle` est validé par `_ID_RE` ; `--root` exige `--battle`."""
    out: dict = {"pairs": []}
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in allowed:
            if a in out:
                return f"{a} donné deux fois"
            if i + 1 >= len(rest) or rest[i + 1].startswith("--"):
                return f"{a} attend une valeur"
            out[a] = rest[i + 1]
            i += 2
        elif pairs_ok and a == "--slice":
            if i + 2 >= len(rest) or rest[i + 1].startswith("--") or rest[i + 2].startswith("--"):
                return "--slice attend <id> <worktree>"
            out["pairs"].append((rest[i + 1], rest[i + 2]))
            i += 3
        else:
            return f"argument inattendu : {a}"
    if "--battle" in out and not bs._ID_RE.fullmatch(out["--battle"]):
        return f"--battle : identifiant invalide ({out['--battle']!r})"
    if "--root" in out and "--battle" not in out:
        return "--root exige --battle"
    return out


def _parse_batch(rest: list[str]) -> tuple[str, list[tuple[str, str]], str | None, str | None] | str:
    """`(base, [(slice, worktree)], battle, root)` ou un message d'usage."""
    opts = _parse_opts(rest, ("--base", "--battle", "--root"), pairs_ok=True)
    if isinstance(opts, str):
        return opts
    base = opts.get("--base")
    if base is None or not ac._COMMIT_RE.fullmatch(base):
        return "--base <sha hexadécimal complet> obligatoire"
    if not opts["pairs"]:
        return "au moins un --slice <id> <worktree> est obligatoire"
    return base, opts["pairs"], opts.get("--battle"), opts.get("--root")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in args:
        return _self_test()
    if args and args[0] == "base":
        opts = _parse_opts(args[1:], ("--battle", "--root"))
        if isinstance(opts, str):
            return _usage("base [--battle <id> [--root <worktree>]] : " + opts)
        res = base_batch(os.getcwd(), opts.get("--battle"), opts.get("--root"))
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res["ok"] else 2
    if args and args[0] == "align":
        opts = _parse_opts(args[1:], ("--base", "--battle"))
        if isinstance(opts, str) or "--root" in opts:
            return _usage("align --base <sha hexadécimal complet> [--battle <id>]"
                          + (f" : {opts}" if isinstance(opts, str) else ""))
        base = opts.get("--base")
        if base is None or not ac._COMMIT_RE.fullmatch(base):
            return _usage("align --base <sha hexadécimal complet> [--battle <id>]")
        res = align_worktree(os.getcwd(), base, opts.get("--battle"))
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res["ok"] else 2
    if not args or args[0] not in ("apply", "cleanup"):
        return _usage("sous-commande attendue : base | align | apply | cleanup | --self-test")
    parsed = _parse_batch(args[1:])
    if isinstance(parsed, str):
        return _usage(f"{args[0]} --base <sha> [--battle <id> [--root <worktree>]] --slice <id> "
                      f"<worktree> [...] : " + parsed)
    res = (apply_batch if args[0] == "apply" else cleanup_batch)(os.getcwd(), *parsed)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["ok"] else 2


# --- Self-test : vrais dépôts et worktrees git jetables ------------------------------------

def _sha256_of(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class _Fx:
    """Dépôt principal (battle `B` active, slices 1 et 2 `in_progress`) + worktrees `w1`, `w2`
    sous `.claude/worktrees/` et `wt_out` hors arbre, tous détachés sur `base`."""

    def __init__(self, base_dir: str, fx: tuple) -> None:
        main, w1, wt_out = (os.path.realpath(str(p)) for p in fx)
        self.main, self.w1, self.wt_out = main, w1, wt_out
        self.w2 = os.path.join(os.path.dirname(w1), "w2")
        self.env = {k: v for k, v in os.environ.items() if k not in bs._GIT_ENV_DROP}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        self.write(main, ".gitignore", ".legion/\n.claude/\nbin/\n")
        self.write(main, "src/a.txt", "a\n")
        self.write(main, "docs/b.txt", "b\n")
        self.git(main, "add", "-A")
        self.git(main, "commit", "-q", "-m", "base")
        self.base = self.git(main, "rev-parse", "HEAD").strip()
        for wt in (self.w1, self.wt_out):
            self.git(wt, "checkout", "-q", "--detach", self.base)
        self.git(main, "worktree", "add", "-q", "--detach", self.w2, self.base)
        self.w2 = os.path.realpath(self.w2)
        self.battle_path = os.path.join(main, ".legion", "battles", "B", "battle.json")
        self.set_battle()

    def set_battle(self, allow=("src/**", "docs/**"), deny=("src/secret/**",),
                   statuses=("in_progress", "in_progress"), guard="default") -> None:
        if guard == "default":
            guard = {"allow": list(allow), "deny": list(deny)}
        body = {"id": "B", "guard": guard,
                "slices": [{"id": "slice-1", "status": statuses[0]},
                           {"id": "slice-2", "status": statuses[1]}]}
        os.makedirs(os.path.dirname(self.battle_path), exist_ok=True)
        with open(self.battle_path, "w", encoding="utf-8") as fh:
            json.dump(body, fh)

    def git(self, cwd: str, *a: str) -> str:
        p = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                            "-c", "commit.gpgsign=false", *a], cwd=cwd, env=self.env,
                           capture_output=True, text=True, encoding="utf-8",
                           stdin=subprocess.DEVNULL, timeout=60)
        assert p.returncode == 0, (a, p.stderr)
        return p.stdout

    @staticmethod
    def write(root: str, rel: str, data: str | bytes) -> None:
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data.encode() if isinstance(data, str) else data)

    def read(self, root: str, rel: str) -> bytes | None:
        p = os.path.join(root, *rel.split("/"))
        if not os.path.isfile(p):
            return None
        with open(p, "rb") as fh:
            return fh.read()

    def run(self, *a: str, cwd: str | None = None) -> tuple[int, dict, str]:
        p = subprocess.run([sys.executable, os.path.abspath(__file__), *a], cwd=cwd or self.main,
                           env=self.env, capture_output=True, text=True, encoding="utf-8")
        try:
            out = json.loads(p.stdout) if p.stdout.strip() else {}
        except ValueError:
            out = {"raw": p.stdout}
        return p.returncode, out, p.stderr

    def apply(self, *pairs: tuple[str, str], base: str | None = None,
              cwd: str | None = None) -> tuple[int, dict, str]:
        args = ["apply", "--base", base or self.base]
        for sid, wt in pairs:
            args += ["--slice", sid, wt]
        return self.run(*args, cwd=cwd)

    def cleanup(self, *pairs: tuple[str, str]) -> tuple[int, dict, str]:
        args = ["cleanup", "--base", self.base]
        for sid, wt in pairs:
            args += ["--slice", sid, wt]
        return self.run(*args)

    def base_cmd(self, cwd: str | None = None) -> tuple[int, dict, str]:
        return self.run("base", cwd=cwd)

    def align(self, wt: str, base: str | None = None, cwd: str | None = None) -> tuple[int, dict, str]:
        return self.run("align", "--base", base or self.base, cwd=cwd or wt)

    def foundation(self) -> None:
        """Fondation non commitée dans le principal : fichier modifié, fichier neuf, ignoré, état."""
        self.write(self.main, "src/a.txt", "a-found\n")
        self.write(self.main, "src/found.txt", "found\n")
        self.write(self.main, "bin/x", "ignored\n")
        self.write(self.main, ".legion/battles/B/extra.md", "state\n")

    def tree_of(self, commit: str) -> list[str]:
        return self.git(self.main, "ls-tree", "-r", "--name-only", commit).split()

    def worktrees(self) -> list[str]:
        return [os.path.realpath(l[len("worktree "):]) for l in
                self.git(self.main, "worktree", "list", "--porcelain").splitlines()
                if l.startswith("worktree ")]

    def branches(self) -> list[str]:
        return self.git(self.main, "branch", "--format=%(refname:short)").split()

    def both(self) -> tuple[tuple[str, str], tuple[str, str]]:
        return ("slice-1", self.w1), ("slice-2", self.w2)

    def ac(self, *a: str) -> tuple[int, dict]:
        p = subprocess.run([sys.executable, os.path.join(_SCRIPTS_DIR, "artifact_check.py"), *a],
                           cwd=self.main, env=self.env, capture_output=True, text=True, encoding="utf-8")
        try:
            return p.returncode, json.loads(p.stdout)
        except ValueError:
            return p.returncode, {"raw": p.stdout}

    def fingerprint(self, tmp: str) -> str:
        rc, out = self.ac("tree-snapshot", "--out", os.path.join(tmp, "fp.json"))
        assert rc == 0 and out["ok"], out
        return out["fingerprint"]

    def stable(self) -> tuple:
        """Ce qui ne doit jamais bouger : battle.json, index et HEAD du principal et des worktrees."""
        return (_sha256_of(self.battle_path),
                *(self.git(r, "ls-files", "-s") + self.git(r, "rev-parse", "HEAD")
                  for r in (self.main, self.w1, self.w2)))


def _with_fx(name: str, body) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fx = None
        try:
            raw = bs._git_worktree_fixture(Path(os.path.realpath(tmp)))
            fx = _Fx(tmp, raw) if raw is not None else None
        except (OSError, subprocess.SubprocessError, AssertionError):
            fx = None
        if fx is None:
            print(f"SKIP: {name} (git absent ou inutilisable)", file=sys.stderr)
            return
        body(fx, tmp)


def _f1_edits(fx: _Fx) -> None:
    fx.write(fx.w1, "src/a.txt", "a1\n")
    fx.write(fx.w1, "src/n.txt", "new\n")
    os.remove(os.path.join(fx.w2, "docs", "b.txt"))
    fx.write(fx.w2, "docs/c.bin", bytes(range(256)))


def _t_import_smoke() -> None:                               # R2
    for n in ("_git", "_Refuse", "_toplevel", "_is_legion", "_has_symlink_between", "_GIT_FORCED",
              "_ROOT_ALWAYS_ALLOW", "_COMMIT_RE", "_MAX_LIST"):
        assert hasattr(ac, n), n
    for n in ("resolve_state_root", "main_repo_root", "load_active_battle", "glob_match", "_ID_RE",
              "guard_of"):
        assert hasattr(bs, n), n


def _t_f1_nominal() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        _f1_edits(fx)
        head, idx = fx.git(fx.main, "rev-parse", "HEAD"), fx.git(fx.main, "ls-files", "-s")
        rc, out, _ = fx.apply(*fx.both())
        assert rc == 0 and out["ok"] is True, (rc, out)
        assert fx.read(fx.main, "src/a.txt") == b"a1\n" and fx.read(fx.main, "src/n.txt") == b"new\n"
        assert fx.read(fx.main, "docs/b.txt") is None
        assert fx.read(fx.main, "docs/c.bin") == bytes(range(256))
        by = {a["slice"]: {(f["status"], f["path"]) for f in a["files"]} for a in out["applied"]}
        assert by["slice-1"] == {("M", "src/a.txt"), ("A", "src/n.txt")}, by
        assert by["slice-2"] == {("D", "docs/b.txt"), ("A", "docs/c.bin")}, by
        assert all(a["committed"] is False for a in out["applied"])
        assert fx.git(fx.main, "rev-parse", "HEAD") == head          # HEAD intact
        assert fx.git(fx.main, "ls-files", "-s") == idx              # index intact
    _with_fx("F1", body)


def _t_f2_order() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        _f1_edits(fx)
        rc, out, _ = fx.apply(("slice-2", fx.w2), ("slice-1", fx.w1))
        assert rc == 0, out
        assert [a["slice"] for a in out["applied"]] == ["slice-1", "slice-2"], out
    _with_fx("F2", body)


def _t_f3_overlap() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.write(fx.w1, "src/a.txt", "one\n")
        fx.write(fx.w2, "src/a.txt", "two\n")
        before = fx.fingerprint(tmp)
        rc, out, _ = fx.apply(*fx.both())
        assert rc == 2 and out["ok"] is False and out["applied"] == [], (rc, out)
        assert out["conflict"] == {"slice": "slice-2", "kind": "overlap", "files": ["src/a.txt"]}, out
        assert fx.fingerprint(tmp) == before
        assert fx.read(fx.main, "src/a.txt") == b"a\n"
    _with_fx("F3", body)


def _t_f4_dirty_main() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.write(fx.main, "src/a.txt", "dirty\n")
        fx.write(fx.w1, "src/a.txt", "one\n")
        fx.write(fx.w2, "docs/c.bin", b"\x00\x01")
        rc, out, _ = fx.apply(*fx.both())
        assert rc == 2 and out["conflict"]["kind"] == "dirty", out
        assert out["conflict"]["slice"] == "slice-1" and out["conflict"]["files"] == ["src/a.txt"], out
        assert fx.read(fx.main, "docs/c.bin") is None                # lot atomique
        assert fx.read(fx.main, "src/a.txt") == b"dirty\n"
    _with_fx("F4", body)


def _t_f5_untracked_present() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.write(fx.main, "src/n.txt", "mine\n")
        fx.write(fx.w1, "src/n.txt", "theirs\n")
        before = fx.fingerprint(tmp)
        rc, out, _ = fx.apply(*fx.both())
        assert rc == 2 and out["conflict"]["kind"] in ("dirty", "apply"), out
        assert "src/n.txt" in out["conflict"]["files"], out
        assert fx.fingerprint(tmp) == before and fx.read(fx.main, "src/n.txt") == b"mine\n"
    _with_fx("F5", body)


def _t_f6_out_of_scope_and_deny() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        before = fx.fingerprint(tmp)
        fx.write(fx.w1, "other/x.txt", "x\n")
        rc, out, _ = fx.apply(*fx.both())
        assert rc == 2 and out["refused"] is True and out["applied"] == [], out
        assert [(o["slice"], o["path"]) for o in out["out_of_scope"]] == [("slice-1", "other/x.txt")]
        os.remove(os.path.join(fx.w1, "other", "x.txt"))
        fx.write(fx.w2, "src/secret/k.txt", "k\n")
        rc, out, _ = fx.apply(*fx.both())
        assert rc == 2 and out["refused"] is True, out
        assert [(o["slice"], o["path"]) for o in out["out_of_scope"]] == [("slice-2", "src/secret/k.txt")]
        assert fx.fingerprint(tmp) == before
    _with_fx("F6", body)


def _t_f7_legion_forced() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.set_battle(allow=("src/**", "docs/**", ".legion/**", "**"))   # seul `.legion/` doit bloquer
        fx.write(fx.w1, ".legion/x", "state\n")
        fx.git(fx.w1, "add", "-f", ".legion/x")
        fx.git(fx.w1, "commit", "-q", "-m", "legion")
        rc, out, _ = fx.apply(*fx.both())
        assert rc == 2 and out["refused"] is True, out
        assert out["out_of_scope"][0]["path"] == ".legion/x", out
        assert not os.path.exists(os.path.join(fx.main, ".legion", "x"))
    _with_fx("F7", body)


def _t_f8_committed_and_dirty() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.write(fx.w1, "src/a.txt", "committed\n")
        fx.git(fx.w1, "commit", "-q", "-am", "builder commit")
        fx.write(fx.w1, "src/n.txt", "uncommitted\n")
        rc, out, _ = fx.apply(("slice-1", fx.w1))
        assert rc == 0, out
        assert out["applied"][0]["committed"] is True, out
        assert fx.read(fx.main, "src/a.txt") == b"committed\n"
        assert fx.read(fx.main, "src/n.txt") == b"uncommitted\n"
    _with_fx("F8", body)


def _t_f9_head_not_descendant() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.git(fx.w1, "checkout", "-q", "--orphan", "other")
        fx.git(fx.w1, "commit", "-q", "-m", "unrelated")
        rc, out, _ = fx.apply(("slice-1", fx.w1))
        assert rc == 2 and out["refused"] is True and "descend" in out["reason"], out
    _with_fx("F9", body)


def _t_f10_invalid_worktrees() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        ghost = os.path.join(fx.main, ".claude", "worktrees", "ghost")
        os.makedirs(ghost)
        cases = [(("slice-1", ghost),), (("slice-1", fx.wt_out),), (("slice-1", fx.main),),
                 (("slice-1", fx.w1), ("slice-2", fx.w1)),
                 (("slice-1", os.path.join(fx.w1, "src")),), (("slice-1", fx.w1 + ";rm"),)]
        for pairs in cases:
            rc, out, _ = fx.apply(*pairs)
            assert rc == 2 and out["refused"] is True and out["applied"] == [], (pairs, out)
    _with_fx("F10", body)


def _t_f11_slice_problems() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        for pairs in ((("slice-9", fx.w1),), (("slice-1", fx.w1), ("slice-1", fx.w2)),
                      (("bad id!", fx.w1),)):
            rc, out, _ = fx.apply(*pairs)
            assert rc == 2 and out["refused"] is True, (pairs, out)
        fx.set_battle(statuses=("done", "in_progress"))
        rc, out, _ = fx.apply(("slice-1", fx.w1))
        assert rc == 2 and out["refused"] is True and "statut" in out["reason"], out
    _with_fx("F11", body)


def _t_f12_fail_closed() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.write(fx.w1, "src/n.txt", "n\n")
        fx.set_battle(allow=())                                   # guard non armé
        rc, out, _ = fx.apply(("slice-1", fx.w1))
        assert rc == 2 and out["refused"] is True and "armé" in out["reason"], out
        fx.set_battle(guard="oops")                               # bloc invalide
        rc, out, _ = fx.apply(("slice-1", fx.w1))
        assert rc == 2 and out["refused"] is True, out
        fx.set_battle(guard=None)                                 # guard absent = non armé
        rc, out, _ = fx.apply(("slice-1", fx.w1))
        assert rc == 2 and out["refused"] is True, out
        fx.set_battle()
        ptr = os.path.join(fx.main, ".legion", "active-battle")
        os.remove(ptr)                                            # plus de battle active
        rc, out, _ = fx.apply(("slice-1", fx.w1))
        assert rc == 2 and out["refused"] is True, out
        assert fx.read(fx.main, "src/n.txt") is None
    _with_fx("F12", body)


def _t_f13_nested_repo() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        nested = os.path.join(fx.w1, "src", "nested")
        os.makedirs(nested)
        fx.git(nested, "init", "-q")
        fx.write(nested, "f.txt", "f\n")
        fx.git(nested, "add", "-A")
        fx.git(nested, "commit", "-q", "-m", "n")
        rc, out, _ = fx.apply(*fx.both())
        assert rc == 2 and out["refused"] is True and out["applied"] == [], out
        assert not os.path.exists(os.path.join(fx.main, "src", "nested"))
    _with_fx("F13", body)


def _t_f14_state_untouched() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.write(fx.w1, "src/a.txt", "one\n")
        fx.write(fx.w2, "src/a.txt", "two\n")
        before = fx.stable()
        rc, _, _ = fx.apply(*fx.both())                           # échec : overlap
        assert rc == 2 and fx.stable() == before
        fx.write(fx.w2, "src/a.txt", "a\n")                       # slice-2 redevient neutre
        fx.write(fx.w2, "docs/c.txt", "c\n")
        rc, out, _ = fx.apply(*fx.both())                         # succès
        assert rc == 0, out
        assert fx.stable() == before
    _with_fx("F14", body)


def _t_f15_integrity_envelope() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        _f1_edits(fx)
        snap = os.path.join(tmp, "before.json")
        rc, out = fx.ac("tree-snapshot", "--out", snap)
        assert rc == 0, out
        rc, res, _ = fx.apply(*fx.both())
        assert rc == 0, res
        rc, ver = fx.ac("tree-verify", "--before", snap, "--fingerprint", out["fingerprint"], "--guard")
        assert rc == 0 and ver["ok"] is True, ver
        assert not ver.get("out_of_scope") and not ver.get("fault"), ver
    _with_fx("F15", body)


def _t_f16_cleanup_nominal() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        _f1_edits(fx)
        fx.git(fx.main, "worktree", "remove", "--force", fx.wt_out)
        fx.git(fx.w1, "checkout", "-q", "-b", "harness/x")
        rc, out, _ = fx.apply(*fx.both())
        assert rc == 0, out
        fx.set_battle(statuses=("done", "done"))
        rc, out, _ = fx.cleanup(*fx.both())
        assert rc == 0 and out["ok"] is True and out["kept"] == [], (rc, out)
        assert {r["slice"]: r["branch"] for r in out["removed"]} == {"slice-1": "harness/x", "slice-2": None}, out
        assert fx.worktrees() == [fx.main], fx.worktrees()
        assert "harness/x" not in fx.branches()
        assert fx.read(fx.main, "src/n.txt") == b"new\n"              # le code reste dans le principal
    _with_fx("F16", body)


def _t_f17_cleanup_before_apply() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        _f1_edits(fx)
        fx.git(fx.w1, "checkout", "-q", "-b", "harness/x")
        fx.set_battle(statuses=("done", "done"))
        before = fx.worktrees()
        rc, out, _ = fx.cleanup(*fx.both())
        assert rc == 2 and out["ok"] is False and out["removed"] == [], (rc, out)
        assert [k["slice"] for k in out["kept"]] == ["slice-1", "slice-2"], out
        assert fx.worktrees() == before and "harness/x" in fx.branches()
        assert fx.read(fx.w1, "src/n.txt") == b"new\n"                # rien n'est perdu
        fx.set_battle()                                               # statut in_progress : refus d'entrée
        rc, out, _ = fx.cleanup(*fx.both())
        assert rc == 2 and out["refused"] is True and fx.worktrees() == before, out
    _with_fx("F17", body)


def _t_f18_cleanup_branch_kept() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        main_branch = fx.git(fx.main, "rev-parse", "--abbrev-ref", "HEAD").strip()
        fx.git(fx.w1, "checkout", "-q", "--ignore-other-worktrees", main_branch)   # branche courante du principal
        fx.git(fx.w2, "checkout", "-q", "-b", "harness/y")
        fx.git(fx.wt_out, "checkout", "-q", "--ignore-other-worktrees", "harness/y")  # extraite ailleurs
        fx.set_battle(statuses=("done", "done"))
        rc, out, _ = fx.cleanup(*fx.both())
        assert rc == 0 and out["ok"] is True, (rc, out)
        assert all(r["branch"] is None and r.get("branch_kept") for r in out["removed"]), out
        assert main_branch in fx.branches() and "harness/y" in fx.branches()
        assert fx.worktrees() == sorted([fx.main, fx.wt_out], key=fx.worktrees().index), fx.worktrees()
    _with_fx("F18", body)


def _t_f19_usage() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        bad = (["bogus"], [], ["apply"], ["apply", "--base", "zz", "--slice", "s", "w"],
               ["apply", "--base", fx.base, "--slice", "slice-1"],
               ["apply", "--base", fx.base], ["apply", "--base", fx.base, "--slice", "a", "b", "--x"],
               ["cleanup"], ["cleanup", "--base", fx.base])
        for a in bad:
            rc, out, err = fx.run(*a)
            assert rc == 1 and err.strip() and out == {}, (a, rc, err)
    _with_fx("F19", body)


def _t_f20_bounded_output() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        for i in range(60):
            fx.write(fx.w1, f"other/f{i}.txt", "x\n")
        rc, out, _ = fx.apply(("slice-1", fx.w1))
        assert rc == 2 and out["refused"] is True, out
        assert len(out["out_of_scope"]) <= _MAX_LIST and out["out_of_scope_count"] == 60, out
    _with_fx("F20", body)


def _t_f21_mode_and_symlink() -> None:
    if os.name == "nt":
        print("SKIP: F21 (modes POSIX et liens symboliques)", file=sys.stderr)
        return

    def body(fx: _Fx, tmp: str) -> None:
        fx.write(fx.w1, "src/run.sh", "#!/bin/sh\n")
        os.chmod(os.path.join(fx.w1, "src", "run.sh"), 0o755)
        os.symlink("a.txt", os.path.join(fx.w1, "src", "link"))
        rc, out, _ = fx.apply(("slice-1", fx.w1))
        assert rc == 0, out
        assert os.stat(os.path.join(fx.main, "src", "run.sh")).st_mode & 0o111
        assert os.path.islink(os.path.join(fx.main, "src", "link"))
        assert os.readlink(os.path.join(fx.main, "src", "link")) == "a.txt"
    _with_fx("F21", body)


def _t_f22_cleanup_mode_lost() -> None:
    """F22/F23 : `cleanup` garde le worktree si le mode du delta manque au principal."""
    if os.name == "nt":
        print("SKIP: F22 (modes POSIX et liens symboliques)", file=sys.stderr)
        return

    def body(fx: _Fx, tmp: str) -> None:
        fx.git(fx.main, "worktree", "remove", "--force", fx.wt_out)
        fx.write(fx.w1, "src/run.sh", "#!/bin/sh\n")
        os.chmod(os.path.join(fx.w1, "src", "run.sh"), 0o755)
        os.symlink("a.txt", os.path.join(fx.w1, "src", "link"))
        rc, out, _ = fx.apply(("slice-1", fx.w1))
        assert rc == 0, out
        fx.set_battle(statuses=("done", "done"))
        run_sh, link = os.path.join(fx.main, "src", "run.sh"), os.path.join(fx.main, "src", "link")
        os.chmod(run_sh, 0o644)                                                   # F22
        os.remove(link)
        with open(link, "w", encoding="utf-8", newline="") as fh:                # F23 : même contenu
            fh.write("a.txt")
        rc, out, _ = fx.cleanup(("slice-1", fx.w1))
        assert rc == 2 and out["removed"] == [], out
        reason = out["kept"][0]["reason"]
        assert "src/run.sh" in reason and "src/link" in reason, reason
        assert fx.w1 in fx.worktrees()
        # core.fileMode=false : le bit exécutable est ignoré, le lien devenu fichier reste refusé.
        fx.git(fx.main, "config", "core.fileMode", "false")
        rc, out, _ = fx.cleanup(("slice-1", fx.w1))
        reason = out["kept"][0]["reason"]
        assert rc == 2 and "src/link" in reason and "src/run.sh" not in reason, out
        os.remove(link)
        os.symlink("a.txt", link)
        rc, out, _ = fx.cleanup(("slice-1", fx.w1))
        assert rc == 0 and out["kept"] == [] and fx.w1 not in fx.worktrees(), out
    _with_fx("F22", body)


def _t_b1_base_nominal() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.foundation()
        rc, out, _ = fx.base_cmd()
        assert rc == 0 and out["ok"] is True and out["frozen"] is True, (rc, out)
        base, head = out["base"], fx.git(fx.main, "rev-parse", "HEAD").strip()
        assert base != head and out["head"] == head and out["files"] == 2, out
        fx.git(fx.main, "merge-base", "--is-ancestor", head, base)
        files = fx.tree_of(base)
        assert "src/found.txt" in files and not any(f.startswith(("bin/", ".legion/")) for f in files), files
        assert fx.git(fx.main, "show", f"{base}:src/a.txt") == "a-found\n"
    _with_fx("B1", body)


def _t_b2_base_clean_fallback() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.write(fx.main, "bin/x", "ignored\n")
        fx.write(fx.main, ".legion/battles/B/extra.md", "state\n")
        rc, out, _ = fx.base_cmd()
        head = fx.git(fx.main, "rev-parse", "HEAD").strip()
        assert rc == 0 and out["base"] == head and out["frozen"] is False, (rc, out)
    _with_fx("B2", body)


def _t_b3_base_unignored_state() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.write(fx.main, ".gitignore", "bin/\n")                    # `.legion/` et `.claude/` non ignorés
        fx.write(fx.main, ".legion/battles/B/extra.md", "state\n")
        rc, out, _ = fx.base_cmd()
        assert rc == 0 and out["frozen"] is True, (rc, out)
        files = fx.tree_of(out["base"])
        assert not any(f.startswith((".legion", ".claude")) for f in files), files
        assert fx.git(fx.main, "show", f"{out['base']}:.gitignore") == "bin/\n"
        entries = fx.git(fx.main, "ls-tree", "-r", out["base"])
        assert "160000" not in entries, entries
    _with_fx("B3", body)


def _t_b4_base_invariants() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.foundation()
        before = (fx.git(fx.main, "rev-parse", "HEAD"), fx.git(fx.main, "ls-files", "-s"),
                  fx.git(fx.main, "for-each-ref"), fx.fingerprint(tmp), fx.git(fx.main, "branch"))
        rc, out, _ = fx.base_cmd()
        assert rc == 0 and out["frozen"] is True, out
        after = (fx.git(fx.main, "rev-parse", "HEAD"), fx.git(fx.main, "ls-files", "-s"),
                 fx.git(fx.main, "for-each-ref"), fx.fingerprint(tmp), fx.git(fx.main, "branch"))
        assert before == after
    _with_fx("B4", body)


def _t_b5_base_nested_repo() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        nested = os.path.join(fx.main, "src", "nested")
        os.makedirs(nested)
        fx.git(nested, "init", "-q")
        fx.write(nested, "f.txt", "f\n")
        fx.git(nested, "add", "-A")
        fx.git(nested, "commit", "-q", "-m", "n")
        before = fx.git(fx.main, "for-each-ref")
        rc, out, _ = fx.base_cmd()
        assert rc == 2 and out["refused"] is True and "gitlink" in out["reason"], (rc, out)
        assert fx.git(fx.main, "for-each-ref") == before
    _with_fx("B5", body)


def _t_b6_base_context() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        rc, out, _ = fx.base_cmd(cwd=fx.w1)
        assert rc == 2 and out["refused"] is True, (rc, out)
        rc, out, _ = fx.base_cmd(cwd=os.path.join(fx.main, "src"))
        assert rc == 2 and out["refused"] is True, (rc, out)
        fx.set_battle(allow=())
        rc, out, _ = fx.base_cmd()
        assert rc == 2 and out["refused"] is True and "armé" in out["reason"], (rc, out)
        rc, _, err = fx.run("base", "extra")
        assert rc == 1 and err.strip()
    _with_fx("B6", body)


def _t_a1_align_nominal() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.foundation()
        base = fx.base_cmd()[1]["base"]
        rc, out, _ = fx.align(fx.w1, base)
        assert rc == 0 and out["ok"] is True and out["aligned"] is True and out["head"] == base, (rc, out)
        assert fx.git(fx.w1, "rev-parse", "HEAD").strip() == base
        assert fx.read(fx.w1, "src/found.txt") == b"found\n" and fx.read(fx.w1, "src/a.txt") == b"a-found\n"
        assert fx.git(fx.w1, "status", "--porcelain", "-uall") == ""
        rc, out, _ = fx.align(fx.w1, base)
        assert rc == 0 and out["aligned"] is False, out
    _with_fx("A1", body)


def _t_a2_align_branch() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.foundation()
        base = fx.base_cmd()[1]["base"]
        fx.git(fx.w1, "checkout", "-q", "-b", "harness/x")
        rc, out, _ = fx.align(fx.w1, base)
        assert rc == 0 and out["aligned"] is True, (rc, out)
        assert fx.git(fx.main, "rev-parse", "harness/x").strip() == base
        assert fx.git(fx.w1, "symbolic-ref", "HEAD").strip() == "refs/heads/harness/x"
    _with_fx("A2", body)


def _t_a3_align_refusals() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.foundation()
        base = fx.base_cmd()[1]["base"]
        fx.write(fx.w1, "src/dirty.txt", "mine\n")                   # worktree sale
        rc, out, _ = fx.align(fx.w1, base)
        assert rc == 2 and out["refused"] is True, (rc, out)
        assert fx.read(fx.w1, "src/dirty.txt") == b"mine\n"
        assert fx.git(fx.w1, "rev-parse", "HEAD").strip() == fx.base
        fx.git(fx.w2, "checkout", "-q", "--orphan", "other")          # HEAD sans lien
        fx.git(fx.w2, "commit", "-q", "-m", "unrelated")
        head2 = fx.git(fx.w2, "rev-parse", "HEAD")
        rc, out, _ = fx.align(fx.w2, base)
        assert rc == 2 and out["refused"] is True, (rc, out)
        assert fx.git(fx.w2, "rev-parse", "HEAD") == head2
        rc, out, _ = fx.align(fx.main, base, cwd=fx.main)             # depuis le principal
        assert rc == 2 and out["refused"] is True, (rc, out)
        assert fx.git(fx.main, "rev-parse", "HEAD").strip() == fx.base
        rc, out, _ = fx.align(fx.wt_out, base)                        # hors .claude/worktrees/
        assert rc == 2 and out["refused"] is True, (rc, out)
        os.remove(os.path.join(fx.w1, "src", "dirty.txt"))
        rc, out, _ = fx.align(fx.w1, "0" * 40)                        # base inconnue
        assert rc == 2 and out["refused"] is True, (rc, out)
        rc, out, _ = fx.align(fx.w1, base, cwd=os.path.join(fx.w1, "src"))   # sous-dossier
        assert rc == 2 and out["refused"] is True, (rc, out)
        for a in (["align"], ["align", "--base"], ["align", "--base", "zz"],
                  ["align", "--base", base, "x"]):
            rc, out, err = fx.run(*a, cwd=fx.w1)
            assert rc == 1 and err.strip() and out == {}, (a, rc, err)
    _with_fx("A3", body)


def _t_a4_align_base_is_head() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        rc, out, _ = fx.base_cmd()
        assert out["base"] == fx.base and out["frozen"] is False, out
        rc, out, _ = fx.align(fx.w1, fx.base)
        assert rc == 0 and out["ok"] is True and out["aligned"] is False, (rc, out)
    _with_fx("A4", body)


def _t_e1_end_to_end() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.foundation()
        fx.git(fx.main, "worktree", "remove", "--force", fx.wt_out)
        fx.git(fx.w1, "checkout", "-q", "-b", "harness/x")
        rc, out, _ = fx.base_cmd()
        assert rc == 0 and out["frozen"] is True, out
        base = out["base"]
        fx.base = base
        snap = os.path.join(tmp, "s1.json")
        rc, s1 = fx.ac("tree-snapshot", "--out", snap)
        assert rc == 0, s1
        head, idx = fx.git(fx.main, "rev-parse", "HEAD"), fx.git(fx.main, "ls-files", "-s")
        for wt in (fx.w1, fx.w2):
            rc, out, _ = fx.align(wt, base)
            assert rc == 0 and out["aligned"] is True, (wt, out)
        fx.write(fx.w1, "src/a.txt", "a-builder\n")
        fx.write(fx.w1, "src/found.txt", "found-builder\n")
        fx.write(fx.w2, "docs/u2.txt", "u2\n")
        rc, ver = fx.ac("tree-verify", "--before", snap, "--fingerprint", s1["fingerprint"],
                        "--batch-worktrees")
        assert rc == 0 and ver["ok"] is True, ver
        rc, res, _ = fx.apply(*fx.both())
        assert rc == 0 and res["ok"] is True, res
        assert fx.read(fx.main, "src/a.txt") == b"a-builder\n"
        assert fx.read(fx.main, "src/found.txt") == b"found-builder\n"
        assert fx.read(fx.main, "docs/u2.txt") == b"u2\n"
        assert fx.git(fx.main, "rev-parse", "HEAD") == head and fx.git(fx.main, "ls-files", "-s") == idx
        rc, ver = fx.ac("tree-verify", "--before", snap, "--fingerprint", s1["fingerprint"], "--guard")
        assert rc == 0 and ver["ok"] is True, ver
        fx.set_battle(statuses=("done", "done"))
        rc, out, _ = fx.cleanup(*fx.both())
        assert rc == 0 and out["ok"] is True and out["kept"] == [], (rc, out)
        assert fx.worktrees() == [fx.main] and "harness/x" not in fx.branches(), fx.worktrees()
    _with_fx("E1", body)


def _t_e2_not_aligned() -> None:
    def body(fx: _Fx, tmp: str) -> None:
        fx.foundation()
        base = fx.base_cmd()[1]["base"]
        fx.write(fx.w1, "docs/u1.txt", "u1\n")                       # w1 jamais aligné
        before = (fx.read(fx.main, "src/a.txt"), fx.git(fx.main, "rev-parse", "HEAD"))
        rc, out, _ = fx.apply(("slice-1", fx.w1), base=base)
        assert rc == 2 and out["refused"] is True and "descend" in out["reason"], (rc, out)
        assert (fx.read(fx.main, "src/a.txt"), fx.git(fx.main, "rev-parse", "HEAD")) == before
        assert fx.read(fx.main, "docs/u1.txt") is None
    _with_fx("E2", body)


class _WtFx(_Fx):
    """`_Fx` plus une battle `A` en mode worktree (`<main>/.claude/worktrees/A` sur `me/A`) et deux
    builders `agent-1` / `agent-2` coupés depuis le HEAD du principal. Le pointeur reste sur la
    battle `B` (in-place) : `A` ne se lit que par `--battle A`."""

    def __init__(self, base_dir: str, fx: tuple) -> None:
        super().__init__(base_dir, fx)
        self.wt_a = self.add_wt("A", "me/A", self.base)
        self.a_path = os.path.join(self.main, ".legion", "battles", "A", "battle.json")
        self.set_a()
        self.a1 = self.add_wt("agent-1", "worktree-agent-1", "HEAD")
        self.a2 = self.add_wt("agent-2", "worktree-agent-2", "HEAD")

    def add_wt(self, name: str, branch: str, start: str) -> str:
        path = os.path.join(self.main, ".claude", "worktrees", name)
        self.git(self.main, "worktree", "add", "-q", "-b", branch, path, start)
        return os.path.realpath(path)

    def set_a(self, statuses=("in_progress", "in_progress"), aborted=None, worktree="default") -> None:
        if worktree == "default":
            worktree = {"path": self.wt_a, "branch": "me/A"}
        body = {"id": "A", "guard": {"allow": ["src/**", "docs/**", "lib/**"], "deny": []},
                "slices": [{"id": "slice-1", "status": statuses[0]},
                           {"id": "slice-2", "status": statuses[1]}], "worktree": worktree}
        if aborted:
            body["aborted"] = aborted
        os.makedirs(os.path.dirname(self.a_path), exist_ok=True)
        with open(self.a_path, "w", encoding="utf-8") as fh:
            json.dump(body, fh)

    def other_battle(self, name: str) -> str:
        """Battle `name` (conforme : worktree sous `.claude/worktrees/<name>`), vivante."""
        path = self.add_wt(name, f"me/{name}", self.base)
        bp = os.path.join(self.main, ".legion", "battles", name, "battle.json")
        os.makedirs(os.path.dirname(bp), exist_ok=True)
        with open(bp, "w", encoding="utf-8") as fh:
            json.dump({"id": name, "worktree": {"path": path, "branch": f"me/{name}"}}, fh)
        return path

    def fi(self, cmd: str, *pairs: tuple[str, str], base: str | None = None, battle: str | None = "A",
           root: str | None = "default", cwd: str | None = None) -> tuple[int, dict, str]:
        args = [cmd]
        if cmd != "base":
            args += ["--base", base or self.base]
        if battle is not None:
            args += ["--battle", battle]
        if root is not None and cmd != "align":
            args += ["--root", self.wt_a if root == "default" else root]
        for sid, wt in pairs:
            args += ["--slice", sid, wt]
        return self.run(*args, cwd=cwd)

    def main_state(self) -> tuple:
        return (self.git(self.main, "rev-parse", "HEAD"), self.git(self.main, "ls-files", "-s"),
                self.git(self.main, "status", "--porcelain", "-uall"),
                self.git(self.main, "for-each-ref"))

    def pair(self) -> tuple[tuple[str, str], tuple[str, str]]:
        return ("slice-1", self.a1), ("slice-2", self.a2)


def _with_wt_fx(name: str, body) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        wfx = None
        try:
            raw = bs._git_worktree_fixture(Path(os.path.realpath(tmp)))
            wfx = _WtFx(tmp, raw) if raw is not None else None
        except (OSError, subprocess.SubprocessError, AssertionError):
            wfx = None
        if wfx is None:
            print(f"SKIP: {name} (git absent ou inutilisable)", file=sys.stderr)
            return
        body(wfx, tmp)


def _t_w1_worktree_flow() -> None:                                   # W1 W2 W3 W4
    def body(fx: _WtFx, tmp: str) -> None:
        fx.write(fx.wt_a, "src/found.txt", "found\n")                # fondation non commitée dans wtA
        before = fx.main_state()
        rc, out, _ = fx.fi("base")
        assert rc == 0 and out["frozen"] is True and out["root"] == fx.wt_a, (rc, out)
        base = out["base"]
        assert fx.git(fx.main, "rev-parse", f"{base}^") == fx.git(fx.wt_a, "rev-parse", "HEAD")
        assert fx.main_state() == before                                # le principal est intact
        for wt in (fx.a1, fx.a2):
            rc, out, _ = fx.fi("align", base=base, root=None, cwd=wt)
            assert rc == 0 and out["aligned"] is True and out["head"] == base, (rc, out)
        fx.write(fx.a1, "src/n.txt", "new\n")
        fx.write(fx.a2, "docs/u2.txt", "u2\n")
        fx.write(fx.a2, "lib/y.txt", "y\n")                           # W4 : dans l'allow de A, pas de B
        rc, out, _ = fx.fi("apply", *fx.pair(), base=base)
        assert rc == 0 and out["ok"] is True and out["root"] == fx.wt_a, (rc, out)
        assert fx.read(fx.wt_a, "src/n.txt") == b"new\n" and fx.read(fx.wt_a, "lib/y.txt") == b"y\n"
        assert fx.read(fx.wt_a, "src/found.txt") == b"found\n"
        assert fx.read(fx.main, "src/n.txt") is None
        assert fx.main_state()[:3] == before[:3]                        # HEAD, index, status du principal
        fx.set_a(statuses=("done", "done"))
        rc, out, _ = fx.fi("cleanup", *fx.pair(), base=base)
        assert rc == 0 and out["ok"] is True and len(out["removed"]) == 2, (rc, out)
        assert {r["branch"] for r in out["removed"]} == {"worktree-agent-1", "worktree-agent-2"}, out
        assert "me/A" in fx.branches() and not any(b.startswith("worktree-agent") for b in fx.branches())
        assert fx.wt_a in fx.worktrees() and fx.a1 not in fx.worktrees()
        assert fx.main_state()[:3] == before[:3]                        # refs : seules les branches builders partent
    _with_wt_fx("W1", body)


def _t_w5_root_refusals() -> None:
    def body(fx: _WtFx, tmp: str) -> None:
        for root in (fx.main, fx.a1, os.path.join(fx.wt_a, "src"), os.path.join(tmp, "absent")):
            rc, out, _ = fx.fi("apply", ("slice-1", fx.a1), root=root)
            assert rc == 2 and out["refused"] is True, (root, rc, out)
            rc, out, _ = fx.fi("base", root=root)
            assert rc == 2 and out["refused"] is True, (root, rc, out)
    _with_wt_fx("W5", body)


def _t_w6_worktree_battle_needs_root() -> None:
    def body(fx: _WtFx, tmp: str) -> None:
        rc, out, _ = fx.fi("apply", ("slice-1", fx.a1), root=None)
        assert rc == 2 and out["refused"] is True and "--root" in out["reason"], (rc, out)
        bs._write_pointer(Path(fx.main), "A")                          # mode pointeur, battle en worktree
        rc, out, _ = fx.fi("apply", ("slice-1", fx.a1), battle=None, root=None)
        assert rc == 2 and out["refused"] is True and "--root" in out["reason"], (rc, out)
        rc, out, _ = fx.fi("base", battle=None, root=None)
        assert rc == 2 and out["refused"] is True, (rc, out)
        bs._write_pointer(Path(fx.main), "B")
    _with_wt_fx("W6", body)


def _t_w7_usage() -> None:
    def body(fx: _WtFx, tmp: str) -> None:
        for a in (["base", "--root", fx.wt_a], ["base", "--battle", "../x"], ["base", "--battle"],
                  ["base", "--battle", "A", "--battle", "A"], ["align", "--base", fx.base, "--root", "x"],
                  ["apply", "--base", fx.base, "--root", fx.wt_a, "--slice", "slice-1", fx.a1],
                  ["apply", "--base", fx.base, "--battle", "a b", "--slice", "slice-1", fx.a1]):
            rc, out, err = fx.run(*a)
            assert rc == 1 and err.strip() and out == {}, (a, rc, err)
    _with_wt_fx("W7", body)


def _t_w8_battle_unusable() -> None:
    def body(fx: _WtFx, tmp: str) -> None:
        rc, out, _ = fx.fi("base", battle="Z", root=fx.wt_a)               # inconnue
        assert rc == 2 and out["refused"] is True, (rc, out)
        fx.set_a(aborted={"reason": "x"})                                  # abandonnée
        rc, out, _ = fx.fi("base")
        assert rc == 2 and out["refused"] is True, (rc, out)
        rc, out, _ = fx.fi("align", base=fx.base, root=None, cwd=fx.a1)
        assert rc == 2 and out["refused"] is True, (rc, out)
        with open(fx.a_path, "w", encoding="utf-8") as fh:                 # illisible
            fh.write("{oops")
        rc, out, _ = fx.fi("base")
        assert rc == 2 and out["refused"] is True, (rc, out)
    _with_wt_fx("W8", body)


def _t_w9_battle_worktrees_refused() -> None:
    def body(fx: _WtFx, tmp: str) -> None:
        wt_c = fx.other_battle("C")
        for wt in (fx.wt_a, wt_c):
            rc, out, _ = fx.fi("apply", ("slice-1", wt))
            assert rc == 2 and out["refused"] is True and "battle" in out["reason"], (wt, rc, out)
        assert fx.read(fx.wt_a, "src/a.txt") == b"a\n"
    _with_wt_fx("W9", body)


def _t_w10_align_refused_in_battle_worktree() -> None:
    def body(fx: _WtFx, tmp: str) -> None:
        wt_c = fx.other_battle("C")
        fx.write(fx.main, "src/m.txt", "m\n")
        fx.git(fx.main, "add", "-A")
        fx.git(fx.main, "commit", "-q", "-m", "m1")
        base = fx.git(fx.main, "rev-parse", "HEAD").strip()
        heads = {w: fx.git(w, "rev-parse", "HEAD") for w in (fx.wt_a, wt_c)}
        for wt in (fx.wt_a, wt_c):
            rc, out, _ = fx.fi("align", base=base, root=None, cwd=wt)
            assert rc == 2 and out["refused"] is True, (wt, rc, out)
            assert fx.git(wt, "rev-parse", "HEAD") == heads[wt]
        assert fx.git(fx.main, "rev-parse", "me/A") == heads[fx.wt_a]
    _with_wt_fx("W10", body)


def _t_w11_w12_align_main_head() -> None:
    def body(fx: _WtFx, tmp: str) -> None:
        fx.write(fx.wt_a, "src/found.txt", "found\n")
        base = fx.fi("base")[1]["base"]
        fx.write(fx.main, "docs/m1.txt", "m1\n")                          # le principal avance après create
        fx.git(fx.main, "add", "-A")
        fx.git(fx.main, "commit", "-q", "-m", "m1")
        a3 = fx.add_wt("agent-3", "worktree-agent-3", "HEAD")
        rc, out, _ = fx.fi("align", base=base, root=None, cwd=a3)             # W11
        assert rc == 0 and out["aligned"] is True and out["origin"] == "main_head", (rc, out)
        assert out["head"] == base and fx.git(a3, "rev-parse", "HEAD").strip() == base
        a4 = fx.add_wt("agent-4", "worktree-agent-4", "HEAD")                 # W12 : worktree sale
        fx.write(a4, "docs/mine.txt", "mine\n")
        head4 = fx.git(a4, "rev-parse", "HEAD")
        rc, out, _ = fx.fi("align", base=base, root=None, cwd=a4)
        assert rc == 2 and out["refused"] is True, (rc, out)
        assert fx.read(a4, "docs/mine.txt") == b"mine\n" and fx.git(a4, "rev-parse", "HEAD") == head4
        a5 = fx.add_wt("agent-5", "worktree-agent-5", "HEAD")                 # W12 : orphelin
        fx.git(a5, "checkout", "-q", "--orphan", "other")
        fx.git(a5, "commit", "-q", "-m", "unrelated")
        head5 = fx.git(a5, "rev-parse", "HEAD")
        rc, out, _ = fx.fi("align", base=base, root=None, cwd=a5)
        assert rc == 2 and out["refused"] is True, (rc, out)
        assert fx.git(a5, "rev-parse", "HEAD") == head5
        fx.git(fx.main, "branch", "merged-feature", "HEAD")                    # R1 : branche non harnais, fusionnée
        a6 = os.path.join(fx.main, ".claude", "worktrees", "feat-6")
        fx.git(fx.main, "worktree", "add", "-q", a6, "merged-feature")
        a6 = os.path.realpath(a6)
        fx.write(fx.main, "docs/m2.txt", "m2\n")
        fx.git(fx.main, "add", "-A")
        fx.git(fx.main, "commit", "-q", "-m", "m2")
        head6 = fx.git(a6, "rev-parse", "HEAD")
        rc, out, _ = fx.fi("align", base=base, root=None, cwd=a6)
        assert rc == 2 and out["refused"] is True, (rc, out)
        assert fx.git(a6, "rev-parse", "HEAD") == head6
        a7 = os.path.join(fx.main, ".claude", "worktrees", "det-7")           # R1 : HEAD détaché accepté
        fx.git(fx.main, "worktree", "add", "-q", "--detach", a7, "HEAD~1")
        rc, out, _ = fx.fi("align", base=base, root=None, cwd=os.path.realpath(a7))
        assert rc == 0 and out["origin"] == "main_head", (rc, out)
    _with_wt_fx("W11", body)


def _t_w13_cleanup_keeps_battle_branch() -> None:
    def body(fx: _WtFx, tmp: str) -> None:
        fx.git(fx.a1, "checkout", "-q", "--ignore-other-worktrees", "me/A")   # builder sur la branche de la battle
        fx.set_a(statuses=("done", "done"))
        rc, out, _ = fx.fi("cleanup", ("slice-1", fx.a1))
        assert rc == 0 and out["ok"] is True, (rc, out)
        assert out["removed"][0]["branch"] is None and out["removed"][0].get("branch_kept"), out
        assert "me/A" in fx.branches() and fx.wt_a in fx.worktrees()
    _with_wt_fx("W13", body)


def _t_w14_cwd() -> None:
    def body(fx: _WtFx, tmp: str) -> None:
        for cwd in (os.path.join(fx.main, "src"), fx.a2, os.path.join(fx.wt_a, "src")):
            rc, out, _ = fx.fi("apply", ("slice-1", fx.a1), cwd=cwd)
            assert rc == 2 and out["refused"] is True, (cwd, rc, out)
        fx.write(fx.a1, "src/n.txt", "new\n")
        rc, out, _ = fx.fi("apply", ("slice-1", fx.a1), cwd=fx.wt_a)            # cwd = root : permis
        assert rc == 0, (rc, out)
    _with_wt_fx("W14", body)


def _t_w15_inplace_explicit() -> None:
    def body(fx: _WtFx, tmp: str) -> None:
        fx.write(fx.w1, "src/n.txt", "new\n")
        for extra in ((), ("--root", fx.main)):
            rc, out, _ = fx.run("apply", "--base", fx.base, "--battle", "B", *extra,
                                "--slice", "slice-1", fx.w1)
            assert rc == 0 and out["root"] == fx.main, (extra, rc, out)
            os.remove(os.path.join(fx.main, "src", "n.txt"))
        rc, out, _ = fx.run("apply", "--base", fx.base, "--battle", "B", "--root", fx.w1,
                            "--slice", "slice-1", fx.w1)
        assert rc == 2, (rc, out)                                       # --root w1 : pas le principal
    _with_wt_fx("W15", body)


def _t_cross_worktree_batch() -> None:
    """Test croisé (GH#157, slice-6) : les vrais CLI (battle_state, battle_worktree, fan_in,
    artifact_check, hook guard) en sous-processus sur un vrai dépôt + remote nu. Battle `A` en
    worktree qui fait le lot ; battle `B` (in-place) tient le pointeur pendant tout le lot ; le
    principal avance après `create` (tolérance C3)."""
    import shutil
    if shutil.which("git") is None:
        print("SKIP: cross_worktree_batch (git absent)", file=sys.stderr)
        return
    here = os.path.dirname(os.path.abspath(__file__))
    scripts = {n: os.path.join(here, n) for n in ("battle_state.py", "battle_worktree.py", "fan_in.py",
                                                  "artifact_check.py")}
    guard_hook = os.path.join(os.path.dirname(here), "hooks", "guard.py")
    with tempfile.TemporaryDirectory() as tmp0:
        tmp = os.path.realpath(tmp0)
        env = {k: v for k, v in os.environ.items() if k not in bs._GIT_ENV_DROP}
        gitconfig = os.path.join(tmp, "gitconfig")             # identité : battle_worktree la exige
        with open(gitconfig, "w", encoding="utf-8") as fh:
            fh.write("[user]\n\tname = t\n\temail = t@t\n")
        env.update(GIT_CONFIG_GLOBAL=gitconfig, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        env.pop("LEGION_GUARD_OFF", None)
        main, remote = os.path.join(tmp, "main"), os.path.join(tmp, "remote.git")

        def git(cwd: str, *a: str) -> str:
            p = subprocess.run(["git", "-c", "commit.gpgsign=false", *a], cwd=cwd, env=env,
                               capture_output=True, text=True, encoding="utf-8",
                               stdin=subprocess.DEVNULL, timeout=60)
            assert p.returncode == 0, (a, p.stderr)
            return p.stdout.strip()

        def cli(script: str, *a: str, cwd: str = main, stdin: str | None = None) -> tuple[int, dict, str]:
            p = subprocess.run([sys.executable, script, *a], cwd=cwd, env=env, capture_output=True,
                               text=True, encoding="utf-8", input=stdin, timeout=120,
                               **({} if stdin is not None else {"stdin": subprocess.DEVNULL}))
            try:
                out = json.loads(p.stdout) if p.stdout.strip() else {}
            except ValueError:
                out = {"raw": p.stdout}
            return p.returncode, out, p.stderr

        def bst(*a: str) -> dict:
            rc, out, err = cli(scripts["battle_state.py"], *a)
            assert rc == 0, (a, rc, out, err)
            return out

        def wr(root: str, rel: str, data: str) -> None:
            _Fx.write(root, rel, data)

        try:
            git(tmp, "init", "-q", "--bare", "-b", "main", remote)
            os.makedirs(main)
            git(main, "init", "-q", "-b", "main")
            wr(main, ".gitignore", ".legion/\n.claude/\n")
            wr(main, "src/a.txt", "a\n")
            wr(main, "docs/b.txt", "b\n")
            git(main, "add", "-A")
            git(main, "commit", "-q", "-m", "base")
            git(main, "remote", "add", "origin", remote)
            git(main, "push", "-q", "-u", "origin", "main")
        except (OSError, subprocess.SubprocessError, AssertionError) as exc:
            print(f"SKIP: cross_worktree_batch (git inutilisable : {exc})", file=sys.stderr)
            return
        # 1. battle A en worktree, session sidA ; puis B in-place : le pointeur vaut B.
        bst("init", "A", "--ticket", "A", "--title", "A", "--profile", "feature")
        bs.bind_session(Path(main), "sidA", "A")
        rc, created, err = cli(scripts["battle_worktree.py"], "create", "--battle", "A")
        assert rc == 0 and created["ok"] is True, (rc, created, err)
        wt_a = os.path.realpath(created["path"])
        bst("set-meta", "--battle", "A", "--worktree-path", created["path"],
            "--worktree-branch", created["branch"], "--worktree-base", created["base"])
        bst("set-guard", "--battle", "A", "--allow", "src/**", "docs/**")
        bst("transition", "think", "done", "--battle", "A")
        bst("transition", "plan", "in_progress", "--battle", "A")
        bst("transition", "plan", "done", "--verdict", "accept", "--battle", "A")
        bst("approve-plan", "--battle", "A")
        bst("set-slices", "--battle", "A", "slice-1", "slice-2")
        bst("transition", "build", "in_progress", "--battle", "A")
        bst("slice", "slice-1", "in_progress", "--battle", "A")
        bst("slice", "slice-2", "in_progress", "--battle", "A")
        bst("init", "B", "--ticket", "B", "--title", "B", "--profile", "feature")
        bs.bind_session(Path(main), "sidB", "B")
        with open(os.path.join(main, ".legion", "active-battle"), encoding="utf-8") as fh:
            assert "B" in fh.read()
        # 2. fondation non commitée dans wtA ; le principal avance (M1) après create.
        wr(wt_a, "src/found.txt", "found\n")
        wr(main, "docs/m1.txt", "m1\n")
        git(main, "add", "-A")
        git(main, "commit", "-q", "-m", "M1")
        # 3. empreinte du principal
        def fp_main() -> tuple:
            return (git(main, "rev-parse", "HEAD"), git(main, "ls-files", "-s"),
                    git(main, "status", "--porcelain", "-uall"), git(main, "for-each-ref"))
        before_main = fp_main()
        # 4. base
        fi = scripts["fan_in.py"]
        rc, out, err = cli(fi, "base", "--battle", "A", "--root", wt_a)
        assert rc == 0 and out["frozen"] is True, (rc, out, err)
        base = out["base"]
        assert git(main, "rev-parse", f"{base}^") == git(wt_a, "rev-parse", "HEAD")
        # 5. S1
        ac_ = scripts["artifact_check.py"]
        s1, s2 = os.path.join(tmp, "s1.json"), os.path.join(tmp, "s2.json")
        rc, out, err = cli(ac_, "tree-snapshot", "--battle", "A", "--root", wt_a, "--out", s1)
        assert rc == 0 and out["ok"] is True, (rc, out, err)
        fp1 = out["fingerprint"]
        # 6. builders simulés, coupés depuis le HEAD du principal (qui a avancé après create)
        agents = []
        for n in (1, 2):
            path = os.path.join(main, ".claude", "worktrees", f"agent-{n}")
            git(main, "worktree", "add", "-q", "-b", f"worktree-agent-{n}", path, "HEAD")
            agents.append(os.path.realpath(path))
        a1, a2 = agents
        assert git(main, "rev-parse", "HEAD") != git(wt_a, "rev-parse", "HEAD")
        # 7. align depuis chaque agent
        for ag in agents:
            rc, out, err = cli(fi, "align", "--base", base, "--battle", "A", cwd=ag)
            assert rc == 0 and out.get("ok") is True and out.get("origin") == "main_head", (ag, rc, out, err)
            assert git(ag, "rev-parse", "HEAD") == base
        # 8. écritures des builders
        wr(a1, "src/a.txt", "a1\n")
        wr(a2, "docs/u2.txt", "u2\n")
        # 9. tree-verify --base de chaque builder
        for ag in agents:
            rc, out, err = cli(ac_, "tree-verify", "--base", base, "--battle", "A", "--root", ag, "--guard")
            assert rc == 0 and out["ok"] is True, (ag, rc, out, err)
        # 10. tree-verify du lot
        rc, out, err = cli(ac_, "tree-verify", "--battle", "A", "--root", wt_a, "--before", s1,
                           "--fingerprint", fp1, "--batch-worktrees")
        assert rc == 0 and out["ok"] is True, (rc, out, err)
        # 11. garde-fou du hook
        def hook(cmd: str, sid: str) -> tuple[int, str]:
            payload = json.dumps({"tool_name": "Bash", "agent_type": "claude", "session_id": sid,
                                  "tool_input": {"command": cmd}})
            p = subprocess.run([sys.executable, guard_hook], cwd=main, env=env, input=payload,
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
            return p.returncode, p.stdout + p.stderr
        pairs = ["--slice", "slice-1", a1, "--slice", "slice-2", a2]
        apply_cmd = f'python3 "{fi}" apply --base {base} {" ".join(pairs)}'
        code, msg = hook(apply_cmd, "sidA")
        assert code == 2 and "A" in msg, (code, msg)
        code, msg = hook(apply_cmd + " --battle A --root " + wt_a, "sidA")
        assert code == 0, (code, msg)
        # 12. apply dans wtA, principal intact
        rc, out, err = cli(fi, "apply", "--base", base, "--battle", "A", "--root", wt_a, *pairs)
        assert rc == 0 and out["ok"] is True, (rc, out, err)
        assert _Fx.read(_Fx, wt_a, "src/a.txt") == b"a1\n" and _Fx.read(_Fx, wt_a, "docs/u2.txt") == b"u2\n"
        assert _Fx.read(_Fx, wt_a, "src/found.txt") == b"found\n"
        assert fp_main()[:3] == before_main[:3]               # refs : seules les branches builders s'ajoutent
        # 13. S2, verify --guard, slice done
        rc, out, err = cli(ac_, "tree-snapshot", "--battle", "A", "--root", wt_a, "--out", s2)
        assert rc == 0, (rc, out, err)
        rc, out, err = cli(ac_, "tree-verify", "--battle", "A", "--root", wt_a, "--before", s2,
                           "--fingerprint", out["fingerprint"], "--guard")
        assert rc == 0 and out["ok"] is True, (rc, out, err)
        bst("slice", "slice-1", "done", "--battle", "A")
        bst("slice", "slice-2", "done", "--battle", "A")
        # 14. cleanup
        rc, out, err = cli(fi, "cleanup", "--base", base, "--battle", "A", "--root", wt_a, *pairs)
        assert rc == 0 and out["ok"] is True and len(out["removed"]) == 2, (rc, out, err)
        branches = git(main, "branch", "--format=%(refname:short)").split()
        assert created["branch"] in branches and not any(b.startswith("worktree-agent") for b in branches)
        assert wt_a in [os.path.realpath(l[len("worktree "):]) for l in
                        git(main, "worktree", "list", "--porcelain").splitlines() if l.startswith("worktree ")]
        assert fp_main() == before_main                              # principal strictement identique
        # 15. refus
        for root in (main, a1):
            rc, out, _ = cli(fi, "apply", "--base", base, "--battle", "A", "--root", root, *pairs[:3])
            assert rc == 2, (root, rc, out)
        rc, out, _ = cli(fi, "apply", "--base", base, "--slice", "slice-1", wt_a)
        assert rc == 2, (rc, out)
        rc, out, _ = cli(fi, "align", "--base", base, "--battle", "A", cwd=wt_a)
        assert rc == 2, (rc, out)
        rc, out, _ = cli(fi, "apply", "--base", base, "--battle", "B", "--slice", "slice-1", wt_a)
        assert rc == 2, (rc, out)                                     # B in-place : chemin actuel, refuse ce worktree


_TESTS = (_t_import_smoke, _t_f1_nominal, _t_f2_order, _t_f3_overlap, _t_f4_dirty_main,
          _t_f5_untracked_present, _t_f6_out_of_scope_and_deny, _t_f7_legion_forced,
          _t_f8_committed_and_dirty, _t_f9_head_not_descendant, _t_f10_invalid_worktrees,
          _t_f11_slice_problems, _t_f12_fail_closed, _t_f13_nested_repo, _t_f14_state_untouched,
          _t_f15_integrity_envelope, _t_f16_cleanup_nominal, _t_f17_cleanup_before_apply,
          _t_f18_cleanup_branch_kept, _t_f19_usage, _t_f20_bounded_output, _t_f21_mode_and_symlink,
          _t_f22_cleanup_mode_lost, _t_b1_base_nominal, _t_b2_base_clean_fallback,
          _t_b3_base_unignored_state, _t_b4_base_invariants, _t_b5_base_nested_repo,
          _t_b6_base_context, _t_a1_align_nominal, _t_a2_align_branch, _t_a3_align_refusals,
          _t_a4_align_base_is_head, _t_e1_end_to_end, _t_e2_not_aligned,
          _t_w1_worktree_flow, _t_w5_root_refusals, _t_w6_worktree_battle_needs_root, _t_w7_usage,
          _t_w8_battle_unusable, _t_w9_battle_worktrees_refused,
          _t_w10_align_refused_in_battle_worktree, _t_w11_w12_align_main_head,
          _t_w13_cleanup_keeps_battle_branch, _t_w14_cwd, _t_w15_inplace_explicit,
          _t_cross_worktree_batch)


def _self_test() -> int:
    failed = 0
    for fn in _TESTS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - on rapporte puis on continue
            failed += 1
            print(f"FAIL: {fn.__name__}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if failed:
        print(f"FAIL: fan_in self-test ({failed}/{len(_TESTS)} en échec)", file=sys.stderr)
        return 1
    print("OK: fan_in self-test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # accents FR sur une console cp1252
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
