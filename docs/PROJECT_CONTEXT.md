# Project context — what we know

A personal bouldering assistant built on TopLogger data. It answers questions about
your climbing: which styles you're weak on, whether you're progressing, how the gym
grades against community consensus, what to project next, what's about to be stripped.

It doubles as an AI-engineering portfolio project: agents, RAG, guardrails
(input/output validation, PII masking, prompt-injection defence), prompt versioning,
and cost/latency work (caching, model routing, fallback).

---

## 1. Data access — final picture

**Endpoint:** `POST https://app.toplogger.nu/graphql`
Accepts a JSON array of operations (batching) and GraphQL aliases
(`c1: climb(...) c2: climb(...)`), so many climbs fit in one request.

| Data | Auth | Status |
|---|---|---|
| Gym catalog (`climbs`) | none | **confirmed** public |
| Gym metadata (walls, colours, setters, tags, groups) | none | **confirmed public 2026-09-23** |
| Per-climb grade/rating histograms | none | **confirmed public 2026-09-23** |
| Per-climb toppers list (`climbUsers`) | **required** | **confirmed 2026-09-23** — the one public-looking endpoint that is not |
| Anything about the user (sessions, ticks, stats) | **required** | **confirmed** — returns `UNAUTHENTICATED` |

A token-free public demo is therefore possible for everything except toppers.

**Official position:** TopLogger has no public API. They said a CSV/XLSX export of
*your own logs* is planned, no date. A follow-up asked whether it will include climb
metadata; unanswered. Don't plan around it.

### Auth flow

- `authSignin(email, password, recaptchaToken)` — needs a reCAPTCHA token.
  **Manual only.** Never automate or work around it.
- `authSigninRefreshToken(refreshToken: JWT!)` returns
  `{ access { token expiresAt } refresh { token expiresAt } }`.
- Access token lives **10 minutes**.
- Refresh token lives **14 days, sliding and rotating**: every refresh returns a new
  refresh token with a fresh 14-day expiry. Sync at least once every 14 days and a
  manual login is never needed again.
- Treat the old refresh token as dead after use. Save the new one *before* anything else.
- **Confirmed 2026-09-21:** the refresh mutation works with the refresh token sent in
  *both* the variable and `Authorization: Bearer`. Whether the header is *required*
  is still untested (the first attempt succeeded, so no negative case was run).
- **Confirmed 2026-09-21:** `authSigninRefreshToken` returns `AuthTokens` **directly**
  — `{ access {...} refresh {...} }`, with no `tokens` wrapper. Verified by a probe:
  selecting `tokens` fails validation with *Cannot query field "tokens" on type
  "AuthTokens"*. Note `authSignin` is different: it returns `Auth { tokens { ... } user }`.
- GraphQL **introspection is disabled** (`INTROSPECTION_DISABLED` from Apollo Server), so
  schema shapes must be probed with deliberately invalid queries — validation errors name
  the type, and run before execution, so they cost no token.

### Known IDs

- Gym `falkors`: `6k5d6kkbrp6y4rqmj84ip`
- User ID: set locally in config (`TOPLOGGER_USER_ID`), not committed.

---

## 2. Queries captured

All from browser devtools on the TopLogger web app.

**Public catalog** — `climbs(gymId, climbType: boulder) { data { ... } }`. Useful fields:
`id grade gradeAuto gradeTicks ticksCount ticksCountLabel ratingsAverage commentsCount
inAt outPlannedAt picPath videoPath wall{id nameLoc} holdColor{id color}
climbSetters{gymAdmin{id name}} climbTagClimbs{communitySuggestion climbTag{nameLoc type}}`

**Gym metadata** — `gym(gymId) { holdColors walls setters climbGroups climbTags }`

**Per-climb stats** — `climb(gymId, id) { gradeAdmin gradeVoteStats{grade count}
ratingVoteStats{stars count} }`

**Toppers** — `climbUsers(gymId, climbId, ticked: true, pagination)` returns names,
user IDs, avatars, dates, `tickType`. **Aggregate at ingest; never store identities.**
We ask only for `tickType` and `tickedFirstAtDate`, so nothing identifying arrives at all.
**Requires auth**, and `pagination.perPage` is capped at 10 — see §6.

**User history (auth)** —
- `climbDays(userId, gymId, gymIdNotNull, totalTriesMin, statsAtDateMin/Max)` with nested
  `climbUserDays(climbType: "boulder", limit: N) { tickType wasRepeat climb{...} }` —
  per-session ticks, **including climbs since stripped**.
  **Correction (2026-09-23):** `gymIdNotNull` is a `Boolean`, not an id — the gym filter
  is a separate `gymId`. And `limit:` is *not* capped at 10; we send 100. See §6.
- `user(id).gradeDistribution(climbType, climbedAtMin/Max)` — counts per grade.
  **Correction (2026-09-23):** the fields are `GradeCount { grade count countRp countFl }`;
  there is no `rp`/`fl`, and `climbType` here is an unquoted enum.
- `climbDays(userId) { statsAtDate bouldersGrade }` — TopLogger's own rating of you over time.
- `climbDays(...) { bouldersRpGrade bouldersFlGrade }` — redpoint vs flash level over time.
- `climbDayTotalsByWeek` — weekly tick totals.
- `climbUsers(userId, pointsMin: 1)` — top-ten scoring ticks.

**Not yet captured:** comment text for a climb; logbook-style per-tick try counts over
time (if distinct from `climbUserDays`); media base URL.

---

## 3. Field semantics and encodings

**Grades** — integer, Font scale × 100 with sixths:
`600` 6A · `617` 6A+ · `633` 6B · `650` 6B+ · `667` 6C · `683` 6C+ ·
`700` 7A · `717` 7A+ · `733` 7B …
Below 5A the steps are irregular (250, 300, 367, 400, 433, 467 seen) — treat as "easy".
`0` means ungraded placeholder — filter out.

**tickType** — `2` flash (gets +10 bonus points), `1` redpoint/top (`rp`).
`0` presumably tried-not-sent — still unconfirmed, but rows with `tickType: 0` also carry
`tickedFirstAtDate: null`, which fits (§6.5). Counted separately, never folded into either.

**ratingsAverage** — mean of `(stars − 1) × 25`, so 0–100. 75 = four stars.

**ticksCountLabel** — `soft` / `hard` / null. Inferred meaning: tick count relative to
what's expected for that grade and time on the wall (a 6C with 26 ticks → soft,
with 7 ticks → hard). Recomputable ourselves as a continuous feature from
`ticksCount`, `grade`, `inAt`.

**gradeTicks** — **not a reliable difficulty signal.** Contradicts its own labels,
floors around 600 for easy climbs, defaults to 783 when there are no ticks. Ignore.

**gradeAuto** — true when community grading is active. **gradeAdmin** may read 0;
whether the setter's original grade is recoverable is unverified — in the first real
stats pull it read `0` on every climb that had community votes (§6.7).

**Vote histograms** are tiny (often 1–4 votes). Any consensus grade needs a minimum
vote count or shrinkage toward the setter grade.

### Data quality rules

- Filter climbs with `grade == 0`.
- `Guest Setter` is a catch-all, not a person. Exclude from setter-style analysis.
- `climbSetters` can be empty.
- Climbs have no names (`name: null`). Human reference = hold colour name + wall + set date.
  Colour names come from gym metadata `holdColors`.
- Wall `overhang` is 0 everywhere — no steepness data.
- Tags are sparse (~1 climb in 6). `communitySuggestion` distinguishes setter vs user tags.
  24 tags in two types — `HOLD` (Jugs, Crimps, Slopers, Pinches, Pockets, Ledges,
  Volumes, Dual texture) and `CLIMB` (Powerful, Technical, Dynamic, Dyno, Reachy,
  Flexibility, Puzzle, Pumpy, Traverse, Scary, Compression, Expansion, Footwork,
  Dihedral, Edge, Mantle).
- The catalog is *current wall only*. A climb disappearing between snapshots gives its
  strip date. Stripped climbs in the user's history carry `outAt`.
- Toppers lists exclude non-public users. Use `ticksCount` for totals; use toppers only
  for the flash/redpoint ratio.
- `picPath` / `videoPath` are relative paths; base URL not yet known.

---

## 4. Decisions already made

**Architecture**
- **Source adapter protocol** — nothing downstream knows where data came from.
  TopLogger now; MoonBoard or a TopLogger export later are just new adapters.
- **Three layers, three tools.** Personal ticks → SQL. Catalog and relationships → graph.
  Text (comments, beta, training knowledge) → vector retrieval. Never semantic search
  over structured personal data.
- **Raw is immutable.** Every API response is saved untouched, dated, before parsing.
  Loaders read from raw files, never from the live API.
- **Snapshots, not overwrites.** Catalog pulled daily; every row carries `fetched_at`.
  This builds the historical catalog and enables time-travel evaluation.

**Schema** (starting point: `schema.sql`, `sources/base.py`)
- `climb_tag` keeps `origin` (source / inferred / hand_label) and `model_version` in its
  key. Hand labels are the test set for tag inference.
- `comment.body_raw` vs `body_masked` — raw text never reaches a model.
- `grade_norm` numeric column for comparison across systems.
- Replace per-person `grade_opinion` with aggregate tables:
  `grade_vote_stats(climb_id, grade, count, fetched_at)` and
  `rating_vote_stats(climb_id, stars, count, fetched_at)`.
- Topper data stored only as per-climb aggregates (flash count, redpoint count,
  ticks per week since `inAt`).

**Policy**
- No other users' names, IDs or avatars are ever persisted.
- Tokens never committed, logged or printed.
- Personal use; rate-limited; the scraped corpus is never published.
  Public repo ships the adapter plus a sanitised sample dataset.

---

## 5. Secondary source: MoonBoard

Kept as fallback and as a possible second corpus. Public data, no login needed:
- `github.com/jrchang612/MoonBoardRNN` — scraped 2016 problems plus expert-rated hold
  difficulty per hand.
- `github.com/e-sr/moonboard` — problems and hold setups.
- Published grade-prediction baselines exist (~40–51% exact, ~77–83% within one grade;
  humans ~45% / ~85%) — useful comparison points.
Don't mix board versions.

---

## 6. Open questions

1. ~~Do gym metadata, per-climb stats and toppers work without a token?~~
   **Answered 2026-09-23** by the first ingestion run — see the auth table in §1.
   Catalog, gym metadata and per-climb stats are all public; **toppers are not**.
2. ~~Is `climbUserDays(limit: 100)` honoured or capped at 10?~~
   **Answered 2026-09-23: honoured.** Not a cap — the web app's `limit: 10` is a UI
   choice. Measured over the same window on a real account: 297 ticks at `limit: 10`,
   **391 at `limit: 100`**, busiest day 69 climbs. Using 10 silently drops about a
   quarter of the tick history, so `DEFAULT_SESSION_CLIMB_LIMIT` is 100.
3. Comment query and text shape. *(Still open — Phase 8.)*
4. Media base URL for `picPath` / `videoPath`. *(Still open.)*
5. Meaning of `tickType: 0`. **Partial evidence 2026-09-23:** dropping `ticked: true`
   from `climbUsers` returns rows with `tickType: 0` *and* `tickedFirstAtDate: null`,
   consistent with "tried, not sent". Not yet conclusive; keep counting it separately.
6. Is the `Authorization` header *required* on the refresh mutation? *(Still open —
   the positive case works, no negative case has been run.)*
7. Is `gradeAdmin` (setter's grade) recoverable once community grading kicks in?
   **Leaning no:** in the first real stats pull, `gradeAdmin` reads `0` on climbs that
   have community grade votes. Not yet checked against a climb with `gradeAuto: false`.

### Schema facts established by probing (2026-09-23)

Introspection is disabled, but **a query that fails validation comes back as HTTP 400
with the errors in the body** — not as a 200 carrying an `errors` array. Validation runs
before execution, so this costs no token and reads no data. Apollo often adds
*Did you mean …?*. This is the cheapest tool we have for reading the schema; it is wired
up as `uv run python -m ingest probe`.

- `climbUsers` → type **`ClimbUsersPaginated`**, whose `data` is a list of **`ClimbUser`**.
  Valid fields on `ClimbUser` include `tickType`, **`tickedFirstAtDate`**, `points`,
  `wasRepeat`, `ticked`, `gradedAt`. (`tickedAt`, `date`, `tickedFirstAt` do not exist.)
- `climbUsers(pagination: {page:, perPage:})` is the right shape, but **`perPage` is
  capped at 10** — `11` returns `BAD_REQUEST`, `10` and below succeed. Omitting
  `pagination` also yields 10. So toppers cost one request per 10 tickers per climb.
- **Unauthenticated `climbUsers` reports `BAD_REQUEST` for a real climb id** but
  `UNAUTHENTICATED` for a nonexistent one. The error code is not a reliable signal that
  authentication is the fix — the adapter therefore retries the first attempt at any
  operation with a token whatever the code says.
- `climbDays(gymIdNotNull:)` is a **`Boolean`**, not an id — it means "only days at some
  gym". The gym filter is a separate `gymId` argument. §2's phrasing implied otherwise.
- `user.gradeDistribution` returns **`GradeCount { grade, count, countRp, countFl }`**.
  §2's "split rp/fl" is not the field naming: `rp` and `fl` do not exist.
- `gradeDistribution(climbType:)` takes an **enum** (`boulder`, unquoted) while
  `climbUserDays(climbType:)` takes a **string** (`"boulder"`). TopLogger is genuinely
  inconsistent here; both are reproduced as the server accepts them.
- `climb(gymId:)` is declared `ID!`. A quoted string literal coerces to it, which is why
  the adapter inlines validated literals instead of declaring typed variables.

### First real pull (2026-09-23, gym `falkors`)

179 climbs in the catalog, **26 of them `grade: 0`** (~15%), leaving 153 after the §3
filter. Grades observed: 300, 400, 433, 467, 500, 517, 533, 550, 567, 583, 600, 617, 633…
— consistent with the Font×100-with-sixths encoding. Personal history: 102 session days,
391 ticks.
