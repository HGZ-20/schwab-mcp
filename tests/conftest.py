"""Shared pytest fixtures and helpers."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from schwab_mcp.auth.models import TokenData
from schwab_mcp.auth.storage import DEFAULT_SUBJECT, SchwabTokenStore
from schwab_mcp.config import Settings


@pytest.fixture
def base_settings(tmp_path: Path) -> Settings:
    """Isolated Settings instance using temporary storage paths."""
    return Settings(
        schwab_app_key="test_app_key",
        schwab_app_secret="test_app_secret",
        schwab_callback_url="https://127.0.0.1:8182",
        schwab_token_path=tmp_path / "token.json",
        schwab_audit_log_path=tmp_path / "trade_audit.jsonl",
        mcp_oauth_state_path=tmp_path / "oauth_state.json",
        schwab_dry_run=True,
        schwab_read_only=False,
        schwab_max_order_usd=5000.0,
    )


def seed_valid_token(
    settings: Settings,
    *,
    subject: str = DEFAULT_SUBJECT,
    access_token: str = "valid_access_token",
    refresh_token: str = "valid_refresh_token",
) -> TokenData:
    now = time.time()
    token = TokenData(
        access_token=access_token,
        refresh_token=refresh_token,
        obtained_at=now,
        access_expires_at=now + 1500,
        refresh_obtained_at=now,
        refresh_expires_at=now + 6 * 24 * 3600,
    )
    SchwabTokenStore(settings.schwab_token_path).save(token, subject=subject)
    return token


async def call_tool_json(mcp: Any, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await mcp.call_tool(name, args or {})
    if isinstance(result, tuple):
        content_blocks = result[0]
    elif hasattr(result, "content"):
        content_blocks = result.content
    else:
        content_blocks = result
    return json.loads(content_blocks[0].text)
