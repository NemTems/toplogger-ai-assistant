"""Sync commands: pull data from a `Source` and write it to `data/raw/`.

Source-agnostic — see `ingest.sync`. Responses are written untouched, except toppers
data, which names other climbers and is aggregated in memory into per-climb counts
before it is written.
"""
