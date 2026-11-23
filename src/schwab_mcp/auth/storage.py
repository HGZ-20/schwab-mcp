"""Atomic 0600 file-backed storage for Schwab tokens and MCP OAuth 2.1 state."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..exceptions import SchwabAuthError
from .models import OAuthProviderState, TokenData

DEFAULT_SUBJECT = "default"


class TokenStoreDocument(BaseModel):
    """Canonical on-disk schema for per-subject Charles Schwab OAuth tokens."""

    version: int = 1
    subjects: dict[str, TokenData] = Field(default_factory=dict)


def atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    """Atomically write JSON to disk with restrictive file permissions."""
    resolved = path.expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{resolved.name}.", dir=str(resolved.parent))
    try:
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp_name, resolved)
        with contextlib.suppress(OSError):
            os.chmod(resolved, mode)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


class SchwabTokenStore:
    """Stores Charles Schwab OAuth 2.0 tokens keyed by isolated subject IDs."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()

    def exists(self) -> bool:
        return self.path.exists()

    def _read_doc(self) -> TokenStoreDocument:
        if not self.path.exists():
            return TokenStoreDocument()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return TokenStoreDocument.model_validate(raw)
        except Exception as exc:
            raise SchwabAuthError(f"Failed to read token store {self.path}: {exc}") from exc

    def _write_doc(self, doc: TokenStoreDocument) -> None:
        if not doc.subjects:
            if self.path.exists():
                self.path.unlink()
            return
        atomic_write_json(self.path, doc.model_dump(mode="json"))

    def load(self, subject: str = DEFAULT_SUBJECT) -> TokenData | None:
        return self._read_doc().subjects.get(subject)

    def save(self, token: TokenData, subject: str = DEFAULT_SUBJECT) -> None:
        doc = self._read_doc()
        doc.subjects[subject] = token
        self._write_doc(doc)

    def delete(self, subject: str = DEFAULT_SUBJECT) -> bool:
        doc = self._read_doc()
        if subject not in doc.subjects:
            return False
        del doc.subjects[subject]
        self._write_doc(doc)
        return True

    def list_subjects(self) -> list[str]:
        return sorted(self._read_doc().subjects.keys())


class OAuthStateStore:
    """Persists MCP OAuth 2.1 clients, pending flows, auth codes, and issued tokens."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()

    def load(self) -> OAuthProviderState:
        if not self.path.exists():
            return OAuthProviderState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return OAuthProviderState.model_validate(raw)
        except Exception as exc:
            raise SchwabAuthError(
                f"Failed to read MCP OAuth state file {self.path}: {exc}",
                auth_required=False,
            ) from exc

    def save(self, state: OAuthProviderState) -> None:
        state.prune_expired()
        atomic_write_json(self.path, state.model_dump(mode="json"))
