"""Journal central du RETEX OUTILLAGE de legion : les adaptations de plugin
suggerees lors des retros, agregees a travers TOUTES les batailles/repos pour pouvoir
les exploiter (prioriser les ameliorations).

Distinct du RETEX « code/projet » (qui part en memoire projet Claude). Ici on
capture le meta : ce qui a frotte dans l'OUTIL pendant la bataille et comment
l'adapter. « Le plugin » = legion en priorite (orchestrateur, gates, hooks,
commandes, scripts) ; chaque entree peut aussi viser un plugin delegue
(`dotnet-claude-kit`) quand il est la source.

Stockage : `~/.claude/legion/plugin-retex.jsonl` (append-only => sur en
concurrence, jamais de read-modify-write). Surchargeable par `--journal`.

Cycle de vie (toujours append-only) : une entree recoit un **id stable** (derive
de son contenu). La traiter = appendre un **tombstone** `{type:"resolved", id}` ;
aucune mutation/suppression en place. `list` masque par defaut les entrees
resolues (boucle qui reste actionnable), `--all` les montre toutes, `--resolved`
ne montre que l'historique resolu.

Une entree :
    { id, ts, plugin, battle, repo, area, severity, observation, suggestion }
Champs requis a l'append : plugin, observation, suggestion. Les autres sont
completes/optionnels. `id` est derive si absent (entrees legacy comprises).

Usage :
    python plugin_retex.py append  --file <entries.json> [--battle B] [--repo R] [--journal P]
    python plugin_retex.py list    [--plugin NAME] [--all | --resolved] [--journal P]
    python plugin_retex.py resolve <id> [<id> ...] [--note N] [--journal P]
    python plugin_retex.py resolve --all [--plugin NAME] [--note N] [--journal P]
    python plugin_retex.py --self-test

`--file` : un objet entree, ou une liste d'entrees (les suggestions d'une bataille).
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REQUIRED = ("plugin", "observation", "suggestion")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _journal_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return Path.home() / ".claude" / "legion" / "plugin-retex.jsonl"


def _entry_id(entry: dict) -> str:
    """Id stable d'une entree RETEX. Prefere un `id` explicite, sinon derive du
    contenu (`ts|plugin|observation`) — deterministe, donc les entrees legacy
    (sans id) recoivent le meme id a chaque lecture."""
    stored = str(entry.get("id") or "").strip()
    if stored:
        return stored
    seed = f"{entry.get('ts','')}|{entry.get('plugin','')}|{entry.get('observation','')}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def _normalize(entry: dict, battle: str | None, repo: str | None) -> dict | None:
    """Valide/complete une entree. Retourne None si un champ requis manque."""
    if not isinstance(entry, dict):
        return None
    if any(not str(entry.get(k, "")).strip() for k in _REQUIRED):
        return None
    out = {
        "id": None,                                  # rempli ci-dessous
        "ts": entry.get("ts") or _now_iso(),
        "plugin": entry["plugin"],
        "battle": entry.get("battle") or battle,
        "repo": entry.get("repo") or repo,
        "area": entry.get("area"),                   # gate:/hook:/command:/skill:/script:
        "severity": entry.get("severity"),           # blocker | friction | annoyance | idea
        "observation": entry["observation"],
        "suggestion": entry["suggestion"],
    }
    out["id"] = _entry_id(out)                        # derive apres ts/plugin/observation
    return out


def append(entries: list, journal: Path, battle: str | None, repo: str | None) -> tuple[int, int]:
    """Append les entrees valides au journal. Retourne (ecrites, ignorees)."""
    journal.parent.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    with open(journal, "a", encoding="utf-8") as f:
        for raw in entries:
            entry = _normalize(raw, battle, repo)
            if entry is None:
                skipped += 1
                continue
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            written += 1
    return written, skipped


def _append_tombstones(ids: list[str], journal: Path, note: str | None) -> int:
    """Append un tombstone `resolved` par id (idempotent : re-resoudre est inoffensif)."""
    journal.parent.mkdir(parents=True, exist_ok=True)
    ts = _now_iso()
    written = 0
    with open(journal, "a", encoding="utf-8") as f:
        for rid in ids:
            rec = {"type": "resolved", "id": rid, "ts": ts}
            if note:
                rec["note"] = note
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1
    return written


def _read_journal(journal: Path) -> list:
    out = []
    try:
        with open(journal, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _split(records: list) -> tuple[list, set]:
    """Separe les entrees RETEX des tombstones. Retourne (entrees, ids_resolus)."""
    entries, resolved = [], set()
    for r in records:
        if not isinstance(r, dict):
            continue
        if r.get("type") == "resolved":
            rid = str(r.get("id") or "").strip()
            if rid:
                resolved.add(rid)
        else:
            r = dict(r)
            r["id"] = _entry_id(r)
            entries.append(r)
    return entries, resolved


def _to_markdown(entries: list, plugin_filter: str | None, resolved: set, mode: str) -> str:
    """mode ∈ {active, all, resolved}. `active` masque les resolus (defaut)."""
    rows = [e for e in entries if not plugin_filter or e.get("plugin") == plugin_filter]
    if mode == "active":
        rows = [e for e in rows if e["id"] not in resolved]
    elif mode == "resolved":
        rows = [e for e in rows if e["id"] in resolved]
    if not rows:
        return "_(aucune entrée RETEX plugin)_"
    title = {"active": "actives", "all": "toutes", "resolved": "résolues"}[mode]
    lines = [f"# RETEX outillage — plugins legion ({title})", ""]
    by_plugin: dict[str, list] = {}
    for e in rows:
        by_plugin.setdefault(e.get("plugin") or "?", []).append(e)
    for plugin in sorted(by_plugin):
        lines.append(f"## {plugin} ({len(by_plugin[plugin])})")
        for e in by_plugin[plugin]:
            done = " ✓" if e["id"] in resolved else ""
            tag = f"[{e.get('severity') or '-'}] {e.get('area') or '-'}"
            src = " · ".join(x for x in (e.get("battle"), e.get("repo")) if x)
            lines.append(f"- `{e['id']}`{done} **{tag}** — {e['observation']}")
            lines.append(f"  → {e['suggestion']}  _({src})_")
        lines.append("")
    return "\n".join(lines).rstrip()


def main() -> int:
    args = sys.argv[1:]
    if "--self-test" in args:
        return _self_test()
    if not args:
        print("usage: plugin_retex.py append --file F | list [--all|--resolved] | "
              "resolve <id…>|--all", file=sys.stderr)
        return 1

    def opt(name):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None

    journal = _journal_path(opt("--journal"))
    cmd = args[0]

    if cmd == "append":
        path = opt("--file")
        if not path:
            print("append requiert --file <entries.json>", file=sys.stderr)
            return 1
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"lecture --file impossible: {exc}", file=sys.stderr)
            return 2
        entries = data if isinstance(data, list) else [data]
        written, skipped = append(entries, journal, opt("--battle"), opt("--repo"))
        print(f"RETEX plugin : {written} écrite(s), {skipped} ignorée(s) → {journal}", file=sys.stderr)
        return 0

    if cmd == "list":
        mode = "all" if "--all" in args else "resolved" if "--resolved" in args else "active"
        entries, resolved = _split(_read_journal(journal))
        print(_to_markdown(entries, opt("--plugin"), resolved, mode))
        return 0

    if cmd == "resolve":
        entries, resolved = _split(_read_journal(journal))
        note = opt("--note")
        if "--all" in args:
            plugin_filter = opt("--plugin")
            targets = sorted({
                e["id"] for e in entries
                if e["id"] not in resolved and (not plugin_filter or e.get("plugin") == plugin_filter)
            })
            if not targets:
                print("RETEX plugin : aucune entrée active à résoudre.", file=sys.stderr)
                return 0
            n = _append_tombstones(targets, journal, note)
            print(f"RETEX plugin : {n} entrée(s) marquée(s) résolue(s) → {journal}", file=sys.stderr)
            return 0
        # ids positionnels (hors flags et valeurs de flags)
        flags_with_val = {"--journal", "--plugin", "--note"}
        ids, i, rest = [], 0, args[1:]
        while i < len(rest):
            t = rest[i]
            if t in flags_with_val:
                i += 2
                continue
            if t.startswith("--"):
                i += 1
                continue
            ids.append(t)
            i += 1
        if not ids:
            print("resolve requiert un ou plusieurs <id>, ou --all", file=sys.stderr)
            return 1
        known = {e["id"] for e in entries}
        unknown = [x for x in ids if x not in known]
        if unknown:
            print(f"⚠️ id(s) inconnu(s) (tombstone posé quand même) : {', '.join(unknown)}", file=sys.stderr)
        n = _append_tombstones(ids, journal, note)
        print(f"RETEX plugin : {n} entrée(s) marquée(s) résolue(s) → {journal}", file=sys.stderr)
        return 0

    print(f"commande inconnue: {cmd}", file=sys.stderr)
    return 1


def _self_test() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        journal = Path(d) / "plugin-retex.jsonl"
        entries = [
            {"plugin": "legion", "area": "gate:reviewer",
             "observation": "Gate bloque sur un faux positif Roslyn", "suggestion": "Tolérer X"},
            {"plugin": "legion", "observation": "incomplet"},  # manque suggestion -> ignoré
        ]
        written, skipped = append(entries, journal, "2026-06-10-retex", "legion")
        assert (written, skipped) == (1, 1), (written, skipped)
        rows, resolved = _split(_read_journal(journal))
        assert len(rows) == 1 and rows[0]["battle"] == "2026-06-10-retex" and rows[0]["repo"] == "legion"
        assert rows[0]["ts"]                                    # horodaté
        rid = rows[0]["id"]
        assert rid and len(rid) == 12                           # id stable dérivé
        assert not resolved                                     # rien de résolu encore

        # id déterministe (re-lecture => même id)
        rows2, _ = _split(_read_journal(journal))
        assert rows2[0]["id"] == rid

        # markdown actif: montre l'entrée
        md = _to_markdown(rows, None, resolved, "active")
        assert "Tolérer X" in md and rid in md
        assert _to_markdown(rows, "dotnet-claude-kit", resolved, "active") == "_(aucune entrée RETEX plugin)_"

        # resolve par id => masqué en actif, visible en resolved/all
        assert _append_tombstones([rid], journal, None) == 1
        rows3, resolved3 = _split(_read_journal(journal))
        assert resolved3 == {rid}
        assert _to_markdown(rows3, None, resolved3, "active") == "_(aucune entrée RETEX plugin)_"
        assert rid in _to_markdown(rows3, None, resolved3, "resolved")
        assert "✓" in _to_markdown(rows3, None, resolved3, "all")

        # resolve --all sur un journal frais avec 2 entrées actives
        j2 = Path(d) / "j2.jsonl"
        append([
            {"plugin": "legion", "observation": "A", "suggestion": "a"},
            {"plugin": "dotnet-claude-kit", "observation": "B", "suggestion": "b"},
        ], j2, None, None)
        e2, r2 = _split(_read_journal(j2))
        targets = sorted({e["id"] for e in e2 if e["id"] not in r2})
        assert len(targets) == 2
        _append_tombstones(targets, j2, "batch")
        e3, r3 = _split(_read_journal(j2))
        assert r3 == set(targets)
        assert _to_markdown(e3, None, r3, "active") == "_(aucune entrée RETEX plugin)_"
    print("OK: plugin_retex self-test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # French + arrows on a cp1252 console
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
