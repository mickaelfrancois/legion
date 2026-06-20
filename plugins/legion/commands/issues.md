---
description: List the open GitHub issues of the current repo — the pre-THINK entry point to `/legion:recon <n>` and `/legion:battle start <n>`.
argument-hint: (no args)
---

List the **open GitHub issues** of the current repo.

1. **`gh` preflight**: verify that `gh` is available and authenticated.

   ```bash
   gh auth status
   ```

   If `gh` is missing from PATH or unauthenticated, print a clear warning:
   "⚠️ `gh` is required and must be authenticated (`gh auth login`). Command aborted."
   Stop there — do not continue silently.

2. **Fetch**: retrieve every open issue (`gh` defaults to `--state open`, made
   explicit here to remove any ambiguity; `--limit 200` so no issue is hidden).

   ```bash
   gh issue list --state open --json number,title,labels --limit 200
   ```

3. **Render**: print one line per issue, sorted by issue number **descending**
   (`gh`'s native order, restated explicitly). Strict format:

   ```
   #<id>  <title>  [<labels>]
   ```

   - Labels joined by `, ` inside brackets (e.g. `[bug, help wanted]`).
   - No label → empty brackets `[]` (do not omit the brackets, do not crash).

4. **"No open issue" case**: print "No open issue on this repo." This is not an
   error — stop cleanly after the message.

5. **Footer** (always rendered, even on an empty list):

   ```
   To sharpen an issue: /legion:recon <n>
   To start a battle:   /legion:battle start <n>
   ```

   Always in the namespaced `/legion:` form — never the bare `/recon` or `/battle`.
