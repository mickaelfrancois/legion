"""Hook PostToolUse (legion) : lie la session Claude a la battle qu'elle vient d'initialiser
ou d'activer (GH#170).

Declencheur : une commande Bash/PowerShell qui appelle `battle_state.py init <id>` ou
`battle_state.py activate <id>`. Le hook decoupe la commande (shlex), passe les mots qui suivent
`battle_state.py` au vrai parser (`_build_parser`) et ne lie que `init` / `activate`.
`--repo` est respecte (sinon : depot principal du `cwd`).

Preuve de succes : `tool_response.stdout` est un JSON `{"ok": true, "battle": id}`. A defaut
de stdout exploitable, le pointeur `active-battle` doit valoir `id` et la battle etre vivante.
Puis `bind_session(racine, cle principale, id)` ecrit `.legion/sessions/<cle>.json`.

Sonde opt-in (`LEGION_HOOK_PROBE=1` ou fichier `.legion/hook-probe`) : `probe()` note les NOMS
de cles du payload (jamais de valeurs).

Ne bloque jamais (exit 0) et ne leve jamais d'exception.

Tests CLI :
    py session_bind.py --self-test
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_IMPORT_ERROR: str | None = None
try:
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    import battle_state as _bs
except Exception as _exc:  # jamais planter a l'import : le hook se desactive, le self-test echoue
    _IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

SHELL_TOOLS = ("Bash", "PowerShell")
BIND_COMMANDS = ("init", "activate")
_PUNCT = set(";&|<>()")


def _words(command: str) -> list[str] | None:
    """Mots de `command` (operateurs shell = jetons separes), `None` si non analysable."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""  # `GH#1` n'est pas un commentaire
        return list(lex)
    except ValueError:
        return None


def _battle_state_calls(command: str) -> list[list[str]]:
    """Pour chaque appel `battle_state.py` : mots qui le suivent jusqu'au prochain operateur shell."""
    words = _words(command)
    if not words:
        return []
    calls: list[list[str]] = []
    i = 0
    while i < len(words):
        if os.path.basename(words[i].replace("\\", "/")) == "battle_state.py":
            j = i + 1
            while j < len(words) and not (words[j] and set(words[j]) <= _PUNCT):
                j += 1
            calls.append(words[i + 1:j])
            i = j
        else:
            i += 1
    return calls


def _parse(argv: list[str]):
    """Namespace du vrai parser (`init` / `activate` seulement), sinon `None`. Silencieux."""
    sink = io.StringIO()
    try:
        with contextlib.redirect_stderr(sink), contextlib.redirect_stdout(sink):
            ns, _rest = _bs._build_parser().parse_known_args(argv)
    except (Exception, SystemExit):  # noqa: BLE001 - appel malforme (_Refuse, argparse) : ignore
        return None
    if getattr(ns, "cmd", None) not in BIND_COMMANDS or not isinstance(getattr(ns, "id", None), str):
        return None
    return ns


def _stdout_of(data: dict) -> str | None:
    resp = data.get("tool_response")
    if isinstance(resp, dict):
        out = resp.get("stdout")
        return out if isinstance(out, str) and out.strip() else None
    if isinstance(resp, str) and resp.strip():
        return resp
    return None


def _json_results(out: str) -> list[dict]:
    """Objets JSON de stdout : le texte entier, sinon une ligne = un objet (commandes enchainees)."""
    try:
        res = json.loads(out.strip())
        return [res] if isinstance(res, dict) else []
    except (ValueError, RecursionError):
        pass
    found: list[dict] = []
    for line in out.splitlines():
        try:
            res = json.loads(line.strip())
        except (ValueError, RecursionError):
            continue
        if isinstance(res, dict):
            found.append(res)
    return found


def _proofs(data: dict, root_of: list[Path], ids: list[str]) -> list[bool]:
    """Succes de chaque appel `init`/`activate` (dans l'ordre).

    Stdout avec JSON : seules les lignes portant `previous_active` (propre a init/activate) comptent ;
    la N-ieme ligne prouve le N-ieme appel (`ok: true` et `battle == id`). Nombres differents :
    ambigu, rien n'est lie. Stdout sans JSON : pointeur == id vivant (repli).
    """
    out = _stdout_of(data)
    results = _json_results(out) if out is not None else []
    if results:
        lines = [r for r in results if "previous_active" in r]
        if len(lines) != len(ids):
            return [False] * len(ids)
        return [r.get("ok") is True and r.get("battle") == bid for r, bid in zip(lines, ids)]
    proofs = []
    for root, bid in zip(root_of, ids):
        active = _bs.load_active_battle(root)
        proofs.append(active is not None and active[0] == bid and _bs._is_live(active[1]))
    return proofs


def _handle(data) -> None:
    """Lie la session si le payload decrit un `init` / `activate` reussi. Ne leve jamais."""
    try:
        if _IMPORT_ERROR is not None or not isinstance(data, dict):
            return
        cwd = Path(str(data.get("cwd") or os.getcwd()))
        # Sonde (noms de cles seulement) : racine d'etat du cwd.
        _bs.probe(data, _bs.main_repo_root(cwd))
        if data.get("tool_name") not in SHELL_TOOLS:
            return
        inp = data.get("tool_input")
        command = inp.get("command") if isinstance(inp, dict) else None
        if not isinstance(command, str):
            return
        calls = _battle_state_calls(command)
        if not calls:
            return
        keys = _bs.session_keys(data)
        if not keys:
            return
        roots: list[Path] = []
        ids: list[str] = []
        for argv in calls:  # appels init/activate valides, dans l'ordre
            ns = _parse(argv)
            if ns is None:
                continue
            repo = getattr(ns, "repo", None)
            roots.append(Path(repo) if repo else _bs.main_repo_root(cwd))
            ids.append(ns.id)
        # liaison finale = dernier appel reussi (une session = une battle)
        for root, bid, ok in zip(roots, ids, _proofs(data, roots, ids)):
            if ok:
                _bs.bind_session(root, keys[0], bid)
    except Exception:  # noqa: BLE001 - contrat : un hook ne plante jamais la session
        return


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()
    if _IMPORT_ERROR is not None:
        print(f"[session_bind] desactive : installation legion incomplete ({_IMPORT_ERROR}).", file=sys.stderr)
        return 0
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (ValueError, RecursionError):
        return 0
    _handle(data)
    return 0


# --- Self-test --------------------------------------------------------------------------

def _self_test() -> int:
    if _IMPORT_ERROR is not None:
        print(f"FAIL: import battle_state : {_IMPORT_ERROR}", file=sys.stderr)
        return 1
    tests = [_t_init, _t_activate_compound, _t_failure_proofs, _t_per_call, _t_chained, _t_repo, _t_nothing, _t_probe, _t_hooks_json]
    for t in tests:
        t()
    print(f"OK: {len(tests)} tests session_bind")
    return 0


def _mk(tmp: str) -> Path:
    root = Path(tmp) / "repo"
    root.mkdir()
    return root


def _init(root: Path, bid: str) -> None:
    code, res = _bs.run_command(["--repo", str(root), "init", bid, "--ticket", "GH#1", "--title", "T",
                                 "--profile", "feature"], lambda *a: None, Path(root).parent / "fleet.d")
    assert code == 0 and res["ok"], res


def _payload(command: str, cwd: Path, sid: str | None = "sid-1", stdout: str | None = None, **extra) -> dict:
    p: dict = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": str(cwd),
               "tool_input": {"command": command}}
    if sid is not None:
        p["session_id"] = sid
    if stdout is not None:
        p["tool_response"] = {"stdout": stdout}
    p.update(extra)
    return p


def _binding(root: Path, key: str):
    f = root / ".legion" / "sessions" / f"{key}.json"
    return json.loads(f.read_text(encoding="utf-8"))["battle"] if f.is_file() else None


def _ok(bid: str) -> str:
    return json.dumps({"ok": True, "battle": bid, "previous_active": None})


def _t_init() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _mk(tmp)
        _init(root, "A")
        cmd = f'python3 "/x/scripts/battle_state.py" init A --ticket GH#1 --title T --profile feature --repo "{root}"'
        _handle(_payload(cmd, root, stdout=_ok("A")))
        assert _binding(root, "sid-1") == "A"


def _t_activate_compound() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _mk(tmp)
        _init(root, "A")
        cmd = f'cd x && python "/x/scripts/battle_state.py" activate A --repo "{root}"'
        _handle(_payload(cmd, root, stdout=_ok("A")))
        assert _binding(root, "sid-1") == "A"
        # une autre session active B : la liaison de A reste, B a la sienne
        _init(root, "B")
        _handle(_payload(f'python battle_state.py activate B --repo "{root}"', root, sid="sid-2", stdout=_ok("B")))
        assert _binding(root, "sid-2") == "B" and _binding(root, "sid-1") == "A"


def _t_failure_proofs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _mk(tmp)
        _init(root, "A")
        _init(root, "B")  # pointeur = B
        cmd = f'python battle_state.py activate A --repo "{root}"'
        _handle(_payload(cmd, root, stdout=json.dumps({"ok": False, "battle": "A"})))
        assert _binding(root, "sid-1") is None                       # ok:false
        _handle(_payload(cmd, root))                                 # stdout absent, pointeur = B
        assert _binding(root, "sid-1") is None
        _handle(_payload(f'python battle_state.py activate B --repo "{root}"', root))  # pointeur = B
        assert _binding(root, "sid-1") == "B"                        # repli sur le pointeur


def _t_per_call() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _mk(tmp)
        _init(root, "A")
        # activate A echoue, puis set-guard --battle A reussit : A n'est PAS lie
        cmd = (f'python battle_state.py activate A --repo "{root}" ; '
               f'python battle_state.py set-guard --battle A --repo "{root}"')
        out = json.dumps({"ok": False, "battle": "A"}) + "\n" + json.dumps({"ok": True, "battle": "A"})
        _handle(_payload(cmd, root, stdout=out))
        assert _binding(root, "sid-1") is None
        # appel malforme puis activate B valide : B est lie
        _init(root, "B")
        cmd = (f'python battle_state.py init --nope ; python battle_state.py activate B --repo "{root}"')
        _handle(_payload(cmd, root, stdout=_ok("B")))
        assert _binding(root, "sid-1") == "B"


def _t_chained() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _mk(tmp)
        _init(root, "A")
        _init(root, "B")
        # lecture puis activation : la liaison suit l'activation (GH#177)
        cmd = f'python battle_state.py validate --battle A --repo "{root}" && python battle_state.py activate B --repo "{root}"'
        _handle(_payload(cmd, root, stdout=_ok("B")))
        assert _binding(root, "sid-1") == "B"
        # init puis activate : deux lignes JSON ok, liaison finale = B (une session = une battle)
        cmd = (f'python battle_state.py init A --ticket t --title t --profile feature --repo "{root}" && '
               f'python battle_state.py activate B --repo "{root}"')
        _handle(_payload(cmd, root, sid="sid-2", stdout=_ok("A") + "\n" + _ok("B")))
        assert _binding(root, "sid-2") == "B"


def _t_repo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _mk(tmp)
        other = Path(tmp) / "elsewhere"
        other.mkdir()
        _init(root, "A")
        _handle(_payload(f'python battle_state.py activate A --repo "{root}"', other, stdout=_ok("A")))
        assert _binding(root, "sid-1") == "A"
        assert not (other / ".legion").exists()


def _t_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _mk(tmp)
        _init(root, "A")
        cmd = f'python battle_state.py activate A --repo "{root}"'
        _handle(_payload(cmd, root, sid=None, stdout=_ok("A")))               # sans cle
        _handle(_payload(f'python battle_state.py activate "A --repo {root}', root, stdout=_ok("A")))  # non analysable
        _handle(_payload(f'python battle_state.py validate --repo "{root}"', root, stdout=_ok("A")))   # autre sous-commande
        _handle(_payload(cmd.replace("activate", "close"), root, stdout=_ok("A")))
        _handle(_payload("ls -la", root, stdout=_ok("A")))
        _handle(dict(_payload(cmd, root, stdout=_ok("A")), tool_name="Edit"))
        _handle("pas un dict")
        _handle({})
        sdir = root / ".legion" / "sessions"
        assert not sdir.exists() or not list(sdir.glob("*.json"))


def _t_probe() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _mk(tmp)
        (root / ".legion").mkdir()
        (root / ".legion" / "hook-probe").write_text("", encoding="utf-8")
        _handle(_payload("ls", root, sid="SECRET-SID-42", agent_type="x:y"))
        f = root / ".legion" / "hook-probe.jsonl"
        assert f.is_file()
        text = f.read_text(encoding="utf-8")
        assert "SECRET-SID-42" not in text and "session_id" in text, text


def _t_hooks_json() -> None:
    hooks = json.loads((Path(__file__).resolve().parent / "hooks.json").read_text(encoding="utf-8"))
    entry = [e for e in hooks["hooks"]["PostToolUse"] if e["matcher"] == "Bash|PowerShell"]
    assert len(entry) == 1, entry
    assert any("hooks/session_bind.py" in h["command"] for h in entry[0]["hooks"]), entry


if __name__ == "__main__":
    sys.exit(main())
