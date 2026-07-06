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
understanding**. Walk down each branch of the decision tree, resolving dependencies
between decisions one at a time.

**The discipline (this is what makes it work):**

- **One question at a time.** Wait for the answer before the next question. Asking
  several at once is bewildering — never batch them.
- **Recommend an answer to every question.** Don't ask blank questions; propose your
  recommended answer (and why), so the user reacts to a concrete proposal rather than
  starting from nothing.
- **Explore the repo instead of asking** whenever a question can be settled from the
  code. Use `Grep`/`Glob`/`Read` to find the existing pattern, the affected files, the
  current behaviour — then bring the finding to the user, don't make them recite it.
- **One branch at a time.** Resolve a decision before opening the next that depends on
  it; surface trade-offs as you go.
- **Verify every code-level claim before it enters the « Cadrage ».** A file, symbol, or
  current-behaviour statement you write into the brief must be checked with `Read`/`Grep`
  first. If you cannot verify it, frame it as an **assumption to confirm in PLAN** —
  never assert it as fact. (RETEX: a « Cadrage » named the wrong file as carrying a
  per-phase visual and omitted the one that actually displayed it; the architect caught
  it before any code, but a verified claim would have spared the push-back.)
- **State the scope of any rule or check the « Cadrage » proposes.** When the brief
  prescribes a control (a new gate, a threshold, a lint rule…), say **what it applies
  to** — the slice diff vs the whole repo. Scope left implicit defaults wrong: legion's
  review/security gates already impute findings to the **diff**, so a new check states
  it the same way. (RETEX: a lint rule « repo not formatted → revise » left its scope
  unstated; the implicit whole-repo default was wrong, fixed only in PR review.)

**Cover every branch** (skip a branch only once it is genuinely settled): the
underlying problem/intent, what is **in scope**, what is **explicitly out of scope**,
the **assumptions** being made, the **acceptance criteria** (must be checkable), edge
cases, and dependencies / risks.

**Stop when** (completion criterion — all must hold): the scope is unambiguous, the
acceptance criteria are checkable, the out-of-scope is stated explicitly, and the open
dependencies are resolved. That is "shared understanding" — don't stop earlier, don't
drag past it.

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

**Critères d'acceptation.**
- [ ] <critère vérifiable>

**Risques / dépendances.**
- <le cas échéant ; omettre la rubrique si vide>
```

Keep the date placeholder filled with today's date. Omit a rubric only if it would be
empty (except *Hors périmètre*, which must always be explicit — state "Rien d'autre
pour l'instant" rather than leaving it blank).

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
- One question at a time, recommended answer each, explore the repo before asking.
- « Cadrage » in French; identifiers English.
- Confirm before the `gh issue edit`; degrade to paste-ready output if `gh` is absent.
- Pose the `legion-recon` label at the outward write (both the `gh issue edit` and the
  `gh issue create` fallback), created idempotently: **non-blocking** (a label failure
  warns, the « Cadrage » prevails) and under the **same OK** as the body — never a second
  confirmation, never posed in the degraded `gh`-absent mode.
- No `cd`; read with the `Read` tool, not `cat`/`type`.
