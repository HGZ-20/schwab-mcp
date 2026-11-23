"""End-to-end tests for Mode 1 Remote MCP OAuth 2.1 (Cloudflare-style Connect flow)."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import respx
from mcp.types import LATEST_PROTOCOL_VERSION, PROTOCOL_VERSION_META_KEY
from starlette.testclient import TestClient

from schwab_mcp.auth.schwab_oauth import SCHWAB_TOKEN_URL
from schwab_mcp.config import Settings
from schwab_mcp.schwab.client import MARKETDATA_BASE_URL, TRADER_BASE_URL, SchwabClient
from schwab_mcp.server.app import create_server
from schwab_mcp.server.http import build_http_app

MCP_2026_META = {
    PROTOCOL_VERSION_META_KEY: LATEST_PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _make_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _remote_settings(tmp_path: Path) -> Settings:
    return Settings(
        schwab_app_key="remote_app_key",
        schwab_app_secret="remote_app_secret",
        schwab_callback_url="",
        mcp_public_url="https://schwab-mcp.example.com",
        mcp_auth_mode="oauth",
        schwab_token_path=tmp_path / "token.json",
        schwab_audit_log_path=tmp_path / "audit.jsonl",
        mcp_oauth_state_path=tmp_path / "oauth_state.json",
    )


@respx.mock
def test_full_mcp_oauth_2_1_cloudflare_style_flow(tmp_path: Path) -> None:
    """Exercise the full MCP 2026-07-28 OAuth 2.1 lifecycle end-to-end:
    401 + WWW-Authenticate -> Protected Resource Metadata -> Auth Server Metadata
    -> Dynamic Client Registration -> /authorize (PKCE) -> Schwab redirect
    -> /schwab/callback -> Client redirect -> /token -> stateless server/discover & tools/call
    -> /token refresh rotation -> /revoke -> rejected 401.
    """
    settings = _remote_settings(tmp_path)

    # Mock upstream Schwab token exchange + quote endpoint
    respx.post(SCHWAB_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "schwab_live_access_1",
                "refresh_token": "schwab_live_refresh_1",
                "token_type": "Bearer",
                "expires_in": 1800,
                "scope": "api",
            },
        )
    )
    respx.get(f"{MARKETDATA_BASE_URL}/quotes").mock(
        return_value=httpx.Response(
            200,
            json={
                "NVDA": {
                    "symbol": "NVDA",
                    "assetMainType": "EQUITY",
                    "quote": {"lastPrice": 142.50, "mark": 142.55},
                }
            },
        )
    )

    mcp = create_server(settings=settings, enable_oauth=True)
    app = build_http_app(mcp, settings=settings, host="0.0.0.0")

    with TestClient(app, base_url="https://schwab-mcp.example.com") as tc:
        # 0. Health check is publicly accessible and reports MCP 2026-07-28
        r_health = tc.get("/health")
        assert r_health.status_code == 200
        assert r_health.json()["auth_mode"] == "oauth"
        assert r_health.json()["mcp_protocol_version"] == "2026-07-28"

        # 1. Unauthenticated /mcp returns 401 with RFC 9728 resource_metadata pointer
        r_unauth = tc.post("/mcp", json={})
        assert r_unauth.status_code == 401
        www_auth = r_unauth.headers.get("www-authenticate", "")
        assert "resource_metadata=" in www_auth
        assert ".well-known/oauth-protected-resource" in www_auth

        # 2. Discover Protected Resource Metadata & Authorization Server Metadata
        r_prm = tc.get("/.well-known/oauth-protected-resource/mcp")
        assert r_prm.status_code == 200
        prm = r_prm.json()
        assert prm["resource"] == "https://schwab-mcp.example.com/mcp"
        assert any(
            s.rstrip("/") == "https://schwab-mcp.example.com" for s in prm["authorization_servers"]
        )

        r_asm = tc.get("/.well-known/oauth-authorization-server")
        assert r_asm.status_code == 200
        asm = r_asm.json()
        assert asm["authorization_endpoint"] == "https://schwab-mcp.example.com/authorize"
        assert asm["token_endpoint"] == "https://schwab-mcp.example.com/token"
        assert asm["registration_endpoint"] == "https://schwab-mcp.example.com/register"
        assert asm["revocation_endpoint"] == "https://schwab-mcp.example.com/revoke"

        # 3. Dynamic Client Registration (public PKCE client, just like Cursor / Codex)
        r_reg = tc.post(
            "/register",
            json={
                "client_name": "Cursor Test Client",
                "redirect_uris": ["http://127.0.0.1:54321/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert r_reg.status_code == 201
        client_id = r_reg.json()["client_id"]

        # 4. Client starts /authorize with PKCE S256 & RFC 8707 resource indicator
        verifier, challenge = _make_pkce()
        r_auth = tc.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "http://127.0.0.1:54321/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "cursor_state_99",
                "resource": "https://schwab-mcp.example.com/mcp",
            },
            follow_redirects=False,
        )
        assert r_auth.status_code == 302
        schwab_redirect = urlparse(r_auth.headers["location"])
        assert schwab_redirect.netloc == "api.schwabapi.com"
        schwab_qs = parse_qs(schwab_redirect.query)
        assert schwab_qs["client_id"] == ["remote_app_key"]
        assert schwab_qs["redirect_uri"] == ["https://schwab-mcp.example.com/schwab/callback"]
        upstream_state = schwab_qs["state"][0]

        # 5. User approves at Schwab -> Schwab redirects browser to /schwab/callback
        r_cb = tc.get(
            "/schwab/callback",
            params={"code": "C0.schwab_auth_code@", "state": upstream_state},
            follow_redirects=False,
        )
        assert r_cb.status_code == 302
        client_redirect = urlparse(r_cb.headers["location"])
        assert client_redirect.scheme == "http"
        assert client_redirect.netloc == "127.0.0.1:54321"
        client_qs = parse_qs(client_redirect.query)
        assert client_qs["state"] == ["cursor_state_99"]
        mcp_code = client_qs["code"][0]

        # 6. Client exchanges MCP code + PKCE verifier at /token
        r_tok = tc.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": mcp_code,
                "redirect_uri": "http://127.0.0.1:54321/callback",
                "client_id": client_id,
                "code_verifier": verifier,
                "resource": "https://schwab-mcp.example.com/mcp",
            },
        )
        assert r_tok.status_code == 200
        tok_body = r_tok.json()
        access_token = tok_body["access_token"]
        refresh_token = tok_body["refresh_token"]
        assert tok_body["token_type"].lower() == "bearer"

        # 7. Authenticated MCP 2026-07-28 stateless `server/discover` + `tools/call` (`get_quotes`)
        r_disc = tc.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
                "MCP-Method": "server/discover",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": MCP_2026_META},
            },
        )
        assert r_disc.status_code == 200
        assert "schwab-mcp" in r_disc.text

        r_call = tc.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
                "MCP-Method": "tools/call",
                "MCP-Name": "get_quotes",
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "_meta": MCP_2026_META,
                    "name": "get_quotes",
                    "arguments": {"symbols": ["NVDA"]},
                },
            },
        )
        assert r_call.status_code == 200
        assert "142.5" in r_call.text

        # 8. Refresh token rotation
        r_ref = tc.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
        )
        assert r_ref.status_code == 200
        new_access = r_ref.json()["access_token"]
        assert new_access != access_token

        # Old access token was revoked on rotation
        r_old = tc.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        )
        assert r_old.status_code == 401

        # 9. Revoke new token -> subsequent /mcp fails with 401
        r_rev = tc.post(
            "/revoke",
            data={"token": new_access, "client_id": client_id, "client_secret": ""},
        )
        assert r_rev.status_code == 200

        r_after_rev = tc.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {new_access}",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
        )
        assert r_after_rev.status_code == 401


@respx.mock
def test_remote_oauth_multi_user_subject_isolation(tmp_path: Path) -> None:
    """Two users authenticating to the same hosted server receive isolated Schwab tokens & accounts."""
    settings = _remote_settings(tmp_path)

    # Distinct id_tokens so User A and User B get deterministic distinct subjects
    def _fake_jwt(sub: str) -> str:
        body = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
        return f"eyJhbGciOiJSUzI1NiJ9.{body}.sig"

    respx.post(SCHWAB_TOKEN_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "access_token": "schwab_tok_user_A",
                    "refresh_token": "schwab_ref_user_A",
                    "id_token": _fake_jwt("alice"),
                    "expires_in": 1800,
                },
            ),
            httpx.Response(
                200,
                json={
                    "access_token": "schwab_tok_user_B",
                    "refresh_token": "schwab_ref_user_B",
                    "id_token": _fake_jwt("bob"),
                    "expires_in": 1800,
                },
            ),
        ]
    )

    def _account_numbers_handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        if "schwab_tok_user_A" in auth:
            return httpx.Response(200, json=[{"accountNumber": "11111111", "hashValue": "HASH_A"}])
        if "schwab_tok_user_B" in auth:
            return httpx.Response(200, json=[{"accountNumber": "22222222", "hashValue": "HASH_B"}])
        return httpx.Response(401)

    respx.get(f"{TRADER_BASE_URL}/accounts/accountNumbers").mock(
        side_effect=_account_numbers_handler
    )
    respx.get(f"{TRADER_BASE_URL}/accounts/HASH_A").mock(
        return_value=httpx.Response(
            200, json={"securitiesAccount": {"accountNumber": "11111111", "positions": []}}
        )
    )
    respx.get(f"{TRADER_BASE_URL}/accounts/HASH_B").mock(
        return_value=httpx.Response(
            200, json={"securitiesAccount": {"accountNumber": "22222222", "positions": []}}
        )
    )

    schwab = SchwabClient(settings)
    mcp = create_server(settings=settings, client=schwab, enable_oauth=True)
    app = build_http_app(mcp, settings=settings, host="0.0.0.0")

    def _complete_oauth(tc: TestClient, code_name: str) -> str:
        r_reg = tc.post(
            "/register",
            json={
                "client_name": f"Client-{code_name}",
                "redirect_uris": ["http://127.0.0.1:44444/cb"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        cid = r_reg.json()["client_id"]
        verifier, challenge = _make_pkce()
        r_auth = tc.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": "http://127.0.0.1:44444/cb",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": f"st_{code_name}",
                "resource": "https://schwab-mcp.example.com/mcp",
            },
            follow_redirects=False,
        )
        up_state = parse_qs(urlparse(r_auth.headers["location"]).query)["state"][0]
        r_cb = tc.get(
            "/schwab/callback",
            params={"code": code_name, "state": up_state},
            follow_redirects=False,
        )
        mcp_code = parse_qs(urlparse(r_cb.headers["location"]).query)["code"][0]
        r_tok = tc.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": mcp_code,
                "redirect_uri": "http://127.0.0.1:44444/cb",
                "client_id": cid,
                "code_verifier": verifier,
                "resource": "https://schwab-mcp.example.com/mcp",
            },
        )
        assert r_tok.status_code == 200
        return r_tok.json()["access_token"]

    def _get_positions_with_token(tc: TestClient, bearer: str) -> str:
        r_pos = tc.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {bearer}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
                "MCP-Method": "tools/call",
                "MCP-Name": "get_positions",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "_meta": MCP_2026_META,
                    "name": "get_positions",
                    "arguments": {},
                },
            },
        )
        assert r_pos.status_code == 200
        return r_pos.text

    with TestClient(app, base_url="https://schwab-mcp.example.com") as tc:
        tok_a = _complete_oauth(tc, "code_alice")
        tok_b = _complete_oauth(tc, "code_bob")

        res_a = _get_positions_with_token(tc, tok_a)
        res_b = _get_positions_with_token(tc, tok_b)
        assert "11111111" in res_a and "22222222" not in res_a
        assert "22222222" in res_b and "11111111" not in res_b
