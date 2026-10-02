---
description: Start Legatus (the legion web UI) from any repo — resolves the marketplace clone, launches it detached on http://localhost:5021 (default), and opens the browser.
argument-hint: [--dry-run] [--port N]
---

Start **Legatus** — the local read-only web UI that tracks legion battles across all
your repos. Legatus is **not** in the plugin cache; it lives in the **marketplace
clone** (a full checkout of the legion repo under `~/.claude/plugins/marketplaces/`), so
this command works from **any repo** and on Linux, WSL2, macOS and Windows. Never `cd`;
run from the current directory.

Run one command (fall back to `python3` when `python` is absent, e.g. on WSL2/Linux):

```bash
$(command -v python || command -v python3) "${CLAUDE_PLUGIN_ROOT}/scripts/legatus.py" [--dry-run] [--port <N>]
```

**Pass through only the two supported options**, never the raw argument string:
`--dry-run` if the user asked for it, and `--port <N>` only when `N` is a plain integer
(1-65535). Drop anything else and tell the user it was ignored. Arguments: `$ARGUMENTS`

The script checks that `dotnet` is on PATH, resolves the Legatus project by glob
(preferring an already-built clone), reuses a running instance on port 5021 (default), otherwise
launches `dotnet run` **detached** (it survives this session) and opens the browser
(`wslview` or `cmd.exe /c start` on WSL, `webbrowser` elsewhere). It prints one JSON
object on stdout. Read its `state` and report to the user:

| `state` | Meaning | Tell the user |
|---|---|---|
| `started` | Launched detached. | "Legatus started (PID `pid`) → `url`". If `ready` is `false`, add that the first build is slow and the page may need a refresh; point to `log`. If `built` is `false`, note the first launch compiles. |
| `already_running` | Port 5021 already listens. | "Legatus is already running — reopened `url`". |
| `not_found` | No clone under `~/.claude/plugins/marketplaces/*/ui/legatus/`. | Warn that the legion marketplace may not be installed. Nothing was launched. |
| `no_dotnet` | `dotnet` not on PATH. | Warn that the .NET SDK is required. Nothing was launched. |
| `failed` | `dotnet` exited before listening. | Report `message` and show the `log` path (`~/.claude/legion/legatus.log`). |

If `browser_opened` is `false`, print the `url` so the user can open it manually. The
`message` field already holds a ready-to-print sentence.

`--port N` launches and probes Legatus on port N instead of 5021. `--dry-run` reports
what would happen (`state: dry_run`) without launching anything, and shows the
`dotnet run` command in `command`.
