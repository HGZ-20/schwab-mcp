# Deployment Guide (Mode 1 Remote Hosting)

Deploy `schwab-mcp` behind HTTPS to get a **Cloudflare-like one-click OAuth 2.1 MCP server** accessible from Cursor, Codex, Claude Desktop, Claude Code, or any remote agent.

---

## Option A: Docker Compose + Caddy (Recommended — Automatic HTTPS)

1. Point a DNS `A` / `AAAA` record (e.g. `schwab-mcp.example.com`) to your server's public IP.
2. Open ports `80` and `443` in your firewall.
3. In [Charles Schwab Developer Portal](https://developer.schwab.com/), add:
   ```text
   https://schwab-mcp.example.com/schwab/callback
   ```
4. Configure `.env`:
   ```dotenv
   SCHWAB_APP_KEY=your_app_key
   SCHWAB_APP_SECRET=your_app_secret
   MCP_PUBLIC_URL=https://schwab-mcp.example.com
   MCP_PUBLIC_DOMAIN=schwab-mcp.example.com
   SCHWAB_DRY_RUN=true
   SCHWAB_MAX_ORDER_USD=5000.0
   ```
5. Launch:
   ```bash
   docker compose up -d
   curl https://schwab-mcp.example.com/health
   ```

---

## Option B: Cloudflare Tunnel (Zero Open Inbound Ports)

If you don't want to expose ports `80`/`443` on your VPS or home server:

1. Run `schwab-mcp` locally on `127.0.0.1:8765`:
   ```bash
   uvx schwab-mcp serve \
     --host 127.0.0.1 \
     --port 8765 \
     --public-url https://schwab-mcp.example.com
   ```
2. Create a Cloudflare Tunnel routing `https://schwab-mcp.example.com` -> `http://127.0.0.1:8765`.
3. Register `https://schwab-mcp.example.com/schwab/callback` in Schwab Developer Portal.

---

## Option C: Nginx Reverse Proxy + Systemd

Make sure Nginx disables response buffering so Streamable HTTP SSE streams flush immediately:

```nginx
server {
    listen 443 ssl http2;
    server_name schwab-mcp.example.com;

    ssl_certificate     /etc/letsencrypt/live/schwab-mcp.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/schwab-mcp.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }
}
```

---

## Option D: Static Bearer Token Mode (Private / Internal Automation)

If you prefer a static Bearer token instead of OAuth 2.1 (e.g., for an internal script or single-owner server where you manage Schwab auth via `schwab-mcp auth login` or `start_auth`):

```bash
export MCP_AUTH_MODE=bearer
export MCP_HTTP_AUTH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvx schwab-mcp serve --host 0.0.0.0 --port 8765
```

Clients attach `Authorization: Bearer <MCP_HTTP_AUTH_TOKEN>` on `/mcp`. The `/health` endpoint remains accessible for container/load-balancer readiness probes.
