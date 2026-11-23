# Authentication Guide

`schwab-mcp` supports two first-class authentication architectures tailored to how you run your AI agents.

---

## Mode 1: Remote Hosted MCP OAuth 2.1 (Cloudflare-style)

Best when you host `schwab-mcp` on a VPS, cloud VM, or home server with a public HTTPS domain and want zero-configuration, one-click login from **Cursor, Codex, Claude Desktop, Claude Code, or any remote MCP client**.

### How It Works

```text
MCP Client (Cursor / Codex)       schwab-mcp (Public HTTPS)          Charles Schwab OAuth 2.0
          |                                   |                                   |
          |--- POST /mcp (no token) --------->|                                   |
          |<-- 401 + resource_metadata -------|                                   |
          |--- GET /.well-known/oauth-* ----->|                                   |
          |--- POST /register (DCR) --------->|                                   |
          |--- Browser: GET /authorize ------>|--- 302 Schwab /v1/oauth/authorize>|
          |                                   |                                   | (User approves)
          |                                   |<-- GET /schwab/callback?code=... -|
          |                                   |--- POST /v1/oauth/token --------->|
          |                                   |<-- Schwab access + 7d refresh ----|
          |<-- 302 client redirect?code=... --|                                   |
          |--- POST /token (PKCE verifier) -->|                                   |
          |<-- MCP access + refresh tokens ---|                                   |
          |--- POST /mcp (Bearer token) ----->| (Maps MCP subject -> Schwab token)|
```

### Configuration Checklist

1. In [Schwab Developer Portal](https://developer.schwab.com/), add your public callback URL:
   ```text
   https://schwab-mcp.example.com/schwab/callback
   ```
2. Configure environment variables (or CLI flags) on your server:
   ```dotenv
   SCHWAB_APP_KEY=your_app_key
   SCHWAB_APP_SECRET=your_app_secret
   MCP_PUBLIC_URL=https://schwab-mcp.example.com
   MCP_AUTH_MODE=oauth
   ```
3. Add the MCP server URL in your client:
   ```json
   {
     "mcpServers": {
       "schwab": {
         "url": "https://schwab-mcp.example.com/mcp"
       }
     }
   }
   ```

### Multi-User Isolation & 7-Day Refresh Expiry

- Every authenticated MCP session is bound to a hashed `subject` derived from Schwab's `id_token` (or a random 256-bit subject ID).
- Both Schwab tokens (`token.json` canonical `subjects` map) and cached account lists are isolated per `subject`.
- Access tokens auto-refresh transparently. When Schwab's hard 7-day refresh token expires, `/token` returns `invalid_grant` and `/mcp` returns `401`, prompting your MCP client to display a clean one-click **Reconnect** button.

---

## Mode 2: Local Any-Agent (`uvx schwab-mcp` stdio)

Best when running directly on your laptop/workstation with no server or public domain.

### Shared Local Token Storage

By default, `schwab-mcp` stores tokens in your user config directory (`0600` permissions):
- **Linux / macOS**: `~/.config/schwab-mcp/token.json` (respects `$XDG_CONFIG_HOME`)
- **Windows**: `%APPDATA%\schwab-mcp\token.json`
- **Custom**: set `SCHWAB_DATA_DIR` or `SCHWAB_TOKEN_PATH`

Authenticate once and **every local agent** (Cursor, Codex, Claude Desktop, Claude Code, Antigravity) immediately shares the authenticated session for 7 days.

### Eliminating Localhost HTTPS Certificate Warnings with `mkcert` (Optional)

Because Charles Schwab mandates `https://` even for `127.0.0.1` callbacks, an auto-generated self-signed certificate triggers a browser warning ("Your connection is not private -> Advanced -> Proceed").

To make Mode 2 **100% warning-free**, generate a locally trusted cert once using [mkcert](https://github.com/FiloSottile/mkcert):

```bash
# Install local CA once
mkcert -install

# Generate trusted cert for 127.0.0.1 and localhost
mkdir -p ~/.config/schwab-mcp/certs
mkcert \
  -cert-file ~/.config/schwab-mcp/certs/mkcert.crt \
  -key-file ~/.config/schwab-mcp/certs/mkcert.key \
  127.0.0.1 localhost ::1
```

Then configure:
```dotenv
SCHWAB_TLS_CERT_PATH=~/.config/schwab-mcp/certs/mkcert.crt
SCHWAB_TLS_KEY_PATH=~/.config/schwab-mcp/certs/mkcert.key
```

Verify with `schwab-mcp doctor` (`"trusted_local_tls_cert": true`).

### Headless / SSH Server Fallback

If `start_auth` runs on a headless server without a browser (or if you prefer not to click past a self-signed certificate):
1. Open the `authorization_url` returned by `start_auth` in any browser.
2. Approve access on Charles Schwab.
3. Copy the full redirected URL (`https://127.0.0.1:8182/?code=...&state=...`) from the address bar (a "connection refused" page is expected).
4. Paste it directly into chat — the agent calls `complete_auth(callback_url)` and resumes your task.
