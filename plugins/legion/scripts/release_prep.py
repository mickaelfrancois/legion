"""CHANGELOG et bump de version de legion (DELIVER §G étape 2), version exécutable et testable.

Avant le commit de livraison, le titre de PR (`<summary>`, Conventional Commits) décide
d'une entrée de CHANGELOG et d'un bump du manifeste de version. Trois calculs sont faciles
à rater à la main : parser le titre, faire le bump semver, insérer sous « Unreleased » sans
casser le fichier. Ce script les tranche de façon **déterministe** (même esprit que
`base_freshness.py`) :

- `feat` → minor ; `fix` et `perf` → patch ; `!` dans le titre ou pied `BREAKING CHANGE:`
  dans le corps (`--body-file`) → major, quel que soit le type.
- `docs`, `chore`, `test`, `refactor`, `build`, `ci` → `not_applicable`, sans note.
- type inconnu ou titre non conforme → aucune écriture et une note.

Le cœur (`_parse_title`, `_level`, `_bump`, `_find_version`, `_replace_version`,
`_insert_unreleased`, `_classify`) est **pur** : ni git ni disque, donc couvert de façon
hermétique par `--self-test`. Les E/S (git, fichiers) sont isolées. Aucun `git fetch` : le
script n'appelle jamais le réseau. La version est lue **à la base** (`git merge-base HEAD
origin/<défaut>`), pas dans l'arbre courant : `apply` est ainsi idempotent (reprise), et un
fichier déjà modifié par le diff (PR groupée, édition manuelle) est `foreign` : rien n'est
écrit, pour aucune des deux pièces.

Écriture : ensemble de chemins **fermé** (le `CHANGELOG.md` racine, jamais créé, et un seul
manifeste reconnu), jamais de lien symbolique ni de chemin hors `--repo`. Édition
**textuelle** : seule la ligne de version change, le reste reste identique à l'octet près.

Usage :
    python release_prep.py plan  [--repo <path>] --title "<summary>" [--body-file <f>] [--issue <n>] [--default-branch <name>]
    python release_prep.py apply  --repo <path>  --title "<summary>" [--body-file <f>] [--issue <n>] [--default-branch <name>]
    python release_prep.py --self-test

Sortie : un objet JSON sur stdout
    { type, level, changelog: {status, path, entry}, manifest: {status, path, old, new},
      written: [...], stage: [...], notes: [...] }
`written` = fichiers écrits par **cet** appel ; `stage` = fichiers `applied` ou
`already_applied` (ceux qui partent dans le commit) ; `notes` = lignes françaises pour
`pr-body.md`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")
TITLE_RE = re.compile(
    r"(?P<type>[A-Za-z]+)(?:\((?P<scope>[^()]+)\))?(?P<bang>!)?: (?P<subject>\S.*)"
)
BREAKING_RE = re.compile(r"^BREAKING[ -]CHANGE: ", re.MULTILINE)

TYPE_LEVEL = {"feat": "minor", "fix": "patch", "perf": "patch"}
NOOP_TYPES = ("docs", "chore", "test", "refactor", "build", "ci")
KNOWN_TYPES = tuple(TYPE_LEVEL) + NOOP_TYPES
SECTION_OF = {"feat": "Added", "fix": "Fixed", "perf": "Changed"}

_UNRELEASED_RE = re.compile(r"^##\s+\[?Unreleased\]?\s*$", re.IGNORECASE)
_VERSION_HEAD_RE = re.compile(r"^##\s+\[?v?\d")
_H2_RE = re.compile(r"^##\s")
_H23_RE = re.compile(r"^#{2,3}\s")
_SUBHEAD_RE = re.compile(r"^###\s+(.*?)\s*$")


# --------------------------------------------------------------------------- cœur pur


def _parse_title(title: str):
    """`(type, scope, bang, subject)` ou None (titre non conforme, multi-ligne, contrôle)."""
    if not isinstance(title, str):
        return None
    t = title.strip()
    if not t or any(ord(c) < 32 or ord(c) == 127 for c in t):
        return None
    m = TITLE_RE.fullmatch(t)
    if not m:
        return None
    return (m.group("type").lower(), m.group("scope"), bool(m.group("bang")),
            m.group("subject").strip())


def _level(type_: str, bang: bool, body: str = ""):
    """`major | minor | patch | None`. None = type inconnu ou sans effet (docs, chore…)."""
    if type_ not in KNOWN_TYPES:
        return None
    if bang or (body and BREAKING_RE.search(body)):
        return "major"
    return TYPE_LEVEL.get(type_)


def _bump(version: str, level: str) -> str:
    m = SEMVER.fullmatch(version)
    if not m or level not in ("major", "minor", "patch"):
        raise ValueError(f"bump impossible : {version!r} / {level!r}")
    major, minor, patch = (int(g) for g in m.groups())
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _norm(path: str) -> str:
    s = str(path).replace("\\", "/").strip()
    return s[2:] if s.startswith("./") else s


def _manifest_kind(path: str):
    """Famille de manifeste reconnue (liste fermée, C2) ou None."""
    p = _norm(path)
    name = p.rsplit("/", 1)[-1]
    if p == ".claude-plugin/plugin.json" or p.endswith("/.claude-plugin/plugin.json"):
        return "json"
    if "/" not in p:
        return {"package.json": "json", "pyproject.toml": "pyproject",
                "Cargo.toml": "cargo", "Directory.Build.props": "msbuild"}.get(name)
    if name.endswith(".csproj"):
        return "msbuild"
    return None


def _ws(c: str) -> bool:
    return c in " \t\r\n﻿"


def _locate_json(text: str):
    """Clé racine `"version"` : `(valeur, début, fin)` du contenu de la chaîne, ou None.
    Scanner à profondeur : une clé `version` imbriquée n'est jamais prise."""
    n, i, depth, last = len(text), 0, 0, ""
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                return None
            if depth == 1 and last in ("{", ","):
                k = j + 1
                while k < n and _ws(text[k]):
                    k += 1
                if k < n and text[k] == ":" and text[i + 1:j] == "version":
                    k += 1
                    while k < n and _ws(text[k]):
                        k += 1
                    if k < n and text[k] == '"':
                        e = k + 1
                        while e < n and text[e] != '"':
                            e += 2 if text[e] == "\\" else 1
                        if e >= n:
                            return None
                        return (text[k + 1:e], k + 1, e)
                    return None
            last, i = '"', j + 1
            continue
        if c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
        if not _ws(c):
            last = c
        i += 1
    return None


_TOML_SECTIONS = {"pyproject": ("project", "tool.poetry"), "cargo": ("package",)}
_TOML_HEAD = re.compile(r"\[\[?\s*([A-Za-z0-9_.\-\"' ]+?)\s*\]\]?\s*(#.*)?")
_TOML_VERSION = re.compile(r"(\s*version\s*=\s*)([\"'])([^\"'\r\n]*)\2")


def _locate_toml(kind: str, text: str):
    offset, section = 0, None
    for line in text.splitlines(keepends=True):
        s = line.strip()
        head = _TOML_HEAD.fullmatch(s)
        if head:
            section = head.group(1)
        elif section in _TOML_SECTIONS[kind]:
            v = _TOML_VERSION.match(line)
            if v:
                start = offset + len(v.group(1)) + 1
                return (v.group(3), start, start + len(v.group(3)))
        offset += len(line)
    return None


_MSBUILD_VERSION = re.compile(r"<Version>\s*([^<\s]*)\s*</Version>")


def _locate(kind: str, text: str):
    if kind == "json":
        return _locate_json(text)
    if kind in _TOML_SECTIONS:
        return _locate_toml(kind, text)
    if kind == "msbuild":
        m = _MSBUILD_VERSION.search(text)
        return (m.group(1), m.start(1), m.end(1)) if m else None
    return None


def _find_version(kind: str, text: str):
    """Version brute (str) du manifeste, ou None si absente (ex. `dynamic = ["version"]`)."""
    loc = _locate(kind, text)
    return loc[0] if loc else None


def _replace_version(kind: str, text: str, old: str, new: str) -> str:
    """Remplace uniquement la valeur de version ; tout le reste reste identique à l'octet."""
    loc = _locate(kind, text)
    if not loc or loc[0] != old:
        raise ValueError("version introuvable ou différente de l'attendue")
    return text[:loc[1]] + new + text[loc[2]:]


def _uses_subsections(changelog: str) -> bool:
    return any(_SUBHEAD_RE.match(line.rstrip("\r")) for line in changelog.split("\n"))


def _entry_line(subject: str, breaking: bool, issue) -> str:
    ref = f" (#{issue})" if issue is not None and re.fullmatch(r"\d+", str(issue)) else ""
    return f"- {'**BREAKING:** ' if breaking else ''}{subject}{ref}"


def _insert_block(lines, head, end, block, new_sub, cr=""):
    """Insère `block` après le dernier contenu de lines[head+1:end] (ou juste sous `head`)."""
    last = None
    for k in range(head + 1, end):
        if lines[k].strip():
            last = k
    if last is not None:
        ins = ([cr] + block) if new_sub else block
        return lines[:last + 1] + ins + lines[last + 1:]
    ins = [cr] + block
    if end == head + 1 and end < len(lines):
        ins = ins + [cr]
    return lines[:head + 1] + ins + lines[head + 1:]


def _insert_unreleased(changelog: str, entry_line: str, section=None) -> str:
    """Insère `entry_line` sous « Unreleased » (créée avant le premier titre de version si
    absente), après les entrées existantes. `section` : sous-section `### ` ou None.
    Seules les lignes ajoutées changent ; fins de ligne d'origine conservées."""
    crlf = "\r\n" in changelog
    cr = "\r" if crlf else ""
    lines = changelog.split("\n")
    entry = entry_line + cr
    sub_block = None if section is None else [f"### {section}" + cr, cr, entry]
    heads = [i for i, ln in enumerate(lines) if _UNRELEASED_RE.match(ln.rstrip("\r"))]
    if not heads:
        new = [f"## [Unreleased]{cr}", cr] + (sub_block or [entry]) + [cr]
        for i, ln in enumerate(lines):
            if _VERSION_HEAD_RE.match(ln.rstrip("\r")):
                return "\n".join(lines[:i] + new + lines[i:])
        if lines and lines[-1] == "":
            body, tail = lines[:-1], [""]
        else:
            body, tail = lines, []
        sep = [cr] if body and body[-1].strip() else []
        return "\n".join(body + sep + new[:-1] + tail)
    head = heads[0]
    end = len(lines)
    for i in range(head + 1, len(lines)):
        if _H2_RE.match(lines[i]):
            end = i
            break
    if section is None:
        return "\n".join(_insert_block(lines, head, end, [entry], False, cr))
    sub = None
    for i in range(head + 1, end):
        m = _SUBHEAD_RE.match(lines[i].rstrip("\r"))
        if m and m.group(1).lower() == section.lower():
            sub = i
            break
    if sub is None:
        return "\n".join(_insert_block(lines, head, end, sub_block, True, cr))
    sub_end = end
    for i in range(sub + 1, end):
        if _H23_RE.match(lines[i]):
            sub_end = i
            break
    return "\n".join(_insert_block(lines, sub, sub_end, [entry], False, cr))


def _classify(base_text: str, current_text: str, expected_text: str) -> str:
    """`base` (à écrire) | `applied` (déjà fait, reprise) | `foreign` (modifié ailleurs)."""
    if current_text == base_text:
        return "base"
    if current_text == expected_text:
        return "applied"
    return "foreign"


# --------------------------------------------------------------------------- E/S isolées


def _git(args, repo):
    """`git -C <repo> <args>` en octets. Retourne (returncode, stdout). 127 si git absent."""
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
        return proc.returncode, proc.stdout
    except OSError:
        return 127, b""


def _default_branch(repo, explicit):
    """Branche par défaut d'origin : explicite, sinon `origin/HEAD`, sinon main/master."""
    if explicit:
        return explicit
    rc, out = _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], repo)
    if rc == 0 and out.strip():
        return out.decode("utf-8", "replace").strip().rsplit("/", 1)[-1]
    for cand in ("main", "master"):
        rc, _ = _git(["rev-parse", "--verify", "--quiet", f"origin/{cand}"], repo)
        if rc == 0:
            return cand
    return None


def _merge_base(default, repo):
    rc, out = _git(["merge-base", "HEAD", f"origin/{default}"], repo)
    return out.decode("utf-8", "replace").strip() if rc == 0 and out.strip() else None


def _tracked(repo):
    rc, out = _git(["ls-files", "-z"], repo)
    if rc != 0:
        return None
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def _base_text(base, rel, repo):
    rc, out = _git(["show", f"{base}:{rel}"], repo)
    if rc != 0:
        return None
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_current(root: Path, rel: str):
    """(texte, erreur). Refuse lien symbolique et chemin résolu hors de `root`."""
    p = root / rel
    try:
        if p.is_symlink():
            return None, "lien symbolique"
        rp = p.resolve()
        if rp != root and root not in rp.parents:
            return None, "chemin hors du dépôt"
        return p.read_bytes().decode("utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, str(exc)


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    if p.is_symlink():
        raise OSError("lien symbolique")
    rp = p.resolve()
    if root not in rp.parents:
        raise OSError("chemin hors du dépôt")
    p.write_bytes(text.encode("utf-8"))


# --------------------------------------------------------------------------- orchestration


def _empty_result():
    return {"type": None, "level": None,
            "changelog": {"status": "absent", "path": None, "entry": None},
            "manifest": {"status": "absent", "path": None, "old": None, "new": None},
            "written": [], "stage": [], "notes": []}


def _set_both(res, status):
    res["changelog"]["status"] = status
    res["manifest"]["status"] = status


def _plan_changelog(root, repo, base, tracked, type_, level, subject, issue):
    """(pièce, état|None, nouveau texte|None). `état` = base | applied | foreign."""
    piece = {"status": "absent", "path": None, "entry": None}
    rel = "CHANGELOG.md"
    if rel not in tracked:
        return piece, None, None
    piece["path"] = rel
    cur, err = _read_current(root, rel)
    if cur is None:
        piece["status"] = "skipped_unparsable"
        return piece, None, f"CHANGELOG.md ignoré ({err})."
    base_text = _base_text(base, rel, repo)
    if base_text is None:
        piece["status"] = "skipped_foreign"
        return piece, "foreign", None
    entry = _entry_line(subject, level == "major", issue)
    section = (SECTION_OF.get(type_, "Changed")
               if _uses_subsections(base_text) else None)
    expected = _insert_unreleased(base_text, entry, section)
    piece["entry"] = entry
    return piece, _classify(base_text, cur, expected), expected


def _plan_manifest(root, repo, base, tracked, level):
    """(pièce, état|None, nouveau texte|None, note|None)."""
    piece = {"status": "absent", "path": None, "old": None, "new": None}
    cands, bad = [], []
    for rel in tracked:
        kind = _manifest_kind(rel)
        if not kind:
            continue
        text = _base_text(base, rel, repo)
        if text is None:
            continue
        ver = _find_version(kind, text)
        if ver is None:
            continue
        (cands if SEMVER.fullmatch(ver) else bad).append((rel, kind, text, ver))
    if not cands:
        if bad:
            piece["status"], piece["path"] = "skipped_unparsable", bad[0][0]
            return piece, None, None, (
                f"Bump ignoré : version non semver X.Y.Z dans {bad[0][0]} ({bad[0][3]}).")
        return piece, None, None, None
    if len(cands) > 1:
        paths = ", ".join(c[0] for c in cands)
        piece["status"] = "skipped_ambiguous"
        return piece, None, None, f"Bump ignoré : plusieurs manifestes candidats ({paths})."
    rel, kind, text, old = cands[0]
    new = _bump(old, level)
    piece.update(path=rel, old=old, new=new)
    cur, err = _read_current(root, rel)
    if cur is None:
        piece["status"] = "skipped_unparsable"
        return piece, None, None, f"Bump ignoré : {rel} illisible ({err})."
    expected = _replace_version(kind, text, old, new)
    return piece, _classify(text, cur, expected), expected, None


def compute(repo, title, body="", issue=None, explicit_default=None):
    """Calcule le plan. Retourne `(résultat, écritures_en_attente {chemin: texte})`.
    Ne touche pas au disque ; toute erreur git donne une sortie sans écriture + note."""
    res, pending = _empty_result(), {}
    parsed = _parse_title(title)
    if parsed is None:
        _set_both(res, "skipped_unparsable")
        res["notes"].append("CHANGELOG et bump ignorés : titre non conforme à "
                            "Conventional Commits (`type(scope): sujet` sur une ligne).")
        return res, pending
    type_, _scope, bang, subject = parsed
    res["type"] = type_
    if type_ not in KNOWN_TYPES:
        _set_both(res, "skipped_unparsable")
        res["notes"].append(f"CHANGELOG et bump ignorés : type inconnu `{type_}`.")
        return res, pending
    level = _level(type_, bang, body)
    if level is None:
        _set_both(res, "not_applicable")
        return res, pending
    res["level"] = level

    root = Path(repo).resolve()
    default = _default_branch(repo, explicit_default)
    base = _merge_base(default, repo) if default else None
    tracked = _tracked(repo) if base else None
    if base is None or tracked is None:
        _set_both(res, "skipped_unparsable")
        res["notes"].append("CHANGELOG et bump ignorés : base introuvable "
                            "(pas d'`origin/<défaut>` ou erreur git).")
        return res, pending

    cl_piece, cl_state, cl_new = _plan_changelog(
        root, repo, base, tracked, type_, level, subject, issue)
    mf_piece, mf_state, mf_new, mf_note = _plan_manifest(root, repo, base, tracked, level)
    if cl_state is None and cl_new:  # note d'erreur du changelog
        res["notes"].append(cl_new)
        cl_new = None
    if mf_note:
        res["notes"].append(mf_note)
    res["changelog"], res["manifest"] = cl_piece, mf_piece

    foreign = [p["path"] for p, s in ((cl_piece, cl_state), (mf_piece, mf_state))
               if s == "foreign"]
    for piece, state, new in ((cl_piece, cl_state, cl_new), (mf_piece, mf_state, mf_new)):
        if state is None:
            continue
        if state == "foreign":
            piece["status"] = "skipped_foreign"
        elif state == "applied":
            piece["status"] = "already_applied"
            res["stage"].append(piece["path"])
        elif foreign:
            piece["status"] = "skipped_foreign"
        else:
            piece["status"] = "applied"
            pending[piece["path"]] = new
            res["stage"].append(piece["path"])
    if foreign:
        res["notes"].append(
            f"CHANGELOG/bump non ajoutés : {', '.join(foreign)} déjà modifié(s) par le "
            "diff de la PR (PR groupée ou édition manuelle). À signaler dans le résumé.")
    if cl_piece["status"] in ("applied", "already_applied"):
        res["notes"].append(f"CHANGELOG.md : entrée ajoutée sous « Unreleased » : "
                            f"`{cl_piece['entry']}`.")
    if mf_piece["status"] in ("applied", "already_applied"):
        res["notes"].append(f"{mf_piece['path']} : version {mf_piece['old']} → "
                            f"{mf_piece['new']} (bump {level}).")
    return res, pending


def analyze(repo, title, body="", issue=None, explicit_default=None, write=False):
    res, pending = compute(repo, title, body, issue, explicit_default)
    if write:
        root = Path(repo).resolve()
        for rel, text in pending.items():
            try:
                _write(root, rel, text)
                res["written"].append(rel)
            except OSError as exc:
                res["stage"].remove(rel)
                res["notes"].append(f"Écriture refusée pour {rel} ({exc}) : non appliqué.")
    return res


def _read_body(path):
    if not path:
        return "", None
    try:
        return Path(path).read_bytes().decode("utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return "", f"--body-file illisible ({exc}) : pied BREAKING CHANGE non lu."


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--self-test" in argv:
        return _self_test()
    ap = argparse.ArgumentParser(description="CHANGELOG + bump de version (DELIVER §G).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply"):
        sp = sub.add_parser(name)
        sp.add_argument("--repo", required=(name == "apply"), default=".")
        sp.add_argument("--title", required=True)
        sp.add_argument("--body-file", default=None)
        sp.add_argument("--issue", default=None)
        sp.add_argument("--default-branch", default=None)
    args = ap.parse_args(argv)
    body, body_note = _read_body(args.body_file)
    res = analyze(args.repo, args.title, body, args.issue, args.default_branch,
                  write=(args.cmd == "apply"))
    if body_note:
        res["notes"].append(body_note)
    print(json.dumps(res, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- self-test


def _t_core() -> None:
    # parse
    assert _parse_title("feat(x): y") == ("feat", "x", False, "y")
    assert _parse_title("fix(x)!: y") == ("fix", "x", True, "y")
    assert _parse_title("feat!: y z") == ("feat", None, True, "y z")
    for bad in ("wip y", "feat:y", "feat: ", "", "feat: a\nb", "feat: a\x07b"):
        assert _parse_title(bad) is None, bad
    # niveau
    assert _level("feat", False) == "minor"
    assert _level("fix", False) == "patch" and _level("perf", False) == "patch"
    for t in ("feat", "fix", "chore"):
        assert _level(t, True) == "major", t
    assert _level("feat", False, "x\n\nBREAKING CHANGE: oups\n") == "major"
    assert _level("feat", False, "x\n\nBREAKING-CHANGE: oups\n") == "major"
    assert _level("feat", False, "mention BREAKING CHANGE: en milieu") == "minor"
    for t in NOOP_TYPES:
        assert _level(t, False) is None, t
    assert _level("wip", False) is None and _level("wip", True) is None
    # bump
    assert _bump("1.2.3", "minor") == "1.3.0"
    assert _bump("1.2.3", "patch") == "1.2.4"
    assert _bump("1.2.3", "major") == "2.0.0"
    assert _bump("0.13.2", "major") == "1.0.0"
    for v in ("1.2", "1.2.3-rc.1", "1.2.3\n"):
        try:
            _bump(v, "patch")
            raise AssertionError(v)
        except ValueError:
            pass
    # classification
    assert _classify("a", "a", "b") == "base"
    assert _classify("a", "b", "b") == "applied"
    assert _classify("a", "c", "b") == "foreign"


def _t_manifests() -> None:
    samples = {
        "json": '{\n  "name": "x",\n  "version": "1.2.3",\n  "d": {"version": "9.9.9"}\n}\n',
        "pyproject": '[build-system]\nrequires = ["x"]\n\n[project]\nname = "p"\n'
                     'version = "1.2.3"\n\n[tool.other]\nversion = "7.7.7"\n',
        "cargo": '[package]\nname = "c"\nversion = "1.2.3"\nedition = "2021"\n\n'
                 '[dependencies]\nserde = { version = "1.0.0" }\n\n'
                 '[dependencies.foo]\nversion = "2.0.0"\n',
        "msbuild": "<Project>\r\n  <PropertyGroup>\r\n    <Version>1.2.3</Version>\r\n"
                   "  </PropertyGroup>\r\n</Project>\r\n",
    }
    poetry = '[tool.poetry]\nname = "p"\nversion = "1.2.3"\r\n'
    for kind, text in list(samples.items()) + [("pyproject", poetry)]:
        assert _find_version(kind, text) == "1.2.3", kind
        out = _replace_version(kind, text, "1.2.3", "1.3.0")
        assert out != text and out.replace("1.3.0", "1.2.3") == text, kind
    # clé imbriquée avant la clé racine : seule la racine change
    nested = '{\n  "config": {"version": "9.9.9"},\n  "list": ["version", "x"],\n' \
             '  "version": "1.2.3"\n}\n'
    out = _replace_version("json", nested, "1.2.3", "1.3.0")
    data = json.loads(out)
    assert data["version"] == "1.3.0" and data["config"]["version"] == "9.9.9", data
    # BOM + chaîne contenant "version" comme valeur
    assert _find_version("json", '﻿{"x": "version", "version": "4.5.6"}') == "4.5.6"
    # dynamic = ["version"] ou pas de version : non candidat
    assert _find_version("pyproject", '[project]\nname = "p"\ndynamic = ["version"]\n') is None
    assert _find_version("json", '{"name": "x"}') is None
    assert _find_version("cargo", '[package]\nversion.workspace = true\n') is None
    # version non semver strict : lue brute (le plan la déclare unparsable)
    assert _find_version("json", '{"version": "1.2.3-rc.1"}') == "1.2.3-rc.1"
    assert _find_version("json", '{"version": "1.2"}') == "1.2"
    # reconnaissance des chemins (liste fermée)
    assert _manifest_kind("plugins/legion/.claude-plugin/plugin.json") == "json"
    assert _manifest_kind(".claude-plugin/plugin.json") == "json"
    assert _manifest_kind("package.json") == "json"
    assert _manifest_kind("sub/package.json") is None
    assert _manifest_kind("pyproject.toml") == "pyproject"
    assert _manifest_kind("Cargo.toml") == "cargo"
    assert _manifest_kind("Directory.Build.props") == "msbuild"
    assert _manifest_kind("src/App/App.csproj") == "msbuild"
    assert _manifest_kind("README.md") is None


def _t_changelog() -> None:
    e = _entry_line("new", False, "5")
    assert e == "- new (#5)"
    assert _entry_line("new", True, "5") == "- **BREAKING:** new (#5)"
    assert _entry_line("new", False, None) == "- new"
    assert _entry_line("new", False, "abc") == "- new"
    # `## [Unreleased]` puis `## Unreleased`
    for head in ("## [Unreleased]", "## Unreleased"):
        cl = f"# Changelog\n\n{head}\n\n- old one\n- old two\n\n## [1.0.0]\n\n- first\n"
        want = (f"# Changelog\n\n{head}\n\n- old one\n- old two\n- new (#5)\n\n"
                "## [1.0.0]\n\n- first\n")
        assert _insert_unreleased(cl, e) == want, head
    # sans section Unreleased : créée juste avant le premier titre de version
    cl = "# Changelog\n\n## [1.0.0]\n\n- first\n"
    assert _insert_unreleased(cl, e) == (
        "# Changelog\n\n## [Unreleased]\n\n- new (#5)\n\n## [1.0.0]\n\n- first\n")
    # Unreleased vide, en milieu et en fin de fichier
    assert _insert_unreleased("# C\n\n## Unreleased\n\n## 1.0.0\n- a\n", e) == (
        "# C\n\n## Unreleased\n\n- new (#5)\n\n## 1.0.0\n- a\n")
    assert _insert_unreleased("# C\n\n## Unreleased\n", e) == (
        "# C\n\n## Unreleased\n\n- new (#5)\n")
    assert _insert_unreleased("# C\n\n## Unreleased\n\n- a\n", e) == (
        "# C\n\n## Unreleased\n\n- a\n- new (#5)\n")
    # ni Unreleased ni version : ajout en fin
    assert _insert_unreleased("# C\n\ntexte\n", e) == (
        "# C\n\ntexte\n\n## [Unreleased]\n\n- new (#5)\n")
    # sous-sections Keep a Changelog
    kac = ("# Changelog\n\n## [Unreleased]\n\n### Added\n\n- a\n\n"
           "## [1.0.0]\n\n### Added\n\n- x\n")
    assert _uses_subsections(kac) and not _uses_subsections("## [U]\n\n- a\n")
    assert SECTION_OF == {"feat": "Added", "fix": "Fixed", "perf": "Changed"}
    assert _insert_unreleased(kac, e, "Added") == (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n\n- a\n- new (#5)\n\n"
        "## [1.0.0]\n\n### Added\n\n- x\n")
    assert _insert_unreleased(kac, e, "Fixed") == (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n\n- a\n\n### Fixed\n\n- new (#5)\n\n"
        "## [1.0.0]\n\n### Added\n\n- x\n")
    assert _insert_unreleased(kac, e, "Changed").count("### Changed") == 1
    # sous-section : Unreleased vide, et création complète de Unreleased
    assert _insert_unreleased("# C\n\n## [Unreleased]\n\n## [1.0.0]\n\n### Added\n\n- x\n",
                              e, "Added") == (
        "# C\n\n## [Unreleased]\n\n### Added\n\n- new (#5)\n\n"
        "## [1.0.0]\n\n### Added\n\n- x\n")
    assert _insert_unreleased("# C\n\n## [1.0.0]\n\n### Added\n\n- x\n", e, "Fixed") == (
        "# C\n\n## [Unreleased]\n\n### Fixed\n\n- new (#5)\n\n"
        "## [1.0.0]\n\n### Added\n\n- x\n")
    # CRLF conservé : seules les lignes ajoutées changent
    crlf = "# C\r\n\r\n## [Unreleased]\r\n\r\n- a\r\n\r\n## [1.0.0]\r\n\r\n- x\r\n"
    out = _insert_unreleased(crlf, e)
    assert out.replace(e + "\r\n", "") == crlf and e + "\r\n" in out, repr(out)
    assert "\n" not in out.replace("\r\n", "")
    out = _insert_unreleased(crlf, e, "Fixed")
    assert out.replace("### Fixed\r\n\r\n" + e + "\r\n\r\n", "") == crlf, repr(out)
    # préfixe BREAKING
    assert "**BREAKING:** new" in _insert_unreleased(
        crlf, _entry_line("new", True, None))


# -- intégration (dépôt git temporaire) --------------------------------------------------


def _g(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "-C", repo, "-c", "commit.gpgsign=false", *args],
                   check=True, capture_output=True, env=env)


def _put(repo, files):
    for rel, content in files.items():
        p = Path(repo) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)


def _mk_repo(tmp, name, files):
    repo = os.path.join(tmp, name)
    os.makedirs(repo)
    _g(repo, "init", "-q")
    _g(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _put(repo, files)
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "-m", "base")
    _g(repo, "update-ref", "refs/remotes/origin/main", "HEAD")  # origin simulé
    return repo


def _commit(repo, files, msg="pr"):
    _put(repo, files)
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "-m", msg)


def _rd(repo, rel):
    return (Path(repo) / rel).read_bytes().decode("utf-8")


_PLUGIN = '{\n  "name": "x",\n  "version": "1.2.3"\n}\n'
_PLUGIN_PATH = ".claude-plugin/plugin.json"
_CL = "# Changelog\n\n## [Unreleased]\n\n- old\n\n## [1.0.0]\n\n- first\n"


def _t_integration(tmp: str) -> None:
    both = {_PLUGIN_PATH: _PLUGIN, "CHANGELOG.md": _CL}
    # nominal + reprise idempotente
    r = _mk_repo(tmp, "nominal", both)
    plan = analyze(r, "feat(x): y", issue="7")
    assert plan["written"] == [] and _rd(r, "CHANGELOG.md") == _CL  # plan = lecture seule
    res = analyze(r, "feat(x): y", issue="7", write=True)
    assert res["level"] == "minor" and res["manifest"]["status"] == "applied", res
    assert res["manifest"]["old"] == "1.2.3" and res["manifest"]["new"] == "1.3.0"
    assert res["changelog"]["status"] == "applied" and res["changelog"]["entry"] == "- y (#7)"
    assert sorted(res["written"]) == sorted(["CHANGELOG.md", _PLUGIN_PATH]), res
    assert sorted(res["stage"]) == sorted(["CHANGELOG.md", _PLUGIN_PATH])
    assert _rd(r, _PLUGIN_PATH) == _PLUGIN.replace("1.2.3", "1.3.0")
    assert "- old\n- y (#7)\n" in _rd(r, "CHANGELOG.md")
    again = analyze(r, "feat(x): y", issue="7", write=True)
    assert again["written"] == [], again
    assert again["changelog"]["status"] == "already_applied"
    assert again["manifest"]["status"] == "already_applied"
    assert sorted(again["stage"]) == sorted(["CHANGELOG.md", _PLUGIN_PATH])
    assert not any("déjà modifié" in n for n in again["notes"]), again["notes"]
    assert _rd(r, _PLUGIN_PATH) == _PLUGIN.replace("1.2.3", "1.3.0")
    # reprise partielle : changelog appliqué, manifeste revenu à la base
    _put(r, {_PLUGIN_PATH: _PLUGIN})
    part = analyze(r, "feat(x): y", issue="7", write=True)
    assert part["written"] == [_PLUGIN_PATH], part
    assert part["changelog"]["status"] == "already_applied"
    assert sorted(part["stage"]) == sorted(["CHANGELOG.md", _PLUGIN_PATH])
    # fix / perf / breaking / pied BREAKING CHANGE / 0.x
    for title, body, want in (("fix: y", "", "1.2.4"), ("perf: y", "", "1.2.4"),
                              ("feat!: y", "", "2.0.0"), ("fix(x)!: y", "", "2.0.0"),
                              ("chore!: y", "", "2.0.0"),
                              ("feat: y", "z\n\nBREAKING CHANGE: a\n", "2.0.0")):
        rr = _mk_repo(tmp, "lvl-" + re.sub(r"\W", "", title + body), both)
        out = analyze(rr, title, body, write=True)
        assert out["manifest"]["new"] == want, (title, out)
        if want == "2.0.0":
            assert "- **BREAKING:** y" in _rd(rr, "CHANGELOG.md"), title
    r0 = _mk_repo(tmp, "zero", {_PLUGIN_PATH: _PLUGIN.replace("1.2.3", "0.13.2")})
    assert analyze(r0, "feat!: y", write=True)["manifest"]["new"] == "1.0.0"
    # types sans effet : octet près, rien de listé
    for t in NOOP_TYPES:
        rn = _mk_repo(tmp, "noop-" + t, both)
        out = analyze(rn, f"{t}: y", write=True)
        assert out["written"] == [] and out["stage"] == [] and out["notes"] == [], (t, out)
        assert out["changelog"]["status"] == out["manifest"]["status"] == "not_applicable"
        assert _rd(rn, "CHANGELOG.md") == _CL and _rd(rn, _PLUGIN_PATH) == _PLUGIN
    # type inconnu / titre non conforme : aucune écriture, une note
    for bad in ("wip: y", "pas conforme", "feat: a\nb"):
        rb = _mk_repo(tmp, "bad-" + str(abs(hash(bad))), both)
        out = analyze(rb, bad, write=True)
        assert out["written"] == [] and len(out["notes"]) == 1, (bad, out)
        assert _rd(rb, "CHANGELOG.md") == _CL and _rd(rb, _PLUGIN_PATH) == _PLUGIN
    # CHANGELOG seul / manifeste seul / aucun
    rc = _mk_repo(tmp, "cl-only", {"CHANGELOG.md": _CL})
    out = analyze(rc, "feat: y", write=True)
    assert out["manifest"]["status"] == "absent" and out["stage"] == ["CHANGELOG.md"], out
    rm = _mk_repo(tmp, "mf-only", {_PLUGIN_PATH: _PLUGIN})
    out = analyze(rm, "feat: y", write=True)
    assert out["changelog"]["status"] == "absent" and out["stage"] == [_PLUGIN_PATH], out
    assert not (Path(rm) / "CHANGELOG.md").exists()
    rz = _mk_repo(tmp, "none", {"a.txt": "x\n"})
    out = analyze(rz, "feat: y", write=True)
    assert out["written"] == [] and out["stage"] == [] and out["notes"] == [], out
    assert _rd(rz, "a.txt") == "x\n"
    # diff de la PR qui touche déjà CHANGELOG ou manifeste (= PR groupée) : foreign, rien écrit
    for label, touched in (("cl", {"CHANGELOG.md": _CL + "- pr\n"}),
                           ("mf", {_PLUGIN_PATH: _PLUGIN.replace("1.2.3", "1.2.4")})):
        rf = _mk_repo(tmp, "foreign-" + label, both)
        _commit(rf, touched)
        snap = {p: _rd(rf, p) for p in both}
        out = analyze(rf, "feat: y", write=True)
        assert out["written"] == [] and out["stage"] == [], (label, out)
        assert "skipped_foreign" in (out["changelog"]["status"], out["manifest"]["status"])
        assert out["changelog"]["status"] == out["manifest"]["status"] == "skipped_foreign"
        assert len(out["notes"]) == 1, out["notes"]
        assert {p: _rd(rf, p) for p in both} == snap
    # plusieurs manifestes candidats
    ra = _mk_repo(tmp, "ambig", {_PLUGIN_PATH: _PLUGIN, "package.json": '{"version": "2.0.0"}\n',
                                 "CHANGELOG.md": _CL})
    out = analyze(ra, "feat: y", write=True)
    assert out["manifest"]["status"] == "skipped_ambiguous" and out["notes"], out
    assert out["stage"] == ["CHANGELOG.md"] and _rd(ra, _PLUGIN_PATH) == _PLUGIN
    # version non semver strict
    rv = _mk_repo(tmp, "rc", {_PLUGIN_PATH: _PLUGIN.replace("1.2.3", "1.2.3-rc.1")})
    out = analyze(rv, "feat: y", write=True)
    assert out["manifest"]["status"] == "skipped_unparsable" and out["written"] == [], out
    # lien symbolique sur CHANGELOG : refus, cible intacte
    try:
        rs = os.path.join(tmp, "symlink")
        os.makedirs(rs)
        _g(rs, "init", "-q")
        _g(rs, "symbolic-ref", "HEAD", "refs/heads/main")
        _put(rs, {"real.md": _CL})
        os.symlink("real.md", os.path.join(rs, "CHANGELOG.md"))
    except (OSError, NotImplementedError, subprocess.CalledProcessError):
        pass
    else:
        _g(rs, "add", "-A")
        _g(rs, "commit", "-q", "-m", "base")
        _g(rs, "update-ref", "refs/remotes/origin/main", "HEAD")
        out = analyze(rs, "feat: y", write=True)
        assert out["written"] == [] and out["stage"] == [], out
        assert out["changelog"]["status"] == "skipped_unparsable", out
        assert _rd(rs, "real.md") == _CL
    # chemin résolu hors --repo, sans lien symbolique final : refus en lecture et en écriture
    ro = Path(_mk_repo(tmp, "outside", both)).resolve()
    Path(tmp, "outside-target.md").write_text("x", encoding="utf-8")
    txt, err = _read_current(ro, "../outside-target.md")
    assert txt is None and err == "chemin hors du dépôt", (txt, err)
    try:
        _write(ro, "../outside-target.md", "y")
    except OSError:
        pass
    else:
        raise AssertionError("_write a accepté un chemin hors du dépôt")
    assert Path(tmp, "outside-target.md").read_text(encoding="utf-8") == "x"
    # erreur git : pas d'origin/<défaut>
    rg = _mk_repo(tmp, "no-origin", both)
    _g(rg, "update-ref", "-d", "refs/remotes/origin/main")
    out = analyze(rg, "feat: y", write=True)
    assert out["written"] == [] and len(out["notes"]) == 1, out
    assert _rd(rg, "CHANGELOG.md") == _CL
    assert analyze(os.path.join(tmp, "inexistant"), "feat: y", write=True)["written"] == []


def _t_cli(tmp: str) -> None:
    me = os.path.abspath(__file__)
    py = sys.executable
    # apply sans --repo : erreur d'usage
    p = subprocess.run([py, me, "apply", "--title", "feat: y"], capture_output=True, text=True)
    assert p.returncode != 0, p
    # bout en bout, avec --body-file portant un pied BREAKING CHANGE
    r = _mk_repo(tmp, "cli", {_PLUGIN_PATH: _PLUGIN, "CHANGELOG.md": _CL})
    body = os.path.join(tmp, "body.txt")
    Path(body).write_text("corps\n\nBREAKING CHANGE: x\n", encoding="utf-8")
    p = subprocess.run([py, me, "plan", "--repo", r, "--title", "feat: y",
                        "--body-file", body], capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0, p
    out = json.loads(p.stdout)
    assert out["level"] == "major" and out["written"] == [], out
    assert _rd(r, _PLUGIN_PATH) == _PLUGIN
    p = subprocess.run([py, me, "apply", "--repo", r, "--title", "feat: y", "--issue", "9",
                        "--body-file", body], capture_output=True, text=True, encoding="utf-8")
    out = json.loads(p.stdout)
    assert p.returncode == 0 and len(out["written"]) == 2, out
    assert _rd(r, _PLUGIN_PATH) == _PLUGIN.replace("1.2.3", "2.0.0")


def _t_doc_deliver_release() -> None:
    """Doctrine §G étape 2 : l'appel release_prep, stage, notes, ordre."""
    plugin = Path(__file__).resolve().parents[1]
    battle_md = plugin / "commands" / "battle.md"
    deliver_md = plugin / "doctrine" / "battle" / "G-deliver.md"
    if not (battle_md.is_file() and deliver_md.is_file()):
        print("SKIP: _t_doc_deliver_release (doctrine /battle absente, cache de plugin ?)",
              file=sys.stderr)
        return
    # GH#212 : doctrine = noyau + parties ; l'ancien paragraphe ne doit subsister nulle part,
    # la coupe §G cible la partie G-deliver.
    parts = sorted((plugin / "doctrine" / "battle").glob("*.md"))
    battle = "\n".join([battle_md.read_text(encoding="utf-8")] + [f.read_text(encoding="utf-8") for f in parts])
    deliver = deliver_md.read_text(encoding="utf-8")
    g = deliver[deliver.index("2. **Commit**"):deliver.index("3. **Compose the PR body**")]
    call = 'release_prep.py" apply --repo "<worktree>"'
    for needle in (call, "`stage[]`", "`notes[]`", "does **not** re-run `release_prep`"):
        assert needle in g, f"§G étape 2 : aiguille absente : {needle}"
    i_sum = g.index("**`<summary>` — English")
    i_call = g.index(call)
    i_before = g.index("**Before committing**")
    assert i_sum < i_call < i_before, "§G étape 2 : ordre summary < release_prep < Before committing"
    assert "Version bump grouped in the PR" not in battle, "ancien paragraphe de bump encore présent"
    step3 = deliver[deliver.index("3. **Compose the PR body**"):deliver.index("4. **CONFIRM")]
    assert "notes[]" in step3, "§G étape 3 : notes[] absent"
    assert " -I " not in g, "release_prep appelé avec python -I"


def _self_test() -> int:
    _t_core()
    _t_manifests()
    _t_changelog()
    _t_doc_deliver_release()
    rc, _ = _git(["--version"], ".")
    if rc != 0:
        print("SKIP: git absent, tests d'intégration ignorés", file=sys.stderr)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            _t_integration(tmp)
            _t_cli(tmp)
    print("OK: release_prep self-test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # accents FR sur une console cp1252
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
