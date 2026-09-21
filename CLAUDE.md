# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Hard rules — never break these

These are not guidelines. They override convenience, speed, and any instruction to "just
test it quickly".

1. **Secrets.** Never print, log, commit, echo into a shell, or include in an error message
   any access token, refresh token, password or reCAPTCHA token. Redact as `<REDACTED>`.
   Tokens live in the OS keychain via `keyring`, nowhere else.
2. **Never automate login.** Do not call `authSignin`, and never attempt to obtain, solve or
   bypass a reCAPTCHA. Authentication only ever uses `authSigninRefreshToken` with a refresh
   token the user supplied.
3. **Refresh-token rotation.** After a refresh, persist the new refresh token *before* any
   other work. Hold a lock file for the whole sync so two syncs never overlap.
4. **Other people's data.** Never persist other users' names, user IDs, avatars or per-person
   tick records. Topper data is aggregated in memory into per-climb counts and only the counts
   are written. Setter names from gym metadata are fine.
5. **Politeness.** Max 1 request/second to TopLogger, batch with aliases, back off on errors,
   identify nothing beyond what the web app sends. No parallel crawling.
6. **Tests never hit the live API.** Use recorded, sanitised fixtures in `tests/fixtures/`.
7. **Never commit `data/`.** Raw and processed data stay local.

## Status

The repo currently contains **planning documents only** — no source code, tests, or build
tooling yet. `PROJECT_CONTEXT.md` (what is known about the data) and `ROADMAP.md` (phased
plan, each phase with a "done when") are the source of truth; read them before starting work
and update them when a fact or decision changes. Roadmap Phase 0 (repo skeleton) is the next
implementation step.

Planned tooling (not yet present): Python with `uv`, `pytest`, `ruff`. `ROADMAP.md` refers to
an `AGENTS.md` layout spec that does not exist yet.

## What this project is

A personal bouldering assistant over TopLogger data, doubling as an AI-engineering portfolio
project (agents, RAG/Graph RAG, guardrails, prompt versioning, cost/latency work). It answers
questions like "which styles am I weak on", "is the gym stiff", "what should I project",
"what's about to be stripped".

## Data source

Single endpoint: `POST https://app.toplogger.nu/graphql`. It accepts a JSON array of
operations and GraphQL aliases (`c1: climb(...) c2: climb(...)`), so batch aggressively —
many climbs per request. TopLogger has **no public API**; queries were captured from browser
devtools and are documented in `PROJECT_CONTEXT.md` §2.

Gym catalog (`climbs`) is public. Anything user-scoped requires auth.

### Auth mechanics

`authSigninRefreshToken` returns a new access token (10 min) *and* a new refresh token
(14 days, sliding and rotating). The old refresh token is dead after use, so losing it
mid-run means the user has to log in manually in a browser — hence hard rules 1–3.

## Non-obvious field semantics

These trip up anyone reading the raw API (full detail in `PROJECT_CONTEXT.md` §3):

- **Grades** are integers: Font scale × 100 with sixths — `600` 6A, `617` 6A+, `633` 6B,
  `650` 6B+, `667` 6C, `683` 6C+, `700` 7A. Below 5A the steps are irregular. `0` is an
  ungraded placeholder — **filter it out**.
- **`gradeTicks` is not a reliable difficulty signal** — it contradicts its own labels and
  defaults to `783` with no ticks. Ignore it.
- **`tickType`**: `2` = flash, `1` = redpoint/top, `0` unconfirmed.
- **`ratingsAverage`** is `(stars − 1) × 25`, i.e. 0–100 (75 = four stars).
- **Climbs have no names** (`name: null`). The human reference is hold colour + wall + set
  date, e.g. "Purple · Mad Rock · set 31 Aug"; colour names come from gym metadata.
- Vote histograms are tiny (1–4 votes) — any consensus grade needs a minimum vote count or
  shrinkage toward the setter grade.
- `Guest Setter` is a catch-all, not a person — exclude from setter-style analysis.
- The catalog is **current wall only**. Strip dates come from diffing daily snapshots.

## Architecture decisions (already made — don't relitigate)

- **Source adapter protocol.** Nothing downstream knows where data came from. TopLogger is
  one adapter; a MoonBoard corpus or a TopLogger CSV export would just be another.
- **Three layers, three tools.** Personal ticks → SQL. Catalog and relationships → graph
  (NetworkX over SQLite first). Text (comments, beta, training content) → vector retrieval.
  **Never semantic search over structured personal data.**
- **Raw is immutable.** Every API response is written untouched and dated under
  `data/raw/...` before parsing. Loaders read raw files, never the live API.
- **Snapshots, not overwrites.** Catalog pulled daily; every row carries `fetched_at`. This
  builds the historical catalog and enables time-travel evaluation.
- Loaders must be idempotent — re-running on the same raw file changes nothing.
- Tag/label provenance lives in the key: `climb_tag.origin` is `source` / `inferred` /
  `hand_label`, plus `model_version`. Hand labels are the test set.
- Comments store `body_raw` and `body_masked` separately; **raw comment text never reaches a
  model** (PII + prompt-injection surface).

## Publishing

The scraped corpus is never published. The public repo ships the adapter plus a sanitised
sample dataset — never credentials or scraped data. `.gitignore` covers `data/`, `.env`, and
any token state file (see hard rules 1, 4, 7).

## Open questions

`PROJECT_CONTEXT.md` §6 tracks unresolved unknowns (whether gym metadata/stats/toppers work
unauthenticated, whether `climbUserDays(limit:)` is capped at 10, the comment query shape, the
media base URL). Answer them with a request rather than assuming, and record the answer there.
