"""Lanceur Legatus multi-OS de legion (`/legion:legatus`), sans PowerShell.

Résout le clone marketplace de Legatus (glob, jamais de chemin en dur), vérifie que
`dotnet` est disponible, détecte si Legatus écoute déjà (port 5021 par défaut, `--port`), le lance **détaché**
(il survit à la session Claude) puis ouvre le navigateur. Fonctionne sous Linux, WSL2,
macOS et Windows natif.

Le cœur de décision est **pur** (`_is_wsl`, `_pick_project`, `_choose_opener`, `_command`,
`_plan`, `_wait_ready`) : ni disque, ni socket, ni process réels. La couche I/O (`_find_projects`,
`_port_listening`, `_launch`, `_open_browser`) reste mince. `--self-test` est hermétique.

Usage :
    python legatus.py [--project <csproj>] [--dry-run] [--timeout <s>] [--port 5021]
`--port` fixe le port sondé ET le port d'écoute de Legatus (`--urls` transmis à l'application
quand il diffère de 5021).
    python legatus.py --self-test

Sortie : un objet JSON sur stdout
    { ok, state, url, project, built, wsl, opener, listening, pid, ready,
      browser_opened, log, command, message }
`state` : started | already_running | not_found | no_dotnet | failed | dry_run.
Exit : 0 si ok, 2 si refusé/échec (no_dotnet, not_found, failed), 1 si usage invalide.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path

DEFAULT_PORT = 5021
DEFAULT_TIMEOUT = 90.0
CSPROJ_GLOB = "*/ui/legatus/src/presentation/IA.Legatus.csproj"
DLL_REL = Path("bin") / "Debug" / "net10.0" / "IA.Legatus.dll"
PROBE_HOSTS = ("127.0.0.1", "::1")


# --------------------------------------------------------------------------- cœur pur

def _is_wsl(text: str | None) -> bool:
    """Contenu de /proc/version -> WSL ? (WSL1 « Microsoft », WSL2 « microsoft »)."""
    return bool(text) and "microsoft" in text.lower()


def _pick_project(candidates: list[str], is_built) -> tuple[str, bool] | None:
    """Préfère un clone déjà construit ; sinon le premier (ordre trié). None si vide."""
    ordered = sorted(candidates)
    if not ordered:
        return None
    for c in ordered:
        if is_built(c):
            return c, True
    return ordered[0], False


def _choose_opener(is_wsl: bool, which, url: str) -> list[str] | None:
    """argv de l'ouvreur, ou None = utiliser `webbrowser` (Linux natif, macOS, Windows)."""
    if is_wsl:
        if which("wslview"):
            return ["wslview", url]
        return ["cmd.exe", "/c", "start", "", url]
    return None


def _command(proj_dir: str, port: int) -> list[str]:
    """argv de `dotnet run`. `--urls` n'est ajouté que si le port diffère du défaut."""
    argv = ["dotnet", "run", "--project", proj_dir, "--launch-profile", "http"]
    if port != DEFAULT_PORT:
        argv += ["--", "--urls", f"http://localhost:{port}"]
    return argv


def _plan(*, dotnet: str | None, project: tuple[str, bool] | None, listening: bool,
          dry_run: bool, wsl: bool, opener: list[str] | None, url: str) -> dict:
    """Décide l'état avant tout effet de bord. `action` : stop | open | launch."""
    diag = {"url": url, "wsl": wsl, "opener": opener[0] if opener else "webbrowser",
            "listening": listening, "project": project[0] if project else None,
            "built": project[1] if project else None}
    if not dotnet:
        return {**diag, "ok": False, "state": "no_dotnet", "action": "stop",
                "message": "The .NET SDK is required (`dotnet` not found on PATH)."}
    if not project:
        return {**diag, "ok": False, "state": "not_found", "action": "stop",
                "message": ("Legatus project not found under "
                            "~/.claude/plugins/marketplaces/*/ui/legatus/. "
                            "Is the legion marketplace installed?")}
    if dry_run:
        return {**diag, "ok": True, "state": "dry_run", "action": "stop",
                "message": "Dry run: nothing launched."}
    if listening:
        return {**diag, "ok": True, "state": "already_running", "action": "open",
                "message": f"Legatus is already running - reopened {url}"}
    return {**diag, "ok": True, "state": "started", "action": "launch", "message": ""}


def _wait_ready(poll, listening, timeout: float, clock, sleep,
                interval: float = 0.5) -> str:
    """Attend l'écoute du port. Rend 'ready' | 'died' | 'timeout'."""
    deadline = clock() + timeout
    while True:
        if poll() is not None:
            return "died"
        if listening():
            return "ready"
        if clock() >= deadline:
            return "timeout"
        sleep(interval)


# --------------------------------------------------------------------------- I/O

def _read_proc_version() -> str | None:
    try:
        return Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _marketplaces_root() -> Path:
    return Path.home() / ".claude" / "plugins" / "marketplaces"


def _find_projects(root: Path) -> list[str]:
    try:
        return sorted(str(p) for p in Path(root).glob(CSPROJ_GLOB))
    except OSError:
        return []


def _is_built(csproj: str) -> bool:
    return (Path(csproj).parent / DLL_REL).is_file()


def _port_listening(port: int, hosts=PROBE_HOSTS, timeout: float = 0.3) -> bool:
    """Vrai si un hôte accepte une connexion. OSError = n'écoute pas (IPv6 souvent off)."""
    for host in hosts:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                if s.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def _log_path() -> Path:
    return Path.home() / ".claude" / "legion" / "legatus.log"


def _launch(argv: list[str], cwd: str, log: Path):
    """Exécute `argv` détaché : survit à la session, aucun pipe gardé ouvert."""
    log.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log, "ab")
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, **kwargs)
    finally:
        logf.close()


def _open_browser(argv: list[str] | None, url: str) -> bool:
    if argv:
        try:
            cwd = "/mnt/c" if argv[0] == "cmd.exe" and os.path.isdir("/mnt/c") else None
            subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass  # interop désactivée : repli webbrowser
    try:
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 - l'échec navigateur ne doit jamais faire échouer
        return False


# --------------------------------------------------------------------------- CLI

def run(args: dict, *, which=shutil.which, probe=_port_listening, root: Path | None = None,
        proc_version: str | None = None, launch=_launch, opener_fn=_open_browser,
        clock=time.monotonic, sleep=time.sleep) -> tuple[dict, int]:
    port = args["port"]
    url = f"http://localhost:{port}"
    wsl = _is_wsl(proc_version if proc_version is not None else _read_proc_version())
    opener = _choose_opener(wsl, which, url)
    dotnet = which("dotnet")
    if args.get("project"):
        p = str(args["project"])
        project = (p, _is_built(p)) if Path(p).is_file() else None
    else:
        project = _pick_project(_find_projects(root or _marketplaces_root()), _is_built)
    listening = probe(port) if dotnet and project else False
    plan = _plan(dotnet=dotnet, project=project, listening=listening,
                 dry_run=args["dry_run"], wsl=wsl, opener=opener, url=url)
    action = plan.pop("action")
    proj_dir = str(Path(project[0]).parent) if project else None
    cmd = _command(proj_dir, port) if project else None
    out = {**plan, "pid": None, "ready": None, "browser_opened": None, "log": None,
           "command": cmd}
    if action == "open":
        out["browser_opened"] = opener_fn(opener, url)
    elif action == "launch":
        log = _log_path()
        out["log"] = str(log)
        try:
            proc = launch(cmd, proj_dir, log)
        except OSError as exc:
            out.update(ok=False, state="failed", message=f"Could not start dotnet: {exc}")
            return out, 2
        out["pid"] = proc.pid
        res = _wait_ready(proc.poll, lambda: probe(port), args["timeout"], clock, sleep)
        if res == "died":
            out.update(ok=False, state="failed", ready=False,
                       message=f"dotnet exited early (code {proc.poll()}). See {log}.")
            return out, 2
        out["ready"] = res == "ready"
        out["browser_opened"] = opener_fn(opener, url)
        out["message"] = (f"Legatus started (PID {proc.pid}) -> {url}" if out["ready"] else
                          f"Legatus started (PID {proc.pid}) but not ready after "
                          f"{args['timeout']:.0f}s (first build is slow) -> {url}. See {log}.")
    return out, (0 if out["ok"] else 2)


def _parse(argv: list[str]) -> dict | None:
    a = {"project": None, "dry_run": False, "timeout": DEFAULT_TIMEOUT, "port": DEFAULT_PORT}
    i = 0
    try:
        while i < len(argv):
            t = argv[i]
            if t == "--dry-run":
                a["dry_run"] = True
                i += 1
            elif t == "--project":
                a["project"] = argv[i + 1]
                i += 2
            elif t == "--timeout":
                a["timeout"] = float(argv[i + 1])
                i += 2
            elif t == "--port":
                a["port"] = int(argv[i + 1])
                i += 2
            else:
                return None
    except (IndexError, ValueError):
        return None
    return a


def main() -> int:
    argv = sys.argv[1:]
    if "--self-test" in argv:
        return _self_test()
    args = _parse(argv)
    if args is None:
        print("usage: legatus.py [--project <csproj>] [--dry-run] [--timeout <s>] "
              "[--port <n>] | --self-test", file=sys.stderr)
        return 1
    out, code = run(args)
    print(json.dumps(out, ensure_ascii=False))
    return code


# --------------------------------------------------------------------------- self-test

def _self_test() -> int:
    failures: list[str] = []

    def check(name, fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {exc!r}")

    def mk_tree(root: Path, name: str, built: bool) -> str:
        d = root / name / "ui" / "legatus" / "src" / "presentation"
        d.mkdir(parents=True)
        csproj = d / "IA.Legatus.csproj"
        csproj.write_text("<Project/>", encoding="utf-8")
        if built:
            (d / DLL_REL).parent.mkdir(parents=True)
            (d / DLL_REL).write_bytes(b"x")
        return str(csproj)

    def t_pick_prefers_built():
        with tempfile.TemporaryDirectory() as t:
            mk_tree(Path(t), "a", False)
            b = mk_tree(Path(t), "b", True)
            got = _pick_project(_find_projects(Path(t)), _is_built)
            assert got == (b, True), got

    def t_pick_none():
        with tempfile.TemporaryDirectory() as t:
            assert _pick_project(_find_projects(Path(t)), _is_built) is None
            out, code = run({"project": None, "dry_run": False, "timeout": 1, "port": 1},
                            which=lambda n: "/x/dotnet", probe=lambda p: False,
                            root=Path(t), proc_version="Linux")
            assert out["state"] == "not_found" and out["ok"] is False and code == 2, out
            assert out["command"] is None, out

    def t_project_override():
        with tempfile.TemporaryDirectory() as t:
            mk_tree(Path(t), "built", True)
            forced = mk_tree(Path(t), "forced", False)
            out, _ = run({"project": forced, "dry_run": True, "timeout": 1, "port": 1},
                         which=lambda n: "/x/dotnet", probe=lambda p: False,
                         root=Path(t), proc_version="Linux")
            assert out["project"] == forced and out["built"] is False, out

    def t_port_listening():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            assert _port_listening(port, hosts=("127.0.0.1",)) is True
        finally:
            srv.close()
        assert _port_listening(port, hosts=("127.0.0.1",)) is False

    def t_is_wsl():
        assert _is_wsl("Linux version 6.6.87.2-microsoft-standard-WSL2 (gcc)")
        assert _is_wsl("Linux version 4.4.0-19041-Microsoft (Microsoft@Microsoft.com)")
        assert not _is_wsl("Linux version 6.8.0-generic (buildd@lcy02)")
        assert not _is_wsl("Darwin Kernel Version 23.0.0")
        assert not _is_wsl(None)

    def t_choose_opener():
        u = "http://localhost:5021"
        assert _choose_opener(True, lambda n: "/usr/bin/wslview", u) == ["wslview", u]
        assert _choose_opener(True, lambda n: None, u) == ["cmd.exe", "/c", "start", "", u]
        assert _choose_opener(False, lambda n: "/usr/bin/wslview", u) is None

    def t_no_dotnet():
        with tempfile.TemporaryDirectory() as t:
            mk_tree(Path(t), "a", True)
            out, code = run({"project": None, "dry_run": False, "timeout": 1, "port": 1},
                            which=lambda n: None, probe=lambda p: True,
                            root=Path(t), proc_version="Linux")
            assert out["state"] == "no_dotnet" and out["ok"] is False and code == 2, out

    def t_dry_run_output():
        def boom(*a, **k):
            raise AssertionError("must not launch/open/probe-launch in dry-run")
        with tempfile.TemporaryDirectory() as t:
            csproj = mk_tree(Path(t), "a", True)
            out, code = run({"project": None, "dry_run": True, "timeout": 1, "port": 5021},
                            which=lambda n: "/x/" + n, probe=lambda p: False,
                            root=Path(t), proc_version="Linux ... microsoft-standard-WSL2",
                            launch=boom, opener_fn=boom)
            assert code == 0 and out["ok"] is True and out["state"] == "dry_run", out
            assert out["project"] == csproj and out["built"] is True, out
            assert out["wsl"] is True and out["opener"] == "wslview", out
            assert out["listening"] is False and out["url"] == "http://localhost:5021", out
            assert out["pid"] is None, out
            assert out["command"] == ["dotnet", "run", "--project", str(Path(csproj).parent),
                                      "--launch-profile", "http"], out
            assert "--urls" not in out["command"], out

    def t_already_running():
        with tempfile.TemporaryDirectory() as t:
            mk_tree(Path(t), "a", True)
            opened = []

            def boom(*a, **k):
                raise AssertionError("must not launch")
            out, code = run({"project": None, "dry_run": False, "timeout": 1, "port": 5021},
                            which=lambda n: "/x/dotnet", probe=lambda p: True,
                            root=Path(t), proc_version="Linux", launch=boom,
                            opener_fn=lambda o, u: opened.append(u) or True)
            assert out["state"] == "already_running" and code == 0 and opened, out

    class FakeProc:
        def __init__(self, rc):
            self.pid, self._rc = 42, rc

        def poll(self):
            return self._rc

    def t_failed_early_exit():
        with tempfile.TemporaryDirectory() as t:
            mk_tree(Path(t), "a", True)
            out, code = run({"project": None, "dry_run": False, "timeout": 5, "port": 5021},
                            which=lambda n: "/x/dotnet", probe=lambda p: False,
                            root=Path(t), proc_version="Linux",
                            launch=lambda a, c, l: FakeProc(1),
                            opener_fn=lambda o, u: True, sleep=lambda s: None)
            assert out["state"] == "failed" and out["ok"] is False and code == 2, out
            assert out["log"], out

    def t_command_default_port():
        assert _command("/p", 5021) == ["dotnet", "run", "--project", "/p",
                                        "--launch-profile", "http"]

    def t_command_custom_port():
        c = _command("/p", 5099)
        assert c[-5:] == ["--launch-profile", "http", "--", "--urls",
                          "http://localhost:5099"], c
        assert c.index("--launch-profile") < c.index("--urls"), c

    def t_dry_run_custom_port():
        def boom(*a, **k):
            raise AssertionError("must not launch/open in dry-run")
        with tempfile.TemporaryDirectory() as t:
            mk_tree(Path(t), "a", True)
            out, code = run({"project": None, "dry_run": True, "timeout": 1, "port": 5099},
                            which=lambda n: "/x/dotnet", probe=lambda p: False,
                            root=Path(t), proc_version="Linux", launch=boom, opener_fn=boom)
            assert code == 0 and out["state"] == "dry_run", out
            assert out["url"] == "http://localhost:5099", out
            c = out["command"]
            assert c[c.index("--urls") + 1] == "http://localhost:5099", c

    def t_launch_receives_command():
        with tempfile.TemporaryDirectory() as t:
            csproj = mk_tree(Path(t), "a", True)
            seen, opened, state = [], [], {"up": False}

            class Proc:
                pid = 7

                def poll(self):
                    return None

            def fake_launch(argv, cwd, log):
                seen.append((argv, cwd))
                state["up"] = True
                return Proc()
            out, code = run({"project": None, "dry_run": False, "timeout": 5, "port": 5099},
                            which=lambda n: "/x/dotnet", probe=lambda p: state["up"],
                            root=Path(t), proc_version="Linux", launch=fake_launch,
                            opener_fn=lambda o, u: opened.append(u) or True,
                            sleep=lambda s: None)
            assert out["state"] == "started" and out["ready"] is True and code == 0, out
            assert seen == [(out["command"], str(Path(csproj).parent))], seen
            assert opened == ["http://localhost:5099"], opened

    def t_wait_timeout_and_ready():
        ticks = iter(range(0, 1000))
        r = _wait_ready(lambda: None, lambda: False, 3, lambda: next(ticks), lambda s: None)
        assert r == "timeout", r
        n = {"i": 0}

        def ls():
            n["i"] += 1
            return n["i"] >= 3
        assert _wait_ready(lambda: None, ls, 30, time.monotonic, lambda s: None) == "ready"

    for name, fn in [("t_pick_prefers_built", t_pick_prefers_built),
                     ("t_pick_none", t_pick_none),
                     ("t_project_override", t_project_override),
                     ("t_port_listening", t_port_listening),
                     ("t_is_wsl", t_is_wsl),
                     ("t_choose_opener", t_choose_opener),
                     ("t_no_dotnet", t_no_dotnet),
                     ("t_dry_run_output", t_dry_run_output),
                     ("t_already_running", t_already_running),
                     ("t_failed_early_exit", t_failed_early_exit),
                     ("t_command_default_port", t_command_default_port),
                     ("t_command_custom_port", t_command_custom_port),
                     ("t_dry_run_custom_port", t_dry_run_custom_port),
                     ("t_launch_receives_command", t_launch_receives_command),
                     ("t_wait_timeout_and_ready", t_wait_timeout_and_ready)]:
        check(name, fn)
    if failures:
        for f in failures:
            print("FAIL " + f, file=sys.stderr)
        return 1
    print("OK: legatus self-test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
