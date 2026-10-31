## §G — deliver (branch, commit, push, PR) — final step

Precondition: `build` `done` (whatever the profile, `spike` included: without a BUILD
there is nothing to commit) and every required review/test/security gate `done`. Enter
the phase with `battle_state.py transition deliver in_progress --battle <id>` — the script checks
`build` and every required gate and **refuses** otherwise (exit `2`: relay the `reason`, do not
push). This step **writes and pushes**. En mode `autonomous` (chemin heureux), la PR est composée, poussée et
ouverte **sans OK bloquant** — l'humain relit le code sur GitHub. Les filets §G.0
(ci-dessous) sont la **dernière barrière** : chacun, s'il se déclenche, **escalade**
(cas 4 de la taxonomie — §F). En mode `step`, le comportement historique est
maintenu : afficher l'effet sortant et attendre un OK explicite (§G.4 ci-dessous).
Once the PR is open, hand back: the human reviews it. New review comments are handled
by `/legion:battle address` (§H, repeatable); when the PR is stabilized,
`/legion:retro` closes the battle.

**Worktree mode** (`battle.json` has a `worktree` block, `<worktree>` = `worktree.path`): the
session stays in the main checkout, so every git command of this section targets the worktree:
`git -C "<worktree>" add`, `git -C "<worktree>" commit`, `git -C "<worktree>" push`,
`git -C "<worktree>" diff` / `status` / `rebase`, and
`base_freshness.py --repo "<worktree>"`. The PR is opened with `gh pr create --head <worktree.branch>`.
The branch of the main checkout never moves. `git fetch origin` can run from either checkout; with concurrent battles use `git fetch --no-tags origin` (a tag fetched during a gate is a `[git-state]` fault).

0. **Pre-branch safety nets.** Before branching, two checks:

   a. **Base freshness** — the gates must have judged the base you actually ship.
      **Deterministic helper:** after `git fetch origin`, run
      `python "<plugin-root>/scripts/base_freshness.py" --touched <files from build-report.md>`
      — it returns a JSON verdict (`fresh` / `waive_pure_merge` / `waive_disjoint` /
      `regate`) computed by the git logic below, so you don't judge it by hand. The rest
      of this item explains what each verdict means and how to act on it.
      Fetch and compare HEAD against the remote default branch: `git fetch origin`,
      then `git rev-list --count HEAD..origin/<default>` (resolve `<default>` via
      `git symbolic-ref refs/remotes/origin/HEAD`, fallback
      `gh repo view --json defaultBranchRef`). **> 0 → the local base is behind
      origin.** But "behind by N commits" is **not** the same as "the shipped tree
      differs": a pure merge commit (the branch was already merged elsewhere) leaves
      origin's tree **byte-identical** to yours. So before forcing a full re-run, **test
      the actual tree delta**, not just the commit count —
      `git diff --quiet HEAD origin/<default>` (exit 0 = identical tree):

      - **Tree delta empty** (`git diff --quiet` exits 0) — the divergence is commits
        only (a pure merge); the tree the gates judged is byte-identical to the base you
        ship. **Waive the re-gate** — a re-run would re-test the exact same code. Run a
        **sanity `dotnet build`/`dotnet test`**, rebase the work onto `origin/<default>`,
        then continue to step 1. (RETEX: HEAD was 1 pure-merge commit behind with an
        empty tree diff — a full re-gate would have judged byte-identical code.)
      - **Tree delta non-empty** — origin changed the shipped base. Compare the **base
        delta** (`git diff --name-only HEAD origin/<default>`) against the files this
        battle touched (`build-report.md`):
        - **Disjoint** — the incoming delta touches **no** file the slice changed or
          depends on (e.g. an unrelated `.gitignore` or docs commit). The gated surface
          is unaffected, so you **may skip the full re-gate**; **document the
          justification** (the disjoint delta file list) in the relay, rebase onto
          `origin/<default>`, then continue. (RETEX: the net forced a full re-run on a
          base delta that was a single orthogonal `.gitignore` commit.)
        - **Intersecting** — the delta touches a file the slice changed or depends on
          (work merged under another SHA, a dependency/SDK migration): **stop, integrate
          first** (rebase or merge origin), then run
          `battle_state.py invalidate --reason rebase --battle <id>` (the verdicts are stale) and
          **re-run BUILD + the whole required cascade from `lint` on the updated base**
          before delivering. A rebase changes what ships, so gate
          verdicts on the stale base do **not** carry over. (RETEX: a base behind
          origin's default was only caught at deliver, after the gates had validated a
          base that wasn't shipped.)

   b. **Empty remote** — the flow assumes the remote already has a base branch.
      Check `git ls-remote --heads origin` — **no heads** means an uninitialized
      repo. Delivering a feature branch into it makes that branch the **default**,
      so the PR's source == target → `gh pr create` fails (HTTP 400), no PR
      possible. If the remote is empty, **stop the normal flow** and offer a choice
      (do **not** branch yet):
      - **Bootstrap a base** — create an initial commit (empty is fine) on the
        default branch, push it (`git push -u origin <default>`) so it becomes the
        default, then replant the feature work on `<me>/<token>` and continue from
        step 1.
      - **Import directly on the default branch** — push the work as the default
        branch (no PR for this first drop; the repo gets its history). Subsequent
        battles deliver normally.

      **Do not script the default-branch switch** — flipping a repo's default
      branch is a **repo-admin UI action** on GitHub; surface it to the user rather
      than attempting it by script.

1. **Branch name.**

   **Worktree mode** (`battle.json` has a `worktree` block): the branch already exists, the
   script made it at start. **Create no branch and run no `checkout`.** Run
   `python "<plugin-root>/scripts/battle_worktree.py" where --battle <id>` before the commit and require
   `ok: true` (the worktree is on `worktree.branch`; read `reason` and `worktree_branch` otherwise).
   Otherwise **escalate (case 4)**: the worktree is not on the battle's branch, never switch it.
   Commit and push with `git -C "<worktree>"` on `worktree.branch`; the branch of the main checkout is never touched. Skip the rest of
   this step and go to step 2.

   **`--in-place`** (no `worktree` block): the rule below, unchanged. `<me>/<token>`: `<me>` from `git config user.email` (local
   part before `@`; fallback `user.name`), `<token>` from `battle.json.ticket`
   (`GH#<n>` → `<n>`) or the battle slug if no issue. Check first with
   `git rev-parse -q --verify refs/heads/<me>/<token>`:
   - **absent** → create it: `git checkout -b <me>/<token>`;
   - **present and `battle.json` `delivery.pr_url` is null** → an interrupted DELIVER of this
     battle (branch created or commit made, push/PR not yet) and a stale/planted branch look
     alike, so decide **deterministically**, with `<b>` = `refs/heads/<me>/<token>` and `HEAD` =
     the current commit, taken **before** any checkout:
     - `git rev-parse <b>` **equals** `git rev-parse HEAD` → the branch holds no commit of its
       own (created, nothing committed yet): resume with `git checkout <me>/<token>`;
     - `git rev-list --parents -n1 <b>` prints **exactly two** fields (the commit and a single
       parent) and the second one **equals** `git rev-parse HEAD` (exactly one non-merge commit
       on top of the base; a merge commit is refused, since `git diff-tree` lists none of its
       paths and the whitelist would pass empty) **and** every path of `git diff-tree --no-commit-id --name-only -r <b>` is on the
       commit whitelist of step 2 **and** `git diff --quiet <b> -- <those paths>` exits 0 (the
       committed content is byte-identical to the working tree the gates judged): the commit
       step already ran, resume with `git checkout <me>/<token>` and skip to step 3 (push);
     - **anything else** → **refuse and escalate (case 4)**: a stale branch, or a ref planted
       by a gate (the tree fingerprint reports a new branch as `[git-state]`, but a branch that
       predates the snapshot is not seen). Never reuse it, never `git branch -f` over it; the
       user removes or renames it.
     No state field records the branch: the rule reads only git facts, so a resumed DELIVER
     needs no extra write, and a planted commit can only pass by carrying content identical to
     what was already verified.
   - **present and `delivery.pr_url` is set** → this battle's own delivery being resumed:
     `git checkout <me>/<token>`.
   Never `cd`.

2. **Commit** — stage the **code/test changes only**, by an **explicit whitelist
   of paths** (e.g. `git add src/… tests/…`). **Never `git add -A`/`.`** and never
   trust the `.legion/` ignore: as a defensive net, **unstage the battle dir
   unconditionally before committing**: `git reset -q HEAD .legion` (and verify
   with `git status` that no `.legion/` path is staged).

   **Resolve the `.gitignore` change `start` may have introduced.** If §A.1 added a
   `.legion/` line to the repo's `.gitignore`, that is a pending working-tree change
   the issue did not ask for — and it falls **outside** the whitelist/`git reset`
   discipline above (it is not a `.legion/` path). Decide its fate **explicitly**,
   do not leave it dangling: either **commit it first** as a standalone
   `chore: ignore .legion battle artifacts`, or **add `.gitignore` to the whitelist**
   of this commit (it is repo hygiene this battle introduced). Confirm the choice
   with the user. (RETEX: this self-induced change was ambiguous at commit time.)

   **`<summary>` — English, Conventional Commits format** `type(scope): subject`
   (imperative mood, no trailing period, ≤ ~70 chars). `type` ∈
   `feat|fix|refactor|perf|docs|test|build|ci|chore`; `scope` = the touched area.
   This subject feeds **both** the commit subject **and** the PR title (step 5).

   **CHANGELOG and version bump (release_prep).** Right after `<summary>` is fixed (and after
   the §G.0 rebase, so the base read is the one that ships), run the deterministic helper. It
   derives the level from the `<summary>` type (`feat` → minor, `fix`/`perf` → patch, `!` or a
   `BREAKING CHANGE:` footer → major; `docs`/`chore`/`test`/`refactor`/`build`/`ci` → nothing),
   then writes a `CHANGELOG.md` entry under « Unreleased » and bumps the one recognized manifest
   version. It never creates a `CHANGELOG.md` and never calls the network.
   If the commit body carries a `BREAKING CHANGE:` footer, write the body to
   `<state>/.legion/battles/<id>/commit-msg.txt` **before** this call, so the footer can be read.
   Never let a backtick or `$(` from the title reach the shell (pass it escaped or through a file).
   ```bash
   python "<plugin-root>/scripts/release_prep.py" apply --repo "<worktree>" --title "<summary>" --issue <n> --body-file "<state>/.legion/battles/<id>/commit-msg.txt"
   ```
   `--repo` is the worktree root; under `--in-place` it is the root of the checkout. `--issue`
   is for a numeric issue only, and `--body-file` only if a commit-message body exists (the
   `BREAKING CHANGE:` footer is read there). Read the JSON on stdout:
   - **`stage[]`** → add these paths to this commit's path whitelist. This is `stage`, not
     `written[]`: on a resumed DELIVER `written` is empty but `stage` still lists both files, so
     they stay in the same commit (the `--in-place` resume rule above needs one commit whose
     paths are all whitelisted).
   - **`notes[]`** → copy these lines into `pr-body.md` (step 3).
   The script writes nothing if the PR diff already touches the CHANGELOG or the manifest
   (`skipped_foreign`), and it is idempotent on a resume. The helper writes from the main
   session, outside the `guard.py` filter, over a closed set of paths. A slice of the plan that
   edits a manifest by hand still goes through `/legion:freeze <current globs> <manifest glob>`.
   §H (`address`) does **not** re-run `release_prep`: a fix commit after DELIVER never raises
   the bump question a second time.
   For a PR grouping several battles, run this step **once**, on the battle that delivers the
   PR, and give the grouped `<summary>` the type of the highest level (breaking > feat >
   fix/perf).

   **Before committing** (defense in depth, see §E): refuse if a merge/cherry-pick/revert is
   pending — `git rev-parse -q --verify MERGE_HEAD`, `CHERRY_PICK_HEAD` or `REVERT_HEAD`
   answering means a stray state file would turn the commit into a merge commit with an
   unexpected parent. Escalate; never commit through it.

   Then commit with the session's co-author trailer:
   ```
   <summary>

   <1–3 lines: what & why, from spec.md>

   <co-author trailer>
   ```

   `<co-author trailer>` is the attribution line **this session** prescribes (the
   harness attribution instructions, or the user's `CLAUDE.md` / memory rule, which
   take precedence). **Never hard-code a model name** here — it goes stale at the next
   model change. If the session prescribes no trailer, omit it.

   **Where the commit-message file lives (if you use one).** A multi-line body with
   accents or other non-ASCII punctuation is fragile through `-m` on a Windows console;
   writing the message to a file and committing with `git commit -F <file>` is more
   robust. When you do, that file **must live under `<state>/.legion/battles/<id>/`** (e.g.
   `<state>/.legion/battles/<id>/commit-msg.txt`) — **never outside the repo** (no
   `$CLAUDE_JOB_DIR/tmp/…`). Reason: once a write scope is armed (`/freeze` or
   `/guard`), the `guard.py` hook blocks every write outside it, while `.legion/**` is
   always allowed and git-ignored — so a scratch message file there is writable yet
   never committed (the path whitelist + `git reset -q HEAD .legion` above already
   guarantee it). `git commit -m` stays fine for a short one-line subject.

3. **Compose the PR body** → write `<state>/.legion/battles/<id>/pr-body.md` from the
   artifacts: intent/scope (`spec.md`), approach (`plan.md`), and the gate verdicts
   (review/test/security `accept`). **Written in French** (identifiers & file names
   stay English). Apply the **writing charter**
   (`battle-workflow` § « Charte de style des documents ») — simple, precise language;
   **keep it synthetic, with no separate « En bref »** (the body is short by design),
   and reread it against the charter before pushing. Also copy the `notes[]` lines returned by `release_prep.py` (step 2), if any. For a
   numeric issue, **end the body with `Closes #<n>`** so
   merging the PR auto-closes the issue. **Exception, profile `spike`:** end with
   `Refs #<n>` instead, so merging never closes the issue. This is the payoff of the artifact
   pipeline — the PR documents itself.

4. **CONFIRM (mode `step` uniquement)** — En mode `step`, avant de pousser, afficher
   à l'utilisateur **tout l'effet sortant** : branche cible, message de commit,
   `git diff --stat`, titre/cible de la PR, **et le commentaire d'issue** (§G.6, pour
   une issue numérique). **Attendre un OK explicite** — un seul OK couvre alors push +
   PR + commentaire d'issue, sans re-solliciter l'humain à l'étape 6. Ne pas pousser
   avant.

   En mode `autonomous`, sauter ce CONFIRM : passer directement à l'étape 5. Les
   filets §G.0 sont la seule barrière — la discipline de staging (whitelist de chemins,
   `git reset -q HEAD .legion`) reste intacte et **non affaiblie** : elle s'applique
   quelle que soit la valeur de `run.mode`.

5. **Push & open the PR**:
   ```bash
   git push -u origin <me>/<token>   # worktree mode: git -C "<worktree>" push -u origin <worktree.branch>
   gh pr create --title "<summary>" --body-file "<state>/.legion/battles/<id>/pr-body.md" --fill-first --base <default-branch>
   ```
   For the `spike` profile, add `--draft`: the PR opens as a draft.
   Use the repo's default branch as `--base` (read it once, e.g.
   `gh repo view --json defaultBranchRef`). Record the printed PR URL with
   `battle_state.py set-delivery --pr-url <url> --battle <id>` (writes `delivery.pr_url` and sets
   `delivery.pr_state = open`). If `gh` is unavailable → give the user the
   push command + a ready-to-paste PR body and stop.

6. **Comment the issue** (numeric issue only; best-effort, never blocking). Write a
   **short battle-review comment** to `<state>/.legion/battles/<id>/wi-comment.md`: 3–5
   lines, **in French**, what was delivered and why it matters (from `spec.md` +
   gate outcomes); end with the PR URL. Apply the **writing charter** (`battle-workflow`
   § « Charte de style des documents ») — simple, precise language; it is already short,
   so **no separate « En bref »**; reread it against the charter before posting. Then:
   ```bash
   gh issue comment <n> --body-file "<state>/.legion/battles/<id>/wi-comment.md"
   ```
   The issue itself closes on merge via `Closes #<n>` — do not close it here (a `spike`
   PR carries `Refs #<n>` instead: the issue stays open). On
   failure → **warn and continue**: the PR is already created. A constrained/headless
   session may also **refuse** this comment (an auto-mode classifier blocks an outward
   write under the user's identity that the run's authorization did not explicitly
   cover). That refusal is **acceptable, not an error**: the comment is best-effort and
   `Closes #<n>` (or `Refs #<n>` for a `spike`) already links the PR to the issue. Note it and move on. (RETEX: the
   comment was refused in an autonomous session; the PR was already created and linked.)

7. **Close the phase** — `battle_state.py transition deliver done --battle <id>` (`delivery.pr_url`
   was recorded at step 5). Report the PR URL; if the PR draws review comments,
   point to `/legion:battle address` (§H); suggest `/legion:retro` once the PR is
   stabilized. Remind the user that `/legion:battle status` tracks the PR (state, CI,
   merge).
