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
    python artifact_check.py --self-test

Sortie : un objet JSON sur stdout
    snapshot → { exists, mtime_ns, size }
    verify   → { ok, checks: { exists, non_empty, canonical, fresh }, reason }
Codes de sortie : 0 = ok ; 2 = `verify` refusé (`ok:false`) ; 1 = erreur d'usage.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile


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


def _usage(msg: str) -> int:
    print(f"usage invalide : {msg}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in args:
        return _self_test()
    if not args or args[0] not in ("snapshot", "verify"):
        return _usage("sous-commande attendue : snapshot | verify | --self-test")
    sub, rest = args[0], args[1:]
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


_TESTS = (_t_absent, _t_empty, _t_not_canonical, _t_relative_equals_absolute,
          _t_stale_same_mtime, _t_fresh_newer_mtime, _t_fresh_created,
          _t_snapshot_shape, _t_cli_exit_codes)


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
