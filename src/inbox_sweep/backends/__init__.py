"""Backend factory."""

from __future__ import annotations

import getpass
import os

from inbox_sweep.backends.base import Backend, BackendError
from inbox_sweep.config import Config


def get_backend(cfg: Config, kind: str | None = None, interactive: bool = True) -> Backend:
    kind = (kind or cfg.backend).lower()
    if kind == "demo":
        from inbox_sweep.backends.demo import DemoBackend
        return DemoBackend(cfg.data_path / "demo-mailbox.json", me=(cfg.me or ["you@example.com"])[0])
    if kind == "gmail":
        try:
            from inbox_sweep.backends.gmail_api import GmailBackend
        except ImportError as exc:
            raise BackendError(
                "The Gmail backend needs extra packages: pip install 'inbox-sweep[gmail]'"
            ) from exc
        return GmailBackend(cfg.credentials, cfg.token, interactive=interactive)
    if kind == "imap":
        from inbox_sweep.backends.imap import ImapBackend
        if not cfg.imap_user:
            raise BackendError("Set [imap] user in sweep.toml")
        password = os.environ.get("INBOX_SWEEP_IMAP_PASSWORD")
        if not password:
            if not interactive:
                raise BackendError("Set INBOX_SWEEP_IMAP_PASSWORD")
            password = getpass.getpass(f"IMAP app password for {cfg.imap_user}: ")
        return ImapBackend(cfg.imap_host, cfg.imap_port, cfg.imap_user, password)
    raise BackendError(f"Unknown backend '{kind}'. Use gmail, imap or demo.")


__all__ = ["Backend", "BackendError", "get_backend"]
