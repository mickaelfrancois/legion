"""Mesure du contexte de la session et fraîcheur des notes de reprise (GH#212), lecture seule.

L'orchestrateur d'une battle propose un reset de contexte aux transitions de phase quand le
contexte de sa session dépasse un seuil. Ce script fournit les deux faits dont il a besoin. Il
n'écrit **jamais** rien (ni `battle.json`, ni `.legion/`), n'utilise **jamais** le réseau et rend
un seul objet JSON sur stdout. La doctrine (`commands/battle.md`, « Reset de contexte ») décide
quoi faire de ce JSON.

Sous-commandes (le dépôt principal est déduit du cwd, ou donné par `--repo`) :

- `measure --battle <id> [--repo <path>]` : taille du contexte de la session liée à la battle.
  Taille = `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` du **dernier**
  enregistrement assistant du fil principal ayant un `message.usage` non nul (jamais la somme des
  tours : ce serait un coût cumulé). Sont ignorés : `isSidechain: true`, `message.model ==
  "<synthetic>"`, usage tout à zéro. Le transcript est celui que `session_bind` a enregistré dans
  `.legion/sessions/<clé>.json` (champ `transcript`) ; seule la queue du fichier est lue.
  Sortie : `{ok, battle, measurable, context_tokens|null, threshold, propose, reason, header|null}`.
  `propose` est vrai si et seulement si `measurable`, `context_tokens > RESET_THRESHOLD`
  (strictement) et qu'aucune phase ni slice n'est `in_progress` (jamais au milieu d'une slice).
  `reason` : `ok`, `below_threshold`, `phase_in_progress`, `slice_in_progress`, `no_binding`,
  `no_transcript`, `transcript_mismatch`, `transcript_unreadable`, `no_usage`, `invalid_battle`,
  `battle_unreadable`. `header` (seulement si `propose`) : en-tête à recopier tel quel en tête de
  `resume-notes.md`. Exit 0 pour tout argument valide, mesure impossible comprise.
- `notes --battle <id> [--repo <path>]` : fraîcheur de `<battle>/resume-notes.md`.
  Sortie : `{ok, battle, present, fresh, reason, bytes, path}`. `reason` : `fresh`, `absent`,
  `stale`, `no_header`, `other_battle`, `unreadable`, `invalid_battle`, `battle_unreadable`.
  `fresh` si et seulement si l'en-tête se lit, que `battle` correspond et que sa ligne `phases:`
  est identique à celle recalculée depuis `battle.json` : toute avancée de phase périme les notes.

Usage :
    python context_check.py measure --battle <id> [--repo <path>]
    python context_check.py notes --battle <id> [--repo <path>]
    python context_check.py --self-test

Codes de sortie : 0 = réponse JSON (même si la mesure est impossible) ; 1 = erreur d'usage.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import battle_state as bs  # noqa: E402

RESET_THRESHOLD = 120_000
TAIL_BYTES = 8 * 1024 * 1024
NOTES_NAME = "resume-notes.md"
_USAGE_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


# --- Lecture du transcript ------------------------------------------------------------------

def _record_context(rec) -> int | None:
    """Taille de contexte d'un enregistrement de transcript, `None` s'il ne compte pas."""
    if not isinstance(rec, dict) or rec.get("isSidechain") is True:
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict) or msg.get("model") == "<synthetic>":
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    total = 0
    for field in _USAGE_FIELDS:
        v = usage.get(field)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            total += v
    return total or None


def last_context_tokens(path: str, tail_bytes: int = TAIL_BYTES) -> tuple[int | None, str]:
    """`(taille, raison)` : `(n, "ok")`, `(None, "transcript_unreadable")` si le fichier ne se lit
    pas, `(None, "no_usage")` si la queue ne contient aucun usage exploitable. Ne lève jamais."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            start = max(0, size - tail_bytes)
            fh.seek(start)
            raw = fh.read()
    except (OSError, ValueError):
        return None, "transcript_unreadable"
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # la première ligne de la fenêtre peut être partielle
    for line in reversed(lines):
        try:
            tokens = _record_context(json.loads(line))
        except (ValueError, RecursionError):
            continue
        if tokens is not None:
            return tokens, "ok"
    return None, "no_usage"


# --- État de la battle ----------------------------------------------------------------------

def _root(cwd: str, repo: str | None) -> Path:
    return Path(repo) if repo else bs.main_repo_root(Path(cwd))


def _read_battle(root: Path, battle_id: str) -> tuple[dict | None, str | None]:
    """`(battle.json, None)` ou `(None, raison)`."""
    if not isinstance(battle_id, str) or not bs._ID_RE.fullmatch(battle_id):
        return None, "invalid_battle"
    try:
        data = json.loads((bs.battles_dir(root) / battle_id / "battle.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None, "battle_unreadable"
    return (data, None) if isinstance(data, dict) else (None, "battle_unreadable")


def _phases_line(battle: dict) -> str:
    """`k=statut` des phases, dans l'ordre des clés de `battle.json`."""
    phases = battle.get("phases")
    if not isinstance(phases, dict):
        return ""
    parts = []
    for key, val in phases.items():
        status = val.get("status") if isinstance(val, dict) else None
        parts.append(f"{key}={status if isinstance(status, str) else 'pending'}")
    return " ".join(parts)


def _in_progress(battle: dict) -> str | None:
    """`phase_in_progress` / `slice_in_progress` si un travail est en cours, sinon `None`."""
    phases = battle.get("phases")
    if isinstance(phases, dict) and any(isinstance(v, dict) and v.get("status") == "in_progress"
                                        for v in phases.values()):
        return "phase_in_progress"
    slices = battle.get("slices")
    if isinstance(slices, list) and any(isinstance(s, dict) and s.get("status") == "in_progress"
                                        for s in slices):
        return "slice_in_progress"
    return None


def _bound_transcript(root: Path, battle_id: str) -> tuple[str | None, str | None, str]:
    """`(clé, transcript, raison)` de la liaison la plus récente vers la battle ; `raison` vaut
    `ok`, `no_binding` ou `no_transcript`."""
    best = None
    for f, bid, data in bs._list_bindings(root):
        if bid != battle_id or not isinstance(data, dict):
            continue
        stamp = data.get("bound_at") if isinstance(data.get("bound_at"), str) else ""
        if best is None or stamp > best[0]:
            best = (stamp, f.stem, data)
    if best is None:
        return None, None, "no_binding"
    transcript = best[2].get("transcript")
    if not isinstance(transcript, str) or not transcript:
        return best[1], None, "no_transcript"
    return best[1], transcript, "ok"


# --- Sous-commandes -------------------------------------------------------------------------

def _measure_result(battle_id, measurable, tokens, propose, reason, header=None) -> dict:
    return {"ok": True, "battle": battle_id, "measurable": measurable, "context_tokens": tokens,
            "threshold": RESET_THRESHOLD, "propose": propose, "reason": reason, "header": header}


def measure(cwd: str, battle_id: str, repo: str | None = None, tail_bytes: int = TAIL_BYTES) -> dict:
    """Mesure du contexte et décision de proposer un reset. Ne lève jamais."""
    try:
        root = _root(cwd, repo)
        battle, why = _read_battle(root, battle_id)
        if battle is None:
            return _measure_result(battle_id, False, None, False, why)
        key, transcript, why = _bound_transcript(root, battle_id)
        if transcript is None:
            return _measure_result(battle_id, False, None, False, why)
        if bs._transcript_kind_key(transcript) != ("main", key):
            return _measure_result(battle_id, False, None, False, "transcript_mismatch")
        tokens, why = last_context_tokens(transcript, tail_bytes)
        if tokens is None:
            return _measure_result(battle_id, False, None, False, why)
        busy = _in_progress(battle)
        if busy is not None:
            return _measure_result(battle_id, True, tokens, False, busy)
        if tokens <= RESET_THRESHOLD:
            return _measure_result(battle_id, True, tokens, False, "below_threshold")
        header = (f"---\nbattle: {battle_id}\nwritten_at: {bs._now_iso()}\n"
                  f"context_tokens: {tokens}\nphases: {_phases_line(battle)}\n---\n")
        return _measure_result(battle_id, True, tokens, True, "ok", header)
    except Exception:  # noqa: BLE001 - contrat : jamais d'exception, jamais de proposition
        return _measure_result(battle_id, False, None, False, "battle_unreadable")


def _notes_result(battle_id, present, fresh, reason, size=0, path=None, ok=True) -> dict:
    return {"ok": ok, "battle": battle_id, "present": present, "fresh": fresh, "reason": reason,
            "bytes": size, "path": path}


def _parse_header(text: str) -> dict | None:
    """Champs `clé: valeur` du bloc `---` en tête de `text`, `None` si aucun bloc fermé."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fields: dict = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        key, sep, val = line.partition(":")
        if not sep or not key.strip():
            return {}
        fields[key.strip()] = val.strip()
    return {}  # bloc jamais refermé


def notes(cwd: str, battle_id: str, repo: str | None = None) -> dict:
    """Fraîcheur de `resume-notes.md`. Ne lève jamais."""
    try:
        root = _root(cwd, repo)
        battle, why = _read_battle(root, battle_id)
        if battle is None:
            return _notes_result(battle_id, False, False, why, ok=False)
        path = bs.battles_dir(root) / battle_id / NOTES_NAME
        spath = str(path)
        if not path.is_file():
            return _notes_result(battle_id, False, False, "absent", path=spath)
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return _notes_result(battle_id, True, False, "unreadable", path=spath)
        size = len(raw)
        head = _parse_header(text)
        if head is None:
            return _notes_result(battle_id, True, False, "no_header", size, spath)
        if "battle" not in head or "phases" not in head:
            return _notes_result(battle_id, True, False, "unreadable", size, spath)
        if head["battle"] != battle_id:
            return _notes_result(battle_id, True, False, "other_battle", size, spath)
        if head["phases"] != _phases_line(battle):
            return _notes_result(battle_id, True, False, "stale", size, spath)
        return _notes_result(battle_id, True, True, "fresh", size, spath)
    except Exception:  # noqa: BLE001
        return _notes_result(battle_id, False, False, "battle_unreadable", ok=False)


# --- CLI ------------------------------------------------------------------------------------

def _usage(msg: str) -> int:
    print(json.dumps({"ok": False, "usage": msg}, ensure_ascii=False), file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in args:
        return _self_test()
    if not args or args[0] not in ("measure", "notes"):
        return _usage("sous-commande attendue : measure | notes | --self-test")
    cmd, rest = args[0], args[1:]
    battle_id = repo = None
    i = 0
    while i < len(rest):
        if rest[i] == "--battle" and i + 1 < len(rest) and battle_id is None:
            battle_id = rest[i + 1]
        elif rest[i] == "--repo" and i + 1 < len(rest) and repo is None:
            repo = rest[i + 1]
        else:
            return _usage(f"{cmd} --battle <id> [--repo <path>]")
        i += 2
    if battle_id is None:
        return _usage(f"{cmd} --battle <id> [--repo <path>]")
    fn = measure if cmd == "measure" else notes
    print(json.dumps(fn(os.getcwd(), battle_id, repo), ensure_ascii=False))
    return 0


# --- Self-test : dépôts jetables, hors ligne ------------------------------------------------

def _line(inp=0, cr=0, cc=0, **extra) -> str:
    msg = {"usage": {"input_tokens": inp, "cache_read_input_tokens": cr,
                     "cache_creation_input_tokens": cc, "output_tokens": 9}}
    msg.update(extra.pop("message", {}))
    rec = {"type": "assistant", "message": msg}
    rec.update(extra)
    return json.dumps(rec)


class _Fx:
    """Dépôt jetable avec une battle `B` (phases/slices au choix) liée à la session `sid-1`."""

    def __init__(self, tmp: str, phases=None, slices=None, bind=True, transcript_lines=None) -> None:
        self.root = Path(tmp) / "repo"
        (self.root / ".legion" / "battles" / "B").mkdir(parents=True)
        self.tpath = Path(tmp) / "proj" / "sid-1.jsonl"
        self.tpath.parent.mkdir()
        body = {"id": "B", "phases": phases or {"think": {"status": "done"}, "plan": {"status": "done"},
                                                "build": {"status": "pending"}}}
        if slices is not None:
            body["slices"] = slices
        (self.root / ".legion" / "battles" / "B" / "battle.json").write_text(json.dumps(body), encoding="utf-8")
        if transcript_lines is not None:
            self.tpath.write_text("\n".join(transcript_lines) + "\n", encoding="utf-8")
        if bind:
            bs.bind_session(self.root, "sid-1", "B", str(self.tpath))

    def measure(self, **kw) -> dict:
        return measure(str(self.root), "B", str(self.root), **kw)

    def notes(self) -> dict:
        return notes(str(self.root), "B", str(self.root))


def _t_measure() -> None:
    with tempfile.TemporaryDirectory() as tmp:  # M1 : nominal, 100k + 30k + 5k
        fx = _Fx(tmp, transcript_lines=[_line(inp=100_000, cr=30_000, cc=5_000)])
        res = fx.measure()
        assert res["measurable"] and res["context_tokens"] == 135_000 and res["propose"], res
        assert res["reason"] == "ok" and res["header"].startswith("---\nbattle: B\n"), res
    with tempfile.TemporaryDirectory() as tmp:  # M2 : sous le seuil
        res = _Fx(tmp, transcript_lines=[_line(inp=50_000)]).measure()
        assert res["measurable"] and not res["propose"] and res["reason"] == "below_threshold", res
        assert res["header"] is None
    with tempfile.TemporaryDirectory() as tmp:  # M3 : seul le dernier tour compte
        lines = [_line(cr=100_000)] * 10 + [_line(inp=50_000)]
        assert _Fx(tmp, transcript_lines=lines).measure()["context_tokens"] == 50_000
    with tempfile.TemporaryDirectory() as tmp:  # M4 : sidechain, synthétique, zéro en fin de fichier
        lines = [_line(cr=80_000), _line(cr=500_000, isSidechain=True),
                 _line(cr=400_000, message={"model": "<synthetic>"}), _line(), "{pas json"]
        assert _Fx(tmp, transcript_lines=lines).measure()["context_tokens"] == 80_000
    with tempfile.TemporaryDirectory() as tmp:  # M5 : aucune liaison (mode pointeur)
        res = _Fx(tmp, bind=False, transcript_lines=[_line(cr=900_000)]).measure()
        assert not res["measurable"] and not res["propose"] and res["reason"] == "no_binding", res
    with tempfile.TemporaryDirectory() as tmp:  # M6 : liaison antérieure à la feature
        fx = _Fx(tmp, bind=False, transcript_lines=[_line(cr=900_000)])
        bs.bind_session(fx.root, "sid-1", "B")
        res = fx.measure()
        assert not res["measurable"] and res["reason"] == "no_transcript", res
    with tempfile.TemporaryDirectory() as tmp:  # M7 : absent, vide, corrompu
        fx = _Fx(tmp)
        assert fx.measure()["reason"] == "transcript_unreadable"
        fx.tpath.write_text("", encoding="utf-8")
        assert fx.measure()["reason"] == "no_usage"
        fx.tpath.write_text("{x\n[1,2]\nnull\n", encoding="utf-8")
        res = fx.measure()
        assert res["reason"] == "no_usage" and not res["propose"], res
    with tempfile.TemporaryDirectory() as tmp:  # M8 : nom de transcript ≠ <clé>.jsonl
        fx = _Fx(tmp, bind=False, transcript_lines=[_line(cr=900_000)])
        d = fx.root / ".legion" / "sessions"
        d.mkdir(parents=True)
        (d / "sid-1.json").write_text(json.dumps({"battle": "B", "bound_at": "T",
                                                  "transcript": str(fx.tpath.parent / "autre.jsonl")}),
                                      encoding="utf-8")
        res = fx.measure()
        assert not res["measurable"] and res["reason"] == "transcript_mismatch", res
    with tempfile.TemporaryDirectory() as tmp:  # M9 : > 120k mais travail en cours
        lines = [_line(cr=300_000)]
        fx = _Fx(tmp, phases={"think": {"status": "done"}, "build": {"status": "in_progress"}},
                 transcript_lines=lines)
        res = fx.measure()
        assert res["measurable"] and not res["propose"] and res["reason"] == "phase_in_progress", res
    with tempfile.TemporaryDirectory() as tmp:
        fx = _Fx(tmp, slices=[{"id": "slice-1", "status": "done"}, {"id": "slice-2", "status": "in_progress"}],
                 transcript_lines=[_line(cr=300_000)])
        res = fx.measure()
        assert not res["propose"] and res["reason"] == "slice_in_progress", res
    with tempfile.TemporaryDirectory() as tmp:  # M10 : id invalide, battle absente
        fx = _Fx(tmp, transcript_lines=[_line(cr=300_000)])
        res = measure(str(fx.root), "../x", str(fx.root))
        assert not res["measurable"] and res["reason"] == "invalid_battle", res
        res = measure(str(fx.root), "Z", str(fx.root))
        assert not res["measurable"] and res["reason"] == "battle_unreadable", res
    with tempfile.TemporaryDirectory() as tmp:  # M11 : exactement le seuil
        fx = _Fx(tmp, transcript_lines=[_line(inp=RESET_THRESHOLD)])
        res = fx.measure()
        assert res["measurable"] and not res["propose"] and res["reason"] == "below_threshold", res
        fx.tpath.write_text(_line(inp=RESET_THRESHOLD + 1) + "\n", encoding="utf-8")
        assert fx.measure()["propose"] is True
    with tempfile.TemporaryDirectory() as tmp:  # M12 : transcript plus grand que la fenêtre
        filler = _line(cr=1_000, message={"pad": "x" * 400})
        lines = [_line(cr=999_999)] + [filler] * 40 + [_line(cr=222_222)]
        fx = _Fx(tmp, transcript_lines=lines)
        res = fx.measure(tail_bytes=2_000)
        assert res["context_tokens"] == 222_222, res
        res = fx.measure(tail_bytes=250)   # fenêtre au milieu de la dernière ligne : ligne partielle ignorée
        assert res["reason"] in ("ok", "no_usage"), res
        assert res["context_tokens"] in (None, 222_222), res


def _t_notes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fx = _Fx(tmp, transcript_lines=[_line(cr=300_000)])
        npath = fx.root / ".legion" / "battles" / "B" / NOTES_NAME
        res = fx.notes()  # N1
        assert res["ok"] and not res["present"] and res["reason"] == "absent", res
        header = fx.measure()["header"]
        npath.write_text(header + "# Notes de reprise — B\n", encoding="utf-8")  # N2
        res = fx.notes()
        assert res["present"] and res["fresh"] and res["reason"] == "fresh" and res["bytes"] > 0, res
        bj = fx.root / ".legion" / "battles" / "B" / "battle.json"  # N3 : une phase a changé
        data = json.loads(bj.read_text(encoding="utf-8"))
        data["phases"]["build"]["status"] = "done"
        bj.write_text(json.dumps(data), encoding="utf-8")
        res = fx.notes()
        assert res["present"] and not res["fresh"] and res["reason"] == "stale", res
        npath.write_text("---\nbattle: B\nphases\n---\n", encoding="utf-8")  # N4 : en-tête malformé
        assert fx.notes()["reason"] == "unreadable"
        npath.write_text("---\nbattle: B\nphases: x", encoding="utf-8")  # jamais refermé
        assert fx.notes()["reason"] == "unreadable"
        npath.write_text("---\nbattle: AUTRE\nphases: x\n---\n", encoding="utf-8")
        res = fx.notes()
        assert not res["fresh"] and res["reason"] == "other_battle", res
        npath.write_bytes(b"\xff\xfe\x00bad")
        assert fx.notes()["reason"] == "unreadable"
        npath.write_text("Notes libres, sans en-tête.\n", encoding="utf-8")  # N5
        res = fx.notes()
        assert res["present"] and not res["fresh"] and res["reason"] == "no_header", res
        res = notes(str(fx.root), "../x", str(fx.root))
        assert not res["ok"] and res["reason"] == "invalid_battle", res


def _t_cross() -> None:  # X1 : vrais CLI battle_state init, session_bind, context_check measure
    hooks = os.path.join(os.path.dirname(_SCRIPTS_DIR), "hooks")
    if not os.path.isfile(os.path.join(hooks, "session_bind.py")):
        print("SKIP: context_check X1 (session_bind.py introuvable)", file=sys.stderr)
        return
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    import session_bind  # noqa: E402

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        root.mkdir()
        py = sys.executable
        init = [py, os.path.join(_SCRIPTS_DIR, "battle_state.py"), "init", "B", "--ticket", "GH#1",
                "--title", "T", "--profile", "feature", "--repo", str(root)]
        env = {**os.environ, "LEGION_FLEET": str(Path(tmp) / "fleet.json")}
        proc = subprocess.run(init, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
        assert proc.returncode == 0, proc.stderr
        proj = Path(tmp) / "proj"
        proj.mkdir()
        tpath = proj / "sid-x.jsonl"
        tpath.write_text(_line(inp=70_000, cr=60_000, cc=1_000) + "\n", encoding="utf-8")
        command = " ".join(f'"{a}"' for a in init)
        session_bind._handle({"hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": str(root),
                              "session_id": "sid-x", "transcript_path": str(tpath),
                              "tool_input": {"command": command},
                              "tool_response": {"stdout": proc.stdout}})
        out = subprocess.run([py, os.path.abspath(__file__), "measure", "--battle", "B", "--repo", str(root)],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL)
        assert out.returncode == 0, out.stderr
        res = json.loads(out.stdout)
        # `init` laisse think en `in_progress` : la mesure est juste, la proposition est refusée.
        assert res["measurable"] and res["context_tokens"] == 131_000, res
        assert res["propose"] is False and res["reason"] == "phase_in_progress", res
        bad = subprocess.run([py, os.path.abspath(__file__), "measure", "--bogus"],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL)
        assert bad.returncode == 1 and not bad.stdout.strip(), bad


def _self_test() -> int:
    tests = (_t_measure, _t_notes, _t_cross)
    for t in tests:
        t()
    print(f"OK: {len(tests)} tests context_check", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
