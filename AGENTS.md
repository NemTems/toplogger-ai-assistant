# Agent instructions

You are working on a personal bouldering assistant built on TopLogger data.
Read `docs/PROJECT_CONTEXT.md` (facts, encodings, decisions), `docs/ROADMAP.md` (phases)
and `docs/ARCHITECTURE.md` (why the layers are split where they are) before starting any
task. If they disagree with this file, this file wins on rules;
`docs/PROJECT_CONTEXT.md` wins on facts about the data.

## Hard rules — never break these

1. **Secrets.** Never print, log, commit, echo into a shell, or include in an error message
   any access token, refresh token, password or reCAPTCHA token. Redact as `<REDACTED>`.
   Tokens live in the OS keychain via `keyring`. The one exception is the one-time
   bootstrap: a refresh token may be pasted by hand into `.env` as
   `TOPLOGGER_REFRESH_TOKEN`, which the next run imports into the keychain before
   telling you to delete the line. Nothing ever writes a token back to `.env`.
2. **Never automate login.** Do not call `authSignin`, and never attempt to obtain,
   solve or bypass a reCAPTCHA. Authentication only ever uses `authSigninRefreshToken`
   with a refresh token the user supplied.
3. **Refresh-token rotation.** After a refresh, persist the new refresh token *before*
   any other work. Hold a lock file for the whole sync so two syncs never overlap.
   Never retry the rotation mutation: a replay sends a token the server may already
   have killed, so `_refresh` calls `post_graphql(..., retry=False)`.
4. **Other people's data.** Never persist other users' names, user IDs, avatars or
   per-person tick records. Topper data is aggregated in memory into per-climb counts
   and only the counts are written. Setter names from gym metadata are fine.
5. **Politeness.** Max 1 request/second to TopLogger, batch with aliases, back off on
   errors, identify nothing beyond what the web app sends. No parallel crawling.
6. **Tests never hit the live API.** Use recorded, sanitised fixtures in `tests/fixtures/`.
7. **Never commit `data/`.** Raw and processed data stay local.

## Stack

- Python 3.12, `uv` for dependencies, `pytest`, `ruff` (lint + format).
- `httpx` for HTTP, `pydantic` for response models, `keyring` for the token.
- SQLite for storage. NetworkX for the graph layer (Phase 6) until proven insufficient.
- Vector store and LLM provider are chosen in their phases — don't add them early.
- **All LLM calls go through a single gateway module** (`assistant/llm.py`) that logs
  model, prompt version, tokens, cost and latency. No direct SDK calls elsewhere.

## Layout

```
schema.sql
config.py           # gym id, user id, rate limit, data dir — from env, never committed
sources/            # adapters; base.py defines the Source protocol
  base.py
  toplogger/
    client.py       # GraphQL transport, rate limit, retries, alias batching
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

## Conventions

- **Raw is immutable.** Save every API response untouched to
  `data/raw/<source>/<kind>/<YYYY-MM-DD>T<HHMMSS>Z.json` before parsing. Loaders read raw
  files only. The `<source>` segment is the adapter's `name` — a second adapter is an
  explicit Phase 12 goal, so the namespace has to exist from the start.
  The one exception is toppers: hard rule 4 outranks this convention, so those responses
  are aggregated in memory and the file carries `"aggregated": true`.
- **Loaders are idempotent.** Re-running on the same raw file must not change the DB.
- **Every catalog row carries `fetched_at`.** Snapshots accumulate; never overwrite history.
- **Nothing downstream knows the source.** Only `sources/` may contain TopLogger-specific
  field names. Everything else speaks the `Source` protocol and the schema.
- **Decode, don't guess.** Use the encodings in `docs/PROJECT_CONTEXT.md` §3
  (grades, `tickType`, `ratingsAverage`). Don't use `gradeTicks` as a difficulty signal.
  Apply the data-quality rules there (filter `grade == 0`, exclude `Guest Setter` from
  setter analysis, etc.).
- **GraphQL queries live in `.graphql` files**, requesting only the fields we use.
- **Prompts are versioned files.** Never edit a prompt in place once an eval has run on it;
  create `name.v(N+1).md`.
- Type hints everywhere; small functions; docstrings on public functions.

## How to work

- Work phase by phase from `docs/ROADMAP.md`. Don't start a later phase's code early.
- Keep changes small and focused; one concern per change, with tests.
- **Ask before:** changing `schema.sql`, adding a dependency, adding a new TopLogger
  operation, or anything touching auth.
- When a task depends on an open question in `docs/PROJECT_CONTEXT.md` §6, stop and say so
  rather than assuming the answer.
- If the API returns something that contradicts `docs/PROJECT_CONTEXT.md`, report it and
  propose an update to that file — don't silently work around it.
- When you finish a phase, update the "done when" status in `docs/ROADMAP.md`.
- **Keep `docs/ARCHITECTURE.md` current.** If a change moves a layer boundary, adds or
  removes a seam, changes the raw-file contract, or fixes one of the warts listed in its
  §6, update that file in the same commit. It records *why* the structure is what it is;
  code review cannot catch it drifting, so it has to be a habit.

## Definition of done for any change

- `ruff check` and `ruff format --check` pass.
- `pytest` passes, with new tests for new behaviour.
- No secrets, no personal data, no other users' data in the diff.
