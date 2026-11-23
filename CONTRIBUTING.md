# Contributing to schwab-mcp

Thank you for considering contributing to **schwab-mcp**! Contributions of all kinds — bug reports, feature requests, documentation improvements, and pull requests — are welcome.

## Development Setup

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/HGZ-20/schwab-mcp.git
cd schwab-mcp
uv sync --all-extras
```

## Running Tests & Quality Checks

```bash
# Run full unit & integration test suite (mocked, never touches live Schwab accounts)
uv run pytest -v

# Lint and formatting check
uv run ruff check src tests
uv run ruff format --check src tests

# Static type checking
uv run mypy src
```

## Architecture Overview

```text
src/schwab_mcp/
├── auth/        # Schwab OAuth 2.0, Mode 1 MCP OAuth 2.1 provider, Mode 2 local HTTPS callback
├── schwab/      # Direct async REST client (Market Data v1 & Trader v1), formatters, order builders
├── risk/        # Multi-layer RiskGuard (dry-run, read-only, notional caps, symbol rules, JSONL audit)
├── server/      # MCPServer factory, Streamable HTTP ASGI app, modular tools, resources, prompts
├── cli.py       # Unified `schwab-mcp` CLI (stdio, serve, auth, doctor)
├── config.py    # XDG-compliant Pydantic Settings with env + CLI overrides
└── exceptions.py
```

## Guidelines

1. **Safety first**: Never weaken default risk guardrails (`SCHWAB_DRY_RUN=true`, `confirm_live=True`, `0600` token/audit permissions).
2. **No secrets in tests or commits**: All tests must use `respx` mocks and temporary directories (`tmp_path`). Never commit `.env`, `token.json`, `oauth_state.json`, or real account hashes.
3. **MCP protocol standards**: Every new tool must include accurate `ToolAnnotations` (`read_only_hint`, `destructive_hint`, `idempotent_hint`, `open_world_hint`) and clear docstrings.
4. **Tests required**: Include unit/integration tests for any new endpoint, order builder, risk rule, or auth behavior.

## Submitting a Pull Request

1. Fork the repository and create a feature branch (`git checkout -b feat/my-feature`).
2. Make focused, well-tested changes.
3. Ensure `pytest`, `ruff`, and `mypy` all pass cleanly.
4. Open a pull request describing the motivation, changes, and verification steps.
