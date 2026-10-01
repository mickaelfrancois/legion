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
  that was not asked in a round. A recommendation accepted by `ok` or by silence is a
  valid decision — it was asked.

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
depend on it. Ask in **rounds**, and let the user answer only where they disagree.

### Pre-scan (coverage) — before the first round

Rate each of the seven branches from the issue as it stands: **intent**, **in scope**,
**out of scope**, **assumptions**, **acceptance criteria**, **edge cases**,
**risks / dependencies**. Each gets one rating:

- **Clear** — settled by the issue (or the repo); no question needed.
- **Partial** — present but ambiguous or incomplete.
- **Missing** — absent.

Show the ratings as a compact table (branch | rating | one-line reason) before the
first round, and let the user correct it ("non, le périmètre est Partial"). A branch
wrongly rated Clear would never be questioned, so the table is the user's check.
**Only Partial and Missing branches are questioned.** On the create path (§1, no issue
yet), every branch is Missing.

### Rounds

- **A round asks every question that depends on no open answer.** A question whose
  answer hinges on another question still open goes to a **later** round, never the
  same one. Resolve a decision before asking the ones that depend on it; surface
  trade-offs as you go.
- **Number the questions; at most 5 per round.** If more than 5 can be asked, keep the
  5 with the highest impact on scope or acceptance criteria and carry the rest to the
  next round. There is no cap on the number of rounds.
- **Recommend an answer to every question, with a short justification.** Don't ask
  blank questions: the user reacts to a concrete proposal rather than starting from
  nothing.
- **Lettered options when there are real alternatives.** Such a question offers **2 to 4
  lettered options** (a, b, c, d) in their natural order — do not move the recommended
  one to the top. The ➡️ line gives the recommended **letter** and its justification.
  Add no « autre » option: a free answer is always accepted (« 2: autre chose… »). Keep
  an open question only when the answers cannot be listed (a name, a free threshold).
  Why: without visible alternatives the user can still answer freely, but only by
  inventing an answer, so in practice they rarely deviate from the recommendation.
- **Plain text, not `AskUserQuestion`.** A text round lets the user answer only the
  numbers they want to change; the tool forces one interaction per question.
- **Format of a round:**
  ```markdown
  **Q1 — <titre court>.** <la question>
  - a. <option>
  - b. <option>
  - c. <option>

  ➡️ Recommandé : **b** — <justification courte>

  **Q2 — <titre court>.** <question ouverte : réponse non énumérable>
  ➡️ Recommandé : <réponse> — <justification courte>
  ```
  End the round with one line: « Réponds `ok`, ou seulement les numéros à changer. »

### Answers by exception

- **`ok` accepts the whole round.** A question left unanswered in a reply **accepts
  its recommendation**. The user only answers to deviate (« 2: b »).
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
  so in the round with one line: « En attente d'exploration : <fait> ». If every
  remaining question waits on an exploration, announce the wait and hold the round
  until `Explore` returns — do not fall back to a direct large search, which would
  bring the dumps back into the interview.
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

**Nothing to sharpen.** If every branch is Clear (after any correction of the table),
run no round: go to §3 and write a **minimal « Cadrage »** built from the issue, without
a new « Décisions » line — but if the issue already carries a `**Décisions.**` rubric
(re-run), keep it as is (see §3). It still goes through §4 — same OK, same `legion-recon`
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
and the recommendation **as text** (the option's wording), never as a bare letter: a
letter means nothing outside its round. Keep the label
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
2. **CONFIRM.** Show the user the full new issue body (or a clear diff), and note that
   the write also **poses the `legion-recon` label**. **Wait for an explicit OK.** Do not
   write before that. The label follows this **same OK** — never ask a second time for it.
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
- Rounds of ≤5 independent numbered questions, a recommended answer + short
  justification each; real alternatives as 2-4 lettered options (➡️ = letter +
  justification, a free answer still accepted; « Décisions » lines in text, not
  letters); `ok` or silence = recommendation accepted; explore the repo
  before asking — large searches via a background `Explore` (never blocking the round),
  direct `Read`/`Grep` otherwise; every `file:line` re-checked with `Read`.
- A contradicting answer reopens its branch and replaces its « Décisions » line.
- Stays on the WHAT: no technical approach, that is the `architect`'s job.
- Decisions belong to the user: facts are yours, decisions are theirs; no « Décisions »
  line without a question asked in a round; a contradicting fact reopens the branch,
  never a silent correction.
- « Cadrage » in French; identifiers English.
- Confirm before the `gh issue edit`; degrade to paste-ready output if `gh` is absent.
- Pose the `legion-recon` label at the outward write (both the `gh issue edit` and the
  `gh issue create` fallback), created idempotently: **non-blocking** (a label failure
  warns, the « Cadrage » prevails) and under the **same OK** as the body — never a second
  confirmation, never posed in the degraded `gh`-absent mode.
- No `cd`; read with the `Read` tool, not `cat`/`type`.
