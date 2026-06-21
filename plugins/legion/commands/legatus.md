---
description: Start Legatus (the legion web UI) from any repo — resolves the marketplace clone, launches it detached on http://localhost:5021, and opens the browser.
argument-hint: (no args)
---

Start **Legatus** — the local read-only web UI that tracks legion battles across all
your repos. Legatus is **not** in the plugin cache; it lives in the **marketplace
clone** (a full checkout of the legion repo under `~/.claude/plugins/marketplaces/`), so
this command works from **any repo**. It launches the app **detached** so it survives
this Claude session, then opens your browser. Never `cd`; run from the current directory.

1. **`dotnet` preflight** — verify the .NET SDK is on PATH.

   ```powershell
   Get-Command dotnet -ErrorAction SilentlyContinue
   ```

   If `dotnet` is missing, print a clear warning and stop:
   "⚠️ The .NET SDK is required (`dotnet` not found on PATH). Command aborted."

2. **Resolve the Legatus project** in the marketplace clone — **by glob, never a
   hard-coded path**. Prefer a clone that is already built (`IA.Legatus.dll` present), so
   the first launch is fast:

   ```powershell
   $proj = Get-ChildItem "$env:USERPROFILE\.claude\plugins\marketplaces\*\ui\legatus\src\presentation\IA.Legatus.csproj" -ErrorAction SilentlyContinue |
       Sort-Object { -not (Test-Path (Join-Path $_.DirectoryName 'bin\Debug\net10.0\IA.Legatus.dll')) } |
       Select-Object -First 1
   if (-not $proj) {
       Write-Warning 'Legatus project not found under ~/.claude/plugins/marketplaces/*/ui/legatus/. Is the legion marketplace installed?'
       return
   }
   $projDir = $proj.DirectoryName
   ```

   - **No match** (`$proj` is `$null`) → guard **before** touching `$projDir`: print "⚠️ Legatus project not found under `~/.claude/plugins/marketplaces/*/ui/legatus/`. Is the legion marketplace installed?" and **stop** — launch nothing.
   - **Found but not built** (`$projDir\bin\Debug\net10.0\IA.Legatus.dll` absent) → note it: `dotnet run` will build it on the first launch (slower), then continue.

3. **Already running?** Legatus listens on port **5021**. If something already listens
   there, do **not** start a second instance — just reopen the browser and stop:

   ```powershell
   Get-NetTCPConnection -LocalPort 5021 -State Listen -ErrorAction SilentlyContinue
   ```

   If it returns a connection, run `Start-Process 'http://localhost:5021'`, print
   "Legatus is already running — reopened http://localhost:5021", and **stop here**.

4. **Launch detached** — start `dotnet run` as an **independent process** via
   `Start-Process` (not `Start-Job`/`&`, which die with the session), forcing the `http`
   profile (port 5021):

   ```powershell
   $p = Start-Process dotnet -PassThru -WorkingDirectory $projDir `
       -ArgumentList 'run','--project',$projDir,'--launch-profile','http'
   ```

5. **Open the browser (fallback)** — `dotnet run` honors `launchBrowser` only when
   interactive; detached, it may not fire. Give Kestrel a moment to start, then open the
   URL yourself (a duplicate tab is harmless; a missing one is not):

   ```powershell
   Start-Sleep -Seconds 3
   Start-Process 'http://localhost:5021'
   ```

6. **Report** — print the URL and the launch state, e.g.
   "Legatus started (PID $($p.Id)) → http://localhost:5021".
