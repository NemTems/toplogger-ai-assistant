# Roadmap — what we need to do

Ordered so each phase produces something usable and the eval harness exists before
anything is optimised. Every phase has a "done when".

## Immediate next actions

1. Answer open questions 1, 2 and 6 in `PROJECT_CONTEXT.md` with a few Postman requests.
2. Capture the comment query from devtools on a climb with `commentsCount > 0`.
3. Set up the repo (Phase 0).

---

## Phase 0 — Repo and secrets

- Python project, `uv`, `pytest`, `ruff`.
- Layout per `AGENTS.md`.
- `.gitignore` covers `data/`, `.env`, any token state file — **in the first commit**.
- Refresh token stored via `keyring` (OS keychain), not in a file in the repo.
- Config: gym ID, user ID, request rate limit, data directory.

**Done when:** a fresh clone runs `pytest` green and contains no secrets or personal data.

**Status: done (2026-09-21).** `uv sync` + `uv run pytest` green from a clean checkout (14 tests). `tests/test_repo_hygiene.py` asserts `data/`, `.env` and the db path are gitignored and scans tracked files for credential-shaped strings — verified to actually fail on a planted token, not just pass vacuously. Note the repo skeleton is **not yet committed**: only `.gitignore`, `CLAUDE.md`, `LICENSE` and `README.md` are tracked.

## Phase 1 — Auth module

- `get_access_token()`: read refresh token → call `authSigninRefreshToken` →
  **persist new refresh token first** → return access token.
- Lock file so two syncs never run at once.
- Clear error with instructions when the refresh token has expired
  (log in manually in the browser, paste the new refresh token via a CLI command).
- Tokens never appear in logs, exceptions or repr.

**Done when:** running it twice in a row succeeds; killing it mid-run never loses the token.

**Status: done (2026-09-21).** Verified live against the real API: two consecutive runs
both returned an access token, rotating the refresh token each time. 45 offline tests pass,
including a mutation-checked ordering test for the persist-before-return guarantee. One bug
found and fixed during live testing: a rejected token in the keychain shadowed a freshly
pasted `.env` token, so re-pasting appeared to do nothing.

## Phase 2 — Raw ingestion

- `sync catalog` — public `climbs` + gym metadata → `data/raw/catalog/YYYY-MM-DD.json`.
- `sync stats` — batched aliased grade/rating histograms for all current climbs.
- `sync toppers` — per-climb flash/redpoint counts and tick dates, **aggregated before
  writing**; no names or user IDs ever touch disk.
- `sync me` — authenticated user history (`climbDays` + `climbUserDays`,
  grade distribution, grade time series).
- Rate limit (e.g. ≤1 request/second), retries with backoff, request batching.
- Scheduled daily for catalog; weekly for `me`.

**Done when:** a week of daily catalog snapshots exists and one full personal history pull
is on disk.

**Status: verified live (2026-09-23).** All four commands ran against the real API and
wrote data: 179 climbs (153 after the `grade == 0` filter), gym metadata, stats for 25
climbs in **one** aliased request, aggregated toppers for 5 climbs, and a full personal
history of 102 session days / 391 ticks. 156 offline tests pass. Two consecutive catalog
runs produced two snapshots with neither overwritten.

Hard rule 4 verified on real data: the written toppers file contains no name, user ID or
avatar field of any kind, only per-climb counts. Both safety guarantees are
mutation-checked rather than merely asserted — breaking the aggregation or disabling the
raw-file credential guard each makes the relevant tests fail.

**Open questions §6.1 and §6.2 are now answered** (see `PROJECT_CONTEXT.md` §6):

- Catalog, gym metadata and per-climb stats are **public**; toppers **require auth**.
- `climbUserDays(limit:)` is **not** capped at 10. The web app's 10 was dropping ~24% of
  the tick history; the default is now 100.

Five bugs the live run found that no offline test could have:

1. `user.gradeDistribution` has no `rp`/`fl` — the real fields are `countRp`/`countFl`.
2. `climbDays(gymIdNotNull:)` is a **Boolean**, not an id; the gym filter is `gymId`.
3. `climbUsers(pagination: {perPage:})` is **capped at 10** — 11 returns `BAD_REQUEST`.
4. Unauthenticated `climbUsers` answers `BAD_REQUEST` for a real climb id but
   `UNAUTHENTICATED` for a fake one, so the auth probe cannot trust the error code.
5. A query that fails validation returns **HTTP 400 with the errors in the body**, not a
   200 carrying `errors` — the transport was discarding exactly the diagnostic we needed.

**Not yet done:** the "week of daily catalog snapshots" the *done when* asks for simply
needs time — `catalog` must run daily for a week. `docs/RUNBOOK.md` has the launchd and
cron snippets; nothing is installed. A full uncapped `toppers` run has also not been done
(~20 minutes at 1 rps).

## Phase 3 — Normalise and load

- Update `schema.sql` per decisions in `PROJECT_CONTEXT.md` §4.
- Loaders: raw JSON → SQLite. Idempotent; re-running on the same raw file changes nothing.
- Apply data-quality rules (§3). Derive strip dates from snapshot diffs.
- Grade decoding to `grade_norm` and human labels.
- Readable climb reference: "Purple · Mad Rock · set 31 Aug".

**Done when:** a SQL query answers "my hardest sends last month" and
"which climbs came down this week" correctly.

## Phase 4 — Derived signals

- Per-climb difficulty signals: flash ratio, ticks-per-week since set, own continuous
  soft/hard score, shrunken consensus grade from vote histograms.
- Personal profile: send rate and flash rate by grade, by wall, by setter (excluding
  Guest Setter), by tag once tags exist.
- Progression: rolling max/median sent grade, redpoint vs flash level over time.

**Done when:** each signal has a short written definition and a sanity-check notebook.

## Phase 5 — Tag inference (the core ML/LLM feature)

- Hand-label 100–200 climbs with the 24-tag taxonomy (`origin = hand_label`).
- Inputs: climb photo, a frame or two from the setter video, comments where present.
- Multimodal model proposes tags with confidence; stored with `origin = inferred`
  and `model_version`.
- Evaluate against hand labels and against existing setter tags. Precision/recall per tag.
- Prompts versioned in files; every eval run records prompt version, model, cost.

**Done when:** a results table compares at least two prompt versions on the same labelled set.

## Phase 6 — Graph layer

- Nodes: climb, wall, setter, tag, hold colour, climb group, you.
- Edges: set-by, on-wall, has-tag, sent/flashed/tried (you only), similar-to
  (shared tags + wall + grade band).
- Start with NetworkX over SQLite; move to a graph DB only if queries demand it.
- Multi-hop questions that need it, e.g. "climbs by setters whose problems I flash,
  in a style I'm weak at, coming down this week".

**Done when:** at least five multi-hop questions answer correctly from the graph where
plain SQL or vector search would need hand-written joins or fail.

## Phase 7 — Question set and agent

- Write the target question list (~12–20) with expected answers computed from the DB.
  Examples: weakest styles; best climb to project now; soft 6B+s I haven't tried;
  am I improving; which setter's grades are stiffest; what's coming down soon.
- Router: classify question → SQL tool / graph tool / retrieval tool / hybrid plan.
- Agent loop with tool calls; answers must cite climb references that exist.

**Done when:** the question set runs end to end with a pass rate you can report.

## Phase 8 — Retrieval (text) layer

- Corpus: climb comments (masked), plus external climbing technique/training content.
- Chunking, embeddings, vector store; hybrid with metadata filters (wall, grade).
- This is the prompt-injection surface — comments and web content are untrusted.

**Done when:** "how do people do the crux on X" and "how do I train slopers" return
grounded answers with sources.

## Phase 9 — Guardrails

- Input: prompt-injection detection on retrieved text; delimiting/spotlighting untrusted content.
- PII: masking pipeline for comment text before embedding (names, handles).
- Output: every climb/setter mentioned must exist in the DB; schema-validated tool outputs;
  no medical advice on injury questions (redirect).
- Red-team set: poisoned comments, hallucination bait.

**Done when:** attack-success rate and hallucinated-entity rate are measured and reported.

## Phase 10 — Evals

- Time-travel: freeze DB at date D, predict send/flash on climbs you tried after D,
  score against what actually happened.
- Tag inference P/R (Phase 5). Question-set accuracy (Phase 7). Guardrail metrics (Phase 9).
- One command runs all evals and writes a results table.

## Phase 11 — Cost and latency

- All LLM calls through one gateway module: logs model, tokens, cost, latency.
- Routing: cheap model for classification/routing, strong model for synthesis;
  fallback on errors/timeouts.
- Caching: prompt caching of static context (taxonomy, gym metadata, profile),
  semantic cache for repeat questions.
- Report: quality held vs cost/latency saved per change.

## Phase 12 — Portfolio packaging

- README: problem, architecture diagram, results table
  (tag P/R, question accuracy, time-travel send prediction, injection success rate,
  hallucination rate, cost/query, p95 latency) per system version.
- Sanitised sample dataset so anyone can run the demo without a TopLogger account.
- Ship the adapter, not credentials or scraped data.
