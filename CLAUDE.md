# CLAUDE.md

Guidance for Claude Code and any other agent working in this repository.

## Hard rules — never break these

Each rule names what enforces it. If a mechanism is in your way, stop and ask — never
weaken it to make a task pass.

1. **Secrets.** Never print, log, commit, echo into a shell, or put in an error message any
   access token, refresh token, password or reCAPTCHA token; redact as `<REDACTED>`. Tokens
   live in the OS keychain via `keyring`. The one exception: the user may paste a refresh
   token by hand into `.env` as `TOPLOGGER_REFRESH_TOKEN`, which the next run imports into
   the keychain. Nothing ever writes a token back to `.env`.
   *Enforced by:* `redact()` in `sources/toplogger/client.py`; `ingest/raw_store.py`
   refusing secret-bearing payloads; the `gitleaks` and `no-dotenv` pre-commit hooks;
   `tests/test_repo_hygiene.py`; the `.env` deny rules in `.claude/settings.json`.
2. **Never automate login.** Never call `authSignin`, and never try to obtain, solve or
   bypass a reCAPTCHA. Auth only ever uses `authSigninRefreshToken` with a refresh token the
   user supplied.
   *Enforced by:* `check_operation` in `post_graphql`, which raises
   `ForbiddenOperationError` before sending; `tests/test_repo_hygiene.py`.
3. **Refresh-token rotation.** After a refresh, persist the new refresh token *before* any
   other work. Hold the lock file for the whole sync so two syncs never overlap. Never
   retry the rotation mutation: a replay sends a token the server may already have killed,
   so `_refresh` calls `post_graphql(..., retry=False)`.
   *Enforced by:* `tests/test_auth.py` (persist-before-return, lock) and the `retry=False`
   tests in `tests/test_client.py`.
4. **Other people's data.** Never persist other users' names, user IDs, avatars or
   per-person tick records. Toppers are aggregated in memory into per-climb counts and only
   the counts are written. Setter names from gym metadata are fine.
   *Enforced by:* the toppers query requesting no identifying field and
   `aggregate_toppers`, both tested in `tests/test_adapter.py`.
5. **Politeness.** At most 1 request/second to TopLogger, batched with aliases, with
   backoff on errors, from one process at a time. Requests carry only the headers
   `post_graphql` sets (`Content-Type`, plus `Authorization` when authenticated); don't add
   a User-Agent, cookies or any other identifying header.
   *Enforced by:* `_throttle` in `client.py` and `test_rate_limiter_spaces_requests`. The
   limiter is per process, so the sync lock is what stops two crawlers.
6. **Tests never hit the live API.** Tests use `httpx.MockTransport` and the sanitised
   fixtures in `tests/fixtures/`.
   *Convention only:* nothing blocks the network in tests yet, so a test that forgets to
   pass `client=` will reach the real endpoint.
7. **Never commit `data/`, never publish the scraped corpus.** Raw and processed data stay
   local; the public repo ships the adapter and a sanitised sample only.
   *Enforced by:* `.gitignore`; the `no-data-dir` pre-commit hook;
   `tests/test_repo_hygiene.py`.
8. **Raw comment text never reaches a model.** Comments store `body_raw` and `body_masked`
   separately; only `body_masked` may go into a prompt, an embedding or a tool result.
   *Convention only:* comments aren't ingested yet. When they are, enforce it in the LLM
   gateway (`assistant/llm.py`).

### You are a model too

Rules 1, 4 and 8 cover your own context: `data/raw/` holds other people's data and
unmasked comments, and `.env` may hold a refresh token.

- Never read anything under `data/` or `.env`. `.claude/settings.json` blocks Read, Edit,
  `cat`, `head` and `sed` on them, but not `grep -r`, `sqlite3` or a Python one-liner —
  those are forbidden too.
- Work only from `tests/fixtures/`. If you need the shape of real data, ask the user for a
  sanitised sample and add it as a fixture.
- Don't run anything that sends live requests (`python -m ingest ...`) unless asked.

### Subagents

Whenever a task can be split into independent parts, spawn one subagent per part and run
them in parallel; the main session integrates and verifies.

- No subagent sends live requests to TopLogger — parallel agents would break rule 5.
- Give each subagent its own files, and name in its brief the rules that apply and the
  files it must not touch.
- The main session owns this file and `docs/`, and runs the full suite at the end.

## Commands

```bash
uv sync                                          # install deps (Python 3.12)
uv run pre-commit install                        # once per clone: secret + data/ hooks
uv run pytest                                    # full suite
uv run pytest tests/test_config.py::test_defaults  # a single test
uv run ruff check . && uv run ruff format --check .
```

Use `uv run pytest`, not `python -m pytest`: the flat layout isn't installed
(`[tool.uv] package = false`), and plain `python -m` hides a broken `pythonpath` by adding
the CWD itself.

**A change is done when** ruff and pytest pass, new behaviour has tests, the diff has no
secrets or personal data, and any new hard-rule mechanism is in its *Enforced by* line.

## Which doc, when

| When you are… | Read |
| :- | :- |
| starting feature work | `docs/ROADMAP.md`: current phase and its "done when"; don't start a later phase early |
| touching parsing, grades, loaders, or anything that interprets a field | `docs/PROJECT_CONTEXT.md` §3 |
| adding or changing a GraphQL operation | `docs/PROJECT_CONTEXT.md` §1–2 |
| relying on something unconfirmed | `docs/PROJECT_CONTEXT.md` §6 — stop and ask, don't assume |
| moving code between layers, adding a module, changing the raw-file layout | `docs/ARCHITECTURE.md` §1–4 |
| touching auth or tokens | `docs/PROJECT_CONTEXT.md` §1 "Auth flow", `docs/ARCHITECTURE.md` §2 |
| running or scheduling a sync | `docs/RUNBOOK.md` |

`docs/PROJECT_CONTEXT.md` wins on facts about the data, `docs/ARCHITECTURE.md` on layer
boundaries.

## How to work

- Keep changes small: one concern per change, with tests.
- **Ask before** changing `schema.sql`, adding a dependency, adding a TopLogger operation
  (an entry in `ALLOWED_OPERATIONS` in `client.py`), or touching auth.
- If the API contradicts `docs/PROJECT_CONTEXT.md`, report it and propose an update — don't
  silently work around it.
- When you finish a phase, update its "done when" in `docs/ROADMAP.md`.
- If a change moves a layer boundary, adds or removes a seam, changes the raw-file contract
  or fixes a wart in its §6, update `docs/ARCHITECTURE.md` in the same commit.

## Stack and layout

Python 3.12, `uv`, `pytest`, `ruff`; `httpx`, `pydantic`, `keyring`; SQLite. Vector store
and LLM provider are chosen in their phases — don't add them early. **All LLM calls go
through `assistant/llm.py`**, which logs model, prompt version, tokens, cost and latency.

```
schema.sql
config.py           # gym id, user id, rate limit, data dir — from env, never committed
sources/            # adapters; base.py defines the Source protocol
  toplogger/
    client.py       # GraphQL transport, operation allowlist, rate limit, retries, batching
    auth.py         # refresh-token flow, keyring, lock
    queries/        # one .graphql file per operation
    adapter.py      # TopLoggerSource implementing Source
ingest/             # sync commands: raw JSON -> data/raw/ (source-agnostic)
load/               # raw JSON -> SQLite, idempotent
features/           # derived signals (Phase 4)
assistant/          # router, tools, agent, llm gateway, guardrails
prompts/            # versioned prompt files: name.vN.md
evals/              # eval sets and runners
tests/fixtures/     # sanitised recorded responses
data/               # gitignored: raw/, db/
```

## Conventions (decided — don't relitigate)

- **Raw is immutable.** Save every API response untouched to
  `data/raw/<source>/<kind>/<YYYY-MM-DD>T<HHMMSS>Z.json` before parsing; loaders read raw
  files, never the live API. Exception: toppers are aggregated in memory (rule 4) and the
  file carries `"aggregated": true`.
- **Loaders are idempotent.** Re-running on the same raw file must not change the DB.
- **Snapshots, not overwrites.** Every catalog row carries `fetched_at`; history accumulates.
- **Nothing downstream knows the source.** Only `sources/` may contain TopLogger field
  names; everything else speaks the `Source` protocol and the schema.
- **Two layers, two tools.** Ticks, catalog and relationships → SQL. Text (comments, beta,
  training content) → vector retrieval. No graph layer. **Never semantic search over
  structured personal data.**
- **Tag provenance lives in the key.** `climb_tag.origin` is `source` / `inferred` /
  `hand_label`, plus `model_version`. Hand labels are the test set.
- **GraphQL queries live in `.graphql` files**, requesting only the fields we use.
- **Prompts are versioned.** Never edit a prompt after an eval has run on it; create
  `name.v(N+1).md`.
- Type hints everywhere; small functions; docstrings on public functions.
- **Comments are about the code in front of them.** Say what it does or what isn't obvious
  from reading it. No references to this file, hard rules, conversations, earlier
  versions or removed code, and no justifying decisions in the code itself.

## What this project is

A personal bouldering assistant over TopLogger data, doubling as an AI-engineering portfolio
project (agents, RAG, guardrails, prompt versioning, cost/latency work). It answers
questions like "which styles am I weak on", "is the gym stiff", "what should I project",
"what's about to be stripped".

Single endpoint: `POST https://app.toplogger.nu/graphql`, no public API — queries were
captured from browser devtools. It accepts aliases (`c1: climb(...) c2: climb(...)`), so
batch. The gym catalog is public; anything user-scoped needs auth. `authSigninRefreshToken`
returns a 10-min access token *and* a new 14-day refresh token; the old one dies on use,
which is why rules 1–3 exist.

## Field semantics — decode, don't guess

`docs/PROJECT_CONTEXT.md` §3 is authoritative; this is what trips people up.

- **Grades** are integers: Font scale × 100 with sixths — `600` 6A, `617` 6A+, `633` 6B,
  `650` 6B+, `667` 6C, `683` 6C+, `700` 7A. Below 5A the steps are irregular. `0` is an
  ungraded placeholder — **filter it out**.
- **`gradeTicks` is not a difficulty signal** — it contradicts its own labels and defaults
  to `783` with no ticks. Ignore it.
- **`tickType`**: `2` = flash, `1` = redpoint/top, `0` unconfirmed.
- **`ratingsAverage`** is `(stars − 1) × 25`, i.e. 0–100 (75 = four stars).
- **Climbs have no names.** The human reference is hold colour + wall + set date, e.g.
  "Purple · Mad Rock · set 31 Aug"; colour names come from gym metadata.
- Vote histograms are tiny (1–4 votes) — a consensus grade needs a minimum vote count or
  shrinkage toward the setter grade.
- `Guest Setter` is a catch-all, not a person — exclude from setter analysis.
- The catalog is **current wall only**. Strip dates come from diffing daily snapshots.
