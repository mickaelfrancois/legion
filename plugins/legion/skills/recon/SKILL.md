---
name: recon
description: Reconnaissance before a legion battle — relentlessly interview the user to sharpen a rough GitHub issue into a well-scoped feature brief, exploring the repo to answer its own questions, then append a structured "Cadrage" section to the issue. Use before /legion:battle start, when a GitHub issue is vague or thin, or when the user says "recon", "cadrer l'issue", "affiner l'issue", "challenge cette feature", "sharpen this issue".
---

# recon — sharpen an issue before the battle

`recon` is the **pre-THINK reconnaissance step** of `legion`. A GitHub issue often
carries a *rough idea*; `recon` interviews the user to turn it into a **well-scoped
objective**, exploring the repo to answer its own questions, then writes the result
back as a **« Cadrage »** section on the issue. The payoff is downstream:
`/legion:battle start <n>` then seeds a `spec.md` that is already sharp, and the
`architect` gate has far less to push back on.

> **Commands are namespaced.** Surface the next step as `/legion:battle start <n>`
> (bare `/battle` resolves to *Unknown command* — never relay it verbatim).

## Invariants — what recon never does

- **Stateless / pre-THINK.** Never touch `.legion/` — no `battle.json`, no `spec.md`,
  no battle directory. `recon` runs *before* a battle exists.
- **Never starts a battle.** It hands off to `/legion:battle start <n>`; it does not
  invoke it.
- **Confirm before any outward write.** Editing the issue is an outward effect —
  show the exact new body and wait for an explicit OK (same discipline as
  `/legion:battle deliver`). Never edit silently.
- **French for the brief, English for identifiers.** The « Cadrage » prose is written
  in French (legion convention for artifacts); type/file/symbol names stay English.
- **Stays on the WHAT.** `recon` sharpens *what* to build — intent, scope, criteria.
  It never proposes a technical approach (layers, design, implementation choices):
  that is the `architect`'s job at PLAN. Exploring the repo to establish facts stays
  allowed.
- **Decisions belong to the user.** A **fact** can be checked in the repo or the
  environment (current behaviour, a file, an existing convention): establishing it is
  your job. A **decision** is about what to build (scope, criterion, trade-off,
  priority): only the user takes it. Never write a `**Décisions.**` line for a question
  that was not asked in a round. A recommendation accepted by selecting it is a valid
  decision — it was asked.

## §1 — Preflight

1. **Resolve the issue.** The invocation argument is a GitHub issue number.
   - **Numeric** (e.g. `1234`) → read it:
     ```bash
     gh issue view 1234 --json number,title,body,labels
     ```
     This title/body/labels is the rough idea you will sharpen.
   - **No argument / not an issue** → ask the user which issue number to recon. If
     there is genuinely no issue yet (just an idea in the conversation), say so and
     offer a fallback: run the recon now, then **propose** `gh issue create` at the
     end so a battle has something to start from. Pass the body via a `--body-file`
     temp file and **confirm first** (same discipline as §4) — the create path inherits
     the same cp1252-safe, confirmed-write behaviour as the edit path. This create path
     **also poses the `legion-recon` label**, created idempotently first (same pattern as
     §4), under the same confirmation — no second OK:
     ```bash
     gh label create legion-recon --description "Issue cadrée par /legion:recon" --color 0E8A16
     gh issue create --label legion-recon --title "<title>" --body-file <path-to-temp-body>
     ```
     `gh label create` fails harmlessly if the label already exists — ignore that error.
2. **Check `gh`.** Run `gh auth status`. If `gh` is missing or unauthenticated →
   **degrade gracefully**: still run the full recon, but at the end **print the
   « Cadrage » block ready to paste** instead of editing the issue. Warn the user once.
   In this degraded mode **no `legion-recon` label can be posed** — say so plainly; never
   claim the label was applied when it was not.
3. **Reading files.** Use the `Read` tool, never `cat`/`type` (a Windows cp1252 console
   crashes on non-ASCII with `UnicodeEncodeError`). Never `cd`; operate from the
   current directory.

## §2 — The recon (core loop)

Interview the user **relentlessly** about the feature until you reach a **shared
understanding**. Map the decisions as a tree: each decision opens the ones that
depend on it. Ask in **rounds**, each round being **one `AskUserQuestion` call**.

### Pre-scan (coverage) — before the first round

Rate each of the seven branches from the issue as it stands: **intent**, **in scope**,
**out of scope**, **assumptions**, **acceptance criteria**, **edge cases**,
**risks / dependencies**. Each gets one rating:

- **Clear** — settled by the issue (or the repo); no question needed.
- **Partial** — present but ambiguous or incomplete.
- **Missing** — absent.

Show the ratings as a compact table (branch | rating | one-line reason) in text before
the first round. A branch wrongly rated Clear would never be questioned, so the table is
the user's check: the **first question of round 1** validates it (header `Pré-scan`,
options « Correct (Recommandé) » / « À corriger » — the user names the correction
through the free answer, e.g. « le périmètre est Partial »). A correction adds the
newly Partial/Missing branches' questions to the next round. **Only Partial and Missing
branches are questioned.** On the create path (§1, no issue yet), every branch is
Missing — the validation question is still asked.

### Rounds

- **A round asks every question that depends on no open answer.** A question whose
  answer hinges on another question still open goes to a **later** round, never the
  same one. Resolve a decision before asking the ones that depend on it; surface
  trade-offs as you go.
- **At most 4 questions per round** — the `AskUserQuestion` limit. If more can be
  asked, keep the 4 with the highest impact on scope or acceptance criteria and carry
  the rest to the next round. There is no cap on the number of rounds.
- **Recommend an answer to every question, with a short justification.** Don't ask
  blank questions: the user reacts to a concrete proposal rather than starting from
  nothing.
- **2 to 4 options per question, the recommended one first.** Its label ends with
  ` (Recommandé)`; its `description` carries the short justification. The other
  options' descriptions state their consequence or trade-off. Add no « autre » option:
  the tool always offers a free answer.
- **Open questions** (a name, a free threshold — answers that cannot be listed) still
  go through the tool: the recommended value is the first option, plus one plausible
  alternative; the free answer covers the rest.
- **`header`** is the question's short title (≤ 12 characters). Use `multiSelect` only
  when several answers genuinely combine (e.g. which items enter the scope); a
  multi-select question still marks its recommended options ` (Recommandé)`.
- **Text around the call.** Before the call, give in plain text whatever the
  questions need and cannot hold — the facts found in the repo, a trade-off too long
  for a description, the « En attente d'exploration » line. Keep it short.
- **Format of a round** (one call):
  ```json
  {"questions": [
    {"question": "<la question>", "header": "<titre court>", "multiSelect": false,
     "options": [
       {"label": "<option> (Recommandé)", "description": "<justification courte>"},
       {"label": "<option>", "description": "<conséquence / compromis>"}
     ]}
  ]}
  ```

### Reading the answers

- **The recommended option selected** → the recommendation is the decision.
- **Another option selected** → it is the decision, logged with its ` _(écart : …)_`
  (see §3).
- **A free answer** → read what it actually says. It may be a decision (logged as
  text, with its écart), a correction of the pre-scan, a question back, or a request
  to stop: follow it — never map it onto the nearest option.
- **No answer** (call refused, dismissed, tool unavailable) → nothing is decided:
  never assume the recommendations. Say so in text and ask how to proceed.
- **An answer that contradicts an earlier decision reopens that branch** in the next
  round: its « Décisions » line is **replaced**, never duplicated, and the questions
  that depended on it are asked again.
- **A discovered fact that contradicts an earlier answer reopens that branch** the same
  way: present the fact in the next round, ask the question again, and replace its
  « Décisions » line once the user answers. Never correct the decision yourself — even
  when the user was wrong about the code, the fact is yours, the decision is theirs.

### Discipline

- **Explore the repo instead of asking** whenever a question can be settled from the
  code — then bring the finding to the user, don't make them recite it. Pick the tool by
  the size of the search:
  - a **known file or symbol** → read it directly (`Read` / `Grep`);
  - a **large search** (several files, a convention to sweep) → delegate it to an
    `Explore` sub-agent through the `Agent` tool, **in the background**, so its file
    dumps stay out of the interview's context.
- **Never block a round on an exploration.** While `Explore` runs, the round asks the
  questions that do not depend on it. A question that needs the fact being searched
  depends on an open answer (see Rounds): it moves to the round after the result. Say
  so in the text before the call with one line: « En attente d'exploration : <fait> ».
  If every remaining question waits on an exploration, announce the wait and hold the
  round until `Explore` returns — do not fall back to a direct large search, which
  would bring the dumps back into the interview.
- **Fallback.** `Explore` unavailable, failing or empty → search directly with
  `Grep` / `Read`. Still nothing → an **assumption to confirm in PLAN** (rule below).
  Never ask the user for a fact the repo can give.
- **Verify every code-level claim before it enters the « Cadrage ».** A file, symbol, or
  current-behaviour statement you write into the brief must be checked with `Read`/`Grep`
  first — including every `file:line` an `Explore` sub-agent reports: it reads excerpts,
  so re-read each one with `Read` before it enters the brief. If you cannot verify it,
  frame it as an **assumption to confirm in PLAN** — never assert it as fact. (RETEX: a
  « Cadrage » named the wrong file as carrying a per-phase visual and omitted the one
  that actually displayed it; the architect caught it before any code, but a verified
  claim would have spared the push-back.)
- **State the scope of any rule or check the « Cadrage » proposes.** When the brief
  prescribes a control (a new gate, a threshold, a lint rule…), say **what it applies
  to** — the slice diff vs the whole repo. Scope left implicit defaults wrong: legion's
  review/security gates already impute findings to the **diff**, so a new check states
  it the same way. (RETEX: a lint rule « repo not formatted → revise » left its scope
  unstated; the implicit whole-repo default was wrong, fixed only in PR review.)

**Cover every Partial and Missing branch** of the pre-scan (skip a branch only once it
is genuinely settled): the underlying problem/intent, what is **in scope**, what is
**explicitly out of scope**, the **assumptions** being made, the **acceptance criteria**
(must be checkable), edge cases, and dependencies / risks.

**Stop when** (completion criterion — all must hold): the scope is unambiguous, the
acceptance criteria are checkable, the out-of-scope is stated explicitly, and the open
dependencies are resolved. That is "shared understanding" — don't stop earlier, don't
drag past it.

**Nothing to sharpen.** If every branch is Clear, round 1 holds only the pre-scan
validation question; once it is answered « Correct », run no further round: go to §3
and write a **minimal « Cadrage »** built from the issue, without a new « Décisions »
line — but if the issue already carries a `**Décisions.**` rubric (re-run), keep it as
is (see §3). It still goes through §4 — same OK, same `legion-recon`
label — so the issue carries the structured rubrics `start` seeds `spec.md` from.

## §3 — Synthesize the « Cadrage »

Compose, **in French**, a section mirroring the legion `spec.md` structure (so it
pre-builds the future spec). Identifiers and file names stay English.

```markdown
## Cadrage

_Affiné via `/legion:recon` le <AAAA-MM-JJ>._

**Intention.** <le problème et le résultat attendu, en 1–3 phrases>

**Dans le périmètre.**
- <…>

**Hors périmètre.**
- <ce qui est explicitement exclu>

**Hypothèses.**
- <hypothèses retenues pendant le cadrage>

**Décisions.**
- <question courte> → <réponse retenue>
- <question courte> → <réponse retenue> _(écart : reco « <recommandation> »)_

**Critères d'acceptation.**
- [ ] <critère vérifiable>

**Risques / dépendances.**
- <le cas échéant ; omettre la rubrique si vide>
```

Keep the date placeholder filled with today's date. Omit a rubric only if it would be
empty (except *Hors périmètre*, which must always be explicit — state "Rien d'autre
pour l'instant" rather than leaving it blank).

**The `**Décisions.**` rubric** logs one line per question settled during the rounds:
`- <question courte> → <réponse retenue>`. When the answer departs from your
recommendation, append ` _(écart : reco « <recommandation> »)_`, so the `architect`
sees the rejected recommendation without reading the issue. Write the retained answer
and the recommendation **as text** (the option's wording, without the ` (Recommandé)`
suffix), never as a bare option number. Keep the label
**exactly** `**Décisions.**`: `/legion:battle start` looks for it to copy these lines
into `spec.md`. Omit the rubric when no question was asked **and** the issue carries
no earlier `**Décisions.**` rubric — an earlier one is always kept (see the re-run
rule below).

**Re-run on an issue already framed.** If the issue already carries a `## Cadrage` with
a `**Décisions.**` rubric, **start from its lines**: add the new decisions, and replace
a line whose decision this session contradicts — never duplicate it. The pre-scan rates
the branches already settled Clear, so dropping the old lines would silently lose
decisions the `architect` must still see.

## §4 — Update the issue (outward — confirm first)

1. **Compose the new body, non-destructively:**
   - If the current body **already contains a `## Cadrage` section** (a previous recon
     run), **replace that section in place** — do not append a second one (idempotent
     re-run).
   - Otherwise **append** the « Cadrage » section **below the original idea**, leaving
     the original text **intact**.
2. **CONFIRM.** Show the user the full new issue body (or a clear diff) in text, then
   ask through `AskUserQuestion` (header `Publication`), noting that the write also
   **poses the `legion-recon` label**. Options: « Publier (Recommandé) » ·
   « Modifier » (the user says what to change; apply it and ask again) · « Afficher le
   bloc seulement » (print the « Cadrage » for manual paste, write nothing). Only
   « Publier » is an OK: no answer, or a free answer that is not a clear OK, writes
   nothing. The label follows this **same OK** — never ask a second time for it.
3. **Write** via a temp body file (avoids shell-quoting issues with multi-line French),
   and **pose the `legion-recon` label** in the same confirmed write. Create the label
   first (idempotent, same pattern as `legion-opportunity` in `commands/retro.md`), then
   edit the body and add the label:
   ```bash
   gh label create legion-recon --description "Issue cadrée par /legion:recon" --color 0E8A16
   gh issue edit <n> --body-file <path-to-temp-body> --add-label legion-recon
   ```
   `gh label create` fails harmlessly if the label already exists — **ignore that error**.
   The label is a **non-blocking** signal: if `--add-label` fails, warn and continue — the
   « Cadrage » body is the output that prevails, never aborted for a label.
   On `gh` failure for the body itself → fall back to printing the « Cadrage » block for
   manual paste and warn; never leave the user unsure whether the write happened.
4. **Hand off.** Point to the next step: `/legion:battle start <n>` will now seed a
   sharp `spec.md` from the refined issue. `recon` stops here — it does not start the
   battle.

## Guardrails (recap)

- Pre-THINK and stateless: nothing under `.legion/`, no battle started.
- Pre-scan shown first; only Partial / Missing branches are questioned.
- Pre-scan validated by the first question of round 1.
- One round = one `AskUserQuestion` call of ≤4 independent questions; 2-4 options each,
  the recommended one first, labelled ` (Recommandé)`, its justification in the
  description; open questions too (recommended value + one alternative, free answer
  for the rest); « Décisions » lines in text; a free answer is read for what it says;
  no answer = nothing decided; explore the repo
  before asking — large searches via a background `Explore` (never blocking the round),
  direct `Read`/`Grep` otherwise; every `file:line` re-checked with `Read`.
- A contradicting answer reopens its branch and replaces its « Décisions » line.
- Stays on the WHAT: no technical approach, that is the `architect`'s job.
- Decisions belong to the user: facts are yours, decisions are theirs; no « Décisions »
  line without a question asked in a round; a contradicting fact reopens the branch,
  never a silent correction.
- « Cadrage » in French; identifiers English.
- Confirm before the `gh issue edit` through `AskUserQuestion` (only « Publier » is an
  OK); degrade to paste-ready output if `gh` is absent.
- Pose the `legion-recon` label at the outward write (both the `gh issue edit` and the
  `gh issue create` fallback), created idempotently: **non-blocking** (a label failure
  warns, the « Cadrage » prevails) and under the **same OK** as the body — never a second
  confirmation, never posed in the degraded `gh`-absent mode.
- No `cd`; read with the `Read` tool, not `cat`/`type`.
