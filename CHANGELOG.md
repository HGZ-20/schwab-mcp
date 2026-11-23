# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-23

### Added
- **Mode 1 — Remote Hosted MCP OAuth 2.1 (Cloudflare-style Connect flow)**:
  - Full standards-compliant **MCP `2026-07-28`** Authorization Server (`/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`, Dynamic Client Registration `/register`, PKCE `/authorize`, `/token` with refresh rotation, `/revoke`, RFC 8707 resource validation).
  - Upstream Charles Schwab OAuth 2.0 proxy via `/schwab/callback` with per-subject token and account cache isolation.
  - Persistent `0600` OAuth state storage across restarts, optional `--stateless-http` mode, and public `/health` endpoint.
- **Mode 2 — Pure Local Any-Agent Support (`uvx schwab-mcp`)**:
  - XDG-compliant user storage (`~/.config/schwab-mcp/`) shared seamlessly across Cursor, Codex, Claude Desktop, Claude Code, and Antigravity.
  - CLI credential & guardrail overrides (`--app-key`, `--app-secret`, `--callback-url`, `--dry-run`/`--live`, `--read-only`, `--max-order-usd`).
  - In-chat `start_auth` & `complete_auth` MCP tools with **MCP `2026-07-28` URL-mode Elicitation** (`ctx.elicit_url` + `send_elicit_complete`), automatic browser opening, temporary local HTTPS callback capture (auto-generated SAN self-signed cert or user-supplied `mkcert` trusted cert), and manual URL-paste fallback.
- **Latest MCP Protocol (`2026-07-28`, SDK v2.3)**:
  - Stateless `server/discover` and per-request `_meta` protocol negotiation (`io.modelcontextprotocol/protocolVersion`).
  - `ToolAnnotations` (`read_only_hint`, `destructive_hint`, `idempotent_hint`, `open_world_hint`) on every tool.
  - Read-only MCP Resources (`schwab://auth/status`, `schwab://risk/config`, `schwab://accounts`).
  - Guided MCP Prompts (`safe_order_checklist`, `analyze_position`, `option_chain_review`).
- **Unified CLI**: `schwab-mcp` (default stdio), `schwab-mcp serve`, `schwab-mcp auth <login|status|refresh|logout>`, and `schwab-mcp doctor`.
- **Production Packaging & Open-Source Tooling**: Multi-stage `Dockerfile`, `docker-compose.yml` with Caddy auto-TLS, GitHub Actions CI & PyPI release workflows, comprehensive English & Chinese documentation.

### Changed
- Complete modular refactor into `auth/`, `schwab/`, `risk/`, and `server/` packages.
- Upgraded `mcp` dependency to `>=2.3.0` (`2026-07-28` specification) and trimmed unrelated runtime dependencies (`numpy`, `websockets`).
- Adopted a single canonical `TokenStoreDocument` (`version: 1`, `subjects`) schema and single `schwab-mcp` CLI entrypoint without legacy compatibility shims.

### Removed
- Removed unrelated backtesting, paper-trading strategies, notification bots, and cached bar datasets to keep the repository strictly focused as a production Charles Schwab MCP Server.
