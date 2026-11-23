# Security & Risk Architecture

Because `schwab-mcp` can interface with real brokerage accounts, security and trading safety are enforced at every layer.

---

## 1. Multi-Layer Trading Risk Guardrails (`RiskGuard`)

| Layer | Configuration | Behavior |
| :--- | :--- | :--- |
| **1. Global Dry-Run** | `SCHWAB_DRY_RUN=true` *(default)* | All order placement & replacement tools route to Schwab's official `previewOrder` endpoint. **Cannot be bypassed by AI tool arguments (`dry_run=False` / `confirm_live=True`).** |
| **2. Global Read-Only** | `SCHWAB_READ_ONLY=true` | Completely blocks live order submission, replacement, and cancellation. |
| **3. Per-Order USD Cap** | `SCHWAB_MAX_ORDER_USD=5000.0` | Rejects any order whose estimated notional exceeds the cap. Automatically fetches live quotes for `MARKET` and `TRAILING_STOP` orders; sizes `OCO` by max child leg and `TRIGGER`/Bracket orders by primary entry leg. |
| **4. Symbol Rules** | `SCHWAB_ALLOWED_SYMBOLS` / `SCHWAB_BLOCKED_SYMBOLS` | Enforced against both equity tickers and extracted underlyings of OCC option contracts. |
| **5. Explicit Confirmation** | `confirm_live=True` | Even when `SCHWAB_DRY_RUN=false`, live submission and cancellation require explicit `confirm_live=True`. |
| **6. Structured Audit Log** | `trade_audit.jsonl` (`0600`) | Every rejected, dry-run, preview, live, and cancel action is appended with UTC timestamps and full order payload. |

---

## 2. OAuth & Credential Storage Security

- **Restrictive File Permissions (`0600`)**: `token.json`, `oauth_state.json`, `trade_audit.jsonl`, and generated TLS private keys are written atomically via `tempfile.mkstemp` + `os.replace` with `0600` owner-only permissions.
- **No Secrets in Tool Outputs**: `get_auth_status`, `schwab://auth/status`, `/health`, and `schwab-mcp doctor` never return `access_token`, `refresh_token`, `SCHWAB_APP_SECRET`, or raw `id_token` payloads.
- **OAuth 2.1 Protections (Mode 1)**:
  - Mandatory PKCE (`S256`) enforced by the MCP SDK token handler.
  - 256-bit cryptographically random `state`, authorization codes, access tokens, and refresh tokens (`secrets.token_urlsafe(32)`).
  - RFC 8707 `resource` indicator validation (`validate_token_resource=True`) preventing cross-resource token replay.
  - One-time authorization code consumption and automatic refresh token rotation (old access + refresh tokens immediately invalidated).
  - Full revocation support via `/revoke` that also purges orphaned upstream Schwab tokens.
  - Per-subject isolation of Schwab tokens and cached account numbers.
- **DNS Rebinding Protection**: Automatically enabled on localhost and configurable for public hosts via `TransportSecuritySettings`.

---

## 3. Reporting Security Vulnerabilities

Please **do not** report security vulnerabilities in public GitHub issues. Contact the maintainers privately or open a GitHub Security Advisory with reproduction details.
