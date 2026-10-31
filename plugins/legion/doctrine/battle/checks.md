*Shared checks of §E. Read this part together with `E-gates.md`; `A-start.md`, `D-build.md` and `H-address.md` also refer to it.*

### Tree integrity check (shared — every gate and every builder)

A gate is read-only on the code, a builder writes inside `guard.allow`. The `guard.py`
hook filters the obvious Bash writes, but it cannot see everything (`python -c`,
`bash -c`, `eval`). This check is the guarantee: it fingerprints the working tree
(`HEAD`, `git status` entries with content hashes, protected battle state, index masks,
the local git config, the git hooks and `info/attributes`) before and after the agent, and any difference the agent was not allowed to make is a
fault. `<id>` is the validated battle id, never `$ARGUMENTS`.

1. **Snapshot.** After `transition <phase-key> in_progress` (or `slice <id> in_progress`)
   and **before** `Agent`, run
   `python "<plugin-root>/scripts/artifact_check.py" tree-snapshot --battle <id> --out "<state>/.legion/battles/<id>/_tree-before.json"`.
   **Worktree mode:** add `--root "<worktree>"` (`<worktree>` = `worktree.path`) so the snapshot
   covers the battle's worktree, not the main checkout; run it from the main checkout.
   Keep the `fingerprint` it prints; it must match `^[0-9a-f]{64}$`. A refusal (exit `2`)
   → do not launch the agent, **escalation case 5**.
2. **Verify.** After the return and **before** the delivery check (below), run
   `artifact_check.py tree-verify --battle <id> --before "<state>/.legion/battles/<id>/_tree-before.json" --fingerprint <fingerprint>`.
   **Worktree mode:** add `--root "<worktree>"` here too. Gates receive the **Code root**
   = `worktree.path` and still write their artifact under `<state>/.legion/`.
   For a builder (or any BUILD), add `--guard`. Passing the fingerprint back protects the
   snapshot file against a rewrite: a file that no longer matches is refused.
3. **On a fault** (`fault: true`, exit `2`): the gate's verdict **does not count**. Run
   `transition <phase-key> blocked` **without a verdict**, no retry, no restore, and
   **escalation case 6** with `changed` / `out_of_scope` (for a builder: case 3). On a
   **refusal** (`refused: true`): same blocking, **escalation case 5**.
4. **Re-run.** If the delivery check re-invokes a gate, take a **new** snapshot first.
5. **Transient file.** `_tree-before.json` is overwritten on the next pass, like
   `_threads.json`, and stays git-ignored with `.legion/`.
6. **Serial gates.** Gates run one after the other, so a fault is charged gate by gate. A
   parallel batch of gates shares one snapshot (taken after **all** their `in_progress`
   calls) and one `tree-verify`; a fault is charged to the whole batch.
7. **Human edits.** A file the human changes during a gate (IDE, format on save) is a
   fault charged to the gate. The escalation message says so; the path list settles it.
8. **Build outputs.** `bin/`, `obj/`, `TestResults/`, `coverage/` must be git-ignored,
   otherwise `lint` / `test-engineer` fault. The escalation then advises completing the
   `.gitignore`.
9. **Never write `battle.json` between `tree-snapshot` and `tree-verify`.** The protected
   state with `--battle <id>` is the `battle.json` of **that** battle and the session bindings
   that point at it; the `.legion/active-battle` pointer is not hashed, and the worktrees,
   branches and `branch.<b>.*` config keys of the **other** battles are excluded, as is
   `refs/remotes/**`. So any `battle_state.py` call on this battle in that window
   (`transition`, `slice`, `verdict`…) raises a false fault, while another session's
   `activate` or commits do not. For a batch of gates run in parallel, take the snapshot **after** the
   `in_progress` calls, run the `tree-verify` for the batch, and record **all** the
   verdicts (`verdict …` / `transition …`) only **after** it. The same order holds for
   builders: `tree-verify` first, then `slice <id> done|blocked`.

### Gate artifact delivery check (shared — every gate, incl. `architect` and `pr-triage`)

A gate now writes its own artifact, so a returned verdict no longer **proves** the
artifact exists. A verdict counts **only if its artifact was written this pass**.
Wrap every gate invocation with this check — it is deterministic (metadata only, you
never read the artifact's content, which would refill the context the confinement
spares). It runs through `python "<plugin-root>/scripts/artifact_check.py" …` (fall
back to `python3`); the script prints one JSON object on stdout and works the same on
Windows, Linux, WSL and macOS:

1. **Before** invoking, resolve the expected artifact path
   `<state>/.legion/battles/<id>/<artifact>` (architect → `plan.md`, lint → `gate-lint.md`,
   reviewer → `gate-review.md`, test-engineer → `gate-test.md`, security →
   `gate-security.md`, pr-triage → `pr-feedback.md`) and, **if it already exists** (a
   re-loop round),
   capture its current modified-time with
   `artifact_check.py snapshot <expected>` → `{exists, mtime_ns, size}`. Keep `mtime_ns`
   only if `exists` is true; otherwise pass no `--since` in step 3.
2. The gate returns `VERDICT … ARTIFACT: <path>`.
3. **After** the return, run
   `artifact_check.py verify <expected> [--since <mtime_ns>] --returned <ARTIFACT: path>`.
   It checks **all four** points below — metadata only, no content read — and answers
   `{ok, checks:{exists, non_empty, canonical, fresh}, reason}` (exit `0` if `ok`, exit
   `2` if not). **Always pass `--returned`**, or the `canonical` check is skipped.
   - the file at the expected path **exists**;
   - it is **non-empty** (`size > 0`). A gate can return a verdict
     yet leave a **0-byte** artifact (the `Write` never landed, or wrote nothing); such
     an empty file still **exists**, so the existence check alone would wave it through.
     (RETEX: an empty `gate-review.md` would have passed the literal check.) The
     `guard.py` hook now **also** blocks a gate's blank `Write` upstream (defense in
     depth); this orchestrator check still catches the case where the gate wrote
     **nothing at all** (no `Write` for the hook to intercept).
   - the returned `ARTIFACT:` path **equals** the expected canonical path (the guard
     already blocks a wrong *write*; this catches a wrong path in the *returned* string);
   - it was **written this pass** — it either did not exist before, or its
     modified-time is now **strictly newer** than the value captured in step 1 (so a
     stale artifact from a previous round is never mistaken for a fresh one).
4. **On any failure** (`ok:false` or exit `2`) → do **not** record the verdict and do **not** advance.
   Re-invoke the gate **once** with an explicit reminder ("write your artifact to
   `<exact path>` first, then return your verdict"). If it still fails → run
   `transition <phase-key> blocked` (no verdict), surface it to the user, and stop. **Never advance the pipeline
   on a verdict whose fresh artifact you could not confirm.**

### Non-.NET stack — gate adaptation

When the battle is flagged **non-.NET** (§A.preflight stack detection), the default
.NET assumptions do not hold. For **every** gate prompt (here in §E and the
`architect` gate in §A.1), make the deviation explicit so the subagent does not
fall back to .NET tooling:

- **Drop the .NET tooling assumptions** from the prompt: no native C# `LSP`,
  no `dotnet build` / `dotnet test`. State the repo's **actual** build/test/lint
  commands instead (read them from `package.json` scripts, `Makefile`, CI config —
  whatever the repo uses) and pass them in the prompt.
- **Tell the subagent to skip its .NET-specific rule lists** (RETEX: subagents
  applied .NET rules for lack of an equivalent). Instruct it to reason from the
  repo's own conventions and the generic review/test lenses, not a .NET ruleset.
- **Keep the verdict contract unchanged** (`accept` / `accept_with_opportunity` /
  `revise` / `reject`) — only the *toolchain* the gate reasons over changes, not the
  cascade or how you persist it.

The orchestrator owns this: the agents default to .NET; it is the gate prompt that
carries the non-.NET deviation per battle.
