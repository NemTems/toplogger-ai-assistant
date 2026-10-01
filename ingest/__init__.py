"""Sync commands: pull data from a `Source` and write it to `data/raw/`.

Source-agnostic — see `ingest.sync`. Responses are written untouched, with one
deliberate exception: toppers data is aggregated in memory first, because hard
rule 4 outranks the "raw is immutable" convention.
"""
