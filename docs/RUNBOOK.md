# Runbook — running a sync

Operational notes for Phase 2 ingestion. Facts about the data live in
`PROJECT_CONTEXT.md`; the rules live in `AGENTS.md`. This file is only about what to
type and what to do when something goes wrong.

## First run, in order

```bash
uv sync

# 1. Answer the open questions in PROJECT_CONTEXT §6 with real requests.
#    Writes no climbing data. Record what it prints in §6.
uv run python -m ingest probe

# 2. See what a sync would do, without touching the network.
uv run python -m ingest catalog --dry-run

# 3. For real.
uv run python -m ingest catalog
uv run python -m ingest stats   --max-climbs 25
uv run python -m ingest toppers --max-climbs 5
uv run python -m ingest me
```

`stats` and `toppers` read their climb IDs from the newest catalog snapshot on disk,
so `catalog` has to run first. `--max-climbs` exists to keep a first real run cheap;
drop it once the output looks right.

`uv run python -m ingest all` runs everything in dependency order on a single source
instance, which pays for the unauthenticated probe and the access token once instead
of four times.

## What lands on disk

```
data/raw/toplogger/catalog/2026-09-23T101500Z.json
data/raw/toplogger/gym/…
data/raw/toplogger/stats/…
data/raw/toplogger/toppers/…      ← aggregated, see below
data/raw/toplogger/history/…
data/raw/toplogger/user_stats/…
data/raw/toplogger/_probe/…
```

Files are append-only and never overwritten — a second run on the same day adds a
second timestamped file. That is the point: the historical catalog is built by
diffing snapshots, and a climb that disappears between two of them gives its strip
date.

`toppers` is by far the most expensive command. `climbUsers` is per-climb, cannot be
alias-batched, and its `perPage` is capped at 10, so a climb with 90 tickers costs 9
requests. Measured 2026-09-23: **5 climbs took 43 requests**. At ~150 climbs that is
roughly 20 minutes at 1 rps. Use `--max-climbs` unless you mean it.

`toppers` is also the one kind that is **not** a faithful recording. The upstream
response carries other climbers' names and user IDs; hard rule 4 outranks the
"raw is immutable" convention, so the adapter aggregates in memory and only
per-climb counts are written. Those files carry `"aggregated": true`.

`data/` is gitignored in its entirety (hard rule 7).

## Tokens

The first run needs a refresh token. Log in to TopLogger in a browser, copy the
refresh token from devtools, and paste it into `.env` as `TOPLOGGER_REFRESH_TOKEN=…`.
The next run imports it into the OS keychain and tells you to delete the line.
Nothing ever writes a token back to `.env`.

The token rotates on every use and is valid for 14 days, sliding. **Sync at least
once a fortnight and you never have to log in by hand again.** Miss that window and
it is a manual browser login.

One sync acquires one access token and reuses it, rather than rotating the stored
refresh token once per request.

## When it goes wrong

| Symptom | What it means | What to do |
|---|---|---|
| `RefreshTokenMissing` | Nothing in the keychain, nothing in `.env` | Paste a fresh refresh token into `.env` and re-run |
| `RefreshTokenExpired` | The stored token was rejected — expired, revoked, or already used | Paste a fresh one into `.env`. A *different* value there overrides the dead keychain copy automatically |
| `RefreshTokenRotationFailed` | A new token was issued but the keychain write failed. The old one is now dead | Manual browser login; this is the bad one |
| `SyncAlreadyRunning` | Another sync holds `data/sync.lock` | Wait for it. If nothing is running, delete the lock file |
| `SyncError: no catalog snapshot…` | `stats`/`toppers` ran before `catalog` | Run `catalog` first |
| `RawWriteRefused` | Something credential-shaped was about to be written to a data file | Do not work around it. Report it — it means a token reached a payload |

## Scheduling

Not installed by anything here — arm it yourself when the output has settled.
The roadmap wants **catalog daily** and **me weekly**.

### launchd (macOS)

`~/Library/LaunchAgents/nu.toplogger.sync-catalog.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>nu.toplogger.sync-catalog</string>
  <key>WorkingDirectory</key>   <string>/Users/andrii/Documents/Projects/personal/toplogger-ai-assistant</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/uv</string>
    <string>run</string>
    <string>python</string>
    <string>-m</string>
    <string>ingest</string>
    <string>catalog</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>   <integer>4</integer>
    <key>Minute</key> <integer>30</integer>
  </dict>
  <key>StandardOutPath</key>    <string>/tmp/toplogger-sync.log</string>
  <key>StandardErrorPath</key>  <string>/tmp/toplogger-sync.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/nu.toplogger.sync-catalog.plist
launchctl start nu.toplogger.sync-catalog     # run once now to check it works
```

Duplicate the file with `me` in place of `catalog` and a `<key>Weekday</key>` entry
for the weekly pull.

Check `which uv` — the path above assumes Homebrew on Apple Silicon. launchd jobs get
almost no environment, so an absolute path is not optional.

### cron

```cron
30 4 * * *  cd /path/to/toplogger-ai-assistant && /opt/homebrew/bin/uv run python -m ingest catalog  >> /tmp/toplogger-sync.log 2>&1
15 5 * * 1  cd /path/to/toplogger-ai-assistant && /opt/homebrew/bin/uv run python -m ingest me       >> /tmp/toplogger-sync.log 2>&1
```

### Before you trust a scheduled job

A scheduler that fails silently is worse than none. The keychain in particular
behaves differently outside a login session — run the job once by hand through
`launchctl start`, confirm a file actually appeared under `data/raw/`, and check the
log. Then set a reminder to look again a fortnight later, because that is when an
unnoticed failure costs you a manual login.
