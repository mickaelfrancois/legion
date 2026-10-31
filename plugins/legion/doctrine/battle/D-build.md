## §D — build a slice (BUILD phase)

Precondition: `transition build in_progress` (below) must succeed. The script checks
that `phases.plan` is `done` with an `accept` / `accept_with_opportunity` verdict and
that the human approved the plan (`approve-plan`, §A.1 step 6). The approval is
required for every exit from `pending` (`in_progress`, `done` and `blocked`). On a refusal, relay the
`reason` and point to PLAN (or, for a missing approval, to §B).

Resolve the target from the argument: a specific `slice-N`, or `all` (every
slice listed in `plan.md`, in order). Default to the slice returned by
`python "<plugin-root>/scripts/battle_state.py" next-slice --battle <id>` (first slice not `done`;
`slice: null` when all are done or none were declared — aggregated build).

**Per-slice state.** When the battle declares slices (`set-slices`, §A.1 step 6), record
each slice: `… battle_state.py slice <id> in_progress --battle <battle-id>` **before** coding or delegating,
then `… slice <id> done|blocked --warnings N --files <paths…>` from the result
`{ slice_id, build_ok, warnings, files_touched }` (`done` if `build_ok`, else `blocked`).
Only the orchestrator writes this state; the builder never touches `battle.json`.
When the `--files` of a `slice … done` match a sensitive path (auth, secrets, dependencies,
endpoints), the script adds `security` to `required_gates` by itself and traces it in
`run.security_auto`; it says so in its `warnings`. **Relay that warning to the user**
(« security ajoutée automatiquement : <files> ») — the gate then runs in §E.
Files touched **outside** a `slice … done` (corrective BUILD, polishing round, §H fixes,
aggregated BUILD) are reported the same way with
`… battle_state.py touch-files --files <paths…> --battle <battle-id>` (same sensitive-path
rule, idempotent, refused with exit `2` on an aborted or closed battle). Relay its `warnings`.

**Mark the phase in progress first.** Before coding or delegating, run
`python "<plugin-root>/scripts/battle_state.py" transition build in_progress --battle <id>`
(this is also the precondition check). The
phase must **never stay `pending`** once a build has started — that's how `/fleet`
and a resumed session see work happening. You will reclassify it at the end.

**Tree integrity check (§E).** Every BUILD is wrapped in it: `tree-snapshot` right after
`slice <id> in_progress` (or after `transition build in_progress` when no slice is
declared), and `tree-verify … --guard` at the end, **before** `slice <id> done|blocked`.
`--guard` reads `allow` / `deny` from the active battle itself; never pass globs on the
command line. A fault → `slice <id> blocked`, `transition build blocked` and **escalation
case 3** with `out_of_scope`. The same wrap applies to every corrective BUILD (§E (d),
§H steps 0 and 3), with `tree-verify --guard` before the commit.

**Mode — inline (default).** Code the slice yourself in this session, applying the
same conventions as the `builder` agent: its internal code rules and xUnit conventions, the repo's neighbouring code and tests
first. Verify with
`dotnet build` from the current directory (worktree mode: `dotnet build "<wt>/…"`, see the block « Worktree mode — working from the main checkout »; or `dotnet build <stack.build_target>`
when `battle.json.stack.build_target` is set — repo without a `.sln`); **note the
warning count** from the build summary. For `all`, build **each slice in order**,
collecting a `{ slice_id, build_ok, warnings }` per slice and recording it with `slice …`
as above. On a **bug battle** (`kind: "bug"`), the slice that carries the regression row of
the matrix is built **test first**: write the regression test, run it on the unfixed code and
see it fail on its own assertion (exit ≠ 0, not an import or collection error), note `HEAD`
(`git -C "<wt>" rev-parse HEAD`), then fix and run it green. Record this in a
`### Test de régression` section of the slice report: test name, command, `HEAD` sha of the
red run, an excerpt of the red output, an excerpt of the green output. Write the slice report yourself: `build-report-<slice-id>.md` in the battle dir (same format as the builder's: starts with `## <slice-id>`, no `#` H1, optional trailing `## Hors périmètre — candidats issue` section), one file per slice. This keeps a mix of `build slice-1 --auto` and inline `build slice-2` correct. Consolidation into `build-report.md` happens once, in the classification below.

**Mode — `--auto`.** Delegate to the `builder` agent via the `Agent` tool
(`subagent_type: builder`). Pass a self-contained prompt: battle dir, `plan.md`
path, `slice_id`, the `guard.allow` globs from `battle.json`, and — when set —
`stack.build_target` (the explicit build target for a repo without a `.sln`), and on a bug
battle the input « Battle de bug » (the builder does not read `battle.json`; it then builds the
regression slice test first and fills `### Test de régression`). Pass the battle
dir and the `plan.md` path as **absolute paths in the main repo** (`<main>/.legion/battles/<id>/`,
`<main>` = the main checkout). A sequential builder writes its own `build-report-<slice_id>.md` at that absolute
path, never into the `.legion/` of a worktree. An isolated (parallel) builder writes **no** report file (the
harness refuses it any write outside its worktree): it returns its report in its final message, between
`<<<BUILD-REPORT <slice_id>>>>` and `<<<END BUILD-REPORT>>>`, and you write the file at step 2. The hooks and the
state CLIs (`battle_state.py`, `artifact_check.py --guard`) resolve the battle from the main
repo (`git rev-parse --git-common-dir`). Each slice owns a distinct report file, so parallel builders never race on a shared file; the
missing-report check is done by `merge-reports` (below). Tell each builder it must not write another slice's report. For `all`,
dispatch independent slices in parallel with `isolation: worktree`; keep
dependent slices sequential. **Sequential builders** are wrapped in the tree integrity
check with `--guard` (above). In worktree mode (`battle.json` has a `worktree` block) a
sequential builder gets the **Code root** = `worktree.path` (it runs its tools from there with
their directory option, never from the cwd), and still the battle dir and `plan.md` as
absolute paths under `<state>`. **Parallel builders** write in their own worktree, so their
deltas must be merged back into the **integration tree** (the **fan-in**, `scripts/fan_in.py`,
the only script that merges builder code into the integration tree; `release_prep.py` writes only
the CHANGELOG and one manifest at DELIVER). The integration tree is
`worktree.path` in worktree mode, the main checkout otherwise. Every `fan_in.py` call carries
`--battle <id>`; in worktree mode `base`, `apply` and `cleanup` also carry
`--root "<wt>"` (`<wt>` = `worktree.path`, the integration tree; `<builder-wt>` = a builder's own worktree, never passed to `--root`). Run them from the main checkout. The
builders' own worktrees (`agent-*`) always sit under `<main>/.claude/worktrees/`, next to the
battle's worktree, which `fan_in.py` and `tree-verify` refuse as a builder worktree. A
worktree-mode builder gets **no** Code root: its code lives in its own worktree. Run a
parallel batch in this fixed order:

1. Call `slice <id> in_progress` for **every** slice of the batch. Then freeze the integration
   tree as the batch base: `<base>` = the `base` field of
   `python "<plugin-root>/scripts/fan_in.py" base --battle <id>` (worktree mode:
   `… base --battle <id> --root "<wt>"`; validate `^[0-9a-f]{40}$`). It commits
   the integration tree as it stands, uncommitted and untracked files included (`.legion/`,
   `.claude/worktrees/` and ignored files excluded), into an unreferenced commit on top of
   its `HEAD`; on a clean tree it returns `HEAD` itself. It touches no ref, no `HEAD` and no index.
   It refuses (exit 2) on a nested repository. Never use `git rev-parse HEAD` here: a foundation
   slice that is still uncommitted would be invisible to the builders. Then take the snapshot S1
   of the integration tree: `tree-snapshot --battle <id> [--root "<wt>"] --out <state>/.legion/battles/<id>/_tree-before.json`. No
   `battle_state.py` write between this snapshot and step 4.
2. Launch the builders in parallel (`isolation: worktree`). Each builder prompt carries
   `<base>`, the battle id and the absolute path of `fan_in.py`: the builder's first action is
   `fan_in.py align --base <base> --battle <id>`, which moves its worktree onto `<base>`
   (`reset --keep`, the harness branch is kept). If `align` fails the builder returns
   `build_ok: false` and stops. **If `align` refuses** (for example the worktree `HEAD` is not an
   ancestor of `<base>`), run the batch sequentially instead: no workaround.
   **On each builder's return, write its report right away.** Extract the lines between the
   line `<<<BUILD-REPORT <slice_id>>>>` and the **first following line** that is exactly
   `<<<END BUILD-REPORT>>>` (each marker alone on its line; check the id in the marker
   matches the slice) and write them **verbatim** with the Write tool to
   `<main>/.legion/battles/<id>/build-report-<slice_id>.md` (overwriting any earlier version;
   never edit or reword it). Then check it with
   `python "<plugin-root>/scripts/artifact_check.py" verify <path>` (`exists` +
   `non_empty`). This write touches neither `battle.json` nor `.legion/active-battle`, which
   `tree-snapshot` excludes from its paths, so it is allowed between S1 and step 4 (it is not a
   `battle_state.py` write). Error cases: a block that is absent or empty means the report is
   absent from the builder's return: write a minimal `## <slice_id>` report yourself
   (`build_ok`, `warnings`, files from the return, residual
   "rapport absent du retour du builder"), relay a warning and flag it for the REFLECT. If your
   Write is refused or `verify` fails, retry the write once, then fall back to the same minimal
   report; if that also fails, the slice has no report and `merge-reports` (exit 2) blocks it
   downstream. A corrective BUILD of an isolated builder returns the **full** report (old text
   plus `### Correction (<gate>)`), so overwriting the file loses nothing. A return that is unreadable (no
   `build_ok`) is treated as `build_ok == false` (step 5). A non-empty `write_failures` is relayed
   to the user; with `build_ok: true` it is a non-blocking warning, flagged for the REFLECT.
3. For each builder take its worktree path from `git worktree list --porcelain` (never from
   the builder's returned text), check it matches `^[A-Za-z0-9._/:\\ -]+$` (no `$`, backtick
   or `"`), then run `artifact_check.py tree-verify --base <base> --battle <id> --root "<builder-wt>" --guard`
   (the script also checks the path against `git worktree list`). In `--base` mode it also
   requires `<base>` to be an ancestor of the worktree `HEAD` (fault `[base]`): this proves the
   builder aligned. It follows the same path as any other fault of this step (step 5).
4. On the integration tree run
   `tree-verify --battle <id> [--root "<wt>"] --before S1 --fingerprint F1 --batch-worktrees`
   **without a filter**: no isolated builder may touch the integration tree, and a fault is charged to
   the whole batch. The fingerprint includes the list of registered worktrees and every branch
   except those checked out in a registered worktree, so a worktree that appeared or disappeared
   is a `[git-state]` fault. `--batch-worktrees` is the single, fixed exception: it accepts only
   worktrees that **appeared since the snapshot**, sit under `<main>/.claude/worktrees/` (the
   main checkout's folder, also in worktree mode), are not `prunable`, are neither the
   integration tree nor the worktree of a known battle, and whose admin entry and `.git` file
   point at each other. Never weaken this
   verify; it precedes any fan-in write. Leave the harness worktrees in place until the fan-in
   (never remove them earlier: their branches would then read as new branches); never pass the
   flag for a gate or a sequential builder.
5. A fault in step 3 or 4, or any red builder (`build_ok == false`): **no fan-in**. Record
   `slice <id> blocked` for **every** slice of the batch, then `transition build blocked`; apply
   escalation case 3 for a fault, otherwise the auto-correction loop (see below). The worktrees
   are kept.
6. Take a new snapshot S2 (same `_tree-before.json`, overwritten; worktree mode: with
   `--root "<wt>"`). **No `battle_state.py` write between S2 and step 8.**
7. Run `python "<plugin-root>/scripts/fan_in.py" apply --base <base> --battle <id> --slice <id> "<builder-wt>" …`
   (worktree mode: add `--root "<wt>"`; one `--slice <id> "<builder-wt>"` pair per slice,
   paths taken from `git worktree list --porcelain` and validated as in step 3). It computes each worktree's delta
   against `<base>` (builder commits and untracked files included, ignored files excluded),
   checks every path against `guard.allow` and `.legion/`, checks the whole batch for conflicts
   (`overlap`, `dirty`, `apply`), then writes everything with one `git apply` (working tree only;
   HEAD and index untouched). It is all-or-nothing and writes neither `battle.json` nor anything
   under `.legion/`. It processes slices in `battle.json.slices` order. Exit 0: JSON
   `{ ok, applied:[{ slice, worktree, committed, files:[{ status, path }] }] }`. Exit 2: JSON with
   `refused` + `out_of_scope`, or `conflict:{ slice, kind, files }`, or `fault`; the integration tree is
   unchanged. Exit 1: usage error.
8. Run `tree-verify --battle <id> [--root "<wt>"] --before S2 --fingerprint F2 --guard` on the
   integration tree (no `--batch-worktrees`: the worktrees did not move). A fault: `slice <id> blocked` for every
   slice of the batch, then `transition build blocked`, escalation **case 3**. The worktrees are
   kept (no `cleanup`), and the changes `apply` wrote stay in the working tree for diagnosis.
9. On any exit 2 of `apply` in step 7 (a conflict, a refusal or a fault): `slice <id> blocked`
   for every slice of the batch, then `transition build blocked`. A conflict is escalation
   **case 7** (relay the slice, the `kind` and the files); an out-of-scope refusal or a fault is
   **case 3**. The integration tree is unchanged and the worktrees are kept.
10. On success: `slice <id> done --warnings N --files <files from the apply JSON>` for each
    slice. The `--files` list comes from the tool, not from the builder's return. Invariant:
    a slice is `done` only when its code is in the integration tree. A slice whose worktree the harness
    removed (no change) skips `apply`: record it `done` with no files; `merge-reports` still
    requires its report, which you already wrote at step 2.
11. Verify the project **once** for the whole batch, on the integration tree (worktree mode: on
    `<wt>`, with the tool's directory option, see the block « Worktree mode — working from
    the main checkout »): the stack's build and tests (.NET:
    `dotnet build` + `dotnet test`; other stack: the repo's commands). If red:
    `transition build blocked`, escalation **case 7**, worktrees kept.
12. If green: `python "<plugin-root>/scripts/fan_in.py" cleanup --base <base> --battle <id> --slice <id> "<builder-wt>" …`
    (worktree mode: add `--root "<wt>"`; same pairs). It removes each worktree and its harness
    branch only after proving the integration tree already holds the delta; it never removes the
    battle's branch (`branch_kept`). Otherwise it keeps them (`kept`, exit 2). A refusal is
    **non-blocking**: relay it as a warning and flag it for the REFLECT.
    **Resuming an interrupted batch.** `<base>` is held only in the orchestrator's context and is
    no longer `git rev-parse HEAD` of the integration tree. A builder that aligned and did not commit has
    `HEAD` = `<base>` in its worktree: read it there (`git -C "<builder-wt>" rev-parse HEAD`). If no
    aligned worktree is left, re-run the batch from step 1.
13. Continue with the classification below: `merge-reports`, then `transition build done`.

Collect each `{ slice_id, build_ok, warnings, files_touched, write_failures }` from the builders. For a
sequential builder, call `slice <id> in_progress` before delegating, and on its return
`slice <id> done|blocked --warnings N --files …` with that result; a red build →
`slice <id> blocked`, then `transition build blocked`. A sequential builder writes its own
`build-report-<slice_id>.md`; an isolated builder's report is written by you at step 2.

After build (either mode), **classify the result and record it immediately** with
`transition` — for `all`, only after **every targeted slice** has a result:
- **any `build_ok == false`** → `slice <id> blocked` (if slices are declared — on a
  slice's own build only; a corrective BUILD from §E (d) makes no `slice …` call), then
  `transition build blocked`; relay the
  residual errors. En mode `autonomous`, entrer dans la boucle d'auto-correction
  (§E — boucle de `revise`), en commençant par
  `python "<plugin-root>/scripts/battle_state.py" bump-autocorrect build --fails '<json array of the residual errors>' --battle <id>`
  (`continue` ⇒ relancer le build : `transition build in_progress`, puis
  `slice <id> in_progress` ; `escalate` ⇒ escalade, cf. §E) ; en mode `step`,
  stop. Do not advance until resolved.
- **all `build_ok` (with or without warnings)** → **consolidate the reports first**, then
  `transition build done` **only when
  every declared slice is `done`** (the script refuses otherwise and cites the remaining
  ids). With `build slice-N` while other slices are still `pending`, leave `build`
  `in_progress` and announce the next slice (`next-slice`): in `autonomous` mode chain
  straight into it, in `step` mode hand back.
  Consolidation: once every targeted slice is `done` (and only then, before `transition build done`,
  in every mode; for a parallel batch, after the fan-in `cleanup` of step 12), run
  `python "<plugin-root>/scripts/battle_state.py" merge-reports --battle <id>`. It assembles `build-report.md`
  from the `build-report-<id>.md` files in the `battle.json.slices` order (plan order), with one
  final grouped `## Hors périmètre — candidats issue` section; it never writes `battle.json` and is
  idempotent. With no declared slice (aggregated BUILD) it only checks that `build-report.md` exists;
  in that case, also run `touch-files --files <files_touched of the builder> --battle <id>` before
  `transition build done`, so a sensitive file still adds `security`.
  **Bug battle** (`kind: "bug"`): before `transition build done`, read the
  `### Test de régression` section of the slice report. Check the test name against
  `^[A-Za-z0-9_.:/\[][A-Za-z0-9_.:/\[\]-]{0,199}$` (never a leading `-`) and the sha against `^[0-9a-f]{40}$` (they come from an agent
  report: never pass an unchecked value to the shell), then run
  `python "<plugin-root>/scripts/battle_state.py" set-meta --regression-test <name> --regression-red-sha <sha> --battle <id>`.
  When the plan's regression row is an approved `acceptation humaine différée`, run
  `set-meta --regression-waiver "<reason copied from plan.md>" --battle <id>` instead (no `"`,
  `` ` `` or `$` in it). Never grant a waiver the plan does not carry. `transition build done`
  is refused (exit `2`) while no declaration exists: relay the `reason`; a missing section or a
  value that fails its check means the BUILD is incomplete (`transition build blocked`).
  On **exit 2** (`missing: [ids]`, or a blank report): for a slice of a parallel batch, do not re-run
  its builder (its worktree may be gone): apply the step 2 fallback (minimal report). Otherwise re-run
  the builder of each missing slice for its report only, or write that `build-report-<id>.md` yourself (inline); if it is still missing,
  `slice <id> blocked` then `transition build blocked`. Relay the script's `warnings` (injected
  title, undeclared extra report) to the user. For a legacy battle (slices `done` before this
  flow, no per-slice report), split `build-report.md` into per-slice files yourself, then re-run.
  Warnings are **non-blocking remarks**: log them (in `build-report.md` and in the
  relay to the user), then **auto-advance straight into §E** (the gate cascade)
  without waiting for a separate command — exactly as for a clean build. Announce
  the chaining and the warning count so the user sees them. En mode `step`, rendre
  la main après avoir annoncé les warnings, sans enchaîner automatiquement.
  **Reset point T2**: right after `transition build done` and before the first gate of §E, apply
  the « Reset de contexte » section of the core file. Never after a `slice <id> done`.

Recording the phase status is **not optional**: a build that produced code but
left `phases.build.status` at `pending`/`in_progress` is a bug — always run the
final `transition build done|blocked` before handing back.

The orchestrator sequences `builder → gates` — the builder never calls a gate.
