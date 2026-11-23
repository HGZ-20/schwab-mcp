"""Integration tests for Mode 2 local loopback HTTPS callback auto-capture."""

from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx

from schwab_mcp.auth.local_callback import LocalCallbackServer, ensure_localhost_tls_cert
from schwab_mcp.config import Settings


async def test_local_https_callback_captures_code_and_stops(base_settings: Settings) -> None:
    cert, key, trusted = ensure_localhost_tls_cert(base_settings.certs_dir)
    assert cert.is_file()
    assert key.is_file()
    assert trusted is False
    assert (os.stat(key).st_mode & 0o777) == 0o600

    received_codes: list[str] = []

    async def _on_code(code: str) -> None:
        received_codes.append(code)

    listener = LocalCallbackServer(base_settings)
    # Port 0 asks the OS for an ephemeral free port so tests never collide
    status = await listener.start(
        expected_state="state_xyz",
        on_code=_on_code,
        timeout_sec=30,
        callback_url="https://127.0.0.1:0",
    )
    assert status.active is True
    assert status.listening_url is not None

    port = urlparse(status.listening_url).port
    assert port and port > 0

    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.get(f"https://127.0.0.1:{port}/?code=C0.test_code%40&state=state_xyz")
        assert resp.status_code == 200
        assert "Charles Schwab Connected" in resp.text

    final = await listener.wait_until_complete(5.0)
    assert final.completed is True
    assert received_codes == ["C0.test_code@"]
    await listener.stop()


async def test_local_https_callback_rejects_state_mismatch(base_settings: Settings) -> None:
    received: list[str] = []

    async def _on_code(code: str) -> None:
        received.append(code)

    listener = LocalCallbackServer(base_settings)
    status = await listener.start(
        expected_state="expected_state",
        on_code=_on_code,
        timeout_sec=30,
        callback_url="https://127.0.0.1:0",
    )
    port = urlparse(status.listening_url or "").port

    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.get(f"https://127.0.0.1:{port}/?code=C0.evil&state=wrong_state")
        assert resp.status_code == 400
        assert "Security Check Failed" in resp.text

    assert received == []
    await listener.stop()
