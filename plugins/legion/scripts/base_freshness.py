"""Filet base-freshness de legion (DELIVER §G.0.a), version exécutable et testable.

Avant de livrer, les gates doivent avoir jugé la base réellement poussée. « HEAD en
retard de N commits sur origin » ne veut pas dire « l'arbre livré diffère » : un merge
pur laisse l'arbre **identique**. Ce script tranche de façon **déterministe** ce que la
doctrine §G.0.a décrivait en prose, pour que l'orchestrateur ne le calcule plus à la
main (RETEX 184b947a61da / fbfda4e506b1) :

- `behind == 0`                         → **fresh**          (base à jour, rien à faire).
- en retard, mais arbre identique       → **waive_pure_merge** (merge pur : re-gate waive,
                                          sanity build/test, puis rebase).
- en retard, arbre différent, delta de base **disjoint** des fichiers touchés
                                        → **waive_disjoint**  (re-gate skippable, justifier
                                          avec `base_delta`, puis rebase).
- en retard, arbre différent, delta **intersecte** les fichiers touchés
                                        → **regate**          (re-run BUILD + gates sur la
                                          base à jour).

Le cœur de décision (`_classify`) est **pur** : il ne touche ni git ni le disque, donc
il est couvert par `--self-test` de façon **hermétique** (aucune dépendance au repo hôte
— le script est appelé depuis n'importe quel repo cible, toute stack). Les appels git
(`_behind_count`, `_tree_delta_empty`, `_delta_files`, `_default_branch`) sont isolés ;
toute erreur git dégrade vers **regate** (le choix sûr : en cas de doute, on re-gate).

`--touched` = les fichiers de la slice (lus de `build-report.md` par l'orchestrateur).
Le script compare l'**intersection des chemins** ; les dépendances transitives (« le
delta touche un fichier dont la slice dépend ») restent au jugement de l'orchestrateur.

Usage :
    python base_freshness.py --touched <f1> <f2> … [--default-branch <name>] [--repo <path>]
    python base_freshness.py --self-test

Sortie : un objet JSON sur stdout
    { verdict, behind, tree_delta_empty, base_delta, intersection, reason }
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run_git(args: list[str], repo: str) -> tuple[int, str]:
    """Exécute `git -C <repo> <args>`. Retourne (returncode, stdout). 127 si git absent."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, encoding="utf-8",
        )
        return proc.returncode, proc.stdout
    except OSError:
        return 127, ""


def _default_branch(repo: str, explicit: str | None) -> str | None:
    """Branche par défaut d'origin : `--default-branch` s'il est fourni, sinon
    `origin/HEAD`, sinon `main`/`master` si l'une existe. None si introuvable."""
    if explicit:
        return explicit
    rc, out = _run_git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], repo)
    if rc == 0 and out.strip():
        return out.strip().rsplit("/", 1)[-1]
    for cand in ("main", "master"):
        rc, _ = _run_git(["rev-parse", "--verify", "--quiet", f"origin/{cand}"], repo)
        if rc == 0:
            return cand
    return None


def _behind_count(default: str, repo: str) -> int:
    rc, out = _run_git(["rev-list", "--count", f"HEAD..origin/{default}"], repo)
    if rc != 0:
        raise RuntimeError(f"rev-list HEAD..origin/{default} a échoué")
    return int(out.strip() or "0")


def _tree_delta_empty(default: str, repo: str) -> bool:
    """`git diff --quiet HEAD origin/<default>` : exit 0 = arbre identique, 1 = diff."""
    rc, _ = _run_git(["diff", "--quiet", "HEAD", f"origin/{default}"], repo)
    if rc not in (0, 1):
        raise RuntimeError("git diff --quiet a échoué")
    return rc == 0


def _delta_files(default: str, repo: str) -> list[str]:
    rc, out = _run_git(["diff", "--name-only", "HEAD", f"origin/{default}"], repo)
    if rc != 0:
        raise RuntimeError("git diff --name-only a échoué")
    return [line for line in out.splitlines() if line.strip()]


def _norm(path: str) -> str:
    """Chemin posix relatif normalisé (séparateurs, préfixe `./`)."""
    s = str(path).replace("\\", "/").strip()
    return s[2:] if s.startswith("./") else s


def _classify(behind: int, tree_delta_empty: bool, delta_files: list[str],
              touched: list[str]) -> dict:
    """Cœur de décision — **pur** (ni git ni disque), donc testable hermétiquement."""
    norm_delta = [_norm(p) for p in delta_files]
    touched_set = {_norm(p) for p in touched}
    base = {
        "behind": behind,
        "tree_delta_empty": tree_delta_empty,
        "base_delta": norm_delta,
        "intersection": [],
    }
    if behind == 0:
        return {**base, "verdict": "fresh",
                "reason": "Base à jour : HEAD n'est pas en retard sur origin."}
    if tree_delta_empty:
        return {**base, "verdict": "waive_pure_merge",
                "reason": (f"En retard de {behind} commit(s) mais arbre identique "
                           f"(merge pur) : re-gate waivé, sanity build/test puis rebase.")}
    if not touched_set:
        return {**base, "verdict": "regate",
                "reason": ("Fichiers touchés inconnus : disjonction impossible à prouver, "
                           "re-gate par sécurité.")}
    inter = sorted(p for p in norm_delta if p in touched_set)
    if not inter:
        return {**base, "verdict": "waive_disjoint",
                "reason": ("Delta de base disjoint des fichiers touchés : re-gate "
                           "skippable (justifier avec base_delta), puis rebase.")}
    return {**base, "verdict": "regate", "intersection": inter,
            "reason": ("Delta de base intersecte les fichiers touchés : re-run "
                       "BUILD + gates sur la base à jour avant de livrer.")}


def analyze(touched: list[str], repo: str, explicit_default: str | None) -> dict:
    """Orchestre les appels git puis `_classify`. Toute erreur git → regate (sûr)."""
    default = _default_branch(repo, explicit_default)
    if not default:
        return {"verdict": "regate", "behind": None, "tree_delta_empty": None,
                "base_delta": [], "intersection": [],
                "reason": ("Branche par défaut d'origin introuvable : re-gate par "
                           "sécurité (préciser --default-branch).")}
    try:
        behind = _behind_count(default, repo)
        if behind == 0:
            return _classify(0, True, [], touched)
        empty = _tree_delta_empty(default, repo)
        delta = [] if empty else _delta_files(default, repo)
        return _classify(behind, empty, delta, touched)
    except (RuntimeError, ValueError) as exc:
        return {"verdict": "regate", "behind": None, "tree_delta_empty": None,
                "base_delta": [], "intersection": [],
                "reason": f"Erreur git ({exc}) : re-gate par sécurité."}


def main() -> int:
    args = sys.argv[1:]
    if "--self-test" in args:
        return _self_test()

    touched: list[str] = []
    default: str | None = None
    repo = "."
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--touched":
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                touched.append(args[i])
                i += 1
        elif a == "--default-branch":
            default = args[i + 1] if i + 1 < len(args) else None
            i += 2
        elif a == "--repo":
            repo = args[i + 1] if i + 1 < len(args) else "."
            i += 2
        else:
            i += 1

    print(json.dumps(analyze(touched, repo, default), ensure_ascii=False))
    return 0


def _self_test() -> int:
    # behind == 0 -> fresh (prime sur tout le reste)
    assert _classify(0, False, [], [])["verdict"] == "fresh"
    assert _classify(0, True, ["x"], ["x"])["verdict"] == "fresh"
    # en retard + arbre identique -> waive_pure_merge
    r = _classify(3, True, [], [])
    assert r["verdict"] == "waive_pure_merge" and r["behind"] == 3, r
    # en retard + arbre différent + delta disjoint -> waive_disjoint
    assert _classify(1, False, ["docs/x.md"], ["src/A.cs"])["verdict"] == "waive_disjoint"
    # en retard + arbre différent + intersection -> regate (avec l'intersection)
    r = _classify(1, False, ["src/A.cs", "docs/y.md"], ["src/A.cs"])
    assert r["verdict"] == "regate" and r["intersection"] == ["src/A.cs"], r
    # normalisation des chemins : backslash et préfixe ./ comparés correctement
    assert _classify(1, False, ["src\\A.cs"], ["src/A.cs"])["verdict"] == "regate"
    assert _classify(1, False, ["./src/A.cs"], ["src/A.cs"])["verdict"] == "regate"
    # fichiers touchés inconnus -> regate par sécurité (jamais de waiver aveugle)
    assert _classify(2, False, ["src/A.cs"], [])["verdict"] == "regate"
    # analyze : branche par défaut non résolvable -> regate (dégradation sûre)
    deg = analyze(["src/A.cs"], "/chemin/inexistant/xyz", None)
    assert deg["verdict"] == "regate", deg
    print("OK: base_freshness self-test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # accents FR sur une console cp1252
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
