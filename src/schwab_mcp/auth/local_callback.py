"""Local loopback HTTPS callback listener and browser helper for Mode 2 (stdio) auth."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import html
import ipaddress
import os
import ssl
import sys
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from ..config import Settings

CallbackHandlerFn = Callable[[str], Awaitable[None]]


def can_open_browser() -> bool:
    """Heuristically detect whether a desktop browser can be opened in this environment."""
    if sys.platform in {"darwin", "win32"}:
        return True
    return bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY") or os.getenv("BROWSER"))


def try_open_browser(url: str) -> bool:
    """Attempt to open a URL in the user's default browser without blocking headless servers."""
    if not can_open_browser():
        return False
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def ensure_localhost_tls_cert(
    certs_dir: Path,
    *,
    custom_cert_path: Path | None = None,
    custom_key_path: Path | None = None,
) -> tuple[Path, Path, bool]:
    """Return (cert_path, key_path, is_custom_trusted).

    If the user configured SCHWAB_TLS_CERT_PATH & SCHWAB_TLS_KEY_PATH (e.g. via `mkcert`),
    uses those directly for zero browser warnings. Otherwise generates and caches a
    self-signed SAN certificate for 127.0.0.1 / localhost / ::1.
    """
    if custom_cert_path and custom_key_path:
        cert = custom_cert_path.expanduser()
        key = custom_key_path.expanduser()
        if cert.is_file() and key.is_file():
            return cert, key, True

    certs_dir = certs_dir.expanduser()
    certs_dir.mkdir(parents=True, exist_ok=True)
    cert_path = certs_dir / "localhost.crt"
    key_path = certs_dir / "localhost.key"

    if cert_path.is_file() and key_path.is_file():
        return cert_path, key_path, False

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.UTC)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "schwab-mcp local callback"),
        ]
    )
    san = x509.SubjectAlternativeName(
        [
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            x509.IPAddress(ipaddress.IPv6Address("::1")),
        ]
    )
    cert_obj = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )

    key_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_bytes = cert_obj.public_bytes(serialization.Encoding.PEM)

    key_path.write_bytes(key_bytes)
    with contextlib.suppress(OSError):
        os.chmod(key_path, 0o600)
    cert_path.write_bytes(cert_bytes)
    with contextlib.suppress(OSError):
        os.chmod(cert_path, 0o644)
    return cert_path, key_path, False


def _render_html_page(title: str, heading: str, body: str, *, success: bool) -> bytes:
    accent = "#10b981" if success else "#ef4444"
    icon = "&#10003;" if success else "&#10007;"
    doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f172a; color: #f8fafc; margin: 0; min-height: 100vh;
      display: flex; align-items: center; justify-content: center;
    }}
    .card {{
      background: #1e293b; border: 1px solid #334155; border-radius: 14px;
      padding: 2rem 2.5rem; max-width: 480px; text-align: center;
      box-shadow: 0 20px 40px rgba(0,0,0,0.35);
    }}
    .badge {{
      width: 56px; height: 56px; border-radius: 9999px; margin: 0 auto 1rem;
      display: flex; align-items: center; justify-content: center;
      background: {accent}; color: #fff; font-size: 28px; font-weight: 700;
    }}
    h1 {{ font-size: 1.35rem; margin: 0 0 0.75rem; }}
    p {{ color: #cbd5e1; line-height: 1.55; margin: 0; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">{icon}</div>
    <h1>{html.escape(heading)}</h1>
    <p>{html.escape(body)}</p>
  </div>
</body>
</html>"""
    return doc.encode("utf-8")


@dataclass
class CallbackCaptureStatus:
    """Observable status of an active or completed local callback capture session."""

    active: bool
    listening_url: str | None = None
    trusted_cert: bool = False
    completed: bool = False
    error: str | None = None


class LocalCallbackServer:
    """Temporary asyncio HTTPS loopback server that captures Schwab's OAuth callback automatically."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._server: asyncio.AbstractServer | None = None
        self._timeout_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._done_event = asyncio.Event()
        self._expected_state: str | None = None
        self._on_code: CallbackHandlerFn | None = None
        self._status = CallbackCaptureStatus(active=False)

    @property
    def status(self) -> CallbackCaptureStatus:
        return self._status

    async def start(
        self,
        *,
        expected_state: str | None,
        on_code: CallbackHandlerFn,
        timeout_sec: int = 300,
        callback_url: str | None = None,
    ) -> CallbackCaptureStatus:
        """Start listening on the configured loopback callback URL."""
        await self.stop()

        target = urlparse(callback_url or self.settings.effective_callback_url)
        host = target.hostname or "127.0.0.1"
        if host.lower() not in {"127.0.0.1", "localhost", "::1"}:
            self._status = CallbackCaptureStatus(
                active=False,
                error=f"Callback host {host!r} is not loopback; local auto-capture skipped.",
            )
            return self._status

        scheme = (target.scheme or "https").lower()
        port = target.port if target.port is not None else (443 if scheme == "https" else 80)

        ssl_ctx: ssl.SSLContext | None = None
        trusted = False
        if scheme == "https":
            cert_path, key_path, trusted = ensure_localhost_tls_cert(
                self.settings.certs_dir,
                custom_cert_path=self.settings.schwab_tls_cert_path,
                custom_key_path=self.settings.schwab_tls_key_path,
            )
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

        self._expected_state = expected_state
        self._on_code = on_code
        self._done_event = asyncio.Event()

        bind_host = "127.0.0.1" if host.lower() == "localhost" else host
        try:
            self._server = await asyncio.start_server(
                self._handle_connection,
                host=bind_host,
                port=port,
                ssl=ssl_ctx,
            )
        except OSError as exc:
            self._status = CallbackCaptureStatus(
                active=False,
                error=f"Could not bind local callback listener on {bind_host}:{port}: {exc}",
            )
            return self._status

        actual_port = self._server.sockets[0].getsockname()[1] if self._server.sockets else port
        self._status = CallbackCaptureStatus(
            active=True,
            listening_url=f"{scheme}://{host}:{actual_port}{target.path or '/'}",
            trusted_cert=trusted,
            completed=False,
            error=None,
        )
        self._timeout_task = asyncio.create_task(self._auto_expire(timeout_sec))
        return self._status

    async def _auto_expire(self, timeout_sec: int) -> None:
        try:
            await asyncio.sleep(timeout_sec)
            if self._server is not None and not self._status.completed:
                self._status.error = "Local callback listener timed out."
                await self.stop()
        except asyncio.CancelledError:
            pass

    async def wait_until_complete(self, timeout_sec: float) -> CallbackCaptureStatus:
        """Wait up to `timeout_sec` for the callback to complete."""
        if self._status.completed or not self._status.active:
            return self._status
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._done_event.wait(), timeout=timeout_sec)
        return self._status

    async def stop(self) -> None:
        """Stop the local callback listener if running."""
        if self._timeout_task is not None:
            current = asyncio.current_task()
            if self._timeout_task is not current:
                self._timeout_task.cancel()
            self._timeout_task = None
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        self._status.active = False

    async def _write_http_response(
        self,
        writer: asyncio.StreamWriter,
        status_line: str,
        body: bytes,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        headers = (
            f"HTTP/1.1 {status_line}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(headers + body)
        await writer.drain()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        should_stop = False
        try:
            raw_request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
        except Exception:
            writer.close()
            return

        try:
            first_line = raw_request.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
            parts = first_line.split(" ")
            request_target = parts[1] if len(parts) >= 2 else "/"
            parsed = urlparse(request_target)

            if parsed.path == "/favicon.ico":
                await self._write_http_response(writer, "204 No Content", b"", "text/plain")
                return

            params = parse_qs(parsed.query)
            err = (params.get("error") or [""])[0].strip()
            if err:
                desc = (params.get("error_description") or [""])[0].strip()
                msg = f"Schwab denied authorization ({err}): {desc}".strip()
                self._status.error = msg
                body = _render_html_page(
                    "Schwab Auth Failed",
                    "Authorization Failed",
                    msg,
                    success=False,
                )
                await self._write_http_response(writer, "400 Bad Request", body)
                should_stop = True
                return

            code = (params.get("code") or [""])[0].strip()
            returned_state = (params.get("state") or [""])[0].strip() or None
            if not code:
                body = _render_html_page(
                    "Schwab Callback",
                    "Waiting for Authorization",
                    "No authorization code was present in this request.",
                    success=False,
                )
                await self._write_http_response(writer, "400 Bad Request", body)
                return

            if self._expected_state and returned_state and returned_state != self._expected_state:
                self._status.error = "OAuth state mismatch."
                body = _render_html_page(
                    "Schwab Auth Failed",
                    "Security Check Failed",
                    "OAuth state parameter did not match. Please restart authentication.",
                    success=False,
                )
                await self._write_http_response(writer, "400 Bad Request", body)
                should_stop = True
                return

            if self._on_code is not None:
                await self._on_code(code)

            self._status.completed = True
            self._status.error = None
            body = _render_html_page(
                "Charles Schwab Connected",
                "Charles Schwab Connected!",
                "Authentication succeeded and your token has been saved securely (0600). "
                "You can close this browser tab and return to your AI agent.",
                success=True,
            )
            await self._write_http_response(writer, "200 OK", body)
            should_stop = True
        except Exception as exc:
            self._status.error = str(exc)
            body = _render_html_page(
                "Schwab Auth Error",
                "Token Exchange Failed",
                str(exc),
                success=False,
            )
            with contextlib.suppress(Exception):
                await self._write_http_response(writer, "500 Internal Server Error", body)
            should_stop = True
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            if should_stop:
                self._done_event.set()
                self._stop_task = asyncio.create_task(self.stop())
