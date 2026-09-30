"""Delivery check d'artefact de gate de legion (battle.md §E), version exécutable.

Une gate écrit elle-même son artefact : son verdict ne **prouve** donc plus qu'il existe.
L'orchestrateur vérifie, sur les seules **métadonnées** (jamais le contenu), quatre points.
Ce script les calcule de façon **déterministe**, sans PowerShell, sur Windows, Linux, WSL
et macOS :

- `exists`     : le fichier attendu existe ;
- `non_empty`  : il fait plus de 0 octet (une gate peut rendre un verdict en laissant un
                 artefact vide) ;
- `canonical`  : le chemin `ARTIFACT:` retourné désigne le même fichier que le chemin
                 attendu (relatif ≡ absolu, casse ignorée là où le système l'ignore).
                 `null` si `--returned` n'est pas fourni ;
- `fresh`      : écrit à ce passage. Sans `--since`, le fichier n'existait pas avant, donc
                 `fresh` = `exists`. Avec `--since <mtime_ns>`, son mtime doit être
                 **strictement** plus récent.

Le cœur (`_evaluate`) est **pur** : il reçoit un instantané et des chemins déjà normalisés,
sans toucher au disque. La couche I/O (`_stat`, `_canon`) est isolée. Les mtime sont des
entiers (`st_mtime_ns`), jamais des flottants.

Usage :
    python artifact_check.py snapshot <path>
    python artifact_check.py verify <path> [--since <mtime_ns>] [--expected <path>]
                                           [--returned <path>]
    python artifact_check.py tree-snapshot --out <fichier> [--root <dir>]
    python artifact_check.py tree-verify --before <fichier> --fingerprint <sha>
                                         [--root <dir>] [--guard | --allow <glob>...]
                                         [--batch-worktrees]
    python artifact_check.py tree-verify --before <fichier> --fingerprint <sha> --batch-worktrees
    python artifact_check.py tree-verify --base <sha> --root <worktree> --guard
    python artifact_check.py --self-test

Sortie : un objet JSON sur stdout
    snapshot      → { exists, mtime_ns, size }
    verify        → { ok, checks: { exists, non_empty, canonical, fresh }, reason }
    tree-snapshot → { ok, fingerprint, count } (le snapshot complet va dans `--out`)
    tree-verify   → { ok, fault, refused, changed_count, changed[<=50], out_of_scope[<=50],
                      reason }
Codes de sortie : 0 = ok ; 2 = `verify` refusé (`ok:false`), faute d'arbre (`fault:true`) ou
refus (`refused:true`) ; 1 = erreur d'usage.

Contrôle d'intégrité de l'arbre (GH#66, couche 1). Une gate (lecture seule) ou un builder
(périmètre `guard.allow`) ne doit pas modifier l'arbre de travail au-delà de son droit, y
compris par Bash. `tree-snapshot` prend une empreinte (HEAD, branche, entrées de
`git status -uall` avec le hash de leur contenu, état protégé `.legion/active-battle` +
`battle.json`, masques d'index `assume-unchanged`/`skip-worktree` + `info/exclude`) ; `.legion/`
et les worktrees enregistrés sont exclus des chemins. `tree-verify` recalcule et compare.
`--fingerprint` (retenu par l'orchestrateur) protège le fichier `--before` contre une
réécriture. `--guard` lit `allow`/`deny` de la battle active de la racine d'état
(`battle_state.resolve_state_root` : dépôt principal depuis un worktree lié, comme `guard.py`) ;
`--base` compare un worktree à un arbre propre au commit donné, dont le HEAD doit descendre du
commit donné (faute `[base]` sinon : worktree non aligné, jamais filtrable). Fail-closed : hors dépôt,
`git` absent, fichier illisible → refus (exit 2), jamais `ok`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time


def _stat(path: str) -> dict:
    """Instantané I/O : `{exists, mtime_ns, size}` ; `null` si le fichier est absent."""
    try:
        st = os.stat(path)
    except OSError:
        return {"exists": False, "mtime_ns": None, "size": None}
    return {"exists": True, "mtime_ns": st.st_mtime_ns, "size": st.st_size}


def _canon(path: str) -> str:
    """Forme canonique d'un chemin : absolu, liens résolus, casse normalisée (Windows)."""
    return os.path.normcase(os.path.realpath(path))


def _evaluate(snap: dict, since: int | None, expected: str, returned: str | None) -> dict:
    """Cœur de décision — **pur** (ni disque ni processus), donc testable hermétiquement.

    `expected` et `returned` sont déjà canonisés par l'appelant."""
    exists = bool(snap.get("exists"))
    size = snap.get("size")
    mtime = snap.get("mtime_ns")
    checks = {
        "exists": exists,
        "non_empty": exists and isinstance(size, int) and size > 0,
        "canonical": None if returned is None else (returned == expected),
        "fresh": exists and (since is None or (isinstance(mtime, int) and mtime > since)),
    }
    failed = [k for k, v in checks.items() if v is False]
    if not failed:
        return {"ok": True, "checks": checks,
                "reason": "Artefact présent, non vide, au bon chemin et écrit à ce passage."}
    msgs = {
        "exists": "artefact absent",
        "non_empty": "artefact vide (0 octet)",
        "canonical": "chemin ARTIFACT retourné différent du chemin attendu",
        "fresh": ("artefact périmé (mtime non postérieur au snapshot)"
                  if exists else "artefact non écrit à ce passage"),
    }
    return {"ok": False, "checks": checks,
            "reason": "Échec : " + " ; ".join(msgs[k] for k in failed) + "."}


def verify(path: str, since: int | None, expected: str | None, returned: str | None) -> dict:
    """Couche I/O : stat + canonisation, puis `_evaluate`."""
    exp = _canon(expected if expected else path)
    ret = None if returned is None else _canon(returned)
    return _evaluate(_stat(path), since, exp, ret)


# --- Empreinte de l'arbre (GH#66, couche 1) -----------------------------------------------

_MAX_LIST = 50          # taille max des listes `changed` / `out_of_scope` sur stdout
_MAX_DEPTH = 3          # profondeur max des dépôts imbriqués / sous-modules
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_ROOT_ALWAYS_ALLOW = (".gitignore",)   # comme guard.py (setup orchestrateur)


class _Refuse(Exception):
    """Refus fail-closed : la commande ne peut pas conclure (jamais `ok`)."""


# Options de config git imposees a CHAQUE appel : la config du depot est modifiable par une
# gate (`core.fsmonitor` fait repondre « rien n'a change », `core.trustctime=false` masque une
# reecriture a mtime restaure). Le hash de la config locale (`_git_config_state`) couvre le reste.
_GIT_FORCED = (
    "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
    "-c", "core.trustctime=true", "-c", "core.checkStat=default",
    "-c", "core.ignoreStat=false",
)


def _git(root: str, *args: str, index: str | None = None, data: bytes | None = None) -> bytes:
    """`git --no-optional-locks <options imposees> -C <root> ...` -> stdout en octets ;
    `_Refuse` sur tout echec. `index` : `GIT_INDEX_FILE` impose (un `GIT_INDEX_FILE` herite du
    shell est toujours ignore) ; `data` : stdin."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    env.pop("GIT_INDEX_FILE", None)
    if index is not None:
        env["GIT_INDEX_FILE"] = index
    try:
        p = subprocess.run(["git", "--no-optional-locks", *_GIT_FORCED, "-C", root, *args],
                           capture_output=True, env=env,
                           **({"input": data} if data is not None else {"stdin": subprocess.DEVNULL}))
    except (OSError, ValueError) as exc:  # git introuvable, racine inutilisable
        raise _Refuse(f"git indisponible ({type(exc).__name__}: {exc})") from exc
    if p.returncode != 0:
        msg = p.stderr.decode("utf-8", "replace").strip().splitlines()
        raise _Refuse(f"git {args[0]} a échoué : {msg[0] if msg else 'code ' + str(p.returncode)}")
    return p.stdout


def _git_ok(root: str, *args: str) -> bytes | None:
    """Variante tolérante : `None` si git répond non-zéro (mais `_Refuse` si git manque)."""
    try:
        return _git(root, *args)
    except _Refuse as exc:
        if "indisponible" in str(exc):
            raise
        return None


def _toplevel(start: str) -> str:
    out = _git(start, "rev-parse", "--show-toplevel").decode("utf-8", "replace").strip()
    if not out:
        raise _Refuse("racine du dépôt introuvable")
    return os.path.realpath(out)


def _worktrees(root: str) -> list[str]:
    """Chemins (réels) des worktrees enregistrés, racine courante comprise."""
    out = _git(root, "worktree", "list", "--porcelain")
    paths = []
    for line in out.split(b"\n"):
        if line.startswith(b"worktree "):
            paths.append(os.path.realpath(os.fsdecode(line[len(b"worktree "):])))
    return paths


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_entry(root: str, rel: str, depth: int) -> str:
    """Contenu d'une entrée : sha256 des octets bruts, `link:`, `absent`, `special:`, `tree:`.

    Un fichier spécial (FIFO, socket) n'est jamais ouvert : le lire pourrait bloquer."""
    path = os.path.join(root, *rel.split("/"))
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return f"error:{exc.errno}"
    mode = st.st_mode
    try:
        if stat.S_ISLNK(mode):
            return "link:" + hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
        if stat.S_ISREG(mode):
            return _sha256_file(path)
        if stat.S_ISDIR(mode):
            if depth >= _MAX_DEPTH:
                return "tree:too-deep"
            if not os.path.lexists(os.path.join(path, ".git")):
                return "dir:plain"
            return "tree:" + _fingerprint(_collect(path, depth + 1, nested=True))
    except OSError as exc:
        return f"error:{exc.errno}"
    return f"special:{stat.S_IFMT(mode):o}"


def _parse_status(raw: bytes) -> list[tuple[str, str]]:
    """`git status --porcelain=v1 -z` -> [(XY, chemin)]. R/C : le champ suivant est l'origine."""
    fields = raw.split(b"\0")
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(fields):
        f = fields[i]
        i += 1
        if len(f) < 4:
            continue
        xy = f[:2].decode("ascii", "replace")
        out.append((xy, os.fsdecode(f[3:])))
        if "R" in xy or "C" in xy:
            if i < len(fields) and fields[i]:
                out.append((xy + "<", os.fsdecode(fields[i])))
            i += 1
    return out


def _is_legion(rel: str) -> bool:
    return rel == ".legion" or rel.startswith(".legion/")   # casse exacte (FS sensible)


def _read_state(root: str) -> dict:
    """sha256 de `.legion/active-battle` et de `battle.json` de la battle active."""
    def h(p: str) -> str:
        try:
            return _sha256_file(p)
        except OSError:
            return "absent"
    ptr = os.path.join(root, ".legion", "active-battle")
    state = {"active-battle": h(ptr), "battle.json": "absent"}
    try:
        with open(ptr, "rb") as fh:
            bid = fh.read(4096).decode("utf-8", "replace").strip()
    except OSError:
        bid = ""
    if bid and re.fullmatch(r"[A-Za-z0-9._-]+", bid) and bid not in (".", ".."):
        state["battle.json"] = h(os.path.join(root, ".legion", "battles", bid, "battle.json"))
    return state


def _status_no_stat_cache(root: str) -> bytes:
    """`git status` sur une copie de l'index **sans donnees stat** : git rehashe alors le contenu
    de chaque fichier suivi. Une gate qui reecrit `.git/index` (stat du nouveau fichier, blob
    d'origine) ne masque plus une modification ; un `git status`/`diff` de l'orchestrateur, qui
    rafraichit l'index reel, n'a aucun effet sur l'empreinte (l'index reel n'est jamais hashe
    en octets). Les options de l'index (assume-unchanged, skip-worktree) sont couvertes par
    `masks` ; ici elles sont ignorees, donc ces fichiers sont compares sur le contenu aussi."""
    entries = _git(root, "ls-files", "-s", "-z")
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "index")
        _git(root, "update-index", "-z", "--index-info", index=tmp, data=entries)
        return _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                    "--ignore-submodules=none", index=tmp)


def _collect(root: str, depth: int = 0, nested: bool = False) -> dict:
    """Empreinte brute de l'arbre à `root` (I/O). `nested` : dépôt imbriqué (sans état)."""
    head_raw = _git_ok(root, "rev-parse", "-q", "--verify", "HEAD")
    head = head_raw.decode().strip() if head_raw and head_raw.strip() else None
    br_raw = _git_ok(root, "symbolic-ref", "-q", "HEAD")
    branch = br_raw.decode("utf-8", "replace").strip() if br_raw and br_raw.strip() else None
    status = _status_no_stat_cache(root)
    entries = _parse_status(status)
    wts: list[str] = []
    if not nested:
        me = os.path.realpath(root)
        wts = [w for w in _worktrees(root)
               if w != me and not me.startswith(w + os.sep)]  # hors soi et ses ancêtres
    idx: dict[str, str] = {}     # sha de l'entree d'index : un blob indexe change sous « MM »/« AM »
    for ent in _git(root, "ls-files", "-s", "-z").split(b"\0"):
        meta, _, name = ent.partition(b"\t")
        if name:
            idx[os.fsdecode(name)] = meta.decode("ascii", "replace").replace(" ", ":")
    paths: dict[str, str] = {}
    for xy, rel in entries:
        rel = rel.rstrip("/")
        if not rel or _is_legion(rel):
            continue
        if wts:
            real = os.path.realpath(os.path.join(root, *rel.split("/")))
            if any(real == w or real.startswith(w + os.sep) for w in wts):
                continue
        paths[rel] = f"{xy}:{idx.get(rel, '-')}:{_hash_entry(root, rel, depth)}"
    lsf = _git(root, "ls-files", "-v", "-z")
    masked = []
    for f in lsf.split(b"\0"):
        if len(f) > 2 and (f[:1].islower() or f[:1] == b"S"):
            masked.append(f[:1].decode() + " " + os.fsdecode(f[2:]))
    excl = _hash_git_path(root, "info/exclude")
    return {"root": os.path.realpath(root), "head": head, "branch": branch, "paths": paths,
            "state": {} if nested else _read_state(root),
            "masks": {"paths": sorted(masked), "exclude": excl,
                      "ignore_files": _ignored_control_files(root)},
            "gitcfg": {} if nested else _git_config_state(root)}


def _hash_git_path(root: str, gitpath: str) -> str:
    """sha256 d'un fichier du dossier git (`rev-parse --git-path`), `absent` sinon."""
    raw = _git_ok(root, "rev-parse", "--git-path", gitpath)
    if not raw or not raw.strip():
        return "absent"
    try:
        return _sha256_file(os.path.join(root, os.fsdecode(raw.strip())))
    except OSError:
        return "absent"


def _hash_dir_files(path: str) -> str:
    """sha256 des noms + contenus des fichiers d'un dossier (recursif) ; `absent` si inexistant."""
    if not os.path.isdir(path):
        return "absent"
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            h.update(os.fsencode(os.path.relpath(full, path).replace(os.sep, "/")) + b"\0")
            try:
                h.update(_sha256_file(full).encode())
            except OSError as exc:
                h.update(f"error:{exc.errno}".encode())
    return h.hexdigest()


# Etat du dossier git (GH#66, auto-corrections 2 et 3). Une gate peut ecrire un fichier d'etat
# que git LIT : `MERGE_HEAD`/`CHERRY_PICK_HEAD`/`REVERT_HEAD` (le prochain `git commit` cree un
# commit de merge dont le parent est choisi par la gate), `refs/` (dont la branche de livraison
# `<me>/<token>`), `packed-refs`, `rebase-merge/`, `sequencer/`, `shallow`, `info/grafts`,
# `modules/`, la liste des worktrees... On hashe donc les chemins du git-dir (et du dossier
# commun pour un worktree lie) que git lit, cf. `_git_reads` ; un fichier inconnu de git (ex. les
# `sg-hook-once-*` d'un hook tiers) est inerte : git l'ignore, ce n'est pas une faute.
# Exclus, car inertes ou ecrits par git lui-meme :
#   - `objects/` : contenu adresse, inerte tant qu'aucune ref n'y pointe (limite documentee, §4.6) ;
#   - `index`, `*.lock`, `sharedindex.*` : `git status`/`diff` de l'orchestrateur rafraichissent
#     l'index reel (octets instables). Son contenu logique est couvert par `status` sur un index
#     sans donnees stat (`_status_no_stat_cache`) ;
#   - `logs/` (reflogs), `FETCH_HEAD`, `COMMIT_EDITMSG`, `gc.log`, `gc.pid` : journaux ecrits par
#     git, jamais lus pour decider du contenu d'un commit ou d'un push ;
#   - les branches (`refs/heads/<b>` et lignes `packed-refs`) actuellement extraites dans un
#     worktree enregistre (`_worktree_state`) : celles des builders isoles bougent legitimement
#     pendant un lot. TOUTE autre branche est hashee (creer `<me>/<token>` est une faute) ;
#   - `worktrees/` (dossier commun) : remplace par la liste des worktrees enregistres (chemin,
#     branche, gitdir), qui est dans l'empreinte ;
#   - `hooks/`, `config`, `info/exclude`, `info/attributes` : hashes a part (fautes nommees).
_STATE_SKIP = frozenset({"index", "objects", "logs", "FETCH_HEAD", "COMMIT_EDITMSG", "gc.log",
                         "gc.pid"})
_STATE_TOP_SKIP = frozenset({"hooks", "config", "worktrees"})   # dossier commun, niveau racine
# Fichiers/dossiers de la racine d'un git-dir que git lit (liste blanche). S'y ajoutent, par
# motif : `*_HEAD` (MERGE_HEAD, CHERRY_PICK_HEAD, REVERT_HEAD, ORIG_HEAD, REBASE_HEAD...),
# `BISECT_*` et `NOTES_MERGE_*`.
_STATE_READ = frozenset({
    "HEAD", "refs", "packed-refs", "shallow", "modules", "info", "worktrees", "commondir", "gitdir",
    "locked", "config", "config.worktree", "hooks", "rebase-merge", "rebase-apply", "sequencer",
    "AUTO_MERGE", "MERGE_MSG", "MERGE_MODE", "MERGE_RR", "SQUASH_MSG", "rr-cache", "remotes",
    "branches", "sparse-checkout"})
_STATE_READ_FOLDED = frozenset(x.casefold() for x in _STATE_READ)
_STATE_MAX = 50000
# Entrees legitimes du dossier administratif d'un worktree lie (mode `--base`, builder isole).
_EMPTY_STATE = hashlib.sha256().hexdigest()     # `_state_hash` d'un dossier sans entree retenue
_PRIVATE_ALLOW = frozenset({"HEAD", "commondir", "gitdir", "index", "logs", "ORIG_HEAD",
                            "COMMIT_EDITMSG", "locked", "config.worktree", "refs"})


def _git_reads(name: str) -> bool:
    """`name` (racine d'un git-dir) est-il un chemin que git lit ? Sinon il est inerte."""
    # Comparaison insensible a la casse : sur NTFS/APFS git ouvre `MERGE_HEAD` via `merge_head`.
    # Sur un FS sensible, `merge_head` devient une fausse faute rare et sans danger.
    n = name.casefold()
    if n == "fetch_head":
        return False
    return (n in _STATE_READ_FOLDED or n.endswith("_head")
            or n.startswith(("bisect_", "notes_merge")))


def _looks_like_gitdir(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "HEAD")) and (
        os.path.isdir(os.path.join(path, "objects")) or os.path.isfile(os.path.join(path, "commondir")))


def _state_hash(base: str, *, root_skip: frozenset = frozenset(), only_names: frozenset | None = None,
                invert: bool = False, skip_refs: frozenset = frozenset()) -> str:
    """sha256 (noms + type + contenu) de l'arborescence de `base` : chemins que git lit
    (`_git_reads`), hors exclusions ci-dessus. `only_names`/`invert` : au niveau racine, ne garder
    que les entrees HORS de cette liste (mode `--base` : tout ce qui n'est pas legitime dans un
    worktree lie). `skip_refs` : refs de branches extraites dans un worktree enregistre."""
    if not os.path.isdir(base):
        return "absent"
    h = hashlib.sha256()
    count = 0

    def walk(cur: str, rel: str, top: bool) -> None:
        nonlocal count
        gitdir_like = top or (rel.startswith("modules/") and _looks_like_gitdir(cur))
        try:
            names = sorted(os.listdir(cur))
        except OSError as exc:
            h.update(f"{rel}\0error:{exc.errno}\n".encode("utf-8", "surrogateescape"))
            return
        for name in names:
            r = f"{rel}/{name}" if rel else name
            if gitdir_like and (name in _STATE_SKIP or name.endswith(".lock")
                                or name.startswith("sharedindex.") or not _git_reads(name)):
                continue
            if top and name in root_skip:
                continue
            if top and only_names is not None and invert and name in only_names:
                continue
            if r in skip_refs:
                continue     # branche extraite dans un worktree enregistre
            if top and r in ("info/exclude", "info/attributes"):
                continue
            count += 1
            if count > _STATE_MAX:
                raise _Refuse("état du dossier git trop volumineux")
            full = os.path.join(cur, name)
            try:
                st = os.lstat(full)
            except OSError as exc:
                h.update(f"{r}\0error:{exc.errno}\n".encode("utf-8", "surrogateescape"))
                continue
            if stat.S_ISLNK(st.st_mode):
                kind = "l:" + hashlib.sha256(os.fsencode(os.readlink(full))).hexdigest()
            elif stat.S_ISDIR(st.st_mode):
                if not r.startswith("refs/heads/"):     # un dossier de branche (`a/b`) n'est pas un etat
                    h.update(f"{r}\0d\n".encode("utf-8", "surrogateescape"))
                walk(full, r, False)
                continue
            elif stat.S_ISREG(st.st_mode):
                try:
                    if name == "packed-refs" and gitdir_like:
                        kind = "f:" + _packed_refs_hash(full, skip_refs)
                    else:
                        kind = "f:" + _sha256_file(full)
                except OSError as exc:
                    kind = f"error:{exc.errno}"
            else:
                kind = f"special:{stat.S_IFMT(st.st_mode):o}"
            h.update(f"{r}\0{kind}\n".encode("utf-8", "surrogateescape"))

    walk(base, "", True)
    return h.hexdigest()


def _packed_refs_hash(path: str, skip_refs: frozenset = frozenset()) -> str:
    """sha256 de `packed-refs` sans les lignes des branches de `skip_refs`."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for line in fh:
            parts = line.rstrip(b"\r\n").split(b" ", 1)
            if len(parts) == 2 and os.fsdecode(parts[1]) in skip_refs:
                continue
            h.update(line)
    return h.hexdigest()


def _worktree_state(root: str, common: str) -> tuple[dict, frozenset]:
    """Worktrees enregistres : `({list, admin}, branches)`. `list` : `chemin\tbranche\tprunable`
    (`git worktree list --porcelain`, sans sha : le HEAD d'un builder bouge) ; `admin` :
    `nom\tgitdir` de chaque `<commun>/worktrees/*/gitdir`. `branches` : refs extraites (exclues
    de l'empreinte des refs). Un worktree apparu, disparu ou redirige change `list`/`admin`."""
    entries, branches = [], set()
    for block in _git(root, "worktree", "list", "--porcelain").split(b"\n\n"):
        path, branch, prunable = None, "detached", ""
        for line in block.split(b"\n"):
            if line.startswith(b"worktree "):
                path = os.path.realpath(os.fsdecode(line[len(b"worktree "):]))
            elif line.startswith(b"branch "):
                branch = os.fsdecode(line[len(b"branch "):])
                branches.add(branch)
            elif line.startswith(b"prunable"):
                prunable = "prunable"
            elif line == b"bare":
                branch = "bare"
        if path is not None:
            entries.append(f"{path}\t{branch}\t{prunable}")
    admin = []
    wdir = os.path.join(common, "worktrees")
    try:
        names = sorted(os.listdir(wdir))
    except OSError:
        names = []
    for name in names:
        try:
            with open(os.path.join(wdir, name, "gitdir"), "rb") as fh:
                content = os.fsdecode(fh.read(8192).strip())
        except OSError as exc:
            content = f"error:{exc.errno}"
        admin.append(f"{name}\t{content}")
    return {"list": sorted(entries), "admin": admin}, frozenset(branches)


def _git_state(root: str, base_mode: bool = False) -> dict:
    """Empreinte de l'etat du git-dir prive et du dossier commun (cf. ci-dessus).
    `base_mode` (builder isole, `--base`) : la partie privee ne retient que les entrees hors de
    `_PRIVATE_ALLOW` (fichier d'etat ecrit par le builder) ; elle doit etre vide."""
    gd_raw = _git_ok(root, "rev-parse", "--absolute-git-dir")
    cm_raw = _git_ok(root, "rev-parse", "--git-common-dir")
    if not gd_raw or not gd_raw.strip() or not cm_raw or not cm_raw.strip():
        raise _Refuse("dossier git introuvable")
    gitdir = os.path.realpath(os.fsdecode(gd_raw.strip()))
    common = os.path.realpath(os.path.join(root, os.fsdecode(cm_raw.strip())))
    wts, skip = _worktree_state(root, common)
    out = {"common": _state_hash(common, root_skip=_STATE_TOP_SKIP, skip_refs=skip),
           "worktrees": wts}
    if gitdir == common:
        out["private"] = "same"
    elif base_mode:
        out["private"] = _state_hash(gitdir, only_names=_PRIVATE_ALLOW, invert=True)
    else:
        out["private"] = _state_hash(gitdir)
    return out


def _global_state(root: str) -> str:
    """Config git globale/systeme et fichiers `attributes`/`ignore` effectifs (explicites ou XDG) :
    un filtre `clean` ou un `excludesFile` poses la-bas changent ce que git voit et indexe."""
    h = hashlib.sha256()
    for scope in ("--global", "--system"):
        h.update(_git_ok(root, "config", scope, "--list", "-z") or b"")
        h.update(b"\0|\0")
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    files = [os.path.join(xdg, "git", "attributes"), os.path.join(xdg, "git", "ignore")]
    for key in ("core.attributesfile", "core.excludesfile"):
        raw = _git_ok(root, "config", "--path", "--get", key)
        if raw and raw.strip():
            files.append(os.path.join(root, os.fsdecode(raw.strip())))
    for f in sorted(set(files)):
        try:
            d = _sha256_file(f) if os.path.isfile(f) else "absent"
        except OSError as exc:
            d = f"error:{exc.errno}"
        h.update(os.fsencode(f) + b"\0" + d.encode() + b"\n")
    return h.hexdigest()


def _git_config_state(root: str, base_mode: bool = False) -> dict:
    """Hash de la config locale/worktree, des hooks (dossier reel + `core.hooksPath`) et de
    `info/attributes` : une gate qui les modifie change le comportement de git ou le commit livre."""
    cfg = _git_ok(root, "config", "--local", "--list", "-z") or b""
    # `--worktree` n'a de sens (et n'echoue pas des qu'il y a plusieurs worktrees) que si
    # `extensions.worktreeConfig` est actif ; sinon il relit la config locale, deja hashee.
    wt_on = (_git_ok(root, "config", "--local", "--type=bool", "--get", "extensions.worktreeConfig")
             or b"").strip() == b"true"
    wcfg = (_git_ok(root, "config", "--worktree", "--list", "-z") or b"") if wt_on else b""
    hooks_raw = _git_ok(root, "rev-parse", "--git-path", "hooks")
    hooks_dir = os.path.join(root, os.fsdecode(hooks_raw.strip())) if hooks_raw and hooks_raw.strip() else ""
    common_raw = _git_ok(root, "rev-parse", "--git-common-dir")
    common = os.path.join(root, os.fsdecode(common_raw.strip())) if common_raw and common_raw.strip() else ""
    return {
        "config": hashlib.sha256(cfg + b"\0--worktree\0" + wcfg).hexdigest(),
        "hooks": hashlib.sha256((_hash_dir_files(hooks_dir) + "|"
                                 + _hash_dir_files(os.path.join(common, "hooks") if common else "")
                                 ).encode()).hexdigest(),
        "attributes": _hash_git_path(root, "info/attributes"),
        "gitstate": _git_state(root, base_mode),
        "global": _global_state(root),
    }


def _ignored_control_files(root: str) -> dict:
    """Fichiers `.gitignore` eux-memes ignores (`*` dans un `.gitignore` qui s'ignore) : invisibles
    dans `status`, ils masquent des creations. Empreinte de chaque `.gitignore` ignore (ou present
    au sommet d'un dossier ignore replie)."""
    raw = _git_ok(root, "status", "--porcelain=v1", "-z", "--ignored=traditional")
    out: dict[str, str] = {}
    if not raw:
        return out
    for xy, rel in _parse_status(raw):
        if xy != "!!" or _is_legion(rel.rstrip("/")):
            continue
        rel = rel.rstrip("/") if rel.endswith("/") else rel
        cand = rel + "/.gitignore" if os.path.isdir(os.path.join(root, *rel.split("/"))) else (
            rel if rel.rsplit("/", 1)[-1] == ".gitignore" else None)
        if cand:
            full = os.path.join(root, *cand.split("/"))
            try:
                if os.path.isfile(full):
                    out[cand] = _sha256_file(full)
            except OSError as exc:
                out[cand] = f"error:{exc.errno}"
    return out


_CORE_KEYS = ("root", "head", "branch", "paths", "state", "masks", "gitcfg")


def _fingerprint(core: dict) -> str:
    """sha256 d'une sérialisation canonique (JSON trié, ASCII) des champs du snapshot."""
    body = {k: core.get(k) for k in _CORE_KEYS}
    data = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("ascii")).hexdigest()


def _tree_diff(before: dict, after: dict, committed: list[str] | None) -> dict:
    """Cœur de comparaison — **pur**. `committed` : chemins du `git diff` entre les deux HEAD
    (ou `None` si indisponible). Retourne `{changed, faults}` : `faults` = fautes nommées qui
    ne sont jamais filtrables (HEAD sans liste, branche, état protégé, masque d'index)."""
    bp, ap = before.get("paths") or {}, after.get("paths") or {}
    changed = {k for k in set(bp) | set(ap) if bp.get(k) != ap.get(k)}
    faults: list[str] = []
    if before.get("head") != after.get("head"):
        if not committed:   # liste indisponible OU commit vide / `reset --soft` : invisible sinon
            faults.append("HEAD")
        else:
            changed.update(committed)
    elif before.get("branch") != after.get("branch"):
        faults.append("branch")
    bs, as_ = before.get("state") or {}, after.get("state") or {}
    if bs.get("active-battle") != as_.get("active-battle"):
        faults.append(".legion/active-battle")
    if bs.get("battle.json") != as_.get("battle.json"):
        faults.append(".legion/battles/<active>/battle.json")
    if before.get("masks") != after.get("masks"):
        faults.append("index-mask")
    bg, ag = before.get("gitcfg") or {}, after.get("gitcfg") or {}
    if bg.get("config") != ag.get("config"):
        faults.append("git-config")
    if bg.get("hooks") != ag.get("hooks"):
        faults.append("git-hooks")
    if bg.get("attributes") != ag.get("attributes"):
        faults.append("git-attributes")
    if bg.get("gitstate") != ag.get("gitstate"):
        faults.append("git-state")
    if bg.get("global") != ag.get("global") and "git-config" not in faults:
        faults.append("git-config")
    return {"changed": sorted(changed), "faults": faults}


def _apply_filter(changed: list[str], flt: dict | None) -> list[str]:
    """Chemins fautifs. `flt` `None` : tout changement est une faute (gates). Sinon
    `{allow, deny, armed}` (même règle que `guard.py`) : `.gitignore` racine autorisé ;
    non armé -> tout autorisé ; sinon faute si dans `deny` ou hors `allow`."""
    if flt is None:
        return list(changed)
    from battle_state import glob_match  # import paresseux : seul --guard/--allow en dépend
    if not flt["armed"]:
        return []
    bad = []
    for rel in changed:
        if glob_match(rel, _ROOT_ALWAYS_ALLOW):
            continue
        if (flt["deny"] and glob_match(rel, flt["deny"])) or not glob_match(rel, flt["allow"]):
            bad.append(rel)
    return bad


def _write_atomic(path: str, text: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def tree_snapshot(out: str, root_arg: str | None) -> dict:
    root = _toplevel(root_arg or os.getcwd())
    core = _collect(root)
    fp = _fingerprint(core)
    try:
        _write_atomic(out, json.dumps({"version": 1, "fingerprint": fp, **core},
                                      sort_keys=True, ensure_ascii=True))
    except OSError as exc:
        raise _Refuse(f"écriture du snapshot impossible : {exc}") from exc
    return {"ok": True, "fingerprint": fp, "count": len(core["paths"])}


def _load_guard_filter() -> dict:
    """`allow`/`deny` de la battle active de la racine d'état (`--guard`).

    Racine d'état = `battle_state.resolve_state_root(cwd)` : dépôt principal depuis un worktree
    lié, sinon le cwd. Distincte de la racine d'arbre (`_toplevel`, racine d'édition).
    `_Refuse` si illisible/invalide."""
    try:
        if _SCRIPTS_DIR not in sys.path:
            sys.path.insert(0, _SCRIPTS_DIR)
        import battle_state as bs
        from pathlib import Path
    except Exception as exc:  # noqa: BLE001
        raise _Refuse(f"battle_state.py inimportable ({type(exc).__name__}: {exc})") from exc
    active = bs.load_active_battle(bs.resolve_state_root(Path(os.getcwd())))
    if active is None:
        raise _Refuse("--guard : aucune battle active lisible (fail-closed)")
    guard, valid = bs.guard_of(active[1])
    if not valid:
        raise _Refuse("--guard : bloc `guard` invalide (fail-closed)")
    allow, deny = guard.get("allow") or [], guard.get("deny") or []
    return {"allow": allow, "deny": deny, "armed": bool(allow)}


_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _committed_paths(root: str, old: str | None, new: str | None) -> list[str] | None:
    if not old or not new:
        return None
    raw = _git_ok(root, "diff", "--name-only", "-z", old, new)
    if raw is None:
        return None
    return [os.fsdecode(x) for x in raw.split(b"\0") if x]


def _has_symlink_between(base: str, path: str) -> bool:
    """Un composant de `path` sous `base` (worktree inclus) est-il un lien symbolique ?"""
    cur = base
    for part in os.path.relpath(path, base).split(os.sep):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return True
    return False


def _accept_batch_worktrees(before: dict, after: dict, root: str) -> None:
    """Seule exception a « la liste des worktrees est dans l'empreinte » (`--batch-worktrees`,
    verify du tronc apres un lot de builders `--auto` isoles) : retire de `after` les worktrees
    APPARUS depuis le snapshot qui sont legitimes, c'est-a-dire (1) sous `<root>/.claude/worktrees/`,
    (2) non `prunable`, (3) dont l'entree admin `<commun>/worktrees/<nom>/gitdir` pointe vers
    `<chemin>/.git` et dont le fichier `.git` renvoie vers `<commun>/worktrees/<nom>`. Un worktree
    disparu ou redirige, ou tout autre nouveau worktree, reste une faute `[git-state]`."""
    try:
        bw = before["gitcfg"]["gitstate"]["worktrees"]
        aw = after["gitcfg"]["gitstate"]["worktrees"]
        bl, ba = set(bw["list"]), set(bw["admin"])
        common_raw = _git(root, "rev-parse", "--git-common-dir")
    except (KeyError, TypeError, _Refuse):
        return
    common = os.path.realpath(os.path.join(root, os.fsdecode(common_raw.strip())))
    rroot = os.path.realpath(root)
    base = os.path.join(rroot, ".claude", "worktrees")
    # Un lien symbolique entre la racine et le lot (`.claude`, `.claude/worktrees`) redirigerait
    # « le lot » vers un dossier suivi : l'exception est alors refusee (faute, pas d'acceptation).
    if any(os.path.islink(x) for x in (os.path.join(rroot, ".claude"), base)):
        return
    keep_list, keep_admin = list(aw["list"]), list(aw["admin"])
    for ent in aw["list"]:
        if ent in bl:
            continue
        path, _, rest = ent.partition("\t")
        if not path.startswith(base + os.sep) or rest.endswith("prunable"):
            continue
        if _has_symlink_between(base, path):
            continue
        tracked = _git_ok(root, "ls-files", "-z", "--", os.path.relpath(path, rroot))
        if tracked is None or tracked.strip(b"\0"):
            continue     # le chemin du worktree contient des fichiers suivis par le tronc
        for adm in aw["admin"]:
            name, _, gd = adm.partition("\t")
            if adm in ba or os.path.realpath(gd) != os.path.join(path, ".git"):
                continue
            try:
                with open(os.path.join(path, ".git"), "rb") as fh:
                    ptr = os.fsdecode(fh.read(8192).strip())
            except OSError:
                continue
            if ptr.startswith("gitdir:") and os.path.realpath(
                    ptr[len("gitdir:"):].strip()) == os.path.join(common, "worktrees", name):
                keep_list.remove(ent)
                keep_admin.remove(adm)
                break
    aw["list"], aw["admin"] = keep_list, keep_admin


def tree_verify(before_path: str | None, fingerprint: str | None, base: str | None,
                root_arg: str | None, guard: bool, allow: list[str] | None,
                batch_worktrees: bool = False) -> dict:
    flt = None
    if guard:
        flt = _load_guard_filter()
    elif allow is not None:
        flt = {"allow": allow, "deny": [], "armed": True}
    if base is not None:
        # Builder isolé : la référence est un arbre propre au commit `base`.
        cur = _toplevel(os.getcwd())
        root = os.path.realpath(root_arg or "")
        if root not in _worktrees(cur):
            raise _Refuse("--root n'est pas un worktree enregistré du dépôt courant")
        after = _collect(root)
        if after.get("gitcfg"):
            after["gitcfg"] = dict(after["gitcfg"], gitstate=_git_state(root, base_mode=True))
        before = {"head": base, "branch": after.get("branch"), "paths": {},
                  "state": {"active-battle": "absent", "battle.json": "absent"},
                  "masks": {"paths": [], "exclude": after["masks"]["exclude"],
                            "ignore_files": after["masks"]["ignore_files"]},
                  "gitcfg": dict(after.get("gitcfg") or {}, gitstate=(
                      dict(after["gitcfg"]["gitstate"],
                           private=(after["gitcfg"]["gitstate"]["private"] if
                                    after["gitcfg"]["gitstate"]["private"] == "same" else
                                    _EMPTY_STATE))
                      if after.get("gitcfg") else None))}
    else:
        try:
            with open(before_path, "r", encoding="utf-8") as fh:  # type: ignore[arg-type]
                before = json.load(fh)
        except (OSError, ValueError, RecursionError) as exc:
            raise _Refuse(f"snapshot --before illisible ({type(exc).__name__})") from exc
        if not isinstance(before, dict) or _fingerprint(before) != fingerprint:
            raise _Refuse("empreinte du snapshot différente de --fingerprint (fichier réécrit ?)")
        root = _toplevel(root_arg or os.getcwd())
        if before.get("root") != root:
            raise _Refuse("racine du dépôt différente de celle du snapshot")
        after = _collect(root)
        if batch_worktrees:
            _accept_batch_worktrees(before, after, root)
    committed = None
    if before.get("head") != after.get("head"):
        committed = _committed_paths(root, before.get("head"), after.get("head"))
    diff = _tree_diff(before, after, committed)
    if base is not None and _git_ok(root, "merge-base", "--is-ancestor", base, "HEAD") is None:
        diff["faults"].append("base")            # worktree non aligné : HEAD ne descend pas de base
    oos = _apply_filter(diff["changed"], flt)
    fault = bool(diff["faults"] or oos)
    named = [f"[{f}]" for f in diff["faults"]]
    reason = ("Arbre conforme." if not fault else
              "Faute : " + "; ".join(
                  ([f"contrôle {', '.join(named)}"] if named else [])
                  + ([f"{len(oos)} chemin(s) hors périmètre"] if oos else [])) + ".")
    return {"ok": not fault, "fault": fault, "refused": False,
            "changed_count": len(diff["changed"]),
            "changed": diff["changed"][:_MAX_LIST] + named,
            "out_of_scope": oos[:_MAX_LIST], "reason": reason}


def _tree_main(sub: str, rest: list[str]) -> int:
    val_opts = {"--out", "--root", "--before", "--fingerprint", "--base"}
    opts: dict[str, str] = {}
    allow: list[str] | None = None
    guard = False
    batch = False
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in val_opts:
            if i + 1 >= len(rest) or a in opts:
                return _usage(f"{a} attend une valeur unique")
            opts[a] = rest[i + 1]
            i += 2
        elif a == "--guard":
            guard = True
            i += 1
        elif a == "--batch-worktrees":
            batch = True
            i += 1
        elif a == "--allow":
            if allow is not None:
                return _usage("--allow ne se répète pas (liste de globs à la suite)")
            allow = []
            i += 1
            while i < len(rest) and not rest[i].startswith("--"):
                allow.append(rest[i])
                i += 1
            if not allow:
                return _usage("--allow attend au moins un glob")
        else:
            return _usage(f"argument inattendu : {a}")
    try:
        if sub == "tree-snapshot":
            if "--out" not in opts or guard or batch or allow is not None or set(opts) - {"--out", "--root"}:
                return _usage("tree-snapshot --out <fichier> [--root <dir>]")
            res = tree_snapshot(opts["--out"], opts.get("--root"))
            print(json.dumps(res, ensure_ascii=False))
            return 0
        if guard and allow is not None:
            return _usage("--guard et --allow sont exclusifs")
        if batch and "--base" in opts:
            return _usage("--batch-worktrees ne s'emploie qu'avec --before (verify du tronc)")
        if "--base" in opts:
            if "--before" in opts or "--fingerprint" in opts or "--root" not in opts:
                return _usage("tree-verify --base <sha> --root <worktree> [--guard]")
            if not _COMMIT_RE.match(opts["--base"]):
                return _usage("--base doit être un sha de commit hexadécimal")
        else:
            if "--before" not in opts or "--fingerprint" not in opts:
                return _usage("tree-verify --before <fichier> --fingerprint <sha> [...]")
            if not _SHA_RE.match(opts["--fingerprint"]):
                return _usage("--fingerprint doit être un sha256 hexadécimal")
        res = tree_verify(opts.get("--before"), opts.get("--fingerprint"), opts.get("--base"),
                          opts.get("--root"), guard, allow, batch)
    except _Refuse as exc:
        res = {"ok": False, "fault": False, "refused": True, "changed_count": 0,
               "changed": [], "out_of_scope": [], "reason": f"Refus : {exc}"}
        if sub == "tree-snapshot":
            res = {"ok": False, "refused": True, "reason": f"Refus : {exc}"}
        print(json.dumps(res, ensure_ascii=False))
        return 2
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["ok"] else 2


def _usage(msg: str) -> int:
    print(f"usage invalide : {msg}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in args:
        return _self_test()
    if not args or args[0] not in ("snapshot", "verify", "tree-snapshot", "tree-verify"):
        return _usage("sous-commande attendue : snapshot | verify | tree-snapshot | "
                      "tree-verify | --self-test")
    sub, rest = args[0], args[1:]
    if sub.startswith("tree-"):
        return _tree_main(sub, rest)
    if sub == "snapshot":
        if len(rest) != 1 or rest[0].startswith("--"):
            return _usage("snapshot <path>")
        print(json.dumps(_stat(rest[0]), ensure_ascii=False))
        return 0

    path: str | None = None
    opts: dict[str, str] = {}
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("--since", "--expected", "--returned"):
            if i + 1 >= len(rest):
                return _usage(f"{a} attend une valeur")
            opts[a] = rest[i + 1]
            i += 2
        elif a.startswith("--") or path is not None:
            return _usage(f"argument inattendu : {a}")
        else:
            path = a
            i += 1
    if path is None:
        return _usage("verify <path> [--since N] [--expected P] [--returned P]")
    since: int | None = None
    if "--since" in opts:
        try:
            since = int(opts["--since"])
        except ValueError:
            return _usage("--since doit être un entier (mtime_ns)")
    res = verify(path, since, opts.get("--expected"), opts.get("--returned"))
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["ok"] else 2


# --- Self-test hermétique (fichiers dans un tmpdir, mtime posés par os.utime) -------------

def _write(p: str, data: bytes) -> None:
    with open(p, "wb") as fh:
        fh.write(data)


def _t_absent() -> None:
    with tempfile.TemporaryDirectory() as d:
        r = verify(os.path.join(d, "nope.md"), None, None, None)
        assert r["ok"] is False and r["checks"]["exists"] is False, r
        assert "absent" in r["reason"], r


def _t_empty() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "g.md")
        _write(p, b"")
        r = verify(p, None, None, None)
        assert r["checks"]["exists"] is True and r["checks"]["non_empty"] is False, r
        assert r["ok"] is False, r


def _t_not_canonical() -> None:
    with tempfile.TemporaryDirectory() as d:
        p, q = os.path.join(d, "g.md"), os.path.join(d, "autre.md")
        _write(p, b"x")
        r = verify(p, None, None, q)
        assert r["checks"]["canonical"] is False and r["ok"] is False, r


def _t_relative_equals_absolute() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "g.md")
        _write(p, b"x")
        old = os.getcwd()
        os.chdir(d)
        try:
            r = verify(p, None, p, "g.md")
        finally:
            os.chdir(old)
        assert r["checks"]["canonical"] is True and r["ok"] is True, r


def _t_stale_same_mtime() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "g.md")
        _write(p, b"x")
        snap = _stat(p)
        r = verify(p, snap["mtime_ns"], None, None)
        assert r["checks"]["fresh"] is False and r["ok"] is False, r
        assert "périmé" in r["reason"], r


def _t_fresh_newer_mtime() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "g.md")
        _write(p, b"x")
        before = _stat(p)["mtime_ns"]
        os.utime(p, ns=(before + 10**9, before + 10**9))
        r = verify(p, before, None, p)
        assert r["checks"]["fresh"] is True and r["ok"] is True, r


def _t_fresh_created() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "g.md")
        _write(p, b"x")
        r = verify(p, None, None, None)
        assert r["checks"]["fresh"] is True and r["checks"]["canonical"] is None, r
        assert r["ok"] is True, r


def _t_snapshot_shape() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "g.md")
        assert _stat(p) == {"exists": False, "mtime_ns": None, "size": None}
        _write(p, b"abc")
        s = _stat(p)
        assert s["exists"] is True and s["size"] == 3 and isinstance(s["mtime_ns"], int), s


def _t_cli_exit_codes() -> None:
    me = os.path.abspath(__file__)

    def run(*a: str) -> tuple[int, str]:
        p = subprocess.run([sys.executable, me, *a], capture_output=True, text=True,
                           encoding="utf-8")
        return p.returncode, p.stdout

    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "g.md")
        rc, out = run("verify", p)
        assert rc == 2 and json.loads(out)["ok"] is False, (rc, out)
        rc, out = run("snapshot", p)
        assert rc == 0 and json.loads(out)["exists"] is False, (rc, out)
        _write(p, b"x")
        rc, out = run("verify", p, "--returned", p)
        assert rc == 0 and json.loads(out)["ok"] is True, (rc, out)
        rc, _ = run("verify", p, "--since", "pas-un-entier")
        assert rc == 1, rc
        rc, _ = run()
        assert rc == 1, rc



# --- Self-tests de l'empreinte d'arbre : dépôt git temporaire, jamais le repo réel ---------

class _TreeRepo:
    """Dépôt git jetable (config globale/système neutralisée) ; les commandes passent par la CLI."""

    def __init__(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._td.name)
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                        GIT_TERMINAL_PROMPT="0")
        self.git("init", "-q")
        self.write(".gitignore", "bin/\n")
        self.write("src/a.txt", "a\n")
        self.write("docs/b.txt", "b\n")
        self.git("add", "-A")
        self.commit("init")
        self.snap = os.path.join(self.root, ".legion", "_tree-before.json")
        self.fp = ""

    def __enter__(self) -> "_TreeRepo":
        return self

    def __exit__(self, *exc) -> None:
        self._td.cleanup()

    def git(self, *a: str, cwd: str | None = None) -> str:
        p = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                            "-c", "commit.gpgsign=false", *a], cwd=cwd or self.root,
                           env=self.env, capture_output=True, text=True, encoding="utf-8")
        assert p.returncode == 0, (a, p.stderr)
        return p.stdout

    def commit(self, msg: str) -> None:
        self.git("commit", "-q", "-m", msg)

    def write(self, rel: str, data: str | bytes) -> None:
        p = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        _write(p, data.encode() if isinstance(data, str) else data)

    def run(self, *a: str, env: dict | None = None, cwd: str | None = None) -> tuple[int, dict]:
        p = subprocess.run([sys.executable, os.path.abspath(__file__), *a], cwd=cwd or self.root,
                           env=env or self.env, capture_output=True, text=True, encoding="utf-8")
        try:
            out = json.loads(p.stdout) if p.stdout.strip() else {}
        except ValueError:
            out = {"raw": p.stdout}
        return p.returncode, out

    def snapshot(self) -> dict:
        rc, out = self.run("tree-snapshot", "--out", self.snap)
        assert rc == 0 and out["ok"] is True, (rc, out)
        self.fp = out["fingerprint"]
        return out

    def verify(self, *extra: str) -> tuple[int, dict]:
        return self.run("tree-verify", "--before", self.snap, "--fingerprint", self.fp, *extra)

    def battle(self, guard: dict | str | None) -> None:
        """Pose une battle active `B` (guard dict, ou texte brut pour un fichier corrompu)."""
        bd = os.path.join(self.root, ".legion", "battles", "B")
        os.makedirs(bd, exist_ok=True)
        _write(os.path.join(self.root, ".legion", "active-battle"), b"B")
        body = guard if isinstance(guard, str) else json.dumps({"id": "B", "guard": guard})
        _write(os.path.join(bd, "battle.json"), body.encode())


def _t_tree_changes() -> None:
    with _TreeRepo() as r:
        r.snapshot()
        rc, out = r.verify()
        assert rc == 0 and out["ok"] is True and out["refused"] is False, (rc, out)  # rien
        r.write("src/a.txt", "changed\n")                                            # T1
        rc, out = r.verify()
        assert rc == 2 and out["fault"] is True and "src/a.txt" in out["changed"], out
        r.write("src/a.txt", "a\n")
        assert r.verify()[0] == 0                       # retour à l'identique : conforme
        r.write("new/a/b.txt", "n")                                                   # T2
        rc, out = r.verify()
        assert rc == 2 and "new/a/b.txt" in out["changed"], out
    with _TreeRepo() as r:
        r.write("loose.txt", "x")
        r.snapshot()
        os.remove(os.path.join(r.root, "loose.txt"))                                  # T3 (non suivi)
        assert r.verify()[0] == 2
        r.snapshot()
        os.remove(os.path.join(r.root, "src", "a.txt"))                               # T3 (suivi)
        rc, out = r.verify()
        assert rc == 2 and "src/a.txt" in out["changed"], out


def _t_tree_dirty_start() -> None:
    with _TreeRepo() as r:
        r.write("src/a.txt", "dirty\n")
        r.snapshot()
        assert r.verify()[0] == 0                                                     # T5
        r.write("src/a.txt", "dirty again\n")                                        # T4
        rc, out = r.verify()
        assert rc == 2 and "src/a.txt" in out["changed"], out


def _t_tree_index_and_head() -> None:
    with _TreeRepo() as r:
        r.write("src/a.txt", "m\n")
        r.snapshot()
        r.git("add", "src/a.txt")                                                     # T6
        rc, out = r.verify()
        assert rc == 2 and "src/a.txt" in out["changed"], out
    with _TreeRepo() as r:
        r.snapshot()
        r.write("src/a.txt", "m\n")
        r.git("add", "-A")
        r.commit("x")                                                                 # T7
        rc, out = r.verify()
        assert rc == 2 and "src/a.txt" in out["changed"], out
    with _TreeRepo() as r:
        r.snapshot()
        r.git("mv", "src/a.txt", "src/c.txt")                                         # T8
        rc, out = r.verify()
        assert rc == 2 and "src/a.txt" in out["changed"] and "src/c.txt" in out["changed"], out
    with _TreeRepo() as r:                    # branche changée au même commit
        r.snapshot()
        r.git("checkout", "-q", "-b", "other")
        rc, out = r.verify()
        assert rc == 2 and "[branch]" in out["changed"], out


def _t_tree_ignored_and_legion() -> None:
    with _TreeRepo() as r:
        r.snapshot()
        r.write("bin/x", "1")                                                         # T9
        r.write(".legion/battles/B/gate-lint.md", "# g")                              # T10
        rc, out = r.verify()
        assert rc == 0 and out["ok"] is True, out
        r.write("bin/x", "2")
        assert r.verify()[0] == 0


def _t_tree_state() -> None:
    with _TreeRepo() as r:
        r.battle(None)
        r.snapshot()
        assert r.verify()[0] == 0
        r.write(".legion/battles/B/battle.json", json.dumps({"id": "B", "x": 1}))     # T11
        rc, out = r.verify("--allow", "**")
        assert rc == 2 and out["fault"] is True and any("battle.json]" in c for c in out["changed"]), out
        r.snapshot()
        r.write(".legion/active-battle", "C")
        rc, out = r.verify("--allow", "**")
        assert rc == 2 and "[.legion/active-battle]" in out["changed"], out


def _t_tree_index_mask() -> None:
    with _TreeRepo() as r:
        r.snapshot()
        r.git("update-index", "--assume-unchanged", "src/a.txt")                      # T12
        rc, out = r.verify()
        assert rc == 2 and "[index-mask]" in out["changed"], out
    with _TreeRepo() as r:
        r.snapshot()
        r.write(".git/info/exclude", "secret\n")
        rc, out = r.verify()
        assert rc == 2 and "[index-mask]" in out["changed"], out


def _t_tree_crlf() -> None:
    with _TreeRepo() as r:
        r.write("src/a.txt", b"l1\r\nl2\r\n")
        r.snapshot()
        r.write("src/a.txt", b"l1\nl2\n")                                           # T13
        assert r.verify()[0] == 2


def _t_tree_allow_filter() -> None:
    with _TreeRepo() as r:
        r.snapshot()
        r.write("src/a.txt", "1")
        assert r.verify("--allow", "src/**")[0] == 0                                  # T14
        r.write("docs/b.txt", "2")
        rc, out = r.verify("--allow", "src/**")
        assert rc == 2 and out["out_of_scope"] == ["docs/b.txt"], out
        assert r.verify("--allow", "src/**", "docs/**")[0] == 0
    with _TreeRepo() as r:                           # `.gitignore` racine : toujours autorisé
        r.battle({"allow": ["src/**"]})
        r.snapshot()
        r.write(".gitignore", "bin/\nobj/\n")
        assert r.verify("--guard")[0] == 0


def _t_tree_guard() -> None:
    with _TreeRepo() as r:                                                            # T15
        r.battle({"allow": ["src/**"], "deny": ["src/secret/**"]})
        r.snapshot()
        r.write("src/a.txt", "1")
        assert r.verify("--guard")[0] == 0
        r.write("src/secret/k", "1")
        rc, out = r.verify("--guard")
        assert rc == 2 and out["out_of_scope"] == ["src/secret/k"], out
    with _TreeRepo() as r:                                   # non armé : tout autorisé
        r.battle({"allow": []})
        r.snapshot()
        r.write("docs/b.txt", "1")
        assert r.verify("--guard")[0] == 0
    with _TreeRepo() as r:                                   # hors périmètre
        r.battle({"allow": ["src/**"]})
        r.snapshot()
        r.write("docs/b.txt", "1")
        rc, out = r.verify("--guard")
        assert rc == 2 and out["out_of_scope"] == ["docs/b.txt"], out
    for bad in ({"allow": "src/**"}, "{oops"):               # bloc invalide / illisible -> refus
        with _TreeRepo() as r:
            r.battle({"allow": ["src/**"]})
            r.snapshot()
            r.battle(bad)
            rc, out = r.verify("--guard")
            assert rc == 2 and out["refused"] is True and out["ok"] is False, (bad, out)
    with _TreeRepo() as r:                                   # aucune battle active -> refus
        r.snapshot()
        rc, out = r.verify("--guard")
        assert rc == 2 and out["refused"] is True, out


def _t_tree_usage() -> None:
    with _TreeRepo() as r:                                                            # T16
        r.snapshot()
        assert r.verify("--guard", "--allow", "x")[0] == 1
        assert r.run("tree-verify", "--before", r.snap)[0] == 1
        assert r.run("tree-verify", "--fingerprint", r.fp)[0] == 1
        assert r.run("tree-verify", "--before", r.snap, "--fingerprint", "zz")[0] == 1
        assert r.run("tree-verify", "--base", "abc", "--root", r.root)[0] == 1
        assert r.run("tree-snapshot")[0] == 1
        assert r.run("tree-snapshot", "--out", r.snap, "--guard")[0] == 1
        assert r.run("tree-verify", "--before", r.snap, "--fingerprint", r.fp, "--allow")[0] == 1
        assert r.run("tree-verify", "--bogus")[0] == 1


def _t_tree_refusals() -> None:
    with _TreeRepo() as r:                                                            # T17
        r.snapshot()
        data = json.load(open(r.snap, encoding="utf-8"))
        data["paths"] = {}
        data["head"] = "0" * 40
        _write(r.snap, json.dumps(data).encode())                # snapshot réécrit
        rc, out = r.verify()
        assert rc == 2 and out["refused"] is True and out["ok"] is False, out
        r.snapshot()
        _write(r.snap, b"pas du json")
        assert r.verify()[1]["refused"] is True
        r.snapshot()
        os.remove(r.snap)
        assert r.verify()[1]["refused"] is True
    with _TreeRepo() as r, _TreeRepo() as o:                 # root différent
        r.snapshot()
        rc, out = r.run("tree-verify", "--before", r.snap, "--fingerprint", r.fp,
                        "--root", o.root)
        assert rc == 2 and out["refused"] is True, out


def _t_tree_no_git() -> None:
    with tempfile.TemporaryDirectory() as d:                                          # T18
        d = os.path.realpath(d)
        env = dict(os.environ, GIT_CEILING_DIRECTORIES=os.path.dirname(d),
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        me = os.path.abspath(__file__)
        for args in (["tree-snapshot", "--out", os.path.join(d, "s.json")],
                     ["tree-verify", "--before", os.path.join(d, "s.json"),
                      "--fingerprint", "0" * 64]):
            p = subprocess.run([sys.executable, me, *args], cwd=d, env=env, capture_output=True,
                               text=True, encoding="utf-8")
            assert p.returncode == 2 and json.loads(p.stdout)["refused"] is True, (args, p.stdout)
        env["PATH"] = os.path.join(d, "no-such-bin")         # git introuvable
        p = subprocess.run([sys.executable, me, "tree-snapshot", "--out", os.path.join(d, "s.json")],
                           cwd=d, env=env, capture_output=True, text=True, encoding="utf-8")
        out = json.loads(p.stdout)
        assert p.returncode == 2 and out["refused"] is True and out["ok"] is False, p.stdout
        assert not os.path.exists(os.path.join(d, "s.json"))


def _t_tree_special_files() -> None:
    if not hasattr(os, "mkfifo") or not hasattr(os, "symlink"):
        return
    with _TreeRepo() as r:                                                            # T19
        os.mkfifo(os.path.join(r.root, "pipe"))
        os.symlink("nowhere", os.path.join(r.root, "lnk"))
        r.snapshot()
        data = json.load(open(r.snap, encoding="utf-8"))
        assert data["paths"]["lnk"].startswith("??:-:link:"), data["paths"]
        assert _hash_entry(r.root, "pipe", 0).startswith("special:")   # jamais ouvert
        assert r.verify()[0] == 0


def _t_tree_worktrees() -> None:
    with _TreeRepo() as r:
        base = r.git("rev-parse", "HEAD").strip()
        wt = os.path.join(r.root, ".claude", "worktrees", "w1")                       # T21
        r.git("worktree", "add", "-q", "-b", "wt1", wt)
        r.battle({"allow": ["src/**"]})
        r.snapshot()
        data = json.load(open(r.snap, encoding="utf-8"))
        assert not any(k.startswith(".claude") for k in data["paths"]), data["paths"]
        os.makedirs(os.path.join(wt, "src"), exist_ok=True)                           # T20
        _write(os.path.join(wt, "src", "n.txt"), b"1")
        rc, out = r.run("tree-verify", "--base", base, "--root", wt, "--guard")
        assert rc == 0 and out["ok"] is True, out
        os.makedirs(os.path.join(wt, "docs"), exist_ok=True)
        _write(os.path.join(wt, "docs", "z.txt"), b"1")
        rc, out = r.run("tree-verify", "--base", base, "--root", wt, "--guard")
        assert rc == 2 and out["out_of_scope"] == ["docs/z.txt"], out
        assert r.verify()[0] == 0                            # l'arbre principal reste intact
        with tempfile.TemporaryDirectory() as other:         # racine non enregistrée -> refus
            rc, out = r.run("tree-verify", "--base", base, "--root", other, "--guard")
            assert rc == 2 and out["refused"] is True, out


def _t_tree_base_ancestry() -> None:
    with _TreeRepo() as r:                                                            # V1
        old = r.git("rev-parse", "HEAD").strip()
        r.write("src/found.txt", "f\n")
        r.git("add", "-A")
        r.commit("fondation")
        new = r.git("rev-parse", "HEAD").strip()
        wt = os.path.join(r.root, ".claude", "worktrees", "w1")
        r.git("worktree", "add", "-q", "--detach", wt, old)      # worktree resté à l'ancien HEAD
        r.battle({"allow": ["src/**"]})
        rc, out = r.run("tree-verify", "--base", new, "--root", wt, "--guard")
        assert rc == 2 and out["fault"] is True and "[base]" in out["changed"], out
        r.git("reset", "-q", "--hard", new, cwd=wt)              # aligne
        rc, out = r.run("tree-verify", "--base", new, "--root", wt, "--guard")
        assert rc == 0 and out["ok"] is True, out


def _t_tree_stdout_bounded() -> None:
    with _TreeRepo() as r:                                                            # T22
        for i in range(500):
            r.write(f"many/f{i}.txt", "x")
        rc, out = r.run("tree-snapshot", "--out", r.snap)
        assert rc == 0 and set(out) == {"ok", "fingerprint", "count"} and out["count"] == 500, out
        r.fp = out["fingerprint"]
        for i in range(500):
            r.write(f"more/f{i}.txt", "x")
        rc, out = r.verify()
        assert rc == 2 and out["changed_count"] == 500 and len(out["changed"]) == _MAX_LIST, out


def _t_tree_nested_repo() -> None:
    with _TreeRepo() as r:
        sub = os.path.join(r.root, "nest")
        os.makedirs(sub)
        r.git("init", "-q", cwd=sub)
        _write(os.path.join(sub, "f"), b"1")
        r.snapshot()
        assert r.verify()[0] == 0
        _write(os.path.join(sub, "f"), b"2")
        rc, out = r.verify()
        assert rc == 2 and "nest" in out["changed"], out


def _t_tree_diff_pure() -> None:
    b = {"head": "h", "branch": "m", "paths": {"a": "x"}, "state": {}, "masks": {}}
    a = {"head": "h", "branch": "m", "paths": {"a": "y", "n": "z"}, "state": {}, "masks": {}}
    assert _tree_diff(b, b, None) == {"changed": [], "faults": []}
    assert _tree_diff(b, a, None)["changed"] == ["a", "n"]
    assert _tree_diff(b, dict(a, head="h2"), None)["faults"] == ["HEAD"]
    assert _tree_diff(b, dict(b, head="h2"), [])["faults"] == ["HEAD"]        # commit vide
    g = dict(b, gitcfg={"config": "1", "hooks": "1", "attributes": "1"})
    assert _tree_diff(g, dict(g, gitcfg={"config": "2", "hooks": "2", "attributes": "1"}),
                      None)["faults"] == ["git-config", "git-hooks"]
    assert "c" in _tree_diff(b, dict(a, head="h2"), ["c"])["changed"]
    assert _apply_filter(["a"], None) == ["a"]
    assert _apply_filter(["a"], {"allow": [], "deny": [], "armed": False}) == []


def _t_tree_git_config_and_hooks() -> None:
    """GH#66 (security FAIL) : config git et hooks font partie de l'empreinte."""
    with _TreeRepo() as r:                    # core.fsmonitor posé par la gate
        r.snapshot()
        r.write("fsm.sh", "#!/bin/sh\nexit 0\n")
        r.git("config", "core.fsmonitor", os.path.join(r.root, "fsm.sh"))
        rc, out = r.verify("--allow", "**")
        assert rc == 2 and "[git-config]" in out["changed"], out
    with _TreeRepo() as r:                    # core.trustctime=false posé par la gate
        r.snapshot()
        r.git("config", "core.trustctime", "false")
        rc, out = r.verify("--allow", "**")
        assert rc == 2 and "[git-config]" in out["changed"], out
    with _TreeRepo() as r:                    # .git/hooks/pre-commit écrit par la gate
        r.snapshot()
        r.write(".git/hooks/pre-commit", "#!/bin/sh\nexit 0\n")
        rc, out = r.verify("--allow", "**")
        assert rc == 2 and "[git-hooks]" in out["changed"], out
    with _TreeRepo() as r:                    # core.hooksPath -> dossier hors .git
        r.write("hk/pre-commit", "#!/bin/sh\nexit 0\n")
        r.git("config", "core.hooksPath", os.path.join(r.root, "hk"))
        r.snapshot()
        r.write("hk/pre-commit", "#!/bin/sh\necho x\n")
        rc, out = r.verify("--allow", "**")
        assert rc == 2 and "[git-hooks]" in out["changed"], out
    with _TreeRepo() as r:                    # info/attributes
        r.snapshot()
        r.write(".git/info/attributes", "* text=auto\n")
        rc, out = r.verify("--allow", "**")
        assert rc == 2 and "[git-attributes]" in out["changed"], out
    with _TreeRepo() as r:                    # config inchangée : conforme
        r.snapshot()
        assert r.verify()[0] == 0


def _t_tree_forced_git_options() -> None:
    """Les options imposées par `_git` rendent inopérants les réglages posés AVANT le snapshot."""
    with _TreeRepo() as r:
        old = time.time() - 30
        r.write("src/a.txt", "aa\n")
        os.utime(os.path.join(r.root, "src", "a.txt"), (old, old))
        r.git("add", "-A")
        r.commit("old-mtime")
        r.git("config", "core.trustctime", "false")
        r.snapshot()
        p = os.path.join(r.root, "src", "a.txt")
        time.sleep(1.1)                           # git compare le ctime à la seconde
        _write(p, b"bb\n")                       # même taille, mtime restauré
        os.utime(p, (old, old))
        rc, out = r.verify()
        assert rc == 2 and "src/a.txt" in out["changed"], out
    with _TreeRepo() as r:                    # fsmonitor menteur déjà en place avant le snapshot
        r.write("fsm.sh", "#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(r.root, "fsm.sh"), 0o755)
        r.git("config", "core.fsmonitor", os.path.join(r.root, "fsm.sh"))
        r.snapshot()
        r.write("src/a.txt", "evil\n")
        rc, out = r.verify()
        assert rc == 2 and "src/a.txt" in out["changed"], out


def _t_tree_empty_commit_and_ignores() -> None:
    with _TreeRepo() as r:                    # commit vide : HEAD bouge sans chemin (review R1)
        r.snapshot()
        r.git("commit", "-q", "--allow-empty", "-m", "e")
        rc, out = r.verify()
        assert rc == 2 and "[HEAD]" in out["changed"], out
    with _TreeRepo() as r:                    # .gitignore qui s'ignore (security WARN T1)
        r.snapshot()
        r.write("src/.gitignore", "*\n")
        r.write("src/Evil.cs", "x")
        rc, out = r.verify()
        assert rc == 2 and "[index-mask]" in out["changed"], out
    with _TreeRepo() as r:                    # dossier neuf dont le .gitignore ignore tout
        r.snapshot()
        r.write("newdir/.gitignore", "*\n")
        r.write("newdir/Evil.cs", "x")
        rc, out = r.verify()
        assert rc == 2 and "[index-mask]" in out["changed"], out
    with _TreeRepo() as r:                    # core.excludesFile
        r.write("ex.txt", "*.cs\n")
        r.snapshot()
        r.git("config", "core.excludesFile", os.path.join(r.root, "ex.txt"))
        r.write("src/New.cs", "x")
        rc, out = r.verify()
        assert rc == 2 and "[git-config]" in out["changed"], out
    with _TreeRepo() as r:                    # `.LEGION` (autre casse) n'est pas `.legion`
        r.snapshot()
        r.write(".LEGION/code.txt", "x")
        if os.path.isdir(os.path.join(r.root, ".LEGION")) and not os.path.isdir(
                os.path.join(r.root, ".legion")):
            rc, out = r.verify()
            assert rc == 2 and ".LEGION/code.txt" in out["changed"], out


def _t_tree_git_state() -> None:
    """GH#66 (security FAIL 2) : l'etat du git-dir est empreinte de facon generique."""
    faults = (
        ("MERGE_HEAD", lambda r: r.write(".git/MERGE_HEAD", r.git("rev-parse", "HEAD"))),
        ("CHERRY_PICK_HEAD", lambda r: r.write(".git/CHERRY_PICK_HEAD", "x\n")),
        ("REVERT_HEAD", lambda r: r.write(".git/REVERT_HEAD", "x\n")),
        ("ORIG_HEAD", lambda r: r.write(".git/ORIG_HEAD", "x\n")),
        ("ref de tag", lambda r: r.write(".git/refs/tags/evil", r.git("rev-parse", "HEAD"))),
        ("ref distant", lambda r: r.write(".git/refs/remotes/origin/main", r.git("rev-parse", "HEAD"))),
        ("packed-refs", lambda r: r.write(
            ".git/packed-refs", "# pack-refs with: peeled fully-peeled sorted \n"
            + r.git("rev-parse", "HEAD").strip() + " refs/tags/evil\n")),
        ("rebase-merge/", lambda r: os.makedirs(os.path.join(r.root, ".git", "rebase-merge"))),
        ("sequencer/", lambda r: r.write(".git/sequencer/todo", "pick x\n")),
        ("shallow", lambda r: r.write(".git/shallow", r.git("rev-parse", "HEAD"))),
        ("info/grafts", lambda r: r.write(".git/info/grafts", r.git("rev-parse", "HEAD"))),
        ("modules/", lambda r: r.write(".git/modules/m/HEAD", "x\n")),
        ("objects/info/alternates", lambda r: r.write(".git/objects/info/alternates", "/x\n")),
    )
    for label, write in faults:
        with _TreeRepo() as r:
            r.snapshot()
            write(r)
            rc, out = r.verify("--allow", "**")
            if label == "objects/info/alternates":      # objects/ est exclu : limite documentee
                assert rc == 0, (label, out)
            else:
                assert rc == 2 and "[git-state]" in out["changed"], (label, out)
    with _TreeRepo() as r:                    # lectures et ecritures « inertes » de git : aucune faute
        r.snapshot()
        os.utime(os.path.join(r.root, "src", "a.txt"), (time.time() + 5, time.time() + 5))
        r.git("status")                       # rafraichit l'index reel
        r.git("diff")
        r.git("log")
        r.git("ls-files", "-s")
        r.write(".git/FETCH_HEAD", "abc\tnot-for-merge\n")           # ce que `git fetch` ecrit
        r.write(".git/logs/refs/remotes/origin/main", "x\n")
        r.write(".git/objects/ab/cdef", "x")                          # objet (hash-object -w)
        r.write(".git/index.lock", "")
        r.write(".git/COMMIT_EDITMSG", "m\n")
        r.write(".git/sg-hook-once-toolu_x", "1")                     # hook tiers : inconnu de git
        r.write(".git/description", "d\n")
        assert r.verify()[0] == 0, r.verify()
    with _TreeRepo() as r:                    # index falsifie : blob d'origine + stat du nouveau fichier
        old = r.git("rev-parse", ":src/a.txt").strip()
        r.snapshot()
        r.write("src/a.txt", "x\n")
        past = time.time() - 30                # evite la detection « racy git » (mtime >= index)
        os.utime(os.path.join(r.root, "src", "a.txt"), (past, past))
        time.sleep(1.1)
        r.git("add", "src/a.txt")
        new = r.git("rev-parse", ":src/a.txt").strip()
        ip = os.path.join(r.root, ".git", "index")
        raw = open(ip, "rb").read()
        raw = raw[:-20].replace(bytes.fromhex(new), bytes.fromhex(old))
        _write(ip, raw + hashlib.sha1(raw).digest())
        assert r.git("status", "--porcelain", "--", "src").strip() == "", "index falsifie non effectif"
        rc, out = r.verify()
        assert rc == 2 and "src/a.txt" in out["changed"], out
    with _TreeRepo() as r:                    # config globale : filtre / excludesFile poses hors depot
        gcfg = os.path.join(r.root, "..", os.path.basename(r.root) + ".gitconfig")
        _write(gcfg, b"")
        try:
            r.env["GIT_CONFIG_GLOBAL"] = gcfg
            r.snapshot()
            _write(gcfg, b"[core]\n\texcludesFile = /x\n")
            rc, out = r.verify("--allow", "**")
            assert rc == 2 and "[git-config]" in out["changed"], out
        finally:
            os.unlink(gcfg)
    with _TreeRepo() as r:                    # attributes XDG
        xdg = os.path.join(r.root, "xdg")
        r.env["XDG_CONFIG_HOME"] = xdg
        r.snapshot()
        r.write("xdg/git/attributes", "* filter=hide\n")
        rc, out = r.verify("--allow", "**")
        assert rc == 2 and "[git-config]" in out["changed"], out
    with _TreeRepo() as r:                    # builder isole : MERGE_HEAD dans son git-dir prive
        base = r.git("rev-parse", "HEAD").strip()
        wt = os.path.join(r.root, ".claude", "worktrees", "w9")
        r.git("worktree", "add", "-q", "-b", "wt9", wt)
        r.battle({"allow": ["src/**"]})
        priv = os.path.realpath(r.git("rev-parse", "--absolute-git-dir", cwd=wt).strip())
        _write(os.path.join(priv, "MERGE_HEAD"), base.encode() + b"\n")
        rc, out = r.run("tree-verify", "--base", base, "--root", wt, "--guard")
        assert rc == 2 and "[git-state]" in out["changed"], out


def _t_tree_refs_and_worktrees() -> None:
    """GH#66 (security tour 3) : branches, worktrees enregistres, fichiers inconnus de git."""
    with _TreeRepo() as r:                    # fichier inconnu de git : inerte ; MERGE_HEAD : faute
        r.snapshot()
        r.write(".git/sg-hook-once-x", "1")
        assert r.verify()[0] == 0
        r.write(".git/MERGE_HEAD", r.git("rev-parse", "HEAD"))
        rc, out = r.verify("--allow", "**")
        assert rc == 2 and "[git-state]" in out["changed"], out
    sha = lambda r: r.git("rev-parse", "HEAD").strip()   # noqa: E731
    with _TreeRepo() as r:                    # branche de livraison creee par une gate (loose)
        r.snapshot()
        r.write(".git/refs/heads/me/42", sha(r))
        rc, out = r.verify("--allow", "**")
        assert rc == 2 and "[git-state]" in out["changed"], out
    with _TreeRepo() as r:                    # idem via packed-refs
        r.snapshot()
        r.write(".git/packed-refs", "# pack-refs with: peeled fully-peeled sorted \n"
                + sha(r) + " refs/heads/me/42\n")
        rc, out = r.verify("--allow", "**")
        assert rc == 2 and "[git-state]" in out["changed"], out
    with _TreeRepo() as r:                    # branche d'un worktree deja enregistre : bouge sans faute
        wt = os.path.join(r.root, ".claude", "worktrees", "w1")
        r.git("worktree", "add", "-q", "-b", "mf/wt1", wt)
        r.snapshot()
        _write(os.path.join(wt, "f.txt"), b"1")
        r.git("add", "f.txt", cwd=wt)
        r.git("commit", "-q", "-m", "b", cwd=wt)
        assert r.verify()[0] == 0, r.verify()
    with _TreeRepo() as r:                    # faux worktree qui pointe un dossier suivi
        r.write("lib/c.txt", "1\n")
        r.git("add", "lib/c.txt")
        r.commit("lib")
        r.snapshot()
        fake = os.path.join(r.root, ".git", "worktrees", "evil")
        r.write(".git/worktrees/evil/gitdir", os.path.join(r.root, "lib", ".git") + "\n")
        r.write(".git/worktrees/evil/HEAD", sha(r) + "\n")
        r.write(".git/worktrees/evil/commondir", "../..\n")
        r.write("lib/c.txt", "2\n")
        for extra in ((), ("--batch-worktrees",)):
            rc, out = r.verify("--allow", "**", *extra)
            assert rc == 2 and "[git-state]" in out["changed"], (extra, out)
        assert os.path.isdir(fake)
    with _TreeRepo() as r:                    # blob indexe change sous « MM » : vu (WARN tour 3)
        r.write("src/a.txt", "one\n")
        r.git("add", "src/a.txt")
        r.write("src/a.txt", "two\n")        # « MM » : indexe puis re-modifie
        r.snapshot()
        r.write("src/a.txt", "three\n")
        r.git("add", "src/a.txt")
        r.write("src/a.txt", "two\n")        # contenu worktree identique, blob indexe different
        rc, out = r.verify()
        assert rc == 2 and "src/a.txt" in out["changed"], out
    with _TreeRepo() as r:                    # lot de builders : worktree apparu apres le snapshot
        r.snapshot()
        wt = os.path.join(r.root, ".claude", "worktrees", "w2")
        r.git("worktree", "add", "-q", "-b", "mf/b2", wt)
        rc, out = r.verify()
        assert rc == 2 and "[git-state]" in out["changed"], out
        assert r.verify("--batch-worktrees")[0] == 0, r.verify("--batch-worktrees")
        evil = os.path.join(r.root, "outside")            # hors de .claude/worktrees : refuse
        r.git("worktree", "add", "-q", "-b", "evil", evil)
        rc, out = r.verify("--batch-worktrees")
        assert rc == 2 and "[git-state]" in out["changed"], out
        assert r.run("tree-snapshot", "--out", r.snap, "--batch-worktrees")[0] == 1
    with _TreeRepo() as r:                    # `.claude` lien symbolique vers un dossier suivi : faute
        r.write("lib/c.txt", "1\n")
        r.git("add", "lib/c.txt")
        r.commit("lib")
        r.snapshot()
        os.symlink(os.path.join(r.root, "lib"), os.path.join(r.root, ".claude"))
        r.git("worktree", "add", "-q", "--detach", os.path.join(r.root, ".claude", "worktrees", "w9"))
        rc, out = r.verify("--allow", "**", "--batch-worktrees")
        assert rc == 2 and "[git-state]" in out["changed"], out
    with _TreeRepo() as r:                    # worktree du lot dont le chemin est suivi : faute
        r.write(".claude/worktrees/w8/keep.txt", "1\n")
        r.git("add", "-f", ".claude/worktrees/w8/keep.txt")
        r.commit("tracked")
        r.snapshot()
        shutil.rmtree(os.path.join(r.root, ".claude", "worktrees", "w8"))
        r.git("worktree", "add", "-q", "--force", "--detach", os.path.join(r.root, ".claude", "worktrees", "w8"))
        rc, out = r.verify("--allow", "**", "--batch-worktrees")
        assert rc == 2 and "[git-state]" in out["changed"], out
    with _TreeRepo() as r:                    # casse : `merge_head` lu par git sur FS insensible
        r.snapshot()
        r.write(".git/merge_head", sha(r))
        rc, out = r.verify("--allow", "**")
        assert rc == 2 and "[git-state]" in out["changed"], out
    with _TreeRepo() as r:                    # worktree disparu : faute meme avec le drapeau
        wt = os.path.join(r.root, ".claude", "worktrees", "w3")
        r.git("worktree", "add", "-q", "-b", "mf/b3", wt)
        r.snapshot()
        r.git("worktree", "remove", "--force", wt)
        rc, out = r.verify("--batch-worktrees")
        assert rc == 2 and "[git-state]" in out["changed"], out


def _t_tree_guard_from_worktree() -> None:
    """GH#128 : `--guard` lancé depuis un worktree lié lit la battle du dépôt principal."""
    with _TreeRepo() as r:
        base = r.git("rev-parse", "HEAD").strip()
        wt = os.path.join(r.root, ".claude", "worktrees", "w1")
        r.git("worktree", "add", "-q", "-b", "wt1", wt)
        r.battle({"allow": ["src/**"]})
        snap = os.path.join(r.root, ".legion", "_wt-before.json")
        rc, out = r.run("tree-snapshot", "--out", snap, cwd=wt)                        # A1
        assert rc == 0 and out["ok"] is True, (rc, out)
        fp = out["fingerprint"]
        os.makedirs(os.path.join(wt, "src"), exist_ok=True)
        _write(os.path.join(wt, "src", "n.txt"), b"1")
        rc, out = r.run("tree-verify", "--before", snap, "--fingerprint", fp, "--guard", cwd=wt)
        assert rc == 0 and out["ok"] is True, out
        os.makedirs(os.path.join(wt, "docs"), exist_ok=True)
        _write(os.path.join(wt, "docs", "z.txt"), b"1")
        rc, out = r.run("tree-verify", "--before", snap, "--fingerprint", fp, "--guard", cwd=wt)
        assert rc == 2 and out["out_of_scope"] == ["docs/z.txt"], out
        rc, out = r.run("tree-verify", "--base", base, "--root", wt, "--guard", cwd=wt)  # A2
        assert rc == 2 and out["out_of_scope"] == ["docs/z.txt"], out
        os.remove(os.path.join(r.root, ".legion", "active-battle"))                    # A3
        rc, out = r.run("tree-verify", "--base", base, "--root", wt, "--guard", cwd=wt)
        assert rc == 2 and out["refused"] is True, out


_TREE_TESTS = (
    _t_tree_git_state, _t_tree_refs_and_worktrees,
    _t_tree_git_config_and_hooks, _t_tree_forced_git_options, _t_tree_empty_commit_and_ignores,
    _t_tree_diff_pure, _t_tree_changes, _t_tree_dirty_start, _t_tree_index_and_head,
    _t_tree_ignored_and_legion, _t_tree_state, _t_tree_index_mask, _t_tree_crlf,
    _t_tree_allow_filter, _t_tree_guard, _t_tree_usage, _t_tree_refusals, _t_tree_no_git,
    _t_tree_special_files, _t_tree_worktrees, _t_tree_base_ancestry, _t_tree_guard_from_worktree,
    _t_tree_stdout_bounded, _t_tree_nested_repo,
)

_TESTS = (_t_absent, _t_empty, _t_not_canonical, _t_relative_equals_absolute,
          _t_stale_same_mtime, _t_fresh_newer_mtime, _t_fresh_created,
          _t_snapshot_shape, _t_cli_exit_codes) + _TREE_TESTS


def _self_test() -> int:
    failed = 0
    for fn in _TESTS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - on rapporte puis on continue
            failed += 1
            print(f"FAIL: {fn.__name__}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if failed:
        print(f"FAIL: artifact_check self-test ({failed}/{len(_TESTS)} en échec)",
              file=sys.stderr)
        return 1
    print("OK: artifact_check self-test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # accents FR sur une console cp1252
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
