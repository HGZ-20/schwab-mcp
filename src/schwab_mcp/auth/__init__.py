"""Authentication & OAuth 2.0 / OAuth 2.1 management for schwab-mcp."""

from .local_callback import (
    CallbackCaptureStatus,
    LocalCallbackServer,
    can_open_browser,
    ensure_localhost_tls_cert,
    try_open_browser,
)
from .mcp_oauth import DEFAULT_MCP_SCOPE, SchwabOAuthProvider
from .models import (
    ACCESS_REFRESH_BUFFER_SEC,
    DEFAULT_ACCESS_LIFETIME_SEC,
    REFRESH_TOKEN_LIFETIME_SEC,
    OAuthProviderState,
    TokenData,
)
from .schwab_oauth import (
    SCHWAB_AUTHORIZE_URL,
    SCHWAB_TOKEN_URL,
    SchwabAuthManager,
    derive_subject_from_token,
)
from .storage import DEFAULT_SUBJECT, OAuthStateStore, SchwabTokenStore, TokenStoreDocument

__all__ = [
    "ACCESS_REFRESH_BUFFER_SEC",
    "DEFAULT_ACCESS_LIFETIME_SEC",
    "DEFAULT_MCP_SCOPE",
    "DEFAULT_SUBJECT",
    "REFRESH_TOKEN_LIFETIME_SEC",
    "SCHWAB_AUTHORIZE_URL",
    "SCHWAB_TOKEN_URL",
    "CallbackCaptureStatus",
    "LocalCallbackServer",
    "OAuthProviderState",
    "OAuthStateStore",
    "SchwabAuthManager",
    "SchwabOAuthProvider",
    "SchwabTokenStore",
    "TokenData",
    "TokenStoreDocument",
    "can_open_browser",
    "derive_subject_from_token",
    "ensure_localhost_tls_cert",
    "try_open_browser",
]
