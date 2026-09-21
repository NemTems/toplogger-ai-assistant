# toplogger-ai-assistant

A personal bouldering assistant over [TopLogger](https://toplogger.nu) data. It answers
questions like "which styles am I weak on", "is the gym stiff", "what should I project", and
"what's about to be stripped", built as an AI-engineering portfolio project (agents,
RAG/Graph RAG, guardrails, prompt versioning, cost/latency work). See `docs/PROJECT_CONTEXT.md`
for what is known about the data and `docs/ROADMAP.md` for the phased implementation plan.

## Development

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.12.

Install dependencies:

```
uv sync
```

Create your local config (gitignored; holds no tokens — those live in the OS keychain):

```
cp .env.example .env
```

Run the test suite:

```
uv run pytest
```

Run a single test:

```
uv run pytest tests/test_config.py::test_defaults
```

Lint and format:

```
uv run ruff check
uv run ruff format
```
