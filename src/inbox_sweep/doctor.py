"""`inbox-sweep doctor`: check everything a run needs and say exactly how to fix it."""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from inbox_sweep.config import Config, ConfigError, load_config

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    status: str
    name: str
    detail: str
    fix: str = ""


def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def run_checks(config_path: str | None, online: bool = True) -> list[Check]:
    checks: list[Check] = []
    add = checks.append
    v = sys.version_info
    add(Check(OK if v >= (3, 10) else FAIL, "Python", f"{v.major}.{v.minor}.{v.micro}",
              "Install Python 3.10 or newer (e.g. `uv python install 3.12`)."))

    path = config_path or ("sweep.toml" if Path("sweep.toml").exists() else None)
    if path is None:
        add(Check(FAIL, "Config", "no sweep.toml here", "Run `inbox-sweep init`."))
        return checks
    try:
        cfg = load_config(path)
    except (ConfigError, ValueError) as exc:
        add(Check(FAIL, "Config", str(exc), "Fix the line it names, or re-run `inbox-sweep init --force`."))
        return checks
    add(Check(OK, "Config", f"{path} (backend: {cfg.backend})"))
    if os.name == "posix" and Path(path).stat().st_mode & 0o077:
        add(Check(WARN, "Config permissions", "readable by other users", f"chmod 600 {path}"))

    if not cfg.me or any(m.endswith("@example.com") for m in cfg.me):
        add(Check(FAIL, "Your addresses", "[account] me is not set",
                  "List every address you send from: replies are the strongest protection."))
    else:
        add(Check(OK, "Your addresses", ", ".join(cfg.me)))

    _backend_checks(cfg, add, online)
    _data_checks(cfg, add)
    _ai_checks(cfg, add, online)
    add(Check(OK if cfg.private else WARN, "Private mode",
              "on: output shows counts only" if cfg.private else "off: output names senders and subjects",
              "" if cfg.private else "Set [privacy] private = true if an AI agent runs these commands."))
    return checks


def _backend_checks(cfg: Config, add, online: bool) -> None:
    if cfg.backend == "gmail":
        missing = [m for m in ("googleapiclient", "google_auth_oauthlib") if not _has(m)]
        if missing:
            add(Check(FAIL, "Gmail packages", "missing " + ", ".join(missing), "pip install 'inbox-sweep[gmail]'"))
            return
        creds = Path(cfg.credentials).expanduser()
        add(Check(OK if creds.exists() else FAIL, "Gmail OAuth client", str(creds),
                  "Create one: docs/gmail-api-setup.md"))
        token = Path(cfg.token).expanduser()
        if not token.exists():
            add(Check(WARN, "Gmail sign-in", "not signed in yet", "Run `inbox-sweep auth`."))
        elif online:
            _try_account(cfg, add, "Gmail sign-in")
    elif cfg.backend == "outlook":
        if not _has("msal"):
            add(Check(FAIL, "Outlook packages", "missing msal", "pip install 'inbox-sweep[outlook]'"))
            return
        add(Check(OK if cfg.outlook_client_id else FAIL, "Outlook app registration",
                  cfg.outlook_client_id or "[outlook] client_id is empty", "See docs/outlook-setup.md"))
        token = Path(cfg.outlook_token).expanduser()
        if not token.exists():
            add(Check(WARN, "Outlook sign-in", "not signed in yet", "Run `inbox-sweep auth`."))
        elif online and cfg.outlook_client_id:
            _try_account(cfg, add, "Outlook sign-in")
    elif cfg.backend == "imap":
        if not cfg.imap_user:
            add(Check(FAIL, "IMAP user", "[imap] user is empty", "Set it to your full email address."))
        if online:
            try:
                with socket.create_connection((cfg.imap_host, cfg.imap_port), timeout=5):
                    pass
                add(Check(OK, "IMAP server", f"{cfg.imap_host}:{cfg.imap_port} reachable ({cfg.imap_tls})"))
            except OSError as exc:
                add(Check(FAIL, "IMAP server", f"{cfg.imap_host}:{cfg.imap_port}: {exc}",
                          "Check [imap] host/port, your network, or that your local bridge is running."))
        if not os.environ.get("INBOX_SWEEP_IMAP_PASSWORD"):
            add(Check(WARN, "IMAP password", "INBOX_SWEEP_IMAP_PASSWORD not set (you'll be prompted)",
                      "Use an app password: docs/imap-setup.md"))
    elif cfg.backend == "demo":
        add(Check(OK, "Backend", "demo mailbox (no account needed)"))


def _try_account(cfg: Config, add, name: str) -> None:
    from inbox_sweep.backends import BackendError, get_backend
    try:
        account = get_backend(cfg, interactive=False).account()
        add(Check(OK, name, f"signed in as {account}"))
    except (BackendError, Exception) as exc:  # network, expired token, revoked consent
        add(Check(FAIL, name, str(exc)[:160], "Run `inbox-sweep auth` again."))


def _data_checks(cfg: Config, add) -> None:
    data = cfg.data_path
    try:
        data.mkdir(parents=True, exist_ok=True)
        probe = data / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add(Check(OK, "Data folder", str(data.resolve())))
    except OSError as exc:
        add(Check(FAIL, "Data folder", f"{data}: {exc}", "Set [paths] data_dir to a writable folder."))
        return
    if not cfg.db_path.exists():
        add(Check(WARN, "Index", "not built yet", "Run `inbox-sweep index`."))
        return
    from inbox_sweep.store import Store, StoreError
    try:
        with Store(cfg.db_path) as store:
            count = store.message_count()
            indexed_at = store.get_meta("indexed_at")
    except StoreError as exc:
        add(Check(FAIL, "Index", str(exc), "Upgrade inbox-sweep, or `inbox-sweep index --fresh`."))
        return
    if indexed_at:
        days = (time.time() - indexed_at) / 86400
        stale = cfg.max_index_age_days and days > cfg.max_index_age_days
        add(Check(WARN if stale else OK, "Index", f"{count:,} messages, updated {days:.1f} days ago",
                  "Run `inbox-sweep index` (incremental) before planning." if stale else ""))
    else:
        add(Check(OK, "Index", f"{count:,} messages"))


def _ai_checks(cfg: Config, add, online: bool) -> None:
    if not cfg.ai_enabled:
        add(Check(OK, "AI step", "off (rules only)"))
        return
    if cfg.ai_provider == "anthropic":
        ok = _has("anthropic")
        add(Check(WARN if ok else FAIL, "AI step", "provider anthropic: subject lines leave this machine"
                  if ok else "anthropic package missing",
                  "Use provider = 'local' to keep data on this machine." if ok else "pip install 'inbox-sweep[ai]'"))
        return
    if cfg.ai_provider == "openai-compatible":
        add(Check(WARN, "AI step", f"data goes to {cfg.ai_base_url}", "Use provider = 'local' to keep data here."))
        return
    if online:
        try:
            with urllib.request.urlopen(cfg.ai_base_url.rstrip("/") + "/models", timeout=3):
                pass
            add(Check(OK, "AI step", f"local model server at {cfg.ai_base_url}"))
        except OSError as exc:
            add(Check(FAIL, "AI step", f"no local model server at {cfg.ai_base_url} ({exc})",
                      "Start it (e.g. `ollama serve` and `ollama pull llama3.1:8b`), or set [ai] enabled = false."))


def _can_print(text: str) -> bool:
    try:
        text.encode(getattr(sys.stdout, "encoding", None) or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def render(checks: list[Check]) -> str:
    fancy = _can_print("✓✗→")
    marks = {OK: "✓", WARN: "!", FAIL: "✗"} if fancy else {OK: "ok", WARN: "!!", FAIL: "XX"}
    arrow = "→" if fancy else "->"
    lines = []
    for c in checks:
        lines.append(f" {marks[c.status]} {c.name}: {c.detail}")
        if c.fix and c.status != OK:
            lines.append(f"     {arrow} {c.fix}")
    failed = sum(c.status == FAIL for c in checks)
    lines.append("")
    lines.append("All good." if not failed else f"{failed} problem(s) to fix.")
    return "\n".join(lines)
