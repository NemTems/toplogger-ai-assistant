# Architecture

How the pieces fit and **why they are split where they are**. The *what* is readable from
the code; this file exists for the *why*, because most of the structure here is downstream
of the hard rules in `AGENTS.md` rather than of taste, and that is not obvious from any
single file.

Companion docs: `PROJECT_CONTEXT.md` (facts about the data), `ROADMAP.md` (phases),
`RUNBOOK.md` (how to run it).

---

## 1. Dependency direction

Everything points downward. Nothing below knows about anything above it.

```
                         ingest/cli.py
                              │                    composition root — the only
         ┌────────────────────┼──────────────┐     module that names a concrete
         ▼                    ▼              ▼     source and wires it together
   ingest/sync.py     ingest/tokens.py   sources/toplogger/adapter.py
         │                    │              │    ← the ONLY module that knows
         ▼                    ▼              ▼      TopLogger's field names
  ingest/raw_store.py    ...auth.py     ...client.py
         │                    │              │    ← transport: 1 rps, retries,
         └────────────────────┴──────┬───────┘      redaction, alias batching
                                     ▼
                       config.py   ·   sources/base.py
```

Exact internal imports, as of the Phase 2 completion:

| Module | Imports from the project |
|---|---|
| `config.py` | — |
| `sources/base.py` | — |
| `sources/toplogger/client.py` | `config` |
| `sources/toplogger/auth.py` | `config`, `…client` |
| `sources/toplogger/adapter.py` | `config`, `…client` |
| `ingest/raw_store.py` | `config`, `…client` (one helper — see §6) |
| `ingest/tokens.py` | `…auth` (see §6) |
| `ingest/sync.py` | `config`, `ingest.raw_store`, **`sources.base`** |
| `ingest/cli.py` | everything |

**`ingest/sync.py` importing `sources.base` and not `sources.toplogger` is load-bearing.**
It is what makes the protocol real rather than decorative. `tests/test_sync.py` drives it
with a stub `Source` for the same reason: if those tests needed the real adapter, the
layer would already have leaked.

`sources/toplogger/adapter.py` does **not** import `auth.py`. Tokens arrive injected
(§3.3).

---

## 2. The layers

### `config.py`
Settings from environment / `.env`, prefix `TOPLOGGER_`. Deliberately has **no**
token/password/secret field — there is a test asserting its `repr` cannot leak one, and
another asserting no field is *named* anything credential-shaped. Derived paths (`raw_dir`,
`db_path`) are properties, not fields. `get_settings()` is `lru_cache`d; the test suite
clears that cache around every test.

### `sources/base.py` — the protocol
A `runtime_checkable` `Protocol`, eight members, all in climbing-domain terms:

```
name                             fetch_climb_stats(climb_ids)
fetch_catalog()                  fetch_climb_popularity(climb_ids)
fetch_gym_metadata()             fetch_user_history(user_id)
climb_ids(catalog)               fetch_user_stats(user_id)
```

No `gymId`, no `tickType`, no GraphQL. A MoonBoard corpus or a TopLogger CSV export would
be another implementation, which is an explicit Phase 12 goal.

`fetch_climb_popularity`'s docstring is normative, not descriptive: implementations **must**
aggregate across climbers before returning. The rule lives on the protocol so a second
adapter inherits it.

### `sources/toplogger/client.py` — transport
Owns everything that is true of the endpoint regardless of what you ask it for:

- **Rate limiting.** Process-wide module state (`_rate_lock`, `_last_request_at`), not
  per-client — hard rule 5 caps the *process*, so two clients must not double the rate.
- **Retries** with backoff, and `retry=False` for non-idempotent operations (hard rule 3:
  refresh-token rotation must never be replayed).
- **Redaction.** `redact()` masks known secret values *and* anything token-shaped;
  `looks_like_jwt()` is the precise half, for callers that must refuse rather than rewrite.
- **Alias batching.** `build_aliased_query` / `post_aliased` put many climbs in one
  document (`c0: climb(…) c1: …`). IDs are inlined as validated literals, not GraphQL
  variables, because introspection is disabled and a wrongly-declared variable type fails
  validation — a string literal coerces to either `ID!` or `String!`, so it sidesteps the
  unknown. Hence `_SAFE_LITERAL` validation is mandatory, not defensive.
- **4xx bodies.** A failed-validation query returns HTTP 400 *with the errors in the body*,
  so the body is surfaced (redacted, truncated) rather than discarded. Without this,
  schema probing is impossible.

### `sources/toplogger/auth.py` — credentials
Refresh-token rotation, the OS keychain, and the sync lock. The ordering guarantee —
persist the new refresh token *before* returning — is hard rule 3, and there is a
mutation-checked test for it. `authSignin` is never called from here (hard rule 2).

### `sources/toplogger/adapter.py` — the only TopLogger-aware layer
Field names, argument shapes, auth probing, pagination, and the toppers aggregation.
Queries live in `queries/*.graphql` as **selection sets**, not standalone documents —
arguments are rendered in Python, for the literal-inlining reason above.

### `ingest/raw_store.py` — the on-disk contract
Envelope, path scheme, credential guard, atomic append-only writes. See §4.

### `ingest/tokens.py` — one token per sync
`get_access_token()` rotates the stored refresh token on **every** call, and the access
token lives 10 minutes. Calling it per request would rotate the credential hundreds of
times per sync, each rotation a window where a crash costs a manual browser login. So the
provider caches and re-acquires on a TTL (480s, under the 10-minute expiry).

It is **lazy**: a fully-public `catalog` sync never acquires a token at all.

### `ingest/sync.py` — orchestration
Calls protocol methods, hands results to the raw store, returns a `SyncReport`. Knows
nothing about GraphQL or TopLogger.

### `ingest/cli.py` — composition root
Typer commands, the sync lock, and error rendering. The only place a concrete source is
named. A composition root knowing concrete types is normal; everything below it not
knowing them is the point.

---

## 3. The three seams

### 3.1 The `Source` protocol — swaps the provider
Discussed above. The subtle member is **`climb_ids(catalog)`**: `sync.py` must pick which
climbs to fetch stats for, which means reading a stored catalog payload. Rather than teach
`ingest/` a field name, it hands the payload back to the source that produced it.

### 3.2 Raw files — swaps the live API for replayable history
`data/raw/<source>/<kind>/<YYYY-MM-DD>T<HHMMSS>Z.json`. Every response is written before
anything parses it. Loaders (Phase 3) read **files, never the live API**.

Two consequences worth stating, because they look like inefficiency otherwise:

- **Snapshots accumulating is the feature.** A climb vanishing between two catalogs is how
  its strip date is derived. Nothing is ever overwritten; a same-second collision gets a
  `_01` suffix (`_`, not `-`, because `-` sorts *before* `.json` and would silently break
  `latest_raw`'s newest-by-name shortcut).
- **`stats` and `toppers` read climb IDs from the stored catalog, not a fresh fetch.** It
  keeps a day's snapshots describing the same wall even if a climb is stripped mid-sync.

### 3.3 `token_supplier` — keeps `sources/` free of `ingest/`
The adapter never calls `get_access_token()`; it receives a `Callable[[], SecretStr]`.
That inverts what would otherwise be an upward dependency, and it lets the token cache sit
*above* the adapter while still being consulted per request.

**The supplier must be called per request, never memoised on the adapter.** The TTL check
only runs when the supplier is called, and a `toppers` crawl at 1 rps can legitimately
outlive a 10-minute token. A cached `SecretStr` makes a long sync die partway through.
There is a test pinning this.

---

## 4. The raw-file contract

```json
{ "source": "toplogger", "kind": "catalog",
  "fetched_at": "2026-09-23T09:02:41Z",
  "operation": "fetch_catalog", "variables": {…},
  "aggregated": false,
  "payload": { … the provider's response, verbatim … } }
```

- **Refuses, never redacts.** If the serialised bytes carry a JWT shape or a known secret,
  the write is refused and *nothing* is created. A rewritten payload is a corrupted
  recording that looks fine — the worse failure. This is why `looks_like_jwt` exists
  separately from `redact`: `redact`'s deliberately broad "40+ opaque characters" rule
  would fire on a long `picPath`.
- **Atomic.** Temp file then `os.replace`, so a killed run never leaves a half-written file
  a loader will happily read as truth.
- **Scans the whole envelope**, not just the payload — `variables` is the likelier place
  for a credential to land.

### The one exception: `aggregated: true`

`toppers` is **not** a faithful recording. `climbUsers` returns other climbers' names, user
IDs and avatars; hard rule 4 outranks "raw is immutable", so the adapter aggregates in
memory and only per-climb counts are written. The flag is in the envelope so a reader of
the file does not have to know that from somewhere else.

Two independent defences, deliberately not relying on each other:

1. `climb_users.graphql` does not *ask* for identifying fields.
2. `aggregate_toppers` reads only `tickType` and `tickedFirstAtDate` off each row, so
   nothing else survives the call stack even if someone edits the query later.

---

## 5. What the hard rules actually shaped

| Rule | Structural consequence |
|---|---|
| 1 · secrets | `raw_store` refuses rather than redacts; `config` has no secret field; errors carry codes, not bodies, except redacted 4xx excerpts |
| 2 · never automate login | No `authSignin` path exists at all |
| 3 · rotation | `ingest/tokens.py` exists; `post_graphql(retry=False)`; persist-before-return, mutation-tested |
| 4 · other people's data | Aggregation lives *inside* the adapter; `aggregated` flag; the protocol docstring is normative |
| 5 · politeness | Throttle is process-wide state; batching is a transport concern; no parallelism anywhere |
| 6 · tests offline | Every entry point takes injectable `client=` / `sleep=` / `clock=` |
| 7 · never commit `data/` | `test_repo_hygiene.py` asserts it, and scans tracked files for credential shapes |

The two safety guarantees (rules 1 and 4) are **mutation-checked**, not merely asserted:
breaking the toppers aggregation, or disabling the raw-file credential guard, each makes
the relevant tests fail. A guard whose test passes vacuously is worse than no guard.

---

## 6. Known warts

Both are real, both are small, neither blocks anything:

- **`ingest/raw_store.py` imports `looks_like_jwt` from `sources/toplogger/client.py`.**
  The helper is source-agnostic; only its location is not. Fix: move it to a shared module.
- **`ingest/tokens.py` imports `sources/toplogger/auth.py`.** The token cache is
  fundamentally about TopLogger's auth model, so it arguably belongs in
  `sources/toplogger/`. Fix: move it there.

Neither carries a TopLogger *field name*, so neither breaks the letter of the "nothing
downstream knows the source" rule — but both make `ingest/` less source-agnostic than
`sync.py`'s own docstring claims. The day a second adapter appears, do both.

---

## 7. Not built yet

`load/`, `features/`, `assistant/`, `evals/`, `prompts/` are `__init__.py` only. There is
no `schema.sql`. See `ROADMAP.md` for what goes in each.

The one architectural decision already made about them: **three layers, three tools** —
personal ticks to SQL, catalog and relationships to a graph, text to vector retrieval.
Never semantic search over structured personal data. And all LLM calls go through a single
gateway (`assistant/llm.py`) that logs model, prompt version, tokens, cost and latency.
